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
import collections
import copy
import ctypes
import glob
import hashlib
import io
import json
import math
import zipfile
import random
import re
import os
import queue
import shutil
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
import winreg
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

import official
from pluginhost import PluginHost
from wssdk import DeviceWS

HERE = os.path.dirname(os.path.abspath(__file__))     # 程序自带资源（界面、默认壁纸、图标）
ROOT = os.path.dirname(HERE)
UI_DIR = os.path.join(HERE, "ui")
FROZEN = bool(getattr(sys, "frozen", False))          # 绿色版（PyInstaller 打包）
APP_DIR = os.path.dirname(sys.executable) if FROZEN else ROOT
# 用户数据（配置、场景、上传的图标、插件数据）：绿色版放 %APPDATA%\M18Console，
# 源码运行时仍放在 app 目录里（和以前一样）；也可以用环境变量 M18_DATA_DIR 指定
DATA_DIR = os.environ.get("M18_DATA_DIR") or (
    os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "M18Console") if FROZEN else HERE)

BLOCKED_HELP = (
    "这台电脑不允许本程序写文件（拒绝访问）。\n\n"
    "通常是安全软件，或 Windows 安全中心的「受控文件夹访问」（勒索软件防护）拦截了未签名的程序。\n\n"
    "解决办法（任选其一）：\n"
    "1. Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护 → 管理勒索软件防护 →\n"
    "   「通过受控文件夹访问允许应用」，把 M18Console.exe 加进去\n"
    "2. 在你装的安全软件（360 / 火绒 / 卡巴斯基等）里把 M18Console.exe 加入信任\n"
    "3. 运行源码里的「诊断.bat」，自动检查是谁拦截并一键放行\n"
    "4. 这台电脑上改用源码版（双击「启动 M18 控制台.bat」）"
)


