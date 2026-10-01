PY ?= .venv/Scripts/python.exe

.PHONY: proto lint test test-fast license bench clips app

proto:
	$(PY) scripts/gen_proto.py

lint:
	$(PY) -m ruff check worker app scripts tests

test:
	$(PY) -m pytest -q

test-fast:
	$(PY) -m pytest -q -m "not slow and not ui"

license:
	$(PY) scripts/ci/check_license.py

clips:
	$(PY) scripts/make_test_clips.py

bench:
	$(PY) scripts/bench.py tests/data/synthetic/street_faces.mp4

app:
	$(PY) -m app.main
