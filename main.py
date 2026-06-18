"""病理裁剪工具 入口点"""

import ctypes
import logging
import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtSvg import QSvgRenderer
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
    # 配置日志：写入用户主目录下的日志文件（便于诊断打包后的问题）
    _log_path = Path.home() / "pathology-crop-tool.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        filename=str(_log_path),
        filemode="a",
    )
    _logger = logging.getLogger("main")
    _logger.info("=" * 60)
    _logger.info("病理裁剪工具启动")
    _logger.info("=" * 60)

    # 捕获未处理异常，写入日志（PyInstaller console=False 时异常不会显示）
    def _log_uncaught(exc_type, exc_value, exc_tb):
        _logger.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _log_uncaught

    # 让 Windows 任务栏使用窗口图标而非默认 exe 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PathologyCropTool")

    app = QApplication(sys.argv)
    app.setApplicationName("病理裁剪工具")
    app.setOrganizationName("病理裁剪工具")

    # 运行时路径：PyInstaller 打包后使用 sys._MEIPASS
    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))

    # 应用图标：优先 SVG（矢量清晰），fallback 到 ICO
    icon = QIcon()
    svg_path = base_dir / "icon.svg"
    ico_path = base_dir / "icon.ico"
    if svg_path.exists():
        renderer = QSvgRenderer(str(svg_path))
        for size in (16, 32, 48, 64, 128, 256):
            pix = QPixmap(size, size)
            pix.fill(Qt.GlobalColor.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(p)
            p.end()
            icon.addPixmap(pix)
    elif ico_path.exists():
        icon = QIcon(str(ico_path))
    app.setWindowIcon(icon)

    theme_dir = base_dir / "liver_portal_crop"
    set_theme_dir(theme_dir)
    _create_arrow_pixmaps(theme_dir)

    # 加载 QSS 主题
    qss = load_theme("dark")
    if qss:
        app.setStyleSheet(qss)

    window = MainWindow()
    window.setWindowIcon(icon)  # 窗口图标
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
