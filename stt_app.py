"""한국어 강의 STT - 간단한 데스크톱 앱.

Python이나 pip 없이도 실행할 수 있도록 PyInstaller로 exe 패키징하는 것을
전제로 만들어졌습니다 (build_exe.ps1 참고). exe 자체는 CPU 전용으로 작게
빌드되고, 실행 중 NVIDIA GPU가 감지되면 사용자 동의를 받아 CUDA 가속
라이브러리(cublas/cudnn/nvrtc, 약 1.3GB)를 PyPI에서 내려받아
%LOCALAPPDATA%에 캐싱합니다(최초 1회). AMD GPU나 내장 그래픽, GPU가 없는
환경에서는 다운로드 없이 바로 CPU로 동작합니다.
"""

import ctypes
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
DEFAULT_MODEL_SIZE = "large-v3"
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


class SttApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("한국어 강의 STT")
        self.root.geometry("640x520")
        self.root.minsize(520, 400)

        self.selected_files: list[str] = []
        self.output_dir: str | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.model = None
        self.model_size_loaded: str | None = None
        self.gpu_checked = False
        self.declined_gpu_download = False

        self._build_widgets()
        self.root.after(100, self._drain_log_queue)

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

        self.files_label = ttk.Label(self.root, text="선택된 파일 없음", foreground="#555")
        self.files_label.pack(fill="x", padx=10)

        self.output_label = ttk.Label(
            self.root, text="출력 폴더: (원본 파일과 같은 폴더)", foreground="#555"
        )
        self.output_label.pack(fill="x", padx=10, pady=(0, 6))

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

    # ---- GPU 확인/다운로드 (메인 스레드) ---------------------------------

    def _maybe_offer_gpu_download(self) -> None:
        """변환 시작 직전, 메인 스레드에서 1회 호출. 대화상자를 띄울 수
        있으므로 반드시 메인 스레드에서 실행한다."""
        if self.gpu_checked or cuda_libs_ready():
            return
        self.gpu_checked = True

        if not detect_nvidia_gpu():
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

        self.pending_cuda_download = False
        self._maybe_offer_gpu_download()

        self.start_button.config(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

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

                segments, info = model.transcribe(audio_path, language="ko", beam_size=5)
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
