import subprocess
from dataclasses import dataclass, field


def _run(
    cmd: list[str],
    failure_message: str = "Command failed",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(failure_message, result.stderr)
    return result


# ---------------------------------------------------------------------------
# Image operations
# ---------------------------------------------------------------------------


def _resolve_image_name(image: str) -> str:
    """Ensure the image name has a registry prefix to avoid unqualified search errors."""
    parts = image.split("/", 1)
    if len(parts) == 1:
        return f"docker.io/library/{image}"

    first_part = parts[0]
    if "." not in first_part and ":" not in first_part and first_part != "localhost":
        return f"docker.io/{image}"

    return image


def pull_image(image: str, force_pull: bool = False, max_retries: int = 10) -> None:
    image = _resolve_image_name(image)
    if image_exists(image) and not force_pull:
        print(f"Image: {image} already exists. Skipping the pull.")
        return

    for attempt in range(max_retries):
        try:
            _run(
                ["podman", "pull", image],
                f"Failed to pull image {image}",
            )
            return
        except RuntimeError:
            if attempt + 1 == max_retries:
                raise
            _run(["docker", "login"], "Failed to login to Docker Hub")


def image_exists(name: str) -> bool:
    name = _resolve_image_name(name)
    result = _run(["podman", "image", "exists", name], check=False)
    return result.returncode == 0


def inspect_image(name: str, fmt: str) -> str:
    name = _resolve_image_name(name)
    result = _run(
        ["podman", "image", "inspect", name, "--format", fmt],
        f"Failed to inspect image {name}",
    )
    return result.stdout.strip()


def get_image_info(name: str) -> dict:
    """Return size and id metadata for an image."""
    info: dict = {}
    try:
        size_bytes = int(inspect_image(name, "{{.Size}}"))
        info["size_bytes"] = size_bytes
        info["size_mb"] = round(size_bytes / (1024 * 1024), 2)
        info["image_id"] = inspect_image(name, "{{.Id}}")[:12]
    except (RuntimeError, ValueError) as exc:
        info["error"] = str(exc)
    return info


def commit(container_id: str, image_name: str) -> None:
    _run(
        ["podman", "commit", container_id, image_name],
        f"Failed to commit container {container_id[:12]} as {image_name}",
    )


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


@dataclass
class ContainerOptions:
    image: str
    command: list[str] = field(default_factory=lambda: ["sleep", "infinity"])
    volumes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    workdir: str | None = None
    memory: str | None = None
    cpus: str | None = None
    network: str | None = None
    userns: str | None = None
    cgroup_parent: str | None = None

    def __post_init__(self):
        self.image = _resolve_image_name(self.image)


class Container:
    """
    Context manager that owns a single podman container.

    Usage:

        opts = ContainerOptions(image="ubuntu:22.04", command=["bash", "-c", "..."])
        with Container(opts) as c:
            exit_code = c.wait()
            stdout, stderr = c.logs()
    """

    def __init__(self, options: ContainerOptions):
        self.options = options
        self.container_id: str | None = None

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "Container":
        self._start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop_and_remove()

    # -- public API -------------------------------------------------------

    def exec(
        self,
        cmd: list[str],
        user: str | None = None,
        failure_message: str = "exec failed",
    ) -> subprocess.CompletedProcess[str]:
        full_cmd = ["podman", "exec"]
        if user is not None:
            full_cmd.extend(["-u", user])
        if self.options.workdir:
            full_cmd.extend(["-w", self.options.workdir])
        full_cmd.append(self._id())
        full_cmd.extend(cmd)
        return _run(full_cmd, failure_message)

    def cp(self, src: str, dest: str) -> None:
        _run(
            ["podman", "cp", src, f"{self._id()}:{dest}"],
            f"Failed to copy {src} into container",
        )

    def wait(self, timeout: int | None = None) -> int:
        cmd = ["podman", "wait", self._id()]
        if timeout is not None:
            cmd.extend(["--timeout", str(timeout)])
        result = _run(cmd, "Failed to wait for container")
        raw = result.stdout.strip()
        return int(raw) if raw else -1

    def logs(self) -> tuple[str, str]:
        result = _run(["podman", "logs", self._id()], "Failed to get container logs")
        return result.stdout, result.stderr

    def stop_and_remove(self) -> None:
        if self.container_id is None:
            return
        cid_short = self.container_id[:12]
        subprocess.run(["podman", "stop", self.container_id], capture_output=True)
        subprocess.run(["podman", "rm", self.container_id], capture_output=True)
        print(f"  Removed container: {cid_short}")
        self.container_id = None

    # -- internals --------------------------------------------------------

    def _id(self) -> str:
        if self.container_id is None:
            raise RuntimeError("Container has not been started")
        return self.container_id

    def _start(self) -> None:
        opts = self.options
        cmd: list[str] = ["podman", "run", "-d"]

        if opts.userns:
            cmd.append(f"--userns={opts.userns}")
        if opts.network:
            cmd.append(f"--network={opts.network}")
        for vol in opts.volumes:
            cmd.extend(["-v", vol])
        for key, value in opts.env.items():
            cmd.extend(["-e", f"{key}={value}"])
        if opts.workdir:
            cmd.extend(["-w", opts.workdir])
        if opts.memory:
            cmd.append(f"--memory={opts.memory}")
        if opts.cpus:
            cmd.append(f"--cpus={opts.cpus}")
        if opts.cgroup_parent:
            cmd.append(f"--cgroup-parent={opts.cgroup_parent}")

        cmd.append(opts.image)
        cmd.extend(opts.command)

        result = _run(cmd, "Failed to start container")
        self.container_id = result.stdout.strip()
        print(f"  Container started: {self.container_id}")
