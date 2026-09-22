"""Caden reads historical evidence without changing bytes or accepting unknown versions."""
import hashlib
import json

import pytest

from experiments.trajectory_replay.expand_workloads import checked_entries
from experiments.trajectory_replay.summarize_campaigns import load as load_replay
from experiments.sandboxfs_memory.summarize_campaigns import load as load_memory


@pytest.mark.parametrize('prefix', ['caden', 'orca'])
def test_versioned_report_readers(prefix, tmp_path):
    for suffix, reader in [('trajectory-replay-v1', load_replay),
                           ('sandboxfs-memory-v1', load_memory)]:
        path = tmp_path / (suffix + '.json')
        payload = json.dumps({'schema': f'{prefix}-{suffix}'}).encode()
        path.write_bytes(payload)
        assert reader(path)['schema'] == f'{prefix}-{suffix}'
        assert path.read_bytes() == payload
        path.write_text(json.dumps({'schema': f'{prefix}-{suffix}9'}))
        with pytest.raises(ValueError):
            reader(path)


@pytest.mark.parametrize('prefix', ['caden', 'orca'])
def test_historical_manifest_is_read_only_and_still_hash_checked(prefix, tmp_path):
    workload = json.dumps({'schema': f'{prefix}-tool-trajectory-v1', 'tool_count': 0}).encode()
    task = tmp_path / 'task.json'
    task.write_bytes(workload)
    manifest = json.dumps({'schema': f'{prefix}-tool-trajectory-manifest-v1',
        'workloads': [{'path': 'task.json', 'tool_count': 0,
                       'sha256': hashlib.sha256(workload).hexdigest()}]}).encode()
    path = tmp_path / 'manifest.json'
    path.write_bytes(manifest)
    _, entries = checked_entries(tmp_path)
    assert entries[0]['workload']['schema'] == f'{prefix}-tool-trajectory-v1'
    assert path.read_bytes() == manifest
    assert task.read_bytes() == workload
    task.write_bytes(workload + b' ')
    with pytest.raises(ValueError, match='checksum'):
        checked_entries(tmp_path)
