#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ClipShare —— 通过指定 IP 的端到端剪贴板共享。

特性:
  * Ctrl+C 之后自动把剪贴板文本推送到已绑定 IP 的对端（序列号监听，非轮询内容）
  * 对端剪贴板变化也会回推过来，双向同步，自动防止回环
  * 绑定方式为一条「配对链接」: clipshare://IP:PORT/TOKEN?name=主机名
  * 只认具体 IP，不使用 mDNS / 广播发现
  * 纯 Python 标准库实现，Windows 剪贴板通过 ctypes 直调 Win32 API
  * 支持开机自启（HKCU Run 键，无需管理员）

常用命令:
  python clipshare.py install            # 安装开机自启并立即后台启动
  python clipshare.py link               # 打印本机配对链接（--wan 取公网 IP）
  python clipshare.py bind "<链接>"       # 绑定对端
  python clipshare.py peers              # 查看已绑定对端
  python clipshare.py status             # 查看运行状态
  python clipshare.py send "文本"         # 写入本机剪贴板并推送给所有对端
  python clipshare.py stop / restart / uninstall
"""

from __future__ import annotations

import argparse
import atexit
import collections
import ctypes
import hmac
import json
import locale
import logging
import logging.handlers
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.3.0"
APP_NAME = "ClipShare"
IS_WIN = sys.platform.startswith("win")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipShare"

try:  # 设置面板 HTML（缺失时降级，不影响同步功能）
    from webui import HTML_UI
except Exception:  # pragma: no cover
    HTML_UI = "<h1>设置面板缺失：请把 webui.py 放在同一目录</h1>"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def app_home() -> str:
    d = os.environ.get("CLIPSHARE_HOME")
    if not d:
        if IS_WIN:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "share"
            )
        d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


HOME = app_home()
CONFIG_PATH = os.path.join(HOME, "config.json")
LOG_PATH = os.path.join(HOME, "clipshare.log")

log = logging.getLogger("clipshare")


def setup_logging(silent: bool) -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if not silent:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        log.addHandler(sh)


# --------------------------------------------------------------------------
# Windows 剪贴板（ctypes 直调，零依赖）
# --------------------------------------------------------------------------
class WinClipboard:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u.OpenClipboard.argtypes = [ctypes.c_void_p]
        self.u.OpenClipboard.restype = ctypes.c_bool
        self.u.CloseClipboard.restype = ctypes.c_bool
        self.u.EmptyClipboard.restype = ctypes.c_bool
        self.u.GetClipboardData.argtypes = [ctypes.c_uint]
        self.u.GetClipboardData.restype = ctypes.c_void_p
        self.u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        self.u.SetClipboardData.restype = ctypes.c_void_p
        self.u.GetClipboardSequenceNumber.restype = ctypes.c_uint
        self.k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        self.k.GlobalAlloc.restype = ctypes.c_void_p
        self.k.GlobalLock.argtypes = [ctypes.c_void_p]
        self.k.GlobalLock.restype = ctypes.c_void_p
        self.k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.k.GlobalUnlock.restype = ctypes.c_bool
        self.k.GlobalFree.argtypes = [ctypes.c_void_p]
        self.k.GlobalFree.restype = ctypes.c_void_p

    def seq(self) -> int:
        try:
            return int(self.u.GetClipboardSequenceNumber())
        except Exception:
            return 0

    def _open(self, retries: int = 25, delay: float = 0.02) -> bool:
        for _ in range(retries):
            if self.u.OpenClipboard(None):
                return True
            time.sleep(delay)
        return False

    def read(self):
        if not self._open():
            return None
        try:
            h = self.u.GetClipboardData(self.CF_UNICODETEXT)
            if not h:
                return None
            p = self.k.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def write(self, text: str) -> bool:
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not self._open():
            return False
        ok = False
        try:
            self.u.EmptyClipboard()
            h = self.k.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
            if not h:
                return False
            p = self.k.GlobalLock(h)
            if not p:
                self.k.GlobalFree(h)
                return False
            ctypes.memmove(p, data, len(data))
            self.k.GlobalUnlock(h)
            if self.u.SetClipboardData(self.CF_UNICODETEXT, h):
                ok = True  # 成功后句柄归系统所有，不能 GlobalFree
            else:
                self.k.GlobalFree(h)
        finally:
            self.u.CloseClipboard()
        return ok


class PosixClipboard:
    """Linux 后端：通过 wl-copy/wl-paste（Wayland）或 xclip/xsel（X11）读写剪贴板。

    没有系统级序列号，用「按内容变化自增计数」模拟 seq()，语义与 Windows 版一致。
    """

    def __init__(self) -> None:
        import shutil

        self._counter = 0
        self._cached = None
        self._read_cmd = None
        self._write_cmd = None
        self.tool = ""

        wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        has_wl = shutil.which("wl-paste") and shutil.which("wl-copy")
        has_xclip = shutil.which("xclip")
        has_xsel = shutil.which("xsel")

        if wayland and has_wl:
            self.tool = "wl-clipboard"
            self._read_cmd = ["wl-paste", "--no-newline"]
            self._write_cmd = ["wl-copy"]
        elif has_xclip:
            self.tool = "xclip"
            self._read_cmd = ["xclip", "-selection", "clipboard", "-o"]
            self._write_cmd = ["xclip", "-selection", "clipboard", "-i"]
        elif has_xsel:
            self.tool = "xsel"
            self._read_cmd = ["xsel", "--clipboard", "--output"]
            self._write_cmd = ["xsel", "--clipboard", "--input"]
        elif has_wl:
            self.tool = "wl-clipboard"
            self._read_cmd = ["wl-paste", "--no-newline"]
            self._write_cmd = ["wl-copy"]
        else:
            raise SystemExit(
                "未找到剪贴板工具。请先安装：\n"
                "  Debian/Ubuntu : sudo apt install xclip        （Wayland: wl-clipboard）\n"
                "  Fedora        : sudo dnf install xclip\n"
                "  Arch          : sudo pacman -S xclip"
            )

    def _raw_read(self):
        import subprocess as sp

        try:
            r = sp.run(self._read_cmd, capture_output=True, timeout=3)
            if r.returncode != 0:
                return None
            return r.stdout.decode("utf-8", "replace")
        except Exception:
            return None

    def seq(self) -> int:
        text = self._raw_read()
        if text != self._cached:
            self._cached = text
            self._counter += 1
        return self._counter

    def read(self):
        self.seq()  # 顺带刷新缓存
        return self._cached

    def write(self, text: str) -> bool:
        import subprocess as sp

        try:
            r = sp.run(self._write_cmd, input=text.encode("utf-8"), timeout=5)
            if r.returncode == 0:
                self._cached = text
                self._counter += 1
                return True
            return False
        except Exception:
            return False


CLIP = None


def clipboard():
    global CLIP
    if CLIP is None:
        CLIP = WinClipboard() if IS_WIN else PosixClipboard()
    return CLIP


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def default_config() -> dict:
    return {
        "name": socket.gethostname(),
        "bind": "0.0.0.0",   # 监听地址；可指定具体 IP，如 192.168.2.65
        "port": 8765,
        "advertise": "",      # 对外公布地址（写进配对链接）；留空=自动取本机 IP
        "port_auto": True,    # 端口被占用时自动改用其它空闲端口
        "token": secrets.token_hex(16),
        "peers": [],          # [{"name","host","port","token"}]
        "max_kb": 512,
        "poll_ms": 200,
        "sync_files": False,
        "log_level": "info",   # info | debug（托盘/面板可切换）
        "fw_autocheck": True,  # 绑定/启动时自动检测防火墙与连通性
        "fw_autoopen": False,  # 检测到被拦截时自动放行（False=只在用户点确认后放行）
        "fw_mode": "temp",     # temp=退出/闪退自动恢复；persist=常驻规则
    }


def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception as exc:  # 配置损坏时不让服务起不来
            log.error("配置文件解析失败，将重建: %s", exc)
            cfg = {}
    base = default_config()
    changed = not os.path.exists(CONFIG_PATH)
    for k, v in base.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        changed = True
    cfg["port"] = int(cfg["port"])
    cfg["max_kb"] = int(cfg["max_kb"])
    cfg["poll_ms"] = max(50, int(cfg["poll_ms"]))
    for p in cfg.get("peers") or []:
        p["port"] = int(p.get("port") or 8765)
    # 监听地址 / 对外公布地址：统一规整，避免空串或 "auto" 导致链接异常
    cfg["bind"] = str(cfg.get("bind") or "0.0.0.0").strip() or "0.0.0.0"
    adv = str(cfg.get("advertise") or "").strip()
    cfg["advertise"] = "" if adv.lower() in ("auto", "自动") else adv
    cfg["port_auto"] = bool(cfg.get("port_auto", True))
    cfg["fw_autocheck"] = bool(cfg.get("fw_autocheck", True))
    cfg["fw_autoopen"] = bool(cfg.get("fw_autoopen", False))
    cfg["fw_mode"] = "persist" if str(cfg.get("fw_mode", "temp")).lower() == "persist" else "temp"
    if not (1 <= cfg["port"] <= 65535):
        cfg["port"] = 8765
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_mtime() -> float:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------
# HTTP 小工具（禁用系统代理，避免局域网请求被 Clash 之类接管）
# --------------------------------------------------------------------------
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def peer_base(p: dict) -> str:
    return f"http://{host_for_url(str(p['host']))}:{int(p['port'])}"


def http_json(url: str, payload=None, token: str = "", timeout: float = 5.0):
    headers = {"X-Token": token or "", "User-Agent": f"ClipShare/{VERSION}"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read(8 * 1024 * 1024)
        return r.status, json.loads(raw.decode("utf-8") or "{}")


# --------------------------------------------------------------------------
# 核心
# --------------------------------------------------------------------------
def apply_log_level(cfg: dict) -> None:
    lvl = logging.DEBUG if str(cfg.get("log_level", "info")).lower() == "debug" else logging.INFO
    log.setLevel(lvl)
    for h in log.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.handlers.RotatingFileHandler
        ):
            h.setLevel(lvl)


def now_ms() -> int:
    return int(time.time() * 1000)


class Core:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.started_at = time.time()
        self.lock = threading.RLock()
        self.seen = collections.deque(maxlen=1024)
        self.last_seq = clipboard().seq()
        self.last_text = clipboard().read() or ""
        self.latest = None
        self.push_count = 0
        self.recv_count = 0
        self.last_push_at = 0
        self.last_recv_at = 0
        self.suppress_until = 0.0
        self.stop_event = threading.Event()
        self.paused = False
        self.peer_state: dict = {}
        self.cfg_mtime = config_mtime()
        self.pusher = Pusher(self)

    # ---- 热重载配置（bind / unbind / config 之后即时生效） ----
    def reload(self) -> bool:
        try:
            new_cfg = load_config()
        except Exception as exc:
            log.warning("配置重载失败: %s", exc)
            return False
        with self.lock:
            self.cfg = new_cfg
            self.cfg_mtime = config_mtime()
        log.info("配置已重载：对端 %d 个", len(new_cfg.get("peers") or []))
        return True

    # ---- 收到对端剪贴板 ----
    def apply_remote(self, clip: dict) -> str:
        if self.paused:
            return "paused"
        text = clip.get("text")
        if not isinstance(text, str) or text == "":
            return "empty"
        if len(text.encode("utf-8")) > self.cfg["max_kb"] * 1024:
            return "too_large"
        cid = clip.get("id") or ""
        with self.lock:
            if cid and cid in self.seen:
                return "dup"
            if cid:
                self.seen.append(cid)
            if text == self.last_text:
                return "same"
            self.suppress_until = time.time() + 0.6
            ok = clipboard().write(text)
            self.last_seq = clipboard().seq()
            self.last_text = text
            self.latest = clip
            self.recv_count += 1
            self.last_recv_at = int(time.time())
        if ok:
            log.info("← 收到 %s 的剪贴板 %d 字", clip.get("from") or "?", len(text))
            return "ok"
        log.warning("剪贴板写入失败（可能被其它程序占用）")
        return "write_failed"

    # ---- 本地主动推送（含 send 命令） ----
    def broadcast(self, text: str, origin: str = "local") -> None:
        if not text:
            return
        with self.lock:
            clipboard().write(text)
            self.last_seq = clipboard().seq()
            self.last_text = text
            self.suppress_until = time.time() + 0.6
            self.push_count += 1
            self.last_push_at = int(time.time())
        payload = {
            "id": uuid.uuid4().hex,
            "text": text,
            "ts": now_ms(),
            "from": self.cfg["name"],
        }
        self.latest = payload
        self.pusher.enqueue(payload)
        log.info("→ 推送剪贴板 %d 字 (%s)", len(text), origin)


class Pusher(threading.Thread):
    def __init__(self, core: Core) -> None:
        super().__init__(daemon=True, name="pusher")
        self.core = core
        self.q: queue.Queue = queue.Queue()

    def enqueue(self, payload: dict) -> None:
        self.q.put(payload)

    def run(self) -> None:
        while not self.core.stop_event.is_set():
            try:
                payload = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            peers = list(self.core.cfg.get("peers") or [])
            if not peers:
                log.debug("没有已绑定的对端，跳过推送")
                continue
            for peer in peers:
                self._deliver(peer, payload)

    def _deliver(self, peer: dict, payload: dict) -> None:
        url = peer_base(peer) + "/api/clip"
        key = peer_key(peer)
        last_err = ""
        for attempt in range(3):
            try:
                http_json(url, payload, token=peer.get("token", ""), timeout=4.0)
                self.core.peer_state[key] = {"ok": True, "err": "", "ts": int(time.time())}
                log.debug("推送到 %s 成功", key)
                return
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
                if exc.code in (401, 403):
                    break
            except Exception as exc:
                last_err = type(exc).__name__ + (f": {exc}" if str(exc) else "")
            time.sleep(0.4 * (attempt + 1))
        self.core.peer_state[key] = {"ok": False, "err": last_err, "ts": int(time.time())}
        log.warning("推送到 %s 失败: %s", key, last_err)


def poller_loop(core: Core) -> None:
    """监听剪贴板序列号：Ctrl+C / 任意复制动作都会触发。"""
    clip = clipboard()
    last_cfg_check = 0.0
    while not core.stop_event.is_set():
        time.sleep(core.cfg["poll_ms"] / 1000.0)
        try:
            # 每秒检查一次配置文件改动 → 热加载对端
            if time.time() - last_cfg_check > 1.0:
                last_cfg_check = time.time()
                if config_mtime() != core.cfg_mtime:
                    core.reload()
            seq = clip.seq()
            if seq == 0 or seq == core.last_seq:
                continue
            core.last_seq = seq
            if core.paused:
                # 暂停期间不推送，但记录内容，恢复后不会补发历史
                core.last_text = clip.read() or core.last_text
                continue
            if time.time() < core.suppress_until:
                continue  # 自己刚写入的，不回推
            text = clip.read()
            if not text:
                continue  # 图片 / 文件等非文本
            if len(text.encode("utf-8")) > core.cfg["max_kb"] * 1024:
                log.debug("内容超过 %d KB，跳过", core.cfg["max_kb"])
                continue
            with core.lock:
                if text == core.last_text:
                    continue
                core.last_text = text
                core.push_count += 1
                core.last_push_at = int(time.time())
            payload = {
                "id": uuid.uuid4().hex,
                "text": text,
                "ts": now_ms(),
                "from": core.cfg["name"],
            }
            core.latest = payload
            core.pusher.enqueue(payload)
            log.info("→ 检测到复制，推送 %d 字", len(text))
        except Exception as exc:
            log.debug("轮询异常: %s", exc)


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
CORE: Core | None = None
SERVER = None
TRAY = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ClipShare/{VERSION}"
    sys_version = ""
    timeout = 20

    def log_message(self, fmt, *args):
        log.debug("http " + (fmt % args))

    # ---- 基础 ----
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        got = self.headers.get("X-Token", "")
        want = CORE.cfg["token"] if CORE else ""
        try:
            return hmac.compare_digest(got, want)
        except Exception:
            return False

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        limit = (CORE.cfg["max_kb"] * 1024 * 4) if CORE else 4 * 1024 * 1024
        if n <= 0 or n > limit:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _common(self) -> dict:
        return {
            "ok": True,
            "name": CORE.cfg["name"],
            "version": VERSION,
            "port": CORE.cfg["port"],
            "ts": now_ms(),
        }

    def _html(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path)

        # 设置面板：令牌通过查询参数带入（本地访问，便于浏览器直接打开）
        if path.path in ("/", "/ui", "/ui/"):
            qs = urllib.parse.parse_qs(path.query)
            t = (qs.get("t") or [""])[0]
            try:
                ok = hmac.compare_digest(t, CORE.cfg["token"])
            except Exception:
                ok = False
            if not ok:
                return self._html("<h1>401</h1><p>令牌无效，请从托盘菜单重新打开设置面板。</p>", 401)
            return self._html(HTML_UI)

        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if path.path in ("/api/ping", "/api/info"):
            return self._json(200, self._common())
        if path.path == "/api/clip":
            qs = urllib.parse.parse_qs(path.query)
            since = int(qs.get("since", ["0"])[0] or 0)
            clip = CORE.latest
            return self._json(200, {"ok": True, "clip": clip if clip and clip["ts"] > since else None})
        if path.path == "/api/status":
            return self._json(200, self._status())
        if path.path == "/api/mylink":
            return self._json(
                200,
                {
                    "ok": True,
                    "link": build_link(CORE.cfg),
                    "host": advertise_host(CORE.cfg),
                    "port": CORE.cfg["port"],
                },
            )
        if path.path == "/api/network":
            cfg = CORE.cfg
            return self._json(
                200,
                {
                    "ok": True,
                    "bind": cfg["bind"],
                    "port": int(cfg["port"]),
                    "advertise": cfg.get("advertise", ""),
                    "port_auto": bool(cfg.get("port_auto", True)),
                    "auto_host": advertise_host({"advertise": ""}),
                    "candidates": local_ipv4_list(),
                    "listen": list(SERVER.server_address[:2]) if SERVER else None,
                    "link": build_link(cfg),
                },
            )
        if path.path == "/api/log":
            qs = urllib.parse.parse_qs(path.query)
            try:
                n = int((qs.get("lines") or ["200"])[0])
            except ValueError:
                n = 200
            return self._json(200, {"ok": True, "text": tail_log(n)})
        if path.path == "/api/config":
            cfg = CORE.cfg
            return self._json(
                200,
                {
                    "ok": True,
                    "config": {
                        "name": cfg["name"],
                        "bind": cfg["bind"],
                        "port": cfg["port"],
                        "advertise": cfg.get("advertise", ""),
                        "port_auto": bool(cfg.get("port_auto", True)),
                        "max_kb": cfg["max_kb"],
                        "poll_ms": cfg["poll_ms"],
                        "log_level": cfg.get("log_level", "info"),
                        "fw_autocheck": bool(cfg.get("fw_autocheck", True)),
                        "fw_autoopen": bool(cfg.get("fw_autoopen", False)),
                        "fw_mode": cfg.get("fw_mode", "temp"),
                    },
                    "autostart": autostart_installed(),
                },
            )
        if path.path == "/api/reload":
            ok = CORE.reload()
            return self._json(200, {"ok": ok, "peers": len(CORE.cfg.get("peers") or [])})
        if path.path == "/api/firewall":
            return self._json(200, fw_check())
        if path.path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=_shutdown, daemon=True).start()
            return
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/clip":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "bad_json"})
            res = CORE.apply_remote(body)
            code = 200 if res in ("ok", "same", "dup", "paused") else 400
            return self._json(code, {"ok": res in ("ok", "same", "dup", "paused"), "result": res})

        if path == "/api/broadcast":
            body = self._body()
            if not isinstance(body, dict) or not isinstance(body.get("text"), str):
                return self._json(400, {"ok": False, "error": "bad_json"})
            CORE.broadcast(body["text"], origin="cli")
            return self._json(200, {"ok": True, "peers": len(CORE.cfg.get("peers") or [])})

        if path == "/api/bind":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            if body.get("link"):
                try:
                    peer = parse_link(str(body["link"]))
                except ValueError as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
            else:
                host = str(body.get("host") or "").strip()
                token = str(body.get("token") or "").strip()
                if not host or not token:
                    return self._json(400, {"ok": False, "error": "IP 和令牌必填"})
                try:
                    port = int(body.get("port") or 8765)
                except (TypeError, ValueError):
                    port = 8765
                peer = {
                    "host": host,
                    "port": port,
                    "token": token,
                    "name": str(body.get("name") or host),
                }
            reverse = bool(body.get("reverse", True))
            updated = upsert_peer(CORE.cfg, peer)
            CORE.reload()
            ok, detail = handshake(peer)
            log.info("绑定对端 %s:%s（%s）→ %s", peer["host"], peer["port"],
                     "更新" if updated else "新增", "成功" if ok else detail)

            # 双向：让对方也把我加进去，一次绑定两边互通
            rev: dict = {"ok": False, "skipped": True, "error": ""}
            if reverse and ok:
                rev = reverse_bind(CORE.cfg, peer)
                if rev.get("ok"):
                    log.info("双向绑定成功：对方已把本机加入对端列表（%s）", rev.get("detail") or "ok")
                elif not rev.get("skipped"):
                    log.warning("对方未能回绑本机：%s", rev.get("error"))
                for p in CORE.cfg.get("peers") or []:
                    if peer_key(p) == peer_key(peer):
                        p["mutual"] = bool(rev.get("ok"))
                        save_config(CORE.cfg)
                        break

            # 需要时给出防火墙体检结果，让面板可以提示一键放行
            fw = None
            if CORE.cfg.get("fw_autocheck", True) and (not ok or not rev.get("ok")):
                fw = fw_check()
                if fw.get("state") == "blocked" and CORE.cfg.get("fw_autoopen", False):
                    opened = fw_open(int(CORE.cfg["port"]), CORE.cfg.get("fw_mode", "temp"))
                    fw["auto_opened"] = opened
                    if opened.get("ok"):
                        ok2, detail2 = handshake(peer)
                        ok, detail = ok2, detail2
                        if ok and reverse:
                            rev = reverse_bind(CORE.cfg, peer)
                            for p in CORE.cfg.get("peers") or []:
                                if peer_key(p) == peer_key(peer):
                                    p["mutual"] = bool(rev.get("ok"))
                                    save_config(CORE.cfg)
                                    break

            return self._json(
                200,
                {
                    "ok": ok,
                    "peer": f"{peer['name']} {peer_key(peer)}",
                    "updated": updated,
                    "error": "" if ok else detail,
                    "reverse": rev,
                    "mutual": bool(rev.get("ok")),
                    "firewall": fw,
                },
            )

        if path == "/api/peer":
            """对端主动回绑 / 主动解除：实现一次绑定两边互通。"""
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            src = (self.client_address[0] if self.client_address else "") or ""

            # ---- 解绑通知 ----
            if body.get("remove"):
                host = str(body.get("host") or "").strip()
                try:
                    port = int(body.get("port") or 0)
                except (TypeError, ValueError):
                    port = 0
                peers = CORE.cfg.get("peers") or []
                keep = []
                for p in peers:
                    ph = str(p.get("host") or "").strip("[]")
                    pp = int(p.get("port") or 0)
                    hit = (host and ph == host and (not port or pp == port)) or \
                          (src and not is_loopback(src) and ph == src and (not port or pp == port))
                    if not hit:
                        keep.append(p)
                n = len(peers) - len(keep)
                if n:
                    CORE.cfg["peers"] = keep
                    save_config(CORE.cfg)
                    CORE.reload()
                    log.info("对端通知解绑，已移除 %d 个对端", n)
                return self._json(200, {"ok": True, "removed": n})

            # ---- 回绑 ----
            me = body.get("peer") if isinstance(body.get("peer"), dict) else {}
            try:
                port = int(me.get("port") or 8765)
            except (TypeError, ValueError):
                port = 8765
            token = str(me.get("token") or "").strip()
            name = str(me.get("name") or "").strip() or src
            # 用实际连过来的 IP：对端上报的地址可能是 Clash TUN 之类的假地址
            host = src if (src and not is_loopback(src)) else str(me.get("host") or src).strip()
            if not host:
                return self._json(400, {"ok": False, "error": "拿不到对端地址"})
            if not token:
                return self._json(400, {"ok": False, "error": "对端令牌为空"})

            # 不把自己加为对端
            if host in local_ipv4_list() and int(port) == int(CORE.cfg["port"]):
                return self._json(200, {"ok": True, "skipped": True, "added": False,
                                        "error": "对端就是本机，已跳过"})
            peer = {"host": host, "port": port, "token": token, "name": name, "mutual": True}
            updated = upsert_peer(CORE.cfg, peer)
            CORE.reload()
            ok, detail = handshake(peer, timeout=3.0)
            log.info("对端 %s 回绑本机（%s:%s）→ %s", name, host, port, "握手成功" if ok else detail)
            return self._json(
                200,
                {"ok": True, "added": not updated, "updated": updated, "skipped": False,
                 "handshake": ok, "detail": detail, "peer": f"{name} {peer_key(peer)}"},
            )

        if path == "/api/unbind":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            key = str(body.get("key") or "")
            notify = bool(body.get("notify", True))
            target = None
            for p in CORE.cfg.get("peers") or []:
                if key and key in (str(p.get("name")), str(p.get("host")), peer_key(p)):
                    target = p
                    break
            n = remove_peer(CORE.cfg, key)
            CORE.reload()
            told = {"ok": False, "skipped": True}
            if notify and target is not None:
                told = reverse_unbind(CORE.cfg, target)
                log.info("解绑已通知对端 %s（移除 %s 个）", peer_key(target), told.get("removed", 0))
            return self._json(200, {"ok": n > 0, "removed": n, "notify": told})

        if path == "/api/pause":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            CORE.paused = bool(body.get("paused"))
            log.info("同步已%s", "暂停" if CORE.paused else "恢复")
            return self._json(200, {"ok": True, "paused": CORE.paused})

        if path == "/api/config":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            if "log_level" in body:
                CORE.cfg["log_level"] = "debug" if str(body["log_level"]).lower() == "debug" else "info"
                apply_log_level(CORE.cfg)
                save_config(CORE.cfg)
                log.info("日志级别切换为 %s", CORE.cfg["log_level"])
            for key in ("fw_autocheck", "fw_autoopen"):
                if key in body:
                    CORE.cfg[key] = bool(body[key])
                    save_config(CORE.cfg)
            if "fw_mode" in body:
                CORE.cfg["fw_mode"] = "persist" if str(body["fw_mode"]).lower() == "persist" else "temp"
                save_config(CORE.cfg)
            if "autostart" in body:
                if body["autostart"]:
                    install_autostart()
                else:
                    uninstall_autostart()
            return self._json(
                200,
                {
                    "ok": True,
                    "log_level": CORE.cfg.get("log_level", "info"),
                    "autostart": autostart_installed(),
                    "fw_autocheck": bool(CORE.cfg.get("fw_autocheck", True)),
                    "fw_autoopen": bool(CORE.cfg.get("fw_autoopen", False)),
                    "fw_mode": CORE.cfg.get("fw_mode", "temp"),
                },
            )

        if path == "/api/firewall":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            action = str(body.get("action") or "check").lower()
            try:
                port = int(body.get("port") or CORE.cfg["port"])
            except (TypeError, ValueError):
                port = int(CORE.cfg["port"])
            mode = str(body.get("mode") or CORE.cfg.get("fw_mode", "temp")).lower()
            if action == "open":
                res = fw_open(port, mode)
                return self._json(200 if res.get("ok") or res.get("elevating") else 409, res)
            if action == "close":
                res = fw_close(None if body.get("port") in (None, "", 0) else port)
                return self._json(200 if res.get("ok") else 409, res)
            return self._json(200, fw_check(port))

        if path == "/api/network":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            try:
                bind = validate_bind(str(body.get("bind", CORE.cfg["bind"])))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            try:
                port = int(body.get("port", CORE.cfg["port"]))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "端口必须是数字"})
            if not (1 <= port <= 65535):
                return self._json(400, {"ok": False, "error": "端口范围 1-65535"})
            advertise = str(body.get("advertise", CORE.cfg.get("advertise", "")) or "").strip()
            if advertise.lower() in ("auto", "自动"):
                advertise = ""
            port_auto = bool(body.get("port_auto", CORE.cfg.get("port_auto", True)))
            res = rebind_service(bind, port, advertise, port_auto)
            return self._json(200 if res.get("ok") else 409, res)

        if path == "/api/reload":
            ok = CORE.reload()
            return self._json(200, {"ok": ok, "peers": len(CORE.cfg.get("peers") or [])})
        if path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=_shutdown, daemon=True).start()
            return
        return self._json(404, {"ok": False, "error": "not_found"})

    def _status(self) -> dict:
        cfg = CORE.cfg
        return {
            "ok": True,
            "name": cfg["name"],
            "version": VERSION,
            "bind": f"{cfg['bind']}:{cfg['port']}",
            "port": int(cfg["port"]),
            "advertise": cfg.get("advertise", ""),
            "port_auto": bool(cfg.get("port_auto", True)),
            "link": build_link(cfg),
            "listen": SERVER.server_address[:2] if SERVER else None,
            "uptime_s": int(time.time() - CORE.started_at),
            "paused": bool(CORE.paused),
            "log_level": cfg.get("log_level", "info"),
            "autostart": autostart_installed(),
            "peers": [
                {
                    "key": peer_key(p),
                    "name": p.get("name"),
                    "host": p.get("host"),
                    "port": p.get("port"),
                    "mutual": bool(p.get("mutual")),
                    "state": CORE.peer_state.get(peer_key(p), {}),
                }
                for p in cfg.get("peers") or []
            ],
            "firewall": {
                "open": bool(fw_managed(int(cfg["port"]))),
                "mode": (fw_managed(int(cfg["port"])) or {}).get("mode", cfg.get("fw_mode", "temp")),
                "backend": fw_backend() or "none",
                "autocheck": bool(cfg.get("fw_autocheck", True)),
                "autoopen": bool(cfg.get("fw_autoopen", False)),
                "fw_mode": cfg.get("fw_mode", "temp"),
            },
            "push_count": CORE.push_count,
            "recv_count": CORE.recv_count,
            "last_push_at": CORE.last_push_at,
            "last_recv_at": CORE.last_recv_at,
            "clip_len": len(CORE.last_text or ""),
        }


def _shutdown() -> None:
    time.sleep(0.2)
    log.info("收到停止指令，退出")
    if CORE:
        CORE.stop_event.set()
    if SERVER:
        try:
            SERVER.shutdown()
        except Exception:
            pass
    if TRAY is not None:
        try:
            TRAY.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 网络辅助
# --------------------------------------------------------------------------
def local_ip(peer: str = "8.8.8.8") -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip"):
        try:
            with _OPENER.open(url, timeout=6) as r:
                ip = r.read(64).decode("utf-8", "ignore").strip()
            if ip and re.match(r"^[0-9a-fA-F:.]+$", ip):
                return ip
        except Exception:
            continue
    return ""


def local_service_addr(cfg: dict) -> str:
    b = (cfg.get("bind") or "0.0.0.0").strip()
    if b in ("0.0.0.0", "", "*", "::"):
        return "127.0.0.1"
    return b


def ip_rank(ip: str) -> int | None:
    """本机 IPv4 的可用于对外的优先级，越小越优先；None 表示不适合写进链接。

    会排除回环、链路本地，以及 Clash TUN / WireGuard 这类虚拟网卡
    （198.18.0.0/15 是 Clash TUN、100.64/10 是运营商级 NAT），
    否则自动生成的链接会指向一个对端根本连不上的地址。
    """
    try:
        a, b = (int(x) for x in str(ip).split(".")[:2])
    except Exception:
        return None
    if a in (0, 127):
        return None
    if a == 169 and b == 254:
        return None
    if a == 198 and b in (18, 19):
        return None
    if a == 100 and 64 <= b <= 127:
        return None
    if a == 192 and b == 168:
        return 0
    if a == 10:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    return 5


def local_ipv4_list() -> list[str]:
    """本机可用于对外的 IPv4，按优先级排序（去掉回环与虚拟网卡）。"""
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except Exception:
        pass
    seen: list[str] = []
    for ip in found:
        if ip and ip not in seen and ip_rank(ip) is not None:
            seen.append(ip)
    return sorted(seen, key=lambda ip: ip_rank(ip) or 99)


def can_bind(bind: str, port: int) -> bool:
    """快速探测某地址端口能否绑定（不做监听，仅占用测试）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((bind, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def pick_free_port(bind: str, start: int, span: int = 40) -> int | None:
    """从 start 开始找一个能绑定的端口；找不到就让系统随机分配一个。"""
    start = max(1, int(start))
    for port in range(start, min(65535, start + span) + 1):
        if can_bind(bind, port):
            return port
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((bind, 0))
            return int(s.getsockname()[1])
        finally:
            s.close()
    except OSError:
        return None


def validate_bind(value: str) -> str:
    """校验监听地址：空 → 0.0.0.0；否则必须是本机可绑定的 IPv4 / localhost。"""
    v = (value or "").strip()
    if v in ("", "*", "all", "全部"):
        return "0.0.0.0"
    if v in ("localhost", "127.0.0.1") or v == "0.0.0.0":
        return v
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", v):
        raise ValueError("监听地址要填 IPv4，如 0.0.0.0 / 192.168.2.65")
    if not can_bind(v, 0):
        raise ValueError(f"{v} 不是本机地址，无法监听")
    return v


def advertise_host(cfg: dict) -> str:
    """生成配对链接时使用的地址：优先用户指定，否则取本机最像局域网的那个 IP。"""
    adv = str(cfg.get("advertise") or "").strip()
    if adv:
        return adv
    ips = local_ipv4_list()
    if ips:
        return ips[0]
    return local_ip()


def service_call(cfg: dict, path: str, payload=None, timeout: float = 5.0):
    url = f"http://{host_for_url(local_service_addr(cfg))}:{cfg['port']}{path}"
    return http_json(url, payload, token=cfg["token"], timeout=timeout)


def service_alive(cfg: dict) -> bool:
    try:
        _, obj = service_call(cfg, "/api/ping", timeout=2.0)
        return bool(obj.get("ok"))
    except Exception:
        return False


def notify_reload(cfg: dict) -> bool:
    """让正在运行的进程立刻重新读取配置（令牌变更时会失败，需 restart）。"""
    try:
        _, obj = service_call(cfg, "/api/reload", payload={}, timeout=3.0)
        return bool(obj.get("ok"))
    except Exception:
        return False


# --------------------------------------------------------------------------
# 配对链接
# --------------------------------------------------------------------------
LINK_RE = re.compile(
    r"^clipshare://(?P<host>\[[0-9a-fA-F:]+\]|[^:/?#]+)(?::(?P<port>\d+))?"
    r"/(?P<token>[A-Za-z0-9_\-]+)(?:\?(?P<query>[^#]*))?$"
)


def build_link(cfg: dict, host: str | None = None) -> str:
    host = host or advertise_host(cfg)
    q = urllib.parse.urlencode({"name": cfg["name"]})
    return f"clipshare://{host_for_url(host)}:{cfg['port']}/{cfg['token']}?{q}"


def parse_link(link: str) -> dict:
    link = link.strip().strip("'\"")
    m = LINK_RE.match(link)
    if not m:
        raise ValueError(
            "链接格式不正确，应形如 clipshare://192.168.1.184:8765/令牌?name=主机名"
        )
    host = m.group("host").strip("[]")
    query = urllib.parse.parse_qs(m.group("query") or "")
    return {
        "host": host,
        "port": int(m.group("port") or 8765),
        "token": m.group("token"),
        "name": (query.get("name") or [""])[0] or host,
    }


def peer_key(p: dict) -> str:
    return f"{p.get('host')}:{int(p.get('port') or 8765)}"


def upsert_peer(cfg: dict, peer: dict) -> bool:
    """写入对端配置，返回是否为更新已有项。"""
    peers = cfg.setdefault("peers", [])
    for p in peers:
        if str(p.get("host")) == str(peer["host"]) and int(p.get("port")) == int(peer["port"]):
            p.update(peer)
            save_config(cfg)
            return True
    peers.append(peer)
    save_config(cfg)
    return False


def remove_peer(cfg: dict, key: str) -> int:
    key = (key or "").strip()
    peers = cfg.get("peers") or []
    keep = [
        p
        for p in peers
        if key
        not in (
            str(p.get("name")),
            str(p.get("host")),
            peer_key(p),
            f"{p.get('name')}@{peer_key(p)}",
        )
    ]
    cfg["peers"] = keep
    save_config(cfg)
    return len(peers) - len(keep)


def handshake(peer: dict, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        _, obj = http_json(peer_base(peer) + "/api/ping", token=peer.get("token", ""), timeout=timeout)
        return True, f"{obj.get('name')} v{obj.get('version')}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "令牌不匹配（请重新复制对方配对链接）"
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}：检查 IP/端口/防火墙/对方是否开机"


# --------------------------------------------------------------------------
# 防火墙放行：用户确认后一键开端口，软件关闭 / 闪退自动恢复
# --------------------------------------------------------------------------
# 规则命名统一为 ClipShare-<端口>-<PID>，同时把「谁开的、什么时候开的」记进
# firewall.json。下次启动（含闪退后重启）先按 PID 存活情况回收孤儿规则，
# 再兜底扫描同前缀但无记录的规则删除，保证不会在防火墙里留下洞口。
FW_PREFIX = "ClipShare"
FW_STATE_PATH = os.path.join(HOME, "firewall.json")
FW_LOCK = threading.RLock()
FW_MANAGED: dict = {}          # port -> entry（本进程开过的规则）
_CLEANUP_REGISTERED = False
_WIN_CTRL_REF = None           # 防止 ctypes 回调被垃圾回收


def _decode_out(raw: bytes) -> str:
    """netsh / ufw 的输出可能是 UTF-8，也可能是系统 ANSI（中文 GBK），逐个试。"""
    encs = ["utf-8", locale.getpreferredencoding(False) or "utf-8"]
    if IS_WIN:
        encs.append("mbcs")
    for enc in encs:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _sh(cmd: list[str], timeout: float = 12.0) -> tuple[int, str]:
    """执行命令，返回 (returncode, 合并输出)。隐藏窗口，超时不卡死。"""
    flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
    except FileNotFoundError:
        return 127, "找不到命令：" + (cmd[0] if cmd else "?")
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as exc:
        return 126, f"{type(exc).__name__}: {exc}"
    raw = (r.stdout or b"") + (b"\n" + r.stderr if r.stderr else b"")
    return r.returncode, _decode_out(raw).strip()


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if IS_WIN:
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def fw_backend() -> str:
    """返回可用的防火墙后端：netsh / ufw / firewalld / iptables / "" """
    if IS_WIN:
        return "netsh"
    for exe, name in (("ufw", "ufw"), ("firewall-cmd", "firewalld"), ("iptables", "iptables")):
        if shutil.which(exe):
            return name
    return ""


def fw_rule_name(port: int, pid: int | None = None) -> str:
    return f"{FW_PREFIX}-{int(port)}-{int(pid if pid else os.getpid())}"


# ---- 状态文件 ----
def fw_state_load() -> dict:
    try:
        with open(FW_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data.setdefault("rules", [])
    return data


def fw_state_save(st: dict) -> None:
    tmp = FW_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FW_STATE_PATH)


def fw_state_upsert(entry: dict) -> None:
    with FW_LOCK:
        st = fw_state_load()
        key = str(entry.get("name"))
        for i, e in enumerate(st["rules"]):
            if str(e.get("name")) == key or (
                int(e.get("port") or 0) == int(entry.get("port") or 0)
            ):
                st["rules"][i] = entry
                break
        else:
            st["rules"].append(entry)
        fw_state_save(st)


def fw_state_drop(entry: dict) -> None:
    with FW_LOCK:
        st = fw_state_load()
        st["rules"] = [
            e for e in st["rules"]
            if str(e.get("name")) != str(entry.get("name"))
        ]
        fw_state_save(st)


# ---- Windows 防火墙（netsh） ----
def _win_has_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _win_elevate(params: str) -> bool:
    """用 ShellExecute(runas) 弹出 UAC，请求管理员执行一条内部命令。"""
    exe = sys.executable
    args = params
    if not is_frozen():
        args = f'"{os.path.abspath(__file__)}" {params}'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        return int(rc) > 32
    except Exception:
        return False


def _win_rule_add(name: str, port: int) -> tuple[bool, str]:
    rc, out = _sh(
        [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={name}", "dir=in", "action=allow", "protocol=TCP",
            f"localport={int(port)}", "profile=any", "enable=yes",
            f"description={FW_PREFIX} temp allow TCP {int(port)}",
        ]
    )
    if rc == 0:
        return True, out
    return False, out or f"netsh 退出码 {rc}"


def _win_rule_del(name: str) -> tuple[bool, str]:
    rc, out = _sh(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
    if rc == 0:
        return True, out
    # 规则本来就不存在，也算“已恢复”
    if re.search(r"(没有与指定标准相匹配的规则|No rules match)", out or ""):
        return True, out
    return False, out or f"netsh 退出码 {rc}"


def _win_rule_exists(name: str) -> bool:
    rc, out = _sh(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"])
    return rc == 0 and name.lower() in out.lower()


def _win_rule_blocks(timeout: float = 20.0) -> list[dict]:
    rc, out = _sh(["netsh", "advfirewall", "firewall", "show", "rule", "name=all"], timeout)
    if rc != 0:
        return []
    keys = {
        "rule name": "name", "规则名称": "name",
        "enabled": "enabled", "已启用": "enabled", "启用": "enabled",
        "direction": "direction", "方向": "direction",
        "action": "action", "操作": "action",
        "localport": "localport", "本地端口": "localport",
        "protocol": "protocol", "协议": "protocol",
    }
    blocks: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        sep = ":" if ":" in line else ("：" if "：" in line else "")
        if not sep:
            continue
        k, _, v = line.partition(sep)
        key = keys.get(k.strip().lower())
        if key:
            cur[key] = v.strip()
    if cur:
        blocks.append(cur)
    return blocks


def _win_rule_names() -> list[str]:
    prefix = FW_PREFIX.lower() + "-"
    names = []
    for b in _win_rule_blocks():
        n = (b.get("name") or "").strip()
        if n.lower().startswith(prefix):
            names.append(n)
    return names


def _yes(v: str) -> bool:
    return str(v).strip().lower() in ("yes", "true", "是", "启用", "1", "on")


def _port_in_list(value: str, port: int) -> bool:
    v = (value or "").strip()
    if v.lower() in ("any", "all", "*", "任何", "全部"):
        return True
    for part in re.split(r"[,\s]+", v):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit() and int(a) <= int(port) <= int(b):
                return True
        elif part.isdigit() and int(part) == int(port):
            return True
    return False


def _win_port_allowed(port: int) -> bool:
    for b in _win_rule_blocks():
        if not _yes(b.get("enabled", "yes")):
            continue
        d = (b.get("direction") or "").lower()
        if d and d not in ("in", "inbound", "入站", "入"):
            continue
        a = (b.get("action") or "").lower()
        if a and a not in ("allow", "允许"):
            continue
        if _port_in_list(b.get("localport") or "", int(port)):
            return True
    return False


def _win_firewall_on() -> bool:
    rc, out = _sh(["netsh", "advfirewall", "show", "currentprofile"])
    if rc != 0:
        return False
    # 中文输出形如「状态                                  启用」（空格分隔，无冒号）
    m = re.search(r"(?im)^\s*(state|状态)\s*[:：]?\s*(.+?)\s*$", out)
    if not m:
        return False
    v = m.group(2).strip().lower()
    return v.startswith("on") or ("启用" in v) or ("打开" in v) or v in ("开", "yes", "true")


# ---- Linux 防火墙 ----
def _lin_cmd(port: int, add: bool) -> list[str] | None:
    be = fw_backend()
    p = f"{int(port)}/tcp"
    if be == "ufw":
        return ["ufw", "allow" if add else "delete allow", p] if add else ["ufw", "delete", "allow", p]
    if be == "firewalld":
        return ["firewall-cmd", "--add-port" if add else "--remove-port", p]
    if be == "iptables":
        verb = "-I" if add else "-D"
        return ["iptables", verb, "INPUT", "-p", "tcp", "--dport", str(int(port)), "-j", "ACCEPT"]
    return None


def _lin_apply(port: int, add: bool) -> tuple[bool, str]:
    cmd = _lin_cmd(port, add)
    if not cmd:
        return False, "未检测到 ufw / firewalld / iptables，无法自动放行"
    rc, out = _sh(cmd)
    if rc == 0:
        return True, out
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return False, out or f"退出码 {rc}"
    for tool in ("sudo", "pkexec"):
        if shutil.which(tool):
            pre = ["sudo", "-n"] if tool == "sudo" else ["pkexec"]
            rc2, out2 = _sh(pre + cmd)
            if rc2 == 0:
                return True, out2
    return False, out or f"退出码 {rc}（需要 root 权限）"


def _lin_port_allowed(port: int) -> bool:
    be = fw_backend()
    if be == "iptables":
        rc, out = _sh(["iptables", "-S", "INPUT"])
        return rc == 0 and re.search(rf"-p tcp .*--dport {int(port)} .*-j ACCEPT", out) is not None
    if be == "ufw":
        rc, out = _sh(["ufw", "status"])
        return rc == 0 and re.search(rf"^{int(port)}/tcp\s", out, re.M) is not None
    if be == "firewalld":
        rc, out = _sh(["firewall-cmd", "--list-ports"])
        return rc == 0 and f"{int(port)}/tcp" in out
    return False


# ---- 统一入口 ----
def _rule_add(port: int, name: str) -> tuple[bool, str]:
    if IS_WIN:
        return _win_rule_add(name, port)
    return _lin_apply(port, True)


def _rule_del(entry: dict) -> tuple[bool, str]:
    if IS_WIN:
        return _win_rule_del(str(entry.get("name") or fw_rule_name(entry.get("port") or 0, entry.get("pid"))))
    return _lin_apply(int(entry.get("port") or 0), False)


def fw_managed(port: int) -> dict | None:
    for e in fw_state_load()["rules"]:
        if int(e.get("port") or 0) == int(port):
            return e
    return None


def fw_check(port: int | None = None, bind: str | None = None, deep: bool = True) -> dict:
    """体检：防火墙是否开启、端口是否已放行、有没有明显的自缚手脚配置。"""
    cfg = CORE.cfg if CORE else load_config()
    port = int(port or cfg["port"])
    bind = bind or cfg.get("bind", "0.0.0.0")
    backend = fw_backend()
    hints: list[str] = []
    listening = False
    probe_host = local_service_addr({"bind": bind})
    try:
        with socket.create_connection((probe_host, port), timeout=0.6):
            listening = True
    except Exception:
        listening = bool(SERVER is not None and int(SERVER.server_address[1]) == port)

    rule = fw_managed(port)
    rule_exists = False
    if rule and IS_WIN:
        rule_exists = _win_rule_exists(str(rule.get("name")))
    elif rule:
        rule_exists = _lin_port_allowed(port)
    port_allowed = rule_exists
    firewall_on = False
    if backend == "netsh":
        firewall_on = _win_firewall_on()
        if deep and not rule_exists:
            port_allowed = _win_port_allowed(port)
    elif backend:
        firewall_on = True
        if deep and not rule_exists:
            port_allowed = _lin_port_allowed(port)

    if str(bind) in ("127.0.0.1", "::1", "localhost"):
        hints.append(f"监听地址是 {bind}，其它机器根本连不进来，建议改成 0.0.0.0 或本机内网 IP")
    if not listening:
        hints.append(f"当前没有在 {bind}:{port} 上监听到服务（服务未运行或端口已变）")
    if not backend:
        hints.append("没检测到可管理的防火墙（Windows 防火墙 / ufw / firewalld / iptables），可能装的是第三方安全软件，需要手动放行")
    elif firewall_on and not port_allowed:
        hints.append(f"防火墙已开启，但没有放行 TCP {port} 的入站规则 —— 对端很可能连不进来")
    elif firewall_on and port_allowed and not rule_exists:
        hints.append(f"TCP {port} 已有放行规则（非 ClipShare 创建），无需处理")
    elif rule_exists:
        hints.append(f"TCP {port} 已放行（规则 {rule.get('name')}，{'退出自动恢复' if rule.get('mode') != 'persist' else '常驻'}）")

    if not backend:
        state = "unknown"
    elif firewall_on and not port_allowed:
        state = "blocked"
    elif firewall_on:
        state = "ok"
    else:
        state = "ok"
    managed = []
    stale = 0
    for e in fw_state_load()["rules"]:
        alive = _pid_alive(e.get("pid"))
        dead_owner = (not alive) and int(e.get("pid") or 0) != os.getpid()
        if dead_owner:
            stale += 1
        managed.append({
            "port": int(e.get("port") or 0),
            "mode": e.get("mode", "temp"),
            "rule": e.get("name", ""),
            "alive": alive,
            "stale": dead_owner,
        })
    if stale:
        hints.append(
            f"发现 {stale} 条上次异常退出遗留的放行规则，点“恢复”即可撤销（需要管理员授权）"
        )
    return {
        "ok": True,
        "port": port,
        "bind": bind,
        "backend": backend or "none",
        "supported": bool(backend),
        "listening": listening,
        "firewall_on": firewall_on,
        "rule_exists": rule_exists,
        "port_allowed": port_allowed,
        "state": state,
        "need_admin": IS_WIN and not _win_has_admin(),
        "mode": (rule or {}).get("mode", cfg.get("fw_mode", "temp")),
        "rule": (rule or {}).get("name", ""),
        "managed": managed,
        "stale": stale,
        "hints": hints,
        "message": "；".join(hints) or f"TCP {port} 通信正常",
    }


def fw_open(port: int, mode: str = "temp", elevate: bool = True, pid: int | None = None) -> dict:
    port = int(port)
    mode = "persist" if str(mode).lower() == "persist" else "temp"
    owner = int(pid or os.getpid())
    name = fw_rule_name(port, owner)
    entry = {
        "name": name,
        "port": port,
        "pid": owner,
        "mode": mode,
        "backend": fw_backend(),
        "ts": int(time.time()),
    }
    if IS_WIN:
        ok, err = _win_rule_add(name, port)
        if not ok:
            if elevate and not _win_has_admin():
                if _win_elevate(f"fw --open --port {port} --mode {mode} --pid {os.getpid()}"):
                    threading.Thread(
                        target=_wait_elevated_rule, args=(name, port, mode), daemon=True,
                        name="fw-wait",
                    ).start()
                    return {
                        "ok": False, "elevating": True, "port": port, "mode": mode, "rule": name,
                        "error": "已弹出管理员授权窗口，请在 UAC 里点“是”；放行成功后面板会自动刷新",
                    }
                return {"ok": False, "port": port, "error": "无法请求管理员权限：" + (err or "未知原因")}
            return {"ok": False, "port": port, "error": err or "放行失败（netsh 返回非零）"}
    else:
        ok, err = _lin_apply(port, True)
        if not ok:
            return {"ok": False, "port": port, "error": err or "放行失败"}
    fw_state_upsert(entry)
    FW_MANAGED[port] = entry
    log.info("已放行 TCP %s（%s）规则=%s", port,
             "常驻" if mode == "persist" else "临时，退出自动恢复", name)
    return {"ok": True, "port": port, "mode": mode, "rule": name}


def _wait_elevated_rule(name: str, port: int, mode: str) -> None:
    """等 UAC 子进程把规则写进来（用户点授权的时间不可控，最多等 60 秒）。"""
    for _ in range(60):
        time.sleep(1.0)
        if _win_rule_exists(name):
            entry = {
                "name": name, "port": int(port), "pid": os.getpid(), "mode": mode,
                "backend": "netsh", "ts": int(time.time()),
            }
            fw_state_upsert(entry)
            FW_MANAGED[int(port)] = entry
            log.info("管理员授权成功，TCP %s 已放行（%s）", port,
                     "常驻" if mode == "persist" else "临时，退出自动恢复")
            return
    log.warning("等待管理员放行超时（TCP %s）：UAC 可能已被取消", port)


def _elevate_fw(params: str) -> bool:
    if not IS_WIN or _win_has_admin():
        return False
    return _win_elevate(params)


def fw_close(port: int | None = None) -> dict:
    """关闭放行。port 为空 = 恢复全部。删不掉时（没管理员权限）弹一次 UAC 兜底。"""
    st = fw_state_load()
    targets = [e for e in st["rules"] if port is None or int(e.get("port") or 0) == int(port)]
    if not targets:
        return {"ok": True, "closed": 0, "error": "", "note": "没有需要恢复的规则"}
    done, errs = 0, []
    for e in targets:
        ok, err = _rule_del(e)
        if ok:
            done += 1
            fw_state_drop(e)
            FW_MANAGED.pop(int(e.get("port") or 0), None)
        else:
            errs.append(f"{e.get('name')}: {err or '失败'}")
    if done:
        log.info("已恢复 %d 条防火墙放行规则", done)
    if errs and _elevate_fw("fw --close --all"):
        return {
            "ok": True, "closed": done, "elevating": True,
            "error": "剩余规则需要管理员权限，已弹出授权窗口，点“是”即可恢复",
        }
    return {"ok": not errs, "closed": done, "error": "；".join(errs)}


def fw_cleanup_all(reason: str = "") -> int:
    """退出/闪退恢复：删除本进程开的临时规则（常驻规则保留）。"""
    st = fw_state_load()
    n = 0
    leftover: list[dict] = []
    for e in list(st["rules"]):
        if e.get("mode") == "persist":
            continue
        pid = e.get("pid")
        if pid and int(pid) != os.getpid() and _pid_alive(pid):
            continue  # 另一个 ClipShare 实例还在用，别动
        ok, _ = _rule_del(e)
        if ok:
            fw_state_drop(e)
            FW_MANAGED.pop(int(e.get("port") or 0), None)
            n += 1
        else:
            leftover.append(e)
    if n:
        log.info("防火墙已恢复：删除 %d 条临时放行规则%s", n, f"（{reason}）" if reason else "")
    # 没管理员权限删不掉时，弹一次 UAC 兜底（点“是”即恢复，点“否”留给下次启动回收）
    if leftover and _elevate_fw("fw --close --all"):
        log.info("已请求管理员权限恢复 %d 条遗留规则", len(leftover))
    return n


def fw_reap_stale() -> int:
    """启动时回收上次闪退 / 强杀遗留的规则。"""
    st = fw_state_load()
    n = 0
    for e in list(st["rules"]):
        if e.get("mode") == "persist":
            continue
        pid = e.get("pid")
        if not pid or int(pid) == os.getpid():
            continue  # 本进程自己正在用的不动
        if _pid_alive(pid):
            continue
        ok, err = _rule_del(e)
        if ok:
            fw_state_drop(e)
            n += 1
        else:
            log.warning(
                "上次遗留的放行规则 %s（TCP %s）未能自动恢复：%s",
                e.get("name"), e.get("port"), (err or "")[:120],
            )
    if IS_WIN:
        known = {str(e.get("name")) for e in fw_state_load()["rules"]}
        try:
            for nm in _win_rule_names():
                if nm not in known:
                    if _win_rule_del(nm)[0]:
                        n += 1
        except Exception as exc:
            log.debug("孤儿规则扫描失败: %s", exc)
    if n:
        log.warning("已清理上次异常退出遗留的防火墙规则 %d 条", n)
    return n


def fw_migrate(old_port: int, new_port: int) -> None:
    """换端口后把放行规则跟着搬过去。"""
    old_port, new_port = int(old_port), int(new_port)
    if old_port == new_port:
        return
    e = fw_managed(old_port)
    if not e:
        return
    mode = e.get("mode", "temp")
    fw_close(old_port)
    fw_open(new_port, mode, elevate=False)
    log.info("端口 %s → %s，放行规则已同步迁移（%s）", old_port, new_port, mode)


def _win_install_ctrl_handler() -> None:
    """控制台被关掉 / 注销 / 关机时也能把端口恢复回去。"""
    global _WIN_CTRL_REF
    try:
        PHANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _cb(event: int) -> bool:
            if event in (0, 2, 5, 6):  # CTRL_C / CTRL_CLOSE / LOGOFF / SHUTDOWN
                try:
                    fw_cleanup_all(f"控制台事件 {event}")
                except Exception:
                    pass
            return False

        _WIN_CTRL_REF = PHANDLER(_cb)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CTRL_REF, True)
    except Exception:
        pass


def _posix_signal_handler(signum, frame) -> None:
    try:
        fw_cleanup_all(f"信号 {signum}")
    except Exception:
        pass
    if signum == getattr(signal, "SIGINT", 2):
        raise KeyboardInterrupt
    sys.exit(0)


def fw_register_exit_hooks() -> None:
    """注册退出/崩溃清理钩子（atexit + 控制台事件 + 信号），保证不留洞口。"""
    global _CLEANUP_REGISTERED
    if _CLEANUP_REGISTERED:
        return
    _CLEANUP_REGISTERED = True
    atexit.register(lambda: fw_cleanup_all("进程退出"))
    if IS_WIN:
        _win_install_ctrl_handler()
    else:
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _posix_signal_handler)
            except Exception:
                pass


def fw_startup_housekeeping() -> None:
    """启动时的防火墙善后：后台回收遗留规则 + 注册退出钩子。"""
    fw_register_exit_hooks()
    threading.Thread(target=fw_reap_stale, daemon=True, name="fw-reap").start()


# ---- 双向绑定：绑完让对方也把我加进去 ----
def self_peer(cfg: dict) -> dict:
    return {
        "host": advertise_host(cfg),
        "port": int(cfg["port"]),
        "token": cfg["token"],
        "name": cfg["name"],
    }


def reverse_bind(cfg: dict, peer: dict, timeout: float = 4.0) -> dict:
    """告诉对端「把我也加为对端」，实现一次绑定双向互通。"""
    try:
        _, obj = http_json(
            peer_base(peer) + "/api/peer",
            {"peer": self_peer(cfg)},
            token=peer.get("token", ""),
            timeout=timeout,
        )
        return {
            "ok": bool(obj.get("ok")),
            "skipped": bool(obj.get("skipped")),
            "added": bool(obj.get("added")),
            "updated": bool(obj.get("updated")),
            "handshake": bool(obj.get("handshake")),
            "error": obj.get("error") or "",
            "detail": obj.get("detail") or "",
        }
    except urllib.error.HTTPError as exc:
        msg = "令牌不匹配（请重新复制对方配对链接）" if exc.code in (401, 403) else f"HTTP {exc.code}"
        return {"ok": False, "error": msg}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}：连不上对方（IP/端口/防火墙/对方是否在线）"}


def reverse_unbind(cfg: dict, peer: dict, timeout: float = 3.0) -> dict:
    """解绑时通知对方把我删掉，避免单向残留。"""
    try:
        _, obj = http_json(
            peer_base(peer) + "/api/peer",
            {"remove": True, "host": advertise_host(cfg), "port": int(cfg["port"])},
            token=peer.get("token", ""),
            timeout=timeout,
        )
        return {"ok": bool(obj.get("ok")), "removed": int(obj.get("removed") or 0)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}"}


def is_loopback(host: str) -> bool:
    h = str(host or "").strip().strip("[]")
    return h in ("127.0.0.1", "::1", "localhost", "127.0.0.0")


def tail_log(lines: int = 200) -> str:
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return "".join(data[-max(1, min(lines, 2000)):])
    except FileNotFoundError:
        return ""
    except Exception as exc:
        return f"读取日志失败：{exc}"


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def gui_python() -> str:
    if is_frozen():
        return sys.executable
    exe = sys.executable or "python"
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        alt = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(alt):
            return alt
    return exe


def daemon_exe() -> str | None:
    """打包版会额外产出一个无控制台窗口的同目录程序（托盘入口）。

    注意：Windows 文件名不区分大小写，所以 CLI 版必须叫 clipshare-cli.exe，
    不能叫 clipshare.exe，否则会和 ClipShare.exe 互相覆盖。
    """
    if not is_frozen():
        return None
    here = os.path.dirname(sys.executable)
    names = (
        ["ClipShare.exe", "clipshared.exe", "ClipShare", "clipshared"]
        if os.name == "nt"
        else ["ClipShare", "clipshared"]
    )
    for n in names:
        cand = os.path.join(here, n)
        if os.path.exists(cand):
            return cand
    return None


def base_argv() -> list[str]:
    if is_frozen():
        return [sys.executable]
    return [gui_python(), os.path.abspath(__file__)]


def autostart_command() -> str:
    exe = daemon_exe() or sys.executable
    if is_frozen():
        return f'"{exe}" gui --silent'
    return f'"{gui_python()}" "{os.path.abspath(__file__)}" gui --silent'


# ---- Linux: systemd user unit 或 XDG autostart ----
def _linux_unit_path() -> str:
    return os.path.expanduser("~/.config/systemd/user/clipshare.service")


def _linux_desktop_path() -> str:
    return os.path.expanduser("~/.config/autostart/clipshare.desktop")


def install_autostart_linux() -> bool:
    import shutil

    exe = daemon_exe() or sys.executable
    if is_frozen():
        cmd = f'"{exe}" run --silent'
    else:
        cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}" run --silent'

    if shutil.which("systemctl"):
        unit = (
            "[Unit]\n"
            "Description=ClipShare clipboard sync\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={cmd}\n"
            "Restart=on-failure\n"
            "RestartSec=3\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        try:
            p = _linux_unit_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(unit)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            r = subprocess.run(
                ["systemctl", "--user", "enable", "--now", "clipshare.service"],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                print("已安装 systemd user 服务并启动。常驻生效需执行一次： loginctl enable-linger $USER")
                return True
            print(f"systemctl 返回 {r.returncode}: {(r.stderr or '').strip()[:200]}")
        except Exception as exc:
            print(f"systemd 安装失败: {exc}")

    # 回退：XDG autostart（需要图形登录会话）
    try:
        p = _linux_desktop_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                "[Desktop Entry]\nType=Application\n"
                "Name=ClipShare\nComment=Clipboard sync\n"
                f"Exec={cmd}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n"
            )
        print(f"已写入 XDG 自启：{p}")
        return True
    except Exception as exc:
        print(f"写入 XDG 自启失败: {exc}")
        return False


def uninstall_autostart_linux() -> bool:
    import shutil

    ok = True
    if shutil.which("systemctl") and os.path.exists(_linux_unit_path()):
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "clipshare.service"], check=False
        )
        try:
            os.remove(_linux_unit_path())
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        except Exception:
            ok = False
    if os.path.exists(_linux_desktop_path()):
        try:
            os.remove(_linux_desktop_path())
        except Exception:
            ok = False
    return ok



