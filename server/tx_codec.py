"""
tx_codec.py — Pure binary transaction codec for CloudyCoin
===========================================================

Wire format (all multi-byte integers are big-endian):

  ┌─────────────────────────────────────────────────────────┐
  │ 1B   tx_type   0 = normal, 1 = genesis, 2 = coinbase   │
  ├─────────────────────────────────────────────────────────┤
  │ [only when tx_type == 2 (coinbase)]                     │
  │   varint  block_index                                   │
  ├─────────────────────────────────────────────────────────┤
  │ 1B   n_inputs                                           │
  │  ╔══ repeated n_inputs times ═══════════════════════╗   │
  │  ║ 32B  tx_id       (binary, from hex)              ║   │
  │  ║  1B  out_idx                                     ║   │
  │  ║ 64B  public_key  (raw uncompressed, from hex)     ║   │
  │  ║ 64B  signature   (raw R||S DER-less, from hex)   ║   │
  │  ╚═══════════════════════════════════════════════════╝   │
  ├─────────────────────────────────────────────────────────┤
  │ 1B   n_outputs                                          │
  │  ╔══ repeated n_outputs times ══════════════════════╗   │
  │  ║ 32B  address  (binary SHA-256 digest, from hex)  ║   │
  │  ║ varint  amount                                   ║   │
  │  ╚═══════════════════════════════════════════════════╝   │
  ├─────────────────────────────────────────────────────────┤
  │ [only when tx_type == 0 (normal)]                       │
  │  32B  tx_id  (binary SHA-256 digest, from hex)          │
  └─────────────────────────────────────────────────────────┘

Varint encoding (identical to Bitcoin's CompactSize):
  value < 0xFD          →  1 byte   (value)
  value < 0x1_0000      →  3 bytes  (0xFD + 2B big-endian)
  value < 0x1_0000_0000 →  5 bytes  (0xFE + 4B big-endian)
  otherwise             →  9 bytes  (0xFF + 8B big-endian)

Typical normal-tx size (1 input, 2 outputs):
  1 + 1 + (32+1+64+64) + 1 + 2×(32+1) + 32  =  263 bytes
  vs ~700+ bytes for the equivalent JSON.
"""

import struct
import io
import hashlib
import json


