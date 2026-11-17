"""离线假设备测试：不导入 server/core，不启动服务，不读取或修改用户数据。

运行：python -B -m unittest discover -s tests -p test_album_render.py -v
测量：python -B tests/test_album_render.py --benchmark
基线：python -B tests/test_album_render.py --benchmark --baseline
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load_mixins(baseline=False):
    # core 导入时会探测/创建真实数据目录，因此只注入本测试所需的常量与纯函数。
    core = types.ModuleType("core")
    constants = dict(KEY_COUNT=15, KEY_SIZE=64, KEY_GAP=27, GRID_COLS=5, GRID_ROWS=3,
                     IMAGE_EXTS=(".png", ".jpg", ".gif"),
                     ALBUM_TRANSITIONS=("none", "fade", "slide", "wipe", "mosaic"))
    for name in ("GIF_CACHE", "CROP_CACHE", "LIB_THUMB_DIR", "LIBRARY_DIR", "PLUGIN_DATA",
                 "SCENE_DIR", "UPLOAD_DIR", "WALLPAPER"):
        constants[name] = "测试未设置路径"
    for name, value in constants.items():
        setattr(core, name, value)
    core.log = lambda *_: None
    core._touch = lambda p: os.utime(p, None)
    core.asset_path = lambda p: p
    core._inside = lambda *_: False
    core.parse_color = lambda s: tuple(bytes.fromhex(s.lstrip("#")))
    core.sweep_dir = lambda *_a, **_k: (0, 0)
    official = types.ModuleType("official")
    official.read_image = lambda p: None
    modules = []
    with patch.dict(sys.modules, {"core": core, "official": official}):
        for name in ("app_media", "app_render"):
            path = ROOT / "app" / f"{name}.py"
            spec = importlib.util.spec_from_file_location(f"offline_{name}", path)
            module = importlib.util.module_from_spec(spec)
            if baseline:
                code = subprocess.check_output(
                    ["git", "-C", str(ROOT), "show", f"HEAD:app/{name}.py"],
                    text=True, encoding="utf-8")
                exec(compile(code, str(path), "exec"), module.__dict__)
            else:
                spec.loader.exec_module(module)
            modules.append(module)
    return tuple(modules)


MEDIA, RENDER = load_mixins()


class FakeDevice:
    """只记录 SDK 调用；返回成功并不模拟 ACK，更不等于设备已完成显示。"""
    connected = True
    gif_native = True
    gif_running = False
    session = 1
    image_rotation = 0

    def __init__(self, delay=0):
        self.calls = []
        self.delay = delay
        self.hook = None
        self.fail_image = False
        self.fail_gif = False

    def record(self, name, *args):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((name, *args))
        if self.hook:
            self.hook(name, args)
        return True

    def send_image(self, key, jpeg):
        self.record("image", key, jpeg)
        return not self.fail_image

    def set_key_gif(self, key, path):
        self.record("gif", key, path)
        return not self.fail_gif

    def clear_key_gif(self, key):
        return self.record("clear", key)

    def start_gif_loop(self):
        self.gif_running = True
        return self.record("start")

    def stop_gif_loop(self):
        self.gif_running = False
        return self.record("stop")

    def set_screen_brightness(self, value):
        return self.record("brightness", value)

    def clear_queue(self):
        return self.record("queue")

    def refresh(self):
        return self.record("refresh")


def make_app(folder, media=MEDIA, render=RENDER, delay=0):
    class OfflineApp(media.MediaMixin, render.RenderMixin):
        def broadcast(self, data):
            self.broadcasts.append(data)

        def _save_config(self):
            pass

        def _save_soon(self, *args):
            pass

        def current_scene(self):
            return self.active_scene or self.custom_scene_name

        def _layer_reload(self):
            pass          # 相册场景的按键层（app_scene_keys）不在本测试范围

    for module in (media, render):
        module.UPLOAD_DIR = str(folder)
        module.GIF_CACHE = str(folder / "gifcache")
    (folder / "gifcache").mkdir(exist_ok=True)
    app = OfflineApp()
    if hasattr(app, "_init_media_render_state"):
        app._init_media_render_state()
    app._album_lock = threading.Lock()  # 基线测量使用，不影响新实现
    app._gif_lock = threading.RLock()
    app.device = FakeDevice(delay)
    app.broadcasts = []
    for key in ("writes_paused", "quiet", "lighting_only", "yielded", "screen_off", "_pushing"):
        setattr(app, key, False)
    app.in_standby = True
    app.preview_mode = "album"
    app.album_items = []
    app.album_index = 0
    app.album_scene_name = ""
    app._album_scene = {}
    app._album_return = None
    app.album_transition = "none"
    app.album_fit = "cover"
    app.album_slideshow = True
    app.album_interval = 8
    app.standby_source = "album"
    app._album_last = 0
    app.scene_rev = 0
    app.wallpaper_gap = 6
    app.wallpaper_zoom = 1.0
    app.wallpaper_ox = app.wallpaper_oy = 0
    app.wallpaper_flip = False
    app.wallpaper_fit = "cover"
    app.rotation = 0
    app.standby_brightness = 100
    app.screen_brightness = 100
    app.selected_key = 0
    app.key_overrides = {}
    app.key_styles = {}
    app.key_actions = {}
    app.key_gestures = {}
    app.plugin_visual = {}
    app.key_previews = {}
    app._num_cache = {}
    app._gif_active = {}
    app._gif_session = 1
    app._black_frame = None
    app._last_paint = app._last_activity = 0
    app.active_scene = "普通场景"
    app.custom_scene_name = "自定义"
    app.clear_before_paint = False
    return app


def make_gif(path, count=10, size=(360, 216)):
    frames = []
    for i in range(count):
        frame = Image.new("RGB", size, (20 + i * 7 % 230, 30 + i * 13 % 220, 60 + i * 17 % 190))
        # 每块有不同颜色与移动图案，避免 Pillow 合并成单帧。
        from PIL import ImageDraw
        draw = ImageDraw.Draw(frame)
        for k in range(15):
            x, y = (k % 5) * size[0] // 5, (k // 5) * size[1] // 3
            draw.rectangle((x + i % 12, y + 4, x + 32 + i % 12, y + 32),
                           fill=(k * 15, i * 19 % 255, 220 - k * 10))
        frames.append(frame)
    frames[0].save(path, "GIF", save_all=True, append_images=frames[1:],
                   duration=[70 + i % 3 * 10 for i in range(count)], loop=0, optimize=False)


def image_color(jpeg):
    with Image.open(io.BytesIO(jpeg)) as image:
        return image.convert("RGB").getpixel((32, 32))


def wait_worker(app, timeout=10):
    deadline = time.monotonic() + timeout
    while app._album_worker_running and time.monotonic() < deadline:
        time.sleep(0.005)
    if app._album_worker_running:
        raise AssertionError("相册工作线程未在超时内结束")


class AlbumTransitionTests(unittest.TestCase):
    """并行写屏下四种过渡都是整板逐帧：每帧一次算完整块画布，而不是按列/按键依次揭开。"""
    size = (5 * 64 + 4 * 27, 3 * 64 + 2 * 27)

    def share(self, frame):
        # 新图(白)所占像素比例
        data = frame.convert("L").getdata()
        return sum(v > 127 for v in data) / len(data)

    def test_every_frame_spans_whole_board_and_progresses(self):
        prev, cur = Image.new("RGB", self.size, "black"), Image.new("RGB", self.size, "white")
        mix = MEDIA.MediaMixin._transition_frame
        noise = MEDIA.MediaMixin._transition_noise(self.size)
        for mode in ("fade", "slide", "wipe", "mosaic"):
            shares = [self.share(mix(mode, prev, cur, i / 8, noise)) if mode != "fade"
                      else mix(mode, prev, cur, i / 8).convert("L").getpixel((0, 0)) / 255 for i in range(1, 8)]
            self.assertEqual(shares, sorted(shares), mode)             # 单调推进
            self.assertGreater(shares[0], 0.0, mode)
            self.assertLess(shares[3], 1.0, mode)                      # 中间帧仍是新旧混合
        # 马赛克：中间帧的新旧方块散布在每个键里，而不是整键整键地出现
        frame = mix("mosaic", prev, cur, 0.5, noise).convert("L")
        mixed_keys = 0
        for row in range(3):
            for col in range(5):
                tile = frame.crop((col * 91, row * 91, col * 91 + 64, row * 91 + 64))
                low, high = tile.getextrema()
                mixed_keys += low < 128 < high
        self.assertGreaterEqual(mixed_keys, 13)
        # 擦除：柔边竖线，同一列上下一致（整列同一时刻），左新右旧
        frame = mix("wipe", prev, cur, 0.5).convert("L")
        x = self.size[0] // 3
        self.assertEqual(len({frame.getpixel((x, y)) for y in range(self.size[1])}), 1)
        self.assertGreater(frame.getpixel((5, 5)), 200)
        self.assertLess(frame.getpixel((self.size[0] - 5, 5)), 50)


class AlbumRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="m18-offline-")
        self.folder = Path(self.temp.name)
        self.app = make_app(self.folder)
        make_gif(self.folder / "movie.gif")
        for name, color in (("red.png", (240, 20, 20)), ("green.png", (20, 240, 20)),
                            ("blue.png", (20, 20, 240))):
            Image.new("RGB", (360, 216), color).save(self.folder / name)

    def tearDown(self):
        wait_worker(self.app)
        self.temp.cleanup()

    def test_cold_hot_native_gif_and_send_measurement(self):
        self.app.album_items = ["movie.gif"]
        self.assertTrue(self.app._push_album())
        cold = dict(self.app._render_perf["album"])
        self.assertEqual(len(self.app._gif_active), 15)
        paths = self.app._album_snapshot()
        # 磁盘热缓存跳过完整帧解码/裁切/编码，而不是只缓存 Image 对象。
        with patch.object(self.app, "_album_frame", side_effect=AssertionError("热缓存重新解码")):
            self.assertEqual(len(self.app._album_gif_paths(paths)), 15)
        with patch.object(self.app, "_atomic_gif", side_effect=AssertionError("热缓存重新编码")):
            self.assertTrue(self.app._push_album())
        warm = self.app._render_perf["album"]
        self.assertGreater(cold["prepareMs"], 0)
        self.assertGreater(warm["sendMs"], 0)
        self.assertGreaterEqual(warm["totalMs"], warm["prepareMs"] + warm["sendMs"])
        self.assertFalse(self.app._pushing)

    def test_native_cache_invalidation_and_sprite_invalidation(self):
        self.app.album_items = ["movie.gif"]
        before = self.app._album_gif_paths(self.app._album_snapshot())
        sprite_before = self.app.album_canvas_gif(index=0)
        for field, value in (("album_fit", "contain"),
                             ("wallpaper_zoom", 1.25), ("rotation", 180),
                             ("standby_brightness", 40)):
            setattr(self.app, field, value)
            after = self.app._album_gif_paths(self.app._album_snapshot())
            self.assertNotEqual(before[1], after[1], field)
            before = after
        self.assertNotEqual(sprite_before, self.app.album_canvas_gif(index=0))
        p = self.folder / "movie.gif"
        make_gif(p, count=12)
        stamp = p.stat()
        os.utime(p, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        self.assertNotEqual(before[1], self.app._album_gif_paths(self.app._album_snapshot())[1])
        for out in self.app._gif_active.values():
            self.assertTrue(Path(out).is_file())

    def test_same_gif_concurrent_conversion_is_single_writer(self):
        self.app.album_items = ["movie.gif"]
        snapshot = self.app._album_snapshot()
        results = []
        with patch.object(self.app, "_atomic_gif", wraps=self.app._atomic_gif) as encoder:
            workers = [threading.Thread(target=lambda: results.append(self.app._album_gif_paths(snapshot)))
                       for _ in range(5)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive())
            self.assertEqual(encoder.call_count, 15)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result == results[0] for result in results))
        for out in results[0].values():
            with Image.open(out) as image:
                self.assertGreater(image.n_frames, 1)
        self.assertFalse(list((self.folder / "gifcache").glob("*.tmp")))

    def test_atomic_failure_keeps_previous_file_and_cleans_temporary(self):
        out = self.folder / "gifcache" / "keep.gif"
        out.write_bytes(b"previous")
        with patch.object(Image.Image, "save", side_effect=OSError("模拟编码失败")):
            with self.assertRaises(OSError):
                self.app._atomic_gif(str(out), [Image.new("RGB", (64, 64))], [100])
        self.assertEqual(out.read_bytes(), b"previous")
        self.assertFalse(list(out.parent.glob("*.tmp")))

    def test_source_cache_reuses_bytes_but_has_independent_clock(self):
        path = str(self.folder / "movie.gif")
        first = self.app._load_image_source(path)
        with patch.object(MEDIA.Image, "open", side_effect=AssertionError("热缓存又读取源文件")):
            second = self.app._load_image_source(path)
        self.assertIs(first["frames"], second["frames"])
        first["idx"], first["next"] = 4, 100
        self.assertEqual(second["idx"], 0)
        self.assertEqual(second["next"], 0)
        make_gif(self.folder / "movie.gif", count=11)
        self.assertIsNot(self.app._load_image_source(path)["frames"], first["frames"])

    def test_caches_have_entry_and_byte_limits(self):
        for i in range(103):
            path = self.folder / f"static_{i}.png"
            Image.new("RGB", (16, 16), (i, 30, 60)).save(path)
            self.app._load_image_source(str(path))
        self.assertLessEqual(len(self.app._image_source_cache), 96)
        self.assertLessEqual(sum(v[1] for v in self.app._image_source_cache.values()), 32 * 1048576)
        from collections import OrderedDict
        cache = OrderedDict()
        for i in range(5):
            self.app._cache_put(cache, i, b"value", 60, 100, 3)
        self.assertEqual(list(cache), [4])
        self.app._cache_put(cache, "oversized", b"value", 101, 100, 3)
        self.assertNotIn("oversized", cache)

    def test_first_frame_preview_does_not_decode_whole_gif(self):
        self.app.album_items = ["movie.gif"]
        with patch.object(self.app, "_fit_canvas", wraps=self.app._fit_canvas) as fitter:
            self.assertIsNotNone(self.app._album_canvas(0))
            self.assertEqual(fitter.call_count, 1)

    def test_rapid_switch_coalesces_to_latest_one_worker(self):
        self.app.album_items = ["red.png", "green.png", "blue.png"]
        entered, release = threading.Event(), threading.Event()
        original = self.app._album_frame
        counter = [0]

        def blocked(*args, **kwargs):
            counter[0] += 1
            if counter[0] == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(self.app, "_album_frame", side_effect=blocked):
            self.app._queue_album_push()
            self.assertTrue(entered.wait(5))
            for i in range(51):
                self.app.album_goto(index=i % 3)
            self.app.album_goto(index=2)
            release.set()
            wait_worker(self.app)
        images = [call for call in self.app.device.calls if call[0] == "image"]
        self.assertEqual(len(images), 15)
        self.assertTrue(all(image_color(call[2])[2] > 200 for call in images))
        self.assertEqual(counter[0], 2)
        self.assertIsNone(self.app._album_pending)

    def test_new_request_cancels_remaining_old_send(self):
        self.app.album_items = ["red.png", "blue.png"]
        hit = [False]

        def hook(name, args):
            if name == "image" and not hit[0]:
                hit[0] = True
                self.app.album_goto(index=1)

        self.app.device.hook = hook
        self.app._queue_album_push()
        wait_worker(self.app)
        images = [call for call in self.app.device.calls if call[0] == "image"]
        self.assertEqual(len(images), 16)
        self.assertGreater(image_color(images[0][2])[0], 200)
        self.assertTrue(all(image_color(call[2])[2] > 200 for call in images[1:]))

    def test_cancel_inflight_two_lane_encoding_has_no_stale_device_writes(self):
        self.app.album_items = ["movie.gif", "blue.png"]
        entered, release = threading.Event(), threading.Event()
        original = self.app._atomic_gif
        writes = []

        def blocked(out, frames, durations):
            writes.append(out)
            entered.set()
            self.assertTrue(release.wait(5))
            return original(out, frames, durations)

        with patch.object(self.app, "_atomic_gif", side_effect=blocked):
            self.app._queue_album_push()
            self.assertTrue(entered.wait(5))
            self.app.album_goto(index=1)
            release.set()
            wait_worker(self.app)
        self.assertLessEqual(len(writes), 2)  # 已在 Pillow 编码内的任务可完成，排队的旧任务不再编码
        self.assertNotIn("gif", [c[0] for c in self.app.device.calls])
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(images), 15)
        self.assertTrue(all(image_color(c[2])[2] > 200 for c in images))

    def test_exit_during_cold_preparation_does_not_revive_album(self):
        self.app.album_items = ["movie.gif"]
        entered, release = threading.Event(), threading.Event()
        original = self.app._album_gif_paths

        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(self.app, "_album_gif_paths", side_effect=blocked):
            self.app._queue_album_push()
            self.assertTrue(entered.wait(5))
            self.app.album_stop()
            call_count = len(self.app.device.calls)
            release.set()
            wait_worker(self.app)
        self.assertEqual(len(self.app.device.calls), call_count)
        self.assertFalse(self.app.album_active())
        self.assertEqual(self.app._gif_active, {})
        self.assertFalse(self.app._pushing)

    def test_exit_during_gif_commit_no_old_start_after_restore(self):
        self.app.album_items = ["movie.gif"]
        hit = [False]

        def hook(name, args):
            if name == "gif" and not hit[0]:
                hit[0] = True
                self.app.device.hook = None
                self.app.album_stop()

        self.app.device.hook = hook
        self.assertFalse(self.app._push_album())
        self.assertFalse(self.app.album_active())
        self.assertEqual(self.app._gif_active, {})
        self.assertFalse(self.app.device.gif_running)

    def test_scene_revision_invalidates_album_and_keys_preparation(self):
        self.app.album_items = ["movie.gif"]
        request = self.app._album_request()
        self.app.scene_rev += 1
        self.assertFalse(self.app._push_album(_request=request))
        self.assertEqual(self.app.device.calls, [])
        self.app.in_standby = False
        original = self.app._render_key
        hit = [False]

        def changed(key):
            if not hit[0]:
                self.app.scene_rev += 1
                hit[0] = True
            return original(key)

        with patch.object(self.app, "_render_key", side_effect=changed):
            self.assertFalse(self.app.apply_keys())
        self.assertEqual(self.app.device.calls, [])

    def test_push_guards_and_device_failures(self):
        self.app.album_items = ["red.png"]
        for attr in ("screen_off", "quiet", "writes_paused", "lighting_only", "yielded"):
            setattr(self.app, attr, True)
            self.assertFalse(self.app._push_album(), attr)
            setattr(self.app, attr, False)
        self.app.device.fail_image = True
        self.assertFalse(self.app._push_album())
        self.assertFalse(self.app._render_perf["album"]["ok"])
        self.app.device.fail_image = False
        self.app.device.gif_native = False
        self.assertTrue(self.app._push_album())

    def test_all_transitions_keep_final_frame_and_throttle(self):
        self.app.album_items = ["blue.png"]
        prev = Image.new("RGB", self.app._canvas_size(), (240, 20, 20))
        for mode, count in (("none", 15), ("fade", 45), ("slide", 45), ("wipe", 30), ("mosaic", 30)):
            self.app.album_transition = mode
            self.app.device.calls.clear()
            with patch.object(MEDIA.time, "sleep") as sleeper:
                self.assertTrue(self.app._push_album(prev))
            images = [call for call in self.app.device.calls if call[0] == "image"]
            self.assertEqual(len(images), count, mode)
            self.assertEqual([call[1] for call in images[-15:]], list(range(1, 16)))
            self.assertTrue(all(image_color(call[2])[2] > 200 for call in images[-15:]))
            if mode in ("fade", "slide"):
                self.assertEqual([c.args[0] for c in sleeper.call_args_list].count(0.16), 2)
            if mode in ("wipe", "mosaic"):
                self.assertIn(0.14, [c.args[0] for c in sleeper.call_args_list])

    def test_transition_cancellation_drops_remaining_intermediate_frames(self):
        self.app.album_items = ["red.png", "blue.png"]
        self.app.album_index = 1
        self.app.album_transition = "fade"
        prev = Image.new("RGB", self.app._canvas_size(), (240, 20, 20))
        interrupted = [False]

        def interrupt(seconds):
            if seconds == 0.16 and not interrupted[0]:
                interrupted[0] = True
                self.app.album_goto(index=0)

        with patch.object(MEDIA.time, "sleep", side_effect=interrupt):
            self.assertFalse(self.app._push_album(prev))
        wait_worker(self.app)
        images = [c for c in self.app.device.calls if c[0] == "image"]
        self.assertEqual(len(images), 30)  # 旧过渡只发一帧；新目标完整 15 键
        self.assertTrue(all(image_color(c[2])[0] > 200 for c in images[-15:]))

    def test_blank_and_reconnect_cancel_old_task(self):
        self.app.album_items = ["movie.gif"]
        request = self.app._album_request()
        self.app.device.session += 1
        self.assertFalse(self.app._push_album(_request=request))
        self.assertEqual(self.app.device.calls, [])
        request = self.app._album_request()
        self.app.screen_off = True
        self.assertTrue(self.app.blank_screen())
        count = len(self.app.device.calls)
        self.assertFalse(self.app._push_album(_request=request))
        self.assertEqual(len(self.app.device.calls), count)
        self.assertEqual(self.app.device.calls[-1], ("brightness", 0))

    def test_gif_send_failure_is_not_marked_active_or_started(self):
        self.app.album_items = ["movie.gif"]
        self.app.device.fail_gif = True
        self.assertFalse(self.app._push_album())
        self.assertEqual(self.app._gif_active, {})
        self.assertNotIn("start", [c[0] for c in self.app.device.calls])

    def test_same_album_keepalive_does_not_reset_native_clock(self):
        self.app.album_items = ["movie.gif"]
        self.assertTrue(self.app._push_album())
        self.app.device.calls.clear()
        before = len(self.app.broadcasts)
        clock = self.app._album_last
        self.assertTrue(self.app._push_album())
        self.assertEqual(self.app._album_last, clock)  # 长间隔轮播不能被 25 秒补帧一直延期
        self.assertEqual(len(self.app.device.calls), 16)
        self.assertTrue(all(c[0] in ("image", "refresh") for c in self.app.device.calls))
        self.assertEqual(len(self.app.broadcasts), before)

    def test_http_explicit_index_without_importing_or_starting_server(self):
        import ast
        import urllib.parse
        import warnings
        # server 的其它旧字符串含转义警告；只取 GET 方法，不运行那些代码。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse((ROOT / "app" / "server.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
        namespace = {"APP": self.app, "urllib": urllib}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "离线路由", "exec"), namespace)
        self.app.album_items = ["red.png", "blue.png"]
        self.app.album_index = 1
        self.app._album_scene_data = lambda scene: {"items": ["red.png", "blue.png"], "index": 1, "fit": "cover"}

        class Request:
            def __init__(self, url):
                self.path = url
                self.wfile = io.BytesIO()
                self.status = None

            def _check_access(self, **kwargs):
                return True

            def send_error(self, status):
                self.status = status

            def send_response(self, status):
                self.status = status

            def send_header(self, *args):
                pass

            def end_headers(self):
                pass

        for index, expected in (("0", 200), ("-1", 404), ("2", 404), ("abc", 400)):
            request = Request(f"/api/album/canvas.gif?scene=test&index={index}")
            namespace["do_GET"](request)
            self.assertEqual(request.status, expected)
            if expected == 200:
                with Image.open(io.BytesIO(request.wfile.getvalue())) as image:
                    self.assertGreater(image.convert("RGB").getpixel((32, 32))[0], 200)

    def setup_key_gifs(self):
        self.app.in_standby = False
        self.app.preview_mode = "wallpaper"
        data = self.app._load_image_source(str(self.folder / "movie.gif"))
        self.app.key_overrides = {1: dict(data), 2: dict(data)}
        self.assertTrue(self.app.apply_keys())
        self.app.device.calls.clear()

    def test_preview_membership_resync_once_not_triple(self):
        self.setup_key_gifs()
        with patch.object(self.app, "_preview_source", return_value=str(self.folder / "red.png")):
            self.assertTrue(self.app.set_key_preview(1, "测试"))
        calls = self.app.device.calls
        self.assertEqual(len([c for c in calls if c[0] == "stop"]), 1)
        self.assertEqual([c[1] for c in calls if c[0] == "gif"], [2])
        self.app.device.calls.clear()
        self.assertTrue(self.app.clear_key_preview())
        calls = self.app.device.calls
        self.assertEqual(len([c for c in calls if c[0] == "stop"]), 1)
        self.assertEqual(sorted(c[1] for c in calls if c[0] == "gif"), [1, 2])
        self.app.device.calls.clear()
        self.assertTrue(self.app._gif_start_all())
        self.assertEqual(self.app.device.calls, [])

    def test_update_key_uploads_each_member_once(self):
        self.setup_key_gifs()
        self.app.key_styles[1] = {"text": "new"}
        self.app._gif_update_key(1)
        self.assertEqual(sorted(c[1] for c in self.app.device.calls if c[0] == "gif"), [1, 2])
        self.assertEqual(len([c for c in self.app.device.calls if c[0] == "start"]), 1)

    def test_sprite_explicit_index_and_hot_cache(self):
        self.app.album_items = ["red.png", "blue.png"]
        self.app.album_index = 1
        data = self.app.album_canvas_gif(index=0)
        with Image.open(io.BytesIO(data)) as image:
            self.assertGreater(image.convert("RGB").getpixel((32, 32))[0], 200)
        self.assertIsNone(self.app.album_canvas_gif(index=-1))
        self.assertIsNone(self.app.album_canvas_gif(index=2))
        with patch.object(self.app, "_album_frame", side_effect=AssertionError("精灵热缓存重新解码")):
            self.assertEqual(data, self.app.album_canvas_gif(index=0))


def benchmark(baseline=False):
    media, render = load_mixins(baseline)
    results = {"version": "HEAD基线" if baseline else "当前优化",
               "device": "假设备，每次SDK调用等待1ms；无ACK/物理完成测量"}
    with tempfile.TemporaryDirectory(prefix="m18-benchmark-") as folder_name:
        folder = Path(folder_name)
        app = make_app(folder, media, render, delay=0.001)
        make_gif(folder / "bench.gif", count=40, size=(640, 384))
        app.album_items = ["bench.gif"]
        for name in ("albumCold", "albumHot"):
            start = time.perf_counter()
            app._push_album()
            results[name] = {"wallMs": round((time.perf_counter() - start) * 1000, 2),
                             "sdkCalls": len(app.device.calls)}
            if hasattr(app, "_render_perf"):
                results[name].update({k: round(v, 2) for k, v in app._render_perf["album"].items()
                                     if k.endswith("Ms")})
            app.device.calls.clear()
        # 再测真实换组后的编码热缓存，和「同图补帧」的幂等优化区分开。
        (folder / "other.gif").write_bytes((folder / "bench.gif").read_bytes())
        app.album_items = ["bench.gif", "other.gif"]
        app.album_index = 1
        app._push_album()
        app.device.calls.clear()
        app.album_index = 0
        start = time.perf_counter()
        app._push_album()
        results["albumDifferentGroupHot"] = {"wallMs": round((time.perf_counter() - start) * 1000, 2),
                                               "sdkCalls": len(app.device.calls)}
        if hasattr(app, "_render_perf"):
            results["albumDifferentGroupHot"].update({k: round(v, 2) for k, v in app._render_perf["album"].items()
                                                      if k.endswith("Ms")})
        app.device.calls.clear()
        for name in ("spriteCold", "spriteHot"):
            start = time.perf_counter()
            data = app.album_canvas_gif()
            results[name] = {"wallMs": round((time.perf_counter() - start) * 1000, 2), "bytes": len(data)}
        app.in_standby = False
        app.preview_mode = "wallpaper"
        for name in ("sceneCold", "sceneHot"):
            start = time.perf_counter()
            # 模拟场景配置加载 15 个相同来源图标，含图片准备和完整发送调用。
            app.key_overrides = {k: app._load_image_source(str(folder / "bench.gif")) for k in range(1, 16)}
            loaded = time.perf_counter()
            app.apply_keys()
            results[name] = {"wallMs": round((time.perf_counter() - start) * 1000, 2),
                             "sourcePrepareMs": round((loaded - start) * 1000, 2),
                             "sdkCalls": len(app.device.calls)}
            if hasattr(app, "_render_perf"):
                results[name].update({k: round(v, 2) for k, v in app._render_perf["keys"].items()
                                     if k.endswith("Ms")})
            app.device.calls.clear()
        # 普通混合场景：12 个不同静态图 + 3 个不同 GIF 文件，避免只看重复来源的收益。
        mixed = []
        for k in range(1, 16):
            path = folder / (f"mixed_{k}.gif" if k <= 3 else f"mixed_{k}.png")
            if k <= 3:
                path.write_bytes((folder / "bench.gif").read_bytes())
            else:
                Image.new("RGB", (640, 384), (k * 11, 40, 180)).save(path)
            mixed.append(path)
        for name in ("sceneMixedCold", "sceneMixedHot"):
            start = time.perf_counter()
            app.key_overrides = {k: app._load_image_source(str(path)) for k, path in enumerate(mixed, 1)}
            loaded = time.perf_counter()
            app.apply_keys()
            results[name] = {"wallMs": round((time.perf_counter() - start) * 1000, 2),
                             "sourcePrepareMs": round((loaded - start) * 1000, 2),
                             "sdkCalls": len(app.device.calls)}
            if hasattr(app, "_render_perf"):
                results[name].update({k: round(v, 2) for k, v in app._render_perf["keys"].items()
                                     if k.endswith("Ms")})
            app.device.calls.clear()
        # 恢复 15 GIF 组，单独衡量预览引起的整组重传，不混入普通场景。
        app.key_overrides = {k: app._load_image_source(str(folder / "bench.gif")) for k in range(1, 16)}
        app.apply_keys()
        app.device.calls.clear()
        start = time.perf_counter()
        app.key_previews = {1: app._black_jpeg()}
        app._repaint_keys([1])
        # 基线调用方会再额外做一次 resync；新实现只在内部做一次。
        if baseline:
            app._gif_resync()
        results["previewStart"] = {"wallMs": round((time.perf_counter() - start) * 1000, 2),
                                   "sdkCalls": len(app.device.calls),
                                   "gifUploads": sum(c[0] == "gif" for c in app.device.calls)}
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        benchmark("--baseline" in sys.argv)
    else:
        unittest.main()
