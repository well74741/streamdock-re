"""灯效离线假设备测试：不导入 core/server、不接真实 SDK、不碰用户数据。

运行：python -B -m unittest discover -s tests -p 'test_effect*.py' -v
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import io
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "app" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_effects():
    # 只执行 core 中纯几何函数的 AST，不执行任何顶层文件/设备/数据目录逻辑。
    tree = ast.parse((ROOT / "app" / "core.py").read_text(encoding="utf-8"))
    geometry = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_led_geometry")
    namespace = {}
    exec(compile(ast.Module(body=[geometry], type_ignores=[]), "纯几何", "exec"), namespace)
    leds, keys = namespace["_led_geometry"]()
    core = types.ModuleType("core")
    core.LED_POINTS, core.KEY_POINTS = leds, keys
    core.KEY_SIZE, core.KEY_GAP = 64, 27
    for name in ("DATA_DIR", "DEFAULT_LIGHTING", "EDITION", "KEY_COUNT", "LED_COUNT", "MODES",
                 "action_label", "action_value", "hex_to_rgb", "hsv", "log"):
        setattr(core, name, None)
    audio = load("offline_audio_loopback", "audio_loopback.py")
    with patch.dict(sys.modules, {"core": core, "audio_loopback": audio}):
        effects = load("offline_effects", "app_effects.py")
        lighting = load("offline_effect_lighting", "app_lighting.py")
    return effects, audio, lighting


FX, AUDIO, LIGHTING = load_effects()


class FakeAudio:
    def __init__(self):
        self.starts = self.stops = 0
        self.value = {"available": False, "status": "stopped", "level": 0.0, "bands": [0.0] * 8}

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def snapshot(self):
        return {**self.value, "bands": list(self.value["bands"])}


class FakeDevice:
    connected = True
    session = 1

    def __init__(self):
        self.calls = []
        self.fail_image = False
        self.fail_led = False
        self.hook = None

    def call(self, method, *args):
        self.calls.append((method, *args))
        if self.hook:
            self.hook(method, args)
        return True

    def send_image(self, key, data):
        self.call("image", key, data)
        return not self.fail_image

    def refresh(self):
        return self.call("refresh")

    def set_leds(self, colors):
        self.call("leds", colors)
        return not self.fail_led

    def stop_gif_loop(self):
        return self.call("stopGif")


class FakeApp(FX.EffectsMixin):
    def __init__(self):
        self.device = FakeDevice()
        self._paint_lock = threading.RLock()
        self._pushing = False
        self.writes_paused = self.quiet = self.yielded = False
        self.screen_off = self.lighting_only = False
        self._dirty = False
        self._frame_hash = 0
        self.restores = 0
        self.broadcasts = []
        self.init_effects()
        self._effect_audio = FakeAudio()
        # 手动驱动 tick，不启动后台线程，保证测试设备调用精确且无竞态。
        self._effect_threads = [types.SimpleNamespace(join=lambda **_: None)]
        self._effect_origin = "http://127.0.0.1:1"
        self._effect_launch = False

    def apply_keys(self):
        self.restores += 1
        return True

    def broadcast(self, value):
        self.broadcasts.append(value)

    def _to_hw(self, colors):
        return colors

    def _gif_stop_all(self):
        self.device.call("clearTrackedGif")


def jpeg(color="red", size=(64, 64)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=78)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def frame(seq=1, color="red", keys=True):
    return {"seq": seq, "colors": [[10, 20, 30] for _ in range(24)],
            **({"keys": {str(i): jpeg(color) for i in range(1, 16)}} if keys else {})}


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.app.load_effect({"name": "离线", "code": "M18FX.on('frame',()=>{});"})
        self.token = self.app._effect_token

    def tearDown(self):
        self.app.close_effects()

    def test_browser_does_not_inherit_explorer_compatibility_layer(self):
        with patch.dict(os.environ, {"__COMPAT_LAYER": "DetectorsAppHealth", "M18_TEST_SENTINEL": "kept"}):
            with patch.object(FX, "find_effect_browser", return_value="test-edge.exe"), \
                    patch.object(FX.subprocess, "Popen") as launch, \
                    patch.object(FX, "effect_browser_job", return_value=None):
                try:
                    self.app._effect_spawn_host()
                    env = launch.call_args.kwargs["env"]
                    self.assertFalse(any(k.upper() == "__COMPAT_LAYER" for k in env))
                    self.assertEqual(env["M18_TEST_SENTINEL"], "kept")
                    self.assertEqual(os.environ["__COMPAT_LAYER"], "DetectorsAppHealth")
                finally:
                    self.app._effect_proc = None  # Mock 不是真实进程，不走进程结束路径

    def test_active_only_audio_and_restore(self):
        self.assertEqual(self.app._effect_audio.starts, 1)
        self.assertTrue(self.app.effect_active())
        self.app.stop_effect()
        self.assertFalse(self.app.effect_active())
        self.assertEqual(self.app.restores, 1)
        self.assertEqual(self.app._effect_audio.snapshot()["level"], 0)
        self.assertTrue(self.app._dirty)
        self.assertIsNone(self.app._frame_hash)
        self.assertIsNone(self.app.effect_preview())

    def test_restore_hook_owns_scene_recovery(self):
        reasons = []
        self.app._effect_runtime_failed = lambda reason: reasons.append((reason, self.app.effect_active()))
        self.app.stop_effect("timeout", restore=True)
        self.assertEqual(reasons, [("timeout", False)])
        self.assertEqual(self.app.restores, 0)

    def test_tokens_rotate_and_never_exposed(self):
        self.app._effect_log("error", "凭证 " + self.token)
        self.assertNotIn(self.token, str(self.app.effect_logs()))
        self.assertNotIn(self.token, str(self.app.effect_state()))
        for token in ("", "scene-name", None, self.token + "bad"):
            with self.assertRaises(PermissionError):
                self.app.effect_host_request("GET", "/effects/session", token)
        self.app.load_effect({"name": "下一会话", "code": "// 无动作"})
        self.assertNotEqual(self.token, self.app._effect_token)
        with self.assertRaises(PermissionError):
            self.app.receive_effect_frame(self.token, frame())

    def test_geometry_inputs_and_bounded_queue(self):
        config = self.app.effect_host_request("GET", "/effects/session", self.token)
        self.assertEqual((config["width"], config["height"]), (1000, 740))
        self.assertEqual(len(config["keys"]), 18)
        self.assertEqual(len(config["leds"]), 24)
        for key in range(1, 19):
            self.assertTrue(self.app.effect_input(key, True))
            self.assertTrue(self.app.effect_input(key, False))
        poll = self.app.effect_host_request("GET", "/effects/poll", self.token)
        self.assertEqual(len(poll["events"]), 36)
        self.assertEqual(poll["events"][-1]["down"], False)
        self.assertEqual(poll["events"][-1]["x"], FX.KEY_POINTS[18][0])
        for _ in range(400):
            self.app.effect_input(1, True)
        self.assertEqual(len(self.app._effect_events), 128)
        with self.assertRaises(ValueError):
            self.app.effect_input(19, True)
        with self.assertRaises(ValueError):
            self.app.effect_input(1, "down")
        self.app.stop_effect(restore=False)
        self.assertFalse(self.app.effect_input(1, True))

    def test_latest_frames_jpeg_diff_and_screen_fps(self):
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        self.app.receive_effect_frame(self.token, frame(2, "blue"))
        self.assertEqual(self.app._effect_metrics["dropped"], 1)
        self.app._effect_screen_tick(10)
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(images), 15)
        self.assertEqual(images[0][2], base64.b64decode(jpeg("blue").split(",")[1]))
        self.assertTrue(any(c[0] == "stopGif" for c in self.app.device.calls))
        self.app.receive_effect_frame(self.token, frame(3, "red"))
        self.app._effect_screen_tick(10.01)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "image"]), 15)
        self.app._effect_screen_tick(10.1)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "image"]), 15)
        self.app._effect_screen_tick(10.25)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "image"]), 30)
        self.app.receive_effect_frame(self.token, frame(4, "red"))
        self.app._effect_screen_tick(10.5)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "image"]), 30)
        self.assertFalse(self.app._pushing)

    def test_partial_screen_update_preserves_other_keys(self):
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        self.app._effect_screen_tick(10)
        self.app.device.calls.clear()
        update = frame(2, "blue")
        update["keys"] = {k: v for k, v in update["keys"].items() if 6 <= int(k) <= 10}
        self.app.receive_effect_frame(self.token, update)
        self.assertEqual(len(self.app._effect_images), 15)
        self.app._effect_screen_tick(10.25)
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual({c[1] for c in images}, set(range(6, 11)))
        self.assertEqual(len(images), 5)

    def test_budget_waits_between_batches_not_between_keys(self):
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        sent_at = []
        def record(method, _args):
            if method == "image":
                sent_at.append(time.monotonic())
                self.app._effect_last_led = 0
                self.app._effect_led_hash = None
                self.app._effect_led_frame()  # 屏幕批次绝不能回写旧LED快照
        self.app.device.hook = record
        self.app._effect_screen_tick(10)
        self.assertEqual(len(sent_at), 15)
        self.assertLess(sent_at[-1] - sent_at[0], 0.03)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "leds"]), 0)
        self.app._effect_led_frame()
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "leds"]), 1)
        self.assertAlmostEqual(self.app._effect_next_batch, 10.25)
        self.app.receive_effect_frame(self.token, frame(2, "blue"))
        self.app._effect_screen_tick(10.1)
        self.assertEqual(len(sent_at), 15)
        self.app._effect_screen_tick(10.25)
        self.assertEqual(len(sent_at), 30)
        self.assertFalse(self.app._effect_pushing)

    def test_black_is_retried_and_old_screen_snapshot_cannot_relight(self):
        class Device(FakeDevice):
            def __init__(self):
                super().__init__()
                self.hardware = [[0,0,0] for _ in range(24)]
                self.clears = 0
            def set_leds(self, colors):
                ok = super().set_leds(colors)
                if ok: self.hardware = [list(rgb) for rgb in colors]
                return ok
            def clear_leds(self):
                self.clears += 1
                self.call('clearLeds', self.clears)
                if self.clears > 1: self.hardware = [[0,0,0] for _ in range(24)]
                return True  # 第一条清零模拟被设备吞掉，但socket返回发送成功
        self.app.device = Device()
        lit = frame(1, 'red')
        lit['colors'] = [[0,0,0] for _ in range(24)]
        lit['colors'][23] = [20,40,90]
        with patch.object(FX.time, 'monotonic', return_value=10):
            self.app.receive_effect_frame(self.token, lit)
            self.app._effect_led_frame()
        with patch.object(FX.time, 'monotonic', return_value=10.1):
            self.app.receive_effect_frame(self.token, {'seq':2, 'colors':[[0,0,0] for _ in range(24)]})
            self.app._effect_led_frame()
        self.assertEqual(self.app.device.hardware[23], [20,40,90])
        self.app._effect_screen_tick(10.2)  # 延迟的旧红色屏幕快照仍在，但它不再写灯珠
        with patch.object(FX.time, 'monotonic', return_value=10.23): self.app._effect_led_frame()
        self.assertTrue(all(rgb == [0,0,0] for rgb in self.app.device.hardware))
        stale = frame(3, 'blue')
        stale['colors'] = [[0,0,0] for _ in range(24)]
        stale['screenColors'] = lit['colors']
        self.app.receive_effect_frame(self.token, stale)
        self.app._effect_screen_tick(10.5)
        with patch.object(FX.time, 'monotonic', return_value=10.5): self.app._effect_led_frame()
        self.assertTrue(all(rgb == [0,0,0] for rgb in self.app.device.hardware))
        clears = self.app.device.clears
        with patch.object(FX.time, 'monotonic', return_value=10.9): self.app._effect_led_frame()
        self.assertEqual(self.app.device.clears, clears)
        with patch.object(FX.time, 'monotonic', return_value=11.6): self.app._effect_led_frame()
        self.assertEqual(self.app.device.clears, clears + 1)
        self.app.stop_effect(restore=False)
        with patch.object(FX.time, 'monotonic', return_value=20): self.assertIsNone(self.app._effect_led_frame())
        self.assertEqual(self.app.device.clears, clears + 1)

    def test_screen_socket_batch_does_not_receive_stale_led_colors(self):
        captures = []
        self.app.device.send_image_batch = lambda images, colors, cancelled=None: captures.append(colors) or True
        self.app.receive_effect_frame(self.token, frame(1, 'red'))
        self.app.receive_effect_frame(self.token, {'seq':2, 'colors':[[0,0,0] for _ in range(24)]})
        self.app._effect_screen_tick(10)
        self.assertEqual(captures, [None])
        self.assertIsNone(self.app._effect_led_hash)

    def test_old_plugins_cannot_overwrite_active_effect(self):
        tree = ast.parse((ROOT/'app/app_plugins.py').read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PluginsMixin')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_plugin_flush_loop']
        class Done(Exception): pass
        active = [True]
        step = [0]
        fixture = [None]
        def sleep(_delay):
            step[0] += 1
            if step[0] == 2:
                self.assertEqual(fixture[0].device.calls, [])
                self.assertIn(1, fixture[0]._plugin_dirty)
                active[0] = False
            elif step[0] == 3:
                raise Done()
        ns = {'time': types.SimpleNamespace(sleep=sleep, time=lambda: 1.0), 'KEY_COUNT': 15}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), '插件推图回归', 'exec'), ns)
        app = ns['PluginsMixin']()
        fixture[0] = app
        app.effect_active = lambda: active[0]
        app.device = FakeDevice()
        app._plugin_dirty = {1: 0}
        app._gif_active = {}
        app.clients = []
        for attr in ('in_standby', 'writes_paused', 'quiet', 'lighting_only', 'yielded', 'screen_off'):
            setattr(app, attr, False)
        app._gif_update_key = lambda _key: None
        app._render_key = lambda _key: b'fake-jpeg'
        with self.assertRaises(Done):
            app._plugin_flush_loop()
        self.assertEqual([c[0] for c in app.device.calls], ['image', 'refresh'])

    def test_rotation_invalidates_static_effect_tiles(self):
        tree = ast.parse((ROOT/'app/app_media.py').read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MediaMixin')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'set_rotation']
        ns = {'threading': threading}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), '灯效旋转回归', 'exec'), ns)
        self.app._num_cache = {}
        self.app._gif_active = {}
        self.app._save_config = lambda: None
        self.app._effect_sent = {1: 'already-sent'}
        self.assertTrue(ns['MediaMixin'].set_rotation(self.app, 90))
        self.assertEqual(self.app.device.image_rotation, 90)
        self.assertEqual(self.app._effect_sent, {})

    def test_screen_schedule_counts_work_inside_interval(self):
        clock = [0.0]
        starts = []
        class Waiter:
            def is_set(self):
                return len(starts) >= 5
        self.app._effect_quit = Waiter()
        def paint(_now):
            starts.append(clock[0])
            clock[0] += 0.02
        try:
            self.app._effect_screen_tick = paint
            with patch.object(FX.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(FX.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
                self.app._effect_screen_loop()
            self.assertEqual(len(starts), 5)
            self.assertTrue(all(abs((b - a) - 0.05) < 0.001 for a, b in zip(starts, starts[1:])))
        finally:
            self.app._effect_quit = threading.Event()

    def test_initial_blank_takes_over_even_without_render(self):
        self.assertEqual(len(self.app._effect_images), 15)
        self.app._effect_screen_tick(10)
        self.assertEqual(self.app._effect_metrics["images"], 15)
        self.assertTrue(any(c[0] == "stopGif" for c in self.app.device.calls))
        with Image.open(io.BytesIO(self.app.effect_preview())) as image:
            self.assertEqual(image.size, (500, 370))
            self.assertEqual(image.getpixel((100, 100)), (0, 0, 0))

    def test_slow_device_still_finishes_same_snapshot_for_all_keys(self):
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        seq = [1]
        def slow(name, _):
            if name == "image":
                # 每次慢send都跨过下一图像帧，但本批15键必须固定为开始时的同一快照。
                threading.Event().wait(0.03)
                seq[0] += 1
                self.app.receive_effect_frame(self.token, frame(seq[0], "blue" if seq[0] % 2 == 0 else "red"))
        self.app.device.hook = slow
        self.app._effect_screen_tick(10)
        touched = [c[1] for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(touched), 15)
        self.assertEqual(set(touched), set(range(1, 16)))
        red = base64.b64decode(jpeg("red").split(",")[1])
        self.assertTrue(all(c[2] == red for c in self.app.device.calls if c[0] == "image"))

    def test_new_frame_cannot_mix_pixels_inside_batch(self):
        first = frame(1, "red")
        first["colors"] = [[255, 0, 0] for _ in range(24)]
        second = frame(2, "blue")
        second["colors"] = [[0, 0, 255] for _ in range(24)]
        self.app.receive_effect_frame(self.token, first)
        def hook(name, _):
            if name == "image":
                self.app.device.hook = None
                self.app.receive_effect_frame(self.token, second)
        self.app.device.hook = hook
        self.app._effect_screen_tick(10)
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(images), 15)
        self.assertTrue(all(c[2] == base64.b64decode(jpeg("red").split(",")[1]) for c in images))
        self.assertFalse(any(c[0] == "leds" for c in self.app.device.calls))
        self.app._effect_led_frame()
        self.assertEqual([c for c in self.app.device.calls if c[0] == "leds"][-1][1], second["colors"])
        self.assertEqual(self.app._effect_screen_seq, 1)
        self.app._effect_screen_tick(10.25)
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(images), 30)
        self.assertTrue(all(c[2] == base64.b64decode(jpeg("blue").split(",")[1]) for c in images[15:]))
        self.assertEqual([c for c in self.app.device.calls if c[0] == "leds"][-1][1], second["colors"])

    def test_screen_respects_blocked_and_reconnect(self):
        self.app.receive_effect_frame(self.token, frame())
        for attr in ("writes_paused", "quiet", "yielded", "screen_off", "lighting_only", "_pushing"):
            setattr(self.app, attr, True)
            self.app._effect_screen_tick(10)
            setattr(self.app, attr, False)
            self.assertEqual(self.app.device.calls, [])
        self.app._effect_screen_tick(10)
        self.app.device.session += 1
        self.app._effect_screen_tick(10.25)
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "image"]), 30)

    def test_wake_repaint_invalidates_jpeg_cache_without_session_change(self):
        self.app.receive_effect_frame(self.token, frame())
        self.app._effect_screen_tick(10)
        self.app._effect_screen_tick(10.25)
        self.assertEqual(self.app._effect_metrics["images"], 15)
        self.app.effect_repaint()
        self.app._effect_screen_tick(10.5)
        self.assertEqual(self.app._effect_metrics["images"], 30)

    def test_failed_sends_are_retried(self):
        self.app.receive_effect_frame(self.token, frame())
        self.app.device.fail_image = True
        self.app._effect_screen_tick(10)
        self.assertEqual(self.app._effect_sent, {})
        self.app.device.fail_image = False
        self.app._effect_screen_tick(10.25)
        self.assertEqual(len(self.app._effect_sent), 15)

    def test_leds_have_independent_limit_and_pushing_gate(self):
        self.app.receive_effect_frame(self.token, frame(keys=False))
        with patch.object(FX.time, "monotonic", return_value=10):
            self.app._effect_led_frame(push=False)
            self.assertEqual(self.app.device.calls, [])
            self.app._effect_led_frame()
            self.app._effect_colors[0] = [1, 2, 3]
            self.app._effect_led_frame()
        self.assertEqual(len(self.app.device.calls), 1)
        self.app._pushing = True
        with patch.object(FX.time, "monotonic", return_value=10.1):
            self.app._effect_led_frame()
        self.assertEqual(len(self.app.device.calls), 1)
        self.app._pushing = False
        with patch.object(FX.time, "monotonic", return_value=10.1):
            self.app._effect_led_frame()
        self.assertEqual(len(self.app.device.calls), 2)

    def test_independent_led_thread_and_shutdown_are_bounded(self):
        self.app.stop_effect(restore=False)
        self.app._effect_threads = []
        self.app.lighting_only = True  # 隔离LED频率测量；图片_pushing让位在另一个测试覆盖。
        self.app.start_effects("http://127.0.0.1:1", launch_host=False)
        self.assertEqual(len(self.app._effect_threads), 3)
        self.app.load_effect({"name": "线程", "code": "// 不执行用户代码"})
        stamps = []
        self.app.device.hook = lambda name, _: stamps.append(time.monotonic()) if name == "leds" else None
        deadline = time.monotonic() + 0.7
        value = 1
        while time.monotonic() < deadline:
            with self.app._effect_lock:
                self.app._effect_colors[0] = [value, 0, 0]
            value += 1
            threading.Event().wait(0.02)
        self.app.close_effects()
        self.assertGreaterEqual(len(stamps), 12)
        self.assertLessEqual(len(stamps), 19)
        self.assertTrue(all(b - a >= 0.039 for a, b in zip(stamps, stamps[1:])))
        self.assertEqual(self.app._effect_threads, [])
        self.assertFalse(self.app.effect_active())

    def test_lighting_compute_returns_effect_without_writing(self):
        self.app.receive_effect_frame(self.token, frame(keys=False))
        result = LIGHTING.LightingMixin._compute(self.app, 0)
        self.assertEqual(result[0], [10, 20, 30])
        result[0][0] = 99
        self.assertEqual(self.app._effect_colors[0][0], 10)
        self.assertEqual(self.app.device.calls, [])

    def test_rejects_unbounded_or_invalid_frames(self):
        bad = [dict(frame(), seq=True), dict(frame(), seq=0), dict(frame(), colors=[]),
               dict(frame(), colors=[[256, 0, 0]] * 24), dict(frame(), colors=[[True, 0, 0]] * 24),
               dict(frame(), keys={"16": jpeg()}), dict(frame(), keys={"1": jpeg(size=(128, 64))}),
               dict(frame(), keys={"1": "data:image/jpeg;base64,broken"}),
               dict(frame(), preview=jpeg(size=(1000, 740)))]
        for value in bad:
            with self.assertRaises(ValueError):
                self.app.receive_effect_frame(self.token, value)
        self.app.receive_effect_frame(self.token, frame(keys=False))
        with self.assertRaises(ValueError):
            self.app.receive_effect_frame(self.token, frame(keys=False))
        for seq in range(2, 31):          # 宿主25fps，后端每秒最多30个请求
            self.app.receive_effect_frame(self.token, frame(seq, keys=False))
        with self.assertRaises(ValueError):
            self.app.receive_effect_frame(self.token, frame(31, keys=False))

    def test_preview_log_bounds_and_error_stops(self):
        value = frame(keys=False)
        value["preview"] = jpeg(size=(500, 370))
        self.app.receive_effect_frame(self.token, value)
        self.assertIsInstance(self.app.effect_preview(), bytes)
        for i in range(220):
            self.app._effect_log("info", str(i))
        self.assertEqual(len(self.app.effect_logs(limit=400)), 200)
        since = self.app.effect_logs()[-2]["seq"]
        self.assertEqual(len(self.app.effect_logs(since=since)), 1)
        self.app.effect_host_request("POST", "/effects/error", self.token, {"message": "代码失败"})
        self.assertFalse(self.app.effect_active())
        self.assertEqual(self.app.effect_state()["status"], "error")
        self.assertEqual(self.app.restores, 1)

    def test_watchdog_closes_stalled_session(self):
        self.app._effect_started = time.monotonic() - 11
        thread = threading.Thread(target=self.app._effect_watch_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while self.app.effect_active() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        self.app._effect_quit.set()
        thread.join(2)
        self.assertFalse(self.app.effect_active())
        self.assertEqual(self.app.effect_state()["status"], "timeout")

    def test_assets_require_worker_network_blocking_header(self):
        _, mime, headers = FX.effect_asset("/effects/effect-worker.js")
        self.assertIn("javascript", mime)
        self.assertIn("connect-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'unsafe-eval';", headers["Content-Security-Policy"])
        self.assertIn("worker-src 'none'", headers["Content-Security-Policy"])
        self.assertIsNone(FX.effect_asset("/effects/../../server.py"))


class ParallelDevice(FakeDevice):
    """并行写屏：每键一条写图连接，writers_idle 模拟 SDK 回执。"""
    parallel_writes = True
    gif_native = True
    image_rotation = 0

    def __init__(self):
        super().__init__()
        self.idle = True

    def send_image_batch(self, images, colors=None, cancelled=None):
        if cancelled and cancelled():
            return False
        self.call("batch", tuple(images))
        self.idle = False
        return True

    def writers_idle(self, keys=None):
        return self.idle

    def wait_writers_idle(self, timeout=1.0):
        self.call("waitIdle")
        self.idle = True
        return True


class ParallelEffectTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.app.device = ParallelDevice()
        self.app.load_effect({"name": "并行", "code": "// 无动作"})
        self.token = self.app._effect_token

    def tearDown(self):
        self.app.close_effects()

    def batches(self):
        return [c for c in self.app.device.calls if c[0] == "batch"]

    def test_full_board_waits_for_receipts_not_budget(self):
        self.assertTrue(self.app.effect_state()["parallel"])
        self.assertTrue(self.app.effect_host_request("GET", "/effects/poll", self.token)["parallel"])
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        self.app._effect_screen_tick(10)
        self.assertEqual(len(self.batches()[0][1]), 15)          # 15键同一批
        self.app.receive_effect_frame(self.token, frame(2, "blue"))
        self.app._effect_screen_tick(10.06)                       # 回执未到：不发，不在SDK里排旧帧
        self.assertEqual(len(self.batches()), 1)
        self.app.device.idle = True
        self.app._effect_screen_tick(10.07)                       # 回执到了：不等60张/秒预算(250ms)
        self.assertEqual(len(self.batches()), 2)
        self.assertEqual(len(self.batches()[1][1]), 15)
        self.app.device.idle = True
        self.app.receive_effect_frame(self.token, frame(3, "red"))
        self.app._effect_screen_tick(10.09)                       # 仍保留20fps上限
        self.assertEqual(len(self.batches()), 2)
        self.app._effect_screen_tick(10.13)
        self.assertEqual(len(self.batches()), 3)

    def test_exit_waits_for_writers_before_restore(self):
        order = []
        self.app.device.hook = lambda method, args: order.append(method)
        self.app.apply_keys = lambda: order.append("restore") or True
        self.app.receive_effect_frame(self.token, frame(1))
        self.app._effect_screen_tick(10)
        self.app.stop_effect()
        self.assertLess(order.index("waitIdle"), order.index("restore"))

    def test_without_sdk_receipts_falls_back_to_budget(self):
        self.app.device.gif_native = False
        self.assertFalse(self.app.effect_state()["parallel"])
        self.app.receive_effect_frame(self.token, frame(1, "red"))
        self.app._effect_screen_tick(10)
        self.app.receive_effect_frame(self.token, frame(2, "blue"))
        self.app._effect_screen_tick(10.1)                        # 15张/60每秒 = 250ms 预算
        self.assertEqual(len(self.batches()), 1)


class AudioTests(unittest.TestCase):
    def test_wasapi_render_only_com_release_and_buffer_release(self):
        C = AUDIO.C
        fmt = C.create_string_buffer(struct.pack("<HHIIHHH", 3, 2, 48000, 384000, 8, 32, 0))
        pcm = C.create_string_buffer(struct.pack("<ffff", 0.5, -0.8, 0.1, 0.2))
        identities = [C.create_unicode_buffer("默认播放A"), C.create_unicode_buffer("默认播放B")]
        state = {"defaults": 0, "packets": 0}
        releases, calls = [], []
        def pointer(out, value):
            C.cast(out, C.POINTER(C.c_void_p))[0] = value
        def create(_cls, _outer, _context, _iid, out):
            pointer(out, 1)
            return 0
        ole = types.SimpleNamespace(CoInitializeEx=Mock(return_value=0),
            CoCreateInstance=Mock(side_effect=create), CoTaskMemFree=Mock(), CoUninitialize=Mock())
        def call(ptr, slot, args=(), types=()):
            pid = ptr.value
            calls.append((pid, slot))
            if pid == 1 and slot == 4:
                self.assertEqual(args[:2], (0, 1))  # eRender/eMultimedia，禁止 eCapture
                pointer(args[2], 2 if state["defaults"] == 0 else 6)
                state["defaults"] += 1
            elif pid in (2, 6) and slot == 5:
                pointer(args[0], C.addressof(identities[0 if pid == 2 else 1]))
            elif pid == 2 and slot == 3:
                pointer(args[3], 3)
            elif pid == 3 and slot == 8:
                pointer(args[0], C.addressof(fmt))
            elif pid == 3 and slot == 3:
                self.assertEqual(args[0:2], (0, 0x20000))  # 共享 render loopback
            elif pid == 3 and slot == 14:
                pointer(args[1], 4)
            elif pid == 4 and slot == 5:
                C.cast(args[0], C.POINTER(C.c_uint32))[0] = 2 if state["packets"] == 0 else 0
            elif pid == 4 and slot == 3:
                pointer(args[0], C.addressof(pcm))
                C.cast(args[1], C.POINTER(C.c_uint32))[0] = 2
                C.cast(args[2], C.POINTER(C.c_uint32))[0] = 0
                state["packets"] += 1
            return 0
        with patch.object(C, "WinDLL", return_value=ole, create=True), \
                patch.object(AUDIO, "_call", side_effect=call), \
                patch.object(AUDIO, "_release", side_effect=lambda p: releases.append(p.value) if p else None):
            capture = AUDIO._Capture()
            self.assertEqual(len(list(capture.read())[0]), 2)
            self.assertIn((4, 4), calls)  # ReleaseBuffer
            self.assertTrue(capture.changed())
            capture.close()
            capture.close()
        self.assertEqual(releases, [6, 4, 3, 2, 1])
        self.assertIn((3, 11), calls)  # IAudioClient.Stop
        ole.CoInitializeEx.assert_called_once()
        ole.CoUninitialize.assert_called_once()
        self.assertEqual(ole.CoTaskMemFree.call_count, 3)

    def test_fft_eight_bands_sine_and_silence(self):
        level, bands = AUDIO.energy([0.0] * 2048, 48000)
        self.assertEqual(level, 0)
        self.assertEqual(bands, [0.0] * 8)
        for band, hz in ((1, 100), (3, 700), (5, 4000), (7, 15000)):
            signal = [0.2 * math.sin(2 * math.pi * hz * i / 48000) for i in range(2048)]
            level, bands = AUDIO.energy(signal, 48000)
            self.assertEqual(max(range(8), key=lambda i: bands[i]), band)
            self.assertGreater(level, 0)
            self.assertTrue(all(0 <= v <= 1 for v in bands))

    def test_formats_float_and_integer_are_finite(self):
        data = struct.pack("<ffff", 0.5, -0.8, float("nan"), float("inf"))
        self.assertEqual(AUDIO._mono(data, 2, 32, True), [-0.800000011920929, 0])
        for bits in (16, 24, 32):
            data = ((1 << (bits - 2)).to_bytes(bits // 8, "little", signed=True)) * 2
            self.assertEqual(AUDIO._mono(data, 2, bits, False), [0.5])

    def test_capture_default_switch_rebuild_and_close(self):
        instances = []
        class FakeCapture:
            rate = 48000
            def __init__(self):
                self.closed = False
                self.owner = threading.get_ident()
                instances.append(self)
            def read(self):
                return [[0.1] * 2048]
            def changed(self):
                return len(instances) == 1
            def close(self):
                self.closed = True
                self.closed_on = threading.get_ident()
        audio = AUDIO.AudioLoopback(FakeCapture)
        self.assertEqual(audio.snapshot()["status"], "stopped")
        self.assertEqual(instances, [])
        audio.start()
        deadline = time.monotonic() + 3
        while len(instances) < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        audio.stop()
        self.assertGreaterEqual(len(instances), 2)
        self.assertTrue(all(c.closed and c.owner == c.closed_on for c in instances))
        self.assertEqual(audio.snapshot(), {"level": 0.0, "bands": [0.0] * 8,
                                           "available": False, "status": "stopped"})
        self.assertIsNone(audio._thread)

    def test_unavailable_does_not_fall_back_to_microphone(self):
        calls = []
        def failed():
            calls.append(True)
            raise AUDIO.AudioError("无播放端")
        audio = AUDIO.AudioLoopback(failed)
        audio.start()
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(audio.snapshot()["status"], "unavailable")
        self.assertFalse(audio.snapshot()["available"])
        audio.stop()


if __name__ == "__main__":
    unittest.main()
