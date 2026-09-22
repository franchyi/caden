from pathlib import Path

from experiments.cxl_tiering.run_tiering_suite import CONFIGS, CXL_CONFIGS, replay_extras


def options(label):
    return replay_extras(label, cache_roots=Path('/test/cache.json'), cache_budget_mib=64,
                         session_serving=True, pager_socket='/run/test-pager.sock')


def test_cxl_keeps_arrival_and_cache_options():
    args = options('crate-cxl-cache')
    assert '--arrival-driven' in args
    assert args[args.index('--service-cache-roots') + 1] == '/test/cache.json'
    assert args[args.index('--service-cache-budget-mib') + 1] == '64'
    assert args[args.index('--cxl-pager-socket') + 1] == '/run/test-pager.sock'
    assert CONFIGS['crate-cxl-cache'] == CONFIGS['crate-cxl']
    assert 'crate-cxl-cache' in CXL_CONFIGS


def test_ssd_and_vanilla_do_not_receive_cxl_socket():
    assert '--cxl-pager-socket' not in options('crate-ssd-cache')
    assert '--service-cache-roots' in options('crate-ssd-cache')
    assert options('baseline') == ['--arrival-driven']


def test_plain_cxl_keeps_arrivals_without_daemon_cache():
    assert options('crate-cxl') == ['--arrival-driven', '--cxl-pager-socket', '/run/test-pager.sock']
