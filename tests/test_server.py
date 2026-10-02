"""G1-11 gRPC 워커: 토큰·경로 검사·스트림·제어."""
from __future__ import annotations

import json
import secrets
import threading

import grpc
import pytest

from worker.pb import nuriblur_pb2 as pb
from worker.pb import nuriblur_pb2_grpc as pbg
from worker.server import serve

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def worker(tmp_path_factory, short_clip):
    root = tmp_path_factory.mktemp("srv")
    token = secrets.token_hex(32)
    server, svc, port = serve(0, token, [str(root), str(short_clip.parent)], block=False)
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    stub = pbg.NuriBlurWorkerStub(ch)
    md = (("x-nb-token", token),)
    stub.svc = svc  # 서버 내부 상태 확인용(테스트 전용)
    yield stub, md, root, short_clip
    ch.close()
    svc.shutdown()
    server.stop(0)


def test_token_required(worker):
    stub, md, root, clip = worker
    with pytest.raises(grpc.RpcError) as e:
        stub.Probe(pb.ProbeRequest(path=str(clip)))
    assert e.value.code() == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(grpc.RpcError) as e:
        stub.Probe(pb.ProbeRequest(path=str(clip)), metadata=(("x-nb-token", "wrong"),))
    assert e.value.code() == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(grpc.RpcError) as e:
        list(stub.Analyze(pb.AnalyzeRequest(path=str(clip), project_path="x"), metadata=(("x-nb-token", "bad"),)))
    assert e.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_path_guard(worker, tmp_path):
    stub, md, root, clip = worker
    with pytest.raises(grpc.RpcError) as e:
        stub.Probe(pb.ProbeRequest(path=str(root / ".." / ".." / "etc" / "passwd")), metadata=md)
    assert e.value.code() == grpc.StatusCode.PERMISSION_DENIED
    outside = tmp_path / "x.mp4"
    with pytest.raises(grpc.RpcError) as e:
        stub.Probe(pb.ProbeRequest(path=str(outside)), metadata=md)
    assert e.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_full_flow(worker):
    stub, md, root, clip = worker
    info = stub.Health(pb.Empty(), metadata=md)
    assert info.ep and info.version
    m = stub.Probe(pb.ProbeRequest(path=str(clip)), metadata=md)
    assert m.frames > 0 and m.width == 1280
    proj = root / "case.nbproj"
    evs = list(stub.Analyze(pb.AnalyzeRequest(job_id="ja", path=str(clip), project_path=str(proj), profile="cpu",
                                              detect_interval=2), metadata=md))
    types = {e.type for e in evs}
    assert "progress" in types and evs[-1].type == "done", [(e.type, e.message) for e in evs if e.type == "error"]
    assert any(e.type == "preview" and e.preview_jpeg[:2] == b"\xff\xd8" for e in evs)

    tl = stub.ListTracks(pb.ListTracksRequest(project_path=str(proj), at_frame=-1), metadata=md)
    assert tl.tracks
    face = next(t for t in tl.tracks if t.cls == "face")
    at = stub.ListTracks(pb.ListTracksRequest(project_path=str(proj), at_frame=face.start_f), metadata=md)
    assert any(t.id == face.id and len(t.boxes) == 1 for t in at.tracks)

    dl = stub.ApplyRules(pb.ApplyRulesRequest(project_path=str(proj), rules=[
        pb.ProtectRule(kind="click", payload_json=json.dumps({"track_id": face.id}))]), metadata=md)
    assert {d.track_id for d in dl.decisions if d.protected} >= {face.id}
    assert dl.rules and dl.rules[0].id > 0
    kept = stub.ApplyRules(pb.ApplyRulesRequest(project_path=str(proj), keep_stored=True), metadata=md)
    assert [r.kind for r in kept.rules] == ["click"]

    j0 = stub.GetFrame(pb.FrameRequest(project_path=str(proj), frame=5, masked=False, max_width=640), metadata=md)
    j1 = stub.GetFrame(pb.FrameRequest(project_path=str(proj), frame=5, masked=True, max_width=640,
                                       profile=pb.RenderProfile(style="solid")), metadata=md)
    assert j0.jpeg[:2] == b"\xff\xd8" and j1.jpeg != j0.jpeg

    out = root / "out.mp4"
    evs = list(stub.Render(pb.RenderRequest(job_id="jr", project_path=str(proj), output_path=str(out),
                                            profile=pb.RenderProfile(style="pixelate", strip_meta=True, keep_audio=True),
                                            run_audit=True), metadata=md))
    done = [e for e in evs if e.type == "done"]
    assert done and done[0].audit_exposures == 0 and len(done[0].output_sha256) == 64
    # 재검사 중에도 미리보기 화면이 진행된다
    first_audit = next(i for i, e in enumerate(evs) if e.type == "progress" and e.stage == "audit")
    assert any(e.type == "preview" for e in evs[first_audit:])
    ar = stub.AuditCheck(pb.AuditRequest(project_path=str(proj), output_path=str(out)), metadata=md)
    assert ar.exposures == 0

    # 오탐 확인: 재검사 결과에 없는 항목은 거부, 사유 없으면 FAILED_PRECONDITION
    req = pb.DismissRequest(project_path=str(proj), output_path=str(out), classes=["face"], actor="t", reason="반사")
    req.items.add(frame=3, x=1, y=2, w=20, h=24, conf=0.3)
    dr = stub.DismissExposures(req, metadata=md)
    assert dr.accepted == 0 and dr.refused == 1 and dr.remaining == 0
    req.reason = " "
    with pytest.raises(grpc.RpcError) as e:
        stub.DismissExposures(req, metadata=md)
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_cancel_and_resume(worker):
    stub, md, root, clip = worker
    proj = root / "cancel.nbproj"
    got = []

    def run():
        for e in stub.Analyze(pb.AnalyzeRequest(job_id="jc", path=str(clip), project_path=str(proj), profile="cpu",
                                                detect_interval=1), metadata=md):
            got.append(e)
            if e.type == "progress" and e.stage == "detect" and e.frame >= 3:
                stub.Control(pb.ControlRequest(job_id="jc", action="cancel"), metadata=md)

    t = threading.Thread(target=run)
    t.start()
    t.join(120)
    assert any(e.type == "error" and e.code == "E_CANCELLED" for e in got)
    # 체크포인트부터 재개(-1 = 자동)
    evs = list(stub.Analyze(pb.AnalyzeRequest(job_id="jc2", path=str(clip), project_path=str(proj), profile="cpu",
                                              detect_interval=1, resume_from_frame=-1), metadata=md))
    assert evs[-1].type == "done"
    assert any("이어서" in e.message for e in evs if e.type == "log")


