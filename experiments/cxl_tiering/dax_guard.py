#!/usr/bin/env python3
"""Read-only evidence around a reserved Device-DAX range (never writes the device).

Prints SHA256 of the 2 MiB guards immediately before and after the range, plus
a fingerprint of the range's first and last 2 MiB so a later reader can tell
whether the expendable range held data. Holder snapshots are evidence only:
they cannot prove cross-host exclusion.
"""
import argparse
import hashlib
import json
import mmap
import os
import subprocess
import time

GUARD = 2 << 20
ap = argparse.ArgumentParser()
ap.add_argument("--device", default="/dev/dax0.0")
ap.add_argument("--offset", type=int, required=True)
ap.add_argument("--capacity", type=int, required=True)
a = ap.parse_args()
if a.offset % GUARD or a.capacity % GUARD or a.offset - GUARD < (256 << 30) or a.offset + a.capacity + GUARD > (512 << 30):
    raise SystemExit("range and its guards must be 2 MiB aligned inside the upper half")
descriptor = os.open(a.device, os.O_RDONLY)
record = {"observed_unix": time.time(), "host": os.uname().nodename, "device": a.device,
          "offset": a.offset, "capacity": a.capacity, "access": "read-only mmap"}
try:
    for name, start in (("guard_before", a.offset - GUARD), ("guard_after", a.offset + a.capacity),
                        ("range_first_2mib", a.offset), ("range_last_2mib", a.offset + a.capacity - GUARD)):
        with mmap.mmap(descriptor, GUARD, mmap.MAP_SHARED, mmap.PROT_READ, offset=start) as view:
            data = bytes(view)
        record[name] = {"sha256": hashlib.sha256(data).hexdigest(), "all_zero": not any(data)}
finally:
    os.close(descriptor)
holders = subprocess.run(["fuser", "-v", a.device], capture_output=True, text=True)
record["local_holders_snapshot"] = (holders.stdout + holders.stderr).strip()
print(json.dumps(record, indent=2))
