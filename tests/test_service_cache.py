import errno
import os
import threading
import time

import pytest

from caden.service_cache import ServiceCacheController

MIB = 1 << 20


def controller(tmp_path, monkeypatch, *, idle=lambda base: True):
    path = tmp_path.resolve() / "owned.service" / "daemon"
    path.mkdir(parents=True)
    for name, value in {"memory.current": str(512*MIB), "memory.swap.current": "0",
                        "memory.swap.max": "max", "memory.reclaim": "",
                        "memory.stat": f"file {500*MIB}\ninactive_file {400*MIB}\n"
                                       f"anon {8*MIB}\nfile_dirty 0\nfile_mapped 0\n"}.items():
        (path/name).write_text(value)
    c = ServiceCacheController({"test": path}, idle=idle, interval_seconds=0.01)
    original = c._write
    calls = []

    def write(domain, name, value):
        calls.append((name, value))
        # Regular fixture files need truncate semantics; cgroupfs does not.
        (domain.path/name).write_text(value)
        if name == "memory.reclaim" and int(value.split()[0]):
            current = int((path/"memory.current").read_text())
            (path/"memory.current").write_text(str(current-int(value.split()[0])))
    monkeypatch.setattr(c, "_write", write)
    return c, path, calls


def test_bounds_release_and_restore(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    c.start()
    deadline = time.monotonic()+2
    while not c.events and time.monotonic() < deadline:
        time.sleep(0.001)
    c.close()
    assert c.events
    assert all(e['requested_bytes'] <= 16*MIB for e in c.events)
    assert all(e['charge_delta_bytes'] == e['requested_bytes'] for e in c.events)
    assert (path/'memory.swap.max').read_text() == 'max'
    assert c.domains['test'].fd == -1
    with pytest.raises(RuntimeError):
        c.start()


def test_active_and_foreground_are_protected(tmp_path, monkeypatch):
    idle = [False]
    c, path, calls = controller(tmp_path, monkeypatch, idle=lambda base: idle[0])
    c.start()
    time.sleep(.03)
    assert not c.events
    with c.foreground('test'):
        idle[0] = True
        time.sleep(.03)
        assert not c.events
    c.close()


def test_inflight_chunk_is_charged_before_foreground(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    entered, release, foreground = threading.Event(), threading.Event(), threading.Event()
    write = c._write
    def blocking(domain, name, value):
        if name == 'memory.reclaim' and int(value.split()[0]):
            entered.set()
            assert release.wait(2)
        write(domain, name, value)
    monkeypatch.setattr(c, '_write', blocking)
    c.start()
    assert entered.wait(2)
    def caller():
        with c.foreground('test'):
            foreground.set()
    thread = threading.Thread(target=caller)
    thread.start()
    time.sleep(.02)
    assert not foreground.is_set()
    release.set()
    assert foreground.wait(2)
    thread.join()
    c.close()


def test_old_kernel_fallback_and_partial_progress(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    write = c._write
    def fallback(domain, name, value):
        if name == 'memory.reclaim':
            if 'swappiness' in value:
                raise OSError(errno.EINVAL, 'old kernel')
            write(domain, name, value)
            raise OSError(errno.EAGAIN, 'partial')
        write(domain, name, value)
    monkeypatch.setattr(c, '_write', fallback)
    c.start()
    deadline=time.monotonic()+2
    while not c.events and time.monotonic()<deadline:
        time.sleep(.001)
    c.close()
    assert c.events and all(e['partial'] and not e['file_only_supported'] for e in c.events)
    assert calls[0] == ('memory.swap.max', '0')
    assert (path/'memory.swap.max').read_text() == 'max'


def test_start_failure_restores_limits(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    write = c._write
    def fail(domain, name, value):
        if name == 'memory.reclaim':
            raise OSError(errno.EPERM, 'denied')
        write(domain, name, value)
    monkeypatch.setattr(c, '_write', fail)
    with pytest.raises(OSError):
        c.start()
    assert (path/'memory.swap.max').read_text() == 'max'
    assert c.domains['test'].fd == -1


def test_paths_and_unknown_bases_fail_closed(tmp_path):
    root = tmp_path.resolve()
    for bad in (root, root/'foreign.service', root/'daemon', root/'foo.service'/'..'/'daemon'):
        with pytest.raises(ValueError):
            ServiceCacheController({'test': bad})
    actual=root/'owned.service'/'daemon'
    actual.mkdir(parents=True)
    (root/'link.service').symlink_to(actual.parent)
    with pytest.raises(ValueError):
        ServiceCacheController({'test': root/'link.service'/'daemon'})
    c=ServiceCacheController({'test': actual})
    with pytest.raises(ValueError):
        with c.foreground('foreign'):
            pass


def test_no_reclaim_below_reserve_or_dirty_budget(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    (path/'memory.current').write_text(str(64*MIB))
    c.start()
    time.sleep(.02)
    assert not c.events
    with c.foreground('test'):
        (path/'memory.current').write_text(str(512*MIB))
        (path/'memory.stat').write_text(f'file {500*MIB}\ninactive_file {400*MIB}\nfile_dirty {400*MIB}\n')
    time.sleep(.02)
    c.close()
    assert not c.events


def test_shared_base_foreground_calls_can_overlap(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    both = threading.Barrier(2)
    completed, errors = [], []
    def caller():
        try:
            with c.foreground('test'):
                both.wait(timeout=2)
                assert c.step('test') == 0
            completed.append(True)
        except BaseException as error:
            errors.append(error)
    workers = [threading.Thread(target=caller) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
    assert not errors and len(completed) == 2


def test_empty_domains_do_not_each_consume_a_rate_limit_interval(tmp_path, monkeypatch):
    c, path, calls = controller(tmp_path, monkeypatch)
    c.domains = {str(i): next(iter(c.domains.values())) for i in range(4)}
    visits = []
    monkeypatch.setattr(c, 'step', lambda base: visits.append(base) or 0)
    class StopAfterFirstWait:
        def is_set(self):
            return False
        def wait(self, delay):
            return True
    c._stop = StopAfterFirstWait()
    c._run()
    assert visits == ['0', '1', '2', '3']
