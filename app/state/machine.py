"""작업 1건 상태 머신 (docs/02 상태 머신을 그대로).

CREATED → PROBED → ANALYZING ⇄ PAUSED → ANALYZED → RULES_APPLIED → REVIEWING
        → RENDERING ⇄ PAUSED → AUDITED → PENDING_APPROVAL → APPROVED → DELIVERED → RETAINED → PURGED
        └ 어느 단계든 CANCELLED / FAILED
- REJECTED(반려)는 REVIEWING으로 되돌린다. 재렌더링은 RENDERING부터.
- AUDITED는 재검사 0건일 때만. 1건 이상이면 REVIEWING.
"""
from __future__ import annotations

from enum import StrEnum


class S(StrEnum):
    CREATED = "CREATED"
    PROBED = "PROBED"
    ANALYZING = "ANALYZING"
    PAUSED = "PAUSED"
    ANALYZED = "ANALYZED"
    RULES_APPLIED = "RULES_APPLIED"
    REVIEWING = "REVIEWING"
    RENDERING = "RENDERING"
    AUDITED = "AUDITED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    DELIVERED = "DELIVERED"
    RETAINED = "RETAINED"
    PURGED = "PURGED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


ANY_TERMINAL = {S.CANCELLED, S.FAILED}

TRANSITIONS: dict[S, set[S]] = {
    S.CREATED: {S.PROBED},
    S.PROBED: {S.ANALYZING, S.ANALYZED},                 # 기존 프로젝트 열기 → ANALYZED
    S.ANALYZING: {S.PAUSED, S.ANALYZED},
    S.PAUSED: {S.ANALYZING, S.RENDERING},                # 일시정지 → 원래 단계로 재개
    S.ANALYZED: {S.RULES_APPLIED, S.ANALYZING},
    S.RULES_APPLIED: {S.RULES_APPLIED, S.REVIEWING, S.RENDERING, S.ANALYZING},  # ANALYZING: 처음부터 다시 분석
    S.REVIEWING: {S.RULES_APPLIED, S.REVIEWING, S.RENDERING, S.AUDITED, S.ANALYZING},  # 남은 노출이 모두 '오탐 확인'되면 AUDITED
    S.RENDERING: {S.PAUSED, S.AUDITED, S.REVIEWING},
    S.AUDITED: {S.PENDING_APPROVAL, S.RENDERING, S.REVIEWING, S.RULES_APPLIED, S.DELIVERED, S.ANALYZING},  # 기관 모드 아님 → 바로 제공
    S.PENDING_APPROVAL: {S.APPROVED, S.REVIEWING},       # 반려 → REVIEWING
    S.APPROVED: {S.DELIVERED, S.REVIEWING},
    S.DELIVERED: {S.RETAINED, S.PURGED},
    S.RETAINED: {S.PURGED},
    S.PURGED: set(),
    S.CANCELLED: {S.PROBED, S.ANALYZING, S.ANALYZED, S.RULES_APPLIED, S.REVIEWING},  # 재시도/재개
    S.FAILED: {S.PROBED, S.ANALYZING, S.ANALYZED, S.RULES_APPLIED, S.REVIEWING},
}


class InvalidTransition(Exception):
    pass


class JobState:
    def __init__(self, state: S = S.CREATED):
        self.state = state
        self.history: list[S] = [state]
        self._resume_to: S | None = None

    def can(self, to: S) -> bool:
        return to in ANY_TERMINAL or to in TRANSITIONS[self.state]

    def go(self, to: S) -> S:
        if to == S.PAUSED:
            self._resume_to = self.state
        if self.state == S.PAUSED and to not in ANY_TERMINAL and to != self._resume_to:
            raise InvalidTransition(f"{self.state} -> {to} (resume to {self._resume_to})")
        if not self.can(to):
            raise InvalidTransition(f"{self.state.value} -> {to.value}")
        self.state = to
        self.history.append(to)
        return to

    def resume(self) -> S:
        if self.state != S.PAUSED or self._resume_to is None:
            raise InvalidTransition("not paused")
        return self.go(self._resume_to)

    @property
    def analyzed(self) -> bool:
        return self.state not in (S.CREATED, S.PROBED, S.ANALYZING) and not (
            self.state in ANY_TERMINAL and S.ANALYZED not in self.history)


# 작업 상태 → 화면 단계(1~6)
STEP_OF = {
    S.CREATED: 1, S.PROBED: 1, S.ANALYZING: 2, S.PAUSED: 2, S.ANALYZED: 3, S.RULES_APPLIED: 3,
    S.REVIEWING: 4, S.RENDERING: 2, S.AUDITED: 6, S.PENDING_APPROVAL: 6, S.APPROVED: 6, S.DELIVERED: 6,
    S.RETAINED: 6, S.PURGED: 6, S.CANCELLED: 1, S.FAILED: 1,
}
