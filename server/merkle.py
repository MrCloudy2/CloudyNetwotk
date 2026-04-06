r"""
merkle.py — Merkle Tree for CloudyCoin binary transactions
===========================================================

How the tree is built
---------------------
Given N binary transactions (as produced by tx_codec.tx_to_bytes), the tree
is constructed bottom-up:

  1. LEAF LAYER — each transaction is double-SHA256'd into a 32-byte leaf hash.
     Double hashing (SHA256d) is used because it defeats length-extension attacks
     on the hash function — the same reason Bitcoin uses it.

  2. PAIR & HASH — adjacent leaves are concatenated and double-SHA256'd to form
     the next layer up. This repeats until one hash remains: the Merkle Root.

  3. ODD NODE RULE — if a layer has an odd number of nodes, the last node is
     duplicated before pairing. This matches Bitcoin's behaviour exactly.

  4. SINGLE TX — if there is exactly one transaction, its leaf hash IS the root.

Visual example (4 transactions):

    Tx0   Tx1   Tx2   Tx3           ← raw binary transactions
     │     │     │     │
    H0    H1    H2    H3            ← leaf layer  (SHA256d of each tx)
      \   /       \   /
      H01         H23              ← inner layer  (SHA256d of H0‖H1, H2‖H3)
         \       /
          H0123                    ← Merkle Root

Visual example (3 transactions — odd node duplication):

    Tx0   Tx1   Tx2
     │     │     │
    H0    H1    H2  H2*            ← H2 duplicated to make even
      \   /       \  /
      H01         H22             ← inner layer
         \       /
          H0122                   ← Merkle Root

Merkle Proof
------------
A proof that transaction i is in the tree consists of the sibling hash at
each level, plus a direction bit (LEFT or RIGHT) so the verifier knows
which side to place their running hash when re-hashing.

To verify: start with leaf hash of Tx_i, then for each (sibling, direction)
pair in the proof, compute SHA256d(running ‖ sibling) or SHA256d(sibling ‖
running) according to the direction, until you reach the root.  If the
reconstructed root matches the block's Merkle Root, the proof is valid.

Public API
----------
  merkle_root(tx_bytes_list)          → bytes (32)
  merkle_proof(tx_bytes_list, index)  → list[ProofNode]
  verify_proof(leaf_bytes, proof, root_bytes) → bool

  ProofNode is a named tuple: (hash: bytes, side: str)  side ∈ {"LEFT","RIGHT"}
"""

import hashlib
from collections import namedtuple
from typing import List

# ---------------------------------------------------------------------------
# Core primitive
# ---------------------------------------------------------------------------

def _sha256d(data: bytes) -> bytes:
    """Double-SHA256: the standard hash primitive throughout this module."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _leaf(tx_bytes: bytes) -> bytes:
    """Hash a raw binary transaction into its Merkle leaf."""
    return _sha256d(tx_bytes)


def _combine(left: bytes, right: bytes) -> bytes:
    """Hash two child nodes into their parent."""
    return _sha256d(left + right)


# ---------------------------------------------------------------------------
# Layer builder
# ---------------------------------------------------------------------------

def _build_tree(leaves: List[bytes]) -> List[List[bytes]]:
    """
    Build the full Merkle tree and return every layer as a list of lists.

    layers[0] is the leaf layer, layers[-1] is [root].
    Storing every layer is what makes proof generation O(log N) per query
    without recomputing anything.
    """
    if not leaves:
        raise ValueError("Cannot build a Merkle tree with zero transactions")

    layers = [list(leaves)]           # layer 0 = leaves

    current = layers[0]
    while len(current) > 1:
        # Duplicate the last node if the layer is odd-length
        if len(current) % 2 == 1:
            current = current + [current[-1]]   # don't mutate original

        next_layer = []
        for i in range(0, len(current), 2):
            next_layer.append(_combine(current[i], current[i + 1]))

        layers.append(next_layer)
        current = next_layer

    return layers


# ---------------------------------------------------------------------------
# Public: Merkle Root
# ---------------------------------------------------------------------------

def merkle_root(tx_bytes_list: List[bytes]) -> bytes:
    """
    Compute the Merkle Root for a list of binary-encoded transactions.

    Parameters
    ----------
    tx_bytes_list : list[bytes]
        Raw binary transactions as produced by tx_codec.tx_to_bytes().
        Must contain at least one transaction.

    Returns
    -------
    bytes
        32-byte Merkle Root.
    """
    leaves = [_leaf(tx) for tx in tx_bytes_list]
    layers = _build_tree(leaves)
    return layers[-1][0]


# ---------------------------------------------------------------------------
# Merkle Proof
# ---------------------------------------------------------------------------

ProofNode = namedtuple("ProofNode", ["hash", "side"])
"""
A single node in a Merkle proof.

