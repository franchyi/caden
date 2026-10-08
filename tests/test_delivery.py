import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from experiments.cxl_tiering.runtime_binaries import runtime_bin

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendor_pinned_and_complete():
    manifest = load_script("verify_vendor").verify()
    assert manifest["upstream_commit"] == "652aa279bbb2afb4068d4b838e2df8e103b247fe"
    assert "cmd/sandboxd/main.go" in manifest["files"]
    assert not (ROOT / ".gitmodules").exists()


def test_runtime_requires_complete_build(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    with pytest.raises(ValueError, match="missing executable"):
        runtime_bin(tmp_path)
    for name in ("sandboxfsd", "sandboxfsctl", "sandboxd"):
        path = binaries / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    assert runtime_bin(tmp_path) == binaries.resolve()
    assert runtime_bin(tmp_path / "unused", binaries) == binaries.resolve()
    (binaries / "sandboxd").chmod(0o644)
    with pytest.raises(ValueError, match="sandboxd"):
        runtime_bin(tmp_path)


def test_launch_script_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "experiments/cxl_tiering/launch-daemon.sh")], check=True)


@pytest.fixture
def export_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "third_party/sandboxfs").mkdir(parents=True)
    (repo / "third_party/sandboxfs/go.mod").write_text("module sandboxfs\n")
    (repo / "third_party/sandboxfs.provenance.json").write_text(
        json.dumps({"upstream_commit": "652aa279bbb2afb4068d4b838e2df8e103b247fe"}))
    (repo / "script.sh").write_text("#!/bin/sh\n")
    (repo / "script.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Delivery Test",
                    "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "fixture"], check=True)
    return repo


@pytest.mark.parametrize("archive", [False, True])
def test_source_export_and_no_overwrite(export_repo, tmp_path, archive):
    exporter = load_script("export_source")
    destination = tmp_path / ("delivery.tar.gz" if archive else "source")
    result = exporter.export(destination, root=export_repo, archive=archive)
    assert result["sandboxfs_distribution"] == "vendored"
    assert result["sandboxfs"] == "652aa279bbb2afb4068d4b838e2df8e103b247fe"
    assert hashlib.sha256(json.dumps(result["source_sha256"], sort_keys=True).encode()).hexdigest() == result["source_manifest_sha256"]
    if archive:
        with tarfile.open(destination) as tar:
            assert "caden/third_party/sandboxfs/go.mod" in tar.getnames()
            assert "caden/.git/config" not in tar.getnames()
            assert tar.getmember("caden/script.sh").mode & 0o111
    else:
        assert (destination / "SOURCE_PROVENANCE.json").is_file()
        assert not (destination / ".git").exists()
    with pytest.raises(ValueError, match="existing export"):
        exporter.export(destination, root=export_repo, archive=archive)


def test_export_rejects_dirty_source(export_repo, tmp_path):
    (export_repo / "unreviewed.txt").write_text("not reviewed")
    with pytest.raises(ValueError, match="commit the reviewed"):
        load_script("export_source").export(tmp_path / "source", root=export_repo)


def test_vendored_and_exported_commit_identity(export_repo, tmp_path):
    from experiments.sandboxfs_memory.run_campaign import git_commit
    assert git_commit(export_repo / "third_party/sandboxfs") == "652aa279bbb2afb4068d4b838e2df8e103b247fe"
    destination = tmp_path / "source"
    result = load_script("export_source").export(destination, root=export_repo)
    assert git_commit(destination) == result["commit"]
    assert git_commit(destination / "third_party/sandboxfs") == result["sandboxfs"]
