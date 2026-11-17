"""Windows 默认播放设备 WASAPI loopback；只向调用方暴露能量，不录麦克风。

只使用标准库。COM 对象完全属于采集线程；停用会释放对象和临时 PCM 内存。
没有文件写入、网络、设备枚举日志或录音接口。非 Windows 返回 unavailable。
"""
from __future__ import annotations

import collections
import ctypes as C
import math
import os
import struct
import threading
import time
import uuid

BAND_EDGES = (20, 60, 150, 400, 1000, 2500, 6000, 12000, 24000)


class GUID(C.Structure):
    _fields_ = [("data1", C.c_uint32), ("data2", C.c_uint16),
                ("data3", C.c_uint16), ("data4", C.c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class WaveFormat(C.Structure):
    _pack_ = 1
    _fields_ = [("tag", C.c_uint16), ("channels", C.c_uint16),
                ("rate", C.c_uint32), ("byte_rate", C.c_uint32),
                ("align", C.c_uint16), ("bits", C.c_uint16), ("extra", C.c_uint16)]


class AudioError(Exception):
    pass


def _call(ptr, slot, args=(), types=()):
    """COM vtable 调用；HRESULT 使用固定 32 位，避免平台 long 的歧义。"""
    table = C.cast(ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
    fn = C.WINFUNCTYPE(C.c_int32, C.c_void_p, *types)(table[slot])
    result = fn(ptr, *args)
    if result < 0:
        raise AudioError(f"WASAPI 0x{result & 0xffffffff:08x}")
    return result


def _release(ptr):
    if ptr:
        table = C.cast(ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
        C.WINFUNCTYPE(C.c_uint32, C.c_void_p)(table[2])(ptr)


class _Capture:
    """仅在线程内使用；poll_default 只查询 eRender/eMultimedia。"""
    def __init__(self):
        self.ole = C.WinDLL("ole32", use_last_error=True)
        self.ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_uint32]
        self.ole.CoInitializeEx.restype = C.c_int32
        hr = self.ole.CoInitializeEx(None, 0)
        if hr < 0:
            raise AudioError("COM 初始化失败")
        self.initialized = True
        self.ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, C.c_uint32,
                                             C.POINTER(GUID), C.POINTER(C.c_void_p)]
        self.ole.CoCreateInstance.restype = C.c_int32
        self.ole.CoTaskMemFree.argtypes = [C.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.enum = C.c_void_p()
        self.device = C.c_void_p()
        self.client = C.c_void_p()
        self.capture = C.c_void_p()
        self.started = False
        try:
            cls = GUID.of("bcde0395-e52f-467c-8e3d-c4579291692e")
            iid = GUID.of("a95664d2-9614-4f35-a746-de8db63617e6")
            hr = self.ole.CoCreateInstance(C.byref(cls), None, 23, C.byref(iid), C.byref(self.enum))
            if hr < 0:
                raise AudioError("无法读取默认播放设备")
            self.device, self.device_id = self._default()
            iid = GUID.of("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
            _call(self.device, 3, (C.byref(iid), 23, None, C.byref(self.client)),
                  (C.POINTER(GUID), C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)))
            fmt = C.c_void_p()
            _call(self.client, 8, (C.byref(fmt),), (C.POINTER(C.c_void_p),))
            try:
                wave = C.cast(fmt, C.POINTER(WaveFormat)).contents
                self.rate, self.channels, self.align = wave.rate, wave.channels, wave.align
                self.bits = wave.bits
                tag = wave.tag
                if tag == 0xfffe:
                    if wave.extra < 22:
                        raise AudioError("不完整的播放格式")
                    sub = GUID.from_buffer_copy(C.string_at(fmt.value + 24, 16))
                    tag = sub.data1
                self.floating = tag == 3
                if not (8000 <= self.rate <= 384000 and 1 <= self.channels <= 32):
                    raise AudioError("不支持的播放格式")
                if (tag not in (1, 3) or self.bits not in (16, 24, 32, 64)
                        or (self.floating and self.bits not in (32, 64))
                        or (not self.floating and self.bits == 64)
                        or self.align != self.channels * (self.bits // 8)):
                    raise AudioError("不支持的播放格式")
                # 共享模式 + LOOPBACK，只能取播放端；没有捕获端或麦克风回退。
                _call(self.client, 3, (0, 0x00020000, 1_000_000, 0, fmt, None),
                      (C.c_int32, C.c_uint32, C.c_int64, C.c_int64, C.c_void_p, C.c_void_p))
            finally:
                self.ole.CoTaskMemFree(fmt)
            iid = GUID.of("c8adbd64-e71e-48a0-a4de-185c395cd317")
            _call(self.client, 14, (C.byref(iid), C.byref(self.capture)),
                  (C.POINTER(GUID), C.POINTER(C.c_void_p)))
            _call(self.client, 10)
            self.started = True
        except BaseException:
            self.close()
            raise

    def _default(self):
        device = C.c_void_p()
        _call(self.enum, 4, (0, 1, C.byref(device)),
              (C.c_int32, C.c_int32, C.POINTER(C.c_void_p)))
        identity = C.c_void_p()
        try:
            _call(device, 5, (C.byref(identity),), (C.POINTER(C.c_void_p),))
            return device, C.wstring_at(identity)
        except BaseException:
            _release(device)
            raise
        finally:
            if identity:
                self.ole.CoTaskMemFree(identity)

    def changed(self):
        device, identity = self._default()
        _release(device)
        return identity != self.device_id

    def read(self):
        """最多取 32 包，ReleaseBuffer 始终在 finally；原始字节只在当前调用存在。"""
        for _ in range(32):
            size = C.c_uint32()
            _call(self.capture, 5, (C.byref(size),), (C.POINTER(C.c_uint32),))
            if not size.value:
                break
            data, frames, flags = C.c_void_p(), C.c_uint32(), C.c_uint32()
            _call(self.capture, 3, (C.byref(data), C.byref(frames), C.byref(flags), None, None),
                  (C.POINTER(C.c_void_p), C.POINTER(C.c_uint32), C.POINTER(C.c_uint32),
                   C.c_void_p, C.c_void_p))
            try:
                if frames.value > self.rate // 2 or frames.value * self.align > 4 * 1024 * 1024:
                    raise AudioError("播放缓冲区过大")
                if flags.value & 2:  # AUDCLNT_BUFFERFLAGS_SILENT
                    yield [0.0] * min(frames.value, 4096)
                else:
                    pcm = C.string_at(data, frames.value * self.align)
                    yield _mono(pcm, self.channels, self.bits, self.floating)
            finally:
                _call(self.capture, 4, (frames.value,), (C.c_uint32,))

    def close(self):
        try:
            if self.started:
                try:
                    _call(self.client, 11)
                except AudioError:
                    pass
                self.started = False
        finally:
            for field in ("capture", "client", "device", "enum"):
                ptr = getattr(self, field, None)
                _release(ptr)
                setattr(self, field, C.c_void_p())
            if self.initialized:
                self.ole.CoUninitialize()
                self.initialized = False


def _mono(pcm, channels, bits, floating):
    """短暂降混，消除 NaN/Inf；保留最多最近 4096 帧，不返回 PCM 字节。"""
    width = bits // 8
    align = width * channels
    start = max(0, len(pcm) // align - 4096) * align
    mono = []
    fmt = ("<f" if bits == 32 else "<d") if floating else None
    for offset in range(start, len(pcm) - align + 1, align):
        # 各声道能量采用最强声道的有符号样本，避免反相立体声完全抵消。
        values = []
        for ch in range(channels):
            i = offset + ch * width
            if floating:
                v = struct.unpack_from(fmt, pcm, i)[0]
            else:
                v = int.from_bytes(pcm[i:i + width], "little", signed=True) / (1 << (bits - 1))
            values.append(max(-1.0, min(1.0, v)) if math.isfinite(v) else 0.0)
        mono.append(max(values, key=abs))
    return mono


def energy(samples, rate):
    """2048 点 Hann/FFT；输出 0..1 RMS 电平及八个固定频段能量（不输出频谱相位）。"""
    n = 2048
    samples = list(samples)[-n:]
    if not samples:
        return 0.0, [0.0] * 8
    level = min(1.0, math.sqrt(sum(v * v for v in samples) / len(samples)) * 3.0)
    samples = [0.0] * (n - len(samples)) + samples
    values = [complex(v * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))), 0)
              for i, v in enumerate(samples)]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= n:
        root = complex(math.cos(-2 * math.pi / length), math.sin(-2 * math.pi / length))
        half = length // 2
        for start in range(0, n, length):
            w = 1 + 0j
            for k in range(start, start + half):
                a, b = values[k], values[k + half] * w
                values[k], values[k + half] = a + b, a - b
                w *= root
        length *= 2
    powers = [0.0] * 8
    for k in range(1, n // 2):
        hz = k * rate / n
        for band in range(8):
            if BAND_EDGES[band] <= hz < BAND_EDGES[band + 1]:
                powers[band] += abs(values[k]) ** 2
                break
    bands = [min(1.0, math.sqrt(p) * (6.0 / n)) for p in powers]
    return level, bands


class AudioLoopback:
    def __init__(self, capture_factory=None):
        self._factory = capture_factory or _Capture
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = self._empty("stopped")

    @staticmethod
    def _empty(status):
        return {"level": 0.0, "bands": [0.0] * 8, "available": False, "status": status}

    def snapshot(self):
        with self._lock:
            return {**self._state, "bands": list(self._state["bands"])}

    def _publish(self, state):
        with self._lock:
            self._state = state

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._publish(self._empty("starting"))
        self._thread = threading.Thread(target=self._run, name="m18-fx-loopback", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        # 不可在线程未退出时清掉引用/重新开启第二个 COM 线程。
        if not thread or not thread.is_alive():
            self._thread = None
        self._publish(self._empty("stopped"))

    def _run(self):
        if os.name != "nt" and self._factory is _Capture:
            self._publish(self._empty("unavailable"))
            return
        samples = collections.deque(maxlen=2048)
        try:
            while not self._stop.is_set():
                capture = None
                try:
                    capture = self._factory()
                    check_at = tick_at = last_packet = time.monotonic()
                    while not self._stop.wait(0.01):
                        now = time.monotonic()
                        if now - check_at >= 1.0:
                            check_at = now
                            if capture.changed():
                                raise AudioError("默认播放设备已改变")
                        for packet in capture.read():
                            samples.extend(packet)
                            last_packet = now
                            if self._stop.is_set():
                                break
                        if now - tick_at >= 0.05:
                            tick_at = now
                            if now - last_packet > 0.15:
                                samples.clear()
                            level, bands = energy(samples, capture.rate)
                            self._publish({"level": level, "bands": bands, "available": True,
                                           "status": "ok" if level > 0.001 else "silent"})
                except Exception:
                    # 不泄漏设备 ID 或 PCM；拔出/默认端点变化/音频服务重启后重建 COM。
                    self._publish(self._empty("recovering" if capture else "unavailable"))
                finally:
                    samples.clear()
                    if capture:
                        capture.close()
                if self._stop.wait(0.5):
                    break
        finally:
            samples.clear()
            self._publish(self._empty("stopped"))