def autostart_installed() -> bool:
    if not IS_WIN:
        return os.path.exists(_linux_unit_path()) or os.path.exists(_linux_desktop_path())
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def install_autostart() -> bool:
    if not IS_WIN:
        return install_autostart_linux()
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        return True
    except Exception as exc:
        print(f"写入注册表失败: {exc}")
        return False


def uninstall_autostart() -> bool:
    if not IS_WIN:
        return uninstall_autostart_linux()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        print(f"删除注册表项失败: {exc}")
        return False


# --------------------------------------------------------------------------
# 后台进程控制
# --------------------------------------------------------------------------
def spawn_background() -> None:
    here = (
        os.path.dirname(sys.executable)
        if is_frozen()
        else os.path.dirname(os.path.abspath(__file__))
    )
    kwargs = {"cwd": here}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    else:
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        kwargs["start_new_session"] = True
    subprocess.Popen(base_argv() + ["gui", "--silent"], **kwargs)


def cmd_start(cfg: dict) -> int:
    if service_alive(cfg):
        print(f"服务已在运行（{local_service_addr(cfg)}:{cfg['port']}）")
        return 0
    spawn_background()
    for _ in range(40):
        time.sleep(0.25)
        live = load_config()  # 子进程可能因为端口被占用自动换了端口，要以磁盘上的为准
        if service_alive(live):
            print(f"已启动：{live['bind']}:{live['port']}  名称={live['name']}")
            if int(live["port"]) != int(cfg["port"]):
                print(f"注意：端口 {cfg['port']} 被占用，已自动改用 {live['port']}")
            return 0
    print("启动失败或超时，请查看日志：", LOG_PATH)
    return 1


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def cmd_run(args, cfg: dict) -> int:
    global CORE, SERVER
    clipboard()  # 平台不支持时在这里给出明确提示
    try:
        SERVER, port, note = bind_with_fallback(cfg)
    except OSError as exc:
        log.error("监听 %s:%s 失败: %s（端口被占用，或服务已在运行）", cfg["bind"], cfg["port"], exc)
        if not args.silent:
            print(f"监听 {cfg['bind']}:{cfg['port']} 失败：{exc}")
            if cfg.get("port_auto", True):
                print("可换端口：clipshare-cli.exe config --port 8899，或在设置面板改监听地址/端口")
        return 2
    CORE = Core(cfg)
    CORE.pusher.start()
    threading.Thread(target=poller_loop, args=(CORE,), daemon=True, name="poller").start()
    fw_startup_housekeeping()  # 回收上次闪退遗留的防火墙规则 + 注册退出自动恢复

    if note and not args.silent:
        print(note)

    log.info("ClipShare %s 已启动  监听=%s:%s  名称=%s  对端=%d",
             VERSION, cfg["bind"], port, cfg["name"], len(cfg.get("peers") or []))
    if not args.silent:
        print(f"监听 {cfg['bind']}:{cfg['port']}  对端 {len(cfg.get('peers') or [])} 个  Ctrl+C 退出")
    # 服务循环放后台线程：这样运行中换监听地址/端口不会导致进程退出
    _serve_in_background(SERVER)
    try:
        while not CORE.stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        CORE.stop_event.set()
        _stop_server(SERVER)
        fw_cleanup_all("服务停止")
        log.info("已停止")
    return 0


