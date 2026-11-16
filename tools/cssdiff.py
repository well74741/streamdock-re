"""改 CSS 前后对比：用无头 Edge 抓控制台在几种典型界面下每个元素的计算样式，确认外观没被误伤。

    python tools/cssdiff.py snap before.json 1600 1200     # 改之前（控制台要开着）
    ……改 app/ui/style.css……
    python tools/cssdiff.py snap after.json 1600 1200      # 改之后
    python tools/cssdiff.py diff before.json after.json    # 0 处不同 = 外观完全一致

窄窗口下的 @media 规则另抓 1250 / 1050 宽各一次。灯珠颜色是实时灯效，不参与对比。
"""
import base64, json, os, socket, subprocess, sys, threading, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

PROPS = """display position top right bottom left float clear z-index box-sizing width height min-width min-height max-width
max-height margin-top margin-right margin-bottom margin-left padding-top padding-right padding-bottom padding-left
border-top-width border-right-width border-bottom-width border-left-width border-top-style border-top-color
border-right-color border-bottom-color border-left-color border-top-left-radius border-top-right-radius
border-bottom-left-radius border-bottom-right-radius color background-color background-image background-size
background-position font-family font-size font-weight font-style line-height letter-spacing text-align
text-transform text-decoration-line white-space overflow-x overflow-y opacity visibility box-shadow outline-style
outline-color outline-width cursor transform transition-property transition-duration flex-direction flex-wrap
justify-content align-items align-self flex-grow flex-shrink flex-basis gap row-gap column-gap grid-template-columns
grid-template-rows order pointer-events user-select filter backdrop-filter aspect-ratio object-fit
text-overflow vertical-align list-style-type content""".split()

# 每种界面：进入前执行的脚本
STATES = {
    "主界面": "1",
    "选中按键": "selectKey(13); 1",
    "选中导航键": "selectKey(17); 1",
    "待机相册页签": "window.m18ShowStandbyTab && window.m18ShowStandbyTab('album'); 1",
    "设备设置弹窗": "document.querySelector('#device-btn')?.click(); 1",
    "接入AI弹窗": "(document.querySelector('#cp-agent') || {click(){}}).click(); 1",
    "确认框": "confirmDialog('测试确认框 <b>粗体</b>'); 1",
    "提示条": "toast('测试提示', 60000); 1",
}

SNAP_JS = r"""(() => {
  const PROPS = %s;
  const out = [];
  const all = [...document.querySelectorAll('body *')];
  let tiles = 0;
  for (const el of all) {
    if (el.closest('.tiles') && ++tiles > 120) continue;      // 图标库瓦片只取前面一部分
    const cs = getComputedStyle(el);
    const before = getComputedStyle(el, '::before'), after = getComputedStyle(el, '::after');
    const path = [];
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const i = n.parentElement ? [...n.parentElement.children].indexOf(n) : 0;
      path.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + ':' + i);
    }
    const v = {};
    for (const p of PROPS) v[p] = cs.getPropertyValue(p);
    for (const p of ['content', 'color', 'background-color', 'width', 'height', 'display']) {
      v['::before ' + p] = before.getPropertyValue(p);
      v['::after ' + p] = after.getPropertyValue(p);
    }
    out.push([path.join('>'), (el.className && el.className.baseVal === undefined ? el.className : ''), v]);
  }
  return out;
})()"""


