"""Stream Dock M18 控制台的公共部分：常量、数据目录、输入模拟、动作标签、路径与小工具。

由 server.py 拆出；server.py 和各 app_*.py 都从这里导入。
"""
from __future__ import annotations
import colorsys
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import official




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
KEY_GAP = 27                 # 裁图、壁纸和相册统一使用固定的物理间隙补偿
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


_log_writes = 0


def log(msg: str) -> None:
    global _log_writes
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    _log_writes += 1
    if _log_writes % 200 == 0:
        _rotate_log()


def _rotate_log() -> None:
    """日志文件（pythonw 运行时 stdout 重定向到的那个）超过 2 MB 就换一份，只留上一份。"""
    f = sys.stdout
    path = getattr(f, "name", "")
    if not (isinstance(path, str) and path.endswith("server.log")):
        return
    try:
        if os.path.getsize(path) < 2_000_000:
            return
        f.flush()
        f.close()
        os.replace(path, path + ".1")
    except OSError:
        pass
    new = open(path, "a", buffering=1, encoding="utf-8")
    sys.stdout = new
    sys.stderr = new


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


def _touch(path: str) -> None:
    """记一下「最近用过」（NTFS 默认不更新访问时间，用修改时间代替）。一天内用过就不再改，省磁盘写。"""
    try:
        if time.time() - os.path.getmtime(path) > 86400:
            os.utime(path, None)
    except OSError:
        pass


def sweep_dir(folder: str, max_age_days: float, max_mb: float = 0, keep=frozenset(), recursive: bool = False) -> tuple[int, int]:
    """删掉超过 max_age_days 没用过的文件；总量超过 max_mb 时再从最旧的删起。keep 里的文件不动。
    返回 (删掉的文件数, 释放的字节数)。"""
    if not os.path.isdir(folder):
        return 0, 0
    items = []
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for root, _dirs, files in walker:
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p):
                items.append((st.st_mtime, st.st_size, p))
    keep = {os.path.normcase(os.path.abspath(k)) for k in keep if k}
    now = time.time()
    removed, freed = 0, 0
    items.sort()                                    # 最旧的在前
    total = sum(sz for _, sz, _ in items)
    for mtime, size, p in items:
        if os.path.normcase(os.path.abspath(p)) in keep:
            continue
        too_old = now - mtime > max_age_days * 86400
        too_big = max_mb and total > max_mb * 1024 * 1024
        if not (too_old or too_big):
            continue
        try:
            os.remove(p)
            removed += 1
            freed += size
            total -= size
        except OSError:
            pass
    if recursive:                                   # 清掉删空的子目录
        for root, dirs, _files in os.walk(folder, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
    return removed, freed


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
