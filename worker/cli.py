"""nuriblur CLI (G1-12) — 워커 코드를 in-process로 호출하는 헤드리스 진입점.

  nuriblur probe in.mp4
  nuriblur analyze in.mp4 -o case.nbproj [--profile cpu] [--interval 2] [--resume]
  nuriblur tracks case.nbproj [--frame N]
  nuriblur rules case.nbproj --protect 3,7 [--unprotect 5] [--plate 12가3456] [--manual "f,x,y,w,h;f,x,y,w,h"]
  nuriblur render case.nbproj -o out.mp4 [--style pixelate] [--audit]
  nuriblur audit case.nbproj out.mp4
  nuriblur worker            (gRPC 서버; NURIBLUR_PORT/NURIBLUR_TOKEN)

종료 코드: 0 성공, 1 일반 오류, 3 재검사 노출 있음, 4 모델 라이선스/해시 오류, 130 취소.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

EXIT_OK, EXIT_ERR, EXIT_EXPOSURE, EXIT_MODEL, EXIT_CANCEL = 0, 1, 3, 4, 130


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


class Printer:
    def __init__(self, quiet: bool = False, as_json: bool = False):
        self.quiet, self.as_json = quiet, as_json
        self.last_pct = -1

    def __call__(self, e: dict[str, Any]) -> None:
        t = e.get("type")
        if self.as_json:
            if t != "preview":
                print(json.dumps({k: v for k, v in e.items() if k != "preview_jpeg"}, ensure_ascii=False), flush=True)
            return
        if t in ("log", "warning", "error"):
            tag = {"log": "INFO", "warning": "WARN", "error": "ERROR"}[t]
            code = f" {e['code']}" if e.get("code") else ""
            print(f"[{tag}]{code} {e.get('message', '')}", file=sys.stderr if t != "log" else sys.stdout, flush=True)
        elif t == "progress" and not self.quiet and e.get("total"):
            pct = int(100 * e["frame"] / max(e["total"], 1))
            if pct != self.last_pct and pct % 5 == 0:
                self.last_pct = pct
                fps = f" {e['fps']:.1f}fps" if e.get("fps") else ""
                print(f"  {e.get('stage', '')}: {pct}%{fps}", flush=True)


def cmd_probe(a) -> int:
    from worker.pipeline.probe import probe

    print(json.dumps(probe(a.input, with_hash=not a.no_hash), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_analyze(a) -> int:
    from worker.jobs import JobControl, new_job_id
    from worker.pipeline.analyze import analyze

    jid = new_job_id()
    stats = analyze(a.input, a.output, jid, JobControl(jid), Printer(a.quiet, a.json),
                    classes=tuple(a.classes.split(",")), profile=a.profile, detect_interval=a.interval,
                    resume_from_frame=-1 if a.resume else 0)
    print(json.dumps(stats, ensure_ascii=False))
    return EXIT_OK


def cmd_tracks(a) -> int:
    from worker.io.project import Project
    from worker.pipeline.link import merge_suggestions

    with Project.open(a.project) as p:
        dec = p.decisions()
        tracks = p.tracks(with_boxes=True, at_frame=a.frame if a.frame is not None else None)
        for t in tracks:
            d = dec.get(t.id, {})
            state = "보호" if d.get("protected") else "마스킹"
            if d.get("flag") == "REVIEW":
                state += "·검수"
            print(f"{t.cls[0].upper()}#{t.id}\t{t.cls}\tf{t.start_f}-{t.end_f}\tconf {t.conf_avg:.2f}\t{state}"
                  + (f"\tlinked P#{t.linked_person_id}" if t.linked_person_id else "")
                  + (f"\tmerged→#{t.merged_into}" if t.merged_into else ""))
        if a.frame is None:
            for s in merge_suggestions(p.tracks(with_boxes=True), p.media().get("fps") or 30):
                print(f"병합 제안: #{s.from_id} → #{s.to_id} (유사도 {s.similarity:.2f}, {s.reason})")
    return EXIT_OK


def cmd_rules(a) -> int:
    from worker.io.project import Project
    from worker.pipeline import rules as rules_mod

    if a.ref:
        print("참조 사진 매칭(--ref)은 V1(G4-02)에서 지원합니다", file=sys.stderr)
        return EXIT_ERR
    with Project.open(a.project) as p:
        rules = [] if a.reset else [dict(id=r["id"], kind=r["kind"], payload=r["payload"]) for r in p.rules()]
        for tid in _ids(a.protect):
            rules = [r for r in rules if not (r["kind"] == "click" and r["payload"].get("track_id") == tid)]
            rules.append({"kind": "click", "payload": {"track_id": tid, "protect": True}})
        for tid in _ids(a.unprotect):
            rules = [r for r in rules if not (r["kind"] == "click" and r["payload"].get("track_id") == tid)]
        if a.plate:
            rules.append({"kind": "plate_text", "payload": {"plates": a.plate.split(","), "max_edit": 1}})
        if a.manual:
            frames = [[float(v) for v in kf.split(",")] for kf in a.manual.split(";") if kf.strip()]
            rules.append({"kind": "manual_box", "payload": {"frames": frames, "cls": "face"}})
        if a.mask_exposures:
            last = json.loads(p.get_meta("last_audit") or "{}").get("exposures", [])
            span = int(p.get_meta("detect_interval") or 2)
            rules += rules_mod.exposure_rules(last, span=span)
            print(f"노출 {len(last)}건을 수동 박스로 마스킹합니다")
        dec = rules_mod.apply(p, rules, actor="cli")
        p.save()
        prot = [d["track_id"] for d in dec if d["protected"]]
        rev = [d["track_id"] for d in dec if d["flag"] == "REVIEW"]
        print(json.dumps({"rules": len(p.rules()), "protected": prot, "review": rev}, ensure_ascii=False))
    return EXIT_OK


def _ids(s: str | None) -> list[int]:
    return [int(x) for x in (s or "").split(",") if x.strip()]


def cmd_render(a) -> int:
    from worker.jobs import JobControl, new_job_id
    from worker.pipeline.render import render

    jid = new_job_id()
    prof = {"style": a.style, "strength": a.strength, "pad_ratio": a.pad_ratio, "pad_frames": a.pad_frames,
            "codec": a.codec, "quality": a.quality, "strip_meta": not a.keep_meta, "watermark": a.watermark or "",
            "keep_audio": not a.no_audio, "mask_body_when_face_masked": a.mask_body,
            "mask_head_when_no_face": not a.no_head_fallback}
    r = render(a.project, a.output, prof, jid, JobControl(jid), Printer(a.quiet, a.json), run_audit=a.audit)
    print(json.dumps({k: v for k, v in r.items() if k != "exposures"} | {"exposures": len(r["exposures"])},
                     ensure_ascii=False))
    if r["exposures"]:
        for e in r["exposures"][:20]:
            print(f"  노출 f{e['frame']} {e['cls']} ({e['x']:.0f},{e['y']:.0f},{e['w']:.0f},{e['h']:.0f}) conf {e['conf']:.2f}")
        return EXIT_EXPOSURE
    return EXIT_OK


def cmd_audit(a) -> int:
    from worker.pipeline.render import audit_only

    ex = audit_only(a.project, a.output)
    print(json.dumps({"exposures": len(ex), "items": ex[:50]}, ensure_ascii=False, indent=1))
    return EXIT_EXPOSURE if ex else EXIT_OK


def cmd_dismiss(a) -> int:
    import getpass

    from worker.io.project import Project
    from worker.pipeline.dismiss import dismiss, refusal

    with Project.open(a.project) as p:
        last = json.loads(p.get_meta("last_audit") or "{}").get("exposures", [])
        pick = [e for e in last if not refusal(e)] if a.all else [last[i] for i in _ids(a.index) if 0 <= i < len(last)]
        r = dismiss(p, a.output, pick, a.actor or getpass.getuser(), a.reason)
    print(json.dumps({"accepted": len(r["accepted"]), "refused": r["refused"], "remaining": r["remaining"]},
                     ensure_ascii=False))
    return EXIT_OK if r["remaining"] == 0 else EXIT_EXPOSURE


def cmd_worker(a) -> int:
    from worker.server import main as server_main

    return server_main(a.rest)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nuriblur", description="NuriBlur 영상 비식별 처리 (로컬 전용)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", action="store_true", help="이벤트를 JSON 줄로 출력")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("probe")
    s.add_argument("input")
    s.add_argument("--no-hash", action="store_true")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("analyze")
    s.add_argument("input")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--profile", default="cpu", choices=["gpu_precise", "gpu_fast", "cpu"])
    s.add_argument("--interval", type=int, default=0)
    s.add_argument("--classes", default="face,person,plate")
    s.add_argument("--resume", action="store_true", help="체크포인트부터 이어서")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("tracks")
    s.add_argument("project")
    s.add_argument("--frame", type=int)
    s.set_defaults(fn=cmd_tracks)

    s = sub.add_parser("rules")
    s.add_argument("project")
    s.add_argument("--protect", help="보호할 트랙 ID (쉼표)")
    s.add_argument("--unprotect", help="보호 해제할 트랙 ID (쉼표)")
    s.add_argument("--plate", help="보호할 번호판 텍스트 (쉼표)")
    s.add_argument("--ref", help="참조 사진 (V1)")
    s.add_argument("--manual", help='수동 박스 키프레임 "f,x,y,w,h;f,x,y,w,h"')
    s.add_argument("--reset", action="store_true", help="기존 규칙 삭제")
    s.add_argument("--mask-exposures", action="store_true", help="마지막 재검사 노출 영역을 수동 박스로 마스킹")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("dismiss", help="재검사 오탐 확인('노출 아님') — 신뢰도 0.5 미만 얼굴·번호판만")
    s.add_argument("project")
    s.add_argument("-o", "--output", required=True, help="최근 재검사한 출력본")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--index", help="최근 재검사 노출 목록의 순번 (0부터, 쉼표)")
    g.add_argument("--all", action="store_true", help="확인 가능한 노출 전부")
    s.add_argument("--reason", required=True, help="확인 사유 (기록됨)")
    s.add_argument("--actor", help="확인자 (기본: Windows 계정)")
    s.set_defaults(fn=cmd_dismiss)

    s = sub.add_parser("render")
    s.add_argument("project")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--style", default="pixelate", choices=["pixelate", "gaussian", "solid"])
    s.add_argument("--strength", type=float, default=8.0)
    s.add_argument("--pad-ratio", type=float, default=1.25)
    s.add_argument("--pad-frames", type=int, default=5)
    s.add_argument("--codec", default="source", choices=["source", "h264", "hevc"])
    s.add_argument("--quality", default="source", choices=["source", "high", "normal"])
    s.add_argument("--keep-meta", action="store_true")
    s.add_argument("--no-audio", action="store_true")
    s.add_argument("--watermark")
    s.add_argument("--mask-body", action="store_true")
    s.add_argument("--no-head-fallback", action="store_true")
    s.add_argument("--audit", action="store_true", help="노출 재검사 실행")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("audit")
    s.add_argument("project")
    s.add_argument("output")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("worker")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_worker)
    return ap


def main(argv: list[str] | None = None) -> int:
    _utf8()
    a = build_parser().parse_args(argv)
    from worker.errors import Cancelled, ModelHashError, ModelLicenseError, NBError

    try:
        return a.fn(a)
    except (ModelHashError, ModelLicenseError) as e:
        print(f"[ERROR] {e.code} {e}", file=sys.stderr)
        return EXIT_MODEL
    except Cancelled:
        print("[ERROR] E_CANCELLED", file=sys.stderr)
        return EXIT_CANCEL
    except NBError as e:
        if e.code in ("E_MODEL_HASH", "E_MODEL_LICENSE"):
            print(f"[ERROR] {e.code} {e}", file=sys.stderr)
            return EXIT_MODEL
        print(f"[ERROR] {e.code} {e}", file=sys.stderr)
        return EXIT_ERR
    except KeyboardInterrupt:
        return EXIT_CANCEL


if __name__ == "__main__":
    sys.exit(main())
