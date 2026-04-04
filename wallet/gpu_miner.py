"""
=============================================================================
  gpu_miner.py  —  PyOpenCL GPU mining backend for Chain Wallet
  Import this module; do not run it directly.
=============================================================================
"""

import threading
import time
import hashlib
import json

import numpy as np
import requests

try:
    import pyopencl as cl
    OPENCL_AVAILABLE = True
except ImportError:
    cl = None
    OPENCL_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
#  OpenCL kernel — SHA-256 mined directly on GPU
# ─────────────────────────────────────────────────────────────────────────────
KERNEL_SOURCE = r"""
#define ROTR(x,n) (rotate((uint)(x), (uint)(32-(n))))
#define CH(x,y,z)  (((x)&(y))^(~(x)&(z)))
#define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))
#define EP0(x) (ROTR(x,2)^ROTR(x,13)^ROTR(x,22))
#define EP1(x) (ROTR(x,6)^ROTR(x,11)^ROTR(x,25))
#define SIG0(x)(ROTR(x,7)^ROTR(x,18)^((x)>>3))
#define SIG1(x)(ROTR(x,17)^ROTR(x,19)^((x)>>10))

__constant uint K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

__kernel void mine(
    __global const uchar *pre_nonce,
    int                   pre_len,
    __global const uchar *post_nonce,
    int                   post_len,
    __global const uint  *target,
    long                  start_nonce,
    __global long        *result_nonce,
    __global int         *found
) {
    long nonce = start_nonce + get_global_id(0);
    if (*found) return;

    /* Build nonce string */
    uchar nonce_str[21];
    int nonce_len = 0;
    long tmp = nonce;
    if (tmp == 0) { nonce_str[nonce_len++] = '0'; }
    else {
        uchar rev[20]; int rlen = 0;
        while (tmp > 0) { rev[rlen++] = '0' + (tmp % 10); tmp /= 10; }
        for (int i = rlen-1; i >= 0; i--) nonce_str[nonce_len++] = rev[i];
    }

    int total = pre_len + nonce_len + post_len;

    /* Assemble message */
    uchar msg[256];
    for (int i = 0; i < pre_len;   i++) msg[i]                       = pre_nonce[i];
    for (int i = 0; i < nonce_len; i++) msg[pre_len + i]             = nonce_str[i];
    for (int i = 0; i < post_len;  i++) msg[pre_len + nonce_len + i] = post_nonce[i];

    /* SHA-256 padding */
    msg[total] = 0x80;
    int pad_end = total + 1;
    while ((pad_end % 64) != 56) { msg[pad_end++] = 0x00; }
    ulong bitlen = (ulong)total * 8;
    for (int i = 7; i >= 0; i--) { msg[pad_end++] = (bitlen >> (8*i)) & 0xff; }

    int num_blocks = pad_end / 64;

    uint h0=0x6a09e667, h1=0xbb67ae85, h2=0x3c6ef372, h3=0xa54ff53a;
    uint h4=0x510e527f, h5=0x9b05688c, h6=0x1f83d9ab, h7=0x5be0cd19;

    for (int blk = 0; blk < num_blocks; blk++) {
        uint w[64];
        for (int i = 0; i < 16; i++) {
            int o = blk*64 + i*4;
            w[i] = ((uint)msg[o]<<24)|((uint)msg[o+1]<<16)|((uint)msg[o+2]<<8)|msg[o+3];
        }
        for (int i = 16; i < 64; i++)
            w[i] = SIG1(w[i-2]) + w[i-7] + SIG0(w[i-15]) + w[i-16];

        uint a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,hh=h7;
        for (int i = 0; i < 64; i++) {
            uint t1 = hh + EP1(e) + CH(e,f,g) + K[i] + w[i];
            uint t2 = EP0(a) + MAJ(a,b,c);
            hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        h0+=a; h1+=b; h2+=c; h3+=d; h4+=e; h5+=f; h6+=g; h7+=hh;
    }

    uint hash[8] = {h0,h1,h2,h3,h4,h5,h6,h7};
    for (int i = 0; i < 8; i++) {
        if (hash[i] < target[i]) {
            if (atomic_cmpxchg(found, 0, 1) == 0) *result_nonce = nonce;
            return;
        }
        if (hash[i] > target[i]) return;
    }
    if (atomic_cmpxchg(found, 0, 1) == 0) *result_nonce = nonce;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _target_to_uint32(target_int: int) -> np.ndarray:
    """Convert integer target to 8 × uint32 big-endian array for the GPU."""
    b = target_int.to_bytes(32, "big")
    return np.frombuffer(b, dtype=np.uint32).byteswap()


def detect_gpu() -> str | None:
    """
    Return a human-readable GPU name if an OpenCL GPU is available, else None.
    Safe to call even if pyopencl is not installed.
    """
    if not OPENCL_AVAILABLE:
        return None
    try:
        for p in cl.get_platforms():
            for d in p.get_devices():
                if d.type == cl.device_type.GPU:
                    return d.name.strip()
    except Exception:
        pass
    return None


def _eta_str(target: int, total_hashes: int, hashrate: float) -> str:
    if hashrate <= 0:
        return "∞"
    expected  = (2 ** 256) // (target + 1)
    remaining = max(0, expected - total_hashes)
    eta_s     = remaining / hashrate
    if eta_s > 86400: return ">1 day"
    if eta_s > 3600:  return f"{eta_s/3600:.1f}h"
    if eta_s > 60:    return f"{eta_s/60:.1f}m"
    return f"{eta_s:.0f}s"


# ─────────────────────────────────────────────────────────────────────────────
#  GpuMiner — main class
# ─────────────────────────────────────────────────────────────────────────────
class GpuMiner:
    """
    Runs the GPU mining loop synchronously (call from a QThread or background thread).

    Parameters
    ----------
    address            : coinbase reward address
    server_url         : base URL of the blockchain node
    batch_size         : nonces per GPU dispatch  (1 000 000 = full speed)
    inter_batch_sleep  : seconds to sleep between batches  (0 = full speed)
    on_log(str)        : called with human-readable log messages
    on_stats(dict)     : called after every batch with mining stats
    on_found(idx, bh)  : called when a block is solved
    """

    def __init__(
        self,
        address:           str,
        server_url:        str,
        batch_size:        int   = 1_000_000,
        inter_batch_sleep: float = 0.0,
        on_log    = None,
        on_stats  = None,
        on_found  = None,
    ):
        self.address            = address
        self.server_url         = server_url
        self.batch_size         = batch_size
        self.inter_batch_sleep  = inter_batch_sleep
        self.on_log   = on_log   or (lambda msg: print(msg))
        self.on_stats = on_stats or (lambda d: None)
        self.on_found = on_found or (lambda idx, bh: None)

        self._stop_flag    = threading.Event()
        self._blocks_found = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def stop(self):
        """Signal the mining loop to exit (thread-safe)."""
        self._stop_flag.set()

    def run(self):
        """Blocking mining loop — run this in a QThread."""
        self._stop_flag.clear()

        if not OPENCL_AVAILABLE:
            self.on_log("✗ PyOpenCL not installed.  Run:  pip install pyopencl")
            return

        try:
            ctx, queue, prog, device = self._setup_gpu()
        except RuntimeError as e:
            self.on_log(f"✗ GPU init failed: {e}")
            return

        self.on_log(f"⛏  GPU: {device.name.strip()}")
        self.on_log(
            f"   Compute units: {device.max_compute_units}"
            f"  |  Batch: {self.batch_size:,}"
            f"  |  Sleep: {self.inter_batch_sleep*1000:.0f} ms"
        )

        mf = cl.mem_flags

        while not self._stop_flag.is_set():
            # ── 1. Fetch job ─────────────────────────────────────────────────
            try:
                resp = requests.get(f"{self.server_url}/get_mining_job", timeout=10)
                if resp.status_code != 200:
                    self.on_log("✗ Bad server response, retrying…")
                    self._stop_flag.wait(4)
                    continue
                job = resp.json()
            except Exception as e:
                self.on_log(f"✗ Server unreachable: {e}")
                self._stop_flag.wait(4)
                continue

            index         = job["index"]
            prev_hash     = job["previous_hash"]
            target        = job["difficulty_target"]
            job_txs       = job["transactions"]
            reward_amount = job["block_reward"]

            coinbase_tx = {
                "tx_id":   f"coinbase_{index}",
                "inputs":  [],
                "outputs": [{"address": self.address, "amount": reward_amount}],
            }
            block_transactions = [coinbase_tx] + job_txs
            timestamp = int(time.time())

            # ── 2. Build JSON split around the nonce field ───────────────────
            template = json.dumps({
                "difficulty_target": target,
                "index":             index,
                "nonce":             0,
                "previous_hash":     prev_hash,
                "timestamp":         timestamp,
                "transactions":      block_transactions,
            }, sort_keys=True)

            split      = template.index('"nonce": ') + len('"nonce": ')
            pre_bytes  = template[:split].encode()
            post_bytes = template[split + 1:].encode()

            self.on_log(
                f"▶ GPU Job #{index}  target={hex(target)[:18]}…  txs={len(job_txs)}"
            )

            # ── 3. Upload static buffers ─────────────────────────────────────
            pre_buf  = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                                 hostbuf=np.frombuffer(pre_bytes,  dtype=np.uint8))
            post_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                                 hostbuf=np.frombuffer(post_bytes, dtype=np.uint8))
            target_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                                   hostbuf=_target_to_uint32(target))

            result_nonce_buf = cl.Buffer(ctx, mf.READ_WRITE, size=8)   # int64
            found_buf        = cl.Buffer(ctx, mf.READ_WRITE, size=4)   # int32

            start_nonce  = np.int64(0)
            start_t      = time.time()
            poll_t       = start_t
            total_hashes = 0
            solved       = False
            last_hr      = 0.0

            # ── 4. GPU batch loop ────────────────────────────────────────────
            while not self._stop_flag.is_set():
                # Reset per-batch output buffers
                cl.enqueue_fill_buffer(queue, found_buf,        np.int32(0),  0, 4)
                cl.enqueue_fill_buffer(queue, result_nonce_buf, np.int64(-1), 0, 8)

                prog.mine(
                    queue, (self.batch_size,), None,
                    pre_buf,  np.int32(len(pre_bytes)),
                    post_buf, np.int32(len(post_bytes)),
                    target_buf,
                    start_nonce,
                    result_nonce_buf,
                    found_buf,
                )

                found_host  = np.zeros(1, dtype=np.int32)
                result_host = np.zeros(1, dtype=np.int64)
                cl.enqueue_copy(queue, found_host,  found_buf)
                cl.enqueue_copy(queue, result_host, result_nonce_buf)
                queue.finish()

                total_hashes += self.batch_size
                start_nonce  += np.int64(self.batch_size)

                now      = time.time()
                elapsed  = now - start_t
                last_hr  = total_hashes / elapsed if elapsed > 0 else 0

                self.on_stats({
                    "hashrate":     last_hr,
                    "nonce":        total_hashes,
                    "block":        index,
                    "blocks_found": self._blocks_found,
                    "eta":          _eta_str(target, total_hashes, last_hr),
                })

                if found_host[0]:
                    solved = True
                    break

                # External solve check every 5 s
                if now - poll_t > 5:
                    poll_t = now
                    try:
                        chain = requests.get(
                            f"{self.server_url}/blockchain", timeout=5
                        ).json()
                        if len(chain["chain"]) > index:
                            self.on_log(
                                f"⚡ Block {index} mined externally — new job"
                            )
                            break
                    except Exception:
                        pass

                # Throttle sleep (respects stop_flag)
                if self.inter_batch_sleep > 0:
                    self._stop_flag.wait(self.inter_batch_sleep)

            # ── 5. Submit solved block ───────────────────────────────────────
            if solved:
                winning_nonce = int(result_host[0])
                elapsed       = time.time() - start_t

                verify = json.dumps({
                    "difficulty_target": target,
                    "index":             index,
                    "nonce":             winning_nonce,
                    "previous_hash":     prev_hash,
                    "timestamp":         timestamp,
                    "transactions":      block_transactions,
                }, sort_keys=True)
                block_hash = hashlib.sha256(verify.encode()).hexdigest()

                self._blocks_found += 1
                self.on_log(
                    f"✦ SOLVED block {index}!  nonce={winning_nonce:,}"
                    f"  hash={block_hash[:20]}…  time={elapsed:.1f}s"
                )

                submission = {
                    "nonce":          winning_nonce,
                    "timestamp":      timestamp,
                    "reward_address": self.address,
                    "index":          index,
                    "transactions":   job_txs,
                }
                try:
                    res = requests.post(
                        f"{self.server_url}/submit_block",
                        json=submission, timeout=10,
                    )
                    self.on_log(f"  Server: {res.json().get('message', '?')}")
                except Exception as e:
                    self.on_log(f"  Submit error: {e}")

                self.on_found(index, block_hash)
                self.on_stats({
                    "hashrate":     last_hr,
                    "nonce":        total_hashes,
                    "block":        index,
                    "blocks_found": self._blocks_found,
                })
                self._stop_flag.wait(1)   # brief pause before next job

    # ── Private ───────────────────────────────────────────────────────────────

    def _setup_gpu(self):
        for p in cl.get_platforms():
            for d in p.get_devices():
                if d.type == cl.device_type.GPU:
                    ctx   = cl.Context([d])
                    queue = cl.CommandQueue(ctx)
                    prog  = cl.Program(ctx, KERNEL_SOURCE).build()
                    return ctx, queue, prog, d
        raise RuntimeError(
            "No OpenCL GPU found — install GPU drivers and pyopencl."
        )
