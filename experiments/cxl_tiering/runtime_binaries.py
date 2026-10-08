"""Select one complete SandboxFS build; never silently mix daemon versions."""
import os
from pathlib import Path


def runtime_bin(inputs: Path, override: Path | None = None) -> Path:
    directory = (override if override is not None else inputs / "bin").resolve(strict=True)
    for name in ("sandboxfsd", "sandboxfsctl", "sandboxd"):
        binary = directory / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"missing executable in SandboxFS build: {binary}")
    return directory
