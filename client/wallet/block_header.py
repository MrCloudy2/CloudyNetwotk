"""
block_header.py — CloudyCoin 112-byte binary block header
==========================================================

Layout (all fields big-endian, total = 112 bytes):

  Offset  Size  Type      Field
  ──────  ────  ────────  ──────────────────────────────────────────
       0     4  uint32    version
       4    32  bytes     previous_hash   (raw 256-bit digest)
      36    32  bytes     merkle_root     (raw 256-bit digest)
      68     4  uint32    timestamp       (Unix seconds)
      72    32  bytes     difficulty_target (raw 256-bit integer, big-endian)
     104     8  uint64    nonce
  ──────  ────
     112  bytes  TOTAL

Design notes
────────────
version (4B uint32)
    Allows future hard-forks to change validation rules without breaking
    existing parsers.  Currently always HEADER_VERSION = 1.

previous_hash / merkle_root (32B each)
    Stored as raw bytes — exactly what SHA-256 produces.  Compared to
    encoding as hex strings inside JSON, this saves 64 bytes per field.

timestamp (4B uint32)
    Sufficient until year 2106.  Matches Bitcoin's field width.

difficulty_target (32B)
    Bitcoin compresses this into a 4-byte "nBits" mantissa/exponent format.
    CloudyCoin stores the full 256-bit target.  This avoids the ~24-bit
    precision loss of nBits and the associated "target expansion" bugs,
    at the cost of 28 extra bytes per header.

nonce (8B uint64)
    Bitcoin's 32-bit nonce (~4 billion values) is exhausted by a modern
    GPU in under a second.  A 64-bit nonce (~1.8 × 10¹⁹ values) gives a
    GPU miner enough search space within a single timestamp second,
    removing the need to roll the timestamp or extra-nonce tricks for
    home-scale mining.

Hash function
─────────────
calculate_hash_binary() hashes the 112-byte header with double-SHA256
(SHA256d), matching Bitcoin's block hash primitive.  The result is returned
as a hex string so it integrates with existing chain storage and comparison
code in server1.py.

Public API
──────────
  HEADER_VERSION          int      current protocol version constant
  HEADER_SIZE             int      always 112
  pack_header(...)        → bytes  112-byte struct
  unpack_header(data)     → dict   all six fields
  calculate_hash_binary(block, transactions) → str   64-char hex SHA256d
"""

import hashlib
import struct
import time

# ── Constants ────────────────────────────────────────────────────────────────

HEADER_VERSION = 1
HEADER_SIZE    = 112          # bytes

# struct format string: big-endian, no padding
# >  = big-endian
# I  = uint32   (4B)  — version
# 32s= 32 bytes       — previous_hash
# 32s= 32 bytes       — merkle_root
# I  = uint32   (4B)  — timestamp
# 32s= 32 bytes       — difficulty_target
# Q  = uint64   (8B)  — nonce
_HEADER_FMT  = ">I 32s 32s I 32s Q"
_HEADER_STRUCT = struct.Struct(_HEADER_FMT)

assert _HEADER_STRUCT.size == HEADER_SIZE, (
    f"BUG: header struct is {_HEADER_STRUCT.size}B, expected {HEADER_SIZE}B"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hex_to_32b(hex_str: str, field_name: str) -> bytes:
    """Convert a 64-char hex string to exactly 32 raw bytes."""
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        raise ValueError(f"{field_name}: not valid hex — {hex_str!r}")
    if len(raw) != 32:
        raise ValueError(
            f"{field_name}: expected 32 bytes (64 hex chars), got {len(raw)}"
        )
    return raw


def _int_to_32b(value: int, field_name: str) -> bytes:
    """Encode a 256-bit non-negative integer as 32 big-endian bytes."""
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name}: must be a non-negative integer, got {value!r}")
    try:
        return value.to_bytes(32, byteorder="big")
    except OverflowError:
        raise ValueError(f"{field_name}: value {value} exceeds 256 bits")


