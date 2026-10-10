"""A deterministic, dependency-free stand-in for a real embedding model.

Hashes word trigram-ish features into a fixed number of buckets and L2-normalizes
the result, so cosine distance behaves sensibly for the demo's sample text. It is
not a good embedding model -- it has no semantics beyond shared vocabulary -- but
it is reproducible, needs no network, and lets the example focus on decay rather
than on inference. Swap in a real model and nothing else here changes.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import List

DIMENSIONS = 64

_WORD = re.compile(r"[a-z0-9']+")

# Words that carry no signal in this tiny corpus and would otherwise dominate
# the shared-vocabulary similarity.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "their there this to was were will with user users user's".split()
)


def _bucket(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % DIMENSIONS


def embed(text: str) -> List[float]:
    """A unit-length vector of length `DIMENSIONS` for this text."""
    vector = [0.0] * DIMENSIONS
    tokens = [t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS]
    for token in tokens:
        vector[_bucket(token)] += 1.0
        # A prefix feature gives partial credit for related word forms
        # ("pricing" / "price"), which keeps the demo's searches intuitive.
        if len(token) > 4:
            vector[_bucket(token[:4])] += 0.5

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # No usable tokens: return a fixed unit vector so the value stays valid.
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


def to_sql(vector: List[float]) -> str:
    """pgvector's text input format, e.g. ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
