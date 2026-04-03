This README.md is designed for developers who want to build their own custom mining software for your blockchain. It explains the cryptographic "handshake" required to get a block accepted by the server.
⛏️ Mining Guide: How to Build a Miner

Welcome, Miner! This guide explains the technical specifications for interacting with the Cloudy Blockchain at http://cloudy.freemyip.com:8765.

To successfully mine a block, your software must follow a strict lifecycle of Fetching, Constructing, Hashing, and Submitting.
1. The Mining Lifecycle

The process follows these four steps:
Phase A: Fetching the Job

Send a GET request to /get_mining_job.
The server will return a JSON object containing:

    index: The height of the block you are working on.

    previous_hash: The hash of the last confirmed block.

    difficulty_target: A large integer. Your block's hash must be less than or equal to this number.

    transactions: A list of user transactions currently in the mempool.

    block_reward: The current subsidy for solving a block.

Phase B: Block Construction

Before you start hashing, you must build the block header exactly how the server expects it.

    The Coinbase Transaction: You must create a special transaction called the "coinbase."

        tx_id: Must be the string "coinbase_" plus the block index (e.g., coinbase_5).

        inputs: Must be an empty list [].

        outputs: A list containing one object with your address and the amount (the block_reward from the job).

    Transaction Ordering: You must put the Coinbase Transaction at Index 0 of your transaction list, followed by the transactions provided by the server.

Phase C: The Proof-of-Work Loop

You are looking for a nonce (a random number) that satisfies the difficulty.
The block data is a JSON dictionary containing:

    index, previous_hash, timestamp, difficulty_target, nonce, and transactions.

Crucial: When hashing, the JSON must be serialized with alphabetical key sorting to ensure the hash matches the server's calculation.
Phase D: Submission

Once you find a hash where Block_Hash≤Target, send a POST request to /submit_block.
2. API Specification
[GET] /get_mining_job

Request:
HTTP

GET http://cloudy.freemyip.com:8765/get_mining_job

Response:
JSON

{
  "index": 10,
  "previous_hash": "0000abc...",
  "difficulty_target": 2695953520...,
  "transactions": [...],
  "block_reward": 1
}

[POST] /submit_block

Payload:
JSON

{
  "nonce": 12345,
  "timestamp": 1712170000,
  "reward_address": "YOUR_WALLET_ADDRESS",
  "index": 10,
  "transactions": [...] 
}

Note: transactions here should be the list provided by the server (do not include the coinbase here, the server adds it automatically based on your reward_address).
3. Implementation Details (Python Example)

To ensure your hashes match the server, use this logic:
Python

import hashlib
import json

def calculate_hash(block_data):
    # Sort keys is MANDATORY
    encoded_data = json.dumps(block_data, sort_keys=True).encode()
    return hashlib.sha256(encoded_data).hexdigest()

# Your loop:
while int(calculate_hash(candidate), 16) > target:
    candidate['nonce'] += 1

4. Important Rules

    Stale Blocks: If another miner solves the block while you are working, your submission will be rejected. It is recommended to check /blockchain or re-fetch the job every 1,000,000 nonces.

    Timestamping: Your timestamp must be greater than the previous block's timestamp and no more than 2 hours in the future.

    Rate Limiting: The server allows 30 requests per minute. Avoid spamming /get_mining_job in a tight loop; only fetch a new job when you finish a block or see the height change.
