import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(*args, env=None):
    e = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
    return subprocess.run([sys.executable, "-m", "worker.cli", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env=e, timeout=600)


def test_probe_json(street_clip):
    r = _run("probe", str(street_clip), "--no-hash")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["frames"] == 180


def test_exit_code_4_on_model_hash_error(tmp_path, short_clip):
    models = ROOT / "models"
    data = json.loads((models / "manifest.json").read_text(encoding="utf-8"))
    for m in data["models"]:
        if m["file"]:
            shutil.copy(models / m["file"], tmp_path / m["file"])
    data["models"][0]["sha256"] = "0" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    r = _run("analyze", str(short_clip), "-o", str(tmp_path / "x.nbproj"), env={"NURIBLUR_MODELS": str(tmp_path)})
    assert r.returncode == 4, r.stderr
    assert "E_MODEL_HASH" in r.stderr


@pytest.mark.slow
def test_cli_pipeline_and_exit_codes(short_clip, tmp_path):
    proj, out = tmp_path / "c.nbproj", tmp_path / "o.mp4"
    assert _run("--quiet", "analyze", str(short_clip), "-o", str(proj), "--interval", "2").returncode == 0
    r = _run("tracks", str(proj))
    assert r.returncode == 0 and "F#" in r.stdout
    tid = r.stdout.split("F#")[1].split("\t")[0]
    r = _run("rules", str(proj), "--protect", tid)
    assert json.loads(r.stdout)["protected"]
    r = _run("--quiet", "render", str(proj), "-o", str(out), "--audit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _run("audit", str(proj), str(out)).returncode == 0
    # 원본을 출력본처럼 재검사하면 노출 → 종료 코드 3
    assert _run("audit", str(proj), str(short_clip)).returncode == 3
