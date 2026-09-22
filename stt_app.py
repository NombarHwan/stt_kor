"""Transcribe to Learn (TTL) - 강의 녹음을 받아쓰는 데스크톱 앱.

표시 이름은 "Transcribe to Learn" 이지만, 설치 폴더·설정 경로(APP_DIR_NAME)·
exe 이름·설치 마법사 AppId 는 STT_KOR 그대로 둔다. 바꾸면 기존 사용자가
업그레이드가 아니라 별도 앱으로 설치되고, 기억해 둔 폴더 설정과 이미 받아둔
CUDA 라이브러리(약 1.3GB)를 잃는다.

Python이나 pip 없이도 실행할 수 있도록 PyInstaller로 exe 패키징하는 것을
전제로 만들어졌습니다 (build_exe.ps1 참고). exe 자체는 CPU 전용으로 작게
빌드되고, 실행 중 NVIDIA GPU가 감지되면 사용자 동의를 받아 CUDA 가속
라이브러리(cublas/cudnn/nvrtc, 약 1.3GB)를 PyPI에서 내려받아
%LOCALAPPDATA%에 캐싱합니다(최초 1회). AMD GPU나 내장 그래픽, GPU가 없는
환경에서는 다운로드 없이 바로 CPU로 동작합니다.

앱 시작 시 RAM/CPU 코어/GPU(VRAM)를 감지해 적정 모델을 자동으로 골라
주고(recommend_model), 사용자가 모델을 바꾸면 예상 처리 시간·품질·메모리
경고 등 주의사항을 화면에 표시합니다(model_notices).

인식 언어는 한국어/영어/자동 감지 중에서 고릅니다(LANGUAGES). 영어로 "고정"한
경우에는 같은 등급의 영어 전용 모델로 자동으로 바꿔 씁니다(ENGLISH_MODEL_REPOS).
영어 전용 모델은 영어에 더 정확한 대신 한국어를 만나면 결과가 망가지므로 자동
감지에서는 쓰지 않습니다. 언어에 따라 받는 모델 파일이 달라지므로, 영어를 처음
고르면 추가 다운로드가 생길 수 있습니다.

마지막으로 쓴 오디오 폴더와 출력 폴더, 인식 언어는 %LOCALAPPDATA%/STT_KOR/
settings.json에 기억해 두고, 다음 실행 때 복원합니다.

실행하면 GitHub 릴리스를 확인해(하루 몇 번으로 제한) 새 버전이 있으면 패치노트와
함께 알려주고, 설치 파일을 내려받아 설치 마법사를 띄웁니다. APP_VERSION 줄은
릴리스 워크플로가 태그 버전으로 덮어씁니다.

인자를 주고 실행하면 창 없이 터미널에서 바로 동작하는 개발자용 CLI가 됩니다
(run_cli 참고). 예: python stt_app.py 0910 -> 녹음 폴더에서 이름에 "0910"이
들어간 오디오를 모두 찾아 변환. 설정·모델 캐시·CUDA 라이브러리는 GUI와 같은
것을 쓰므로 둘을 섞어 써도 됩니다.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from typing import Callable

# HuggingFace 캐시의 심볼릭 링크를 끈다. faster_whisper(=huggingface_hub)를
# import 하기 전에 걸어야 한다 - huggingface_hub.constants 가 import 시점에
# 환경변수를 한 번만 읽기 때문이다.
#
# 이유: 관리자 권한도 개발자 모드도 없는 Windows(= 우리 사용자 대부분, 설치
# 마법사도 PrivilegesRequired=lowest 로 돈다)에서 모델을 "처음" 받을 때
# huggingface_hub 가 크래시한다.
#   1. are_symlinks_supported() 는 지원 여부를 실제로 검사하기 "전에" 캐시
#      딕셔너리에 True 를 먼저 써 넣는다.
#   2. 모델 파일은 여러 스레드가 동시에 받는다. 검사가 끝나기 전에 들어온
#      스레드는 그 True 를 읽고 os.symlink() 를 호출한다.
#   3. 그 os.symlink() 는 WinError 1314 로 실패하는데, 이게 PermissionError 가
#      아니라 그냥 OSError 라서 _create_symlink() 의 except PermissionError
#      방어를 그대로 통과한다. -> 변환이 통째로 죽는다.
# 이 변수를 켜면 검사 자체를 건너뛰고 항상 복사본을 쓰므로 경쟁 자체가 없다.
# (디스크를 조금 더 쓰지만, 우리는 어차피 스냅샷 하나만 보관한다.)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
# 위를 켜면 huggingface_hub 가 "심볼릭 링크를 못 씁니다" 경고를 띄우는데,
# 일반 사용자에게는 겁만 주는 문구라 끈다.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

APP_DIR_NAME = "STT_KOR"

# 릴리스 워크플로(.github/workflows/release.yml)가 태그 버전으로 이 줄을 덮어쓴다.
# 형식을 바꾸면 워크플로의 "Stamp version" 단계도 함께 고쳐야 한다.
APP_VERSION = "1.1.0"

GITHUB_REPO = "NombarHwan/stt_kor"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# 릴리스 본문에서 패치노트를 잘라낼 때 기준이 되는 제목(워크플로가 넣는다).
PATCH_NOTES_HEADING = "## 이번 업데이트"
# 자동 업데이트 확인 최소 간격(초). 실행할 때마다 GitHub를 두드리지 않도록.
UPDATE_CHECK_INTERVAL = 6 * 3600

# ctranslate2가 필요로 하는 CUDA 런타임 구성 요소. 버전은 현재 개발 환경에
# 설치된 nvidia-*-cu12 wheel과 맞춰뒀습니다. ctranslate2를 업그레이드하면
# 여기 버전도 함께 맞춰야 할 수 있습니다.
CUDA_PACKAGES = [
    ("nvidia-cublas-cu12", "12.9.2.10"),
    ("nvidia-cudnn-cu12", "9.25.1.1"),
    ("nvidia-cuda-nvrtc-cu12", "12.9.86"),
]

MODEL_SIZES = ["large-v3", "turbo", "medium", "small", "base", "tiny"]
# 하드웨어 감지 전까지 쓰는 잠정 기본값. 감지가 끝나면 recommend_model()이
# 사용자가 아직 콤보박스를 건드리지 않은 경우에 한해 권장값으로 바꾼다.
DEFAULT_MODEL_SIZE = "medium"

# 콤보박스에 보이는 모델 키 -> 실제로 내려받는 CTranslate2 저장소.
#
# faster-whisper 에 내장된 짧은 이름("large-v3" 등)을 그대로 넘겨도 동작하지만,
# 저장소를 적어 두면 (1) 캐시 확인(_model_cached)을 정확히 할 수 있고
# (2) 업스트림이 별칭을 바꿔도 받는 모델이 흔들리지 않는다. 실제로 1.2.1 의
# turbo 별칭이 가리키는 mobiuslabsgmbh 저장소는 소유자가 바뀌어 지금은
# HuggingFace 리다이렉트로만 접근되므로, turbo 는 저장소를 직접 지정한다.
MODEL_REPOS = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
    "tiny": "Systran/faster-whisper-tiny",
}

# 인식 언어를 영어로 "고정"했을 때만 대신 쓰는 영어 전용 모델.
#
# 같은 크기에서 영어 전용 모델이 다국어 모델보다 영어에 정확하고, 이득은
# base 쪽에서 가장 크다(크기가 커질수록 차이는 줄어든다). medium 자리에는
# distil 판을 쓴다 - 디코더가 24층에서 2층으로 줄어 용량은 절반인데 영어 품질은
# medium 급이라, GPU 없는 환경에서 체감이 가장 크다.
#
# large-v3 / turbo 에는 영어 전용판이 없다(large 에는 애초에 .en 이 없다).
# 그 등급에서는 다국어 모델이 영어에서도 최고 성능이라 바꿀 이유도 없다.
#
# 주의: 영어 전용 모델은 한국어를 만나면 그 구간을 통째로 망친다. 그래서 언어가
# "en" 으로 확정됐을 때만 쓰고, 자동 감지(None)에서는 절대 쓰지 않는다.
ENGLISH_MODEL_REPOS = {
    "medium": "Systran/faster-distil-whisper-medium.en",
    "small": "Systran/faster-whisper-small.en",
    "base": "Systran/faster-whisper-base.en",
    "tiny": "Systran/faster-whisper-tiny.en",
}

# 저장소별 최초 1회 다운로드 용량.
DOWNLOAD_SIZE = {
    "Systran/faster-whisper-large-v3": "약 3.1 GB",
    "deepdml/faster-whisper-large-v3-turbo-ct2": "약 1.6 GB",
    "Systran/faster-whisper-medium": "약 1.5 GB",
    "Systran/faster-whisper-small": "약 480 MB",
    "Systran/faster-whisper-base": "약 145 MB",
    "Systran/faster-whisper-tiny": "약 75 MB",
    "Systran/faster-distil-whisper-medium.en": "약 790 MB",
    "Systran/faster-whisper-small.en": "약 480 MB",
    "Systran/faster-whisper-base.en": "약 145 MB",
    "Systran/faster-whisper-tiny.en": "약 75 MB",
}

# 모델별 한국어 강의 기준 품질 설명.
MODEL_INFO = {
    "tiny": {"quality": "매우 낮음 — 키워드 수준, 받아쓰기 부적합"},
    "base": {"quality": "낮음 — 대략적인 내용 파악용"},
    "small": {"quality": "보통 — 깨끗한 녹음이면 요지 파악, 교정 많이 필요"},
    "medium": {"quality": "좋음 — 일반 강의는 신뢰할 만함, 가벼운 교정"},
    "turbo": {"quality": "최상 — large-v3에 준하는 품질을 몇 배 빠르게"},
    "large-v3": {"quality": "최상 — 전문용어·숫자에 강함, 거의 교정 불필요"},
}

# 1시간 분량 오디오 기준 예상 처리 시간. backend: gpu / cpu_strong / cpu_weak.
#
# turbo 는 large-v3 와 인코더(32층)가 같고 디코더만 32층 -> 4층으로 줄어든
# 모델이다. 그래서 디코딩이 지배적인 GPU 에서 이득이 가장 크고, 인코더 비중이
# 커지는 CPU 에서는 배수가 그만큼 나오지 않는다.
EST_TIME = {
    "tiny": {"gpu": "1분 내외", "cpu_strong": "3~6분", "cpu_weak": "10~15분"},
    "base": {"gpu": "1~2분", "cpu_strong": "5~10분", "cpu_weak": "15~25분"},
    "small": {"gpu": "2~4분", "cpu_strong": "15~30분", "cpu_weak": "40~70분"},
    "medium": {"gpu": "3~6분", "cpu_strong": "45~90분", "cpu_weak": "2~3시간"},
    "turbo": {"gpu": "2~5분", "cpu_strong": "1~2시간", "cpu_weak": "4시간 이상"},
    "large-v3": {"gpu": "5~12분", "cpu_strong": "2~4시간", "cpu_weak": "5시간 이상"},
}


def uses_english_only_model(model: str, language: str | None) -> bool:
    """영어 전용 모델로 바꿔 쓰는 조합인지. 자동 감지(None)에서는 항상 False."""
    return language == "en" and model in ENGLISH_MODEL_REPOS


def resolve_model_repo(model: str, language: str | None) -> str:
    """콤보박스 선택값 + 인식 언어 -> 실제로 로드할 저장소 이름."""
    if uses_english_only_model(model, language):
        return ENGLISH_MODEL_REPOS[model]
    return MODEL_REPOS[model]

# 인식 언어. Whisper 다국어 모델(tiny~large-v3)은 약 100개 언어를 지원한다.
# (표시 이름, Whisper 언어 코드). 코드가 None 이면 오디오 앞부분으로 자동 감지.
# 설정 파일에는 코드(자동 감지는 "auto")로 저장한다.
LANGUAGES = [
    ("한국어", "ko"),
    ("English", "en"),
    ("자동 감지", None),
]
DEFAULT_LANGUAGE = "ko"
# 자동 감지 시 앞에서부터 최대 몇 개의 30초 구간을 볼지. 강의 녹음은 수업 전
# 잡음·정적으로 시작하는 경우가 많아 첫 30초만 보면 틀리기 쉽다. 확신(0.5 초과)이
# 서는 구간에서 바로 멈추므로 대부분은 첫 구간만 보고 끝난다.
LANGUAGE_DETECTION_SEGMENTS = 4

AUDIO_FILETYPES = [
    ("오디오 파일", "*.mp3 *.wav *.m4a *.mp4 *.aac *.flac *.ogg *.wma"),
    ("모든 파일", "*.*"),
]


# ---- 환각(자막 크레딧) 걸러내기 ------------------------------------------

# Whisper는 학습 데이터의 유튜브·방송 자막을 통째로 외웠고, 그 자막이 붙어 있던
# 구간은 대개 말소리가 없는 구간(엔딩 음악, 정적)이다. 그래서 강의 녹음의 조용한
# 구간에서 실제 오디오와 무관한 "자막 크레딧" 문장을 뱉는다. vad_filter로 대부분
# 막히지만, 남는 것들을 아래 패턴으로 마지막에 한 번 더 걸러낸다.
#
# 강의 내용을 실수로 지우지 않도록, 짧은 줄(HALLUCINATION_MAX_LEN 이하)이면서
# 패턴에 걸릴 때만 버린다.
HALLUCINATION_MAX_LEN = 40
HALLUCINATION_PATTERNS = [
    re.compile(r"자막\s*(제공|제작|by|By|BY)"),
    re.compile(r"(한글|영어)?\s*자막\s*[:：]"),
    re.compile(r"시청\s*(해|해\s)?\s*주(셔서|셔|시고)?\s*감사"),
    re.compile(r"구독\s*(과|,|하고|과\s)?\s*좋아요"),
    re.compile(r"좋아요\s*(와|과|,)?\s*구독"),
    # "다음 시간에 뵙겠습니다" 는 강의에서 실제로 쓰는 말이라 뺀다. 유튜브 특유의
    # "다음 영상에서 만나요" 만 잡는다.
    re.compile(r"다음\s*영상에\s*(서\s*)?(만나|뵙)"),
    re.compile(r"(MBC|KBS|SBS|YTN|JTBC)\s*뉴스"),
    re.compile(r"[Ss]ubtitles?\s+by"),
    re.compile(r"[Aa]mara\.org"),
    re.compile(r"[Tt]hanks?\s+for\s+watching"),
    re.compile(r"[Tt]hank\s+you\s+(so\s+much\s+|very\s+much\s+)?for\s+watching"),
    re.compile(r"[Ll]ike\s+and\s+subscribe"),
    re.compile(r"[Ss]ubscribe\s+to\s+(my|our|the)\s+channel"),
    # "see you next time" 은 강의에서 실제로 쓰는 말이라 뺀다.
    re.compile(r"[Ss]ee\s+you\s+in\s+the\s+next\s+video"),
]


def is_hallucination(text: str) -> bool:
    """무음 구간에서 나오는 자막 크레딧 문구로 보이면 True."""
    stripped = text.strip()
    if not stripped or len(stripped) > HALLUCINATION_MAX_LEN:
        return False
    return any(pattern.search(stripped) for pattern in HALLUCINATION_PATTERNS)


# ---- 함께 묶여 나가는 리소스 ---------------------------------------------


def resource_path(*parts: str) -> str:
    """아이콘처럼 exe 에 함께 묶여 나가는 파일의 경로.

    PyInstaller 로 묶이면 실행할 때 임시 폴더에 풀리고 그 위치가 sys._MEIPASS
    에 들어온다. 소스로 실행할 때는 이 파일 옆을 본다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def running_as_msix() -> bool:
    """MSIX(마이크로소프트 스토어) 패키지로 실행 중인지.

    스토어로 배포한 판에서는 앱이 스스로 업데이트하면 안 된다.
    - 설치 위치(WindowsApps)가 읽기 전용이라 덮어쓸 수 없다.
    - 설치 마법사를 돌려 봐야 업그레이드가 아니라 두 번째 사본이 깔린다.
    - 업데이트는 스토어가 한다.
    GitHub 에서 받은 설치본은 패키지가 아니므로 예전처럼 자체 업데이트한다.

    GetCurrentPackageFullName 은 패키지가 아니면 APPMODEL_ERROR_NO_PACKAGE
    (15700) 를 돌려준다."""
    if os.name != "nt":
        return False
    try:
        length = ctypes.c_uint32(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(
            ctypes.byref(length), None
        )
        return rc != 15700
    except Exception:
        return False


def set_taskbar_identity() -> None:
    """작업 표시줄이 이 앱을 python.exe 가 아니라 우리 앱으로 보게 한다.

    이걸 안 해 주면 소스로 실행했을 때 작업 표시줄에 파이썬 아이콘이 뜬다.
    창을 만들기 전에 불러야 한다. Windows 전용이라 실패는 무시한다."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "NombarHwan.TranscribeToLearn"
        )
    except Exception:
        pass


# ---- 사용자 설정 저장 (마지막 폴더 기억) ---------------------------------


def app_data_dir() -> str:
    """설정/CUDA 캐시/업데이트 파일을 두는 곳. 설치 위치와 무관하게 유지된다."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def settings_path() -> str:
    return os.path.join(app_data_dir(), "settings.json")


def load_settings() -> dict:
    """저장된 설정을 읽는다. 파일이 없거나 깨졌으면 빈 dict."""
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    """설정을 저장한다. 실패해도 앱 동작에는 영향이 없으므로 조용히 넘어간다."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _valid_dir(path: object) -> str | None:
    """설정에서 읽은 폴더 경로가 아직 살아있을 때만 돌려준다(USB 분리 등 대비)."""
    if isinstance(path, str) and path and os.path.isdir(path):
        return path
    return None


# ---- 업데이트 확인 (GitHub 릴리스) ---------------------------------------


@dataclass
class Release:
    version: str          # "1.0.5"
    notes: str            # 짧은 패치노트
    setup_url: str | None  # Setup.exe 직접 다운로드 주소 (없으면 웹페이지로 안내)
    setup_name: str
    setup_size: int
    page_url: str


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.0.5' -> (1, 0, 5). 숫자로 못 읽는 부분에서 멈춘다."""
    parts: list[int] = []
    for chunk in (text or "").strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return bool(new) and new > old


def extract_patch_notes(body: str) -> str:
    """릴리스 본문에서 PATCH_NOTES_HEADING 섹션만 뽑는다. 없으면 본문 앞부분."""
    lines = (body or "").splitlines()
    picked: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = line.strip() == PATCH_NOTES_HEADING
            continue
        if capturing:
            picked.append(line)
    text = "\n".join(picked).strip()
    if not text:
        text = "\n".join(lines).strip()
    return "\n".join(text.splitlines()[:40]).strip()


def fetch_latest_release() -> Release | None:
    """GitHub의 최신 정식 릴리스 정보. 태그를 못 읽으면 None (draft/prerelease 제외)."""
    req = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={"User-Agent": "stt-kor-app", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    version = ".".join(str(n) for n in parse_version(data.get("tag_name") or ""))
    if not version:
        return None

    setup_url, setup_name, setup_size = None, "", 0
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith("setup.exe"):
            setup_url = asset.get("browser_download_url")
            setup_name = name
            setup_size = int(asset.get("size") or 0)
            break

    return Release(
        version=version,
        notes=extract_patch_notes(data.get("body") or ""),
        setup_url=setup_url,
        setup_name=setup_name,
        setup_size=setup_size,
        page_url=data.get("html_url") or RELEASES_PAGE_URL,
    )


def installer_launch_command(setup_path: str, pid: int, app_dir: str | None) -> list[str]:
    """pid 프로세스(이 프로그램)가 완전히 끝난 뒤 설치 파일을 실행하는 명령.

    - 고정 시간만 기다리면, 종료가 늦을 때(모델·GPU 메모리 해제 등) STT_KOR.exe가
      아직 잠겨 있어 설치 마법사가 exe를 교체하지 못한다. 그래서 종료를 직접 기다린다.
    - 설치 마법사는 기본적으로 처음 설치했던 폴더(%LOCALAPPDATA%\\Programs\\STT_KOR)에
      깐다. zip을 풀어 쓰거나 폴더를 옮긴 사용자는 새 버전이 엉뚱한 곳에 깔리고
      평소 쓰던 바로가기는 옛 버전을 계속 띄우므로, app_dir(지금 실행 중인 폴더)를
      /DIR로 넘겨 그 자리에 덮어쓴다.
    한글·공백·작은따옴표가 든 경로도 안전하도록 -EncodedCommand로 넘긴다.
    """

    def ps_quote(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    script = (
        f"Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue; "
        f"Start-Process -FilePath {ps_quote(setup_path)}"
    )
    if app_dir:
        script += " -ArgumentList " + ps_quote('/DIR="' + app_dir.rstrip("\\/") + '"')
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
        "-EncodedCommand", encoded,
    ]


# ---- GPU 감지 및 CUDA 라이브러리 온디맨드 다운로드 ------------------------


def cuda_libs_dir() -> str:
    return os.path.join(app_data_dir(), "cuda_libs")


def cuda_libs_ready() -> bool:
    return os.path.isfile(os.path.join(cuda_libs_dir(), ".complete"))


def detect_nvidia_gpu() -> bool:
    """PowerShell로 NVIDIA GPU 존재 여부를 확인한다. 실패하면 False."""
    if os.name != "nt":
        return False
    try:
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except AttributeError:
        creationflags = 0
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController).Name",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
        )
        return "nvidia" in result.stdout.lower()
    except Exception:
        return False


