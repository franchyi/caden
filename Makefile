PYTHON ?= python3

.PHONY: test test-core test-agent native-check

test: test-core test-agent

test-core:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -B -m pytest -q -p no:cacheprovider tests

test-agent:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/agent_pipeline $(PYTHON) -B -m pytest -q -p no:cacheprovider experiments/agent_pipeline/tests

native-check:
	$(MAKE) -C native/cxl_coldstore check
