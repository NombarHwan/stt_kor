"""한국어 강의 STT - 간단한 데스크톱 앱.

Python이나 pip 없이도 실행할 수 있도록 PyInstaller로 exe 패키징하는 것을
전제로 만들어졌습니다 (build_exe.ps1 참고). exe 자체는 CPU 전용으로 작게
빌드되고, 실행 중 NVIDIA GPU가 감지되면 사용자 동의를 받아 CUDA 가속
라이브러리(cublas/cudnn/nvrtc, 약 1.3GB)를 PyPI에서 내려받아
%LOCALAPPDATA%에 캐싱합니다(최초 1회). AMD GPU나 내장 그래픽, GPU가 없는
환경에서는 다운로드 없이 바로 CPU로 동작합니다.

앱 시작 시 RAM/CPU 코어/GPU(VRAM)를 감지해 적정 모델을 자동으로 골라
주고(recommend_model), 사용자가 모델을 바꾸면 예상 처리 시간·품질·메모리
경고 등 주의사항을 화면에 표시합니다(model_notices).
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import urllib.request
import zipfile
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

APP_DIR_NAME = "STT_KOR"

# ctranslate2가 필요로 하는 CUDA 런타임 구성 요소. 버전은 현재 개발 환경에
# 설치된 nvidia-*-cu12 wheel과 맞춰뒀습니다. ctranslate2를 업그레이드하면
# 여기 버전도 함께 맞춰야 할 수 있습니다.
CUDA_PACKAGES = [
    ("nvidia-cublas-cu12", "12.9.2.10"),
    ("nvidia-cudnn-cu12", "9.25.1.1"),
    ("nvidia-cuda-nvrtc-cu12", "12.9.86"),
]

MODEL_SIZES = ["large-v3", "medium", "small", "base", "tiny"]
# 하드웨어 감지 전까지 쓰는 잠정 기본값. 감지가 끝나면 recommend_model()이
# 사용자가 아직 콤보박스를 건드리지 않은 경우에 한해 권장값으로 바꾼다.
DEFAULT_MODEL_SIZE = "medium"

# 모델별 최초 1회 다운로드 용량과 한국어 강의 기준 품질 설명.
MODEL_INFO = {
    "tiny": {"download": "약 75 MB", "quality": "매우 낮음 — 키워드 수준, 받아쓰기 부적합"},
    "base": {"download": "약 145 MB", "quality": "낮음 — 대략적인 내용 파악용"},
    "small": {"download": "약 480 MB", "quality": "보통 — 깨끗한 녹음이면 요지 파악, 교정 많이 필요"},
    "medium": {"download": "약 1.5 GB", "quality": "좋음 — 일반 강의는 신뢰할 만함, 가벼운 교정"},
    "large-v3": {"download": "약 3.1 GB", "quality": "최상 — 전문용어·숫자에 강함, 거의 교정 불필요"},
}

# 1시간 분량 오디오 기준 예상 처리 시간. backend: gpu / cpu_strong / cpu_weak.
EST_TIME = {
    "tiny": {"gpu": "1분 내외", "cpu_strong": "3~6분", "cpu_weak": "10~15분"},
    "base": {"gpu": "1~2분", "cpu_strong": "5~10분", "cpu_weak": "15~25분"},
    "small": {"gpu": "2~4분", "cpu_strong": "15~30분", "cpu_weak": "40~70분"},
    "medium": {"gpu": "3~6분", "cpu_strong": "45~90분", "cpu_weak": "2~3시간"},
    "large-v3": {"gpu": "5~12분", "cpu_strong": "2~4시간", "cpu_weak": "5시간 이상"},
}

AUDIO_FILETYPES = [
    ("오디오 파일", "*.mp3 *.wav *.m4a *.mp4 *.aac *.flac *.ogg *.wma"),
    ("모든 파일", "*.*"),
]


# ---- GPU 감지 및 CUDA 라이브러리 온디맨드 다운로드 ------------------------


def cuda_libs_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME, "cuda_libs")


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


def recommend_model(hw: Hardware | None) -> tuple[str, str]:
    """(권장 모델, 이유 문구)."""
    if hw is None:
        return "medium", "하드웨어를 확인하지 못해 안전한 기본값(medium)을 사용합니다."
    if hw.has_nvidia:
        if hw.vram_gb is None or hw.vram_gb >= 5.5:
            return (
                "large-v3",
                f"NVIDIA GPU 감지 ({hw.gpu_name or '모델명 미상'}) — 최고 품질 모델을 쓸 수 있습니다.",
            )
        if hw.vram_gb >= 3.5:
            return (
                "medium",
                f"NVIDIA GPU VRAM 약 {hw.vram_gb:.0f}GB — large-v3에는 부족해 medium을 권장합니다.",
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


def _model_cached(model: str) -> bool:
    """faster-whisper(HuggingFace 캐시)에 모델이 이미 받아져 있는지 추정."""
    name = model.replace("/", "--")
    bases = []
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            bases.append(os.environ[var])
    if os.environ.get("HF_HOME"):
        bases.append(os.path.join(os.environ["HF_HOME"], "hub"))
    bases.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    for b in bases:
        try:
            if glob.glob(os.path.join(b, f"models--*faster-whisper-{name}*")):
                return True
        except Exception:
            pass
    return False


def model_notices(model: str, hw: Hardware | None) -> list[tuple[str, str]]:
    """선택한 모델에 대한 (수준, 문구) 목록. 수준은 'warn' 또는 'info'."""
    backend = _model_backend(hw)
    est = EST_TIME[model][backend]
    where = "GPU 사용 시" if backend == "gpu" else "CPU 사용"
    out: list[tuple[str, str]] = [
        ("info", f"1시간 분량 기준 예상 처리 시간: 약 {est} ({where})"),
        ("info", f"품질: {MODEL_INFO[model]['quality']}"),
    ]
    if not _model_cached(model):
        out.append(
            ("info", f"이 모델을 처음 쓰면 최초 1회 {MODEL_INFO[model]['download']}를 내려받습니다.")
        )

    ram = hw.ram_gb if hw else None
    if model == "large-v3":
        if backend != "gpu":
            out.append(
                ("warn", "GPU를 쓰지 않아 처리 시간이 매우 깁니다. medium 이하를 권장합니다.")
            )
        if ram is not None and ram < 12:
            out.append(
                (
                    "warn",
                    f"large-v3는 RAM 16GB를 권장합니다. 현재 약 {ram:.0f}GB로 "
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


class SttApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("한국어 강의 STT")
        self.root.geometry("640x600")
        self.root.minsize(520, 480)

        self.selected_files: list[str] = []
        self.output_dir: str | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.model = None
        self.model_size_loaded: str | None = None
        self.gpu_checked = False
        self.declined_gpu_download = False
        self.hw: Hardware | None = None
        self.model_user_touched = False

        self._build_widgets()
        self.root.after(100, self._drain_log_queue)
        threading.Thread(target=self._detect_hw_worker, daemon=True).start()

    # ---- UI ---------------------------------------------------------

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

        self.files_label = ttk.Label(self.root, text="선택된 파일 없음", foreground="#555")
        self.files_label.pack(fill="x", padx=10)

        self.output_label = ttk.Label(
            self.root, text="출력 폴더: (원본 파일과 같은 폴더)", foreground="#555"
        )
        self.output_label.pack(fill="x", padx=10, pady=(0, 6))

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

        self.root.bind("<Configure>", self._on_resize)
        self._refresh_model_note()

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="변환할 오디오 파일 선택", filetypes=AUDIO_FILETYPES)
        if not paths:
            return
        self.selected_files = list(paths)
        names = ", ".join(os.path.basename(p) for p in self.selected_files)
        self.files_label.config(text=f"선택된 파일 ({len(self.selected_files)}개): {names}")

    def _choose_output_dir(self) -> None:
        directory = filedialog.askdirectory(title="출력 폴더 선택")
        if not directory:
            return
        self.output_dir = directory
        self.output_label.config(text=f"출력 폴더: {directory}")

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
        rec, reason = recommend_model(hw)

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
        summary = " · ".join(bits) if bits else "하드웨어 정보를 읽지 못함"

        if not self.model_user_touched:
            self.model_var.set(rec)
        self.hw_label.config(text=f"감지: {summary}\n권장 모델: {rec} — {reason}")
        self._refresh_model_note()

    def _on_model_selected(self, _event: object = None) -> None:
        self.model_user_touched = True
        self._refresh_model_note()

    def _refresh_model_note(self) -> None:
        notices = model_notices(self.model_var.get(), self.hw)
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

    def _download_cuda_libs(self) -> None:
        libs_dir = cuda_libs_dir()
        tmp_dir = os.path.join(libs_dir, "_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            for pkg_name, version in CUDA_PACKAGES:
                self._log(f"GPU 라이브러리 조회 중: {pkg_name} {version}")
                url, size = _pypi_wheel_url(pkg_name, version)
                wheel_path = os.path.join(tmp_dir, pkg_name + ".whl")
                self._download_file(url, wheel_path, pkg_name, size)
                self._log(f"압축 해제 중: {pkg_name}")
                _extract_nvidia_dlls(wheel_path, libs_dir)
                os.remove(wheel_path)
            with open(os.path.join(libs_dir, ".complete"), "w", encoding="utf-8") as f:
                f.write("ok")
            self._log("GPU 가속 라이브러리 준비 완료.")
        except Exception:
            self._log(
                "GPU 라이브러리 다운로드에 실패했습니다. CPU로 진행합니다.\n"
                + traceback.format_exc()
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _download_file(self, url: str, dest_path: str, label: str, total_hint: int) -> None:
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
                            self._log(f"  {label}: {pct}% ({mb}MB / {total_mb}MB)")

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
        warns = [text for level, text in model_notices(model, self.hw) if level == "warn"]
        if not warns:
            return True
        body = (
            f"선택한 모델: {model}\n\n"
            + "\n".join("• " + w for w in warns)
            + "\n\n이대로 진행할까요?"
        )
        return messagebox.askokcancel("모델 확인", body)

    def _output_path_for(self, audio_path: str) -> str:
        base = os.path.splitext(os.path.basename(audio_path))[0]
        out_dir = self.output_dir or os.path.dirname(audio_path)
        return os.path.join(out_dir, base + "_transcript.txt")

    def _ensure_model(self, model_size: str):
        if self.model is not None and self.model_size_loaded == model_size:
            return self.model

        _register_cuda_dlls()
        from faster_whisper import WhisperModel

        self._log(f"모델 로딩 중... ({model_size}, GPU 있으면 자동 사용, 없으면 CPU)")
        self._log("※ 처음 실행하는 모델이면 인터넷에서 다운로드가 필요합니다 (수백MB~수GB).")
        self.model = WhisperModel(model_size, device="auto", compute_type="auto")
        self.model_size_loaded = model_size
        self._log("모델 로딩 완료.")
        return self.model

    def _run_worker(self) -> None:
        try:
            if getattr(self, "pending_cuda_download", False):
                self._download_cuda_libs()
                self.pending_cuda_download = False

            model_size = self.model_var.get()
            model = self._ensure_model(model_size)

            total = len(self.selected_files)
            for i, audio_path in enumerate(self.selected_files, start=1):
                output_path = self._output_path_for(audio_path)
                self._log(f"[{i}/{total}] {os.path.basename(audio_path)}")

                if os.path.exists(output_path):
                    self._log(f"  이미 처리됨, 건너뜀: {os.path.basename(output_path)}")
                    continue

                segments, info = model.transcribe(
                    audio_path,
                    language="ko",
                    beam_size=5,
                    condition_on_previous_text=False,
                    hallucination_silence_threshold=2.0,
                )
                self._log(f"  감지된 언어: {info.language} (확률 {info.language_probability:.2%})")

                lines = []
                for seg in segments:
                    timestamp = f"[{seg.start:6.1f}s → {seg.end:6.1f}s]"
                    line = f"{timestamp} {seg.text.strip()}"
                    self._log("  " + line)
                    lines.append(line)

                out_dir = os.path.dirname(output_path)
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                self._log(f"  저장 완료: {output_path}")

            self._log("모든 파일 처리 완료.")
        except Exception:
            self._log("오류 발생:\n" + traceback.format_exc())
        finally:
            self.root.after(0, self._on_worker_done)

    def _on_worker_done(self) -> None:
        self.progress.stop()
        self.start_button.config(state="normal")


def main() -> None:
    root = tk.Tk()
    SttApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
