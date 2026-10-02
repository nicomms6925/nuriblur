"""결과 보고서 PDF (G3-04) — reportlab, 한글 폰트 임베드.

항목(docs/08): 보고서 번호, 접수번호, 처리 근거, 청구인·보호대상 근거, 마스킹 통계(트랙 수·스타일·강도),
검수 처리 내역, 노출 재검사 결과, 출력 파일명·SHA-256·메타데이터 제거 여부,
결재 서명란(단계 수 + 출력 제공), 감사 로그 요약(부록), AI 자동 검출 고지(AI기본법 투명성).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from worker.orgmode.approval import Approval
from worker.orgmode.auditlog import AuditLog
from worker.orgmode.fonts import korean_font_path

BRAND = colors.HexColor("#0E7A57")
GREY = colors.HexColor("#6F7785")
LINE = colors.HexColor("#D9DEE5")
FONT = "NBKorean"

AI_NOTICE = ("본 출력물의 얼굴·번호판 마스킹은 AI 자동 검출 결과를 바탕으로 하며, 담당자 검수와 노출 재검사를 거쳤습니다. "
             "자동 검출에는 누락 가능성이 있으므로 제공 전 담당자가 확인해야 합니다. "
             "모든 처리는 기관 내부 PC에서 수행되었으며 영상·모델·임베딩은 외부로 전송되지 않았습니다.")


def _font() -> str:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return FONT
    p = korean_font_path()
    if p is None:
        raise RuntimeError("한글 폰트를 찾을 수 없습니다 (NURIBLUR_FONT 환경변수 또는 packaging/fonts)")
    pdfmetrics.registerFont(TTFont(FONT, str(p)))
    return FONT


def project_summary(project_path: str) -> dict[str, Any]:
    """프로젝트에서 보고서용 통계만 읽는다(추론 없음)."""
    from worker.io.project import Project

    out: dict[str, Any] = {}
    if not project_path or not Path(project_path).exists():
        return out
    with Project.open(project_path) as p:
        media = p.media()
        tracks = p.tracks()
        dec = p.decisions()
        rules = p.rules()
        prof = json.loads(p.get_meta("render_profile") or "{}")
        renders = p.jobs("render")
    out["media"] = media
    out["counts"] = {c: sum(1 for t in tracks if t.cls == c) for c in ("face", "person", "plate")}
    out["masked"] = {c: sum(1 for t in tracks if t.cls == c and not (dec.get(t.id) or {}).get("protected"))
                     for c in ("face", "person", "plate")}
    prot = [t for t in tracks if (dec.get(t.id) or {}).get("protected")]
    kinds = {r["id"]: r["kind"] for r in rules}
    label = {"click": "담당자 클릭 지정", "ref_face": "참조 사진 일치", "plate_text": "번호판 텍스트"}
    out["protected"] = [f"{t.cls[0].upper()}#{t.id} · {label.get(kinds.get(dec[t.id]['source_rule_id']), '승계')}"
                        f" ({dec[t.id]['confidence']:.2f})" for t in prot]
    out["review"] = {
        "merged": sum(1 for t in tracks if t.merged_into),
        "manual": sum(1 for r in rules if r["kind"] == "manual_box"),
        "review_flags": sum(1 for d in dec.values() if d["flag"] == "REVIEW"),
        "exclusions": sum(1 for r in rules if r["kind"] in ("region", "timerange")),
    }
    out["profile"] = prof
    out["render"] = renders[-1] if renders else None
    return out


def report_number(case: dict[str, Any]) -> str:
    return "NB-" + datetime.now().strftime("%Y-%m%d") + "-" + case["id"][-6:].upper()


def generate(approval: Approval, case_id: str, out_path: str | Path, operator: str = "") -> Path:
    f = _font()
    case = approval.case(case_id)
    steps = approval.steps(case_id)
    summ = project_summary(case["project_path"])
    log = AuditLog(approval.db).entries(case_id)
    chain_ok, _ = AuditLog(approval.db).verify()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    st = {
        "title": ParagraphStyle("t", fontName=f, fontSize=16, leading=21, textColor=colors.black),
        "sub": ParagraphStyle("s", fontName=f, fontSize=8.5, leading=11, textColor=GREY),
        "h": ParagraphStyle("h", fontName=f, fontSize=10.5, leading=14, textColor=BRAND, spaceBefore=8, spaceAfter=3),
        "b": ParagraphStyle("b", fontName=f, fontSize=9, leading=13),
        "small": ParagraphStyle("sm", fontName=f, fontSize=7.5, leading=10, textColor=GREY),
        "k": ParagraphStyle("k", fontName=f, fontSize=8, leading=11, textColor=GREY),
    }
    P = Paragraph
    rn = report_number(case)
    media = summ.get("media") or {}
    rend = summ.get("render") or {}
    prof = summ.get("profile") or {}
    exposures = case["audit_exposures"]
    story: list[Any] = []
    head = Table([[P("영상 비식별 처리 결과 보고서", st["title"]),
                   P(f"재검사 {'통과 · 노출 0건' if exposures == 0 else f'노출 {exposures}건'}", st["b"])],
                  [P(f"{rn} · {Path(media.get('path', '')).name} · {datetime.now():%Y-%m-%d %H:%M}", st["sub"]), ""]],
                 colWidths=[130 * mm, 45 * mm])
    head.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 1.5, BRAND), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
    story += [head, Spacer(1, 6)]

    style_label = {"pixelate": "픽셀화", "gaussian": "블러", "solid": "단색"}.get(prof.get("style", ""), prof.get("style", "-"))
    c, m = summ.get("counts", {}), summ.get("masked", {})
    rv = summ.get("review", {})
    grid = [
        ("보고서 번호", rn),
        ("접수번호", case["receipt_no"] or "-"),
        ("처리 근거", case["legal_basis"] or "-"),
        ("청구인(보호대상)", f"{case['requester_name'] or '-'} · " + (", ".join(summ.get("protected", [])) or "지정 없음")),
        ("마스킹 처리", f"얼굴 {m.get('face', 0)}/{c.get('face', 0)} · 전신 {m.get('person', 0)}/{c.get('person', 0)} · "
                    f"번호판 {m.get('plate', 0)}/{c.get('plate', 0)} 트랙 · {style_label} 강도 {prof.get('strength', '-')}"
                    f" · 여백 ×{prof.get('pad_ratio', '-')} · 패딩 {prof.get('pad_frames', '-')}프레임"),
        ("검수", f"병합 {rv.get('merged', 0)} · 수동 박스 {rv.get('manual', 0)} · 검수 플래그 {rv.get('review_flags', 0)}"
               f" · 제외 규칙 {rv.get('exclusions', 0)}"),
        ("노출 재검사", "출력본을 검출기(신뢰도 0.2)로 재검사: 보호대상 외 얼굴 "
                    + (f"{exposures}건" if exposures is not None else "미실시")),
        ("출력 파일", f"{Path(case['output_path'] or '').name or '-'} · SHA-256 {case['output_sha256'] or '-'}"),
        ("메타데이터 제거", "예 (GPS·촬영일시 등)" if prof.get("strip_meta", True) else "아니오"),
        ("원본", f"{Path(media.get('path', '')).name} · SHA-256 {media.get('sha256', '-')}"),
        ("처리 환경", f"로컬 처리 · 인코더 {json.loads(rend.get('stats') or '{}').get('encoder', '-') if rend else '-'}"),
    ]
    t = Table([[P(k, st["k"]), P(v, st["b"])] for k, v in grid], colWidths=[32 * mm, 143 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [t, Spacer(1, 10), P("결재", st["h"])]

    cells = []
    for s in steps:
        when = (s.decided_at or "")[:16].replace("T", " ")
        cells.append(P(f"<font size=7 color='#6F7785'>{s.role}</font><br/>"
                       f"{(s.user or '-') + (' · ' + when if s.decision == 'APPROVED' else ' · 대기')}", st["b"]))
    delivered = case["status"] in ("DELIVERED", "RETAINED", "PURGED")
    cells.append(P("<font size=7 color='#6F7785'>출력 제공</font><br/>"
                   + (f"제공 완료 · 보관 ~{(case['retention_until'] or '')[:10]}" if delivered else "—"), st["b"]))
    w = 175 * mm / len(cells)
    sign = Table([cells], colWidths=[w] * len(cells), rowHeights=[18 * mm])
    sign.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [sign, Spacer(1, 10), P("고지", st["h"]), P(AI_NOTICE, st["small"])]

    story += [PageBreak(), P("부록 — 감사 로그", st["h"]),
              P(f"해시 체인 검증: {'정상' if chain_ok else '이상 발견'} · 항목 {len(log)}건 · 보고서 작성 {operator}", st["small"]),
              Spacer(1, 4)]
    rows = [[P("시각", st["k"]), P("행위자", st["k"]), P("행위", st["k"]), P("대상·내용", st["k"])]]
    for e in log[-200:]:
        detail = e["detail"] if len(e["detail"] or "") < 160 else e["detail"][:157] + "…"
        rows.append([P(e["ts"][5:16].replace("T", " "), st["small"]), P(e["actor"], st["small"]),
                     P(e["action"], st["small"]), P(f"{e['target']} {detail}", st["small"])])
    lt = Table(rows, colWidths=[24 * mm, 22 * mm, 26 * mm, 103 * mm], repeatRows=1)
    lt.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(lt)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(f, 7)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 10 * mm, f"NuriBlur · {rn} · {case['receipt_no'] or ''}")
        canvas.drawRightString(192 * mm, 10 * mm, f"{doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"영상 비식별 처리 결과 보고서 {rn}",
                            author="NuriBlur")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    approval.set_report(case_id, operator or "시스템", str(out_path))
    return out_path
