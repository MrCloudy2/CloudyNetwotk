import pyopencl as cl
import numpy as np
import hashlib
import json
import time
import requests
import struct

# --- CONFIGURATION ---
SERVER_URL     = "http://cloudy.freemyip.com:8765"
REWARD_ADDRESS = "382e55d49815ac0deebdc665107ca3617e51fa6c487be25e73c3603c555e2bfc"
BATCH_SIZE     = 1_000_000   # nonces per GPU batch — tune this up/down

# --- OpenCL SHA-256 GPU Kernel ---
KERNEL_SOURCE = """
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

void sha256_block(uint *w, uint *hash_out) {
    uint h0=0x6a09e667, h1=0xbb67ae85, h2=0x3c6ef372, h3=0xa54ff53a;
    uint h4=0x510e527f, h5=0x9b05688c, h6=0x1f83d9ab, h7=0x5be0cd19;

    uint w2[64];
    for (int i = 0; i < 16; i++) w2[i] = w[i];
    for (int i = 16; i < 64; i++)
        w2[i] = SIG1(w2[i-2]) + w2[i-7] + SIG0(w2[i-15]) + w2[i-16];

    uint a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,hh=h7;
    for (int i = 0; i < 64; i++) {
        uint t1 = hh + EP1(e) + CH(e,f,g) + K[i] + w2[i];
        uint t2 = EP0(a) + MAJ(a,b,c);
        hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    hash_out[0]=h0+a; hash_out[1]=h1+b; hash_out[2]=h2+c; hash_out[3]=h3+d;
    hash_out[4]=h4+e; hash_out[5]=h5+f; hash_out[6]=h6+g; hash_out[7]=h7+hh;
}

// Write uint as big-endian into buf at offset, return new offset
int write_uint_be(uchar *buf, int off, uint val) {
    buf[off+0] = (val >> 24) & 0xff;
    buf[off+1] = (val >> 16) & 0xff;
    buf[off+2] = (val >>  8) & 0xff;
    buf[off+3] = (val      ) & 0xff;
    return off + 4;
}

__kernel void mine(
    __global const uchar *pre_nonce,   // JSON bytes before nonce
    int                   pre_len,
    __global const uchar *post_nonce,  // JSON bytes after nonce
    int                   post_len,
    __global const uint  *target,      // 8 x uint32 big-endian target
    long                  start_nonce,
    __global long        *result_nonce, // -1 if not found
    __global int         *found
) {
    long nonce = start_nonce + get_global_id(0);

    // Already found by another thread
    if (*found) return;

    // Build nonce string
    uchar nonce_str[21];
    int nonce_len = 0;
    long tmp = nonce;
    if (tmp == 0) { nonce_str[nonce_len++] = '0'; }
    else {
        uchar rev[20]; int rlen = 0;
        while (tmp > 0) { rev[rlen++] = '0' + (tmp % 10); tmp /= 10; }
        for (int i = rlen-1; i >= 0; i--) nonce_str[nonce_len++] = rev[i];
    }

    // Assemble full message: pre + nonce_str + post
    int total = pre_len + nonce_len + post_len;

    // SHA-256 pad into 512-bit (64-byte) blocks
    // Max message we expect is ~300 bytes -> 2 blocks max
    uchar msg[256];
    for (int i = 0; i < pre_len;   i++) msg[i]             = pre_nonce[i];
    for (int i = 0; i < nonce_len; i++) msg[pre_len + i]   = nonce_str[i];
    for (int i = 0; i < post_len;  i++) msg[pre_len + nonce_len + i] = post_nonce[i];

    // Padding
    msg[total] = 0x80;
    int pad_end = total + 1;
    while ((pad_end % 64) != 56) { msg[pad_end++] = 0x00; }
    ulong bitlen = (ulong)total * 8;
    for (int i = 7; i >= 0; i--) { msg[pad_end++] = (bitlen >> (8*i)) & 0xff; }

    int num_blocks = pad_end / 64;

    // SHA-256 state
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

    // Compare hash <= target (big-endian uint comparison)
    uint hash[8] = {h0,h1,h2,h3,h4,h5,h6,h7};
    for (int i = 0; i < 8; i++) {
        if (hash[i] < target[i]) {
            if (atomic_cmpxchg(found, 0, 1) == 0)
                *result_nonce = nonce;
            return;
        }
        if (hash[i] > target[i]) return;
    }
    // exact match
    if (atomic_cmpxchg(found, 0, 1) == 0)
        *result_nonce = nonce;
}
"""


def target_to_uint8(target_int):
    """Convert integer target to 32 big-endian bytes."""
    return target_int.to_bytes(32, 'big')

def target_to_uint32(target_int):
    """Convert integer target to 8 x uint32 array (big-endian)."""
    b = target_int.to_bytes(32, 'big')
    return np.frombuffer(b, dtype=np.uint32).byteswap()  # GPU needs big-endian uint32


