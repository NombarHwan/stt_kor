import os
import sys


def _preload_nvidia_dlls() -> None:
    # ctranslate2's own LoadLibrary calls for CUDA dependencies don't honor
    # os.add_dll_directory, so cublas64_12.dll intermittently fails to
    # resolve. Preloading it here with ctypes puts it in the process's
    # already-loaded module table, so ctranslate2's later lookup finds it
    # immediately instead of searching.
    if os.name != "nt":
        return
    try:
        import ctypes
        import nvidia.cublas
        import nvidia.cuda_nvrtc
        import nvidia.cudnn
    except ImportError:
        return

    for pkg in (nvidia.cublas, nvidia.cuda_nvrtc, nvidia.cudnn):
        for base in pkg.__path__:
            bin_dir = os.path.join(base, "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)

    for dll_name in ("cublas64_12.dll", "cublasLt64_12.dll"):
        for base in nvidia.cublas.__path__:
            dll_path = os.path.join(base, "bin", dll_name)
            if os.path.isfile(dll_path):
                try:
                    ctypes.WinDLL(dll_path)
                except OSError:
                    pass
                break


_preload_nvidia_dlls()

from faster_whisper import WhisperModel

MODEL_SIZE = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

INPUT_DIR = r"C:\Users\heosu\OneDrive\Documents\소리 녹음"
OUTPUT_DIR = r"C:\Users\heosu\OneDrive\바탕 화면\Study_Material\2학기_강의STT"


def find_matches(keyword: str) -> list[str]:
    if os.path.exists(keyword):
        return [keyword]

    if not os.path.isdir(INPUT_DIR):
        return []

    keyword_lower = keyword.lower()
    matches = []
    for name in sorted(os.listdir(INPUT_DIR)):
        full_path = os.path.join(INPUT_DIR, name)
        if os.path.isfile(full_path) and keyword_lower in name.lower():
            matches.append(full_path)
    return matches


def output_path_for(audio_path: str) -> str:
    base = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(OUTPUT_DIR, base + "_transcript.txt")


def transcribe(model: WhisperModel, audio_path: str, output_path: str) -> str:
    print(f"변환 시작: {audio_path}")
    segments, info = model.transcribe(audio_path, language="ko", beam_size=5)

    print(f"감지된 언어: {info.language} (확률 {info.language_probability:.2%})")
    print("-" * 60)

    lines = []
    for seg in segments:
        timestamp = f"[{seg.start:6.1f}s → {seg.end:6.1f}s]"
        line = f"{timestamp} {seg.text.strip()}"
        print(line)
        lines.append(line)

    result = "\n".join(lines)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    print("-" * 60)
    print(f"저장 완료: {output_path}")
    return result


def print_usage() -> None:
    print("사용법: python stt_korean.py <검색어 또는 파일명> [검색어2 ...]")
    print("  - 녹음 폴더 안의 파일명에 검색어가 포함되면 모두 변환 대상이 됩니다.")
    print("  - 예: python stt_korean.py 0901")
    print("  - 예: python stt_korean.py 정치와공론 컴퓨터구조")
    print()
    print(f"녹음 폴더: {INPUT_DIR}")
    if os.path.isdir(INPUT_DIR):
        files = sorted(os.listdir(INPUT_DIR))
        if files:
            print("현재 파일 목록:")
            for name in files:
                print(f"  - {name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    keywords = sys.argv[1:]
    targets = []
    seen = set()
    for keyword in keywords:
        found = find_matches(keyword)
        if not found:
            print(f"'{keyword}'와(과) 일치하는 파일을 찾을 수 없습니다.")
            continue
        for path in found:
            if path not in seen:
                seen.add(path)
                targets.append(path)

    if not targets:
        print("변환할 파일이 없습니다.")
        sys.exit(1)

    print(f"검색 결과 {len(targets)}개 파일 발견:")
    for path in targets:
        print(f"  - {os.path.basename(path)}")
    print()

    print(f"모델 로딩 중... ({MODEL_SIZE}, {DEVICE})")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)

    total = len(targets)
    for i, audio_path in enumerate(targets, start=1):
        output_path = output_path_for(audio_path)
        print(f"[{i}/{total}] {os.path.basename(audio_path)}")

        if os.path.exists(output_path):
            print(f"  이미 처리됨, 건너뜀: {os.path.basename(output_path)}")
            continue

        transcribe(model, audio_path, output_path)