class CDP:
    def __init__(self, w, h, port=9335):
        prof = os.path.join(os.environ["TEMP"], "m18-cssdiff-profile")
        import shutil
        shutil.rmtree(prof, ignore_errors=True)           # 每次全新的浏览器配置（localStorage 不串）
        self.p = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}",
                                   f"--remote-debugging-port={port}", f"--window-size={w},{h}",
                                   "--hide-scrollbars", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                tab = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1))
                           if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        path = tab["webSocketDebuggerUrl"].split(f":{port}", 1)[1]
        self.s = socket.create_connection(("127.0.0.1", port))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall("\r\n".join([f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket",
                                     "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                                     "", ""]).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.replies, self.n = {}, 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _need(self, k):
        while len(self.buf) < k:
            d = self.s.recv(1 << 20)
            if not d:
                raise EOFError
            self.buf += d

    def _reader(self):
        try:
            self._loop()
        except Exception:
            pass

    def _loop(self):
        while True:
            self._need(2)
            ln, off = self.buf[1] & 0x7F, 2
            if ln == 126:
                self._need(4); ln = int.from_bytes(self.buf[2:4], "big"); off = 4
            elif ln == 127:
                self._need(10); ln = int.from_bytes(self.buf[2:10], "big"); off = 10
            self._need(off + ln)
            msg, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
            try:
                m = json.loads(msg)
                if "id" in m:
                    self.replies[m["id"]] = m
            except Exception:
                pass

    def call(self, method, params=None):
        self.n += 1
        i = self.n
        data = json.dumps({"id": i, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        L = len(data)
        hdr = bytes([0x81]) + (bytes([0x80 | L]) if L < 126 else bytes([0xFE]) + L.to_bytes(2, "big") if L < 65536
                               else bytes([0xFF]) + L.to_bytes(8, "big"))
        self.s.sendall(hdr + mask + bytes(b ^ mask[j % 4] for j, b in enumerate(data)))
        t0 = time.time()
        while i not in self.replies:
            if time.time() - t0 > 60:
                raise TimeoutError(method)
            time.sleep(0.02)
        return self.replies.pop(i)

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    def close(self):
        self.p.kill()


def snap(out, w=1600, h=1200):
    c = CDP(w, h)
    try:
        res = {}
        for name, prep in STATES.items():
            c.call("Page.navigate", {"url": "http://127.0.0.1:8642/?noevents"})
            time.sleep(3.5)
            c.js("document.querySelectorAll('.pressed').forEach(e => e.classList.remove('pressed')); 1")
            c.js(prep)
            time.sleep(0.8)
            # 动画 / 过渡统一停掉，避免抓到中间帧
            c.js("document.getAnimations().forEach(a => a.finish && a.finish()); 1")
            res[name] = c.js(SNAP_JS % json.dumps(PROPS))
            shot = c.call("Page.captureScreenshot", {"format": "png"})["result"]["data"]
            open(out.replace(".json", f"_{name}.png"), "wb").write(base64.b64decode(shot))
            print(name, len(res[name]), "个元素")
        json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    finally:
        c.close()


def diff(a, b):
    A, B = json.load(open(a, encoding="utf-8")), json.load(open(b, encoding="utf-8"))
    total = 0
    for name in A:
        ea = {p: v for p, _, v in A[name]}
        eb = {p: v for p, _, v in B.get(name, [])}
        cls = {p: c for p, c, _ in A[name]}
        diffs = []
        if set(ea) != set(eb):
            diffs.append(("元素结构不同", len(ea), len(eb)))
        for p in ea:
            if p not in eb:
                continue
            live = "dot" in (cls.get(p) or "").split()          # 灯珠颜色是实时灯效（行内样式），不算
            for k, v in ea[p].items():
                if live and k in ("background-color", "box-shadow"):
                    continue
                if eb[p].get(k) != v:
                    diffs.append((p.split(">")[-3:], cls.get(p), k, v, eb[p].get(k)))
        total += len(diffs)
        print(f"== {name}：{len(diffs)} 处不同")
        for d in diffs[:25]:
            print("   ", d)
    print("合计", total, "处不同")
    return total


if __name__ == "__main__":
    if sys.argv[1] == "snap":
        snap(sys.argv[2], *(int(x) for x in sys.argv[3:5]))
        sys.stdout.flush()
        os._exit(0)
    else:
        sys.exit(1 if diff(sys.argv[2], sys.argv[3]) else 0)