def setup_gpu():
    platforms = cl.get_platforms()
    for p in platforms:
        for d in p.get_devices():
            if d.type == cl.device_type.GPU:
                ctx = cl.Context([d])
                queue = cl.CommandQueue(ctx)
                prog = cl.Program(ctx, KERNEL_SOURCE).build()
                print(f"GPU: {d.name}  |  Compute units: {d.max_compute_units}  |  Max workgroup: {d.max_work_group_size}")
                return ctx, queue, prog, d
    raise RuntimeError("No GPU found!")


def mine():
    print("Setting up GPU...")
    ctx, queue, prog, device = setup_gpu()
    print(f"Starting GPU miner — {REWARD_ADDRESS}\n")

    mf = cl.mem_flags

    while True:
        # 1. Fetch job
        try:
            response = requests.get(f"{SERVER_URL}/get_mining_job", timeout=10)
            if response.status_code != 200:
                print(f"\r[-] Bad server response, retrying...    ", end="", flush=True)
                time.sleep(2)
                continue
            job = response.json()
        except Exception as e:
            print(f"\r[-] Server unreachable: {e}    ", end="", flush=True)
            time.sleep(5)
            continue

        index         = job["index"]
        prev_hash     = job["previous_hash"]
        target        = job["difficulty_target"]
        job_txs       = job["transactions"]
        reward_amount = job["block_reward"]

        coinbase_tx = {
            "tx_id":   f"coinbase_{index}",
            "inputs":  [],
            "outputs": [{"address": REWARD_ADDRESS, "amount": reward_amount}],
        }
        block_transactions = [coinbase_tx] + job_txs
        timestamp = int(time.time())

        # Pre-build JSON split around nonce
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

        print(f"\r  Block #{index} | difficulty={hex(target)} | txs={len(job_txs)}")

        # Upload static buffers to GPU
        pre_buf  = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(pre_bytes,  dtype=np.uint8))
        post_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=np.frombuffer(post_bytes, dtype=np.uint8))
        target_arr = target_to_uint32(target)
        target_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=target_arr)

        result_nonce_buf = cl.Buffer(ctx, mf.READ_WRITE, size=8)   # int64
        found_buf        = cl.Buffer(ctx, mf.READ_WRITE, size=4)   # int32

        start_nonce = np.int64(0)
        start_t     = time.time()
        poll_t      = start_t
        total_hashes = 0
        solved      = False

        while True:
            # Reset found flag and result each batch
            cl.enqueue_fill_buffer(queue, found_buf,        np.int32(0),  0, 4)
            cl.enqueue_fill_buffer(queue, result_nonce_buf, np.int64(-1), 0, 8)

            prog.mine(
                queue, (BATCH_SIZE,), None,
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

            total_hashes += BATCH_SIZE
            start_nonce  += np.int64(BATCH_SIZE)

            now      = time.time()
            elapsed  = now - start_t
            hashrate = total_hashes / elapsed if elapsed > 0 else 0

            expected  = (2 ** 256) // (target + 1)
            remaining = max(0, expected - total_hashes)
            eta_s     = remaining / hashrate if hashrate > 0 else float("inf")

            if eta_s > 86400:   eta_str = ">1 day"
            elif eta_s > 3600:  eta_str = f"{eta_s/3600:.1f}h"
            elif eta_s > 60:    eta_str = f"{eta_s/60:.1f}m"
            else:               eta_str = f"{eta_s:.0f}s"

            if hashrate >= 1_000_000_000: hr_str = f"{hashrate/1e9:.2f} GH/s"
            elif hashrate >= 1_000_000:   hr_str = f"{hashrate/1e6:.1f} MH/s"
            else:                         hr_str = f"{hashrate/1e3:.1f} kH/s"

            print(
                f"\r  Block #{index} | {hr_str} | Nonces: {total_hashes:,} | "
                f"Elapsed: {elapsed:.0f}s | ETA: {eta_str}    ",
                end="", flush=True
            )

            if found_host[0]:
                winning_nonce = int(result_host[0])
                solved = True
                break

            # External solve check every 5s
            if now - poll_t > 5:
                poll_t = now
                try:
                    chain = requests.get(f"{SERVER_URL}/blockchain", timeout=5).json()
                    if len(chain["chain"]) > index:
                        print(f"\n[!] Block {index} solved externally — getting new job...")
                        break
                except Exception:
                    pass

        if solved:
            elapsed = time.time() - start_t
            # Verify hash
            verify = json.dumps({
                "difficulty_target": target,
                "index":             index,
                "nonce":             winning_nonce,
                "previous_hash":     prev_hash,
                "timestamp":         timestamp,
                "transactions":      block_transactions,
            }, sort_keys=True)
            block_hash = hashlib.sha256(verify.encode()).hexdigest()

            print(f"\n[+] SOLVED block {index}! nonce={winning_nonce:,}  hash={block_hash}  time={elapsed:.1f}s")

            submission = {
                "nonce":          winning_nonce,
                "timestamp":      timestamp,
                "reward_address": REWARD_ADDRESS,
                "index":          index,
                "transactions":   job_txs,
            }
            try:
                res = requests.post(f"{SERVER_URL}/submit_block", json=submission, timeout=10)
                print(f"[*] Server: {res.json().get('message')}")
            except Exception as e:
                print(f"[-] Submit failed: {e}")

            time.sleep(1)


if __name__ == "__main__":
    mine()