def test_mask_last_audit_and_batch_merge(worker):
    """'모두 마스킹하고 다시 내보내기'는 UI 200건 제한 없이 재검사 노출 전부를 수동 박스로, 병합은 일괄 처리."""
    stub, md, root, clip = worker
    proj = root / "case.nbproj"
    p = stub.svc._project(str(proj))
    exps = [{"frame": f, "cls": "face", "x": 10.0 + f, "y": 20.0, "w": 30.0, "h": 36.0, "conf": 0.3} for f in range(250)]
    p.set_meta("last_audit", json.dumps({"output": str(root / "out.mp4"), "exposures": exps, "count": 250}))
    kept = stub.ApplyRules(pb.ApplyRulesRequest(project_path=str(proj), keep_stored=True), metadata=md)
    dl = stub.ApplyRules(pb.ApplyRulesRequest(project_path=str(proj), rules=kept.rules, mask_last_audit=True), metadata=md)
    audit_boxes = [r for r in dl.rules if r.kind == "manual_box" and json.loads(r.payload_json).get("source") == "audit"]
    assert len(audit_boxes) == 250 and len(dl.rules) == len(kept.rules) + 250

    tl = stub.ListTracks(pb.ListTracksRequest(project_path=str(proj), at_frame=-1), metadata=md)
    faces = sorted(t.id for t in tl.tracks if t.cls == "face" and not t.merged_into)[:4]
    ack = stub.MergeTracks(pb.MergeRequest(project_path=str(proj), from_ids=[faces[1], faces[3]],
                                           to_ids=[faces[0], faces[2]]), metadata=md)
    assert ack.ok and ack.message == "2"
    tl = stub.ListTracks(pb.ListTracksRequest(project_path=str(proj), at_frame=-1), metadata=md)
    merged = {t.id: t.merged_into for t in tl.tracks}
    assert merged[faces[1]] == faces[0] and merged[faces[3]] == faces[2]