def ui_url(cfg: dict) -> str:
    host = local_service_addr(cfg)
    return f"http://{host_for_url(host)}:{cfg['port']}/ui?t={cfg['token']}"


def open_settings(cfg: dict) -> str:
    url = ui_url(cfg)
    if os.environ.get("CLIPSHARE_NO_OPEN"):
        log.info("跳过打开浏览器（CLIPSHARE_NO_OPEN）：%s", url)
        return url
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return url


def fatal(msg: str) -> None:
    log.error(msg)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, "ClipShare", 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _new_http_server(bind: str, port: int):
    ThreadingHTTPServer.daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程绑定同一端口（请求被随机劫持），必须关闭
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    return ThreadingHTTPServer((bind, int(port)), Handler)


def bind_with_fallback(cfg: dict, strict: bool = False):
    """按配置绑定监听。端口被占用且允许自动避让时，自动换一个空闲端口。

    返回 (server, port, note)；note 非空表示发生了自动改端口。
    """
    bind, want = cfg["bind"], int(cfg["port"])
    try:
        return _new_http_server(bind, want), want, ""
    except OSError as exc:
        if strict or not cfg.get("port_auto", True):
            raise
        alt = pick_free_port(bind, want + 1, 60)
        if not alt:
            raise
        try:
            srv = _new_http_server(bind, alt)
        except OSError:
            raise exc
        note = f"端口 {want} 被占用（{getattr(exc, 'strerror', None) or exc}），已自动改用 {alt}"
        log.warning(note)
        cfg["port"] = int(alt)
        save_config(cfg)
        return srv, int(alt), note


