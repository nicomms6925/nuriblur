"""proto/nuriblur.proto → worker/pb, app/pb 생성 (G0-02).

grpcio-tools가 만든 *_pb2_grpc.py는 절대 import(`import nuriblur_pb2`)를 쓰므로
패키지 내부 상대 import로 고쳐 쓴다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto" / "nuriblur.proto"
TARGETS = [ROOT / "worker" / "pb", ROOT / "app" / "pb"]


def main() -> int:
    from grpc_tools import protoc

    for out in TARGETS:
        out.mkdir(parents=True, exist_ok=True)
        rc = protoc.main([
            "grpc_tools.protoc",
            f"-I{PROTO.parent}",
            f"--python_out={out}",
            f"--pyi_out={out}",
            f"--grpc_python_out={out}",
            str(PROTO),
        ])
        if rc != 0:
            print(f"protoc 실패: {out}", file=sys.stderr)
            return rc
        grpc_file = out / "nuriblur_pb2_grpc.py"
        src = grpc_file.read_text(encoding="utf-8")
        src = re.sub(r"^import nuriblur_pb2 as", "from . import nuriblur_pb2 as", src, flags=re.M)
        grpc_file.write_text(src, encoding="utf-8")
        (out / "__init__.py").write_text('"""생성 코드 — 직접 수정하지 말 것 (make proto)."""\n', encoding="utf-8")
        print(f"생성: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
