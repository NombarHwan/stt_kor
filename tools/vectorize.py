"""307px 래스터 로고를 외곽선(폴리곤)으로 바꿔, 어떤 크기로든 깨끗하게 그린다.

원본이 307px 뿐이라 그대로 확대하면 대각선에 픽셀 계단이 그대로 커진다.
이 로고는 사실상 직선과 원호로만 이루어진 2색 도형이므로, 경계를 따라가
폴리곤으로 만든 뒤

  1) RDP 단순화 - 1픽셀 계단을 하나의 직선으로 접는다 (계단이 사라진다)
  2) 완만한 꼭짓점만 부드럽게 - 마이크 원호는 둥글려 주고 A 꼭지점 같은
     날카로운 모서리는 그대로 둔다
  3) 4배 수퍼샘플링으로 그린 뒤 축소 - 어느 크기에서도 깔끔한 안티에일리어싱

다시 칠하는 방식이라 2160px 로 뽑아도 원본 해상도에 매이지 않는다.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw


def trace(mask: np.ndarray) -> list[list[tuple[float, float]]]:
    """이진 마스크의 경계를 닫힌 폴리곤들로 뽑는다(픽셀 격자 좌표).

    각 검은 픽셀의 네 변 중 바깥과 맞닿은 변만 방향을 맞춰 모은 뒤, 끝점을
    이어 고리를 만든다. 구멍은 바깥 윤곽과 반대 방향으로 나온다."""
    h, w = mask.shape
    pad = np.zeros((h + 2, w + 2), bool)
    pad[1:-1, 1:-1] = mask

    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    ys, xs = np.where(pad)
    for y, x in zip(ys.tolist(), xs.tolist()):
        if not pad[y - 1, x]:
            edges.setdefault((x, y), []).append((x + 1, y))
        if not pad[y, x + 1]:
            edges.setdefault((x + 1, y), []).append((x + 1, y + 1))
        if not pad[y + 1, x]:
            edges.setdefault((x + 1, y + 1), []).append((x, y + 1))
        if not pad[y, x - 1]:
            edges.setdefault((x, y + 1), []).append((x, y))

    loops = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        cur = start
        prev_dir = None
        while True:
            outs = edges.get(cur)
            if not outs:
                break
            if len(outs) == 1:
                nxt = outs.pop(0)
            else:
                # 대각선으로 스치는 지점. 들어온 방향에서 가장 시계방향으로
                # 꺾이는 쪽을 골라야 고리가 엉키지 않는다.
                def turn(p):
                    d = (p[0] - cur[0], p[1] - cur[1])
                    if prev_dir is None:
                        return 0
                    return math.atan2(
                        prev_dir[0] * d[1] - prev_dir[1] * d[0],
                        prev_dir[0] * d[0] + prev_dir[1] * d[1],
                    )
                nxt = min(outs, key=turn)
                outs.remove(nxt)
            if not edges[cur]:
                del edges[cur]
            prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start:
                break
            loop.append(cur)
        if len(loop) >= 4:
            loops.append([(float(x) - 1.0, float(y) - 1.0) for x, y in loop])
    return loops


def _rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker. 계단을 직선 하나로 접는 역할을 한다."""
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if norm == 0:
            d = math.hypot(px - a[0], py - a[1])
        else:
            d = abs(dy * (px - a[0]) - dx * (py - a[1])) / norm
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [a, b]
    return _rdp(pts[: idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def simplify(loop, eps: float):
    """닫힌 고리를 RDP 로 단순화한다."""
    closed = loop + [loop[0]]
    out = _rdp(closed, eps)
    return out[:-1]


def smooth(loop, rounds: int = 2, sharp_deg: float = 42.0):
    """완만하게 꺾이는 꼭짓점만 둥글린다.

    모든 꼭짓점을 둥글리면(일반적인 Chaikin) A 의 뾰족한 꼭지점까지 뭉개진다.
    꺾임이 임계값보다 큰 '진짜 모서리'는 건드리지 않는다."""
    limit = math.radians(sharp_deg)
    for _ in range(rounds):
        n = len(loop)
        if n < 4:
            return loop
        out = []
        for i in range(n):
            p0, p1, p2 = loop[(i - 1) % n], loop[i], loop[(i + 1) % n]
            v1 = (p1[0] - p0[0], p1[1] - p0[1])
            v2 = (p2[0] - p1[0], p2[1] - p1[1])
            ang = abs(
                math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1])
            )
            if ang > limit:
                out.append(p1)                      # 날카로운 모서리는 보존
            else:
                out.append((p0[0] + 0.75 * v1[0], p0[1] + 0.75 * v1[1]))
                out.append((p1[0] + 0.25 * v2[0], p1[1] + 0.25 * v2[1]))
        loop = out
    return loop


