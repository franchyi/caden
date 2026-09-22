"""Private prepared-base inodes for scoped cache experiments; no global purge."""
import json
import os
import subprocess
from pathlib import Path


def prepare(source: Path, target: Path, tasks: list[dict], receipt: Path) -> None:
    if target.exists() or target.is_symlink() or not target.is_absolute():
        raise ValueError("private base output must be a new absolute directory")
    if target.parent.resolve() != target.parent or source.resolve() != source:
        raise ValueError("private base roots must not contain symlinks")
    indices = [f"{task['sequence']:02d}" for task in tasks]
    if len(set(indices)) != len(indices) or any(not index.isdecimal() for index in indices):
        raise ValueError("invalid task sequences")
    target.mkdir()
    records = []
    for index in indices:
        src, dst = source/index, target/index
        if not src.is_dir() or src.is_symlink():
            raise ValueError(f"unsafe prepared source: {src}")
        subprocess.run(["cp", "-a", "--reflink=auto", "--", str(src), str(dst)], check=True)
        advised = files = 0
        # DONTNEED is limited to newly created regular files, never symlink
        # referents. Reflinks share disk extents, not inode/page-cache identity.
        for directory, dirs, names in os.walk(dst, followlinks=False):
            for name in names:
                path = Path(directory)/name
                if path.is_symlink() or not path.is_file():
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if os.fstat(fd).st_ino == (src/path.relative_to(dst)).stat().st_ino and \
                            os.fstat(fd).st_dev == (src/path.relative_to(dst)).stat().st_dev:
                        raise RuntimeError("private preparation unexpectedly reused a source inode")
                    os.fsync(fd)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    advised += os.fstat(fd).st_size
                    files += 1
                finally:
                    os.close(fd)
        records.append({"source": str(src), "private_base": str(dst), "files": files,
                        "advised_logical_bytes": advised})
    receipt.write_text(json.dumps({"private_inode_preparation": records,
        "global_cache_purge": False,
        "warmup": "normal base-register reads all bytes before each configuration"}, indent=2)+'\n')
