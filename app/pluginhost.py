"""插件宿主：兼容 Stream Dock / Elgato 插件协议。

    插件进程（Node.js / exe / 后台浏览器里的网页） ──WebSocket JSON── PluginHost ── App（按键、画面）

- 启动方式（与官方一致）：
    Node.js：node20.exe <CodePath> -port P -pluginUUID U -registerEvent registerPlugin -info JSON
    exe    ：<CodePath>       -port P -pluginUUID U -registerEvent registerPlugin -info JSON
    网页   ：后台无头 Edge 打开 /plugin-host.html，用 iframe 加载插件页并调用
             connectElgatoStreamDeckSocket(port, uuid, "registerPlugin", info)
- 属性检查器（插件设置页）在控制台界面里用 iframe 加载，注册事件 registerPropertyInspector，
  uuid 用该动作实例的 context。
- 安全：WebSocket 握手校验 Origin——浏览器里别的网页连不上；本地进程（没有 Origin）放行。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time

from wssdk import _xor_mask

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# 极简 WebSocket 服务端（只处理文本帧；客户端帧必有掩码）
# --------------------------------------------------------------------------- #
class WSConn:
    def __init__(self, sock: socket.socket, addr, origin: str):
        self.sock = sock
        self.addr = addr
        self.origin = origin
        self.role = ""                 # "plugin" / "pi" / "host"
        self.uuid = ""                 # 插件 UUID 或 PI 对应的 context
        self.closed = False
        self._lock = threading.Lock()
        self._buf = b""

    def send_json(self, obj) -> bool:
        if self.closed:
            return False
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        try:
            with self._lock:
                self.sock.sendall(bytes(header) + data)
            return True
        except OSError:
            self.close()
            return False

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read_message(self) -> str | None:
        """读一条完整消息（拼接分片）；连接关闭返回 None。"""
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = _xor_mask(payload, mask)
            if opcode == 0x8:
                return None
            if opcode == 0x9:                      # ping → pong
                with self._lock:
                    self.sock.sendall(bytes([0x8A, len(payload)]) + payload[:125])
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "ignore")

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class WSServer:
    def __init__(self, on_message, on_close, allowed_origins):
        self.on_message = on_message
        self.on_close = on_close
        self.allowed_origins = allowed_origins     # 可调用：返回允许的 Origin 集合
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                sock, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock, addr), daemon=True).start()

    def _serve(self, sock: socket.socket, addr) -> None:
        conn = None
        try:
            sock.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            key = headers.get("sec-websocket-key")
            origin = headers.get("origin", "")
            if not key or headers.get("upgrade", "").lower() != "websocket":
                sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            # 浏览器发起的连接必须来自本控制台自己的页面；本地进程没有 Origin，放行
            if origin and origin.lower() not in self.allowed_origins():
                sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            sock.settimeout(None)
            conn = WSConn(sock, addr, origin)
            conn._buf = rest
            while True:
                msg = conn.read_message()
                if msg is None:
                    break
                try:
                    self.on_message(conn, json.loads(msg))
                except ValueError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    print(f"[pluginhost] 处理消息出错：{exc}", flush=True)
        except (OSError, ConnectionError):
            pass
        finally:
            if conn:
                conn.close()
                try:
                    self.on_close(conn)
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    sock.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# 插件进程
# --------------------------------------------------------------------------- #
def plugin_kind(manifest: dict, folder: str) -> tuple[str, str]:
    """返回 (类型, 入口文件绝对路径)。类型：node / exe / html / builtin / unsupported。"""
    if manifest.get("PrivateAPI"):
        return "builtin", ""
    code = manifest.get("CodePathWin") or manifest.get("CodePath") or ""
    if not code:
        return "unsupported", ""
    path = os.path.normpath(os.path.join(folder, code))
    low = code.lower()
    if low.endswith((".html", ".htm")):
        return "html", path
    if low.endswith((".js", ".mjs", ".cjs")) or manifest.get("Nodejs"):
        return "node", path
    if low.endswith(".exe"):
        return "exe", path
    if low.endswith((".cmd", ".bat")):
        return "cmd", path
    if os.path.isfile(path + ".exe"):
        return "exe", path + ".exe"
    return "unsupported", path


class PluginHost:
    """管理插件进程与连接；具体按键语义由 App 通过回调处理。"""

    def __init__(self, *, install_root: str, data_dir: str, http_port_fn, log,
                 on_plugin_message, on_pi_message, on_plugin_ready, on_host_message):
        self.install_root = install_root
        self.data_dir = data_dir
        self.http_port_fn = http_port_fn
        self.log = log
        self.on_plugin_message = on_plugin_message
        self.on_pi_message = on_pi_message
        self.on_plugin_ready = on_plugin_ready
        self.on_host_message = on_host_message
        self.lock = threading.RLock()
        self.plugins: dict[str, dict] = {}        # uuid -> {folder, manifest, kind, entry}
        self.conns: dict[str, WSConn] = {}        # 插件 uuid -> 连接
        self.pi_conns: dict[str, WSConn] = {}     # context -> PI 连接
        self.host_conn: WSConn | None = None      # 后台浏览器宿主页
        self.procs: dict[str, subprocess.Popen] = {}
        self.pending: dict[str, list] = {}        # 插件没连上前的待发消息
        self.html_wanted: set[str] = set()
        self.started: set[str] = set()
        self.failed: dict[str, str] = {}
        self.fallback_node: str | None = None     # 官方驱动卸载后用本地备份的 node
        self._browser_started = 0.0
        os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
        self.server = WSServer(self._on_message, self._on_close, self._origins)
        self._kill_leftovers()
        log(f"插件宿主已启动：ws://127.0.0.1:{self.server.port}")

    # -- 基础 ------------------------------------------------------------ #
    def _origins(self) -> set:
        p = self.http_port_fn()
        return {f"http://127.0.0.1:{p}", f"http://localhost:{p}"}

    def register_plugins(self, plugins: dict) -> None:
        """plugins: uuid -> {folder, manifest}（来自官方插件目录）。"""
        with self.lock:
            for uid, p in plugins.items():
                kind, entry = plugin_kind(p["manifest"], p["folder"])
                self.plugins[uid] = {**p, "kind": kind, "entry": entry}

    def device_info(self) -> dict:
        return getattr(self, "_device_info", {"id": "M18", "name": "Stream Dock M18",
                                              "size": {"columns": 5, "rows": 3}, "type": 0})

    def set_device(self, device_id: str, name: str = "Stream Dock M18") -> None:
        self._device_info = {"id": device_id or "M18", "name": name,
                             "size": {"columns": 5, "rows": 3}, "type": 0}

    def info_json(self, uid: str) -> str:
        p = self.plugins.get(uid) or {}
        man = p.get("manifest") or {}
        return json.dumps({
            "application": {"font": "Microsoft YaHei", "language": "zh_CN", "platform": "windows",
                            "platformVersion": "10.0", "version": "6.5"},
            "plugin": {"uuid": uid, "version": str(man.get("Version") or "1.0")},
            "devicePixelRatio": 1,
            "colors": {"buttonPressedBackgroundColor": "#303030FF",
                       "buttonPressedBorderColor": "#646464FF",
                       "buttonPressedTextColor": "#969696FF",
                       "highlightColor": "#0078FFFF"},
            "devices": [self.device_info()],
        }, ensure_ascii=False)

    def status(self) -> dict:
        with self.lock:
            return {uid: ("connected" if uid in self.conns else
                          "failed" if uid in self.failed else
                          "starting" if uid in self.started else "idle")
                    for uid in self.plugins}

    # -- 发送 ------------------------------------------------------------ #
    def send(self, uid: str, msg: dict) -> None:
        """发给插件；还没连上就先排队并确保已启动。"""
        with self.lock:
            conn = self.conns.get(uid)
            if conn and not conn.closed:
                conn.send_json(msg)
                return
            self.pending.setdefault(uid, []).append(msg)
            if len(self.pending[uid]) > 200:
                self.pending[uid] = self.pending[uid][-200:]
        self.ensure_running(uid)

    def send_pi(self, context: str, msg: dict) -> None:
        conn = self.pi_conns.get(context)
        if conn and not conn.closed:
            conn.send_json(msg)

    def send_host(self, msg: dict) -> bool:
        conn = self.host_conn
        return bool(conn and not conn.closed and conn.send_json(msg))

    # -- 启动 / 停止 ------------------------------------------------------ #
    def ensure_running(self, uid: str) -> bool:
        with self.lock:
            p = self.plugins.get(uid)
            if not p or uid in self.failed:
                return False
            if uid in self.started:
                return True
            self.started.add(uid)
        kind = p["kind"]
        try:
            if kind in ("node", "exe", "cmd"):
                self._spawn(uid, p)
            elif kind == "html":
                with self.lock:
                    self.html_wanted.add(uid)
                self._ensure_browser()
                self._push_html_list()
            else:
                with self.lock:
                    self.failed[uid] = "内置插件由官方程序实现" if kind == "builtin" else "无法识别的插件类型"
                self.log(f"插件 {uid} 暂不支持：{self.failed[uid]}")
                return False
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.failed[uid] = str(exc)
                self.started.discard(uid)
            self.log(f"插件 {uid} 启动失败：{exc}")
            return False
        return True

    def _node_exe(self) -> str | None:
        for cand in (os.path.join(self.install_root, "node", "node20.exe"),
                     os.path.join(self.install_root, "node", "node.exe")):
            if os.path.isfile(cand):
                return cand
        if self.fallback_node and os.path.isfile(self.fallback_node):
            return self.fallback_node
        from shutil import which
        return which("node")

    def _spawn(self, uid: str, p: dict) -> None:
        entry = p["entry"]
        if not os.path.isfile(entry):
            raise FileNotFoundError(f"找不到入口 {entry}")
        args = ["-port", str(self.server.port), "-pluginUUID", uid,
                "-registerEvent", "registerPlugin", "-info", self.info_json(uid)]
        if p["kind"] == "cmd":
            cmd = ["cmd", "/c", entry] + args
        elif p["kind"] == "node":
            node = self._node_exe()
            if not node:
                raise FileNotFoundError("找不到 Node.js（官方安装目录 node\\node20.exe）")
            cmd = [node, entry] + args
        else:
            cmd = [entry] + args
        log_path = os.path.join(self.data_dir, "logs", f"{uid}.log")
        logf = open(log_path, "a", encoding="utf-8", errors="replace")
        logf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {' '.join(cmd[:2])}\n")
        logf.flush()
        proc = subprocess.Popen(cmd, cwd=p["folder"], stdout=logf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        with self.lock:
            self.procs[uid] = proc
        self._save_pids()
        self.log(f"插件已启动：{uid}（{p['kind']}，pid {proc.pid}）")
        threading.Thread(target=self._watch_proc, args=(uid, proc), daemon=True).start()

    def _watch_proc(self, uid: str, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self.lock:
            if self.procs.get(uid) is not proc:
                return
            self.procs.pop(uid, None)
            self.started.discard(uid)
            if getattr(self, "_stopping", False):
                return
            restarts = self.__dict__.setdefault("_restarts", {})
            times = [t for t in restarts.get(uid, []) if time.time() - t < 60] + [time.time()]
            restarts[uid] = times
            give_up = len(times) > 3
            if give_up:
                self.failed[uid] = f"进程反复退出（退出码 {code}），详见 plugin-logs/{uid}.log"
        self.log(f"插件进程退出：{uid}（退出码 {code}）" + ("，已停止重试" if give_up else "，稍后重启"))
        if not give_up:
            time.sleep(2)
            self.ensure_running(uid)

    def _profile_dir(self) -> str:
        return os.path.join(os.environ.get("LOCALAPPDATA") or self.data_dir, "M18Console", "pluginhost")

    def _ensure_browser(self) -> None:
        """网页插件跑在后台浏览器里（宿主页 /plugin-host.html）。

        用「有窗口」的浏览器而不是 --headless：便签 / 待办这类插件按下键后靠
        window.open 打开一个弹窗，无头模式里弹窗根本不会显示，用户看到的就是
        「点了没反应」。窗口被挪到屏幕外并从任务栏隐藏，平时看不见；插件自己
        弹出的窗口则在正常位置显示（和官方驱动的手感一致）。
        """
        if self.host_conn and not self.host_conn.closed:
            return
        if time.time() - self._browser_started < 15:
            return
        self._browser_started = time.time()
        exe = None
        for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if os.path.isfile(cand):
                exe = cand
                break
        if not exe:
            self.log("找不到 Edge/Chrome，网页类插件无法运行")
            return
        self._kill_browser()
        url = f"http://127.0.0.1:{self.http_port_fn()}/plugin-host.html?ws={self.server.port}"
        args = [exe, "--no-first-run", "--no-default-browser-check",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--autoplay-policy=no-user-gesture-required",
                "--disable-popup-blocking",            # 按键是 WS 事件，没有用户手势，默认会被拦
                "--mute-audio", "--window-position=-3200,-3200",
                f"--user-data-dir={self._profile_dir()}", url]
        subprocess.Popen(args, creationflags=NO_WINDOW, close_fds=True)
        threading.Thread(target=self._hide_host_window, daemon=True).start()
        self.log("已启动后台插件浏览器（网页类插件）")

    def _hide_host_window(self) -> None:
        """把后台浏览器窗口从任务栏和 Alt+Tab 里藏掉（WS_EX_TOOLWINDOW）。

        窗口本身留在屏幕外不关闭——关掉它插件就停了；插件自己打开的弹窗是
        独立窗口，不受影响，照常显示在桌面上。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
        except Exception:  # noqa: BLE001
            return
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
        for _ in range(80):                      # 最多等 20 秒
            hwnd = user32.FindWindowW(None, "M18 插件宿主")
            if hwnd:
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                      (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                return
            time.sleep(0.25)

    def _push_html_list(self) -> None:
        with self.lock:
            items = []
            for uid in sorted(self.html_wanted):
                p = self.plugins.get(uid)
                if not p:
                    continue
                rel = os.path.relpath(p["entry"], p["folder"]).replace(os.sep, "/")
                items.append({"uuid": uid, "url": f"/plugins/{uid}/{rel}", "info": self.info_json(uid)})
        self.send_host({"event": "plugins", "items": items, "port": self.server.port})

    def _kill_browser(self) -> None:
        marker = self._profile_dir().replace("'", "''")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{marker}') }} | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=15, creationflags=NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass

    def _pid_file(self) -> str:
        return os.path.join(self.data_dir, "plugin-pids.json")

    def _save_pids(self) -> None:
        with self.lock:
            data = {uid: [p.pid, p.args[0] if isinstance(p.args, list) else ""]
                    for uid, p in self.procs.items()}
        try:
            with open(self._pid_file(), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    def _kill_leftovers(self) -> None:
        """上次异常退出留下的插件进程：只杀 pid 和程序路径都对得上的。"""
        try:
            with open(self._pid_file(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        for uid, (pid, exe) in data.items():
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").ExecutablePath"],
                    capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
                if out and exe and os.path.normcase(out) == os.path.normcase(exe):
                    subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                                   capture_output=True, timeout=10, creationflags=NO_WINDOW)
                    self.log(f"清理上次残留的插件进程 {uid}（pid {pid}）")
            except Exception:  # noqa: BLE001
                continue
        self._kill_browser()
        try:
            os.remove(self._pid_file())
        except OSError:
            pass

    def reload(self, uid: str) -> None:
        """自制插件保存后热重载：清掉失败记录；网页插件让宿主页重建它的 iframe。"""
        with self.lock:
            self.failed.pop(uid, None)
            running_html = uid in self.html_wanted
        if running_html and not self.send_host({"event": "reload", "uuid": uid}):
            self._ensure_browser()

    def forget(self, uid: str) -> None:
        """删除插件：停掉并从列表里移除。"""
        with self.lock:
            self.html_wanted.discard(uid)
            self.started.discard(uid)
            self.failed.pop(uid, None)
            self.plugins.pop(uid, None)
            self.pending.pop(uid, None)
            conn = self.conns.pop(uid, None)
            proc = self.procs.pop(uid, None)
        if conn:
            conn.close()
        if proc:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10, creationflags=NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass
        self._push_html_list()

    def stop_all(self) -> None:
        self._stopping = True
        with self.lock:
            procs = list(self.procs.items())
            self.procs.clear()
        for uid, proc in procs:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10, creationflags=NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass
        self._kill_browser()
        try:
            os.remove(self._pid_file())
        except OSError:
            pass

    # -- 收消息 ------------------------------------------------------------ #
    def _on_message(self, conn: WSConn, msg: dict) -> None:
        event = msg.get("event")
        if not conn.role:
            if event == "registerPlugin" and msg.get("uuid") in self.plugins:
                uid = msg["uuid"]
                conn.role, conn.uuid = "plugin", uid
                with self.lock:
                    old = self.conns.get(uid)
                    self.conns[uid] = conn
                    queued = self.pending.pop(uid, [])
                if old and old is not conn:
                    old.close()
                self.log(f"插件已连接：{uid}")
                conn.send_json({"event": "deviceDidConnect", "device": self.device_info()["id"],
                                "deviceInfo": {k: v for k, v in self.device_info().items() if k != "id"}})
                # 显示类事件按「当前」可见状态重发一遍（on_plugin_ready），排队里的旧版本丢掉，避免插件收到重复 willAppear
                queued = [m for m in queued if m.get("event") not in ("willAppear", "willDisappear")]
                self.on_plugin_ready(uid)
                for m in queued:
                    conn.send_json(m)
            elif event == "registerPropertyInspector" and msg.get("uuid"):
                conn.role, conn.uuid = "pi", str(msg["uuid"])
                with self.lock:
                    self.pi_conns[conn.uuid] = conn
                # 官方宿主在设置页注册后会主动推一次 didReceiveSettings，
                # 不少设置页（如系统监控）收到它才把界面显示出来，自己并不发 getSettings
                self.on_pi_message(conn.uuid, {"event": "getSettings"})
            elif event == "registerHost" and conn.origin:
                conn.role = "host"
                self.host_conn = conn
                self.log("后台插件浏览器已就绪")
                self._push_html_list()
            return
        if conn.role == "plugin":
            self.on_plugin_message(conn.uuid, msg)
        elif conn.role == "pi":
            self.on_pi_message(conn.uuid, msg)
        elif conn.role == "host":
            self.on_host_message(msg)

    def _on_close(self, conn: WSConn) -> None:
        with self.lock:
            if conn.role == "plugin" and self.conns.get(conn.uuid) is conn:
                self.conns.pop(conn.uuid, None)
                self.log(f"插件断开：{conn.uuid}")
                # 网页插件掉线时让宿主页重载它
                if self.plugins.get(conn.uuid, {}).get("kind") == "html":
                    self.started.discard(conn.uuid)
            elif conn.role == "pi" and self.pi_conns.get(conn.uuid) is conn:
                self.pi_conns.pop(conn.uuid, None)
            elif conn.role == "host" and self.host_conn is conn:
                self.host_conn = None
                if self.html_wanted and not getattr(self, "_stopping", False):
                    threading.Timer(3.0, self._ensure_browser).start()
