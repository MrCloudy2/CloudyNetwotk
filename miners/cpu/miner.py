import sys
import json
import time
import ctypes
import requests
import hashlib

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
SERVER_URL = "http://cloudy.freemyip.com:8765"
WALLET_ADDRESS = "382e55d49815ac0deebdc665107ca3617e51fa6c487be25e73c3603c555e2bfc"

try:
    miner_lib = ctypes.CDLL("./libminer.so")
except Exception as e:
    print(f"Error loading libminer.so: {e}")
    sys.exit(1)

miner_lib.mine_c.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,  # Prefix
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,  # Suffix
    ctypes.POINTER(ctypes.c_ubyte),                # Target bytes
    ctypes.c_ulonglong,                            # Start nonce
    ctypes.c_ulonglong,                            # Attempts
    ctypes.POINTER(ctypes.c_ubyte)                 # Output hash buffer
]
miner_lib.mine_c.restype = ctypes.c_longlong

def display_human_difficulty(target_int):
    # 1. Calculate Leading Hex Zeros
    # A full SHA-256 hash is 64 hex characters long.
    target_hex = hex(target_int)[2:].zfill(64)
    leading_zeros = 64 - len(hex(target_int)[2:])

    # 2. Calculate Difficulty Score
    # We define '1' as the easiest possible target (all bits set to 1)
    max_target = 2**256 - 1
    difficulty_score = max_target / target_int if target_int > 0 else float('inf')

    # 3. Calculate "Bits" (as seen in your ExplorerTab)
    # This shows how many bits of the 256-bit hash are effectively 'available'
    bits = target_int.bit_length()

    print("=" * 40)
    print(f"DIFFICULTY REPORT")
    print("-" * 40)
    print(f"Difficulty Score : {difficulty_score:,.2f}")
    print(f"Leading Zeros    : {leading_zeros} (Hex characters)")
    print(f"Network Bits     : {bits} bits")
    print(f"Raw Target Hex   : 0x{target_hex[:16]}...")
    print("=" * 40)

# Example usage with a target from your wallet.py logic:
# target = job["difficulty_target"]
# display_human_difficulty(target)


def calculate_hash_pure(block: dict) -> str:
    """Exact replica of the wallet.py algorithm for double-checking."""
    header = {
        "index":             block["index"],
        "previous_hash":     block["previous_hash"],
        "timestamp":         block["timestamp"],
        "difficulty_target": block["difficulty_target"],
        "nonce":             block["nonce"],
        "transactions":      block["transactions"],
    }
    raw = json.dumps(header, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()

def main():
    print("=" * 60)
    print("  SUPERCHARGED HYBRID MINER (STATE CACHING + RATE LIMITED)")
    print("=" * 60)
    print(f"Mining to address: {WALLET_ADDRESS}\n")

    while True:
        try:
            job = requests.get(f"{SERVER_URL}/get_mining_job", timeout=3).json()
        except Exception as e:
            print(f"Error fetching job: {e}. Retrying in 8s...")
            time.sleep(8)
            continue

        index = job["index"]
        prev_hash = job["previous_hash"]
        target = job["difficulty_target"]
        mempool_txs = job["transactions"]
        reward = job["block_reward"]
        timestamp = int(time.time())

        # --- CALL THE FUNCTION HERE ---
        display_human_difficulty(target)
        # ------------------------------


        print(f"▶ Block #{index} started | Target: {hex(target)[:16]}... | Txs: {len(mempool_txs)}")

        coinbase = {
            "tx_id": f"coinbase_{index}",
            "inputs": [],
            "outputs": [{"address": WALLET_ADDRESS, "amount": reward}],
        }
        block_txs = [coinbase] + mempool_txs

        # 1. Create a dummy block with an empty string as the nonce
        dummy_header = {
            "index": index,
            "previous_hash": prev_hash,
            "timestamp": timestamp,
            "difficulty_target": target,
            "nonce": "",
            "transactions": block_txs,
        }

        # 2. Dump EXACTLY like wallet.py
        template_bytes = json.dumps(dummy_header, sort_keys=True, ensure_ascii=False).encode()

        # 3. Split by the quoted empty string to extract perfect prefix and suffix
        prefix_bytes, suffix_bytes = template_bytes.split(b'""')

        c_prefix = (ctypes.c_ubyte * len(prefix_bytes)).from_buffer_copy(prefix_bytes)
        c_suffix = (ctypes.c_ubyte * len(suffix_bytes)).from_buffer_copy(suffix_bytes)

        target_bytes_arr = target.to_bytes(32, byteorder='big')
        c_target = (ctypes.c_ubyte * 32).from_buffer_copy(target_bytes_arr)

        out_hash = (ctypes.c_ubyte * 32)()

        nonce = 0
        batch_size = 10_000_000  # Increased batch size since C++ is much faster now
        t0 = time.time()

        # Track when we last asked the server for a job to avoid rate limits
        last_network_check = time.time()

        while True:
            # Delegate heavy lifting to C++
            result = miner_lib.mine_c(
                c_prefix, len(prefix_bytes),
                c_suffix, len(suffix_bytes),
                c_target,
                nonce,
                batch_size,
                out_hash
            )

            # RATE LIMIT PROTECTOR: Only check network if 8 seconds have passed
            current_time = time.time()
            if current_time - last_network_check >= 8.0:
                try:
                    current_job = requests.get(f"{SERVER_URL}/get_mining_job", timeout=2).json()
                    last_network_check = time.time() # Reset timer

                    if current_job["index"] != index:
                        print(f"\n⏹ Block #{index} stale. Someone else solved it.")
                        break
                except:
                    last_network_check = time.time() # Reset timer even on fail so we don't spam
                    pass

            # C++ Found a winning nonce!
            if result != -1:
                winning_nonce = result
                dummy_header["nonce"] = winning_nonce

                final_hash = calculate_hash_pure(dummy_header)

                if int(final_hash, 16) <= target:
                    print(f"\n🎉 SUCCESS! Winning Nonce verified: {winning_nonce}")
                    print(f"Verified Hash: {final_hash}")

                    submit_payload = {
                        "nonce": winning_nonce,
                        "timestamp": timestamp,
                        "reward_address": WALLET_ADDRESS,
                        "index": index,
                        "transactions": mempool_txs
                    }

                    try:
                        resp = requests.post(
                            f"{SERVER_URL}/submit_block",
                            json=submit_payload,
                            timeout=5
                        )
                        print(f"📡 Submission Response: {resp.status_code} - {resp.text}")
                    except Exception as e:
                        print(f"❌ Failed to submit block: {e}")

                    break
                else:
                    print(f"\n⚠️ Math mismatch! Python Hash: {final_hash}")

            nonce += batch_size
            elapsed = max(time.time() - t0, 0.001)
            print(f" ⛏  Hashes: {nonce:,} | Speed: {nonce / elapsed / 1000_000:.2f} MH/s", end="\r")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMiner stopped by user.")
        sys.exit(0)
