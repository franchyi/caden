"""Route normal sandboxfsctl API calls to pinned per-task OS environments."""
import subprocess
import threading


class RoutedCtlRunner:
    def __init__(self, bases, runner=subprocess.run):
        if not bases or not all(isinstance(k, str) and isinstance(v, str) for k, v in bases.items()):
            raise ValueError("base/socket map must be a nonempty string mapping")
        self.bases = dict(bases)
        self.sandboxes = {}
        self.lock = threading.Lock()
        self.runner = runner

    def __call__(self, argv, timeout=None):
        command = list(argv)
        # The execution adapter's fixed prefix: ctl --socket X --timeout Ys.
        if len(command) < 6 or command[1] != "--socket" or command[3] != "--timeout":
            raise ValueError("unexpected sandboxfsctl command prefix")
        operation, args = command[5], command[6:]
        with self.lock:
            if operation == "create":
                base = args[args.index("--base") + 1]
                ident = args[args.index("--id") + 1]
                socket = self.bases[base]
                if ident in self.sandboxes:
                    raise ValueError("duplicate sandbox id")
                self.sandboxes[ident] = socket
            elif operation in {"exec-json", "destroy", "inspect", "state"}:
                socket = self.sandboxes[args[0]]
            else:
                raise ValueError(f"unsupported routed operation: {operation}")
        command[2] = socket
        result = self.runner(command, check=False, capture_output=True, text=True, timeout=timeout)
        if operation == "destroy" and result.returncode == 0:
            with self.lock:
                self.sandboxes.pop(args[0], None)
        return result
