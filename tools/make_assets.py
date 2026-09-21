"""TTL_logo.png 하나에서 Microsoft Store(MSIX) 아이콘 세트와 .ico 를 만든다.

  python tools/make_assets.py

원본은 2800x1752 흰 배경 PNG 안에 307x307 짜리 로고가 박혀 있는 형태다.
그래서 (1) 로고만 잘라내고 (2) 흰 배경을 알파로 바꾸고 (3) 필요한 크기로
다시 그린다.

원본이 307px 뿐이라 픽셀을 그냥 늘리면 대각선 계단이 그대로 커진다. 그래서
경계를 폴리곤으로 추출해(vectorize.py) 크기마다 다시 칠한다. 원본 해상도에
매이지 않으므로 2160px 박스아트도 깨끗하다.

32px 이하에서는 하단 "T.T.L" 글자가 읽히지 않고 얼룩만 남는다. 이 글자들은
A 와 연결되지 않은 별도 도형이라 폴리곤 단계에서 통째로 빼고 A + 마이크만
남긴다(vectorize.split_letters).

색 변형을 두 벌 만든다. Windows 는 작업 표시줄 등에서 배경판 없이
(altform-unplated) 아이콘을 얹는데, 검은 로고는 어두운 테마에서 사라지기
때문이다.
  - dark  : 검은 로고  -> 밝은 배경용 (타일, altform-lightunplated)
  - light : 흰 로고    -> 어두운 배경용 (altform-unplated, BadgeLogo)
"""

from __future__ import annotations

import math
import os
import struct

import numpy as np
from PIL import Image

import vectorize

SRC = "TTL_logo.png"
OUT = "assets"

# Windows 배율. 100% 를 기준으로 각 자산의 실제 픽셀 크기가 정해진다.
SCALES = (100, 125, 150, 200, 400)

# (자산 이름, 100% 기준 가로, 세로, 로고가 캔버스에서 차지할 비율)
#
# 비율이 1.0 이 아닌 것들은 타일이다. 타일은 로고가 꽉 차면 답답해 보이고
# Windows 디자인 가이드도 여백을 권한다. 반대로 작업 표시줄에 쓰이는
# targetsize 자산은 작게 보여야 하므로 꽉 채운다.
TILES = [
    ("Square44x44Logo", 44, 44, 1.00),
    ("Square71x71Logo", 71, 71, 0.60),
    ("Square150x150Logo", 150, 150, 0.60),
    ("Square310x310Logo", 310, 310, 0.60),
    ("Wide310x150Logo", 310, 150, 0.60),
    ("SplashScreen", 620, 300, 0.60),
    ("StoreLogo", 50, 50, 0.80),
    ("BadgeLogo", 24, 24, 1.00),
]

# 작업 표시줄 / Alt+Tab / 탐색기에서 쓰는 크기들.
TARGET_SIZES = (16, 24, 32, 48, 256)

# .ico 에 넣을 크기.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# 이 크기 미만에서는 "T.T.L" 을 빼고 A + 마이크만 쓴다.
SIMPLIFY_BELOW = 48


def load_mask() -> np.ndarray:
    """원본에서 로고 부분만 잘라 이진 마스크로 만든다.

    배경이 투명이 아니라 흰색이라, 알파가 아니라 밝기로 그림을 가른다."""
    im = Image.open(SRC).convert("RGBA")
    lum = np.array(im)[:, :, :3].astype(np.float32).mean(axis=2)
    ys, xs = np.where(lum < 128)
    lum = lum[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    return lum < 128


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    mask = load_mask()
    full = vectorize.build(mask)
    simple = vectorize.split_letters(full)
    print(f"원본 로고 {mask.shape[1]}x{mask.shape[0]} -> 폴리곤 {len(full)}개")

    def draw(w, h, ratio, white):
        """작은 크기에는 글자를 뺀 버전을 쓴다."""
        polys = simple if min(w, h) < SIMPLIFY_BELOW else full
        return vectorize.render(polys, w, h, ratio, white)

    made = 0

    # --- 타일 / 스플래시 / 스토어 로고 (배율별) ---
    for name, bw, bh, ratio in TILES:
        white = name == "BadgeLogo"     # 알림 배지는 단색 흰색만 허용된다
        for scale in SCALES:
            # Windows 규격은 올림이다 (71 x 1.5 = 106.5 -> 107, 50 x 1.25 -> 63).
            w = math.ceil(bw * scale / 100)
            h = math.ceil(bh * scale / 100)
            path = f"{OUT}/{name}.scale-{scale}.png"
            draw(w, h, ratio, white).save(path)
            made += 1
        # 배율 접미사 없는 기본 파일도 둔다(도구에 따라 이걸 찾는다).
        draw(bw, bh, ratio, white).save(f"{OUT}/{name}.png")
        made += 1

    # --- 작업 표시줄 / Alt+Tab / 탐색기용 targetsize ---
    for size in TARGET_SIZES:
        variants = {
            "": False,                        # 배경판 있음(밝은 판) -> 검정
            "_altform-unplated": True,        # 배경판 없음, 어두운 테마 -> 흰색
            "_altform-lightunplated": False,  # 배경판 없음, 밝은 테마 -> 검정
        }
        for suffix, white in variants.items():
            path = f"{OUT}/Square44x44Logo.targetsize-{size}{suffix}.png"
            draw(size, size, 1.0, white).save(path)
            made += 1

    # --- 스토어 등록 페이지용 ---
    draw(300, 300, 0.80, False).save(f"{OUT}/StoreListing-logo-300.png")
    draw(2160, 2160, 0.60, False).save(f"{OUT}/StoreListing-boxart-2160.png")
    draw(1920, 1080, 0.45, False).save(f"{OUT}/StoreListing-hero-1920x1080.png")
    made += 3

    # --- exe / 설치 마법사용 .ico ---
    frames = [draw(s, s, 1.0, False) for s in ICO_SIZES]
    frames[-1].save(
        f"{OUT}/TTL.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1]
    )
    made += 1

    print(f"{OUT}/ 에 {made}개 파일 생성")


if __name__ == "__main__":
    main()
