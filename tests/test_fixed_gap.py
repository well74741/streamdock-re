"""固定间隙与初始相册 UI 的离线回归；不导入 core/server，不连接真实设备。

运行：python -B -m unittest discover -s tests -p test_fixed_gap.py -v
浏览器验证：M18_TEST_BROWSER=1 python -B -m unittest discover -s tests -p test_fixed_gap.py -v
浏览器使用独立临时配置和本地 HTML，所有 fetch 都由测试桩接管。
"""
from __future__ import annotations

import ast
import copy
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

from PIL import Image

from test_album_render import MEDIA, make_app, make_gif

ROOT = Path(__file__).resolve().parents[1]


def load_selected_class(filename, class_name, methods, namespace):
    """只加载需要的纯方法，避免模块导入时触碰用户数据目录。"""
    path = ROOT / "app" / filename
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[class_name]


class FixedGapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="m18-fixed-gap-")
        self.folder = Path(self.temp.name)
        self.app = make_app(self.folder)
        names = {"Image": Image, "os": os, "shutil": shutil, "uuid": uuid,
                 "CROP_CACHE": str(self.folder / "crop"), "GRID_COLS": 5, "GRID_ROWS": 3,
                 "KEY_COUNT": 15, "KEY_SIZE": 64, "KEY_GAP": 27,
                 "asset_path": lambda p: p, "log": lambda *_: None}
        catalog = load_selected_class("app_catalog.py", "CatalogMixin",
                                      {"_crop_dir", "_crop_canvas", "_crop_visual_index", "crop_preview"}, names)
        self.crop = catalog()
        self.crop._fit_canvas = self.app._fit_canvas

    def tearDown(self):
        self.temp.cleanup()

    def test_core_constant_without_importing_data_directory(self):
        tree = ast.parse((ROOT / "app/core.py").read_text(encoding="utf-8-sig"))
        gap = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "KEY_GAP" for t in n.targets))
        self.assertEqual(ast.literal_eval(gap), 27)

    def test_old_global_and_scene_gap_values_are_ignored(self):
        for old in (None, 0, 6, 64, -999, "损坏的旧值"):
            with self.subTest(gap=old):
                self.app.wallpaper_gap = old
                self.app.album_scene_name = "相册测试"
                self.app._album_scene = {"gap": old}
                self.assertEqual(self.app._gap(old), 27)
                self.assertEqual(self.app._alb_gap(), 27)
                self.assertEqual(self.app._alb_gap_eff(), 27)
                for rotation in (0, 90, 180, 270):
                    self.app.rotation = rotation
                    expected = (246, 428) if rotation % 180 else (428, 246)
                    self.assertEqual(self.app._canvas_size(old), expected)
                    spec = self.app._album_render_spec({"fit": "fill", "gap": old})
                    self.assertEqual(spec["gap"], 27)
                    self.assertEqual(spec["size"], expected)

    def test_static_crop_and_album_use_identical_15_tiles(self):
        source = Image.new("RGB", (428, 246))
        source.putdata([(x % 256, y % 256, (x + y) % 256)
                        for y in range(246) for x in range(428)])
        path = self.folder / "坐标.png"
        source.save(path)
        for old in (0, 6, 64, "损坏的旧值"):
            with self.subTest(gap=old):
                frames, durations, animated = self.crop._crop_canvas(str(path), "fill", old)
                album = self.app._canvas_tiles(source, old)
                self.assertFalse(animated)
                self.assertEqual(durations, [])
                for key in range(1, 16):
                    col, row = (key - 1) % 5, 2 - (key - 1) // 5
                    self.assertEqual(frames[key - 1][0].size, (64, 64))
                    self.assertEqual(frames[key - 1][0].getpixel((0, 0)),
                                     source.getpixel((col * 91, row * 91)))
                    self.assertEqual(frames[key - 1][0].tobytes(), album[key].tobytes())

    def test_crop_preview_reports_fixed_gap_for_static_and_gif(self):
        Image.new("RGB", (428, 246), (30, 100, 220)).save(self.folder / "still.png")
        make_gif(self.folder / "movie.gif", count=3)
        for name in ("still.png", "movie.gif"):
            for old in (0, 64, "损坏的旧值"):
                with self.subTest(file=name, gap=old):
                    result = self.crop.crop_preview(str(self.folder / name), gap=old)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["gap"], 27)
                    self.assertEqual(len(result["tiles"]), 15)
                    self.assertEqual(result["animated"], name.endswith(".gif"))

    def test_album_state_reports_fixed_gap_without_mutating_legacy_scene(self):
        old = {"items": [], "gap": 6, "fit": "cover"}
        self.app._album_scene_data = lambda _name: old
        result = self.app.album_state(scene="相册测试")
        self.assertEqual(result["gap"], 27)
        self.assertFalse(result["gapOwn"])
        self.assertEqual(old["gap"], 6)
        self.assertEqual(self.app.album_state()["gap"], 27)
        self.assertFalse(self.app.album_state()["gapOwn"])

    def test_album_settings_ignores_gap_and_preserves_legacy_fields(self):
        old = {"items": [], "gap": 6, "fit": "cover"}
        self.app._album_scene_edit = lambda _name, fn: fn(old)
        self.app.preview_mode = "wallpaper"
        self.app.album_settings({"gap": "不是数字", "fit": "contain"}, scene="相册测试")
        self.assertEqual(old, {"items": [], "gap": 6, "fit": "contain"})

    def test_old_gap_does_not_change_wallpaper_or_native_gif_cache(self):
        path = self.folder / "movie.gif"
        make_gif(path, count=3)
        self.app.wallpaper_file = str(path)
        self.app.album_items = [path.name]
        initial_wallpaper = self.app._canvas_key()
        initial_gif = self.app._album_gif_paths(self.app._album_snapshot())
        for old in (0, 64):
            self.app.wallpaper_gap = old
            self.app._album_scene["gap"] = old
            self.assertEqual(initial_wallpaper, self.app._canvas_key())
            self.assertEqual(initial_gif, self.app._album_gif_paths(self.app._album_snapshot()))

    def test_load_config_ignores_even_malformed_gap_without_losing_other_settings(self):
        cfg = {"gap": "损坏的旧值", "rotation": 90, "customScene": "离线场景", "wallpaperEnabled": False}
        original = copy.deepcopy(cfg)
        names = {"os": os, "json": json, "shutil": shutil, "core": object(), "KEY_GAP": 27,
                 "LED_COUNT": 24, "STANDBY_MIN": 5, "STANDBY_MAX": 3600,
                 "AUTOBLANK_MIN": 1, "AUTOBLANK_MAX": 3600, "LEGACY_ALBUM_NAMES": (),
                 "log": lambda *_: None, "UPLOAD_DIR": str(self.folder)}
        pages = load_selected_class("app_pages.py", "PagesMixin", {"_load_config"}, names)
        self.app._read_config_file = lambda: cfg
        self.app.wallpaper_enabled = True
        self.app.pages = [{"images": {}, "actions": {}}]
        pages._load_config(self.app)
        self.assertEqual(self.app.wallpaper_gap, 27)
        self.assertEqual(self.app.rotation, 90)
        self.assertEqual(self.app.custom_scene_name, "离线场景")
        self.assertFalse(self.app.wallpaper_enabled)
        self.assertEqual(cfg, original)

    def test_wallpaper_cannot_overwrite_active_effect(self):
        self.app.effect_active = lambda: True
        self.app._cancel_album_push = lambda: self.fail("灯效播放时不应取消相册任务")
        self.assertFalse(self.app._push_canvas("离线测试"))
        self.assertFalse(self.app._push_canvas_locked("离线测试"))
        self.assertEqual(self.app.device.calls, [])

    def test_ui_has_no_gap_controls_or_writeback(self):
        js = (ROOT / "app/ui/app.js").read_text(encoding="utf-8-sig")
        markup = (ROOT / "app/ui/index.html").read_text(encoding="utf-8-sig")
        for control in ("wallpaper-gap", "as-gap", "crop-gap"):
            self.assertNotIn(control, js)
            self.assertNotIn(control, markup)
        self.assertNotIn("pushGap", js)
        self.assertIn("gap: KEY_GAP", js)