def _fatal_box(title: str, text: str) -> None:
    """打包版没有控制台：用系统对话框说明原因，而不是甩一段 Python 报错。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        print(f"{title}\n{text}")


def _ensure_data_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return path
    except PermissionError as exc:
        if not FROZEN:
            raise
        # 绿色版：AppData 写不进去时，试试程序旁边的 data 文件夹
        alt = os.path.join(APP_DIR, "data")
        if os.path.normcase(alt) != os.path.normcase(path):
            try:
                os.makedirs(alt, exist_ok=True)
                with open(os.path.join(alt, ".write-test"), "w") as fh:
                    fh.write("ok")
                os.remove(os.path.join(alt, ".write-test"))
                return alt
            except OSError:
                pass
        _fatal_box("Stream Dock M18 控制台 无法启动", f"{BLOCKED_HELP}\n\n（被拒绝的位置：{exc.filename or path}）")
        raise SystemExit(1)


DATA_DIR = _ensure_data_dir(DATA_DIR)
# 打包版：refs/ 不会带进安装目录，官方 Python SDK 被复制到 sdk/ 下（开关屏 / 唤醒要用）
SDK_BUNDLED = os.path.join(APP_DIR, "sdk")
if os.path.isdir(SDK_BUNDLED):
    sys.path.insert(0, SDK_BUNDLED)

# 用 pythonw 启动时没有控制台，把输出重定向到日志，避免 print 崩溃
if sys.stdout is None or sys.stderr is None:
    _log_path = os.path.join(DATA_DIR, "server.log")
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
CONFIG_BAK_DIR = os.path.join(DATA_DIR, "config-backups")
ICON_DIR = os.path.join(DATA_DIR, "assets", "icons")
MY_ICON_DIR = os.path.join(DATA_DIR, "assets", "myicons")   # 「我的图标」：裁图工具存进来的图标包
CROP_CACHE = os.path.join(DATA_DIR, "cache", "crop")        # 裁图工具预览用的临时目录
LIBRARY_DIR = os.path.join(DATA_DIR, "assets", "library")   # 「相册」分类：收进来的图片
LIB_THUMB_DIR = os.path.join(DATA_DIR, "cache", "lib-thumb")  # 相册缩略图缓存（侧栏只加载小图，省内存）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_STILL_CACHE: dict = {}                                     # 动图缩略图首帧缓存：(路径, mtime) -> PNG 字节
SCENE_DIR = os.path.join(DATA_DIR, "scenes")
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
if os.environ.get("M18_NO_OFFICIAL"):
    # 模拟「这台电脑没装官方驱动」（测试用）：官方安装目录和数据目录都当不存在
    SD_ROOT = os.path.join(os.environ.get("TEMP", "C:\\"), "m18-no-official")
    official.SD_DATA = os.path.join(SD_ROOT, "data")
WALLPAPER = os.path.join(HERE, "assets", "wallpaper.jpg")
WS_SDK_EXE = os.environ.get("M18_WS_SDK") or os.path.normpath(
    os.path.join(APP_DIR, "ws-sdk", "StreamDockWebsocketSDK.exe"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "assets", "uploads")
GIF_CACHE = os.path.join(DATA_DIR, "assets", "gifcache")   # 交给 SDK 原生播放的 64×64 动图
PLUGIN_DATA = os.path.join(DATA_DIR, "plugin-data")          # 插件全局设置、进程日志
CUSTOM_PLUGIN_DIR = os.path.join(DATA_DIR, "custom-plugins")  # 自制插件（标准 .sdPlugin 目录）
CUSTOM_PREFIX = "com.m18.custom."
MIRROR_DIR = os.path.join(DATA_DIR, "official-mirror")   # 官方图标包 / 插件 / Node 的本地备份
# 随包内置的官方插件（com.hotspot.streamdock.*，PrivateAPI 那批）：不装官方驱动也能用
BUILTIN_PLUGIN_DIR = os.path.join(HERE, "builtin-plugins")
official.EXTRA_PLUGIN_ROOTS.extend([os.path.join(MIRROR_DIR, "plugins"), CUSTOM_PLUGIN_DIR])
if os.path.isdir(BUILTIN_PLUGIN_DIR):
    official.EXTRA_PLUGIN_ROOTS.insert(0, BUILTIN_PLUGIN_DIR)   # 内置的排最前
official.CUSTOM_PLUGIN_ROOTS.append(CUSTOM_PLUGIN_DIR)
official.EXTRA_ICON_ROOTS.append(os.path.join(MIRROR_DIR, "icons"))
PRESET_DIR = os.path.join(HERE, "presets")   # 首次运行载入的默认场景（音乐控制台 / 相册1）
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# 源码版和绿色版可以同时放在同一台电脑上：端口共用（谁先开谁生效），数据目录各自独立。
EDITION = "绿色版" if getattr(sys, "frozen", False) else "源码版"
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_LNKS = {"绿色版": "Stream Dock M18 控制台.lnk", "源码版": "Stream Dock M18 控制台（源码版）.lnk"}
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

MODES = ["off", "solid", "rainbow", "breathe", "chase", "gradient", "ripple", "custom"]
DEFAULT_LIGHTING = {"mode": "chase", "color": "#00aaff", "speed": 2.0, "brightness": 100}
CONSOLE_LABELS = {"screen": "屏幕开关", "mute": "屏蔽按键开关",
                  "light": "灯光开关", "lightfx": "灯光效果切换"}
STANDBY_MIN, STANDBY_MAX = 3, 300          # 待机等待秒数
AUTOBLANK_MIN, AUTOBLANK_MAX = 15, 1800    # 自动息屏：空闲多少秒后刷黑屏（15 秒 ~ 30 分钟）
ALBUM_TRANSITIONS = ("none", "fade", "slide", "wipe", "mosaic")
LEGACY_ALBUM_NAMES = ("相册", "幻灯片")      # 旧配置里可能把待机相册记成当前场景，读进来要纠正


def _led_geometry():
    """灯珠和按键在同一坐标系里的位置（和界面上的灯圈一致，1000×740），给涟漪算距离用。"""
    L, R, T, B = 70, 930, 70, 690
    pitch_x, span_y = (R - L) / 6, B - T
    pts = [(L, T + span_y * i / 5) for i in range(6)]
    pts += [(L + pitch_x * i, B) for i in range(1, 6)]
    pts += [(R, B - span_y * i / 5) for i in range(6)]
    pts += [(R - pitch_x * i, T) for i in range(1, 6)]
    pts += [(213, 566), (787, 566)]                       # 正面两颗
    g = 0.12
    cell = min(1000 * 0.74 / (5 + 4 * g), 740 * 0.50 / (3 + 2 * g))
    pitch = cell * (1 + g)
    left = 500 - (5 * cell + 4 * cell * g) / 2
    top = 740 * 0.42 - (3 * cell + 2 * cell * g) / 2
    keys = {}
    for k in range(1, 16):
        col, row_top = (k - 1) % 5, 2 - (k - 1) // 5
        keys[k] = (left + col * pitch + cell / 2, top + row_top * pitch + cell / 2)
    keys.update({16: (350, 566), 17: (500, 566), 18: (650, 566)})
    return pts, keys


LED_POINTS, KEY_POINTS = _led_geometry()


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


# -- SendInput：输入任意文字（含中文）、鼠标 ---------------------------------- #
_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send_inputs(items) -> bool:
    arr = (_INPUT * len(items))(*items)
    try:
        return ctypes.windll.user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) == len(items)
    except Exception:  # noqa: BLE001
        return False


def type_text(text: str, enter: bool = False) -> bool:
    """逐字符发 Unicode 键盘事件：中文、符号都能输入，不受输入法影响。"""
    items = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            for up in (0, 2):
                items.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0x0D, dwFlags=up))))
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            for flags in (0x4, 0x4 | 0x2):           # KEYEVENTF_UNICODE (+ KEYUP)
                items.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wScan=code, dwFlags=flags))))
    if enter:
        for up in (0, 2):
            items.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0x0D, dwFlags=up))))
    ok = True
    for i in range(0, len(items), 64):              # 分批发，长文本也不丢字
        ok = _send_inputs(items[i:i + 64]) and ok
        time.sleep(0.005)
    return ok


MOUSE_LABELS = {
    "click": "左键单击", "double": "左键双击", "right": "右键单击", "middle": "中键单击",
    "scrollup": "滚轮向上", "scrolldown": "滚轮向下",
}


def mouse_action(button: str, pos: str = "") -> bool:
    try:
        if pos:
            x, y = (int(v.strip()) for v in pos.replace("，", ",").split(",")[:2])
            ctypes.windll.user32.SetCursorPos(x, y)
    except (ValueError, OSError):
        pass
    m = lambda flags, data=0: _INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(dwFlags=flags, mouseData=data)))  # noqa: E731
    seq = {
        "click": [m(0x2), m(0x4)],
        "double": [m(0x2), m(0x4), m(0x2), m(0x4)],
        "right": [m(0x8), m(0x10)],
        "middle": [m(0x20), m(0x40)],
        "scrollup": [m(0x800, 120)],
        "scrolldown": [m(0x800, ctypes.c_ulong(-120).value)],
    }.get(button)
    return bool(seq) and _send_inputs(seq)


def foreground_app() -> tuple[str, str]:
    """前台窗口所属程序的 exe 名（小写）和窗口标题。"""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return "", title.value
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower(), title.value
        finally:
            kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def close_target(target: str) -> bool:
    """关闭前台窗口（相当于点 ×），或按进程名温和关闭。"""
    target = (target or "").strip()
    if not target:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return bool(hwnd) and bool(ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0))   # WM_CLOSE
    name = target if target.lower().endswith(".exe") else target + ".exe"
    try:
        subprocess.run(["taskkill", "/IM", name], capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:  # noqa: BLE001
        return False


WINDOW_LABELS = {"minimize": "最小化当前窗口", "maximize": "最大化 / 还原当前窗口", "desktop": "显示桌面"}


def window_action(op: str) -> bool:
    """对前台窗口操作。按 Dock 不会抢焦点，所以「前台窗口」就是你正在用的那个程序。"""
    if op == "desktop":
        return send_hotkey("win+d")
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    if op == "minimize":
        user32.ShowWindowAsync(hwnd, 6)                                  # SW_MINIMIZE
        return True
    if op == "maximize":
        user32.ShowWindowAsync(hwnd, 9 if user32.IsZoomed(hwnd) else 3)  # SW_RESTORE / SW_MAXIMIZE
        return True
    return False


class SoundPlayer:
    """播放音效（wav / mp3 / wma…），走 Windows MCI，独立线程排队执行。"""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.n = 0
        self.open_aliases: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _mci(self, cmd: str) -> int:
        try:
            return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
        except Exception:  # noqa: BLE001
            return -1

    def _loop(self) -> None:
        while True:
            op, path = self.q.get()
            if op == "stop":
                self._mci("close all")
                self.open_aliases.clear()
                continue
            self.n += 1
            alias = f"m18snd{self.n}"
            if self._mci(f'open "{path}" type mpegvideo alias {alias}') == 0:
                self._mci(f"play {alias} from 0")
                self.open_aliases.append(alias)
            # MCI 设备播完不会自动释放：只保留最近 8 个（允许叠着播），更早的关掉
            while len(self.open_aliases) > 8:
                self._mci(f"close {self.open_aliases.pop(0)}")

    def play(self, path: str) -> None:
        self.q.put(("play", path))

    def stop(self) -> None:
        self.q.put(("stop", ""))


def action_value(spec: dict) -> str:
    """动作在编辑区「值」里显示 / 编辑的内容。"""
    kind = spec.get("type")
    if kind == "toggle":
        return " | ".join(spec.get("keys") or [])
    if kind == "text":
        return spec.get("text", "")
    if kind == "mouse":
        return spec.get("pos", "")
    if kind == "close":
        return spec.get("target", "")
    if kind == "scene":
        return spec.get("scene", "")
    if kind == "sound":
        return spec.get("path", "")
    if kind == "folder":
        return spec.get("name", "")
    return str(spec.get("name") or spec.get("keys") or spec.get("url") or spec.get("path")
               or spec.get("action") or spec.get("macro") or "")


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
    if kind == "plugin":
        return f"插件 · {spec.get('name') or spec.get('action', '').rsplit('.', 1)[-1]}"
    if kind == "text":
        return "输入密码" if spec.get("secret") else f"输入：{spec.get('text', '')[:16]}"
    if kind == "toggle":
        return "双状态：" + " ⇄ ".join(spec.get("keys") or [])
    if kind == "mouse":
        return MOUSE_LABELS.get(spec.get("button", ""), "鼠标")
    if kind == "close":
        return f"关闭：{spec.get('target') or '当前窗口'}"
    if kind == "window":
        return WINDOW_LABELS.get(spec.get("op", ""), "窗口操作")
    if kind == "scene":
        return f"场景：{spec.get('scene', '')}"
    if kind == "console":
        return f"控制台：{CONSOLE_LABELS.get(spec.get('op', ''), spec.get('op', ''))}"
    if kind == "folder":
        return f"📁 {spec.get('name') or '文件夹'}"
    if kind == "sound":
        return f"音效：{os.path.basename(spec.get('path', ''))}" if spec.get("path") else "停止音效"
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


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(root))
    return path == root or path.startswith(root + os.sep)


def data_path(name: str) -> str:
    """相对路径按数据目录解析（上传的文件都在那里）；绝对路径原样返回。"""
    return name if os.path.isabs(name) else os.path.normpath(os.path.join(DATA_DIR, name))


def asset_path(name: str) -> str | None:
    """把界面传来的相对路径解析成绝对路径，只允许数据目录或程序目录里的文件。"""
    if not name:
        return None
    for root in (DATA_DIR, HERE):
        path = os.path.normpath(name if os.path.isabs(name) else os.path.join(root, name))
        if _inside(path, root) and os.path.isfile(path):
            return path
    return None


def store_path(path: str) -> str:
    """写进配置的路径：数据目录里的存相对路径，程序自带的用 app: 前缀，其他存绝对路径。"""
    if not path:
        return ""
    if _inside(path, DATA_DIR):
        return os.path.relpath(path, DATA_DIR)
    if _inside(path, HERE):
        return "app:" + os.path.relpath(path, HERE)
    return path


def load_path(value: str) -> str:
    if value.startswith("app:"):
        return os.path.join(HERE, value[4:])
    if os.path.isabs(value):
        return value
    cand = os.path.join(DATA_DIR, value)
    return cand if os.path.exists(cand) else os.path.join(HERE, value)


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 应用状态 + 灯光引擎
# --------------------------------------------------------------------------- #
def hex_to_rgb(value: str):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        return (0, 0, 0)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:              # 非十六进制（坏配置 / 插件传了乱值）：当黑色，别让灯效线程崩掉
        return (0, 0, 0)


def run_forever(fn, name: str) -> None:
    """后台循环的守护：循环体抛异常时记一笔、歇 1 秒再重进，而不是整条线程悄悄退出
    （以前动图 / 插件画面 / 灯效线程只要撞上一次「遍历时字典被改」就永久停摆）。"""
    fails = 0
    while True:
        try:
            fn()
            return                  # 循环函数自己正常返回（一般不会）
        except Exception as exc:  # noqa: BLE001
            fails += 1
            if fails <= 5 or fails % 100 == 0:
                log(f"后台任务「{name}」出错（第 {fails} 次，已自动恢复）：{type(exc).__name__}: {exc}")
            time.sleep(1.0)


def start_loop(fn, name: str) -> None:
    threading.Thread(target=run_forever, args=(fn, name), daemon=True, name=name).start()


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
        # 插件运行时状态：画面 / 标题都按动作实例 context 记（不落盘）
        self.plugin_host: PluginHost | None = None
        self._plugins_live = False
        self._plugin_lock = threading.RLock()
        self.plugin_visual: dict[str, dict] = {}
        self._plugin_visible: dict[str, tuple] = {}   # context -> (键号, 动作)
        self._plugin_dirty: dict[int, float] = {}
        self._raster_pending: dict[str, str] = {}     # SVG 光栅化请求 id -> context
        self.key_gestures: dict = {}                  # 当前页：键号 -> {"long": 动作, "double": 动作}
        self._press: dict[int, dict] = {}             # 长按 / 双击判定
        self._wake_swallow: set[int] = set()          # 待机时按下、只用来唤醒的键（连同它的松开一起吞掉）
        self.sound = SoundPlayer()
        self._scene_history: list[str] = []           # 「返回上一场景」
        self._auto_scene = None                       # 因为前台程序自动切过去的场景
        self._auto_return = None
        self._auto_applying = False
        self.recent_apps = deque(maxlen=12)
        self.plugin_logs: deque = deque(maxlen=1500)  # 插件日志（给界面和 AI 智能体拉取）
        self._plog_seq = 0
        self._gif_active: dict[int, str] = {}   # 正在由 SDK 原生播放的键 → GIF 文件
        self._gif_session = -1
        self.official_root_cfg = ""      # config.json 里手动指定的官方软件目录
        self.clients: list[queue.Queue] = []
        self.events = deque(maxlen=40)
        self.device = DeviceWS(self._on_input, exe=WS_SDK_EXE)
        self.device.quiet = False
        self.lighting = {
            **DEFAULT_LIGHTING,
            "custom": [[0, 0, 0] for _ in range(LED_COUNT)],
        }
        self._ripples: list = []          # 涟漪灯效：[(按键, 开始时间)]
        self._save_timer = None
        # 相册：整屏铺图片，可幻灯片播放
        self.album_items: list[str] = []          # UPLOAD_DIR 里的文件名
        self.album_index = 0
        self.album_slideshow = True
        self.album_interval = 8
        self.album_transition = "fade"
        self.album_fit = "cover"
        self.standby_source = "wallpaper"         # 待机时显示：wallpaper 壁纸 / album 相册幻灯片
        self._album_last = 0.0
        self._album_lock = threading.Lock()
        # 相册场景（可以有多个）：播放时用场景自己的图片和设置，待机相册不受影响
        self.album_scene_name = ""                # 正在播放的相册场景名（空 = 没在放相册场景）
        self._album_scene: dict = {}               # {"items": [...], "index": 0, "slideshow": …, …}
        self._album_return = None                  # 进相册前的场景名，退出时回到这里
        # 场景 / 页切换计数：界面用它判断要不要重拉按键预览（同名图片文件在不同场景里也要换）
        self.scene_rev = 0
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
        self.keep_awake = True       # 默认保活：设备自己睡过去后，WS SDK 叫不醒它（黑屏、收不到按键）
        self.close_to_tray = True    # 关掉界面窗口后留在托盘后台（关掉这个开关就是直接退出）
        self.standby_sec = 300
        self.autoblank_sec = 0         # 自动息屏：0 = 关；>0 = 空闲多少秒后刷黑屏（和待机画面二选一）
        self.standby_brightness = 60
        self.in_standby = False
        self.selected_key = 0
        self.screen_off = False
        self._blanked = False          # 已经刷成黑屏（息屏）了，别重复刷
        self._black_frame: bytes | None = None
        self.yielded = False
        self._light_back = ""          # 灯光开关：关灯前用的效果，再按一次开回来
        self.clipboard = None
        self.custom_scene_name = "自定义"
        self.active_scene = ""
        self._suspend_snapshot = False
        self._applying_scene = False
        self.autostart = self.autostart_state()["this"]
        self._last_activity = time.time()
        self._last_input_seen = time.time()   # 最近一次收到真实按键事件（看门狗用）
        self._last_tick = time.time()         # 巡检线程上一次醒来的时刻（用来发现「电脑休眠过」）
        self._last_wake = 0.0                 # 最近一次发唤醒包的时间（防止连发把设备按回休眠）
        self.clear_before_paint = False
        self.writes_paused = False
        # 仅灯光模式（旧调试开关，界面已去掉，固定关闭）
        self.lighting_only = False
        self.quiet = False
        self._num_cache: dict[int, bytes] = {}
        # 界面里「还没保存」的预览（拖图标悬停 / 放下后未点保存）：只画到设备上，不进配置
        self.key_previews: dict[int, bytes] = {}
        self._calib_until = 0.0
        self._identify = None
        self._pushing = False
        self._migrate_official_plugins()
        self._seed_presets()
        self._load_config()
        self.device.image_rotation = self.rotation
        if self._convert_builtin_plugins():
            self._save_config()
        self._bind_page()
        self._ensure_custom_snapshot()
        self._library_seed_existing()
        for page in self.pages:
            for k, name in ((16, "上一页"), (17, "主页"), (18, "下一页")):
                page["actions"].setdefault(k, {"type": "nav", "name": name})
        self._dirty = True
        self._t0 = time.time()
        self._frame = [[0, 0, 0] for _ in range(LED_COUNT)]
        self._frame_hash = None
        self._perf: dict = {}            # 灯效诊断：帧间隔 / 写灯带耗时
        self._anim_pushes = 0            # 诊断：主机侧推的动图张数（和灯带抢同一条通道）
        self._gif_retry_at = 0.0         # 上一次尝试恢复「SDK 原生播放动图」的时间
        self._last_push = 0.0
        self.official_running = self._detect_official()
        self.device.connect()
        if self.device.connected and not self.quiet and not self.yielded:
            self.device.set_screen_brightness(self.screen_brightness)
            self.device.set_led_brightness(self.lighting["brightness"])
            if self.wallpaper_enabled:
                self.apply_keys()
        start_loop(self._engine_loop, "灯效")
        start_loop(self._animation_loop, "动图")
        start_loop(self._watch_loop, "设备监视")
        start_loop(self._keepalive_loop, "保活")
        start_loop(self._standby_loop, "待机")
        start_loop(self._album_loop, "相册幻灯片")
        start_loop(self._diag_loop, "诊断")
        start_loop(self._official_loop, "官方软件检测")
        threading.Thread(target=self._auto_sync_official, daemon=True).start()
        threading.Thread(target=self._warm_gif_cache, daemon=True).start()
        threading.Thread(target=self.index_official_sources, daemon=True).start()
        start_loop(self._app_watch_loop, "前台程序监视")
        threading.Thread(target=self._auto_mirror_icons, daemon=True).start()

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
        # 热插拔线程和「官方驱动退出」线程可能同时发现设备回来：只接管一次
        lock = self.__dict__.setdefault("_restore_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            self._restore_all_locked()
        finally:
            lock.release()

    def _restore_all_locked(self) -> None:
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
            # WS SDK 的 wakeup() 是空实现，真正叫醒屏要靠背光命令。
            # 只在接管时发一次，别周期性发（连发会让设备进入「画面不刷新」的状态）。
            if self.wake_panel():
                self._last_wake = time.time()
            time.sleep(0.5)
            self.apply_keys()
            self.broadcast({"type": "status", "connected": True})
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
        if "\n" in text:
            lines = [ln for ln in text.split("\n") if ln.strip()][:3] or [""]
        elif od.textlength(text, font=font) > KEY_SIZE - 6 and len(text) > 3:
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

    @staticmethod
    def _draw_flash(img, kind: str):
        """插件 showOk / showAlert：在键面上盖一个对勾 / 感叹号。"""
        img = img.convert("RGB")
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([0, 0, KEY_SIZE, KEY_SIZE], fill=(0, 0, 0, 120))
        if kind == "ok":
            d.ellipse([16, 16, 48, 48], fill=(40, 180, 90, 255))
            d.line([(24, 32), (30, 39), (41, 25)], fill=(255, 255, 255, 255), width=4)
        else:
            d.polygon([(32, 13), (52, 48), (12, 48)], fill=(230, 160, 30, 255))
            d.rectangle([30, 24, 34, 38], fill=(255, 255, 255, 255))
            d.rectangle([30, 41, 34, 45], fill=(255, 255, 255, 255))
        return img

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
        act0 = self.key_actions.get(key)
        if isinstance(act0, dict) and act0.get("type") == "toggle" and "|" in text:
            parts = text.split("|")
            text = parts[int(act0.get("state") or 0) % len(parts)].strip()
        base = self._key_jpeg(key)
        vis = self._plugin_vis(key)
        flash = ""
        if vis:
            if vis.get("title") is not None:
                text = str(vis["title"]).strip()
            if vis.get("flash") and vis["flash"][1] > time.time():
                flash = vis["flash"][0]
        ck = (key, text, bg, fg, opacity, size, pos, hash(base) if base else 0, flash)
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

        img = self._draw_text(img, dict(style, text=text or ("" if base or vis else str(key))))
        if flash:
            img = self._draw_flash(img, flash)

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        if len(self._num_cache) > 400:          # 时钟 / 监控类插件每帧一张新图：超量就整体清掉重建
            self._num_cache.clear()
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

    # -- Dock 同步预览 ------------------------------------------------------ #
    def _preview_source(self, src: str) -> str | None:
        """界面传来的预览来源 → 本机图片文件。只认这几种，不接受任意路径。"""
        src = src or ""
        if src.startswith("pack:"):
            pack_id, _, fname = src[5:].partition("/")
            return self.icon_pack_file(pack_id, fname)
        if src.startswith("lib:"):
            return self.library_path(src[4:])
        if src.startswith("plugin:"):
            plugin_id, _, idx = src[7:].rpartition("/")
            try:
                return self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                return None
        if src.startswith("asset:"):
            return asset_path(src[6:])
        if src.startswith("program:"):
            return self.program_icon(src[8:])
        return None

    def set_key_preview(self, key: int, src: str) -> bool:
        """把一张还没保存的图临时显示在设备的某个键上（只取第一帧，不写配置）。"""
        if not (1 <= key <= KEY_COUNT):
            return False
        path = self._preview_source(src)
        if not path or not os.path.isfile(path):
            return False
        try:
            img = official.read_image(path)
            raw = img[1] if img else open(path, "rb").read()
            with Image.open(io.BytesIO(raw)) as im:
                rgb = ImageOps.fit(self._flat_rgb(im), (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=90)
        except Exception as exc:  # noqa: BLE001
            log(f"预览图读取失败 {path}: {exc}")
            return False
        others = [k for k in self.key_previews if k != key]
        self.key_previews = {key: buf.getvalue()}       # 同一时间只预览一个键
        self._repaint_keys([key, *others])
        return True

    def clear_key_preview(self, key: int = 0) -> bool:
        keys = list(self.key_previews) if not key else [k for k in self.key_previews if k == key]
        if not keys:
            return False
        for k in keys:
            self.key_previews.pop(k, None)
        self._repaint_keys(keys)
        return True

    def _repaint_keys(self, keys) -> None:
        if not (self.device.connected and not (self.in_standby or self.writes_paused or self.quiet
                                               or self.lighting_only or self.yielded or self.screen_off)):
            return
        self._anim_pause_until = time.time() + 0.6
        for k in keys:
            if not (1 <= k <= KEY_COUNT):
                continue
            if k in self.key_previews:
                # 预览盖住原生动图：先停掉这个键的动图，否则 SDK 会马上把下一帧画回去
                with self._gif_lock:
                    if self._gif_active.pop(k, None):
                        self.device.clear_key_gif(k)
            else:
                self._gif_update_key(k)
            if k not in self._gif_active:
                self.device.send_image(k, self._render_key(k))
        self.device.refresh()

    def _plugin_vis(self, key: int) -> dict | None:
        spec = self.key_actions.get(key)
        if isinstance(spec, dict) and spec.get("type") == "plugin":
            return self.plugin_visual.get(spec.get("context") or "")
        return None

    def _key_jpeg(self, key: int) -> bytes | None:
        pv = self.key_previews.get(key)
        if pv:
            return pv
        vis = self._plugin_vis(key)
        if vis and vis.get("jpeg"):
            return vis["jpeg"]
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
            if not getattr(self, "_blanked", False):
                self.blank_screen()     # 息屏：15 个键刷黑 + 背光 0
            return False
        self._blanked = False
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

    def _gif_want(self, key: int) -> str | None:
        """这个键应该由 SDK 播哪个 GIF：插件发来的动图 > 插件静态图（不播）> 按键自己的动图。"""
        vis = self._plugin_vis(key)
        if vis and (vis.get("gif") or vis.get("jpeg")):
            return vis.get("gif")
        return self._gif_path_for(key, self.key_overrides.get(key))

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
            for key in range(1, KEY_COUNT + 1):
                path = None if key in self.key_previews else self._gif_want(key)
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
            keys = sorted(want.keys())
        if keys:
            self.broadcast({"type": "gif", "t": time.time(), "keys": keys})

    def _gif_resync(self) -> None:
        """让所有正在播的动图从同一时刻重新开始，否则新加进来的那张会一直错位。

        只 stop/start 循环不够：每张动图是各自加载、各自计时的，停一下再开
        并不会把它们拉回同一个起点。这里整组清掉重发，再一起开始播。
        """
        with self._gif_lock:
            if not self._gif_active:
                return
            items = dict(self._gif_active)
            self.device.stop_gif_loop()
            for key in items:
                self.device.clear_key_gif(key)
            for key, path in items.items():
                self.device.set_key_gif(key, path)
            self._gif_active = dict(items)
            self.device.start_gif_loop()
            keys = sorted(items.keys())
        self.broadcast({"type": "gif", "t": time.time(), "keys": keys})

    def _gif_update_key(self, key: int) -> None:
        """单个键换图 / 改标题：只动这一个键。要在写静态画面之前调用。"""
        with self._gif_lock:
            self._gif_sync_session()
            want = self._gif_want(key) if self._gif_allowed() and key not in self.key_previews else None
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
        # 新加的动图要和大家对齐：整组停一下再一起开始
        if want:
            self._gif_resync()

    def apply_standby(self) -> bool:
        """待机：整屏铺壁纸（壁纸亮度在渲染层调暗，屏幕亮度保持正常）。"""
        if self.preview_mode == "album":
            self.preview_mode = "wallpaper"
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

    def _standby_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if self.album_active():
                    if self.device.connected and time.time() - self._last_paint > 25:
                        self._push_album()       # 定期补一帧，避免设备自己睡过去
                elif (self.autoblank_sec > 0 and not self.screen_off and not self.in_standby
                        and self.device.connected and not self.quiet and not self.yielded
                        and time.time() - self._last_activity > self.autoblank_sec):
                    # 自动息屏：空闲够久就把 15 个键刷黑 + 背光 0（按键通道还在，按任意键唤醒）
                    self.screen_off = True
                    self.blank_screen()
                    self.broadcast({"type": "status", "screenOff": True})
                    log(f"自动息屏：空闲 {self.autoblank_sec} 秒，屏幕已关闭（按任意键唤醒）")
                elif (self.wallpaper_enabled and self.standby_sec > 0
                        and not self.in_standby and not self.screen_off
                        and self.device.connected and not self.quiet
                        and time.time() - self._last_activity > self.standby_sec):
                    if self.standby_source == "album" and self.album_items:
                        self._album_return = None          # 待机进的相册，按键回到原来的按键画面
                        self.album_play()
                    else:
                        self.apply_standby()
                elif (self.in_standby and self.device.connected
                      and time.time() - self._last_paint > 25):
                    # 长时间待机时定期补一帧，避免设备自己睡过去显示开机图
                    self.apply_standby()
            except Exception:  # noqa: BLE001
                pass

    def _black_jpeg(self) -> bytes:
        """一张全黑的 64×64，用来把 15 个键刷成黑屏。"""
        if self._black_frame is None:
            img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), (0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72)
            self._black_frame = buf.getvalue()
        return self._black_frame

    def blank_screen(self) -> bool:
        """息屏：15 个键全刷成黑 + 背光 0 + 停掉动图。

        不用 transport 的硬件 sleep —— 设备一睡就不再上报按键，三个导航键按下去也叫不醒。
        刷黑在观感上就是关机，按键通道还活着，按一下就回按键画面。
        """
        dev = self.device
        if dev is None or not getattr(dev, "connected", False):
            return False
        try:
            self._gif_stop_all()
        except Exception:  # noqa: BLE001
            pass
        ok = True
        try:
            black = self._black_jpeg()
            for key in range(1, KEY_COUNT + 1):
                if not dev.send_image(key, black):
                    ok = False
            dev.refresh()
            self._frame_hash = None
        except Exception:  # noqa: BLE001
            ok = False
        try:
            dev.set_screen_brightness(0)
        except Exception:  # noqa: BLE001
            pass
        self._blanked = ok
        if ok:
            self.broadcast({"type": "status", "screenOff": True})
            log("已刷黑屏：15 个键全黑、背光 0（按键通道还在，按任意键唤醒）")
        return ok

    def sleep_panel(self) -> bool:
        """息屏（导航键「屏幕开关」的关屏动作）。"""
        return self.blank_screen()

    def wake_panel(self) -> bool:
        dev = self.device
        if dev is None or not getattr(dev, "connected", False):
            return False
        self._blanked = False
        try:
            dev.wakeup()
        except Exception:  # noqa: BLE001
            pass
        try:
            return bool(dev.set_screen_brightness(self.screen_brightness))
        except Exception:  # noqa: BLE001
            return False

    CONSOLE_LIGHT_MODES = ("solid", "rainbow", "breathe", "chase", "gradient", "ripple")

    def console_action(self, op: str) -> str:
        """按键上的「控制台」动作：屏幕开关 / 屏蔽按键 / 灯光开关 / 灯光效果切换。"""
        op = (op or "").strip()
        if op == "screen":
            if self.screen_off:
                self.screen_off = False
                self.wake_panel()
                self.apply_keys()
                self.broadcast({"type": "status", "screenOff": False})
                return "屏幕已打开"
            self.screen_off = True
            threading.Thread(target=self.sleep_panel, daemon=True).start()
            self.broadcast({"type": "status", "screenOff": True})
            return "屏幕已关闭"
        if op == "mute":
            self.input_muted = not self.input_muted
            self.broadcast({"type": "status", "inputMuted": self.input_muted})
            return "按键已屏蔽（只有这个键还能用）" if self.input_muted else "按键已恢复"
        if op == "light":
            mode = self.lighting.get("mode") or "chase"
            if mode == "off":
                back = self._light_back or "chase"
                self._light_back = ""
                self.set_lighting({"mode": back})
                return f"灯光已打开（{back}）"
            self._light_back = mode
            self.set_lighting({"mode": "off"})
            return "灯光已关闭"
        if op == "lightfx":
            modes = list(self.CONSOLE_LIGHT_MODES)
            cur = self.lighting.get("mode") or "chase"
            nxt = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else modes[0]
            self.set_lighting({"mode": nxt})
            return f"灯光效果：{nxt}"
        return "未知的控制台动作"

    def _wake_from_standby(self) -> None:
        """按键唤醒：先叫醒设备，再整屏重画。"""
        # 打开第二个句柄会有扰动，只有确实很久没画过、而且最近没叫醒过才用
        now = time.time()
        if not self.screen_off and now - self._last_paint > 600 and now - self._last_wake > 60:
            if self.wake_panel():
                self._last_wake = now
        try:
            if not self.screen_off:      # 息屏时别把背光调回去，否则黑屏会泛白
                self.device.set_screen_brightness(self.screen_brightness)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
        self.apply_keys()

    # -- 页面 -------------------------------------------------------------- #
    # -- 文件夹 ------------------------------------------------------------ #
    # 文件夹 = 一个不参与翻页的子页面（page["folder"] = True），按键动作 {"type":"folder","page":子页 id}。
    # 子页 11 号键（左上角）默认是「返回上级」；页面用稳定 id 关联，增删 / 调整页面不会串。
    FOLDER_BACK_KEY = 11

    @staticmethod
    def _new_page_id() -> str:
        return os.urandom(5).hex()

    def _ensure_page_ids(self) -> None:
        seen = set()
        for p in self.pages:
            if not p.get("id") or p["id"] in seen:
                p["id"] = self._new_page_id()
            seen.add(p["id"])

    def _page_index(self, pid: str) -> int:
        for i, p in enumerate(self.pages):
            if p.get("id") == pid:
                return i
        return -1

    def _main_pages(self) -> list[int]:
        """参与翻页的页面（不含文件夹子页）。"""
        return [i for i, p in enumerate(self.pages) if not p.get("folder")]

    def _folder_owner(self, pid: str):
        """文件夹子页 → (所在页序号, 按键)。"""
        for i, p in enumerate(self.pages):
            for k, spec in (p.get("actions") or {}).items():
                if isinstance(spec, dict) and spec.get("type") == "folder" and spec.get("page") == pid:
                    return i, k
        return None

    def _folder_path(self, index: int | None = None) -> list[dict]:
        """当前页的层级路径（面包屑）：[{name, index}, …]，最后一个是当前页。"""
        index = self.page if index is None else index
        chain, guard = [], 0
        while 0 <= index < len(self.pages) and guard < 20:
            p = self.pages[index]
            chain.append({"name": p.get("name", ""), "index": index, "folder": bool(p.get("folder"))})
            if not p.get("folder"):
                break
            owner = self._folder_owner(p.get("id"))
            if not owner:
                break
            index = owner[0]
            guard += 1
        return chain[::-1]

    def create_folder(self, key: int, name: str = "", gesture: str = "") -> dict:
        """把当前页的某个键设成文件夹：新建子页（11 号键 = 返回），按键标题默认是文件夹名。"""
        if not (1 <= key <= KEY_COUNT):
            return {"ok": False, "error": "只有屏幕键可以做文件夹"}
        name = (name or "").strip()[:20] or "文件夹"
        self._ensure_page_ids()
        parent = self.pages[self.page]
        sub = {"id": self._new_page_id(), "name": name, "folder": True, "images": {}, "styles": {},
               "gestures": {},
               "actions": {self.FOLDER_BACK_KEY: {"type": "nav", "name": "返回上级"},
                           16: {"type": "nav", "name": "返回上级"},
                           17: {"type": "nav", "name": "主页"},
                           18: {"type": "nav", "name": "返回上级"}}}
        back = self._load_image_source(self._glyph_icon("back"))
        if back:
            sub["images"][self.FOLDER_BACK_KEY] = back
        sub["styles"][self.FOLDER_BACK_KEY] = {"text": "返回", "size": 14, "pos": "bottom"}
        self.pages.append(sub)
        spec = {"type": "folder", "page": sub["id"], "name": name}
        if gesture in ("long", "double"):
            parent.setdefault("gestures", {}).setdefault(key, {})[gesture] = spec
        else:
            parent["actions"][key] = spec
            if key not in parent["images"]:            # 没有自己的图标：用文件夹图标 + 名字
                icon = self._load_image_source(self._glyph_icon("folder"))
                if icon:
                    parent["images"][key] = icon
                st = parent.setdefault("styles", {}).setdefault(key, {})
                if not (st.get("text") or "").strip():
                    st.update({"text": name[:12], "size": 14, "pos": "bottom"})
        self._save_config()
        if self.device.connected and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        log(f"按键 {key} 已设为文件夹「{name}」")
        return {"ok": True, "page": sub["id"]}

    @staticmethod
    def _glyph_icon(kind: str) -> str:
        """文件夹 / 返回 图标（设备字体画不了 emoji，直接画成图片），生成一次放在上传目录。"""
        path = os.path.join(UPLOAD_DIR, f"m18_{kind}.png")
        if os.path.isfile(path):
            return path
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        im = Image.new("RGB", (144, 144), (22, 30, 44))
        d = ImageDraw.Draw(im)
        if kind == "folder":
            d.rounded_rectangle([26, 30, 72, 52], radius=7, fill=(214, 150, 40))
            d.rounded_rectangle([22, 42, 122, 102], radius=10, fill=(242, 184, 75))
            d.rounded_rectangle([22, 54, 122, 102], radius=10, fill=(250, 200, 95))
        else:
            d.line([(44, 66), (104, 66)], fill=(150, 200, 255), width=14)
            d.polygon([(26, 66), (60, 38), (60, 94)], fill=(150, 200, 255))
        im.save(path, "PNG")
        return path

    def open_folder(self, pid: str) -> bool:
        idx = self._page_index(pid)
        if idx < 0:
            return False
        return self.goto_page(idx) or idx == self.page

    def folder_back(self) -> bool:
        """返回上级：子页回到放文件夹的那一页；主页面上没有上级，不动。"""
        p = self.pages[self.page]
        if not p.get("folder"):
            return False
        owner = self._folder_owner(p.get("id"))
        target = owner[0] if owner else (self._main_pages() or [0])[0]
        return self.goto_page(target)

    def _drop_orphan_folders(self) -> int:
        """删掉没有任何按键指向的文件夹子页（连带子页里的子文件夹）。"""
        removed = 0
        while True:
            refs = set()
            for p in self.pages:
                specs = list((p.get("actions") or {}).values())
                for g in (p.get("gestures") or {}).values():
                    specs.extend((g or {}).values())
                refs.update(s.get("page") for s in specs if isinstance(s, dict) and s.get("type") == "folder")
            orphans = [i for i, p in enumerate(self.pages) if p.get("folder") and p.get("id") not in refs]
            if not orphans:
                return removed
            cur_id = self.pages[self.page].get("id")
            for i in reversed(orphans):
                self.pages.pop(i)
                removed += 1
            idx = self._page_index(cur_id)
            self.page = idx if idx >= 0 else (self._main_pages() or [0])[0]

    def _bind_page(self) -> None:
        self.key_previews = {}
        self._ensure_page_ids()
        page = self.pages[self.page]
        self.key_overrides = page["images"]
        self.key_actions = page["actions"]
        self.key_styles = page.setdefault("styles", {})
        self.key_gestures = page.setdefault("gestures", {})
        self._plugin_sync()

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
        p = self.pages[self.page]
        log(f"进入文件夹「{p.get('name')}」" if p.get("folder") else f"切换到第 {self.page + 1} 页")
        return True

    def add_page(self) -> int:
        self.pages.append({
            "id": self._new_page_id(),
            "name": f"第 {len(self._main_pages()) + 1} 页",
            "images": {}, "actions": {
                16: {"type": "nav", "name": "上一页"},
                17: {"type": "nav", "name": "主页"},
                18: {"type": "nav", "name": "下一页"},
            }, "styles": {},
        })
        self._save_config()
        return len(self.pages) - 1

    def delete_page(self, index: int) -> bool:
        index = max(0, min(len(self.pages) - 1, index))
        target = self.pages[index]
        if not target.get("folder") and len(self._main_pages()) <= 1:
            return False
        if target.get("folder"):
            # 删除文件夹子页 = 去掉指向它的按键，子页和里面的子文件夹一起回收
            owner = self._folder_owner(target.get("id"))
            if owner:
                self.pages[owner[0]]["actions"].pop(owner[1], None)
                self.page = owner[0]
        else:
            cur_id = self.pages[self.page].get("id")
            self.pages.pop(index)
            idx = self._page_index(cur_id)
            if idx < 0:                      # 删的就是当前页：落到它前面一页（没有就第一页）
                mains = self._main_pages()
                before = [i for i in mains if i < index]
                idx = before[-1] if before else mains[0]
            self.page = idx
        self._drop_orphan_folders()
        self.page = max(0, min(len(self.pages) - 1, self.page))
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
        if self.pages[index].get("folder"):        # 文件夹改名：指向它的按键名字跟着改
            owner = self._folder_owner(self.pages[index].get("id"))
            if owner:
                self.pages[owner[0]]["actions"][owner[1]]["name"] = name
        self._save_config()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        return True

    def add_nav_pages(self, name: str) -> None:
        """翻页只在主页面之间走；在文件夹里按上一页 / 下一页 = 返回上级。"""
        mains = self._main_pages()
        if name == "返回上级" or (self.pages[self.page].get("folder") and name in ("上一页", "下一页")):
            self.folder_back()
            return
        pos = mains.index(self.page) if self.page in mains else 0
        if name == "下一页":
            if pos + 1 < len(mains):
                self.goto_page(mains[pos + 1])
        elif name == "上一页":
            if pos > 0:
                self.goto_page(mains[pos - 1])
        elif name == "主页":
            self.go_home()

    def go_home(self) -> bool:
        """「主页」键：回到当前场景的第一页；相册场景里是回到第一张图片。"""
        if self.album_active():
            return self.album_goto(index=0)
        mains = self._main_pages()
        target = mains[0] if mains else 0
        if self.page == target:
            log("已经是第 1 页")
            return False
        return self.goto_page(target)

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
            self.standby_sec = max(STANDBY_MIN, min(STANDBY_MAX, int(cfg.get("standby", 300))))
            lit = cfg.get("lighting")
            if isinstance(lit, dict):
                for k in ("mode", "color", "speed", "brightness"):
                    if lit.get(k) is not None:
                        self.lighting[k] = lit[k]
                if self.lighting["mode"] not in MODES:
                    self.lighting["mode"] = DEFAULT_LIGHTING["mode"]
                custom = lit.get("custom")
                if isinstance(custom, list) and len(custom) == LED_COUNT:
                    self.lighting["custom"] = [[int(v) for v in c[:3]] for c in custom]
            alb = cfg.get("album")
            if isinstance(alb, dict):
                self.album_items = [str(n) for n in (alb.get("items") or [])
                                    if os.path.isfile(os.path.join(UPLOAD_DIR, os.path.basename(str(n))))]
                self.album_slideshow = bool(alb.get("slideshow", True))
                self.album_interval = max(2, min(600, int(alb.get("interval", 8))))
                tr = str(alb.get("transition") or "fade")
                self.album_transition = tr if tr in ALBUM_TRANSITIONS else "fade"
                self.album_fit = "contain" if alb.get("fit") == "contain" else "cover"
            self.standby_source = "album" if cfg.get("standbySource") == "album" else "wallpaper"
            self.standby_brightness = max(0, min(100, int(cfg.get("standbyBrightness", 60))))
            self.custom_scene_name = str(cfg.get("customScene") or "自定义")[:40]
            self.active_scene = str(cfg.get("activeScene") or "")
            if self.active_scene in LEGACY_ALBUM_NAMES:   # 旧版把待机相册当成场景用
                self.active_scene = self.custom_scene_name
            self.wallpaper_enabled = bool(cfg.get("wallpaperEnabled", self.wallpaper_enabled))
            try:
                ab = int(cfg.get("autoBlank", 0) or 0)
            except (TypeError, ValueError):
                ab = 0
            self.autoblank_sec = 0 if ab <= 0 else max(AUTOBLANK_MIN, min(AUTOBLANK_MAX, ab))
            if self.autoblank_sec and self.wallpaper_enabled:
                self.wallpaper_enabled = False    # 二选一：自动息屏开着就不用待机画面
            self.keep_awake = bool(cfg.get("keepAwake", True))
            self.close_to_tray = bool(cfg.get("closeToTray", True))
            pages = cfg.get("pages")
            if isinstance(pages, list) and pages:
                self.pages = []
                for p in pages:
                    self.pages.append({
                        "id": str(p.get("id") or "") or self._new_page_id(),
                        "folder": bool(p.get("folder")),
                        "name": str(p.get("name") or "页"),
                        "images": {},
                        "actions": {},
                        "styles": {int(k): v for k, v in (p.get("styles") or {}).items()
                                   if str(k).isdigit()},
                        "gestures": {int(k): v for k, v in (p.get("gestures") or {}).items()
                                     if str(k).isdigit() and isinstance(v, dict) and v},
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
                path = load_path(wp)
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
            for attempt in range(3):
                try:
                    self._save_config_locked()
                    break
                except RuntimeError as exc:     # 别的线程正在增删按键字典：稍等重拍一次快照
                    if attempt == 2:
                        log(f"保存配置失败：{exc}")
                    time.sleep(0.05)
        self._plugin_sync()

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
            "wallpaperEnabled": self.wallpaper_enabled,
            "autoBlank": self.autoblank_sec,
            "keepAwake": self.keep_awake,
            "closeToTray": self.close_to_tray,
            "lighting": {k: self.lighting[k] for k in ("mode", "color", "speed", "brightness", "custom")},
            "standbySource": self.standby_source,
            "album": {"items": self.album_items, "slideshow": self.album_slideshow,
                      "interval": self.album_interval, "transition": self.album_transition,
                      "fit": self.album_fit},
            "pages": [
                {
                    "id": p.get("id") or "",
                    **({"folder": True} if p.get("folder") else {}),
                    "name": p.get("name", "页"),
                    "images": {str(k): os.path.basename(v.get("file", "") or "")
                               for k, v in p["images"].items() if v.get("file")},
                    "actions": {str(k): v for k, v in p["actions"].items()},
                    "styles": {str(k): v for k, v in (p.get("styles") or {}).items()},
                    "gestures": {str(k): v for k, v in (p.get("gestures") or {}).items() if v},
                }
                for p in self.pages
            ],
            "wallpaper": store_path(self.wallpaper_file) if self.wallpaper_file else "",
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
        self.library_collect([path])     # 换壁纸时，这张图也收进图标库的「相册」
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
        # 以前这里会整屏铺壁纸（像进了待机），只重画这一个键就够了
        if self.device.connected and not self.in_standby:
            self._gif_update_key(key)
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True

    def _apply_key(self, key: int) -> None:
        self._gif_update_key(key)
        if not self.key_overrides.get(key):
            return
        self.device.send_image(key, self._render_key(key))

    def _keepalive_loop(self) -> None:
        """低频巡检（30 秒）。

        注意：这里**不再**周期性发唤醒包。实测每 30 秒 call 一次 wakeup_screen，
        设备过一阵会进入「灯亮着但画面不再更新」的状态（切场景不同步）。
        唤醒只在真正需要时做一次（启动、按亮/灭、长时间没画过）。
        """
        while True:
            time.sleep(30)
            now = time.time()
            gap = now - getattr(self, "_last_tick", now)
            self._last_tick = now
            try:
                # 这一轮本该只睡 30 秒：实际过去很久，说明电脑刚休眠过。
                # 休眠后 SDK 那条连接常常变成「能发不能收」，灯还亮着但按键没反应。
                if gap > 90:
                    log(f"系统休眠了约 {int(gap / 60)} 分钟，正在恢复按键通道")
                    threading.Thread(target=self._after_resume, daemon=True).start()
                    continue
                if not self.device.connected or self.screen_off or self.yielded:
                    continue
                # 输入通道看门狗：15 分钟一个按键都没有，重订阅一次（比整条重连温和）
                if now - self._last_input_seen > 900:
                    self.device.resubscribe_input()
                    self._last_input_seen = now
                    log("输入通道 15 分钟无事件，已重新订阅按键")
                # 很久没画过（设备可能已睡过去）：叫醒一次再补画，别连发
                if now - self._last_paint > 1800 and now - self._last_wake > 600:
                    self._last_wake = now
                    self.wake_panel()
                    log("30 分钟没画过，发一次唤醒并补画")
                    self.apply_keys("keepalive")
            except Exception:  # noqa: BLE001
                pass

    def _after_resume(self) -> None:
        """电脑从休眠里醒过来之后收拾残局。

        休眠后有两个坑：
        1. SDK 连接常常变成「能发不能收」—— 灯效照写（灯还是对的），但按键读不回来，
           界面上看着「已连接」，按了却没反应；
        2. 设备自己那块屏也睡了 —— 再写图会被丢掉，看着就是「屏幕卡在醒之前那一帧」。
        所以先探活，活着就重发 read 拉回输入通道，再把背光关一下开一下强制重新初始化面板，
        然后整版重推一次、1.5 秒后再补推一次（刚醒的头几帧容易被设备丢掉）。
        探不通（半死连接）就整条断开，交给热插拔线程重连并重推。
        """
        try:
            dev = self.device
            if not dev.connected:
                return
            probe = getattr(dev, "probe_alive", None)
            if not dev.check_present() or (probe is not None and not probe()):
                log("休眠恢复：连接没有回应，重新连接设备")
                dev.mark_lost("休眠恢复后连接无回应")
                self.broadcast({"type": "status", "connected": False})
                return
            dev.resubscribe_input()
            if not self.screen_off:
                # 背光 0 → 原亮度：把设备面板从休眠里蹬一脚，否则推过去的图不刷新
                try:
                    dev.wakeup()
                    dev.set_screen_brightness(0)
                    time.sleep(0.12)
                    dev.set_screen_brightness(self.screen_brightness)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.25)
            if self.screen_off:
                self._blanked = False
                self.blank_screen()
            elif self.in_standby:
                self.apply_standby()
            else:
                self.apply_keys()
            self._gif_start_all()
            self._frame_hash = None
            self._dirty = True
            self._last_activity = time.time()
            self._last_input_seen = time.time()
            # 补推：设备刚醒时前面的图可能被吞掉，1.5 秒后再来一遍（不动「上一次画」的时间）
            time.sleep(1.5)
            if self.device.connected and not self.screen_off:
                try:
                    if self.in_standby:
                        self.apply_standby()
                    else:
                        self.apply_keys()
                    self._gif_start_all()
                except Exception:  # noqa: BLE001
                    pass
            log("休眠恢复：按键通道已重新订阅，画面和动图已重推")
            if not (getattr(dev, "firmware", "") or "").strip():
                log("设备上报的固件号是空的：这块屏可能处于异常状态（画面卡住时用设备页的「拔插设备」）")
                self.broadcast({"type": "notice",
                                "text": "设备状态看起来不太对：屏幕要是卡住不动，到设备页点「拔插设备（软件）」。",
                                "ms": 6500})
        except Exception as exc:  # noqa: BLE001
            log(f"休眠恢复处理失败：{exc}")

    def device_instance_id(self) -> str:
        """找 Dock 的 USB 复合设备实例 ID（软件拔插要用）。"""
        try:
            out = subprocess.run(["pnputil", "/enum-devices", "/connected"],
                                 capture_output=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # pnputil 的输出会跟着系统语言变，所以不认「Instance ID:」这个标签，
            # 直接在文本里找 USB\VID_6603&PID_1012\<序列号> 这串（标签是本地化的时候也稳）
            text = out.stdout.decode("mbcs", "ignore") + out.stderr.decode("mbcs", "ignore")
            import re as _re
            m = _re.search(r"USB\\VID_6603&PID_1012\\[^\\\s\"',]+", text, _re.IGNORECASE)
            if m:
                return m.group(0)
            log(f"软件拔插：枚举设备时没看到 VID_6603（输出 {len(text)} 字节）")
        except Exception as exc:  # noqa: BLE001
            log(f"软件拔插：枚举设备失败：{exc}")
        return ""

    def usb_reset(self) -> dict:
        """软件拔插：让 Windows 重新枚举这块设备，等于把 USB 线拔了再插。

        设备固件偶尔会卡在「命令收得到、画面不刷新」的状态（睡醒后最常见），
        这种情况普通命令救不回来，只有重新枚举能好。
        """
        import ctypes  # 局部导入：只有这条救援路径用得到

        inst = self.device_instance_id()
        if not inst:
            log("软件拔插：没找到 Dock 的 USB 实例 ID（设备可能已经掉了）")
            return {"ok": False, "error": "没找到设备的 USB 实例 ID"}
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            is_admin = False
        log(f"软件拔插：{inst}（管理员={is_admin}）")
        cmd = ["pnputil", "/restart-device", inst]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=40)
            text = (out.stdout + out.stderr).decode("utf-8", "ignore").strip()
        except Exception as exc:  # noqa: BLE001
            log(f"软件拔插失败：{exc}")
            return {"ok": False, "error": f"执行失败：{exc}"}
        if out.returncode != 0:
            log(f"软件拔插：pnputil 返回 {out.returncode}：{text[:300]}")
            low = text.lower()
            if "denied" in low or "拒绝" in text or "privilege" in low or "权限" in text:
                # 不是管理员：弹一次 UAC，用户点「是」之后由系统重启设备
                log("软件拔插：权限不够，弹一次 UAC 让用户确认")
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Start-Process pnputil -ArgumentList "
                                    f"'-restart-device' '{inst}' -Verb RunAs -Wait"],
                                   capture_output=True, timeout=90)
                except Exception as exc:  # noqa: BLE001
                    log(f"软件拔插：提权也没成功：{exc}")
                    return {"ok": False, "error": f"需要管理员权限才能拔插设备：{exc}"}
                text = "已通过管理员权限重启设备"
            else:
                return {"ok": False, "error": text or "重启设备失败"}
        log(f"软件拔插：{inst} 已重启，等设备重新枚举")
        # 等它重新接上（热插拔线程会自动接管并重画）
        for _ in range(24):
            time.sleep(0.5)
            if self.device.connected:
                break
        return {"ok": True, "instance": inst, "message": text, "connected": self.device.connected}

    def hard_recover(self, hard: bool = False) -> dict:
        """画面卡住（推过去的图不刷新，但灯效、按键都正常）时的重手恢复。

        按顺序：停动图 → 清掉所有键图（clearAllIcon）→ 背光 0 → 背光还原 →
        整版重推 + 动图重启。还不行就 hard=True：整条 SDK 连接断开重连
        （重连会重新枚举 HID，等于软拔插一次 USB）。
        """
        dev = self.device
        steps: list[str] = []
        if dev is None or not getattr(dev, "connected", False):
            return {"ok": False, "error": "设备没连上", "steps": steps}
        try:
            self._gif_stop_all()
            steps.append("停动图")
            dev.set_screen_brightness(0)
            steps.append("背光 0")
            time.sleep(0.2)
            dev.clear_keys()
            steps.append("清屏")
            time.sleep(0.2)
            dev.set_screen_brightness(self.screen_brightness)
            steps.append(f"背光 {self.screen_brightness}")
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            steps.append(f"前置出错：{exc}")
        if hard:
            try:
                dev.disconnect()
                steps.append("断开连接")
                time.sleep(1.2)
                if dev.connect():
                    steps.append("重新连接成功")
                    self._restore_all()
                else:
                    steps.append("重新连接失败")
            except Exception as exc:  # noqa: BLE001
                steps.append(f"重连出错：{exc}")
            log(f"强制恢复画面：{' / '.join(steps)}")
            return {"ok": True, "steps": steps}
        try:
            if self.screen_off:
                self._blanked = False
                self.blank_screen()
                steps.append("重新息屏")
            elif self.in_standby:
                self.apply_standby()
                steps.append("重推待机画面")
            else:
                self.apply_keys()
                self._gif_start_all()
                steps.append("重推 15 个键")
            self._frame_hash = None
            self._dirty = True
        except Exception as exc:  # noqa: BLE001
            steps.append(f"重推出错：{exc}")
        log(f"恢复画面：{' / '.join(steps)}")
        return {"ok": True, "steps": steps}

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
            host_keys = [k for k, it in list(self.key_overrides.items())
                         if it.get("type") == "anim" and it.get("frames") and k not in self._gif_active
                         and not self._plugin_vis(k)]
            if not host_keys:
                continue
            # 能用 SDK 原生播放就别走「主机推图」：推图走的是和灯带同一条通道，
            # 18 张/秒往里塞会把灯珠的帧挤断（灯效看起来一顿一顿）。
            # 原生播放被停掉后（待机/壁纸/息屏/整屏重画都会 stop），这里负责把它恢复回来。
            if self._gif_allowed():
                now_g = time.time()
                if now_g - self._gif_retry_at > 2.0:
                    self._gif_retry_at = now_g
                    self._gif_start_all()
                if self._gif_active:
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
                self._anim_pushes += pushed      # 诊断：主机侧推了多少张动图（会占通道）

    def _gap(self, gap: int | None = None) -> int:
        """键间缝隙；相册场景可以有自己的缝隙，传 gap 就按它算（None = 全局设置）。"""
        g = self.wallpaper_gap if gap is None else gap
        return max(0, min(64, int(g)))

    def _canvas_size(self, gap: int | None = None) -> tuple[int, int]:
        gap = self._gap(gap)
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

    def _canvas(self, mode: str | None = None) -> Image.Image | None:
        """当前要显示的画面，尺寸 = 含缝隙的总画布。支持填充方式 / 缩放 / 位移。"""
        w, h = self._canvas_size()
        mode = mode or self.preview_mode
        if mode == "album":
            return self._album_canvas(self.album_index)
        if mode == "calibration":
            return self._calibration_source()
        try:
            src = Image.open(self.wallpaper_file).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log(f"壁纸处理失败：{exc}")
            return None
        return self._fit_canvas(src, w, h)

    def _fit_canvas(self, src: Image.Image, w: int, h: int, zoom_offset: bool = True,
                    fit: str | None = None) -> Image.Image:
        """壁纸和相册共用的铺放：填充 / 完整 / 拉伸 + 缩放（壁纸还有拖动位移）+ 待机亮度。"""
        zoom = max(0.2, min(4.0, float(self.wallpaper_zoom)))
        tw, th = max(1, int(w * zoom)), max(1, int(h * zoom))
        fit = fit or self.wallpaper_fit
        if fit == "fill":
            scaled = src.resize((tw, th), Image.Resampling.LANCZOS)
        elif fit == "contain":
            base = ImageOps.pad(src, (w, h), color=(0, 0, 0),
                                method=Image.Resampling.LANCZOS)
            scaled = base.resize((tw, th), Image.Resampling.LANCZOS)
        else:
            scaled = ImageOps.fit(src, (tw, th), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        x = (w - scaled.width) // 2 + (int(self.wallpaper_ox) if zoom_offset else 0)
        y = (h - scaled.height) // 2 + (int(self.wallpaper_oy) if zoom_offset else 0)
        canvas.paste(scaled, (x, y))
        # 待机画面单独调暗（不影响按键画面的屏幕亮度）
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
            self.wallpaper_flip, self.rotation, "calibration" if self.preview_mode == "calibration" else "wp",
            self.standby_brightness,
        )

    def canvas_png(self) -> bytes | None:
        """壁纸卡片的预览：相册播放时也显示壁纸本身。"""
        key = self._canvas_key()
        cached = getattr(self, "_canvas_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        canvas = self._canvas("calibration" if self.preview_mode == "calibration" else "wallpaper")
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
                buf = io.BytesIO()           # 方向由设备层统一转（DeviceWS.image_rotation）
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

    # -- 旋转 ---------------------------------------------------------------- #
    def set_rotation(self, value: int) -> bool:
        rot = value % 360
        self.rotation = rot if rot in (0, 90, 180, 270) else 0
        self.device.image_rotation = self.rotation
        self._num_cache.clear()
        self._gif_active = {}
        self._save_config()
        if self.album_active():
            return self._push_album()
        if self.preview_mode == "calibration":
            return self.apply_calibration()
        if self.in_standby:
            return self.apply_standby()
        threading.Thread(target=self.apply_keys, daemon=True).start()
        return True

    # -- 相册 ------------------------------------------------------------------ #
    def album_active(self) -> bool:
        return self.in_standby and self.preview_mode == "album"

    # 相册数据源：在放相册场景时用场景自己的，否则用待机相册（全局设置）
    def _alb_items(self) -> list:
        if self.album_scene_name:
            return self._album_scene.get("items") or []
        return self.album_items

    def _alb_index(self) -> int:
        if self.album_scene_name:
            return int(self._album_scene.get("index") or 0)
        return self.album_index

    def _alb_slideshow(self) -> bool:
        if self.album_scene_name:
            return bool(self._album_scene.get("slideshow", True))
        return self.album_slideshow

    def _alb_interval(self) -> float:
        if self.album_scene_name:
            return float(self._album_scene.get("interval") or 8)
        return self.album_interval

    def _alb_transition(self) -> str:
        if self.album_scene_name:
            tr = str(self._album_scene.get("transition") or "fade")
            return tr if tr in ALBUM_TRANSITIONS else "fade"
        return self.album_transition

    def _alb_fit(self) -> str:
        if self.album_scene_name:
            fit = str(self._album_scene.get("fit") or "cover")
            return fit if fit in ("cover", "contain", "fill") else "cover"
        return self.album_fit

    def _alb_gap(self) -> int | None:
        """相册场景自己的键间缝隙；没单独设过就返回 None（跟全局设置走）。"""
        if self.album_scene_name:
            g = self._album_scene.get("gap")
            return None if g is None else self._gap(g)
        return None

    def _alb_gap_eff(self) -> int:
        g = self._alb_gap()
        return self._gap() if g is None else g

    def _alb_set_index(self, index: int) -> None:
        n = len(self._alb_items())
        if n <= 0:
            return
        index = int(index) % n
        if self.album_scene_name:
            self._album_scene["index"] = index
        else:
            self.album_index = index

    # -- 相册场景：一个场景 = 一组图片 + 过渡设置，存在场景文件里 --------------- #
    def _album_scene_data(self, name: str) -> dict | None:
        """读相册场景的 album 段；不是相册场景返回 None。"""
        if not name or name == self.custom_scene_name:
            return None
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return None
        alb = data.get("album")
        return alb if isinstance(alb, dict) else {}

    def is_album_scene(self, name: str) -> bool:
        return self._album_scene_data(name) is not None

    def _album_scene_items(self, name: str) -> list:
        alb = self._album_scene_data(name) or {}
        return [str(n) for n in (alb.get("items") or []) if str(n)]

    def _album_scene_write(self, name: str, alb: dict) -> bool:
        """把 album 段写回场景文件（顺手补齐 pages，方便导出 / 复制）。"""
        path = self._scene_path(name)
        data = self._scene_file_data(path) or {}
        if not isinstance(data.get("pages"), list):
            data["pages"] = [{"name": "相册", "images": {}, "actions": {}}]
            data.setdefault("page", 0)
        data["sceneType"] = "album"
        data["album"] = alb
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"保存相册场景失败：{exc}")
            return False
        return True

    def _album_scene_edit(self, name: str, fn) -> bool:
        """改相册场景的 album 段：fn(alb) 返回 False 表示没改动。"""
        alb = self._album_scene_data(name)
        if alb is None:
            log(f"「{name}」不是相册场景，改不了图片")
            return False
        alb = dict(alb)
        if not fn(alb):
            return False
        alb["items"] = [str(n) for n in (alb.get("items") or []) if str(n)]
        if not self._album_scene_write(name, alb):
            return False
        if self.album_scene_name == name:      # 正在放这个场景，内存里也同步
            alb["index"] = int(alb.get("index") or 0)
            self._album_scene = alb
        self.broadcast({"type": "album", **self.album_state()})
        return True

    ALBUM_NAV = {16: "上一页", 17: "主页", 18: "下一页"}

    def _album_nav_page(self, name: str) -> dict | None:
        """相册场景文件里的第一页（导航键的动作/手势都存在这里，不跟当前页走）。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return None
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        if not isinstance(pages[0], dict):
            return None
        return pages[0]

    def _album_nav_write(self, name: str, key: int, spec: dict | None, gesture: str = "") -> bool:
        """把相册场景里导航键的动作写回场景文件：长按 / 双击进 gestures，短按进 actions。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return False
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            pages = [{"name": name, "images": {}, "actions": {}}]
            data["pages"] = pages
            data["page"] = 0
        page = pages[0]
        if gesture in ("long", "double"):
            gs = page.get("gestures")
            if not isinstance(gs, dict):
                gs = {}
                page["gestures"] = gs
            one = gs.get(str(key))
            if not isinstance(one, dict):
                one = {}
            if spec:
                one[gesture] = spec
            else:
                one.pop(gesture, None)
            if one:
                gs[str(key)] = one
            else:
                gs.pop(str(key), None)
            if not gs:
                page.pop("gestures", None)
        else:
            acts = page.get("actions")
            if not isinstance(acts, dict):
                acts = {}
                page["actions"] = acts
            if spec:
                acts[str(key)] = spec
            else:
                acts.pop(str(key), None)
        try:
            path = self._scene_path(name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log(f"写入相册场景导航键失败：{exc}")
            return False

    def _album_nav_load(self, name: str) -> None:
        """进相册场景时，把场景文件里导航键的动作/手势装进内存，编辑区就能看到。"""
        page = self._album_nav_page(name)
        if not page:
            return
        acts = page.get("actions") if isinstance(page.get("actions"), dict) else {}
        for k in self.ALBUM_NAV:
            spec = acts.get(str(k))
            if isinstance(spec, dict):
                self.key_actions[k] = spec
        gs = page.get("gestures") if isinstance(page.get("gestures"), dict) else {}
        for k, one in gs.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            if key in self.ALBUM_NAV and isinstance(one, dict) and one:
                self.key_gestures[key] = one

    def _album_nav_step(self, key: int) -> None:
        """相册场景里导航键的短按：◀ 上一张 / ⌂ 第一张 / ▶ 下一张。"""
        if key == 16:
            self.album_goto(-1)
        elif key == 18:
            self.album_goto(1)
        elif key == 17:
            self.go_home()

    def _ensure_album_nav(self, name: str) -> None:
        """相册场景的三个导航键默认是 上页 / 主页 / 下页：补进场景文件，编辑区里能看到。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            return
        acts = pages[0].get("actions")
        if not isinstance(acts, dict):
            acts = {}
            pages[0]["actions"] = acts
        changed = False
        for k, nav in self.ALBUM_NAV.items():
            if str(k) not in acts:
                acts[str(k)] = {"type": "nav", "name": nav}
                changed = True
        if not changed:
            return
        try:
            tmp = self._scene_path(name) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._scene_path(name))
        except OSError as exc:
            log(f"写入相册场景默认导航键失败：{exc}")

    def _album_file_for(self, items: list, index: int) -> str | None:
        if not items:
            return None
        name = os.path.basename(items[index % len(items)])
        path = os.path.join(UPLOAD_DIR, name)
        return path if os.path.isfile(path) else None

    def album_state(self, active: bool | None = None, scene: str = "") -> dict:
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = [str(n) for n in (alb.get("items") or []) if str(n)]
            fit = str(alb.get("fit") or "cover")
            gap = alb.get("gap")
            playing = self.album_active() and self.album_scene_name == scene
            index = self._alb_index() if playing else int(alb.get("index") or 0)
            return {"items": items, "index": index, "active": bool(playing),
                    "slideshow": bool(alb.get("slideshow", True)),
                    "interval": max(2, min(600, int(alb.get("interval") or 8))),
                    "transition": str(alb.get("transition") or "fade"),
                    "fit": fit if fit in ("cover", "contain", "fill") else "cover",
                    "gap": self._gap() if gap is None else self._gap(gap),
                    "gapOwn": gap is not None,
                    "source": "album-scene", "scene": scene}
        return {"items": list(self._alb_items()), "index": self._alb_index(),
                "active": self.album_active() if active is None else active,
                "slideshow": self._alb_slideshow(), "interval": int(self._alb_interval()),
                "transition": self._alb_transition(), "fit": self._alb_fit(),
                "gap": self._alb_gap_eff(), "gapOwn": self._alb_gap() is not None,
                "standby": {"items": len(self.album_items),
                            "index": int(self.album_index),
                            "slideshow": bool(self.album_slideshow),
                            "interval": max(2, min(600, int(self.album_interval)))},
                "source": self.standby_source, "scene": self.album_scene_name}

    def _album_file(self, index: int) -> str | None:
        return self._album_file_for(self._alb_items(), index)

    def _album_copy_in(self, files: list) -> list:
        """把拖进来的图片收进上传目录，返回文件名列表（相册场景和待机相册共用）。"""
        names = []
        self._album_from_library = set()     # 这一次有几个是从「相册」分类来的
        for f in files[:200]:
            p = asset_path(f)
            if not p or not os.path.isfile(p):
                continue
            # 允许上传目录里的图，也允许图标库「相册」分类里的图（拖过去的时候）
            if not (_inside(p, UPLOAD_DIR) or _inside(p, LIBRARY_DIR)):
                continue
            try:
                with Image.open(p) as im:
                    im.verify()
            except Exception:  # noqa: BLE001
                continue
            if _inside(p, UPLOAD_DIR):
                name = os.path.relpath(p, UPLOAD_DIR).replace("\\", "/")
                if "/" not in name:
                    names.append(name)
                    continue
            # 来自上传目录的子文件夹，或者来自「相册」分类：复制一份到上传目录根下
            dst = os.path.join(UPLOAD_DIR, f"album_{int(time.time() * 1000)}_{len(names)}{os.path.splitext(p)[1]}")
            shutil.copyfile(p, dst)
            names.append(os.path.basename(dst))
            if _inside(p, LIBRARY_DIR):
                self._album_from_library.add(os.path.basename(dst))
        return names

    # -- 图标库的「相册」分类 -------------------------------------------- #
    def library_items(self) -> list:
        """「相册」分类里的图片（按时间倒序，最新收进来的排最前）。"""
        out = []
        try:
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            for fn in os.listdir(LIBRARY_DIR):
                if not fn.lower().endswith(IMAGE_EXTS):
                    continue
                fp = os.path.join(LIBRARY_DIR, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
        except Exception:  # noqa: BLE001
            return out
        out.sort(key=lambda it: (-it["mtime"], it["name"]))
        return out

    def library_path(self, name: str) -> str | None:
        safe = os.path.basename(name or "")
        if not safe:
            return None
        fp = os.path.join(LIBRARY_DIR, safe)
        return fp if os.path.isfile(fp) else None

    def library_thumb(self, name: str, size: int = 168) -> str | None:
        """相册缩略图：侧栏只加载小图，避免十几张十几 MB 的原图把界面拖卡。"""
        src = self.library_path(name)
        if not src:
            return None
        try:
            mtime = int(os.path.getmtime(src))
        except OSError:
            mtime = 0
        stem = os.path.splitext(os.path.basename(src))[0]
        os.makedirs(LIB_THUMB_DIR, exist_ok=True)
        dst = os.path.join(LIB_THUMB_DIR, f"{stem}-{mtime}-{size}.jpg")
        if os.path.isfile(dst):
            return dst
        try:
            with Image.open(src) as im:
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((size, size), Image.LANCZOS)
                im.save(dst, "JPEG", quality=84)
            return dst
        except Exception as exc:  # noqa: BLE001
            log(f"生成相册缩略图失败：{exc}")
            return None

    def library_collect(self, paths) -> int:
        """把图片收进「相册」分类：复制一份到 LIBRARY_DIR，重名自动加序号。"""
        n = 0
        try:
            os.makedirs(LIBRARY_DIR, exist_ok=True)
        except Exception:  # noqa: BLE001
            return 0
        for src in paths or []:
            try:
                if not src or not os.path.isfile(src):
                    continue
                if not src.lower().endswith(IMAGE_EXTS):
                    continue
                if os.path.normpath(os.path.dirname(os.path.abspath(src))) == os.path.normpath(LIBRARY_DIR):
                    continue                # 已经在相册里了
                base = os.path.basename(src)
                stem, ext = os.path.splitext(base)
                dst = os.path.join(LIBRARY_DIR, base)
                try:
                    if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                        continue            # 同一个文件已经收过了
                except OSError:
                    pass
                i = 2
                while os.path.exists(dst):
                    dst = os.path.join(LIBRARY_DIR, f"{stem} {i}{ext}")
                    i += 1
                shutil.copy2(src, dst)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        if n:
            self.broadcast({"type": "library", "items": self.library_items()})
            log(f"相册收进 {n} 张图片")
        return n

    def library_add(self, files) -> dict:
        paths = []
        for f in (files or [])[:200]:
            p = asset_path(f)
            if p and os.path.isfile(p):
                paths.append(p)
        n = self.library_collect(paths)
        return {"ok": bool(n), "added": n, "items": self.library_items()}

    def pick_program(self) -> str:
        """弹一个「选择程序」窗口（服务端在本机弹，浏览器拿不到真实路径）。"""
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$o=New-Object System.Windows.Forms.Form;"
            "$o.TopMost=$true; $o.ShowInTaskbar=$false; $o.FormBorderStyle='None';"
            "$o.StartPosition='CenterScreen'; $o.Size=New-Object System.Drawing.Size(1,1);"
            "$o.Show();"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='选择要绑定的程序（.exe / 快捷方式）';"
            "$d.Filter='程序 (*.exe;*.lnk;*.bat;*.cmd)|*.exe;*.lnk;*.bat;*.cmd|所有文件 (*.*)|*.*';"
            "$d.CheckFileExists=$true;"
            "if($d.ShowDialog($o) -eq 'OK'){ $d.FileName };"
            "$o.Close()"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                                 capture_output=True, text=True, timeout=300)
            return (out.stdout or "").strip()
        except Exception as exc:  # noqa: BLE001
            log(f"打开选择程序窗口失败：{exc}")
            return ""

    def _lnk_target_bytes(self, data: bytes) -> str:
        """从 .lnk 的字节里解析目标程序路径（先按结构读，再退化成字符串扫描）。"""
        if not data or len(data) < 0x4C:
            return ""
        import struct
        cands: list[str] = []
        flags = 0
        try:
            flags = struct.unpack_from("<I", data, 0x14)[0]
            pos = 0x4C
            if flags & 0x1:                         # HasLinkTargetIDList
                idl = struct.unpack_from("<H", data, pos)[0]
                pos += 2 + idl
            if flags & 0x2 and pos + 0x1C <= len(data):   # HasLinkInfo
                _li_size, li_hdr, li_flags = struct.unpack_from("<III", data, pos)
                if li_flags & 0x1:                  # VolumeIDAndLocalBasePath
                    base_off = struct.unpack_from("<I", data, pos + 16)[0]
                    start = pos + base_off
                    end = data.find(b"\x00", start)
                    if 0 < start < len(data) and end > start:
                        try:
                            cands.append(data[start:end].decode("mbcs", "ignore"))
                        except Exception:             # noqa: BLE001
                            pass
                    if li_hdr >= 0x24 and pos + 0x20 <= len(data):
                        uni_off = struct.unpack_from("<I", data, pos + 28)[0]
                        if uni_off:
                            start = pos + uni_off
                            end = start
                            while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
                                end += 2
                            if end > start:
                                cands.append(data[start:end].decode("utf-16-le", "ignore"))
        except Exception:                             # noqa: BLE001
            pass
        # 兜底：有些安装器写的快捷方式不规范，直接扫「X:\...\xxx.exe」这种串
        import re as _re
        pat = _re.compile(r"[A-Za-z]:\\[^\x00:*?\"<>|\r\n]{1,240}?\.(?:exe|com|bat|cmd)", _re.I)
        for enc in ("utf-16-le", "mbcs"):
            try:
                blob = data.decode(enc, "ignore")
            except Exception:                         # noqa: BLE001
                continue
            cands.extend(m.group(0) for m in pat.finditer(blob))
        for c in cands:
            p = os.path.expandvars(c.strip().strip('"'))
            if p and os.path.isfile(p):
                return p
        return ""

    def _program_roots(self) -> list[str]:
        roots = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "APPDATA"):
            p = os.environ.get(env)
            if p:
                roots.append(p)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(os.path.join(local, "Programs"))
        sysroot = os.environ.get("SystemRoot")
        if sysroot:
            roots.extend([os.path.join(sysroot, "System32"), os.path.join(sysroot, "SysWOW64")])
        home = os.path.expanduser("~")
        for name in ("Desktop", "Downloads", "OneDrive\\Desktop", "OneDrive\\桌面", "桌面"):
            roots.append(os.path.join(home, name))
        out = []
        for r in roots:
            if os.path.isdir(r) and r not in out:
                out.append(r)
        return out

    def _find_program(self, name: str, size: int = 0) -> str:
        """在常见目录里按「文件名（+ 大小）」找程序，找不到再让用户选。"""
        want = os.path.basename(name or "").lower()
        if not want:
            return ""
        deadline = time.time() + 6.0
        skip = {"$recycle.bin", "system volume information", "winsxs", "node_modules",
                "__pycache__", ".git", "temp", "tmp", "cache", "cache2", "crashdumps",
                "installer", "packages"}
        hits: list[str] = []
        for root in self._program_roots():
            if len(hits) >= 8 or time.time() > deadline:
                break
            for dirpath, dirnames, filenames in os.walk(root):
                if len(hits) >= 8 or time.time() > deadline:
                    break
                dirnames[:] = [d for d in dirnames if d.lower() not in skip]
                for fn in filenames:
                    if fn.lower() != want:
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        if size and os.path.getsize(fp) != size:
                            continue
                    except OSError:
                        continue
                    hits.append(fp)
                    if len(hits) >= 8:
                        break
        if not hits:
            return ""
        hits.sort(key=lambda p: (0 if "\\program files" in p.lower() else 1, len(p)))
        return hits[0]

    def resolve_program(self, name: str, size: int = 0, data: bytes | None = None,
                        path: str = "") -> dict:
        """尽量把拖进来的程序 / 快捷方式解析成真实路径，省掉让用户再选一次。"""
        hint = (path or "").strip().strip('"')
        if hint and os.path.isfile(hint):
            return {"ok": True, "path": hint, "how": "drop"}
        base = os.path.basename((name or "").strip())
        is_lnk = base.lower().endswith(".lnk")
        if data and is_lnk:
            tgt = self._lnk_target_bytes(data)
            if tgt:
                return {"ok": True, "path": tgt, "how": "shortcut"}
        if is_lnk:
            for root in self._program_roots():
                cand = os.path.join(root, base)
                if not os.path.isfile(cand):
                    continue
                try:
                    with open(cand, "rb") as fh:
                        tgt = self._lnk_target_bytes(fh.read())
                except OSError:
                    tgt = ""
                if not tgt:
                    tgt = self._lnk_target(cand)      # 退回到 PowerShell 解析
                if tgt and os.path.isfile(tgt):
                    return {"ok": True, "path": tgt, "how": "shortcut"}
        if not is_lnk:
            hit = self._find_program(base, size)
            if hit:
                return {"ok": True, "path": hit, "how": "search"}
        return {"ok": False, "error": "没找到这个程序的位置"}

    def program_icon(self, path: str, size: int = 64) -> str | None:
        """取程序 / 快捷方式的图标，存成 PNG 缓存。"""
        # 网络路径（\\主机\共享）和设备路径（\\?\、\\.\）一律不碰：
        # 访问它们会让 Windows 自动把本机账号的 NTLM 凭据发给对方
        if not path or path.replace("/", "\\").startswith("\\\\") or not os.path.isfile(path):
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:  # noqa: BLE001
            return None
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            mtime = 0
        stem = "".join(ch for ch in os.path.basename(path) if ch.isalnum() or ch in ".-_")[:40]
        os.makedirs(LIB_THUMB_DIR, exist_ok=True)
        dst = os.path.join(LIB_THUMB_DIR, f"icon-{stem}-{mtime}-{size}.png")
        if os.path.isfile(dst):
            return dst

        class SHFILEINFO(ctypes.Structure):
            _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                        ("dwAttributes", wintypes.DWORD),
                        ("szDisplayName", wintypes.WCHAR * 260),
                        ("szTypeName", wintypes.WCHAR * 80)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        hicon = None
        try:
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            info = SHFILEINFO()
            SHGFI_ICON, SHGFI_LARGEICON = 0x100, 0x0
            if not shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info),
                                          ctypes.sizeof(info), SHGFI_ICON | SHGFI_LARGEICON):
                return None
            hicon = info.hIcon
            if not hicon:
                return None
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            hdc = user32.GetDC(None)
            memdc = gdi32.CreateCompatibleDC(hdc)
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old = gdi32.SelectObject(memdc, hbmp)
            ctypes.memset(bits, 0, size * size * 4)
            user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, None, 3)
            raw = ctypes.string_at(bits, size * size * 4)
            gdi32.SelectObject(memdc, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc)
            im = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
            if im.getchannel("A").getextrema()[1] == 0:
                im = im.convert("RGB").convert("RGBA")
            bg = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            bg.alpha_composite(im)
            bg.convert("RGB").save(dst, "PNG")
            return dst
        except Exception as exc:  # noqa: BLE001
            log(f"取程序图标失败：{exc}")
            return None
        finally:
            try:
                if hicon:
                    ctypes.windll.user32.DestroyIcon(hicon)
            except Exception:  # noqa: BLE001
                pass

    def bind_program(self, key: int, path: str) -> dict:
        """把程序 / 快捷方式绑到按键上，并尽量用它的图标当按键画面。"""
        p = (path or "").strip().strip('"')
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "找不到这个文件"}
        if not self.set_key_action(key, {"type": "launch", "path": p}):
            return {"ok": False, "error": "绑定失败"}
        icon = self.program_icon(p)
        if icon:
            self.set_key_image(key, icon)
        self._save_config()
        return {"ok": True, "name": os.path.basename(p), "icon": bool(icon)}

    def library_remove(self, name: str) -> bool:
        fp = self.library_path(name)
        if not fp:
            return False
        try:
            os.remove(fp)
        except OSError:
            return False
        self.broadcast({"type": "library", "items": self.library_items()})
        return True

    def album_add(self, files: list, scene: str = "") -> bool:
        names = self._album_copy_in(files)
        if not names:
            return False
        # 加到待机相册 / 相册场景的图，也顺手收进图标库的「相册」里。
        # 从「相册」分类拖过去的不用再收一次（否则每拖一次就多一份重复图）。
        skip = getattr(self, "_album_from_library", set())
        rest = [os.path.join(UPLOAD_DIR, n) for n in names if n not in skip]
        if rest:
            self.library_collect(rest)
        if scene:
            def add(alb: dict) -> bool:
                alb["items"] = list(alb.get("items") or []) + names
                return True
            return self._album_scene_edit(scene, add)
        self.album_items.extend(names)
        self._save_config()
        return True

    def album_remove(self, index: int, scene: str = "") -> bool:
        if scene:
            def drop(alb: dict) -> bool:
                items = list(alb.get("items") or [])
                if not (0 <= index < len(items)):
                    return False
                items.pop(index)
                alb["items"] = items
                if int(alb.get("index") or 0) >= len(items):
                    alb["index"] = 0
                return True
            ok = self._album_scene_edit(scene, drop)
            if ok and not self._album_scene_items(scene) and self.album_scene_name == scene:
                threading.Thread(target=self.album_stop, daemon=True).start()
            return ok
        if not (0 <= index < len(self.album_items)):
            return False
        self.album_items.pop(index)
        if self.album_index >= len(self.album_items):
            self.album_index = 0
        if not self.album_items and self.album_active() and not self.album_scene_name:
            threading.Thread(target=self.album_stop, daemon=True).start()
        self._save_config()
        return True

    def album_move(self, src: int, dst: int, scene: str = "") -> bool:
        if scene:
            def move(alb: dict) -> bool:
                items = list(alb.get("items") or [])
                n = len(items)
                if not (0 <= src < n and 0 <= dst < n) or src == dst:
                    return False
                items.insert(dst, items.pop(src))
                alb["items"] = items
                return True
            return self._album_scene_edit(scene, move)
        n = len(self.album_items)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return False
        self.album_items.insert(dst, self.album_items.pop(src))
        self._save_config()
        return True

    def album_settings(self, body: dict, scene: str = "") -> None:
        if scene:
            def change(alb: dict) -> bool:
                if "slideshow" in body:
                    alb["slideshow"] = bool(body["slideshow"])
                if "interval" in body:
                    alb["interval"] = max(2, min(600, int(body["interval"])))
                if body.get("transition") in ALBUM_TRANSITIONS:
                    alb["transition"] = body["transition"]
                if body.get("fit") in ("cover", "contain", "fill"):
                    alb["fit"] = body["fit"]
                if "gap" in body:
                    alb["gap"] = max(0, min(64, int(body["gap"])))
                return True
            if self._album_scene_edit(scene, change):
                self._album_last = time.time()
                if self.album_active() and self.album_scene_name == scene:
                    threading.Thread(target=self._push_album, daemon=True).start()
            return
        if body.get("source") in ("wallpaper", "album"):
            self.standby_source = body["source"]
            self._save_config()
        if "slideshow" in body:
            self.album_slideshow = bool(body["slideshow"])
            self._album_last = time.time()
        if "interval" in body:
            self.album_interval = max(2, min(600, int(body["interval"])))
        if body.get("transition") in ALBUM_TRANSITIONS:
            self.album_transition = body["transition"]
        if body.get("fit") in ("cover", "contain", "fill"):
            self.album_fit = body["fit"]
            if self.album_active() and not self.album_scene_name:
                threading.Thread(target=self._push_album, daemon=True).start()
        self._save_soon(0.5)

    def _album_canvas(self, index: int) -> Image.Image | None:
        """相册图片铺满整版（含键间缝隙补偿和旋转），按内容缓存。"""
        return self._album_canvas_generic({"fit": self._alb_fit(), "gap": self._alb_gap()},
                                          self._alb_items(), index)

    def _album_canvas_generic(self, alb: dict, items: list, index: int) -> Image.Image | None:
        """按指定的图片列表 + 填充方式 + 缝隙铺整版；相册场景和待机相册共用。"""
        path = self._album_file_for(items, index)
        fit = str(alb.get("fit") or "cover")
        if fit not in ("cover", "contain", "fill"):
            fit = "cover"
        gap = self._gap(alb.get("gap"))
        w, h = self._canvas_size(gap)
        if not path:
            return Image.new("RGB", (w, h), (0, 0, 0))
        key = (path, os.path.getmtime(path), w, h, fit, self.standby_brightness)
        cache = getattr(self, "_album_cache", {})
        if key in cache:
            return cache[key]
        try:
            with Image.open(path) as im:
                src = self._flat_rgb(im)
            img = self._fit_canvas(src, w, h, zoom_offset=False, fit=fit)
        except Exception as exc:  # noqa: BLE001
            log(f"相册图片读取失败 {path}: {exc}")
            return Image.new("RGB", (w, h), (0, 0, 0))
        cache = dict(list(cache.items())[-8:])
        cache[key] = img
        self._album_cache = cache
        return img

    def _canvas_tiles(self, canvas: Image.Image, gap: int | None = None) -> dict:
        """整版画面切成 15 块（设备方向由 DeviceWS 统一转）。"""
        pitch = KEY_SIZE + self._gap(gap)
        out = {}
        for key in range(1, KEY_COUNT + 1):
            col = (key - 1) % GRID_COLS
            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
            sc, sr = self._cell_slot(col, row_top)
            out[key] = canvas.crop((sc * pitch, sr * pitch, sc * pitch + KEY_SIZE, sr * pitch + KEY_SIZE))
        return out

    def _send_tiles(self, tiles: dict, keys=None) -> None:
        for key in keys or tiles:
            buf = io.BytesIO()
            tiles[key].save(buf, "JPEG", quality=88, subsampling=0)
            self.device.send_image(key, buf.getvalue())

    def _album_can_push(self) -> bool:
        return (self.device.connected and not (self.writes_paused or self.quiet or self.yielded
                                               or self.screen_off))

    def _push_album(self, prev: Image.Image | None = None) -> bool:
        """把当前相册图片推到设备；prev 不为空时按设置的过渡效果切换。"""
        if not self._album_can_push():
            return False
        with self._album_lock:
            cur = self._album_canvas(self._alb_index())
            if cur is None:
                return False
            gap = self._alb_gap_eff()
            mode = self._alb_transition() if prev is not None and prev.size == cur.size else "none"
            self._pushing = True
            try:
                if mode in ("fade", "slide"):
                    steps = 5
                    w = cur.width
                    for i in range(1, steps + 1):
                        k = i / steps
                        if mode == "fade":
                            frame = Image.blend(prev, cur, k)
                        else:
                            ease = 1 - (1 - k) ** 2
                            off = int(w * ease)
                            frame = Image.new("RGB", cur.size)
                            frame.paste(prev, (-off, 0))
                            frame.paste(cur, (w - off, 0))
                        self._send_tiles(self._canvas_tiles(frame, gap))
                        self.device.refresh()
                        time.sleep(0.07)
                elif mode in ("wipe", "mosaic"):
                    tiles = self._canvas_tiles(cur, gap)
                    if mode == "wipe":       # 从左往右一列一列换（按观看方向的列）
                        groups: dict = {}
                        for key in tiles:
                            col = (key - 1) % GRID_COLS
                            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
                            groups.setdefault(self._cell_slot(col, row_top)[0], []).append(key)
                        order = [groups[c] for c in sorted(groups)]
                        gap_s = 0.09
                    else:
                        keys = list(tiles)
                        random.shuffle(keys)
                        order = [[k] for k in keys]
                        gap_s = 0.035
                    for group in order:
                        self._send_tiles(tiles, group)
                        self.device.refresh()
                        time.sleep(gap_s)
                else:
                    self._send_tiles(self._canvas_tiles(cur, gap))
                    self.device.refresh()
            finally:
                self._pushing = False
            self._last_paint = time.time()
        return True

    def album_play(self, index: int | None = None) -> bool:
        items = self._alb_items()
        if not items:
            return False
        if not self.album_scene_name:
            # 待机幻灯片（不是相册场景）：按任意键就回到当前场景
            self._album_return = None
        if index is not None:
            self._alb_set_index(index)
        self._gif_stop_all()
        self.preview_mode = "album"
        self.in_standby = True
        self._album_last = time.time()
        try:
            if not self.screen_off:
                self.device.set_screen_brightness(self.screen_brightness)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self._push_album, daemon=True).start()
        self.broadcast({"type": "album", **self.album_state()})
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True, "quiet": True})
        log(f"相册开始播放（{len(items)} 张）")
        return True

    def _play_album_scene(self, name: str) -> bool:
        """切到一个相册场景：用这个场景自己的图片和过渡设置播放。"""
        alb = self._album_scene_data(name)
        if alb is None:
            return False
        items = [str(n) for n in (alb.get("items") or []) if str(n)]
        if not items:
            log(f"相册场景「{name}」里还没有图片，先在编辑区加几张")
            return False
        if not self.album_active():
            self._album_return = self.active_scene or self.custom_scene_name
        self._ensure_album_nav(name)
        if not self.album_active():
            # 记住进相册前这三个键的动作，退出时还原
            self._album_nav_backup = {
                k: (self.key_actions.get(k),
                    dict(self.key_gestures.get(k) or {})) for k in self.ALBUM_NAV
            }
        self._album_nav_load(name)
        self.album_scene_name = name
        fit = str(alb.get("fit") or "cover")
        self._album_scene = {
            "items": items,
            "index": int(alb.get("index") or 0) % len(items),
            "slideshow": bool(alb.get("slideshow", True)),
            "interval": max(2, min(600, int(alb.get("interval") or 8))),
            "transition": str(alb.get("transition") or "fade") if str(alb.get("transition") or "") in ALBUM_TRANSITIONS else "fade",
            "fit": fit if fit in ("cover", "contain", "fill") else "cover",
            "gap": None if alb.get("gap") is None else self._gap(alb.get("gap")),
        }
        ok = self.album_play()
        if ok:
            log(f"已切换到相册场景：{name}（{len(items)} 张）")
        return ok

    def album_goto(self, delta: int = 1, index=None) -> bool:
        items = self._alb_items()
        if not items:
            return False
        prev = self._album_canvas(self._alb_index()) if self.album_active() else None
        if index is not None:
            self._alb_set_index(int(index))
        else:
            self._alb_set_index(self._alb_index() + delta)
        self._album_last = time.time()
        if self.album_active():
            threading.Thread(target=self._push_album, args=(prev,), daemon=True).start()
        self.broadcast({"type": "album", **self.album_state()})
        return True

    def album_stop(self) -> None:
        if self.preview_mode != "album":
            return
        scene_name = self.album_scene_name
        self.preview_mode = "wallpaper"
        self.in_standby = False
        self.album_scene_name = ""
        self._album_scene = {}
        self._last_activity = time.time()
        # 退出相册：把那三个导航键还原成进相册之前的样子，别把相册的设置带到原场景里
        backup = getattr(self, "_album_nav_backup", None)
        if backup:
            for k, (act, gs) in backup.items():
                if act is None:
                    self.key_actions.pop(k, None)
                else:
                    self.key_actions[k] = act
                if gs:
                    self.key_gestures[k] = gs
                else:
                    self.key_gestures.pop(k, None)
            self._album_nav_backup = None
        back = getattr(self, "_album_return", None)
        self._album_return = None
        self.broadcast({"type": "album", **self.album_state()})
        log(("相册场景已退出" if scene_name else "幻灯片已退出") + (f"，回到「{back}」" if back else ""))
        if back and back != (self.active_scene or self.custom_scene_name) and self.apply_scene(back):
            self.broadcast({"type": "scene", "name": back, "ok": True})
            return
        self.apply_keys()
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True, "quiet": True})

    def _album_loop(self) -> None:
        while True:
            time.sleep(0.25)
            try:
                items = self._alb_items()
                if (self.album_active() and self._alb_slideshow() and len(items) > 1
                        and time.time() - self._album_last >= self._alb_interval()):
                    self.album_goto(1)
            except Exception as exc:  # noqa: BLE001
                log(f"幻灯片出错：{exc}")
                time.sleep(2)

    def _album_path_for(self, scene: str, index: int) -> str | None:
        if scene:
            items = self._album_scene_items(scene)
            return self._album_file_for(items, index)
        return self._album_file(index) if 0 <= index < len(self._alb_items()) else None

    def album_thumb(self, index: int, scene: str = "") -> bytes | None:
        path = self._album_path_for(scene, index)
        if not path:
            return None
        try:
            with Image.open(path) as im:
                img = ImageOps.fit(self._flat_rgb(im), (160, 96), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None

    def _album_scene_index(self, scene: str, alb: dict) -> int:
        """正在播放这个场景时用内存里的当前张，否则用场景文件里存的。"""
        if self.album_active() and self.album_scene_name == scene:
            return self._alb_index()
        return int(alb.get("index") or 0)

    def album_preview_jpg(self, scene: str = "") -> bytes | None:
        """相册卡片里的大预览：当前图片按整版画布（观看方向）渲染。"""
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = self._album_scene_items(scene)
            if not items:
                return None
            canvas = self._album_canvas_generic(alb, items, self._album_scene_index(scene, alb))
        else:
            if not self.album_items:
                return None
            canvas = self._album_canvas(self.album_index)
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def album_tiles_png(self, scene: str = "") -> bytes | None:
        """相册当前图切成 15 块拼成精灵图（和设备上一致），给界面键盘预览叠在上面。"""
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = self._album_scene_items(scene)
            if not items:
                return None
            canvas = self._album_canvas_generic(alb, items, self._album_scene_index(scene, alb))
            gap = self._gap(alb.get("gap"))
        else:
            canvas = self._album_canvas(self.album_index)
            gap = self._alb_gap_eff()
        if canvas is None:
            return None
        sprite = Image.new("RGB", (GRID_COLS * KEY_SIZE, GRID_ROWS * KEY_SIZE), (0, 0, 0))
        for key, tile in self._canvas_tiles(canvas, gap).items():
            col = (key - 1) % GRID_COLS
            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
            # 界面里键格会反向转回正，所以这里给「观看方向」的图块，不转
            sprite.paste(tile, (col * KEY_SIZE, row_top * KEY_SIZE))
        buf = io.BytesIO()
        sprite.save(buf, "PNG")
        return buf.getvalue()

    # -- 外部输入 ---------------------------------------------------------- #
    def _on_input(self, key: int, state: int) -> None:
        self._last_input_seen = time.time()
        if state == 1 and self.lighting.get("mode") == "ripple":
            # 涟漪是纯灯光互动：屏蔽按键时也照常亮
            self.add_ripple(key)
            self._dirty = True
        if self.input_muted:
            # 屏蔽按键时，只有设了「控制台 → 屏蔽按键开关」的键还能用，否则关不回来
            acts = [self.key_actions.get(key) or {}]
            acts += [g for g in (self.key_gestures.get(key) or {}).values() if isinstance(g, dict)]
            if not any(a.get("type") == "console" and a.get("op") == "mute" for a in acts if isinstance(a, dict)):
                return
        if self.screen_off and state == 1:
            # 息屏时按任何键（含三个导航键）只负责唤醒，不执行动作：
            # 否则「长按 = 屏幕开关」的那个键会在唤醒的同时又把自己关掉。
            self.screen_off = False
            self._wake_swallow.add(key)
            self.broadcast({"type": "status", "screenOff": False})
            log(f"按键 {key}：屏幕是关的，先唤醒")
            threading.Thread(target=self._wake_screen_now, daemon=True).start()
            return
        if state == 0 and key in self._wake_swallow:
            self._wake_swallow.discard(key)
            return
        if self.album_active():
            # 相册场景播放中：点任意屏幕键都是「下一个场景」，导航键是 上页/主场景/下页；
            # 幻灯片（待机相册）只是待机画面，播放时 ◀ ▶ 翻照片、按其他键回到当前场景。
            self._last_activity = time.time()
            g = self.key_gestures.get(key) or {}
            if self.album_scene_name and (g.get("long") or g.get("double")):
                # 相册场景里给导航键设了长按/双击：照常按手势判定，短按还是 上页/主页/下页
                self._gesture(key, state, g)
                return
            if state == 1:
                self._wake_swallow.add(key)
                if self.album_scene_name:
                    # 相册场景：◀ ▶ 翻相册里的图片，中间键回主场景，其余屏幕键切下一个场景
                    if key == 16:
                        threading.Thread(target=self.album_goto, args=(-1,), daemon=True).start()
                    elif key == 18:
                        threading.Thread(target=self.album_goto, args=(1,), daemon=True).start()
                    elif key == 17:
                        threading.Thread(target=self.go_home, daemon=True).start()
                    else:
                        threading.Thread(target=self.next_scene, daemon=True).start()
                elif key in (16, 18) and len(self._alb_items()) > 1:
                    threading.Thread(target=self.album_goto, args=(-1 if key == 16 else 1,),
                                     daemon=True).start()
                else:
                    threading.Thread(target=self.album_stop, daemon=True).start()
            elif key in self._wake_swallow:
                self._wake_swallow.discard(key)
            return
        idle = time.time() - self._last_activity
        self._last_activity = time.time()
        was_standby = self.in_standby       # 唤醒线程会马上改掉它，先记下来
        # 只有真的在待机、或很久没操作（设备可能自己睡过去显示开机图）才整屏补画。
        # 以前是「距上次整屏绘制 > 1 秒」就补画，几乎每次按键都会先重写 15 个键、停启动图，
        # 这一大批数据堵在 SDK 队列前面，按键动作和插件画面就跟着延迟。
        if was_standby or idle > 600:
            threading.Thread(target=self._wake_from_standby, daemon=True).start()
        item = {"type": "input", "key": key, "state": state, "t": time.time()}
        self.events.appendleft(item)
        self.broadcast(item)
        log(f"按键事件 key={key} state={state} standby={was_standby}")
        # 待机壁纸状态下的第一次按键只用来唤醒：按下和随后的松开都不触发动作（插件也收不到）
        if state == 1 and was_standby:
            self._wake_swallow.add(key)
            log(f"按键 {key}：待机中，仅唤醒，不执行动作")
            return
        if state == 0 and key in self._wake_swallow:
            self._wake_swallow.discard(key)
            return
        act = self.key_actions.get(key)
        g = self.key_gestures.get(key) or {}
        if not (g.get("long") or g.get("double")):
            # 插件动作要收按下 / 松开两个事件，所以先走插件通道
            if isinstance(act, dict) and act.get("type") == "plugin":
                self._plugin_key(key, act, state)
                return
            # 只有短按：按下立即执行，零延迟
            if state == 1:
                if self.repaint_on_press:
                    self._maybe_repaint()
                threading.Thread(target=self._run_action, args=(key,), daemon=True).start()
            return
        # 设了长按 / 双击：一律交给手势判定。以前这里先判断插件就 return，
        # 结果「插件键 + 长按/双击」永远收不到手势，设了也没反应。
        self._gesture(key, state, g)

    def wake_from_console(self, why: str = "") -> bool:
        """息屏时只要在控制台里做了任何操作，就把屏幕点亮并补回画面。"""
        if not self.screen_off:
            return False
        self.screen_off = False
        self._last_activity = time.time()
        self._blanked = False
        self.broadcast({"type": "status", "screenOff": False})
        log(f"控制台操作（{why or 'api'}）→ 屏幕已唤醒")
        threading.Thread(target=self._wake_screen_now, daemon=True).start()
        return True

    def _wake_screen_now(self) -> None:
        """息屏状态下按了键：点亮屏幕并把按键画面补回去。"""
        try:
            self.wake_panel()
            try:
                self.device.resubscribe_input()   # 顺便把按键通道重新订一次，防止它悄悄断掉
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12)
            # 唤醒后回到息屏前那个场景，而不是让闲置计时立刻又进待机
            self._last_activity = time.time()
            if self.in_standby:
                self.in_standby = False
                self.preview_mode = "wallpaper"
            self.apply_keys()
        except Exception as exc:  # noqa: BLE001
            log(f"唤醒屏幕失败：{exc}")

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
                elif op == "text":
                    type_text(step.get("text", ""))
                elif op == "url":
                    if step.get("url"):
                        os.startfile(step["url"])
                elif op == "launch":
                    path = step.get("path") or ""
                    if os.path.exists(path):
                        os.startfile(path)
            except Exception as exc:  # noqa: BLE001
                log(f"宏步骤失败 {op}: {exc}")

    @staticmethod
    def _startup_points_to_me(ed: str) -> bool:
        """启动文件夹里的快捷方式确实指向这个版本吗？

        只「看得见文件」不够：早期用源码版跑过时留下的 `.lnk` 指着 `启动….bat`，
        绿色版看到它会误以为自启已经开着。所以还要看快捷方式的目标。
        """
        lnk = os.path.join(STARTUP_DIR, STARTUP_LNKS[ed])
        if not os.path.isfile(lnk):
            return False
        target = App._lnk_target(lnk).lower()
        if not target:                       # 读不出目标时按存在算，避免误报成「关」
            return True
        if ed == "源码版":
            return target.endswith(".bat")
        return target.endswith("m18console.exe")

    @staticmethod
    def autostart_state() -> dict:
        """开机自启现状：本版本有没有开；另一个版本有没有开（只会有一个生效，后启动的只是打开窗口）。"""
        other = "源码版" if EDITION == "绿色版" else "绿色版"
        this_on = App._startup_points_to_me(EDITION) or bool(App._run_value())
        other_on = App._startup_points_to_me(other)
        return {"this": this_on, "other": other if other_on else ""}

    # -- 开机自启：启动文件夹被拦时，退而写当前用户的 Run 项 -------------- #
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    RUN_NAME = "M18Console"

    @staticmethod
    def _run_value() -> str:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, App.RUN_KEY) as k:
                return str(winreg.QueryValueEx(k, App.RUN_NAME)[0])
        except OSError:
            return ""

    @staticmethod
    def _set_run(enabled: bool, cmd: str = "") -> bool:
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, App.RUN_KEY) as k:
                if enabled:
                    winreg.SetValueEx(k, App.RUN_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(k, App.RUN_NAME)
                    except OSError:
                        pass
            return True
        except OSError as exc:
            log(f"写注册表自启失败：{exc}")
            return False

    _lnk_cache: dict = {}

    @staticmethod
    def _lnk_target(lnk: str) -> str:
        """快捷方式指向哪儿（按修改时间缓存，状态接口频繁调用也不会每次起 PowerShell）。"""
        try:
            mtime = os.path.getmtime(lnk)
        except OSError:
            return ""
        hit = App._lnk_cache.get(lnk)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk.replace(chr(39), chr(39) * 2)}').TargetPath"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            App._lnk_cache[lnk] = (mtime, out.strip())
            return out.strip()
        except Exception:  # noqa: BLE001
            return ""

    def set_autostart(self, enabled: bool) -> bool:
        lnk = os.path.join(STARTUP_DIR, STARTUP_LNKS[EDITION])
        bat = sys.executable if FROZEN else os.path.abspath(os.path.join(HERE, "..", "启动 M18 控制台.bat"))
        # 两个版本只留一个开机自启，否则开机会先后启动两次
        others = [os.path.join(STARTUP_DIR, n) for n in STARTUP_LNKS.values() if n != STARTUP_LNKS[EDITION]]
        q = lambda p: p.replace("'", "''")  # noqa: E731
        if enabled:
            ps = "".join(f"Remove-Item -LiteralPath '{q(o)}' -ErrorAction SilentlyContinue;" for o in others) + (
                f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
                f"$s.TargetPath='{q(bat)}';$s.WorkingDirectory='{q(os.path.dirname(bat))}';"
                f"$s.Description='Stream Dock M18 控制台（{EDITION}）';$s.WindowStyle=7;$s.Save()"
            )
        else:
            ps = f"Remove-Item -LiteralPath '{q(lnk)}' -ErrorAction SilentlyContinue"
            if EDITION == "源码版":           # 旧版本建的同名快捷方式（指向 .bat 的）也一起删
                old = os.path.join(STARTUP_DIR, STARTUP_LNKS["绿色版"])
                if os.path.isfile(old) and self._lnk_target(old).lower().endswith(".bat"):
                    ps += f";Remove-Item -LiteralPath '{q(old)}' -ErrorAction SilentlyContinue"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"设置开机自启失败：{exc}")
            return False
        if enabled:
            ok = os.path.isfile(lnk)
            if ok:
                self._set_run(False)          # 启动文件夹成功了，清掉旧的注册表项
            else:
                # 「启动」文件夹常被安全软件的勒索防护拦写（火绒等）：
                # 退而写当前用户的 Run 项，效果一样，且不触发受保护文件夹
                ok = self._set_run(True, f'"{bat}"')
                log("启动文件夹写不进去，已改用注册表自启" if ok
                    else "开机自启没设置成功：启动文件夹和注册表都写不进去")
            if not ok:
                self.autostart = self.autostart_state()["this"]
                self._save_config()
                return False
        else:
            self._set_run(False)
        self.autostart = self.autostart_state()["this"]
        self._save_config()
        log(f"开机自启（{EDITION}）：{'开' if enabled else '关'}")
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
            self._convert_builtin_plugins()
            self._bind_page()
        finally:
            self._suspend_snapshot = False
        self._dirty = True
        if self.device.connected and not self.yielded:
            if not self.screen_off:
                self.device.set_screen_brightness(self.screen_brightness)
            self.apply_keys()
        return True

    def _scene_path(self, name: str) -> str:
        if name == self.custom_scene_name:
            return os.path.join(SCENE_DIR, "__custom__.json")
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (name or ""))[:40]
        return os.path.join(SCENE_DIR, safe.strip() + ".json")

    ALBUM_SCENE = "幻灯片"        # 待机幻灯片的显示名（它只是待机画面，不再占场景列表）

    def _scene_flags(self) -> dict:
        """场景的界面属性（停用等），存在场景目录的 .scene-flags.json 里；自定义 / 相册场景也能设。"""
        try:
            with open(os.path.join(SCENE_DIR, ".scene-flags.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_scene_muted(self, name: str, muted: bool) -> bool:
        name = self._canonical_scene(name)
        flags = self._scene_flags()
        item = flags.setdefault(name, {})
        if muted:
            item["muted"] = True
        else:
            item.pop("muted", None)
            if not item:
                flags.pop(name, None)
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(os.path.join(SCENE_DIR, ".scene-flags.json"), "w", encoding="utf-8") as fh:
                json.dump(flags, fh, ensure_ascii=False, indent=1)
        except OSError:
            return False
        self._links_cache = None
        log(f"场景「{name}」已{'停用（切换时跳过）' if muted else '启用'}")
        return True

    def _scene_order_path(self) -> str:
        return os.path.join(SCENE_DIR, ".scene-order.json")

    def _scene_order(self) -> list:
        """用户拖出来的场景顺序（名字列表），文件不存在就是空。"""
        try:
            with open(self._scene_order_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return [str(n) for n in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def set_scene_order(self, names: list) -> bool:
        exist = [it["name"] for it in self.list_scenes()]
        order = []
        for n in names or []:
            n = str(n)
            if n in exist and n not in order:
                order.append(n)
        order += [n for n in exist if n not in order]
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_order_path(), "w", encoding="utf-8") as fh:
                json.dump(order, fh, ensure_ascii=False, indent=1)
        except OSError:
            return False
        self._links_cache = None
        log("场景顺序已保存：" + " / ".join(order))
        return True

    def list_scenes(self) -> list:
        official_names = self.official_scene_names()
        flags = self._scene_flags()
        cur = self.current_scene()
        items = [{"name": self.custom_scene_name, "custom": True, "sceneType": "normal",
                  "active": cur == self.custom_scene_name}]
        try:
            files = sorted(os.listdir(SCENE_DIR))
        except OSError:
            files = []
        for f in files:
            if not f.lower().endswith(".json") or f.startswith(("__", ".")):
                continue
            name = os.path.splitext(f)[0]
            path = os.path.join(SCENE_DIR, f)
            data = {}
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    loaded = json.load(fh)
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, AttributeError):
                data = {}
            alb = data.get("album") if data.get("sceneType") == "album" else None
            if isinstance(alb, dict):
                items.append({"name": name, "custom": False, "album": True, "albumScene": True,
                              "sceneType": "album", "official": name in official_names,
                              "count": len([n for n in (alb.get("items") or []) if n]),
                              "active": cur == name})
                continue
            items.append({"name": name, "custom": False, "sceneType": "normal",
                          "official": name in official_names,
                          "apps": data.get("apps") or [],
                          "active": cur == name})
        for it in items:
            it["muted"] = bool(flags.get(it["name"], {}).get("muted"))
            if it["sceneType"] == "album":
                it["empty"] = int(it.get("count") or 0) == 0
        order = self._scene_order()
        if order:
            rank = {n: i for i, n in enumerate(order)}
            items.sort(key=lambda it: rank.get(it["name"], len(order)))
        return items

    def current_scene(self) -> str:
        """当前在用的场景名；播放相册场景时就是那个相册场景（幻灯片只是待机画面，不算场景）。"""
        if self.album_active() and self.album_scene_name:
            return self.album_scene_name
        return self.active_scene or self.custom_scene_name

    @staticmethod
    def _fresh_contexts(data: dict) -> dict:
        """场景另存 / 复制后，里面的插件实例要换新 context，否则和原场景共用同一个实例。"""
        for page in data.get("pages") or []:
            for spec in (page.get("actions") or {}).values():
                if isinstance(spec, dict) and spec.get("type") == "plugin":
                    spec["context"] = os.urandom(16).hex()
        return data

    def save_scene(self, name: str) -> bool:
        name = (name or "").strip()[:40]
        if not name:
            return False
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                json.dump(self._fresh_contexts(self.export_config()), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存场景失败：{exc}")
            return False
        # 新建只是保存一份，不切换当前场景
        log(f"已保存场景：{name}")
        return True

    def create_scene(self, name: str, kind: str = "normal") -> dict:
        """新建场景：普通场景（把当前画面存一份）或相册场景（空的图片集，编辑区里再加图）。"""
        name = (name or "").strip()[:40]
        if not name:
            return {"ok": False, "reason": "场景名不能为空"}
        name = self._canonical_scene(name)
        if name == self.custom_scene_name or os.path.isfile(self._scene_path(name)):
            return {"ok": False, "reason": f"「{name}」已经有一个同名场景了"}
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            if kind == "album":
                data = {"sceneType": "album",
                        "album": {"items": [], "index": 0, "slideshow": True, "interval": 8,
                                  "transition": "fade", "fit": "cover"},
                        "pages": [{"name": name, "images": {},
                                   "actions": {str(k): {"type": "nav", "name": v}
                                               for k, v in self.ALBUM_NAV.items()}}],
                        "page": 0}
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
            else:
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(self._fresh_contexts(self.export_config()), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"新建场景失败：{exc}")
            return {"ok": False, "reason": f"新建失败：{exc}"}
        log(f"已新建{'相册' if kind == 'album' else '普通'}场景：{name}")
        return {"ok": True, "name": name, "kind": "album" if kind == "album" else "normal"}

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
                data = self._fresh_contexts(json.load(fh))
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

    # -- 单个场景导出 / 导入（.m18scene = zip：scene.json + 图片 + 用到的自制插件） ----------- #
    SCENE_PKG_VERSION = 1

    def _scene_data(self, name: str) -> dict | None:
        if name == self.custom_scene_name or not name:
            if self.active_scene in ("", self.custom_scene_name):
                return self.export_config()
            path = os.path.join(SCENE_DIR, "__custom__.json")
        else:
            path = self._scene_path(name)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def export_scene(self, name: str) -> dict:
        """打包一个场景：只含按键内容（页面、图片、动作、样式、关联程序），不含全局设置。"""
        if name == self.active_scene and name != self.custom_scene_name:
            self._snapshot_custom_scene()
        data = self._scene_data(name)
        if not data or not isinstance(data.get("pages"), list):
            return {"ok": False, "error": "读取场景失败"}
        scene = {k: data[k] for k in ("pages", "page", "apps") if k in data}
        buf = io.BytesIO()
        images, plugins = set(), set()
        for p in scene["pages"]:
            images.update(str(v) for v in (p.get("images") or {}).values() if v)
            specs = list((p.get("actions") or {}).values())
            for g in (p.get("gestures") or {}).values():
                specs.extend((g or {}).values())
            for s in specs:
                if isinstance(s, dict) and s.get("type") == "plugin" and str(s.get("plugin", "")).startswith(CUSTOM_PREFIX):
                    plugins.add(s["plugin"])
        missing = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("scene.json", json.dumps({"format": "m18scene", "version": self.SCENE_PKG_VERSION,
                                                  "name": name, "app": "M18 Console",
                                                  "scene": scene}, ensure_ascii=False, indent=1))
            for img in sorted(images):
                src = os.path.join(UPLOAD_DIR, os.path.basename(img))
                if not os.path.isfile(src):
                    src = os.path.join(ICON_DIR, os.path.basename(img))
                if os.path.isfile(src):
                    zf.write(src, f"images/{os.path.basename(img)}")
                else:
                    missing.append(img)
            for pid in sorted(plugins):
                folder = self._custom_folder(pid)
                if not os.path.isdir(folder):
                    continue
                for root, _dirs, files in os.walk(folder):
                    for f in files:
                        full = os.path.join(root, f)
                        zf.write(full, "plugins/" + os.path.relpath(full, CUSTOM_PLUGIN_DIR).replace("\\", "/"))
        log(f"已导出场景「{name}」：{len(images) - len(missing)} 张图片，{len(plugins)} 个自制插件")
        return {"ok": True, "name": name, "filename": f"{name}.m18scene",
                "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                "images": len(images) - len(missing), "plugins": len(plugins)}

    def import_scene_package(self, b64: str, name: str = "") -> dict:
        try:
            raw = base64.b64decode(b64.split(",", 1)[-1])
            zf = zipfile.ZipFile(io.BytesIO(raw))
            meta = json.loads(zf.read("scene.json").decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "不是有效的场景文件（.m18scene）"}
        if meta.get("format") != "m18scene" or not isinstance((meta.get("scene") or {}).get("pages"), list):
            return {"ok": False, "error": "不是有效的场景文件（.m18scene）"}
        total = sum(i.file_size for i in zf.infolist())
        if total > 300 * 1024 * 1024 or len(zf.infolist()) > 5000:
            return {"ok": False, "error": "场景文件过大"}
        scene = meta["scene"]
        # 图片：按内容哈希改名放进上传目录，同一张图重复导入不会堆副本
        renamed = {}
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        for info in zf.infolist():
            if not info.filename.startswith("images/") or info.is_dir():
                continue
            base = os.path.basename(info.filename)
            ext = os.path.splitext(base)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp") or not base:
                continue
            data = zf.read(info)
            new = f"scn_{hashlib.sha1(data).hexdigest()[:16]}{ext}"
            dst = os.path.join(UPLOAD_DIR, new)
            if not os.path.isfile(dst):
                with open(dst, "wb") as fh:
                    fh.write(data)
            renamed[base] = new
        # 自制插件：已存在同 id 的不覆盖（保留本机的版本）
        plugins_added = 0
        existing = set(os.listdir(CUSTOM_PLUGIN_DIR)) if os.path.isdir(CUSTOM_PLUGIN_DIR) else set()
        for info in zf.infolist():
            parts = info.filename.split("/")
            if parts[0] != "plugins" or len(parts) < 3 or info.is_dir():
                continue
            if not parts[1].startswith(CUSTOM_PREFIX) or not parts[1].endswith(".sdPlugin") or ".." in parts:
                continue
            dst = os.path.join(CUSTOM_PLUGIN_DIR, *parts[1:])
            if not _inside(dst, CUSTOM_PLUGIN_DIR):
                continue
            if parts[1] in existing:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(zf.read(info))
            if parts[2] == "manifest.json":
                plugins_added += 1
        for p in scene["pages"]:
            p["images"] = {k: renamed.get(os.path.basename(str(v)), os.path.basename(str(v)))
                           for k, v in (p.get("images") or {}).items() if v}
        self._fresh_contexts(scene)
        # 场景名：重名自动加序号
        base_name = ((name or meta.get("name") or "导入的场景").strip() or "导入的场景")[:36]
        if base_name == self.custom_scene_name:
            base_name += " 导入"
        target, i = base_name, 2
        while os.path.isfile(self._scene_path(target)):
            target = f"{base_name} {i}"
            i += 1
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_path(target), "w", encoding="utf-8") as fh:
                json.dump(scene, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            return {"ok": False, "error": f"保存失败：{exc}"}
        if plugins_added:
            self._refresh_plugins()
        target = self._canonical_scene(target)
        log(f"已导入场景「{target}」：{len(renamed)} 张图片，{plugins_added} 个自制插件")
        return {"ok": True, "name": target, "images": len(renamed), "plugins": plugins_added}

    def _canonical_scene(self, name: str) -> str:
        """场景名统一成文件名对应的写法（全角括号等会被换成 _），避免同一场景两种名字。"""
        if not name or name == self.custom_scene_name:
            return name
        return os.path.splitext(os.path.basename(self._scene_path(name)))[0]

    @staticmethod
    def _scene_has_content(data) -> bool:
        """场景里到底有没有内容：相册场景看有没有图片，普通场景看图片和非导航动作。"""
        if not isinstance(data, dict):
            return False
        if data.get("sceneType") == "album":
            alb = data.get("album") or {}
            return any(str(n) for n in (alb.get("items") or [])) if isinstance(alb, dict) else False
        for page in data.get("pages") or []:
            if not isinstance(page, dict):
                continue
            if page.get("images"):
                return True
            for act in (page.get("actions") or {}).values():
                if not (isinstance(act, dict) and act.get("type") == "nav"):
                    return True
        return False

    def _scene_file_data(self, path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def scene_hint(self, name: str) -> str:
        """场景切不了时给界面看的原因；返回空串表示可以切。"""
        name = self._canonical_scene(name)
        if name not in {it["name"] for it in self.list_scenes()}:
            if name in LEGACY_ALBUM_NAMES:
                return "「幻灯片」是待机画面，在右侧「待机画面 → 幻灯片」里设置，不占场景列表"
            return f"没有叫「{name}」的场景"
        target = (os.path.join(SCENE_DIR, "__custom__.json")
                  if name == self.custom_scene_name else self._scene_path(name))
        data = self._scene_file_data(target)
        if data is not None and data.get("sceneType") == "album":
            if not self._album_scene_items(name):
                return f"相册场景「{name}」里还没有图片，先在编辑区里拖几张图进去"
            return ""
        if data is not None and not self._scene_has_content(data):
            if name == self.custom_scene_name:
                return "「自定义」场景还是空的：先在界面上摆好按键，改动会自动存进「自定义」"
            return f"场景「{name}」里没有任何图片和动作，先给它加内容再切换"
        return ""

    def apply_scene(self, name: str) -> bool:
        name = self._canonical_scene(name)
        if not name:
            return False
        self.scene_rev += 1
        # 息屏状态下从控制台换场景：先亮屏，再切过去（否则会先补画旧场景、再被切走）
        if self.screen_off:
            self.screen_off = False
            self._blanked = False
            self.in_standby = False
            self._last_activity = time.time()
            try:
                self.wake_panel()
            except Exception:  # noqa: BLE001
                pass
            self.broadcast({"type": "status", "screenOff": False})
            log("换场景时屏幕是关的，先亮屏")
        if self.is_album_scene(name):
            if self._play_album_scene(name):
                return True
            log(f"相册场景「{name}」里还没有图片，切不过去")
            return False
        if self.album_active():
            self.preview_mode = "wallpaper"            # 退出相册，下面会整屏重画按键
            self.in_standby = False
            self.album_scene_name = ""
            self._album_scene = {}
            self._album_return = None
        if not self._auto_applying:
            self._auto_scene = None
        # 切走之前先把「自定义」里正在改的内容存一下，免得切回来少东西
        if (self.active_scene or self.custom_scene_name) == self.custom_scene_name:
            self._snapshot_custom_scene()
        target = (os.path.join(SCENE_DIR, "__custom__.json")
                  if name == self.custom_scene_name else self._scene_path(name))
        data = self._scene_file_data(target)
        if data is not None and not self._scene_has_content(data):
            # 空场景一切换就会把刚摆好的画面清空，宁可不切
            log(f"场景「{name}」里没有内容（图片和动作都是空的），不切换，免得把当前画面清掉")
            return False
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
        if not self._scene_has_content(cfg):
            # 当前画面是空的——多半是刚切场景失败 / 配置丢了。
            # 这时候写进快照会把原来存好的内容一起抹掉，所以只在快照本来就是空的时候才写。
            if os.path.isfile(path) and self._scene_has_content(self._scene_file_data(path)):
                log(f"当前画面是空的，没有写进场景「{name}」（避免覆盖已有内容）")
                return
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

    # -- 官方资源本地备份 ---------------------------------------------------- #
    @staticmethod
    def _shrink_gif(data: bytes, size: int) -> bytes:
        """动图逐帧缩小（按键只有 64×64），保留每帧时长和循环；本来就小的原样返回。"""
        try:
            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) <= size:
                    return data
                frames, durations = [], []
                for frame in ImageSequence.Iterator(im):
                    f = frame.convert("RGBA")
                    f.thumbnail((size, size), Image.Resampling.LANCZOS)
                    frames.append(f)
                    durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
            buf = io.BytesIO()
            frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                           loop=0, disposal=2, optimize=True)
            out = buf.getvalue()
            return out if len(out) < len(data) else data
        except Exception:  # noqa: BLE001
            return data

    def mirror_icons(self) -> int:
        """官方图标包备份到数据目录：PNG/JPG 缩成 144×144（按键只要 64×64），动图原样复制。"""
        root = os.path.join(MIRROR_DIR, "icons")
        made = 0
        for folder in official.icon_pack_dirs(SD_ROOT):
            if os.path.normpath(folder).startswith(os.path.normpath(root)):
                continue                                   # 已经是备份
            dst = os.path.join(root, os.path.basename(folder))
            info = official.icon_pack_info(folder)
            os.makedirs(os.path.join(dst, "icons"), exist_ok=True)
            for fname in os.listdir(folder):
                if fname.lower().endswith(".json"):
                    shutil.copyfile(os.path.join(folder, fname), os.path.join(dst, fname))
            for src in info["files"]:
                name = os.path.basename(src)
                ext = os.path.splitext(name)[1].lower()
                out = os.path.join(dst, "icons", name if ext in (".gif",) else os.path.splitext(name)[0] + ".png")
                if os.path.isfile(out):
                    continue
                try:
                    if ext in (".gif", ".mbg"):
                        img = official.read_image(src)
                        if img:
                            with open(os.path.join(dst, "icons", os.path.splitext(name)[0] + ".gif"), "wb") as fh:
                                fh.write(self._shrink_gif(img[1], 72))
                    else:
                        with Image.open(src) as im:
                            im = im.convert("RGBA")
                            im.thumbnail((96, 96), Image.Resampling.LANCZOS)
                            im.save(out, "PNG", optimize=True)
                    made += 1
                except Exception:  # noqa: BLE001
                    continue
        if made:
            log(f"官方图标包已备份到本地：新增 {made} 个图标")
            self._catalog(refresh=True)
        return made

    def mirror_plugins(self) -> dict:
        """第三方插件 + 官方自带的 Node 复制到数据目录：卸载官方驱动后插件照样能跑。"""
        root = os.path.join(MIRROR_DIR, "plugins")
        os.makedirs(root, exist_ok=True)
        copied = 0
        for p in self._catalog()["plugins"].values():
            folder = p["folder"]
            if p.get("builtin") or p.get("custom") or os.path.normpath(folder).startswith(os.path.normpath(MIRROR_DIR)):
                continue
            dst = os.path.join(root, os.path.basename(folder))
            man_old = official._plugin_manifest(dst) or {}
            if man_old.get("Version") == p.get("version") and os.path.isdir(dst):
                continue
            shutil.rmtree(dst, ignore_errors=True)
            try:
                shutil.copytree(folder, dst)
                copied += 1
            except OSError as exc:
                log(f"备份插件失败 {p['id']}：{exc}")
        node_src = os.path.join(SD_ROOT, "node", "node20.exe")
        node_dst = os.path.join(MIRROR_DIR, "node", "node20.exe")
        if os.path.isfile(node_src) and (not os.path.isfile(node_dst)
                                         or os.path.getsize(node_src) != os.path.getsize(node_dst)):
            os.makedirs(os.path.dirname(node_dst), exist_ok=True)
            shutil.copyfile(node_src, node_dst)
        if self.plugin_host:
            self.plugin_host.fallback_node = node_dst
        log(f"官方插件已备份到本地：{copied} 个")
        self._refresh_plugins()
        return {"plugins": copied, "node": os.path.isfile(node_dst)}

    def _auto_mirror_icons(self) -> None:
        time.sleep(20)                                     # 等启动忙完再在后台慢慢做
        try:
            self.mirror_icons()
        except Exception as exc:  # noqa: BLE001
            log(f"备份图标包失败：{exc}")

    def official_ready(self) -> bool:
        """官方 Stream Dock 驱动装没装：没装时「同步官方」没有东西可以同步。"""
        root = SD_ROOT or ""
        if not root or not os.path.isdir(root):
            return False
        return os.path.basename(os.path.normpath(root)).lower() != "m18-no-official"

    def sync_official(self, mode: str = "new", include_plugins: bool = False,
                      include_scenes: bool = True, include_icons: bool = True) -> dict:
        """「同步官方」按钮。

        可以只挑一部分同步：include_scenes / include_icons / include_plugins。
        场景按 mode 只加新的（new）或全部覆盖重导（all）。
        """
        if include_scenes or include_icons or include_plugins:
            self._catalog(refresh=True)
        if self.plugin_host and (include_scenes or include_icons or include_plugins):
            plugins = {}
            for uid, p in self._catalog()["plugins"].items():
                man = official._plugin_manifest(p["folder"])
                if man:
                    plugins[uid] = {"folder": p["folder"], "manifest": man}
            self.plugin_host.register_plugins(plugins)
        result = {"scenes": []}
        if include_scenes:
            result = self.sync_official_scenes(only_new=(mode != "all"))
        if include_icons:
            result["iconsMirrored"] = self.mirror_icons()
        mirrored_plugins = 0
        if include_plugins:
            mirrored = self.mirror_plugins()
            mirrored_plugins = mirrored.get("plugins", 0)
            result["node"] = mirrored.get("node", False)
        active = self.active_scene
        if mode == "all" and active and active in [r["name"] for r in result["scenes"]]:
            self.apply_scene(active)          # 当前场景被重导了，重新载入
        cat = self._catalog()
        result.update({"packs": len(cat["packs"]), "icons": sum(len(p["files"]) for p in cat["packs"].values()),
                       "plugins": len(cat["plugins"]), "mirroredPlugins": mirrored_plugins})
        return result

    def sync_official_scenes(self, only_new: bool = False) -> dict:
        """把官方驱动里 M18 的全部场景（含预设）转换后写进场景列表。不会切换当前场景。"""
        plugins = official.PluginIndex(SD_ROOT)
        rev = self._vk_reverse()
        base = self.export_config()
        report, names = [], []
        os.makedirs(SCENE_DIR, exist_ok=True)
        existing = self.official_scene_names()
        for item in official.list_scene_sources():
            if only_new and os.path.isfile(self._scene_path(item["name"].strip()[:40])):
                names.append(item["name"].strip()[:40])
                continue
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
            app_id = str(item["manifest"].get("AppIdentifier") or "").strip()
            if app_id and app_id != "*":
                cfg["apps"] = [os.path.basename(app_id).lower()]
            try:
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, ensure_ascii=False, indent=2)
            except OSError as exc:
                log(f"写入场景「{name}」失败：{exc}")
                continue
            names.append(name)
            report.append({"name": name, "kind": item["kind"], **sc["stats"]})
        names = sorted(set(names) | (existing if only_new else set()))
        try:
            with open(self._official_mark_path(), "w", encoding="utf-8") as fh:
                json.dump({"names": names, "time": int(time.time()), "pluginsMigrated": True},
                          fh, ensure_ascii=False)
        except OSError:
            pass
        log("官方场景同步完成：" + "，".join(
            f"{r['name']}（{r['supported']}/{r['total']} 键可执行，{r['pages']} 页）" for r in report))
        return {"scenes": report}

    def _seed_presets(self) -> None:
        """首次运行：把随包带的默认场景（音乐控制台 / 相册1）装进用户数据目录。

        只在用户还没有任何场景文件时做一次；相册图片同时拷进上传目录（场景里记的是文件名）。
        """
        try:
            preset_scenes = os.path.join(PRESET_DIR, "scenes")
            if not os.path.isdir(preset_scenes):
                return
            existing = []
            if os.path.isdir(SCENE_DIR):
                existing = [n for n in os.listdir(SCENE_DIR)
                            if n.endswith(".json") and not n.startswith(".") and n != "__custom__.json"]
            if existing:
                return
            os.makedirs(SCENE_DIR, exist_ok=True)
            album_src = os.path.join(PRESET_DIR, "album1")
            if os.path.isdir(album_src):
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                for name in sorted(os.listdir(album_src)):
                    src = os.path.join(album_src, name)
                    dst = os.path.join(UPLOAD_DIR, name)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        shutil.copy2(src, dst)
            names = []
            for name in sorted(os.listdir(preset_scenes)):
                if not name.endswith(".json"):
                    continue
                shutil.copy2(os.path.join(preset_scenes, name), os.path.join(SCENE_DIR, name))
                names.append(os.path.splitext(name)[0])
            if names:
                log(f"已载入默认场景：{' / '.join(names)}")
        except Exception as exc:  # noqa: BLE001
            log(f"载入默认场景失败：{exc}")

    def _library_seed_existing(self) -> None:
        """「相册」分类还是空的时候，把当前壁纸和待机幻灯片里的图先收进去。"""
        try:
            if self.library_items():
                return
            paths = []
            wp = self.wallpaper_file
            if wp:
                p = wp if os.path.isabs(wp) else data_path(wp)
                if p and os.path.isfile(p):
                    paths.append(p)
            items = list(self.album_items or [])
            for idx in range(min(len(items), 60)):
                p = self._album_file_for(items, idx)
                if p:
                    paths.append(p)
            if paths:
                n = self.library_collect(paths)
                if n:
                    log(f"相册分类已收进 {n} 张（当前壁纸和待机幻灯片）")
        except Exception as exc:  # noqa: BLE001
            log(f"收集现有图片失败：{exc}")

    def _migrate_official_plugins(self) -> None:
        """已同步过的官方场景里，插件控件（时钟 / 天气等）当时没接上：一次性补上插件动作。
        只补「没有动作」的键，不覆盖用户改过的内容；当前正在用的场景也同步改 config.json。"""
        mark = self._official_mark_path()
        try:
            with open(mark, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return
        if meta.get("pluginsMigrated") or not meta.get("names"):
            return
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
                live = json.load(fh)
        except (OSError, ValueError):
            live = {}
        plugins = official.PluginIndex(SD_ROOT)
        rev = self._vk_reverse()
        sources = {it["name"]: it for it in official.list_scene_sources()}
        added_total = 0
        for name in meta["names"]:
            item = sources.get(name)
            path = self._scene_path(name)
            if not item or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    scene = json.load(fh)
                conv = official.convert_scene(item, plugins, SD_ROOT, rev)
            except Exception as exc:  # noqa: BLE001
                log(f"补全官方场景插件失败（{name}）：{exc}")
                continue
            targets = [scene["pages"]]
            if live.get("activeScene") == name and isinstance(live.get("pages"), list):
                targets.append(live["pages"])
            added = 0
            for pages in targets:
                for i, page in enumerate(conv["pages"]):
                    if i >= len(pages):
                        break
                    acts = pages[i].setdefault("actions", {})
                    for key, spec in page["actions"].items():
                        if spec.get("type") == "plugin" and str(key) not in acts:
                            acts[str(key)] = dict(spec)
                            added += 1
            if added:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(scene, fh, ensure_ascii=False, indent=2)
                added_total += added
        if live.get("pages") and added_total:
            with self._save_lock:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(live, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)
        meta["pluginsMigrated"] = True
        with open(mark, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        log(f"官方场景插件控件补全：新增 {added_total} 个插件动作")

    def _auto_sync_official(self) -> None:
        """首次启动自动同步一次；之后用户删掉的官方场景不会再自己冒出来。"""
        if os.path.isfile(self._official_mark_path()):
            return
        try:
            self.sync_official_scenes()
        except Exception as exc:  # noqa: BLE001
            log(f"自动同步官方场景失败：{exc}")

    def _scene_usable(self, it: dict) -> bool:
        """这个场景现在切得过去吗：停用的跳过，空相册 / 空场景也跳过。"""
        if it.get("muted"):
            return False
        if it.get("sceneType") == "album":
            return bool(self._album_scene_items(it["name"]))
        path = (os.path.join(SCENE_DIR, "__custom__.json")
                if it.get("custom") else self._scene_path(it["name"]))
        data = self._scene_file_data(path)
        return data is None or self._scene_has_content(data)

    def _step_scene(self, step: int) -> bool:
        items = self.list_scenes()
        names = [it["name"] for it in items]
        cur = self.current_scene()
        usable = {it["name"] for it in items if self._scene_usable(it)}
        idx = names.index(cur) if cur in names else -1
        target = None
        for hop in range(1, len(names) + 1):
            cand = names[(idx + step * hop) % len(names)]
            if cand in usable and cand != cur:
                target = cand
                break
        if not target:
            log("没有可切换的场景（其他场景都停用了或是空的）")
            return False
        if cur != target:
            self._scene_history.append(cur)
            del self._scene_history[:-20]
        ok = self.apply_scene(target)
        self.broadcast({"type": "scene", "name": target, "ok": ok})
        return ok

    def next_scene(self) -> bool:
        """官方「场景切换」键：按场景列表顺序切到下一个。"""
        return self._step_scene(1)

    def swap_keys(self, a: int, b: int) -> bool:
        top = KEY_COUNT + 3
        if a == b or not (1 <= a <= top) or not (1 <= b <= top):
            return False
        for store in (self.key_overrides, self.key_actions, self.key_styles, self.key_gestures):
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
        if not (1 <= key <= KEY_COUNT + 3):      # 16/17/18 是三个导航键，也要能复制
            return False
        self.clipboard = {
            "image": copy.deepcopy(self.key_overrides.get(key)),
            "action": copy.deepcopy(self.key_actions.get(key)),
            "style": copy.deepcopy(self.key_styles.get(key)),
            "gestures": copy.deepcopy(self.key_gestures.get(key)),
        }
        return any(self.clipboard.values())

    def paste_key(self, key: int) -> bool:
        if not self.clipboard or not (1 <= key <= KEY_COUNT + 3):
            return False
        for name, store in (("image", self.key_overrides),
                            ("action", self.key_actions),
                            ("style", self.key_styles),
                            ("gestures", self.key_gestures)):
            val = self.clipboard.get(name)
            if val:
                store[key] = copy.deepcopy(val)
                if name == "action" and isinstance(store[key], dict) and store[key].get("type") == "plugin":
                    store[key]["context"] = os.urandom(16).hex()
            else:
                store.pop(key, None)
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def clear_key_all(self, key: int) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):
            return False
        self.key_overrides.pop(key, None)
        self.key_actions.pop(key, None)
        self.key_styles.pop(key, None)
        self.key_gestures.pop(key, None)
        if self._drop_orphan_folders():
            self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    LONG_PRESS_SEC = 0.5
    DOUBLE_GAP_SEC = 0.3

    def _gesture(self, key: int, state: int, g: dict) -> None:
        """设了长按 / 双击的键：长按 ≥0.5 秒触发长按；两次点击间隔 <0.3 秒触发双击；否则是短按。"""
        st = self._press.setdefault(key, {})
        run = lambda act: threading.Thread(target=self._run_action, args=(key, act), daemon=True).start()  # noqa: E731

        def short() -> None:
            # 相册场景里的导航键，短按就是 上页 / 主场景 / 下页（别去跑那条 nav 动作）
            if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
                threading.Thread(target=self._album_nav_step, args=(key,), daemon=True).start()
                return
            run(None)
        if state == 1:
            st["down"] = time.time()
            st["long_fired"] = False
            if g.get("long"):
                down = st["down"]

                def fire_long():
                    if st.get("down") == down and not st.get("released"):
                        st["long_fired"] = True
                        log(f"按键 {key} → 长按")
                        run(g["long"])
                st["released"] = False
                st["long_timer"] = threading.Timer(self.LONG_PRESS_SEC, fire_long)
                st["long_timer"].start()
            return
        st["released"] = True
        down = st.get("down")
        if down:
            held = (time.time() - down) * 1000
            log(f"按键 {key} 松开，按住 {held:.0f}ms（长按阈值 {self.LONG_PRESS_SEC * 1000:.0f}ms）")
        if st.get("long_timer"):
            st["long_timer"].cancel()
        if st.get("long_fired"):
            return
        if g.get("double"):
            pending = st.get("single_timer")
            if pending and pending.is_alive():
                pending.cancel()
                st["single_timer"] = None
                log(f"按键 {key} → 双击")
                run(g["double"])
                return
            st["single_timer"] = threading.Timer(self.DOUBLE_GAP_SEC, short)
            st["single_timer"].start()
        else:
            short()

    def _run_action(self, key: int, act: dict | None = None) -> None:
        act = act if act is not None else self.key_actions.get(key)
        if not act:
            return
        kind = act.get("type") or "launch"

        if kind == "plugin":
            # 插件动作被设成短按 / 长按 / 双击时，这里补一对按下+松开，插件照常收到 keyDown / keyUp
            self._plugin_key(key, act, 1)
            self._plugin_key(key, act, 0)
            log(f"按键 {key} → 插件 {act.get('plugin')}")
            return

        if kind == "console":
            msg = self.console_action(str(act.get("op") or ""))
            log(f"按键 {key} → 控制台：{msg}")
            return

        if kind == "folder":
            ok = self.open_folder(str(act.get("page") or ""))
            log(f"按键 {key} → 打开文件夹「{act.get('name')}」" + ("" if ok else "（子页不存在）"))
            return

        if kind == "nav":
            name = act.get("name") or ""
            if name in ("上一页", "下一页", "主页", "返回上级"):
                self.add_nav_pages(name)
            elif name == "下一场景":
                self.next_scene()
            elif name == "返回上一场景":
                self.back_scene()
            elif name.startswith("跳到第"):
                digits = "".join(ch for ch in name if ch.isdigit())
                mains = self._main_pages()
                if digits and 0 < int(digits) <= len(mains):
                    self.goto_page(mains[int(digits) - 1])
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

        if kind == "toggle":
            keys = act.get("keys") or []
            if not keys:
                return
            st = int(act.get("state") or 0) % len(keys)
            ok = send_hotkey(keys[st])
            act["state"] = (st + 1) % len(keys)
            log(f"按键 {key} → 双状态热键 {keys[st]}（切到状态 {act['state'] + 1}）{'✓' if ok else '✗'}")
            self._save_config()
            if self.device.connected and 1 <= key <= KEY_COUNT and not self.in_standby:
                self.device.send_image(key, self._render_key(key))
                self.device.refresh()
            self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
            return

        if kind == "text":
            ok = type_text(act.get("text", ""), bool(act.get("enter")))
            log(f"按键 {key} → 输入{'密码' if act.get('secret') else '文本'} {'✓' if ok else '✗'}")
            return

        if kind == "mouse":
            ok = mouse_action(act.get("button", "click"), act.get("pos", ""))
            log(f"按键 {key} → {MOUSE_LABELS.get(act.get('button', ''), '鼠标')} {'✓' if ok else '✗'}")
            return

        if kind == "window":
            ok = window_action(act.get("op", ""))
            log(f"按键 {key} → {WINDOW_LABELS.get(act.get('op', ''), '窗口操作')} {'✓' if ok else '✗'}")
            return

        if kind == "close":
            ok = close_target(act.get("target", ""))
            log(f"按键 {key} → 关闭 {act.get('target') or '前台窗口'} {'✓' if ok else '✗'}")
            return

        if kind == "scene":
            name = act.get("scene", "")
            self._scene_history.append(self.current_scene())
            del self._scene_history[:-20]
            ok = self.apply_scene(name)
            self.broadcast({"type": "scene", "name": name, "ok": ok})
            log(f"按键 {key} → 切换场景 {name} {'✓' if ok else '✗'}")
            return

        if kind == "sound":
            path = act.get("path", "")
            if path and os.path.isfile(path):
                self.sound.play(path)
                log(f"按键 {key} → 播放音效 {os.path.basename(path)}")
            else:
                self.sound.stop()
                log(f"按键 {key} → 停止音效")
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

    @staticmethod
    def _parse_macro(raw: str) -> list:
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
            elif op in ("text", "type"):
                if val:
                    steps.append({"op": "text", "text": val})
            elif op in ("launch", "open", "run"):
                path = data_path(val)
                if val and os.path.exists(path):
                    steps.append({"op": "launch", "path": path})
        return steps

    def _build_action(self, spec: dict) -> dict | None:
        """界面提交的动作 → 存储格式；内容不合法返回 None。"""
        kind = spec.get("type") or "launch"
        if kind == "nav":
            name = str(spec.get("name") or "")
            return {"type": "nav", "name": name} if name else None
        if kind == "hotkey":
            keys = str(spec.get("keys") or "").strip()
            return {"type": "hotkey", "keys": keys} if keys else None
        if kind == "toggle":
            keys = [k.strip() for k in str(spec.get("keys") or "").split("|") if k.strip()]
            return {"type": "toggle", "keys": keys[:2], "state": 0} if len(keys) >= 2 else None
        if kind == "media":
            action = str(spec.get("action") or "")
            return {"type": "media", "action": action} if action in MEDIA_LABELS else None
        if kind == "macro":
            raw = str(spec.get("macro") or "")
            steps = self._parse_macro(raw)
            return {"type": "macro", "steps": steps, "macro": raw} if steps else None
        if kind == "url":
            url = str(spec.get("url") or "").strip()
            return {"type": "url", "url": url} if url else None
        if kind == "text":
            text = str(spec.get("text") or "")
            return {"type": "text", "text": text, "enter": bool(spec.get("enter")),
                    "secret": bool(spec.get("secret"))} if text else None
        if kind == "mouse":
            button = str(spec.get("button") or "click")
            return {"type": "mouse", "button": button, "pos": str(spec.get("pos") or "").strip()} \
                if button in MOUSE_LABELS else None
        if kind == "close":
            return {"type": "close", "target": str(spec.get("target") or "").strip()}
        if kind == "window":
            op = str(spec.get("op") or "")
            return {"type": "window", "op": op} if op in WINDOW_LABELS else None
        if kind == "scene":
            name = str(spec.get("scene") or "").strip()
            return {"type": "scene", "scene": name} if name else None
        if kind == "sound":
            path = str(spec.get("path") or "").strip()
            if path and not os.path.isfile(path):
                return None
            return {"type": "sound", "path": path}
        if kind == "console":
            op = str(spec.get("op") or "")
            return {"type": "console", "op": op} if op in CONSOLE_LABELS else None
        raw = str(spec.get("path") or "")
        path = data_path(raw)
        if not raw or not os.path.exists(path):
            return None
        return {"type": "launch", "path": path}

    def reset_nav_key(self, key: int) -> bool:
        """把 16/17/18 还原成默认：只保留短按（上一页 / 主页 / 下一页）。"""
        if key not in self.ALBUM_NAV:
            return False
        spec = {"type": "nav", "name": self.ALBUM_NAV[key]}
        self.key_gestures.pop(key, None)
        self.key_actions[key] = dict(spec)
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            self._album_nav_write(self.album_scene_name, key, spec, "")
            for g in ("long", "double"):
                self._album_nav_write(self.album_scene_name, key, None, g)
        self._save_config()
        if self.device.connected and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        log(f"按键 {key} 已还原默认：{spec['name']}")
        return True

    def set_key_action(self, key: int, spec: dict) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):
            return False
        if spec.get("type") == "folder":
            gesture = spec.get("gesture") if spec.get("gesture") in ("long", "double") else ""
            cur = (self.key_gestures.get(key) or {}).get(gesture) if gesture else self.key_actions.get(key)
            name = str(spec.get("name") or "").strip()[:20] or "文件夹"
            if isinstance(cur, dict) and cur.get("type") == "folder" and self._page_index(cur.get("page", "")) >= 0:
                cur["name"] = name                      # 已经是文件夹：改名
                self.pages[self._page_index(cur["page"])]["name"] = name
                self._save_config()
                return True
            ok = self.create_folder(key, name, gesture).get("ok", False)
            if ok and self._drop_orphan_folders():
                self._bind_page()
            return ok
        action = self._build_action(spec)
        if action is None:
            return False
        gesture = spec.get("gesture")
        if gesture in ("long", "double"):
            self.key_gestures.setdefault(key, {})[gesture] = action
        else:
            self.key_actions[key] = action
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            # 相册场景的导航键存在场景文件里，切场景回来还在
            self._album_nav_write(self.album_scene_name, key, action,
                                  gesture if gesture in ("long", "double") else "")
        if self._drop_orphan_folders():           # 原来是文件夹、被换成别的动作：子页一并回收
            self._bind_page()
        self._save_config()
        if self.device.connected and 1 <= key <= KEY_COUNT and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True

    def clear_key_gesture(self, key: int, gesture: str) -> bool:
        g = self.key_gestures.get(key) or {}
        if gesture not in g:
            return False
        g.pop(gesture, None)
        if not g:
            self.key_gestures.pop(key, None)
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            self._album_nav_write(self.album_scene_name, key, None, gesture)
        if self._drop_orphan_folders():
            self._bind_page()
        self._save_config()
        return True

    def back_scene(self) -> bool:
        while self._scene_history:
            name = self._scene_history.pop()
            if name != self.current_scene():
                ok = self.apply_scene(name)
                self.broadcast({"type": "scene", "name": name, "ok": ok})
                return ok
        return False

    # -- 前台程序自动切换场景 ---------------------------------------------- #
    def _scene_links(self) -> dict:
        """exe 名（小写）→ 场景名。5 秒缓存。"""
        cache = getattr(self, "_links_cache", None)
        if cache and time.time() - cache[0] < 5:
            return cache[1]
        links = {}
        for item in self.list_scenes():
            if item.get("muted"):
                continue
            for app in item.get("apps") or []:
                links.setdefault(app.lower(), item["name"])
        self._links_cache = (time.time(), links)
        return links

    def set_scene_apps(self, name: str, apps) -> bool:
        if name == self.custom_scene_name:
            return False
        path = self._scene_path(name)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        cleaned = []
        for a in apps or []:
            a = os.path.basename(str(a).strip().strip('"')).lower()
            if a and not a.endswith(".exe"):
                a += ".exe"
            if a and a not in cleaned:
                cleaned.append(a)
        data["apps"] = cleaned
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self._links_cache = None
        log(f"场景「{name}」关联程序：{', '.join(cleaned) or '（无）'}")
        return True

    def _app_watch_loop(self) -> None:
        """前台切到关联了场景的程序 → 自动切过去；离开 → 切回原来的场景。"""
        while True:
            time.sleep(0.8)
            try:
                self._app_watch_tick(*foreground_app())
            except Exception as exc:  # noqa: BLE001
                log(f"前台程序监测异常：{exc}")
                time.sleep(5)

    def _app_watch_tick(self, exe: str, title: str) -> None:
        if not exe or "M18 控制台" in title:
            return            # 正在用控制台窗口本身，不来回切
        if exe in self.recent_apps:
            self.recent_apps.remove(exe)
        self.recent_apps.appendleft(exe)
        links = self._scene_links()
        target = links.get(exe)
        active = self._canonical_scene(self.active_scene or self.custom_scene_name)
        if target:
            if active != target:
                if not self._auto_scene:
                    self._auto_return = active
                self._auto_scene = target
                self._auto_apply(target, f"前台程序 {exe}")
        elif self._auto_scene and active == self._auto_scene:
            back = self._auto_return or self.custom_scene_name
            self._auto_scene = None
            self._auto_apply(back, "离开关联程序")

    def _auto_apply(self, name: str, why: str) -> None:
        self._auto_applying = True
        try:
            ok = self.apply_scene(name)
        finally:
            self._auto_applying = False
        log(f"自动切换场景 → {name}（{why}）{'✓' if ok else '✗'}")
        self.broadcast({"type": "scene", "name": name, "ok": ok})

    def clear_key_action(self, key: int, gesture: str = "") -> bool:
        if gesture in ("long", "double"):
            return self.clear_key_gesture(key, gesture)
        if key not in self.key_actions:
            return False
        self.key_actions.pop(key, None)
        if self._drop_orphan_folders():
            self._bind_page()
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
            for folder in sorted(glob.glob(os.path.join(MY_ICON_DIR, "*.sdIconPack"))):
                info = official.icon_pack_info(folder)      # 「我的图标」：裁图工具存的
                if info["files"] and info["id"] not in packs:
                    info["mine"] = True
                    packs[info["id"]] = info
            plugins = {p["id"]: p for p in official.plugin_list(SD_ROOT)}
            cat = self._catalog_cache = {"packs": packs, "plugins": plugins}
            log(f"官方资源：图标包 {len(packs)} 个（{sum(len(p['files']) for p in packs.values())} 个图标），"
                f"插件 {len(plugins)} 个")
        return cat

    def icon_packs(self, refresh: bool = False) -> list:
        def _rev(p: dict) -> int:
            """包目录的修改时间：同名重裁后让前端不会拿浏览器旧缓存显示。"""
            try:
                return int(os.path.getmtime(p.get("folder") or ""))
            except (OSError, TypeError):
                return 0

        packs = [{
            "id": p["id"], "name": p["name"], "author": p["author"],
            "count": len(p["files"]), "mine": bool(p.get("mine")),
            "crop": bool(p.get("mine")), "rev": _rev(p),
            "files": [os.path.basename(f) for f in p["files"]],
        } for p in self._catalog(refresh)["packs"].values()]
        packs.sort(key=lambda p: (0 if p["mine"] else 1, p["name"].lower()))
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
                "downloaded": p["downloaded"], "hasIcon": bool(p["icon"]), "custom": p.get("custom", False),
                "builtin": p.get("builtin", False), "hotkeyPack": p.get("hotkeyPack", False),
                "mirrored": os.path.normpath(p["folder"]).startswith(os.path.normpath(MIRROR_DIR)),
                "actions": [{"uuid": a.get("uuid", ""), "name": a["name"], "tooltip": a["tooltip"],
                             "hasIcon": bool(a["icon"]),
                             "supported": a.get("supported", True)}
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

    # -- 删除图标 / 插件（进回收站，可恢复） ---------------------------------- #
    @staticmethod
    def _recycle(path: str) -> bool:
        """把文件或目录移进 Windows 回收站；成功返回 True。"""
        path = os.path.abspath(path or "")
        if not path or not os.path.exists(path):
            return False

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p),
                        ("wFunc", ctypes.c_uint),
                        ("pFrom", ctypes.c_wchar_p),
                        ("pTo", ctypes.c_wchar_p),
                        ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", ctypes.c_int),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", ctypes.c_wchar_p)]

        op = SHFILEOPSTRUCTW()
        op.wFunc = 3                                    # FO_DELETE
        op.pFrom = path + "\0"                          # 文件名列表以双 0 结尾
        op.fFlags = 0x40 | 0x10 | 0x4 | 0x400           # 允许撤销 / 不确认 / 静默 / 不弹错误框
        try:
            shell32 = ctypes.windll.shell32
            shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
            shell32.SHFileOperationW.restype = ctypes.c_int
            rc = shell32.SHFileOperationW(ctypes.byref(op))
        except Exception as exc:  # noqa: BLE001
            log(f"调回收站失败：{exc}")
            return False
        return rc == 0 and not op.fAnyOperationsAborted

    def delete_icon(self, pack_id: str, fname: str) -> tuple[bool, str]:
        """删掉图标包里的一个图标（pack_id = local 时删本地图标）。"""
        path = self.icon_pack_file(pack_id, fname)
        if not path:
            return False, "找不到这个图标"
        if not self._recycle(path):
            return False, f"删除失败（可能被占用）：{os.path.basename(path)}"
        self._catalog_cache = None
        log(f"已删除图标：{path}")
        return True, "已删除这个图标"

    def delete_icon_pack(self, pack_id: str) -> tuple[bool, str]:
        if pack_id == "local":
            return False, "本地图标是单个文件，请在图标上右键逐个删除"
        pack = self._catalog()["packs"].get(pack_id)
        if not pack:
            return False, "找不到这个图标包"
        folder = pack["folder"]
        if not self._recycle(folder):
            return False, f"删除失败（可能被占用）：{folder}"
        self._catalog_cache = None
        log(f"已删除图标包：{folder}")
        return True, f"已删除图标包「{pack['name']}」"

    def reveal_icon_pack(self, pack_id: str, fname: str = "") -> bool:
        """在资源管理器里打开图标包；给了文件名就选中那个图标。"""
        target = self.icon_pack_file(pack_id, fname) if fname else ""
        if not target:
            if pack_id == "local":
                target = ICON_DIR if os.path.isdir(ICON_DIR) else os.path.dirname(ICON_DIR)
            else:
                pack = self._catalog()["packs"].get(pack_id)
                if not pack:
                    return False
                target = pack["folder"]
        return self.reveal_in_explorer(target)

    # -- 裁图工具：一张大图裁成 15 张，正好对应 15 个屏幕键 ---------------- #
    def _crop_dir(self, token: str) -> str:
        return os.path.join(CROP_CACHE, "".join(c for c in (token or "") if c.isalnum())[:32])

    def _crop_canvas(self, src: str, fit: str, gap: int) -> tuple[list, list[int], bool]:
        """返回 (15 张图块（按 1..15 排列）、每帧时长、是否动图)。"""
        fit = fit if fit in ("cover", "contain", "fill") else "cover"
        g = max(0, min(64, int(gap)))
        w = GRID_COLS * KEY_SIZE + (GRID_COLS - 1) * g
        h = GRID_ROWS * KEY_SIZE + (GRID_ROWS - 1) * g
        tiles: list = [[] for _ in range(KEY_COUNT)]
        durations: list[int] = []
        with Image.open(src) as im:
            try:
                frames_total = int(getattr(im, "n_frames", 1) or 1)
            except Exception:  # noqa: BLE001
                frames_total = 1
            animated = frames_total > 1
            for i in range(frames_total if animated else 1):
                if animated:
                    im.seek(i)
                    durations.append(int(im.info.get("duration") or 100))
                frame = im.convert("RGBA")
                canvas = self._fit_canvas(frame, w, h, zoom_offset=False, fit=fit)
                for key in range(1, KEY_COUNT + 1):
                    col = (key - 1) % GRID_COLS
                    row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
                    box = (col * (KEY_SIZE + g), row_top * (KEY_SIZE + g),
                           col * (KEY_SIZE + g) + KEY_SIZE, row_top * (KEY_SIZE + g) + KEY_SIZE)
                    tiles[key - 1].append(canvas.crop(box).convert("RGB"))
        return tiles, durations, animated

    @staticmethod
    def _crop_visual_index(key: int) -> int:
        """键号 → 图标库里的文件名序号（按屏幕从上到下、从左到右数）。
        控制台第一行是 11–15、第二行 6–10、第三行 1–5，
        所以 11 号键存成 01，1 号键存成 11，图标库展开时才是屏幕上的样子。"""
        col = (key - 1) % GRID_COLS
        row_from_bottom = (key - 1) // GRID_COLS
        row_from_top = GRID_ROWS - 1 - row_from_bottom
        return row_from_top * GRID_COLS + col + 1

    def crop_preview(self, file: str, fit: str = "cover", gap: int = 27, name: str = "") -> dict:
        src = asset_path(file)
        if not src:
            return {"ok": False, "reason": "找不到这张图片，重新拖一次"}
        token = uuid.uuid4().hex
        out_dir = self._crop_dir(token)
        try:
            os.makedirs(out_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(out_dir, "src" + (os.path.splitext(src)[1].lower() or ".png")))
            tiles, durations, animated = self._crop_canvas(src, fit, gap)
            ext = ".gif" if animated else ".png"
            for idx, frames in enumerate(tiles, start=1):
                # 文件名按屏幕上看到的顺序补零：图标库按文件名排序时和 15 个键一一对应
                dst = os.path.join(out_dir, f"{self._crop_visual_index(idx):02d}{ext}")
                if animated:
                    frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:],
                                   duration=durations or 100, loop=0, optimize=False)
                else:
                    frames[0].save(dst, "PNG")
        except Exception as exc:  # noqa: BLE001
            log(f"裁图失败：{exc}")
            return {"ok": False, "reason": f"这张图片裁不了：{exc}"}
        shown = "".join(c for c in (name or "").strip() if c not in '\\/:*?"<>|').strip()
        name = (shown[:60] or os.path.splitext(os.path.basename(src))[0])
        return {"ok": True, "token": token, "name": name, "animated": animated,
                "frames": len(durations) if animated else 1,
                "fit": fit if fit in ("cover", "contain", "fill") else "cover",
                "gap": max(0, min(64, int(gap))),
                "src": f"/api/iconcrop/src?token={token}",
                "tiles": [f"/api/iconcrop/tile?token={token}&n={self._crop_visual_index(i):02d}{ext}"
                          for i in range(1, KEY_COUNT + 1)]}

    def crop_src(self, token: str) -> tuple[bytes, str] | None:
        folder = self._crop_dir(token)
        for path in sorted(glob.glob(os.path.join(folder, "src.*"))):
            ext = os.path.splitext(path)[1].lower()
            ctype = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp"}.get(
                         ext, "application/octet-stream")
            try:
                with open(path, "rb") as fh:
                    return fh.read(), ctype
            except OSError:
                return None
        return None

    def crop_tile(self, token: str, name: str) -> tuple[bytes, str] | None:
        folder = self._crop_dir(token)
        safe = os.path.basename(name)
        if not safe or not folder:
            return None
        path = os.path.join(folder, safe)
        if not os.path.isfile(path):
            return None
        ctype = "image/gif" if safe.lower().endswith(".gif") else "image/png"
        try:
            with open(path, "rb") as fh:
                return fh.read(), ctype
        except OSError:
            return None

    def crop_save(self, token: str, name: str = "") -> dict:
        folder = self._crop_dir(token)
        files = sorted(glob.glob(os.path.join(folder, "*"))) if folder else []
        if not files:
            return {"ok": False, "reason": "预览已经过期，重新裁一次"}
        pack_name = "".join(c for c in (name or "").strip() if c not in '\\/:*?"<>|').strip() or "我的图标"
        pack_name = pack_name[:40]
        os.makedirs(MY_ICON_DIR, exist_ok=True)
        base, n = pack_name, 2
        while os.path.exists(os.path.join(MY_ICON_DIR, f"{base}.sdIconPack")):
            base = f"{pack_name} {n}"
            n += 1
        pack_dir = os.path.join(MY_ICON_DIR, f"{base}.sdIconPack")
        icons = os.path.join(pack_dir, "icons")
        try:
            os.makedirs(icons, exist_ok=True)
            for path in files:
                fname = os.path.basename(path)
                if fname.startswith("src."):
                    continue   # 原图只是给编辑区左侧预览用的，不进图标包
                shutil.copy2(path, os.path.join(icons, fname))
            with open(os.path.join(pack_dir, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"Name": base, "Author": "我", "Version": "1.0.0",
                           "Description": "裁图工具生成的 15 键图标"}, fh, ensure_ascii=False, indent=2)
            with open(os.path.join(pack_dir, "zh_CN.json"), "w", encoding="utf-8") as fh:
                json.dump({"Name": base}, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存图标包失败：{exc}")
            return {"ok": False, "reason": f"保存失败：{exc}"}
        shutil.rmtree(folder, ignore_errors=True)
        self._catalog_cache = None
        log(f"已保存图标包：{base}（15 个图标）")
        info = official.icon_pack_info(pack_dir)
        return {"ok": True, "pack": {"id": info["id"], "name": info["name"], "mine": True,
                                     "count": len(info["files"]),
                                     "files": [os.path.basename(f) for f in info["files"]]}}

    def crop_cancel(self, token: str) -> bool:
        shutil.rmtree(self._crop_dir(token), ignore_errors=True)
        return True

    def _drop_plugin_bindings(self, plugin_id: str) -> int:
        """把绑在已删插件上的按键清空，返回清掉的个数。"""
        removed = 0
        for page in self.pages:
            acts = page.get("actions") or {}
            for k in list(acts):
                spec = acts.get(k)
                if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("plugin") == plugin_id:
                    del acts[k]
                    removed += 1
            gest = page.get("gestures") or {}
            for k in list(gest):
                per = gest.get(k)
                if not isinstance(per, dict):
                    continue
                for g in list(per):
                    spec = per.get(g)
                    if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("plugin") == plugin_id:
                        del per[g]
                        removed += 1
                if not per:
                    gest.pop(k, None)
        if removed:
            self._save_config()
        return removed

    def delete_plugin(self, plugin_id: str) -> tuple[bool, str]:
        """删除整个官方插件目录（进回收站）。"""
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return False, "找不到这个插件"
        folder, name = p["folder"], p["name"]
        if p.get("custom"):
            return False, "自制插件请到左侧「自制插件」里删除"
        if self.plugin_host:
            self.plugin_host.forget(plugin_id)
        if not self._recycle(folder):
            return False, f"删除失败（可能被插件占着）：{folder}"
        self._catalog_cache = None
        self._refresh_plugins()
        cleared = self._drop_plugin_bindings(plugin_id)
        if cleared:
            self.apply_keys()
        log(f"已删除插件：{name}（{folder}）" + (f"，清空 {cleared} 个按键绑定" if cleared else ""))
        return True, f"已删除插件「{name}」" + (f"，{cleared} 个按键已清空" if cleared else "")

    def delete_plugin_action(self, plugin_id: str, uuid: str) -> tuple[bool, str]:
        """从插件的 manifest.json 里删掉一个动作（原清单备份为 manifest.json.m18bak）。"""
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return False, "找不到这个插件"
        if p.get("builtin"):
            return False, "官方内置插件的动作是程序自带功能，删不掉"
        folder = p["folder"]
        mpath = os.path.join(folder, "manifest.json")
        try:
            with open(mpath, encoding="utf-8-sig") as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            return False, "读不到这个插件的 manifest.json"
        acts = [a for a in (man.get("Actions") or []) if isinstance(a, dict)]
        keep = [a for a in acts if str(a.get("UUID")) != uuid]
        if len(keep) == len(acts):
            return False, "找不到这个动作"
        gone = next((a for a in acts if str(a.get("UUID")) == uuid), {})
        aname = str(gone.get("Name") or uuid.rsplit(".", 1)[-1])
        try:
            shutil.copyfile(mpath, mpath + ".m18bak")
            man["Actions"] = keep
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            return False, f"写插件清单失败：{exc}"
        for lname in ("zh_CN.json", "zh-CN.json", "en.json", "en_US.json"):
            lpath = os.path.join(folder, lname)
            try:
                with open(lpath, encoding="utf-8-sig") as fh:
                    loc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(loc, dict) and uuid in loc:
                aname = str((loc.get(uuid) or {}).get("Name") or aname)
                loc.pop(uuid, None)
                try:
                    with open(lpath, "w", encoding="utf-8") as fh:
                        json.dump(loc, fh, ensure_ascii=False, indent=2)
                except OSError:
                    pass
        if self.plugin_host:
            self.plugin_host.reload(plugin_id)
        self._catalog_cache = None
        self._refresh_plugins()
        log(f"已删除插件动作：{plugin_id}/{uuid}（原清单备份 manifest.json.m18bak）")
        return True, f"已删除动作「{aname}」"

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
        elif name.startswith("lib:"):
            src = self.library_path(name[4:])
        elif name.startswith("plugin:"):
            plugin_id, _, idx = name[7:].rpartition("/")
            try:
                src = self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                src = None
        if name.startswith(("pack:", "plugin:", "lib:")):
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

    # -- 插件 ------------------------------------------------------------ #
    PLUGIN_GLOBAL = os.path.join(PLUGIN_DATA, "global-settings.json")

    def start_plugins(self) -> None:
        """HTTP 服务起来之后再启动插件宿主（网页插件要从本服务加载）。"""
        try:
            os.makedirs(PLUGIN_DATA, exist_ok=True)
            self.plugin_host = PluginHost(
                install_root=SD_ROOT, data_dir=PLUGIN_DATA, http_port_fn=lambda: PLUGIN_PORT, log=self._host_log,
                on_plugin_message=self._on_plugin_msg, on_pi_message=self._on_pi_msg,
                on_plugin_ready=self._on_plugin_ready, on_host_message=self._on_host_msg)
            plugins = {}
            for uid, p in self._catalog()["plugins"].items():
                man = official._plugin_manifest(p["folder"])
                if man:
                    plugins[uid] = {"folder": p["folder"], "manifest": man}
            self.plugin_host.register_plugins(plugins)
            self.plugin_host.fallback_node = os.path.join(MIRROR_DIR, "node", "node20.exe")
            serial = (self.device.info or {}).get("serial") or "M18"
            self.plugin_host.set_device(f"M18-{serial}")
        except Exception as exc:  # noqa: BLE001
            log(f"插件宿主启动失败：{exc}")
            self.plugin_host = None
            return
        self._plugins_live = True
        start_loop(self._plugin_flush_loop, "插件画面")
        self._plugin_sync()

    def _device_id(self) -> str:
        return self.plugin_host.device_info()["id"] if self.plugin_host else "M18"

    @staticmethod
    def _coords(key: int) -> dict:
        return {"column": (key - 1) % GRID_COLS, "row": (key - 1) // GRID_COLS}

    def _plugin_payload(self, key: int, spec: dict) -> dict:
        return {"settings": spec.get("settings") or {}, "coordinates": self._coords(key),
                "state": int(spec.get("state") or 0), "isInMultiAction": False, "controller": "Keypad"}

    def _plugin_event(self, event: str, key: int, spec: dict, payload: dict | None = None) -> None:
        if not self.plugin_host:
            return
        msg = {"event": event, "action": spec.get("action"), "context": spec.get("context"),
               "device": self._device_id(), "payload": payload or self._plugin_payload(key, spec)}
        self.plugin_host.send(spec.get("plugin"), msg)

    def _plugin_sync(self) -> None:
        """当前页上的插件动作 = 可见实例：新出现的发 willAppear，离开的发 willDisappear。"""
        if not self._plugins_live:
            return
        with self._plugin_lock:
            now = {}
            for key, spec in list(self.key_actions.items()):
                if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("context"):
                    now[spec["context"]] = (key, spec)
            for ctx, (key, spec) in list(self._plugin_visible.items()):
                if ctx not in now or now[ctx][0] != key:
                    self._plugin_event("willDisappear", key, spec)
                    self._plugin_visible.pop(ctx, None)
            for ctx, (key, spec) in now.items():
                if ctx not in self._plugin_visible:
                    self._plugin_visible[ctx] = (key, spec)
                    self._plugin_appear(key, spec)
                else:
                    self._plugin_visible[ctx] = (key, spec)

    def _title_params(self, spec: dict) -> dict:
        """标题样式：官方场景里存的 > 插件 manifest 里该状态的默认值 > 通用默认。"""
        tp = spec.get("titleParameters")
        if isinstance(tp, dict) and tp:
            src = tp
        else:
            states, _ = self._plugin_states(spec)
            st = int(spec.get("state") or 0)
            raw = states[st] if 0 <= st < len(states) and isinstance(states[st], dict) else {}
            src = {"fontFamily": raw.get("FontFamily"), "fontSize": raw.get("FontSize"),
                   "fontStyle": raw.get("FontStyle"), "fontUnderline": raw.get("FontUnderline"),
                   "showTitle": raw.get("ShowTitle"), "titleAlignment": raw.get("TitleAlignment"),
                   "titleColor": raw.get("TitleColor")}
        try:
            size = int(float(src.get("fontSize") or 10))
        except (TypeError, ValueError):
            size = 10
        return {
            "fontFamily": str(src.get("fontFamily") or ""),
            "fontSize": size,
            "fontStyle": str(src.get("fontStyle") or "Regular"),
            "fontUnderline": bool(src.get("fontUnderline")),
            "showTitle": src.get("showTitle") is not False,
            "titleAlignment": str(src.get("titleAlignment") or "bottom"),
            "titleColor": str(src.get("titleColor") or "#ffffff"),
        }

    def _plugin_appear(self, key: int, spec: dict) -> None:
        """willAppear 后紧跟 titleParametersDidChange——不少插件要拿到字体 / 颜色才开始画（官方也是这个顺序）。"""
        self._plugin_event("willAppear", key, spec)
        payload = dict(self._plugin_payload(key, spec))
        payload.update({"title": "", "titleParameters": self._title_params(spec)})
        self._plugin_event("titleParametersDidChange", key, spec, payload=payload)

    def _on_plugin_ready(self, uid: str) -> None:
        with self._plugin_lock:
            for ctx, (key, spec) in self._plugin_visible.items():
                if spec.get("plugin") == uid:
                    # 插件（重新）连上：清掉旧实例留下的画面 / 标题，免得热重载后残留
                    if self.plugin_visual.pop(ctx, None) is not None:
                        self._plugin_dirty[key] = time.time()
                    self._plugin_appear(key, spec)

    def _plugin_key(self, key: int, spec: dict, state: int) -> None:
        self._plugin_event("keyDown" if state == 1 else "keyUp", key, spec)
        if state != 0:
            return
        # 多状态动作：松开后宿主自动切到下一个状态（官方 / Elgato 的默认行为）
        states, auto = self._plugin_states(spec)
        if len(states) > 1 and auto:
            spec["state"] = (int(spec.get("state") or 0) + 1) % len(states)
            self._plugin_state_image(spec)
            self._save_config()

    def _plugin_manifest_action(self, spec: dict) -> tuple[str | None, dict]:
        p = self._catalog()["plugins"].get(spec.get("plugin") or "")
        if not p:
            return None, {}
        man = official._plugin_manifest(p["folder"]) or {}
        for act in man.get("Actions") or []:
            if isinstance(act, dict) and act.get("UUID") == spec.get("action"):
                return p["folder"], act
        return p["folder"], {}

    def _plugin_states(self, spec: dict) -> tuple[list, bool]:
        _, act = self._plugin_manifest_action(spec)
        return list(act.get("States") or []), not act.get("DisableAutomaticStates")

    def _plugin_state_image(self, spec: dict) -> None:
        folder, act = self._plugin_manifest_action(spec)
        states = act.get("States") or []
        st = int(spec.get("state") or 0)
        if not (0 <= st < len(states)):
            return
        path = official.find_file(folder, (states[st] or {}).get("Image"))
        img = official.read_image(path) if path else None
        if img:
            self._plugin_apply_raster(spec.get("context"), img[1])

    def _find_context(self, ctx: str):
        """在当前场景所有页里找这个实例。返回 (页号, 键号, 动作)。"""
        for pi, page in enumerate(self.pages):
            for key, spec in page.get("actions", {}).items():
                if isinstance(spec, dict) and spec.get("context") == ctx:
                    return pi, key, spec
        return None, None, None

    def _plugin_mark(self, ctx: str) -> None:
        hit = self._plugin_visible.get(ctx)
        if hit:
            self._plugin_dirty[hit[0]] = time.time()

    def _vis(self, ctx: str) -> dict:
        return self.plugin_visual.setdefault(ctx, {"rev": 0})

    def _plugin_apply_raster(self, ctx: str, raw: bytes) -> None:
        """插件发来的位图（PNG/JPG/GIF）→ 64×64 键面；多帧 GIF 交给 SDK 原生播放。"""
        try:
            im = Image.open(io.BytesIO(raw))
            frames = int(getattr(im, "n_frames", 1) or 1)
            first = ImageOps.fit(self._flat_rgb(im), (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
        except Exception as exc:  # noqa: BLE001
            log(f"插件图片解析失败：{exc}")
            return
        buf = io.BytesIO()
        first.save(buf, "JPEG", quality=90)
        vis = self._vis(ctx)
        vis["jpeg"] = buf.getvalue()
        vis["gif"] = None
        if frames > 1:
            os.makedirs(GIF_CACHE, exist_ok=True)
            path = os.path.join(GIF_CACHE, f"plg_{hashlib.sha1(raw).hexdigest()[:16]}.gif")
            if not os.path.isfile(path):
                with open(path, "wb") as fh:
                    fh.write(raw)
            vis["gif"] = path
        vis["rev"] += 1
        self._plugin_mark(ctx)

    def _plugin_set_image(self, uid: str, ctx: str, image) -> None:
        if not ctx:
            return
        if not image:
            vis = self._vis(ctx)
            vis["jpeg"] = vis["gif"] = None
            vis["rev"] += 1
            self._plugin_mark(ctx)
            return
        image = str(image)
        raw, is_svg = None, False
        if image.startswith("data:"):
            header, _, data = image.partition(",")
            is_svg = "svg" in header
            try:
                raw = base64.b64decode(data) if ";base64" in header else \
                    urllib.parse.unquote(data).encode("utf-8")
            except Exception:  # noqa: BLE001
                return
        else:
            folder = (self._catalog()["plugins"].get(uid) or {}).get("folder")
            path = official.find_file(folder, image)
            if path:
                with open(path, "rb") as fh:
                    raw = fh.read()
                is_svg = path.lower().endswith(".svg")
        if not raw:
            return
        if is_svg or raw.lstrip()[:5] in (b"<svg ", b"<?xml") or raw.lstrip().startswith(b"<svg"):
            # SVG 交给后台浏览器画成 PNG（Pillow 不会画 SVG）
            rid = os.urandom(6).hex()
            self._raster_pending[rid] = ctx
            if len(self._raster_pending) > 200:
                self._raster_pending.pop(next(iter(self._raster_pending)))
            svg_uri = "data:image/svg+xml;base64," + base64.b64encode(raw).decode()
            if self.plugin_host and not self.plugin_host.send_host(
                    {"event": "rasterize", "id": rid, "image": svg_uri, "size": 144}):
                self.plugin_host._ensure_browser()
            return
        self._plugin_apply_raster(ctx, raw)

    def _plog(self, plugin: str, message: str, level: str = "info") -> None:
        self._plog_seq += 1
        self.plugin_logs.append({"seq": self._plog_seq, "t": round(time.time(), 3), "plugin": plugin,
                                 "level": level, "message": str(message)[:2000]})

    def _host_log(self, message: str) -> None:
        log(message)
        self._plog("host", message, "error" if any(w in message for w in ("失败", "退出", "不支持")) else "info")

    def plugin_log_items(self, plugin: str = "", since: int = 0, limit: int = 200) -> list:
        items = [x for x in list(self.plugin_logs) if x["seq"] > since and (not plugin or x["plugin"] == plugin
                                                         or (x["plugin"] == "host" and plugin in x["message"]))]
        return items[-max(1, min(1000, limit)):]

    def _on_host_msg(self, msg: dict) -> None:
        if msg.get("event") == "rasterized":
            ctx = self._raster_pending.pop(str(msg.get("id")), None)
            data = str(msg.get("image") or "")
            if ctx and "," in data:
                try:
                    self._plugin_apply_raster(ctx, base64.b64decode(data.split(",", 1)[1]))
                except Exception:  # noqa: BLE001
                    pass
        elif msg.get("event") == "log":
            log(f"[插件浏览器] {msg.get('message')}")
            text = str(msg.get("message") or "")
            uid = text.split("：", 1)[0] if "：" in text else "host"
            self._plog(uid, text, "error" if "出错" in text or "没有" in text else "info")

    def _plugin_global(self) -> dict:
        try:
            with open(self.PLUGIN_GLOBAL, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _set_plugin_global(self, uid: str, settings) -> None:
        data = self._plugin_global()
        data[uid] = settings if isinstance(settings, dict) else {}
        try:
            os.makedirs(PLUGIN_DATA, exist_ok=True)
            with open(self.PLUGIN_GLOBAL, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            log(f"保存插件全局设置失败：{exc}")

    def _did_receive_settings(self, key: int, spec: dict) -> dict:
        return {"event": "didReceiveSettings", "action": spec.get("action"), "context": spec.get("context"),
                "device": self._device_id(), "payload": self._plugin_payload(key, spec)}

    def _common_plugin_msg(self, uid: str, ev: str, msg: dict, reply) -> bool:
        """插件和设置页共有的消息。reply(obj) 回给发送方。"""
        payload = msg.get("payload") or {}
        if ev == "openUrl":
            url = str(payload.get("url") or "")
            if url.lower().startswith(("http://", "https://")):
                try:
                    os.startfile(url)
                except OSError:
                    pass
            else:
                log(f"插件 {uid} 试图打开非网页地址，已忽略：{url[:80]}")
            return True
        if ev == "logMessage":
            message = str(payload.get("message"))
            log(f"[插件 {uid}] {message[:300]}")
            self._plog(uid, message, "error" if any(w in message for w in ("错误", "出错", "Error", "失败")) else "info")
            return True
        if ev == "getGlobalSettings":
            reply({"event": "didReceiveGlobalSettings",
                   "payload": {"settings": self._plugin_global().get(uid) or {}}})
            return True
        if ev == "setGlobalSettings":
            self._set_plugin_global(uid, payload)
            return True
        return False

    def _on_plugin_msg(self, uid: str, msg: dict) -> None:
        ev = msg.get("event")
        ctx = msg.get("context") or ""
        payload = msg.get("payload") or {}
        host = self.plugin_host
        if uid.startswith(CUSTOM_PREFIX):
            if ev == "logMessage":
                self.broadcast({"type": "pluginlog", "plugin": uid,
                                "message": str(payload.get("message"))[:2000], "t": time.time()})
            elif ev and ev.startswith("m18."):
                self._custom_call(uid, ev, payload)
                return
        if self._common_plugin_msg(uid, ev, msg, lambda m: host.send(uid, m)):
            if ev == "setGlobalSettings":
                # 设置页可能开着：同步给它
                for c, (k, sp) in list(self._plugin_visible.items()):
                    if sp.get("plugin") == uid:
                        host.send_pi(c, {"event": "didReceiveGlobalSettings", "payload": {"settings": payload}})
            return
        if ev == "setImage":
            self._plugin_set_image(uid, ctx, payload.get("image"))
        elif ev == "setTitle":
            vis = self._vis(ctx)
            title = payload.get("title")
            vis["title"] = None if title is None else str(title)
            vis["rev"] += 1
            self._plugin_mark(ctx)
        elif ev == "setState":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                spec["state"] = int(payload.get("state") or 0)
                self._plugin_state_image(spec)
        elif ev in ("showOk", "showAlert"):
            vis = self._vis(ctx)
            vis["flash"] = ("ok" if ev == "showOk" else "alert", time.time() + 1.2)
            self._plugin_mark(ctx)
            threading.Timer(1.3, self._plugin_mark, args=(ctx,)).start()
        elif ev == "setSettings":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                spec["settings"] = payload if isinstance(payload, dict) else {}
                self._save_config()
                host.send_pi(ctx, self._did_receive_settings(key, spec))
        elif ev == "getSettings":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                host.send(uid, self._did_receive_settings(key, spec))
        elif ev == "sendToPropertyInspector":
            host.send_pi(ctx, {"event": "sendToPropertyInspector", "action": msg.get("action"),
                               "context": ctx, "payload": payload})

    def _on_pi_msg(self, ctx: str, msg: dict) -> None:
        ev = msg.get("event")
        payload = msg.get("payload") or {}
        _, key, spec = self._find_context(ctx)
        if spec is None:
            return
        uid = spec.get("plugin")
        host = self.plugin_host
        if self._common_plugin_msg(uid, ev, msg, lambda m: host.send_pi(ctx, m)):
            if ev == "setGlobalSettings":
                host.send(uid, {"event": "didReceiveGlobalSettings", "payload": {"settings": payload}})
            return
        if ev == "setSettings":
            spec["settings"] = payload if isinstance(payload, dict) else {}
            self._save_config()
            host.send(uid, self._did_receive_settings(key, spec))
        elif ev == "getSettings":
            host.send_pi(ctx, self._did_receive_settings(key, spec))
        elif ev == "sendToPlugin":
            host.send(uid, {"event": "sendToPlugin", "action": spec.get("action"),
                            "context": ctx, "payload": payload})

    def _plugin_flush_loop(self) -> None:
        """插件画面推到设备：每键最快 10 帧/秒、每 50ms 最多推 2 个键，界面预览 2 次/秒。"""
        last_dev: dict[int, float] = {}
        last_ui: dict[int, float] = {}
        ui_due: set[int] = set()          # 界面预览被节流掉、需要补发的键
        while True:
            time.sleep(0.05)
            now = time.time()
            for key in list(ui_due):
                if now - last_ui.get(key, 0) >= 0.5:
                    ui_due.discard(key)
                    last_ui[key] = now
                    vis = self._plugin_vis(key) or {}
                    self.broadcast({"type": "keyvisual", "key": key, "rev": vis.get("rev", 0)})
            if not self._plugin_dirty:
                continue
            dirty = dict(self._plugin_dirty)
            ready = sorted(dirty, key=dirty.get)
            can_dev = (self.device.connected and not (self.in_standby or self.writes_paused or self.quiet
                                                      or self.lighting_only or self.yielded or self.screen_off))
            pushed = 0
            for key in ready:
                if now - last_dev.get(key, 0) < 0.1:
                    continue
                self._plugin_dirty.pop(key, None)
                last_dev[key] = now
                if can_dev and 1 <= key <= KEY_COUNT:
                    self._gif_update_key(key)
                    if key not in self._gif_active:
                        self.device.send_image(key, self._render_key(key))
                    pushed += 1
                if self.clients:
                    ui_due.add(key)
                if pushed >= 2:
                    break
            if pushed:
                self.device.refresh()

    # -- 自制插件 ---------------------------------------------------------- #
    def _custom_call(self, uid: str, ev: str, payload: dict) -> None:
        """自制插件专属：发热键 / 打开程序 / 输入文字（第三方插件没有这些权限）。"""
        try:
            if ev == "m18.hotkey":
                ok = send_hotkey(str(payload.get("keys") or ""))
            elif ev == "m18.launch":
                path = str(payload.get("path") or "")
                ok = bool(path) and (os.path.exists(path) or path.lower().startswith(("http://", "https://")))
                if ok:
                    os.startfile(path)
            elif ev == "m18.text":
                ok = type_text(str(payload.get("text") or ""), bool(payload.get("enter")))
            elif ev == "m18.media":
                vk = MEDIA_LABELS.get(str(payload.get("action") or ""), ("", ""))[0]
                ok = bool(vk) and send_hotkey(vk)
            elif ev == "m18.http":
                threading.Thread(target=self._custom_http, args=(uid, payload), daemon=True).start()
                return
            else:
                ok = False
            log(f"[自制插件 {uid[len(CUSTOM_PREFIX):]}] {ev} {'✓' if ok else '✗'}")
        except Exception as exc:  # noqa: BLE001
            log(f"[自制插件 {uid}] {ev} 失败：{exc}")

    def _custom_http(self, uid: str, payload: dict) -> None:
        """M18.http：由控制台代发网络请求（插件页在 127.0.0.1 下，直接 fetch 常被跨域拦截）。"""
        rid = payload.get("id")
        url = str(payload.get("url") or "")
        result = {"id": rid, "status": 0, "body": "", "headers": {}, "error": ""}
        target = urllib.parse.urlparse(url)
        local_ports = {PORT, PLUGIN_PORT, getattr(getattr(self.plugin_host, "server", None), "port", 0)}
        if not url.lower().startswith(("http://", "https://")):
            result["error"] = "只支持 http / https 地址"
        elif (target.hostname or "").lower() in ("127.0.0.1", "localhost", "::1", "0.0.0.0") and \
                (target.port or (443 if target.scheme == "https" else 80)) in local_ports:
            result["error"] = "不允许请求控制台自己的接口"
        else:
            body = payload.get("body")
            data = body.encode("utf-8") if isinstance(body, str) else None
            headers = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
            headers.setdefault("User-Agent", "M18Console/1.0")
            req = urllib.request.Request(url, data=data, method=str(payload.get("method") or "GET").upper(),
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read(2 * 1024 * 1024)
                    result.update(status=resp.status, headers=dict(resp.headers.items()))
            except urllib.error.HTTPError as exc:
                raw = exc.read(2 * 1024 * 1024)
                result.update(status=exc.code, headers=dict(exc.headers.items()))
            except Exception as exc:  # noqa: BLE001
                raw = b""
                result["error"] = str(exc)
            charset = "utf-8"
            ctype = result["headers"].get("Content-Type", "")
            if "charset=" in ctype:
                charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
            try:
                result["body"] = raw.decode(charset, "replace") if raw else ""
            except LookupError:
                result["body"] = raw.decode("utf-8", "replace")
        self._plog(uid, f"M18.http {url[:120]} → {result['status'] or result['error']}",
                   "error" if result["error"] or result["status"] >= 400 else "info")
        if self.plugin_host:
            self.plugin_host.send(uid, {"event": "m18.httpResult", "payload": result})

    CUSTOM_RUNTIME = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title></head><body>
<script>
/* M18 自制插件运行时：把官方插件协议包装成几个简单的函数（由控制台生成，请在控制台里编辑） */
(() => {
  const OFFSET = __OFFSET__;
  // 错误信息里的行号换算成「你写的代码」的行号
  const fmt = (e) => String((e && e.stack) || e).split("\n").slice(0, 3).join("\n")
    .replace(/https?:\/\/[^\s)]*index\.html:(\d+):(\d+)/g, (m, l) => (l > OFFSET ? `代码第 ${l - OFFSET} 行` : "运行时"));
  let ws = null;
  const keys = new Map(), handlers = {}, timers = new Map(), pending = new Map();
  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const M18 = {
    on(ev, fn) { handlers[ev] = fn; },
    log(...a) { send({ event: "logMessage", payload: { message: a.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(" ") } }); },
    openUrl(url) { send({ event: "openUrl", payload: { url } }); },
    hotkey(keysText) { send({ event: "m18.hotkey", payload: { keys: keysText } }); },
    launch(path) { send({ event: "m18.launch", payload: { path } }); },
    type(text, enter = false) { send({ event: "m18.text", payload: { text, enter } }); },
    media(action) { send({ event: "m18.media", payload: { action } }); },
    // 由控制台代发请求：不受浏览器跨域限制。返回 { ok, status, text, json(), headers }
    http(url, opts = {}) {
      return new Promise((resolve, reject) => {
        const id = Math.random().toString(36).slice(2) + Date.now();
        const body = opts.body == null ? null : (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body));
        pending.set(id, { resolve, reject });
        send({ event: "m18.http", payload: { id, url, method: opts.method || "GET", headers: opts.headers || {}, body } });
        setTimeout(() => { if (pending.delete(id)) reject(new Error("请求超时")); }, 20000);
      });
    },
    sleep(ms) { return new Promise((r) => setTimeout(r, ms)); },
    keys() { return [...keys.values()]; },
  };
  const call = (ev, k, extra) => {
    const fn = handlers[ev];
    if (!fn) return;
    try {
      const r = fn(k, extra);
      if (r && r.catch) r.catch((e) => M18.log(`${ev} 出错：${fmt(e)}`));
    } catch (e) { M18.log(`${ev} 出错：${fmt(e)}`); }
  };
  const keyOf = (msg) => {
    const ctx = msg.context;
    let k = keys.get(ctx);
    if (!k) {
      k = {
        context: ctx, settings: {}, state: 0, coordinates: {},
        setTitle: (t) => send({ event: "setTitle", context: ctx, payload: { title: t == null ? "" : String(t), target: 0 } }),
        setImage: (img) => send({ event: "setImage", context: ctx, payload: { image: img || "", target: 0 } }),
        setState: (s) => send({ event: "setState", context: ctx, payload: { state: s } }),
        showOk: () => send({ event: "showOk", context: ctx }),
        showAlert: () => send({ event: "showAlert", context: ctx }),
        save: (obj) => { Object.assign(k.settings, obj || {}); send({ event: "setSettings", context: ctx, payload: k.settings }); },
        draw: (fn, size = 144) => {
          const c = document.createElement("canvas");
          c.width = c.height = size;
          const g = c.getContext("2d");
          try { fn(g, size); } catch (e) { M18.log(`draw 出错：${fmt(e)}`); return; }
          k.setImage(c.toDataURL("image/png"));
        },
        every: (ms, fn) => {
          const id = setInterval(() => { try { fn(k); } catch (e) { M18.log(`every 出错：${fmt(e)}`); } }, Math.max(100, ms));
          if (!timers.has(ctx)) timers.set(ctx, []);
          timers.get(ctx).push(id);
          return id;
        },
      };
      keys.set(ctx, k);
    }
    const p = msg.payload || {};
    if (p.settings) k.settings = p.settings;
    if (p.coordinates) k.coordinates = p.coordinates;
    if (typeof p.state === "number") k.state = p.state;
    return k;
  };
  window.M18 = M18;
  window.connectElgatoStreamDeckSocket = (port, uuid, registerEvent) => {
    ws = new WebSocket("ws://127.0.0.1:" + port);
    ws.onopen = () => { send({ event: registerEvent, uuid }); call("ready"); };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.event === "m18.httpResult") {
        const p = msg.payload || {}, cb = pending.get(p.id);
        if (!cb) return;
        pending.delete(p.id);
        if (p.error) cb.reject(new Error(p.error));
        else cb.resolve({ ok: p.status >= 200 && p.status < 300, status: p.status, headers: p.headers || {},
          text: p.body || "", json: () => JSON.parse(p.body || "null") });
        return;
      }
      if (!msg.context) { if (msg.event === "didReceiveGlobalSettings") call(msg.event, null, msg.payload); return; }
      const k = keyOf(msg);
      if (msg.event === "willDisappear") {
        (timers.get(msg.context) || []).forEach(clearInterval);
        timers.delete(msg.context);
        call("willDisappear", k);
        keys.delete(msg.context);
        return;
      }
      call(msg.event, k, msg.payload);
    };
  };
  window.addEventListener("error", (e) => {
    const line = e.lineno > OFFSET ? `（代码第 ${e.lineno - OFFSET} 行）` : "";
    M18.log(`脚本错误：${e.message}${line}`);
  });
  window.addEventListener("unhandledrejection", (e) => M18.log(`未处理的异步错误：${e.reason}`));
})();
</script>
<script>
__CODE__
</script>
</body></html>
"""

    @staticmethod
    def _valid_custom_id(pid: str) -> bool:
        return bool(re.fullmatch(re.escape(CUSTOM_PREFIX) + r"[0-9a-zA-Z_-]{1,40}", pid or ""))

    def _custom_folder(self, pid: str) -> str:
        if not self._valid_custom_id(pid):
            raise ValueError(f"非法的自制插件 id：{pid!r}")
        return os.path.join(CUSTOM_PLUGIN_DIR, f"{pid}.sdPlugin")

    def list_custom_plugins(self) -> list:
        out = []
        for folder in sorted(glob.glob(os.path.join(CUSTOM_PLUGIN_DIR, "*.sdPlugin"))):
            try:
                with open(os.path.join(folder, "m18.json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            meta["id"] = os.path.basename(folder)[:-len(".sdPlugin")]
            meta["hasIcon"] = os.path.isfile(os.path.join(folder, "icon.png"))
            meta["mtime"] = os.path.getmtime(os.path.join(folder, "m18.json"))
            out.append(meta)
        out.sort(key=lambda m: m.get("name", "").lower())
        return out

    @staticmethod
    def _default_icon(name: str) -> bytes:
        """没选图标时生成一个：彩色圆角方块 + 名称首字。"""
        hue = (int(hashlib.md5(name.encode("utf-8")).hexdigest()[:4], 16) % 360) / 360.0
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.78))
        img = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 139, 139], radius=28, fill=(r, g, b, 255))
        ch = (name.strip() or "?")[0]
        font = None
        for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\segoeuib.ttf"):
            try:
                font = ImageFont.truetype(path, 76)
                break
            except Exception:  # noqa: BLE001
                continue
        font = font or ImageFont.load_default()
        w = d.textlength(ch, font=font)
        d.text(((144 - w) / 2, 22), ch, font=font, fill=(255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _custom_icon_bytes(self, icon: str, name: str) -> bytes | None:
        """icon：pack:… / plugin:… / data:image/…；返回 144×144 PNG。"""
        raw = None
        if icon.startswith("pack:"):
            pack_id, _, fname = icon[5:].partition("/")
            path = self.icon_pack_file(pack_id, fname)
            img = official.read_image(path) if path else None
            raw = img[1] if img else None
        elif icon.startswith("plugin:"):
            plugin_id, _, idx = icon[7:].rpartition("/")
            try:
                path = self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                path = None
            img = official.read_image(path) if path else None
            raw = img[1] if img else None
        elif icon.startswith("data:image/") and "," in icon:
            try:
                raw = base64.b64decode(icon.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                raw = None
        if not raw:
            return None
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            im = ImageOps.fit(im, (144, 144), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None

    def save_custom_plugin(self, data: dict) -> dict:
        name = str(data.get("name") or "").strip()[:30]
        action_name = str(data.get("actionName") or name).strip()[:30] or name
        code = str(data.get("code") or "")
        if not name:
            return {"ok": False, "error": "插件名称不能为空"}
        pid = str(data.get("id") or "").strip()
        if not pid.startswith(CUSTOM_PREFIX) or not os.path.isdir(self._custom_folder(pid)):
            slug = "".join(ch for ch in hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:10])
            pid = f"{CUSTOM_PREFIX}{slug}"
        folder = self._custom_folder(pid)
        os.makedirs(folder, exist_ok=True)
        icon = str(data.get("icon") or "")
        icon_png = self._custom_icon_bytes(icon, name) if icon else None
        icon_path = os.path.join(folder, "icon.png")
        if icon_png or not os.path.isfile(icon_path):
            with open(icon_path, "wb") as fh:
                fh.write(icon_png or self._default_icon(name))
        manifest = {
            "Name": name, "UUID": pid, "Version": "1.0", "Author": "M18 控制台（自制）",
            "Description": str(data.get("description") or "")[:200],
            "CodePath": "index.html", "Icon": "icon", "Category": "自制插件", "CategoryIcon": "icon",
            "SDKVersion": 2, "OS": [{"Platform": "windows", "MinimumVersion": "10"}],
            "Software": {"MinimumVersion": "2.9"},
            "Actions": [{"UUID": f"{pid}.action", "Name": action_name, "Icon": "icon",
                         "States": [{"Image": "icon", "TitleAlignment": "middle"}],
                         "Tooltip": str(data.get("description") or name)[:100]}],
        }
        head = self.CUSTOM_RUNTIME.split("__CODE__")[0]
        offset = head.count("\n")
        html = (self.CUSTOM_RUNTIME.replace("__TITLE__", name.replace("<", ""))
                .replace("__OFFSET__", str(offset))
                .replace("__CODE__", code.replace("</script", "<\\/script")))
        meta = {"name": name, "actionName": action_name, "description": manifest["Description"],
                "template": str(data.get("template") or ""), "code": code}
        for fname, content in (("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)),
                               ("index.html", html),
                               ("m18.json", json.dumps(meta, ensure_ascii=False, indent=2))):
            with open(os.path.join(folder, fname), "w", encoding="utf-8") as fh:
                fh.write(content)
        self._refresh_plugins()
        if self.plugin_host:
            self.plugin_host.reload(pid)
        # 已经绑在按键上的：按键名字跟着改
        for page in self.pages:
            for spec in (page.get("actions") or {}).values():
                if isinstance(spec, dict) and spec.get("plugin") == pid:
                    spec["name"] = action_name
        log(f"自制插件已保存：{name}（{pid}）")
        return {"ok": True, "id": pid}

    def delete_custom_plugin(self, pid: str) -> bool:
        if not self._valid_custom_id(pid):
            return False
        folder = self._custom_folder(pid)
        if not os.path.isdir(folder):
            return False
        if self.plugin_host:
            self.plugin_host.forget(pid)
        shutil.rmtree(folder, ignore_errors=True)
        self._refresh_plugins()
        log(f"自制插件已删除：{pid}")
        return True

    def _refresh_plugins(self) -> None:
        self._catalog(refresh=True)
        if self.plugin_host:
            plugins = {}
            for uid, p in self._catalog()["plugins"].items():
                man = official._plugin_manifest(p["folder"])
                if man:
                    plugins[uid] = {"folder": p["folder"], "manifest": man}
            self.plugin_host.register_plugins(plugins)

    def _native_for(self, plugin_id: str, action_uuid: str) -> dict | None:
        """官方内置 / 热键包的动作 → 控制台原生动作；不能转换返回 None。"""
        native = official.builtin_native(action_uuid)
        if native:
            return native
        p = self._catalog()["plugins"].get(plugin_id or "")
        man = official._plugin_manifest(p["folder"]) if p else None
        if man and official.is_hotkey_pack(man):
            for a in man.get("Actions") or []:
                if isinstance(a, dict) and a.get("UUID") == action_uuid:
                    keys = official.hotkey_pack_keys(a, self._vk_reverse())
                    if keys and keys != "?":
                        return {"type": "hotkey", "keys": keys}
        return None

    def _convert_builtin_plugins(self) -> int:
        """以前绑定成「插件」的官方内置动作（创建文件夹、打开、热键…）跑不起来：换成原生动作。"""
        changed = 0
        for page in self.pages:
            for key, spec in list((page.get("actions") or {}).items()):
                if isinstance(spec, dict) and spec.get("type") == "plugin":
                    native = self._native_for(str(spec.get("plugin") or ""), str(spec.get("action") or ""))
                    if native:
                        page["actions"][key] = native
                        changed += 1
        if changed:
            log(f"已把 {changed} 个官方内置插件动作换成控制台原生动作")
        return changed

    # -- 给 AI 智能体用的测试接口 ------------------------------------------- #
    def simulate_press(self, key: int, gesture: str = "press") -> bool:
        """模拟按一下设备上的键（短按 / 长按 / 双击），走和真实按键完全相同的处理流程。"""
        if not (1 <= key <= KEY_COUNT + 3) or gesture not in ("press", "long", "double"):
            return False
        if self.in_standby and not self.album_active():
            self._wake_from_standby()          # 模拟按键不需要「第一下只唤醒」（相册里按键由相册自己处理）
        hold = 0.8 if gesture == "long" else 0.06

        def run():
            for i in range(2 if gesture == "double" else 1):
                self._on_input(key, 1)
                time.sleep(hold)
                self._on_input(key, 0)
                time.sleep(0.08)
        threading.Thread(target=run, daemon=True).start()
        log(f"模拟按键 {key}（{ {'press': '短按', 'long': '长按', 'double': '双击'}[gesture] }）")
        return True

    def keys_sheet_png(self, scale: int = 2) -> bytes:
        """当前页 15 个键的画面拼成一张图（和设备上看到的一致，左下角是 1 号键）。"""
        size, gap = KEY_SIZE * scale, 10
        sheet = Image.new("RGB", (GRID_COLS * size + (GRID_COLS + 1) * gap,
                                  GRID_ROWS * size + (GRID_ROWS + 1) * gap), (24, 28, 36))
        d = ImageDraw.Draw(sheet)
        album = self._canvas_tiles(self._album_canvas(self.album_index)) if self.album_active() else None
        for key in range(1, KEY_COUNT + 1):
            col, row_top = (key - 1) % GRID_COLS, GRID_ROWS - 1 - (key - 1) // GRID_COLS
            x, y = gap + col * (size + gap), gap + row_top * (size + gap)
            try:
                tile = album[key].resize((size, size)) if album else                     Image.open(io.BytesIO(self._number_tile(key))).convert("RGB").resize((size, size))
                sheet.paste(tile, (x, y))
            except Exception:  # noqa: BLE001
                d.rectangle([x, y, x + size, y + size], outline=(200, 60, 60))
            d.rectangle([x, y, x + 15, y + 12], fill=(0, 0, 0))
            d.text((x + 2, y), str(key), fill=(255, 220, 90))
        buf = io.BytesIO()
        sheet.save(buf, "PNG")
        return buf.getvalue()

    def agent_config(self) -> dict:
        """接入 AI 智能体（Codex / DeepSeek Harness / Claude / Cursor…）的 MCP 配置。"""
        if FROZEN:
            cmd, args = os.path.join(APP_DIR, "mcp", "m18-mcp.exe"), []
        else:
            py = sys.executable
            if py.lower().endswith("pythonw.exe") and os.path.isfile(py[:-5] + ".exe"):
                py = py[:-5] + ".exe"      # MCP 走标准输入输出，要用带控制台的 python.exe
            cmd, args = py, [os.path.join(HERE, "mcp_server.py")]
        toml_args = ", ".join(json.dumps(a) for a in args)
        guide = os.path.join(APP_DIR if FROZEN else ROOT, "AGENTS.md")
        return {
            "ok": True, "command": cmd, "args": args, "url": f"http://127.0.0.1:{PORT}/",
            "mcpJson": json.dumps({"mcpServers": {"m18-console": {"command": cmd, "args": args}}},
                                  ensure_ascii=False, indent=2),
            "codexToml": f"[mcp_servers.m18-console]\ncommand = {json.dumps(cmd)}\nargs = [{toml_args}]\n"
                         "startup_timeout_sec = 30\ntool_timeout_sec = 120\n",
            "guide": guide if os.path.isfile(guide) else "",
            "dataDir": DATA_DIR, "customPluginDir": CUSTOM_PLUGIN_DIR,
        }

    def bind_plugin_action(self, key: int, plugin_id: str, idx: int) -> dict:
        if not (1 <= key <= KEY_COUNT + 3):
            return {"ok": False}
        p = self._catalog()["plugins"].get(plugin_id)
        if not p or not (0 <= idx < len(p["actions"])):
            return {"ok": False}
        act = p["actions"][idx]
        native = None
        if p.get("builtin") or p.get("hotkeyPack"):
            # 官方内置插件 / 热键包不能独立运行：换成控制台自己的动作
            native = self._native_for(plugin_id, act["uuid"])
            if native is None:
                return {"ok": False, "error": f"「{act['name']}」是官方程序内置的功能，控制台暂不支持"}
            self.key_actions[key] = native
        else:
            self.key_actions[key] = {
                "type": "plugin", "plugin": plugin_id, "action": act["uuid"], "name": act["name"],
                "settings": {}, "context": os.urandom(16).hex(), "state": 0,
            }
        # 换成插件动作：原来的标题文字不再适用（插件自己画 / 设标题），保留配色
        style = self.key_styles.get(key)
        if style and style.pop("text", None) is not None and not any(style.values()):
            self.key_styles.pop(key, None)
        if act.get("icon") and key <= KEY_COUNT:
            img = official.read_image(act["icon"])
            if img:
                stored = self._store_scene_image(img, act["icon"])
                data = self._load_image_source(os.path.join(UPLOAD_DIR, stored))
                if data:
                    self.key_overrides[key] = data
        self._save_config()
        if self.device.connected and key <= KEY_COUNT:
            self._apply_key(key)
            self.device.refresh()
        if native is not None:
            field = official.NEEDS_INPUT.get(native["type"], "x")
            needs = bool(field) and not native.get(field)
            log(f"按键 {key} 绑定官方内置动作 {act['name']} → 原生动作「{action_label(native)}」")
            return {"ok": True, "native": True, "label": action_label(native), "needsInput": needs}
        status = (self.plugin_host.status().get(plugin_id) if self.plugin_host else "unavailable")
        log(f"按键 {key} 绑定插件动作 {act['name']}（{plugin_id}，{status}）")
        return {"ok": True}

    def plugin_pi(self, key: int) -> dict:
        """打开插件设置页需要的参数（界面用 iframe 加载）。"""
        spec = self.key_actions.get(key)
        if not (isinstance(spec, dict) and spec.get("type") == "plugin") or not self.plugin_host:
            return {"ok": False}
        folder, act = self._plugin_manifest_action(spec)
        man = official._plugin_manifest(folder) if folder else {}
        pi_path = act.get("PropertyInspectorPath") or (man or {}).get("PropertyInspectorPath")
        if not pi_path:
            return {"ok": False, "error": "这个插件动作没有设置页"}
        uid = spec["plugin"]
        self.plugin_host.ensure_running(uid)
        action_info = {"action": spec.get("action"), "context": spec.get("context"),
                       "device": self._device_id(), "payload": self._plugin_payload(key, spec)}
        self._plugin_event("propertyInspectorDidAppear", key, spec, payload={})
        return {"ok": True, "url": f"/plugins/{uid}/{pi_path.replace(os.sep, '/')}",
                "frame": f"http://127.0.0.1:{PLUGIN_PORT}/pi-frame.html",
                "port": self.plugin_host.server.port, "uuid": spec.get("context"),
                "info": self.plugin_host.info_json(uid), "actionInfo": json.dumps(action_info, ensure_ascii=False),
                "name": spec.get("name") or ""}

    def plugin_pi_closed(self, key: int) -> None:
        spec = self.key_actions.get(key)
        if isinstance(spec, dict) and spec.get("type") == "plugin":
            self._plugin_event("propertyInspectorDidDisappear", key, spec, payload={})

    def plugin_file(self, uid: str, rel: str) -> str | None:
        p = self._catalog()["plugins"].get(uid)
        if not p:
            return None
        root = os.path.normpath(p["folder"])
        path = os.path.normpath(os.path.join(root, urllib.parse.unquote(rel)))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return None
        return path

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
    def _save_soon(self, delay: float = 1.2) -> None:
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(delay, self._save_config)
        self._save_timer.daemon = True
        self._save_timer.start()

    def reset_lighting(self) -> None:
        with self.lock:
            self.lighting.update(DEFAULT_LIGHTING)
            self._dirty = True
        self.effect_reverse = False
        self.device.set_led_brightness(self.lighting["brightness"])
        self._save_config()

    def add_ripple(self, key: int) -> None:
        if self.lighting.get("mode") != "ripple" or key not in KEY_POINTS:
            return
        now = time.time()
        self._ripples = [r for r in self._ripples if now - r[1] < 3.0][-6:] + [(key, now)]

    def set_lighting(self, patch: dict) -> None:
        self._save_soon()
        clean = {}
        try:
            if patch.get("mode") in MODES:
                clean["mode"] = patch["mode"]
            color = str(patch.get("color") or "")
            if re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
                clean["color"] = color if color.startswith("#") else "#" + color
            if patch.get("speed") is not None:
                clean["speed"] = max(0.1, min(10.0, float(patch["speed"])))
            if patch.get("brightness") is not None:
                # 设备端 LED 亮度是 0–100 的百分比，超过 100 固件会走到奇怪的分支
                clean["brightness"] = max(0, min(100, int(float(patch["brightness"]))))
        except (TypeError, ValueError):
            log(f"灯光参数不合法，已忽略：{patch}")
        with self.lock:
            self.lighting.update(clean)
            self._dirty = True
            snap = {"mode": self.lighting["mode"], "color": self.lighting["color"],
                    "speed": self.lighting["speed"], "brightness": self.lighting["brightness"]}
        self.device.set_led_brightness(self.lighting["brightness"])
        # 按键里的「控制台 → 灯光效果切换」改了模式，界面上的灯效预设也要跟着高亮
        self.broadcast({"type": "lighting", "lighting": snap})

    def set_custom(self, colors) -> None:
        self._save_soon()
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
        if mode == "ripple":
            return self._ripple_frame(cfg, time.time())
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

    def _ripple_frame(self, cfg: dict, now: float):
        """涟漪：从按下的键向四周扩散的光环，按灯珠到按键的真实距离点亮；没按键时整圈微亮。"""
        base = hex_to_rgb(cfg["color"])
        speed = 700.0 * max(0.2, float(cfg["speed"]))          # 每秒扩散多远（坐标单位）
        width = 95.0
        out = []
        ripples = list(self._ripples)
        for (x, y) in LED_POINTS:
            k = 0.0                       # 平时全灭，只有按键时才亮
            for key, t0 in ripples:
                kx, ky = KEY_POINTS[key]
                radius = (now - t0) * speed
                d = math.hypot(x - kx, y - ky)
                fade = max(0.0, 1.0 - radius / 1300.0)
                k = max(k, math.exp(-((d - radius) / width) ** 2) * fade)
            out.append([int(c * min(1.0, k)) for c in base])
        if ripples and now - ripples[-1][1] > 3.0:
            self._ripples = []
        return out

    def _engine_loop(self) -> None:
        t0 = time.time()
        ui_hash = None
        last_tick = time.time()
        gaps = collections.deque(maxlen=240)        # 每帧间隔（诊断灯效是否顺滑）
        sends = collections.deque(maxlen=240)       # 每次写灯带耗时
        skipped = 0
        fails = 0                                   # 写灯带失败次数（转发到设备失败）
        while True:
            time.sleep(1 / 30)
            now_tick = time.time()
            gaps.append((now_tick - last_tick) * 1000.0)
            last_tick = now_tick
            t = time.time() - t0
            if self._identify and time.time() < self._identify[1]:
                frame = [[0, 0, 0] for _ in range(LED_COUNT)]
                frame[self._identify[0]] = [255, 255, 255]
            else:
                frame = self._compute(t)
            static = self.lighting["mode"] in ("off", "solid", "gradient", "custom") or (
                self.lighting["mode"] == "ripple" and not self._ripples)
            h = hash(tuple(tuple(c) for c in frame))
            # 让位 / 静默 / 暂停写入 / 图片传输中，一律不碰灯带。
            # 灯珠和图片共用同一条通道，推图时不去抢——这样图片发得快，灯带也不会被挤得断流。
            # 例外：涟漪正在扩散时照写（灯带一帧很小）。
            rippling = self.lighting["mode"] == "ripple" and bool(self._ripples)
            blocked = self.writes_paused or self.quiet or self.yielded
            can_write = (not blocked) and not (self._pushing and not rippling)

            if static:
                if can_write and self._dirty and h != self._frame_hash:
                    ts = time.perf_counter()
                    ok = self.device.set_leds(self._to_hw(frame))
                    sends.append((time.perf_counter() - ts) * 1000.0)
                    if ok:
                        self._frame_hash = h
                        self._dirty = False
                    else:
                        fails += 1
            elif can_write:
                ts = time.perf_counter()
                ok = self.device.set_leds(self._to_hw(frame))
                sends.append((time.perf_counter() - ts) * 1000.0)
                if ok:
                    self._frame_hash = h
                else:
                    fails += 1
            elif not can_write:
                skipped += 1
            self._perf = {
                "gaps": list(gaps), "sends": list(sends),
                "skipped": skipped, "fails": fails, "pushing": bool(self._pushing),
                "mode": self.lighting["mode"],
            }

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
                "closeToTray": self.close_to_tray,
                "writesPaused": self.writes_paused,
                "quiet": self.quiet,
                "yielded": self.yielded,
                "iconCount": len(self.list_icons()),
                "lightingOnly": self.lighting_only,
                "standby": self.standby_sec,
                "standbyBrightness": self.standby_brightness,
                "wallpaperEnabled": self.wallpaper_enabled,
                "autoBlank": self.autoblank_sec,
                "screenOff": self.screen_off,
                "customScene": self.custom_scene_name,
                "activeScene": self.active_scene,
                "autostart": self.autostart,
                "autostartOther": self.autostart_state()["other"],
                "edition": EDITION,
                "officialReady": self.official_ready(),
                "dataDir": DATA_DIR,
                "inStandby": self.in_standby,
                "album": self.album_state(),
                "library": self.library_items(),
                "inputStats": dict(self.device.stats),
                "lastResponse": getattr(self.device, "last_response", None),
                "rotation": self.rotation,
                "page": self.page,
                "pageCount": len(self.pages),
                "pageNames": [p.get("name", "") for p in self.pages],
                "pageFolder": [bool(p.get("folder")) for p in self.pages],
                "mainPages": self._main_pages(),
                "folderPath": self._folder_path(),
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
                "keyActionValues": {str(k): action_value(v) for k, v in self.key_actions.items()},
                "keyActionSpecs": {str(k): {f: v.get(f) for f in ("enter", "secret", "button", "pos",
                                                                  "action", "state", "op", "page")}
                                   for k, v in self.key_actions.items() if isinstance(v, dict)},
                "keyGestures": {
                    str(k): {gname: {"type": gv.get("type"), "label": action_label(gv),
                                     "value": action_value(gv),
                                     "spec": {f: gv.get(f) for f in ("enter", "secret", "button", "pos",
                                                                     "action", "name", "macro", "op")}}
                             for gname, gv in g.items() if isinstance(gv, dict)}
                    for k, g in self.key_gestures.items() if g},
                "keyImages": {str(k): v.get("type") for k, v in self.key_overrides.items()},
                "keyPlugin": {
                    str(k): {"plugin": v.get("plugin"), "name": v.get("name"),
                             "status": (self.plugin_host.status().get(v.get("plugin"), "idle")
                                        if self.plugin_host else "unavailable"),
                             "rev": (self.plugin_visual.get(v.get("context") or "") or {}).get("rev", 0)}
                    for k, v in self.key_actions.items()
                    if isinstance(v, dict) and v.get("type") == "plugin"},
                # 图片换了文件名就变：界面据此决定要不要重新拉预览（切场景 / 翻页时同一个键会换图）
                "keyImageRev": {str(k): os.path.basename(v.get("file") or "") or str(id(v))
                                for k, v in self.key_overrides.items()},
                # 场景每切一次就 +1：两个场景用了同名图片文件时，靠这个让预览也跟着换
                "sceneRev": self.scene_rev,
                "ledCount": LED_COUNT,
                "keyCount": KEY_COUNT,
                "officialRunning": self.official_running,
                "events": list(self.events)[:12],
            }


APP: App | None = None
TRAY = None          # 系统托盘图标（见 tray.py），关掉界面窗口后靠它重新打开
PORT = 8642
SHUTTING_DOWN = False   # 正在退出：新实例探测到这个标记就别再当成「已有实例在运行」


def _shutdown() -> None:
    global SHUTTING_DOWN
    SHUTTING_DOWN = True    # 先立牌子，后面 1 秒内新启动的实例就不会误判成「已有实例」
    log("正在退出控制台…")

    def _hard_exit() -> None:
        # 收尾里要等浏览器 / SDK 正常退出，万一哪一步卡住，也不能让程序永远挂着
        time.sleep(8)
        os._exit(0)

    threading.Thread(target=_hard_exit, daemon=True).start()
    time.sleep(0.25)
    # 把界面窗口一起关掉：否则那个标签页还停在这个地址上，重新打开时浏览器会
    # 直接切回去，看到的是一张「连不上」的死页面，像是不启动一样。
    try:
        _close_app_windows()
    except Exception:  # noqa: BLE001
        pass
    try:
        if TRAY is not None:
            TRAY.stop()
        if APP is not None:
            if APP.plugin_host:
                APP.plugin_host.stop_all()   # 插件子进程 / 后台浏览器一起收掉
            APP._gif_stop_all()          # SDK 进程不一定随我们退出，别让它继续播旧动图
            APP.device.disconnect()
            # 顺手收掉 SDK 进程：留着它成为孤儿进程，下次启动会连到一个「假死」的会话
            APP.device._kill_sdk_process()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


def running_instance(port: int) -> dict | None:
    """端口上已有控制台在跑就返回它的信息（含 edition：源码版 / 绿色版）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.8) as resp:
            data = resp.read()
        if b"m18-console" not in data:
            return None
        try:
            info = json.loads(data.decode("utf-8"))
        except ValueError:
            return {"app": "m18-console"}
        if info.get("shuttingDown"):
            return None          # 正在退出：别当成「已在运行」，否则新窗口开到一半就断了
        return info
    except Exception:  # noqa: BLE001
        return None


def _port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_previous(port: int, seconds: float = 8.0) -> dict | None:
    """等上一个实例退干净：它还活着就返回它的信息，端口空出来就返回 None。

    刚点「退出」又马上重新打开时，旧进程还要 1 秒左右才真正退掉；这里等它一下，
    免得新进程误判成「已有实例在运行」，只开个窗口就退出去（浏览器随后就空白了）。
    """
    deadline = time.time() + seconds
    while True:
        info = running_instance(port)
        if info:
            return info
        if not _port_busy(port):
            return None
        if time.time() > deadline:
            return None
        time.sleep(0.35)


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
            # 读接口：浏览器跨站发起的（别的网页里的 <img>/<script>）一律拒绝；
            # 页面本身、地址栏直接打开、本机脚本（不带这个头）照常
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site in ("cross-site", "same-site"):
                self.send_error(403, "Cross-site request blocked")
                return False
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
            self._json({"app": "m18-console", "url": f"http://127.0.0.1:{PORT}/", "edition": EDITION,
                        "dataDir": DATA_DIR, "pid": os.getpid(), "shuttingDown": SHUTTING_DOWN})
        elif path == "/api/events":
            self._sse()
        elif path in ("/api/album/tiles.png", "/api/album/preview.jpg") or (
                path.startswith("/api/album/thumb/") and path.endswith(".jpg")):
            # 相册场景的图片走 ?scene=场景名；不传就是「待机画面」里那本相册
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            scene = (q.get("scene") or [""])[0]
            if path.endswith("tiles.png"):
                data, ctype = APP.album_tiles_png(scene), "image/png"
            elif path.endswith("preview.jpg"):
                data, ctype = APP.album_preview_jpg(scene), "image/jpeg"
            else:
                try:
                    data = APP.album_thumb(int(path.rsplit("/", 1)[1][:-4]), scene)
                except ValueError:
                    data = None
                ctype = "image/jpeg"
            if not data:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=600" if "/thumb/" in path else "no-store")
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
        elif path == "/api/iconcrop/src":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            got = APP.crop_src((q.get("token") or [""])[0])
            if not got:
                self.send_error(404)
                return
            data, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/iconcrop/tile":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            got = APP.crop_tile((q.get("token") or [""])[0], (q.get("n") or [""])[0])
            if not got:
                self.send_error(404)
                return
            data, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/iconpacks":
            self._json({"ok": True, "packs": APP.icon_packs()})
        elif path == "/api/perf":
            # 灯效诊断：看看引擎是不是被卡住 / 写灯带是不是变慢
            p = APP._perf or {}
            gaps = sorted(p.get("gaps") or [])
            sends = sorted(p.get("sends") or [])

            def q(arr, r):
                if not arr:
                    return 0.0
                return round(arr[min(len(arr) - 1, int(len(arr) * r))], 1)

            self._json({
                "ok": True, "mode": p.get("mode"), "pushing": bool(p.get("pushing")),
                "skipped": int(p.get("skipped") or 0), "fails": int(p.get("fails") or 0),
                "frames": len(gaps),
                "gifNative": bool(getattr(APP.device, "gif_native", False)),
                "gifActive": len(APP._gif_active),
                "animPushes": int(APP._anim_pushes),
                "gapMs": {"p50": q(gaps, .5), "p95": q(gaps, .95), "max": round(gaps[-1], 1) if gaps else 0.0},
                "sendMs": {"p50": q(sends, .5), "p95": q(sends, .95), "max": round(sends[-1], 1) if sends else 0.0},
            })
        elif path == "/api/plugins":
            self._json({"ok": True, "plugins": APP.plugin_catalog()})
        elif path.startswith("/api/iconpack/"):
            parts = path.split("/", 4)
            fp = APP.icon_pack_file(urllib.parse.unquote(parts[3]), urllib.parse.unquote(parts[4])) \
                if len(parts) == 5 else None
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._image_file(fp, still=bool(query.get("still")))
        elif path == "/api/library":
            self._json({"ok": True, "items": APP.library_items()})
        elif path.startswith("/api/library/thumb/"):
            # 相册缩略图：/api/library/thumb/文件名
            safe = urllib.parse.unquote(path[len("/api/library/thumb/"):])
            fp = APP.library_thumb(safe)
            if not fp:
                self.send_error(404)
                return
            self._image_file(fp)
        elif path.startswith("/api/library/"):
            # 图标库「相册」里的图片：/api/library/文件名
            safe = urllib.parse.unquote(path[len("/api/library/"):])
            fp = APP.library_path(safe)
            if not fp:
                self.send_error(404)
                return
            self._image_file(fp)
        elif path == "/api/asset":
            # 用户数据目录里的图片（上传的图 / 相册里的图），给「还没保存的预览」用
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fp = asset_path(str((query.get("p") or [""])[0]))
            if not fp or os.path.splitext(fp)[1].lower() not in IMAGE_EXTS:
                self.send_error(404)
                return
            self._image_file(fp)
        elif path == "/api/program/icon":
            # 程序的图标（拖进来先看到图标，点保存才真的绑上）
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fp = APP.program_icon(str((query.get("path") or [""])[0]))
            if not fp:
                self.send_error(404)
                return
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
        elif path == "/api/keys/sheet.png":
            data = APP.keys_sheet_png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/api/key/") and path.endswith("/live.jpg"):
            try:
                key = int(path.split("/")[3])
            except (IndexError, ValueError):
                self.send_error(400)
                return
            data = APP._number_tile(key) if 1 <= key <= KEY_COUNT else None
            if not data:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
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
        # 息屏状态下，任何控制台操作都先把屏幕点亮（睡眠接口本身除外）
        if APP.screen_off and path not in ("/api/device/sleep", "/api/quit", "/api/state"):
            APP.wake_from_console(path)
        if path == "/api/lighting":
            APP.set_lighting(body)
            self._json(APP.snapshot())
        elif path == "/api/lighting/custom":
            APP.set_custom(body.get("colors") or [])
            self._json({"ok": True})
        elif path == "/api/screen-brightness":
            APP.screen_brightness = max(0, min(100, int(body.get("value", 100))))
            APP.device.set_screen_brightness(APP.screen_brightness)
            self._json({"ok": True, "value": APP.screen_brightness})
        elif path == "/api/device/reconnect":
            # 强制重连：以前 connect() 在「已连接」时直接返回，点了等于没点
            def _force_reconnect() -> None:
                try:
                    APP.device.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.0)
                try:
                    if APP.device.connect():
                        APP.official_running = App._detect_official()
                        APP._restore_all()
                except Exception as exc:  # noqa: BLE001
                    log(f"重新连接失败：{exc}")

            threading.Thread(target=_force_reconnect, daemon=True).start()
            self._json({"ok": True, "reconnecting": True})
        elif path == "/api/device/hard-reset":
            hard = bool(body.get("hard"))
            threading.Thread(target=APP.hard_recover, args=(hard,), daemon=True).start()
            self._json({"ok": True, "hard": hard})
        elif path == "/api/device/usb-reset":
            # 软件拔插：设备固件卡住（画面不刷）时，只有重新枚举能救回来
            def _reset_job() -> None:
                r = APP.usb_reset()
                if r.get("ok"):
                    APP.broadcast({"type": "notice", "text": "设备已重新枚举，控制台正在接管…", "ms": 4000})
                else:
                    APP.broadcast({"type": "notice",
                                   "text": f"拔插设备没成功：{r.get('error', '')}（也可以手动拔一次 USB 线）",
                                   "ms": 7000})
            threading.Thread(target=_reset_job, daemon=True).start()
            self._json({"ok": True, "resetting": True})
        elif path == "/api/device/resume":
            # 电脑休眠回来后按键没反应时，手动点一下（和自动恢复走同一段代码）
            threading.Thread(target=APP._after_resume, daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/device/gif-resync":
            APP._gif_resync()
            self._json({"ok": True, "keys": sorted(APP._gif_active.keys())})
        elif path == "/api/device/sleep":
            APP.screen_off = True
            try:
                APP.blank_screen()          # 15 个键刷成黑 + 背光 0，按键通道保持可用
            except Exception:  # noqa: BLE001
                pass
            log("已息屏（按键还醒着，按任意键可唤醒）")
            self._json({"ok": True, "screenOff": True})
        elif path == "/api/device/mute":
            APP.input_muted = bool(body.get("value"))
            self._json({"ok": True, "value": APP.input_muted})
        elif path == "/api/activity":
            APP._last_activity = time.time()
            if body.get("wake") and APP.in_standby and not APP.album_active():
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
        elif path == "/api/closetotray":
            APP.close_to_tray = bool(body.get("value"))
            APP._save_config()
            self._json({"ok": True, "value": APP.close_to_tray})
        elif path == "/api/config/export":
            self._json({"ok": True, "config": APP.export_config()})
        elif path == "/api/config/import":
            self._json({"ok": APP.import_config(body.get("config") or {})})
        elif path == "/api/scenes":
            if body.get("syncOfficial"):
                if not APP.official_ready():
                    self._json({"ok": False, "items": APP.list_scenes(),
                                "reason": "没有检测到官方 Stream Dock 驱动，场景同步不了；"
                                          "自带的「相册1」「音乐控制台」和内置插件照常可用。"})
                else:
                    result = APP.sync_official_scenes()
                    self._json({"ok": True, **result, "items": APP.list_scenes()})
            elif "apps" in body and body.get("link"):
                apps = body.get("apps")
                if isinstance(apps, str):
                    apps = [a for a in apps.replace("，", ",").split(",")]
                self._json({"ok": APP.set_scene_apps(str(body["link"]), apps or []),
                            "items": APP.list_scenes()})
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
            elif "create" in body:
                r = APP.create_scene(str(body.get("create") or body.get("name") or ""),
                                     str(body.get("kind") or "normal"))
                self._json({**r, "items": APP.list_scenes()})
            elif body.get("reorder"):
                self._json({"ok": APP.set_scene_order(body.get("reorder") or []),
                            "items": APP.list_scenes()})
            elif body.get("save"):
                self._json({"ok": APP.save_scene(str(body.get("name") or body.get("save") or "")),
                            "items": APP.list_scenes()})
            elif "mute" in body and body.get("name"):
                self._json({"ok": APP.set_scene_muted(str(body["name"]), bool(body["mute"])),
                            "items": APP.list_scenes()})
            elif body.get("apply"):
                want = str(body.get("apply"))
                ok = APP.apply_scene(want)
                APP.broadcast({"type": "scene", "name": want, "ok": ok})
                self._json({"ok": ok,
                            "reason": "" if ok else (APP.scene_hint(want) or "切换失败"),
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
        elif path == "/api/iconpacks/delete":
            pack = str(body.get("pack") or "")
            fname = str(body.get("file") or "")
            ok, msg = APP.delete_icon(pack, fname) if fname else APP.delete_icon_pack(pack)
            self._json({"ok": ok, "message": msg, "packs": APP.icon_packs()})
        elif path == "/api/iconpacks/reveal":
            self._json({"ok": APP.reveal_icon_pack(str(body.get("pack") or ""), str(body.get("file") or ""))})
        elif path == "/api/iconcrop/preview":
            self._json(APP.crop_preview(str(body.get("file") or ""),
                                        str(body.get("fit") or "cover"),
                                        int(body.get("gap") or 27),
                                        str(body.get("name") or "")))
        elif path == "/api/iconcrop/save":
            r = APP.crop_save(str(body.get("token") or ""), str(body.get("name") or ""))
            if r.get("ok"):
                r["packs"] = APP.icon_packs(True)
            self._json(r)
        elif path == "/api/iconcrop/cancel":
            self._json({"ok": APP.crop_cancel(str(body.get("token") or ""))})
        elif path == "/api/plugins/delete":
            ok, msg = APP.delete_plugin(str(body.get("plugin") or ""))
            self._json({"ok": ok, "message": msg, "plugins": APP.plugin_catalog()})
        elif path == "/api/plugins/delete-action":
            ok, msg = APP.delete_plugin_action(str(body.get("plugin") or ""), str(body.get("uuid") or ""))
            self._json({"ok": ok, "message": msg, "plugins": APP.plugin_catalog()})
        elif path == "/api/plugins/reveal":
            p = APP._catalog()["plugins"].get(str(body.get("plugin") or ""))
            self._json({"ok": bool(p) and APP.reveal_in_explorer(p["folder"])})
        elif path == "/api/official/sync":
            # scenes / icons / plugins 三个开关，缺省都按「要」处理，保持老调用兼容
            if not APP.official_ready():
                self._json({"ok": False, "items": APP.list_scenes(),
                            "reason": "没有检测到官方 Stream Dock 驱动：这台电脑没装，或者装在了别的位置。"
                                      "控制台自带的图标包和 28 个内置插件不受影响，不用同步也能用。"})
            else:
                self._json({"ok": True, **APP.sync_official(
                    str(body.get("mode") or "new"),
                    bool(body.get("plugins", False)),
                    bool(body.get("scenes", True)),
                    bool(body.get("icons", True)),
                ), "items": APP.list_scenes()})
        elif path == "/api/apps/recent":
            self._json({"ok": True, "items": list(APP.recent_apps)})
        elif path == "/api/custom-plugins":
            self._json({"ok": True, "items": APP.list_custom_plugins()})
        elif path == "/api/custom-plugins/save":
            self._json(APP.save_custom_plugin(body))
        elif path == "/api/custom-plugins/delete":
            self._json({"ok": APP.delete_custom_plugin(str(body.get("id") or ""))})
        elif path == "/api/custom-plugins/reveal":
            pid = str(body.get("id") or "")
            folder = APP._custom_folder(pid) if pid.startswith(CUSTOM_PREFIX) else ""
            self._json({"ok": bool(folder) and APP.reveal_in_explorer(os.path.join(folder, "manifest.json"))})
        elif path == "/api/key/simulate":
            self._json({"ok": APP.simulate_press(int(body.get("key", 0)), str(body.get("gesture") or "press"))})
        elif path == "/api/plugins/logs":
            self._json({"ok": True, "items": APP.plugin_log_items(str(body.get("plugin") or ""),
                                                                  int(body.get("since") or 0),
                                                                  int(body.get("limit") or 200))})
        elif path == "/api/agent/config":
            self._json(APP.agent_config())
        elif path == "/api/key/plugin":
            self._json(APP.bind_plugin_action(int(body.get("key", 0)), str(body.get("plugin") or ""),
                                              int(body.get("index", -1))))
        elif path == "/api/plugin/pi":
            self._json(APP.plugin_pi(int(body.get("key", 0))))
        elif path == "/api/plugin/pi-closed":
            APP.plugin_pi_closed(int(body.get("key", 0)))
            self._json({"ok": True})
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
        elif path == "/api/program/resolve":
            raw = str(body.get("data") or "")
            blob = None
            if raw:
                try:
                    blob = base64.b64decode(raw)
                except Exception:                     # noqa: BLE001
                    blob = None
                if blob and len(blob) > 4 * 1024 * 1024:
                    blob = None
            try:
                r = APP.resolve_program(str(body.get("name") or ""),
                                        int(body.get("size") or 0), blob,
                                        str(body.get("path") or ""))
            except Exception as exc:                  # noqa: BLE001
                log(f"解析程序路径失败：{exc}")
                r = {"ok": False, "error": "解析失败"}
            self._json(r)
        elif path == "/api/key/pick-program":
            # 浏览器拿不到拖进来的程序真实路径，只能由控制台在本机弹窗选
            picked = APP.pick_program()
            self._json({"ok": bool(picked), "path": picked,
                        "name": os.path.basename(picked) if picked else ""})
        elif path == "/api/key/bind-program":
            self._json(APP.bind_program(int(body.get("key", 0)),
                                        str(body.get("path") or "")))
        elif path == "/api/key/preview":
            if body.get("src"):
                self._json({"ok": APP.set_key_preview(int(body.get("key", 0)), str(body["src"]))})
            else:
                self._json({"ok": APP.clear_key_preview(int(body.get("key", 0)))})
        elif path == "/api/key/icon":
            self._json({"ok": APP.set_key_icon(int(body.get("key", 0)),
                                               str(body.get("name") or ""))})
        elif path == "/api/config/backups":
            self._json({"ok": True, "items": APP.list_config_backups()})
        elif path == "/api/config/restore":
            self._json({"ok": APP.restore_config_backup(str(body.get("name") or ""))})
        elif path == "/api/standby-brightness":
            APP.standby_brightness = max(0, min(100, int(body.get("value", 60))))
            APP._save_config()
            if APP.album_active():
                ok = APP._push_album()
            else:
                ok = APP.apply_standby() if APP.device.connected else True
            self._json({"ok": bool(ok), "value": APP.standby_brightness})
        elif path == "/api/standby":
            APP.standby_sec = max(STANDBY_MIN, min(STANDBY_MAX, int(body.get("sec", 300))))
            APP.autoblank_sec = 0             # 二选一：打开待机画面就关掉自动息屏
            APP._save_config()
            self._json({"ok": True, "sec": APP.standby_sec, "autoBlank": 0})
        elif path == "/api/autoblank":
            sec = max(AUTOBLANK_MIN, min(AUTOBLANK_MAX, int(body.get("sec", 300))))
            on = bool(body.get("enabled", True))
            APP.autoblank_sec = sec if on else 0
            if APP.autoblank_sec:
                APP.wallpaper_enabled = False     # 二选一：开自动息屏就关掉待机画面
                if APP.in_standby:
                    APP._wake_from_standby()
            APP._last_activity = time.time()
            APP._save_config()
            APP.broadcast({"type": "status", "autoBlank": APP.autoblank_sec,
                           "wallpaperEnabled": APP.wallpaper_enabled})
            self._json({"ok": True, "autoBlank": APP.autoblank_sec,
                        "wallpaperEnabled": APP.wallpaper_enabled})
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
        elif path == "/api/wallpaper/calibrate":
            self._json({"ok": APP.apply_calibration()})
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
                self._json({"ok": True, "file": os.path.relpath(dest, DATA_DIR), "target": target})
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
            if APP.album_active():
                ok = APP._push_album()
            elif APP.preview_mode == "calibration":
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
            self._json({"ok": APP.clear_key_action(int(body.get("key", 0)), str(body.get("gesture") or ""))})
        elif path == "/api/key/reset-nav":
            self._json({"ok": APP.reset_nav_key(int(body.get("key", 0)))})
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
            if APP.wallpaper_enabled:
                APP.autoblank_sec = 0        # 二选一：开待机画面就关掉自动息屏
            APP._last_activity = time.time()
            APP._save_config()
            if not APP.wallpaper_enabled and APP.in_standby:
                threading.Thread(target=APP.apply_keys, daemon=True).start()
            self._json({"ok": True, "value": APP.wallpaper_enabled, "autoBlank": APP.autoblank_sec})
        elif path == "/api/wallpaper/default":
            self._json({"ok": APP.restore_default_wallpaper()})
        elif path == "/api/lighting/reverse":
            APP.effect_reverse = bool(body.get("value"))
            APP._save_config()
            APP._dirty = True
            self._json({"ok": True, "value": APP.effect_reverse})
        elif path == "/api/folder/open":
            ok = APP.open_folder(str(body.get("page") or ""))
            self._json({"ok": ok, **APP.snapshot()})
        elif path == "/api/folder/back":
            ok = APP.folder_back()
            self._json({"ok": ok, **APP.snapshot()})
        elif path == "/api/scenes/export":
            r = APP.export_scene(str(body.get("name") or ""))
            self._json(r)
        elif path == "/api/scenes/import":
            r = APP.import_scene_package(str(body.get("data") or ""), str(body.get("name") or ""))
            self._json({**r, "items": APP.list_scenes()})
        elif path == "/api/page":
            if body.get("add"):
                APP.goto_page(APP.add_page())
            elif "index" in body:
                APP.goto_page(int(body["index"]))
            else:
                delta = int(body.get("delta", 0))
                if delta:
                    APP.add_nav_pages("下一页" if delta > 0 else "上一页")
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
            self._json({"ok": APP.set_rotation(int(body.get("value", 0))), "rotation": APP.rotation})
        elif path == "/api/lighting/reset":
            APP.reset_lighting()
            self._json(APP.snapshot())
        elif path == "/api/album":
            self._json(APP.album_state(scene=str(body.get("scene") or "")))
        elif path == "/api/album/add":
            sc = str(body.get("scene") or "")
            self._json({"ok": APP.album_add([str(f) for f in (body.get("files") or [])], sc),
                        **APP.album_state(scene=sc)})
        elif path == "/api/album/remove":
            sc = str(body.get("scene") or "")
            self._json({"ok": APP.album_remove(int(body.get("index", -1)), sc),
                        **APP.album_state(scene=sc)})
        elif path == "/api/album/move":
            sc = str(body.get("scene") or "")
            self._json({"ok": APP.album_move(int(body.get("from", -1)), int(body.get("to", -1)), sc),
                        **APP.album_state(scene=sc)})
        elif path == "/api/album/settings":
            sc = str(body.get("scene") or "")
            APP.album_settings(body, sc)
            self._json(APP.album_state(scene=sc))
        elif path == "/api/album/reveal":
            sc = str(body.get("scene") or "")
            fp = APP._album_path_for(sc, int(body.get("index", -1)))
            ok = False
            if fp:
                try:
                    subprocess.Popen(["explorer", f"/select,{os.path.normpath(fp)}"])
                    ok = True
                    log(f"打开图片位置：{fp}")
                except Exception as exc:  # noqa: BLE001
                    log(f"打开图片位置失败：{exc}")
            self._json({"ok": ok, "path": fp or ""})
        elif path == "/api/library/add":
            self._json(APP.library_add(body.get("files") or []))
        elif path == "/api/library/remove":
            ok = APP.library_remove(str(body.get("name") or ""))
            self._json({"ok": ok, "items": APP.library_items()})
        elif path == "/api/library/reveal":
            fp = APP.library_path(str(body.get("name") or ""))
            ok = False
            if fp:
                try:
                    subprocess.Popen(["explorer", f"/select,{os.path.normpath(fp)}"])
                    ok = True
                    log(f"打开图片位置：{fp}")
                except Exception as exc:  # noqa: BLE001
                    log(f"打开图片位置失败：{exc}")
            self._json({"ok": ok, "path": fp or ""})
        elif path == "/api/album/play":
            idx = body.get("index")
            self._json({"ok": APP.album_play(None if idx is None else int(idx)), **APP.album_state()})
        elif path == "/api/album/stop":
            threading.Thread(target=APP.album_stop, daemon=True).start()
            self._json({"ok": True, **APP.album_state(active=False)})
        elif path == "/api/album/goto":
            ok = APP.album_goto(int(body.get("delta", 0)), body.get("index"))
            self._json({"ok": ok, **APP.album_state()})
        elif path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=_shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def _image_file(self, fp: str | None, still: bool = False) -> None:
        """官方图标 / 插件图：按内容判断类型；.mbg 解密成 GIF。
        still=True 时只取动图的第一帧（图标库里的缩略图用，省得同时放十几个动图）。
        结果按「文件 + 修改时间」缓存，免得每次开图标库都重新解码一遍（解码会占 GIL，拖慢灯效）。"""
        img = official.read_image(fp) if fp else None
        if not img:
            self.send_error(404)
            return
        ext, data = img
        if still and ext in (".gif", ".webp"):
            cache_key = None
            if fp:
                try:
                    cache_key = (fp, os.path.getmtime(fp))
                    hit = _STILL_CACHE.get(cache_key)
                    if hit:
                        ext, data = ".png", hit
                        cache_key = None     # 命中，不再回写
                except OSError:
                    cache_key = None
            if cache_key is not None:
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        im.seek(0)
                        buf = io.BytesIO()
                        im.convert("RGB").save(buf, "PNG")
                    png = buf.getvalue()
                    if len(_STILL_CACHE) > 600:
                        _STILL_CACHE.clear()
                    _STILL_CACHE[cache_key] = png
                    ext, data = ".png", png
                except Exception:  # noqa: BLE001
                    pass
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
        if os.path.basename(name) in ("plugin-host.html", "pi-frame.html"):
            self.send_error(404)
            return
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


# 控制台窗口里要关掉的 Edge 功能。鼠标手势（按住右键拖动画线）会和控制台里的右键拖动冲突，
# 而且以前写注册表策略关它：要么被拒绝访问（关不掉），要么连用户自己的 Edge 一起关了还显示「由你的组织管理」。
# 现在只在控制台这个独立窗口里关，三层同时生效：启动参数关功能 + 窗口自己的配置文件关开关 + 页面里拦右键拖动。
EDGE_DISABLED_FEATURES = [
    "msEdgeMouseGestureSupported", "msEdgeMouseGestureDefaultEnabled",
    "msEdgeCircleToCopilotInMouseGesture", "msEdgeScribbleToCopilotInMouseGesture",
    "msEdgeSuperDragAndDrop", "msEdgeSidebarV2", "msEdgeCollections", "msEdgeWebWidgets", "Translate",
]


def _prepare_browser_profile(profile: str) -> None:
    """控制台窗口专用配置目录：写好「鼠标手势关闭」等偏好（只影响这个窗口，不碰用户自己的 Edge）。"""
    pref = os.path.join(profile, "Default", "Preferences")
    try:
        os.makedirs(os.path.dirname(pref), exist_ok=True)
        try:
            with open(pref, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        mg = data.setdefault("edge_mouse_gesture", {})
        want = {"enabled": False, "engaged": True, "first_tip": True, "hint": False, "track": False,
                "circle_to_copilot_in_web_page_enabled": False}
        if any(mg.get(k) != v for k, v in want.items()):
            mg.update(want)
            tmp = pref + ".m18tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, pref)
    except OSError as exc:
        log(f"写控制台窗口配置失败（不影响使用）：{exc}")


def _cleanup_edge_policy() -> None:
    """旧版本往 HKCU\\Software\\Policies\\Microsoft\\Edge 写过 MouseGestureEnabled=0：
    它会把用户自己的 Edge 手势也关掉、还显示「由你的组织管理」。是我们写的就删掉（键空了连键一起删）。"""
    path = r"Software\Policies\Microsoft\Edge"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            val, _ = winreg.QueryValueEx(key, "MouseGestureEnabled")
            if val != 0:
                return
            winreg.DeleteValue(key, "MouseGestureEnabled")
            empty = winreg.QueryInfoKey(key)[:2] == (0, 0)
        if empty:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        log("已撤销旧版本写入的 Edge 全局策略（MouseGestureEnabled），你自己的 Edge 恢复原样")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 插件专用端口：网页插件 / 插件设置页 / 后台插件宿主页都从这里加载
# --------------------------------------------------------------------------- #
# 和控制台不是同一个端口 = 不同源：第三方插件网页调不了控制台的接口（浏览器同源策略 + 控制台的
# Origin / Sec-Fetch-Site 校验都会拦），也读不到控制台页面。这里也只提供插件文件，别的一概 404。
PLUGIN_PORT = 0
PLUGIN_UI_FILES = {"/plugin-host.html", "/pi-frame.html"}


class PluginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401
        return

    def do_GET(self):  # noqa: N802
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in {f"127.0.0.1:{PLUGIN_PORT}", f"localhost:{PLUGIN_PORT}"}:
            self.send_error(403, "Forbidden host")
            return
        path = urllib.parse.urlparse(self.path).path
        if path in PLUGIN_UI_FILES:
            fp = os.path.join(UI_DIR, path.lstrip("/"))
        elif path.startswith("/plugins/"):
            uid, _, rel = path[len("/plugins/"):].partition("/")
            fp = APP.plugin_file(urllib.parse.unquote(uid), rel) if APP else None
        else:
            fp = None
        if not fp or not os.path.isfile(fp):
            self.send_error(404)
            return
        self._serve(fp)

    def _serve(self, fp: str) -> None:
        ctype = {
            ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8", ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon",
            ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
        }.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.send_error(405)


def start_plugin_server() -> int:
    """在一个空闲端口上起插件专用服务；返回端口号。"""
    srv = QuietServer(("127.0.0.1", 0), PluginHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="插件端口").start()
    return srv.server_address[1]


def open_window(url: str) -> bool:
    """用独立配置目录启动一个应用窗口；启动前先关掉旧的控制台窗口。"""
    _cleanup_edge_policy()
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
        if profile:
            _prepare_browser_profile(profile)
        args = [
            exe,
            f"--app={url}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-features=" + ",".join(EDGE_DISABLED_FEATURES),
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


def _start_tray(url: str) -> None:
    """托盘图标：关掉界面窗口后服务留在后台跑，双击图标可以再打开。"""
    global TRAY
    try:
        from tray import Tray
    except Exception as exc:  # noqa: BLE001
        log(f"托盘图标不可用：{exc}")
        return
    try:
        tray = Tray("Stream Dock M18 控制台（双击打开）", lambda: open_window(url),
                    _shutdown, os.path.join(HERE, "icon.ico"))
        if tray.start():
            TRAY = tray
            log("托盘图标已就绪（关掉窗口后仍在后台运行）")
            threading.Thread(target=_tray_ui_watch, args=(tray,), daemon=True).start()
        else:
            log("托盘图标没创建成功，关掉窗口后只能用 exe 再打开")
    except Exception as exc:  # noqa: BLE001
        log(f"托盘图标出错：{exc}")


def _ui_window_open() -> bool:
    """界面的应用窗口还在不在。

    不能只看 SSE 客户端数量：Edge 关掉窗口后进程可能还赖在后台，
    SSE 连接不断，于是「关了窗口」永远检测不到。这里直接枚举窗口标题。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001
        return True                     # 查不了就当还开着，别误退出
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = [True]

    def on_enum(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith("Stream Dock M18 控制台"):
                found[0] = False
                return False            # 找到了，不用再枚举
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(on_enum), 0)
    except Exception:  # noqa: BLE001
        return True
    return not found[0]


def _close_app_windows() -> None:
    """收掉界面窗口背后的浏览器进程（温和关闭，免得 Edge 下次弹「还原页面」）。"""
    profile = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "M18Console", "browser")
    marker = profile.replace("'", "''")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
             f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{marker}') }} | "
             "ForEach-Object { $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
             "if ($p) { $p.CloseMainWindow() | Out-Null } }"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass


def _tray_ui_watch(tray) -> None:
    """界面窗口关掉时：按设置收进托盘，或者直接退出。"""
    seen = False
    while True:
        time.sleep(2)
        if _ui_window_open():
            seen = True
            continue
        if not seen:
            continue                  # 刚启动时窗口还没画出来，别急着当「关掉了」
        time.sleep(6)                 # 刷新页面 / 切场景会短暂消失，等一下再确认
        if _ui_window_open():
            seen = True
            continue
        seen = False
        if APP is not None and not APP.close_to_tray:
            log("界面窗口已关闭，按设置退出控制台（想让它在后台继续跑，去「更多设置 → 关窗口后收进托盘」打开）")
            _shutdown()
            return
        tray.balloon("控制台已收进托盘", "双击托盘图标可以重新打开窗口")
        log("界面窗口已关闭，控制台收进系统托盘继续运行（双击托盘图标重新打开）")
        _close_app_windows()          # 窗口背后那个浏览器进程留着只会占内存、还挂着死连接


def main() -> int:
    global APP, PORT, PLUGIN_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # 单实例：已有控制台在跑就只开窗口，避免两个进程同时写设备
    other = None if args.port else wait_previous(PORT)
    if other:
        url = f"http://127.0.0.1:{PORT}/"
        running = other.get("edition") or "旧版本"
        if running != EDITION:
            log(f"{running}的控制台已在运行（数据在 {other.get('dataDir', '?')}），本次启动的{EDITION}不再重复启动")
            url += "?" + urllib.parse.urlencode({"notice": f"你打开的是{EDITION}，但{running}已经在运行，当前窗口是{running}。"
                                                           f"要改用{EDITION}，请先在「设备设置 → 退出控制台」后再打开。"})
        else:
            log(f"已有实例在运行，打开 {PORT}")
        if not args.no_browser:
            open_window(url)
        return 0

    APP = App()
    PORT = args.port or free_port(PORT)
    url = f"http://127.0.0.1:{PORT}/"
    server = QuietServer(("127.0.0.1", PORT), Handler)
    PLUGIN_PORT = start_plugin_server()
    threading.Timer(0.5, APP.start_plugins).start()
    server.daemon_threads = True
    log(f"控制台已启动 {url}")
    _start_tray(url)
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