def _start_service(cfg: dict):
    """初始化核心与 HTTP 服务（托盘 / 无界面共用）。"""
    global CORE, SERVER
    clipboard()
    SERVER, port, note = bind_with_fallback(cfg)
    CORE = Core(cfg)
    CORE.pusher.start()
    threading.Thread(target=poller_loop, args=(CORE,), daemon=True, name="poller").start()
    threading.Thread(
        target=SERVER.serve_forever, kwargs={"poll_interval": 0.4}, daemon=True, name="http"
    ).start()
    fw_startup_housekeeping()  # 回收上次闪退遗留的防火墙规则 + 注册退出自动恢复
    if note:
        CORE.port_note = note
    return CORE, SERVER


_REBIND_LOCK = threading.Lock()


def _serve_in_background(srv) -> None:
    threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.4},
        daemon=True, name="http",
    ).start()


def _stop_server(srv) -> None:
    if srv is None:
        return
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass


def _rebind_fail(bind: str, port: int, old_bind: str, old_port: int, exc) -> dict:
    """新地址绑不上：尝试把旧地址恢复回来，保证服务不中断。"""
    global SERVER
    reason = getattr(exc, "strerror", None) or str(exc)
    rolled = False
    try:
        srv = _new_http_server(old_bind, old_port)
        SERVER = srv
        _serve_in_background(srv)
        rolled = True
        log.warning("切换监听 %s:%s 失败（%s），已回滚到 %s:%s",
                    bind, port, reason, old_bind, old_port)
    except Exception as exc2:
        SERVER = None
        log.error("回滚到 %s:%s 也失败：%s", old_bind, old_port, exc2)
    return {
        "ok": False,
        "error": f"无法监听 {bind}:{port} —— {reason}",
        "rolled_back": rolled,
        "bind": old_bind if rolled else None,
        "port": old_port if rolled else None,
    }


