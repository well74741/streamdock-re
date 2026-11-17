"""浏览器灯效会话：屏幕/LED 接管、有界帧管线、独立后台宿主。

不执行用户代码，不负责场景持久化，也不自动注册 HTTP 路由。
路由、生命周期和安全响应头的接法见 effect_guide.md。
"""
from __future__ import annotations

import base64
import collections
import ctypes as C
import hashlib
import hmac
import io
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from PIL import Image
from core import KEY_POINTS, LED_POINTS
from audio_loopback import AudioLoopback

MAX_EFFECT_BODY = 512 * 1024
HOST_CSP = ("default-src 'none'; script-src 'self'; worker-src 'self'; connect-src 'self'; "
            "img-src 'none'; style-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
# Worker 的 CSP 必须在 JS 的 HTTP 响应头上；不能只依赖宿主的 meta 标签。
WORKER_CSP = ("default-src 'none'; script-src 'unsafe-eval'; connect-src 'none'; "
              "worker-src 'none'; child-src 'none'; base-uri 'none'")
ASSETS = {"/effects/host": ("effect-host.html", "text/html; charset=utf-8"),
          "/effects/effect-runtime.js": ("effect-runtime.js", "text/javascript; charset=utf-8"),
          "/effects/effect-worker.js": ("effect-worker.js", "text/javascript; charset=utf-8")}


def effect_asset(path):
    """返回 (bytes, MIME, headers) 或 None；仅在独立插件 origin 提供这些资产。"""
    item = ASSETS.get(path)
    if not item:
        return None
    filename, mime = item
    csp = WORKER_CSP if filename == "effect-worker.js" else HOST_CSP
    return ((Path(__file__).parent / "ui" / filename).read_bytes(), mime,
            {"Content-Security-Policy": csp, "Cache-Control": "no-store",
             "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
             "Cross-Origin-Resource-Policy": "same-origin",
             "Permissions-Policy": "microphone=(), camera=(), geolocation=(), usb=()"})


def effect_guide():
    """返回纯中文文档，不加 JSON 包装；不读取用户数据。"""
    return (Path(__file__).parent / "effect_guide.md").read_text(encoding="utf-8")


def find_effect_browser():
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(path):
            return path
    return None


class _BrowserJob:
    """只收本次 Popen 的进程子树，避免 taskkill 枚举/WMI 卡顿和根进程退出后的孤儿。"""
    def __init__(self, proc):
        class Basic(C.Structure):
            _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64),
                        ("flags", C.c_uint32), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t),
                        ("active", C.c_uint32), ("affinity", C.c_size_t),
                        ("priority", C.c_uint32), ("scheduling", C.c_uint32)]
        class Extended(C.Structure):
            _fields_ = [("basic", Basic), ("io", C.c_uint64 * 6),
                        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [C.c_void_p, C.c_wchar_p]
        self.kernel.CreateJobObjectW.restype = C.c_void_p
        self.kernel.SetInformationJobObject.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32]
        self.kernel.SetInformationJobObject.restype = C.c_int
        self.kernel.AssignProcessToJobObject.argtypes = [C.c_void_p, C.c_void_p]
        self.kernel.AssignProcessToJobObject.restype = C.c_int
        self.kernel.CloseHandle.argtypes = [C.c_void_p]
        self.kernel.CloseHandle.restype = C.c_int
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if self.handle:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (not self.kernel.SetInformationJobObject(self.handle, 9, C.byref(info), C.sizeof(info))
                    or not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle))):
                self.close()

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def effect_browser_job(proc):
    """可供离线浏览器测试复用；非 Windows/不能分配 Job 时回退到精确 PID 清理。"""
    if os.name == "nt":
        try:
            job = _BrowserJob(proc)
            return job if job.handle else None
        except OSError:
            return None
    return None


