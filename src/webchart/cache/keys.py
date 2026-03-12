"""Cache key generation utilities."""

import hashlib
import json
from typing import Any


def make_key(*parts: Any) -> str:
    """Join parts into a cache key."""
    return ":".join(str(p) for p in parts)


def hash_dict(d: dict[str, Any]) -> str:
    """Create a stable hash from a dictionary."""
    canonical = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
