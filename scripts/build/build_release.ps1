# 배포본 빌드: uv 환경 → LGPL PyAV 교체 → 라이선스 게이트 → PyInstaller → Inno Setup(온라인·폐쇄망)
#
#   powershell -ExecutionPolicy Bypass -File scripts\build\build_release.ps1
#
# 결과: .build\installer\NuriBlur-<ver>-setup.exe, NuriBlur-<ver>-offline-setup.exe
# 요구: uv, Visual Studio C++ Build Tools(LGPL PyAV 빌드), Inno Setup 6 (.build\InnoSetup 또는 PATH의 ISCC)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $Root

uv sync --locked --group build
uv run python scripts/gen_proto.py

# 1) LGPL PyAV (없으면 빌드)
$wheel = Get-ChildItem .build\wheelhouse\av-*.whl -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $wheel) {
  & "$PSScriptRoot\build_lgpl_pyav.ps1"
  $wheel = Get-ChildItem .build\wheelhouse\av-*.whl | Select-Object -First 1
}
uv pip install --reinstall --no-deps $wheel.FullName

# 2) 라이선스 게이트 (GPL FFmpeg·금지 모델·AGPL 의존성)
uv run python scripts/ci/check_license.py --packaging
if ($LASTEXITCODE -ne 0) { throw "라이선스 검사 실패 — 배포 중단" }

# 3) PyInstaller
uv run python -m PyInstaller packaging/pyinstaller.spec --noconfirm --distpath .build/dist --workpath .build/pyi-work
& .build\dist\NuriBlur\NuriBlur.exe --probe-encoder h264_mf | Out-Null

# 4) Inno Setup (온라인 / 폐쇄망)
$iscc = if (Test-Path .build\InnoSetup\ISCC.exe) { ".build\InnoSetup\ISCC.exe" } else { "ISCC.exe" }
& $iscc /Q packaging\installer.iss
& $iscc /Q /DOFFLINE packaging\installer.iss
Get-ChildItem .build\installer

# 개발 환경 복구(PyPI av) — 개발·테스트는 uv.lock 그대로
uv sync --locked
