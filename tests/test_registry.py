import json
import shutil

import pytest

from worker.errors import ModelHashError, ModelLicenseError, NBError
from worker.models.registry import MODELS_DIR, Registry, default_registry


def test_manifest_models_verify():
    assert default_registry().verify_all() == []


def _tmp_manifest(tmp_path, **override):
    data = json.loads((MODELS_DIR / "manifest.json").read_text(encoding="utf-8"))
    m = next(x for x in data["models"] if x["id"] == "face_yunet_2023mar")
    m.update(override)
    shutil.copy(MODELS_DIR / m["file"], tmp_path / m["file"])
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path / "manifest.json"


@pytest.mark.parametrize("lic", ["AGPL-3.0", "CC-BY-NC-4.0", "Non-Commercial", "GPL-3.0", "unknown"])
def test_forbidden_license_blocked(tmp_path, monkeypatch, lic):
    import worker.models.registry as reg

    monkeypatch.setattr(reg, "MODELS_DIR", tmp_path)
    r = Registry(_tmp_manifest(tmp_path, license=lic))
    with pytest.raises(ModelLicenseError):
        r.get("face_yunet_2023mar")


def test_hash_mismatch_blocked(tmp_path, monkeypatch):
    import worker.models.registry as reg

    monkeypatch.setattr(reg, "MODELS_DIR", tmp_path)
    r = Registry(_tmp_manifest(tmp_path, sha256="0" * 64))
    with pytest.raises(ModelHashError):
        r.get("face_yunet_2023mar")


def test_unknown_model_blocked():
    with pytest.raises(NBError):
        default_registry().get("ultralytics_yolov8n")


def test_profiles_reference_manifest_models():
    r = default_registry()
    for p in r.profiles.values():
        for mid in (p.object, p.face, p.plate):
            assert mid in r.models
