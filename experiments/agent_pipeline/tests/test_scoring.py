from agent_pipeline.scoring import is_resolved


def test_resolved_when_all_pass():
    assert is_resolved(fail_to_pass={"t_a": 0, "t_b": 0}, pass_to_pass={"t_c": 0}) is True


def test_not_resolved_if_a_fail_to_pass_still_fails():
    assert is_resolved(fail_to_pass={"t_a": 0, "t_b": 1}, pass_to_pass={"t_c": 0}) is False


def test_not_resolved_if_a_pass_to_pass_regresses():
    assert is_resolved(fail_to_pass={"t_a": 0}, pass_to_pass={"t_c": 1}) is False


def test_empty_lists_are_resolved():
    assert is_resolved(fail_to_pass={}, pass_to_pass={}) is True
