"""主题加载与切换工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_theme_dir: Path | None = None
_current_theme: str = "dark"

# 运行时着色（自定义 paint / 动态状态色）；与 QSS token 保持一致
_TOKENS: dict[str, dict[str, str]] = {
    "dark": {
        "accent": "#0A84FF",
        "check_on": "#0A84FF",
        "check_off_bg": "#2A2A31",
        "check_off_fg": "#8E8E96",
        "check_off_pen": "#4A4A54",
        "hint": "rgba(240, 240, 244, 0.55)",
        "success": "#30D158",
        "warning": "#FF9F0A",
        "error": "#FF453A",
        "card_selected_bg": "rgba(10, 132, 255, 0.15)",
        "card_selected_border": "#0A84FF",
        # 病理切片预览区固定深底，保证 ROI 描边对比
        "viewer_bg": "#0F0F11",
    },
    "light": {
        "accent": "#0071E3",
        "check_on": "#0071E3",
        "check_off_bg": "#E8E9ED",
        "check_off_fg": "#6C6C76",
        "check_off_pen": "#C7C7D0",
        "hint": "#6C6C76",
        "success": "#248A3D",
        "warning": "#C93400",
        "error": "#D70015",
        "card_selected_bg": "rgba(0, 113, 227, 0.15)",
        "card_selected_border": "#0071E3",
        "viewer_bg": "#0F0F11",
    },
}


def set_theme_dir(path: Path) -> None:
    global _theme_dir
    _theme_dir = path


def current_theme() -> str:
    return _current_theme


def set_current_theme(name: str) -> None:
    global _current_theme
    _current_theme = name if name in _TOKENS else "dark"


def token(key: str) -> str:
    theme = _TOKENS.get(_current_theme, _TOKENS["dark"])
    return theme.get(key, _TOKENS["dark"].get(key, ""))


def apply_object_name(widget: Any, object_name: str) -> None:
    """运行时切换 objectName，并强制重新应用应用级 QSS。"""
    widget.setObjectName(object_name)
    widget.setStyleSheet("")
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


def load_theme(name: str = "dark") -> str:
    """加载指定主题的 QSS 样式表，返回替换占位符后的完整 QSS。"""
    if _theme_dir is None:
        return ""
    qss_file = _theme_dir / f"theme_{name}.qss" if name != "dark" else _theme_dir / "theme.qss"
    if not qss_file.exists():
        qss_file = _theme_dir / "theme.qss"
        name = "dark"
    with open(qss_file, encoding="utf-8") as f:
        qss = f.read()
    set_current_theme(name)
    return qss.replace("__THEME_DIR__", str(_theme_dir).replace("\\", "/"))
