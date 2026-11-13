#!/usr/bin/env python3
"""
Stream Dock M18 控制台 — Core 服务

职责：设备连接、HID 输入读取、灯光效果引擎、提供给 UI 的 HTTP/SSE 接口。
UI 是纯静态页面（app/ui/），可以后续直接包进 Electron。

启动：
    python server.py              # 自动选端口并打开应用窗口
    python server.py --no-browser  # 只起服务
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import copy
import ctypes
import glob
import hashlib
import io
import json
import math
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

import official
from wssdk import DeviceWS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI_DIR = os.path.join(HERE, "ui")
SDK_SRC = os.path.join(ROOT, "refs", "devicesdk", "Python-SDK", "src")
sys.path.insert(0, SDK_SRC)

# 用 pythonw 启动时没有控制台，把输出重定向到日志，避免 print 崩溃
if sys.stdout is None or sys.stderr is None:
    _log_path = os.path.join(HERE, "server.log")
    try:
        if os.path.getsize(_log_path) > 1_000_000:
            os.replace(_log_path, _log_path + ".1")
    except OSError:
        pass
    _log = open(_log_path, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

LED_COUNT = 24
KEY_COUNT = 15
CANDIDATES = [(0x6603, 0x1012), (0x6603, 0x1009), (0x5548, 0x1000)]
CONFIG_BAK_DIR = os.path.join(HERE, "config-backups")
ICON_DIR = os.path.join(HERE, "assets", "icons")
SCENE_DIR = os.path.join(HERE, "scenes")
def _find_sd_root() -> str:
    """定位官方 Stream Dock 安装目录（图标库 / 插件索引用）。

    优先级：环境变量 M18_SD_ROOT → 各盘常见安装位置。config.json 的
    officialRoot 会在加载配置时再覆盖一次。找不到时返回第一个候选（相关功能为空列表）。
    """
    env = os.environ.get("M18_SD_ROOT", "")
    if env and os.path.isdir(env):
        return env
    candidates = []
    for drive in "CDEFG":
        for base in ("Program Files", "Program Files (x86)"):
            candidates.append(rf"{drive}:\{base}\Stream Dock")
            candidates.append(rf"{drive}:\{base}\StreamDock")
            candidates.append(rf"{drive}:\{base}\HotSpot\StreamDock")
    for path in candidates:
        if os.path.isfile(os.path.join(path, "StreamDock.exe")):
            return path
    return candidates[0]


SD_ROOT = _find_sd_root()
WALLPAPER = os.path.join(HERE, "assets", "wallpaper.jpg")
WS_SDK_EXE = os.environ.get("M18_WS_SDK") or os.path.normpath(
    os.path.join(HERE, "..", "ws-sdk", "StreamDockWebsocketSDK.exe"))
CONFIG_PATH = os.path.join(HERE, "config.json")
UPLOAD_DIR = os.path.join(HERE, "assets", "uploads")
GIF_CACHE = os.path.join(HERE, "assets", "gifcache")   # 交给 SDK 原生播放的 64×64 动图
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# 15 个键排成 5 列 × 3 行，单键 64×64
KEY_SIZE = 64
GRID_COLS, GRID_ROWS = 5, 3

# 上报码 -> 物理键序号（1 = 左下角，左→右，下→上）
CODE_TO_KEY = {
    0x0B: 1, 0x0C: 2, 0x0D: 3, 0x0E: 4, 0x0F: 5,
    0x06: 6, 0x07: 7, 0x08: 8, 0x09: 9, 0x0A: 10,
    0x01: 11, 0x02: 12, 0x03: 13, 0x04: 14, 0x05: 15,
    0x25: 16, 0x30: 17, 0x31: 18,
}

MODES = ["off", "solid", "rainbow", "breathe", "chase", "gradient", "custom"]


VK_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    # 多媒体键
    "mediaplaypause": 0xB3, "mediaplay": 0xB3, "mediastop": 0xB2,
    "mediaprev": 0xB1, "medianext": 0xB0,
    "volumemute": 0xAD, "volumeup": 0xAF, "volumedown": 0xAE,
    # 其他常用键
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "capslock": 0x14,
    "numlock": 0x90, "scrolllock": 0x91, "apps": 0x5D, "menu": 0x5D,
    "num*": 0x6A, "num+": 0x6B, "num-": 0x6D, "num.": 0x6E, "num/": 0x6F,
    # 兼容旧版录制器按 e.key 存下来的写法
    "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
    "meta": 0x5B, "os": 0x5B, "del": 0x2E, "ins": 0x2D,
}

# 这些键在键盘上属于「扩展键」，不带 KEYEVENTF_EXTENDEDKEY 发出去
# 会被不少程序当成小键盘上的同名键（例如方向键变成小键盘 2/4/6/8）
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,   # PgUp/PgDn/End/Home/方向键
    0x2C, 0x2D, 0x2E,                                 # PrintScreen/Insert/Delete
    0x5B, 0x5D, 0x6F, 0x90,                           # Win/Apps/小键盘除号/NumLock
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,         # 多媒体
}

MEDIA_LABELS = {
    "playpause": ("mediaplaypause", "播放 / 暂停"),
    "next": ("medianext", "下一首"),
    "prev": ("mediaprev", "上一首"),
    "stop": ("mediastop", "停止"),
    "mute": ("volumemute", "静音"),
    "volup": ("volumeup", "音量 +"),
    "voldown": ("volumedown", "音量 −"),
    # 不是系统按键，由控制台自己调屏幕亮度（官方「亮度」动作导入后也落到这里）
    "brightup": ("", "屏幕亮度 +"),
    "brightdown": ("", "屏幕亮度 −"),
}
for _i in range(1, 25):
    VK_NAMES[f"f{_i}"] = 0x6F + _i          # F1–F24
for _i in range(10):
    VK_NAMES[f"num{_i}"] = 0x60 + _i        # 小键盘 0–9
# 旧录制器会把 Shift+1 存成 shift+!：符号映射回它所在的物理键（shift 已在组合里）
for _sym, _base in zip('!@#$%^&*()_{}|:"<>?~', "1234567890-[]\\;',./`"):
    VK_NAMES.setdefault(_sym, VK_NAMES.get(_base) or ord(_base))

KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2


def _key_event(vk: int, up: bool) -> None:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    user32.keybd_event(vk, scan, flags, 0)


def _split_combo(combo: str) -> list:
    """按 + 拆组合键，但允许加号本身作为按键：ctrl++ → [ctrl, +]，num+ → [num+]。"""
    parts, cur = [], ""
    for ch in combo.replace(" ", "").lower():
        if ch == "+" and cur not in ("", "num"):
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def send_hotkey(combo: str) -> bool:
    """用 Win32 keybd_event 发一组组合键，例如 ctrl+shift+s。"""
    parts = _split_combo(combo)
    if not parts:
        return False
    vks = []
    for p in parts:
        if p in VK_NAMES:
            vks.append(VK_NAMES[p])
        elif p == "+":
            vks.append(0xBB)          # 主键盘的 =/+ 键
        elif len(p) == 1 and (p.isalnum()):
            vks.append(ord(p.upper()))
        else:
            return False
    mods = [v for v in vks if v in (0x11, 0x10, 0x12, 0x5B)]
    keys = [v for v in vks if v not in mods]
    if not keys:
        return False
    try:
        for m in mods:
            _key_event(m, up=False)
        for k in keys:
            _key_event(k, up=False)
            _key_event(k, up=True)
        for m in reversed(mods):
            _key_event(m, up=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def action_label(spec: dict) -> str:
    kind = spec.get("type")
    if kind == "nav":
        return spec.get("name", "")
    if kind == "hotkey":
        return spec.get("keys", "")
    if kind == "media":
        return MEDIA_LABELS.get(spec.get("action", ""), ("", ""))[1]
    if kind == "macro":
        return f"宏 · {len(spec.get('steps') or [])} 步"
    if kind == "url":
        return spec.get("url", "")
    path = spec.get("path", "")
    # 盘符 / 以斜杠结尾的文件夹取不到 basename，直接显示路径
    return os.path.basename(path.rstrip("\\/")) or path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pick_path(mode: str = "file") -> str:
    """弹出系统原生的文件/文件夹选择框（浏览器拿不到本地路径，只能由服务端弹）。"""
    if mode == "folder":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description='选择要绑定的文件夹';"
            "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}"
        )
    else:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.OpenFileDialog;"
            "$f.Title='选择程序 / 快捷方式 / 脚本';"
            "$f.Filter='所有文件|*.*';"
            "if($f.ShowDialog() -eq 'OK'){[Console]::Write($f.FileName)}"
        )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        log(f"文件选择框失败：{exc}")
        return ""


def resolve_lnk(path: str) -> str:
    """解析 Windows 快捷方式的目标路径。"""
    script = (
        "$s=New-Object -ComObject WScript.Shell;"
        f"$l=$s.CreateShortcut('{path.replace(chr(39), chr(39)*2)}');"
        "[Console]::Write($l.TargetPath)"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def asset_path(name: str) -> str | None:
    """把配置里的相对路径解析成绝对路径，并限制在项目目录内。"""
    if not name:
        return None
    path = name if os.path.isabs(name) else os.path.join(HERE, name)
    path = os.path.normpath(path)
    if not path.startswith(HERE) or not os.path.isfile(path):
        return None
    return path


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
class Device:
    def __init__(self, on_input):
        self.on_input = on_input
        self.tr = None
        self.lock = threading.RLock()
        self.connected = False
        self.info = {}
        self.error = ""
        self._stop = threading.Event()
        self._threads = []
        self._read_errors = 0
        self._seen_raw: set[str] = set()
        self.stats = {"read": 0, "data": 0, "none": 0, "err": 0}
        self.quiet = False

    # -- 生命周期 ---------------------------------------------------------- #
    def connect(self) -> bool:
        try:
            from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI
        except Exception as exc:  # noqa: BLE001
            self.error = f"设备 SDK 加载失败：{exc}"
            self.connected = False
            log(self.error)
            return False
        self.disconnect()
        self._stop.clear()
        tr = LibUSBHIDAPI()
        found = None
        for vid, pid in CANDIDATES:
            devs = tr.enumerate_devices(vendor_id=vid, product_id=pid)
            if devs:
                found = (vid, pid, devs[0])
                break
        if not found:
            self.error = "未检测到 M18 设备"
            self.connected = False
            return False

        vid, pid, info = found
        if not tr.open(bytes(info["path"], "utf-8")):
            self.error = f"打开设备失败：{tr.get_last_error()}"
            self.connected = False
            return False

        try:
            tr.set_report_size(513, 1025, 0)
            fw = tr.get_firmware_version()
        except Exception as exc:  # noqa: BLE001
            fw = ""
            log(f"读取固件失败: {exc}")

        self.tr = tr
        self.connected = True
        self.error = ""
        self.info = {
            "vid": f"0x{vid:04X}",
            "pid": f"0x{pid:04X}",
            "serial": info.get("serial_number") or "",
            "firmware": fw,
            "keys": KEY_COUNT,
            "leds": LED_COUNT,
            "path": info.get("path", ""),
        }
        for target in (self._read_loop, self._heartbeat_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        log(f"设备已连接 {self.info['vid']}/{self.info['pid']} 固件={fw}")
        return True

    def disconnect(self) -> None:
        self._stop.set()
        with self.lock:
            if self.tr is not None:
                try:
                    self.tr.close()
                except Exception:  # noqa: BLE001
                    pass
            self.tr = None
            self.connected = False
        self._threads = []

    def check_present(self) -> bool:
        """不重开设备，只确认它还在不在总线上。"""
        try:
            from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI

            probe = LibUSBHIDAPI()
            for vid, pid in CANDIDATES:
                if probe.enumerate_devices(vendor_id=vid, product_id=pid):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def mark_lost(self, reason: str) -> None:
        self.error = reason
        log(reason)
        self.disconnect()

    # -- 写入 -------------------------------------------------------------- #
    def write(self, fn, *args):
        with self.lock:
            if not self.connected or self.tr is None:
                return False
            try:
                fn(self.tr, *args)
                return True
            except Exception as exc:  # noqa: BLE001
                self.error = f"写入失败：{exc}"
                log(self.error)
                return False

    def set_leds(self, colors) -> bool:
        return self.write(lambda tr, c: tr.set_single_led_color(c), colors)

    def set_led_brightness(self, value: int) -> bool:
        return self.write(lambda tr, v: tr.set_led_brightness(v), value)

    def reset_leds(self) -> bool:
        return self.write(lambda tr: tr.reset_led_color())

    def set_screen_brightness(self, value: int) -> bool:
        return self.write(lambda tr, v: tr.set_key_brightness(v), value)

    def clear_queue(self) -> bool:
        return self.write(lambda tr: tr.clear_task_queue())

    def change_mode(self, mode: int) -> bool:
        return self.write(lambda tr, m: tr.change_mode(m), mode)

    def send_image(self, key: int, jpeg: bytes) -> bool:
        return self.write(lambda tr, k, data: tr.set_key_image_stream(data, k), key, jpeg)

    def refresh(self) -> bool:
        return self.write(lambda tr: tr.refresh_screen())

    def clear_keys(self) -> bool:
        return self.write(lambda tr: tr.clear_all_keys())

    # -- 读线程 ------------------------------------------------------------ #
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            tr = self.tr
            if tr is None:
                time.sleep(0.1)
                continue
            try:
                data = tr.read(200)
                self._read_errors = 0
                self.stats["read"] += 1
            except Exception:  # noqa: BLE001
                self._read_errors += 1
                self.stats["err"] += 1
                time.sleep(0.2)
                continue
            if data and len(data) < 11:
                raw = " ".join(f"{b:02X}" for b in data[:16])
                if raw not in self._seen_raw and len(self._seen_raw) < 20:
                    self._seen_raw.add(raw)
                    log(f"短包 len={len(data)} {raw}")
                continue
            if not data or len(data) < 11:
                self.stats["none"] += 1
                time.sleep(0.01)      # 读不到时不空转
                continue
            self.stats["data"] += 1
            code, state = data[9], data[10]
            if code in CODE_TO_KEY and state in (0, 1):
                try:
                    self.on_input(CODE_TO_KEY[code], state)
                except Exception as exc:  # noqa: BLE001
                    log(f"输入回调异常: {exc}")
            else:
                raw = " ".join(f"{b:02X}" for b in data[:16])
                if raw not in self._seen_raw:
                    if len(self._seen_raw) < 20:
                        self._seen_raw.add(raw)
                    log(f"未识别输入包 {raw}")

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(8)
            if self._stop.is_set():
                break
            if self.quiet:
                continue
            self.write(lambda tr: tr.heartbeat())


# --------------------------------------------------------------------------- #
# 应用状态 + 灯光引擎
# --------------------------------------------------------------------------- #
def hex_to_rgb(value: str):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        return (0, 0, 0)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def parse_color(value: str):
    """#RRGGBB → (r,g,b)；格式不对返回 None。"""
    v = (value or "").strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        return None
    try:
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return [int(r * 255), int(g * 255), int(b * 255)]