def _pypi_wheel_url(pkg_name: str, version: str) -> tuple[str, int]:
    api_url = f"https://pypi.org/pypi/{pkg_name}/{version}/json"
    req = urllib.request.Request(api_url, headers={"User-Agent": "stt-kor-app"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    for entry in data.get("urls", []):
        if entry["filename"].endswith("win_amd64.whl"):
            return entry["url"], entry.get("size", 0)
    raise RuntimeError(f"{pkg_name} {version}용 win_amd64 wheel을 찾을 수 없습니다.")


def _extract_nvidia_dlls(wheel_path: str, dest_dir: str) -> None:
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("nvidia/") and name.lower().endswith(".dll"):
                target = os.path.join(dest_dir, *name.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _register_cuda_dlls() -> None:
    """WhisperModel 생성 직전에 호출. 다운로드된(또는 pip로 설치된) CUDA
    DLL 디렉터리를 프로세스의 DLL 검색 경로에 등록하고, ctranslate2가 나중에
    찾다가 실패하는 일이 없도록 cublas 계열을 미리 로드해둔다. NVIDIA
    라이브러리가 어디에도 없으면 아무 것도 하지 않고 조용히 반환한다(CPU
    사용).
    """
    if os.name != "nt":
        return

    bin_dirs = []
    cached = cuda_libs_dir()
    for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
        d = os.path.join(cached, *sub.split("/"))
        if os.path.isdir(d):
            bin_dirs.append(d)

    if not bin_dirs:
        # 개발 환경(pip install nvidia-*)에서 소스로 바로 실행하는 경우의
        # 대체 경로.
        #
        # 주의: 이 import 는 try 안에 있어도 PyInstaller 의 정적 분석에는 잡힌다.
        # 개발 PC 처럼 nvidia-*-cu12 가 깔려 있으면 2GB 가 통째로 exe 에 들어간다
        # (릴리스 러너에는 없어서 CI 빌드만 작았다). 그래서 빌드 스크립트에서
        # --exclude-module nvidia 로 뺀다. 묶인 앱은 어차피 %LOCALAPPDATA% 에
        # 내려받은 DLL 을 쓰므로 이 경로가 필요 없다.
        try:
            import nvidia.cublas
            import nvidia.cuda_nvrtc
            import nvidia.cudnn
        except ImportError:
            return
        for pkg in (nvidia.cublas, nvidia.cuda_nvrtc, nvidia.cudnn):
            for base in pkg.__path__:
                bin_dir = os.path.join(base, "bin")
                if os.path.isdir(bin_dir):
                    bin_dirs.append(bin_dir)

    for bin_dir in bin_dirs:
        os.add_dll_directory(bin_dir)

    for bin_dir in bin_dirs:
        for dll_name in ("cublas64_12.dll", "cublasLt64_12.dll"):
            dll_path = os.path.join(bin_dir, dll_name)
            if os.path.isfile(dll_path):
                try:
                    ctypes.WinDLL(dll_path)
                except OSError:
                    pass


# ---- 하드웨어 감지 및 모델 추천 -----------------------------------------


@dataclass
class Hardware:
    ram_gb: float | None
    cpu_name: str
    cpu_cores: int | None   # 물리 코어
    cpu_threads: int        # 논리 프로세서
    has_nvidia: bool
    gpu_name: str
    vram_gb: float | None


def _total_ram_gb() -> float | None:
    try:
        if os.name == "nt":

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / (1024 ** 3)
        else:
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
    except Exception:
        return None
    return None


def _powershell_hw() -> tuple[str | None, int | None, list[str]]:
    """(CPU 이름, 물리 코어 수, GPU 이름 목록). 실패 시 (None, None, [])."""
    if os.name != "nt":
        return None, None, []
    try:
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except AttributeError:
        creationflags = 0
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$vc=Get-CimInstance Win32_VideoController;"
        "$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1;"
        "[pscustomobject]@{gpus=@($vc.Name);cpu=$cpu.Name;"
        "cores=[int]$cpu.NumberOfCores}|ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
        )
        data = json.loads(result.stdout.strip() or "{}")
    except Exception:
        return None, None, []
    gpus = data.get("gpus") or []
    if isinstance(gpus, str):
        gpus = [gpus]
    gpus = [g for g in gpus if g]
    try:
        cores = int(data.get("cores")) if data.get("cores") else None
    except (TypeError, ValueError):
        cores = None
    return (data.get("cpu") or None), cores, gpus


def _nvidia_vram_gb() -> float | None:
    try:
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except AttributeError:
        creationflags = 0
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creationflags,
        )
    except Exception:
        return None
    vals = [int(t) for t in result.stdout.replace(",", " ").split() if t.strip().isdigit()]
    return max(vals) / 1024 if vals else None


def detect_hardware() -> Hardware:
    ram = _total_ram_gb()
    threads = os.cpu_count() or 2
    cpu_name, cores, gpu_names = _powershell_hw()
    has_nvidia = any("nvidia" in g.lower() for g in gpu_names)
    gpu_name = next(
        (g for g in gpu_names if "nvidia" in g.lower()),
        gpu_names[0] if gpu_names else "",
    )
    vram = _nvidia_vram_gb() if has_nvidia else None
    return Hardware(ram, cpu_name or "", cores, threads, has_nvidia, gpu_name, vram)


def recommend_model(
    hw: Hardware | None, language: str | None = DEFAULT_LANGUAGE
) -> tuple[str, str]:
    """(권장 모델, 이유 문구).

    언어를 함께 받는 이유: 영어로 고정하면 medium 이하가 영어 전용 모델로 바뀌어
    같은 등급에서도 더 정확해지므로, 저사양 구간의 권장이 달라진다."""
    if hw is None:
        return "medium", "하드웨어를 확인하지 못해 안전한 기본값(medium)을 사용합니다."
    if hw.has_nvidia:
        # turbo 는 large-v3 와 품질이 사실상 같으면서 파일도 VRAM 도 절반 수준이라,
        # 예전에 medium 으로 내려보내던 중급 GPU 까지 최고 품질로 끌어올릴 수 있다.
        if hw.vram_gb is None or hw.vram_gb >= 3.5:
            return (
                "turbo",
                f"NVIDIA GPU 감지 ({hw.gpu_name or '모델명 미상'}) — "
                "최고 품질(turbo)을 빠르게 쓸 수 있습니다.",
            )
        return "small", f"NVIDIA GPU VRAM 약 {hw.vram_gb:.0f}GB — small을 권장합니다."
    ram = hw.ram_gb or 8.0
    cores = hw.cpu_cores or hw.cpu_threads or 2
    if ram >= 15 and cores >= 6:
        return (
            "medium",
            f"GPU 없음 · RAM {ram:.0f}GB · {cores}코어 — 속도와 품질의 균형점으로 medium을 권장합니다.",
        )
    if ram >= 7 and cores >= 4:
        return (
            "small",
            f"GPU 없음 · RAM {ram:.0f}GB · {cores}코어 — small을 권장합니다 "
            "(medium은 1시간 강의에 1시간 이상 걸립니다).",
        )
    if language == "en":
        return (
            "base",
            f"GPU 없음 · 저사양 (RAM {ram:.0f}GB) — base를 권장합니다 "
            "(영어 전용 모델로 실행되어 같은 등급의 한국어보다는 정확합니다).",
        )
    return (
        "base",
        f"GPU 없음 · 저사양 (RAM {ram:.0f}GB) — base를 권장합니다 "
        "(정확도는 낮고 요지 확인용입니다).",
    )


def _model_backend(hw: Hardware | None) -> str:
    if hw and hw.has_nvidia:
        return "gpu"
    cores = (hw.cpu_cores or hw.cpu_threads) if hw else (os.cpu_count() or 2)
    ram = (hw.ram_gb if hw else None) or 8.0
    return "cpu_strong" if (cores and cores >= 6 and ram >= 15) else "cpu_weak"


def _model_cached(repo: str) -> bool:
    """HuggingFace 캐시에 이 저장소가 이미 받아져 있는지 확인.

    저장소 이름(owner/name)을 그대로 캐시 폴더명으로 바꿔 보므로, .en 이나
    distil 처럼 이름 규칙이 다른 모델도 정확히 잡힌다."""
    folder = "models--" + repo.replace("/", "--")
    bases = []
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            bases.append(os.environ[var])
    if os.environ.get("HF_HOME"):
        bases.append(os.path.join(os.environ["HF_HOME"], "hub"))
    bases.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    for b in bases:
        try:
            # 받다 만 저장소도 폴더는 남으므로 가중치 파일까지 있는지 본다.
            if glob.glob(os.path.join(b, folder, "snapshots", "*", "model.bin")):
                return True
        except Exception:
            pass
    return False


def language_code(label: str) -> str | None:
    """콤보박스 표시 이름 -> Whisper 언어 코드 (자동 감지는 None)."""
    for name, code in LANGUAGES:
        if name == label:
            return code
    return DEFAULT_LANGUAGE


def language_label(code: object) -> str:
    """설정에 저장된 코드("ko"/"en"/"auto") -> 콤보박스 표시 이름.
    저장된 값이 없거나(None) 모르는 값이면 기본 언어."""
    for name, c in LANGUAGES:
        if (c or "auto") == code:
            return name
    return next(name for name, c in LANGUAGES if c == DEFAULT_LANGUAGE)


def model_notices(
    model: str, hw: Hardware | None, language: str | None = DEFAULT_LANGUAGE
) -> list[tuple[str, str]]:
    """선택한 모델·언어에 대한 (수준, 문구) 목록. 수준은 'warn' 또는 'info'."""
    backend = _model_backend(hw)
    repo = resolve_model_repo(model, language)
    english_only = uses_english_only_model(model, language)
    est = EST_TIME[model][backend]
    where = "GPU 사용 시" if backend == "gpu" else "CPU 사용"
    quality = MODEL_INFO[model]["quality"]
    if language != "ko":
        quality += " (한국어 기준)"
    out: list[tuple[str, str]] = [
        ("info", f"1시간 분량 기준 예상 처리 시간: 약 {est} ({where})"),
        ("info", f"품질: {quality}"),
    ]
    if language == "en":
        out.append(
            (
                "info",
                "영어는 Whisper가 가장 많이 학습한 언어라 같은 모델에서 대체로 "
                "한국어보다 정확합니다.",
            )
        )
    elif language is None:
        out.append(
            (
                "info",
                "자동 감지는 녹음 앞부분으로 언어를 판단합니다. 강의 언어를 알고 있다면 "
                "직접 고르는 편이 정확합니다.",
            )
        )
    if english_only:
        out.append(
            (
                "info",
                f"영어 전용 모델({repo.rsplit('/', 1)[-1]})로 실행됩니다. 같은 크기의 "
                "다국어 모델보다 영어에 정확하지만, 강의 중 한국어가 섞이면 그 구간은 "
                "잘못 인식됩니다. 한국어가 섞이는 수업이면 turbo나 large-v3를 쓰세요.",
            )
        )
        if model == "medium":
            out.append(
                ("info", "이 조합은 디코더를 줄인 distil 판이라 위 예상 시간보다 빠릅니다.")
            )
    if not _model_cached(repo):
        out.append(
            ("info", f"이 모델을 처음 쓰면 최초 1회 {DOWNLOAD_SIZE[repo]}를 내려받습니다.")
        )

    ram = hw.ram_gb if hw else None
    if model in ("large-v3", "turbo"):
        if model == "large-v3" and backend != "gpu":
            out.append(
                (
                    "warn",
                    "GPU를 쓰지 않아 처리 시간이 매우 깁니다. 같은 품질에 훨씬 빠른 "
                    "turbo나, medium 이하를 권장합니다.",
                )
            )
        elif model == "turbo" and backend != "gpu":
            out.append(
                (
                    "warn",
                    "turbo는 large-v3와 인코더가 같아 GPU 없이는 1시간 분량에 1시간 "
                    "이상 걸립니다. CPU만 쓴다면 medium 이하를 권장합니다.",
                )
            )
        ram_need = 16 if model == "large-v3" else 8
        if ram is not None and ram < ram_need * 0.75:
            out.append(
                (
                    "warn",
                    f"{model}는 RAM {ram_need}GB를 권장합니다. 현재 약 {ram:.0f}GB로 "
                    "변환 중 프로그램이 종료될 수 있습니다.",
                )
            )
        if hw and hw.has_nvidia and not cuda_libs_ready():
            out.append(
                ("info", "GPU 가속을 위해 최초 1회 CUDA 라이브러리(약 1.3GB)를 내려받습니다.")
            )
    elif model == "medium":
        if backend == "cpu_weak":
            out.append(
                ("warn", "이 PC 사양에서는 1시간 분량 처리에 1시간 이상 걸릴 수 있습니다.")
            )
        if ram is not None and ram < 8:
            out.append(
                ("warn", f"medium은 RAM 8GB 이상을 권장합니다. 현재 약 {ram:.0f}GB.")
            )
    else:
        out.append(
            (
                "info",
                "정확도가 낮아 고유명사·숫자·전문용어에서 오류가 잦습니다. "
                "녹취록으로 쓰려면 교정이 필요합니다.",
            )
        )
        if model in ("base", "tiny"):
            out.append(
                ("warn", "이 모델은 대략적인 내용 파악용입니다. 정확한 받아쓰기에는 적합하지 않습니다.")
            )
    return out


# ---- 변환 코어 (GUI와 CLI가 함께 쓴다) -----------------------------------
#
# 아래 함수들은 tkinter에 의존하지 않는다. 로그를 어디에 뿌릴지만 log 콜백으로
# 갈아 끼우면 GUI(로그 창)와 CLI(stdout)가 완전히 같은 변환 코드를 쓴다.


def output_path_for(audio_path: str, output_dir: str | None) -> str:
    base = os.path.splitext(os.path.basename(audio_path))[0]
    out_dir = output_dir or os.path.dirname(audio_path)
    return os.path.join(out_dir, base + "_transcript.txt")


def download_cuda_libs(log: Callable[[str], None]) -> None:
    libs_dir = cuda_libs_dir()
    tmp_dir = os.path.join(libs_dir, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        for pkg_name, version in CUDA_PACKAGES:
            log(f"GPU 라이브러리 조회 중: {pkg_name} {version}")
            url, size = _pypi_wheel_url(pkg_name, version)
            wheel_path = os.path.join(tmp_dir, pkg_name + ".whl")
            _download_file(url, wheel_path, pkg_name, size, log)
            log(f"압축 해제 중: {pkg_name}")
            _extract_nvidia_dlls(wheel_path, libs_dir)
            os.remove(wheel_path)
        with open(os.path.join(libs_dir, ".complete"), "w", encoding="utf-8") as f:
            f.write("ok")
        log("GPU 가속 라이브러리 준비 완료.")
    except Exception:
        log(
            "GPU 라이브러리 다운로드에 실패했습니다. CPU로 진행합니다.\n"
            + traceback.format_exc()
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _download_file(
    url: str, dest_path: str, label: str, total_hint: int, log: Callable[[str], None]
) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "stt-kor-app"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or total_hint or 0)
        downloaded = 0
        last_pct = -1
        with open(dest_path, "wb") as out:
            while True:
                buf = resp.read(1024 * 1024)
                if not buf:
                    break
                out.write(buf)
                downloaded += len(buf)
                if total:
                    pct = int(downloaded * 100 / total)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        mb = downloaded // (1024 * 1024)
                        total_mb = total // (1024 * 1024)
                        log(f"  {label}: {pct}% ({mb}MB / {total_mb}MB)")


def load_model(repo: str, log: Callable[[str], None]):
    """WhisperModel을 만든다. CUDA DLL 등록도 여기서 해 준다.

    repo 는 resolve_model_repo() 가 고른 HuggingFace 저장소 이름이다."""
    _register_cuda_dlls()
    from faster_whisper import WhisperModel

    log(f"모델 로딩 중... ({repo}, GPU 있으면 자동 사용, 없으면 CPU)")
    log("※ 처음 실행하는 모델이면 인터넷에서 다운로드가 필요합니다 (수백MB~수GB).")
    model = WhisperModel(repo, device="auto", compute_type="auto")
    log("모델 로딩 완료.")
    return model


def transcribe_file(
    model, audio_path: str, output_path: str, language: str | None, log: Callable[[str], None]
) -> None:
    """오디오 하나를 변환해 output_path에 저장한다."""
    segments, info = model.transcribe(
        audio_path,
        language=language,
        language_detection_segments=LANGUAGE_DETECTION_SEGMENTS,
        beam_size=5,
        condition_on_previous_text=False,
        # hallucination_silence_threshold 는 word_timestamps 가 True 일 때만
        # 동작한다 (faster_whisper/transcribe.py 에서 처리 블록이 word_timestamps
        # 분기 안에 들어 있다). 켜 두면 무음을 건너뛰어 오히려 더 빠르다.
        word_timestamps=True,
        hallucination_silence_threshold=2.0,
        # vad_filter 는 켜지 않는다. faster-whisper 1.2.1 의 Silero VAD 는
        # 앞부분에 조용한 구간이 길게 있으면 내부 LSTM 상태가 포화돼 그 뒤
        # 말소리를 전부 무음으로 판정한다(같은 구간 단독 입력 시 확률 0.47 ->
        # 앞 150초를 붙이면 0.00). 수업 시작 전부터 녹음을 켜 두는 강의
        # 파일에서 결과가 통째로 비어버리므로 쓰면 안 된다.
    )
    if language is None:
        log(f"  감지된 언어: {info.language} (확률 {info.language_probability:.2%})")

    lines = []
    dropped = 0
    previous_text = None
    for seg in segments:
        text = seg.text.strip()
        # 자막 크레딧 환각, 그리고 바로 앞줄과 완전히 똑같은 반복은 버린다.
        if is_hallucination(text) or (text and text == previous_text):
            dropped += 1
            log(f"  · 제외(환각 추정): {text}")
            continue
        previous_text = text
        timestamp = f"[{seg.start:6.1f}s → {seg.end:6.1f}s]"
        line = f"{timestamp} {text}"
        log("  " + line)
        lines.append(line)
    if dropped:
        log(f"  환각으로 보이는 {dropped}줄을 결과에서 제외했습니다.")

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"  저장 완료: {output_path}")


class SttApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Transcribe to Learn v{APP_VERSION}")
        self._apply_window_icon()
        self.root.geometry("640x600")
        self.root.minsize(520, 480)

        self.settings = load_settings()
        self.selected_files: list[str] = []
        self.input_dir: str | None = _valid_dir(self.settings.get("input_dir"))
        self.output_dir: str | None = _valid_dir(self.settings.get("output_dir"))
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.model = None
        # 언어에 따라 같은 등급도 다른 저장소를 쓰므로, 캐시 키는 저장소 이름이다.
        self.model_repo_loaded: str | None = None
        self.hw_summary = "하드웨어 정보를 읽지 못함"
        self.gpu_checked = False
        self.declined_gpu_download = False
        self.hw: Hardware | None = None
        self.model_user_touched = False
        self.is_msix = running_as_msix()

        self._build_widgets()
        self.root.after(100, self._drain_log_queue)
        threading.Thread(target=self._detect_hw_worker, daemon=True).start()
        # 창이 뜬 뒤에 대화상자를 띄우도록 한 박자 늦춘다.
        self.root.after(400, self._show_release_notes_after_update)
        self.root.after(1200, self._maybe_check_update)

    # ---- UI ---------------------------------------------------------

    def _apply_window_icon(self) -> None:
        """제목 표시줄과 작업 표시줄 아이콘. default=True 라서 대화상자에도 붙는다.

        아이콘이 없어도 앱은 멀쩡히 돌아야 하므로 실패는 조용히 넘긴다."""
        try:
            self.root.iconbitmap(default=resource_path("assets", "TTL.ico"))
        except Exception:
            pass

    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Button(top, text="오디오 파일 선택", command=self._choose_files).pack(side="left")
        ttk.Button(top, text="출력 폴더 선택", command=self._choose_output_dir).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(top, text="모델:").pack(side="left", padx=(16, 4))
        self.model_var = tk.StringVar(value=DEFAULT_MODEL_SIZE)
        model_combo = ttk.Combobox(
            top, textvariable=self.model_var, values=MODEL_SIZES, width=10, state="readonly"
        )
        model_combo.pack(side="left")
        model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)

        ttk.Label(top, text="언어:").pack(side="left", padx=(12, 4))
        self.language_var = tk.StringVar(value=language_label(self.settings.get("language")))
        language_combo = ttk.Combobox(
            top,
            textvariable=self.language_var,
            values=[name for name, _ in LANGUAGES],
            width=9,
            state="readonly",
        )
        language_combo.pack(side="left")
        language_combo.bind("<<ComboboxSelected>>", self._on_language_selected)

        self.files_label = ttk.Label(self.root, text="선택된 파일 없음", foreground="#555")
        self.files_label.pack(fill="x", padx=10)

        output_row = ttk.Frame(self.root)
        output_row.pack(fill="x", padx=10, pady=(0, 6))
        self.output_label = ttk.Label(output_row, text="", foreground="#555")
        self.output_label.pack(side="left", fill="x", expand=True)
        self.output_reset_button = ttk.Button(
            output_row, text="기억 지우기", width=10, command=self._reset_output_dir
        )
        self._refresh_output_label()

        self.hw_label = ttk.Label(
            self.root, text="하드웨어 확인 중...", foreground="#555", justify="left"
        )
        self.hw_label.pack(fill="x", padx=10, pady=(0, 2))

        self.model_note_label = ttk.Label(self.root, text="", foreground="#555", justify="left")
        self.model_note_label.pack(fill="x", padx=10, pady=(0, 6))

        action_row = ttk.Frame(self.root)
        action_row.pack(fill="x", padx=10, pady=(0, 6))
        self.start_button = ttk.Button(action_row, text="변환 시작", command=self._start)
        self.start_button.pack(side="left")
        self.progress = ttk.Progressbar(action_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        status_row = ttk.Frame(self.root)
        status_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(status_row, text=f"버전 {APP_VERSION}", foreground="#777").pack(side="left")
        self.update_button = ttk.Button(
            status_row, text="업데이트 확인", command=self._manual_update_check
        )
        # 스토어 판에서는 업데이트를 스토어가 맡으므로 이 버튼을 내보내지 않는다.
        if not self.is_msix:
            self.update_button.pack(side="right")
        else:
            ttk.Label(
                status_row, text="업데이트는 Microsoft Store에서 받습니다", foreground="#777"
            ).pack(side="right")

        self.root.bind("<Configure>", self._on_resize)
        self._refresh_model_note()

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="변환할 오디오 파일 선택",
            filetypes=AUDIO_FILETYPES,
            initialdir=self.input_dir or "",
        )
        if not paths:
            return
        self.selected_files = list(paths)
        names = ", ".join(os.path.basename(p) for p in self.selected_files)
        self.files_label.config(text=f"선택된 파일 ({len(self.selected_files)}개): {names}")
        self.input_dir = os.path.dirname(self.selected_files[0]) or self.input_dir
        self._remember(input_dir=self.input_dir)

    def _choose_output_dir(self) -> None:
        directory = filedialog.askdirectory(
            title="출력 폴더 선택", initialdir=self.output_dir or self.input_dir or ""
        )
        if not directory:
            return
        self.output_dir = directory
        self._refresh_output_label()
        self._remember(output_dir=directory)

    def _reset_output_dir(self) -> None:
        """기억해 둔 출력 폴더를 지우고 '원본과 같은 폴더' 동작으로 되돌린다."""
        self.output_dir = None
        self._refresh_output_label()
        self._remember(output_dir=None)

    def _refresh_output_label(self) -> None:
        if self.output_dir:
            self.output_label.config(text=f"출력 폴더: {self.output_dir}")
            self.output_reset_button.pack(side="left", padx=(8, 0))
        else:
            self.output_label.config(text="출력 폴더: (원본 파일과 같은 폴더)")
            self.output_reset_button.pack_forget()

    def _remember(self, **values: object) -> None:
        for key, value in values.items():
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value
        save_settings(self.settings)

    # ---- logging ------------------------------------------------------

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    # ---- 하드웨어 감지 / 모델 안내 -------------------------------------

    def _detect_hw_worker(self) -> None:
        hw = detect_hardware()
        self.root.after(0, lambda: self._on_hw_detected(hw))

    def _on_hw_detected(self, hw: Hardware) -> None:
        self.hw = hw

        bits: list[str] = []
        if hw.ram_gb:
            bits.append(f"RAM {hw.ram_gb:.0f}GB")
        cores = hw.cpu_cores or hw.cpu_threads
        if cores:
            bits.append(f"CPU {cores}코어")
        if hw.has_nvidia:
            bits.append(f"NVIDIA GPU{f' {hw.vram_gb:.0f}GB' if hw.vram_gb else ''}")
        elif hw.gpu_name:
            bits.append("GPU 가속 미지원(비 NVIDIA)")
        self.hw_summary = " · ".join(bits) if bits else "하드웨어 정보를 읽지 못함"

        self._apply_recommendation()

    def _apply_recommendation(self) -> None:
        """하드웨어와 현재 언어에 맞는 권장 모델을 라벨에 반영한다.

        언어를 바꾸면 권장 모델이 달라질 수 있어 언어 변경 때도 다시 부른다.
        다만 사용자가 콤보박스를 직접 건드린 뒤에는 그 선택을 덮어쓰지 않는다."""
        rec, reason = recommend_model(self.hw, language_code(self.language_var.get()))
        if not self.model_user_touched:
            self.model_var.set(rec)
        self.hw_label.config(text=f"감지: {self.hw_summary}\n권장 모델: {rec} — {reason}")
        self._refresh_model_note()

    def _on_model_selected(self, _event: object = None) -> None:
        self.model_user_touched = True
        self._refresh_model_note()

    def _on_language_selected(self, _event: object = None) -> None:
        code = language_code(self.language_var.get())
        self._remember(language=code or "auto")
        # 언어가 바뀌면 영어 전용 모델 교체 여부와 권장 모델이 함께 달라진다.
        if self.hw is None:
            self._refresh_model_note()
        else:
            self._apply_recommendation()

    def _refresh_model_note(self) -> None:
        notices = model_notices(
            self.model_var.get(), self.hw, language_code(self.language_var.get())
        )
        has_warn = any(level == "warn" for level, _ in notices)
        lines = [("⚠ " if level == "warn" else "· ") + text for level, text in notices]
        self.model_note_label.config(
            text="\n".join(lines), foreground="#b23b3b" if has_warn else "#555"
        )

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is self.root:
            width = max(300, event.width - 40)
            self.hw_label.config(wraplength=width)
            self.model_note_label.config(wraplength=width)

    # ---- GPU 확인/다운로드 (메인 스레드) ---------------------------------

    def _maybe_offer_gpu_download(self) -> None:
        """변환 시작 직전, 메인 스레드에서 1회 호출. 대화상자를 띄울 수
        있으므로 반드시 메인 스레드에서 실행한다."""
        if self.gpu_checked or cuda_libs_ready():
            return
        self.gpu_checked = True

        has_nvidia = self.hw.has_nvidia if self.hw else detect_nvidia_gpu()
        if not has_nvidia:
            self._log("NVIDIA GPU가 감지되지 않았습니다. CPU로 실행합니다.")
            return

        if self.declined_gpu_download:
            return

        answer = messagebox.askyesno(
            "GPU 가속 사용",
            "NVIDIA GPU가 감지되었습니다.\n\n"
            "GPU로 변환하면 훨씬 빠르지만, 최초 1회 약 1.3GB의 CUDA 라이브러리를\n"
            "다운로드해야 합니다 (내 PC의 %LOCALAPPDATA%에 저장, 이후 재사용).\n\n"
            "지금 다운로드할까요? '아니오'를 선택하면 CPU로 진행합니다\n"
            "(나중에 다시 물어볼 수 있도록 이번 실행에서만 적용됩니다).",
        )
        if answer:
            self.pending_cuda_download = True
        else:
            self.declined_gpu_download = True

    def _download_file(self, url: str, dest_path: str, label: str, total_hint: int) -> None:
        _download_file(url, dest_path, label, total_hint, self._log)

    # ---- 업데이트 (UI는 메인 스레드, 네트워크는 백그라운드) ---------------

    def _show_release_notes_after_update(self) -> None:
        """업데이트 직후 첫 실행이면, 받아뒀던 패치노트를 한 번 보여준다."""
        version = self.settings.get("pending_notes_version")
        if not isinstance(version, str) or version != APP_VERSION:
            return
        notes = self.settings.get("pending_notes")
        self._remember(pending_notes_version=None, pending_notes=None)

        self._log(f"v{APP_VERSION} 로 업데이트되었습니다.")
        if not isinstance(notes, str) or not notes.strip():
            return
        for line in notes.strip().splitlines():
            self._log("  " + line)
        messagebox.showinfo(
            f"업데이트 완료 (v{APP_VERSION})",
            "이번 버전에서 달라진 점\n\n" + notes.strip()[:1500],
        )

    def _maybe_check_update(self) -> None:
        """자동 확인. 마지막 확인 후 UPDATE_CHECK_INTERVAL 이 지났을 때만 한다."""
        if self.is_msix:
            return
        last = self.settings.get("last_update_check")
        if isinstance(last, (int, float)) and 0 <= time.time() - last < UPDATE_CHECK_INTERVAL:
            return
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _manual_update_check(self) -> None:
        if self.is_msix:
            return
        self.update_button.config(state="disabled")
        self._log("업데이트를 확인하는 중...")
        threading.Thread(target=self._check_update_worker, args=(True,), daemon=True).start()

    def _check_update_worker(self, manual: bool = False) -> None:
        try:
            release, error = fetch_latest_release(), None
        except Exception as exc:  # 인터넷 없음/차단/GitHub 장애 등
            release, error = None, exc
        self.root.after(0, lambda: self._on_update_checked(release, error, manual))

    def _on_update_checked(self, release: Release | None, error: object, manual: bool) -> None:
        self._remember(last_update_check=int(time.time()))
        self.update_button.config(state="normal")

        if release is None:
            self._log("업데이트 확인 실패 (인터넷 연결을 확인하세요).")
            if manual:
                messagebox.showwarning(
                    "업데이트 확인 실패",
                    "업데이트 정보를 가져오지 못했습니다.\n"
                    "인터넷 연결을 확인한 뒤 다시 시도해 주세요.\n\n"
                    f"{error}",
                )
            return

        if not is_newer(release.version, APP_VERSION):
            self._log(f"최신 버전을 쓰고 있습니다 (v{APP_VERSION}).")
            if manual:
                messagebox.showinfo("업데이트 확인", f"최신 버전을 쓰고 있습니다 (v{APP_VERSION}).")
            return

        if not manual and self.settings.get("skip_version") == release.version:
            self._log(f"새 버전 v{release.version} 이(가) 있지만 건너뛰기로 설정되어 있습니다.")
            return

        choice = self._ask_update(release)
        if choice == "skip":
            self._remember(skip_version=release.version)
            self._log(f"v{release.version} 업데이트를 건너뜁니다.")
        elif choice == "now":
            self._start_update(release)

    def _ask_update(self, release: Release) -> str:
        """'now' / 'later' / 'skip' 중 하나를 돌려주는 모달 대화상자."""
        dlg = tk.Toplevel(self.root)
        dlg.title("새 업데이트")
        dlg.geometry("560x440")
        dlg.minsize(420, 320)
        dlg.transient(self.root)
        choice = {"value": "later"}

        def pick(value: str) -> None:
            choice["value"] = value
            dlg.destroy()

        ttk.Label(
            dlg, text=f"새 버전 {release.version} 이(가) 나왔습니다!", font=("", 12, "bold")
        ).pack(anchor="w", padx=14, pady=(14, 2))
        ttk.Label(dlg, text=f"지금 쓰는 버전: {APP_VERSION}", foreground="#555").pack(
            anchor="w", padx=14
        )
        ttk.Label(dlg, text="이번 업데이트 내용", foreground="#555").pack(
            anchor="w", padx=14, pady=(12, 2)
        )

        notes_frame = ttk.Frame(dlg)
        notes_frame.pack(fill="both", expand=True, padx=14)
        notes_text = tk.Text(notes_frame, wrap="word", height=8)
        notes_scroll = ttk.Scrollbar(notes_frame, command=notes_text.yview)
        notes_text.configure(yscrollcommand=notes_scroll.set)
        notes_text.insert("1.0", release.notes or "(변경 내용이 제공되지 않았습니다)")
        notes_text.configure(state="disabled")
        notes_text.pack(side="left", fill="both", expand=True)
        notes_scroll.pack(side="right", fill="y")

        if release.setup_url:
            how = (
                "[지금 업데이트]를 누르면 설치 파일을 내려받고, 프로그램을 닫은 뒤\n"
                "설치 마법사가 실행됩니다. 설정과 받아둔 모델은 그대로 유지됩니다."
            )
        else:
            how = "[지금 업데이트]를 누르면 다운로드 페이지를 브라우저로 엽니다."
        ttk.Label(dlg, text=how, foreground="#555", justify="left").pack(
            anchor="w", padx=14, pady=(10, 0)
        )

        size_hint = f" ({release.setup_size // (1024 * 1024)}MB)" if release.setup_size else ""
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=14, pady=12)
        ttk.Button(row, text="지금 업데이트" + size_hint, command=lambda: pick("now")).pack(
            side="left"
        )
        ttk.Button(row, text="나중에", command=lambda: pick("later")).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="이 버전 건너뛰기", command=lambda: pick("skip")).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", lambda: pick("later"))
        dlg.grab_set()
        self.root.wait_window(dlg)
        return choice["value"]

    def _start_update(self, release: Release) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("작업 진행 중", "변환이 끝난 뒤에 다시 시도해 주세요.")
            return
        if not release.setup_url or os.name != "nt":
            webbrowser.open(release.page_url)
            return

        # 새 버전으로 다시 켰을 때 보여줄 패치노트를 미리 저장해 둔다.
        self._remember(pending_notes_version=release.version, pending_notes=release.notes)
        self.start_button.config(state="disabled")
        self.update_button.config(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._update_worker, args=(release,), daemon=True)
        self.worker.start()

    def _update_worker(self, release: Release) -> None:
        try:
            dest_dir = os.path.join(app_data_dir(), "updates")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, release.setup_name or "STT_KOR-Setup.exe")
            self._log(f"업데이트 다운로드 중: {release.setup_name}")
            self._download_file(release.setup_url, dest, "설치 파일", release.setup_size)
            self._log("다운로드 완료.")
            self.root.after(0, lambda: self._launch_installer(dest))
        except Exception:
            self._log("업데이트 다운로드 실패:\n" + traceback.format_exc())
            self.root.after(0, lambda: self._update_download_failed(release))
        finally:
            self.root.after(0, self._on_worker_done)

    def _update_download_failed(self, release: Release) -> None:
        self._remember(pending_notes_version=None, pending_notes=None)
        if messagebox.askyesno(
            "업데이트 실패",
            "설치 파일을 내려받지 못했습니다.\n\n다운로드 페이지를 브라우저로 열까요?",
        ):
            webbrowser.open(release.page_url)

    def _launch_installer(self, path: str) -> None:
        if not messagebox.askokcancel(
            "설치 시작",
            "설치 파일을 내려받았습니다.\n\n"
            "[확인]을 누르면 이 프로그램이 닫히고 설치가 시작됩니다.\n"
            "설치가 끝나면 마지막 화면의 '지금 실행'을 누르거나,\n"
            "평소 쓰던 아이콘으로 다시 실행하면 새 버전이 뜹니다.",
        ):
            self._log(f"설치 파일 위치: {path}")
            return
        # exe 로 실행 중이면 지금 이 폴더에 덮어쓴다. 소스로 실행 중이면 설치 마법사 기본값.
        app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None
        try:
            subprocess.Popen(
                installer_launch_command(path, os.getpid(), app_dir),
                creationflags=0x00000200 | 0x08000000,  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
        except Exception:
            self._log("설치 파일을 실행하지 못했습니다. 직접 실행해 주세요: " + path)
            return
        self.root.destroy()

    # ---- transcription --------------------------------------------------

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.selected_files:
            messagebox.showwarning("파일 없음", "먼저 오디오 파일을 선택하세요.")
            return
        if not self._confirm_model_choice():
            return

        self.pending_cuda_download = False
        self._maybe_offer_gpu_download()

        self.start_button.config(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _confirm_model_choice(self) -> bool:
        """선택한 모델에 경고(warn) 수준 주의사항이 있으면 진행 여부를 되묻는다."""
        model = self.model_var.get()
        notices = model_notices(model, self.hw, language_code(self.language_var.get()))
        warns = [text for level, text in notices if level == "warn"]
        if not warns:
            return True
        body = (
            f"선택한 모델: {model}\n\n"
            + "\n".join("• " + w for w in warns)
            + "\n\n이대로 진행할까요?"
        )
        return messagebox.askokcancel("모델 확인", body)

    def _output_path_for(self, audio_path: str) -> str:
        return output_path_for(audio_path, self.output_dir)

    def _ensure_model(self, repo: str):
        if self.model is not None and self.model_repo_loaded == repo:
            return self.model
        self.model = load_model(repo, self._log)
        self.model_repo_loaded = repo
        return self.model

    def _run_worker(self) -> None:
        try:
            if getattr(self, "pending_cuda_download", False):
                download_cuda_libs(self._log)
                self.pending_cuda_download = False

            model_size = self.model_var.get()
            language = language_code(self.language_var.get())
            model = self._ensure_model(resolve_model_repo(model_size, language))
            self._log(f"인식 언어: {language_label(language or 'auto')}")

            total = len(self.selected_files)
            for i, audio_path in enumerate(self.selected_files, start=1):
                output_path = self._output_path_for(audio_path)
                self._log(f"[{i}/{total}] {os.path.basename(audio_path)}")

                if os.path.exists(output_path):
                    self._log(f"  이미 처리됨, 건너뜀: {os.path.basename(output_path)}")
                    continue

                transcribe_file(model, audio_path, output_path, language, self._log)

            self._log("모든 파일 처리 완료.")
        except Exception:
            self._log("오류 발생:\n" + traceback.format_exc())
        finally:
            self.root.after(0, self._on_worker_done)

    def _on_worker_done(self) -> None:
        self.progress.stop()
        self.start_button.config(state="normal")
        self.update_button.config(state="normal")


# ---- 개발자용 CLI --------------------------------------------------------
#
# 인자를 하나라도 주고 실행하면 GUI 대신 터미널에서 바로 변환한다.
#   python stt_app.py 0910                  녹음 폴더에서 이름에 0910이 든 파일 전부
#   python stt_app.py 컴퓨터구조 정보보호    검색어 여러 개
#   python stt_app.py D:/rec/강의.m4a       파일 경로를 직접
# 검색어는 녹음 폴더(설정에 기억된 input_dir, 또는 -i) 안 파일 이름에 대한 부분
# 일치다. 설정 파일·모델 캐시·CUDA 라이브러리는 GUI와 같은 것을 쓰므로 둘을 섞어
# 써도 된다.

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".wma")

CLI_EPILOG = """예시:
  python stt_app.py 0910                    이름에 0910이 든 녹음 전부 변환
  python stt_app.py 컴퓨터구조 정보보호      검색어 여러 개 (각각 찾아서 전부)
  python stt_app.py 0910 -m turbo -l en     모델·언어 지정
  python stt_app.py --list                  녹음 폴더의 오디오 목록만 보기
  python stt_app.py -i "D:/소리 녹음" --save 녹음 폴더를 바꾸고 기억시키기

인자 없이 실행하면 기존 GUI 앱이 뜹니다."""


def is_audio_file(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTS)


def find_matches(keyword: str, input_dir: str | None) -> list[str]:
    """검색어 하나에 해당하는 오디오 파일들을 찾는다.

    존재하는 파일 경로를 주면 그 파일 하나, 아니면 녹음 폴더에서 이름에 검색어가
    들어간 오디오 파일을 전부 고른다(대소문자 무시, 하위 폴더는 보지 않는다)."""
    if os.path.isfile(keyword):
        return [os.path.abspath(keyword)]
    if not input_dir:
        return []
    kw = keyword.lower()
    matches = []
    for name in sorted(os.listdir(input_dir)):
        full = os.path.join(input_dir, name)
        if os.path.isfile(full) and is_audio_file(name) and kw in name.lower():
            matches.append(full)
    return matches


def _cli_show_folder(input_dir: str | None) -> None:
    if not input_dir:
        print("녹음 폴더가 지정되어 있지 않습니다.")
        print("  -i/--input-dir 로 지정하거나(--save 를 붙이면 기억), GUI에서")
        print("  오디오 파일을 한 번 고르면 그 폴더를 기억합니다.")
        return
    print(f"녹음 폴더: {input_dir}")
    names = [n for n in sorted(os.listdir(input_dir)) if is_audio_file(n)]
    if not names:
        print("  (오디오 파일 없음)")
        return
    print("현재 파일 목록:")
    for name in names:
        print(f"  - {name}")


def _ask_yes_no(question: str) -> bool:
    """터미널에서 y/n 을 묻는다. 입력을 받을 수 없으면 '아니오'."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes", "예")


def _cli_language(flag: str | None, settings: dict) -> str | None:
    """-l 값 또는 설정에 저장된 언어 -> Whisper 언어 코드(자동 감지는 None)."""
    known = [code or "auto" for _, code in LANGUAGES]
    value = flag or settings.get("language") or DEFAULT_LANGUAGE
    if value not in known:
        value = DEFAULT_LANGUAGE
    return None if value == "auto" else value


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stt_app.py",
        description="Transcribe to Learn (TTL) - 터미널에서 바로 쓰는 개발자용 모드.",
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "keywords", nargs="*", metavar="검색어", help="파일 이름 일부 또는 파일 경로"
    )
    parser.add_argument(
        "-m", "--model", choices=MODEL_SIZES, help="모델 (기본: 하드웨어에 맞춰 자동)"
    )
    parser.add_argument(
        "-l", "--lang", choices=["ko", "en", "auto"], help="인식 언어 (기본: 설정값)"
    )
    parser.add_argument("-i", "--input-dir", help="녹음 폴더 (기본: 설정에 기억된 폴더)")
    parser.add_argument("-o", "--output-dir", help="결과 저장 폴더 (기본: 설정값, 없으면 원본 옆)")
    parser.add_argument("-f", "--force", action="store_true", help="결과 파일이 있어도 다시 변환")
    parser.add_argument("--list", action="store_true", help="녹음 폴더의 오디오 목록만 출력")
    parser.add_argument(
        "--gpu",
        dest="gpu",
        action="store_true",
        default=None,
        help="묻지 않고 CUDA 라이브러리를 내려받아 GPU 사용",
    )
    parser.add_argument("--cpu", dest="gpu", action="store_false", help="묻지 않고 CPU로 실행")
    parser.add_argument(
        "--save", action="store_true", help="이번에 준 폴더/언어를 기본값으로 기억 (GUI와 공유)"
    )
    parser.add_argument("--version", action="version", version=f"Transcribe to Learn (TTL) {APP_VERSION}")
    return parser


def run_cli(argv: list[str]) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    _cli_use_utf8()

    settings = load_settings()
    if args.input_dir and not _valid_dir(args.input_dir):
        print(f"녹음 폴더를 찾을 수 없습니다: {args.input_dir}")
        return 2
    input_dir = _valid_dir(args.input_dir) or _valid_dir(settings.get("input_dir"))

    output_dir = args.output_dir or _valid_dir(settings.get("output_dir"))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    language = _cli_language(args.lang, settings)

    if args.save:
        if input_dir:
            settings["input_dir"] = input_dir
        if args.output_dir:
            settings["output_dir"] = output_dir
        settings["language"] = language or "auto"
        save_settings(settings)
        print(f"기본값으로 기억했습니다: {settings_path()}")

    if args.list or not args.keywords:
        if not args.list:
            parser.print_help()
            print()
        _cli_show_folder(input_dir)
        # 검색어 없이 --list/--save 만 주는 것은 정상 사용이므로 0.
        return 0 if (args.list or args.save) else 1

    targets: list[str] = []
    for keyword in args.keywords:
        found = find_matches(keyword, input_dir)
        if not found:
            print(f"'{keyword}'와(과) 일치하는 파일을 찾을 수 없습니다.")
            continue
        for path in found:
            if path not in targets:
                targets.append(path)

    if not targets:
        print("변환할 파일이 없습니다.")
        print()
        _cli_show_folder(input_dir)
        return 1

    print(f"검색 결과 {len(targets)}개 파일 발견:")
    for path in targets:
        print(f"  - {os.path.basename(path)}")
    print()

    hw = detect_hardware()
    model_size = args.model
    if model_size:
        print(f"모델: {model_size}")
    else:
        model_size, reason = recommend_model(hw, language)
        print(f"모델 자동 선택: {model_size} - {reason}")
        print("  (직접 고르려면 -m " + " | ".join(MODEL_SIZES) + ")")
    print(f"인식 언어: {language_label(language or 'auto')}")
    for level, text in model_notices(model_size, hw, language):
        print(("  ! " if level == "warn" else "  - ") + text)
    print()

    if not cuda_libs_ready():
        if not hw.has_nvidia:
            print("NVIDIA GPU가 감지되지 않았습니다. CPU로 실행합니다.")
        else:
            want = args.gpu
            if want is None:
                print("NVIDIA GPU가 감지되었습니다. GPU로 변환하면 훨씬 빠르지만,")
                print("최초 1회 약 1.3GB의 CUDA 라이브러리를 내려받아야 합니다.")
                print(f"(저장 위치: {cuda_libs_dir()} - 이후 재사용)")
                want = _ask_yes_no("지금 내려받을까요? [y/N] ")
            if want:
                download_cuda_libs(print)
            else:
                print("CPU로 진행합니다.")
        print()

    model = load_model(resolve_model_repo(model_size, language), print)

    failed = 0
    total = len(targets)
    for i, audio_path in enumerate(targets, start=1):
        output_path = output_path_for(audio_path, output_dir)
        print(f"[{i}/{total}] {os.path.basename(audio_path)}")
        if os.path.exists(output_path) and not args.force:
            print(f"  이미 처리됨, 건너뜀: {output_path}  (--force 로 다시 변환)")
            continue
        try:
            transcribe_file(model, audio_path, output_path, language, print)
        except Exception:
            failed += 1
            print("  변환 실패:")
            print(traceback.format_exc())

    if failed:
        print(f"완료. {total - failed}개 성공, {failed}개 실패.")
        return 1
    print("모든 파일 처리 완료.")
    return 0


def _cli_use_utf8() -> None:
    """출력을 파일·파이프로 넘길 때만 UTF-8로 고정한다.

    콘솔에 직접 찍을 때는 건드리지 않는다. 윈도우 콘솔이면 파이썬이 이미
    유니코드 API로 쓰고 있어서 코드 페이지가 949여도 한글이 제대로 나오는데,
    여기서 UTF-8로 바꿔 버리면 오히려 깨진다. 반대로 리다이렉트된 출력은
    기본값이 cp949라 화살표(→) 같은 글자에서 깨질 수 있어 UTF-8로 맞춘다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _cli_console_ready() -> bool:
    """CLI 출력을 보여 줄 콘솔이 있는지 확인한다.

    소스로 실행하면(python stt_app.py) 항상 있다. exe는 --windowed 로 빌드돼
    자기 콘솔이 없지만, cmd/PowerShell에서 실행했다면 부모 콘솔에 붙어 출력할 수
    있다. 붙을 콘솔이 아예 없으면(바탕화면 아이콘, 파일 끌어다 놓기) 인자를 무시
    하고 GUI를 띄우도록 False를 돌려준다 - 아무 창도 안 뜨는 게 제일 나쁘다."""
    if sys.stdout is not None and sys.stderr is not None:
        return True
    if os.name != "nt":
        return False
    attach_parent_process = -1
    try:
        if not ctypes.windll.kernel32.AttachConsole(attach_parent_process):
            return False
        enc = "cp%d" % ctypes.windll.kernel32.GetConsoleOutputCP()
        sys.stdout = open("CONOUT$", "w", encoding=enc, errors="replace", buffering=1)
        sys.stderr = sys.stdout
        try:
            sys.stdin = open("CONIN$", "r", encoding=enc, errors="replace")
        except OSError:
            pass
    except Exception:
        return False
    return True


def main() -> None:
    argv = sys.argv[1:]
    if argv and _cli_console_ready():
        try:
            code = run_cli(argv)
        except KeyboardInterrupt:
            print()
            print("중단했습니다.")
            code = 130
        raise SystemExit(code)

    set_taskbar_identity()
    root = tk.Tk()
    SttApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
