#!/usr/bin/env python3
"""Exercise cooperative release/restore with a bounded disposable file fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProbeTest(unittest.TestCase):
    def test_patterns_and_region_guards(self):
        binary = Path(__file__).resolve().parent / "build/coldstore_probe"
        page = os.sysconf("SC_PAGE_SIZE")
        capacity, logical = 4 << 20, 2 << 20
        with tempfile.NamedTemporaryFile(prefix="crate-coldstore-probe-") as fixture:
            fixture.truncate(page + capacity + page)
            fixture.seek(0)
            fixture.write(b"A" * page)
            fixture.seek(page + capacity)
            fixture.write(b"Z" * page)
            fixture.flush()
            for codec, pattern in ((c, p) for c in (None, "none", "lz4") for p in ("zeros", "mixed", "random")):
                with self.subTest(codec=codec, pattern=pattern):
                    completed = subprocess.run(
                        [str(binary), "--emulate-file", fixture.name, str(page),
                         str(capacity), str(logical), pattern] + (["--codec", codec] if codec else []),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    result = json.loads(completed.stdout)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["schema"], "crate-cold-tier-backend-probe-v2")
                    self.assertEqual(result["codec"], codec or "none")
                    self.assertEqual(result["backend"], "regular-file-emulation")
                    self.assertTrue(result["source_release_munmap_succeeded"])
                    if sys.platform.startswith("linux"):
                        self.assertTrue(result["source_mapping_absent_after_release"])
                    else:
                        self.assertIsNone(result["source_mapping_absent_after_release"])
                    self.assertEqual(result["crc32_expected"], result["crc32_restored"])
                    self.assertLessEqual(result["allocator_bytes"], logical)
                    if codec != "lz4":
                        for key in ("compression_calls", "decompression_calls", "codec_state_bytes", "zero_pages"):
                            self.assertEqual(result[key], 0)
                        self.assertEqual(result["payload_bytes"], logical)
                        self.assertEqual(result["allocator_bytes"], logical)
                        self.assertEqual(result["raw_pages"], logical // 4096)
                    elif pattern == "zeros":
                        self.assertEqual(result["payload_bytes"], 0)
                    elif pattern == "mixed":
                        self.assertLess(result["payload_bytes"], logical // 2)
                    else:
                        self.assertEqual(result["payload_bytes"], logical)
                    fixture.seek(0)
                    self.assertEqual(fixture.read(page), b"A" * page)
                    fixture.seek(page + capacity)
                    self.assertEqual(fixture.read(page), b"Z" * page)
                    print(json.dumps(result), flush=True)
            bad = subprocess.run([str(binary), "--emulate-file", fixture.name, str(page),
                                  str(capacity), str(logical), "mixed", "--codec", "invalid"], capture_output=True)
            self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
