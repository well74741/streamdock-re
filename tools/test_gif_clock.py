"""用真实 Edge 截图验证图库 GIF 长按/拖动/重建后相位；独立 HTTP，不连接设备。

    python tools/test_gif_clock.py
"""
import base64
import io
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "ui"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


class Browser:
    """测试专用 CDP，仅终止自己启动的浏览器 PID。"""
    def __init__(self):
        self.profile = tempfile.TemporaryDirectory(prefix="m18-gif-clock-")
        self.process = subprocess.Popen([
            str(EDGE), "--headless=new", "--disable-gpu", "--remote-debugging-port=0",
            f"--user-data-dir={self.profile.name}", "--window-size=1200,900", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker = Path(self.profile.name) / "DevToolsActivePort"
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.1)
        self.port = int(marker.read_text().splitlines()[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5) as response:
            tab = next(t for t in json.load(response) if t["type"] == "page")
        path = tab["webSocketDebuggerUrl"].split(f":{self.port}", 1)[1]
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        key = base64.b64encode(b"m18-gif-clock-123").decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        headers, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert b"101" in headers.split(b"\r\n", 1)[0], headers
        self.serial = 0

    def need(self, size):
        while len(self.buf) < size:
            data = self.sock.recv(1 << 20)
            if not data:
                raise EOFError("浏览器 CDP 已断开")
            self.buf += data

    def call(self, method, params=None):
        self.serial += 1
        data = json.dumps({"id": self.serial, "method": method, "params": params or {}}).encode()
        mask = b"M18!"
        length = len(data)
        header = bytes([0x81]) + (bytes([0x80 | length]) if length < 126 else
                                 b"\xfe" + struct.pack("!H", length) if length < 65536 else
                                 b"\xff" + struct.pack("!Q", length))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            self.need(2)
            size, offset = self.buf[1] & 127, 2
            if size == 126:
                self.need(4)
                size, offset = int.from_bytes(self.buf[2:4], "big"), 4
            elif size == 127:
                self.need(10)
                size, offset = int.from_bytes(self.buf[2:10], "big"), 10
            self.need(offset + size)
            payload, self.buf = self.buf[offset:offset + size], self.buf[offset + size:]
            if not payload:
                continue
            reply = json.loads(payload)
            if reply.get("id") == self.serial:
                assert "error" not in reply, reply
                return reply["result"]

    def js(self, script):
        result = self.call("Runtime.evaluate", {
            "expression": script, "awaitPromise": True, "returnByValue": True,
        })
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    def screenshot(self):
        return Image.open(io.BytesIO(base64.b64decode(
            self.call("Page.captureScreenshot", {"format": "png"})["data"]))).convert("RGB")

    def close(self):
        try:
            self.call("Browser.close")
        except (OSError, EOFError):
            pass
        self.sock.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.profile.cleanup()


def main():
    frames = [Image.new("RGB", (64, 64), c) for c in ("#e02020", "#20c030", "#3040e0", "#e0c020")]
    data = io.BytesIO()
    frames[0].save(data, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[110, 230, 170, 290], loop=0)
    gif = data.getvalue()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            requests.append(self.path)
            if path == "/slow.gif":
                time.sleep(.4)
            content_type = "text/html"
            if path.endswith(".gif") or path.startswith("/api/album/raw/"):
                payload, content_type = gif, "image/gif"
            elif path == "/app.js":
                # 加载真实应用函数，但不 boot，不访问用户的 API/设备。
                text = (UI / "app.js").read_text(encoding="utf-8-sig")
                payload = re.sub(r"\bboot\(\);\s*$", "", text).encode()
                content_type = "text/javascript"
            elif path in ("/gif-clock.js", "/style.css"):
                payload = (UI / path[1:]).read_bytes()
                content_type = "text/javascript" if path.endswith("js") else "text/css"
            else:
                payload = (UI / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    browser = Browser()
    try:
        browser.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/"})
        for _ in range(100):
            if browser.js("typeof M18GifClock === 'object' && typeof startGhost === 'function'"):
                break
            time.sleep(.05)
        assert browser.js("""(() => {
          buildBoard();
          state={view:{fit:'cover',zoom:100},rotation:0,standbyBrightness:100};
          sceneItems=[{name:'test',sceneType:'album'}]; selectedScene='test';
          const a={scene:'test',items:['a.gif','b.gif'],index:0,active:true,
            fit:'cover',gap:27,interval:3,transition:'none',slideshow:true};
          renderAlbum(a);
          const tile=document.querySelector('#as-strip img');
          const cell=document.querySelector('.ring-board .cell');
          const first=cell.style.getPropertyValue('--album');
          for(let i=0;i<20;i++) renderAlbum({...a});
          if(cell.style.getPropertyValue('--album')!==first) return false;
          if(document.querySelector('#as-strip img')!==tile) return false;
          renderAlbum({...a,index:1});
          if(document.querySelector('#as-strip img')!==tile) return false;
          if(!cell.style.getPropertyValue('--album').includes('index=1&')) return false;
          if(document.querySelector('#as-strip .on').dataset.i!=='1') return false;
          selectedScene=null; sceneItems=[];
          const standby={...a,scene:'',active:true,index:0}; renderAlbum(standby);
          const small=document.querySelector('#album-strip img');
          renderAlbum({...standby,index:1});
          return document.querySelector('#album-strip img')===small;
        })()"""), "相册刷新/换图重复重建或缺少明确 index"
        browser.js("""document.body.innerHTML = `<div id="icon-groups" style="position:absolute;left:30px;top:30px;width:300px">
          <details class="icon-group" open><div class="tiles" style="grid-template-columns:64px 64px 64px">
          <img data-name="pack:test/a.gif" src="/a.gif" draggable="false">
          <img data-name="pack:test/b.gif" src="/b.gif" draggable="false"></div></details></div>`;
          window.testWrites=[]; post=async (...args)=>{testWrites.push(args);return {ok:true}};
          bindIconDrag(document.querySelector('#icon-groups'));
          M18GifClock.observe(document.querySelector('#icon-groups')); 1;""")
        for _ in range(100):
            if browser.js("document.querySelectorAll('.gif-clock-source').length===2"):
                break
            time.sleep(.05)
        assert browser.js("document.querySelectorAll('.gif-clock-source').length") == 2
        original_src = browser.js("[...document.querySelectorAll('#icon-groups img')].map(i=>i.src)")
        colors = set()
        samples = 0

        def compare(ghost=False):
            nonlocal samples
            points = browser.js("""[...document.querySelectorAll('#icon-groups img' + (ghostEl ? ', .drag-ghost' : ''))]
              .map(i=>{const r=i.getBoundingClientRect();return [Math.round(r.x+r.width/2),Math.round(r.y+r.height/2)]})""")
            shot = browser.screenshot()
            pixels = [shot.getpixel(tuple(p)) for p in points]
            assert len(pixels) >= (3 if ghost else 2), points
            assert len(set(pixels)) == 1, (points, pixels)
            colors.add(pixels[0])
            samples += 1

        for _ in range(12):
            compare()
            time.sleep(.07)
        assert len(colors) >= 3, colors
        p = browser.js("(()=>{const r=document.querySelector('#icon-groups img').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
        browser.call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, "x": p[0], "y": p[1]})
        time.sleep(.55)  # 长按
        compare()
        browser.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "buttons": 1, "x": 440, "y": 140})
        browser.js("ghostEl.style.opacity='1'; 1")
        assert browser.js("ghostEl.tagName") == "CANVAS"
        for _ in range(12):
            compare(True)
            time.sleep(.07)
        browser.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": 440, "y": 140})
        browser.js("restartGifAnim({t:1234,keys:[1,2,3]}); 1")
        assert browser.js("[...document.querySelectorAll('#icon-groups img')].map(i=>i.src)") == original_src
        for _ in range(12):
            compare()
            time.sleep(.07)
        # 后加入的文件、延迟解码、列表重建也应加入同一时间轴。
        browser.js("document.querySelector('.tiles').insertAdjacentHTML('beforeend','<img data-name=\"pack:test/c.gif\" src=\"/c.gif\">'); 1")
        time.sleep(.5)
        for _ in range(12):
            compare()
            time.sleep(.07)
        browser.js("document.querySelector('.tiles').innerHTML='<img data-name=\"pack:test/b.gif\" src=\"/b.gif\"><img data-name=\"pack:test/a.gif\" src=\"/a.gif\">'; 1")
        time.sleep(.25)
        for _ in range(12):
            compare()
            time.sleep(.07)
        browser.js("document.querySelector('#icon-groups').style.display='none'; 1")
        time.sleep(.2)
        browser.js("document.querySelector('#icon-groups').style.display='block'; 1")
        time.sleep(.25)
        for _ in range(12):
            compare()
            time.sleep(.07)
        # 实际裁图包的 15 个独立文件，而不是只测两个相同 URL。
        browser.js("""document.querySelector('.tiles').style.gridTemplateColumns='repeat(5,64px)';
          document.querySelector('#icon-groups').style.width='360px';
          document.querySelector('.tiles').innerHTML=Array.from({length:15},(_,i)=>
            `<span class="tile-wrap"><img data-name="pack:test/${i}.gif" src="/${i}.gif"><i class="tile-key">${i+1}</i></span>`).join(''); 1""")
        for _ in range(100):
            if browser.js("document.querySelectorAll('.gif-clock-source').length===15"):
                break
            time.sleep(.05)
        assert browser.js("document.querySelectorAll('.gif-clock-source').length") == 15
        for _ in range(12):
            compare()
            time.sleep(.07)
        browser.js("""document.querySelector('.tiles').lastElementChild.remove();
          document.querySelector('.tiles').insertAdjacentHTML('afterbegin',
            '<img data-name="pack:test/slow.gif" src="/slow.gif">'); 1""")
        time.sleep(.08)
        browser.js("startGhost(document.querySelector('img[data-name=\"pack:test/slow.gif\"]'),440,350); ghostEl.style.opacity='1'; 1")
        assert browser.js("ghostEl.tagName") == "CANVAS", "解码尚未结束时不应克隆原生 GIF"
        time.sleep(1)
        for _ in range(12):
            compare(True)
            time.sleep(.07)
        browser.js("endGhost(); 1")
        assert not browser.js("testWrites"), browser.js("testWrites")
        assert not any("g=" in url for url in requests), requests
        # 在新的根节点上重新加载模块，明确模拟没有 ImageDecoder 的旧浏览器。
        browser.js("""(async()=>{
          Object.defineProperty(window,'ImageDecoder',{value:undefined,configurable:true});
          document.body.innerHTML='<div id="icon-groups"></div>';
          (0,eval)(await (await fetch('/gif-clock.js')).text());
          return typeof ImageDecoder;
        })()""")
        assert browser.js("typeof ImageDecoder") == "undefined"
        assert browser.js("""(() => {
          const root=document.querySelector('#icon-groups');
          root.innerHTML='<img data-name="pack:test/a.gif" src="/a.gif">';
          M18GifClock.observe(root);
          return !root.querySelector('canvas') && !root.querySelector('.gif-clock-source');
        })()"""), "旧浏览器应保留原生动画，不显示空画布"
        print(f"PASS：{samples} 张真实截图；{len(colors)} 种帧颜色；15 文件、长按/拖拽/松手、重播、延迟解码、重建、离屏恢复均同相位；相册重复刷新不重载；原生回退通过；0 写接口。")
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
