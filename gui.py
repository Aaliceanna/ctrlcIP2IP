# -*- coding: utf-8 -*-
"""ClipShare 系统托盘。

依赖 pystray + Pillow（打包进 exe；源码运行需 pip install pystray pillow）。
未安装依赖时 available() 返回 False，调用方应退化为无托盘常驻模式。
"""

from __future__ import annotations


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw  # noqa: F401

        return True
    except Exception:
        return False


def _icon_image(paused: bool = False):
    from PIL import Image, ImageDraw

    bg = (156, 163, 175) if paused else (47, 111, 237)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=15, fill=bg)
    # 剪贴板轮廓 + 两行内容
    d.rounded_rectangle([21, 15, 43, 49], radius=4, outline=(255, 255, 255, 255), width=4)
    d.rectangle([16, 19, 48, 25], fill=bg)
    d.rounded_rectangle([25, 12, 39, 19], radius=3, fill=(255, 255, 255, 255))
    d.rectangle([27, 29, 37, 32], fill=(255, 255, 255, 255))
    d.rectangle([27, 37, 37, 40], fill=(255, 255, 255, 255))
    return img


class TrayApp:
    """hooks 需要提供：title/status/open_ui/paused/toggle_pause/
    debug/toggle_debug/autostart/toggle_autostart/copy_link/open_log/shutdown。"""

    def __init__(self, hooks: dict) -> None:
        self.h = hooks
        self.icon = self._build_icon()

    def _build_icon(self):
        import pystray
        from pystray import Menu, MenuItem

        menu = Menu(
            MenuItem(lambda i: self.h["title"](), None, enabled=False),
            MenuItem(lambda i: self.h["status"](), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("打开设置面板…", self._open_ui, default=True),
            MenuItem("启用同步", self._toggle_pause, checked=lambda i: not self.h["paused"]()),
            MenuItem("详细日志", self._toggle_debug, checked=lambda i: self.h["debug"]()),
            Menu.SEPARATOR,
            MenuItem("复制我的配对链接", self._copy_link),
            MenuItem(lambda i: self.h["fw_label"](), self._toggle_fw,
                     checked=lambda i: self.h["fw_open"]()),
            MenuItem("打开日志文件", self._open_log),
            MenuItem("开机自启", self._toggle_autostart, checked=lambda i: self.h["autostart"]()),
            Menu.SEPARATOR,
            MenuItem("退出 ClipShare", self._quit),
        )
        return pystray.Icon(
            "clipshare", _icon_image(self.h["paused"]()), self.h["title"](), menu
        )

    # ---- 内部 ----
    def _refresh_icon(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.icon = _icon_image(self.h["paused"]())
            self.icon.title = self.h["title"]()
        except Exception:
            pass

    # ---- 菜单动作 ----
    def _open_ui(self, icon=None, item=None) -> None:
        self.h["open_ui"]()

    def _toggle_pause(self, icon=None, item=None) -> None:
        self.h["toggle_pause"]()
        self._refresh_icon()

    def _toggle_debug(self, icon=None, item=None) -> None:
        self.h["toggle_debug"]()

    def _toggle_autostart(self, icon=None, item=None) -> None:
        self.h["toggle_autostart"]()

    def _toggle_fw(self, icon=None, item=None) -> None:
        msg = self.h["toggle_fw"]()
        self._notify(msg)

    def _copy_link(self, icon=None, item=None) -> None:
        link = self.h["copy_link"]()
        self._notify(f"已复制配对链接\n{link}")

    def _open_log(self, icon=None, item=None) -> None:
        self.h["open_log"]()

    def _quit(self, icon=None, item=None) -> None:
        try:
            self.h["shutdown"]()
        finally:
            if self.icon is not None:
                try:
                    self.icon.stop()
                except Exception:
                    pass

    def _notify(self, msg: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(msg, "ClipShare")
        except Exception:
            pass

    # ---- 启动 ----
    def run(self) -> None:
        if self.icon is not None:
            self.icon.run()
