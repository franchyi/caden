import hashlib
import json
import threading

import pytest

from experiments.swebench_verified.session_workloads import build, validate_arrivals
from experiments.trajectory_replay import run_campaign as replay
from caden.pool import ElasticReadyPool, ReadyPoolConfig


def source(tmp_path):
    root = tmp_path / 'original'
    root.mkdir()
    entries = []
    for i in range(2):
        w = {'schema': 'caden-tool-trajectory-v1', 'base': f'base-{i}',
             'source': {'trajectory_id': f'task-{i}', 'instance_id': f'task-{i}'},
             'tool_count': 1, 'tool_execution': {'kind': 'original-commands',
                 'proxy': False, 'anonymous_memory_injection': False},
             'events': [{'type': 'wait', 'duration_ms': 1234},
                        {'type': 'tool', 'argv': ['true']}],
             'fingerprint_expected': {'diff_sha256': 'test', 'untracked': {}}}
        raw = json.dumps(w).encode()
        (root / f'{i}.json').write_bytes(raw)
        entries.append({'path': f'{i}.json', 'sha256': hashlib.sha256(raw).hexdigest(),
                        'tool_count': 1, 'capture_sha256': 'capture'})
    manifest = {'schema': 'caden-tool-trajectory-manifest-v1', 'source': {'dataset': 'fixture'},
                'conversion': {'kind': 'original-commands', 'wait_scale': 1, 'success_filter': False},
                'workloads': entries}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root


def test_replicas_preserve_work_and_detect_mutation(tmp_path):
    original, target = source(tmp_path), tmp_path / 'sessions'
    build(original, target, indices=(0, 1), replicas=3, spacing_ms=12, seed=9)
    workloads, manifest = replay.load_workloads(target)
    validate_arrivals(workloads, manifest, target)
    assert [w['arrival_offset_ms'] for w in workloads] == [0, 12, 24, 36, 48, 60]
    assert len({w['source']['trajectory_id'] for w in workloads}) == 6
    assert len({w['source']['instance_id'] for w in workloads}) == 2
    workloads[0]['events'][0]['duration_ms'] += 1
    with pytest.raises(ValueError, match='changed real work'):
        validate_arrivals(workloads, manifest, target)
    with pytest.raises(ValueError, match='existing'):
        build(original, target)


def test_arrival_replay_does_not_wait_for_other_creates(monkeypatch):
    first_replayed = threading.Event()
    requests, results, ids = [], [], []
    class Scheduler:
        def submit(self, task):
            return task.repo
        def wait_for_background(self, timeout):
            pass
    def finish(scheduler, execution, sequence, workload, request_id, submitted):
        if sequence == 1:
            assert first_replayed.wait(2), 'second create blocked the first replay'
        return {'sequence': sequence, 'request_to_ready_ns': 1,
                'trajectory_id': request_id, 'workload': workload}
    def play(scheduler, execution, request, wait_scale, barrier, commit_barrier, progress):
        assert barrier is None and commit_barrier is None
        if request['sequence'] == 0:
            first_replayed.set()
        return {'trajectory_id': request['trajectory_id'], 'tools': [{}], 'error': ''}
    monkeypatch.setattr(replay, 'finish_create', finish)
    monkeypatch.setattr(replay, 'replay_one', play)
    replay.run_arrival_sessions(Scheduler(), None,
        [{'base': 'first', 'arrival_offset_ms': 0}, {'base': 'second', 'arrival_offset_ms': 0}],
        2, 1, requests, results, ids, None)
    assert len(requests) == len(results) == 2
    assert all(r['planned_arrival_ns'] == r['serving_origin_ns'] for r in requests)


def test_serving_pool_has_only_notified_demand():
    pool = ElasticReadyPool(None, ReadyPoolConfig(target_ready=0, maximum_ready=0), pending_bases=[])
    assert pool._pending_bases == []
    pool.add_pending('actually-arrived')
    assert pool._pending_bases == ['actually-arrived']
    assert [event.action for event in pool.events()] == ['demand_arrived']
    pool.close()
