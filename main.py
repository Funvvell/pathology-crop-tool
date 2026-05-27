"""病理裁剪工具 入口点"""

import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtWidgets import QApplication
from liver_portal_crop.app import MainWindow
from liver_portal_crop.theme import load_theme, set_theme_dir


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
        from PySide6.QtCore import QPoint
        p.drawPolygon([QPoint(*pt) for pt in points])
        p.end()
        pix.save(str(path))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("病理裁剪工具")
    app.setOrganizationName("病理裁剪工具")

    # 运行时路径：PyInstaller 打包后使用 sys._MEIPASS
    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))

    # 应用图标
    icon_path = base_dir / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    theme_dir = base_dir / "liver_portal_crop"
    set_theme_dir(theme_dir)
    _create_arrow_pixmaps(theme_dir)

    # 加载 QSS 主题
    qss = load_theme("dark")
    if qss:
        app.setStyleSheet(qss)

    window = MainWindow()
    window.setWindowIcon(QIcon(str(icon_path)))  # 窗口图标
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