def rebind_service(bind: str, port: int, advertise: str = "",
                   port_auto: bool | None = None) -> dict:
    """运行中切换监听地址/端口；新地址不可用时自动回滚，不会把服务弄挂。

    - 只换了端口：先建立新监听再关旧的，切换过程零中断
    - 只换了地址（端口不变）：必须先关旧的才能绑新的，失败则回滚
    """
    global SERVER
    if CORE is None:
        return {"ok": False, "error": "服务未运行"}
    old = dict(CORE.cfg)
    old_bind, old_port = old["bind"], int(old["port"])
    same = (bind == old_bind and int(port) == old_port)

    with _REBIND_LOCK:
        if not same:
            old_srv = SERVER
            if int(port) != old_port:
                # 端口变了，新旧可以并存 → 先起新的，再关旧的（切换零中断）
                try:
                    new_srv = _new_http_server(bind, port)
                except OSError as exc:
                    # 旧监听完全没动过，什么都不用回滚
                    reason = getattr(exc, "strerror", None) or str(exc)
                    log.warning("切换监听 %s:%s 失败（%s），旧监听 %s:%s 保持不变",
                                bind, port, reason, old_bind, old_port)
                    return {
                        "ok": False,
                        "error": f"无法监听 {bind}:{port} —— {reason}",
                        "rolled_back": True,
                        "bind": old_bind,
                        "port": old_port,
                    }
                SERVER = new_srv
                _serve_in_background(new_srv)
                _stop_server(old_srv)
            else:
                # 同端口换地址，只能先进退：先关旧的才能绑新的
                _stop_server(old_srv)
                try:
                    new_srv = _new_http_server(bind, port)
                except OSError as exc:
                    return _rebind_fail(bind, port, old_bind, old_port, exc)
                SERVER = new_srv
                _serve_in_background(new_srv)

        cfg = CORE.cfg
        cfg["bind"] = bind
        cfg["port"] = int(port)
        cfg["advertise"] = advertise
        if port_auto is not None:
            cfg["port_auto"] = bool(port_auto)
        save_config(cfg)
        log.info("监听已更新为 %s:%s，对外公布 %s", bind, port, advertise_host(cfg))
        if int(port) != old_port:
            fw_migrate(old_port, int(port))  # 端口变了，放行规则跟着搬
        return {
            "ok": True,
            "bind": bind,
            "port": int(port),
            "advertise": cfg.get("advertise", ""),
            "port_auto": bool(cfg.get("port_auto", True)),
            "link": build_link(cfg),
            "ui_url": ui_url(cfg),
            "moved": not same,
        }