# ---------------------------------------------------------------------------
# Varint helpers
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a Bitcoin-style CompactSize varint."""
    if value < 0:
        raise ValueError(f"varint value must be non-negative, got {value}")
    if value < 0xFD:
        return struct.pack("B", value)
    elif value < 0x1_0000:
        return b"\xfd" + struct.pack(">H", value)
    elif value < 0x1_0000_0000:
        return b"\xfe" + struct.pack(">I", value)
    else:
        return b"\xff" + struct.pack(">Q", value)


def _decode_varint(buf: io.BytesIO) -> int:
    """Read one varint from a BytesIO stream and return its integer value."""
    prefix = _read_exact(buf, 1)[0]          # single byte as int
    if prefix < 0xFD:
        return prefix
    elif prefix == 0xFD:
        return struct.unpack(">H", _read_exact(buf, 2))[0]
    elif prefix == 0xFE:
        return struct.unpack(">I", _read_exact(buf, 4))[0]
    else:
        return struct.unpack(">Q", _read_exact(buf, 8))[0]


# ---------------------------------------------------------------------------
# Stream helper
# ---------------------------------------------------------------------------

def _read_exact(buf: io.BytesIO, n: int) -> bytes:
    """Read exactly n bytes or raise an informative error."""
    data = buf.read(n)
    if len(data) != n:
        raise ValueError(
            f"Unexpected end of stream: needed {n} byte(s), got {len(data)}"
        )
    return data


# ---------------------------------------------------------------------------
# TX type detection
# ---------------------------------------------------------------------------

_TX_TYPE_NORMAL  = 0
_TX_TYPE_GENESIS = 1
_TX_TYPE_COINBASE = 2


def _classify_tx(tx: dict) -> int:
    tx_id = tx.get("tx_id", "")
    if tx_id == "genesis_tx":
        return _TX_TYPE_GENESIS
    if tx_id.startswith("coinbase_"):
        return _TX_TYPE_COINBASE
    return _TX_TYPE_NORMAL


def _coinbase_index(tx: dict) -> int:
    """Extract the block index embedded in a coinbase tx_id like 'coinbase_7'."""
    try:
        return int(tx["tx_id"].split("_", 1)[1])
    except (IndexError, ValueError):
        raise ValueError(f"Malformed coinbase tx_id: {tx['tx_id']!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tx_to_bytes(tx: dict) -> bytes:
    """
    Serialise a CloudyCoin transaction dict to its compact binary form.

    Parameters
    ----------
    tx : dict
        A transaction as used throughout server1.py, e.g.::

            {
                "tx_id": "ab12...",
                "inputs": [
                    {
                        "tx_id": "cd34...",
                        "out_idx": 0,
                        "public_key": "04ab...",   # 64B raw uncompressed, hex
                        "signature": "3045..."     # 64B raw R||S, hex
                    }
                ],
                "outputs": [
                    {"address": "ef56...", "amount": 5}
                ]
            }

    Returns
    -------
    bytes
        The binary-encoded transaction.
    """
    out = io.BytesIO()

    tx_type = _classify_tx(tx)
    out.write(struct.pack("B", tx_type))                    # 1B type flag

    if tx_type == _TX_TYPE_COINBASE:
        block_idx = _coinbase_index(tx)
        out.write(_encode_varint(block_idx))                # varint block index

    # ── Inputs ──────────────────────────────────────────────────────────────
    inputs = tx.get("inputs", [])
    if len(inputs) > 255:
        raise ValueError(f"Too many inputs: {len(inputs)} (max 255)")
    out.write(struct.pack("B", len(inputs)))

    for inp in inputs:
        tx_id_val = inp["tx_id"]
        try:
            raw_tx_id = bytes.fromhex(tx_id_val)
            if len(raw_tx_id) != 32:
                raise ValueError(f"Input tx_id must be 32 bytes (64 hex chars), got {len(raw_tx_id)}")
            out.write(b"\x00")        # flag: binary tx_id
            out.write(raw_tx_id)      # 32B
        except ValueError as exc:
            if "non-hexadecimal" in str(exc) or "odd-length" in str(exc):
                encoded_id = tx_id_val.encode("utf-8")
                if len(encoded_id) > 255:
                    raise ValueError(f"String tx_id too long (max 255 bytes): {tx_id_val!r}")
                out.write(b"\x01")                               # flag: string tx_id
                out.write(struct.pack("B", len(encoded_id)))     # 1B length
                out.write(encoded_id)                            # NB string
            else:
                raise                               # 32B spent tx_id

        out_idx = inp["out_idx"]
        if not 0 <= out_idx <= 255:
            raise ValueError(f"out_idx {out_idx} out of range [0, 255]")
        out.write(struct.pack("B", out_idx))                # 1B  out_idx

        raw_pk = bytes.fromhex(inp["public_key"])
        if len(raw_pk) != 64:
            raise ValueError(
                f"public_key must be 64 bytes (raw uncompressed), got {len(raw_pk)}"
            )
        out.write(raw_pk)                                   # 64B public key

        raw_sig = bytes.fromhex(inp["signature"])
        if len(raw_sig) != 64:
            raise ValueError(
                f"signature must be 64 bytes (raw R||S), got {len(raw_sig)}"
            )
        out.write(raw_sig)                                  # 64B signature

    # ── Outputs ─────────────────────────────────────────────────────────────
    outputs = tx.get("outputs", [])
    if len(outputs) > 255:
        raise ValueError(f"Too many outputs: {len(outputs)} (max 255)")
    out.write(struct.pack("B", len(outputs)))

    for output in outputs:
        addr = output["address"]
        try:
            raw_addr = bytes.fromhex(addr)
            if len(raw_addr) != 32:
                raise ValueError(
                    f"address must be 32 bytes (64 hex chars), got {len(raw_addr)}"
                )
            out.write(b"\x00")                              # 1B flag: hex address
            out.write(raw_addr)                             # 32B address
        except ValueError as exc:
            if "non-hexadecimal" in str(exc) or "odd-length" in str(exc):
                # Plain-text address (e.g. "GodMode" in genesis)
                raw_addr = addr.encode("utf-8")
                if len(raw_addr) > 32:
                    raise ValueError(
                        f"Plain-text address too long (max 32 bytes): {addr!r}"
                    )
                out.write(b"\x01")                          # 1B flag: text address
                out.write(struct.pack("B", len(raw_addr)))  # 1B length
                out.write(raw_addr)                         # up to 32B
            else:
                raise
        out.write(_encode_varint(output["amount"]))         # varint amount

    # ── tx_id (normal txs only) ──────────────────────────────────────────────
    if tx_type == _TX_TYPE_NORMAL:
        raw_txid = bytes.fromhex(tx["tx_id"])
        if len(raw_txid) != 32:
            raise ValueError(
                f"tx_id must be 32 bytes (64 hex chars), got {len(raw_txid)}"
            )
        out.write(raw_txid)                                 # 32B tx_id

    return out.getvalue()


def bytes_to_tx(byte_data: bytes) -> dict:
    """
    Deserialise a binary-encoded CloudyCoin transaction back to a dict.

    Parameters
    ----------
    byte_data : bytes
        Raw bytes as produced by :func:`tx_to_bytes`.

    Returns
    -------
    dict
        Transaction dict compatible with the rest of server1.py.

    Raises
    ------
    ValueError
        On any structural or length mismatch.
    """
    buf = io.BytesIO(byte_data)

    tx_type = struct.unpack("B", _read_exact(buf, 1))[0]
    if tx_type not in (_TX_TYPE_NORMAL, _TX_TYPE_GENESIS, _TX_TYPE_COINBASE):
        raise ValueError(f"Unknown tx_type byte: {tx_type}")

    coinbase_block_idx = None
    if tx_type == _TX_TYPE_COINBASE:
        coinbase_block_idx = _decode_varint(buf)

    # ── Inputs ──────────────────────────────────────────────────────────────
    n_inputs = struct.unpack("B", _read_exact(buf, 1))[0]
    inputs = []
    for _ in range(n_inputs):
        tx_id_flag = struct.unpack("B", _read_exact(buf, 1))[0]
        if tx_id_flag == 0x00:
            tx_id = _read_exact(buf, 32).hex()
        elif tx_id_flag == 0x01:
            tlen  = struct.unpack("B", _read_exact(buf, 1))[0]
            tx_id = _read_exact(buf, tlen).decode("utf-8")
        else:
            raise ValueError(f"Unknown input tx_id flag: {tx_id_flag:#04x}")
        out_idx = struct.unpack("B", _read_exact(buf, 1))[0]
        raw_pk  = _read_exact(buf, 64)
        raw_sig = _read_exact(buf, 64)
        inputs.append({
            "tx_id":      tx_id,
            "out_idx":    out_idx,
            "public_key": raw_pk.hex(),
            "signature":  raw_sig.hex(),
        })

    # ── Outputs ─────────────────────────────────────────────────────────────
    n_outputs = struct.unpack("B", _read_exact(buf, 1))[0]
    outputs = []
    for _ in range(n_outputs):
        addr_flag = struct.unpack("B", _read_exact(buf, 1))[0]
        if addr_flag == 0x00:
            address = _read_exact(buf, 32).hex()
        elif addr_flag == 0x01:
            addr_len = struct.unpack("B", _read_exact(buf, 1))[0]
            address  = _read_exact(buf, addr_len).decode("utf-8")
        else:
            raise ValueError(f"Unknown address flag byte: {addr_flag:#04x}")
        amount = _decode_varint(buf)
        outputs.append({"address": address, "amount": amount})

    # ── tx_id ────────────────────────────────────────────────────────────────
    if tx_type == _TX_TYPE_NORMAL:
        tx_id = _read_exact(buf, 32).hex()
    elif tx_type == _TX_TYPE_GENESIS:
        tx_id = "genesis_tx"
    else:  # coinbase
        tx_id = f"coinbase_{coinbase_block_idx}"

    # Ensure the buffer is fully consumed (no trailing garbage)
    trailing = buf.read()
    if trailing:
        raise ValueError(
            f"Unexpected trailing bytes after decoding: {len(trailing)} byte(s)"
        )

    return {
        "tx_id":    tx_id,
        "inputs":   inputs,
        "outputs":  outputs,
    }


# ---------------------------------------------------------------------------
# Convenience: batch encode/decode a full block's transaction list
# ---------------------------------------------------------------------------

def block_txs_to_bytes(transactions: list) -> bytes:
    """
    Encode an entire block's transaction list as:
        [ varint: n_txs ][ varint: tx_len ][ tx_bytes ] ...
    Suitable for compact block storage or P2P transmission.
    """
    out = io.BytesIO()
    out.write(_encode_varint(len(transactions)))
    for tx in transactions:
        tx_bytes = tx_to_bytes(tx)
        out.write(_encode_varint(len(tx_bytes)))
        out.write(tx_bytes)
    return out.getvalue()


def block_txs_from_bytes(byte_data: bytes) -> list:
    """Decode a byte string produced by :func:`block_txs_to_bytes`."""
    buf = io.BytesIO(byte_data)
    n_txs = _decode_varint(buf)
    transactions = []
    for _ in range(n_txs):
        tx_len = _decode_varint(buf)
        tx_bytes = _read_exact(buf, tx_len)
        transactions.append(bytes_to_tx(tx_bytes))
    return transactions


# ---------------------------------------------------------------------------
# Self-test (run with: python tx_codec.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    def _fake_hex(n):
        return os.urandom(n).hex()

    # ── Normal transaction ───────────────────────────────────────────────────
    normal_tx = {
        "tx_id": _fake_hex(32),
        "inputs": [
            {
                "tx_id":      _fake_hex(32),
                "out_idx":    0,
                "public_key": _fake_hex(33),
                "signature":  _fake_hex(64),
            }
        ],
        "outputs": [
            {"address": _fake_hex(32), "amount": 1},
            {"address": _fake_hex(32), "amount": 4},
        ],
    }

    # ── Genesis transaction ──────────────────────────────────────────────────
    genesis_tx = {
        "tx_id": "genesis_tx",
        "inputs": [],
        "outputs": [{"address": "GodMode", "amount": 1}],
    }

    # ── Coinbase transaction ─────────────────────────────────────────────────
    coinbase_tx = {
        "tx_id": "coinbase_42",
        "inputs": [],
        "outputs": [{"address": _fake_hex(32), "amount": 1}],
    }

    all_txs = [normal_tx, genesis_tx, coinbase_tx]
    labels  = ["normal", "genesis", "coinbase"]

    print("=" * 60)
    all_ok = True
    for tx, label in zip(all_txs, labels):
        encoded = tx_to_bytes(tx)
        decoded = bytes_to_tx(encoded)

        ok = (
            decoded["tx_id"]    == tx["tx_id"]
            and decoded["inputs"]  == tx["inputs"]
            and decoded["outputs"] == tx["outputs"]
        )
        size_json = len(json.dumps(tx).encode())
        savings   = round((1 - len(encoded) / size_json) * 100, 1)

        status = "PASS ✓" if ok else "FAIL ✗"
        if not ok:
            all_ok = False
        print(
            f"[{status}] {label:10s}  "
            f"binary={len(encoded):4d}B  json={size_json:4d}B  "
            f"savings={savings}%"
        )

    # Batch encode/decode
    batch = block_txs_to_bytes(all_txs)
    recovered = block_txs_from_bytes(batch)
    batch_ok = all(
        r["tx_id"] == t["tx_id"] and r["outputs"] == t["outputs"]
        for r, t in zip(recovered, all_txs)
    )
    print(f"[{'PASS ✓' if batch_ok else 'FAIL ✗'}] batch      "
          f"total={len(batch)}B for {len(all_txs)} txs")
    print("=" * 60)
    print("All tests passed!" if all_ok and batch_ok else "SOME TESTS FAILED.")
