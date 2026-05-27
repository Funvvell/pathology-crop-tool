"""主题加载与切换工具。"""

from __future__ import annotations

from pathlib import Path

_theme_dir: Path | None = None


def set_theme_dir(path: Path) -> None:
    global _theme_dir
    _theme_dir = path


def load_theme(name: str = "dark") -> str:
    """加载指定主题的 QSS 样式表，返回替换占位符后的完整 QSS。"""
    if _theme_dir is None:
        return ""
    qss_file = _theme_dir / f"theme_{name}.qss" if name != "dark" else _theme_dir / "theme.qss"
    if not qss_file.exists():
        qss_file = _theme_dir / "theme.qss"
    with open(qss_file, encoding="utf-8") as f:
        qss = f.read()
    return qss.replace("__THEME_DIR__", str(_theme_dir).replace("\\", "/"))
