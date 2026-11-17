"""真实 Edge/Chrome Worker 测试；临时 loopback HTTP/浏览器 profile、假设备和假音频。

不调用现有8642服务、不启动SDK、不碰用户数据、不采集真实音频；只结束本测试PID。
无浏览器时明确 skip；CDP/WebSocket 仅标准库，无需 playwright/npm 安装。
"""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from PIL import Image
from test_effects import FX, ROOT, FakeApp, load

WS = load("offline_effect_test_ws", "wssdk.py")


class CDP(WS.WSClient):
    def __init__(self, url):
        info = urlsplit(url)
        super().__init__(info.hostname, info.port)
        self.path = info.path
        self.pending = {}
        self.condition = threading.Condition()
        self.seq = 0
        self.on_message = self.message
        self.on_close = lambda: None

    def connect(self, timeout=5):
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP 握手关闭")
            data += chunk
        header, _, self._buf = data.partition(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n")[0]:
            raise ConnectionError("CDP 握手失败")
        sock.settimeout(None)
        self.sock = sock
        self.thread = threading.Thread(target=self.recv_loop, daemon=True)
        self.thread.start()

    def message(self, value):
        data = json.loads(value)
        if "id" in data:
            with self.condition:
                self.pending[data["id"]] = data
                self.condition.notify_all()

    def command(self, method, params=None, timeout=5):
        with self.condition:
            self.seq += 1
            seq = self.seq
            self.send_json({"id": seq, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            while seq not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("CDP 请求超时：" + method)
                self.condition.wait(remaining)
            result = self.pending.pop(seq)
        if "error" in result:
            raise RuntimeError("CDP 请求失败：" + method)
        return result.get("result", {})

    def evaluate(self, expression):
        value = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in value:
            raise RuntimeError("浏览器求值失败")
        return value["result"].get("value")

    def close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # 不输出 URL/Authorization/测试 token。

    def reply(self, status, data, mime="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != origin.removeprefix("http://"):
            self.reply(403, b"{}")
            return
        if self.path == "/probe.js":
            self.server.probes += 1
            self.reply(200, b"export const value = 1", "text/javascript")
            return
        asset = FX.effect_asset(self.path)
        if self.command == "GET" and asset:
            data, mime, headers = asset
            self.reply(200, data, mime, headers)
            return
        if self.command == "POST" and self.headers.get("Origin") != origin:
            self.reply(403, b"{}")
            return
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        length = int(self.headers.get("Content-Length", 0))
        if length > FX.MAX_EFFECT_BODY:
            self.reply(413, b"{}")
            return
        try:
            body = json.loads(self.rfile.read(length)) if length else None
            value = self.server.app.effect_host_request(self.command, self.path, token, body)
            if self.path == '/effects/frame':
                time.sleep(getattr(self.server, 'frame_reply_delay', 0))
            self.reply(200, json.dumps(value).encode())
        except PermissionError:
            self.reply(403, b"{}")
        except (ValueError, TypeError):
            self.reply(400, b"{}")


@unittest.skipUnless(FX.find_effect_browser(), "未安装 Edge/Chrome，真实 Worker 测试跳过")
class BrowserRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.probes = 0
        cls.server.app = FakeApp()
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.temp = tempfile.TemporaryDirectory(prefix="m18-fx-browser-test-", ignore_cleanup_errors=True)
        cls.proc = None
        cls.job = None
        cls.cdp = None
        try:
            cls.proc = subprocess.Popen([
                FX.find_effect_browser(), "--headless=new", "--disable-gpu", "--no-proxy-server",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-extensions", "--disable-sync", "--mute-audio",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                f"--user-data-dir={cls.temp.name}", "--remote-debugging-port=0", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            cls.job = FX.effect_browser_job(cls.proc)
            active = Path(cls.temp.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 10
            while not active.exists():
                if cls.proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("测试浏览器未启动")
                threading.Event().wait(0.05)
            port = int(active.read_text().splitlines()[0])
            opener = build_opener(ProxyHandler({}))  # CDP只访问本测试本机端口，不走系统代理。
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        pages = json.load(response)
                    page = next(p for p in pages if p["type"] == "page")
                    break
                except (OSError, StopIteration, ValueError):
                    if time.monotonic() > deadline or cls.proc.poll() is not None:
                        raise RuntimeError("测试浏览器CDP未就绪") from None
                    threading.Event().wait(0.05)
            cls.cdp = CDP(page["webSocketDebuggerUrl"])
            cls.cdp.connect()
            cls.cdp.command("Page.enable")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.cdp:
            cls.cdp.close()
        if cls.job:
            cls.job.close()  # 仅自己的浏览器Job子树，根进程已退出也能清掉自己的子进程。
        if cls.proc and cls.proc.poll() is None:
            if not cls.job and os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(cls.proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    cls.proc.kill()
            elif not cls.job:
                cls.proc.terminate()
            try:
                cls.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=3)
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.cdp.command("Page.navigate", {"url": "about:blank"})
        self.app = FakeApp()
        self.server.app = self.app
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.probes = 0
        self.server.frame_reply_delay = 0

    def tearDown(self):
        self.cdp.command("Page.navigate", {"url": "about:blank"})
        self.app.close_effects()

    def wait(self, fn, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if fn():
                return
            threading.Event().wait(0.03)
        self.fail("等待浏览器条件超时；" + str(self.app.effect_state()) + "；" + str(self.app.effect_logs()))

    def launch(self, code):
        # 上个宿主会清掉 fragment；同路径的新 fragment 只是 hash 导航，必须先离开文档。
        self.cdp.command("Page.navigate", {"url": "about:blank"})
        self.app.load_effect({"name": "真实浏览器离线测试", "code": code})
        self.cdp.command("Page.navigate", {"url": self.origin + "/effects/host#" + self.app._effect_token})

    def test_parallel_budget_sends_full_board_every_frame(self):
        from test_effects import ParallelDevice
        self.app.device = ParallelDevice()
        # 每帧整板换色：并行模式下宿主不再被60张/秒预算卡住
        code = """let n = 0; const ctx = M18FX.ctx;
M18FX.on('frame', () => { ctx.fillStyle = ['#f00', '#0f0', '#00f'][n++ % 3]; ctx.fillRect(0, 0, 1000, 740); M18FX.render(); });"""
        self.launch(code)
        self.wait(lambda: self.cdp.evaluate("document.body.dataset.parallel") == "true")
        start = self.app._effect_image_seq
        started = time.monotonic()
        self.wait(lambda: self.app._effect_image_seq >= start + 10, timeout=3)
        elapsed = time.monotonic() - started
        # 预算模式下整板变化只能约4次/秒（10次≈2.5秒）；并行模式应远快于此
        self.assertLess(elapsed, 1.5, f"10 次整板更新用了 {elapsed:.2f}s")

    def test_default_render_preview_and_navigation_wave(self):
        self.launch(None)
        self.wait(lambda: self.app.effect_state()["metrics"]["frames"] >= 3)
        self.assertTrue(self.app.effect_active())
        with Image.open(io.BytesIO(self.app.effect_preview())) as image:
            self.assertEqual(image.size, (500, 370))
        self.assertEqual(len(self.app._effect_images), 15)
        before = self.app.effect_preview()
        self.app.effect_input(18, True)
        self.wait(lambda: self.app.effect_preview() != before)
        self.wait(lambda: any(max(c) > 40 for c in self.app._effect_colors), timeout=2)
        self.app._effect_screen_tick()
        count = len([c for c in self.app.device.calls if c[0] == "image"])
        # 动态帧在批次中更新时，剩余旧图必须被丢弃，而不是硬凑 15 张。
        self.assertGreater(count, 0)
        self.assertLessEqual(count, 15)
        self.assertEqual(self.cdp.evaluate("location.hash"), "")
        self.assertEqual(self.cdp.evaluate("document.body.dataset.status"), "running")
        self.assertEqual(self.app.device.calls[0][0], "clearTrackedGif")

    def test_default_three_row_wave_budget_and_idle_leds(self):
        cold_started = time.monotonic()
        self.launch(None)
        self.wait(lambda: self.app.effect_state()["metrics"]["frames"] >= 4)
        baseline = dict(self.app._effect_images)
        writes = []
        self.app.device.hook = lambda name, args: writes.append((time.monotonic(), args[0])) if name == "image" else None
        threads = [threading.Thread(target=target, daemon=True) for target in
                   (self.app._effect_screen_loop, self.app._effect_led_loop)]
        self.app._effect_threads.extend(threads)
        for thread in threads:
            thread.start()
        self.wait(lambda: len(writes) >= 15)
        cold_ms = (time.monotonic() - cold_started) * 1000
        start = time.monotonic()
        self.app._effect_audio.value = {"available": True, "status": "ok", "level": 0.8, "bands": [0.5] * 8}
        threading.Event().wait(2)
        elapsed = time.monotonic() - start
        timing = self.cdp.evaluate("({poll:document.body.dataset.pollMs,worker:document.body.dataset.workerMs,encode:document.body.dataset.encodeMs,upload:document.body.dataset.uploadMs})")
        print('宿主实际耗时ms：', timing)
        changed = [(t, key) for t, key in writes if t >= start]
        self.assertTrue(changed)
        # v3 波形铺满三排：15 屏都随音频变化；单连接回退模式仍受 60 张/秒预算约束
        self.assertEqual({key for _, key in changed}, set(range(1, 16)))
        self.assertGreater(len(changed) / elapsed / 15, 2.5, "整板应接近单连接预算上限（约4次/屏/秒）")
        self.assertLessEqual(len(changed), int(elapsed * 60) + 15)
        self.assertTrue(all(color == [0, 0, 0] for color in self.app._effect_colors), "仅音频线不能点亮独立底灯")
        pressed_at = time.monotonic()
        self.app.effect_input(18, True)
        self.wait(lambda: any(t >= pressed_at and not 6 <= key <= 10 for t, key in writes), timeout=2)
        hot_ms = (min(t for t, key in writes if t >= pressed_at and not 6 <= key <= 10) - pressed_at) * 1000
        self.wait(lambda: any(max(color) > 40 for color in self.app._effect_colors), timeout=2)
        self.wait(lambda: all(color == [0, 0, 0] for color in self.app._effect_colors), timeout=3)
        print(f"单连接回退模拟发送：{len(changed)/elapsed/15:.1f} 次/屏/秒，总图片 {len(changed)/elapsed:.1f} 张/秒；冷启动全15键 {cold_ms:.0f}ms，热按键首个扩散图 {hot_ms:.0f}ms；无设备ACK。")

    def test_slow_upload_preserves_one_epoch_for_all_screen_tiles(self):
        groups = []
        receive = self.app.receive_effect_frame
        def capture(token, body):
            if body.get('keys'):
                groups.append((body['seq'], body.get('screenSeq'), dict(body['keys']), body.get('screenColors')))
            return receive(token, body)
        self.app.receive_effect_frame = capture
        self.server.frame_reply_delay = 0.12
        self.launch("""
M18FX.on('frame',({frame})=>{
 const color=[[255,0,0],[0,255,0],[0,0,255]][frame%3];
 M18FX.ctx.fillStyle='rgb('+color.join(',')+')';M18FX.ctx.fillRect(0,0,1000,740);
 M18FX.render(M18FX.leds.map(()=>[...color]));
});
""")
        self.wait(lambda: len(groups) >= 3)
        for seq, screen_seq, keys, colors in groups:
            self.assertEqual(screen_seq, seq)
            self.assertEqual(len(keys), 15)
            self.assertEqual(len(set(keys.values())), 1, '同一个批次不能合并不同时间的几行')
            with Image.open(io.BytesIO(base64.b64decode(next(iter(keys.values())).split(',')[1]))) as image:
                pixel = image.getpixel((32,32))
            self.assertTrue(all(abs(pixel[i] - colors[0][i]) < 4 for i in range(3)))
        self.assertTrue(self.app.effect_active())

    def test_audio_and_all_key_down_up_in_separate_realm(self):
        self.app._effect_audio.value = {"available": True, "status": "ok", "level": 0.7, "bands": [0.2] * 8}
        self.launch("""
let value=0;
M18FX.log('REALM '+typeof document+' '+typeof localStorage+' '+location.hash);
M18FX.on('key',e=>{value=e.down?255:0;M18FX.log('KEY '+e.key+' '+e.down);});
M18FX.on('frame',()=>{
 M18FX.ctx.fillStyle='rgb('+value+',0,0)';M18FX.ctx.fillRect(0,0,1000,740);
 M18FX.render(M18FX.leds.map(()=>[value,Math.round(M18FX.audio.level*100),0]));
});
""")
        self.wait(lambda: self.app.effect_state()["metrics"]["frames"] > 1)
        self.assertEqual(self.app._effect_colors[0][1], 70)
        for key in range(1, 19):
            self.app.effect_input(key, True)
            self.app.effect_input(key, False)
        self.app.effect_input(18, True)
        self.wait(lambda: self.app._effect_colors[0][0] == 255)
        self.app.effect_input(18, False)
        self.wait(lambda: self.app._effect_colors[0][0] == 0)
        self.wait(lambda: any("REALM undefined undefined" in e["message"] for e in self.app.effect_logs()))
        self.assertNotIn(self.app._effect_token, str(self.app.effect_logs()))

    def test_worker_csp_blocks_network_and_dynamic_import(self):
        self.launch("""
try { fetch('/probe.js'); } catch(e) { M18FX.log('FETCH_BLOCKED'); }
import('/probe.js').then(()=>M18FX.log('IMPORT_ALLOWED'),()=>M18FX.log('IMPORT_BLOCKED'));
M18FX.on('frame',()=>{M18FX.ctx.fillRect(0,0,1000,740);M18FX.render();});
""")
        self.wait(lambda: any("IMPORT_BLOCKED" in e["message"] for e in self.app.effect_logs()))
        self.assertEqual(self.server.probes, 0)
        self.assertTrue(any("FETCH_BLOCKED" in e["message"] for e in self.app.effect_logs()))
        self.assertTrue(self.app.effect_active())

    def test_runtime_exception_stops_and_restores(self):
        self.launch("// 首行\nM18FX.on('frame',()=>{throw new Error('测试异常');});")
        self.wait(lambda: not self.app.effect_active())
        self.assertEqual(self.app.effect_state()["status"], "error")
        self.assertEqual(self.app.restores, 1)
        self.assertTrue(any("测试异常" in e["message"] for e in self.app.effect_logs()))
        self.assertTrue(any("代码第 2 行" in e["message"] for e in self.app.effect_logs()))
        self.assertEqual(self.cdp.evaluate("document.body.dataset.status"), "error")

    def test_infinite_initialization_is_terminated(self):
        self.launch("while(true) {}")
        self.wait(lambda: not self.app.effect_active(), timeout=5)
        self.assertTrue(any("初始化超过" in e["message"] for e in self.app.effect_logs()))
        self.assertEqual(self.cdp.evaluate("1+1"), 2)
        self.assertEqual(self.app.restores, 1)

    def test_infinite_frame_is_terminated(self):
        self.launch("M18FX.on('frame',()=>{while(true){}});")
        self.wait(lambda: not self.app.effect_active(), timeout=4)
        self.assertTrue(any("750ms" in e["message"] for e in self.app.effect_logs()))
        self.assertEqual(self.cdp.evaluate("1+1"), 2)

    def test_syntax_failure_and_async_handlers_are_stopped(self):
        self.launch("const broken = ;")
        self.wait(lambda: not self.app.effect_active())
        self.assertTrue(any("SyntaxError" in e["message"] for e in self.app.effect_logs()))
        self.launch("M18FX.on('frame',async()=>{});")
        self.wait(lambda: not self.app.effect_active())
        self.assertTrue(any("不能返回 Promise" in e["message"] for e in self.app.effect_logs()))


if __name__ == "__main__":
    unittest.main()
