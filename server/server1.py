import hashlib
import json
import struct
import io
import time
import ecdsa
import os
import threading
import sqlite3
from contextlib import contextmanager
from block_header import (
    calculate_hash_binary, pack_header, unpack_header,
    HEADER_VERSION, HEADER_SIZE,
)
from merkle import merkle_root_hex, merkle_proof, verify_tx_inclusion, tx_leaf_hash
from tx_codec import tx_to_bytes, bytes_to_tx, block_txs_to_bytes
from flask import Flask, request, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=["30 per minute"]
)

# --- CONFIGURABLE CONSTANTS ---
BLOCK_REWARD = 1
TARGET_BLOCK_TIME = 120
SMOOTHNESS = 5
MAX_TIMESTAMP_AGE = 7200
GENESIS_TARGET = 0x0000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
DB_FILE = "blockchain.db"


def get_address_from_public_key(public_key_hex):
    pk_bytes = bytes.fromhex(public_key_hex)
    return hashlib.sha256(pk_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def get_db_connection():
    """Open a connection with row_factory so rows behave like dicts."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist yet."""
    conn = get_db_connection()
    with conn:
        conn.executescript("""
            -- One row per block header
            CREATE TABLE IF NOT EXISTS blocks (
                idx             INTEGER PRIMARY KEY,
                previous_hash   TEXT    NOT NULL,
                block_hash      TEXT    NOT NULL UNIQUE,
                timestamp       INTEGER NOT NULL,
                difficulty_target TEXT  NOT NULL,  -- <-- CHANGED THIS FROM INTEGER TO TEXT
                nonce           INTEGER NOT NULL
            );

            -- One row per transaction; block_idx FK keeps referential integrity
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id       TEXT    PRIMARY KEY,
                block_idx   INTEGER NOT NULL REFERENCES blocks(idx),
                tx_json     TEXT    NOT NULL   -- full tx stored as JSON blob
            );

            -- Live UTXO set: one row per unspent output
            CREATE TABLE IF NOT EXISTS utxos (
                utxo_key    TEXT    PRIMARY KEY,   -- "<tx_id>:<out_idx>"
                address     TEXT    NOT NULL,
                amount      INTEGER NOT NULL
            );
        """)
    conn.close()


def _save_block_to_db(conn, block):
    """
    Persist a fully-formed block (with block_hash) inside an existing
    connection/transaction. Updates the UTXO set atomically.
    """
    # Convert difficulty target to a hex string to prevent SQLite overflow
    diff_target_hex = hex(block["difficulty_target"])

    conn.execute(
        """INSERT INTO blocks
               (idx, previous_hash, block_hash, timestamp, difficulty_target, nonce)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            block["index"],
            block["previous_hash"],
            block["block_hash"],
            block["timestamp"],
            diff_target_hex, # <-- SAVING AS STRING
            block["nonce"],
        ),
    )

    for tx in block["transactions"]:
        conn.execute(
            "INSERT OR REPLACE INTO transactions (tx_id, block_idx, tx_json) VALUES (?, ?, ?)",
            (tx["tx_id"], block["index"], json.dumps(tx)),
        )

    _update_utxos(conn, block["transactions"])


def _update_utxos(conn, transactions):
    """Spend inputs and create new outputs for a list of transactions."""
    for tx in transactions:
        for inp in tx["inputs"]:
            key = f"{inp['tx_id']}:{inp['out_idx']}"
            conn.execute("DELETE FROM utxos WHERE utxo_key = ?", (key,))

        for idx, out in enumerate(tx["outputs"]):
            key = f"{tx['tx_id']}:{idx}"
            conn.execute(
                "INSERT OR REPLACE INTO utxos (utxo_key, address, amount) VALUES (?, ?, ?)",
                (key, out["address"], out["amount"]),
            )


def _load_chain_from_db(conn):
    """Rebuild the full in-memory chain list and UTXO dict from the DB.

    Called once at startup.
    """
    rows = conn.execute("SELECT * FROM blocks ORDER BY idx").fetchall()
    chain = []
    for row in rows:
        block = dict(row)
        # Reattach transactions
        tx_rows = conn.execute(
            "SELECT tx_json FROM transactions WHERE block_idx = ? ORDER BY rowid",
            (block["idx"],),
        ).fetchall()
        block["transactions"] = [json.loads(r["tx_json"]) for r in tx_rows]

        # --- ADD THIS FIX FOR THE INTEGER OVERFLOW ---
        # Convert difficulty target back to an int from its stored hex string representation
        if "difficulty_target" in block and isinstance(
            block["difficulty_target"], str
        ):
            block["difficulty_target"] = int(block["difficulty_target"], 16)
        # ----------------------------------------------

        # Rename 'idx' -> 'index' to match the rest of the code
        block["index"] = block.pop("idx")
        chain.append(block)

    utxo_rows = conn.execute(
        "SELECT utxo_key, address, amount FROM utxos"
    ).fetchall()
    utxos = {
        r["utxo_key"]: {"address": r["address"], "amount": r["amount"]}
        for r in utxo_rows
    }

    return chain, utxos


# ---------------------------------------------------------------------------
# Blockchain class
# ---------------------------------------------------------------------------

class Blockchain:
    def __init__(self):
        self.lock = threading.Lock()
        self.chain = []
        self.mempool = []
        self.utxos = {}
        self.target_block_time = TARGET_BLOCK_TIME
        self.smoothness = SMOOTHNESS
        self.current_target = GENESIS_TARGET

        init_db()

        conn = get_db_connection()
        chain, utxos = _load_chain_from_db(conn)
        conn.close()

        if chain:
            if self._verify_loaded_chain(chain):
                self.chain = chain
                self.utxos = utxos
                self.current_target = self.calculate_next_target()
                print(f"[SUCCESS] Loaded {len(self.chain)} blocks from SQLite. UTXO set rebuilt.")
            else:
                print("[ERROR] DB chain failed verification — starting fresh.")
                self._reset_db_and_genesis()
        else:
            print("[INFO] Empty database. Mining Genesis Block...")
            self._reset_db_and_genesis()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reset_db_and_genesis(self):
        """Wipe all tables and create a fresh genesis block."""
        conn = get_db_connection()
        with conn:
            conn.executescript(
                "DELETE FROM utxos; DELETE FROM transactions; DELETE FROM blocks;"
            )
        conn.close()
        self._create_genesis_block()

    def _verify_loaded_chain(self, chain):
        previous_hash = "0" * 64
        for block in chain:
            if block["previous_hash"] != previous_hash:
                return False
            calculated = self.calculate_hash(block)
            if calculated != block["block_hash"]:
                return False
            if int(calculated, 16) > block["difficulty_target"]:
                return False
            previous_hash = block["block_hash"]
        return True

    def _create_genesis_block(self):
        print("[INFO] Mining Genesis Block... This might take a few seconds.")
        genesis_tx = {
            "tx_id": "genesis_tx",
            "inputs": [],
            "outputs": [{"address": "GodMode", "amount": 1}],
        }
        genesis = {
            "index": 0,
            "previous_hash": "0" * 64,
            "timestamp": int(time.time()),
            "difficulty_target": self.current_target,
            "nonce": 0,
            "transactions": [genesis_tx],
        }
        while True:
            block_hash = self.calculate_hash(genesis)
            if int(block_hash, 16) <= self.current_target:
                genesis["block_hash"] = block_hash
                break
            genesis["nonce"] += 1

        self.chain.append(genesis)
        self.utxos["genesis_tx:0"] = genesis_tx["outputs"][0]
        self._save_block(genesis)
        print(f"[INFO] Genesis Block mined! Hash: {genesis['block_hash']}")

    # ------------------------------------------------------------------
    # Public persistence helpers
    # ------------------------------------------------------------------

    def _save_block(self, block):
        """Persist a single block + UTXO changes to SQLite."""
        try:
            conn = get_db_connection()
            with conn:                       # auto-commit / rollback
                _save_block_to_db(conn, block)
            conn.close()
        except Exception as e:
            print(f"[ERROR] Failed to save block to DB: {e}")

    # ------------------------------------------------------------------
    # Core logic (unchanged from original)
    # ------------------------------------------------------------------

    def calculate_hash(self, block):
        """
        Hash a block using the 112-byte binary header format.
        Delegates to block_header.calculate_hash_binary so the hash is
        computed over a compact, GPU-friendly struct rather than JSON.
        """
        return calculate_hash_binary(block, block.get("transactions", []))

    def calculate_next_target(self):
        if len(self.chain) < 2:
            return GENESIS_TARGET

        blocks_to_count = min(len(self.chain) - 1, 10)
        reference_block = self.chain[-1 - blocks_to_count]
        latest_block = self.chain[-1]

        actual_time = max(1, latest_block["timestamp"] - reference_block["timestamp"])
        expected_time = self.target_block_time * blocks_to_count

        last_target = self.chain[-1].get("difficulty_target", GENESIS_TARGET)
        if not isinstance(last_target, int) or last_target <= 0:
            print(f"[WARN] Bad difficulty_target in chain tip ({last_target!r}), resetting.")
            last_target = GENESIS_TARGET

        lo = (expected_time * 75) // 100
        hi = (expected_time * 125) // 100
        clamped_actual = max(lo, min(actual_time, hi))

        new_target = (last_target * clamped_actual) // expected_time
        MIN_TARGET = GENESIS_TARGET >> 32
        new_target = max(MIN_TARGET, min(new_target, GENESIS_TARGET))
        return new_target

    def validate_transaction(self, tx, is_mempool_check=True):
        total_out = sum(out["amount"] for out in tx["outputs"])
        if total_out <= 0:
            raise ValueError("Invalid output amount")

        sanitized_inputs = [
            {"tx_id": inp["tx_id"], "out_idx": inp["out_idx"], "public_key": inp["public_key"]}
            for inp in tx["inputs"]
        ]
        msg = json.dumps({"inputs": sanitized_inputs, "outputs": tx["outputs"]}, sort_keys=True).encode()

        total_in = 0
        seen_utxos = set()
        for inp in tx["inputs"]:
            utxo_key = f"{inp['tx_id']}:{inp['out_idx']}"
            if utxo_key in seen_utxos:
                raise ValueError(f"Duplicate input in transaction: {utxo_key}")
            seen_utxos.add(utxo_key)
            if utxo_key not in self.utxos:
                raise ValueError(f"UTXO {utxo_key} not found or already spent")

            utxo = self.utxos[utxo_key]
            if get_address_from_public_key(inp["public_key"]) != utxo["address"]:
                raise ValueError("Public key does not match UTXO owner address")

            try:
                vk = ecdsa.VerifyingKey.from_string(
                    bytes.fromhex(inp["public_key"]), curve=ecdsa.SECP256k1
                )
                if not vk.verify(bytes.fromhex(inp["signature"]), msg):
                    raise ValueError("Invalid signature")
            except Exception:
                raise ValueError("Signature verification failed")

            if is_mempool_check:
                if any(
                    f"{i['tx_id']}:{i['out_idx']}" == utxo_key
                    for m in self.mempool
                    for i in m["inputs"]
                ):
                    raise ValueError("Double spend detected in mempool")

            total_in += utxo["amount"]

        if total_in < total_out:
            raise ValueError("Insufficient funds")

        return True



# ---------------------------------------------------------------------------
# Mining job store
# ---------------------------------------------------------------------------
# Maps job_id (hex string) → job record dict for the duration a miner works.
# Records expire after JOB_TTL seconds so memory doesn't grow indefinitely
# even if miners abandon jobs.
#
# Structure of each record:
#   {
#     "index":        int,          block height this job targets
#     "transactions": list[dict],   [coinbase] + selected mempool txs
#     "target":       int,          difficulty target at job creation time
#     "expires_at":   float,        time.time() + JOB_TTL
#   }
#
# The job_id is a 16-byte random token encoded as 32 hex chars — collision
# probability with 1 000 concurrent miners is ~1.5e-33, i.e. negligible.

JOB_TTL = 600          # seconds — 10 minutes, matches TARGET_BLOCK_TIME
_job_store: dict = {}
_job_store_lock = threading.Lock()


def _new_job_id() -> str:
    return os.urandom(16).hex()


def _store_job(job_id: str, record: dict) -> None:
    """Save a job record and evict all expired entries atomically."""
    now = time.time()
    with _job_store_lock:
        # Evict expired jobs on every write — O(n) but n is tiny
        expired = [jid for jid, r in _job_store.items() if r["expires_at"] <= now]
        for jid in expired:
            del _job_store[jid]
        _job_store[job_id] = record


def _lookup_job(job_id: str) -> dict | None:
    """Return the job record if it exists and has not expired, else None."""
    now = time.time()
    with _job_store_lock:
        record = _job_store.get(job_id)
        if record is None or record["expires_at"] <= now:
            return None
        return record


def _delete_job(job_id: str) -> None:
    with _job_store_lock:
        _job_store.pop(job_id, None)

blockchain = Blockchain()


# ---------------------------------------------------------------------------
# Binary wire-encoding helpers
# ---------------------------------------------------------------------------

def _wire_error(message: str, status: int) -> Response:
    """Binary error envelope: [2B uint16 message_length][NB UTF-8 message]."""
    msg = message.encode("utf-8")
    return Response(struct.pack(">H", len(msg)) + msg,
                    status=status, mimetype="application/octet-stream")


def _wire_ok(data: bytes, status: int = 200) -> Response:
    return Response(data, status=status, mimetype="application/octet-stream")


def _wire_varint(n: int) -> bytes:
    """Bitcoin-style CompactSize varint — mirrors tx_codec._encode_varint."""
    if n < 0xFD:
        return struct.pack("B", n)
    elif n < 0x1_0000:
        return b"\xfd" + struct.pack(">H", n)
    elif n < 0x1_0000_0000:
        return b"\xfe" + struct.pack(">I", n)
    else:
        return b"\xff" + struct.pack(">Q", n)


def _encode_block_wire(block: dict) -> bytes:
    """
    Wire format for one block inside /blockchain:
        [4B  uint32  block_index]
        [112B        header  (pack_header)]
        [32B         block_hash raw]
        [4B  uint32  txs_blob_length]
        [NB          block_txs_to_bytes(transactions)]
    """
    out = io.BytesIO()
    out.write(struct.pack(">I", block["index"]))
    tx_bytes_list = [tx_to_bytes(tx) for tx in block.get("transactions", [])]
    root_hex = merkle_root_hex(tx_bytes_list) if tx_bytes_list else "0" * 64
    header_bytes = pack_header(
        version           = block.get("version", HEADER_VERSION),
        previous_hash     = block["previous_hash"],
        merkle_root       = root_hex,
        timestamp         = block["timestamp"],
        difficulty_target = block["difficulty_target"],
        nonce             = block["nonce"],
    )
    out.write(header_bytes)                                  # 112B
    out.write(bytes.fromhex(block["block_hash"]))            # 32B
    txs_blob = block_txs_to_bytes(block["transactions"])
    out.write(struct.pack(">I", len(txs_blob)))              # 4B length prefix
    out.write(txs_blob)
    return out.getvalue()


def _encode_utxos_wire(utxos: dict) -> bytes:
    """
    Wire format for /utxos:
        [4B  uint32  utxo_count]
        for each UTXO (key = "<tx_id>:<out_idx>"):
            [1B   tx_id_flag: 0x00=32B hex, 0x01=string (genesis_tx/coinbase_N)]
            [if 0x00: 32B tx_id raw]
            [if 0x01: 1B len + NB UTF-8]
            [1B   uint8 out_idx]
            [1B   addr_flag: 0x00=32-byte hex, 0x01=text]
            [if 0x00: 32B address]
            [if 0x01: 1B len + NB UTF-8]
            [varint  amount]
    """
    out = io.BytesIO()
    out.write(struct.pack(">I", len(utxos)))
    for key, utxo in utxos.items():
        tx_id_str, out_idx_str = key.rsplit(":", 1)
        # tx_id: 32-byte hex or special string (genesis_tx, coinbase_N)
        try:
            raw_tx_id = bytes.fromhex(tx_id_str)
            if len(raw_tx_id) != 32:
                raise ValueError
            out.write(b"\x00" + raw_tx_id)
        except ValueError:
            raw_tx_id = tx_id_str.encode("utf-8")
            out.write(b"\x01" + struct.pack("B", len(raw_tx_id)) + raw_tx_id)
        out.write(struct.pack("B", int(out_idx_str)))
        # address: 32-byte hex or text (same pattern as tx_codec)
        addr = utxo["address"]
        try:
            raw_addr = bytes.fromhex(addr)
            if len(raw_addr) != 32:
                raise ValueError
            out.write(b"\x00" + raw_addr)
        except ValueError:
            raw_addr = addr.encode("utf-8")
            out.write(b"\x01" + struct.pack("B", len(raw_addr)) + raw_addr)
        out.write(_wire_varint(utxo["amount"]))
    return out.getvalue()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/blockchain", methods=["GET"])
def get_blockchain():
    """
    Binary wire format:
        [4B uint32  chain_length]
        for each block: _encode_block_wire(block)
        [4B uint32  mempool_blob_length]
        [NB         block_txs_to_bytes(mempool)]
    """
    conn = get_db_connection()
    chain, _ = _load_chain_from_db(conn)
    conn.close()
    out = io.BytesIO()
    out.write(struct.pack(">I", len(chain)))
    for block in chain:
        out.write(_encode_block_wire(block))
    mempool_blob = block_txs_to_bytes(blockchain.mempool)
    out.write(struct.pack(">I", len(mempool_blob)))
    out.write(mempool_blob)
    return _wire_ok(out.getvalue())


@app.route("/utxos", methods=["GET"])
def get_utxos():
    """Binary wire format: _encode_utxos_wire(utxos)."""
    return _wire_ok(_encode_utxos_wire(blockchain.utxos))


@app.route("/get_mining_job", methods=["GET"])
def get_mining_job():
    """
    Issue a mining job to a miner.

    Query parameter
    ---------------
    miner_address : str  (required)
        64-char hex SHA-256 of the miner's public key.

    Response  200 OK  —  136-byte binary blob
    -----------------------------------------
        [16B  job_id raw bytes  — send back verbatim in /submit_block]
        [4B   uint32 block_index]
        [112B header  (pack_header, nonce=0; miner overwrites bytes 104-111)]
        [4B   uint32 tx_count  (coinbase + any mempool txs)]

    target and merkle_root are embedded in the header at known offsets:
        target      → bytes [72:104]  (32B big-endian 256-bit integer)
        merkle_root → bytes [36:68]   (32B raw)

    The miner's loop:
        1. Extract 112-byte header from bytes [20:132].
        2. Overwrite bytes [104:112] with a new uint64-BE nonce.
        3. Compute SHA256d(112 bytes).
        4. If result ≤ target (bytes [72:104]) → POST /submit_block.
        5. If nonce exhausted, request a new job (timestamp will have advanced).
    """
    miner_address = request.args.get("miner_address", "").strip()
    if not miner_address:
        return _wire_error("miner_address query parameter is required", 400)
    if len(miner_address) != 64:
        return _wire_error("miner_address must be a 64-char hex string", 400)
    try:
        bytes.fromhex(miner_address)
    except ValueError:
        return _wire_error("miner_address is not valid hex", 400)

    with blockchain.lock:
        block_index = len(blockchain.chain)
        prev_hash   = blockchain.chain[-1]["block_hash"]
        target      = blockchain.calculate_next_target()
        timestamp   = int(time.time())

        # Build coinbase — paying this specific miner
        coinbase_tx = {
            "tx_id":    f"coinbase_{block_index}",
            "inputs":   [],
            "outputs":  [{"address": miner_address, "amount": BLOCK_REWARD}],
        }

        # Select up to 5 mempool transactions
        mempool_txs     = list(blockchain.mempool[:5])
        block_txs       = [coinbase_tx] + mempool_txs

        # Build Merkle root from binary-encoded transactions
        tx_bytes_list   = [tx_to_bytes(tx) for tx in block_txs]
        root_hex        = merkle_root_hex(tx_bytes_list)

        # Pack the full binary header — nonce starts at 0; miner iterates it
        header_bytes = pack_header(
            version           = HEADER_VERSION,
            previous_hash     = prev_hash,
            merkle_root       = root_hex,
            timestamp         = timestamp,
            difficulty_target = target,
            nonce             = 0,
        )

    # Store job outside blockchain.lock — the job store has its own lock
    job_id = _new_job_id()
    _store_job(job_id, {
        "index":        block_index,
        "transactions": block_txs,
        "target":       target,
        "expires_at":   time.time() + JOB_TTL,
    })

    # Binary response: 16B job_id | 4B block_index | 112B header | 4B tx_count
    out = io.BytesIO()
    out.write(bytes.fromhex(job_id))                  # 16B
    out.write(struct.pack(">I", block_index))          # 4B
    out.write(header_bytes)                            # 112B
    out.write(struct.pack(">I", len(block_txs)))       # 4B
    return _wire_ok(out.getvalue())


@app.route("/submit_block", methods=["POST"])
def submit_block():
    """
    Accept a solved block from a miner.

    Request body  —  128-byte binary blob
    --------------------------------------
        [16B  job_id raw bytes  — from /get_mining_job response offset 0]
        [112B solved header     — bytes [104:112] overwritten with winning nonce]

    Response 201  —  36-byte binary blob
    -------------------------------------
        [4B  uint32  block_index]
        [32B block_hash raw]

    Verification sequence
    ---------------------
    1.  Look up the job record by job_id — rejects unknown / expired jobs.
    2.  Decode and unpack the 112-byte header via unpack_header().
    3.  Timestamp checks (future / older-than-previous-block).
    4.  Stale index check (another miner may have already won this height).
    5.  Rebuild the Merkle root from the stored transaction list — if the
        miner tampered with any transaction the root won't match the header
        and the hash check in step 6 will fail.
    6.  SHA256d the raw 112 bytes → compare against difficulty target.
    7.  Re-validate every non-coinbase transaction against the live UTXO set.
    8.  Commit: update chain, UTXOs, mempool, SQLite; delete the job record.
    """
    # ── Parse binary request body: [16B job_id][112B header] ─────────────────
    raw = request.data
    if len(raw) != 16 + HEADER_SIZE:
        return _wire_error(
            f"Request body must be exactly {16 + HEADER_SIZE} bytes "
            f"(16B job_id + {HEADER_SIZE}B header), got {len(raw)}", 400
        )

    current_time = int(time.time())

    # ── Step 1: job lookup (outside blockchain.lock — own lock inside) ───────
    job_id       = raw[:16].hex()
    header_bytes = raw[16:]

    job = _lookup_job(job_id)
    if job is None:
        return _wire_error("Unknown or expired job_id", 400)

    # ── Step 2: decode the solved header ─────────────────────────────────────
    try:
        header = unpack_header(header_bytes)
    except (ValueError, KeyError) as e:
        return _wire_error(f"Invalid header: {e}", 400)

    block_time = header["timestamp"]

    # ── Step 3: timestamp checks ──────────────────────────────────────────────
    if block_time > current_time + MAX_TIMESTAMP_AGE:
        return _wire_error("Block timestamp too far in the future", 400)

    with blockchain.lock:
        if block_time <= blockchain.chain[-1]["timestamp"]:
            return _wire_error("Block timestamp must be greater than previous block", 400)

        # ── Step 4: stale index check ─────────────────────────────────────────
        if job["index"] != len(blockchain.chain):
            _delete_job(job_id)
            return _wire_error("Stale job — a new block was found while you were mining", 400)

        # ── Step 5: Merkle root verification ─────────────────────────────────
        block_txs     = job["transactions"]
        tx_bytes_list = [tx_to_bytes(tx) for tx in block_txs]
        expected_root = merkle_root_hex(tx_bytes_list)

        if header["merkle_root"] != expected_root:
            return _wire_error("Merkle root mismatch — transactions do not match header", 400)

        # ── Step 6: Proof-of-Work check ───────────────────────────────────────
        from block_header import _sha256d
        block_hash = _sha256d(header_bytes).hex()

        if int(block_hash, 16) > job["target"]:
            return _wire_error("Insufficient proof of work", 400)

        if header["previous_hash"] != blockchain.chain[-1]["block_hash"]:
            return _wire_error("previous_hash does not match current chain tip", 400)

        # ── Step 7: validate non-coinbase transactions ────────────────────────
        miner_txs = block_txs[1:]
        try:
            for tx in miner_txs:
                blockchain.validate_transaction(tx, is_mempool_check=False)
        except ValueError as e:
            return _wire_error(f"Block contains invalid transaction: {e}", 400)

        # ── Step 8: commit ────────────────────────────────────────────────────
        accepted_block = {
            "index":             job["index"],
            "previous_hash":     header["previous_hash"],
            "timestamp":         block_time,
            "difficulty_target": job["target"],
            "nonce":             header["nonce"],
            "transactions":      block_txs,
            "block_hash":        block_hash,
        }
        blockchain.chain.append(accepted_block)

        mined_tx_ids    = {tx["tx_id"] for tx in miner_txs}
        spent_utxo_keys = set()
        for tx in block_txs:
            for inp in tx["inputs"]:
                key = f"{inp['tx_id']}:{inp['out_idx']}"
                blockchain.utxos.pop(key, None)
                spent_utxo_keys.add(key)
            for idx, out in enumerate(tx["outputs"]):
                blockchain.utxos[f"{tx['tx_id']}:{idx}"] = out

        blockchain.mempool = [
            tx for tx in blockchain.mempool
            if tx["tx_id"] not in mined_tx_ids
            and not any(
                f"{i['tx_id']}:{i['out_idx']}" in spent_utxo_keys
                for i in tx["inputs"]
            )
        ]

        blockchain._save_block(accepted_block)

    _delete_job(job_id)

    # Binary response: [4B uint32 block_index][32B block_hash]
    return _wire_ok(struct.pack(">I", accepted_block["index"]) + bytes.fromhex(block_hash), 201)


@app.route("/tx_proof/<tx_id>", methods=["GET"])
def get_tx_proof(tx_id):
    """
    Generate a Merkle inclusion proof for a confirmed transaction.

    A light client can use this response to call verify_tx_inclusion()
    locally and confirm the transaction is in a block — without downloading
    the full block or any other transaction.

    Path parameter
    --------------
    tx_id : str
        The transaction ID to prove (64-char hex, or "genesis_tx" /
        "coinbase_<n>" for special transactions).

    Response 200 OK  —  binary blob
    --------------------------------
        [32B  tx_leaf_hash  SHA256d(tx_to_bytes(tx))]
        [4B   uint32  block_index]
        [32B  merkle_root raw]
        [4B   uint32  tx_index  (0 = coinbase)]
        [4B   uint32  block_tx_count]
        [1B   uint8   proof_length  (number of nodes)]
        for each proof node:
            [32B  sibling_hash raw]
            [1B   side: 0x00=LEFT, 0x01=RIGHT]

    Total size: 77 + proof_length * 33 bytes.
    A single-tx block has proof_length=0, so tx_leaf_hash IS the merkle_root.

    Response 404 — binary error envelope (see _wire_error).
    """
    # ── 1. Find the transaction in the database ───────────────────────────────
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT tx_json, block_idx FROM transactions WHERE tx_id = ?",
            (tx_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return _wire_error(f"Transaction {tx_id!r} not found in any confirmed block", 404)

    tx        = json.loads(row["tx_json"])
    block_idx = row["block_idx"]

    # ── 2. Fetch the full ordered transaction list for that block ─────────────
    # We need the list in insertion order (rowid) so the Merkle tree is
    # reconstructed with the same leaf ordering used at mining time.
    conn = get_db_connection()
    try:
        tx_rows = conn.execute(
            "SELECT tx_json FROM transactions WHERE block_idx = ? ORDER BY rowid",
            (block_idx,)
        ).fetchall()
        block_row = conn.execute(
            "SELECT block_hash FROM blocks WHERE idx = ?",
            (block_idx,)
        ).fetchone()
    finally:
        conn.close()

    block_txs = [json.loads(r["tx_json"]) for r in tx_rows]

    # ── 3. Find the position of our target tx in the block ────────────────────
    tx_index = next(
        (i for i, t in enumerate(block_txs) if t["tx_id"] == tx_id),
        None
    )
    if tx_index is None:
        return _wire_error("Transaction found in DB but missing from block — data inconsistency", 500)

    # ── 4. Build binary representations and reconstruct the Merkle tree ───────
    tx_bytes_list = [tx_to_bytes(t) for t in block_txs]
    root_hex      = merkle_root_hex(tx_bytes_list)

    # ── 5. Generate the Merkle proof ──────────────────────────────────────────
    raw_proof = merkle_proof(tx_bytes_list, tx_index)

    # ── 6. Compute this transaction's leaf hash ───────────────────────────────
    leaf_hash_hex = tx_leaf_hash(tx_bytes_list[tx_index])

    # ── 7. Encode binary response ─────────────────────────────────────────────
    out = io.BytesIO()
    out.write(bytes.fromhex(leaf_hash_hex))              # 32B tx_leaf_hash
    out.write(struct.pack(">I", block_idx))              # 4B  block_index
    out.write(bytes.fromhex(root_hex))                   # 32B merkle_root
    out.write(struct.pack(">I", tx_index))               # 4B  tx_index
    out.write(struct.pack(">I", len(block_txs)))         # 4B  block_tx_count
    out.write(struct.pack("B", len(raw_proof)))          # 1B  proof_length
    for node in raw_proof:
        out.write(node.hash)                             # 32B sibling hash
        out.write(b"\x01" if node.side == "RIGHT" else b"\x00")  # 1B side
    return _wire_ok(out.getvalue())


@app.route("/add_transaction", methods=["POST"])
@limiter.limit("10 per minute")
def add_transaction():
    """
    Request body: raw binary transaction (tx_to_bytes / tx_codec wire format).
    Response 201: [32B tx_id raw bytes]
    Response 400: binary error envelope (see _wire_error).
    """
    try:
        tx = bytes_to_tx(request.data)
    except Exception as e:
        return _wire_error(f"Invalid binary transaction: {e}", 400)

    with blockchain.lock:
        try:
            blockchain.validate_transaction(tx, is_mempool_check=True)
            msg = json.dumps(
                {"inputs": tx["inputs"], "outputs": tx["outputs"]}, sort_keys=True
            ).encode()
            tx["tx_id"] = hashlib.sha256(msg).hexdigest()
            blockchain.mempool.append(tx)
            return _wire_ok(bytes.fromhex(tx["tx_id"]), 201)
        except Exception as e:
            return _wire_error(str(e), 400)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765, debug=False)
