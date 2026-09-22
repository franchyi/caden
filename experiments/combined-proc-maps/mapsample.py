"""Sample /proc/<root_pid>/maps at 1 Hz while the process is alive. Writes:
  <out>/maps/tNNNN.maps  -- raw /proc/<pid>/maps snapshot per second
  <out>/series.tsv       -- elapsed  vma  rss_kb  anon_kb   (one row per sample)
`elapsed` is seconds since this sampler started, so it lines up with the tool-stream
trace.txt and procs.txt that are launched alongside it (same run, same ~t0).
Usage: python3 mapsample.py <root_pid> <out_dir>
"""
import os, sys, time

root = int(sys.argv[1])
out = sys.argv[2]
os.makedirs(out + "/maps", exist_ok=True)
ser = open(out + "/series.tsv", "w")
ser.write("elapsed\tvma\trss_kb\tanon_kb\n"); ser.flush()

def alive(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False

t0 = time.time()
i = 0
while alive(root):
    el = time.time() - t0
    try:
        m = open("/proc/%d/maps" % root).read()
    except FileNotFoundError:
        break
    open("%s/maps/t%04d.maps" % (out, i), "w").write(m)
    vma = m.count("\n")
    rss = anon = ""
    try:
        for line in open("/proc/%d/smaps_rollup" % root):
            if line.startswith("Rss:"):
                rss = line.split()[1]
            elif line.startswith("Anonymous:"):
                anon = line.split()[1]
    except OSError:
        pass
    ser.write("%.2f\t%d\t%s\t%s\n" % (el, vma, rss, anon)); ser.flush()
    i += 1
    time.sleep(1)
ser.close()
