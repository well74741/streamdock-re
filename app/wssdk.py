"""极简 WebSocket 客户端 + StreamDock WebSocket SDK 设备封装。

不依赖第三方库：手写握手与帧编解码，够用即可（只收发文本帧）。
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

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            raise ConnectionError("未连接")
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
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

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
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def recv_loop(self) -> None:
        try:
            while True:
                opcode, payload = self._read_frame()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_frame(payload, opcode=0xA)
                    continue
                if opcode in (0x1, 0x2, 0x0):
                    if self.on_message:
                        self.on_message(payload.decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
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

    # -- 进程/连接 ---------------------------------------------------------- #
    def _start_server(self) -> bool:
        if port_open(self.port):
            return True
        if not self.exe or not os.path.isfile(self.exe):
            self.error = "找不到 StreamDockWebsocketSDK.exe"
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
                ws = WSClient("127.0.0.1", self.port)
                ws.on_message = self._on_message
                ws.on_close = self._on_close
                ws.connect()
                self._ws = ws
                self._dev_ready.clear()
                threading.Thread(target=ws.recv_loop, daemon=True).start()
                if not self._dev_ready.wait(8):
                    self.error = "SDK 已连上，但没收到设备就绪事件"
                    return False
                # 开始监听按键
                self._send("read", {})
                return True
            except Exception as exc:  # noqa: BLE001
                self.error = f"连接 SDK 失败：{exc}"
                return False

    def disconnect(self) -> None:
        with self._lock:
            self.connected = False
            if self._ws:
                self._ws.close()
                self._ws = None
            if self._proc:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                self._proc = None

    def _on_close(self) -> None:
        self.connected = False

    # -- 消息 --------------------------------------------------------------- #
    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        event = msg.get("event")
        payload = msg.get("payload") or {}
        if event == "deviceDidConnect":
            self.path = msg.get("path") or payload.get("path") or ""
            self.firmware = payload.get("firmwareVersion") or ""
            self.product = payload.get("product") or ""

            def _hex(value, default):
                try:
                    return int(str(value), 16) if isinstance(value, str) else int(value or default)
                except (TypeError, ValueError):
                    return default

            self.info = {
                "vid": f"0x{_hex(payload.get('vendorID'), 0x6603):04X}",
                "pid": f"0x{_hex(payload.get('productID'), 0x1012):04X}",
                "serial": str(payload.get("serialNumber") or ""),
                "firmware": self.firmware,
                "keys": 15,
                "leds": 24,
                "path": self.path,
                "product": self.product,
            }
            self.connected = True
            self.error = ""
            self._dev_ready.set()
        elif event == "deviceDidDisconnect":
            self.connected = False
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
        return self.connected

    def mark_lost(self, reason: str = "") -> None:
        self.error = reason
        self.connected = False

    def _send(self, event: str, payload: dict) -> bool:
        try:
            if not self._ws or not self.path:
                return False
            self._ws.send_json({"event": event, "path": self.path, "payload": payload})
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- 设备操作（和旧接口同名） -------------------------------------------- #
    def send_image(self, key: int, jpeg: bytes) -> bool:
        b64 = base64.b64encode(jpeg).decode()
        return self._send("setKeyImgData", {
            "keyIndex": key,
            "imgData": f"data:image/jpeg;base64,{b64}",
        })

    def refresh(self) -> bool:
        return self._send("refresh", {})

    def clear_keys(self) -> bool:
        return self._send("clearAllIcon", {})

    def set_screen_brightness(self, value: int) -> bool:
        return self._send("setBrightness", {"brightness": max(0, min(100, int(value)))})

    def set_led_brightness(self, value: int) -> bool:
        return self._send("setLEDBrightness", {"brightness": max(0, min(100, int(value)))})

    def set_leds(self, colors) -> bool:
        payload = {"colors": [[int(c[0]), int(c[1]), int(c[2])] for c in colors]}
        return self._send("setSingleLedColor", payload)

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

    def get_firmware(self) -> str:
        return self.firmware