# ---- 托盘动作 ----
def _toggle_pause() -> None:
    CORE.paused = not CORE.paused
    log.info("同步已%s", "暂停" if CORE.paused else "恢复")


def _toggle_debug() -> None:
    new = "info" if str(CORE.cfg.get("log_level", "info")) == "debug" else "debug"
    CORE.cfg["log_level"] = new
    apply_log_level(CORE.cfg)
    save_config(CORE.cfg)
    log.info("日志级别切换为 %s", new)


def _toggle_autostart() -> None:
    if autostart_installed():
        uninstall_autostart()
        log.info("已取消开机自启")
    else:
        install_autostart()
        log.info("已设置开机自启")


def _toggle_firewall() -> str:
    """托盘一键放行 / 恢复：临时规则在退出或闪退后自动撤销。"""
    port = int(CORE.cfg["port"])
    mode = CORE.cfg.get("fw_mode", "temp")
    if fw_managed(port):
        res = fw_close(port)
        if res.get("ok"):
            return f"已恢复：TCP {port} 放行规则已删除（{res.get('closed', 0)} 条）"
        return f"恢复失败：{res.get('error')}"
    res = fw_open(port, mode)
    if res.get("ok"):
        return f"已放行 TCP {port}（{'常驻' if mode == 'persist' else '退出自动恢复'}）"
    if res.get("elevating"):
        return res.get("error") or "已请求管理员权限"
    return f"放行失败：{res.get('error')}"


