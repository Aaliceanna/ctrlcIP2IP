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
import collections
import ctypes
import hmac
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import shlex
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

VERSION = "1.2.0"
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
                    },
                    "autostart": autostart_installed(),
                },
            )
        if path.path == "/api/reload":
            ok = CORE.reload()
            return self._json(200, {"ok": ok, "peers": len(CORE.cfg.get("peers") or [])})
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
            updated = upsert_peer(CORE.cfg, peer)
            CORE.reload()
            ok, detail = handshake(peer)
            log.info("绑定对端 %s:%s（%s）→ %s", peer["host"], peer["port"],
                     "更新" if updated else "新增", "成功" if ok else detail)
            return self._json(
                200,
                {
                    "ok": ok,
                    "peer": f"{peer['name']} {peer_key(peer)}",
                    "updated": updated,
                    "error": "" if ok else detail,
                },
            )

        if path == "/api/unbind":
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "参数错误"})
            n = remove_peer(CORE.cfg, str(body.get("key") or ""))
            CORE.reload()
            return self._json(200, {"ok": n > 0, "removed": n})

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
                },
            )

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
                    "state": CORE.peer_state.get(peer_key(p), {}),
                }
                for p in cfg.get("peers") or []
            ],
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
        return 1

    if service_alive(cfg):
        hot = notify_reload(cfg)
        print("服务运行中，配置已即时生效。" if hot else "服务运行中，但热加载失败，请执行 restart。")
    else:
        print("提示：服务当前未运行，执行 start 或 install 后生效。")
    return 0


def cmd_unbind(args, cfg: dict) -> int:
    key = args.target.strip()
    peers = cfg.get("peers") or []
    keep = [
        p for p in peers
        if not (key in (p.get("name"), str(p.get("host")), f"{p.get('host')}:{p.get('port')}", f"{p.get('name')}@{p.get('host')}:{p.get('port')}"))
    ]
    if len(keep) == len(peers):
        print(f"没找到对端：{key}")
        return 1
    cfg["peers"] = keep
    save_config(cfg)
    notify_reload(cfg)
    print(f"已解绑 {len(peers) - len(keep)} 个对端")
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

    s = sub.add_parser("bind", help="绑定对端配对链接")
    s.add_argument("link")
    s.set_defaults(fn=cmd_bind)

    s = sub.add_parser("unbind", help="解除绑定")
    s.add_argument("target")
    s.set_defaults(fn=cmd_unbind)

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
