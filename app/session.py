"""작업 큐 항목(영상 1개)의 UI 측 상태. 추론 결과는 워커가 준 값만 들고 있다."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.pb import nuriblur_pb2 as pb
from app.state.machine import JobState, S


@dataclass
class JobItem:
    path: Path
    media: pb.MediaInfo | None = None
    state: JobState = field(default_factory=JobState)
    project_path: Path | None = None
    output_path: Path | None = None
    job_id: str = ""
    tracks: dict[int, pb.Track] = field(default_factory=dict)
    suggestions: list[pb.MergeSuggestion] = field(default_factory=list)
    decisions: dict[int, pb.Decision] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    exposures: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    progress: float = 0.0
    last_output_sha: str = ""
    last_exposures: int = -1
    case_id: str = ""
    error: str = ""
    analysis_running: bool = False
    classes: list[str] = field(default_factory=lambda: ["face", "person", "plate"])
    profile: str = "cpu"
    audit_json: bool = False

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.project_path is None:
            self.project_path = self.path.with_suffix(".nbproj")
        if self.output_path is None:
            self.output_path = self.path.with_name(self.path.stem + "_blur.mp4")

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def s(self) -> S:
        return self.state.state

    def fps(self) -> float:
        return float(self.media.fps) if self.media and self.media.fps else 30.0

    def frames(self) -> int:
        return int(self.media.frames) if self.media else 0

    # ---- 판정 ----
    def status_of(self, tid: int) -> str:
        """protect | review | mask"""
        d = self.decisions.get(tid)
        if d is None:
            return "mask"
        if d.protected:
            return "protect"
        return "review" if d.flag == "REVIEW" else "mask"

    def click_rule_ids(self) -> set[int]:
        return {int(r["payload"].get("track_id")) for r in self.rules
                if r["kind"] == "click" and r["payload"].get("protect", True)}

    def set_click(self, tid: int, protect: bool) -> None:
        self.rules = [r for r in self.rules if not (r["kind"] == "click" and int(r["payload"].get("track_id", -1)) == tid)]
        if protect:
            self.rules.append({"kind": "click", "payload": {"track_id": tid, "protect": True}})
        elif self.decisions.get(tid) and self.decisions[tid].protected:
            # 승계로 보호된 트랙을 끄는 경우: 명시적 해제 규칙
            self.rules.append({"kind": "click", "payload": {"track_id": tid, "protect": False}})

    def rules_pb(self) -> list[pb.ProtectRule]:
        return [pb.ProtectRule(id=int(r.get("id") or 0), kind=r["kind"],
                               payload_json=json.dumps(r["payload"], ensure_ascii=False)) for r in self.rules]

    def load_decisions(self, dl: pb.DecisionList) -> None:
        self.decisions = {d.track_id: d for d in dl.decisions}
        if dl.rules:
            self.rules = [{"id": r.id, "kind": r.kind, "payload": json.loads(r.payload_json or "{}")} for r in dl.rules]

    def counters(self) -> tuple[int, int, int]:
        prot = sum(1 for d in self.decisions.values() if d.protected)
        rev = sum(1 for d in self.decisions.values() if not d.protected and d.flag == "REVIEW")
        return prot, len(self.decisions) - prot - rev, rev

    def manual_boxes_at(self, frame: int) -> list[tuple[float, float, float, float]]:
        """수동 마스킹 규칙(키프레임 박스)의 해당 프레임 박스 — 원본 화면에 '수동 마스킹' 영역을 표시하는 용도."""
        out = []
        for r in self.rules:
            kf = sorted((r.get("payload") or {}).get("frames") or [], key=lambda v: v[0]) if r["kind"] == "manual_box" else []
            for a, b in zip(kf, kf[1:] or kf, strict=False):
                if a[0] <= frame <= b[0]:
                    t = 0.0 if b[0] == a[0] else (frame - a[0]) / (b[0] - a[0])
                    out.append(tuple(a[i] + (b[i] - a[i]) * t for i in range(1, 5)))
                    break
        return out

    def tag(self, t: pb.Track) -> str:
        return {"face": "F", "person": "B", "plate": "P"}.get(t.cls, "?") + f"#{t.id}"
