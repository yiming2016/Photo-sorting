# -*- coding: utf-8 -*-
"""由 logo.svg 渲染多尺寸 .ico（用于 exe 图标与界面内 logo）。"""
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent
SRC = HERE / "logo.svg"
OUT = HERE / "logo.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    renderer = QSvgRenderer(str(SRC))
    frames = []
    for s in SIZES:
        img = QImage(QSize(s, s), QImage.Format_ARGB32)
        img.fill(0)
        painter = QPainter(img)
        renderer.render(painter)
        painter.end()
        tmp = HERE / f"_tmp_{s}.png"
        img.save(str(tmp))
        frames.append((s, Image.open(tmp).convert("RGBA")))
        tmp.unlink()
    frames.sort(key=lambda x: x[0])
    # 用最大尺寸生成多分辨率 ico (PIL 会按 sizes 自动缩放)
    frames[-1][1].save(str(OUT), format="ICO", sizes=[(s, s) for s, _ in frames])
    print("wrote", OUT, "sizes", [s for s, _ in frames])


if __name__ == "__main__":
    main()
