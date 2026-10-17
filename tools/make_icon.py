# -*- coding: utf-8 -*-
"""
生成 assets/app.ico —— 用纯标准库（无需 Pillow）绘制一个多尺寸应用图标。

图标内容：蓝色圆角渐变底 + 白色八分音符（表示“音频”）。
用法：  python tools/make_icon.py
"""

import os
import struct

SIZES = (16, 32, 48, 64, 128, 256)
SS = 4  # 超采样倍数，用于抗锯齿


# ----------------------------------------------------------------- 形状判定
def in_rounded_square(x, y, r=0.24):
    """归一化坐标 [0,1]^2 内是否落在圆角正方形内。"""
    if x < r and y < r:
        return (x - r) ** 2 + (y - r) ** 2 <= r * r
    if x > 1 - r and y < r:
        return (x - (1 - r)) ** 2 + (y - r) ** 2 <= r * r
    if x < r and y > 1 - r:
        return (x - r) ** 2 + (y - (1 - r)) ** 2 <= r * r
    if x > 1 - r and y > 1 - r:
        return (x - (1 - r)) ** 2 + (y - (1 - r)) ** 2 <= r * r
    return True


def in_ellipse(x, y, cx, cy, rx, ry):
    return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0


def in_rect(x, y, x0, x1, y0, y1):
    return x0 <= x <= x1 and y0 <= y <= y1


def in_poly(x, y, pts):
    """射线法判断点是否在多边形内。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def sample(x, y):
    """返回归一化坐标处的 (r, g, b, a)。"""
    if not in_rounded_square(x, y):
        return (0, 0, 0, 0)

    # 背景：自上而下的蓝色渐变
    t = y
    r = int(59 + (29 - 59) * t)
    g = int(130 + (78 - 130) * t)
    b = int(246 + (216 - 246) * t)

    # 白色八分音符：符头 + 符干 + 符尾
    note = (
        in_ellipse(x, y, 0.35, 0.71, 0.160, 0.120)
        or in_rect(x, y, 0.465, 0.545, 0.18, 0.71)
        or in_poly(x, y, [(0.545, 0.18), (0.755, 0.28), (0.545, 0.37)])
    )
    if note:
        r = g = b = 255

    return (r, g, b, 255)


# ----------------------------------------------------------------- 渲染
def render(size):
    """用超采样渲染出 size×size 的 RGBA 像素（按行，自上而下）。"""
    big = size * SS
    grid = [[(0, 0, 0, 0)] * big for _ in range(big)]
    for j in range(big):
        y = (j + 0.5) / big
        row = grid[j]
        for i in range(big):
            row[i] = sample((i + 0.5) / big, y)

    out = []
    n = SS * SS
    for j in range(size):
        row = []
        for i in range(size):
            ar = ag = ab = aa = 0
            for dj in range(SS):
                for di in range(SS):
                    r, g, b, a = grid[j * SS + dj][i * SS + di]
                    ar += r * a
                    ag += g * a
                    ab += b * a
                    aa += a
            if aa:
                row.append((ar // aa, ag // aa, ab // aa, aa // n))
            else:
                row.append((0, 0, 0, 0))
        out.append(row)
    return out


def bmp_payload(rows, size):
    """构造 ICO 内嵌的 BMP（BITMAPINFOHEADER + BGRA XOR 位图 + AND 掩码）。"""
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )
    xor = bytearray()
    for j in range(size - 1, -1, -1):          # BMP 自下而上
        for i in range(size):
            r, g, b, a = rows[j][i]
            xor += bytes((b, g, r, a))
    row_bytes = ((size + 31) // 32) * 4
    and_mask = bytes(row_bytes * size)         # 全 0，透明度交给 alpha 通道
    return header + bytes(xor) + and_mask


def build_ico(path):
    images = [(s, bmp_payload(render(s), s)) for s in SIZES]

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(images))   # ICONDIR
    offset = 6 + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _size, data in images:
        out += data

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(out)
    print(f"已生成图标：{path}（{len(images)} 个尺寸，{len(out)} 字节）")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    build_ico(os.path.join(here, "..", "assets", "app.ico"))
