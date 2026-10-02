RUN ?= uv run

.PHONY: sync proto lint test test-fast license bench clips app regress release

sync:
	uv sync

proto:
	$(RUN) python scripts/gen_proto.py

lint:
	$(RUN) ruff check worker app scripts tests

test:
	$(RUN) pytest -q

test-fast:
	$(RUN) pytest -q -m "not slow and not ui"

license:
	$(RUN) python scripts/ci/check_license.py

clips:
	$(RUN) python scripts/make_test_clips.py

bench:
	$(RUN) python scripts/bench.py tests/data/synthetic/street_faces.mp4 --interval 2 --protect-gt 1

regress:
	$(RUN) python scripts/regress.py tests/data

app:
	$(RUN) python -m app.main

release:
	powershell -ExecutionPolicy Bypass -File scripts/build/build_release.ps1
