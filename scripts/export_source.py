#!/usr/bin/env python3
"""Export a clean Caden commit, with vendored source and a runner manifest.

No raw evidence, ignored files, binaries, virtualenvs or Git metadata enter the
export. A new experiment gets new provenance; historical manifests stay intact.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def export(destination, *, archive=False, root=ROOT):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing existing export: {destination}")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])
    if git("status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("commit the reviewed source before exporting")
    commit = git("rev-parse", "HEAD").decode().strip()
    source = git("archive", "--format=tar", commit)
    files, modes = {}, {}
    with tarfile.open(fileobj=io.BytesIO(source)) as tar:
        for item in tar:
            p = PurePosixPath(item.name)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe archive member")
            if item.isdir():
                continue
            if not item.isfile():
                raise ValueError(f"unsupported archive member: {item.name}")
            files[item.name] = tar.extractfile(item).read()
            modes[item.name] = item.mode
    if "third_party/sandboxfs/go.mod" not in files:
        raise ValueError("export is missing vendored SandboxFS")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    provenance = {
        "repository": "https://github.com/franchyi/caden",
        "commit": commit,
        "source_sha256": hashes,
        "source_manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "scope": "new source export, not the measured September source",
    }
    files["SOURCE_PROVENANCE.json"] = (json.dumps(provenance, indent=2) + "\n").encode()
    modes["SOURCE_PROVENANCE.json"] = 0o644
    if archive:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation ensures a prior delivery cannot be overwritten.
        with destination.open("xb") as out, tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo("caden/" + name)
                info.size, info.mode, info.mtime = len(data), modes[name], 0
                tar.addfile(info, io.BytesIO(data))
    else:
        destination.mkdir(parents=True)
        for name, data in files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(modes[name])
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--directory", type=Path)
    group.add_argument("--archive", type=Path)
    args = parser.parse_args()
    result = export(args.archive or args.directory, archive=bool(args.archive))
    print(f"Exported {result['commit']} ({len(result['source_sha256'])} files)")
