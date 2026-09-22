import json
import sys
from pathlib import Path

import pytest

from experiments.swebench_verified.private_bases import prepare


def test_refuse_existing_or_relative_targets(tmp_path):
    with pytest.raises(ValueError):
        prepare(tmp_path, tmp_path, [], tmp_path/'receipt.json')
    with pytest.raises(ValueError):
        prepare(tmp_path, Path('relative'), [], tmp_path/'receipt.json')


@pytest.mark.skipif(sys.platform != 'linux', reason='GNU cp and Linux fadvise integration')
def test_private_inodes_contents_symlinks_and_receipt(tmp_path):
    root=tmp_path.resolve()
    source=root/'original'
    (source/'00/repository').mkdir(parents=True)
    original=source/'00/repository/data'
    original.write_bytes(b'prepared contents'*100)
    (source/'00/repository/link').symlink_to('data')
    external=root/'never-follow'
    external.write_bytes(b'other owner')
    (source/'00/repository/external').symlink_to(external)
    output=root/'private'
    prepare(source, output, [{'sequence':0}], root/'receipt.json')
    copied=output/'00/repository/data'
    assert copied.read_bytes() == original.read_bytes()
    assert copied.stat().st_ino != original.stat().st_ino
    assert (output/'00/repository/external').is_symlink()
    assert external.read_bytes() == b'other owner'
    assert json.loads((root/'receipt.json').read_text())['private_inode_preparation'][0]['files'] == 1