@unittest.skipUnless(os.environ.get("M18_TEST_BROWSER") == "1", "设置 M18_TEST_BROWSER=1 启用离线浏览器验证")
class InitialAlbumBrowserTests(unittest.TestCase):
    def test_initial_album_empty_album_and_response_races(self):
        browser = os.environ.get("M18_TEST_BROWSER_EXE") or shutil.which("msedge")
        if not browser:
            candidate = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
            browser = str(candidate) if candidate.is_file() else None
        if not browser:
            self.skipTest("未找到 Edge；可用 M18_TEST_BROWSER_EXE 指定 Chromium 浏览器")
        ui = ROOT / "app/ui"
        album = {"scene": "相册测试", "items": ["一.png", "二.gif"], "index": 0, "active": True,
                 "slideshow": True, "interval": 8, "transition": "fade", "fit": "cover", "gap": 6,
                 "standby": {"items": 0, "index": 0, "interval": 8, "slideshow": True}}
        state = {"device": {"connected": False, "info": {}}, "lighting": {"mode": "off", "speed": 1},
                 "album": album, "activeScene": "相册测试", "screenBrightness": 100, "rotation": 0,
                 "standby": 300, "wallpaper": {"gap": 6}, "page": 0, "mainPages": [0],
                 "pageNames": ["主页"], "events": [], "effect": {"active": False}}
        scenes = [{"name": "相册测试", "sceneType": "album", "album": True, "albumScene": True, "active": True},
                  {"name": "普通场景", "sceneType": "normal", "active": False}]
        for state_delay, scene_delay, empty in ((10, 80, False), (80, 10, False), (10, 80, True)):
            with self.subTest(state_delay=state_delay, scene_delay=scene_delay, empty=empty), tempfile.TemporaryDirectory(prefix="m18-ui-offline-") as folder:
                fixture = copy.deepcopy(state)
                if empty:
                    fixture["album"]["items"] = []
                stub = """
window.__errors = [];
window.addEventListener('error', e => { if (e.message) window.__errors.push(e.message); });
window.addEventListener('unhandledrejection', e => window.__errors.push(String(e.reason)));
console.error = (...a) => window.__errors.push(a.map(String).join(' '));
window.__holdAlbum = false;
const fixture = FIXTURE, scenesFixture = SCENES;
window.fetch = async (url, opts = {}) => {
  let value = {ok:true, items:[], packs:[], plugins:[]};
  let delay = 0;
  if (url === '/api/state') { value = structuredClone(fixture); delay = STATE_DELAY; }
  if (url === '/api/scenes') { value = {ok:true,items:scenesFixture}; delay = SCENE_DELAY; }
  if (url === '/api/album') {
    value = structuredClone(fixture.album);
    if (window.__holdAlbum) await new Promise(resolve => window.__releaseAlbum = resolve);
    else delay = 180;
  }
  if (delay) await new Promise(resolve => setTimeout(resolve, delay));
  return {ok:true, json:async () => value};
};
window.EventSource = class { constructor() { throw new Error('离线测试禁止 SSE'); } };
""".replace("FIXTURE", json.dumps(fixture, ensure_ascii=False)).replace("SCENES", json.dumps(scenes, ensure_ascii=False)).replace("STATE_DELAY", str(state_delay)).replace("SCENE_DELAY", str(scene_delay))
                checks = """
window.__bootPromise.then(async () => {
  const checks = [];
  const check = (value, message) => { if (!value) throw new Error(message); checks.push(message); };
  check(document.body.classList.contains('album-scene'), '启动即识别设备的相册场景');
  check(!$('#album-editor').hidden && $('#editor-body').hidden, '启动即显示相册编辑区');
  check($('#as-count').textContent === `${fixture.album.items.length} 张`, '初始真实图片集不被空占位清掉');
  check(!document.querySelector('#wallpaper-gap,#as-gap,#crop-gap'), '间隙控件全部移除');
  await new Promise(resolve => setTimeout(resolve, 250));
  const savedScenes = sceneItems;
  sceneItems = [];
  renderEditor();
  check(activeAlbumScene() === '相册测试' && document.body.classList.contains('album-scene'), '列表未返回也可从状态识别相册');
  sceneItems = savedScenes;
  selectedKey = 16;
  renderEditor();
  check($('#album-editor').hidden && !$('#editor-body').hidden, '导航键仍可普通编辑');
  selectedKey = null;
  renderEditor();
  check(!$('#album-editor').hidden && $('#sel-label').textContent === '相册场景：相册测试', '导航键返回后恢复相册标题和编辑区');
  window.__holdAlbum = true;
  const pending = reloadAlbumEditor();
  selectedScene = '普通场景';
  renderEditor();
  window.__releaseAlbum();
  await pending;
  check($('#album-editor').hidden && !$('#sel-label').textContent.startsWith('相册场景：'), '旧相册响应不覆盖新选中场景');
  selectedScene = null;
  state.album = {...state.album, active:false, scene:''};
  state.activeScene = '普通场景';
  renderEditor();
  check(!document.body.classList.contains('album-scene'), '切回普通场景移除相册 body 类');
  check(window.__errors.length === 0, `无脚本错误：${window.__errors.join(';')}`);
  document.body.dataset.testResult = 'passed';
  document.body.dataset.testChecks = checks.length;
}).catch(error => { document.body.dataset.testResult = 'failed'; document.body.dataset.testError = String(error); });
"""
                markup = (ui / "index.html").read_text(encoding="utf-8-sig")
                js = (ui / "app.js").read_text(encoding="utf-8-sig")
                js = re.sub(r"\bboot\(\);\s*$", "window.__bootPromise = boot();", js)
                markup = markup.replace("<head>", f'<head><base href="{html.escape(ui.as_uri())}/" />')
                markup = re.sub(r'<script src="app\.js[^\"]*"></script>',
                                lambda _: '<script>' + stub + '</script><script>' + js.replace('</script', '<\\/script') + '</script><script>' + checks + '</script>', markup)
                path = Path(folder) / "offline.html"
                path.write_text(markup, encoding="utf-8")
                result = subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                                         "--disable-background-networking", "--disable-extensions",
                                         "--allow-file-access-from-files", "--host-resolver-rules=MAP * ~NOTFOUND",
                                         f"--user-data-dir={Path(folder) / 'profile'}", "--virtual-time-budget=2500",
                                         "--dump-dom", path.as_uri() + "?noevents"],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                verdict = re.search(r'<body[^>]*data-test-result="([^"]+)"[^>]*>', result.stdout)
                self.assertIsNotNone(verdict, result.stdout[-2000:] + result.stderr[-2000:])
                self.assertEqual(verdict.group(1), "passed", verdict.group(0))


if __name__ == "__main__":
    unittest.main()