def _sha256d(data: bytes) -> bytes:
    """Double-SHA256: SHA256(SHA256(data))."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# ── Public: pack ─────────────────────────────────────────────────────────────

def pack_header(
    version:           int,
    previous_hash:     str,   # 64-char hex
    merkle_root:       str,   # 64-char hex
    timestamp:         int,   # Unix seconds
    difficulty_target: int,   # 256-bit integer
    nonce:             int,   # 64-bit integer
) -> bytes:
    """
    Pack the six block-header fields into a 112-byte binary blob.

    Parameters
    ----------
    version : int
        Protocol version (use HEADER_VERSION).
    previous_hash : str
        64-char hex string of the previous block's hash.
    merkle_root : str
        64-char hex string of the block's Merkle Root
        (from merkle.merkle_root_hex()).
    timestamp : int
        Unix timestamp (seconds).  Must fit in uint32.
    difficulty_target : int
        Current mining target as a 256-bit integer
        (from Blockchain.calculate_next_target()).
    nonce : int
        64-bit miner nonce.

    Returns
    -------
    bytes
        Exactly 112 bytes, ready to send to a GPU miner or to hash.
    """
    if not 0 <= version <= 0xFFFF_FFFF:
        raise ValueError(f"version {version} out of uint32 range")
    if not 0 <= timestamp <= 0xFFFF_FFFF:
        raise ValueError(f"timestamp {timestamp} out of uint32 range")
    if not 0 <= nonce <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError(f"nonce {nonce} out of uint64 range")

    raw_prev   = _hex_to_32b(previous_hash,     "previous_hash")
    raw_merkle = _hex_to_32b(merkle_root,        "merkle_root")
    raw_target = _int_to_32b(difficulty_target,  "difficulty_target")

    return _HEADER_STRUCT.pack(
        version,
        raw_prev,
        raw_merkle,
        timestamp,
        raw_target,
        nonce,
    )


# ── Public: unpack ───────────────────────────────────────────────────────────

def unpack_header(data: bytes) -> dict:
    """
    Unpack 112 bytes back into a dictionary of typed Python values.

    Parameters
    ----------
    data : bytes
        Exactly 112 bytes as produced by pack_header().

    Returns
    -------
    dict with keys:
        version           int
        previous_hash     str  (64-char hex)
        merkle_root       str  (64-char hex)
        timestamp         int
        difficulty_target int  (256-bit integer)
        nonce             int
    """
    if len(data) != HEADER_SIZE:
        raise ValueError(
            f"Expected {HEADER_SIZE} bytes, got {len(data)}"
        )

    version, raw_prev, raw_merkle, timestamp, raw_target, nonce = (
        _HEADER_STRUCT.unpack(data)
    )

    return {
        "version":           version,
        "previous_hash":     raw_prev.hex(),
        "merkle_root":       raw_merkle.hex(),
        "timestamp":         timestamp,
        "difficulty_target": int.from_bytes(raw_target, byteorder="big"),
        "nonce":             nonce,
    }


# ── Public: hash (drop-in replacement for Blockchain.calculate_hash) ─────────

def calculate_hash_binary(block: dict, transactions: list) -> str:
    """
    Compute the block hash using the binary header format.

    This is the drop-in replacement for the JSON-based calculate_hash()
    in server1.py.  It:
      1. Builds the Merkle Root from the block's binary-encoded transactions.
      2. Packs the 112-byte header.
      3. Returns SHA256d(header) as a 64-char hex string — same type as
         the old calculate_hash() so no other code needs to change.

    Parameters
    ----------
    block : dict
        A block dict as stored in Blockchain.chain.  Must contain:
        index, previous_hash, timestamp, difficulty_target, nonce.
        The "version" key is optional; defaults to HEADER_VERSION.
    transactions : list
        The block's transaction list (dicts).  Passed separately so the
        caller can build the coinbase before calling this, exactly as
        submit_block() already does.

    Returns
    -------
    str
        64-char lowercase hex string.
    """
    # Import here to avoid circular imports if modules are used independently
    from merkle   import merkle_root_hex
    from tx_codec import tx_to_bytes

    if transactions:
        tx_bytes_list = [tx_to_bytes(tx) for tx in transactions]
        root_hex = merkle_root_hex(tx_bytes_list)
    else:
        # Empty block — use the null hash (shouldn't happen in practice,
        # but keeps the function safe)
        root_hex = "0" * 64

    header_bytes = pack_header(
        version           = block.get("version", HEADER_VERSION),
        previous_hash     = block["previous_hash"],
        merkle_root       = root_hex,
        timestamp         = block["timestamp"],
        difficulty_target = block["difficulty_target"],
        nonce             = block["nonce"],
    )

    return _sha256d(header_bytes).hex()


# ── Self-test  (python block_header.py) ──────────────────────────────────────

if __name__ == "__main__":
    import os

    def _ok(label, result):
        mark = "PASS ✓" if result else "FAIL ✗"
        print(f"  [{mark}] {label}")
        return result

    all_pass = True
    print("=" * 62)
    print("block_header.py — self-test")
    print("=" * 62)

    # ── 1. Round-trip ────────────────────────────────────────────────
    print("\n[1] Pack / unpack round-trip")
    prev_hash  = os.urandom(32).hex()
    merkle     = os.urandom(32).hex()
    ts         = int(time.time())
    target     = 0x0000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
    nonce      = 0xDEADBEEFCAFEBABE

    packed = pack_header(HEADER_VERSION, prev_hash, merkle, ts, target, nonce)
    all_pass &= _ok(f"packed size is {HEADER_SIZE}B", len(packed) == HEADER_SIZE)

    unpacked = unpack_header(packed)
    all_pass &= _ok("version round-trips",           unpacked["version"]           == HEADER_VERSION)
    all_pass &= _ok("previous_hash round-trips",     unpacked["previous_hash"]     == prev_hash)
    all_pass &= _ok("merkle_root round-trips",       unpacked["merkle_root"]       == merkle)
    all_pass &= _ok("timestamp round-trips",         unpacked["timestamp"]         == ts)
    all_pass &= _ok("difficulty_target round-trips", unpacked["difficulty_target"] == target)
    all_pass &= _ok("nonce round-trips",             unpacked["nonce"]             == nonce)

    # ── 2. Hash is deterministic ─────────────────────────────────────
    print("\n[2] Hash determinism")
    hash_a = _sha256d(packed).hex()
    hash_b = _sha256d(packed).hex()
    all_pass &= _ok("same input → same hash", hash_a == hash_b)
    all_pass &= _ok("hash is 64 hex chars",   len(hash_a) == 64)

    # ── 3. Single-bit nonce change flips the hash ────────────────────
    print("\n[3] Avalanche effect")
    packed_nonce1 = pack_header(HEADER_VERSION, prev_hash, merkle, ts, target, nonce)
    packed_nonce2 = pack_header(HEADER_VERSION, prev_hash, merkle, ts, target, nonce + 1)
    all_pass &= _ok("nonce+1 produces a different hash",
                    _sha256d(packed_nonce1) != _sha256d(packed_nonce2))

    # ── 4. Field layout — check byte offsets manually ────────────────
    print("\n[4] Field offsets")
    known_prev = bytes(range(32))          # 0x00 0x01 … 0x1F
    packed_offsets = pack_header(1, known_prev.hex(), "ab" * 32, 0x12345678, 0, 0)
    all_pass &= _ok("version at offset 0 = 1",
                    struct.unpack_from(">I", packed_offsets, 0)[0] == 1)
    all_pass &= _ok("previous_hash at offset 4",
                    packed_offsets[4:36] == known_prev)
    all_pass &= _ok("timestamp at offset 68",
                    struct.unpack_from(">I", packed_offsets, 68)[0] == 0x12345678)
    all_pass &= _ok("nonce at offset 104 = 0",
                    struct.unpack_from(">Q", packed_offsets, 104)[0] == 0)

    # ── 5. Bad-input guards ──────────────────────────────────────────
    print("\n[5] Input validation")
    def _raises(fn, *a, **kw):
        try:
            fn(*a, **kw)
            return False
        except (ValueError, struct.error):
            return True

    all_pass &= _ok("rejects short previous_hash",
                    _raises(pack_header, 1, "deadbeef", merkle, ts, target, 0))
    all_pass &= _ok("rejects non-hex previous_hash",
                    _raises(pack_header, 1, "zz" * 32, merkle, ts, target, 0))
    all_pass &= _ok("rejects negative target",
                    _raises(pack_header, 1, prev_hash, merkle, ts, -1, 0))
    all_pass &= _ok("rejects oversized target (>256 bit)",
                    _raises(pack_header, 1, prev_hash, merkle, ts, 2**256, 0))
    all_pass &= _ok("rejects wrong-length unpack input",
                    _raises(unpack_header, b"\x00" * 80))

    # ── 6. calculate_hash_binary smoke-test ──────────────────────────
    print("\n[6] calculate_hash_binary (integration)")
    # Minimal fake block dict
    fake_block = {
        "index":            1,
        "previous_hash":    prev_hash,
        "timestamp":        ts,
        "difficulty_target": target,
        "nonce":            nonce,
    }
    fake_txs = [
        {
            "tx_id":    "coinbase_1",
            "inputs":   [],
            "outputs":  [{"address": os.urandom(32).hex(), "amount": 1}],
        }
    ]
    try:
        h = calculate_hash_binary(fake_block, fake_txs)
        all_pass &= _ok("returns 64-char hex",            len(h) == 64)
        all_pass &= _ok("deterministic on same input",
                        h == calculate_hash_binary(fake_block, fake_txs))
        # Changing nonce changes hash
        fake_block["nonce"] += 1
        all_pass &= _ok("different nonce → different hash",
                        h != calculate_hash_binary(fake_block, fake_txs))
    except ImportError as exc:
        print(f"  [SKIP] merkle/tx_codec not on path: {exc}")

    print("\n" + "=" * 62)
    print("All tests passed!" if all_pass else "SOME TESTS FAILED.")
    print()

    # ── Field map ────────────────────────────────────────────────────
    print("Header field map:")
    fields = [
        ("version",           0,   4,  "uint32  be"),
        ("previous_hash",     4,  36,  "32 raw bytes"),
        ("merkle_root",      36,  68,  "32 raw bytes"),
        ("timestamp",        68,  72,  "uint32  be"),
        ("difficulty_target",72, 104,  "32 raw bytes (256-bit int be)"),
        ("nonce",           104, 112,  "uint64  be"),
    ]
    print(f"  {'offset':>6}  {'end':>6}  {'size':>4}  {'field':<20}  type")
    print(f"  {'──────':>6}  {'──────':>6}  {'────':>4}  {'──────────────────':20}  ────────────────────")
    for name, start, end, typ in fields:
        print(f"  {start:6d}  {end:6d}  {end-start:4d}  {name:<20}  {typ}")
    print(f"  {'──────':>6}  {'──────':>6}  {'────':>4}")
    print(f"  {'TOTAL':>6}  {'':>6}  {112:4d}")
