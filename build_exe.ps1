# STT_KOR - PyInstaller 빌드 스크립트
#
# 사용법:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# 결과물: dist\STT_KOR.exe  (CPU 전용, 단일 파일)
# GPU(CUDA) 라이브러리는 exe에 넣지 않고, 실행 중 필요할 때
# stt_app.py가 %LOCALAPPDATA%로 내려받습니다.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1) 빌드 도구 및 런타임 의존성 설치
python -m pip install --upgrade pip
python -m pip install --upgrade pyinstaller faster-whisper

# 2) 이전 산출물 정리
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force STT_KOR.spec -ErrorAction SilentlyContinue

# 3) 빌드
#   --collect-all: faster-whisper와 그 바이너리 의존성(모델 로더/오디오 디코더)
#                  의 데이터 파일과 DLL을 빠짐없이 포함
python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name STT_KOR `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-all onnxruntime `
    stt_app.py

Write-Host ""
Write-Host "완료: $(Join-Path $PSScriptRoot 'dist\STT_KOR.exe')"
