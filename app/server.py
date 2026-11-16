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
import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image
import official
from pluginhost import PluginHost
from wssdk import DeviceWS

import core
from core import (  # noqa: F401
    APP_DIR,
    AUTOBLANK_MAX,
    AUTOBLANK_MIN,
    CUSTOM_PLUGIN_DIR,
    CUSTOM_PREFIX,
    DATA_DIR,
    DEFAULT_LIGHTING,
    EDITION,
    FROZEN,
    HERE,
    ICON_DIR,
    IMAGE_EXTS,
    KEY_COUNT,
    LED_COUNT,
    MAX_UPLOAD_BYTES,
    MIRROR_DIR,
    PLUGIN_DATA,
    ROOT,
    STANDBY_MAX,
    STANDBY_MIN,
    STARTUP_DIR,
    STARTUP_LNKS,
    SoundPlayer,
    UI_DIR,
    UPLOAD_DIR,
    WALLPAPER,
    WS_SDK_EXE,
    _STILL_CACHE,
    asset_path,
    log,
    pick_path,
    resolve_lnk,
    start_loop,
)
from app_render import RenderMixin
from app_pages import PagesMixin
from app_media import MediaMixin
from app_scenes import ScenesMixin
from app_catalog import CatalogMixin
from app_plugins import PluginsMixin
from app_lighting import LightingMixin


class App(RenderMixin, PagesMixin, MediaMixin, ScenesMixin, CatalogMixin, PluginsMixin, LightingMixin):
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
        start_loop(self._cache_sweep_loop, "缓存清扫")
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

    def start_plugins(self) -> None:
        """HTTP 服务起来之后再启动插件宿主（网页插件要从本服务加载）。"""
        try:
            os.makedirs(PLUGIN_DATA, exist_ok=True)
            self.plugin_host = PluginHost(
                install_root=core.SD_ROOT, data_dir=PLUGIN_DATA, http_port_fn=lambda: PLUGIN_PORT, log=self._host_log,
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
