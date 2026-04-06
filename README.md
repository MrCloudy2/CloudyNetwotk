# CloudyCoin Mining Guide

A complete reference for building a miner that works with the CloudyCoin node.

---

## Table of Contents

- [Overview](#overview)
- [Address Format](#address-format)
- [Rate Limits](#rate-limits)
- [Mining Protocol](#mining-protocol)
  - [Step 1 — Get a Mining Job](#step-1--get-a-mining-job)
  - [Step 2 — The 112-Byte Block Header](#step-2--the-112-byte-block-header)
  - [Step 3 — Proof of Work](#step-3--proof-of-work)
  - [Step 4 — Submit a Solved Block](#step-4--submit-a-solved-block)
- [Error Responses](#error-responses)
- [Difficulty & Adjustment](#difficulty--adjustment)
- [Jobs & Expiry](#jobs--expiry)
- [Minimal Python Example](#minimal-python-example)
- [Tips for GPU Miners](#tips-for-gpu-miners)

---

## Overview

CloudyCoin uses **SHA-256d proof-of-work** (double SHA-256, identical to Bitcoin's algorithm) over a **112-byte binary block header**. The key difference from Bitcoin is that the nonce field is **64 bits** (not 32), eliminating nonce exhaustion even at very high hashrates.

The server exposes a simple HTTP API. All responses are **binary** (`application/octet-stream`), not JSON. Multi-byte integers are **big-endian** unless noted otherwise.

**Node URL:** `http://cloudy.freemyip.com:8765`

---

## Address Format

Your mining reward address is a **64-character lowercase hex string** — the SHA-256 hash of your raw public key bytes.

```python
import hashlib
address = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()
# e.g. "a3f1c8..." (64 hex chars)
```

This address goes in the `miner_address` query parameter of every job request. The server builds a coinbase transaction that pays `1 COIN` to this address when your block is accepted.

---

## Rate Limits

The server enforces rate limits per IP:

| Endpoint | Limit |
|---|---|
| All endpoints (default) | **30 requests / minute** |
| `/add_transaction` | **10 requests / minute** |

If you exceed these limits you will receive HTTP 429. Design your miner to poll conservatively:

- Fetch a new job only after solving or when the current job expires (~10 min TTL)
- Poll `/blockchain` to detect external solves no more than once every 10 seconds
- Do not hammer the server on error — use exponential backoff

---

## Mining Protocol

The full flow is four steps:

```
GET /get_mining_job  →  136-byte job blob
         ↓
   Hash the header, iterate nonce
         ↓
   hash ≤ target  →  POST /submit_block  →  36-byte confirmation
```

---

### Step 1 — Get a Mining Job

```
GET /get_mining_job?miner_address=<64-char-hex>
```

**Success — HTTP 200, 136 bytes:**

```
Offset  Size  Type      Field
──────  ────  ────────  ─────────────────────────────────────────
     0    16  bytes     job_id  (opaque — send back verbatim on submit)
    16     4  uint32 BE block_index  (height of the block you're mining)
    20   112  bytes     block header  (see below — nonce starts at 0)
   132     4  uint32 BE tx_count  (coinbase + up to 5 mempool txs)
```

Parse it like this:

```python
import struct, requests

resp = requests.get(
    "http://cloudy.freemyip.com:8765/get_mining_job",
    params={"miner_address": your_address},
)
data = resp.content           # 136 bytes

job_id      = data[0:16]      # keep this, send it back with the solution
block_index = struct.unpack(">I", data[16:20])[0]
header      = bytearray(data[20:132])   # mutable — you'll overwrite the nonce
tx_count    = struct.unpack(">I", data[132:136])[0]
```

---

### Step 2 — The 112-Byte Block Header

The header has a fixed layout. All fields are big-endian.

```
Offset  Size  Type       Field
──────  ────  ─────────  ─────────────────────────────────────────────
     0     4  uint32     version
     4    32  bytes      previous_block_hash  (hex-encoded, raw bytes)
    36    32  bytes      merkle_root  (raw bytes)
    68     4  uint32     timestamp  (Unix seconds)
    72    32  bytes      difficulty_target  (256-bit big-endian integer)
   104     8  uint64 BE  nonce  ← YOU WRITE THIS FIELD
```

**Total: 112 bytes.**

The server fills everything except the nonce (which starts at 0). You iterate the nonce and rehash.

Extract the target for comparison:

```python
target = int.from_bytes(header[72:104], "big")
```

---

### Step 3 — Proof of Work

For each nonce value:

1. Write the nonce as a **big-endian uint64** into bytes `[104:112]` of the header.
2. Compute `SHA256d = SHA256(SHA256(header))` over all 112 bytes.
3. Interpret the 32-byte result as a big-endian 256-bit integer.
4. If `hash_value ≤ target`, you have a valid block — go to Step 4.
5. Otherwise increment the nonce and repeat.

```python
import hashlib, struct

def mine(header, target):
    """Returns winning nonce, or None if exhausted."""
    header = bytearray(header)
    for nonce in range(2**64):
        struct.pack_into(">Q", header, 104, nonce)
        digest = hashlib.sha256(hashlib.sha256(header).digest()).digest()
        if int.from_bytes(digest, "big") <= target:
            return nonce
    return None   # 64-bit space exhausted — request a new job
```

Because the nonce is 64 bits, exhaustion is practically impossible at any realistic hashrate. The server will issue a new job with a fresh timestamp periodically anyway (10-minute TTL).

**Do not modify any other field in the header.** The server verifies the merkle root, previous hash, and timestamp independently. Tampering will cause rejection.

---

### Step 4 — Submit a Solved Block

```
POST /submit_block
Content-Type: application/octet-stream
Body: 128 bytes
```

```
Offset  Size  Field
──────  ────  ──────────────────────────────────────────────
     0    16  job_id  (exactly as received from /get_mining_job)
    16   112  solved header  (bytes [104:112] contain the winning nonce)
```

```python
body = job_id + bytes(solved_header)   # 16 + 112 = 128 bytes
resp = requests.post(
    "http://cloudy.freemyip.com:8765/submit_block",
    data=body,
    headers={"Content-Type": "application/octet-stream"},
)
```

**Success — HTTP 201, 36 bytes:**

```
Offset  Size  Field
──────  ────  ──────────────────────
     0     4  uint32 BE  block_index  (confirmed height)
     4    32  bytes      block_hash   (raw, hex-encode for display)
```

```python
if resp.status_code == 201:
    block_index = struct.unpack(">I", resp.content[0:4])[0]
    block_hash  = resp.content[4:36].hex()
    print(f"Block {block_index} accepted! Hash: {block_hash}")
```

**Rejection reasons (HTTP 400):**

| Message | Cause |
|---|---|
| `Unknown or expired job_id` | Job TTL elapsed (10 min) or already submitted |
| `Stale job` | Another miner found this block first |
| `Insufficient proof of work` | Your hash is above the target |
| `Merkle root mismatch` | You modified the transactions |
| `Block timestamp must be greater than previous block` | Timestamp check failed |

---

## Error Responses

All error responses use the same binary envelope regardless of HTTP status code:

```
Offset  Size  Field
──────  ────  ─────────────────────────────
     0     2  uint16 BE  message_length (N)
     2     N  UTF-8 string
```

```python
def decode_error(data: bytes) -> str:
    length = struct.unpack(">H", data[0:2])[0]
    return data[2:2+length].decode("utf-8")
```

---

## Difficulty & Adjustment

The difficulty target is a **256-bit big-endian integer** embedded in the block header at bytes `[72:104]`. A valid block hash must be numerically less than or equal to this value.

The target is adjusted every block using the last 10 blocks:

- **Target block time:** 120 seconds (2 minutes)
- **Adjustment range:** ±25% per adjustment (clamped)
- **Minimum target (hardest):** `GENESIS_TARGET >> 32`
- **Maximum target (easiest):** `0x0000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF`

The target embedded in the header you receive from `/get_mining_job` is the correct one for the block you are mining. You do not need to calculate it yourself.

To display difficulty as "expected hashes to solve":

```python
expected_hashes = (2**256) // (target + 1)
```

---

## Jobs & Expiry

- Each job has a **10-minute TTL**. After that the job_id is invalid and submission returns `Unknown or expired job_id`.
- The server issues a fresh timestamp with each job. If you exhaust the 64-bit nonce space (practically impossible), simply request a new job — the new timestamp gives a different block header and a new nonce space.
- If another miner submits a valid block at the same height while you are working, the server will reject your submission with `Stale job`. Always check for external solves periodically (recommended: every 10 seconds via a lightweight poll — see rate limits).

**Detecting an external solve** without downloading the whole chain:

```python
resp = requests.get("http://cloudy.freemyip.com:8765/blockchain")
# Parse the first 4 bytes for chain length
chain_length = struct.unpack(">I", resp.content[0:4])[0]
if chain_length > block_index:
    # Someone else found it — request a new job
```

---

## Minimal Python Example

A complete, working single-threaded CPU miner:

```python
import hashlib, struct, requests

NODE     = "http://cloudy.freemyip.com:8765"
ADDRESS  = "your_64_char_hex_address_here"

def decode_error(data):
    n = struct.unpack(">H", data[:2])[0]
    return data[2:2+n].decode()

while True:
    # 1. Get job
    r = requests.get(f"{NODE}/get_mining_job", params={"miner_address": ADDRESS})
    if r.status_code != 200:
        print("Error:", decode_error(r.content))
        continue

    job_id      = r.content[0:16]
    block_index = struct.unpack(">I", r.content[16:20])[0]
    header      = bytearray(r.content[20:132])
    target      = int.from_bytes(header[72:104], "big")

    print(f"Mining block {block_index}  target={target.to_bytes(32,'big').hex()[:16]}…")

    # 2. Mine
    for nonce in range(2**64):
        struct.pack_into(">Q", header, 104, nonce)
        digest = hashlib.sha256(hashlib.sha256(header).digest()).digest()
        if int.from_bytes(digest, "big") <= target:
            print(f"  Found nonce={nonce}  hash={digest.hex()[:20]}…")
            break

    # 3. Submit
    body = job_id + bytes(header)
    r = requests.post(f"{NODE}/submit_block", data=body,
                      headers={"Content-Type": "application/octet-stream"})
    if r.status_code == 201:
        idx  = struct.unpack(">I", r.content[0:4])[0]
        hash = r.content[4:36].hex()
        print(f"  Accepted: block #{idx}  {hash}")
    else:
        print(f"  Rejected: {decode_error(r.content)}")
```

---

## Tips for GPU Miners

**Midstate optimisation (saves ~33% of SHA-256 work)**

Because bytes `[0:64]` of the header never change within a job, you can pre-compute the SHA-256 compression of the first block on the CPU once per job. Each GPU thread then only needs to run:

- Pass 1, block 2: compress `header[64:112]` + padding (bitlen = 896 = `0x380`)
- Pass 2: compress the 32-byte inner hash + padding (bitlen = 256 = `0x100`)

This is the "midstate" trick used by all serious SHA-256 miners.

**Pass 1, block 2 layout (64 bytes = 16 × uint32 big-endian):**

```
W[ 0.. 9]  header[64:104]   — 10 words, static per job
W[10]      nonce high 32 bits
W[11]      nonce low  32 bits
W[12]      0x80000000       — SHA-256 padding bit
W[13..14]  0x00000000
W[15]      0x00000380       — bit-length 896
```

**Nonce is big-endian uint64, split into two uint32 words:**

```c
uint64_t nonce = start + thread_id;
uint32_t nonce_hi = (uint32_t)(nonce >> 32);
uint32_t nonce_lo = (uint32_t)(nonce & 0xFFFFFFFF);
// W[10] = nonce_hi, W[11] = nonce_lo
```

**Target comparison is big-endian MSW-first:**

The hash output from the SHA-256 compression is in big-endian word order. Compare `hash[0]` (most significant word) first — most nonces fail on this single comparison, making the early-exit optimisation very effective.

```c
// Fast early exit — ~99.9% of nonces fail here
if (hash[0] > target[0]) return;
// Only if MSW matches, check remaining words...
```

**Workgroup / block size matters:** Calibrate against your specific GPU. For SHA-256 mining, sizes of 128 or 256 threads per block typically work well on NVIDIA hardware, but the optimal value depends on the GPU's register file size and occupancy. Always benchmark.
