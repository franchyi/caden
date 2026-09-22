#!/usr/bin/env python3
"""Release exactly the four exported pilot images from our private Docker API."""
import json
import subprocess
from pathlib import Path

C = Path("/sandboxfs/crate-swebench-20260919")
D = ["docker", "--host", "unix:///run/crate-sv-docker.sock"]


def out(argv):
    return subprocess.check_output(argv, text=True)


def main():
    if out(D + ["ps", "--all", "--quiet"]).strip():
        raise RuntimeError("private daemon still owns containers")
    records = []
    for sequence in range(4):
        receipt = json.loads((C / "artifacts" / f"prepare-{sequence:02d}.json").read_text())
        image = receipt["task"]["image"]
        info = json.loads(out(D + ["image", "inspect", image]))[0]
        if info["Id"] != receipt["image_id"]:
            raise RuntimeError("image identity changed")
        if not (C / "bases" / f"{sequence:02d}" / "repository").is_dir():
            raise RuntimeError("expanded workspace not retained")
        records.append({"image": image, "id": info["Id"], "digest": receipt["image_digest"],
                        "removal": out(D + ["image", "rm", image])})
    (C / "artifacts/released-pilot-image-cache.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
