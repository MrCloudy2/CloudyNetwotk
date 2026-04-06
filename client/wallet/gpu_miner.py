"""
=============================================================================
  gpu_miner.py  —  Dual-backend GPU mining for Chain Wallet
  Import this module; do not run it directly.
=============================================================================

Backend selection
-----------------
On startup GpuMiner tries to initialise both backends:
  1. CUDA   (pycuda + nvrtc)  — NVIDIA only, best performance on Ampere/Blackwell
  2. OpenCL (pyopencl)        — works on NVIDIA, AMD, Intel

Each available backend is calibrated independently (workgroup/block size ×
batch size).  The one with the higher measured hashrate is used for mining.
The result is logged so you always know which backend won and why.

If only one backend is available it is used automatically.
If neither is available the miner exits with a clear error message.

CUDA kernel extras vs OpenCL
-----------------------------
- __funnelshift_r  for rotate  (single PTX instruction, ~15 % faster than shifts)
- lop3.b32         for CH/MAJ  (3-input boolean in one instruction, TETSUO VARIANT 1)
- __shared__       for midstate/tail/target  (same as OpenCL __local)
- No byte-swap on output — CloudyCoin target is big-endian, we match it directly

Mining protocol
---------------
1. GET /get_mining_job?miner_address=<hex>
   Response: 136 bytes  [16B job_id][4B block_index][112B header][4B tx_count]
2. SHA256d(header with nonce at bytes [104:112] big-endian uint64) <= target
3. POST /submit_block  [16B job_id][112B solved header]  →  201 [4B idx][32B hash]
"""

import hashlib
import struct
import threading
import time

import numpy as np
import requests

# ── Optional backends ────────────────────────────────────────────────────────
try:
    import pycuda.driver as cuda
    cuda.init()
    # Quick sanity check — if no CUDA device is accessible, treat as unavailable
    # rather than crashing later inside _CudaBackend.setup()
    if cuda.Device.count() == 0:
        raise RuntimeError("No CUDA devices found")
    CUDA_AVAILABLE = True
except Exception:
    cuda = None
    CUDA_AVAILABLE = False

try:
    import pyopencl as cl
    OPENCL_AVAILABLE = True
except Exception:
    cl = None
    OPENCL_AVAILABLE = False




# =============================================================================
#  NVRTC — runtime CUDA compilation without nvcc
#
#  libnvrtc.so ships with the NVIDIA driver package on Debian/Ubuntu.
#  It is NOT part of the CUDA toolkit, so it works even without nvcc.
#  We call it via ctypes so there is no extra Python dependency.
#
#  If libnvrtc is not found we fall back to pycuda.compiler (needs nvcc).
# =============================================================================

import ctypes, ctypes.util

def _nvrtc_compile(source: str, sm_ver: str) -> bytes:
    """
    Compile CUDA C source to PTX using NVRTC (no nvcc required).
    sm_ver example: "sm_86" for Ampere RTX 3060.
    Returns PTX bytes on success, raises RuntimeError on failure.
    """
    # Locate libnvrtc — name varies by platform and CUDA version
    lib_names = ["libnvrtc.so", "libnvrtc.so.12", "libnvrtc.so.11",
                 "nvrtc64_120_0.dll", "nvrtc64_110_0.dll"]
    lib = None
    for name in lib_names:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            pass
    if lib is None:
        found = ctypes.util.find_library("nvrtc")
        if found:
            lib = ctypes.CDLL(found)
    if lib is None:
        raise RuntimeError("libnvrtc not found — install NVIDIA driver or CUDA toolkit")

    # nvrtcCreateProgram
    prog_p = ctypes.c_void_p(0)
    src_b  = source.encode()
    ret = lib.nvrtcCreateProgram(
        ctypes.byref(prog_p),
        src_b, b"mine.cu",
        ctypes.c_int(0), None, None,
    )
    if ret != 0:
        raise RuntimeError(f"nvrtcCreateProgram failed: {ret}")

    prog = prog_p

    # nvrtcCompileProgram
    arch_opt = f"--gpu-architecture={sm_ver}".encode()
    opts = (ctypes.c_char_p * 2)(
        b"--use_fast_math",
        arch_opt,
    )
    ret = lib.nvrtcCompileProgram(prog, ctypes.c_int(2), opts)
    if ret != 0:
        # Retrieve log
        log_size = ctypes.c_size_t(0)
        lib.nvrtcGetProgramLogSize(prog, ctypes.byref(log_size))
        log_buf = ctypes.create_string_buffer(log_size.value)
        lib.nvrtcGetProgramLog(prog, log_buf)
        lib.nvrtcDestroyProgram(ctypes.byref(prog))
        raise RuntimeError(f"NVRTC compile error:\n{log_buf.value.decode(errors='replace')}")

    # Get PTX
    ptx_size = ctypes.c_size_t(0)
    lib.nvrtcGetPTXSize(prog, ctypes.byref(ptx_size))
    ptx_buf = ctypes.create_string_buffer(ptx_size.value)
    lib.nvrtcGetPTX(prog, ptx_buf)
    lib.nvrtcDestroyProgram(ctypes.byref(prog))
    return ptx_buf.raw


import wire


# =============================================================================
#  Rate limiter (shared by both backends)
# =============================================================================

class _RateLimiter:
    def __init__(self, tokens_per_min, burst, min_gaps=None):
        self._rate        = tokens_per_min / 60.0
        self._burst       = float(burst)
        self._tokens      = float(burst)
        self._last_refill = time.monotonic()
        self._min_gaps    = min_gaps or {}
        self._last_call   = {}
        self._lock        = threading.Lock()

    def _refill(self, now):
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, path):
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                wait_gap = 0.0
                for prefix, gap in self._min_gaps.items():
                    if path.startswith(prefix):
                        last = self._last_call.get(prefix, 0.0)
                        wait_gap = max(wait_gap, (last + gap) - now)
                if wait_gap > 0:
                    pass
                elif self._tokens >= 1.0:
                    self._tokens -= 1.0
                    for prefix in self._min_gaps:
                        if path.startswith(prefix):
                            self._last_call[prefix] = now
                    return
                else:
                    wait_gap = (1.0 - self._tokens) / self._rate
            time.sleep(max(0.01, wait_gap))


_limiter = _RateLimiter(
    tokens_per_min=28,
    burst=6,
    min_gaps={"/get_mining_job": 6.0, "/blockchain": 10.0},
)


def _api_get(url, **kwargs):
    _limiter.acquire("/" + url.split("/", 3)[-1].split("?")[0])
    return requests.get(url, **kwargs)


def _api_post(url, **kwargs):
    _limiter.acquire("/submit_block")
    return requests.post(url, **kwargs)


# =============================================================================
#  CPU midstate  (shared by both backends)
# =============================================================================

_K = [
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,
    0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,
    0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,
    0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,
    0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,
    0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,
    0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,
    0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,
    0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]
_H0  = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
        0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
_M32 = 0xFFFFFFFF


def _rotr32(x, n):
    return ((x >> n) | (x << (32 - n))) & _M32


