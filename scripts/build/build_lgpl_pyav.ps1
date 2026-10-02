# LGPL FFmpeg로 PyAV 휠 빌드 (docs/09: FFmpeg는 LGPL 빌드만)
#
#   powershell -ExecutionPolicy Bypass -File scripts\build\build_lgpl_pyav.ps1
#
# 결과: .build\wheelhouse\av-<ver>-cp311-abi3-win_amd64.whl (LGPL FFmpeg DLL 동봉)
# 요구: Visual Studio Build Tools(C++), uv, 인터넷(BtbN FFmpeg-Builds, PyPI)
# 확인: python scripts\ci\check_license.py --packaging  → 통과해야 배포 가능
param(
  [string]$AvVersion = "18.1.0",
  [string]$FfmpegAsset = "ffmpeg-n8.1-latest-win64-lgpl-shared-8.1"   # PyAV 18 = FFmpeg 8.x (libavcodec 62)
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
$B = Join-Path $Root ".build"
New-Item -ItemType Directory -Force $B | Out-Null
Set-Location $B

# 1) LGPL FFmpeg (shared)
if (-not (Test-Path "ffmpeg-lgpl\bin\avcodec-62.dll")) {
  $url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$FfmpegAsset.zip"
  Invoke-WebRequest $url -OutFile ffmpeg-lgpl.zip
  Expand-Archive ffmpeg-lgpl.zip -DestinationPath . -Force
  if (Test-Path ffmpeg-lgpl) { Remove-Item -Recurse -Force ffmpeg-lgpl }
  Rename-Item $FfmpegAsset ffmpeg-lgpl
}
$cfg = & ".\ffmpeg-lgpl\bin\ffmpeg.exe" -hide_banner -buildconf 2>&1 | Out-String
if ($cfg -match "--enable-gpl|--enable-nonfree|libx264|libx265") { throw "GPL/nonfree 구성 FFmpeg입니다: 중단" }

# 2) PyAV 소스
if (-not (Test-Path "av-$AvVersion")) {
  $meta = Invoke-RestMethod "https://pypi.org/pypi/av/$AvVersion/json"
  $sdist = ($meta.urls | Where-Object packagetype -eq "sdist").url
  Invoke-WebRequest $sdist -OutFile av-src.tar.gz
  tar xzf av-src.tar.gz
}

# 3) 빌드 환경
if (-not (Test-Path bvenv)) { uv venv --python 3.11 bvenv }
uv pip install --python bvenv\Scripts\python.exe "cython>=3.1" setuptools wheel delvewheel

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vcvars = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\Auxiliary\Build\vcvars64.bat" | Select-Object -Last 1
if (-not $vcvars) { throw "Visual Studio C++ Build Tools가 필요합니다" }
$ff = (Resolve-Path ffmpeg-lgpl).Path
@"
@echo off
call "$vcvars" >nul
set DISTUTILS_USE_SDK=1
set MSSdk=1
cd /d "$B\av-$AvVersion"
"$B\bvenv\Scripts\python.exe" setup.py bdist_wheel --ffmpeg-dir=$ff
"@ | Set-Content -Encoding ascii build_av.cmd
cmd /c build_av.cmd
if ($LASTEXITCODE -ne 0) { throw "PyAV 빌드 실패" }

# 4) FFmpeg DLL 동봉
& bvenv\Scripts\delvewheel.exe repair --add-path "$ff\bin" -w wheelhouse (Get-ChildItem "av-$AvVersion\dist\*.whl").FullName
Get-ChildItem wheelhouse
Write-Host "설치: uv pip install --python .venv\Scripts\python.exe --reinstall --no-deps .build\wheelhouse\<휠>"
