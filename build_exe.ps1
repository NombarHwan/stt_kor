# STT_KOR - PyInstaller 로컬 빌드 스크립트
#
# 사용법:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# 결과물:
#   dist\STT_KOR\STT_KOR.exe        <- 실행 파일 (폴더째로 배포)
#   STT_KOR-local-windows-x64.zip   <- 배포용 zip (릴리스는 Actions가 자동 생성)
#
# GPU(CUDA) 라이브러리는 exe에 넣지 않고, 실행 중 필요할 때
# stt_app.py가 %LOCALAPPDATA%로 내려받습니다.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1) 빌드 도구 및 런타임 의존성 설치
python -m pip install --upgrade pip
python -m pip install --upgrade pyinstaller faster-whisper

# 2) 이전 산출물 정리
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force STT_KOR.spec, STT_KOR-local-windows-x64.zip -ErrorAction SilentlyContinue

# 3) 빌드 (onedir + UPX 미사용 -> Windows Defender 오탐 최소화)
#   --collect-all: faster-whisper와 그 바이너리 의존성(모델 로더/오디오 디코더)
#                  의 데이터 파일과 DLL을 빠짐없이 포함
python -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --noupx `
    --name STT_KOR `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-all onnxruntime `
    stt_app.py

# 4) 배포용 zip + 체크섬
$zip = "STT_KOR-local-windows-x64.zip"
Compress-Archive -Path dist/STT_KOR -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$hash  $zip" | Out-File "$zip.sha256.txt" -Encoding ascii

# 5) 설치 마법사(Inno Setup) - ISCC 가 있으면 함께 만든다
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source } else { $iscc = $null }
}
$setup = $null
if ($iscc) {
    & $iscc /DAppVersion=0.0.0-local installer.iss
    if ($LASTEXITCODE -ne 0) { throw "ISCC 빌드 실패 (exit $LASTEXITCODE)" }
    $setup = Join-Path $PSScriptRoot 'installer_out\STT_KOR-v0.0.0-local-Setup.exe'
}

Write-Host ""
Write-Host "완료:"
Write-Host "  실행 파일 : $(Join-Path $PSScriptRoot 'dist\STT_KOR\STT_KOR.exe')"
Write-Host "  배포 zip  : $(Join-Path $PSScriptRoot $zip)"
if ($setup) {
    Write-Host "  설치 파일 : $setup"
} else {
    Write-Host "  설치 파일 : (건너뜀 - Inno Setup 미설치. https://jrsoftware.org/isdl.php )"
}
