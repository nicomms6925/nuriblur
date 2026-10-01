"""모델 레지스트리 (G0-03).

- `models/manifest.json`에 없는 모델은 로드하지 않는다.
- 라이선스 허용 목록 밖이면 E_MODEL_LICENSE, 해시 불일치면 E_MODEL_HASH.
- ONNX Runtime Execution Provider는 CUDA → DirectML → CoreML → CPU 순으로 고른다.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from worker.errors import ModelHashError, ModelLicenseError, NBError

MODELS_DIR = Path(os.environ.get("NURIBLUR_MODELS", Path(__file__).resolve().parents[2] / "models"))

# docs/09: license ∈ {Apache-2.0, MIT, BSD, 자체}
ALLOWED_LICENSES = {"Apache-2.0", "MIT", "BSD", "BSD-2-Clause", "BSD-3-Clause", "자체"}
FORBIDDEN_MARKERS = ("AGPL", "GPL-3", "SSPL", "NON-COMMERCIAL", "NONCOMMERCIAL", "CC-BY-NC", "COMMONS CLAUSE")

PROVIDER_ORDER = [
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
]


@dataclass
class ModelSpec:
    id: str
    file: str | None
    sha256: str | None
    license: str
    task: str
    arch: str
    classes: dict[int, str]
    input_size: int
    audit_capable: bool
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path | None:
        return MODELS_DIR / self.file if self.file else None


@dataclass
class Profile:
    name: str
    object: str
    face: str
    plate: str
    detect_interval: int
    face_long_side: int


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_license(spec: ModelSpec) -> None:
    lic = (spec.license or "").strip()
    if any(m in lic.upper() for m in FORBIDDEN_MARKERS) or lic not in ALLOWED_LICENSES:
        raise ModelLicenseError(f"모델 {spec.id}: 허용되지 않은 라이선스 '{lic}'")


def check_hash(spec: ModelSpec) -> None:
    if spec.file is None:  # 가중치 없는 규칙 기반 모델
        return
    p = spec.path
    if p is None or not p.exists():
        raise ModelHashError(f"모델 {spec.id}: 파일 없음 {p}")
    actual = _sha256(p)
    if not spec.sha256 or actual.lower() != spec.sha256.lower():
        raise ModelHashError(f"모델 {spec.id}: SHA-256 불일치 (manifest {spec.sha256}, 실제 {actual})")


class Registry:
    def __init__(self, manifest_path: Path | None = None):
        self.manifest_path = Path(manifest_path or MODELS_DIR / "manifest.json")
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.models: dict[str, ModelSpec] = {}
        for m in data.get("models", []):
            spec = ModelSpec(
                id=m["id"], file=m.get("file"), sha256=m.get("sha256"), license=m.get("license", ""),
                task=m["task"], arch=m["arch"],
                classes={int(k): v for k, v in (m.get("classes") or {}).items()},
                input_size=int(m.get("input_size") or 0), audit_capable=bool(m.get("audit_capable")),
                source=m.get("source", ""), extra=m,
            )
            self.models[spec.id] = spec
        self.profiles: dict[str, Profile] = {
            name: Profile(name=name, **p) for name, p in (data.get("profiles") or {}).items()
        }
        self._verified: set[str] = set()

    def get(self, model_id: str) -> ModelSpec:
        if model_id not in self.models:
            raise NBError(f"manifest에 없는 모델: {model_id}", code="E_MODEL_LICENSE")
        spec = self.models[model_id]
        if model_id not in self._verified:
            check_license(spec)
            check_hash(spec)
            self._verified.add(model_id)
        return spec

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            raise NBError(f"알 수 없는 프로파일: {name}")
        return self.profiles[name]

    def verify_all(self) -> list[str]:
        """모든 모델 검증. 오류 메시지 목록을 돌려준다 (CI용)."""
        errors = []
        for mid in self.models:
            try:
                self._verified.discard(mid)
                self.get(mid)
            except NBError as e:
                errors.append(f"{e.code}: {e.message}")
        return errors

    def model_hashes(self) -> dict[str, str | None]:
        return {m.id: m.sha256 for m in self.models.values()}


def available_providers() -> list[str]:
    import onnxruntime as ort

    avail = set(ort.get_available_providers())
    forced = os.environ.get("NURIBLUR_EP")
    if forced:
        return [forced, "CPUExecutionProvider"] if forced in avail else ["CPUExecutionProvider"]
    return [p for p in PROVIDER_ORDER if p in avail]


def ep_label() -> str:
    """UI 배지용 실행 환경 문자열."""
    provs = available_providers()
    top = provs[0] if provs else "CPUExecutionProvider"
    return {"CUDAExecutionProvider": "CUDA", "DmlExecutionProvider": "DirectML",
            "CoreMLExecutionProvider": "CoreML"}.get(top, "CPU")


def create_session(spec: ModelSpec):
    import onnxruntime as ort

    if spec.path is None:
        raise NBError(f"모델 {spec.id}는 가중치가 없는 규칙 모델입니다")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 2)
    return ort.InferenceSession(str(spec.path), sess_options=so, providers=available_providers())


@lru_cache(maxsize=1)
def default_registry() -> Registry:
    return Registry()
