"""Shared on-disk namespace layout for the service and offline seed generation."""
from __future__ import annotations

import hashlib
import os
import re


def namespace_directory(namespace: str) -> str:
    """Readable prefix plus the digest of the exact, unnormalized namespace ID."""
    raw = str(namespace)
    prefix = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix or 'namespace'}--{digest}"


def contained_child(root: str, name: str) -> str:
    """Reject escapes and aliases, including links to sibling namespace stores."""
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root or path == root or os.path.basename(path) != name:
        raise ValueError("namespace path must be a direct child of MNEMOTRACE_DATA_DIR without aliases")
    return path


def namespace_path(root: str, namespace: str) -> str:
    """Resolve a namespace store without creating, scanning or adopting directories."""
    return contained_child(root, namespace_directory(namespace))