def _sha256_compress(state, block_bytes):
    W = list(struct.unpack(">16I", block_bytes))
    for i in range(16, 64):
        s0 = _rotr32(W[i-15],7)^_rotr32(W[i-15],18)^(W[i-15]>>3)
        s1 = _rotr32(W[i-2],17)^_rotr32(W[i-2],19) ^(W[i-2]>>10)
        W.append((W[i-16]+s0+W[i-7]+s1)&_M32)
    a,b,c,d,e,f,g,h = state
    for i in range(64):
        S1  = _rotr32(e,6)^_rotr32(e,11)^_rotr32(e,25)
        ch  = (e&f)^(~e&g)
        t1  = (h+S1+ch+_K[i]+W[i])&_M32
        S0  = _rotr32(a,2)^_rotr32(a,13)^_rotr32(a,22)
        maj = (a&b)^(a&c)^(b&c)
        t2  = (S0+maj)&_M32
        h=g;g=f;f=e;e=(d+t1)&_M32;d=c;c=b;b=a;a=(t1+t2)&_M32
    return [(s+v)&_M32 for s,v in zip(state,[a,b,c,d,e,f,g,h])]


def compute_midstate(header_bytes):
    """Compress header[0:64] on the CPU → 8-word uint32 big-endian array."""
    return np.array(_sha256_compress(_H0[:], header_bytes[:64]), dtype=np.uint32)


def header_tail_words(header_bytes):
    """header[64:104] → 10 big-endian uint32 words."""
    return np.array(struct.unpack(">10I", header_bytes[64:104]), dtype=np.uint32)


def _target_to_uint32_be(target_int):
    return np.frombuffer(target_int.to_bytes(32,"big"), dtype=np.dtype(">u4")).astype(np.uint32)