Attributes
----------
hash : bytes
    The 32-byte sibling hash at this level.
side : str
    "LEFT"  — sibling goes on the LEFT  → parent = SHA256d(sibling ‖ running)
    "RIGHT" — sibling goes on the RIGHT → parent = SHA256d(running  ‖ sibling)
"""


def merkle_proof(tx_bytes_list: List[bytes], index: int) -> List[ProofNode]:
    """
    Generate a Merkle Proof showing that tx_bytes_list[index] is in the tree.

    Parameters
    ----------
    tx_bytes_list : list[bytes]
        The full list of binary transactions in the block, same order as
        they appear in the block (coinbase first).
    index : int
        Zero-based index of the transaction you want to prove.

    Returns
    -------
    list[ProofNode]
        Ordered list of (sibling_hash, side) pairs, from leaf level up to
        (but not including) the root.  An empty list means the transaction
        IS the root (single-tx block).

    Raises
    ------
    IndexError
        If index is out of range.
    """
    n = len(tx_bytes_list)
    if not 0 <= index < n:
        raise IndexError(f"index {index} out of range for {n} transaction(s)")

    leaves = [_leaf(tx) for tx in tx_bytes_list]
    layers = _build_tree(leaves)

    proof: List[ProofNode] = []
    current_idx = index

    for layer in layers[:-1]:           # every layer except the root
        # Apply the same odd-duplication rule used during tree construction
        if len(layer) % 2 == 1:
            layer = layer + [layer[-1]]

        if current_idx % 2 == 0:       # current node is LEFT child
            sibling_idx = current_idx + 1
            proof.append(ProofNode(hash=layer[sibling_idx], side="RIGHT"))
        else:                           # current node is RIGHT child
            sibling_idx = current_idx - 1
            proof.append(ProofNode(hash=layer[sibling_idx], side="LEFT"))

        current_idx //= 2              # move up to parent index

    return proof


# ---------------------------------------------------------------------------
# Public: Proof Verification
# ---------------------------------------------------------------------------

def verify_proof(tx_bytes: bytes, proof: List[ProofNode], root: bytes) -> bool:
    """
    Verify that a transaction is included in a block without needing the
    full transaction list — only the proof and the known Merkle Root.

    Parameters
    ----------
    tx_bytes : bytes
        The raw binary transaction to verify (the one you want to prove).
    proof : list[ProofNode]
        The proof returned by merkle_proof().
    root : bytes
        The 32-byte Merkle Root stored in the block header.

    Returns
    -------
    bool
        True if the transaction is provably in the block, False otherwise.
    """
    running = _leaf(tx_bytes)

    for node in proof:
        if node.side == "RIGHT":
            running = _combine(running, node.hash)
        else:
            running = _combine(node.hash, running)

    return running == root


# ---------------------------------------------------------------------------
# Public: verify_tx_inclusion  (hex API — for servers and light clients)
# ---------------------------------------------------------------------------

def verify_tx_inclusion(
    tx_hash: str,
    proof: list,
    merkle_root_h: str,
) -> bool:
    """
    Verify that a transaction is included in a block using only its hash,
    a Merkle proof, and the block's Merkle root — without downloading any
    other transaction in the block.

    This is the primary light-client verification function.  It accepts the
    hex-string types that travel over the JSON API, so callers never need
    to deal with raw bytes.

    Parameters
    ----------
    tx_hash : str
        64-char hex string.  The SHA256d leaf hash of the transaction —
        i.e. SHA256d(tx_to_bytes(tx)).  The server returns this as
        "tx_leaf_hash" in the GET /tx_proof/<tx_id> response.

        Accepting the pre-hashed leaf (rather than the raw transaction)
        keeps this function usable by minimal clients that only store
        tx_ids and hashes, not full transaction bytes.  The server is
        trusted to supply the correct leaf hash; a full client can
        independently recompute it from the raw binary transaction via
        tx_codec.tx_to_bytes() + merkle._leaf().

    proof : list[dict]
        Ordered list of proof nodes from leaf level up to (but not
        including) the root.  Each node is a dict with two keys:

            {
                "hash": "<64-char hex>",   # sibling hash at this level
                "side": "LEFT" | "RIGHT"   # which side the sibling sits on
            }

        An empty list is valid — it means the transaction is the only one
        in the block, so its leaf hash IS the root.

        This is the JSON-serialisable form of the internal ProofNode
        namedtuple.  The server endpoint GET /tx_proof/<tx_id> returns
        proof nodes in exactly this format.

    merkle_root_h : str
        64-char hex string of the Merkle root embedded in the block header.
        A light client that trusts block headers (e.g. after validating PoW)
        can read this directly from the 112-byte binary header at offset
        36–68 via block_header.unpack_header().

    Returns
    -------
    bool
        True  — the proof is cryptographically valid; the transaction was
                 provably included in the block that produced merkle_root_h.
        False — the proof is invalid; the transaction was NOT included, or
                the proof / root is corrupt.

    Raises
    ------
    ValueError
        If any hex string is malformed or any "side" value is not
        "LEFT" / "RIGHT".  Raises rather than returning False so callers
        can distinguish a bad proof from a wrong proof.

    Notes
    -----
    Security properties
    ~~~~~~~~~~~~~~~~~~~
    * Second-preimage resistance of SHA256d means an attacker cannot
      produce a different transaction that hashes to the same leaf.
    * The proof is O(log N) hashes — 5 hashes for a block with 32 txs,
      20 hashes for a block with 1 000 000 txs.
    * This function is self-contained: it performs no network calls, reads
      no database, and has no side effects.  It is safe to call from any
      context, including browser-compiled Python (Pyodide) or a CLI wallet.

    Example
    -------
    >>> from merkle import verify_tx_inclusion
    >>> verify_tx_inclusion(
    ...     tx_hash       = "a1b2c3...",   # 64 hex chars
    ...     proof         = [
    ...         {"hash": "d4e5f6...", "side": "RIGHT"},
    ...         {"hash": "a7b8c9...", "side": "LEFT"},
    ...     ],
    ...     merkle_root_h = "ff00aa...",   # from block header offset 36
    ... )
    True
    """
    # ── Decode and validate inputs ────────────────────────────────────────────
    if len(tx_hash) != 64:
        raise ValueError(
            f"tx_hash must be 64 hex chars (got {len(tx_hash)})"
        )
    try:
        running = bytes.fromhex(tx_hash)
    except ValueError:
        raise ValueError(f"tx_hash is not valid hex: {tx_hash!r}")

    if len(merkle_root_h) != 64:
        raise ValueError(
            f"merkle_root must be 64 hex chars (got {len(merkle_root_h)})"
        )
    try:
        expected_root = bytes.fromhex(merkle_root_h)
    except ValueError:
        raise ValueError(f"merkle_root is not valid hex: {merkle_root_h!r}")

    # ── Walk the proof, re-hashing toward the root ────────────────────────────
    for i, node in enumerate(proof):
        sibling_hex = node.get("hash", "") if isinstance(node, dict) else getattr(node, "hash", b"").hex()
        side        = node.get("side", "") if isinstance(node, dict) else getattr(node, "side", "")

        if side not in ("LEFT", "RIGHT"):
            raise ValueError(
                f"proof[{i}].side must be 'LEFT' or 'RIGHT', got {side!r}"
            )
        if len(sibling_hex) != 64:
            raise ValueError(
                f"proof[{i}].hash must be 64 hex chars (got {len(sibling_hex)})"
            )
        try:
            sibling = bytes.fromhex(sibling_hex)
        except ValueError:
            raise ValueError(f"proof[{i}].hash is not valid hex")

        if side == "RIGHT":
            running = _combine(running, sibling)
        else:
            running = _combine(sibling, running)

    return running == expected_root


def tx_leaf_hash(tx_bytes: bytes) -> str:
    """
    Return the 64-char hex Merkle leaf hash for a binary-encoded transaction.

    This is the value to pass as tx_hash to verify_tx_inclusion() when the
    caller has the full raw transaction available.

    SHA256d(tx_to_bytes(tx)) — identical to what _build_tree() computes
    internally for each leaf.
    """
    return _leaf(tx_bytes).hex()


# ---------------------------------------------------------------------------
# Integration helper: hex root for block headers
# ---------------------------------------------------------------------------

def merkle_root_hex(tx_bytes_list: List[bytes]) -> str:
    """
    Same as merkle_root() but returns a 64-character hex string, ready to
    drop into calculate_hash() in server1.py as a block header field.
    """
    return merkle_root(tx_bytes_list).hex()


# ---------------------------------------------------------------------------
# Self-test  (python merkle.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os, json

    def _rnd_tx():
        """Minimal fake binary transaction for testing."""
        return os.urandom(130)          # realistic-ish size

    def _ok(label, result):
        mark = "PASS ✓" if result else "FAIL ✗"
        print(f"[{mark}] {label}")
        return result

    all_pass = True
    print("=" * 62)

    # ── 1. Single transaction ────────────────────────────────────────
    txs1 = [_rnd_tx()]
    root1 = merkle_root(txs1)
    proof1 = merkle_proof(txs1, 0)
    all_pass &= _ok("single tx: proof is empty", proof1 == [])
    all_pass &= _ok("single tx: verify", verify_proof(txs1[0], proof1, root1))

    # ── 2. Two transactions ──────────────────────────────────────────
    txs2 = [_rnd_tx(), _rnd_tx()]
    root2 = merkle_root(txs2)
    for i in range(2):
        proof = merkle_proof(txs2, i)
        all_pass &= _ok(f"2-tx: verify index {i}", verify_proof(txs2[i], proof, root2))
        all_pass &= _ok(f"2-tx: wrong tx rejected (index {i})",
                        not verify_proof(_rnd_tx(), proof, root2))

    # ── 3. Odd number of transactions (5) ────────────────────────────
    txs5 = [_rnd_tx() for _ in range(5)]
    root5 = merkle_root(txs5)
    for i in range(5):
        proof = merkle_proof(txs5, i)
        all_pass &= _ok(f"5-tx:  verify index {i}", verify_proof(txs5[i], proof, root5))

    # ── 4. Even number of transactions (8) ──────────────────────────
    txs8 = [_rnd_tx() for _ in range(8)]
    root8 = merkle_root(txs8)
    for i in range(8):
        proof = merkle_proof(txs8, i)
        all_pass &= _ok(f"8-tx:  verify index {i}", verify_proof(txs8[i], proof, root8))

    # ── 5. Root is deterministic ─────────────────────────────────────
    all_pass &= _ok("deterministic root",
                    merkle_root(txs8) == merkle_root(txs8))

    # ── 6. Order matters ────────────────────────────────────────────
    reversed_root = merkle_root(list(reversed(txs8)))
    all_pass &= _ok("order matters (reversed root differs)",
                    reversed_root != root8)

    # ── 7. Tampered transaction is rejected ──────────────────────────
    proof_0 = merkle_proof(txs8, 0)
    tampered = bytes([txs8[0][0] ^ 0xFF]) + txs8[0][1:]
    all_pass &= _ok("tampered tx rejected",
                    not verify_proof(tampered, proof_0, root8))

    # ── 8. Proof length is ceil(log2(N)) ─────────────────────────────
    import math
    for n in [1, 2, 3, 4, 5, 7, 8, 16, 17]:
        txs = [_rnd_tx() for _ in range(n)]
        expected_depth = math.ceil(math.log2(n)) if n > 1 else 0
        proof = merkle_proof(txs, 0)
        all_pass &= _ok(
            f"proof depth n={n:2d}: got {len(proof)}, expected {expected_depth}",
            len(proof) == expected_depth,
        )

    print("=" * 62)
    print("All tests passed!" if all_pass else "SOME TESTS FAILED.")

    # ── Usage snippet ────────────────────────────────────────────────
    print("""
--- Integration into server1.py: calculate_hash() ---

from merkle import merkle_root_hex
from tx_codec import tx_to_bytes

def calculate_hash(self, block):
    tx_bytes_list = [tx_to_bytes(tx) for tx in block.get("transactions", [])]
    root = merkle_root_hex(tx_bytes_list) if tx_bytes_list else "0" * 64

    header = {
        "index":            block["index"],
        "previous_hash":    block["previous_hash"],
        "timestamp":        block["timestamp"],
        "difficulty_target": block["difficulty_target"],
        "nonce":            block["nonce"],
        "merkle_root":      root,          # ← replaces raw "transactions" list
    }
    encoded = json.dumps(header, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
""")
