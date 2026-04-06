import hashlib
import json
import multiprocessing
import os
import struct
import time
import requests

# --- CONFIGURATION ---
SERVER_URL = "http://localhost:8765"
MINER_ADDRESS = (
    "382e55d49815ac0deebdc665107ca3617e51fa6c487be25e73c3603c555e2bfc"
)
# ---------------------

HEADER_SIZE = 112
_HEADER_FMT = ">I 32s 32s I 32s Q"
_HEADER_STRUCT = struct.Struct(_HEADER_FMT)


def sha256d(data: bytes) -> bytes:
    """Double-SHA256 required by the server."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def mine_worker(
    worker_id, header_bytes, target_int, start_nonce, stride, stats_dict
):
    """A single worker process that iterates through a subset of the nonce space

    and reports its hash count back to a shared dictionary.
    """
    nonce = start_nonce

    # Pre-unpack the static parts of the header to avoid doing it in the loop
    version, prev_hash, merkle, timestamp, difficulty_target, _ = (
        _HEADER_STRUCT.unpack(header_bytes)
    )

    # Local cache of functions for speed
    pack = _HEADER_STRUCT.pack
    s256d = sha256d

    # Update stats every N hashes to reduce process IPC overhead
    batch_size = 5000
    hashes_since_update = 0

    try:
        while True:
            # Re-pack the header with the incremented nonce
            test_header = pack(
                version, prev_hash, merkle, timestamp, difficulty_target, nonce
            )

            # Double SHA-256
            block_hash = s256d(test_header)

            # Check if it meets difficulty
            if int.from_bytes(block_hash, byteorder="big") <= target_int:
                stats_dict[worker_id] = (
                    stats_dict.get(worker_id, 0) + hashes_since_update + 1
                )
                stats_dict["SOLVED"] = (test_header, block_hash.hex())
                return

            nonce += stride
            hashes_since_update += 1

            if hashes_since_update >= batch_size:
                stats_dict[worker_id] = (
                    stats_dict.get(worker_id, 0) + hashes_since_update
                )
                hashes_since_update = 0

    except KeyboardInterrupt:
        return


def solve_block(header_hex, target_hex):
    """Spawns worker processes and calculates live hash rates on the screen."""
    header_bytes = bytes.fromhex(header_hex)
    target_int = int(target_hex, 16)

    num_workers = os.cpu_count() or 1
    print(f"[Mining] Spawning {num_workers} parallel workers...")

    # Using a manager dictionary allows independent processes to share state
    manager = multiprocessing.Manager()
    stats_dict = manager.dict()

    workers = []
    for i in range(num_workers):
        stats_dict[i] = 0
        p = multiprocessing.Process(
            target=mine_worker,
            args=(i, header_bytes, target_int, i, num_workers, stats_dict),
        )
        p.start()
        workers.append(p)

    start_time = time.time()
    last_check_time = start_time
    last_total_hashes = 0

    try:
        while "SOLVED" not in stats_dict:
            time.sleep(1)  # Refresh display every 1 second
            current_time = time.time()

            # Sum up hashes from all workers
            total_hashes = sum(stats_dict[i] for i in range(num_workers))

            # Calculate instantaneous hashrate since the last second
            time_delta = current_time - last_check_time
            hash_delta = total_hashes - last_total_hashes

            if time_delta > 0:
                hashrate = hash_delta / time_delta

                # Format the display nicely
                if hashrate >= 1000:
                    display_rate = f"{hashrate / 1000:.2f} KH/s"
                else:
                    display_rate = f"{hashrate:.2f} H/s"

                print(
                    f"\r[Mining] Total Hashes: {total_hashes:,} | Speed: {display_rate}",
                    end="",
                    flush=True,
                )

                last_check_time = current_time
                last_total_hashes = total_hashes

        # Fetch the solved result pushed by the winning worker
        solved_header, block_hash = stats_dict["SOLVED"]

    finally:
        # Guarantee all processes shut down safely
        for p in workers:
            p.terminate()
            p.join()

    # Clear line for final output
    print()
    return solved_header.hex(), block_hash


def start_mining_loop():
    print("=== CloudyCoin Python Miner ===")
    print(f"Connecting to: {SERVER_URL}")
    print(f"Miner Address: {MINER_ADDRESS}")
    print("===============================\n")

    while True:
        try:
            print("[Network] Requesting job...")
            resp = requests.get(
                f"{SERVER_URL}/get_mining_job",
                params={"miner_address": MINER_ADDRESS},
            )

            if resp.status_code != 200:
                print(f"[Error] Failed to get job: {resp.text}")
                time.sleep(5)
                continue

            job = resp.json()
            print(f"[Job] Received job for Block Height {job['block_index']}")

            start_time = time.time()
            solved_header_hex, block_hash = solve_block(
                job["header_hex"], job["target_hex"]
            )
            elapsed = time.time() - start_time
            print(
                f"[PoW] Solved in {elapsed:.2f}s! Hash: ...{block_hash[-10:]}"
            )

            print("[Network] Submitting block...")
            submit_resp = requests.post(
                f"{SERVER_URL}/submit_block",
                json={
                    "job_id": job["job_id"],
                    "header_hex": solved_header_hex,
                },
            )

            if submit_resp.status_code in (200, 201):
                print("[Success] Block accepted by the server!\n")
            else:
                print(f"[Rejected] Server said: {submit_resp.text}\n")

            time.sleep(1)

        except requests.ConnectionError:
            print("[Error] Could not connect to server. Retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            print(f"[Error] Unexpected error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    start_mining_loop()
