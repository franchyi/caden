# Pinned LZ4 dependency

Unmodified subset of official LZ4 v1.10.0, commit
`ebb370ca83af193212df4dcbadcc5d87bc0de2f0`:
<https://github.com/lz4/lz4/tree/ebb370ca83af193212df4dcbadcc5d87bc0de2f0/lib>.
Downloaded 2026-09-19; BSD 2-Clause terms retained in `LICENSE` and source headers.

| Upstream path | Local SHA256 |
| --- | --- |
| `lib/lz4.c` | `9396f7de527bc8435de9c7569fb7998e56545a84b4f3c2d808c0235c01774539` |
| `lib/lz4.h` | `26b82efc53d1570f3b54eef02e9c4764c1ad374ff03cac04e2ced5ea4d4c552f` |
| `lib/LICENSE` | `8b58c446121a109ccf32edc094bba3010a3d85e4ee3702950db55e4d3e87736c` |

The Makefile compiles this pinned source directly; no host package installation,
pkg-config, or system LZ4 header/library version is required. zlib remains a
system build dependency and its installed version must be recorded for trials.
