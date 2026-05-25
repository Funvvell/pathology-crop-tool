"""病理裁剪工具 入口点"""

import sys
import os
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtCore import Qt
from liver_portal_crop.app import MainWindow


def _create_arrow_pixmaps(theme_dir: Path):
    """生成上下箭头 PNG 供 QSS 引用。"""
    size = 16
    for direction, filename, points in [
        ("up", "arrow_up.png", [(8, 3), (3, 11), (13, 11)]),
        ("down", "arrow_down.png", [(8, 11), (3, 3), (13, 3)]),
    ]:
        path = theme_dir / filename
        if path.exists():
            continue
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor("#c1c2c5")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon([QtCore.QPoint(*pt) for pt in points])
        p.end()
        pix.save(str(path))


# 引入 QtCore 用于 QPoint
from PySide6 import QtCore


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("病理裁剪工具")
    app.setOrganizationName("病理裁剪工具")

    theme_dir = Path(__file__).parent / "liver_portal_crop"
    _create_arrow_pixmaps(theme_dir)

    # 加载 QSS 主题
    qss_path = theme_dir / "theme.qss"
    if qss_path.exists():
        with open(qss_path, encoding="utf-8") as f:
            qss = f.read()
            # 替换路径占位符为绝对路径
            qss = qss.replace("__THEME_DIR__", str(theme_dir).replace("\\", "/"))
            app.setStyleSheet(qss)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