def _eta_str(target, total_hashes, hashrate):
    if hashrate <= 0: return "∞"
    remaining = max(0, (2**256)//(target+1) - total_hashes)
    eta_s = remaining / hashrate
    if eta_s > 86400: return ">1 day"
    if eta_s > 3600:  return f"{eta_s/3600:.1f}h"
    if eta_s > 60:    return f"{eta_s/60:.1f}m"
    return f"{eta_s:.0f}s"


# =============================================================================
#  Calibration constants (shared)
# =============================================================================

_WG_CANDIDATES    = [64, 128, 256, 512]   # OpenCL workgroup / CUDA block sizes
_BATCH_CANDIDATES = [512_000, 1_000_000, 2_000_000, 4_000_000]
_CAL_DURATION     = 1.2    # seconds to time each candidate


# =============================================================================
#  CUDA kernel source  (nvrtc, compiled at runtime)
#
#  CloudyCoin specifics vs TETSUO's Bitcoin kernel:
#    - 112-byte header, nonce is uint64 at [104:112] big-endian
#    - Split into nonce_hi / nonce_lo (two uint32)
#    - Pass-1 block-2: W[0..9]=tail, W[10]=nonce_hi, W[11]=nonce_lo,
#      W[12]=0x80000000, W[13..14]=0, W[15]=0x380 (bitlen 896)
#    - Output in big-endian word order (MSW at hash[0]) — no byte-swap
#    - Target comparison MSW-first: hash[0] <= target[0] etc.
#
#  CUDA-specific optimisations vs the OpenCL kernel:
#    - __funnelshift_r  for ROTR  (native PTX, ~15 % vs shift+or pair)
#    - lop3.b32  for CH and MAJ   (3-input boolean in one instruction)
#    - __shared__  for midstate/tail/target  (same idea as __local)
# =============================================================================

CUDA_KERNEL = r"""
// Self-contained type definitions — no system headers needed for NVRTC
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;

// ── Rotate via PTX funnel-shift (single instruction on all CUDA GPUs) ───────
__device__ __forceinline__ uint32_t rotr(uint32_t x, int n) {
    return __funnelshift_r(x, x, n);
}

// ── CH and MAJ via lop3 (single PTX instruction, TETSUO VARIANT 1) ──────────
// CH(x,y,z) = (x & y) ^ (~x & z)   truth-table = 0xCA
__device__ __forceinline__ uint32_t ch(uint32_t x, uint32_t y, uint32_t z) {
    uint32_t r;
    asm("lop3.b32 %0, %1, %2, %3, 0xCA;" : "=r"(r) : "r"(x), "r"(y), "r"(z));
    return r;
}
// MAJ(x,y,z) = (x & y) ^ (x & z) ^ (y & z)   truth-table = 0xE8
__device__ __forceinline__ uint32_t maj(uint32_t x, uint32_t y, uint32_t z) {
    uint32_t r;
    asm("lop3.b32 %0, %1, %2, %3, 0xE8;" : "=r"(r) : "r"(x), "r"(y), "r"(z));
    return r;
}

__device__ __forceinline__ uint32_t ep0(uint32_t x){return rotr(x,2)^rotr(x,13)^rotr(x,22);}
__device__ __forceinline__ uint32_t ep1(uint32_t x){return rotr(x,6)^rotr(x,11)^rotr(x,25);}
__device__ __forceinline__ uint32_t sig0(uint32_t x){return rotr(x,7)^rotr(x,18)^(x>>3);}
__device__ __forceinline__ uint32_t sig1(uint32_t x){return rotr(x,17)^rotr(x,19)^(x>>10);}

// Single round with inlined K constant
#define ROUND(a,b,c,d,e,f,g,h,w,k) do { \
    uint32_t _t1=(h)+ep1(e)+ch(e,f,g)+(k)+(w); \
    uint32_t _t2=ep0(a)+maj(a,b,c); \
    (h)=(g);(g)=(f);(f)=(e);(e)=(d)+_t1; \
    (d)=(c);(c)=(b);(b)=(a);(a)=_t1+_t2; \
} while(0)

// Sliding-window schedule: compute W[i] in-place in 16-slot ring buffer
#define WSCHED(W,i) \
    ((W)[(i)&15]=sig1((W)[((i)-2)&15])+(W)[((i)-7)&15]+sig0((W)[((i)-15)&15])+(W)[(i)&15])

extern "C"
__global__ void mine(
    const uint32_t* __restrict__ g_midstate,  // 8 words, big-endian
    const uint32_t* __restrict__ g_tail,      // 10 words, big-endian
    const uint32_t* __restrict__ g_target,    // 8 words, big-endian MSW-first
    uint32_t start_hi,
    uint32_t start_lo,
    uint32_t* __restrict__ result_hi,
    uint32_t* __restrict__ result_lo,
    int*      __restrict__ found
) {
    // ── Load per-block constants into shared memory ──────────────────────────
    __shared__ uint32_t sm[8];   // midstate
    __shared__ uint32_t st[10];  // tail
    __shared__ uint32_t sg[8];   // target

    int tid = threadIdx.x;
    if (tid < 8)  { sm[tid] = g_midstate[tid]; sg[tid] = g_target[tid]; }
    if (tid < 10) { st[tid] = g_tail[tid]; }
    __syncthreads();

    if (*found) return;

    // ── Nonce for this thread ────────────────────────────────────────────────
    uint64_t start = ((uint64_t)start_hi << 32) | (uint64_t)start_lo;
    uint64_t nonce = start + (uint64_t)blockIdx.x * blockDim.x + tid;
    uint32_t n_hi  = (uint32_t)(nonce >> 32);
    uint32_t n_lo  = (uint32_t)(nonce & 0xFFFFFFFFu);

    // ── Pass 1, block 2 — 16-slot sliding window ─────────────────────────────
    uint32_t W[16];
    W[ 0]=st[0]; W[ 1]=st[1]; W[ 2]=st[2]; W[ 3]=st[3];
    W[ 4]=st[4]; W[ 5]=st[5]; W[ 6]=st[6]; W[ 7]=st[7];
    W[ 8]=st[8]; W[ 9]=st[9];
    W[10]=n_hi;  W[11]=n_lo;
    W[12]=0x80000000u; W[13]=0u; W[14]=0u; W[15]=0x00000380u;

    uint32_t a=sm[0],b=sm[1],c=sm[2],d=sm[3];
    uint32_t e=sm[4],f=sm[5],g=sm[6],h=sm[7];

    ROUND(a,b,c,d,e,f,g,h,W[ 0],0x428a2f98u);
    ROUND(a,b,c,d,e,f,g,h,W[ 1],0x71374491u);
    ROUND(a,b,c,d,e,f,g,h,W[ 2],0xb5c0fbcfu);
    ROUND(a,b,c,d,e,f,g,h,W[ 3],0xe9b5dba5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 4],0x3956c25bu);
    ROUND(a,b,c,d,e,f,g,h,W[ 5],0x59f111f1u);
    ROUND(a,b,c,d,e,f,g,h,W[ 6],0x923f82a4u);
    ROUND(a,b,c,d,e,f,g,h,W[ 7],0xab1c5ed5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 8],0xd807aa98u);
    ROUND(a,b,c,d,e,f,g,h,W[ 9],0x12835b01u);
    ROUND(a,b,c,d,e,f,g,h,W[10],0x243185beu);
    ROUND(a,b,c,d,e,f,g,h,W[11],0x550c7dc3u);
    ROUND(a,b,c,d,e,f,g,h,W[12],0x72be5d74u);
    ROUND(a,b,c,d,e,f,g,h,W[13],0x80deb1feu);
    ROUND(a,b,c,d,e,f,g,h,W[14],0x9bdc06a7u);
    ROUND(a,b,c,d,e,f,g,h,W[15],0xc19bf174u);

    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,16),0xe49b69c1u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,17),0xefbe4786u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,18),0x0fc19dc6u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,19),0x240ca1ccu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,20),0x2de92c6fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,21),0x4a7484aau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,22),0x5cb0a9dcu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,23),0x76f988dau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,24),0x983e5152u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,25),0xa831c66du);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,26),0xb00327c8u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,27),0xbf597fc7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,28),0xc6e00bf3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,29),0xd5a79147u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,30),0x06ca6351u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,31),0x14292967u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,32),0x27b70a85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,33),0x2e1b2138u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,34),0x4d2c6dfcu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,35),0x53380d13u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,36),0x650a7354u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,37),0x766a0abbu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,38),0x81c2c92eu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,39),0x92722c85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,40),0xa2bfe8a1u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,41),0xa81a664bu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,42),0xc24b8b70u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,43),0xc76c51a3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,44),0xd192e819u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,45),0xd6990624u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,46),0xf40e3585u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,47),0x106aa070u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,48),0x19a4c116u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,49),0x1e376c08u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,50),0x2748774cu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,51),0x34b0bcb5u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,52),0x391c0cb3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,53),0x4ed8aa4au);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,54),0x5b9cca4fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,55),0x682e6ff3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,56),0x748f82eeu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,57),0x78a5636fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,58),0x84c87814u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,59),0x8cc70208u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,60),0x90befffau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,61),0xa4506cebu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,62),0xbef9a3f7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,63),0xc67178f2u);

    // Inner hash — big-endian word order, no byte-swap
    uint32_t s0=sm[0]+a, s1=sm[1]+b, s2=sm[2]+c, s3=sm[3]+d;
    uint32_t s4=sm[4]+e, s5=sm[5]+f, s6=sm[6]+g, s7=sm[7]+h;

    // ── Pass 2: SHA256(32-byte inner hash) ───────────────────────────────────
    W[0]=s0; W[1]=s1; W[2]=s2; W[3]=s3;
    W[4]=s4; W[5]=s5; W[6]=s6; W[7]=s7;
    W[8]=0x80000000u; W[9]=0u; W[10]=0u; W[11]=0u;
    W[12]=0u; W[13]=0u; W[14]=0u; W[15]=0x00000100u;

    a=0x6a09e667u; b=0xbb67ae85u; c=0x3c6ef372u; d=0xa54ff53au;
    e=0x510e527fu; f=0x9b05688cu; g=0x1f83d9abu; h=0x5be0cd19u;

    ROUND(a,b,c,d,e,f,g,h,W[ 0],0x428a2f98u);
    ROUND(a,b,c,d,e,f,g,h,W[ 1],0x71374491u);
    ROUND(a,b,c,d,e,f,g,h,W[ 2],0xb5c0fbcfu);
    ROUND(a,b,c,d,e,f,g,h,W[ 3],0xe9b5dba5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 4],0x3956c25bu);
    ROUND(a,b,c,d,e,f,g,h,W[ 5],0x59f111f1u);
    ROUND(a,b,c,d,e,f,g,h,W[ 6],0x923f82a4u);
    ROUND(a,b,c,d,e,f,g,h,W[ 7],0xab1c5ed5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 8],0xd807aa98u);
    ROUND(a,b,c,d,e,f,g,h,W[ 9],0x12835b01u);
    ROUND(a,b,c,d,e,f,g,h,W[10],0x243185beu);
    ROUND(a,b,c,d,e,f,g,h,W[11],0x550c7dc3u);
    ROUND(a,b,c,d,e,f,g,h,W[12],0x72be5d74u);
    ROUND(a,b,c,d,e,f,g,h,W[13],0x80deb1feu);
    ROUND(a,b,c,d,e,f,g,h,W[14],0x9bdc06a7u);
    ROUND(a,b,c,d,e,f,g,h,W[15],0xc19bf174u);

    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,16),0xe49b69c1u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,17),0xefbe4786u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,18),0x0fc19dc6u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,19),0x240ca1ccu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,20),0x2de92c6fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,21),0x4a7484aau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,22),0x5cb0a9dcu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,23),0x76f988dau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,24),0x983e5152u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,25),0xa831c66du);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,26),0xb00327c8u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,27),0xbf597fc7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,28),0xc6e00bf3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,29),0xd5a79147u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,30),0x06ca6351u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,31),0x14292967u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,32),0x27b70a85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,33),0x2e1b2138u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,34),0x4d2c6dfcu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,35),0x53380d13u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,36),0x650a7354u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,37),0x766a0abbu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,38),0x81c2c92eu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,39),0x92722c85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,40),0xa2bfe8a1u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,41),0xa81a664bu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,42),0xc24b8b70u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,43),0xc76c51a3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,44),0xd192e819u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,45),0xd6990624u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,46),0xf40e3585u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,47),0x106aa070u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,48),0x19a4c116u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,49),0x1e376c08u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,50),0x2748774cu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,51),0x34b0bcb5u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,52),0x391c0cb3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,53),0x4ed8aa4au);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,54),0x5b9cca4fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,55),0x682e6ff3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,56),0x748f82eeu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,57),0x78a5636fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,58),0x84c87814u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,59),0x8cc70208u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,60),0x90befffau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,61),0xa4506cebu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,62),0xbef9a3f7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,63),0xc67178f2u);

    // ── Early exit on MSW (big-endian word 0 = most significant) ────────────
    uint32_t msw = 0x6a09e667u + a;
    if (msw > sg[0]) return;

    uint32_t h1=0xbb67ae85u+b, h2=0x3c6ef372u+c, h3=0xa54ff53au+d;
    uint32_t h4=0x510e527fu+e, h5=0x9b05688cu+f, h6=0x1f83d9abu+g, h7=0x5be0cd19u+h;

    if (msw == sg[0]) {
        if (h1 > sg[1]) return; if (h1 < sg[1]) goto found;
        if (h2 > sg[2]) return; if (h2 < sg[2]) goto found;
        if (h3 > sg[3]) return; if (h3 < sg[3]) goto found;
        if (h4 > sg[4]) return; if (h4 < sg[4]) goto found;
        if (h5 > sg[5]) return; if (h5 < sg[5]) goto found;
        if (h6 > sg[6]) return; if (h6 < sg[6]) goto found;
        if (h7 > sg[7]) return;
    }

    found:
    if (atomicCAS(found, 0, 1) == 0) {
        *result_hi = n_hi;
        *result_lo = n_lo;
    }
}
"""

# =============================================================================
#  OpenCL kernel source
# =============================================================================

OPENCL_KERNEL = r"""
#define ROTR(x,n)   (rotate((uint)(x),(uint)(32-(n))))
#define CH(x,y,z)   (((x)&(y))^(~(x)&(z)))
#define MAJ(x,y,z)  (((x)&(y))^((x)&(z))^((y)&(z)))
#define EP0(x)      (ROTR(x,2) ^ROTR(x,13)^ROTR(x,22))
#define EP1(x)      (ROTR(x,6) ^ROTR(x,11)^ROTR(x,25))
#define SIG0(x)     (ROTR(x,7) ^ROTR(x,18)^((x)>>3))
#define SIG1(x)     (ROTR(x,17)^ROTR(x,19)^((x)>>10))

#define ROUND(a,b,c,d,e,f,g,h,w,k) do { \
    uint _t1=(h)+EP1(e)+CH(e,f,g)+(k)+(w); \
    uint _t2=EP0(a)+MAJ(a,b,c); \
    (h)=(g);(g)=(f);(f)=(e);(e)=(d)+_t1; \
    (d)=(c);(c)=(b);(b)=(a);(a)=_t1+_t2; \
} while(0)

#define WSCHED(W,i) \
    ((W)[(i)&15]=SIG1((W)[((i)-2)&15])+(W)[((i)-7)&15]+SIG0((W)[((i)-15)&15])+(W)[(i)&15])

__kernel void mine(
    __global const uint *g_midstate, __global const uint *g_tail,
    __global const uint *g_target,
    uint start_hi, uint start_lo,
    __global uint *result_hi, __global uint *result_lo, __global int *found
) {
    __local uint lm[8]; __local uint lt[10]; __local uint lg[8];
    uint lid = get_local_id(0);
    if (lid < 8)  { lm[lid]=g_midstate[lid]; lg[lid]=g_target[lid]; }
    if (lid < 10) { lt[lid]=g_tail[lid]; }
    barrier(CLK_LOCAL_MEM_FENCE);
    if (*found) return;

    ulong gid=get_global_id(0);
    ulong nonce=((ulong)start_hi<<32|(ulong)start_lo)+gid;
    uint n_hi=(uint)(nonce>>32), n_lo=(uint)(nonce&0xFFFFFFFFu);

    uint W[16];
    W[0]=lt[0];W[1]=lt[1];W[2]=lt[2];W[3]=lt[3];W[4]=lt[4];
    W[5]=lt[5];W[6]=lt[6];W[7]=lt[7];W[8]=lt[8];W[9]=lt[9];
    W[10]=n_hi; W[11]=n_lo;
    W[12]=0x80000000u; W[13]=0u; W[14]=0u; W[15]=0x00000380u;

    uint a=lm[0],b=lm[1],c=lm[2],d=lm[3],e=lm[4],f=lm[5],g=lm[6],h=lm[7];

    ROUND(a,b,c,d,e,f,g,h,W[ 0],0x428a2f98u); ROUND(a,b,c,d,e,f,g,h,W[ 1],0x71374491u);
    ROUND(a,b,c,d,e,f,g,h,W[ 2],0xb5c0fbcfu); ROUND(a,b,c,d,e,f,g,h,W[ 3],0xe9b5dba5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 4],0x3956c25bu); ROUND(a,b,c,d,e,f,g,h,W[ 5],0x59f111f1u);
    ROUND(a,b,c,d,e,f,g,h,W[ 6],0x923f82a4u); ROUND(a,b,c,d,e,f,g,h,W[ 7],0xab1c5ed5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 8],0xd807aa98u); ROUND(a,b,c,d,e,f,g,h,W[ 9],0x12835b01u);
    ROUND(a,b,c,d,e,f,g,h,W[10],0x243185beu); ROUND(a,b,c,d,e,f,g,h,W[11],0x550c7dc3u);
    ROUND(a,b,c,d,e,f,g,h,W[12],0x72be5d74u); ROUND(a,b,c,d,e,f,g,h,W[13],0x80deb1feu);
    ROUND(a,b,c,d,e,f,g,h,W[14],0x9bdc06a7u); ROUND(a,b,c,d,e,f,g,h,W[15],0xc19bf174u);

    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,16),0xe49b69c1u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,17),0xefbe4786u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,18),0x0fc19dc6u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,19),0x240ca1ccu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,20),0x2de92c6fu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,21),0x4a7484aau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,22),0x5cb0a9dcu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,23),0x76f988dau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,24),0x983e5152u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,25),0xa831c66du);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,26),0xb00327c8u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,27),0xbf597fc7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,28),0xc6e00bf3u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,29),0xd5a79147u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,30),0x06ca6351u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,31),0x14292967u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,32),0x27b70a85u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,33),0x2e1b2138u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,34),0x4d2c6dfcu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,35),0x53380d13u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,36),0x650a7354u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,37),0x766a0abbu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,38),0x81c2c92eu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,39),0x92722c85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,40),0xa2bfe8a1u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,41),0xa81a664bu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,42),0xc24b8b70u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,43),0xc76c51a3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,44),0xd192e819u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,45),0xd6990624u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,46),0xf40e3585u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,47),0x106aa070u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,48),0x19a4c116u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,49),0x1e376c08u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,50),0x2748774cu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,51),0x34b0bcb5u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,52),0x391c0cb3u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,53),0x4ed8aa4au);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,54),0x5b9cca4fu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,55),0x682e6ff3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,56),0x748f82eeu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,57),0x78a5636fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,58),0x84c87814u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,59),0x8cc70208u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,60),0x90befffau); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,61),0xa4506cebu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,62),0xbef9a3f7u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,63),0xc67178f2u);

    uint s0=lm[0]+a,s1=lm[1]+b,s2=lm[2]+c,s3=lm[3]+d;
    uint s4=lm[4]+e,s5=lm[5]+f,s6=lm[6]+g,s7=lm[7]+h;

    W[0]=s0;W[1]=s1;W[2]=s2;W[3]=s3;W[4]=s4;W[5]=s5;W[6]=s6;W[7]=s7;
    W[8]=0x80000000u;W[9]=0u;W[10]=0u;W[11]=0u;W[12]=0u;W[13]=0u;W[14]=0u;W[15]=0x00000100u;
    a=0x6a09e667u;b=0xbb67ae85u;c=0x3c6ef372u;d=0xa54ff53au;
    e=0x510e527fu;f=0x9b05688cu;g=0x1f83d9abu;h=0x5be0cd19u;

    ROUND(a,b,c,d,e,f,g,h,W[ 0],0x428a2f98u); ROUND(a,b,c,d,e,f,g,h,W[ 1],0x71374491u);
    ROUND(a,b,c,d,e,f,g,h,W[ 2],0xb5c0fbcfu); ROUND(a,b,c,d,e,f,g,h,W[ 3],0xe9b5dba5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 4],0x3956c25bu); ROUND(a,b,c,d,e,f,g,h,W[ 5],0x59f111f1u);
    ROUND(a,b,c,d,e,f,g,h,W[ 6],0x923f82a4u); ROUND(a,b,c,d,e,f,g,h,W[ 7],0xab1c5ed5u);
    ROUND(a,b,c,d,e,f,g,h,W[ 8],0xd807aa98u); ROUND(a,b,c,d,e,f,g,h,W[ 9],0x12835b01u);
    ROUND(a,b,c,d,e,f,g,h,W[10],0x243185beu); ROUND(a,b,c,d,e,f,g,h,W[11],0x550c7dc3u);
    ROUND(a,b,c,d,e,f,g,h,W[12],0x72be5d74u); ROUND(a,b,c,d,e,f,g,h,W[13],0x80deb1feu);
    ROUND(a,b,c,d,e,f,g,h,W[14],0x9bdc06a7u); ROUND(a,b,c,d,e,f,g,h,W[15],0xc19bf174u);

    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,16),0xe49b69c1u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,17),0xefbe4786u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,18),0x0fc19dc6u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,19),0x240ca1ccu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,20),0x2de92c6fu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,21),0x4a7484aau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,22),0x5cb0a9dcu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,23),0x76f988dau);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,24),0x983e5152u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,25),0xa831c66du);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,26),0xb00327c8u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,27),0xbf597fc7u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,28),0xc6e00bf3u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,29),0xd5a79147u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,30),0x06ca6351u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,31),0x14292967u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,32),0x27b70a85u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,33),0x2e1b2138u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,34),0x4d2c6dfcu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,35),0x53380d13u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,36),0x650a7354u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,37),0x766a0abbu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,38),0x81c2c92eu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,39),0x92722c85u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,40),0xa2bfe8a1u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,41),0xa81a664bu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,42),0xc24b8b70u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,43),0xc76c51a3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,44),0xd192e819u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,45),0xd6990624u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,46),0xf40e3585u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,47),0x106aa070u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,48),0x19a4c116u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,49),0x1e376c08u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,50),0x2748774cu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,51),0x34b0bcb5u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,52),0x391c0cb3u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,53),0x4ed8aa4au);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,54),0x5b9cca4fu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,55),0x682e6ff3u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,56),0x748f82eeu); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,57),0x78a5636fu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,58),0x84c87814u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,59),0x8cc70208u);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,60),0x90befffau); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,61),0xa4506cebu);
    ROUND(a,b,c,d,e,f,g,h,WSCHED(W,62),0xbef9a3f7u); ROUND(a,b,c,d,e,f,g,h,WSCHED(W,63),0xc67178f2u);

    uint msw=0x6a09e667u+a;
    if (msw>lg[0]) return;
    uint h1=0xbb67ae85u+b,h2=0x3c6ef372u+c,h3=0xa54ff53au+d;
    uint h4=0x510e527fu+e,h5=0x9b05688cu+f,h6=0x1f83d9abu+g,h7=0x5be0cd19u+h;
    if (msw==lg[0]) {
        if(h1>lg[1])return; if(h1<lg[1])goto _f;
        if(h2>lg[2])return; if(h2<lg[2])goto _f;
        if(h3>lg[3])return; if(h3<lg[3])goto _f;
        if(h4>lg[4])return; if(h4<lg[4])goto _f;
        if(h5>lg[5])return; if(h5<lg[5])goto _f;
        if(h6>lg[6])return; if(h6<lg[6])goto _f;
        if(h7>lg[7])return;
    }
    _f: if(atomic_cmpxchg(found,0,1)==0){*result_hi=n_hi;*result_lo=n_lo;}
}
"""


# =============================================================================
#  Backend classes — identical external interface
#
#  Both expose:
#    .name          str
#    .setup()       → raises on failure
#    .compile()
#    .calibrate(log) → (block_size, batch)
#    .run_batch(midstate, tail, target, start_nonce, block_size, batch)
#                   → (found: bool, nonce_hi: int, nonce_lo: int)
#    .cleanup()
# =============================================================================

class _CudaBackend:
    """pycuda + runtime-compiled kernel."""

    name = "CUDA"

    def __init__(self):
        self._ctx    = None
        self._mod    = None
        self._fn     = None
        self._d_ms   = None   # device midstate buffer
        self._d_tail = None
        self._d_tgt  = None
        self._d_rhi  = None
        self._d_rlo  = None
        self._d_fnd  = None

    def setup(self):
        dev       = cuda.Device(0)
        self._dev = dev
        # make_context() auto-pushes; pop immediately so we start with a
        # clean stack.  Each method that needs CUDA calls push()/pop() itself.
        self._ctx = dev.make_context()
        self._ctx.pop()
        # Read device attributes while ctx is not on the stack (safe via dev obj)
        sm_count  = dev.get_attribute(cuda.device_attribute.MULTIPROCESSOR_COUNT)
        cc        = dev.compute_capability()
        self.device_name = f"{dev.name()}  (SM {cc[0]}.{cc[1]}, {sm_count} SMs)"

    def compile(self):
        self._ctx.push()
        try:
            cc = self._dev.compute_capability()
            sm = f"sm_{cc[0]}{cc[1]}"
            compiled = False

            # ── Path 1: NVRTC via ctypes (no nvcc required) ───────────────────
            _nvrtc_err = None   # captured outside except so it survives the block
            try:
                ptx = _nvrtc_compile(CUDA_KERNEL, sm)
                self._mod = cuda.module_from_buffer(ptx)
                self._fn  = self._mod.get_function("mine")
                compiled  = True
            except Exception as _e:
                _nvrtc_err = _e   # Python 3.11+ deletes 'as' vars after except exits

            # ── Path 2: pycuda.compiler (requires nvcc in PATH) ───────────────
            if not compiled:
                try:
                    import pycuda.compiler as _nvcc
                    self._mod = _nvcc.SourceModule(CUDA_KERNEL, no_extern_c=True,
                                                   options=["--use_fast_math"])
                    self._fn  = self._mod.get_function("mine")
                    compiled  = True
                except Exception as _nvcc_err:
                    raise RuntimeError(
                        f"CUDA compile failed.\n"
                        f"  NVRTC: {str(_nvrtc_err).encode('ascii', 'replace').decode()}\n"
                        f"  nvcc:  {str(_nvcc_err).encode('ascii', 'replace').decode()}\n"
                        f"Install nvidia-cuda-toolkit or ensure libnvrtc.so is present."
                    )
        finally:
            self._ctx.pop()

    def _alloc_persistent(self):
        """Allocate fixed-size output buffers once."""
        self._ctx.push()
        try:
            self._d_rhi = cuda.mem_alloc(4)
            self._d_rlo = cuda.mem_alloc(4)
            self._d_fnd = cuda.mem_alloc(4)
        finally:
            self._ctx.pop()

    def calibrate(self, log):
        self._ctx.push()
        try:
            return self._calibrate_inner(log)
        finally:
            self._ctx.pop()

    def _calibrate_inner(self, log):
        dummy_ms  = np.zeros(8,  dtype=np.uint32)
        dummy_tail= np.zeros(10, dtype=np.uint32)
        dummy_tgt = np.zeros(8,  dtype=np.uint32)

        d_ms   = cuda.to_device(dummy_ms)
        d_tail = cuda.to_device(dummy_tail)
        d_tgt  = cuda.to_device(dummy_tgt)
        d_rhi  = cuda.mem_alloc(4)
        d_rlo  = cuda.mem_alloc(4)
        d_fnd  = cuda.mem_alloc(4)

        # CUDA uses block size (= threads per block), not "workgroup"
        # Max threads per block on any modern CUDA GPU is 1024
        max_block = 1024
        valid_bs  = [b for b in _WG_CANDIDATES if b <= max_block]

        log(f"   [CUDA] Calibrating… block={valid_bs} batch={[f'{b//1000}k' if b < 1_000_000 else f'{b//1_000_000:.0f}M' for b in _BATCH_CANDIDATES]}")

        best_hr, best_bs, best_batch = 0.0, valid_bs[0], _BATCH_CANDIDATES[0]

        zero4 = np.int32(0).tobytes()

        for bs in valid_bs:
            for batch in _BATCH_CANDIDATES:
                # Grid = ceil(batch / bs) blocks
                grid = (batch + bs - 1) // bs

                # Warmup
                for _ in range(2):
                    cuda.memcpy_htod(d_fnd, zero4)
                    self._fn(
                        d_ms, d_tail, d_tgt,
                        np.uint32(0), np.uint32(0),
                        d_rhi, d_rlo, d_fnd,
                        block=(bs,1,1), grid=(grid,1,1),
                    )
                    cuda.Context.synchronize()

                hashes  = 0
                t_start = time.perf_counter()
                t_end   = t_start + _CAL_DURATION

                while time.perf_counter() < t_end:
                    cuda.memcpy_htod(d_fnd, zero4)
                    self._fn(
                        d_ms, d_tail, d_tgt,
                        np.uint32(0), np.uint32(0),
                        d_rhi, d_rlo, d_fnd,
                        block=(bs,1,1), grid=(grid,1,1),
                    )
                    cuda.Context.synchronize()
                    hashes += grid * bs

                elapsed = time.perf_counter() - t_start
                hr      = hashes / elapsed if elapsed > 0 else 0
                log(f"   [CUDA] block={bs:>4}  batch={batch//1_000_000:.1f}M  →  {hr/1e6:.1f} MH/s")

                if hr > best_hr:
                    best_hr, best_bs, best_batch = hr, bs, batch

        log(f"   [CUDA] ✓  block={best_bs}  batch={best_batch//1_000_000:.1f}M  peak={best_hr/1e6:.1f} MH/s")
        return best_bs, best_batch, best_hr

    def run_batch(self, midstate_np, tail_np, target_np, start_nonce, block_size, batch):
        self._ctx.push()
        try:
            # Upload per-job inputs
            d_ms   = cuda.to_device(midstate_np)
            d_tail = cuda.to_device(tail_np)
            d_tgt  = cuda.to_device(target_np)

            cuda.memcpy_htod(self._d_fnd, np.int32(0).tobytes())
            cuda.memcpy_htod(self._d_rhi, np.uint32(0).tobytes())
            cuda.memcpy_htod(self._d_rlo, np.uint32(0).tobytes())

            grid     = (batch + block_size - 1) // block_size
            nonce_hi = np.uint32(int(start_nonce) >> 32)
            nonce_lo = np.uint32(int(start_nonce) & 0xFFFFFFFF)

            self._fn(
                d_ms, d_tail, d_tgt,
                nonce_hi, nonce_lo,
                self._d_rhi, self._d_rlo, self._d_fnd,
                block=(block_size, 1, 1), grid=(grid, 1, 1),
            )
            cuda.Context.synchronize()

            found_h = np.zeros(1, dtype=np.int32)
            rhi_h   = np.zeros(1, dtype=np.uint32)
            rlo_h   = np.zeros(1, dtype=np.uint32)
            cuda.memcpy_dtoh(found_h, self._d_fnd)
            cuda.memcpy_dtoh(rhi_h,   self._d_rhi)
            cuda.memcpy_dtoh(rlo_h,   self._d_rlo)

            return bool(found_h[0]), int(rhi_h[0]), int(rlo_h[0]), grid * block_size

        finally:
            self._ctx.pop()

    def cleanup(self):
        # detach() releases the context regardless of stack state — safe to
        # call from any thread and even if the context was never pushed here.
        if self._ctx:
            try:
                self._ctx.detach()
            except Exception:
                pass
            self._ctx = None


class _OpenCLBackend:
    """pyopencl backend — works on NVIDIA, AMD, Intel."""

    name = "OpenCL"

    def __init__(self):
        self._ctx    = None
        self._queue  = None
        self._prog   = None
        self._device = None
        self._mf     = None
        self._d_rhi  = None
        self._d_rlo  = None
        self._d_fnd  = None

    def setup(self):
        for p in cl.get_platforms():
            for d in p.get_devices():
                if d.type == cl.device_type.GPU:
                    self._device = d
                    self._ctx    = cl.Context([d])
                    self._queue  = cl.CommandQueue(self._ctx)
                    self._mf     = cl.mem_flags
                    self.device_name = d.name.strip()
                    return
        raise RuntimeError("No OpenCL GPU found.")

    def compile(self):
        self._prog   = cl.Program(self._ctx, OPENCL_KERNEL).build()
        # Cache the kernel object once — avoids RepeatedKernelRetrieval warning
        # and saves the overhead of constructing a new cl.Kernel on every batch.
        self._kernel = cl.Kernel(self._prog, "mine")

    def _alloc_persistent(self):
        mf = self._mf
        self._d_rhi = cl.Buffer(self._ctx, mf.READ_WRITE, size=4)
        self._d_rlo = cl.Buffer(self._ctx, mf.READ_WRITE, size=4)
        self._d_fnd = cl.Buffer(self._ctx, mf.READ_WRITE, size=4)

    def calibrate(self, log):
        mf  = self._mf
        ctx = self._ctx
        q   = self._queue

        dummy_ms   = np.zeros(8,  dtype=np.uint32)
        dummy_tail = np.zeros(10, dtype=np.uint32)
        dummy_tgt  = np.zeros(8,  dtype=np.uint32)

        d_ms   = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=dummy_ms)
        d_tail = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=dummy_tail)
        d_tgt  = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=dummy_tgt)
        d_rhi  = cl.Buffer(ctx, mf.READ_WRITE, size=4)
        d_rlo  = cl.Buffer(ctx, mf.READ_WRITE, size=4)
        d_fnd  = cl.Buffer(ctx, mf.READ_WRITE, size=4)

        max_wg   = self._device.max_work_group_size
        valid_wg = [w for w in _WG_CANDIDATES if w <= max_wg] or [min(_WG_CANDIDATES[0], max_wg)]

        log(f"   [OpenCL] Calibrating… wg={valid_wg} batch={[f'{b//1000}k' if b < 1_000_000 else f'{b//1_000_000:.0f}M' for b in _BATCH_CANDIDATES]}")

        best_hr, best_wg, best_batch = 0.0, valid_wg[0], _BATCH_CANDIDATES[0]

        for wg in valid_wg:
            for batch in _BATCH_CANDIDATES:
                gws = ((batch + wg - 1) // wg) * wg

                for _ in range(2):
                    cl.enqueue_fill_buffer(q, d_fnd, np.int32(0), 0, 4)
                    self._kernel(q, (gws,), (wg,), d_ms, d_tail, d_tgt,
                                     np.uint32(0), np.uint32(0), d_rhi, d_rlo, d_fnd)
                    q.finish()

                hashes  = 0
                t_start = time.perf_counter()
                t_end   = t_start + _CAL_DURATION

                while time.perf_counter() < t_end:
                    cl.enqueue_fill_buffer(q, d_fnd, np.int32(0), 0, 4)
                    self._kernel(q, (gws,), (wg,), d_ms, d_tail, d_tgt,
                                  np.uint32(0), np.uint32(0), d_rhi, d_rlo, d_fnd)
                    q.finish()
                    hashes += gws

                elapsed = time.perf_counter() - t_start
                hr      = hashes / elapsed if elapsed > 0 else 0
                log(f"   [OpenCL] wg={wg:>4}  batch={batch//1_000_000:.1f}M  →  {hr/1e6:.1f} MH/s")

                if hr > best_hr:
                    best_hr, best_wg, best_batch = hr, wg, batch

        log(f"   [OpenCL] ✓  wg={best_wg}  batch={best_batch//1_000_000:.1f}M  peak={best_hr/1e6:.1f} MH/s")
        return best_wg, best_batch, best_hr

    def run_batch(self, midstate_np, tail_np, target_np, start_nonce, wg_size, batch):
        mf  = self._mf
        ctx = self._ctx
        q   = self._queue

        d_ms   = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=midstate_np)
        d_tail = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=tail_np)
        d_tgt  = cl.Buffer(ctx, mf.READ_ONLY|mf.COPY_HOST_PTR, hostbuf=target_np)

        gws      = ((batch + wg_size - 1) // wg_size) * wg_size
        nonce_hi = np.uint32(int(start_nonce) >> 32)
        nonce_lo = np.uint32(int(start_nonce) & 0xFFFFFFFF)

        cl.enqueue_fill_buffer(q, self._d_fnd, np.int32(0),  0, 4)
        cl.enqueue_fill_buffer(q, self._d_rhi, np.uint32(0), 0, 4)
        cl.enqueue_fill_buffer(q, self._d_rlo, np.uint32(0), 0, 4)

        self._kernel(q, (gws,), (wg_size,),
                     d_ms, d_tail, d_tgt,
                     nonce_hi, nonce_lo,
                     self._d_rhi, self._d_rlo, self._d_fnd)

        found_h = np.zeros(1, dtype=np.int32)
        rhi_h   = np.zeros(1, dtype=np.uint32)
        rlo_h   = np.zeros(1, dtype=np.uint32)
        cl.enqueue_copy(q, found_h, self._d_fnd)
        cl.enqueue_copy(q, rhi_h,   self._d_rhi)
        cl.enqueue_copy(q, rlo_h,   self._d_rlo)
        q.finish()

        return bool(found_h[0]), int(rhi_h[0]), int(rlo_h[0]), gws

    def cleanup(self):
        pass   # OpenCL cleans up via GC


# =============================================================================
#  detect_gpu — used by wallet.py at import time
# =============================================================================

def detect_gpu():
    """Return a human-readable GPU name string, or None if no GPU found."""
    if CUDA_AVAILABLE:
        try:
            return cuda.Device(0).name()
        except Exception:
            pass
    if OPENCL_AVAILABLE:
        try:
            for p in cl.get_platforms():
                for d in p.get_devices():
                    if d.type == cl.device_type.GPU:
                        return d.name.strip()
        except Exception:
            pass
    return None


# =============================================================================
#  GpuMiner — picks the faster backend via calibration
# =============================================================================

class GpuMiner:
    """
    Dual-backend GPU miner.  Calibrates CUDA and OpenCL independently,
    then mines with whichever scored higher.

    Parameters
    ----------
    batch_size : int
        > 0  →  skip batch calibration, use this fixed value.
        0    →  auto-calibrate (default).
    inter_batch_sleep : float
        Seconds to sleep between kernel launches (throttle).  0 = full speed.
    """

    _HR_WINDOW = 8

    def __init__(
        self,
        address:           str,
        server_url:        str,
        batch_size:        int   = 0,
        inter_batch_sleep: float = 0.0,
        on_log   = None,
        on_stats = None,
        on_found = None,
    ):
        self.address           = address
        self.server_url        = server_url
        self._user_batch       = batch_size
        self.inter_batch_sleep = inter_batch_sleep
        self.on_log    = on_log   or (lambda m: print(m))
        self.on_stats  = on_stats or (lambda d: None)
        self.on_found  = on_found or (lambda i, h: None)

        self._stop_flag    = threading.Event()
        self._blocks_found = 0
        self._backend      = None   # chosen after calibration

    def stop(self):
        self._stop_flag.set()

    # ── Backend selection and calibration ─────────────────────────────────────

    def _init_backends(self):
        """
        Try to initialise and calibrate both backends.
        Returns the faster one, plus its calibrated (block_size, batch).
        Logs clearly what was tried and what won.
        Any unexpected exception from a backend is caught and logged so
        the other backend still gets a chance to run.
        """
        results = []   # [(backend, block_size, batch, peak_hr)]

        # ── CUDA ──────────────────────────────────────────────────────────────
        if CUDA_AVAILABLE:
            b = _CudaBackend()
            try:
                b.setup()
                self.on_log(f"   CUDA device : {b.device_name}")
                self.on_log("   Compiling CUDA kernel…")
                b.compile()
                self.on_log("   CUDA kernel compiled.")
                bs, batch, hr = b.calibrate(self.on_log)
                b._alloc_persistent()
                results.append((b, bs, batch, hr))
            except Exception as e:
                # Log the ASCII-safe version so Windows doesn't choke on
                # any special characters in driver error messages
                safe_err = str(e).encode("ascii", "replace").decode()
                self.on_log(f"   CUDA failed: {safe_err}")
                try:
                    b.cleanup()
                except Exception:
                    pass
        else:
            self.on_log("   CUDA not available (install pycuda for NVIDIA speedup)")

        # ── OpenCL ────────────────────────────────────────────────────────────
        if OPENCL_AVAILABLE:
            b = _OpenCLBackend()
            try:
                b.setup()
                self.on_log(f"   OpenCL device: {b.device_name}")
                self.on_log("   Compiling OpenCL kernel…")
                b.compile()
                self.on_log("   OpenCL kernel compiled.")
                wg, batch, hr = b.calibrate(self.on_log)
                b._alloc_persistent()
                results.append((b, wg, batch, hr))
            except Exception as e:
                self.on_log(f"   OpenCL failed: {e}")
        else:
            self.on_log("   OpenCL not available (install pyopencl for AMD/Intel support)")

        if not results:
            raise RuntimeError("No GPU backend available. Install pycuda or pyopencl.")

        # Pick the faster backend
        results.sort(key=lambda r: r[3], reverse=True)
        winner, bs, batch, hr = results[0]

        if len(results) > 1:
            loser = results[1]
            self.on_log(
                f"\n   ▶ Winner: {winner.name} ({hr/1e6:.1f} MH/s)  "
                f"vs {loser[0].name} ({loser[3]/1e6:.1f} MH/s)"
            )
            # Shut down the losing backend
            try:
                loser[0].cleanup()
            except Exception:
                pass
        else:
            self.on_log(f"   ▶ Using {winner.name} ({hr/1e6:.1f} MH/s)")

        # Override batch if user specified one
        if self._user_batch > 0:
            batch = self._user_batch

        return winner, bs, batch

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        self._stop_flag.clear()

        if not CUDA_AVAILABLE and not OPENCL_AVAILABLE:
            self.on_log("✗ No GPU backend available. Install pycuda or pyopencl.")
            return

        self.on_log("⛏  Initialising GPU backends…")

        try:
            backend, block_size, batch = self._init_backends()
        except RuntimeError as e:
            self.on_log(f"✗ {e}")
            return

        self._backend = backend
        self.on_log(f"   Mining: {backend.name}  block={block_size}  batch={batch:,}")

        retry_delay = 4

        while not self._stop_flag.is_set():
            # ── Fetch job ─────────────────────────────────────────────────────
            try:
                resp = _api_get(
                    f"{self.server_url}/get_mining_job",
                    params={"miner_address": self.address},
                    timeout=10,
                )
                if resp.status_code != 200:
                    self.on_log(f"✗ Server {resp.status_code}, retry in {retry_delay}s…")
                    self._stop_flag.wait(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    continue
                job = wire.decode_mining_job(resp.content)
                retry_delay = 4
            except Exception as e:
                self.on_log(f"✗ Unreachable: {e}, retry in {retry_delay}s…")
                self._stop_flag.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue

            job_id_raw   = job["job_id_raw"]
            block_index  = job["block_index"]
            header_bytes = bytearray(job["header_bytes"])
            target       = job["target"]

            self.on_log(
                f"▶ Job #{block_index}"
                f"  target={target.to_bytes(32,'big').hex()[:16]}…"
                f"  txs={job['tx_count']}"
            )

            midstate_np = compute_midstate(bytes(header_bytes))
            tail_np     = header_tail_words(bytes(header_bytes))
            target_np   = _target_to_uint32_be(target)

            start_nonce  = np.uint64(0)
            start_t      = time.time()
            poll_t       = start_t
            total_hashes = 0
            solved       = False
            hr_window    = []
            last_hr      = 0.0

            while not self._stop_flag.is_set():
                found, rhi, rlo, n_hashed = backend.run_batch(
                    midstate_np, tail_np, target_np,
                    start_nonce, block_size, batch,
                )

                total_hashes += n_hashed
                start_nonce  += np.uint64(n_hashed)

                elapsed  = time.time() - start_t
                hr_window.append(total_hashes / elapsed if elapsed > 0 else 0)
                if len(hr_window) > self._HR_WINDOW:
                    hr_window.pop(0)
                last_hr = sum(hr_window) / len(hr_window)

                self.on_stats({
                    "hashrate":     last_hr,
                    "nonce":        total_hashes,
                    "block":        block_index,
                    "blocks_found": self._blocks_found,
                    "eta":          _eta_str(target, total_hashes, last_hr),
                })

                if found:
                    solved = True
                    break

                now = time.time()
                if now - poll_t > 10:
                    poll_t = now
                    try:
                        r = _api_get(f"{self.server_url}/blockchain", timeout=5)
                        if r.status_code == 200:
                            chain, _ = wire.decode_blockchain(r.content)
                            if len(chain) > block_index:
                                self.on_log(f"⚡ Block {block_index} externally solved — new job")
                                break
                    except Exception:
                        pass

                if self.inter_batch_sleep > 0:
                    self._stop_flag.wait(self.inter_batch_sleep)

            if solved:
                winning_nonce = (rhi << 32) | rlo
                elapsed       = time.time() - start_t

                struct.pack_into(">Q", header_bytes, 104, winning_nonce)
                verify = hashlib.sha256(hashlib.sha256(bytes(header_bytes)).digest()).digest()
                block_hash = verify.hex()

                if int.from_bytes(verify, "big") > target:
                    self.on_log("✗ Backend returned bad nonce — skipping")
                    continue

                self._blocks_found += 1
                self.on_log(
                    f"✦ SOLVED block {block_index}!  nonce={winning_nonce:,}"
                    f"  hash={block_hash[:20]}…  time={elapsed:.1f}s"
                )

                try:
                    body = wire.encode_submit_block(job_id_raw, bytes(header_bytes))
                    res  = _api_post(
                        f"{self.server_url}/submit_block", data=body,
                        headers={"Content-Type": "application/octet-stream"}, timeout=10,
                    )
                    if res.status_code == 201:
                        info = wire.decode_submit_block(res.content)
                        self.on_log(
                            f"  ✓ Accepted: block #{info['block_index']}"
                            f"  hash={info['block_hash'][:20]}…"
                        )
                    else:
                        self.on_log(f"  ✗ Rejected: {wire.decode_error(res.content)}")
                except Exception as e:
                    self.on_log(f"  Submit error: {e}")

                self.on_found(block_index, block_hash)
                self.on_stats({
                    "hashrate": last_hr, "nonce": total_hashes,
                    "block": block_index, "blocks_found": self._blocks_found, "eta": "—",
                })
                self._stop_flag.wait(1)

        if self._backend:
            try:
                self._backend.cleanup()
            except Exception:
                pass
