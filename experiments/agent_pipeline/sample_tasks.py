"""Sample candidate SWE-rebench instance_ids, printed one per line. Restrict to a repo-name regex
(default: pure-Python repos that install cleanly on a modern Python) and spread picks across repos
for diversity. The caller test-fetches candidates and keeps the ones that build."""

import random
import re
import sys

import datasets

DATASET = "nebius/SWE-rebench"
SPLIT = "test"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    repo_pat = sys.argv[2] if len(sys.argv) > 2 else r"simonw/"
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    ds = datasets.load_dataset(DATASET, split=SPLIT)
    repos = ds["repo"]
    ids = ds["instance_id"]
    rx = re.compile(repo_pat)
    idx = [i for i in range(len(ids)) if rx.search(repos[i])]
    random.seed(seed)
    random.shuffle(idx)

    picked, per_repo = [], {}
    # First pass: at most 2 per repo, for diversity.
    for i in idx:
        if per_repo.get(repos[i], 0) < 2:
            picked.append(i)
            per_repo[repos[i]] = per_repo.get(repos[i], 0) + 1
        if len(picked) >= n:
            break
    for i in (j for j in idx if j not in picked):  # backfill if still short
        picked.append(i)
        if len(picked) >= n:
            break

    for i in picked[:n]:
        print(f"{ids[i]}\t{repos[i]}")


if __name__ == "__main__":
    main()
