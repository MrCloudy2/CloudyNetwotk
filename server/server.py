import hashlib
import json
import math
import time
import ecdsa
import os
import threading
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# --- RATE LIMITING SETUP ---
# Now strictly used only for transaction validation to prevent CPU exhaustion
# --- RATE LIMITING SETUP ---
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
    # This applies to all routes by default
    default_limits=["30 per minute"]
)

# --- CONFIGURABLE CONSTANTS ---
BLOCK_REWARD = 1
TARGET_BLOCK_TIME = 120
SMOOTHNESS = 5
MAX_TIMESTAMP_AGE = 7200
GENESIS_TARGET = 0x0000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
CHAIN_FILE = "blockchain.json"


def get_address_from_public_key(public_key_hex):
    """Derives an address from a public key (Standard practice)."""
    pk_bytes = bytes.fromhex(public_key_hex)
    # Simple address derivation: SHA256 hash of the public key
    return hashlib.sha256(pk_bytes).hexdigest()

class Blockchain:
    def __init__(self):
        self.lock = threading.Lock()
        self.chain = []
        self.mempool = []
        self.current_job_txs = [] # Holds transactions currently being mined
        self.utxos = {}
        self.target_block_time = TARGET_BLOCK_TIME
        self.smoothness = SMOOTHNESS
        self.current_target = GENESIS_TARGET

        if not self._load_and_verify_chain():
            print("[INFO] Starting a new blockchain from Genesis.")
            self._create_genesis_block()
            self._save_chain()

    def _create_genesis_block(self):
        print("[INFO] Mining Genesis Block... This might take a few seconds.")
        genesis_tx = {
            "tx_id": "genesis_tx",
            "inputs": [],
            "outputs": [{"address": "GodMode", "amount": 1}]
        }

        genesis = {
            "index": 0,
            "previous_hash": "0" * 64,
            "timestamp": int(time.time()),
            "difficulty_target": self.current_target,
            "nonce": 0,
            "transactions": [genesis_tx]
        }

        while True:
            block_hash = self.calculate_hash(genesis)
            if int(block_hash, 16) <= self.current_target:
                genesis["block_hash"] = block_hash
                break
            genesis["nonce"] += 1

        self.chain.append(genesis)
        self.utxos["genesis_tx:0"] = genesis_tx["outputs"][0]
        print(f"[INFO] Genesis Block mined successfully! Hash: {genesis['block_hash']}")

    def _save_chain(self):
        try:
            with open(CHAIN_FILE, "w") as f:
                json.dump(self.chain, f, indent=4)
        except Exception as e:
            print(f"[ERROR] Failed to save blockchain to disk: {e}")

    def _load_and_verify_chain(self):
        if not os.path.exists(CHAIN_FILE):
            return False

        try:
            with open(CHAIN_FILE, "r") as f:
                loaded_chain = json.load(f)

            if not loaded_chain:
                return False

            temp_utxos = {}
            previous_hash = "0" * 64

            print("[INFO] Validating blockchain from disk...")
            for block in loaded_chain:
                if block["previous_hash"] != previous_hash:
                    return False

                calculated_hash = self.calculate_hash(block)
                if calculated_hash != block["block_hash"]:
                    return False

                if int(calculated_hash, 16) > block["difficulty_target"]:
                    return False

                for tx in block["transactions"]:
                    for inp in tx["inputs"]:
                        utxo_key = f"{inp['tx_id']}:{inp['out_idx']}"
                        temp_utxos.pop(utxo_key, None)

                    for idx, out in enumerate(tx["outputs"]):
                        temp_utxos[f"{tx['tx_id']}:{idx}"] = out

                previous_hash = block["block_hash"]

            self.chain = loaded_chain
            self.utxos = temp_utxos
            self.current_target = self.calculate_next_target()

            print(f"[SUCCESS] Loaded {len(self.chain)} blocks. UTXO set rebuilt.")
            return True

        except Exception as e:
            print(f"[ERROR] Corrupted blockchain file: {e}")
            return False

    def calculate_hash(self, block):
        header = {
            "index": block["index"],
            "previous_hash": block["previous_hash"],
            "timestamp": block["timestamp"],
            "difficulty_target": block["difficulty_target"],
            "nonce": block["nonce"],
            # SECURITY FIX: Transactions are now mathematically bound to the block's hash
            "transactions": block.get("transactions", [])
        }
        encoded = json.dumps(header, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def calculate_next_target(self):
        """
        Difficulty adjustment based on the last 10 solving times.

        BUG FIX: the original code did  int(target * float_ratio)  where
        target is a ~252-bit integer.  Python must convert it to float64
        (53-bit mantissa) for that multiply, silently discarding ~200 bits
        of precision.  On unlucky rounding the result can be 0 or negative,
        which then propagates to every subsequent block as -1 in JSON.

        Fix: keep everything as Python integers throughout.  We express the
        ±25 % clamp in integer arithmetic using *100 scaling, then do one
        integer multiply + floor-divide.  No floats touch the target value.
        """
        if len(self.chain) < 2:
            return GENESIS_TARGET

        blocks_to_count = min(len(self.chain) - 1, 10)
        reference_block = self.chain[-1 - blocks_to_count]
        latest_block    = self.chain[-1]

        actual_time  = latest_block["timestamp"] - reference_block["timestamp"]
        actual_time  = max(1, actual_time)
        expected_time = self.target_block_time * blocks_to_count

        # Sanitise the stored target — if a previous bug left a bad value,
        # fall back to GENESIS_TARGET so we never propagate garbage.
        last_target = self.chain[-1].get("difficulty_target", GENESIS_TARGET)
        if not isinstance(last_target, int) or last_target <= 0:
            print(f"[WARN] Bad difficulty_target in chain tip ({last_target!r}), "
                  f"resetting to GENESIS_TARGET.")
            last_target = GENESIS_TARGET

        # Clamp actual_time to [75 %, 125 %] of expected — pure integer maths.
        # Scale everything ×100 to avoid fractions.
        lo = (expected_time * 75)  // 100   # 0.75 × expected
        hi = (expected_time * 125) // 100   # 1.25 × expected
        clamped_actual = max(lo, min(actual_time, hi))

        # new_target = last_target × (clamped_actual / expected_time)
        # Done entirely in integers with floor division.
        new_target = (last_target * clamped_actual) // expected_time

        # Hard bounds: never zero (unsolvable) or above genesis (too easy).
        MIN_TARGET = GENESIS_TARGET >> 32   # ~32 leading zero bits harder than genesis
        new_target = max(MIN_TARGET, min(new_target, GENESIS_TARGET))

        return new_target






    def validate_transaction(self, tx, is_mempool_check=True):
        """
        Validates a transaction's integrity, ownership, and balance.
        """
        total_in = 0
        total_out = sum(out["amount"] for out in tx["outputs"])

        if total_out <= 0:
            raise ValueError("Invalid output amount")

        # --- THE FIX: Create a sanitized version of inputs without signatures ---
        sanitized_inputs = []
        for inp in tx["inputs"]:
            sanitized_inp = {
                "tx_id": inp["tx_id"],
                "out_idx": inp["out_idx"],
                "public_key": inp["public_key"]
                # Notice we intentionally omit the "signature" key here!
            }
            sanitized_inputs.append(sanitized_inp)

        # Reconstruct the exact message the client should have signed
        msg_data = {"inputs": sanitized_inputs, "outputs": tx["outputs"]}
        msg = json.dumps(msg_data, sort_keys=True).encode()

        input_keys = set()

        # We still loop over the original `tx["inputs"]` because we need to extract
        # the signature string to actually verify it against `msg`.
        for inp in tx["inputs"]:
            utxo_key = f"{inp['tx_id']}:{inp['out_idx']}"

            if utxo_key not in self.utxos:
                raise ValueError(f"UTXO {utxo_key} not found or already spent")

            utxo = self.utxos[utxo_key]

            derived_address = get_address_from_public_key(inp["public_key"])
            if derived_address != utxo["address"]:
                raise ValueError("Public key does not match UTXO owner address")

            # SIGNATURE CHECK using the sanitized `msg`
            try:
                vk = ecdsa.VerifyingKey.from_string(bytes.fromhex(inp["public_key"]), curve=ecdsa.SECP256k1)
                # We verify the signature provided against the signature-less payload
                if not vk.verify(bytes.fromhex(inp["signature"]), msg):
                    raise ValueError("Invalid signature")
            except Exception:
                raise ValueError("Signature verification failed")

            # ... (Rest of your mempool and balance checks remain exactly the same) ...

            if is_mempool_check:
                if any(f"{i['tx_id']}:{i['out_idx']}" == utxo_key for m in self.mempool for i in m["inputs"]):
                    raise ValueError("Double spend detected in mempool")

            total_in += utxo["amount"]
            input_keys.add(utxo_key)

        if total_in < total_out:
            raise ValueError("Insufficient funds")

        return True

blockchain = Blockchain()

# --- API ENDPOINTS ---

@app.route("/blockchain", methods=["GET"])
def get_blockchain():
    return jsonify({"chain": blockchain.chain, "mempool": blockchain.mempool}), 200

@app.route("/utxos", methods=["GET"])
def get_utxos():
    return jsonify(blockchain.utxos), 200

@app.route("/get_mining_job", methods=["GET"])
def get_mining_job():
    with blockchain.lock: # Lock during read/prepare
        # We send the top 5 txs but LEAVE them in mempool so they aren't lost if miner fails
        job_txs = list(blockchain.mempool[:5])
        job = {
            "index": len(blockchain.chain),
            "previous_hash": blockchain.chain[-1]["block_hash"],
            "difficulty_target": blockchain.calculate_next_target(),
            "transactions": job_txs,
            "block_reward": BLOCK_REWARD
        }
    return jsonify(job), 200

@app.route("/submit_block", methods=["POST"])
def submit_block():
    data = request.get_json()
    current_time = int(time.time())
    with blockchain.lock:
        # --- NEW TIMESTAMP SECURITY CHECK ---
        block_time = data.get("timestamp", 0)

        # 1. Reject if the block is from the "future" (beyond your 7200s limit)
        if block_time > current_time + MAX_TIMESTAMP_AGE:
            return jsonify({"message": "Block timestamp too far in the future"}), 400

        # 2. Reject if the block is older than the previous block
        # (Standard blockchain rule: time must strictly move forward)
        if len(blockchain.chain) > 0:
            if block_time <= blockchain.chain[-1]["timestamp"]:
                return jsonify({"message": "Block timestamp must be greater than previous block"}), 400


        # 1. Basic stale check
        if data.get("index") != len(blockchain.chain):
            return jsonify({"message": "Block index is stale"}), 400

        # 2. Reconstruct Coinbase (The miner's reward)
        coinbase_tx = {
            "tx_id": f"coinbase_{data['index']}",
            "inputs": [],
            "outputs": [{"address": data["reward_address"], "amount": BLOCK_REWARD}]
        }

        miner_txs = data.get("transactions", [])
        block_transactions = [coinbase_tx] + miner_txs

        # 3. CRITICAL FIX: Validate every transaction in the submitted block
        # We skip the coinbase (index 0) because it has no inputs to verify
        try:
            for tx in miner_txs:
                # is_mempool_check=False because these are already in a block
                blockchain.validate_transaction(tx, is_mempool_check=False)
        except ValueError as e:
            return jsonify({"message": f"Block contains invalid transaction: {str(e)}"}), 400

        # 4. Construct the candidate block for hash verification
        candidate = {
            "index": data["index"],
            "previous_hash": blockchain.chain[-1]["block_hash"],
            "timestamp": data["timestamp"],
            "difficulty_target": blockchain.calculate_next_target(),
            "nonce": data["nonce"],
            "transactions": block_transactions
        }

        # 5. Proof of Work Check
        block_hash = blockchain.calculate_hash(candidate)
        if int(block_hash, 16) <= candidate["difficulty_target"]:
            candidate["block_hash"] = block_hash
            blockchain.chain.append(candidate)

            # 6. Cleanup Mempool & Update UTXOs
            # Remove any tx from mempool if its ID was mined OR if its inputs were spent
            mined_tx_ids = [tx.get("tx_id") for tx in miner_txs]
            spent_utxo_keys = []

            for tx in block_transactions:
                # Remove spent inputs from global UTXO set
                for inp in tx["inputs"]:
                    key = f"{inp['tx_id']}:{inp['out_idx']}"
                    blockchain.utxos.pop(key, None)
                    spent_utxo_keys.append(key)

                # Add new outputs to global UTXO set
                for idx, out in enumerate(tx["outputs"]):
                    blockchain.utxos[f"{tx['tx_id']}:{idx}"] = out

            # Filter mempool: remove mined transactions AND transactions that now have invalid inputs
            blockchain.mempool = [
                tx for tx in blockchain.mempool
                if tx["tx_id"] not in mined_tx_ids and
                not any(f"{i['tx_id']}:{i['out_idx']}" in spent_utxo_keys for i in tx["inputs"])
            ]

            blockchain._save_chain()
            return jsonify({"message": "Accepted", "index": candidate["index"]}), 201

    return jsonify({"message": "Invalid Proof of Work"}), 400

@app.route("/add_transaction", methods=["POST"])
@limiter.limit("10 per minute")
def add_transaction():
    data = request.get_json()
    tx = data.get("transaction")

    with blockchain.lock:
        try:
            blockchain.validate_transaction(tx, is_mempool_check=True)

            # Re-calculate ID based on validated content
            msg = json.dumps({"inputs": tx["inputs"], "outputs": tx["outputs"]}, sort_keys=True).encode()
            tx["tx_id"] = hashlib.sha256(msg).hexdigest()

            blockchain.mempool.append(tx)
            return jsonify({"message": "Transaction added to mempool"}), 201
        except Exception as e:
            return jsonify({"message": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765, debug=False)
