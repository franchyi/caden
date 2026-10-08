#!/usr/bin/env python3
"""Verify the recorded SandboxFS source snapshot, without Git or a network."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    vendor = root / "third_party/sandboxfs"
    manifest = json.loads((root / "third_party/sandboxfs.provenance.json").read_text())
    if (vendor / ".git").exists():
        raise ValueError("SandboxFS must be ordinary vendored files, not a submodule")
    for name, expected in manifest["files"].items():
        path = vendor / name
        if path.is_symlink() or vendor.resolve() not in path.resolve().parents:
            raise ValueError(f"unsafe vendor path: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"vendored source differs from upstream: {name}")
    return manifest


if __name__ == "__main__":
    manifest = verify()
    print(f"SandboxFS {manifest['upstream_commit']}: {len(manifest['files'])} files verified")
