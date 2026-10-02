# -*- mode: python ; coding: utf-8 -*-
# NuriBlur PyInstaller spec (G3-05)
#
#   pyinstaller packaging/pyinstaller.spec --noconfirm
#
# 출력: dist/NuriBlur/NuriBlur.exe (UI) — 같은 exe를 `--worker` 인자로 실행하면 gRPC 워커 프로세스가 된다.
# 사전 조건 (배포 전 필수):
#   1. `python scripts/ci/check_license.py --packaging` 통과 — PyAV를 LGPL FFmpeg 빌드로 교체(GPL x264/x265 제외)
#   2. packaging/fonts/ 에 OFL 한글 폰트(NotoSansKR-Regular.ttf 등) 배치 — 보고서 PDF·워터마크용
#   3. models/manifest.json 의 모든 모델 해시 검증 통과(번들 포함)
from pathlib import Path

ROOT = Path(SPECPATH).parent
block_cipher = None

datas = [
    (str(ROOT / "models" / "manifest.json"), "models"),
    (str(ROOT / "app" / "i18n" / "ko.json"), "app/i18n"),
    (str(ROOT / "docs" / "09-LICENSE-COMPLIANCE.md"), "licenses"),
]
for onnx in (ROOT / "models").glob("*.onnx"):
    datas.append((str(onnx), "models"))
fonts = ROOT / "packaging" / "fonts"
if fonts.exists():
    datas += [(str(f), "packaging/fonts") for f in fonts.glob("*.ttf")]

hidden = [
    "worker.server", "worker.pipeline.analyze", "worker.pipeline.render", "worker.pipeline.encode",
    "worker.orgmode.report", "grpc", "onnxruntime", "av", "reportlab.pdfbase.ttfonts",
]
excludes = ["ultralytics", "torch", "tensorflow", "matplotlib", "tkinter", "IPython", "pytest"]

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NuriBlur",
    console=False,
    icon=None,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="NuriBlur")
