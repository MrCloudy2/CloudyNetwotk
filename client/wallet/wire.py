"""
wire.py — Binary wire protocol codec for Chain Wallet
======================================================

Mirrors the binary wire format defined in server/server1.py.
All integers are big-endian.

Endpoint summary
----------------
GET /get_mining_job  → 136-byte response
    [16B job_id_raw][4B block_index][112B header][4B tx_count]

POST /submit_block   ← 128-byte request body
    [16B job_id_raw][112B solved header]
                     → 36-byte response on success
    [4B block_index][32B block_hash]

GET /blockchain      → variable
    [4B chain_len] + blocks + [4B mempool_blob_len][NB mempool_blob]
    each block: [4B idx][112B header][32B hash][4B txs_len][NB txs_blob]

GET /utxos           → variable
    [4B utxo_count] + UTXOs

POST /add_transaction ← raw tx_to_bytes() binary body
                      → 32B tx_id on 201

Error responses (any 4xx/5xx): [2B msg_len][NB UTF-8 message]
"""

import struct
import io

from tx_codec import tx_to_bytes, block_txs_from_bytes
from block_header import unpack_header, HEADER_SIZE


# ─────────────────────────────────────────────────────────────────────────────
#  Varint (Bitcoin CompactSize) — matches tx_codec._decode_varint
# ─────────────────────────────────────────────────────────────────────────────

def _read_varint(buf: io.BytesIO) -> int:
    prefix = buf.read(1)[0]
    if prefix < 0xFD:
        return prefix
    elif prefix == 0xFD:
        return struct.unpack(">H", buf.read(2))[0]
    elif prefix == 0xFE:
        return struct.unpack(">I", buf.read(4))[0]
    else:
        return struct.unpack(">Q", buf.read(8))[0]


# ─────────────────────────────────────────────────────────────────────────────
#  Error decode
# ─────────────────────────────────────────────────────────────────────────────

def decode_error(data: bytes) -> str:
    """[2B uint16 msg_len][NB UTF-8 message] → str."""
    if len(data) < 2:
        return f"(empty error body, {len(data)}B)"
    msg_len = struct.unpack(">H", data[:2])[0]
    return data[2:2 + msg_len].decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /blockchain
# ─────────────────────────────────────────────────────────────────────────────

def decode_blockchain(data: bytes) -> tuple:
    """
    Parse the binary /blockchain response.

    Returns
    -------
    (chain, mempool)
        chain   : list of block dicts — keys match the old JSON shape so
                  all existing UI / leaderboard / explorer code works unchanged.
        mempool : list of tx dicts
    """
    buf = io.BytesIO(data)
    chain_len = struct.unpack(">I", buf.read(4))[0]

    chain = []
    for _ in range(chain_len):
        block_index = struct.unpack(">I", buf.read(4))[0]
        header_bytes = buf.read(HEADER_SIZE)
        block_hash   = buf.read(32).hex()
        txs_len      = struct.unpack(">I", buf.read(4))[0]
        txs_blob     = buf.read(txs_len)

        hdr  = unpack_header(header_bytes)
        txs  = block_txs_from_bytes(txs_blob) if txs_blob else []

        chain.append({
            "index":             block_index,
            "previous_hash":     hdr["previous_hash"],
            "merkle_root":       hdr["merkle_root"],
            "timestamp":         hdr["timestamp"],
            "difficulty_target": hdr["difficulty_target"],   # proper int, no overflow
            "nonce":             hdr["nonce"],
            "block_hash":        block_hash,
            "transactions":      txs,
        })

    mempool_len  = struct.unpack(">I", buf.read(4))[0]
    mempool_blob = buf.read(mempool_len)
    mempool = block_txs_from_bytes(mempool_blob) if mempool_blob else []

    return chain, mempool


# ─────────────────────────────────────────────────────────────────────────────
#  GET /utxos
# ─────────────────────────────────────────────────────────────────────────────

def decode_utxos(data: bytes) -> dict:
    """
    Parse the binary /utxos response.

    Returns
    -------
    dict  {"tx_id:out_idx": {"address": str, "amount": int}}
        Keys are the same "tx_id:out_idx" string format the UI already uses.
    """
    buf   = io.BytesIO(data)
    count = struct.unpack(">I", buf.read(4))[0]
    utxos = {}

    for _ in range(count):
        # tx_id
        tx_id_flag = buf.read(1)[0]
        if tx_id_flag == 0x00:
            tx_id = buf.read(32).hex()
        else:
            tlen  = buf.read(1)[0]
            tx_id = buf.read(tlen).decode("utf-8")

        out_idx = buf.read(1)[0]

        # address
        addr_flag = buf.read(1)[0]
        if addr_flag == 0x00:
            address = buf.read(32).hex()
        else:
            alen    = buf.read(1)[0]
            address = buf.read(alen).decode("utf-8")

        amount = _read_varint(buf)
        utxos[f"{tx_id}:{out_idx}"] = {"address": address, "amount": amount}

    return utxos


# ─────────────────────────────────────────────────────────────────────────────
#  GET /get_mining_job
# ─────────────────────────────────────────────────────────────────────────────

def decode_mining_job(data: bytes) -> dict:
    """
    Parse the 136-byte binary /get_mining_job response.

    Returns
    -------
    {
        "job_id_raw":    bytes (16B),
        "block_index":   int,
        "header_bytes":  bytes (112B),  ← mutate nonce at [104:112] during mining
        "tx_count":      int,
        "target":        int,           ← 256-bit int from header[72:104]
    }
    """
    if len(data) != 136:
        raise ValueError(f"Expected 136-byte mining job, got {len(data)}")
    job_id_raw  = data[:16]
    block_index = struct.unpack(">I", data[16:20])[0]
    header_bytes = data[20:132]
    tx_count    = struct.unpack(">I", data[132:136])[0]
    target      = int.from_bytes(header_bytes[72:104], "big")

    return {
        "job_id_raw":   job_id_raw,
        "block_index":  block_index,
        "header_bytes": header_bytes,
        "tx_count":     tx_count,
        "target":       target,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  POST /submit_block
# ─────────────────────────────────────────────────────────────────────────────

def encode_submit_block(job_id_raw: bytes, header_bytes: bytes) -> bytes:
    """Build the 128-byte binary submit body: [16B job_id][112B header]."""
    assert len(job_id_raw) == 16,  f"job_id_raw must be 16 bytes, got {len(job_id_raw)}"
    assert len(header_bytes) == HEADER_SIZE, f"header must be {HEADER_SIZE} bytes"
    return job_id_raw + header_bytes


def decode_submit_block(data: bytes) -> dict:
    """
    Parse the 36-byte success response from /submit_block.

    Returns {"block_index": int, "block_hash": str (64 hex chars)}
    """
    if len(data) != 36:
        raise ValueError(f"Expected 36-byte submit response, got {len(data)}")
    block_index = struct.unpack(">I", data[:4])[0]
    block_hash  = data[4:].hex()
    return {"block_index": block_index, "block_hash": block_hash}


# ─────────────────────────────────────────────────────────────────────────────
#  POST /add_transaction
# ─────────────────────────────────────────────────────────────────────────────

def encode_add_transaction(tx: dict) -> bytes:
    """Encode a transaction dict to the binary body for POST /add_transaction."""
    return tx_to_bytes(tx)


def decode_add_transaction_response(data: bytes) -> str:
    """Parse the 32-byte tx_id returned on a successful 201 response."""
    if len(data) != 32:
        raise ValueError(f"Expected 32-byte tx_id, got {len(data)}")
    return data.hex()
