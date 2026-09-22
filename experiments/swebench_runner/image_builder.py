import os

from experiments.swebench_runner.podman import (
    Container,
    ContainerOptions,
    commit,
    image_exists,
)


_PIP_PYTEST_SETUP = """\
if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3-pip
elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-pip
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-pip
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3-pip
fi
if ! command -v pip3 >/dev/null 2>&1 && ! command -v pip >/dev/null 2>&1; then
    python3 -m ensurepip --default-pip || python -m ensurepip --default-pip
fi
# Always install pytest in the active python environment
python -m pip install pytest || python3 -m pip install pytest || pip install pytest || true
"""

_IMAGE_TAG_VERSION = "v4"


class ImageBuilder:
    """
    Prepares a SWE-Bench image with fixed permissions and test tooling.
    Fixes /testbed permissions (chown -R)
    Installs pip and pytest on a best-effort basis.
    The built image is cached under a deterministic tag.
    """

    def __init__(self, base_image: str):
        self.base_image = base_image
        safe = base_image.replace("/", "_").replace(":", "_")
        self.fixed_image_name = f"localhost/swebench-fixed-{_IMAGE_TAG_VERSION}-{safe}"

    def get_or_build(self) -> str:
        """Return the name of the ready-to-use image, building it if needed."""
        if image_exists(self.fixed_image_name):
            print(f"  Using existing fixed image: {self.fixed_image_name}")
            return self.fixed_image_name

        self._build()
        return self.fixed_image_name

    def _build(self) -> None:
        uid = os.getuid()
        gid = os.getgid()

        opts = ContainerOptions(
            image=f"docker.io/{self.base_image}",
            command=["sleep", "300"],
        )

        with Container(opts) as c:
            print("  Fixing /testbed permissions...")
            c.exec(
                ["chown", "-R", f"{uid}:{gid}", "/testbed"],
                failure_message="Failed to fix /testbed permissions",
            )

            print("  Installing pip and pytest...")
            try:
                c.exec(
                    ["sh", "-c", _PIP_PYTEST_SETUP],
                    user="0",
                    failure_message="Failed to install pip/pytest",
                )
                print("  Successfully installed pip and pytest.")
            except Exception as exc:
                # Non-fatal: some images may already have these or may not
                # support the package managers we try.
                print(f"  Warning: pip/pytest installation failed: {exc}")

            commit(c.container_id, self.fixed_image_name)
            print(f"  Created fixed image: {self.fixed_image_name}")
