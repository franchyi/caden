"""Sample the descendant process tree of a root PID (the claude process), printing the
tree whenever it changes. Captures Bash-tool subprocesses (claude -> bash -> python3 ->
sleep) as they spawn/exit, with relative timestamps to correlate with the stream trace.
Usage: python3 procsample.py <root_pid>
"""
import subprocess, sys, time

root = int(sys.argv[1])
t0 = time.time()

def run(args):
    return subprocess.run(args, capture_output=True, text=True).stdout

def children(pid):
    return [int(x) for x in run(["pgrep", "-P", str(pid)]).split()]

def comm(pid):
    c = run(["ps", "-o", "comm=", "-p", str(pid)]).strip()
    return c.rsplit("/", 1)[-1] if c else "?"

def alive(pid):
    return run(["ps", "-o", "pid=", "-p", str(pid)]).strip() != ""

def tree(pid):
    rows, stack = [], [(pid, 0)]
    while stack:
        p, d = stack.pop(0)
        rows.append((p, comm(p), d))
        stack = [(c, d + 1) for c in children(p)] + stack
    return rows

last = None
while alive(root) and time.time() - t0 < 120:
    snap = " | ".join("%s%s(%d)" % ("  " * d, c, p) for (p, c, d) in tree(root))
    if snap != last:
        print("%6.2fs  %s" % (time.time() - t0, snap)); sys.stdout.flush()
        last = snap
    time.sleep(0.2)
print("%6.2fs  (claude exited)" % (time.time() - t0))
