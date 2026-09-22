#!/usr/bin/env python3
"""Prepare ONLY the campaign-owned images, rootfs, bases and isolated daemons."""
import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

C = Path("/sandboxfs/crate-swebench-20260919")
DOCKER = ["docker", "--host", "unix:///run/crate-sv-docker.sock"]


def run(argv, **kw):
    print("+", " ".join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kw)


def output(argv):
    try:
        return run(argv, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as error:
        print(error.stdout, error.stderr, flush=True)
        raise


def ctl(task, *args):
    return json.loads(output([C / "bin/sandboxfsctl", "--socket", task["socket"], "--timeout", "600s", *args]))


def classify_image_changes(raw):
    """Permit regular-file executable-bit changes without hiding content changes."""
    fields = raw.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ValueError("malformed raw git diff")
    mode_only, content = [], []
    for header, path in zip(fields[::2], fields[1::2]):
        old, new, old_blob, new_blob, status = header.split()
        old = old.removeprefix(":")
        if (old_blob == new_blob and old_blob.strip("0") and status == "M"
                and old != new and {old, new} <= {"100644", "100755"}):
            mode_only.append(path)
        else:
            content.append(path)
    return {"mode_only": mode_only, "content_or_type": content}


def prepare(task):
    index = f"{task['sequence']:02d}"
    receipt = C / "artifacts" / f"prepare-{index}.json"
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved["task"] != task:
            raise RuntimeError("task mismatch in existing preparation")
        print("already prepared", task["instance_id"], flush=True)
        return
    rootfs, base, runtime = (C / "rootfs" / index, C / "bases" / index, C / "runtime" / index)
    if base.exists():
        saved_image = json.loads((C / "artifacts" / f"image-{index}.json").read_text())
        git = ["git", "-c", f"safe.directory={base}/repository", "-C", base / "repository"]
        commit = output(git + ["rev-parse", "HEAD"]).strip()
        if commit != task["base_commit"] or output(git + ["status", "--porcelain"]).strip():
            raise RuntimeError("cannot resume modified/wrong-commit prepared base")
        if os.readlink(rootfs / "testbed") != "/workspace/repository":
            raise RuntimeError("cannot resume unrecognized rootfs adaptation")
        return activate(task, saved_image, None, output(git + ["rev-parse", "HEAD^{tree}"]).strip(), time.time())
    started = time.time()
    run(DOCKER + ["pull", "--platform", "linux/amd64", task["image"]])
    info = json.loads(output(DOCKER + ["image", "inspect", task["image"]]))[0]
    if info["Architecture"] != "amd64" or not info.get("RepoDigests"):
        raise RuntimeError("unpinned/wrong-architecture image")
    digest = info["RepoDigests"][0]
    (C / "artifacts" / f"image-{index}.json").write_text(json.dumps(info, indent=2) + "\n")
    archive = C / "artifacts" / f"rootfs-{index}.tar"
    if not rootfs.exists():
        container_name = f"crate-sv-export-{index}"
        container = output(DOCKER + ["create", "--name", container_name, "--network", "none", digest, "/bin/true"]).strip()
        try:
            run(DOCKER + ["export", "--output", archive, container])
            rootfs.mkdir(parents=True)
            run(["tar", "--extract", "--file", archive, "--directory", rootfs, "--numeric-owner"])
        finally:
            run(DOCKER + ["rm", container])
    elif not archive.exists():
        raise RuntimeError("cannot resume extraction without retained source archive")
    git = ["git", "-c", f"safe.directory={rootfs}/testbed", "-C", rootfs / "testbed"]
    image_commit = output(git + ["rev-parse", "HEAD"]).strip()
    tree = output(git + ["rev-parse", "HEAD^{tree}"]).strip()
    base_tree = output(git + ["rev-parse", task["base_commit"] + "^{tree}"]).strip()
    if output(git + ["status", "--porcelain"]).strip():
        raise RuntimeError("official image checkout is dirty")
    if tree != base_tree:
        raw = output(git + ["diff", "--raw", "-z", "--no-renames", "--no-abbrev", task["base_commit"], "HEAD"])
        changes = classify_image_changes(raw)
        if not set(changes["content_or_type"]) <= {"pyproject.toml", "setup.cfg", "setup.py"}:
            raise RuntimeError(f"image content/type changes need explicit review ({len(changes['content_or_type'])} files): {changes['content_or_type'][:20]}")
        (C / "artifacts" / f"image-change-review-{index}.json").write_text(json.dumps(changes, indent=2) + "\n")
        # Reviewed Astropy image pins setuptools==68.0.0 for image construction.
        # Preserve that patch, restore canonical repo metadata, retain built env.
        image_patch = output(git + ["diff", "--binary", task["base_commit"], "HEAD"])
        (C / "artifacts" / f"image-build-adjustments-{index}.patch").write_text(image_patch)
    # Official image may append an empty 'SWE-bench' commit. Verify identical
    # trees and clean state before selecting the dataset commit in this NEW clone.
    run(git + ["checkout", "--detach", task["base_commit"]])
    commit = output(git + ["rev-parse", "HEAD"]).strip()
    if commit != task["base_commit"]:
        raise RuntimeError(f"image commit {commit} != selected {task['base_commit']}")
    if output(git + ["rev-parse", "HEAD^{tree}"]).strip() != base_tree or output(git + ["status", "--porcelain"]).strip():
        raise RuntimeError("canonical base tree/cleanliness verification failed")
    if not (rootfs / "opt/miniconda3/envs/testbed/bin/python").exists():
        raise RuntimeError("official testbed conda environment missing")
    base.mkdir(parents=True)
    (base / "dependencies").mkdir()
    shutil.move(str(rootfs / "testbed"), str(base / "repository"))
    shutil.move(str(rootfs / "opt/miniconda3"), str(base / "dependencies/miniconda3"))
    (rootfs / "testbed").symlink_to("/workspace/repository")
    (rootfs / "opt/miniconda3").symlink_to("/workspace/dependencies/miniconda3")
    (rootfs / "usr/local/libexec/sandboxfs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(C / "bin/sandboxd", rootfs / "usr/local/libexec/sandboxfs/sandboxd")
    return activate(task, info, image_commit, base_tree, started)


def activate(task, info, image_commit, base_tree, started):
    index = f"{task['sequence']:02d}"
    rootfs, base, runtime = (C / "rootfs" / index, C / "bases" / index, C / "runtime" / index)
    receipt = C / "artifacts" / f"prepare-{index}.json"
    archive = C / "artifacts" / f"rootfs-{index}.tar"
    digest, commit = info["RepoDigests"][0], task["base_commit"]
    (rootfs / "workspace").mkdir(exist_ok=True)
    user = pwd.getpwnam("chaoyi")
    run(["chown", "-R", f"{user.pw_uid}:{user.pw_gid}", base])
    for suffix in ("sandboxes", "state", "measurements"):
        (runtime / suffix).mkdir(parents=True, exist_ok=True)
    service = f"crate-sv-{index}.service"
    loaded = output(["systemctl", "show", service, "--property=LoadState", "--value"]).strip()
    if loaded == "loaded":
        run(["systemctl", "restart", service])
    else:
        run(["systemd-run", f"--unit={service}", "--property=Type=simple",
             "--property=PrivateMounts=yes", "--property=AllowedCPUs=0-7",
             "--property=KillMode=mixed", "/bin/bash", C / "scripts/launch-daemon.sh", index])
    for attempt in range(60):
        if Path(task["socket"]).is_socket():
            break
        time.sleep(0.5)
    register = ctl(task, "base-register", task["base"], str(runtime / "bases/prepared"))
    verify = ctl(task, "base-verify", task["base"])
    if not verify.get("match"):
        raise RuntimeError("base verification failed")
    sandbox = f"sv-preflight-{index}"
    created = ctl(task, "create", "--id", sandbox, "--base", task["base"], "--mode", "t1")
    try:
        command = "export PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/bin:/bin; cd /testbed; python -V; git -c safe.directory=/workspace/repository rev-parse HEAD; python -c 'import sys; print(sys.executable)'; echo private > /tmp/crate-sv-persist; echo private > /workspace/.crate-sv-write"
        smoke = ctl(task, "exec-json", sandbox, "--", "/bin/bash", "-c", command)
        persist = ctl(task, "exec-json", sandbox, "--", "/bin/bash", "-c", "test -f /tmp/crate-sv-persist && test -f /workspace/.crate-sv-write")
        if smoke.get("exit_code") != 0 or persist.get("exit_code") != 0:
            raise RuntimeError(f"sandbox preflight failed: {smoke!r} {persist!r}")
    finally:
        destroyed = ctl(task, "destroy", sandbox)
    receipt.write_text(json.dumps({"task": task, "image_digest": digest,
        "image_id": info["Id"], "image_original_commit": image_commit,
        "base_tree_verified": base_tree, "base_commit_verified": commit,
        "register": register, "verify": verify, "create": created,
        "smoke": smoke, "tmp_persistence": persist, "destroy": destroyed,
        "preparation_seconds": time.time() - started,
        "base_size": output(["du", "-sb", base]).strip(),
        "rootfs_size": output(["du", "-sb", rootfs]).strip(),
        "adaptation": ["testbed and miniconda3 moved into workspace; original absolute paths symlinked", "sandboxd installed into read-only image OS"]}, indent=2) + "\n")
    # Generated transfer archive only; image and expanded tree remain recoverable.
    archive.unlink(missing_ok=True)
    print("PREPARED", task["instance_id"], digest, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4)
    a = ap.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("root required for isolated mount/cgroup setup")
    tasks = json.loads((C / "selection/manifest.json").read_text())["tasks"][:a.limit]
    for task in tasks:
        prepare(task)
    (C / "artifacts" / f"PREPARE_{len(tasks)}_COMPLETE.json").write_text(json.dumps({"tasks": len(tasks), "finished_unix": time.time()}) + "\n")


if __name__ == "__main__":
    main()