def _area(loop) -> float:
    s = 0.0
    for i in range(len(loop)):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % len(loop)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def build(mask: np.ndarray, eps: float = 1.15, rounds: int = 2):
    """마스크 -> (폴리곤, 구멍인가) 목록. 원본 픽셀 좌표계."""
    out = []
    for loop in trace(mask):
        loop = simplify(loop, eps)
        if len(loop) < 3:
            continue
        hole = _area(loop) < 0          # 바깥 윤곽과 반대 방향 = 구멍
        out.append((smooth(loop, rounds), hole))
    return out


def bbox_of(polys) -> tuple[float, float, float, float]:
    xs = [x for loop, _ in polys for x, _ in loop]
    ys = [y for loop, _ in polys for _, y in loop]
    return min(xs), min(ys), max(xs), max(ys)


def split_letters(polys):
    """(A+마이크만, 전체) 로 나눈다.

    원본에서 "T.T.L" 은 A 에서 파낸 구멍이 아니라 A 아래에 따로 그려진 검은
    글자다(연결 성분이 완전히 분리된다). 그래서 가장 큰 바깥 윤곽 하나와 그
    안에 든 구멍만 남기면 글자가 통째로 빠진다. 32px 이하에서는 글자가 읽히지
    않고 얼룩만 되므로 이 버전을 쓴다."""
    outers = [(loop, hole) for loop, hole in polys if not hole]
    holes = [(loop, hole) for loop, hole in polys if hole]
    main = max(outers, key=lambda p: abs(_area(p[0])))
    x0, y0, x1, y1 = bbox_of([main])
    inside = [
        (loop, hole)
        for loop, hole in holes
        if x0 <= min(x for x, _ in loop) and max(x for x, _ in loop) <= x1
        and y0 <= min(y for _, y in loop) and max(y for _, y in loop) <= y1
    ]
    return [main] + inside


def render(polys, w: int, h: int, ratio: float, white: bool = False, ss: int = 4) -> Image.Image:
    """폴리곤을 캔버스 가운데에 ratio 비율로 앉혀 알파 이미지로 그린다.

    ss 배로 크게 그린 뒤 줄여서, 어떤 크기에서도 깔끔한 경계를 얻는다."""
    x0, y0, x1, y1 = bbox_of(polys)
    src = max(x1 - x0, y1 - y0)
    inner = max(1.0, min(w, h) * ratio)
    scale = inner / src
    ox = (w - (x1 - x0) * scale) / 2.0
    oy = (h - (y1 - y0) * scale) / 2.0

    big = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(big)
    for loop, hole in sorted(polys, key=lambda p: p[1]):   # 바깥 먼저, 구멍 나중
        pts = [(((x - x0) * scale + ox) * ss, ((y - y0) * scale + oy) * ss) for x, y in loop]
        d.polygon(pts, fill=0 if hole else 255)
    alpha = big.resize((w, h), Image.LANCZOS)

    out = Image.new("RGBA", (w, h), (255, 255, 255, 0) if white else (0, 0, 0, 0))
    out.putalpha(alpha)
    return out