class App:
    def __init__(self):
        self.lock = threading.RLock()
        # 可重入：保存配置时会顺带读配置写回场景
        self._save_lock = threading.RLock()
        self._gif_lock = threading.RLock()
        self._gif_active: dict[int, str] = {}   # 正在由 SDK 原生播放的键 → GIF 文件
        self._gif_session = -1
        self.official_root_cfg = ""      # config.json 里手动指定的官方软件目录
        self.clients: list[queue.Queue] = []
        self.events = deque(maxlen=40)
        self.device = DeviceWS(self._on_input, exe=WS_SDK_EXE)
        self.device.quiet = False
        self.lighting = {
            "mode": "rainbow",
            "color": "#00aaff",
            "speed": 1.0,
            "brightness": 100,
            "custom": [[0, 0, 0] for _ in range(LED_COUNT)],
        }
        self.screen_brightness = 100
        self.wallpaper_enabled = True
        self.wallpaper_flip = False
        # 屏幕之间的物理间隙（按 64px 键面为单位的像素当量），0 表示不做边框补偿
        self.wallpaper_gap = 6
        self.preview_mode = "wallpaper"
        self.wallpaper_file = WALLPAPER
        self.wallpaper_fit = "cover"
        self.wallpaper_zoom = 1.0
        self.wallpaper_ox = 0
        self.wallpaper_oy = 0
        self.pages: list[dict] = [{"name": "第 1 页", "images": {}, "actions": {}}]
        self.page = 0
        self._last_paint = 0.0
        self.key_actions = self.pages[0]["actions"]
        self.key_overrides = self.pages[0]["images"]
        self.key_styles = self.pages[0].setdefault("styles", {})
        self.led_offset = 20
        self.led_reverse = False
        self.rotation = 0
        self.effect_reverse = False
        self.input_muted = False
        self.repaint_on_press = False
        self.keep_awake = False
        self.standby_sec = 300
        self.standby_brightness = 60
        self.in_standby = False
        self.selected_key = 0
        self.screen_off = False
        self.yielded = False
        self.clipboard = None
        self.custom_scene_name = "自定义"
        self.active_scene = ""
        self._suspend_snapshot = False
        self._applying_scene = False
        startup_lnk = os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs\Startup",
            "Stream Dock M18 控制台.lnk",
        )
        self.autostart = os.path.isfile(startup_lnk)
        self._last_activity = time.time()
        self.clear_before_paint = False
        self.writes_paused = False
        # 仅灯光模式：不碰按键画面，只驱动灯带（和官方软件共存时用）
        self.lighting_only = True
        self.quiet = False
        self._num_cache: dict[int, bytes] = {}
        self._calib_until = 0.0
        self._identify = None
        self._pushing = False
        self._load_config()
        self._bind_page()
        self._ensure_custom_snapshot()
        for page in self.pages:
            for k, name in ((16, "上一页"), (17, "主页"), (18, "下一页")):
                page["actions"].setdefault(k, {"type": "nav", "name": name})
        self._dirty = True
        self._t0 = time.time()
        self._frame = [[0, 0, 0] for _ in range(LED_COUNT)]
        self._frame_hash = None
        self._last_push = 0.0
        self.official_running = self._detect_official()
        self.device.connect()
        if self.device.connected and not self.quiet and not self.yielded:
            self.device.set_screen_brightness(self.screen_brightness)
            self.device.set_led_brightness(self.lighting["brightness"])
            if self.wallpaper_enabled:
                self.apply_keys()
        threading.Thread(target=self._engine_loop, daemon=True).start()
        threading.Thread(target=self._animation_loop, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        threading.Thread(target=self._standby_loop, daemon=True).start()
        threading.Thread(target=self._diag_loop, daemon=True).start()
        threading.Thread(target=self._official_loop, daemon=True).start()
        threading.Thread(target=self._auto_sync_official, daemon=True).start()
        threading.Thread(target=self._warm_gif_cache, daemon=True).start()
        threading.Thread(target=self.index_official_sources, daemon=True).start()

    # -- 热插拔 ------------------------------------------------------------ #
    def _watch_loop(self) -> None:
        """热插拔：定期确认设备还在总线上，拔掉立刻更新状态。"""
        misses = 0
        while True:
            time.sleep(3)
            try:
                if self.device.connected:
                    if self.device.check_present():
                        misses = 0
                    else:
                        misses += 1
                        if misses >= 2:
                            misses = 0
                            self.device.mark_lost("设备已断开")
                            self.broadcast({"type": "status", "connected": False})
                    continue
                if self.yielded:
                    continue          # 让位给官方驱动，别去抢设备
                if self.device.connect():
                    self._restore_all()
            except Exception as exc:  # noqa: BLE001
                log(f"热插拔监测异常：{exc}")

    def _restore_all(self) -> None:
        log("设备已接入，重新接管…")
        try:
            if self.quiet or self.lighting_only:
                log("仅灯光/静默模式：不写按键画面")
                self.device.set_led_brightness(self.lighting["brightness"])
                self.broadcast({"type": "status", "connected": True})
                return
            self.preview_mode = "wallpaper"
            self.device.set_screen_brightness(self.screen_brightness)
            self.device.set_led_brightness(self.lighting["brightness"])
            self._dirty = True
            # 唤醒一次：设备醒着才会开始上报按键
            self.device.wakeup()
            time.sleep(0.5)
            self.apply_keys()
            self.broadcast({"type": "status", "connected": True})
            threading.Thread(target=self._panel_device, daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log(f"重新接管失败：{exc}")

    # -- 按键画面 ---------------------------------------------------------- #
    # -- 待机 -------------------------------------------------------------- #
    def _draw_text(self, img, style: dict):
        """把标题文字画到图像上（颜色 / 透明度 / 字号 / 上下位置）。"""
        text = (style.get("text") or "").strip()
        if not text:
            return img
        fg = parse_color(style.get("color") or "#e8eefc") or (232, 238, 252)
        opacity = max(0, min(100, int(style.get("opacity", 100))))
        size = max(8, min(40, int(style.get("size", 20))))
        pos = style.get("pos") or "center"
        font = None
        for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\segoeuib.ttf"):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:  # noqa: BLE001
                continue
        if font is None:
            font = ImageFont.load_default()
        overlay = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        shadow = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        lines = [text]
        if od.textlength(text, font=font) > KEY_SIZE - 6 and len(text) > 3:
            mid = (len(text) + 1) // 2
            lines = [text[:mid], text[mid:]]
        line_h = size + 3
        total = line_h * len(lines)
        if pos == "top":
            y = 3
        elif pos == "bottom":
            y = KEY_SIZE - total - 3
        else:
            y = (KEY_SIZE - total) / 2
        alpha = int(255 * opacity / 100)
        for line in lines:
            w = od.textlength(line, font=font)
            x = (KEY_SIZE - w) / 2
            sd.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0, int(alpha * 0.55)))
            od.text((x, y), line, font=font, fill=(*fg, alpha))
            y += line_h
        img = Image.alpha_composite(img.convert("RGBA"), shadow)
        return Image.alpha_composite(img, overlay).convert("RGB")

    def _styled_frame(self, key: int, jpeg: bytes) -> bytes:
        """动图帧也要叠标题，保证控制台和屏幕一致。"""
        style = self.key_styles.get(key) or {}
        if not (style.get("text") or "").strip():
            return jpeg
        ck = (key, hash(jpeg), style.get("text"), style.get("color"),
              style.get("opacity"), style.get("size"), style.get("pos"))
        cache = getattr(self, "_frame_text_cache", None)
        if cache is None:
            cache = self._frame_text_cache = {}
        if ck in cache:
            return cache[ck]
        try:
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception:  # noqa: BLE001
            return jpeg
        img = self._draw_text(img, style)
        if self.selected_key == key:
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, KEY_SIZE - 1, KEY_SIZE - 1], outline=(86, 148, 255))
            d.rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], outline=(140, 185, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        data = buf.getvalue()
        if len(cache) > 800:
            cache.clear()
        cache[ck] = data
        return data

    def _number_tile(self, key: int) -> bytes:
        """按键画面：自定义图 / 底色 + 叠加文字（颜色、透明度、字号、位置）。"""
        style = self.key_styles.get(key) or {}
        text = (style.get("text") or "").strip()
        bg = parse_color(style.get("bg") or "#000000") or (0, 0, 0)
        fg = parse_color(style.get("color") or "#e8eefc") or (232, 238, 252)
        opacity = max(0, min(100, int(style.get("opacity", 100))))
        size = max(8, min(40, int(style.get("size", 20))))
        pos = style.get("pos") or "center"
        base = self._key_jpeg(key)
        ck = (key, text, bg, fg, opacity, size, pos, hash(base) if base else 0)
        if ck in self._num_cache:
            return self._num_cache[ck]

        if base:
            try:
                img = Image.open(io.BytesIO(base)).convert("RGB")
                img = ImageOps.fit(img, (KEY_SIZE, KEY_SIZE), Image.LANCZOS)
            except Exception:  # noqa: BLE001
                img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), bg)
        else:
            img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), bg)
            d0 = ImageDraw.Draw(img)
            d0.rounded_rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], radius=8,
                                 outline=(max(bg[0], 30), max(bg[1], 36), max(bg[2], 48)), width=1)

        img = self._draw_text(img, dict(style, text=text or ("" if base else str(key))))

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        self._num_cache[ck] = data
        return data

    def set_key_style(self, key: int, spec: dict) -> bool:
        if not (1 <= key <= KEY_COUNT):
            return False
        self._anim_pause_until = time.time() + 1.0
        style = self.key_styles.setdefault(key, {})
        if "text" in spec:
            style["text"] = str(spec["text"])[:12]
        if "color" in spec and parse_color(str(spec["color"])):
            style["color"] = str(spec["color"])
        if "bg" in spec and parse_color(str(spec["bg"])):
            style["bg"] = str(spec["bg"])
        for field, lo, hi in (("opacity", 0, 100), ("size", 8, 40)):
            if field in spec:
                try:
                    style[field] = max(lo, min(hi, int(spec[field])))
                except (TypeError, ValueError):
                    pass
        if "pos" in spec and str(spec["pos"]) in ("top", "center", "bottom"):
            style["pos"] = str(spec["pos"])
        # 全默认就当没设置过
        if (not (style.get("text") or "").strip()
                and (style.get("color") or "#e8eefc") == "#e8eefc"
                and (style.get("bg") or "#000000") == "#000000"
                and int(style.get("opacity", 100)) == 100
                and int(style.get("size", 20)) == 20
                and (style.get("pos") or "center") == "center"):
            self.key_styles.pop(key, None)
        self._save_config()
        if self.device.connected:
            self._gif_update_key(key)          # 标题变了，动图要重新叠字
            self.device.send_image(key, self._number_tile(key))
            self.device.refresh()
        return True

    def _render_key(self, key: int) -> bytes:
        """按键最终画面：自定义图/底色 + 叠加文字，选中时再叠内边缘高亮。"""
        base = self._number_tile(key)
        if self.selected_key != key:
            return base
        cached = getattr(self, "_sel_cache", None)
        if cached and cached[0] == key and cached[1] == base:
            return cached[2]
        try:
            img = Image.open(io.BytesIO(base)).convert("RGB")
        except Exception:  # noqa: BLE001
            return base
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, KEY_SIZE - 1, KEY_SIZE - 1], outline=(86, 148, 255))
        d.rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], outline=(140, 185, 255))
        d.rectangle([2, 2, KEY_SIZE - 3, KEY_SIZE - 3], outline=(52, 96, 170))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        self._sel_cache = (key, base, data)
        return data

    def select_key(self, key: int) -> bool:
        """控制台选中某个键时，让设备上对应的屏幕亮一圈内边缘。"""
        if not (0 <= key <= KEY_COUNT + 3):
            return False
        old = self.selected_key
        if old == key:
            return True
        self.selected_key = key
        self._anim_pause_until = time.time() + 1.2
        if self.device.connected and not self.in_standby:
            for k in (old, key):
                # SDK 正在原生播放的动图键不重画，免得闪一下静态帧
                if k and 1 <= k <= KEY_COUNT and k not in self._gif_active:
                    self.device.send_image(k, self._render_key(k))
            self.device.refresh()
        return True

    def _key_jpeg(self, key: int) -> bytes | None:
        item = self.key_overrides.get(key)
        if not item:
            return None
        if item.get("type") == "image":
            return item["jpeg"]
        frames = item.get("frames") or []
        return frames[item["idx"]][0] if frames else None

    def _clear_screen(self) -> None:
        """M18 上清屏容易把设备送进异常状态，这里保留开关但默认不做。"""
        if not getattr(self, "clear_before_paint", False):
            return
        try:
            self.device.clear_keys()
        except Exception:  # noqa: BLE001
            pass

    def apply_keys(self) -> bool:
        """正常状态：每个键显示自己的图（没有就用编号占位）。"""
        if not self.device.connected or self.writes_paused or self.quiet or self.lighting_only or self.yielded:
            return False
        if self.screen_off:
            self._gif_stop_all()
            try:
                self.device.set_screen_brightness(0)
            except Exception:  # noqa: BLE001
                pass
            return False
        self._gif_stop_all()
        # 先发亮度命令唤醒面板，否则设备睡过去后写进去的图不会显示
        try:
            self.device.set_screen_brightness(self.screen_brightness)
            time.sleep(0.06)
        except Exception:  # noqa: BLE001
            pass
        self.in_standby = False
        ok = True
        self._pushing = True
        try:
            self.device.clear_queue()
            self._clear_screen()
            for key in range(1, KEY_COUNT + 1):
                jpeg = self._render_key(key)
                if not self.device.send_image(key, jpeg):
                    ok = False
            self.device.refresh()
        finally:
            self._pushing = False
        self._gif_start_all()
        self._last_paint = time.time()
        self._last_activity = time.time()   # 刚画完键面就重置待机计时，避免立刻又进待机
        caller = sys._getframe(1).f_code.co_name
        log(f"重画按键画面（自定义 {len(self.key_overrides)} 个，原生动图 {len(self._gif_active)} 个）来自 {caller}"
            + ("" if ok else "  ⚠ 有命令没发出去"))
        return ok

    # -- 动图：交给 SDK 原生播放 ------------------------------------------- #
    def _gif_allowed(self) -> bool:
        d = self.device
        return bool(getattr(d, "gif_native", False) and d.connected
                    and not (self.writes_paused or self.quiet or self.lighting_only or self.yielded
                             or self.screen_off or self.in_standby))

    def _gif_sync_session(self) -> None:
        """设备重连后 SDK 里的 GIF 已被清空，本地记录也要作废。"""
        session = getattr(self.device, "session", 0)
        if session != self._gif_session:
            self._gif_session = session
            self._gif_active = {}

    def _gif_path_for(self, key: int, item: dict | None, style: dict | None = None) -> str | None:
        """把动图渲染成 64×64 GIF（叠好标题），按内容缓存，交给 SDK 直接播。"""
        if not item or item.get("type") != "anim":
            return None
        src = item.get("file") or ""
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            return None
        if style is None:
            style = self.key_styles.get(key) or {}
        text = {k: style.get(k) for k in ("text", "color", "opacity", "size", "pos")} \
            if (style.get("text") or "").strip() else {}
        sig = hashlib.sha1(json.dumps([os.path.abspath(src), mtime, text], sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()[:20]
        out = os.path.join(GIF_CACHE, f"{sig}.gif")
        if os.path.isfile(out):
            return out
        frames, durations = [], []
        try:
            with Image.open(src) as im:
                for i, frame in enumerate(ImageSequence.Iterator(im)):
                    if i >= 240:
                        break
                    rgb = ImageOps.fit(self._flat_rgb(frame), (KEY_SIZE, KEY_SIZE),
                                       Image.Resampling.LANCZOS)
                    if text:
                        rgb = self._draw_text(rgb, text)
                    frames.append(rgb)
                    durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
            if len(frames) < 2:
                return None
            os.makedirs(GIF_CACHE, exist_ok=True)
            tmp = out + ".tmp"
            frames[0].save(tmp, "GIF", save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, disposal=1, optimize=False)
            os.replace(tmp, out)
            return out
        except Exception as exc:  # noqa: BLE001
            log(f"动图转换失败 {src}: {exc}")
            return None

    def _warm_gif_cache(self) -> None:
        """后台把所有场景里的动图预先转好，第一次切场景 / 翻页也不用等渲染。"""
        time.sleep(3)
        made = 0
        for f in glob.glob(os.path.join(SCENE_DIR, "*.json")):
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    cfg = json.load(fh)
            except (OSError, ValueError):
                continue
            for page in cfg.get("pages") or []:
                styles = page.get("styles") or {}
                for key, name in (page.get("images") or {}).items():
                    path = os.path.join(UPLOAD_DIR, os.path.basename(str(name)))
                    if not path.lower().endswith(".gif") or not os.path.isfile(path):
                        continue
                    try:
                        with Image.open(path) as im:
                            if getattr(im, "n_frames", 1) < 2:
                                continue
                    except Exception:  # noqa: BLE001
                        continue
                    before = len(os.listdir(GIF_CACHE)) if os.path.isdir(GIF_CACHE) else 0
                    self._gif_path_for(int(key), {"type": "anim", "file": path},
                                       styles.get(str(key)) or {})
                    made += (len(os.listdir(GIF_CACHE)) if os.path.isdir(GIF_CACHE) else 0) - before
        if made:
            log(f"动图预渲染完成：新增 {made} 个")

    def _gif_stop_all(self) -> None:
        """整屏换画面（待机 / 壁纸 / 息屏 / 重画）前先停掉 SDK 的动图，否则会盖掉新画面。"""
        with self._gif_lock:
            self._gif_sync_session()
            if not self._gif_active:
                return
            self.device.stop_gif_loop()
            for key in list(self._gif_active):
                self.device.clear_key_gif(key)
            self._gif_active = {}

    def _gif_start_all(self) -> None:
        with self._gif_lock:
            self._gif_sync_session()
            if not self._gif_allowed():
                return
            want = {}
            for key, item in self.key_overrides.items():
                if 1 <= key <= KEY_COUNT:
                    path = self._gif_path_for(key, item)
                    if path:
                        want[key] = path
            for key in [k for k in self._gif_active if k not in want]:
                self.device.clear_key_gif(key)
            for key, path in want.items():
                if self._gif_active.get(key) != path:
                    self.device.set_key_gif(key, path)
            self._gif_active = want
            if want:
                self.device.start_gif_loop()
            else:
                self.device.stop_gif_loop()

    def _gif_update_key(self, key: int) -> None:
        """单个键换图 / 改标题：只动这一个键。要在写静态画面之前调用。"""
        with self._gif_lock:
            self._gif_sync_session()
            want = self._gif_path_for(key, self.key_overrides.get(key)) if self._gif_allowed() else None
            old = self._gif_active.get(key)
            if old == want:
                return
            if old:
                self.device.clear_key_gif(key)
                self._gif_active.pop(key, None)
            if want:
                self.device.set_key_gif(key, want)
                self._gif_active[key] = want
                self.device.start_gif_loop()
            elif not self._gif_active:
                self.device.stop_gif_loop()

    def apply_standby(self) -> bool:
        """待机：整屏铺壁纸（壁纸亮度在渲染层调暗，屏幕亮度保持正常）。"""
        self.in_standby = True
        return self._push_canvas("待机图")

    def _diag_loop(self) -> None:
        last = dict(self.device.stats)
        while True:
            time.sleep(20)
            cur = dict(self.device.stats)
            delta = {k: cur[k] - last[k] for k in cur}
            last = cur
            if self.device.connected:
                log(f"读取统计 {delta}")

    def probe_inputs(self, seconds: int = 15) -> None:
        """并发读取所有 HID 接口若干秒，期间按键就能看出上报走的是哪个口。"""
        try:
            from StreamDock.Transport import LibUSBHIDAPI as L

            lib = L._transport_lib
            lib.transport_hid_enumerate.restype = ctypes.POINTER(L._HidDeviceInfo)
            lib.transport_hid_enumerate.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
            lib.transport_hid_free_enumeration.argtypes = [ctypes.POINTER(L._HidDeviceInfo)]

            ptr = lib.transport_hid_enumerate(0x6603, 0x1012)
            entries = []
            cur = ptr
            while cur:
                info = cur.contents
                entries.append((
                    info.path.decode("utf-8", "ignore") if info.path else "",
                    info.usage_page, info.usage, info.interface_number,
                ))
                if not info.next:
                    break
                cur = info.next
            lib.transport_hid_free_enumeration(ptr)
        except Exception as exc:  # noqa: BLE001
            log(f"输入探测失败：{exc}")
            return

        log(f"开始探测 {seconds} 秒，共 {len(entries)} 个接口 —— 请现在按键")
        for path, up, us, itf in entries:
            log(f"  接口 iface={itf} usage_page=0x{up:04X} usage={us}")

        def reader(path, itf, deadline):
            try:
                tr = L.LibUSBHIDAPI()
                if not tr.open(path.encode()):
                    log(f"  iface={itf} 打不开（可能被主进程占用）")
                    return
                try:
                    tr.set_report_size(513, 1025, 0)
                except Exception:  # noqa: BLE001
                    pass
                got = 0
                seen = set()
                while time.time() < deadline:
                    d = tr.read(100)
                    if d:
                        got += 1
                        raw = " ".join(f"{b:02X}" for b in d[:16])
                        if raw not in seen and len(seen) < 8:
                            seen.add(raw)
                            log(f"  ★iface={itf} 收到 len={len(d)} {raw}")
                    else:
                        time.sleep(0.01)
                tr.close()
                log(f"  iface={itf} 结束，收到 {got} 个包")
            except Exception as exc:  # noqa: BLE001
                log(f"  iface={itf} 异常：{exc}")

        deadline = time.time() + seconds
        threads = [
            threading.Thread(target=reader, args=(p, itf, deadline), daemon=True)
            for p, up, us, itf in entries
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _standby_loop(self) -> None:
        while True:
            time.sleep(2)
            try:
                if (self.wallpaper_enabled and self.standby_sec > 0
                        and not self.in_standby and not self.screen_off
                        and self.device.connected and not self.quiet
                        and time.time() - self._last_activity > self.standby_sec):
                    self.apply_standby()
                elif (self.in_standby and self.device.connected
                      and time.time() - self._last_paint > 25):
                    # 长时间待机时定期补一帧，避免设备自己睡过去显示开机图
                    self.apply_standby()
            except Exception:  # noqa: BLE001
                pass

    def blank_screen(self) -> None:
        """把所有键清成空白，确保息屏时屏幕上没有任何内容。"""
        self._gif_stop_all()
        try:
            self.device.clear_keys()
            self.device.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _panel_device(self):
        """常驻一条 transport 句柄专门发开关屏指令，避免每次开关都重新枚举（很慢）。"""
        dev = getattr(self, "_panel_dev", None)
        if dev is not None and dev.connected:
            return dev
        try:
            dev = Device(lambda k, s: None)
            if not dev.connect():
                return None
        except Exception:  # noqa: BLE001
            return None
        self._panel_dev = dev
        return dev

    def sleep_panel(self) -> bool:
        dev = self._panel_device()
        if not dev:
            return False
        try:
            return bool(dev.write(lambda tr: tr.sleep()))
        except Exception:  # noqa: BLE001
            return False

    def wake_panel(self) -> bool:
        dev = self._panel_device()
        if not dev:
            return False
        try:
            return bool(dev.write(lambda tr: tr.wakeup_screen()))
        except Exception:  # noqa: BLE001
            return False

    def _wake_from_standby(self) -> None:
        """按键唤醒：先叫醒设备，再整屏重画。"""
        # 打开第二个句柄会有扰动，只在确实很久没画过时才用
        if time.time() - self._last_paint > 30:
            self.wake_panel()
        try:
            self.device.set_screen_brightness(self.screen_brightness)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
        self.apply_keys()

    def mark_activity(self) -> None:
        self._last_activity = time.time()
        if self.in_standby:
            threading.Thread(target=self.apply_keys, daemon=True).start()

    # -- 页面 -------------------------------------------------------------- #
    def _bind_page(self) -> None:
        page = self.pages[self.page]
        self.key_overrides = page["images"]
        self.key_actions = page["actions"]
        self.key_styles = page.setdefault("styles", {})

    def goto_page(self, index: int) -> bool:
        if not self.pages:
            return False
        index = max(0, min(len(self.pages) - 1, index))
        if index == self.page:
            return False
        self.page = index
        self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        log(f"切换到第 {self.page + 1} 页")
        return True

    def add_page(self) -> int:
        self.pages.append({
            "name": f"第 {len(self.pages) + 1} 页",
            "images": {}, "actions": {
                16: {"type": "nav", "name": "上一页"},
                17: {"type": "nav", "name": "主页"},
                18: {"type": "nav", "name": "下一页"},
            }, "styles": {},
        })
        self._save_config()
        return len(self.pages) - 1

    def delete_page(self, index: int) -> bool:
        if len(self.pages) <= 1:
            return False
        index = max(0, min(len(self.pages) - 1, index))
        self.pages.pop(index)
        if self.page >= len(self.pages):
            self.page = len(self.pages) - 1
        elif self.page > index:
            self.page -= 1
        self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        log(f"删除第 {index + 1} 页，现在共 {len(self.pages)} 页")
        return True

    def rename_page(self, index: int, name: str) -> bool:
        name = (name or "").strip()[:20]
        if not (0 <= index < len(self.pages)) or not name:
            return False
        self.pages[index]["name"] = name
        self._save_config()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        return True

    def add_nav_pages(self, name: str) -> None:
        if name == "下一页":
            if self.page + 1 < len(self.pages):
                self.goto_page(self.page + 1)
        elif name == "上一页":
            self.goto_page(self.page - 1)
        elif name == "主页":
            self.goto_page(0)

    def _read_config_file(self) -> dict:
        """读 config.json。Windows 上文件正被原子替换的瞬间会 PermissionError，
        这不是损坏，重试即可——以前这里直接回退到旧备份，刚改的设置（如待机时间）就被冲掉了。"""
        last = None
        for _ in range(20):
            try:
                with self._save_lock:
                    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
                        data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except PermissionError as exc:
                last = exc
                time.sleep(0.05)
        raise last

    def _load_config(self) -> None:
        try:
            cfg = self._read_config_file()
        except (OSError, ValueError) as exc:
            example = os.path.join(HERE, "config.example.json")
            if not os.path.isfile(CONFIG_PATH) and os.path.isfile(example):
                try:
                    shutil.copyfile(example, CONFIG_PATH)
                    log("已从 config.example.json 初始化配置")
                except OSError:
                    pass
            # 配置损坏时，优先用最近的自动备份，而不是回退到默认值
            log(f"读取配置失败：{exc}，尝试最近的自动备份…")
            cfg = {}
            for name in self.list_config_backups():
                try:
                    with open(os.path.join(CONFIG_BAK_DIR, name), encoding="utf-8-sig") as fh:
                        cfg = json.load(fh)
                    log(f"已从备份恢复配置：{name}")
                    break
                except (OSError, ValueError):
                    continue
        try:
            self.wallpaper_gap = max(0, min(64, int(cfg.get("gap", self.wallpaper_gap))))
            self.wallpaper_flip = bool(cfg.get("flip", self.wallpaper_flip))
            self.led_offset = int(cfg.get("ledOffset", 20)) % (LED_COUNT - 2)
            self.rotation = int(cfg.get("rotation", 0)) % 360
            self.effect_reverse = bool(cfg.get("effectReverse", False))
            if self.rotation not in (0, 90, 180, 270):
                self.rotation = 0
            fit = str(cfg.get("fit", "cover"))
            self.wallpaper_fit = fit if fit in ("cover", "contain", "fill") else "cover"
            self.wallpaper_zoom = max(0.2, min(4.0, float(cfg.get("zoom", 1.0))))
            self.wallpaper_ox = int(cfg.get("ox", 0))
            self.wallpaper_oy = int(cfg.get("oy", 0))
            self.standby_sec = max(5, min(1800, int(cfg.get("standby", 300))))
            self.standby_brightness = max(0, min(100, int(cfg.get("standbyBrightness", 60))))
            self.custom_scene_name = str(cfg.get("customScene") or "自定义")[:40]
            self.active_scene = str(cfg.get("activeScene") or "")
            self.lighting_only = bool(cfg.get("lightingOnly", True))
            self.wallpaper_enabled = bool(cfg.get("wallpaperEnabled", self.wallpaper_enabled))
            pages = cfg.get("pages")
            if isinstance(pages, list) and pages:
                self.pages = []
                for p in pages:
                    self.pages.append({
                        "name": str(p.get("name") or "页"),
                        "images": {},
                        "actions": {},
                        "styles": {int(k): v for k, v in (p.get("styles") or {}).items()
                                   if str(k).isdigit()},
                    })
                for idx, p in enumerate(pages):
                    for key, name in (p.get("images") or {}).items():
                        path = os.path.join(UPLOAD_DIR, name)
                        if not os.path.isfile(path):
                            path = os.path.join(ICON_DIR, name)
                        if os.path.isfile(path):
                            data = self._load_image_source(path)
                            if data:
                                self.pages[idx]["images"][int(key)] = data
                    for key, spec in (p.get("actions") or {}).items():
                        k = int(key)
                        if isinstance(spec, dict):
                            self.pages[idx]["actions"][k] = spec
                        elif spec:
                            self.pages[idx]["actions"][k] = {"type": "launch", "path": str(spec)}
                self.page = max(0, min(len(self.pages) - 1, int(cfg.get("page", 0))))
            else:
                # 旧版单页配置迁移
                for key, spec in (cfg.get("actions") or {}).items():
                    k = int(key)
                    if isinstance(spec, dict):
                        self.pages[0]["actions"][k] = spec
                    elif spec:
                        self.pages[0]["actions"][k] = {"type": "launch", "path": str(spec)}
                for key, name in (cfg.get("keys") or {}).items():
                    path = os.path.join(UPLOAD_DIR, name)
                    if os.path.isfile(path):
                        data = self._load_image_source(path)
                        if data:
                            self.pages[0]["images"][int(key)] = data
                self.page = 0
            self.led_reverse = bool(cfg.get("ledReverse", False))
            root = str(cfg.get("officialRoot") or "")
            if root and os.path.isdir(root):
                global SD_ROOT
                SD_ROOT = root
                self.official_root_cfg = root
            wp = cfg.get("wallpaper")
            if wp:
                path = wp if os.path.isabs(wp) else os.path.join(HERE, wp)
                if os.path.isfile(path):
                    self.wallpaper_file = path
        except (OSError, ValueError, TypeError):
            pass

    def _backup_config(self) -> None:
        """每次保存前留一份配置快照，最多保留 20 份。"""
        try:
            if not os.path.isfile(CONFIG_PATH):
                return
            now = time.time()
            if now - getattr(self, "_last_backup", 0) < 30:
                return
            self._last_backup = now
            os.makedirs(CONFIG_BAK_DIR, exist_ok=True)
            with open(CONFIG_PATH, "rb") as fh:
                data = fh.read()
            if not data.strip():
                return
            dst = os.path.join(CONFIG_BAK_DIR, f"config-{time.strftime('%Y%m%d-%H%M%S')}.json")
            if not os.path.exists(dst):
                with open(dst, "wb") as fh:
                    fh.write(data)
            files = sorted(glob.glob(os.path.join(CONFIG_BAK_DIR, "config-*.json")))
            for old in files[:-20]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def list_config_backups(self) -> list:
        try:
            files = sorted(glob.glob(os.path.join(CONFIG_BAK_DIR, "config-*.json")), reverse=True)
            return [os.path.basename(f) for f in files[:20]]
        except Exception:  # noqa: BLE001
            return []

    def restore_config_backup(self, name: str) -> bool:
        safe = os.path.basename(name or "")
        path = os.path.join(CONFIG_BAK_DIR, safe)
        if not safe.startswith("config-") or not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        return self.import_config(data)

    def _save_config(self) -> None:
        # 多个请求线程可能同时保存，共用同一个 .tmp 会互相覆盖 / os.replace 报错
        with self._save_lock:
            self._save_config_locked()

    def _save_config_locked(self) -> None:
        self._backup_config()
        cfg = {
            "gap": self.wallpaper_gap,
            "flip": self.wallpaper_flip,
            "ledOffset": self.led_offset,
            "ledReverse": self.led_reverse,
            "rotation": self.rotation,
            "effectReverse": self.effect_reverse,
            "fit": self.wallpaper_fit,
            "zoom": self.wallpaper_zoom,
            "ox": self.wallpaper_ox,
            "oy": self.wallpaper_oy,
            "page": self.page,
            "standby": self.standby_sec,
            "standbyBrightness": self.standby_brightness,
            "customScene": self.custom_scene_name,
            "activeScene": self.active_scene,
            "lightingOnly": self.lighting_only,
            "wallpaperEnabled": self.wallpaper_enabled,
            "pages": [
                {
                    "name": p.get("name", "页"),
                    "images": {str(k): os.path.basename(v.get("file", "") or "")
                               for k, v in p["images"].items() if v.get("file")},
                    "actions": {str(k): v for k, v in p["actions"].items()},
                    "styles": {str(k): v for k, v in (p.get("styles") or {}).items()},
                }
                for p in self.pages
            ],
            "wallpaper": os.path.relpath(self.wallpaper_file, HERE) if self.wallpaper_file else "",
        }
        if self.official_root_cfg:
            cfg["officialRoot"] = self.official_root_cfg
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CONFIG_PATH)   # 原子替换，避免写一半被打断导致配置损坏
            self._snapshot_custom_scene()
        except OSError as exc:
            log(f"保存配置失败：{exc}")

    # -- 灯珠空间映射 ------------------------------------------------------ #
    def _to_hw(self, colors):
        """逻辑顺序（UI 上按一圈排列）→ 设备实际灯珠顺序。

        外圈是 22 颗（与设备一致），只在这一圈里旋转；正面 2 颗固定不动。
        """
        ring = LED_COUNT - 2
        out = [None] * LED_COUNT
        for i in range(ring):
            j = (self.led_offset - i) % ring if self.led_reverse \
                else (self.led_offset + i) % ring
            out[j] = colors[i]
        out[ring] = colors[ring]
        out[ring + 1] = colors[ring + 1]
        return out

    # -- 按键素材 ---------------------------------------------------------- #
    @staticmethod
    def _flat_rgb(img):
        """带透明通道的图先铺黑底再转 RGB（直接 convert 会露出透明区域的杂色）。"""
        if img.mode in ("RGBA", "LA", "P") and (img.mode != "P" or "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            bg.alpha_composite(rgba)
            return bg.convert("RGB")
        return img.convert("RGB")

    def _load_image_source(self, path: str):
        try:
            img = Image.open(path)
            try:
                n = int(getattr(img, "n_frames", 1))
            except Exception:  # noqa: BLE001
                n = 1
            if n > 1:
                frames = []
                for i in range(min(n, 80)):
                    img.seek(i)
                    frame = ImageOps.fit(self._flat_rgb(img),
                                         (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    frame.save(buf, "JPEG", quality=70, subsampling=2)
                    dur = int(img.info.get("duration", 100) or 100)
                    frames.append((buf.getvalue(), max(40, min(1000, dur))))
                return {"type": "anim", "frames": frames, "idx": 0, "next": 0.0, "file": path}
            static = ImageOps.fit(self._flat_rgb(img), (KEY_SIZE, KEY_SIZE),
                                  Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            static.save(buf, "JPEG", quality=90, subsampling=0)
            return {"type": "image", "jpeg": buf.getvalue(), "file": path}
        except Exception as exc:  # noqa: BLE001
            log(f"图片读取失败 {path}: {exc}")
            return None

    def set_wallpaper_file(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        self.wallpaper_file = path
        self._save_config()
        return self.apply_wallpaper()

    def restore_default_wallpaper(self) -> bool:
        """从校准图等临时画面回到我自己的壁纸，并强制清一次屏去掉残留。"""
        self.preview_mode = "wallpaper"
        if os.path.isfile(WALLPAPER):
            self.wallpaper_file = WALLPAPER
        self._save_config()
        self.device.clear_keys()
        self.device.refresh()
        return self.apply_wallpaper()

    def set_key_image(self, key: int, path: str) -> bool:
        if not (1 <= key <= KEY_COUNT) or not os.path.isfile(path):
            return False
        data = self._load_image_source(path)
        if data is None:
            return False
        self.key_overrides[key] = data
        self._save_config()
        self._apply_key(key)
        self.device.refresh()
        return True

    def clear_key(self, key: int) -> bool:
        if key not in self.key_overrides:
            return False
        self.key_overrides.pop(key, None)
        self._save_config()
        return self.apply_wallpaper()

    def _apply_key(self, key: int) -> None:
        self._gif_update_key(key)
        if not self.key_overrides.get(key):
            return
        self.device.send_image(key, self._render_key(key))

    def _apply_overrides(self) -> None:
        for key in list(self.key_overrides):
            self._apply_key(key)

    def _keepalive_loop(self) -> None:
        """低频唤醒（30 秒），只用来维持设备醒着；高频会让它反而睡过去。"""
        while True:
            time.sleep(30)
            try:
                if self.keep_awake and self.device.connected:
                    self.device.wakeup()
            except Exception:  # noqa: BLE001
                pass

    def _animation_loop(self) -> None:
        """按键动图：主机侧逐帧推送（设备本身不循环播放）。"""
        last_refresh = 0.0
        while True:
            time.sleep(0.012)
            if (not self.device.connected or not self.key_overrides
                    or self.in_standby or self.lighting_only
                    or self.quiet or self.writes_paused or self.yielded
                    or self.screen_off
                    or time.time() < getattr(self, "_anim_pause_until", 0)):
                continue
            now = time.time()
            pushed = 0
            # SDK 原生播放的键不归这里管
            host_keys = [k for k, it in self.key_overrides.items()
                         if it.get("type") == "anim" and it.get("frames") and k not in self._gif_active]
            if not host_keys:
                continue
            # SDK 写图没有回执、也没有背压：所有动图合计每秒最多 20 帧，
            # 超出就丢帧而不是排队，否则积压会拖慢之后的灯光 / 换页 / 切场景
            per_key_fps = max(2.0, 20.0 / len(host_keys))
            min_gap = 1.0 / per_key_fps
            for key in host_keys:
                item = self.key_overrides.get(key)
                if not item or now < item.get("next", 0):
                    continue
                item["idx"] = (item["idx"] + 1) % len(item["frames"])
                jpeg, dur = item["frames"][item["idx"]]
                item["next"] = now + max(min_gap, dur / 1000.0)
                self.device.send_image(key, self._styled_frame(key, jpeg))
                pushed += 1
            if pushed and now - last_refresh >= 0.05:
                self.device.refresh()
                last_refresh = now

    def _gap(self) -> int:
        return max(0, min(64, int(self.wallpaper_gap)))

    def _canvas_size(self) -> tuple[int, int]:
        gap = self._gap()
        cols, rows = (GRID_ROWS, GRID_COLS) if self.rotation % 180 else (GRID_COLS, GRID_ROWS)
        return (
            cols * KEY_SIZE + (cols - 1) * gap,
            rows * KEY_SIZE + (rows - 1) * gap,
        )

    def _cell_slot(self, col: int, row: int) -> tuple[int, int]:
        """把「默认朝向」的键位换算成「旋转后」画面里的位置。"""
        rot = self.rotation % 360
        if rot == 90:
            return GRID_ROWS - 1 - row, col
        if rot == 180:
            return GRID_COLS - 1 - col, GRID_ROWS - 1 - row
        if rot == 270:
            return row, GRID_COLS - 1 - col
        return col, row

    def _calibration_source(self) -> Image.Image:
        """校准图案：斜线连续、圆不扁 = 间隙补偿正确。"""
        gap = self._gap()
        w, h = self._canvas_size()
        pitch = KEY_SIZE + gap
        cols, rows = (GRID_ROWS, GRID_COLS) if self.rotation % 180 else (GRID_COLS, GRID_ROWS)
        img = Image.new("RGB", (w, h), (14, 17, 23))
        d = ImageDraw.Draw(img)
        for c in range(cols):
            for r in range(rows):
                x, y = c * pitch, r * pitch
                d.rectangle([x + 1, y + 1, x + KEY_SIZE - 2, y + KEY_SIZE - 2],
                            outline=(46, 56, 72), width=1)
        d.line([(0, 0), (w, h)], fill=(80, 200, 255), width=2)
        d.line([(w, 0), (0, h)], fill=(255, 120, 90), width=2)
        radius = min(w, h) * 0.36
        cx, cy = w / 2, h / 2
        d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                  outline=(255, 255, 255), width=2)
        d.line([(0, cy), (w, cy)], fill=(120, 255, 170), width=1)
        return img

    def _canvas(self) -> Image.Image | None:
        """当前要显示的画面，尺寸 = 含缝隙的总画布。支持填充方式 / 缩放 / 位移。"""
        w, h = self._canvas_size()
        if self.preview_mode == "calibration":
            return self._calibration_source()
        try:
            src = Image.open(self.wallpaper_file).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log(f"壁纸处理失败：{exc}")
            return None

        zoom = max(0.2, min(4.0, float(self.wallpaper_zoom)))
        tw, th = max(1, int(w * zoom)), max(1, int(h * zoom))
        fit = self.wallpaper_fit
        if fit == "fill":
            scaled = src.resize((tw, th), Image.Resampling.LANCZOS)
        elif fit == "contain":
            base = ImageOps.pad(src, (w, h), color=(0, 0, 0),
                                method=Image.Resampling.LANCZOS)
            scaled = base.resize((tw, th), Image.Resampling.LANCZOS)
        else:
            scaled = ImageOps.fit(src, (tw, th), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        x = (w - scaled.width) // 2 + int(self.wallpaper_ox)
        y = (h - scaled.height) // 2 + int(self.wallpaper_oy)
        canvas.paste(scaled, (x, y))
        # 待机壁纸单独调暗（不影响按键画面的屏幕亮度）
        dim = max(0, min(100, int(self.standby_brightness)))
        if dim < 100:
            k = dim / 100.0
            canvas = canvas.point(lambda v: int(v * k))
        return canvas

    def _canvas_key(self):
        try:
            mtime = os.path.getmtime(self.wallpaper_file or "")
        except OSError:
            mtime = 0
        return (
            self.wallpaper_file, mtime, self.wallpaper_gap, self.wallpaper_zoom,
            self.wallpaper_ox, self.wallpaper_oy, self.wallpaper_fit,
            self.wallpaper_flip, self.rotation, self.preview_mode,
            self.standby_brightness,
        )

    def canvas_png(self) -> bytes | None:
        key = self._canvas_key()
        cached = getattr(self, "_canvas_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        canvas = self._canvas()
        if canvas is None:
            return None
        buf = io.BytesIO()
        canvas.save(buf, "PNG", compress_level=1)
        data = buf.getvalue()
        self._canvas_cache = (key, data)
        return data

    def apply_wallpaper(self) -> bool:
        self.preview_mode = "wallpaper"
        self.in_standby = True
        return self._push_canvas("壁纸")

    def apply_calibration(self) -> bool:
        self.preview_mode = "calibration"
        self._calib_until = time.time() + 60
        threading.Timer(20.0, self._end_calibration).start()
        return self._push_canvas("校准图")

    def _end_calibration(self) -> None:
        """校准图只是临时画面，到点无条件回到壁纸。"""
        if self.preview_mode == "calibration":
            self.preview_mode = "wallpaper"
            self.apply_wallpaper()

    def _push_canvas(self, note: str) -> bool:
        """把画布按当前 gap 切块，逐键写入设备。"""
        if not self.device.connected or self.writes_paused or self.quiet or self.lighting_only or self.yielded:
            return False
        if self.screen_off:
            return False
        try:
            self.device.set_screen_brightness(self.screen_brightness)
            time.sleep(0.06)
        except Exception:  # noqa: BLE001
            pass
        canvas = self._canvas()
        if canvas is None:
            return False
        self._gif_stop_all()
        gap = self._gap()
        pitch = KEY_SIZE + gap
        ok = True
        self._pushing = True          # 图片传输期间暂停灯效写入，避免数据包互相穿插
        try:
            self.device.clear_queue()
            self._clear_screen()
            for key in range(1, KEY_COUNT + 1):
                col = (key - 1) % GRID_COLS
                row_from_bottom = (key - 1) // GRID_COLS
                row_top = row_from_bottom if self.wallpaper_flip else (GRID_ROWS - 1 - row_from_bottom)
                slot_col, slot_row = self._cell_slot(col, row_top)
                left = slot_col * pitch
                top = slot_row * pitch
                tile = canvas.crop((left, top, left + KEY_SIZE, top + KEY_SIZE))
                if self.rotation:
                    tile = tile.rotate(self.rotation, expand=False)
                buf = io.BytesIO()
                tile.save(buf, "JPEG", quality=88, subsampling=0)
                if not self.device.send_image(key, buf.getvalue()):
                    ok = False
            self.device.refresh()
        finally:
            self._pushing = False
        self._last_paint = time.time()
        w, h = canvas.size
        log(f"{note}已铺满 {KEY_COUNT} 键（间隙补偿 {gap}px，画布 {w}×{h}，旋转 {self.rotation}°）"
            + ("" if ok else "  ⚠ 有按键写入失败"))
        return ok

    def tiles_png(self) -> bytes | None:
        """15 个键的画面拼成一张 320×192 的精灵图，供界面上的键格直接铺。"""
        canvas = self._canvas()
        if canvas is None:
            return None
        gap = self._gap()
        pitch = KEY_SIZE + gap
        sprite = Image.new("RGB", (GRID_COLS * KEY_SIZE, GRID_ROWS * KEY_SIZE), (0, 0, 0))
        for key in range(1, KEY_COUNT + 1):
            col = (key - 1) % GRID_COLS
            row_from_bottom = (key - 1) // GRID_COLS
            row_top = row_from_bottom if self.wallpaper_flip else (GRID_ROWS - 1 - row_from_bottom)
            slot_col, slot_row = self._cell_slot(col, row_top)
            tile = canvas.crop((slot_col * pitch, slot_row * pitch,
                                slot_col * pitch + KEY_SIZE, slot_row * pitch + KEY_SIZE))
            if self.rotation:
                tile = tile.rotate(self.rotation, expand=False)
            item = self.key_overrides.get(key)
            if item:
                jpeg = item["jpeg"] if item["type"] == "image" else item["frames"][item["idx"]][0]
                try:
                    tile = Image.open(io.BytesIO(jpeg)).convert("RGB").resize(
                        (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
                except Exception:  # noqa: BLE001
                    pass
            sprite.paste(tile, (col * KEY_SIZE, row_top * KEY_SIZE))
        buf = io.BytesIO()
        sprite.save(buf, "PNG")
        return buf.getvalue()

    def preview_png(self) -> bytes | None:
        """给界面用的对照图：和实际送到设备的画面完全一致，并把缝隙位置标出来。"""
        canvas = self._canvas()
        if canvas is None:
            return None
        gap = self._gap()
        w, h = canvas.size
        pitch = KEY_SIZE + gap
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)

        if gap > 0:
            for c in range(GRID_COLS - 1):
                x = (c + 1) * KEY_SIZE + c * gap
                od.rectangle([x, 0, x + gap - 1, h - 1], fill=(6, 8, 12, 210))
            for r in range(GRID_ROWS - 1):
                y = (r + 1) * KEY_SIZE + r * gap
                od.rectangle([0, y, w - 1, y + gap - 1], fill=(6, 8, 12, 150))
        for c in range(GRID_COLS):
            for r in range(GRID_ROWS):
                x, y = c * pitch, r * pitch
                od.rectangle([x, y, x + KEY_SIZE - 1, y + KEY_SIZE - 1],
                             outline=(120, 190, 255, 140), width=1)

        out = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()

    # -- 外部输入 ---------------------------------------------------------- #
    def _on_input(self, key: int, state: int) -> None:
        if self.input_muted:
            return
        self._last_activity = time.time()
        # 设备可能自己睡过去了（显示开机图），任何按键都补画一次，保证画面回来
        if self.in_standby or time.time() - self._last_paint > 1.0:
            threading.Thread(target=self._wake_from_standby, daemon=True).start()
        item = {"type": "input", "key": key, "state": state, "t": time.time()}
        self.events.appendleft(item)
        self.broadcast(item)
        log(f"按键事件 key={key} state={state} standby={self.in_standby}")
        if state == 1:
            if self.repaint_on_press:
                self._maybe_repaint()
            threading.Thread(target=self._run_action, args=(key,), daemon=True).start()

    def _maybe_repaint(self) -> None:
        """设备空闲会自己息屏，醒过来只显示开机图；按键时补一次画面。"""
        if time.time() - self._last_paint < 6:
            return
        self._last_paint = time.time()
        threading.Thread(target=self._repaint_now, daemon=True).start()

    def _repaint_now(self) -> None:
        try:
            if self.preview_mode == "calibration":
                self.preview_mode = "wallpaper"
            self.apply_keys()
        except Exception as exc:  # noqa: BLE001
            log(f"唤醒重绘失败：{exc}")

    def _run_macro(self, key: int, steps: list) -> None:
        log(f"按键 {key} → 宏（{len(steps)} 步）")
        for step in steps:
            op = step.get("op")
            try:
                if op == "delay":
                    time.sleep(max(0.0, min(60.0, float(step.get("sec", 0)))))
                elif op == "hotkey":
                    send_hotkey(step.get("keys", ""))
                elif op == "url":
                    if step.get("url"):
                        os.startfile(step["url"])
                elif op == "launch":
                    path = step.get("path") or ""
                    if os.path.exists(path):
                        os.startfile(path)
            except Exception as exc:  # noqa: BLE001
                log(f"宏步骤失败 {op}: {exc}")

    def set_autostart(self, enabled: bool) -> bool:
        startup = os.path.join(
            os.environ.get("APPDATA", ""),
            r"Microsoft\Windows\Start Menu\Programs\Startup",
        )
        lnk = os.path.join(startup, "Stream Dock M18 控制台.lnk")
        bat = os.path.abspath(os.path.join(HERE, "..", "启动 M18 控制台.bat"))
        if enabled:
            ps = (
                f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{lnk}");'
                f'$s.TargetPath="{bat}";$s.WorkingDirectory="{os.path.dirname(bat)}";'
                f'$s.WindowStyle=7;$s.Save()'
            )
        else:
            ps = f'Remove-Item -LiteralPath "{lnk}" -ErrorAction SilentlyContinue'
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"设置开机自启失败：{exc}")
            return False
        self.autostart = enabled
        self._save_config()
        log(f"开机自启：{'开' if enabled else '关'}")
        return True

    def export_config(self) -> dict:
        try:
            return self._read_config_file()
        except (OSError, ValueError):
            return {}

    def import_config(self, data: dict) -> bool:
        if not isinstance(data, dict) or "pages" not in data:
            return False
        try:
            with self._save_lock:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)
        except OSError as exc:
            log(f"导入配置失败：{exc}")
            return False
        self._suspend_snapshot = True
        try:
            self._load_config()
            self._bind_page()
        finally:
            self._suspend_snapshot = False
        self._dirty = True
        if self.device.connected and not self.yielded:
            self.device.set_screen_brightness(self.screen_brightness)
            self.apply_keys()
        return True

    def _scene_path(self, name: str) -> str:
        if name == self.custom_scene_name:
            return os.path.join(SCENE_DIR, "__custom__.json")
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (name or ""))[:40]
        return os.path.join(SCENE_DIR, safe.strip() + ".json")

    def list_scenes(self) -> list:
        official_names = self.official_scene_names()
        items = [{"name": self.custom_scene_name, "custom": True,
                  "active": self.active_scene in ("", self.custom_scene_name)}]
        try:
            files = sorted(os.listdir(SCENE_DIR))
        except OSError:
            files = []
        for f in files:
            if not f.lower().endswith(".json") or f.startswith(("__", ".")):
                continue
            name = os.path.splitext(f)[0]
            items.append({"name": name, "custom": False,
                          "official": name in official_names,
                          "active": name == self.active_scene})
        return items

    def save_scene(self, name: str) -> bool:
        name = (name or "").strip()[:40]
        if not name:
            return False
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                json.dump(self.export_config(), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存场景失败：{exc}")
            return False
        # 新建只是保存一份，不切换当前场景
        log(f"已保存场景：{name}")
        return True

    def rename_scene(self, old: str, new: str) -> bool:
        new = (new or "").strip()[:40]
        if not new or old == self.custom_scene_name:
            return False
        src = self._scene_path(old)
        if not os.path.isfile(src):
            return False
        try:
            os.replace(src, self._scene_path(new))
        except OSError:
            return False
        if self.active_scene == old:
            self.active_scene = new
            self._save_config()
        return True

    def duplicate_scene(self, name: str) -> bool:
        src = self._scene_path(name)
        if not os.path.isfile(src):
            return False
        base = f"{name} 副本"
        target = base
        i = 2
        while os.path.isfile(self._scene_path(target)):
            target = f"{base} {i}"
            i += 1
        try:
            with open(src, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            with open(self._scene_path(target), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            return False
        return True

    def rename_custom_scene(self, name: str) -> bool:
        name = (name or "").strip()[:40]
        if not name:
            return False
        self.custom_scene_name = name
        self._save_config()
        return True

    # 场景只负责「按键内容」：页面、图片、动作、标题样式、当前页。
    # 待机时间、亮度、壁纸、间隙、灯带等是全局设置，切场景时保持不变。
    SCENE_KEYS = ("pages", "page")

    def _apply_scene_file(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            return False
        merged = self.export_config()
        if not merged:
            log("读取当前配置失败，取消切换场景（避免全局设置被清空）")
            return False
        for k in self.SCENE_KEYS:
            if k in data:
                merged[k] = data[k]
        return self.import_config(merged)

    def apply_scene(self, name: str) -> bool:
        self._applying_scene = True
        try:
            if name == self.custom_scene_name:
                path = os.path.join(SCENE_DIR, "__custom__.json")
                ok = self._apply_scene_file(path)
                if ok:
                    self.active_scene = self.custom_scene_name
                    self._save_config()
                    log("已切换到自定义场景")
                return ok
            ok = self._apply_scene_file(self._scene_path(name))
            if ok:
                self.active_scene = name
                self._save_config()
                log(f"已切换到场景：{name}")
            return ok
        finally:
            self._applying_scene = False

    def delete_scene(self, name: str) -> bool:
        if name == self.custom_scene_name:
            return False
        path = self._scene_path(name)
        if not os.path.isfile(path):
            return False
        if self.active_scene == name:
            self.apply_scene(self.custom_scene_name)
        try:
            os.remove(path)
        except OSError:
            return False
        return True

    def _snapshot_custom_scene(self) -> None:
        """每次保存配置，把当前按键内容写回「正在使用的场景」——各场景各记各的改动。"""
        if getattr(self, "_suspend_snapshot", False) or getattr(self, "_applying_scene", False):
            return
        name = self.active_scene or self.custom_scene_name
        custom = name == self.custom_scene_name
        path = os.path.join(SCENE_DIR, "__custom__.json") if custom else self._scene_path(name)
        if not custom and not os.path.isfile(path):
            return                      # 场景文件已被删掉，不要再凭空建回来
        cfg = self.export_config()
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        for k in self.SCENE_KEYS:
            if k in cfg:
                data[k] = cfg[k]
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"保存场景「{name}」失败：{exc}")

    def _ensure_custom_snapshot(self) -> None:
        path = os.path.join(SCENE_DIR, "__custom__.json")
        if os.path.isfile(path):
            return
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.export_config(), fh, ensure_ascii=False, indent=2)
            log("已创建自定义场景快照")
        except OSError:
            pass

    # -- 官方场景 ---------------------------------------------------------- #
    # -- 官方场景同步 ------------------------------------------------------ #
    OFFICIAL_MARK = ".official-scenes.json"

    def _official_mark_path(self) -> str:
        return os.path.join(SCENE_DIR, self.OFFICIAL_MARK)

    def official_scene_names(self) -> set:
        try:
            with open(self._official_mark_path(), encoding="utf-8") as fh:
                return set(json.load(fh).get("names") or [])
        except (OSError, ValueError, AttributeError):
            return set()

    @staticmethod
    def _vk_reverse() -> dict:
        rev = {}
        for name, vk in VK_NAMES.items():
            rev.setdefault(vk, name)
        return rev

    SOURCES_FILE = os.path.join(UPLOAD_DIR, ".sources.json")

    def _sources(self) -> dict:
        cache = getattr(self, "_sources_cache", None)
        if cache is None:
            try:
                with open(self.SOURCES_FILE, encoding="utf-8") as fh:
                    cache = json.load(fh)
                if not isinstance(cache, dict):
                    cache = {}
            except (OSError, ValueError):
                cache = {}
            self._sources_cache = cache
        return cache

    def _record_sources(self, mapping: dict) -> None:
        """记下 uploads 里每个副本的原始来源（官方图标包 / 场景 / 插件），供「打开图标位置」用。"""
        src = self._sources()
        changed = False
        for name, path in mapping.items():
            if path and src.get(name) != path:
                src[name] = path
                changed = True
        if not changed:
            return
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(self.SOURCES_FILE, "w", encoding="utf-8") as fh:
                json.dump(src, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            log(f"记录图标来源失败：{exc}")

    def _store_scene_image(self, img, source: str | None = None) -> str:
        """场景图标按内容哈希存进 uploads（同一张图只存一份），并记下原始来源。"""
        ext, data = img[0], img[1]
        source = source or (img[2] if len(img) > 2 else None)
        name = f"sd_{hashlib.sha1(data).hexdigest()[:16]}{ext}"
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        if source and name not in self._sources():
            self._record_sources({name: source})
        return name

    def key_image_info(self, key: int) -> dict:
        """按键图标的本地副本和原始位置。"""
        item = self.key_overrides.get(key) or {}
        copy = item.get("file") or ""
        if not copy or not os.path.isfile(copy):
            return {"ok": False}
        source = copy
        if os.path.normpath(os.path.dirname(copy)) == os.path.normpath(UPLOAD_DIR):
            source = self._sources().get(os.path.basename(copy)) or ""
        low = source.lower()
        return {"ok": True, "copy": copy, "source": source,
                "sourceExists": bool(source and os.path.exists(source)),
                "sameFile": bool(source) and os.path.normpath(source) == os.path.normpath(copy),
                # 官方场景图标是加密的 .mbg，或者打包在预设压缩包里，直接看不了
                "encrypted": low.endswith((".mbg", ".streamdockprofile"))}

    def export_key_image(self, key: int) -> str | None:
        """把按键图标（已解密）复制到「图片 › M18 图标」，起个看得懂的名字。"""
        info = self.key_image_info(key)
        if not info.get("ok"):
            return None
        pictures = os.path.join(os.environ.get("USERPROFILE") or HERE, "Pictures", "M18 图标")
        scene = self.active_scene or self.custom_scene_name
        page = self.pages[self.page].get("name", "") if 0 <= self.page < len(self.pages) else ""
        stem = f"{scene}-{page}-键{key}" if page and page != scene else f"{scene}-键{key}"
        stem = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in stem).strip()
        ext = os.path.splitext(info["copy"])[1].lower() or ".png"
        try:
            os.makedirs(pictures, exist_ok=True)
            dst = os.path.join(pictures, stem + ext)
            shutil.copyfile(info["copy"], dst)
            return dst
        except OSError as exc:
            log(f"导出图标失败：{exc}")
            return None

    @staticmethod
    def reveal_in_explorer(path: str) -> bool:
        """在资源管理器里打开所在文件夹并选中该文件。"""
        path = os.path.normpath(path or "")
        if not path or not os.path.exists(path):
            return False
        try:
            subprocess.Popen(f'explorer /select,"{path}"')
            return True
        except OSError as exc:
            log(f"打开文件位置失败：{exc}")
            return False

    def index_official_sources(self) -> None:
        """老版本同步的官方场景没记来源：只转换不写场景文件，把来源补上。"""
        if any(str(v).startswith(SD_ROOT) or "HotSpot" in str(v) for v in self._sources().values()):
            return
        try:
            plugins = official.PluginIndex(SD_ROOT)
            rev = self._vk_reverse()
            mapping = {}
            for item in official.list_scene_sources():
                sc = official.convert_scene(item, plugins, SD_ROOT, rev)
                for page in sc["pages"]:
                    for img in page["images"].values():
                        if len(img) > 2 and img[2]:
                            name = f"sd_{hashlib.sha1(img[1]).hexdigest()[:16]}{img[0]}"
                            mapping.setdefault(name, img[2])
            self._record_sources(mapping)
            log(f"已补记官方场景图标来源 {len(mapping)} 个")
        except Exception as exc:  # noqa: BLE001
            log(f"补记图标来源失败：{exc}")

    def sync_official_scenes(self) -> dict:
        """把官方驱动里 M18 的全部场景（含预设）转换后写进场景列表。不会切换当前场景。"""
        plugins = official.PluginIndex(SD_ROOT)
        rev = self._vk_reverse()
        base = self.export_config()
        report, names = [], []
        os.makedirs(SCENE_DIR, exist_ok=True)
        for item in official.list_scene_sources():
            try:
                sc = official.convert_scene(item, plugins, SD_ROOT, rev)
            except Exception as exc:  # noqa: BLE001
                log(f"官方场景「{item.get('name')}」转换失败：{exc}")
                continue
            multi = len(sc["pages"]) > 1
            pages = []
            for i, page in enumerate(sc["pages"]):
                images = {}
                for key, img in page["images"].items():
                    try:
                        images[str(key)] = self._store_scene_image(img)
                    except OSError as exc:
                        log(f"保存场景图标失败：{exc}")
                actions = {str(k): v for k, v in page["actions"].items()}
                for k, nav in ((16, "上一页"), (17, "主页"), (18, "下一页")):
                    actions.setdefault(str(k), {"type": "nav", "name": nav})
                pages.append({
                    "name": f"第 {i + 1} 页" if multi else sc["name"],
                    "images": images,
                    "actions": actions,
                    "styles": {str(k): v for k, v in page["styles"].items()},
                })
            name = sc["name"].strip()[:40]
            cfg = dict(base)
            cfg.update({"pages": pages, "page": 0, "officialSource": item["id"],
                        "activeScene": name})
            try:
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, ensure_ascii=False, indent=2)
            except OSError as exc:
                log(f"写入场景「{name}」失败：{exc}")
                continue
            names.append(name)
            report.append({"name": name, "kind": item["kind"], **sc["stats"]})
        try:
            with open(self._official_mark_path(), "w", encoding="utf-8") as fh:
                json.dump({"names": names, "time": int(time.time())}, fh, ensure_ascii=False)
        except OSError:
            pass
        log("官方场景同步完成：" + "，".join(
            f"{r['name']}（{r['supported']}/{r['total']} 键可执行，{r['pages']} 页）" for r in report))
        return {"scenes": report}

    def _auto_sync_official(self) -> None:
        """首次启动自动同步一次；之后用户删掉的官方场景不会再自己冒出来。"""
        if os.path.isfile(self._official_mark_path()):
            return
        try:
            self.sync_official_scenes()
        except Exception as exc:  # noqa: BLE001
            log(f"自动同步官方场景失败：{exc}")

    def next_scene(self) -> bool:
        """官方「场景切换」键：按场景列表顺序切到下一个。"""
        names = [it["name"] for it in self.list_scenes()]
        if len(names) < 2:
            return False
        cur = self.active_scene or self.custom_scene_name
        idx = names.index(cur) if cur in names else -1
        target = names[(idx + 1) % len(names)]
        ok = self.apply_scene(target)
        self.broadcast({"type": "scene", "name": target, "ok": ok})
        return ok

    def swap_keys(self, a: int, b: int) -> bool:
        if a == b or not (1 <= a <= KEY_COUNT) or not (1 <= b <= KEY_COUNT):
            return False
        for store in (self.key_overrides, self.key_actions, self.key_styles):
            va, vb = store.get(a), store.get(b)
            if vb is None:
                store.pop(a, None)
            else:
                store[a] = vb
            if va is None:
                store.pop(b, None)
            else:
                store[b] = va
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def copy_key(self, key: int) -> bool:
        if not (1 <= key <= KEY_COUNT):
            return False
        self.clipboard = {
            "image": copy.deepcopy(self.key_overrides.get(key)),
            "action": copy.deepcopy(self.key_actions.get(key)),
            "style": copy.deepcopy(self.key_styles.get(key)),
        }
        return any(self.clipboard.values())

    def paste_key(self, key: int) -> bool:
        if not self.clipboard or not (1 <= key <= KEY_COUNT):
            return False
        for name, store in (("image", self.key_overrides),
                            ("action", self.key_actions),
                            ("style", self.key_styles)):
            val = self.clipboard.get(name)
            if val:
                store[key] = copy.deepcopy(val)
            else:
                store.pop(key, None)
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def clear_key_all(self, key: int) -> bool:
        if not (1 <= key <= KEY_COUNT):
            return False
        self.key_overrides.pop(key, None)
        self.key_actions.pop(key, None)
        self.key_styles.pop(key, None)
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def _run_action(self, key: int) -> None:
        act = self.key_actions.get(key)
        if not act:
            return
        kind = act.get("type") or "launch"

        if kind == "nav":
            name = act.get("name") or ""
            if name in ("上一页", "下一页", "主页"):
                self.add_nav_pages(name)
            elif name == "下一场景":
                self.next_scene()
            elif name.startswith("跳到第"):
                digits = "".join(ch for ch in name if ch.isdigit())
                if digits:
                    self.goto_page(int(digits) - 1)
            else:
                self.broadcast({"type": "nav", "key": key, "name": name})
            log(f"按键 {key} → 导航：{name}")
            return

        if kind == "macro":
            steps = act.get("steps") or []
            threading.Thread(target=self._run_macro, args=(key, steps), daemon=True).start()
            return

        if kind == "media" and act.get("action") in ("brightup", "brightdown"):
            step = 10 if act.get("action") == "brightup" else -10
            self.screen_brightness = max(0, min(100, int(self.screen_brightness) + step))
            self.device.set_screen_brightness(self.screen_brightness)
            self.broadcast({"type": "status", "screenBrightness": self.screen_brightness})
            log(f"按键 {key} → 屏幕亮度 {self.screen_brightness}")
            return

        if kind == "media":
            action = act.get("action", "")
            vk = MEDIA_LABELS.get(action, ("", ""))[0]
            ok = bool(vk) and send_hotkey(vk)
            log(f"按键 {key} → 媒体键 {MEDIA_LABELS.get(action, ('', action))[1]} {'✓' if ok else '✗'}")
            return

        if kind == "hotkey":
            combo = act.get("keys", "")
            ok = send_hotkey(combo)
            log(f"按键 {key} → 热键 {combo} {'✓' if ok else '✗ 组合键无法识别'}")
            return

        if kind == "url":
            url = act.get("url", "")
            if not url:
                return
            try:
                os.startfile(url)
                log(f"按键 {key} → 网页 {url}")
            except Exception as exc:  # noqa: BLE001
                log(f"打开网页失败：{exc}")
            return

        path = act.get("path") or ""
        if not os.path.exists(path):
            log(f"动作路径不存在：{path}")
            return
        try:
            os.startfile(path)  # exe / lnk / 文件夹 / 文档
            log(f"按键 {key} → {os.path.basename(path)}")
        except Exception as exc:  # noqa: BLE001
            log(f"执行失败 {path}: {exc}")

    def set_key_action(self, key: int, spec: dict) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):
            return False
        kind = spec.get("type") or "launch"
        if kind == "nav":
            name = str(spec.get("name") or "")
            if not name:
                return False
            self.key_actions[key] = {"type": "nav", "name": name}
        elif kind == "hotkey":
            keys = str(spec.get("keys") or "").strip()
            if not keys:
                return False
            self.key_actions[key] = {"type": "hotkey", "keys": keys}
        elif kind == "media":
            action = str(spec.get("action") or "")
            if action not in MEDIA_LABELS:
                return False
            self.key_actions[key] = {"type": "media", "action": action}
        elif kind == "macro":
            raw = str(spec.get("macro") or "")
            steps = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                op = parts[0].lower()
                val = parts[1].strip() if len(parts) > 1 else ""
                if op in ("delay", "wait"):
                    try:
                        steps.append({"op": "delay", "sec": float(val)})
                    except ValueError:
                        pass
                elif op in ("hotkey", "key"):
                    if val:
                        steps.append({"op": "hotkey", "keys": val})
                elif op == "url":
                    if val:
                        steps.append({"op": "url", "url": val})
                elif op in ("launch", "open", "run"):
                    path = val if os.path.isabs(val) else os.path.join(HERE, val)
                    if val and os.path.exists(path):
                        steps.append({"op": "launch", "path": path})
            if not steps:
                return False
            self.key_actions[key] = {"type": "macro", "steps": steps, "macro": raw}
        elif kind == "url":
            url = str(spec.get("url") or "").strip()
            if not url:
                return False
            self.key_actions[key] = {"type": "url", "url": url}
        else:
            raw = str(spec.get("path") or "")
            path = raw if os.path.isabs(raw) else os.path.join(HERE, raw)
            if not raw or not os.path.exists(path):
                return False
            self.key_actions[key] = {"type": "launch", "path": path}
        self._save_config()
        return True

    def clear_key_action(self, key: int) -> bool:
        if key not in self.key_actions:
            return False
        self.key_actions.pop(key, None)
        self._save_config()
        return True

    def key_raw_path(self, key: int) -> str | None:
        """返回原始上传文件路径（动图用它做预览，能真正动起来）。"""
        item = self.key_overrides.get(key)
        if not item:
            return None
        path = item.get("file") or ""
        return path if path and os.path.isfile(path) else None

    def key_thumb(self, key: int) -> bytes | None:
        item = self.key_overrides.get(key)
        if not item:
            return None
        jpeg = item["jpeg"] if item["type"] == "image" else item["frames"][item["idx"]][0]
        return jpeg

    @staticmethod
    def _detect_official() -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq StreamDock.exe", "/NH"],
                capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return "StreamDock.exe" in out
        except Exception:  # noqa: BLE001
            return False

    # -- 官方资源库 -------------------------------------------------------- #
    # -- 官方图标包 / 插件（直接读官方目录，不复制） ------------------------ #
    _catalog_lock = threading.Lock()

    def _catalog(self, refresh: bool = False) -> dict:
        with self._catalog_lock:          # 图标包和插件两个请求常同时到，只扫一遍
            return self._catalog_locked(refresh)

    def _catalog_locked(self, refresh: bool) -> dict:
        cat = getattr(self, "_catalog_cache", None)
        if cat is None or refresh:
            packs = {}
            for folder in official.icon_pack_dirs(SD_ROOT):
                info = official.icon_pack_info(folder)
                if info["files"] and info["id"] not in packs:
                    packs[info["id"]] = info
            plugins = {p["id"]: p for p in official.plugin_list(SD_ROOT)}
            cat = self._catalog_cache = {"packs": packs, "plugins": plugins}
            log(f"官方资源：图标包 {len(packs)} 个（{sum(len(p['files']) for p in packs.values())} 个图标），"
                f"插件 {len(plugins)} 个")
        return cat

    def icon_packs(self, refresh: bool = False) -> list:
        packs = [{
            "id": p["id"], "name": p["name"], "author": p["author"],
            "count": len(p["files"]),
            "files": [os.path.basename(f) for f in p["files"]],
        } for p in self._catalog(refresh)["packs"].values()]
        packs.sort(key=lambda p: p["name"].lower())
        names = [p["name"] for p in packs]
        for p in packs:            # 同名图标包（官方有两个 "Icons for Designer"）加上来源后缀
            if names.count(p["name"]) > 1:
                p["name"] += f" · {p['id'].rsplit('.', 1)[-1]}"
        local = self.list_icons()
        if local:
            packs.append({"id": "local", "name": "本地图标", "author": "", "count": len(local),
                          "files": local})
        return packs

    def icon_pack_file(self, pack_id: str, fname: str) -> str | None:
        fname = os.path.basename(fname or "")
        if pack_id == "local":
            path = os.path.join(ICON_DIR, fname)
            return path if fname and os.path.isfile(path) else None
        pack = self._catalog()["packs"].get(pack_id)
        if not pack:
            return None
        for f in pack["files"]:
            if os.path.basename(f) == fname:
                return f
        return None

    def plugin_catalog(self, refresh: bool = False) -> list:
        out = []
        for p in self._catalog(refresh)["plugins"].values():
            out.append({
                "id": p["id"], "name": p["name"], "author": p["author"],
                "version": p["version"], "description": p["description"],
                "downloaded": p["downloaded"], "hasIcon": bool(p["icon"]),
                "actions": [{"name": a["name"], "tooltip": a["tooltip"], "hasIcon": bool(a["icon"])}
                            for a in p["actions"]],
            })
        return out

    def plugin_icon_path(self, plugin_id: str, idx: int) -> str | None:
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return None
        if idx < 0:
            return p["icon"]
        acts = p["actions"]
        return acts[idx]["icon"] if 0 <= idx < len(acts) else None

    def list_icons(self) -> list:
        try:
            files = sorted(os.listdir(ICON_DIR))
        except OSError:
            return []
        exts = (".png", ".gif", ".jpg", ".jpeg", ".webp")
        return [f for f in files if f.lower().endswith(exts)]

    def set_key_icon(self, key: int, name: str) -> bool:
        name = name or ""
        src = None
        if name.startswith("pack:"):
            pack_id, _, fname = name[5:].partition("/")
            src = self.icon_pack_file(pack_id, fname)
        elif name.startswith("plugin:"):
            plugin_id, _, idx = name[7:].rpartition("/")
            try:
                src = self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                src = None
        if name.startswith(("pack:", "plugin:")):
            img = official.read_image(src) if src else None
            if not img or not (1 <= key <= KEY_COUNT):
                return False
            # 复制一份进 uploads：官方删了图标包，按键图也不会丢
            stored = self._store_scene_image(img, src)
            self._anim_pause_until = time.time() + 1.0
            return self.set_key_image(key, os.path.join(UPLOAD_DIR, stored))
        safe = os.path.basename(name)
        path = os.path.join(ICON_DIR, safe)
        if not safe or not os.path.isfile(path):
            return False
        if safe.lower().endswith(".gif"):
            return self.set_key_image(key, path)
        try:
            icon = Image.open(path).convert("RGBA")
        except Exception:  # noqa: BLE001
            return False
        bg = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 255))
        bg.alpha_composite(icon)
        buf = io.BytesIO()
        bg.convert("RGB").save(buf, "JPEG", quality=94)
        self._anim_pause_until = time.time() + 1.0
        self.key_overrides[key] = {"type": "image", "file": path, "jpeg": buf.getvalue()}
        self._save_config()
        if self.device.connected:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True

    def _official_loop(self) -> None:
        """官方驱动在跑就让路，退出后自动接管。"""
        while True:
            time.sleep(3)
            try:
                running = self._detect_official()
                if running and not self.yielded:
                    self.yielded = True
                    log("检测到官方驱动运行，控制台自动退让（不再写设备）")
                    try:
                        self._gif_stop_all()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self.device.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.broadcast({"type": "status", "connected": False})
                elif not running and self.yielded:
                    self.yielded = False
                    log("官方驱动已退出，重新接管设备")
                    if self.device.connect():
                        self._restore_all()
            except Exception as exc:  # noqa: BLE001
                log(f"官方驱动监测异常：{exc}")

    # -- 灯光 -------------------------------------------------------------- #
    def set_lighting(self, patch: dict) -> None:
        with self.lock:
            for k in ("mode", "color", "speed", "brightness"):
                if k in patch and patch[k] is not None:
                    self.lighting[k] = patch[k]
            self.lighting["speed"] = max(0.1, min(10.0, float(self.lighting["speed"])))
            # 设备端 LED 亮度是 0–100 的百分比，超过 100 固件会走到奇怪的分支
            self.lighting["brightness"] = max(0, min(100, int(self.lighting["brightness"])))
            if self.lighting["mode"] not in MODES:
                self.lighting["mode"] = "solid"
            self._dirty = True
        self.device.set_led_brightness(self.lighting["brightness"])

    def paint_led(self, index: int, color: str) -> None:
        rgb = list(hex_to_rgb(color))
        with self.lock:
            index = max(0, min(LED_COUNT - 1, int(index)))
            self.lighting["custom"][index] = rgb
            self.lighting["mode"] = "custom"
            self._dirty = True

    def set_custom(self, colors) -> None:
        with self.lock:
            cleaned = []
            for i in range(LED_COUNT):
                c = colors[i] if i < len(colors) else [0, 0, 0]
                cleaned.append([int(c[0]), int(c[1]), int(c[2])])
            self.lighting["custom"] = cleaned
            self.lighting["mode"] = "custom"
            self._dirty = True

    def _compute(self, t: float):
        with self.lock:
            cfg = dict(self.lighting)
        mode = cfg["mode"]
        n = LED_COUNT - 2          # 外圈 22 颗
        if mode == "custom":
            return [list(c) for c in cfg["custom"]]
        if mode == "off":
            ring = [[0, 0, 0]] * n
        elif mode == "solid":
            ring = [list(hex_to_rgb(cfg["color"]))] * n
        elif mode == "gradient":
            base = hex_to_rgb(cfg["color"])
            second = [255 - base[0], 255 - base[1], 255 - base[2]]
            ring = [
                [int(base[j] + (second[j] - base[j]) * (i / (n - 1))) for j in range(3)]
                for i in range(n)
            ]
        elif mode == "rainbow":
            ring = [hsv(t * 0.15 * cfg["speed"] + i / n, 1.0, 1.0) for i in range(n)]
        elif mode == "breathe":
            v = 0.12 + 0.88 * (0.5 + 0.5 * math.sin(t * 2.0 * cfg["speed"]))
            base = hex_to_rgb(cfg["color"])
            ring = [[int(c * v) for c in base]] * n
        elif mode == "chase":
            base = hex_to_rgb(cfg["color"])
            ring = []
            for i in range(n):
                d = (i - t * cfg["speed"] * 5) % n
                k = max(0.0, 1.0 - min(d, n - d) / 2.5)
                ring.append([int(c * k) for c in base])
        else:
            ring = [[0, 0, 0]] * n

        # 正面两颗跟随"显示顺序"上固定位置的外圈灯，正反向都保持一致
        if self.effect_reverse:
            ring = ring[::-1]
        front = [list(ring[4]), list(ring[11])]
        return list(ring) + front

    def _engine_loop(self) -> None:
        t0 = time.time()
        ui_hash = None
        while True:
            time.sleep(1 / 30)
            t = time.time() - t0
            if self._identify and time.time() < self._identify[1]:
                frame = [[0, 0, 0] for _ in range(LED_COUNT)]
                frame[self._identify[0]] = [255, 255, 255]
            else:
                frame = self._compute(t)
            static = self.lighting["mode"] in ("off", "solid", "gradient", "custom")
            h = hash(tuple(tuple(c) for c in frame))
            # 让位 / 静默 / 暂停写入 / 图片传输中，一律不碰灯带
            can_write = not (self._pushing or self.writes_paused or self.quiet or self.yielded)

            if static:
                if can_write and self._dirty and h != self._frame_hash:
                    self.device.set_leds(self._to_hw(frame))
                    self._frame_hash = h
                    self._dirty = False
            elif can_write:
                self.device.set_leds(self._to_hw(frame))
                self._frame_hash = h

            # 只在画面真的变了时才推给界面（静态灯效不再每秒刷 30 条 SSE）
            if h != ui_hash:
                ui_hash = h
                self._frame = frame
                if self.clients:
                    self.broadcast({"type": "frame", "colors": frame})

    # -- 广播 -------------------------------------------------------------- #
    def broadcast(self, obj) -> None:
        data = json.dumps(obj, separators=(",", ":"))
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "device": {
                    "connected": self.device.connected,
                    "error": self.device.error,
                    "info": self.device.info,
                },
                "lighting": {
                    "mode": self.lighting["mode"],
                    "color": self.lighting["color"],
                    "speed": self.lighting["speed"],
                    "brightness": self.lighting["brightness"],
                    "custom": self.lighting["custom"],
                },
                "screenBrightness": self.screen_brightness,
                "frame": self._compute(time.time() - self._t0),
                "wallpaper": {
                    "enabled": self.wallpaper_enabled,
                    "available": os.path.isfile(self.wallpaper_file),
                    "flip": self.wallpaper_flip,
                    "gap": self.wallpaper_gap,
                    "file": os.path.basename(self.wallpaper_file or ""),
                },
                "led": {"offset": self.led_offset, "reverse": self.led_reverse},
                "effectReverse": self.effect_reverse,
                "inputMuted": self.input_muted,
                "repaintOnPress": self.repaint_on_press,
                "keepAwake": self.keep_awake,
                "writesPaused": self.writes_paused,
                "quiet": self.quiet,
                "yielded": self.yielded,
                "iconCount": len(self.list_icons()),
                "lightingOnly": self.lighting_only,
                "standby": self.standby_sec,
                "standbyBrightness": self.standby_brightness,
                "screenOff": self.screen_off,
                "customScene": self.custom_scene_name,
                "activeScene": self.active_scene,
                "autostart": self.autostart,
                "inStandby": self.in_standby,
                "inputStats": dict(self.device.stats),
                "lastResponse": getattr(self.device, "last_response", None),
                "rotation": self.rotation,
                "page": self.page,
                "pageCount": len(self.pages),
                "pageNames": [p.get("name", "") for p in self.pages],
                "view": {
                    "fit": self.wallpaper_fit,
                    "zoom": self.wallpaper_zoom,
                    "ox": self.wallpaper_ox,
                    "oy": self.wallpaper_oy,
                },
                "keyActions": {str(k): action_label(v) for k, v in self.key_actions.items()},
                "keyActionTypes": {str(k): v.get("type", "launch")
                                   for k, v in self.key_actions.items()},
                "keyStyles": {str(k): v for k, v in self.key_styles.items()},
                "keyActionValues": {
                    str(k): (v.get("name") or v.get("keys") or v.get("url")
                             or v.get("path") or v.get("action") or v.get("macro") or "")
                    for k, v in self.key_actions.items()
                },
                "keyImages": {str(k): v.get("type") for k, v in self.key_overrides.items()},
                # 图片换了文件名就变：界面据此决定要不要重新拉预览（切场景 / 翻页时同一个键会换图）
                "keyImageRev": {str(k): os.path.basename(v.get("file") or "") or str(id(v))
                                for k, v in self.key_overrides.items()},
                "ledCount": LED_COUNT,
                "keyCount": KEY_COUNT,
                "officialRunning": self.official_running,
                "events": list(self.events)[:12],
            }


APP: App | None = None
PORT = 8642


def _shutdown() -> None:
    time.sleep(0.25)
    try:
        if APP is not None:
            APP._gif_stop_all()          # SDK 进程不一定随我们退出，别让它继续播旧动图
            APP.device.disconnect()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


def running_instance(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.8) as resp:
            return b"m18-console" in resp.read()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    # -- 工具 -------------------------------------------------------------- #
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    # -- 访问控制 ---------------------------------------------------------- #
    def _allowed_hosts(self) -> set:
        return {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def _check_access(self, write: bool) -> bool:
        """只服务本机自己的界面，挡掉别的网页对本地接口的跨站调用。

        - Host 必须是 127.0.0.1/localhost:端口（防 DNS rebinding）
        - 写操作：浏览器发来的必须同源（Origin / Sec-Fetch-Site），且必须是 JSON，
          这样跨站请求会先触发 CORS 预检，而本服务从不放行预检。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_hosts():
            self.send_error(403, "Forbidden host")
            return False
        if not write:
            return True
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            self.send_error(403, "Cross-origin request blocked")
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            self.send_error(403, "Cross-site request blocked")
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if int(self.headers.get("Content-Length") or 0) and ctype != "application/json":
            self.send_error(415, "JSON only")
            return False
        return True

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        if not self._check_access(write=False):
            return
        path = self.path.split("?")[0]
        if path == "/api/state":
            self._json(APP.snapshot())
        elif path == "/api/health":
            self._json({"app": "m18-console", "url": f"http://127.0.0.1:{PORT}/"})
        elif path == "/api/events":
            self._sse()
        elif path == "/api/wallpaper/preview.png":
            data = APP.preview_png()
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/wallpaper/tiles.png":
            data = APP.tiles_png()
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/wallpaper/canvas.png":
            data = APP.canvas_png()
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/iconpacks":
            self._json({"ok": True, "packs": APP.icon_packs()})
        elif path == "/api/plugins":
            self._json({"ok": True, "plugins": APP.plugin_catalog()})
        elif path.startswith("/api/iconpack/"):
            parts = path.split("/", 4)
            fp = APP.icon_pack_file(urllib.parse.unquote(parts[3]), urllib.parse.unquote(parts[4])) \
                if len(parts) == 5 else None
            self._image_file(fp)
        elif path.startswith("/api/plugin-icon/"):
            parts = path.split("/")
            try:
                fp = APP.plugin_icon_path(urllib.parse.unquote(parts[3]), int(parts[4]))
            except (IndexError, ValueError):
                fp = None
            self._image_file(fp)
        elif path.startswith("/api/icons/"):
            safe = os.path.basename(path[len("/api/icons/"):])
            fp = os.path.join(ICON_DIR, safe)
            if not safe or not os.path.isfile(fp):
                self.send_error(404)
                return
            ext = os.path.splitext(fp)[1].lower()
            ctype = {
                ".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
            }.get(ext, "application/octet-stream")
            with open(fp, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/api/key/") and path.endswith("/raw"):
            try:
                key = int(path.split("/")[3])
            except (IndexError, ValueError):
                self.send_error(400)
                return
            raw = APP.key_raw_path(key)
            if not raw:
                self.send_error(404)
                return
            ext = os.path.splitext(raw)[1].lower()
            ctype = {
                ".gif": "image/gif", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(ext, "application/octet-stream")
            with open(raw, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/api/key/") and path.endswith("/thumb.png"):
            try:
                key = int(path.split("/")[3])
            except (IndexError, ValueError):
                self.send_error(400)
                return
            data = APP.key_thumb(key)
            if not data:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/", "/index.html"):
            self._static("index.html")
        else:
            self._static(path.lstrip("/"))

    def do_POST(self):  # noqa: N802
        if not self._check_access(write=True):
            return
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/lighting":
            APP.set_lighting(body)
            self._json(APP.snapshot())
        elif path == "/api/lighting/paint":
            APP.paint_led(int(body.get("index", 0)), body.get("color", "#00aaff"))
            self._json({"ok": True})
        elif path == "/api/lighting/custom":
            APP.set_custom(body.get("colors") or [])
            self._json({"ok": True})
        elif path == "/api/screen-brightness":
            APP.screen_brightness = max(0, min(100, int(body.get("value", 100))))
            APP.device.set_screen_brightness(APP.screen_brightness)
            self._json({"ok": True, "value": APP.screen_brightness})
        elif path == "/api/device/reconnect":
            ok = APP.device.connect()
            APP.official_running = App._detect_official()
            if ok:
                APP._restore_all()
            self._json({"ok": ok, **APP.snapshot()})
        elif path == "/api/device/wake-panel":
            ok = APP.wake_panel()
            self._json({"ok": ok})
        elif path == "/api/device/repaint":
            threading.Thread(target=APP._wake_from_standby, daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/device/sleep":
            APP.screen_off = True
            try:
                APP.device.set_screen_brightness(0)
                APP.blank_screen()          # 立刻把 15 个键清空，壁纸/按键都不留
            except Exception:  # noqa: BLE001
                pass

            def _sleep_bg():
                APP.sleep_panel()           # 后台发真正的关屏指令
                log("已息屏（关屏指令已发送）")

            threading.Thread(target=_sleep_bg, daemon=True).start()
            self._json({"ok": True, "screenOff": True})
        elif path == "/api/device/mute":
            APP.input_muted = bool(body.get("value"))
            self._json({"ok": True, "value": APP.input_muted})
        elif path == "/api/device/repaint-on-press":
            APP.repaint_on_press = bool(body.get("value"))
            self._json({"ok": True, "value": APP.repaint_on_press})
        elif path == "/api/device/keep-awake":
            APP.keep_awake = bool(body.get("value"))
            self._json({"ok": True, "value": APP.keep_awake})
        elif path == "/api/device/probe":
            threading.Thread(target=APP.probe_inputs, daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/device/pause-writes":
            APP.writes_paused = bool(body.get("value"))
            if APP.writes_paused:
                APP._gif_stop_all()
            if not APP.writes_paused and APP.device.connected:
                threading.Thread(target=APP.apply_keys, daemon=True).start()
            self._json({"ok": True, "value": APP.writes_paused})
        elif path == "/api/device/lighting-only":
            APP.lighting_only = bool(body.get("value"))
            if APP.lighting_only:
                APP._gif_stop_all()
            APP._save_config()
            if not APP.lighting_only and APP.device.connected:
                threading.Thread(target=APP.apply_keys, daemon=True).start()
            self._json({"ok": True, "value": APP.lighting_only})
        elif path == "/api/device/mode":
            mode = int(body.get("mode", 0))
            ok = APP.device.change_mode(mode)
            self._json({"ok": ok, "mode": mode})
        elif path == "/api/device/reset":
            APP.device.disconnect()
            time.sleep(0.4)
            ok = APP.device.connect()
            if ok:
                APP._restore_all()
            self._json({"ok": ok, **APP.snapshot()})
        elif path == "/api/activity":
            APP._last_activity = time.time()
            if body.get("wake") and APP.in_standby:
                threading.Thread(target=APP.apply_keys, daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/page/delete":
            self._json({"ok": APP.delete_page(int(body.get("index", APP.page)))})
        elif path == "/api/page/rename":
            self._json({"ok": APP.rename_page(int(body.get("index", APP.page)),
                                              str(body.get("name") or ""))})
        elif path == "/api/autostart":
            self._json({"ok": APP.set_autostart(bool(body.get("value"))),
                        "value": APP.autostart})
        elif path == "/api/config/export":
            self._json({"ok": True, "config": APP.export_config()})
        elif path == "/api/config/import":
            self._json({"ok": APP.import_config(body.get("config") or {})})
        elif path == "/api/scenes":
            if body.get("syncOfficial"):
                result = APP.sync_official_scenes()
                self._json({"ok": True, **result, "items": APP.list_scenes()})
            elif body.get("rename_custom"):
                self._json({"ok": APP.rename_custom_scene(str(body["rename_custom"])),
                            "items": APP.list_scenes()})
            elif body.get("rename"):
                self._json({"ok": APP.rename_scene(str(body.get("old") or ""),
                                                   str(body["rename"])),
                            "items": APP.list_scenes()})
            elif body.get("duplicate"):
                self._json({"ok": APP.duplicate_scene(str(body["duplicate"])),
                            "items": APP.list_scenes()})
            elif body.get("save"):
                self._json({"ok": APP.save_scene(str(body.get("name") or body.get("save") or "")),
                            "items": APP.list_scenes()})
            elif body.get("apply"):
                self._json({"ok": APP.apply_scene(str(body.get("apply"))),
                            "items": APP.list_scenes()})
            elif body.get("delete"):
                self._json({"ok": APP.delete_scene(str(body.get("delete"))),
                            "items": APP.list_scenes()})
            else:
                self._json({"ok": True, "items": APP.list_scenes()})
        elif path == "/api/key/swap":
            self._json({"ok": APP.swap_keys(int(body.get("a", 0)), int(body.get("b", 0)))})
        elif path == "/api/key/copy":
            self._json({"ok": APP.copy_key(int(body.get("key", 0)))})
        elif path == "/api/key/paste":
            self._json({"ok": APP.paste_key(int(body.get("key", 0)))})
        elif path == "/api/key/clear-all":
            self._json({"ok": APP.clear_key_all(int(body.get("key", 0)))})
        elif path == "/api/icons":
            self._json({"ok": True, "items": APP.list_icons()})
        elif path == "/api/key/image-info":
            self._json(APP.key_image_info(int(body.get("key", 0))))
        elif path == "/api/key/reveal":
            info = APP.key_image_info(int(body.get("key", 0)))
            if body.get("target") == "export":
                target = APP.export_key_image(int(body.get("key", 0)))
            elif body.get("target") == "copy":
                target = info.get("copy")
            else:
                target = info.get("source")
            self._json({"ok": bool(info.get("ok")) and APP.reveal_in_explorer(target or ""),
                        "path": target or ""})
        elif path == "/api/key/icon":
            self._json({"ok": APP.set_key_icon(int(body.get("key", 0)),
                                               str(body.get("name") or ""))})
        elif path == "/api/catalog/refresh":
            self._json({"ok": True, "packs": APP.icon_packs(refresh=True),
                        "plugins": APP.plugin_catalog()})
        elif path == "/api/config/backups":
            self._json({"ok": True, "items": APP.list_config_backups()})
        elif path == "/api/config/restore":
            self._json({"ok": APP.restore_config_backup(str(body.get("name") or ""))})
        elif path == "/api/standby-brightness":
            APP.standby_brightness = max(0, min(100, int(body.get("value", 60))))
            APP._save_config()
            ok = APP.apply_standby() if APP.device.connected else True
            self._json({"ok": bool(ok), "value": APP.standby_brightness})
        elif path == "/api/standby":
            APP.standby_sec = max(5, min(1800, int(body.get("sec", 300))))
            APP._save_config()
            self._json({"ok": True, "sec": APP.standby_sec})
        elif path == "/api/wallpaper":
            changed = False
            if "flip" in body:
                APP.wallpaper_flip = bool(body["flip"])
                changed = True
            if "gap" in body:
                APP.wallpaper_gap = max(0, min(64, int(body["gap"])))
                changed = True
            if changed:
                APP._save_config()
            # 调整待机壁纸时，设备立刻切到壁纸画面
            if APP.preview_mode == "calibration":
                ok = APP.apply_calibration()
            else:
                ok = APP.apply_standby()
            self._json({"ok": ok, "flip": APP.wallpaper_flip, "gap": APP.wallpaper_gap})
        elif path == "/api/wallpaper/clear":
            APP.device.clear_keys()
            APP.device.refresh()
            self._json({"ok": True})
        elif path == "/api/wallpaper/calibrate":
            self._json({"ok": APP.apply_calibration()})
        elif path == "/api/wallpaper/apply":
            self._json({"ok": APP.apply_wallpaper()})
        elif path == "/api/upload":
            name = str(body.get("name") or "image")
            data = str(body.get("dataUrl") or "")
            if "," not in data:
                self._json({"ok": False, "error": "数据格式不对"}, 400)
                return
            try:
                raw = base64.b64decode(data.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "error": "解码失败"}, 400)
                return
            if len(raw) > MAX_UPLOAD_BYTES:
                self._json({"ok": False, "error": "文件过大"}, 413)
                return
            ext = os.path.splitext(name)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                ext = ".png"

            rel = str(body.get("rel") or "")
            if rel:
                safe = rel.replace("\\", "/").lstrip("/")
                if ".." in safe.split("/") or ":" in safe:
                    self._json({"ok": False, "error": "路径非法"}, 400)
                    return
                dest = os.path.join(UPLOAD_DIR, "dropped", *safe.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(raw)
                target = resolve_lnk(dest) if dest.lower().endswith(".lnk") else ""
                self._json({"ok": True, "file": os.path.relpath(dest, HERE), "target": target})
                return

            os.makedirs(UPLOAD_DIR, exist_ok=True)
            fname = f"{int(time.time() * 1000)}{ext}"
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as fh:
                fh.write(raw)
            self._json({"ok": True, "file": os.path.join("assets", "uploads", fname), "target": ""})
        elif path == "/api/wallpaper/file":
            p = asset_path(str(body.get("file") or ""))
            self._json({"ok": bool(p and APP.set_wallpaper_file(p))})
        elif path == "/api/key/image":
            p = asset_path(str(body.get("file") or ""))
            ok = bool(p and APP.set_key_image(int(body.get("key", 0)), p))
            self._json({"ok": ok})
        elif path == "/api/key/clear":
            self._json({"ok": APP.clear_key(int(body.get("key", 0)))})
        elif path == "/api/led/layout":
            if "offset" in body:
                APP.led_offset = int(body["offset"]) % (LED_COUNT - 2)
            if "reverse" in body:
                APP.led_reverse = bool(body["reverse"])
            APP._save_config()
            APP._dirty = True
            self._json({"ok": True, "offset": APP.led_offset, "reverse": APP.led_reverse})
        elif path == "/api/led/identify":
            APP._identify = (int(body.get("index", 0)) % LED_COUNT, time.time() + 1.5)
            self._json({"ok": True})
        elif path == "/api/wallpaper/view":
            if "fit" in body:
                fit = str(body["fit"])
                APP.wallpaper_fit = fit if fit in ("cover", "contain", "fill") else "cover"
            if "zoom" in body:
                APP.wallpaper_zoom = max(0.2, min(4.0, float(body["zoom"])))
            if "ox" in body:
                APP.wallpaper_ox = int(body["ox"])
            if "oy" in body:
                APP.wallpaper_oy = int(body["oy"])
            APP._save_config()
            # 只在待机画面上生效时才重画设备，否则仅更新预览
            if APP.preview_mode == "calibration":
                ok = APP.apply_calibration()
            else:
                ok = APP.apply_standby()
            self._json({"ok": ok, **{k: getattr(APP, "wallpaper_" + k) for k in ("fit", "zoom", "ox", "oy")}})
        elif path == "/api/key/select":
            self._json({"ok": APP.select_key(int(body.get("key", 0)))})
        elif path == "/api/key/style":
            ok = APP.set_key_style(int(body.get("key", 0)), body)
            self._json({"ok": ok})
        elif path == "/api/key/action":
            ok = APP.set_key_action(int(body.get("key", 0)), body)
            self._json({"ok": ok})
        elif path == "/api/key/action/clear":
            self._json({"ok": APP.clear_key_action(int(body.get("key", 0)))})
        elif path == "/api/device/wake":
            APP.screen_off = False
            APP._anim_pause_until = time.time() + 1.0
            try:
                APP.device.set_screen_brightness(APP.screen_brightness)
            except Exception:  # noqa: BLE001
                pass

            def _wake_bg():
                APP.wake_panel()
                time.sleep(0.05)
                APP._repaint_now()
                log("已亮屏")

            threading.Thread(target=_wake_bg, daemon=True).start()
            self._json({"ok": True, "screenOff": False})
        elif path == "/api/dialog/pick":
            picked = pick_path(str(body.get("mode") or "file"))
            self._json({"ok": bool(picked), "path": picked})
        elif path == "/api/wallpaper/toggle":
            APP.wallpaper_enabled = bool(body.get("value"))
            APP._save_config()
            if not APP.wallpaper_enabled and APP.in_standby:
                threading.Thread(target=APP.apply_keys, daemon=True).start()
            self._json({"ok": True, "value": APP.wallpaper_enabled})
        elif path == "/api/wallpaper/default":
            self._json({"ok": APP.restore_default_wallpaper()})
        elif path == "/api/lighting/reverse":
            APP.effect_reverse = bool(body.get("value"))
            APP._save_config()
            APP._dirty = True
            self._json({"ok": True, "value": APP.effect_reverse})
        elif path == "/api/page":
            if body.get("add"):
                APP.goto_page(APP.add_page())
            elif "index" in body:
                APP.goto_page(int(body["index"]))
            else:
                APP.goto_page(APP.page + int(body.get("delta", 0)))
            self._json({"ok": True, "page": APP.page, "pageCount": len(APP.pages),
                        **APP.snapshot()})
        elif path == "/api/wallpaper/reset":
            APP.wallpaper_gap = 27
            APP.wallpaper_zoom = 1.0
            APP.wallpaper_ox = 0
            APP.wallpaper_oy = 0
            APP.wallpaper_fit = "cover"
            APP.preview_mode = "wallpaper"
            APP._save_config()
            APP.apply_wallpaper()
            self._json({"ok": True})
        elif path == "/api/rotation":
            APP.rotation = int(body.get("value", 0)) % 360
            if APP.rotation not in (0, 90, 180, 270):
                APP.rotation = 0
            APP._save_config()
            ok = APP.apply_calibration() if APP.preview_mode == "calibration" else APP.apply_wallpaper()
            self._json({"ok": ok, "rotation": APP.rotation})
        elif path == "/api/lighting/reset":
            APP.device.reset_leds()
            self._json({"ok": True})
        elif path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=_shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def _image_file(self, fp: str | None) -> None:
        """官方图标 / 插件图：按内容判断类型；.mbg 解密成 GIF。"""
        img = official.read_image(fp) if fp else None
        if not img:
            self.send_error(404)
            return
        ext, data = img
        ctype = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                 ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # -- 静态文件 ---------------------------------------------------------- #
    def _static(self, name: str):
        safe = os.path.normpath(os.path.join(UI_DIR, name))
        if not safe.startswith(UI_DIR) or not os.path.isfile(safe):
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(os.path.splitext(safe)[1].lower(), "application/octet-stream")
        with open(safe, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- SSE --------------------------------------------------------------- #
    def _sse(self):
        q: queue.Queue = queue.Queue(maxsize=120)
        APP.clients.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                APP.clients.remove(q)
            except ValueError:
                pass


class QuietServer(ThreadingHTTPServer):
    """浏览器关窗口/刷新会断开 SSE，这类 ConnectionError 不该刷一屏 traceback。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def free_port(start=8642, tries=12) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def open_window(url: str) -> bool:
    """用独立配置目录启动一个应用窗口；启动前先关掉旧的控制台窗口。"""
    profile = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "M18Console", "browser")
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        profile = ""

    # 只关「用本控制台独立配置目录启动」的浏览器进程，不误伤用户自己的 Edge/Chrome 窗口
    if profile:
        marker = profile.replace("'", "''")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{marker}') }} | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            pass

    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if not os.path.isfile(exe):
            continue
        args = [
            exe,
            f"--app={url}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--window-size=1360,880",
        ]
        if profile:
            args.append(f"--user-data-dir={profile}")
        try:
            subprocess.Popen(
                args,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            log(f"已打开窗口：{os.path.basename(exe)} -> {url}")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"启动浏览器失败：{exc}")

    log(f"未找到 Edge/Chrome，改用默认浏览器打开 {url}")
    webbrowser.open(url)
    return False


def main() -> int:
    global APP, PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # 单实例：已有控制台在跑就只开窗口，避免两个进程同时写设备
    if not args.port and running_instance(PORT):
        log(f"已有实例在运行，打开 {PORT}")
        if not args.no_browser:
            open_window(f"http://127.0.0.1:{PORT}/")
        return 0

    APP = App()
    PORT = args.port or free_port(PORT)
    url = f"http://127.0.0.1:{PORT}/"
    server = QuietServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(f"控制台已启动 {url}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: open_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("退出")
    finally:
        APP.device.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