def _copy_my_link() -> str:
    link = build_link(CORE.cfg)
    try:
        clipboard().write(link)
    except Exception:
        pass
    return link


def _open_log_file() -> None:
    try:
        if os.name == "nt":
            os.startfile(LOG_PATH)  # type: ignore[attr-defined]
        else:
            webbrowser.open("file://" + LOG_PATH)
    except Exception as exc:
        log.warning("打开日志失败: %s", exc)


def cmd_gui(args, cfg: dict) -> int:
    global TRAY
    if service_alive(cfg):
        url = open_settings(cfg)
        log.info("已有实例在运行，打开设置面板 %s", url)
        if not getattr(args, "silent", False):
            print(f"服务已在运行，已打开设置面板：{url}")
        return 0

    try:
        core, server = _start_service(cfg)
    except OSError as exc:
        fatal(
            f"无法监听 {cfg['bind']}:{cfg['port']}：{exc}\n\n"
            "常见原因：端口被其它程序占用，或填的这个 IP 不在本机网卡上。\n"
            "解决办法：在设置面板的「监听地址与端口」里改一个，或勾上"
            "“端口被占用时自动换一个可用端口”。\n"
            f"配置文件：{CONFIG_PATH}"
        )
        return 2

    log.info(
        "ClipShare %s 已启动（托盘）监听=%s:%s 对端=%d",
        VERSION,
        cfg["bind"],
        cfg["port"],
        len(cfg.get("peers") or []),
    )

    try:
        import gui  # 同目录模块
    except Exception as exc:
        gui = None
        log.warning("托盘模块加载失败: %s", exc)

    if gui is None or not gui.available():
        log.warning("托盘组件缺失（pip install pystray pillow），改为前台常驻")
        if not getattr(args, "silent", False):
            print("托盘组件缺失，已改为前台运行，Ctrl+C 退出。设置面板：" + ui_url(cfg))
        try:
            while not core.stop_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown()
        return 0

    hooks = {
        "title": lambda: f"ClipShare · {CORE.cfg['name']}",
        "status": lambda: (
            f"监听 {CORE.cfg['bind']}:{CORE.cfg['port']}"
            f" · 对端 {len(CORE.cfg.get('peers') or [])} 个"
            + ("（已暂停）" if CORE.paused else "")
        ),
        "open_ui": lambda: open_settings(CORE.cfg),
        "paused": lambda: CORE.paused,
        "toggle_pause": _toggle_pause,
        "debug": lambda: str(CORE.cfg.get("log_level", "info")) == "debug",
        "toggle_debug": _toggle_debug,
        "autostart": autostart_installed,
        "toggle_autostart": _toggle_autostart,
        "copy_link": _copy_my_link,
        "open_log": _open_log_file,
        "shutdown": _shutdown,
        "fw_label": lambda: f"防火墙放行 TCP {CORE.cfg['port']}"
                            + ("（点击恢复）" if fw_managed(int(CORE.cfg["port"])) else "（点击放行）"),
        "fw_open": lambda: bool(fw_managed(int(CORE.cfg["port"]))),
        "toggle_fw": _toggle_firewall,
    }
    app = gui.TrayApp(hooks)
    TRAY = app.icon
    if not cfg.get("peers") and not getattr(args, "silent", False):
        open_settings(cfg)  # 首次手动运行自动打开设置面板，引导绑定；开机自启时不弹浏览器
    try:
        app.run()
    finally:
        log.info("托盘退出")
        try:
            server.server_close()
        except Exception:
            pass
    return 0


def cmd_stop(args, cfg: dict) -> int:
    try:
        service_call(cfg, "/api/shutdown", payload={}, timeout=3)
    except Exception:
        pass
    for _ in range(20):
        time.sleep(0.2)
        if not service_alive(cfg):
            print("已停止")
            return 0
    print("未能停止，可能未在运行。日志：" + LOG_PATH)
    return 1


def cmd_restart(args, cfg: dict) -> int:
    cmd_stop(args, cfg)
    time.sleep(0.4)
    return cmd_start(cfg)


def cmd_status(args, cfg: dict) -> int:
    if not service_alive(cfg):
        state = "已安装" if autostart_installed() else "未安装"
        print(f"服务：未运行    开机自启：{state}")
        print(f"配置：{CONFIG_PATH}")
        print(f"日志：{LOG_PATH}")
        return 1
    _, st = service_call(cfg, "/api/status")
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(f"服务：运行中  {st['name']}  v{st['version']}  监听 {st['bind']}  已运行 {st['uptime_s']}s")
    if st.get("link"):
        print(f"配对链接：{st['link']}")
    print(f"自启：{'已安装' if autostart_installed() else '未安装'}    推送 {st['push_count']} 次 / 接收 {st['recv_count']} 次")
    if st["last_push_at"]:
        print(f"最近推送：{time.strftime('%H:%M:%S', time.localtime(st['last_push_at']))}")
    if st["last_recv_at"]:
        print(f"最近接收：{time.strftime('%H:%M:%S', time.localtime(st['last_recv_at']))}")
    print(f"剪贴板缓存：{st['clip_len']} 字")
    peers = st.get("peers") or []
    if not peers:
        print("对端：未绑定（用 bind 命令绑定配对链接）")
    else:
        print("对端：")
        for p in peers:
            s = p.get("state") or {}
            if not s:
                mark = "未探测"
            elif s.get("ok"):
                mark = "○ 正常"
            else:
                mark = "× " + str(s.get("err") or "失败")
            print(f"  - {p.get('name')}  {p.get('host')}:{p.get('port')}  {mark}")
    return 0


def cmd_link(args, cfg: dict) -> int:
    if args.host:
        host = args.host
    elif args.wan:
        host = public_ip()
        if not host:
            print("无法获取公网 IP（可能被墙或离线）。可手动指定：link --host 1.2.3.4")
            return 1
    else:
        host = advertise_host(cfg)
    link = build_link(cfg, host)
    ips = local_ipv4_list()
    print(f"本机 IP   : {', '.join(ips) if ips else local_ip()}")
    print(f"监听      : {cfg['bind']}:{cfg['port']}")
    if args.wan or args.host or cfg.get("advertise"):
        print(f"使用地址 : {host}")
    print(f"配对链接 : {link}")
    if args.wan:
        print("\n提示：公网访问需要在路由器把该端口映射到本机，且本机防火墙放行。")
    print("\n在另一台机器上执行：")
    print(f'  python clipshare.py bind "{link}"')
    return 0


def cmd_bind(args, cfg: dict) -> int:
    try:
        peer = parse_link(args.link)
    except ValueError as exc:
        print(exc)
        return 2
    reverse = not getattr(args, "one_way", False)
    peers = cfg.setdefault("peers", [])
    for p in peers:
        if str(p.get("host")) == peer["host"] and int(p.get("port")) == peer["port"]:
            p.update(peer)
            print(f"已更新对端：{peer['name']}  {peer['host']}:{peer['port']}")
            break
    else:
        peers.append(peer)
        print(f"已绑定对端：{peer['name']}  {peer['host']}:{peer['port']}")
    save_config(cfg)

    # 立即握手验证
    try:
        _, obj = http_json(peer_base(peer) + "/api/ping", token=peer["token"], timeout=4)
        print(f"握手成功：对端 {obj.get('name')} v{obj.get('version')}")
    except urllib.error.HTTPError as exc:
        print(f"握手失败：HTTP {exc.code}（令牌不匹配？请重新复制对方链接）")
        return 1
    except Exception as exc:
        print(f"握手失败：{type(exc).__name__} —— 检查 IP/端口/防火墙/是否开机")
        if cfg.get("fw_autocheck", True):
            fw = fw_check()
            print(fw_hint_text(fw))
            if fw.get("state") == "blocked" and cfg.get("fw_autoopen", False):
                print("按配置自动放行…")
                print(cmd_fw_open(int(cfg["port"]), cfg.get("fw_mode", "temp")))
        return 1

    # 双向：让对方也把本机加为对端
    if reverse:
        rev = reverse_bind(cfg, peer)
        for p in cfg.get("peers") or []:
            if peer_key(p) == peer_key(peer):
                p["mutual"] = bool(rev.get("ok"))
                save_config(cfg)
                break
        if rev.get("ok"):
            print("双向绑定成功：对方已把本机加入对端列表，两边都能互相推送")
        elif rev.get("skipped"):
            print("对方跳过了回绑（可能它认为对端就是自己）")
        else:
            print(f"单向绑定成功，但对方未能回绑本机：{rev.get('error')}")

    if service_alive(cfg):
        hot = notify_reload(cfg)
        print("服务运行中，配置已即时生效。" if hot else "服务运行中，但热加载失败，请执行 restart。")
    else:
        print("提示：服务当前未运行，执行 start 或 install 后生效。")
    return 0


def fw_hint_text(fw: dict) -> str:
    if not fw:
        return ""
    lines = [f"防火墙体检（{fw.get('backend')}）：{fw.get('message')}"]
    if fw.get("state") == "blocked":
        lines.append(f"可执行放行：clipshare-cli.exe fw --open --port {fw.get('port')}")
    return "\n".join(lines)


def cmd_fw_open(port: int, mode: str = "temp", pid: int | None = None) -> str:
    res = fw_open(port, mode, pid=pid)
    if res.get("ok"):
        return f"已放行 TCP {port}（{'常驻' if mode == 'persist' else '临时，退出自动恢复'}）规则={res.get('rule')}"
    if res.get("elevating"):
        return res.get("error") or "已请求管理员权限"
    return f"放行失败：{res.get('error')}"


