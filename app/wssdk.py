"""极简 WebSocket 客户端 + StreamDock WebSocket SDK 设备封装。

不依赖第三方库：手写握手与帧编解码，够用即可（只收发文本帧）。
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import struct
import subprocess
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _xor_mask(payload: bytes, mask: bytes) -> bytes:
    """WebSocket 掩码：整块大整数异或，比逐字节 Python 循环快几十倍（图片帧有几十 KB）。"""
    n = len(payload)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def _field(payload: dict, *names, default=None):
    """按名字取字段，不区分大小写（官方文档写 FirmwareVersion，实际可能是 firmwareVersion）。"""
    if not isinstance(payload, dict):
        return default
    lower = {str(k).lower(): v for k, v in payload.items()}
    for name in names:
        value = lower.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


class WSClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9002):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.on_message = None
        self.on_close = None
        self._buf = b""
        self._send_lock = threading.Lock()
        self.close_reason = ""

    def connect(self, timeout: float = 5.0) -> bool:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手期间连接关闭")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        first = head.split(b"\r\n")[0]
        if b"101" not in first:
            raise ConnectionError(first.decode("utf-8", "ignore"))
        self._buf = rest
        sock.settimeout(None)
        self.sock = sock
        return True

    def send_json(self, obj: dict) -> None:
        self._send_frame(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def _frame_bytes(payload: bytes, opcode: int = 0x1) -> bytes:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        return bytes(header) + _xor_mask(payload, mask)

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            raise ConnectionError("未连接")
        data = self._frame_bytes(payload, opcode)
        with self._send_lock:
            self.sock.sendall(data)

    def send_json_batch(self, objects) -> None:
        """一份有界批次，一次socket写入多条独立WS帧，不把SDK协议改成JSON数组。"""
        if not isinstance(objects, (list, tuple)) or not 1 <= len(objects) <= 32:
            raise ValueError("批次需要1–32条消息")
        frames, size = [], 0
        for obj in objects:
            if not isinstance(obj, dict):
                raise ValueError("批次消息需要对象")
            frame = self._frame_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            size += len(frame)
            if size > 512 * 1024:
                raise ValueError("批次不能超过512KB")
            frames.append(frame)
        with self._send_lock:
            sock = self.sock
            if not sock:
                raise ConnectionError("未连接")
            old_timeout = None
            if os.name == "nt" and isinstance(sock, socket.socket):
                # 只限制发送，不动接收线程的超时或整个系统时钟。
                old_timeout = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, 2000)
            try:
                sock.sendall(b"".join(frames))
            finally:
                if old_timeout is not None:
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, old_timeout)
                    except OSError:
                        pass

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = _xor_mask(payload, mask)
        return fin, opcode, payload

    def recv_loop(self) -> None:
        parts: list[bytes] = []          # 分片消息（FIN=0 + 续帧）拼回完整一条
        try:
            while True:
                fin, opcode, payload = self._read_frame()
                if opcode == 0x8:
                    self.close_reason = "对端关闭连接"
                    break
                if opcode == 0x9:
                    self._send_frame(payload, opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    parts = [payload]
                elif opcode == 0x0:
                    parts.append(payload)
                else:
                    continue
                if fin and self.on_message:
                    data, parts = b"".join(parts), []
                    self.on_message(data.decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            if not self.close_reason:
                self.close_reason = str(exc) or exc.__class__.__name__
        finally:
            if self.on_close:
                self.on_close()

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None


class DeviceWS:
    """用官方 WebSocket SDK 操作设备，接口刻意和旧的 transport 版保持一致。"""

    def __init__(self, on_input, port: int = 9002, exe: str | None = None):
        self.on_input = on_input
        self.port = port
        self.exe = exe
        self.connected = False
        self.error = ""
        self.firmware = ""
        self.product = ""
        self.device_type = ""            # SDK 的设备型号（M18 才有整屏临时背景）
        self.path = ""
        self.quiet = False
        self._ws: WSClient | None = None
        self._proc: subprocess.Popen | None = None
        self._dev_ready = threading.Event()
        self._lock = threading.RLock()
        self._last_led_frame = 0.0
        self.stats = {"read": 0, "data": 0, "none": 0, "err": 0}
        self._read_errors = 0
        self.info = {}
        # SDK 原生 GIF：由 SDK 自己按帧率循环播放，主机只发一次文件路径
        self.gif_native = False
        self.gif_running = False
        self.session = 0                 # 每次重新连上 +1，上层据此判断 GIF 状态是否失效
        # Dock 竖放 / 倒放时，所有键面图（含动图）在这里统一转正：PIL 角度，逆时针
        self.image_rotation = 0
        self._gif_status = threading.Event()
        # 并行写屏：官方 SDK 每写完一张键图都在本连接里固定 sleep 50ms，且一条连接内的命令串行，
        # 15 键整板因此要 ~770ms、自下而上一张张出现。sleep 只阻塞自己那条连接，
        # 所以每个键固定用一条独立连接（同一键的先后顺序不变），整板实测 ~57ms。
        # 所有按键级命令（键图、键 GIF、清键 GIF）都走该键自己的连接，所以同一键的先后顺序不变；
        # 全局命令（清屏、GIF 循环启停）先等各键连接写完再在主连接上执行（_barrier）。
        self._writers: dict[int, WSClient] = {}
        self._writer_pending: dict[int, int] = {}            # 已发出、SDK 还没回执的批数
        self._writer_since: dict[int, float] = {}            # 最近一次发出的时刻
        self._writer_cond = threading.Condition()
        self._writers_lock = threading.Lock()
        self._writer_session = -1
        self.parallel_writes = True

    # -- 进程/连接 ---------------------------------------------------------- #
    @staticmethod
    def _kill_sdk_process() -> None:
        """杀掉所有 StreamDockWebsocketSDK.exe。

        这个 exe 只服务本控制台（官方驱动跑的是 StreamDock.exe），所以可以安全强杀。
        目的是清掉上一个实例崩溃/被结束之后留下的孤儿进程——它可能还占着设备，
        新实例连上去会表现为「命令都发出去了，屏幕却不动」。
        """
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/F", "/IM", "StreamDockWebsocketSDK.exe"],
                           capture_output=True, creationflags=flags, timeout=15)
        except Exception:  # noqa: BLE001
            pass

    def _start_server(self) -> bool:
        if port_open(self.port):
            # 9002 已被占用：如果我们自己没起过 SDK，那它多半是上个实例留下的孤儿进程
            if self._proc is None:
                self._kill_sdk_process()
                for _ in range(30):
                    if not port_open(self.port):
                        break
                    time.sleep(0.1)
            if port_open(self.port):
                return True
        if not self.exe or not os.path.isfile(self.exe):
            self.error = ("找不到 ws-sdk\\StreamDockWebsocketSDK.exe：请把压缩包全部解压后再运行，"
                          "ws-sdk 文件夹要和 M18Console.exe 放在一起")
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [self.exe, "-port", str(self.port)],
            cwd=os.path.dirname(self.exe),
            creationflags=flags,
        )
        for _ in range(60):
            if port_open(self.port):
                return True
            time.sleep(0.2)
        self.error = "SDK 服务没有起来"
        return False

    def connect(self) -> bool:
        with self._lock:
            if self.connected:
                return True
            try:
                if not self._start_server():
                    return False
                # 旧连接（设备拔掉后留下的）先关掉，否则每次重连都会多出一条
                self._drop_ws()
                ws = WSClient("127.0.0.1", self.port)
                # 回调绑定到「这一条」连接：旧连接迟到的消息 / 关闭事件一律忽略
                ws.on_message = lambda raw, _ws=ws: self._on_message(raw, _ws)
                ws.on_close = lambda _ws=ws: self._on_close(_ws)
                ws.connect()
                self._ws = ws
                self._dev_ready.clear()
                threading.Thread(target=ws.recv_loop, daemon=True).start()
                if not self._dev_ready.wait(8):
                    self.error = "没找到 M18：检查 USB 线，并关掉官方 Stream Dock 软件"
                    self.disconnect()      # 清理掉，下次重试用全新的 SDK 进程
                    return False
                # 开始监听按键
                self._send("read", {})
                self._probe_gif()
                self.session += 1
                # 提前建好 15 条写图连接，第一次切场景不用再等握手
                threading.Thread(target=self._warm_writers, daemon=True, name="m18-writers-warm").start()
                return True
            except Exception as exc:  # noqa: BLE001
                self.error = f"连接 SDK 失败：{exc}"
                return False

    def _probe_gif(self) -> None:
        """探测 SDK 是否支持原生 GIF；支持的话先清掉上一次会话留下的 GIF（SDK 断开后仍会继续播）。"""
        self._gif_status.clear()
        self._send("gifLoopStatus", {})
        self.gif_native = self._gif_status.wait(1.0)
        if self.gif_native:
            self._send("stopGifLoop", {})
            for key in range(1, 16):
                self._send("clearKeyGif", {"keyIndex": key})
            self.gif_running = False

    def _drop_ws(self) -> None:
        """只断开 WebSocket，不动 SDK 进程。"""
        ws, self._ws = self._ws, None
        if ws:
            ws.close()
        self._drop_writers()

    def _drop_writers(self) -> None:
        with self._writers_lock:
            writers, self._writers = self._writers, {}
            self._writer_session = -1
        with self._writer_cond:
            self._writer_pending = {}
            self._writer_since = {}
            self._writer_cond.notify_all()
        for ws in writers.values():
            ws.close()

    @property
    def parallel_ready(self) -> bool:
        """并行写屏需要 SDK 支持 gifLoopStatus（回执来源）。"""
        return bool(self.parallel_writes and self.gif_native and self._ws and self.path)

    def _writer(self, key: int) -> WSClient | None:
        """键 key 的专用写图连接；只发按键级命令，不订阅按键（read 只在主连接上发）。"""
        with self._writers_lock:
            if self._writer_session != self.session:
                old, self._writers = self._writers, {}
                self._writer_session = self.session
                for ws in old.values():
                    ws.close()
                with self._writer_cond:
                    self._writer_pending = {}
                    self._writer_since = {}
            ws = self._writers.get(key)
            if ws is not None and ws.sock is not None:
                return ws
            ws = WSClient("127.0.0.1", self.port)

            def done(_key=key):
                with self._writer_cond:
                    if self._writer_pending.get(_key, 0) > 0:
                        self._writer_pending[_key] -= 1
                    self._writer_cond.notify_all()

            def on_message(raw):
                # 每批末尾带一句 gifLoopStatus：收到回复说明 SDK 已把这条连接的命令处理完（含那 50ms）
                if '"gifLoopStatus"' in raw:
                    done()

            def on_close(_key=key, _ws=ws):
                with self._writer_cond:
                    self._writer_pending[_key] = 0
                    self._writer_cond.notify_all()
                with self._writers_lock:
                    if self._writers.get(_key) is _ws:
                        self._writers.pop(_key, None)

            ws.on_message = on_message
            ws.on_close = on_close
            ws.connect(timeout=2.0)
            threading.Thread(target=ws.recv_loop, daemon=True, name=f"m18-writer-{key}").start()
            self._writers[key] = ws
            return ws

    def _warm_writers(self) -> None:
        if not self.parallel_ready:
            return
        for key in range(1, 16):
            try:
                self._writer(key)
            except OSError:
                self.parallel_writes = False
                return

    def _pending(self, key: int, now: float) -> int:
        """回执超过 1 秒没来就当已处理（连接异常时不永久卡住）。"""
        if now - self._writer_since.get(key, 0.0) > 1.0:
            self._writer_pending[key] = 0
        return self._writer_pending.get(key, 0)

    def _send_key(self, key: int, objects: list, *, max_pending: int = 2) -> bool:
        """把按键级命令发到该键的连接；该连接积压达到 max_pending 批时先等（真背压，最多 1 秒）。"""
        if not self.parallel_ready:
            ws = self._ws
            if not ws:
                return False
            try:
                ws.send_json_batch(objects)
                return True
            except Exception:  # noqa: BLE001
                return False
        try:
            ws = self._writer(key)
        except OSError:
            self.parallel_writes = False      # 开不出额外连接：退回单连接串行写
            return self._send_key(key, objects)
        deadline = time.monotonic() + 1.0
        with self._writer_cond:
            while max_pending and self._pending(key, time.monotonic()) >= max_pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._writer_cond.wait(left)
            self._writer_pending[key] = self._writer_pending.get(key, 0) + 1
            self._writer_since[key] = time.monotonic()
        try:
            ws.send_json_batch(objects + [{"event": "gifLoopStatus", "path": self.path, "payload": {}}])
            return True
        except Exception:  # noqa: BLE001
            with self._writer_cond:
                self._writer_pending[key] = 0
                self._writer_cond.notify_all()
            ws.close()
            return False

    def writers_idle(self, keys=None) -> bool:
        """这些键的连接都已处理完已发命令（SDK 回执，不是发送成功）。"""
        keys = range(1, 16) if keys is None else keys
        now = time.monotonic()
        with self._writer_cond:
            return all(self._pending(k, now) == 0 for k in keys)

    def wait_writers_idle(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._writer_cond:
            while not all(self._pending(k, time.monotonic()) == 0 for k in list(self._writer_pending)):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._writer_cond.wait(left)
        return True

    def _barrier(self, event: str, payload: dict) -> bool:
        """全局命令：先等各键连接写完，再在主连接执行并等它处理完，之后的按键命令才不会和它乱序。"""
        if not self.parallel_ready or not self._writers:
            return self._send(event, payload)
        self.wait_writers_idle(1.0)
        self._gif_status.clear()
        ok = self._send(event, payload) and self._send("gifLoopStatus", {})
        if ok:
            self._gif_status.wait(0.5)
        return ok

    def disconnect(self) -> None:
        with self._lock:
            self.connected = False
            self._drop_ws()
            if self._proc:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                self._proc = None

    def _on_close(self, ws: WSClient | None = None) -> None:
        if ws is not None and ws is not self._ws:
            return                       # 已被替换掉的旧连接
        if self.connected and ws is not None and ws.close_reason:
            self.error = f"SDK 连接断开：{ws.close_reason}"
        self.connected = False

    # -- 消息 --------------------------------------------------------------- #
    def _on_message(self, raw: str, ws: WSClient | None = None) -> None:
        if ws is not None and ws is not self._ws:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        event = msg.get("event")
        payload = msg.get("payload") or {}
        if event == "deviceDidConnect":
            self.path = msg.get("path") or _field(payload, "path") or ""
            self.firmware = str(_field(payload, "firmwareVersion", default=""))
            self.product = str(_field(payload, "product", "type", default=""))
            self.device_type = str(_field(payload, "type", default=""))

            def _hex(value, default):
                try:
                    return int(str(value), 16) if isinstance(value, str) else int(value or default)
                except (TypeError, ValueError):
                    return default

            self.info = {
                "vid": f"0x{_hex(_field(payload, 'vendorID'), 0x6603):04X}",
                "pid": f"0x{_hex(_field(payload, 'productID'), 0x1012):04X}",
                "serial": str(_field(payload, "serialNumber", default="")),
                "firmware": self.firmware,
                "keys": 15,
                "leds": 24,
                "path": self.path,
                "product": self.product,
                "type": self.device_type,
            }
            self.connected = True
            self.error = ""
            self._dev_ready.set()
        elif event == "deviceDidDisconnect":
            # 关掉这条连接，让热插拔线程走完整的 connect()：
            # 新连接 → 等 deviceDidConnect → 重新发 read → 上层重推画面/灯光
            self.connected = False
            self.error = "设备已断开"
            if ws is not None and ws is self._ws:
                self._drop_ws()
        elif event == "gifLoopStatus":
            self.gif_running = bool(payload.get("running"))
            self._gif_status.set()
        elif event == "read":
            key = payload.get("keyId")
            state = payload.get("keyUpOrKeyDown")
            self.stats["read"] += 1
            self.stats["data"] += 1
            if isinstance(key, int) and state in ("keyDown", "keyUp"):
                try:
                    self.on_input(key, 1 if state == "keyDown" else 0)
                except Exception:  # noqa: BLE001
                    pass
        else:
            # 其余多为命令回执/错误，记下来方便排查
            self.last_response = (event, payload)
            if event and ("error" in event.lower() or payload.get("success") is False):
                self.error = f"{event}: {payload}"

    def check_present(self) -> bool:
        """连接仍然可用：设备在线、WebSocket 没断、我们拉起的 SDK 进程还活着。"""
        if not self.connected or self._ws is None or self._ws.sock is None:
            return False
        if self._proc is not None and self._proc.poll() is not None:
            return False
        return True

    def mark_lost(self, reason: str = "") -> None:
        self.error = reason
        self.connected = False
        with self._lock:
            self._drop_ws()

    def _send(self, event: str, payload: dict) -> bool:
        try:
            if not self._ws or not self.path:
                return False
            self._ws.send_json({"event": event, "path": self.path, "payload": payload})
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- 设备操作（和旧接口同名） -------------------------------------------- #
    def _rotate_jpeg(self, jpeg: bytes, rotation: int | None = None) -> bytes:
        from PIL import Image
        angle = self.image_rotation if rotation is None else rotation
        im = Image.open(io.BytesIO(jpeg)).convert("RGB").rotate(angle, expand=True)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90, subsampling=0)
        return buf.getvalue()

    def _rotate_gif(self, path: str) -> str:
        """动图逐帧转好方向，按「源文件 + 修改时间 + 角度」缓存在源文件旁边。"""
        from PIL import Image, ImageSequence
        rot = self.image_rotation % 360
        stamp = int(os.path.getmtime(path))
        out = f"{os.path.splitext(path)[0]}.r{rot}_{stamp}.gif"
        if os.path.isfile(out):
            try:
                if time.time() - os.path.getmtime(out) > 86400:
                    os.utime(out, None)     # 记一下最近用过，缓存清扫按这个判断
            except OSError:
                pass
            return out
        frames, durations = [], []
        with Image.open(path) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= 240:
                    break
                frames.append(frame.convert("RGB").rotate(rot, expand=True))
                durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
        if not frames:
            return path
        tmp = out + ".tmp"
        frames[0].save(tmp, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                       loop=0, disposal=1, optimize=False)
        os.replace(tmp, out)
        return out

    def send_image(self, key: int, jpeg: bytes) -> bool:
        if self.image_rotation % 360:
            try:
                jpeg = self._rotate_jpeg(jpeg)
            except Exception:  # noqa: BLE001
                pass
        b64 = base64.b64encode(jpeg).decode()
        if not self.path:
            return False
        # 并行时每张图后跟一个 refresh：不依赖别的连接上的 refresh 先后
        return self._send_key(key, [
            {"event": "setKeyImgData", "path": self.path,
             "payload": {"keyIndex": key, "imgData": f"data:image/jpeg;base64,{b64}"}}]
            + ([{"event": "refresh", "path": self.path, "payload": {}}] if self.parallel_ready else []))

    def send_image_batch(self, images: dict, colors=None, cancelled=None, parallel=None) -> bool:
        """先准备同一方向的图，再一口气写入图片/配对灯珠/refresh；不是硬件原子翻帧。

        parallel=True 时每个键走自己的写图连接（见 _writer），各连接同时开始写；
        调用方应先用 writers_idle() 确认上一批已处理完，避免在 SDK 里排队。
        """
        ws, path, session, rotation = self._ws, self.path, self.session, self.image_rotation
        if not ws or not path or not isinstance(images, dict) or not 1 <= len(images) <= 15:
            return False
        parallel = self.parallel_ready if parallel is None else parallel
        try:
            objects = []
            for key, jpeg in images.items():
                if cancelled and cancelled():
                    return False
                if isinstance(key, bool) or not isinstance(key, int) or not 1 <= key <= 15 \
                        or not isinstance(jpeg, bytes) or len(jpeg) > 64 * 1024:
                    return False
                if rotation % 360:
                    jpeg = self._rotate_jpeg(jpeg, rotation)
                objects.append({"event": "setKeyImgData", "path": path,
                                "payload": {"keyIndex": key, "imgData": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}})
            if parallel and colors is None and self.parallel_ready:
                if ((cancelled and cancelled()) or ws is not self._ws or path != self.path
                        or session != self.session or rotation != self.image_rotation):
                    return False
                ok = True
                for obj in objects:
                    ok = self._send_key(obj["payload"]["keyIndex"],
                                        [obj, {"event": "refresh", "path": path, "payload": {}}],
                                        max_pending=0) and ok
                return ok
            if colors is not None:
                if not isinstance(colors, (list, tuple)) or len(colors) != 24 or any(
                        not isinstance(rgb, (list, tuple)) or len(rgb) != 3 or any(
                            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in rgb)
                        for rgb in colors):
                    return False
                objects.append({"event": "setSingleLedColor", "path": path, "payload": {"colors": colors}})
            objects.append({"event": "refresh", "path": path, "payload": {}})
            if ((cancelled and cancelled()) or ws is not self._ws or path != self.path
                    or session != self.session or rotation != self.image_rotation):
                return False
            ws.send_json_batch(objects)
            return True
        except Exception:  # noqa: BLE001
            return False

    def refresh(self) -> bool:
        return self._send("refresh", {})

    # -- 原生 GIF（SDK 内部按帧率循环推送，不占主机带宽） -------------------- #
    def set_key_gif(self, key: int, path: str) -> bool:
        if self.image_rotation % 360:
            try:
                path = self._rotate_gif(path)
            except Exception:  # noqa: BLE001
                pass
        if not self.path:
            return False
        # 和该键的静态图同一条连接：先后顺序有保证；SDK 解码 GIF 也能各键并行
        return self._send_key(key, [{"event": "setKeyGif", "path": self.path,
                                     "payload": {"keyIndex": key, "imagePath": os.path.normpath(path)}}])

    def clear_key_gif(self, key: int) -> bool:
        if not self.path:
            return False
        return self._send_key(key, [{"event": "clearKeyGif", "path": self.path, "payload": {"keyIndex": key}}])

    def start_gif_loop(self) -> bool:
        # 等各键 GIF 都登记完再统一开始，保持共同时钟
        ok = self._barrier("startGifLoop", {})
        self.gif_running = self.gif_running or ok
        return ok

    def stop_gif_loop(self) -> bool:
        ok = self._barrier("stopGifLoop", {})
        if ok:
            self.gif_running = False
        return ok

    def clear_keys(self) -> bool:
        return self._barrier("clearAllIcon", {})

    def set_screen_brightness(self, value: int) -> bool:
        return self._send("setBrightness", {"brightness": max(0, min(100, int(value)))})

    def set_led_brightness(self, value: int) -> bool:
        return self._send("setLEDBrightness", {"brightness": max(0, min(100, int(value)))})

    def set_leds(self, colors) -> bool:
        payload = {"colors": [[int(c[0]), int(c[1]), int(c[2])] for c in colors]}
        return self._send("setSingleLedColor", payload)

    def clear_leds(self) -> bool:
        """同时清零SDK单珠缓存与全灯颜色；两条消息原序连续发送，不改亮度。"""
        ws, path, session = self._ws, self.path, self.session
        if not ws or not path:
            return False
        messages = [
            {"event": "setSingleLedColor", "path": path, "payload": {"colors": [[0, 0, 0] for _ in range(24)]}},
            {"event": "setLEDColor", "path": path, "payload": {"r": 0, "g": 0, "b": 0}},
        ]
        try:
            if ws is not self._ws or path != self.path or session != self.session:
                return False
            ws.send_json_batch(messages)
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_all_leds(self, r: int, g: int, b: int) -> bool:
        return self._send("setLEDColor", {"r": r, "g": g, "b": b})

    def reset_led_color(self) -> bool:
        return self._send("resetLEDColor", {})

    def set_key_brightness(self, value: int) -> bool:
        return self.set_screen_brightness(value)

    def clear_queue(self) -> bool:
        return True

    def change_mode(self, mode: int) -> bool:
        return True

    def wakeup(self) -> bool:
        return True

    def resubscribe_input(self) -> bool:
        """重新订阅按键事件。

        设备自己进入休眠后，SDK 那边的读取可能停掉；重发一次 read 能把通道拉回来，
        比整条连接重连温和得多（重连会闪一下屏）。
        """
        return self._send("read", {})

    def probe_alive(self, timeout: float = 1.2) -> bool:
        """问一句 gifLoopStatus，能收到回执说明这条连接还能收发。

        电脑休眠后 SDK 连接常常变成「能发不能收」：写图/写灯照常（所以灯还亮着），
        但按键读不回来。这种半死状态 check_present() 看不出来，得真问一次。
        """
        if not self.gif_native:
            return True                     # 老固件不支持这个事件，没法用它探活
        self._gif_status.clear()
        if not self._send("gifLoopStatus", {}):
            return False
        return self._gif_status.wait(timeout)

    def get_firmware(self) -> str:
        return self.firmware
