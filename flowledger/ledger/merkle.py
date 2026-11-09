"""RFC 6962 / RFC 9162 Merkle tree over ledger entry hashes with inclusion proofs."""

from __future__ import annotations

import hashlib


def _leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """Largest power of two strictly less than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def merkle_root(leaves: list[bytes]) -> bytes:
    """Merkle Tree Hash over raw leaf data."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return _leaf(leaves[0])
    k = _split(n)
    return _node(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def inclusion_proof(leaves: list[bytes], index: int) -> list[bytes]:
    """Audit path for leaves[index] (RFC 6962 section 2.1.1)."""
    n = len(leaves)
    if not 0 <= index < n:
        raise IndexError("leaf index out of range")
    if n == 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_proof(leaves[:k], index) + [merkle_root(leaves[k:])]
    return inclusion_proof(leaves[k:], index - k) + [merkle_root(leaves[:k])]


def verify_inclusion(leaf_data: bytes, index: int, tree_size: int, proof: list[bytes], root: bytes) -> bool:
    """Recompute the root from a leaf and its audit path and compare."""
    if not 0 <= index < tree_size:
        return False

    def walk(idx: int, size: int, path: list[bytes]) -> bytes | None:
        if size == 1:
            return _leaf(leaf_data) if not path else None
        if not path:
            return None
        k = _split(size)
        sibling = path[-1]
        if idx < k:
            sub = walk(idx, k, path[:-1])
            return None if sub is None else _node(sub, sibling)
        sub = walk(idx - k, size - k, path[:-1])
        return None if sub is None else _node(sibling, sub)

    computed = walk(index, tree_size, list(proof))
    return computed is not None and computed == root


def ledger_merkle_root(entry_hashes: list[str]) -> str:
    return merkle_root([bytes.fromhex(h) for h in entry_hashes]).hex()
