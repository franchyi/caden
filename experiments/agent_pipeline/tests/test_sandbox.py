from agent_pipeline.sandbox import build_bwrap_argv


def test_argv_binds_repo_and_chdir():
    argv = build_bwrap_argv(repo="/work/repo", command="pytest -q", bwrap="bwrap")
    assert argv[0] == "bwrap"
    assert "--bind" in argv and "/work/repo" in argv and "--chdir" in argv
    i = argv.index("--chdir")
    assert argv[i + 1] == "/work/repo"
    assert argv[-3:] == ["bash", "-c", "pytest -q"]
    assert "--ro-bind" in argv and "/usr" in argv
    assert "--unshare-net" not in argv


def test_argv_injects_env():
    argv = build_bwrap_argv(repo="/r", command="echo hi", env={"HOME": "/r/home"})
    assert "--setenv" in argv
    i = argv.index("HOME")
    assert argv[i + 1] == "/r/home"


def test_base_args_die_with_parent_and_only_existing_binds():
    import os

    from agent_pipeline.sandbox import BASE_ARGS

    assert "--die-with-parent" in BASE_ARGS  # no orphaned sandboxes if the runner dies
    ro = [BASE_ARGS[i + 1] for i, a in enumerate(BASE_ARGS) if a == "--ro-bind"]
    assert ro and all(os.path.exists(p) for p in ro)  # e.g. /lib64 is skipped on hosts without it