class EffectsMixin:
    def init_effects(self):
        self._effect_lock = threading.RLock()
        self._effect_lifecycle = threading.RLock()
        self._effect_quit = threading.Event()
        self._effect_threads = []
        self._effect_active = False
        self._effect_generation = 0
        self._effect_token = ""
        self._effect_name = ""
        self._effect_code = ""
        self._effect_status = "stopped"
        self._effect_origin = ""
        self._effect_launch = True
        self._effect_proc = None
        self._effect_job = None
        self._effect_profile = None
        self._effect_audio = AudioLoopback()
        self._effect_events = collections.deque(maxlen=128)
        self._effect_logs = collections.deque(maxlen=200)
        self._effect_logs_seq = 0
        self._effect_colors = [[0, 0, 0] for _ in LED_POINTS]
        self._effect_images = {}
        self._effect_preview = None
        self._effect_seq = 0
        self._effect_last_poll = 0.0
        self._effect_last_frame = 0.0
        self._effect_started = 0.0
        self._effect_last_led = 0.0
        self._effect_led_hash = None
        self._effect_black_step = 0
        self._effect_next_black = 0.0
        self._effect_sent = {}
        self._effect_screen_seq = -1
        self._effect_image_seq = 0
        self._effect_screen_colors = [[0, 0, 0] for _ in LED_POINTS]
        self._effect_next_batch = 0.0
        self._effect_last_screen = 0.0
        self._effect_last_image = 0.0
        self._effect_cursor = 1
        self._effect_device_session = None
        self._effect_gif_ready = False
        self._effect_log_window = collections.deque(maxlen=32)
        self._effect_frame_window = collections.deque(maxlen=30)
        self._effect_metrics = {"frames": 0, "dropped": 0, "images": 0, "leds": 0, "batches": 0, "batchSendMs": 0.0}
        self._effect_pushing = False

    def start_effects(self, plugin_origin, *, launch_host=True):
        """插件 HTTP 监听成功后调用；只起监督/屏幕/LED线程，此时不采集音频。"""
        url = urlsplit(plugin_origin)
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost")
                or not url.port or url.path not in ("", "/") or url.query or url.fragment
                or url.username or url.password):
            raise ValueError("灯效宿主必须使用本机独立 HTTP origin")
        with self._effect_lifecycle:
            self._effect_origin = f"http://{url.hostname}:{url.port}"
            self._effect_launch = bool(launch_host)
            if self._effect_threads:
                return
            self._effect_quit.clear()
            for target, name in ((self._effect_watch_loop, "m18-fx-watch"),
                                 (self._effect_screen_loop, "m18-fx-screen"),
                                 (self._effect_led_loop, "m18-fx-led")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._effect_threads.append(thread)
                thread.start()

    def _init_effects(self):
        self.init_effects()

    def _start_effect_scene(self, name, code):
        try:
            return bool(self.load_effect({"name": name, "code": code})["active"])
        except RuntimeError:
            return False

    def _stop_effect_runtime(self, *, restore=False):
        return self.stop_effect(restore=restore)

    @staticmethod
    def effect_guide():
        return effect_guide()

    def effect_active(self):
        return bool(getattr(self, "_effect_active", False))

    def load_effect(self, scene):
        """加载 {name, code}；code=None 使用随包默认示例。失败会撤销接管。"""
        if not isinstance(scene, dict):
            raise ValueError("灯效场景必须是对象")
        name = str(scene.get("name") or "灯效场景")[:120]
        code = scene.get("code")
        if code is None:
            code = (Path(__file__).parent / "ui" / "effect-pad.js").read_text(encoding="utf-8")
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 128 * 1024:
            raise ValueError("灯效代码不能为空且不能超过 128 KiB")
        with self._effect_lifecycle:
            if not self._effect_threads:
                raise RuntimeError("请先在插件服务启动后调用 start_effects")
            self.stop_effect(restore=False)
            with self._effect_lock:
                self._effect_generation += 1
                self._effect_token = secrets.token_urlsafe(32)
                self._effect_active = True
                self._effect_name, self._effect_code = name, code
                self._effect_status = "starting"
                self._effect_started = time.monotonic()
                self._effect_last_poll = self._effect_last_frame = 0.0
                self._effect_seq = 0
                self._effect_colors = [[0, 0, 0] for _ in LED_POINTS]
                black = io.BytesIO()
                Image.new("RGB", (64, 64), "black").save(black, "JPEG", quality=78)
                self._effect_images = {k: black.getvalue() for k in range(1, 16)}
                preview = io.BytesIO()
                Image.new("RGB", (500, 370), "black").save(preview, "JPEG", quality=72)
                self._effect_preview = preview.getvalue()
                self._effect_sent = {}
                self._effect_events.clear()
                self._effect_frame_window.clear()
                self._effect_log_window.clear()
                self._effect_led_hash = None
                self._effect_black_step = 0
                self._effect_next_black = 0.0
                self._effect_last_led = 0.0
                self._effect_gif_ready = False
                self._effect_device_session = None
                self._effect_screen_seq = -1
                self._effect_image_seq = 0
                self._effect_screen_colors = [[0, 0, 0] for _ in LED_POINTS]
                self._effect_next_batch = 0.0
                self._effect_last_screen = 0.0
                self._effect_last_image = 0.0
                self._effect_cursor = 1
                self._effect_metrics = {"frames": 0, "dropped": 0, "images": 0, "leds": 0, "batches": 0, "batchSendMs": 0.0}
                self._effect_pushing = False
            try:
                self._effect_audio.start()
                if self._effect_launch:
                    self._effect_spawn_host()
            except Exception:
                self._effect_log("error", "无法启动灯效后台宿主")
                self.stop_effect("error", restore=True)
                raise RuntimeError("无法启动灯效后台宿主") from None
            self._effect_log("info", "已启动灯效场景")
        self._effect_notify()
        return self.effect_state()

    def _effect_spawn_host(self):
        exe = find_effect_browser()
        if not exe:
            raise RuntimeError("未安装 Edge/Chrome")
        self._effect_profile = tempfile.mkdtemp(prefix="m18-effect-")
        args = [exe, "--headless=new", "--disable-gpu", "--no-proxy-server",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-background-networking",
                "--disable-extensions", "--disable-sync", "--mute-audio",
                f"--user-data-dir={self._effect_profile}",
                f"{self._effect_origin}/effects/host#{self._effect_token}"]
        # 不输出命令行：fragment 中有短期凭证；绝不复用用户浏览器 profile。
        # 资源管理器/PCA可能给控制台注入 DetectorsAppHealth 兼容层；
        # Edge 继承后会在约0.1秒内以退出码0结束，尚未打开宿主页。
        # 只清理这个新浏览器的环境，不改主进程环境、系统兼容设置或注册表。
        env = {key: value for key, value in os.environ.items() if key.upper() != "__COMPAT_LAYER"}
        self._effect_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, close_fds=True, env=env,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._effect_job = effect_browser_job(self._effect_proc)

    def _effect_kill_host(self):
        proc, profile = self._effect_proc, self._effect_profile
        job, self._effect_job = self._effect_job, None
        self._effect_proc = self._effect_profile = None
        if job:
            job.close()
        if proc and proc.poll() is None:
            if not job and os.name == "nt":
                # 只杀本次 Popen 创建的 PID 树；不按进程名找浏览器/已有服务。
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
            elif not job:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if profile:
            shutil.rmtree(profile, ignore_errors=True)

    def stop_effect(self, reason="stopped", *, restore=True):
        """先使 token/帧失效，再关自己的浏览器与音频；切场景传 restore=False。"""
        with self._effect_lifecycle:
            with self._effect_lock:
                was_active = self._effect_active
                self._effect_active = False
                self._effect_generation += 1
                self._effect_token = ""
                self._effect_code = ""
                self._effect_status = reason or "stopped"
                self._effect_images = {}
                self._effect_preview = None
                self._effect_events.clear()
            try:
                self._effect_kill_host()
            finally:
                self._effect_audio.stop()
                # 并行写屏连接上可能还有最后一帧在写：等它们写完再恢复原按键，免得旧帧盖回去
                wait = getattr(self.device, "wait_writers_idle", None)
                if wait:
                    wait(1.0)
                self._dirty = True
                self._frame_hash = None
            if was_active:
                if restore:
                    hook = getattr(self, "_effect_runtime_failed", None)
                    if hook:
                        hook(reason or "stopped")  # 场景层负责退出失败场景，避免重连复活坏代码
                    elif hasattr(self, "apply_keys"):
                        self.apply_keys()
                self._effect_notify()
        return self.effect_state()

    def close_effects(self):
        """程序退出/让位前调用；不恢复屏幕，不再留下监督线程。"""
        self._effect_quit.set()
        self.stop_effect(restore=False)
        for thread in self._effect_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3)
        self._effect_threads = []

    def _effect_notify(self):
        if hasattr(self, "broadcast"):
            self.broadcast({"type": "effect", "effect": self.effect_state()})

    def _effect_log(self, level, message):
        with self._effect_lock:
            text = str(message)
            if self._effect_token:
                text = text.replace(self._effect_token, "[会话凭证]")
            self._effect_logs_seq += 1
            self._effect_logs.append({"seq": self._effect_logs_seq, "time": time.time(),
                                      "level": str(level)[:16], "message": text[:2048]})

    def effect_logs(self, since=0, limit=50):
        with self._effect_lock:
            return [entry for entry in self._effect_logs if entry["seq"] > int(since)][
                -max(1, min(200, int(limit))):]

    def effect_state(self):
        with self._effect_lock:
            return {"active": self._effect_active, "name": self._effect_name,
                    "status": self._effect_status, "revision": self._effect_generation,
                    "previewRev": self._effect_seq,
                    "audio": self._effect_audio.snapshot(),
                    "fps": {"preview": 10, "screens": 20, "leds": 25,
                            "fullBoard": 15 if self._effect_parallel() else 4, "imageBudget": 60},
                    "parallel": self._effect_parallel(),
                    "metrics": dict(self._effect_metrics)}

    def effect_preview(self, key=None):
        """返回当前 JPEG bytes 或 None；主 UI 可同源代理，不用暴露宿主 token。"""
        with self._effect_lock:
            if key is None:
                return self._effect_preview
            return self._effect_images.get(int(key))

    def _effect_parallel(self):
        """设备层支持每键独立写图连接（见 DeviceWS._writer）。"""
        device = self.device
        ready = getattr(device, "parallel_ready", None)
        if ready is None:   # 测试替身/旧适配器
            ready = getattr(device, "parallel_writes", False) and getattr(device, "gif_native", False)
        return bool(ready and hasattr(device, "writers_idle"))

    def effect_repaint(self):
        """同一设备会话的息屏/唤醒、旋转或映射改变后，清去重缓存以重送最新画面。"""
        with self._effect_lock:
            self._effect_sent = {}
            self._effect_led_hash = None
            self._effect_screen_seq = -1

    def effect_input(self, key, down):
        """1..18 物理下/上事件；True 表示已消费，不应再执行普通键动作。"""
        if isinstance(key, bool) or not isinstance(key, int) or key not in KEY_POINTS:
            raise ValueError("按键编号必须为 1–18")
        if not isinstance(down, bool):
            raise ValueError("down 必须为布尔值")
        with self._effect_lock:
            if not self._effect_active:
                return False
            x, y = KEY_POINTS[key]
            self._effect_events.append({"key": key, "down": down, "x": x, "y": y,
                                        "time": (time.monotonic() - self._effect_started) * 1000})
            return True

    def _effect_authorize(self, token):
        if (not self._effect_active or not isinstance(token, str) or not token
                or not hmac.compare_digest(token, self._effect_token)):
            raise PermissionError("灯效会话无效")

    def effect_host_request(self, method, path, token, body=None):
        """插件 origin 的 HTTP 路由入口，返回可 JSON 编码对象。

        调用方必须检查 Origin/Host、限制 body<=MAX_EFFECT_BODY、从 Bearer 取 token，
        不支持 query token，不打印 Authorization。PermissionError->403，ValueError->400。
        """
        with self._effect_lock:
            self._effect_authorize(token)
            now = time.monotonic()
            if method == "GET" and path == "/effects/session":
                size = min(740 * 0.50 / (3 + 2 * 0.12), 1000 * 0.74 / (5 + 4 * 0.12))
                return {"code": self._effect_code, "width": 1000, "height": 740,
                        "keys": [{"key": k, "x": x, "y": y, "size": size, "screen": k <= 15}
                                 for k, (x, y) in KEY_POINTS.items()],
                        "leds": [{"index": i, "x": x, "y": y}
                                 for i, (x, y) in enumerate(LED_POINTS)],
                        "fps": 25, "screenFps": 20, "imageBudget": 60}
            if method == "GET" and path == "/effects/poll":
                self._effect_last_poll = now
                events = list(self._effect_events)
                self._effect_events.clear()
                return {"events": events, "audio": self._effect_audio.snapshot(),
                        "time": (now - self._effect_started) * 1000,
                        "parallel": self._effect_parallel()}
            if method == "POST" and path == "/effects/frame":
                return self.receive_effect_frame(token, body)
            if method == "POST" and path == "/effects/log":
                if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                    raise ValueError("日志格式错误")
                for text in body["messages"][:16]:
                    if len(self._effect_log_window) >= 16 and now - self._effect_log_window[-16] < 1:
                        break
                    self._effect_log_window.append(now)
                    self._effect_log("log", text)
                return {"ok": True}
            if method == "POST" and path == "/effects/error":
                message = (body or {}).get("message", "灯效运行失败")
                self._effect_log("error", message)
                generation = self._effect_generation
            else:
                raise ValueError("未知灯效路由")
        # 不在会话锁中做 join/退出，避免锁顺序死锁。
        self._effect_fail(generation, "error")
        return {"ok": True}

    @staticmethod
    def _effect_jpeg(value, preview=False):
        limit = 160 * 1024 if preview else 20 * 1024
        size = (500, 370) if preview else (64, 64)
        prefix = "data:image/jpeg;base64,"
        if not isinstance(value, str) or not value.startswith(prefix) or len(value) > limit * 4 // 3 + 64:
            raise ValueError("灯效帧必须是有界 JPEG")
        try:
            data = base64.b64decode(value[len(prefix):], validate=True)
            if len(data) > limit:
                raise ValueError()
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.size != size:
                    raise ValueError()
                image.verify()
            return data
        except Exception:
            raise ValueError("灯效 JPEG 无效或尺寸错误") from None

    def receive_effect_frame(self, token, body):
        """最新帧槽，不建立帧队列；序号必须递增，最多每秒 30 个请求（宿主按 25fps 发）。"""
        with self._effect_lock:
            self._effect_authorize(token)
            if not isinstance(body, dict):
                raise ValueError("帧必须是对象")
            seq = body.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or not self._effect_seq < seq <= 2 ** 53:
                raise ValueError("帧序号必须递增")
            now = time.monotonic()
            if len(self._effect_frame_window) == 30 and now - self._effect_frame_window[0] < 1:
                raise ValueError("帧率超过限制")
            colors = body.get("colors")
            if not isinstance(colors, list) or len(colors) != len(LED_POINTS):
                raise ValueError("必须提交 24 颗 LED")
            for rgb in colors:
                if not isinstance(rgb, list) or len(rgb) != 3 or any(
                        isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in rgb):
                    raise ValueError("LED 必须为 0–255 整数 RGB")
            keys = body.get("keys", {})
            if not isinstance(keys, dict) or len(keys) > 15 or any(str(k) not in map(str, range(1, 16)) for k in keys):
                raise ValueError("屏幕帧只能包含 1–15 键")
            images = {int(k): self._effect_jpeg(v) for k, v in keys.items()}
            screen_seq = body.get("screenSeq", seq)
            screen_colors = body.get("screenColors", colors)
            if images:
                if (isinstance(screen_seq, bool) or not isinstance(screen_seq, int)
                        or not self._effect_image_seq <= screen_seq <= seq or screen_seq <= 0):
                    raise ValueError("屏幕帧编号无效")
                if not isinstance(screen_colors, list) or len(screen_colors) != len(LED_POINTS) or any(
                        not isinstance(rgb, list) or len(rgb) != 3 or any(
                            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in rgb)
                        for rgb in screen_colors):
                    raise ValueError("屏幕配对LED颜色无效")
            preview = self._effect_jpeg(body["preview"], True) if body.get("preview") else None
            if self._effect_image_seq > 0 and self._effect_screen_seq != self._effect_image_seq and images:
                self._effect_metrics["dropped"] += 1
            was_lit = any(any(rgb) for rgb in self._effect_colors)
            is_lit = any(any(rgb) for rgb in colors)
            if was_lit and not is_lit:
                self._effect_black_step = 0
                self._effect_next_black = 0.0
            elif is_lit:
                self._effect_next_black = float("inf")
            self._effect_colors = [list(rgb) for rgb in colors]
            if images:
                # 宿主只提交变化的键：未变化屏幕保留，不把整版旧目标清空。
                self._effect_images = {**self._effect_images, **images}
                self._effect_image_seq = screen_seq
                self._effect_screen_colors = [list(rgb) for rgb in screen_colors]
            if preview:
                self._effect_preview = preview
            self._effect_seq = seq
            self._effect_frame_window.append(now)
            self._effect_last_frame = now
            self._effect_status = "running"
            self._effect_metrics["frames"] += 1
        return {"ok": True, "seq": seq}

    def _effect_blocked(self, screens=False):
        return (not self.device.connected or any(getattr(self, attr, False)
                for attr in ("writes_paused", "quiet", "yielded"))
                or (screens and any(getattr(self, attr, False) for attr in ("screen_off", "lighting_only"))))

    def _clear_effect_leds(self, frame):
        """清除单珠缓存和全灯颜色，避免固件/SDK里某一角残留；不改亮度或配置。"""
        clear = getattr(self.device, "clear_leds", None)
        if clear:
            return bool(clear())
        ok = bool(self.device.set_leds(self._to_hw(frame)))
        global_clear = getattr(self.device, "set_all_leds", None)
        if global_clear:
            ok = bool(global_clear(0, 0, 0)) and ok
        return ok

    def _effect_led_frame(self, *, push=True):
        """LED唯一写入口，只采用最新颜色；结束黑色小量补发，不把发送成功当显示回执。"""
        with self._effect_lock:
            if not self._effect_active:
                return None
            frame = [list(rgb) for rgb in self._effect_colors]
            dark = not any(any(rgb) for rgb in frame)
            now = time.monotonic()
            digest = (getattr(self.device, "session", 0), tuple(map(tuple, frame)))
            black_due = dark and now >= self._effect_next_black
            if (push and now - self._effect_last_led >= 1 / 25
                    and (digest != self._effect_led_hash or black_due)
                    and not self._effect_blocked() and not getattr(self, "_pushing", False)):
                self._effect_last_led = now
                ok = self._clear_effect_leds(frame) if dark else bool(self.device.set_leds(self._to_hw(frame)))
                if ok:
                    self._effect_led_hash = digest
                    self._effect_metrics["leds"] += 1
                if dark:
                    if ok:
                        delay = (0.12, 0.23, 1.0)[min(self._effect_black_step, 2)]
                        self._effect_black_step = min(3, self._effect_black_step + 1)
                    else:
                        delay = 0.12
                    self._effect_next_black = now + delay
            return frame

    def _effect_screen_tick(self, now=None):
        """同一快照整批提交。

        并行模式（M18 + 官方 SDK）：每键一条写图连接，SDK 每张图固定的 50ms 互不排队，
        整板约 57ms；等上一批 15 条连接都回执处理完再发下一批，不在 SDK 里堆旧帧。
        单连接回退：60图/秒预算在批次之间等待，不在15键之间制造扫屏。
        """
        now = time.monotonic() if now is None else now
        if self._effect_blocked(True) or getattr(self, "_pushing", False):
            return
        paint = getattr(self, "_paint_lock", self._effect_lock)
        if not paint.acquire(blocking=False):
            return
        try:
            with self._effect_lock:
                if not self._effect_active or not self._effect_images or self._effect_blocked(True):
                    return
                session = getattr(self.device, "session", 0)
                if session != self._effect_device_session:
                    self._effect_device_session = session
                    self._effect_sent = {}
                    self._effect_gif_ready = False
                parallel = self._effect_parallel()
                if now < self._effect_next_batch or now - self._effect_last_screen < 0.05:
                    return
                if parallel and not self.device.writers_idle():
                    return          # 上一批还没写完：等回执，只发最新快照
                generation = self._effect_generation
                rotation = getattr(self.device, "image_rotation", 0)
                screen_seq = self._effect_image_seq
                images = dict(self._effect_images)
                # 屏幕快照中的旧LED颜色仅供诊断，不能回写到已结束的最新灯色。
                # 空间交错顺序；不是仅调换上下行，真正的一致性来自冻结快照和整批socket写入。
                order = (8, 11, 5, 1, 15, 6, 10, 13, 3, 7, 9, 12, 14, 2, 4)
                changed = {key: images[key] for key in order if key in images
                           and self._effect_sent.get(key) != hashlib.sha256(images[key]).digest()}
                if not changed:
                    self._effect_screen_seq = screen_seq
                    return
                self._effect_last_screen = now
                self._effect_next_batch = now if parallel else now + len(changed) / 60
                self._pushing = True
                self._effect_pushing = True
                if not self._effect_gif_ready:
                    if hasattr(self, "_gif_stop_all"):
                        self._gif_stop_all()
                    self.device.stop_gif_loop()
                    self._effect_gif_ready = True

            def cancelled():
                with self._effect_lock:
                    return (not self._effect_active or generation != self._effect_generation
                            or self._effect_blocked(True) or session != getattr(self.device, "session", 0)
                            or rotation != getattr(self.device, "image_rotation", 0))

            started = time.perf_counter()
            batch = getattr(self.device, "send_image_batch", None)
            if batch:
                ok = batch(changed, None, cancelled=cancelled)  # LED只由最新颜色线程写入
            else:
                # 假设备/旧适配器兼容路径也始终使用同一快照，绝不在循环中取下一帧像素。
                ok = True
                for key, jpeg in changed.items():
                    if cancelled():
                        ok = False
                        break
                    ok = bool(self.device.send_image(key, jpeg)) and ok
                if ok and not cancelled():
                    ok = bool(self.device.refresh())
            elapsed = (time.perf_counter() - started) * 1000
            with self._effect_lock:
                if (self._effect_active and generation == self._effect_generation
                        and session == getattr(self.device, "session", 0)
                        and rotation == getattr(self.device, "image_rotation", 0)):
                    self._effect_metrics["batchSendMs"] = elapsed
                    if ok:
                        self._effect_sent.update({key: hashlib.sha256(jpeg).digest() for key, jpeg in changed.items()})
                        self._effect_metrics["images"] += len(changed)
                        self._effect_metrics["batches"] += 1
                        self._effect_screen_seq = screen_seq
        finally:
            self._effect_pushing = False
            self._pushing = False
            paint.release()

    def _effect_led_loop(self):
        deadline = time.monotonic()
        while not self._effect_quit.is_set():
            delay = max(0, deadline - time.monotonic())
            if delay > 0:
                time.sleep(delay)
            if self._effect_quit.is_set():
                break
            deadline = time.monotonic() + 1 / 25
            with self._effect_lock:
                generation = self._effect_generation
            try:
                self._effect_led_frame()
            except Exception:
                self._effect_log("error", "灯效 LED 写入失败")
                self._effect_fail(generation, "error")

    def _effect_screen_loop(self):
        # 预算等待在批次之间；同一批的键面不再被16.7ms间隔拆成滚动扫描。
        deadline = time.monotonic()
        while not self._effect_quit.is_set():
            with self._effect_lock:
                due = max(deadline, self._effect_next_batch)
            delay = max(0, due - time.monotonic())
            if delay > 0:
                time.sleep(delay)
            if self._effect_quit.is_set():
                break
            started = time.monotonic()
            with self._effect_lock:
                generation = self._effect_generation
                # 并行模式靠写图回执节流，轮询要细一些，否则每批平白多等半个周期
            deadline = started + (0.01 if self._effect_parallel() else 0.05)
            try:
                self._effect_screen_tick(started)
            except Exception:
                self._effect_log("error", "灯效屏幕写入失败")
                self._effect_fail(generation, "error")

    def _effect_fail(self, generation, reason):
        with self._effect_lifecycle:
            with self._effect_lock:
                if not self._effect_active or generation != self._effect_generation:
                    return
            self.stop_effect(reason, restore=True)

    def _effect_watch_loop(self):
        while not self._effect_quit.wait(0.2):
            with self._effect_lock:
                if not self._effect_active:
                    continue
                now = time.monotonic()
                generation = self._effect_generation
                proc = self._effect_proc
                failed = proc is not None and proc.poll() is not None
                age = now - self._effect_started
                timed_out = (age > 10 and not self._effect_last_frame) or (
                    self._effect_last_frame > 0 and now - self._effect_last_frame > 3)
                timed_out = timed_out or (self._effect_last_poll > 0 and now - self._effect_last_poll > 3)
            if failed or timed_out:
                self._effect_log("error", f"灯效宿主已退出（退出码 {proc.returncode}）" if failed else "灯效无响应，已停止")
                self._effect_fail(generation, "error" if failed else "timeout")