def cmd_fw(args, cfg: dict) -> int:
    """防火墙检测 / 一键放行 / 恢复。"""
    port = int(getattr(args, "port", 0) or cfg["port"])
    mode = str(getattr(args, "mode", "") or cfg.get("fw_mode", "temp")).lower()
    pid = getattr(args, "pid", None)

    if getattr(args, "open", False):
        print(cmd_fw_open(port, mode, pid=pid))
        return 0
    if getattr(args, "close", False):
        res = fw_close(None if getattr(args, "all", False) else port)
        print(f"已恢复 {res.get('closed', 0)} 条规则" if res.get("ok") else f"恢复失败：{res.get('error')}")
        return 0 if res.get("ok") else 1

    fw = fw_check(port)
    print(f"端口        : {fw['port']}（监听 {fw['bind']}）")
    print(f"防火墙后端  : {fw['backend']}")
    print(f"防火墙开启  : {'是' if fw['firewall_on'] else '否'}")
    print(f"本端口放行  : {'是' if fw['rule_exists'] else '否'}"
          f"{'（ClipShare 创建）' if fw['rule_exists'] else ''}")
    print(f"服务监听中  : {'是' if fw['listening'] else '否'}")
    print(f"结论        : {fw['message']}")
    if fw.get("managed"):
        print("已管理的规则：")
        for m in fw["managed"]:
            print(f"  - TCP {m['port']}  {m['mode']}  进程{'存活' if m['alive'] else '已退出'}  {m['rule']}")
    if fw.get("state") == "blocked":
        print(f"\n可执行：clipshare-cli.exe fw --open --port {port}  （临时规则，退出自动恢复）")
        print(f"       clipshare-cli.exe fw --open --port {port} --mode persist  （常驻规则）")
    return 0


def cmd_unbind(args, cfg: dict) -> int:
    key = args.target.strip()
    peers = cfg.get("peers") or []
    target = None
    for p in peers:
        if key in (p.get("name"), str(p.get("host")), peer_key(p),
                   f"{p.get('name')}@{peer_key(p)}"):
            target = p
            break
    keep = [p for p in peers if p is not target]
    if target is None:
        print(f"没找到对端：{key}")
        return 1
    cfg["peers"] = keep
    save_config(cfg)
    notify_reload(cfg)
    print(f"已解绑 {len(peers) - len(keep)} 个对端")
    if not getattr(args, "local_only", False) and target is not None:
        told = reverse_unbind(cfg, target)
        if told.get("ok") and told.get("removed"):
            print(f"已通知对方解除（对方移除 {told['removed']} 个对端）")
        elif told.get("ok"):
            print("已通知对方（对方没有匹配的记录）")
        else:
            print(f"通知对方失败：{told.get('error')}（对方离线时可在它那边手动删除）")
    return 0


def cmd_peers(args, cfg: dict) -> int:
    peers = cfg.get("peers") or []
    if not peers:
        print("尚未绑定任何对端。先在本机 link 生成链接，到对端 bind。")
        return 0
    print(f"{'名称':<18}{'地址':<26}{'令牌':<10}状态")
    for p in peers:
        addr = f"{host_for_url(str(p['host']))}:{p['port']}"
        tok = (p.get("token") or "")[:8] + "…"
        try:
            http_json(peer_base(p) + "/api/ping", token=p.get("token", ""), timeout=3)
            st = "○ 正常"
        except Exception as exc:
            st = f"× {type(exc).__name__}"
        print(f"{str(p.get('name')):<18}{addr:<26}{tok:<10}{st}")
    return 0


def cmd_send(args, cfg: dict) -> int:
    if args.file:
        with open(args.file, "r", encoding=args.encoding) as f:
            text = f.read()
    else:
        text = " ".join(args.text)
    if not text:
        print("没有内容可发送")
        return 2
    if service_alive(cfg):
        _, obj = service_call(cfg, "/api/broadcast", {"text": text})
        print(f"已写入本机剪贴板并推送给 {obj.get('peers', 0)} 个对端（{len(text)} 字）")
        return 0
    clipboard().write(text)
    print("服务未运行：只写入了本机剪贴板，未推送给对端。")
    return 1


def cmd_install(args, cfg: dict) -> int:
    if args.bind:
        try:
            cfg["bind"] = validate_bind(args.bind)
        except ValueError as exc:
            print(exc)
            return 2
    if args.port:
        cfg["port"] = int(args.port)
    if getattr(args, "advertise", None) is not None:
        adv = args.advertise.strip()
        cfg["advertise"] = "" if adv.lower() in ("auto", "自动") else adv
    if args.name:
        cfg["name"] = args.name
    save_config(cfg)
    ok = install_autostart()
    print(f"开机自启：{'已安装' if ok else '安装失败'}")
    print(f"启动项：{autostart_command()}")
    cmd_start(cfg)
    print("\n下一步：")
    print("  1) python clipshare.py link      # 生成配对链接")
    print('  2) 到对端执行 bind "<链接>"')
    return 0 if ok else 1


def cmd_uninstall(args, cfg: dict) -> int:
    cmd_stop(args, cfg)
    uninstall_autostart()
    print("已移除开机自启（配置文件保留）")
    return 0


def cmd_config(args, cfg: dict) -> int:
    dirty = False
    net_changed = False
    probe = {"bind": cfg["bind"], "port": int(cfg["port"]), "token": cfg["token"]}
    if args.bind:
        stamp = str(cfg["bind"])
        try:
            cfg["bind"] = validate_bind(args.bind)
        except ValueError as exc:
            print(exc)
            return 2
        net_changed = net_changed or cfg["bind"] != stamp
        dirty = True
    if args.port:
        new_port = int(args.port)
        if not (1 <= new_port <= 65535):
            print("端口范围 1-65535")
            return 2
        net_changed = net_changed or new_port != int(cfg["port"])
        cfg["port"] = new_port
        dirty = True
    if args.name:
        cfg["name"] = args.name
        dirty = True
    if getattr(args, "advertise", None) is not None:
        adv = args.advertise.strip()
        cfg["advertise"] = "" if adv.lower() in ("auto", "自动") else adv
        dirty = True
    if getattr(args, "port_auto", None) is not None:
        cfg["port_auto"] = bool(args.port_auto)
        dirty = True
    if args.max_kb:
        cfg["max_kb"] = int(args.max_kb)
        dirty = True
    if args.poll_ms:
        cfg["poll_ms"] = max(50, int(args.poll_ms))
        dirty = True
    if args.new_token:
        cfg["token"] = secrets.token_hex(16)
        dirty = True
        print("已生成新令牌 —— 所有对端需重新 bind")
    if dirty:
        if net_changed and not args.new_token and service_alive(probe):
            # 服务在跑：直接调接口热切换监听，不用重启
            try:
                _, res = service_call(
                    probe,
                    "/api/network",
                    {
                        "bind": cfg["bind"],
                        "port": int(cfg["port"]),
                        "advertise": cfg.get("advertise", ""),
                        "port_auto": bool(cfg.get("port_auto", True)),
                    },
                    timeout=20,
                )
            except Exception as exc:
                res = {"ok": False, "error": str(exc)}
            if res.get("ok"):
                print(f"监听已切换为 {res['bind']}:{res['port']}（已生效，无需重启）")
                print(f"新配对链接：{res.get('link', '')}")
            else:
                print(f"切换监听失败：{res.get('error')}（配置保持原样）")
                return 1
        else:
            save_config(cfg)
            if args.new_token:
                print("配置已保存 —— 令牌已变更，必须执行 restart 才对端生效")
            elif notify_reload(cfg):
                print("配置已保存并热加载")
            else:
                print("配置已保存；服务未运行或需 restart")
    print(json.dumps({k: v for k, v in cfg.items() if k != "token"}, ensure_ascii=False, indent=2))
    print(f"配置文件：{CONFIG_PATH}")
    return 0


def cmd_log(args, cfg: dict) -> int:
    if not os.path.exists(LOG_PATH):
        print("暂无日志")
        return 1
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    sys.stdout.write("".join(lines[-args.lines:]))
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipshare",
        description="ClipShare —— 通过指定 IP 的端到端剪贴板共享（Windows / Linux）。"
        "不带参数运行 = 启动托盘界面。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：双击 ClipShare.exe 启动托盘；命令行用 clipshare-cli.exe status 查看状态",
    )
    p.add_argument("-v", "--version", action="version", version=f"ClipShare {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("run", help="前台运行服务")
    s.add_argument("--silent", action="store_true")
    s.set_defaults(fn=cmd_run)

    sub.add_parser("start", help="后台启动服务").set_defaults(fn=lambda a, c: cmd_start(c))
    sub.add_parser("stop", help="停止服务").set_defaults(fn=cmd_stop)
    sub.add_parser("restart", help="重启服务").set_defaults(fn=cmd_restart)

    s = sub.add_parser("status", help="查看运行状态")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("link", help="生成配对链接")
    s.add_argument("--wan", action="store_true", help="使用公网 IP 生成链接")
    s.add_argument("--host", help="手动指定 IP / 域名")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("bind", help="绑定对端配对链接（默认双向：对方也会加上本机）")
    s.add_argument("link")
    s.add_argument("--one-way", dest="one_way", action="store_true",
                   help="只在本机单向添加，不通知对方回绑")
    s.set_defaults(fn=cmd_bind)

    s = sub.add_parser("unbind", help="解除绑定（默认也会通知对方解除）")
    s.add_argument("target")
    s.add_argument("--local-only", dest="local_only", action="store_true",
                   help="只在本机删除，不通知对方")
    s.set_defaults(fn=cmd_unbind)

    s = sub.add_parser("fw", help="防火墙：检测 / 一键放行 / 恢复")
    s.add_argument("--open", dest="open", action="store_true", help="放行端口")
    s.add_argument("--close", dest="close", action="store_true", help="恢复（删除放行规则）")
    s.add_argument("--all", dest="all", action="store_true", help="恢复全部端口的规则")
    s.add_argument("--port", type=int, help="端口号，默认取当前配置")
    s.add_argument("--mode", choices=["temp", "persist"],
                   help="temp=退出/闪退自动恢复（默认）；persist=常驻")
    s.add_argument("--pid", type=int, help=argparse.SUPPRESS)  # UAC 提权子进程用
    s.set_defaults(fn=cmd_fw)

    sub.add_parser("peers", help="列出并对端做连通性探测").set_defaults(fn=cmd_peers)

    s = sub.add_parser("send", help="写入本机剪贴板并推送")
    s.add_argument("text", nargs="*")
    s.add_argument("--file")
    s.add_argument("--encoding", default="utf-8")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("install", help="安装开机自启并启动")
    s.add_argument("--bind", help="监听地址，如 192.168.2.65 或 0.0.0.0")
    s.add_argument("--port", type=int)
    s.add_argument("--advertise", help="写进配对链接的地址；传 auto 表示自动")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_install)

    sub.add_parser("uninstall", help="移除开机自启").set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("config", help="查看/修改配置")
    s.add_argument("--bind", help="监听地址：0.0.0.0=所有网卡，或指定本机 IP")
    s.add_argument("--port", type=int, help="监听端口，默认 8765")
    s.add_argument("--advertise", help="写进配对链接的地址（IP 或 DDNS）；传 auto 表示自动")
    s.add_argument("--port-auto", dest="port_auto", action="store_true",
                   default=None, help="端口被占用时自动换端口")
    s.add_argument("--no-port-auto", dest="port_auto", action="store_false",
                   help="端口被占用时直接报错，不自动换")
    s.add_argument("--name")
    s.add_argument("--max-kb", dest="max_kb", type=int)
    s.add_argument("--poll-ms", dest="poll_ms", type=int)
    s.add_argument("--new-token", dest="new_token", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("gui", help="启动托盘界面（不带参数运行时默认执行）")
    s.add_argument("--silent", action="store_true")
    s.set_defaults(fn=cmd_gui)

    s = sub.add_parser("log", help="查看最近日志")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.set_defaults(fn=cmd_log)

    return p


def _init_console() -> None:
    """统一控制台/管道的编码为 UTF-8，避免 Windows 下中文输出乱码。"""
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _init_console()
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        # 双击 exe / 不带参数运行 → 直接进托盘，不再打印帮助后秒退
        args.cmd = "gui"
        args.silent = False
        args.fn = cmd_gui
    silent = bool(getattr(args, "silent", False))
    os.makedirs(HOME, exist_ok=True)
    setup_logging(silent)
    cfg = load_config()
    apply_log_level(cfg)
    try:
        return int(args.fn(args, cfg) or 0)
    except KeyboardInterrupt:
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("命令执行失败")
        if not silent:
            print(f"出错：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
