const $ = (sel, root = document) => root.querySelector(sel);

const MODE_NAMES = {
  solid: "常亮", rainbow: "彩虹", breathe: "呼吸",
  chase: "流光", gradient: "渐变", ripple: "涟漪", custom: "自定义", off: "关闭",
};

const MODE_DESC = {
  solid: "整圈灯珠显示同一颜色，颜色取自下方色板。",
  rainbow: "色相沿灯圈循环流动，速度由「速度」控制。",
  breathe: "当前颜色整体明暗呼吸。",
  chase: "单点亮光沿灯圈追逐移动。",
  gradient: "从当前颜色过渡到它的补色。",
  ripple: "按下按键时，从按键位置向四周扩散一圈涟漪光。",
  custom: "在灯圈上按住拖动逐颗上色，再涂一次覆盖。",
  off: "关闭灯带。",
};

const SWATCHES = [
  "#ff3b30", "#ff9500", "#ffcc00", "#34c759",
  "#00c7be", "#00aaff", "#5e5ce6", "#af52de",
  "#ff2d55", "#ffffff", "#8e8e93", "#0a84ff",
];

const NAV_PRESETS = [
  "上一页", "下一页", "主页", "下一场景",
  "左选", "确认", "右选",
  "参数-", "参数+", "音量-", "音量+", "无",
];

/* ------------------------------------------------------------------ 几何 */
const RING_W = 1000;
const RING_H = 740;

function ringPositions() {
  // 矩形环：左右各 6 颗（含四角）、上下各 5 颗，共 22
  const L = 70, R = 930, T = 70, B = 690;
  const pitchX = (R - L) / 6;
  const spanY = B - T;
  const pts = [];
  for (let i = 0; i < 6; i++) pts.push([L, T + (spanY * i) / 5]);
  for (let i = 1; i <= 5; i++) pts.push([L + pitchX * i, B]);
  for (let i = 0; i < 6; i++) pts.push([R, B - (spanY * i) / 5]);
  for (let i = 1; i <= 5; i++) pts.push([R - pitchX * i, T]);
  // 正面两颗与导航键：跟 5 号灯珠同一高度，横向间隔与环上一致
  pts.push([213, 566]);
  pts.push([787, 566]);
  return pts.map(([x, y]) => [(x / RING_W) * 100, (y / RING_H) * 100]);
}

const RING_POSITIONS = ringPositions();
const BUTTON_POSITIONS = [[350, 566], [500, 566], [650, 566]];

let state = null;
let currentColor = "#00aaff";
let ledBrightness = 100;
let painting = false;
let localCustom = Array.from({ length: 24 }, () => [0, 0, 0]);
let segments = [];
let lastFrame = null;
let selectedKey = null;
let pickMode = null;

/* ------------------------------------------------------------------ 工具 */
async function post(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  } catch (err) {
    // 服务没响应（刚退出 / 重启中）：给个提示，别让一堆 await 静默挂掉
    if (!post._warned) {
      post._warned = true;
      setTimeout(() => { post._warned = false; }, 5000);
      try { toast("连不上控制台服务，稍后再试", 2200); } catch (_) { /* ignore */ }
    }
    return { ok: false, error: String(err) };
  }
  return res.json().catch(() => ({ ok: res.ok }));
}

/* 用户正在操作的输入框 / 滑杆：服务器推过来的状态不要把它改回旧值
   （以前拖滑杆会来回跳、输入标题会丢字，中文输入法组字时也会被打断） */
const busyUntil = new WeakMap();
function markBusy(el, ms = 800) {
  if (el) busyUntil.set(el, Date.now() + ms);
}
function isBusy(el) {
  if (!el) return false;
  if (document.activeElement === el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type !== "range"))) {
    return true;                                  // 文本框正在输入
  }
  return (busyUntil.get(el) || 0) > Date.now();   // 刚拖过 / 刚改过
}
function setVal(el, v) {
  if (el && !isBusy(el) && String(el.value) !== String(v)) el.value = v;
}
document.addEventListener("input", (e) => markBusy(e.target), true);
document.addEventListener("pointerdown", (e) => { if (e.target?.type === "range") markBusy(e.target, 3000); }, true);
document.addEventListener("pointerup", (e) => { if (e.target?.type === "range") markBusy(e.target, 600); }, true);

/* KeyboardEvent.code → 后端 send_hotkey 认识的键名；修饰键本身返回 null */
const CODE_NAMES = {
  Space: "space", Enter: "enter", NumpadEnter: "enter", Tab: "tab", Backspace: "backspace",
  Delete: "delete", Insert: "insert", Home: "home", End: "end",
  PageUp: "pageup", PageDown: "pagedown",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\",
  Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`",
  PrintScreen: "printscreen", Pause: "pause", CapsLock: "capslock",
  ContextMenu: "apps", NumLock: "numlock", ScrollLock: "scrolllock",
  NumpadMultiply: "num*", NumpadAdd: "num+", NumpadSubtract: "num-",
  NumpadDecimal: "num.", NumpadDivide: "num/",
};

function codeToKeyName(code) {
  if (!code) return null;
  if (CODE_NAMES[code]) return CODE_NAMES[code];
  let m;
  if ((m = code.match(/^Key([A-Z])$/))) return m[1].toLowerCase();
  if ((m = code.match(/^Digit(\d)$/))) return m[1];
  if ((m = code.match(/^Numpad(\d)$/))) return `num${m[1]}`;
  if ((m = code.match(/^F(\d{1,2})$/)) && Number(m[1]) <= 24) return `f${m[1]}`;
  return null;   // Control/Shift/Alt/Meta 等修饰键，或不支持的键
}

function throttle(fn, wait = 60) {
  let last = 0, timer = null, pending = null;
  return (...args) => {
    pending = args;
    const now = Date.now();
    if (now - last >= wait) { last = now; fn(...args); }
    else if (!timer) {
      timer = setTimeout(() => {
        timer = null; last = Date.now(); fn(...pending);
      }, wait - (now - last));
    }
  };
}

const pushLighting = throttle((patch) => post("/api/lighting", patch), 70);
const sendCustom = throttle(() => post("/api/lighting/custom", { colors: localCustom }), 140);

function hexToRgb(hex) {
  const h = String(hex || "").replace("#", "");
  if (h.length !== 6) return [0, 0, 0];
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = reject;
    fr.readAsDataURL(file);
  });
}

async function upload(file, rel) {
  const dataUrl = await readAsDataURL(file);
  const r = await post("/api/upload", { name: file.name, dataUrl, rel: rel || "" });
  if (!r.ok) throw new Error(r.error || "上传失败");
  return r;
}

function toast(msg, ms = 2200) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("show"), 10);
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, ms);
}

/* ------------------------------------------------------------------ 浏览器硬化 */
function hardenBrowser() {
  const noDefault = (e) => { e.preventDefault(); return false; };
  const isEl = (t) => t instanceof Element;
  const noMouseButton = (e) => { if (e.button !== 0) e.preventDefault(); };

  document.addEventListener("contextmenu", (e) => {
    if (isEl(e.target) && e.target.closest(".ring-board .cell, .ring-btn, .key-menu, #sel-thumb, .lib-menu")) return;
    e.preventDefault();
  }, { capture: true });
  document.addEventListener("selectstart", noDefault, { capture: true });
  document.addEventListener("dragstart", (e) => {
    // 右键按住时不启动任何原生拖动；其余只允许显式标记 draggable 的元素（图标库瓦片）
    if (rightHeld || (e.buttons & 2)) { e.preventDefault(); return; }
    if (!(isEl(e.target) && e.target.closest("[draggable='true']"))) e.preventDefault();
  }, { capture: true });
  document.addEventListener("drag", (e) => {
    if (rightHeld || (e.buttons & 2)) e.preventDefault();
  }, { capture: true });
  document.addEventListener("auxclick", noDefault, { capture: true });
  ["pointerdown", "pointerup", "mousedown", "mouseup"].forEach((type) => {
    document.addEventListener(type, noMouseButton, { capture: true });
  });
  // 右键按住拖动：不给浏览器留任何「拖动 / 选区 / 自动滚动 / 拖放搜索」的机会
  let rightHeld = false;
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 2) return;
    rightHeld = true;
    e.preventDefault();
    document.body.classList.add("no-select");
  }, { capture: true });
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 2) return;
    e.preventDefault();
    document.body.classList.add("no-select");
  }, { capture: true });
  const releaseRight = (e) => {
    if (e && e.type === "pointerup" && e.button !== 2 && e.buttons !== 0) return;
    rightHeld = false;
    document.body.classList.remove("no-select");
  };
  document.addEventListener("pointerup", releaseRight, { capture: true });
  document.addEventListener("pointercancel", releaseRight, { capture: true });
  document.addEventListener("blur", releaseRight);
  window.addEventListener("blur", releaseRight);
  ["pointermove", "mousemove", "pointerrawupdate"].forEach((type) => {
    document.addEventListener(type, (e) => {
      // 已经没有右键按下了（pointerup 可能丢在窗口外）：顺手复位
      if (rightHeld && (e.buttons & 2) === 0) { rightHeld = false; document.body.classList.remove("no-select"); }
      if (rightHeld || (e.buttons & 2)) e.preventDefault();
    }, { passive: false, capture: true });
  });
  document.addEventListener("wheel", (e) => {
    if (e.ctrlKey) e.preventDefault();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) e.preventDefault();
    if (e.buttons & 2) e.preventDefault();
  }, { passive: false, capture: true });
  document.addEventListener("keydown", (e) => {
    if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) e.preventDefault();
    const tag = document.activeElement?.tagName;
    if (e.key === "Backspace" && tag !== "INPUT" && tag !== "TEXTAREA") e.preventDefault();
  });
}

/* ------------------------------------------------------------------ 控制台底图 */
function buildBoard() {
  const board = document.querySelector(".ring-board");
  if (!board) return;
  board.innerHTML = "";
  const order = [11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4, 5];
  order.forEach((n) => {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.dataset.key = n;
    cell.innerHTML = `<img class="cell-img" alt="" hidden />
      <span class="cell-num">${n}</span>
      <span class="cell-label" hidden></span>
      <span class="badge" hidden>图</span>
      <span class="cell-act" hidden></span>`;
    cell.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      selectKey(n);
    });
    cell.addEventListener("dragover", (e) => {
      e.preventDefault();
      cell.classList.add("dropping");
    });
    cell.addEventListener("dragleave", () => cell.classList.remove("dropping"));
    cell.addEventListener("drop", (e) => {
      e.preventDefault();
      cell.classList.remove("dropping");
      dropOnKey(n, e.dataTransfer);
    });
    board.appendChild(cell);
  });
}

function layoutBoard() {
  const board = document.querySelector(".ring-board");
  const wrap = $("#ring-wrap");
  if (!board || !wrap) return;
  const WW = wrap.clientWidth;
  const WH = wrap.clientHeight;
  if (!WW || !WH) return;
  // 主控制台用固定间距，不跟随「间隙」滑杆；间隙只在壁纸预览里体现
  const g = 0.12;
  const cell = Math.max(6, Math.floor(Math.min(
    (WW * 0.74) / (5 + 4 * g),
    (WH * 0.50) / (3 + 2 * g),
  )));
  const gapPx = cell * g;
  const w = 5 * cell + 4 * gapPx;
  const h = 3 * cell + 2 * gapPx;
  board.style.width = `${w}px`;
  board.style.height = `${h}px`;
  board.style.gridTemplateColumns = `repeat(5, ${cell}px)`;
  board.style.gridTemplateRows = `repeat(3, ${cell}px)`;
  board.style.columnGap = `${gapPx}px`;
  board.style.rowGap = `${gapPx}px`;
  board.style.backgroundSize = `${w}px ${h}px`;
}

function renderWpGrid() {
  const grid = $("#wp-grid");
  if (!grid) return;
  const gap = Number($("#wallpaper-gap")?.value || 0);
  const side = (state?.rotation ?? 0) % 180 !== 0;     // 竖放：画布是 3 列 × 5 行
  const cols = side ? 3 : 5, rows = side ? 5 : 3;
  const W = cols * 64 + (cols - 1) * gap;
  const H = rows * 64 + (rows - 1) * gap;
  let html = "";
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      const left = ((c * (64 + gap)) / W) * 100;
      const top = ((r * (64 + gap)) / H) * 100;
      html += `<div class="g" style="left:${left}%;top:${top}%;width:${(64 / W) * 100}%;height:${(64 / H) * 100}%"></div>`;
    }
  }
  grid.innerHTML = html;
}

/* 切场景后强制下一次刷新重拉所有按键图（同名文件也能换过来） */
function forceBoardReload() {
  document.querySelectorAll(".ring-board .cell-img").forEach((el) => { el.dataset.v = ""; });
}

function refreshBoard() {
  const stamp = Date.now();
  const img = $("#wp-preview");
  if (img) {
    // 新图加载完成后再清掉拖动时的临时位移，避免松手瞬间回弹一下
    img.onload = () => {
      img.style.transition = "none";
      img.style.transform = "";
      img.dataset.retry = "0";
    };
    const url = `/api/wallpaper/canvas.png?v=${stamp}`;
    img.onerror = () => {
      const tries = Number(img.dataset.retry || 0);
      if (tries < 4) {
        img.dataset.retry = String(tries + 1);
        setTimeout(() => { img.src = `${url}&r=${tries}`; }, 500);
      }
    };
    img.src = url;
  }
  layoutBoard();
  refreshCells();
  renderWpGrid();
}

function splitLabel(text, size = 20) {
  const t = String(text || "");
  const width = (ch) => (/[\x00-\xff]/.test(ch) ? size * 0.55 : size);
  const total = [...t].reduce((sum, ch) => sum + width(ch), 0);
  if (total <= 58) return [t];
  const mid = Math.ceil(t.length / 2);
  return [t.slice(0, mid), t.slice(mid)];
}

function refreshCells() {
  const imgs = state?.keyImages || {};
  const acts = state?.keyActions || {};
  const styles = state?.keyStyles || {};
  const stamp = Date.now();
  document.querySelectorAll(".ring-board .cell").forEach((cell) => {
    const n = cell.dataset.key;
    const hasImg = !!imgs[n];
    const st = styles[n] || {};
    const act = acts[n];
    const imgEl = $(".cell-img", cell);
    // 刚放上去、还没写入的图标：先按这个样子显示（点保存才真正写进按键）
    const pend = pendingIcon && pendingIcon.key === Number(n) ? pendingIcon : null;
    cell.classList.toggle("pending-icon", !!pend);
    if (imgEl) {
      const plug = state?.keyPlugin?.[n];
      if (pend && pend.src) {
        const pv = `pend-${pend.src}`;
        if (imgEl.dataset.v !== pv) {
          imgEl.src = pend.src;
          imgEl.dataset.v = pv;
        }
        imgEl.hidden = false;
      } else if (plug && plug.rev > 0) {
        // 插件在画这个键：显示服务端合成好的实时画面（已含标题）
        const lv = `${n}-live-${plug.rev}`;
        if (imgEl.dataset.v !== lv) {
          imgEl.src = `/api/key/${n}/live.jpg?v=${plug.rev}`;
          imgEl.dataset.v = lv;
        }
        imgEl.hidden = false;
      } else if (hasImg) {
        // 用原始文件（GIF 会实时播放），和控制台"所见即所得"
        // 按图片版本判断：切场景 / 翻页后同一个键换了图，也要重新加载
        // 按图片版本判断：切场景 / 翻页后同一个键换了图，也要重新加载
        const rev = `${state?.sceneRev || 0}-${state?.page || 0}-${n}-${state?.keyImageRev?.[n] || imgs[n]}`;
        if (imgEl.dataset.v !== rev) {
          imgEl.src = `/api/key/${n}/raw?v=${stamp}`;
          imgEl.dataset.v = rev;
        }
        imgEl.hidden = false;
      } else {
        imgEl.hidden = true;
        imgEl.removeAttribute("src");
        imgEl.dataset.v = "";
      }
    }
    cell.style.backgroundImage = "";
    const live = (state?.keyPlugin?.[n]?.rev || 0) > 0;
    cell.style.backgroundColor = hasImg || live ? "" : (st.bg || "#000");
    const label = $(".cell-label", cell);
    if (label) {
      label.innerHTML = splitLabel(st.text, st.size ?? 20).map(escHtml).join("<br>");
      label.style.color = st.color || "#e8eefc";
      label.style.opacity = `${(st.opacity ?? 100) / 100}`;
      // 与设备一致：设备是 64×64、字号 = st.size，这里按实际格子大小等比缩放
      const px = cell.clientWidth || 64;
      label.style.fontSize = `${Math.max(5, (st.size ?? 20) * (px / 64))}px`;
      label.style.lineHeight = "1.15";
      label.style.padding = `${Math.max(1, (3 * px) / 64)}px`;
      label.style.alignItems = st.pos === "top" ? "flex-start" : (st.pos === "bottom" ? "flex-end" : "center");
      label.hidden = !st.text || live;
    }
    const badge = $(".badge", cell);
    if (badge) badge.hidden = !hasImg;
    const actEl = $(".cell-act", cell);
    if (actEl) {
      // 标题已经在底部时不再叠一行动作名（文件夹 / 返回键就是这样）
      actEl.hidden = !act || (!!st.text && st.pos === "bottom" && hasImg);
      actEl.textContent = act || "";
    }
    cell.classList.toggle("has-action", !!act);
    cell.classList.toggle("selected", selectedKey === Number(n));
  });
  document.querySelectorAll(".ring-btn").forEach((b) => {
    b.classList.toggle("selected", selectedKey === Number(b.dataset.key));
  });
}

/* ------------------------------------------------------------------ 灯圈 */
function buildRing() {
  const ring = $("#ring");
  ring.innerHTML = "";
  segments = [];
  RING_POSITIONS.forEach(([x, y], i) => {
    const dot = document.createElement("div");
    dot.className = "dot";
    dot.dataset.index = i;
    dot.dataset.key = i + 1;
    dot.style.left = x + "%";
    dot.style.top = y + "%";
    dot.title = `灯珠 ${i + 1}`;
    dot.innerHTML = `<span class="idx">${i + 1}</span>`;
    ring.appendChild(dot);
    segments.push(dot);
  });
  BUTTON_POSITIONS.forEach(([x, y], i) => {
    const btn = document.createElement("div");
    btn.className = "ring-btn";
    btn.dataset.key = 16 + i;
    btn.title = `导航键 ${i + 1}`;
    btn.style.left = (x / RING_W) * 100 + "%";
    btn.style.top = (y / RING_H) * 100 + "%";
    btn.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      selectKey(16 + i);
    });
    btn.addEventListener("dragover", (e) => {
      e.preventDefault();
      btn.classList.add("dropping");
    });
    btn.addEventListener("dragleave", () => btn.classList.remove("dropping"));
    btn.addEventListener("drop", (e) => {
      e.preventDefault();
      btn.classList.remove("dropping");
      dropOnKey(16 + i, e.dataTransfer);
    });
    ring.appendChild(btn);
  });
  // 若此前已经收到过灯效帧（先拿到状态、后建环），补画一次
  if (lastFrame) paintDots(lastFrame);
}

function paintDotColor(i, rgb) {
  const dot = segments[i];
  if (!dot) return;
  const k = Math.max(0, Math.min(100, ledBrightness)) / 100;
  const r = Math.round(rgb[0] * k);
  const g = Math.round(rgb[1] * k);
  const b = Math.round(rgb[2] * k);
  dot.style.background = `rgb(${r},${g},${b})`;
  const lum = (r * 0.299 + g * 0.587 + b * 0.114) / 255;
  dot.style.boxShadow = lum > 0.12 ? `0 0 12px rgba(${r},${g},${b},0.55)` : "none";
}

function paintDots(colors) {
  if (!colors) return;
  lastFrame = colors;
  for (let i = 0; i < segments.length && i < colors.length; i++) {
    paintDotColor(i, colors[i]);
  }
  // 正面两颗（23/24）跟随显示顺序上的 5 号和 12 号，正反向都一致
  if (segments.length >= 24) {
    [22, 23].forEach((front, k) => {
      const src = segments[[4, 11][k]];
      if (src && segments[front]) {
        segments[front].style.background = src.style.background;
        segments[front].style.boxShadow = src.style.boxShadow;
      }
    });
  }
}

function paintAtIndex(i) {
  const rgb = hexToRgb(currentColor);
  localCustom[i] = rgb;
  paintDotColor(i, rgb);
  if (state?.lighting) state.lighting.mode = "custom";
  applyMode("custom");
  sendCustom();
}

function fillAll(rgb) {
  localCustom = Array.from({ length: 24 }, () => [...rgb]);
  localCustom.forEach((c, i) => paintDotColor(i, c));
  applyMode("custom");
  sendCustom();
}

function bindRingPainting() {
  const ring = $("#ring");
  const idxOf = (el) => {
    const dot = el?.closest?.(".dot");
    return dot ? Number(dot.dataset.index) : -1;
  };
  const block = (e) => e.preventDefault();

  ring.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const i = idxOf(e.target);
    if (i < 0) return;
    painting = true;
    document.body.classList.add("no-select");
    document.addEventListener("contextmenu", block, true);
    document.addEventListener("selectstart", block, true);
    try { window.getSelection()?.removeAllRanges(); } catch (_) { /* ignore */ }
    ring.setPointerCapture(e.pointerId);
    paintAtIndex(i);
    e.preventDefault();
  });

  ring.addEventListener("pointermove", (e) => {
    if (!painting) return;
    const i = idxOf(document.elementFromPoint(e.clientX, e.clientY));
    if (i >= 0) paintAtIndex(i);
  });

  const stop = () => {
    if (!painting) return;
    painting = false;
    document.body.classList.remove("no-select");
    document.removeEventListener("contextmenu", block, true);
    document.removeEventListener("selectstart", block, true);
  };
  window.addEventListener("pointerup", stop);
  window.addEventListener("pointercancel", stop);
  window.addEventListener("blur", stop);
}

/* ------------------------------------------------------------------ 选中与编辑 */
/* 官方图标包 + 插件：直接读官方驱动目录，按包分组、展开时才加载图片 */
let iconPacks = null;
let pluginItems = null;
const SIDE_STATE_KEY = "m18.sidebar.v2";
const sideState = (() => {
  try { return JSON.parse(localStorage.getItem(SIDE_STATE_KEY) || "{}"); } catch { return {}; }
})();
sideState.collapsed ||= {};          // 侧栏分区：scenes / icons / plugins
sideState.openPacks ||= [];          // 展开着的图标包
function saveSideState() {
  try { localStorage.setItem(SIDE_STATE_KEY, JSON.stringify(sideState)); } catch { /* ignore */ }
}

const escHtml = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const packIconUrl = (pack, file) => {
  // 带上包的版本号：重名重裁（例如删掉再裁一个同名包）时浏览器不会拿旧缓存显示
  const rev = (iconPacks || []).find((p) => p && p.id === pack)?.rev || "";
  return `/api/iconpack/${encodeURIComponent(pack)}/${encodeURIComponent(file)}${rev ? `?v=${rev}` : ""}`;
};
const pluginIconUrl = (id, idx) => `/api/plugin-icon/${encodeURIComponent(id)}/${idx}`;

function iconTile(pack, file) {
  const label = file.replace(/\.[^.]+$/, "");
  return `<img loading="lazy" draggable="false" data-name="pack:${escHtml(pack)}/${escHtml(file)}"`
    + ` src="${packIconUrl(pack, file)}" title="${escHtml(label)}" alt="" />`;
}

/* 裁图包里的第 n 张（1 起，按屏幕从左到右、从上到下）对应哪个键 */
const cropKeyOfIndex = (n) => {
  const col = (n - 1) % 5;
  const row = Math.floor((n - 1) / 5);      // 0 = 屏幕上排
  return (2 - row) * 5 + col + 1;
};

function fillPackTiles(details) {
  const tiles = details.querySelector(".tiles");
  if (!tiles || tiles.dataset.filled) return;
  const pack = iconPacks.find((p) => p.id === details.dataset.pack);
  if (!pack) return;
  if (pack.crop) {
    // 裁图包：动图照常播（裁出来的 GIF 就是要看动效），
    // 每格右下角标出它属于哪个键，5 列排布和屏幕一致，拖的时候不会对错行
    tiles.innerHTML = pack.files.map((f, i) => {
      const key = cropKeyOfIndex(i + 1);
      return `<span class="tile-wrap" title="键 ${key}">`
        + `<img loading="lazy" draggable="false" data-name="pack:${escHtml(pack.id)}/${escHtml(f)}"`
        + ` src="${packIconUrl(pack.id, f)}" alt="" />`
        + `<i class="tile-key">${key}</i></span>`;
    }).join("");
  } else {
    tiles.innerHTML = pack.files.map((f) => iconTile(pack.id, f)).join("");
  }
  tiles.dataset.filled = "1";
}

async function loadIcons(refresh = false) {
  const box = $("#icon-groups");
  if (!box) return;
  if (!iconPacks || refresh) {
    try {
      iconPacks = (await (await fetch("/api/iconpacks", { cache: "no-store" })).json()).packs || [];
    } catch (_) {
      iconPacks = [];
    }
  }
  const total = iconPacks.reduce((n, p) => n + p.count, 0);
  const cnt = $("#icon-count");
  if (cnt) cnt.textContent = String(total);
  const q = ($("#icon-search")?.value || "").toLowerCase().trim();

  if (!iconPacks.length) {
    box.innerHTML = `<div class="side-empty">没找到官方图标包。装了官方 Stream Dock 并下载过图标包后点「刷新」。</div>`;
    return;
  }
  if (q) {
    // 搜索：跨所有图标包匹配文件名 / 包名，最多显示 240 个
    const hits = [];
    for (const p of iconPacks) {
      const packHit = p.name.toLowerCase().includes(q);
      for (const f of p.files) {
        if (packHit || f.toLowerCase().includes(q)) hits.push([p.id, f]);
        if (hits.length >= 240) break;
      }
      if (hits.length >= 240) break;
    }
    box.innerHTML = hits.length
      ? `<div class="icon-group search"><div class="group-title">搜索结果<em>${hits.length >= 240 ? "240+" : hits.length}</em></div>`
        + `<div class="tiles">${hits.map(([id, f]) => iconTile(id, f)).join("")}</div></div>`
      : `<div class="side-empty">没有匹配「${escHtml(q)}」的图标</div>`;
    return;
  }
  const mineCount = iconPacks.filter((p) => p.mine).length;
  let seenMine = false, seenOfficial = false;
  box.innerHTML = iconPacks.map((p) => {
    const open = sideState.openPacks.includes(p.id);
    // 裁图包：封面拼成 5×3 的整图缩略，一眼就能看出排布对不对
    const cover = !p.files.length ? ""
      : (p.crop && p.files.length >= 5
        ? `<span class="cover crop-cover">${p.files.slice(0, 15).map((f) => `<img src="${packIconUrl(p.id, f)}" alt="" />`).join("")}</span>`
        : `<img class="cover" src="${packIconUrl(p.id, p.files[0])}" alt="" />`);
    let head = "";
    if (p.mine && !seenMine) {
      seenMine = true;
      head = `<div class="lib-sec">我的图标</div>`;
    }
    if (!p.mine && !seenOfficial) {
      seenOfficial = true;
      head = (!mineCount
        ? `<div class="lib-sec">我的图标</div>`
          + `<div class="side-empty small">还没有：点控制台右下角「裁图工具」，把一张图裁成 15 张就存到这里。</div>`
        : "") + `<div class="lib-sec">官方图标包</div>`;
    }
    return head + `<details class="icon-group" data-pack="${escHtml(p.id)}"${p.crop ? ' data-crop="1"' : ""}${open ? " open" : ""}>`
      + `<summary>${cover}<span class="g-name" title="${escHtml(p.author ? `${p.name} · ${p.author}` : p.name)}">${escHtml(p.name)}</span><em>${p.count}</em></summary>`
      + `<div class="tiles"></div></details>`;
  }).join("");
  box.querySelectorAll("details.icon-group").forEach((d) => {
    if (d.open) fillPackTiles(d);
    d.addEventListener("toggle", () => {
      const id = d.dataset.pack;
      sideState.openPacks = sideState.openPacks.filter((x) => x !== id);
      if (d.open) {
        fillPackTiles(d);
        sideState.openPacks.push(id);
      }
      saveSideState();
    });
  });
}

/* ---------------------------------------------------- 图标库「相册」分类 */
let libraryItems = null;

function libraryTile(it) {
  const name = escHtml(it.name);
  return `<span class="lib-cell" title="${name}" data-file="${name}">`
    + `<img loading="lazy" draggable="false" data-name="lib:${name}"`
    + ` src="/api/library/thumb/${encodeURIComponent(it.name)}?v=${it.mtime || 0}" alt="" /></span>`;
}

function renderLibrary() {
  const box = $("#lib-album");
  if (!box) return;
  const items = libraryItems || [];
  const cnt = $("#lib-count");
  if (cnt) cnt.textContent = String(items.length);
  box.innerHTML = items.length
    ? items.map(libraryTile).join("")
    : `<div class="side-empty small">空的：点右上角「＋ 添加」，或者在壁纸/幻灯片里选过的图会自动收进来。</div>`;
}

async function loadLibrary() {
  try {
    libraryItems = (await (await fetch("/api/library", { cache: "no-store" })).json()).items || [];
  } catch (_) {
    libraryItems = [];
  }
  renderLibrary();
}

function bindLibrary() {
  const add = $("#lib-add");
  if (!add || add.dataset.bound) return;
  add.dataset.bound = "1";
  add.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.multiple = true;
    input.addEventListener("change", async () => {
      const files = [...(input.files || [])];
      if (!files.length) return;
      const rels = [];
      for (const f of files) {
        try {
          const r = await upload(f);
          if (r && r.file) rels.push(r.file);
        } catch (_) { /* 掉一张不算错 */ }
      }
      if (rels.length) {
        const r = await post("/api/library/add", { files: rels });
        toast(`已收进相册 ${r.added || 0} 张`, 1600);
        libraryItems = r.items || [];
        renderLibrary();
      }
    });
    input.click();
  });
}

/* 相册瓦片的右键菜单：和图标库瓦片用同一套菜单（showLibMenu） */
function bindLibraryMenu() {
  document.addEventListener("contextmenu", (e) => {
    const cell = e.target.closest?.(".lib-album .lib-cell");
    if (!cell) return;
    e.preventDefault();
    e.stopPropagation();
    const file = cell.dataset.file;
    showLibMenu(e.clientX, e.clientY, [
      {
        label: "设为壁纸",
        onPick: async () => {
          const r = await post("/api/wallpaper/file", { file: `assets/library/${file}` });
          toast(r.ok ? "已设为壁纸" : "设壁纸失败", 1600);
          await refreshState();
        },
      },
      {
        label: "加到待机幻灯片",
        onPick: async () => {
          const r = await post("/api/album/add", { files: [`assets/library/${file}`] });
          toast(r.ok ? "已加到幻灯片" : "加不进去（可能不是图片）", 1800);
          await refreshState();
        },
      },
      { label: "打开图片位置", onPick: () => post("/api/library/reveal", { name: file }) },
      {
        label: "从相册删除",
        danger: true,
        onPick: async () => {
          if (!(await confirmDialog(`从相册里删掉「${escHtml(file)}」？<br><small>只删相册里的这份，原来的文件不动。</small>`))) return;
          const r = await post("/api/library/remove", { name: file });
          libraryItems = r.items || [];
          renderLibrary();
          toast("已从相册删除", 1400);
        },
      },
    ]);
  });
}

/* 相册分区支持把图片拖进来（和图标库、壁纸、幻灯片一样） */
function bindLibraryDrop() {
  const sec = document.querySelector('.side-sec[data-sec="library"]');
  const box = $("#lib-album");
  if (!sec || !box || sec.dataset.dropBound) return;
  sec.dataset.dropBound = "1";
  let depth = 0;
  const isFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  sec.addEventListener("dragenter", (e) => {
    if (!isFiles(e)) return;
    depth += 1;
    sec.classList.add("drop-hot");
  });
  sec.addEventListener("dragover", (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "copy";
  });
  sec.addEventListener("dragleave", (e) => {
    if (!isFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) sec.classList.remove("drop-hot");
  });
  sec.addEventListener("drop", async (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    sec.classList.remove("drop-hot");
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    const rels = [];
    for (const f of files) {
      if (!/\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name)) continue;
      try {
        const r = await upload(f);
        if (r && r.file) rels.push(r.file);
      } catch (_) { /* 掉一张不算错 */ }
    }
    if (!rels.length) {
      toast("相册只收图片（PNG / JPG / GIF / WebP / BMP）", 2200);
      return;
    }
    const r = await post("/api/library/add", { files: rels });
    libraryItems = r.items || [];
    renderLibrary();
    toast(`已收进相册 ${r.added || 0} 张`, 1600);
  });
}

/* --------------------------------------------------------------- 裁图工具 */
/* 控制台键位（视觉顺序）：上排 11-15、中排 6-10、下排 1-5 */
const CROP_KEYS = [11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4, 5];

function openCropTool() {
  if (document.querySelector(".crop-modal")) return;
  const box = document.createElement("div");
  box.className = "modal confirm-modal crop-modal";
  box.innerHTML = `<div class="confirm-card crop-card">
    <div class="crop-head">
      <h2>裁图工具</h2>
      <p>把一张图片拖到左边，右边就是 15 个屏幕键各自的画面。保存后会进「我的图标」，文件夹名就是图片名。</p>
    </div>
    <div class="crop-body">
      <div class="crop-left">
        <div class="crop-drop" id="crop-drop">
          <img id="crop-src" alt="" hidden />
          <div class="crop-empty" id="crop-empty">把图片拖到这里<br><span>或点一下选择文件 · PNG / JPG / GIF</span></div>
        </div>
        <div class="kv-line as-fit-row"><span>铺放</span>
          <span class="seg-group compact" id="crop-fit">
            <button class="seg on" data-fit="cover">填充</button>
            <button class="seg" data-fit="contain">完整</button>
            <button class="seg" data-fit="fill">拉伸</button>
          </span>
        </div>
        <div class="slider-row tight as-gap-row"><span>间隙</span>
          <input id="crop-gap" type="range" min="0" max="64" step="1" value="27" />
          <b id="crop-gap-val">27</b>
        </div>
        <p class="crop-name" id="crop-name">还没有选图片</p>
        <input type="file" id="crop-file" accept="image/*" hidden />
      </div>
      <div class="crop-grid" id="crop-grid">${CROP_KEYS.map((k) => `<div class="crop-cell" data-key="${k}"><img alt="" /><span>${k}</span></div>`).join("")}</div>
    </div>
    <div class="confirm-actions">
      <button class="seg" id="crop-cancel">取消</button>
      <button class="seg on" id="crop-save" disabled>保存到「我的图标」</button>
    </div>
  </div>`;
  document.body.appendChild(box);

  const drop = $("#crop-drop", box);
  const srcImg = $("#crop-src", box);
  const emptyEl = $("#crop-empty", box);
  const nameEl = $("#crop-name", box);
  const grid = $("#crop-grid", box);
  const saveBtn = $("#crop-save", box);
  const fileEl = $("#crop-file", box);
  const gapEl = $("#crop-gap", box);
  const gapVal = $("#crop-gap-val", box);

  let token = "";
  let fileRel = "";
  let baseName = "";
  let fit = "cover";
  let gap = 27;
  let busy = false;

  // 右侧 15 张的尺寸跟控制台预览区一致（同 cell 边长、同缝隙）
  const boardEl = document.querySelector(".ring-board");
  let cell = 70;
  let cellGap = 8;
  if (boardEl) {
    const cs = getComputedStyle(boardEl);
    const first = parseFloat((cs.gridTemplateColumns || "").split(/\s+/)[0]);
    const g0 = parseFloat(cs.columnGap);
    if (Number.isFinite(first) && first > 4) cell = Math.round(first);
    if (Number.isFinite(g0) && g0 >= 0) cellGap = Math.round(g0);
  }
  const room = Math.max(300, Math.min(620, window.innerWidth * 0.42));
  const want = 5 * cell + 4 * cellGap;
  if (want > room) {
    const k = room / want;
    cell = Math.max(24, Math.floor(cell * k));
    cellGap = Math.max(0, Math.round(cellGap * k));
  }
  const gridH = 3 * cell + 2 * cellGap;
  grid.style.gridTemplateColumns = `repeat(5, ${cell}px)`;
  grid.style.gridTemplateRows = `repeat(3, ${cell}px)`;
  grid.style.columnGap = `${cellGap}px`;
  grid.style.rowGap = `${cellGap}px`;
  grid.style.width = `${5 * cell + 4 * cellGap}px`;
  drop.style.height = `${gridH}px`;

  const cancelToken = () => {
    const old = token;
    token = "";
    if (old) post("/api/iconcrop/cancel", { token: old }).catch(() => {});
  };

  const close = () => {
    cancelToken();
    box.remove();
  };

  const paint = () => {
    saveBtn.disabled = !token || busy;
  };

  const preview = async () => {
    if (!fileRel) return;
    cancelToken();
    saveBtn.disabled = true;
    const r = await post("/api/iconcrop/preview", { file: fileRel, fit, gap, name: baseName });
    if (!r.ok || !r.token) {
      nameEl.textContent = r.reason || r.error || "这张图裁不了，换一张试试";
      saveBtn.disabled = true;
      return;
    }
    token = r.token;
    baseName = r.name || baseName;
    srcImg.src = r.src + `&v=${Date.now()}`;
    srcImg.hidden = false;
    emptyEl.hidden = true;
    nameEl.innerHTML = `保存为：<b>${escHtml(baseName)}</b>${r.animated ? ` · 动图 ${r.frames} 帧` : ""}`;
    const stamp = Date.now();
    grid.querySelectorAll(".crop-cell").forEach((cell) => {
      const k = Number(cell.dataset.key);
      const im = cell.querySelector("img");
      im.src = `${r.tiles[k - 1]}&v=${stamp}`;
    });
    paint();
  };

  const takeFiles = async (files) => {
    const f = (files || [])[0];
    if (!f) return;
    if (!/^image\//.test(f.type || "")) {
      toast("只能放图片（PNG / JPG / GIF）", 1800);
      return;
    }
    nameEl.textContent = "正在读入…";
    baseName = (f.name || "").replace(/\.[^.]+$/, "").slice(0, 60);
    try {
      const r = await upload(f);
      fileRel = r.file;
      await preview();
    } catch (err) {
      nameEl.textContent = `读入失败：${err.message || err}`;
    }
  };

  drop.addEventListener("click", () => fileEl.click());
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    takeFiles(e.dataTransfer?.files);
  });
  fileEl.addEventListener("change", () => {
    takeFiles(fileEl.files);
    fileEl.value = "";
  });

  box.querySelectorAll("#crop-fit [data-fit]").forEach((b) => {
    b.addEventListener("click", async () => {
      fit = b.dataset.fit;
      box.querySelectorAll("#crop-fit [data-fit]").forEach((x) => x.classList.toggle("on", x === b));
      if (fileRel) await preview();
    });
  });

  const pushGap = throttle(async (v) => {
    gap = Number(v);
    if (fileRel) await preview();
  }, 320);
  gapEl.addEventListener("input", () => {
    gapVal.textContent = gapEl.value;
    pushGap(gapEl.value);
  });

  saveBtn.addEventListener("click", async () => {
    if (!token || busy) return;
    busy = true;
    saveBtn.disabled = true;
    saveBtn.textContent = "正在保存…";
    const r = await post("/api/iconcrop/save", { token, name: baseName });
    busy = false;
    saveBtn.textContent = "保存到「我的图标」";
    if (!r.ok) {
      toast(r.reason || r.error || "保存失败", 2600);
      paint();
      return;
    }
    token = "";
    toast(`已存进「我的图标」：${r.pack.name}（${r.pack.count} 个）`, 2400);
    await loadIcons(true);
    box.remove();
  });

  $("#crop-cancel", box).addEventListener("click", close);
  box.addEventListener("click", (e) => { if (e.target === box) close(); });
  document.addEventListener("keydown", function onEsc(e) {
    if (!document.body.contains(box)) { document.removeEventListener("keydown", onEsc); return; }
    if (e.key === "Escape") { document.removeEventListener("keydown", onEsc); close(); }
  });
}

async function loadPlugins(refresh = false) {
  const box = $("#plugin-list");
  if (!box) return;
  if (!pluginItems || refresh) {
    try {
      pluginItems = (await (await fetch("/api/plugins", { cache: "no-store" })).json()).plugins || [];
    } catch (_) {
      pluginItems = [];
    }
  }
  const q = ($("#icon-search")?.value || "").toLowerCase().trim();
  const match = (p) => !q || [p.name, p.author, p.id, ...p.actions.map((a) => a.name)]
    .some((v) => String(v || "").toLowerCase().includes(q));
  const list = pluginItems.filter((p) => !p.custom).filter(match);
  const cnt = $("#plugin-count");
  if (cnt) cnt.textContent = String(pluginItems.filter((p) => !p.custom).length);
  if (!list.length) {
    box.innerHTML = `<div class="side-empty">${pluginItems.length ? "没有匹配的插件" : "没找到官方插件"}</div>`;
    return;
  }
  const row = (p) => {
    const icon = p.hasIcon ? `<img class="pl-icon" src="${pluginIconUrl(p.id, -1)}" alt="" />` : `<span class="pl-icon none">◇</span>`;
    const acts = p.actions.map((a, i) =>
      `<div class="pl-act${a.supported === false ? " unsupported" : ""}" data-uuid="${escHtml(a.uuid || "")}" title="${escHtml(a.supported === false
        ? `${a.name}：官方程序内置的功能，控制台暂不支持` : (a.tooltip || a.name))}">`
      + (a.hasIcon
        ? `<img loading="lazy" draggable="false" data-name="plugin:${escHtml(p.id)}/${i}" src="${pluginIconUrl(p.id, i)}" alt="" />`
        : `<span class="pl-act-noimg">—</span>`)
      + `<span>${escHtml(a.name)}</span></div>`).join("");
    return `<details class="plugin${p.downloaded ? " downloaded" : ""}" data-plugin="${escHtml(p.id)}">`
      + `<summary>${icon}<span class="pl-meta"><b>${escHtml(p.name)}</b>`
      + `<small>${escHtml([p.author, p.version && `v${p.version}`].filter(Boolean).join(" · "))}</small></span>`
      + `<em>${p.actions.length}</em></summary>`
      + (p.description ? `<p class="pl-desc">${escHtml(p.description)}</p>` : "")
      + `<div class="pl-acts">${acts || "<span class='side-empty'>没有动作</span>"}</div></details>`;
  };
  const groups = [
    ["downloaded", "已下载", list.filter((p) => p.downloaded)],
    ["builtin", "官方内置", list.filter((p) => !p.downloaded)],
  ];
  // 分组也可折叠：默认展开「已下载」、收起「官方内置」；搜索时全部展开
  box.innerHTML = groups.filter(([, , items]) => items.length).map(([id, label, items]) => {
    const open = q || (sideState.pluginGroups?.[id] ?? id === "downloaded");
    return `<details class="pl-groupbox" data-group="${id}"${open ? " open" : ""}>`
      + `<summary class="pl-group"><i class="chev"></i><span>${label}</span><em>${items.length}</em></summary>`
      + `${items.map(row).join("")}</details>`;
  }).join("");
  box.querySelectorAll(".pl-groupbox").forEach((d) => {
    d.addEventListener("toggle", () => {
      if (q) return;
      sideState.pluginGroups = { ...(sideState.pluginGroups || {}), [d.dataset.group]: d.open };
      saveSideState();
    });
  });
}

/* ------------------------------------------------- 图标库 / 插件库的右键菜单 */
let libMenuEl = null;

function hideLibMenu() {
  if (libMenuEl) {
    libMenuEl.remove();
    libMenuEl = null;
  }
}

function showLibMenu(x, y, items) {
  hideLibMenu();
  if (!items.length) return;
  const menu = document.createElement("div");
  menu.className = "key-menu lib-menu";
  menu.innerHTML = items.map((it, i) =>
    `<button data-i="${i}"${it.danger ? ' class="danger"' : ""}>${escHtml(it.label)}</button>`).join("");
  document.body.appendChild(menu);
  libMenuEl = menu;
  const w = 230, h = items.length * 34 + 10;
  menu.style.left = `${Math.max(6, Math.min(x, window.innerWidth - w - 8))}px`;
  menu.style.top = `${Math.max(6, Math.min(y, window.innerHeight - h - 8))}px`;
  menu.addEventListener("pointerdown", (e) => e.stopPropagation());
  menu.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const it = items[Number(btn.dataset.i)];
    hideLibMenu();
    it?.onPick?.();
  });
}

document.addEventListener("pointerdown", (e) => {
  if (libMenuEl && !libMenuEl.contains(e.target)) hideLibMenu();
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideLibMenu();
});

async function deleteIconFile(packId, fname) {
  if (!(await confirmDialog(`删除图标「${escHtml(fname)}」？<br><br>文件会移进回收站，可以从回收站还原。`))) return;
  const r = await post("/api/iconpacks/delete", { pack: packId, file: fname });
  toast(r.message || (r.ok ? "已删除" : "删除失败"), 2600);
  if (r.ok) await loadIcons(true);
}

async function deleteIconPack(packId, packName) {
  if (packId === "local") {
    toast("本地图标是单个文件，请在小图标上右键逐个删除", 2400);
    return;
  }
  if (!(await confirmDialog(`删除整个图标包「${escHtml(packName)}」？<br><br>`
    + `官方 Stream Dock 里也会消失，需要时得在商店重新下载。<br>文件会移进回收站。`))) return;
  const r = await post("/api/iconpacks/delete", { pack: packId });
  toast(r.message || (r.ok ? "已删除" : "删除失败"), 2800);
  if (r.ok) await loadIcons(true);
}

async function deletePluginAction(pid, uuid, aname) {
  if (!(await confirmDialog(`删除动作「${escHtml(aname)}」？<br><br>`
    + `会改写这个插件的 manifest.json（原文件备份成 manifest.json.m18bak），`
    + `官方软件里也会看不到这个动作。`))) return;
  const r = await post("/api/plugins/delete-action", { plugin: pid, uuid });
  toast(r.message || (r.ok ? "已删除" : "删除失败"), 3200);
  if (r.ok) {
    await loadPlugins(true);
    await refreshState();
  }
}

async function deletePlugin(pid, pname) {
  if (!(await confirmDialog(`删除整个插件「${escHtml(pname)}」？<br><br>`
    + `插件文件夹会移进回收站；绑着它的按键会被清空。<br>`
    + `官方 Stream Dock 里也会消失，需要时得在商店重新下载。`))) return;
  const r = await post("/api/plugins/delete", { plugin: pid });
  toast(r.message || (r.ok ? "已删除" : "删除失败"), 3000);
  if (r.ok) {
    await loadPlugins(true);
    await loadIcons(true);
    await refreshState();
  }
}

async function revealIcon(packId, fname) {
  const r = await post("/api/iconpacks/reveal", fname ? { pack: packId, file: fname } : { pack: packId });
  if (!r.ok) toast("没找到这个图标：可能已经被删掉了", 2400);
}

async function revealPlugin(pid) {
  const r = await post("/api/plugins/reveal", { plugin: pid });
  if (!r.ok) toast("没找到这个插件：可能已经被删掉了", 2400);
}

function bindLibMenus() {
  $("#icon-groups")?.addEventListener("contextmenu", (e) => {
    const det = e.target.closest("details.icon-group");
    if (!det) return;
    e.preventDefault();
    e.stopPropagation();
    const packId = det.dataset.pack || "";
    const packName = det.querySelector(".g-name")?.textContent?.trim() || packId;
    const items = [];
    const img = e.target.closest("img[data-name]");
    if (img && img.dataset.name.startsWith("pack:")) {
      const rest = img.dataset.name.slice(5);
      const cut = rest.indexOf("/");
      const pid = cut === -1 ? rest : rest.slice(0, cut);
      const fname = cut === -1 ? "" : rest.slice(cut + 1);
      if (fname) {
        items.push({ label: `删除这个图标（${fname.replace(/\.[^.]+$/, "")}）`, danger: true,
                     onPick: () => deleteIconFile(pid, fname) });
        items.push({ label: "打开所在文件夹", onPick: () => revealIcon(pid, fname) });
      }
      items.push({ label: `删除整个图标包（${packName}）`, danger: true,
                   onPick: () => deleteIconPack(pid, packName) });
      if (!fname) items.push({ label: "打开所在文件夹", onPick: () => revealIcon(pid) });
    } else {
      items.push({ label: `删除整个图标包（${packName}）`, danger: true,
                   onPick: () => deleteIconPack(packId, packName) });
      items.push({ label: "打开所在文件夹", onPick: () => revealIcon(packId) });
    }
    showLibMenu(e.clientX, e.clientY, items);
  });

  $("#plugin-list")?.addEventListener("contextmenu", (e) => {
    const row = e.target.closest("details.plugin");
    if (!row) return;
    e.preventDefault();
    e.stopPropagation();
    const pid = row.dataset.plugin || "";
    const pname = row.querySelector(".pl-meta b")?.textContent?.trim() || pid;
    const act = e.target.closest(".pl-act");
    const items = [];
    if (act && act.dataset.uuid) {
      const aname = act.querySelector("span")?.textContent?.trim() || act.dataset.uuid;
      items.push({ label: `删除这个动作（${aname}）`, danger: true,
                   onPick: () => deletePluginAction(pid, act.dataset.uuid, aname) });
      items.push({ label: "打开所在文件夹", onPick: () => revealPlugin(pid) });
    }
    items.push({ label: `删除整个插件（${pname}）`, danger: true, onPick: () => deletePlugin(pid, pname) });
    if (!act) items.push({ label: "打开所在文件夹", onPick: () => revealPlugin(pid) });
    showLibMenu(e.clientX, e.clientY, items);
  });
}

/* 相册图片能拖到哪儿：按键、待机幻灯片、相册场景编辑区、壁纸 */
function libDropZoneAt(x, y) {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const cell = el.closest(".ring-board .cell[data-key]");
  if (cell) return { kind: "key", el: cell };
  if (el.closest("#sel-thumb")) return { kind: "key", el: $("#sel-thumb") };
  const ed = el.closest("#album-editor");
  if (ed) return { kind: "sceneAlbum", el: ed };
  const card = el.closest("#album-card");
  if (card) return { kind: "standbyAlbum", el: card };
  const wp = el.closest("#wp-grid");
  if (wp) return { kind: "wallpaper", el: wp };
  return null;
}

/* 从相册分类拖一张图出去：加进相册场景 / 待机幻灯片 / 设为壁纸 */
async function dropLibraryImage(name, zone) {
  const rel = `assets/library/${name}`;
  if (zone.kind === "wallpaper") {
    const r = await post("/api/wallpaper/file", { file: rel });
    toast(r.ok ? "已设为壁纸" : "设壁纸失败", 1600);
    await refreshState();
    return;
  }
  if (zone.kind === "standbyAlbum") {
    const r = await post("/api/album/add", { files: [rel] });
    if (r && r.items) renderAlbum(r);
    toast(r.ok ? "已加到待机幻灯片" : "加不进去（可能不是图片）", 1800);
    await refreshState();
    return;
  }
  if (zone.kind === "sceneAlbum") {
    const scene = activeAlbumScene();
    if (!scene) {
      toast("先在左上角选中一个相册场景，再拖进来", 2200);
      return;
    }
    const r = await post("/api/album/add", { files: [rel], scene });
    albumSceneSig = "";
    renderAlbumEditor(r, scene);
    window.m18ReloadScenes?.();
    toast(r.ok ? `已加入相册场景「${scene}」` : "加入失败", 1800);
  }
}

/* 图标瓦片 / 插件动作图标：单击 = 应用到选中键，拖动 = 放到目标键（事件委托，几千个图标只绑一次） */
function bindIconDrag(container) {
  container.addEventListener("pointerdown", (e) => {
    // 相册瓦片的 img 外面还包着一层 .lib-cell（全局 img 设了 pointer-events:none）
    let img = e.target.closest("img[data-name]");
    if (!img) img = e.target.closest(".lib-cell")?.querySelector("img[data-name]") || null;
    if (!img || e.button !== 0) return;
    e.preventDefault();
    const start = { x: e.clientX, y: e.clientY };
    let dragging = false;
    const onMove = (ev) => {
      if (!dragging && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 6) {
        dragging = true;
        startGhost(img, ev.clientX, ev.clientY);
      }
      if (!dragging) return;
      moveGhost(ev.clientX, ev.clientY);
      clearDropHover();
      if (img.dataset.name.startsWith("lib:")) {
        const zone = libDropZoneAt(ev.clientX, ev.clientY);
        if (zone && zone.kind !== "key") {
          zone.el.classList.add("lib-drop-hot");
          return;
        }
      }
      const t = dropTargetAt(ev.clientX, ev.clientY);
      if (t && !t.closest(".side-scroll")) t.classList.add("drop-hover");
      // 悬停在某个屏幕键上：设备上的这个键也临时换成这张图，移开就还原
      const hk = t?.dataset?.key ? Number(t.dataset.key) : 0;
      const name = img.dataset.name;
      if (hk >= 1 && hk <= 15) showDockPreview(hk, name);
      else if (pendingIcon) showDockPreview(pendingIcon.key, dockPreviewSrc(pendingIcon));
      else hideDockPreview();
    };
    const onUp = async (ev) => {
      document.removeEventListener("pointermove", onMove, true);
      document.removeEventListener("pointerup", onUp, true);
      endGhost();
      clearDropHover();
      {
        const t0 = dragging ? dropTargetAt(ev.clientX, ev.clientY) : null;
        const k0 = t0?.dataset?.key ? Number(t0.dataset.key) : 0;
        if (dragging && !(k0 >= 1 && k0 <= 15)) {
          if (pendingIcon) showDockPreview(pendingIcon.key, dockPreviewSrc(pendingIcon));
          else hideDockPreview();
        }
      }
      let key = 0;
      if (dragging) {
        const t = dropTargetAt(ev.clientX, ev.clientY);
        if (t?.classList.contains("cp-icon")) {
          setCpIcon(img.dataset.name, img.src);
          toast("已设为自制插件的图标（保存后生效）", 1400);
          return;
        }
        // 相册分类的图：可以拖进相册场景 / 待机幻灯片 / 壁纸
        if (img.dataset.name.startsWith("lib:")) {
          const zone = libDropZoneAt(ev.clientX, ev.clientY);
          if (zone && zone.kind !== "key") {
            await dropLibraryImage(img.dataset.name.slice(4), zone);
            return;
          }
        }
        if (t?.dataset?.key) key = Number(t.dataset.key);
        else if (t?.classList.contains("thumb")) key = selectedKey;
      } else {
        if (!selectedKey) {
          toast("先选一个按键，或直接把图标拖到按键上", 1600);
          return;
        }
        key = selectedKey;
      }
      if (!key) return;
      if (key > 15) {
        toast("底部三个实体键没有屏幕，不能放图标", 1600);
        return;
      }
      if (img.dataset.name.startsWith("plugin:")) {
        const m = img.dataset.name.slice(7).match(/^(.*)\/(\d+)$/);
        if (!m) return;
        const label = img.closest(".pl-act")?.querySelector("span")?.textContent || "插件动作";
        if (key !== selectedKey) selectKey(key);
        setPendingIcon(key, editGesture, {
          kind: "plugin", plugin: m[1], index: Number(m[2]), src: img.src, label,
        });
        toast(`已放到按键 ${key}：${label}`, 1600);
        return;
      }
      if (key !== selectedKey) selectKey(key);
      setPendingIcon(key, editGesture, {
        kind: "icon", name: img.dataset.name, src: img.src, label: "图标",
      });
      toast(`已放到按键 ${key}`, 1400);
    };
    document.addEventListener("pointermove", onMove, true);
    document.addEventListener("pointerup", onUp, true);
  });
}


/* ------------------------------------------------------------------ 自制插件 */
const CP_TEMPLATES = {
  counter: { name: "计数器", code: `// 计数器：每按一次 +1，数字显示在按键上；长按 1 秒清零。重启后也记得。
M18.on("willAppear", (key) => key.setTitle(key.settings.count || 0));

M18.on("keyDown", (key) => {
  key.downAt = Date.now();
});

M18.on("keyUp", (key) => {
  const n = Date.now() - key.downAt > 1000 ? 0 : (key.settings.count || 0) + 1;
  key.save({ count: n });
  key.setTitle(n);
  if (n === 0) key.showOk();
});
` },
  clock: { name: "数字时钟", code: `// 数字时钟：每秒在按键上画一次时间和日期
function paint(key) {
  key.draw((g, s) => {
    const d = new Date();
    g.fillStyle = "#0f1722";
    g.fillRect(0, 0, s, s);
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillStyle = "#7fd4ff";
    g.font = "bold 46px Segoe UI";
    g.fillText(d.toTimeString().slice(0, 5), s / 2, s / 2 - 12);
    g.fillStyle = "#c8d6e5";
    g.font = "22px Microsoft YaHei";
    g.fillText(\`\${d.getMonth() + 1}月\${d.getDate()}日 周\${"日一二三四五六"[d.getDay()]}\`, s / 2, s / 2 + 34);
  });
}

M18.on("willAppear", (key) => {
  paint(key);
  key.every(1000, paint);
});
` },
  web: { name: "网络数据", code: `// 定时请求一个网络接口，把结果显示在按键上；按一下立即刷新。
// 这里用 wttr.in 查天气，换成你自己的地址和解析方式即可。
const CITY = "Shanghai";

async function refresh(key) {
  try {
    // M18.http 由控制台代发请求，不受跨域限制（直接用 fetch 很多接口会被拦）
    const r = await M18.http(\`https://wttr.in/\${CITY}?format=%c%t\`);
    key.setTitle(r.text.trim());
  } catch (e) {
    key.setTitle("请求失败");
    M18.log("请求失败：", e);
  }
}

M18.on("willAppear", (key) => {
  refresh(key);
  key.every(10 * 60 * 1000, refresh);   // 10 分钟刷新一次
});
M18.on("keyDown", refresh);
` },
  toggle: { name: "开关", code: `// 开关：按一下在「开 / 关」之间切换，按键颜色跟着变，并发送对应的热键。
const ON_KEYS = "ctrl+alt+1";
const OFF_KEYS = "ctrl+alt+2";

function paint(key) {
  const on = !!key.settings.on;
  key.draw((g, s) => {
    g.fillStyle = on ? "#1f9d55" : "#3a3f4b";
    g.fillRect(0, 0, s, s);
    g.fillStyle = "#fff";
    g.font = "bold 52px Microsoft YaHei";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(on ? "开" : "关", s / 2, s / 2);
  });
}

M18.on("willAppear", paint);
M18.on("keyDown", (key) => {
  const on = !key.settings.on;
  key.save({ on });
  M18.hotkey(on ? ON_KEYS : OFF_KEYS);
  paint(key);
});
` },
  blank: { name: "空白", code: `// 在这里写你的插件。左下角「API 速查」里有可用的函数。
M18.on("willAppear", (key) => {
  key.setTitle("Hello");
});

M18.on("keyDown", (key) => {
  M18.log("按下了", key.coordinates);
  key.showOk();
});
` },
};

let cpItems = [];
let cpCurrent = { id: null, icon: "", template: "counter", dirty: false };

function cpLog(message, cls = "") {
  const box = $("#cp-console");
  if (!box) return;
  const line = document.createElement("div");
  line.className = `cp-line ${cls}`;
  line.textContent = `[${new Date().toTimeString().slice(0, 8)}] ${message}`;
  box.appendChild(line);
  while (box.childElementCount > 200) box.firstElementChild.remove();
  box.scrollTop = box.scrollHeight;
}

function setCpIcon(name, src) {
  cpCurrent.icon = name;
  const img = $("#cp-icon img");
  img.src = src;
  img.hidden = !src;
  $("#cp-icon span").hidden = !!src;
}

function openCustom(item) {
  if (item) {
    cpCurrent = { id: item.id, icon: "", template: item.template || "", dirty: false };
    $("#cp-name").value = item.name || "";
    $("#cp-action").value = item.actionName && item.actionName !== item.name ? item.actionName : "";
    $("#cp-desc").value = item.description || "";
    $("#cp-template").value = item.template || "blank";
    $("#cp-code").value = item.code || "";
    const src = item.hasIcon ? `${pluginIconUrl(item.id, -1)}?v=${item.mtime}` : "";
    setCpIcon("", src);
  } else {
    cpCurrent = { id: null, icon: "", template: "counter", dirty: false };
    $("#cp-name").value = "";
    $("#cp-action").value = "";
    $("#cp-desc").value = "";
    $("#cp-template").value = "counter";
    $("#cp-code").value = CP_TEMPLATES.counter.code;
    setCpIcon("", "");
  }
  $("#cp-delete").disabled = !cpCurrent.id;
  $("#cp-reveal").disabled = !cpCurrent.id;
  $("#cp-status").textContent = cpCurrent.id ? "" : "新插件（未保存）";
  $("#cp-console").innerHTML = "";
  cpLog(cpCurrent.id ? `正在编辑「${item.name}」` : "新建插件：改好名称和代码后点「保存并运行」", "muted");
  renderCustomList();
}

function renderCustomList() {
  const list = $("#cp-list");
  if (list) {
    list.innerHTML = cpItems.map((it) => `<button class="cp-item${it.id === cpCurrent.id ? " on" : ""}" data-id="${escHtml(it.id)}">`
      + (it.hasIcon ? `<img src="${pluginIconUrl(it.id, -1)}?v=${it.mtime}" alt="" />` : "<i></i>")
      + `<span>${escHtml(it.name)}</span></button>`).join("")
      || `<div class="side-empty">还没有自制插件，点「新建」开始。</div>`;
    list.querySelectorAll(".cp-item").forEach((b) => b.addEventListener("click", () => {
      const it = cpItems.find((x) => x.id === b.dataset.id);
      if (it) openCustom(it);
    }));
  }
  const side = $("#custom-plugin-list");
  if (side) {
    side.innerHTML = cpItems.map((it) => `<div class="pl-act" title="${escHtml(it.description || it.name)}">`
      + `<img loading="lazy" draggable="false" data-name="plugin:${escHtml(it.id)}/0" src="${pluginIconUrl(it.id, 0)}?v=${it.mtime}" alt="" />`
      + `<span>${escHtml(it.actionName || it.name)}</span></div>`).join("")
      || `<span class="side-empty">还没有</span>`;
  }
  const cnt = $("#custom-count");
  if (cnt) cnt.textContent = String(cpItems.length);
}

async function loadCustomPlugins() {
  try {
    cpItems = (await post("/api/custom-plugins", {})).items || [];
  } catch (_) {
    cpItems = [];
  }
  renderCustomList();
}

function bindCustomLab() {
  /* 自制插件区默认收起，点「展开」才占地方 */
  const cpFold = $("#cp-fold");
  const cpBody = $("#cp-body");
  const setCpOpen = (open) => {
    if (!cpBody || !cpFold) return;
    cpBody.hidden = !open;
    cpFold.textContent = open ? "收起" : "展开";
    cpFold.classList.toggle("on", open);
  };
  setCpOpen(false);
  cpFold?.addEventListener("click", () => setCpOpen(!!cpBody?.hidden));

  $("#cp-agent").addEventListener("click", () => {
    setCpOpen(true);
    agentDialog().catch((e) => toast(`打开失败：${e.message || e}`));
  });
  const tpl = $("#cp-template");
  tpl.innerHTML = Object.entries(CP_TEMPLATES).map(([k, t]) => `<option value="${k}">${t.name}</option>`).join("");
  tpl.addEventListener("change", async () => {
    const t = CP_TEMPLATES[tpl.value];
    if (!t) return;
    if (cpCurrent.dirty && !(await confirmDialog(`用「${t.name}」模板替换当前代码？`))) {
      tpl.value = cpCurrent.template || "blank";
      return;
    }
    $("#cp-code").value = t.code;
    cpCurrent.template = tpl.value;
    cpCurrent.dirty = false;
    if (!$("#cp-name").value.trim()) $("#cp-name").value = t.name;
  });
  const code = $("#cp-code");
  code.addEventListener("input", () => { cpCurrent.dirty = true; });
  code.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {                       // Tab 插两个空格，而不是跳出输入框
      e.preventDefault();
      const { selectionStart: a, selectionEnd: b, value } = code;
      code.value = `${value.slice(0, a)}  ${value.slice(b)}`;
      code.selectionStart = code.selectionEnd = a + 2;
      cpCurrent.dirty = true;
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      $("#cp-save").click();
    }
  });
  $("#cp-icon").addEventListener("click", () => $("#cp-icon-file").click());
  $("#cp-icon-file").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const url = await readAsDataURL(f);
    setCpIcon(url, url);
  });
  $("#cp-new").addEventListener("click", () => { setCpOpen(true); openCustom(null); });
  $("#custom-new-side").addEventListener("click", () => {
    setCpOpen(true);
    openCustom(null);
    $("#custom-lab").scrollIntoView({ behavior: "smooth", block: "start" });
    setTimeout(() => $("#cp-name").focus(), 400);
  });
  $("#cp-save").addEventListener("click", async () => {
    const name = $("#cp-name").value.trim();
    if (!name) {
      toast("先给插件起个名字", 1500);
      $("#cp-name").focus();
      return;
    }
    $("#cp-status").textContent = "保存中…";
    const r = await post("/api/custom-plugins/save", {
      id: cpCurrent.id, name, actionName: $("#cp-action").value.trim(), description: $("#cp-desc").value.trim(),
      template: $("#cp-template").value, code: $("#cp-code").value, icon: cpCurrent.icon,
    });
    if (!r.ok) {
      $("#cp-status").textContent = r.error || "保存失败";
      toast(r.error || "保存失败", 1800);
      return;
    }
    const isNew = !cpCurrent.id;
    cpCurrent.id = r.id;
    cpCurrent.icon = "";
    cpCurrent.dirty = false;
    $("#cp-status").textContent = `已保存 ${new Date().toTimeString().slice(0, 8)}`;
    cpLog(isNew ? "已创建并启动；从左侧「自制插件」把它拖到按键上试试" : "已保存，正在运行的实例已热重载", "ok");
    await loadCustomPlugins();
    await loadPlugins(true);
    await refreshState();
    $("#cp-delete").disabled = false;
    $("#cp-reveal").disabled = false;
  });
  $("#cp-delete").addEventListener("click", async () => {
    if (!cpCurrent.id) return;
    const it = cpItems.find((x) => x.id === cpCurrent.id);
    if (!(await confirmDialog(`删除自制插件「${escHtml(it?.name || "")}」？绑定了它的按键会失效。`))) return;
    await post("/api/custom-plugins/delete", { id: cpCurrent.id });
    await loadCustomPlugins();
    await loadPlugins(true);
    openCustom(null);
  });
  $("#cp-reveal").addEventListener("click", () => cpCurrent.id && post("/api/custom-plugins/reveal", { id: cpCurrent.id }));
  loadCustomPlugins().then(() => openCustom(cpItems[0] || null));
}

/* 侧栏分区折叠（记住状态） */
function bindSidebar() {
  document.querySelectorAll(".side-sec[data-sec]").forEach((sec) => {
    const id = sec.dataset.sec;
    sec.classList.toggle("collapsed", !!sideState.collapsed[id]);
    sec.querySelector(".sec-toggle")?.addEventListener("click", () => {
      const now = !sec.classList.contains("collapsed");
      sec.classList.toggle("collapsed", now);
      sideState.collapsed[id] = now;
      saveSideState();
    });
  });
  $("#icon-collapse")?.addEventListener("click", () => {
    sideState.openPacks = [];
    saveSideState();
    document.querySelectorAll("#icon-groups details[open]").forEach((d) => { d.open = false; });
  });
  bindIconDrag($("#icon-groups"));
  bindIconDrag($("#lib-album"));
  bindIconDrag($("#plugin-list"));
  bindIconDrag($("#custom-plugin-list"));
  bindLibMenus();
}

/* ------------------------------------------------------- 拖拽幽灵（通用） */
let ghostEl = null;
function startGhost(srcEl, x, y, cls) {
  endGhost();
  ghostEl = srcEl.cloneNode(true);
  ghostEl.classList.add("drag-ghost");
  if (cls) ghostEl.classList.add(cls);
  ghostEl.style.left = `${x}px`;
  ghostEl.style.top = `${y}px`;
  document.body.appendChild(ghostEl);
}
function moveGhost(x, y) {
  if (ghostEl) {
    ghostEl.style.left = `${x}px`;
    ghostEl.style.top = `${y}px`;
  }
}
function endGhost() {
  if (ghostEl) ghostEl.remove();
  ghostEl = null;
}
function dropTargetAt(x, y) {
  const el = document.elementFromPoint(x, y);
  return el ? el.closest(".cell, .ring-btn, .thumb, .icon-group img, .cp-icon") : null;
}
function clearDropHover() {
  document.querySelectorAll(".drop-hover").forEach((n) => n.classList.remove("drop-hover"));
  document.querySelectorAll(".lib-drop-hot").forEach((n) => n.classList.remove("lib-drop-hot"));
}

/* ---------------------------------------------------------- 按键长按/右键 */
let keyMenuEl = null;
let swapSource = null;
let swapHover = null;
let swapTimer = null;

function ensureKeyMenu() {
  if (keyMenuEl) return keyMenuEl;
  keyMenuEl = document.createElement("div");
  keyMenuEl.className = "key-menu";
  keyMenuEl.hidden = true;
  keyMenuEl.innerHTML = `
    <button data-act="copy">复制</button>
    <button data-act="paste">粘贴</button>
    <button data-act="del">删除</button>`;
  document.body.appendChild(keyMenuEl);
  keyMenuEl.addEventListener("pointerdown", (e) => e.stopPropagation());
  keyMenuEl.addEventListener("click", async (e) => {
    const act = e.target.closest("button")?.dataset.act;
    const key = Number(keyMenuEl.dataset.key || 0);
    if (!act || !key) return;
    hideKeyMenu();
    if (act === "copy") {
      const r = await post("/api/key/copy", { key });
      toast(r.ok ? "已复制该按键" : "这个按键是空的", 1400);
    } else if (act === "paste") {
      const r = await post("/api/key/paste", { key });
      toast(r.ok ? "已粘贴" : "剪贴板是空的", 1400);
      if (r.ok) await refreshState();
    } else if (act === "del") {
      dropPendingForKey(key);
      await post("/api/key/clear-all", { key });
      toast("已清除该按键", 1200);
      await refreshState();
    }
  });
  return keyMenuEl;
}

function showKeyMenu(x, y, key) {
  const menu = ensureKeyMenu();
  menu.dataset.key = String(key);
  menu.hidden = false;
  const w = 96, h = 108;
  menu.style.left = `${Math.min(x, window.innerWidth - w - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - h - 8)}px`;
}

function hideKeyMenu() {
  if (keyMenuEl) keyMenuEl.hidden = true;
  if (thumbMenuEl) thumbMenuEl.hidden = true;
}

/* 插件设置页（Property Inspector）：iframe 加载插件自带的设置网页，按官方协议注册 */
async function openPluginSettings(key) {
  const r = await post("/api/plugin/pi", { key });
  if (!r.ok) {
    toast(r.error || "打不开插件设置", 1800);
    return;
  }
  const modal = document.createElement("div");
  modal.className = "modal pi-modal";
  modal.innerHTML = `<div class="modal-card pi-card">
      <div class="modal-head"><h2>插件设置 · ${escHtml(r.name)}（按键 ${key}）</h2>
        <button class="seg small" data-close>关闭</button></div>
      <iframe class="pi-frame" title="插件设置"></iframe>
    </div>`;
  document.body.appendChild(modal);
  const frame = modal.querySelector("iframe");
  // 设置页由插件专用端口提供（和控制台不同源，调不了控制台接口），中转页负责注册，结果用 postMessage 告诉这里
  const pluginOrigin = new URL(r.frame).origin;
  const onMsg = (e) => {
    if (e.origin !== pluginOrigin || e.source !== frame.contentWindow || e.data?.type !== "m18-pi") return;
    if (e.data.error) toast(e.data.error, 2200);
  };
  window.addEventListener("message", onMsg);
  frame.src = r.frame + "#" + encodeURIComponent(JSON.stringify({
    url: r.url, port: r.port, uuid: r.uuid, info: r.info, actionInfo: r.actionInfo,
  }));
  const close = async () => {
    window.removeEventListener("message", onMsg);
    modal.remove();                       // iframe 卸载即断开设置页连接
    await post("/api/plugin/pi-closed", { key });
    await refreshState();
  };
  modal.querySelector("[data-close]").addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
}

/* 按键编辑预览区右键：在资源管理器里定位图标（原始来源 / 控制台里的副本） */
let thumbMenuEl = null;
async function showThumbMenu(x, y) {
  if (!selectedKey) {
    toast("先选一个按键", 1200);
    return;
  }
  const info = await post("/api/key/image-info", { key: selectedKey });
  if (!info.ok) {
    toast("这个按键没有设置图标", 1400);
    return;
  }
  if (!thumbMenuEl) {
    thumbMenuEl = document.createElement("div");
    thumbMenuEl.className = "key-menu";
    document.body.appendChild(thumbMenuEl);
    thumbMenuEl.addEventListener("pointerdown", (e) => e.stopPropagation());
    thumbMenuEl.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn || btn.disabled) return;
      thumbMenuEl.hidden = true;
      const r = await post("/api/key/reveal", { key: Number(thumbMenuEl.dataset.key), target: btn.dataset.target });
      if (!r.ok) toast("打不开这个位置（文件可能已被移动或删除）", 1800);
      else if (btn.dataset.target === "export") toast(`已导出：${r.path}`, 2600);
    });
  }
  const items = [];
  if (info.sameFile) {
    items.push(`<button data-target="source" title="${escHtml(info.source)}">打开图标位置</button>`);
  } else if (info.encrypted) {
    // 官方场景图标是加密的 .mbg：主入口给解密后的普通 GIF/PNG，原文件放在后面
    items.push(`<button data-target="copy" title="${escHtml(info.copy)}">打开图标位置（已解密，可直接查看）</button>`);
    if (info.sourceExists) {
      items.push(`<button data-target="source" title="${escHtml(info.source)}">打开官方原文件（加密格式）</button>`);
    }
  } else {
    items.push(info.sourceExists
      ? `<button data-target="source" title="${escHtml(info.source)}">打开图标位置</button>`
      : `<button disabled title="来源未知（例如从浏览器拖入的文件）">打开图标位置（来源未知）</button>`);
    items.push(`<button data-target="copy" title="${escHtml(info.copy)}">打开控制台副本位置</button>`);
  }
  items.push(`<button data-target="export" title="复制到「图片 › M18 图标」，按场景和键号命名">导出到图片文件夹</button>`);
  thumbMenuEl.innerHTML = items.join("");
  thumbMenuEl.dataset.key = String(selectedKey);
  thumbMenuEl.hidden = false;
  const w = 230, h = 30 * items.length + 10;
  thumbMenuEl.style.left = `${Math.min(x, window.innerWidth - w - 8)}px`;
  thumbMenuEl.style.top = `${Math.min(y, window.innerHeight - h - 8)}px`;
}

let lastPointer = { x: 0, y: 0 };
document.addEventListener("pointermove", (e) => {
  lastPointer = { x: e.clientX, y: e.clientY };
}, true);

function cleanupSwap() {
  clearTimeout(swapTimer);
  endGhost();
  clearDropHover();
  document.querySelectorAll(".cell.drag-source, .cell.drag-target, .cell.lifting")
    .forEach((c) => c.classList.remove("drag-source", "drag-target", "lifting"));
  swapSource = null;
  swapHover = null;
}

function bindKeyGestures() {
  const targets = [
    ...document.querySelectorAll(".ring-board .cell"),
    ...document.querySelectorAll(".ring-btn"),
  ];
  targets.forEach((el) => {
    const key = Number(el.dataset.key);
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      showKeyMenu(e.clientX, e.clientY, key);
    });
    if (!el.classList.contains("cell")) return;
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      clearTimeout(swapTimer);
      swapTimer = setTimeout(() => {
        swapSource = key;
        el.classList.add("lifting");
        const r = el.getBoundingClientRect();
        startGhost(el, lastPointer.x, lastPointer.y);
        if (ghostEl) {
          ghostEl.style.width = `${r.width}px`;
          ghostEl.style.height = `${r.height}px`;
          ghostEl.style.margin = `-${r.height / 2}px 0 0 -${r.width / 2}px`;
        }
        toast("拖到另一个按键上交换", 1400);
      }, 420);
    });
  });
}

document.addEventListener("pointermove", (e) => {
  if (!swapSource) return;
  moveGhost(e.clientX, e.clientY);
  const el = document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".cell");
  document.querySelectorAll(".cell.drag-target").forEach((c) => c.classList.remove("drag-target"));
  swapHover = el ? Number(el.dataset.key) : null;
  if (el) el.classList.add("drag-target");
}, true);

document.addEventListener("pointerup", async (e) => {
  clearTimeout(swapTimer);
  if (swapSource) {
    const a = swapSource;
    const el = document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".cell");
    const b = el ? Number(el.dataset.key) : swapHover;
    cleanupSwap();
    if (b && b !== a) {
      await post("/api/key/swap", { a, b });
      toast(`已交换 ${a} ↔ ${b}`, 1400);
      selectKey(b);
      await refreshState();
    }
    return;
  }
  hideKeyMenu();
});

document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest?.(".key-menu")) hideKeyMenu();
}, true);

/* 相册图片右键菜单：打开图片所在位置（和图标库同一套菜单样式） */
function closeImageMenu() {
  hideLibMenu();
}

function imageMenu(x, y, opts) {
  showLibMenu(x, y, [{
    label: "打开图片位置",
    onPick: async () => {
      const r2 = await post("/api/album/reveal", { scene: opts.scene || "", index: opts.index });
      if (!r2?.ok) toast("打不开这张图片（文件可能已被移走）", 2000);
    },
  }]);
}

document.addEventListener("contextmenu", (e) => {
  const box = e.target.closest?.("#as-strip, #album-strip, #as-preview, #album-preview");
  if (!box) { closeImageMenu(); return; }
  const thumb = e.target.closest(".album-thumb");
  let index = thumb ? Number(thumb.dataset.i) : NaN;
  if (!Number.isFinite(index)) {
    const on = box.querySelector?.(".album-thumb.on") || document.querySelector(
      `${box.id === "as-strip" ? "#as-strip" : "#album-strip"} .album-thumb.on`);
    index = on ? Number(on.dataset.i) : 0;
  }
  e.preventDefault();
  const scene = box.closest("#album-editor") || box.id === "as-strip" || box.id === "as-preview"
    ? (activeAlbumScene() || "") : "";
  imageMenu(e.clientX, e.clientY, { scene, index });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeImageMenu();
    if (pendingIcon) clearPendingIcon();      // Esc 也撤销临时预览
  }
});

function confirmDialog(message) {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.className = "modal confirm-modal";
    box.innerHTML = `<div class="modal-card confirm-card">
      <div class="confirm-body">${message}</div>
      <div class="confirm-actions">
        <button class="seg" data-act="no">取消</button>
        <button class="seg on" data-act="yes">确定</button>
      </div>
    </div>`;
    document.body.appendChild(box);
    const done = (v) => {
      box.remove();
      resolve(v);
    };
    box.addEventListener("pointerdown", (e) => {
      if (e.target === box) done(false);
    });
    box.querySelector('[data-act="yes"]').addEventListener("click", () => done(true));
    box.querySelector('[data-act="no"]').addEventListener("click", () => done(false));
  });
}

/* 新建场景：先选类型（普通 / 相册），再填名字 */
function newSceneDialog() {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.className = "modal confirm-modal";
    box.innerHTML = `<div class="modal-card confirm-card">
      <div class="confirm-body">
        <p class="scene-new-title">新建场景</p>
        <div class="scene-new-row"><span>名称</span>
          <input id="scene-new-name" maxlength="30" placeholder="例如：工作 / 相册 2" />
        </div>
        <div class="scene-new-row"><span>类型</span>
          <span class="seg-group compact" id="scene-new-kind">
            <button class="seg on" data-kind="normal">普通场景</button>
            <button class="seg" data-kind="album">相册场景</button>
          </span>
        </div>
        <p class="muted small" id="scene-new-hint">普通场景：自己摆 15 个按键的画面和动作。</p>
      </div>
      <div class="confirm-actions">
        <button class="seg" data-act="no">取消</button>
        <button class="seg on" data-act="yes">创建</button>
      </div>
    </div>`;
    document.body.appendChild(box);
    const nameEl = box.querySelector("#scene-new-name");
    const hint = box.querySelector("#scene-new-hint");
    const hints = {
      normal: "普通场景：自己摆 15 个按键的画面和动作。",
      album: "相册场景：整屏轮播图片，点任意屏幕键切到下一个场景。",
    };
    let kind = "normal";
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      box.remove();
      resolve(v);
    };
    const submit = () => {
      const name = (nameEl.value || "").trim();
      if (!name) {
        nameEl.focus();
        return toast("先给场景起个名字", 1600);
      }
      finish({ name, kind });
    };
    nameEl.value = `场景 ${new Date().toLocaleDateString().replace(/\//g, "-")}`;
    nameEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
      if (e.key === "Escape") { e.preventDefault(); finish(null); }
    });
    box.querySelectorAll("#scene-new-kind [data-kind]").forEach((b) => b.addEventListener("click", () => {
      kind = b.dataset.kind;
      box.querySelectorAll("#scene-new-kind [data-kind]").forEach((x) => x.classList.toggle("on", x === b));
      hint.textContent = hints[kind] || hints.normal;
    }));
    box.querySelector('[data-act="yes"]').addEventListener("click", submit);
    box.querySelector('[data-act="no"]').addEventListener("click", () => finish(null));
    box.addEventListener("pointerdown", (e) => { if (e.target === box) finish(null); });
    setTimeout(() => { nameEl.focus(); nameEl.select(); }, 30);
  });
}


async function agentDialog() {
  const cfg = await post("/api/agent/config", {});
  if (!cfg.ok) return toast("读取接入配置失败");
  const prompt = "用 m18-console 这个 MCP 帮我做一个 Stream Dock 自制插件：<在这里描述想要的功能>。"
    + "先调 m18_plugin_api 读开发指南、m18_status 看哪些键空着，写好后绑到空键上，"
    + "用 m18_press_key / m18_view_keys / m18_plugin_logs 自己测试，有报错就修到能用为止。";
  const tabs = [
    { id: "codex", name: "Codex", where: "加到 <code>%USERPROFILE%\\.codex\\config.toml</code> 末尾，然后重开 Codex（命令行 / VS Code 插件 / 桌面版通用）。也可以用命令：",
      code: cfg.codexToml,
      extra: `codex mcp add m18-console -- ${[cfg.command, ...cfg.args].map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(" ")}` },
    { id: "dsh", name: "DeepSeek Harness", where: "保存为项目里的 <code>.dsh/mcp.json</code>（需要启用 dsh-project-mcp 插件），或在配置的 mcpServers 里加上这一段：",
      code: cfg.mcpJson },
    { id: "json", name: "Claude / Cursor / 其他", where: "支持 MCP 的客户端都用这段标准配置：Claude Desktop 放进 <code>claude_desktop_config.json</code>，Cursor 放进 <code>.cursor/mcp.json</code>，Cline / Cherry Studio 等在 MCP 设置里粘贴。",
      code: cfg.mcpJson },
    { id: "chatgpt", name: "ChatGPT", where: "ChatGPT 网页版的连接器只能连公网 HTTPS 服务，<b>连不到你电脑上的控制台</b>。推荐用 <b>Codex</b>（同一个 ChatGPT 账号登录，选第一个页签的配置）。",
      code: "" },
  ];
  const box = document.createElement("div");
  box.className = "modal";
  box.innerHTML = `<div class="modal-card agent-card">
    <div class="modal-head"><h2>接入 AI 智能体（MCP）</h2><button class="seg small" data-x>关闭</button></div>
    <div class="modal-body">
      <p class="muted">配好后，AI 可以直接写自制插件、绑到按键、模拟按键测试、看键面截图和日志，自己改到能用为止。
        控制台没开时会在后台自动启动。共 16 个工具，开发指南：<code>m18_plugin_api</code>。</p>
      <div class="agent-tabs">${tabs.map((t, i) => `<button class="seg small${i ? "" : " on"}" data-tab="${t.id}">${t.name}</button>`).join("")}</div>
      ${tabs.map((t, i) => `<div class="agent-pane" data-pane="${t.id}"${i ? " hidden" : ""}>
        <p>${t.where}</p>
        ${t.code ? `<div class="agent-code"><pre>${escHtml(t.code)}</pre><button class="seg tiny" data-copy="${t.id}">复制</button></div>` : ""}
        ${t.extra ? `<div class="agent-code"><pre>${escHtml(t.extra)}</pre><button class="seg tiny" data-copy="${t.id}-extra">复制</button></div>` : ""}
      </div>`).join("")}
      <p><b>试试这样说</b>（复制后改掉尖括号里的内容）：</p>
      <div class="agent-code"><pre class="wrap">${escHtml(prompt)}</pre><button class="seg tiny" data-copy="prompt">复制</button></div>
      <p class="muted">命令：<code>${escHtml(cfg.command)}</code>${cfg.guide ? ` · 说明文档：<code>${escHtml(cfg.guide)}</code>` : ""}<br>
        AI 能按键、改按键、运行插件，等于能执行你绑定的程序；只接你信任的客户端。</p>
    </div>
  </div>`;
  document.body.appendChild(box);
  const texts = { prompt };
  tabs.forEach((t) => { texts[t.id] = t.code; texts[`${t.id}-extra`] = t.extra; });
  const close = () => box.remove();
  box.querySelector("[data-x]").addEventListener("click", close);
  box.addEventListener("pointerdown", (e) => { if (e.target === box) close(); });
  box.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    box.querySelectorAll("[data-tab]").forEach((x) => x.classList.toggle("on", x === b));
    box.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== b.dataset.tab; });
  }));
  box.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(texts[b.dataset.copy] || "");
      b.textContent = "已复制";
      setTimeout(() => { b.textContent = "复制"; }, 1500);
    } catch (e) { toast("复制失败，请手动选中复制"); }
  }));
}

function choiceDialog(message, choices) {
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.className = "modal confirm-modal";
    box.innerHTML = `<div class="modal-card confirm-card">
      <div class="confirm-body">${message}</div>
      <div class="confirm-actions choice-actions${choices.length > 2 ? " choice-vertical" : ""}">
        ${choices.map((c) => `<button class="seg${c.primary ? " on" : ""}" data-v="${escHtml(c.value)}">${c.label}</button>`).join("")}
        <button class="seg" data-v="">取消</button>
      </div>
    </div>`;
    document.body.appendChild(box);
    const done = (v) => { box.remove(); resolve(v || null); };
    box.addEventListener("pointerdown", (e) => { if (e.target === box) done(null); });
    box.querySelectorAll("[data-v]").forEach((b) => b.addEventListener("click", () => done(b.dataset.v)));
  });
}

/* 场景关联程序：前台切到这些程序时自动切到该场景 */
async function appsDialog(name, current) {
  const recent = ((await post("/api/apps/recent", {})).items || []).filter((a) => !/msedge|explorer|python/.test(a));
  return new Promise((resolve) => {
    const box = document.createElement("div");
    box.className = "modal confirm-modal";
    box.innerHTML = `<div class="modal-card confirm-card apps-card">
      <div class="confirm-body"><b>场景「${escHtml(name)}」关联程序</b><br>
        <span class="muted">前台切到这些程序时自动切到该场景，离开后切回原来的场景。多个用逗号隔开，留空取消关联。</span>
        <input class="sel-input apps-input" value="${escHtml((current || []).join(", "))}" placeholder="例如 Photoshop.exe, Adobe Premiere Pro.exe" />
        ${recent.length ? `<div class="apps-recent"><span class="muted">最近用过：</span>${recent.map((a) => `<button class="seg tiny" data-app="${escHtml(a)}">${escHtml(a)}</button>`).join("")}</div>` : ""}
      </div>
      <div class="confirm-actions">
        <button class="seg" data-v="cancel">取消</button>
        <button class="seg on" data-v="ok">保存</button>
      </div>
    </div>`;
    document.body.appendChild(box);
    const input = box.querySelector(".apps-input");
    box.querySelectorAll("[data-app]").forEach((b) => b.addEventListener("click", () => {
      const list = input.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      if (!list.includes(b.dataset.app)) list.push(b.dataset.app);
      input.value = list.join(", ");
    }));
    const done = (v) => { box.remove(); resolve(v); };
    box.querySelector('[data-v="ok"]').addEventListener("click", () => done(input.value));
    box.querySelector('[data-v="cancel"]').addEventListener("click", () => done(null));
    box.addEventListener("pointerdown", (e) => { if (e.target === box) done(null); });
    input.focus();
  });
}

function selectKey(n) {
  if (pendingIcon && pendingIcon.key !== n) clearPendingIcon();   // 换键：上一个键的临时预览作废
  if (n !== selectedKey) {
    editGesture = "press";                          // 换键时回到「短按」
    formDrafts = {};                                // 上一个键没保存的草稿丢弃
  }
  selectedKey = n;
  refreshCells();
  renderEditor();
  // 设备上对应按键亮起内边缘；同时在控制台里操作也算"活跃"
  post("/api/key/select", { key: n || 0 }).catch(() => {});
  post("/api/activity", { wake: true }).catch(() => {});
}

let editGesture = "press";
// 三个触发方式的「草稿」：切页签不丢、点保存才一起提交
let formDrafts = {};
let sceneNames = [];
let sceneItems = [];          // 场景列表原始数据（含 sceneType / albumScene）
let sceneDragEndedAt = 0;     // 拖动排序刚结束，别把这一下当成单击
let selectedScene = null;     // 列表里选中的场景（编辑区按它决定显示相册还是按键）
// 从图标库点图标 / 插件动作时先只做「临时预览」，点「保存」才真正写进按键
let pendingIcon = null;       // {key, gesture, kind, name?, plugin?, index?, src?, label}

/* Dock 同步预览：拖到按键上悬停、放下后还没保存时，设备上也显示这张图（服务端只画不存） */
let dockPreview = { key: 0, src: "" };
const sendDockPreview = throttle((key, src) => {
  post("/api/key/preview", src ? { key, src } : { key: 0 }).catch(() => {});
}, 90);
function dockPreviewSrc(info) {
  // 界面里的预览 → 服务端认识的来源（不接受任意路径）
  if (!info) return "";
  if (info.kind === "plugin") return `plugin:${info.plugin}/${info.index}`;
  if (info.kind === "icon" && /^(pack|lib|plugin):/.test(info.name || "")) return info.name;
  if (info.kind === "image" && info.name) return `asset:${info.name}`;
  if (info.kind === "program" && info.path) return `program:${info.path}`;
  return "";
}
function showDockPreview(key, src) {
  if (!key || key > 15 || !src) return hideDockPreview();
  if (dockPreview.key === key && dockPreview.src === src) return;
  dockPreview = { key, src };
  sendDockPreview(key, src);
}
function hideDockPreview() {
  if (!dockPreview.key) return;
  dockPreview = { key: 0, src: "" };
  sendDockPreview(0, "");
}

function clearPendingIcon() {
  if (!pendingIcon) return;
  const k = pendingIcon.key;
  pendingIcon = null;
  hideDockPreview();
  document.querySelector(`.ring-board .cell[data-key="${k}"]`)?.classList.remove("pending-icon");
  refreshCells();
  renderEditor();
}

/* 清掉某个键上还没保存的预览（清除 / 删除按键时用；不传 key 就全清） */
function dropPendingForKey(key) {
  if (!pendingIcon) return;
  if (key && pendingIcon.key !== key) return;
  clearPendingIcon();
}

function setPendingIcon(key, gesture, info) {
  if (selectedKey !== key) selectKey(key);          // 先把编辑区切到这个键
  pendingIcon = { key, gesture, ...info };
  showDockPreview(key, dockPreviewSrc(pendingIcon));
  paintPendingIcon();
}

/* 点空白处（既不是按键、也不在编辑区/图标库/插件面板里）就撤销没保存的图标 */
document.addEventListener("pointerdown", (e) => {
  if (!pendingIcon) return;
  const t = e.target;
  if (t.closest(".cell, .ring-btn, #key-editor, #icon-groups, #plugin-list, .icon-group, details.plugin, .img-menu, .lib-menu, .confirm-modal, .toast")) return;
  clearPendingIcon();
}, true);

function paintPendingIcon() {
  if (!pendingIcon) return;
  refreshCells();     // 按键格子直接按「放上去以后」的样子显示
  renderEditor();     // 编辑区的缩略图、功能行也跟着显示，可以继续改标题 / 颜色
}
const VALUE_TYPES = {
  launch: "程序 / 文件 / 文件夹路径（可点「选择文件…」）",
  url: "网址，如 https://example.com",
  hotkey: "组合键，如 ctrl+shift+s（可点「录制热键」）",
  toggle: "两个组合键用 | 隔开，如 ctrl+m | ctrl+shift+m（录制两次）",
  text: "要输入的文字，\\n 表示换行",
  mouse: "坐标 x,y（可空：在当前鼠标位置操作）",
  close: "进程名，如 notepad.exe；留空 = 关闭当前前台窗口",
  sound: "音频文件路径（wav / mp3）；留空 = 停止所有音效",
  folder: "文件夹名称，如：常用工具（按下进入子页，子页左上角是返回键）",
};

function gestureLabel(g) {
  return g === "press" ? "短按" : g === "long" ? "长按" : "双击";
}

const CONSOLE_LABELS = {
  screen: "屏幕开关",
  mute: "屏蔽按键开关",
  light: "灯光开关",
  lightfx: "灯光效果切换",
};

// 把服务端返回的「某个触发方式」转成和表单一样的结构，便于统一填充
function viewToDraft(view) {
  const spec = view?.spec || {};
  const t = view ? view.type : "none";
  const val = view?.value || "";
  return {
    type: t,
    value: ["nav", "media", "macro", "scene", "plugin", "window"].includes(t) ? "" : val,
    nav: t === "nav" ? val : "",
    media: t === "media" ? (spec.action || val) : "",
    macro: t === "macro" ? val : "",
    mouse: t === "mouse" ? (spec.button || "click") : "click",
    window: t === "window" ? (spec.op || "minimize") : "minimize",
    scene: t === "scene" ? val : "",
    console: t === "console" ? (spec.op || "screen") : "screen",
    enter: !!spec.enter,
    secret: !!spec.secret,
  };
}

// 读当前表单（不改服务端）
function readFormDraft() {
  return {
    type: $("#sel-type").value,
    value: $("#sel-value").value,
    nav: $("#sel-nav").value,
    media: $("#sel-media").value,
    macro: $("#sel-macro").value,
    mouse: $("#sel-mouse").value,
    window: $("#sel-window").value,
    scene: $("#sel-scene").value,
    console: $("#sel-console")?.value || "screen",
    enter: !!$("#sel-enter")?.checked,
    secret: !!$("#sel-secret")?.checked,
  };
}

// 草稿 → 提交用的 spec；「未绑定」「插件」返回 null
function specFromDraft(d, key, gesture) {
  const t = d.type;
  if (t === "none" || t === "plugin") return null;
  const spec = { key, type: t, gesture };
  const val = (d.value || "").trim();
  if (t === "folder") spec.name = val || "文件夹";
  else if (t === "nav") spec.name = d.nav;
  else if (t === "hotkey" || t === "toggle") spec.keys = val;
  else if (t === "media") spec.action = d.media;
  else if (t === "macro") spec.macro = d.macro;
  else if (t === "url") spec.url = val;
  else if (t === "text") {
    spec.text = (d.value || "").replace(/\\n/g, "\n");
    spec.enter = !!d.enter;
    spec.secret = !!d.secret;
  } else if (t === "mouse") {
    spec.button = d.mouse;
    spec.pos = val;
  } else if (t === "close") spec.target = val;
  else if (t === "window") spec.op = d.window;
  else if (t === "scene") spec.scene = d.scene;
  else if (t === "console") spec.op = d.console || "screen";
  else if (t === "sound") spec.path = val;
  else spec.path = val;
  return spec;
}

// 草稿的简短说明，用在「键 X：长按：…（未保存）」那一行
function draftLabel(d) {
  const t = d.type;
  const val = (d.value || "").trim();
  if (t === "none") return "未设置";
  if (t === "nav") return `导航：${d.nav}`;
  if (t === "scene") return `场景：${d.scene}`;
  if (t === "console") return `控制台：${CONSOLE_LABELS[d.console] || d.console}`;
  if (t === "folder") return `文件夹：${val || "文件夹"}`;
  if (t === "hotkey" || t === "toggle") return val;
  if (t === "text") return val;
  if (t === "mouse") return `鼠标：${d.mouse}`;
  if (t === "window") return `窗口：${d.window}`;
  if (t === "media") return `媒体键：${d.media}`;
  if (t === "macro") return "宏";
  if (t === "plugin") return "插件动作";
  return val ? `打开：${val}` : t;
}

// 草稿状态即时反馈：保存按钮、页签小条、标题行
// 切换页签时判断：表单里填的和已存的到底一不一样（一样就别当成「未保存」）
function draftDiffersFromSaved(n, g) {
  if (!n) return false;
  const now = specFromDraft(readFormDraft(), n, g);
  const was = specFromDraft(viewToDraft(gestureView(n, g)), n, g);
  return JSON.stringify(now || null) !== JSON.stringify(was || null);
}

function paintDraftMarks() {
  const dirtyBtn = $("#sel-save-top");
  if (dirtyBtn) dirtyBtn.classList.toggle("dirty", Object.keys(formDrafts).length > 0);
  document.querySelectorAll("#sel-gesture button").forEach((b) => {
    b.classList.toggle("dirty", !!formDrafts[b.dataset.g]);
  });
  const n = selectedKey;
  const d = n ? formDrafts[editGesture] : null;
  if (!d) return;
  const line = $("#sel-action");
  if (line) {
    line.textContent = editGesture === "press"
      ? "短按：未保存的改动"
      : `${gestureLabel(editGesture)}：${draftLabel(d)}（未保存）`;
  }
  const h = $("#sel-hint");
  if (h && !h.textContent.startsWith("有未保存的改动")) {
    h.textContent = "有未保存的改动 —— 三种触发方式改完一起点右上角「保存」生效。" + h.textContent;
  }
}

function gestureView(n, g) {
  if (g === "press") {
    const act = state?.keyActions?.[n];
    if (!act) return null;
    return {
      type: state.keyActionTypes?.[n] || "launch", label: act,
      value: state.keyActionValues?.[n] || "", spec: state.keyActionSpecs?.[n] || {},
    };
  }
  return state?.keyGestures?.[n]?.[g] || null;
}

function applyTypeVisibility(t) {
  const show = (sel, on) => { const el = $(sel); if (el) el.hidden = !on; };
  show("#row-value", t in VALUE_TYPES);
  const v = $("#sel-value");
  if (v) {
    v.placeholder = VALUE_TYPES[t] || "";
    v.disabled = !(t in VALUE_TYPES);
    v.type = t === "text" && $("#sel-secret")?.checked ? "password" : "text";
  }
  show("#sel-nav", t === "nav");
  show("#sel-media", t === "media");
  show("#sel-mouse", t === "mouse");
  show("#sel-scene", t === "scene");
  show("#sel-console", t === "console");
  show("#sel-window", t === "window");
  show("#sel-record", t === "hotkey" || t === "toggle");
  show("#sel-pick", t === "launch" || t === "sound");
  const fv = selectedKey ? gestureView(selectedKey, editGesture) : null;
  show("#sel-open-folder", t === "folder" && fv?.type === "folder");
  show("#row-macro", t === "macro");
  show("#row-text-opts", t === "text");
  show("#editor-actions-row", ["hotkey", "toggle", "media", "nav", "mouse", "scene", "console", "launch", "sound", "window", "folder"].includes(t));
  if (t !== "hotkey" && t !== "toggle") $("#sel-record")?.classList.remove("on");
}

function fillSceneSelect(selected) {
  const sel = $("#sel-scene");
  if (!sel) return;
  sel.innerHTML = sceneNames.map((n) => `<option value="${escHtml(n)}"${n === selected ? " selected" : ""}>${escHtml(n)}</option>`).join("");
}

function fillActionForm(n) {
  const draft = formDrafts[editGesture];
  const view = gestureView(n, editGesture);
  const d = draft || viewToDraft(view);
  let t = d.type;
  if (t === "plugin" && editGesture !== "press") t = "none";
  $("#sel-type").value = t;
  $("#sel-value").value = ["nav", "media", "macro", "scene", "plugin", "window"].includes(t) ? "" : (d.value || "");
  $("#sel-macro").value = t === "macro" ? (d.macro || "") : "";
  if (t === "media" && d.media) $("#sel-media").value = d.media;
  if (t === "mouse" && d.mouse) $("#sel-mouse").value = d.mouse;
  if (t === "window" && d.window) $("#sel-window").value = d.window;
  if (t === "console") $("#sel-console").value = d.console || "screen";
  $("#sel-enter").checked = !!d.enter;
  $("#sel-secret").checked = !!d.secret;
  const nav = $("#sel-nav");
  const jumps = Array.from({ length: (state?.mainPages || [0]).length }, (_, i) => `跳到第 ${i + 1} 页`);
  const opts = [...NAV_PRESETS.slice(0, 4), "返回上级", "返回上一场景", ...jumps, ...NAV_PRESETS.slice(4)];
  nav.innerHTML = opts.map((p) => `<option value="${p}">${p}</option>`).join("");
  if (t === "nav" && d.nav) nav.value = d.nav;
  fillSceneSelect(t === "scene" ? (d.scene || "") : "");
  // 触发方式标签：当前 / 已设置 / 有未保存草稿
  document.querySelectorAll("#sel-gesture button").forEach((b) => {
    const g = b.dataset.g;
    b.classList.toggle("on", g === editGesture);
    b.classList.toggle("has", !!gestureView(n, g) && g !== "press");
    b.classList.toggle("dirty", !!formDrafts[g]);
  });
  applyTypeVisibility(t);
  const dirtyBtn = $("#sel-save-top");
  if (dirtyBtn) dirtyBtn.classList.toggle("dirty", Object.keys(formDrafts).length > 0);
  return t;
}

function renderEditor() {
  const n = selectedKey;
  const label = $("#sel-label");
  // 相册场景：编辑区换成图片集，保存/清除按钮跟按键无关，收起来
  // 但三个导航键（16/17/18）还是按键编辑：它们有默认的上页 / 主页 / 下页动作
  const albScene = n && n > 15 ? null : activeAlbumScene();
  syncAlbumBody();
  const bodyEl = $("#editor-body");
  const albEl = $("#album-editor");
  if (albEl) albEl.hidden = !albScene;
  if (bodyEl) bodyEl.hidden = !!albScene;
  // 相册场景的页由图片自动分，页数按钮收起来
  const pageRow = document.querySelector(".page-row");
  if (pageRow) pageRow.classList.toggle("album-scene", !!activeAlbumScene());
  const saveTop = $("#sel-save-top");
  const clearTop = $("#sel-clear-top");
  const resetTop = $("#sel-reset-top");
  if (saveTop) saveTop.hidden = !!albScene;
  if (clearTop) clearTop.hidden = !!albScene;
  // 「还原」只给三个导航键：一键回到 上一页 / 主页 / 下一页，且只保留短按
  if (resetTop) resetTop.hidden = !!albScene || !(n === 16 || n === 17 || n === 18);
  if (albScene) {
    bindAlbumSceneEditor();
    if (albumSceneLoaded !== albScene) {
      albumSceneLoaded = albScene;
      renderAlbumEditor({ items: [], index: 0, slideshow: true, interval: 8, transition: "fade" }, albScene);
      reloadAlbumEditor();
    }
    return;
  }
  if (!n) {
    label.textContent = "未选中按键";
    $("#sel-action").textContent = "未绑定";
    $("#sel-image").textContent = "无（标题 / 编号）";
    $("#sel-nav").hidden = true;
    $("#sel-thumb").style.backgroundImage = "";
    $("#sel-thumb").innerHTML = "<span>拖入图片 / 程序 / 文件夹</span>";
    $("#sel-hint").textContent = "先点一下上面的按键。";
    const rec0 = $("#sel-record");
    if (rec0) {
      rec0.hidden = true;
      rec0.classList.remove("on");
    }
    const row0 = $("#editor-actions-row");
    if (row0) row0.hidden = true;
    const vrow0 = $("#row-value");
    if (vrow0) vrow0.hidden = true;
    applyTypeVisibility("none");
    return;
  }

  const isNav = n > 15;
  label.textContent = `按键 ${n}${isNav ? "（导航键）" : ""}`;
  const st = state?.keyStyles?.[n] || {};
  const img = state?.keyImages?.[n];
  const act = state?.keyActions?.[n];
  const type = state?.keyActionTypes?.[n] || "launch";
  // 刚拖进来、还没点保存的图标 / 程序：编辑区按「已经放上去了」的样子显示
  const pend = pendingIcon && pendingIcon.key === n ? pendingIcon : null;
  const thumb = $("#sel-thumb");
  if (thumb) {
    thumb.classList.add("as-key");
    thumb.style.background = st.bg || "#000";
    if (isNav) {
      thumb.innerHTML = `<span class="thumb-hint">导航键无屏幕</span>`;
    } else {
      const parts = [];
      const plugLive = (state?.keyPlugin?.[n]?.rev || 0) > 0;
      if (plugLive) parts.push(`<img class="thumb-img" src="/api/key/${n}/live.jpg?v=${Date.now()}" alt="" />`);
      else if (pend && pend.src) parts.push(`<img class="thumb-img" src="${pend.src}" alt="" />`);
      else if (img) parts.push(`<img class="thumb-img" src="/api/key/${n}/raw?v=${Date.now()}" alt="" />`);
      const label = plugLive ? "" : (st.text || (img || pend ? "" : String(n)));
      if (label) {
        const tw = thumb.clientWidth || 96;
        const fs = Math.max(6, (st.size ?? 20) * (tw / 64));
        const align = st.pos === "top" ? "flex-start" : (st.pos === "bottom" ? "flex-end" : "center");
        parts.push(`<span class="thumb-label" style="color:${st.color || "#e8eefc"};`
          + `opacity:${(st.opacity ?? 100) / 100};font-size:${fs}px;line-height:1.15;`
          + `padding:${Math.max(1, (3 * tw) / 64)}px;align-items:${align}">`
          + `${splitLabel(label, st.size ?? 20).map(escHtml).join("<br>")}</span>`);
      }
      thumb.innerHTML = parts.join("");
    }
  }
  // 同一个键还在编辑中：服务器推回来的是上一拍的值，别覆盖用户正在打的字（换了键才整体回填）
  const sameKey = renderEditor._key === n;
  renderEditor._key = n;
  const fill = (sel, v) => (sameKey ? setVal($(sel), v) : ($(sel).value = v));
  fill("#sel-text", st.text || "");
  fill("#sel-fg", st.color || "#e8eefc");
  fill("#sel-bg", st.bg || "#000000");
  fill("#sel-opacity", st.opacity ?? 100);
  if (!sameKey || !isBusy($("#sel-opacity"))) $("#sel-opacity-val").textContent = `${st.opacity ?? 100}%`;
  fill("#sel-size", st.size ?? 20);
  if (!sameKey || !isBusy($("#sel-size"))) $("#sel-size-val").textContent = `${st.size ?? 20}`;
  fill("#sel-pos", st.pos || "center");

  $("#sel-action").textContent = !act ? "未绑定"
    : type === "nav" ? `导航：${act}` : type === "plugin" ? act : `打开：${act}`;
  const plug = state?.keyPlugin?.[n];
  const plugRow = $("#row-plugin");
  if (plugRow) {
    plugRow.hidden = !plug;
    if (plug) {
      $("#sel-plugin-status").textContent = {
        connected: "运行中", starting: "启动中…", idle: "未启动（显示时自动启动）",
        failed: "启动失败（见 plugin-data\\logs）", unavailable: "插件宿主不可用",
      }[plug.status] || plug.status;
      $("#sel-plugin-status").dataset.status = plug.status;
    }
  }
  $("#sel-image").textContent = isNav ? "导航键无屏幕"
    : ((img || pend?.src) ? "单独图片 / 动图" : "无（标题 / 编号）");

  const actType = fillActionForm(n);
  const curDraft = formDrafts[editGesture];
  if (editGesture !== "press") {
    const shown = curDraft ? { label: draftLabel(curDraft) } : gestureView(n, editGesture);
    $("#sel-action").textContent = `${gestureLabel(editGesture)}：${shown?.label || "未设置"}${curDraft ? "（未保存）" : ""}`;
  } else if (curDraft) {
    $("#sel-action").textContent = "短按：未保存的改动";
  }
  // 拖进来的插件 / 程序：功能行直接显示它，保存后就变成这个行为
  if (pend && pend.kind === "plugin") $("#sel-action").textContent = `插件：${pend.label || "插件动作"}`;
  else if (pend && pend.kind === "program") $("#sel-action").textContent = `打开：${pend.name}`;

  let hint = editGesture !== "press"
    ? `${gestureLabel(editGesture)}（${editGesture === "long" ? "按住 0.5 秒" : "0.3 秒内按两下"}）时执行。设置后，短按会在松开${editGesture === "double" ? "约 0.3 秒" : ""}后才触发。`
    : actType === "plugin"
    ? "插件动作：按下由插件处理，画面也由插件更新；点「插件设置」配置。改成别的类型即解除绑定。"
    : isNav
      ? "导航键默认：左=上一页，中=主页，右=下一页；也可以改成打开程序、网页或热键。"
      : "拖图片换画面；拖程序 / 快捷方式 / 脚本 / 文件夹绑定动作，按下即打开。";
  if (Object.keys(formDrafts).length) hint = "有未保存的改动 —— 三种触发方式改完一起点右上角「保存」生效。" + hint;
  if (pend) hint = "已经放到这个键上了：标题 / 颜色可以直接改，点右上角「保存」写进按键（点空白处或换键就撤销）。" + hint;
  $("#sel-hint").textContent = hint;
}

/* ------------------------------------------------------------------ 拖放绑定 */
async function uploadTree(entry, prefix, budget) {
  if (budget.count > 400 || budget.bytes > 200 * 1024 * 1024) return;
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    budget.count += 1;
    budget.bytes += file.size;
    await upload(file, prefix + file.name);
    return;
  }
  if (entry.isDirectory) {
    const reader = entry.createReader();
    let all = [];
    for (;;) {
      const batch = await new Promise((res) => reader.readEntries(res));
      if (!batch.length) break;
      all = all.concat(batch);
    }
    for (const child of all) {
      await uploadTree(child, prefix + entry.name + "/", budget);
    }
  }
}

/* 从拖放数据里捞真实路径：Edge 从资源管理器拖文件时会带上 file:/// 形式的地址 */
function filePathFromDt(dt) {
  if (!dt) return "";
  let raw = "";
  try { raw = dt.getData("text/uri-list") || ""; } catch (_) { /* 有的浏览器不给读 */ }
  if (!raw) {
    try { raw = dt.getData("text/plain") || ""; } catch (_) { /* 同上 */ }
  }
  for (const line of String(raw).split(/\r?\n/)) {
    let s = line.trim();
    if (!s || s.startsWith("#")) continue;
    if (/^file:\/\//i.test(s)) {
      try { s = decodeURIComponent(s.replace(/^file:\/\/\//i, "")); } catch (_) { continue; }
    }
    if (/^\/[A-Za-z]:\//.test(s)) s = s.slice(1);
    s = s.replace(/\//g, "\\");
    if (/^[A-Za-z]:\\/.test(s)) return s;
  }
  return "";
}

function fileToBase64(file) {
  return new Promise((resolve) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || "").replace(/^data:[^,]*,/, ""));
    fr.onerror = () => resolve("");
    fr.readAsDataURL(file);
  });
}

/* 先把「拖进来的程序」解析成真实路径，解析不出来才弹选择窗口 */
async function resolveProgram(file, pathHint) {
  const body = { name: file.name, size: file.size };
  if (pathHint) body.path = pathHint;
  if (!pathHint && /\.lnk$/i.test(file.name) && file.size && file.size < 4 * 1024 * 1024) {
    body.data = await fileToBase64(file);
  }
  try {
    return await post("/api/program/resolve", body);
  } catch (_) {
    return { ok: false };
  }
}

async function handleFile(key, file, pathHint) {
  const isImage = /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name);
  if (isImage) {
    // 先放到键上显示出来，点「保存」才真正写进按键
    const up = await upload(file);
    if (!up?.ok) {
      toast(up?.error || "上传失败", 2200);
      return;
    }
    setPendingIcon(key, editGesture, {
      kind: "image",
      name: up.file,
      src: `/api/asset?p=${encodeURIComponent(up.file)}&v=${Date.now()}`,
      label: "图片",
    });
    return;
  }
  // 程序 / 快捷方式：先自己解析路径（拖 href / 解析 .lnk / 按名字找），实在找不到才让用户选
  if (/\.(exe|lnk|bat|cmd|com|msi|appref-ms|ps1|vbs)$/i.test(file.name)) {
    const hit = await resolveProgram(file, pathHint);
    if (hit.ok && hit.path) {
      setPendingIcon(key, editGesture, {
        kind: "program",
        path: hit.path,
        name: hit.name || file.name,
        src: `/api/program/icon?path=${encodeURIComponent(hit.path)}&v=${Date.now()}`,
      });
      return;
    }
    const go = await confirmDialog(
      `「${escHtml(file.name)}」没在常见目录里找到。<br><small>浏览器拿不到拖进来的完整路径，`
      + `点「确定」后会弹一个选择窗口，在里面选它一次就能绑上。</small>`);
    if (!go) return;
    const picked = await post("/api/key/pick-program", {});
    if (!picked.ok) {
      toast("没选程序，绑定取消", 1600);
      return;
    }
    setPendingIcon(key, editGesture, {
      kind: "program",
      path: picked.path,
      name: picked.name || file.name,
      src: `/api/program/icon?path=${encodeURIComponent(picked.path)}&v=${Date.now()}`,
    });
    return;
  }
  // 快捷方式类：解析出网址，类型自动设为「打开网页」
  if (/\.(url|webloc|website)$/i.test(file.name)) {
    let url = "";
    try {
      const text = await file.text();
      const m = text.match(/URL\s*=\s*(\S+)/i) || text.match(/<string>(https?:\/\/[^<]+)<\/string>/i);
      if (m) url = m[1].trim();
    } catch (_) { /* ignore */ }
    if (url) {
      const r = await post("/api/key/action", { key, type: "url", url });
      if (!r.ok) toast("网页绑定失败");
      else toast(`已设为打开网页：${url.slice(0, 40)}`);
      return;
    }
  }
  const up = await upload(file, file.name);
  const target = up.target || up.file;
  const r = await post("/api/key/action", { key, path: target });
  if (!r.ok) toast("绑定失败：找不到该路径");
}

async function dropWallpaper(dt) {
  if (!dt) return;
  const file = dt.files?.[0] || dt.items?.[0]?.getAsFile?.();
  if (!file) return;
  if (!/\.(png|jpe?g|webp|bmp)$/i.test(file.name)) {
    toast("壁纸只支持图片文件", 1800);
    return;
  }
  const up = await upload(file);
  const r = await post("/api/wallpaper/file", { file: up.file });
  if (!r.ok) {
    toast("壁纸设置失败", 1800);
    return;
  }
  await refreshState();
  toast("壁纸已替换", 1400);
}

async function dropOnKey(key, dt) {
  if (!dt) return;
  // 从左侧图标库拖过来的
  const iconName = dt.getData?.("application/x-m18-icon");
  if (iconName) {
    const ok = await post("/api/key/icon", { key, name: iconName });
    toast(ok.ok ? "图标已应用" : "应用失败", 1200);
    if (ok.ok) await refreshState();
    return;
  }
  // 真实路径要在 drop 事件这一拍就读出来，事件过后 dataTransfer 就空了
  const pathHint = filePathFromDt(dt);
  const items = [...(dt.items || [])];
  const entry = items[0]?.webkitGetAsEntry?.();
  try {
    if (entry?.isDirectory) {
      toast("正在导入文件夹…", 1500);
      await uploadTree(entry, "", { count: 0, bytes: 0 });
      const r = await post("/api/key/action", {
        key, path: `assets/uploads/dropped/${entry.name}`,
      });
      if (!r.ok) toast("文件夹绑定失败");
      else toast(`已绑定文件夹：${entry.name}`);
    } else if (entry) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      await handleFile(key, file, pathHint);
    } else if (dt.files?.[0]) {
      await handleFile(key, dt.files[0], pathHint);
    }
  } catch (err) {
    toast("拖入失败：" + err.message);
  }
  await refreshState();
}

/* ------------------------------------------------------------------ 状态 */

function applyMode(mode) {
  document.querySelectorAll("#modes .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.mode === mode);
  });
  $("#mode-label").textContent = MODE_NAMES[mode] || mode;
  $("#mode-desc").textContent = MODE_DESC[mode] || "";
  const clr = $("#clear-led");
  if (clr) clr.hidden = mode !== "custom";
}

// 灯光状态单独应用一份：按键里的「控制台 → 灯光开关 / 灯光效果切换」改了以后，
// 服务端会推一条 lighting，这里把色盘、亮度、速度、模式按钮一起跟上
function applyLighting(L) {
  if (!L) return;
  currentColor = L.color || currentColor;
  ledBrightness = L.brightness ?? 100;
  const colorEl = $("#color");
  setVal(colorEl, currentColor);
  const hexEl = $("#color-hex");
  if (hexEl) hexEl.textContent = String(currentColor).toUpperCase();
  syncSwatch();
  const brightEl = $("#led-brightness");
  if (!isBusy(brightEl)) {
    setVal(brightEl, ledBrightness);
    const brightVal = $("#led-brightness-val");
    if (brightVal) brightVal.textContent = ledBrightness;
  }
  const speedEl = $("#speed");
  if (!isBusy(speedEl)) {
    setVal(speedEl, L.speed);
    const speedVal = $("#speed-val");
    if (speedVal && L.speed != null) speedVal.textContent = Number(L.speed).toFixed(1) + "×";
  }
  applyMode(L.mode);
  if (Array.isArray(L.custom) && L.custom.length === 24) {
    localCustom = L.custom.map((c) => [...c]);
  }
}

function setSwitch(el, on) {
  if (!el) return;
  el.classList.toggle("on", !!on);
  el.setAttribute("aria-checked", on ? "true" : "false");
}

function applyRotation() {
  const stage = $("#ring-stage");
  const wrap = $("#ring-wrap");
  if (!stage || !wrap) return;
  const rot = (state?.rotation ?? 0) % 360;
  const side = rot % 180 !== 0;
  // 竖放时舞台变高、键盘尺寸和比例保持不变（不缩小），只转方向
  stage.style.aspectRatio = side ? "1" : "1.35";
  stage.style.maxHeight = "none";
  const w = stage.clientWidth;
  if (!w) return;
  wrap.style.width = `${w}px`;
  wrap.style.aspectRatio = "1.35";
  if (side && stage.clientHeight < w) stage.style.height = `${w}px`;
  else stage.style.height = "";
  wrap.style.transform = `translate(-50%, -50%) rotate(${rot}deg)`;
  document.querySelectorAll("#rotate-group [data-rot]").forEach((b) => b.classList.toggle("on", Number(b.dataset.rot) === rot));
  document.documentElement.style.setProperty("--rot", `${rot}deg`);
  const names = { 0: "正向摆放", 90: "顺时针竖放", 180: "倒置", 270: "逆时针竖放" };
  $("#rot-label").textContent = names[rot] || `${rot}°`;
  layoutBoard();
}

async function fetchState() {
  const res = await fetch("/api/state", { cache: "no-store" });
  return res.json();
}

// 多处同时要求刷新时合并成一次；后发先至的旧结果直接丢掉
let refreshSeq = 0;
let refreshing = null;
let refreshAgain = false;
async function refreshState() {
  if (refreshing) {               // 正在刷：等它结束后再补刷一次（拿最新的），期间的请求都合并
    refreshAgain = true;
    return refreshing;
  }
  refreshing = (async () => {
    do {
      refreshAgain = false;
      const seq = ++refreshSeq;
      try {
        const s = await fetchState();
        if (seq === refreshSeq) applyState(s);
      } catch (err) {
        console.error("刷新状态失败", err);
      }
    } while (refreshAgain);
  })();
  try {
    await refreshing;
  } finally {
    refreshing = null;
  }
}

function applyState(s) {
  state = s;
  const d = s.device;

  const status = $("#status");
  status.className = "status " + (d.yielded ? "off" : (d.connected ? "on" : "off"));
  $("#status-text").textContent = d.yielded
    ? "官方驱动运行中 · 已退让"
    : (d.connected
      ? `已连接 ${d.info?.firmware ? "· " + d.info.firmware : ""}`
      : d.error || "未连接");

  $("#banner").classList.toggle("hidden", !s.officialRunning);
  if (s.officialRunning) {
    $("#banner").textContent =
      "检测到官方 Stream Dock 软件正在运行：两者会抢画面和灯光，日常使用建议只开一个。";
  }

  const L = s.lighting;
  if (s.frame) paintDots(s.frame);
  applyLighting(L);

  if (!isBusy($("#screen-brightness"))) {
    setVal($("#screen-brightness"), s.screenBrightness);
    $("#screen-brightness-val").textContent = s.screenBrightness;
  }

  const gap = s.wallpaper?.gap ?? 27;
  $("#wallpaper-gap").value = gap;
  updateGapLabel(gap);
  const standby = s.standby ?? 300;
  $("#standby-sec").value = standby;
  $("#standby-sec-val").textContent = `${standby} 秒`;
  setSwitch($("#reverse-led"), s.effectReverse);
  setSwitch($("#mute-btn"), s.inputMuted);
  setSwitch($("#autostart-btn"), s.autostart);
  const note = $("#autostart-note");
  if (note) note.textContent = s.autostartOther ? `（当前是${s.autostartOther}开机自启，打开这里会改成${s.edition}）` : "";
  setSwitch($("#closetotray-btn"), s.closeToTray !== false);
  const ver = $("#app-version");
  if (ver && s.edition) {
    ver.textContent = ver.textContent.replace(/ · .*$/, ` · ${s.edition}`);
    ver.title = `数据目录：${s.dataDir}`;
  }
  if (s.album) renderAlbum(s.album);
  const wpBtn = $("#wallpaper-toggle");
  if (wpBtn) {
    const on = s.wallpaper?.enabled !== false;
    wpBtn.classList.toggle("on", on);
    wpBtn.setAttribute("aria-checked", on ? "true" : "false");
  }
  const abSec = s.autoBlank ?? 0;
  const abBtn = $("#autoblank-toggle");
  if (abBtn) {
    abBtn.classList.toggle("on", abSec > 0);
    abBtn.setAttribute("aria-checked", abSec > 0 ? "true" : "false");
    const row = $("#autoblank-row");
    if (row) row.hidden = !(abSec > 0);
    if (abSec > 0) {
      $("#autoblank-sec").value = abSec;
      $("#autoblank-val").textContent = `${abSec} 秒`;
    }
  }
  setSwitch($("#sleep-btn"), !s.screenOff);

  const view = s.view || { fit: "cover", zoom: 1, ox: 0, oy: 0 };
  const zoomEl = $("#wp-zoom");
  if (zoomEl) {
    zoomEl.value = view.zoom;
    $("#wp-zoom-val").textContent = Math.round(view.zoom * 100) + "%";
  }
  const sb = s.standbyBrightness ?? 60;
  const sbEl = $("#standby-brightness");
  if (sbEl) {
    sbEl.value = sb;
    $("#standby-brightness-val").textContent = sb;
  }
  document.querySelectorAll(".fit-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.fit === view.fit);
  });

  const info = d.info || {};
  $("#device-info").innerHTML = [
    ["状态", d.connected ? "已连接" : d.error || "未连接"],
    ["VID / PID", `${info.vid || "-"} / ${info.pid || "-"}`],
    ["序列号", info.serial || "-"],
    ["固件", info.firmware || "-"],
    ["按键", `${info.keys || 15} 个可显示键 + 3 个导航键`],
    ["灯带", `${info.leds || 24} 颗 RGB`],
    ["壁纸", s.wallpaper?.file || "-"],
    ["设备路径", info.path || "-"],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${escHtml(v)}</dd>`).join("");

  try {
    applyRotation();
    refreshBoard();
    renderEditor();
  } catch (err) {
    console.error("控制台渲染失败", err);
    try {
      toast(`渲染错误：${err && err.message ? err.message : err}`, 8000);
    } catch (_) { /* ignore */ }
  }
  const mains = s.mainPages || [s.page ?? 0];
  const path = s.folderPath || [];
  const inFolder = !!(s.pageFolder || [])[s.page ?? 0];
  const root = path.length ? path[0].index : (s.page ?? 0);
  const pos = Math.max(0, mains.indexOf(root)) + 1;
  const names = s.pageNames || [];
  const rootName = names[root] || `第 ${pos} 页`;
  $("#page-label").textContent = inFolder
    ? `${rootName} › ${path.slice(1).map((p) => p.name).join(" › ")}`
    : `${rootName}（${pos}/${mains.length}）`;
  $("#page-label").classList.toggle("in-folder", inFolder);
  $("#page-back").hidden = !inFolder;
  $("#page-add").hidden = inFolder;
  $("#page-del").textContent = inFolder ? "删除文件夹" : "删除";
  renderLog(s.events || []);
}

function renderLog(events) {
  const box = $("#log");
  if (!box) return;
  if (!events.length) {
    box.innerHTML = '<div class="log-empty">还没有按键事件，按下 M18 上的任意键试试。</div>';
    return;
  }
  box.innerHTML = events.map((e) => {
    const down = e.state === 1;
    const time = new Date(e.t * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    return `<div class="log-row">
      <span class="k">键 ${e.key}</span>
      <span class="s ${down ? "down" : "up"}">${down ? "按下" : "松开"}</span>
      <span class="t">${time}</span>
    </div>`;
  }).join("");
}

function updateGapLabel(gap) {
  $("#wallpaper-gap-val").textContent = gap;
  const pct = (gap / 64) * 100;
}

/* ------------------------------------------------------------------ 色板 */
function buildSwatches() {
  const box = $("#swatches");
  box.innerHTML = "";
  SWATCHES.forEach((c) => {
    const el = document.createElement("button");
    el.className = "sw";
    el.dataset.color = c;
    el.style.background = c;
    el.title = c;
    el.addEventListener("click", () => setColor(c));
    box.appendChild(el);
  });
  syncSwatch();
}

function syncSwatch() {
  document.querySelectorAll("#swatches .sw").forEach((el) => {
    el.classList.toggle("active",
      String(el.dataset.color || "").toLowerCase() === String(currentColor || "").toLowerCase());
  });
}

function setColor(c) {
  currentColor = c;
  $("#color").value = c;
  $("#color-hex").textContent = c.toUpperCase();
  syncSwatch();
  pushLighting({ color: c, mode: state?.lighting?.mode === "off" ? "solid" : undefined });
}

/* ------------------------------------------------------------------ 交互 */
function bindUI() {
  // 设备设置弹窗
  const modal = $("#device-modal");
  const openDevice = () => modal && modal.classList.remove("hidden");
  const closeDevice = () => modal && modal.classList.add("hidden");
  $("#device-btn")?.addEventListener("click", openDevice);
  $("#device-close")?.addEventListener("click", closeDevice);
  modal?.addEventListener("pointerdown", (e) => {
    if (e.target === modal) closeDevice();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDevice();
  }, true);

  document.querySelectorAll("#modes .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      applyMode(chip.dataset.mode);
      pushLighting({ mode: chip.dataset.mode });
    });
  });

  $("#color").addEventListener("input", (e) => setColor(e.target.value));

  $("#led-brightness").addEventListener("input", (e) => {
    ledBrightness = Number(e.target.value);
    $("#led-brightness-val").textContent = ledBrightness;
    if (lastFrame) paintDots(lastFrame);
    pushLighting({ brightness: ledBrightness });
  });

  $("#speed").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#speed-val").textContent = v.toFixed(1) + "×";
    pushLighting({ speed: v });
  });

  // 以前每次 input 都新建一个节流函数 = 根本没节流，拖一下发几十个请求，还被服务器推回来的旧值拽回去
  const pushScreenB = throttle((v) => post("/api/screen-brightness", { value: v }), 120);
  $("#screen-brightness").addEventListener("input", (e) => {
    $("#screen-brightness-val").textContent = e.target.value;
    pushScreenB(Number(e.target.value));
  });

  $("#reset-led").addEventListener("click", async () => {
    applyState(await post("/api/lighting/reset"));
    toast("灯光已恢复默认（流光）", 1400);
  });
  $("#clear-led").addEventListener("click", () => fillAll([0, 0, 0]));
  $("#wake-btn").addEventListener("click", () => post("/api/device/wake"));

  document.querySelectorAll("#rotate-group [data-rot]").forEach((btn) => btn.addEventListener("click", async () => {
    const r = await post("/api/rotation", { value: Number(btn.dataset.rot) });
    if (state) state.rotation = r.rotation;
    applyRotation();
    refreshBoard();
    toast({ 0: "正向摆放", 90: "顺时针竖放", 180: "倒置", 270: "逆时针竖放" }[r.rotation] + "：图标已自动转正", 1600);
  }));
  window.addEventListener("resize", () => applyRotation());

  /* 壁纸 */
  const applyGap = throttle(async (value) => {
    await post("/api/wallpaper", { gap: value });
    refreshBoard();
  }, 420);
  $("#wallpaper-gap").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    updateGapLabel(v);
    applyRotation();
    applyGap(v);
  });

  const pushView = throttle(async (patch) => {
    await post("/api/wallpaper/view", patch);
    if (state) state.view = { ...(state.view || {}), ...patch };
    refreshBoard();
    if (albumState) renderAlbum(albumState);
  }, 220);
  const bindView = (sel, key, fmt) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("input", () => {
      const v = Number(el.value);
      const out = $(sel + "-val");
      if (out) out.textContent = fmt(v);
      pushView({ [key]: v });
    });
  };
  bindView("#wp-zoom", "zoom", (v) => Math.round(v * 100) + "%");

  // 在壁纸预览上拖动 = 平移壁纸显示位置（拖动时纯前端跟手，松手才写回服务端）
  const wpHolder = $("#wp-thumb");
  const wpImg = $("#wp-preview");
  if (wpHolder && wpImg) {
    let drag = null;
    wpHolder.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = {
        x: e.clientX, y: e.clientY,
        ox: state?.view?.ox ?? 0, oy: state?.view?.oy ?? 0,
        moved: false,
      };
      wpImg.style.transition = "none";
      wpHolder.classList.add("dragging");
      try { wpHolder.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
      e.preventDefault();
    });
    wpHolder.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) drag.moved = true;
      // 直接用屏幕像素位移，做到完全跟手
      wpImg.style.transform = `translate(${dx}px, ${dy}px)`;
    });
    const finishDrag = (e) => {
      if (!drag) return;
      const dx = (e?.clientX ?? drag.x) - drag.x;
      const dy = (e?.clientY ?? drag.y) - drag.y;
      const moved = drag.moved;
      const baseOx = drag.ox;
      const baseOy = drag.oy;
      drag = null;
      wpHolder.classList.remove("dragging");
      if (!moved) {
        wpImg.style.transform = "";
        return;
      }
      // 注意：这里不清 transform，等新图加载完再清（refreshBoard 的 onload），
      // 否则松手瞬间会先弹回原位再跳到新位置。
      const gap = Number($("#wallpaper-gap")?.value || 0);
      const canvasW = 5 * 64 + 4 * gap;
      const scale = canvasW / Math.max(1, wpImg.clientWidth);
      const ox = Math.max(-400, Math.min(400, Math.round(baseOx + dx * scale)));
      const oy = Math.max(-400, Math.min(400, Math.round(baseOy + dy * scale)));
      pushView({ ox, oy });
    };
    wpHolder.addEventListener("pointerup", finishDrag);
    wpHolder.addEventListener("pointercancel", finishDrag);
  }

  document.querySelectorAll(".fit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".fit-btn").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      // 换填充方式时把偏移归零，避免上一张图的位移带过来导致画面歪掉
      if (state) state.view = { ...(state.view || {}), fit: btn.dataset.fit, ox: 0, oy: 0 };
      pushView({ fit: btn.dataset.fit, ox: 0, oy: 0 });
    });
  });

  $("#choose-wallpaper").addEventListener("click", () => $("#wallpaper-file").click());
  $("#wallpaper-file").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      const up = await upload(f);
      const r = await post("/api/wallpaper/file", { file: up.file });
      if (r.ok) refreshBoard();
    } catch (err) {
      toast("壁纸设置失败：" + err.message);
    }
  });

  $("#calibrate").addEventListener("click", async () => {
    await post("/api/wallpaper/calibrate");
    refreshBoard();
  });

  $("#reverse-led").addEventListener("click", async () => {
    const v = !(state?.effectReverse);
    const r = await post("/api/lighting/reverse", { value: v });
    if (state) state.effectReverse = r.value;
    setSwitch($("#reverse-led"), r.value);
    toast(r.value ? "灯效方向：反向" : "灯效方向：正向", 1200);
  });

  $("#sleep-btn").addEventListener("click", async () => {
    const off = !(state?.screenOff);
    if (state) state.screenOff = off;
    setSwitch($("#sleep-btn"), !off);
    const r = await post(off ? "/api/device/sleep" : "/api/device/wake");
    if (state) state.screenOff = r.screenOff ?? off;
    setSwitch($("#sleep-btn"), !(state?.screenOff));
    toast(off ? "已息屏" : "已亮屏", 1200);
  });

  $("#wallpaper-toggle").addEventListener("click", async () => {
    const v = !(state?.wallpaper?.enabled ?? true);
    const r = await post("/api/wallpaper/toggle", { value: v });
    const btn = $("#wallpaper-toggle");
    btn.classList.toggle("on", !!r.value);
    btn.setAttribute("aria-checked", r.value ? "true" : "false");
    if (state) state.wallpaper = { ...(state.wallpaper || {}), enabled: r.value };
    if (v) {                                   // 二选一：开待机画面 -> 关自动息屏
      if (state) state.autoBlank = 0;
      const ab = $("#autoblank-toggle");
      if (ab) {
        ab.classList.remove("on");
        ab.setAttribute("aria-checked", "false");
      }
      const row = $("#autoblank-row");
      if (row) row.hidden = true;
    }
    toast(r.value ? "待机壁纸已启用（自动息屏已关闭）" : "待机壁纸已停用", 1600);
  });

  const autoblankSec = () => Number($("#autoblank-sec")?.value || 300);
  const paintAutoblank = (sec) => {
    const on = sec > 0;
    const btn = $("#autoblank-toggle");
    if (btn) {
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-checked", on ? "true" : "false");
    }
    const row = $("#autoblank-row");
    if (row) row.hidden = !on;
    if (on) {
      $("#autoblank-sec").value = sec;
      $("#autoblank-val").textContent = `${sec} 秒`;
    }
    if (state) state.autoBlank = sec;
  };
  $("#autoblank-toggle")?.addEventListener("click", async () => {
    const on = !((state?.autoBlank ?? 0) > 0);
    const r = await post("/api/autoblank", { enabled: on, sec: autoblankSec() });
    paintAutoblank(r.autoBlank ?? 0);
    if (state) state.wallpaper = { ...(state.wallpaper || {}), enabled: r.wallpaperEnabled !== false };
    const wp = $("#wallpaper-toggle");
    if (wp) {
      const wpOn = r.wallpaperEnabled !== false;
      wp.classList.toggle("on", wpOn);
      wp.setAttribute("aria-checked", wpOn ? "true" : "false");
    }
    toast(on ? `自动息屏已开启：空闲 ${r.autoBlank} 秒后关屏（待机画面已关闭）` : "自动息屏已关闭", 1800);
  });
  const pushAutoblank = throttle(async (sec) => {
    const r = await post("/api/autoblank", { enabled: true, sec });
    paintAutoblank(r.autoBlank ?? sec);
  }, 400);
  $("#autoblank-sec")?.addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#autoblank-val").textContent = `${v} 秒`;
    pushAutoblank(v);
  });

  const goPage = async (delta) => {
    applyState(await post("/api/page", { delta }));
    toast(`第 ${(state?.page ?? 0) + 1} 页`, 900);
  };
  $("#page-prev").addEventListener("click", () => goPage(-1));
  $("#page-next").addEventListener("click", () => goPage(1));
  $("#page-add").addEventListener("click", async () => {
    const n = (state?.mainPages || state?.pageNames || []).length;
    if (!(await confirmDialog(`在当前页之后新增一页？<br><small>新页会复制当前页的按键图片和动作，加完一共 ${n + 1} 页。</small>`))) return;
    applyState(await post("/api/page", { add: true }));
  });

  $("#page-rename").addEventListener("click", async () => {
    const idx = state?.page ?? 0;
    const cur = state?.pageNames?.[idx] || `第 ${idx + 1} 页`;
    const name = window.prompt("页面名称", cur);
    if (!name) return;
    await post("/api/page/rename", { index: idx, name });
    await refreshState();
  });

  $("#page-back").addEventListener("click", async () => {
    applyState(await post("/api/folder/back", {}));
  });
  $("#sel-open-folder").addEventListener("click", async () => {
    const pid = gestureView(selectedKey, editGesture)?.spec?.page;
    if (!pid) return;
    selectKey(null);
    applyState(await post("/api/folder/open", { page: pid }));
    toast("已进入文件夹：左上角 11 号键是返回键", 2000);
  });
  $("#page-del").addEventListener("click", async () => {
    const inFolder = !!(state?.pageFolder || [])[state?.page ?? 0];
    if (!inFolder && (state?.mainPages || [0]).length <= 1) {
      toast("至少保留一页", 1400);
      return;
    }
    if (!(await confirmDialog(inFolder
      ? "删除这个文件夹？里面的按键（和子文件夹）会一起删除，放文件夹的那个键也会清空动作。"
      : "删除当前页？页面上的按键图片和动作（含里面的文件夹）会一起删除。"))) return;
    await post("/api/page/delete", { index: state?.page ?? 0 });
    await refreshState();
  });

  $("#crop-tool").addEventListener("click", () => openCropTool());

  $("#wp-reset").addEventListener("click", async () => {
    await post("/api/wallpaper/reset");
    await refreshState();
    toast("壁纸参数已重置", 1200);
  });

  // 把图片拖到壁纸预览上，直接替换壁纸
  const wpBox = $("#wp-preview")?.closest(".wp-thumb") || $("#wp-preview");
  if (wpBox) {
    wpBox.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.stopPropagation();
      wpBox.classList.add("drop-hot");
    });
    wpBox.addEventListener("dragleave", () => wpBox.classList.remove("drop-hot"));
    wpBox.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      wpBox.classList.remove("drop-hot");
      await dropWallpaper(e.dataTransfer);
    });
  }

  $("#mute-btn").addEventListener("click", async () => {
    const v = !(state?.inputMuted);
    const r = await post("/api/device/mute", { value: v });
    if (state) state.inputMuted = r.value;
    setSwitch($("#mute-btn"), r.value);
    toast(r.value ? "已屏蔽按键" : "按键已恢复", 1400);
  });


  $("#default-wallpaper").addEventListener("click", async () => {
    await post("/api/wallpaper/default");
    await refreshState();
  });

  /* 按键编辑区 */
  $("#sel-thumb").addEventListener("dragover", (e) => {
    e.preventDefault();
    $("#sel-thumb").classList.add("dropping");
  });
  $("#sel-thumb").addEventListener("dragleave", () => $("#sel-thumb").classList.remove("dropping"));
  $("#sel-thumb").addEventListener("drop", (e) => {
    e.preventDefault();
    $("#sel-thumb").classList.remove("dropping");
    if (selectedKey) dropOnKey(selectedKey, e.dataTransfer);
  });
  $("#thumb-add").addEventListener("click", () => {
    if (!selectedKey || selectedKey > 15) return;
    $("#key-file").value = "";
    $("#key-file").click();
  });

  $("#thumb-clear").addEventListener("click", async () => {
    if (!selectedKey || selectedKey > 15) return;
    dropPendingForKey(selectedKey);   // 还没保存的预览也要一起清掉，不然图还留在格子上
    await post("/api/key/clear", { key: selectedKey });
    toast("已清除该按键的图片", 1200);
    await refreshState();
  });

  $("#key-file").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f || !selectedKey) return;
    try {
      const up = await upload(f);
      if (!up?.ok) {
        toast(up?.error || "上传失败", 2200);
        return;
      }
      // 和其它拖放一样：先显示在键上，点「保存」才写入
      setPendingIcon(selectedKey, editGesture, {
        kind: "image",
        name: up.file,
        src: `/api/asset?p=${encodeURIComponent(up.file)}&v=${Date.now()}`,
        label: "图片",
      });
    } catch (err) {
      toast("图片设置失败：" + err.message);
    }
  });

  // 下拉只改表单，不提交；点「保存」时三种触发方式一起写进服务端
  $("#sel-nav").addEventListener("change", () => {
    if (selectedKey) formDrafts[editGesture] = readFormDraft();
  });
  $("#sel-type").addEventListener("change", () => {
    if (selectedKey) formDrafts[editGesture] = readFormDraft();
    applyTypeVisibility($("#sel-type").value);
  });
  $("#sel-secret").addEventListener("change", () => applyTypeVisibility($("#sel-type").value));
  document.querySelectorAll("#sel-gesture button").forEach((b) => {
    b.addEventListener("click", () => {
      // 切页签前把当前表单存成草稿，换回来时还在（不再依赖服务端）
      if (selectedKey && draftDiffersFromSaved(selectedKey, editGesture)) formDrafts[editGesture] = readFormDraft();
      editGesture = b.dataset.g;
      renderEditor();
    });
  });
  // 表单里任何改动都记进草稿：这样服务端推送刷新画面时，正在填的内容不会被冲掉
  ["#sel-value", "#sel-macro", "#sel-media", "#sel-mouse", "#sel-window", "#sel-scene", "#sel-console", "#sel-enter", "#sel-secret", "#sel-nav"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const snap = () => {
      if (!selectedKey) return;
      formDrafts[editGesture] = readFormDraft();
      paintDraftMarks();
    };
    el.addEventListener("input", snap);
    el.addEventListener("change", snap);
  });
  $("#sel-pick").addEventListener("click", async () => {
    const r = await post("/api/dialog/pick", { mode: "file" });
    if (r.ok && r.path) {
      $("#sel-value").value = r.path;
      if (selectedKey) formDrafts[editGesture] = readFormDraft();
    }
  });

  $("#sel-save-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    // 拖上来还没写入的图标 / 图片 / 程序：点保存才真正写进去
    let appliedPending = false;
    if (pendingIcon) {
      const p = pendingIcon;
      pendingIcon = null;
      const r = p.kind === "plugin"
        ? await post("/api/key/plugin", { key: p.key, plugin: p.plugin, index: p.index })
        : p.kind === "program"
        ? await post("/api/key/bind-program", { key: p.key, path: p.path })
        : p.kind === "image"
        ? await post("/api/key/image", { key: p.key, file: p.name })
        : await post("/api/key/icon", { key: p.key, name: p.name });
      if (!r?.ok) {
        toast(r?.error || "应用失败", 2600);
        refreshCells();
        renderEditor();
        return;
      }
      hideDockPreview();
      await refreshState();
      if (p.kind === "plugin" && r.native && r.needsInput) {
        selectKey(p.key);
        $("#key-editor")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        toast(`已换成控制台的「${r.label}」动作，请在下方补充内容后再点一次「保存」`, 3400);
      } else {
        toast(`按键 ${p.key} 已保存`, 1600);
      }
      appliedPending = true;
    }
    const n = selectedKey;
    // 当前页签的内容也进草稿，这样三种触发方式可以一起提交
    // （只保存了刚拖上来的图标时，别把"没改过的表单"也当成改动）
    if (!appliedPending || Object.keys(formDrafts).length) formDrafts[editGesture] = readFormDraft();
    const order = ["press", "long", "double"];
    const todo = order.filter((g) => formDrafts[g]);
    if (!todo.length) return;

    // 文件夹被替换：先问一次
    for (const g of todo) {
      const d = formDrafts[g];
      const before = gestureView(n, g);
      if (before?.type === "folder" && d.type !== "folder"
        && !(await confirmDialog(`按键 ${n} 的${gestureLabel(g)}原来是文件夹「${escHtml(before.value)}」，替换后文件夹和里面的按键会一起删除。继续？`))) return;
      if (d.type === "folder" && n > 15) return toast("导航键没有屏幕，不能做文件夹", 1800);
    }

    const saved = [];
    let failed = "";
    let madeFolder = false;
    for (const g of todo) {
      const d = formDrafts[g];
      if (d.type === "plugin") continue;                 // 插件动作不在这里设置
      const before = gestureView(n, g);
      const spec = specFromDraft(d, n, g);
      if (!spec) {                                       // 「未绑定」= 清掉
        if (before) {
          await post("/api/key/action/clear", { key: n, gesture: g });
          saved.push(gestureLabel(g));
        }
        continue;
      }
      if (before?.type === "folder" && d.type === "folder" && before.value === spec.name) continue;
      const r = await post("/api/key/action", spec);
      if (r.ok) {
        saved.push(gestureLabel(g));
        if (d.type === "folder" && before?.type !== "folder") madeFolder = true;
      } else if (!failed) {
        failed = {
          toggle: "双状态热键要填两个组合键，用 | 隔开",
          sound: "找不到这个音频文件",
          text: "要输入的文字不能为空",
        }[d.type] || "路径不存在或内容为空";
      }
    }

    if (saved.length) {
      formDrafts = {};
      toast(`已保存：${saved.join(" + ")}${madeFolder ? "（文件夹已创建，点「打开文件夹编辑」往里放按键）" : ""}`, madeFolder ? 2600 : 1400);
    }
    if (failed) toast("没保存上：" + failed, 2400);
    await refreshState();
  });

  const sendStyle = throttle(async (body) => {
    await post("/api/key/style", body);
  }, 320);
  const pushStyle = (e) => {
    if (!selectedKey || e?.isComposing) return;          // 中文输入法还在组字：等上屏再发
    const body = {
      key: selectedKey,
      text: $("#sel-text").value,
      color: $("#sel-fg").value,
      bg: $("#sel-bg").value,
      opacity: Number($("#sel-opacity").value),
      size: Number($("#sel-size").value),
      pos: $("#sel-pos").value,
    };
    if (state) {                                          // 本地先改，格子立刻跟着变
      state.keyStyles = state.keyStyles || {};
      const { key, ...style } = body;
      state.keyStyles[key] = { ...(state.keyStyles[key] || {}), ...style };
      refreshCells();
    }
    sendStyle(body);       // body 里已带上 key：节流延迟期间切了键也不会写错键
  };
  ["#sel-text", "#sel-fg", "#sel-bg", "#sel-pos"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("change", pushStyle);
  });
  ["#sel-opacity", "#sel-size"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("input", () => {
      const v = Number(el.value);
      const out = $(sel + "-val");
      if (out) out.textContent = sel === "#sel-opacity" ? `${v}%` : `${v}`;
      pushStyle();
    });
  });
  $("#sel-text").addEventListener("input", pushStyle);
  $("#sel-text").addEventListener("compositionend", pushStyle);

  let recording = false;  $("#sel-record").addEventListener("click", () => {
    if (!selectedKey) return;
    recording = !recording;
    $("#sel-record").classList.toggle("on", recording);
    if (recording) toast("按下要录制的组合键（Esc 取消）", 2500);
  });
  document.addEventListener("keydown", (e) => {
    if (!recording) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      recording = false;
      $("#sel-record").classList.remove("on");
      return;
    }
    const parts = [];
    if (e.ctrlKey) parts.push("ctrl");
    if (e.shiftKey) parts.push("shift");
    if (e.altKey) parts.push("alt");
    if (e.metaKey) parts.push("win");
    // 用物理键位 e.code 而不是 e.key：Shift+1 录成 shift+1 而不是 shift+!
    const k = codeToKeyName(e.code);
    if (k) parts.push(k);
    const fnKey = /^f\d+$/.test(k || "");
    if (!k || (parts.length < 2 && !fnKey)) {
      toast(k ? "至少要包含一个修饰键（Ctrl/Alt/Shift/Win），F 键除外" : "这个键暂不支持", 1600);
      return;
    }
    const combo = parts.join("+");
    if ($("#sel-type").value === "toggle") {
      const cur = $("#sel-value").value.split("|").map((x) => x.trim()).filter(Boolean);
      $("#sel-value").value = (cur.length === 1 ? [cur[0], combo] : [combo]).join(" | ");
      if (cur.length !== 1) toast("已录第 1 个，再点「录制热键」录第 2 个", 1800);
    } else {
      $("#sel-value").value = combo;
      $("#sel-type").value = "hotkey";
    }
    applyTypeVisibility($("#sel-type").value);
    recording = false;
    $("#sel-record").classList.remove("on");
  }, true);


  $("#autostart-btn").addEventListener("click", async () => {
    const v = !(state?.autostart);
    const r = await post("/api/autostart", { value: v });
    if (state) state.autostart = r.value;
    setSwitch($("#autostart-btn"), r.value);
    if (r.ok === false) {
      toast("开机自启没设置成功：启动文件夹和注册表都被拦了，把本程序加入安全软件信任区再试", 4200);
    } else {
      toast(r.value ? `已设置开机自启（${state?.edition || ""}）` : "已关闭开机自启", 1600);
    }
    refreshState();
  });

  $("#closetotray-btn").addEventListener("click", async () => {
    const v = !(state?.closeToTray !== false);
    const r = await post("/api/closetotray", { value: v });
    if (state) state.closeToTray = r.value;
    setSwitch($("#closetotray-btn"), r.value);
    toast(r.value ? "关掉界面窗口后会继续在托盘里后台运行" : "关掉界面窗口会直接退出控制台", 2400);
  });

  $("#export-btn").addEventListener("click", async () => {
    const r = await post("/api/config/export");
    const blob = new Blob([JSON.stringify(r.config || {}, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `m18-config-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast("配置已导出", 1400);
  });

  $("#import-btn").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    try {
      const cfg = JSON.parse(await f.text());
      const r = await post("/api/config/import", { config: cfg });
      toast(r.ok ? "配置已导入" : "配置文件格式不对", 2200);
      if (r.ok) await refreshState();
    } catch (_) {
      toast("读取配置文件失败", 2000);
    }
  });

  $("#restore-btn").addEventListener("click", async () => {
    const r = await post("/api/config/backups");
    const items = r.items || [];
    if (!items.length) {
      toast("还没有可用的备份", 1800);
      return;
    }
    const pick = window.prompt(`输入要恢复的备份序号（1 最新）\n\n${items.slice(0, 8).map((n, i) => `${i + 1}. ${n}`).join("\n")}`, "1");
    const idx = Number(pick) - 1;
    if (!(idx >= 0 && idx < items.length)) return;
    const ok = await post("/api/config/restore", { name: items[idx] });
    toast(ok.ok ? "配置已恢复" : "恢复失败", 2000);
    if (ok.ok) await refreshState();
  });

  $("#sel-plugin-pi").addEventListener("click", () => {
    if (selectedKey) openPluginSettings(selectedKey);
  });
  $("#sel-thumb").addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    hideKeyMenu();
    showThumbMenu(e.clientX, e.clientY);
  });
  $("#official-sync").addEventListener("click", async () => {
    if (state && state.officialReady === false) {
      toast("这台电脑没装官方 Stream Dock 驱动（或装在别的位置），没有官方资源可同步。"
        + "自带的图标包、28 个内置插件和默认场景照常能用。", 5200);
      return;
    }
    // 先问同步哪一块：全部 / 场景 / 图标 / 插件
    const what = await choiceDialog(
      "从官方 Stream Dock 驱动同步什么？<br><span class='muted'>官方软件里的场景、图标包和插件，可以只挑一部分。</span>",
      [{ value: "all", label: "全部同步", primary: true },
       { value: "scenes", label: "只同步场景" },
       { value: "icons", label: "只同步图标包" },
       { value: "plugins", label: "只同步插件" }]);
    if (!what) return;

    let mode = "new";
    if (what === "all" || what === "scenes") {
      mode = await choiceDialog("官方场景怎么处理？", [
        { value: "new", label: "只添加新场景（保留已导入的改动）", primary: true },
        { value: "all", label: "全部重新导入（覆盖已导入的官方场景）" }]);
      if (!mode) return;
      if (mode === "all" && !(await confirmDialog("已导入的官方场景里你改过的按键会被覆盖，继续？"))) return;
    }

    let withPlugins = false;
    if (what === "all" || what === "plugins") {
      withPlugins = await confirmDialog(
        "把官方插件（含 Node 运行环境，约 500 MB）复制到本地？<br>"
        + "<span class='muted'>复制后即使卸载官方 Stream Dock 软件，插件也能继续使用。</span>");
      if (!withPlugins && what === "plugins") return;        // 只同步插件 = 复制插件，拒绝就什么都不做
    }

    const wantScenes = what === "all" || what === "scenes";
    const wantIcons = what === "all" || what === "icons";
    toast(withPlugins ? "正在同步并备份插件，可能要一两分钟…" : "正在同步官方资源…", 2500);
    const r = await post("/api/official/sync", {
      mode, scenes: wantScenes, icons: wantIcons, plugins: withPlugins,
    });
    if (r.ok === false) {
      toast(r.reason || "同步失败：没有找到官方驱动", 5200);
      return;
    }
    iconPacks = null;
    pluginItems = null;
    await loadIcons(true);
    await loadPlugins(true);
    await window.m18ReloadScenes?.();
    await refreshState();
    const parts = [];
    if (wantScenes) parts.push(`场景 ${(r.scenes || []).length} 个（${mode === "all" ? "重导" : "新增"}）`);
    if (wantIcons) parts.push(`图标包 ${r.packs ?? 0} 个（${r.icons ?? 0} 个图标）`);
    if (withPlugins) parts.push(`备份插件 ${r.mirroredPlugins ?? 0} 个`);
    toast("同步完成：" + (parts.join(" · ") || "没有变化"), 4200);
  });
  let searchTimer = null;
  $("#icon-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { loadIcons(); loadPlugins(); }, 160);
  });
  bindSidebar();
  bindCustomLab();
  loadIcons();
  loadLibrary();
  bindLibrary();
  bindLibraryDrop();
  bindLibraryMenu();
  loadPlugins();

  let sceneMenuEl = null;
  const ensureSceneMenu = () => {
    if (sceneMenuEl) return sceneMenuEl;
    sceneMenuEl = document.createElement("div");
    sceneMenuEl.className = "key-menu";
    sceneMenuEl.hidden = true;
    document.body.appendChild(sceneMenuEl);
    sceneMenuEl.addEventListener("pointerdown", (e) => e.stopPropagation());
    sceneMenuEl.addEventListener("click", async (e) => {
      const act = e.target.closest("button")?.dataset.act;
      const name = sceneMenuEl.dataset.name;
      const custom = sceneMenuEl.dataset.custom === "1";
      if (!act || !name) return;
      sceneMenuEl.hidden = true;
      if (act === "mute") {
        const r = await post("/api/scenes", { name, mute: sceneMenuEl.dataset.muted !== "1" });
        renderScenes(r.items || []);
        return;
      }
      if (act === "album-edit") {
        window.m18ShowStandbyTab?.("album");
        document.querySelector("#standby-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (act === "rename") {
        startInlineRename(name, custom);
      } else if (act === "export") {
        const r = await post("/api/scenes/export", { name });
        if (!r.ok) return toast(r.error || "导出失败", 2000);
        const bin = Uint8Array.from(atob(r.data), (c) => c.charCodeAt(0));
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([bin], { type: "application/zip" }));
        a.download = r.filename;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 3000);
        toast(`已导出「${name}」（${r.images} 张图片${r.plugins ? `、${r.plugins} 个自制插件` : ""}）`, 2600);
      } else if (act === "dup") {
        const r = await post("/api/scenes", { duplicate: name });
        toast(r.ok ? "已复制场景" : "复制失败", 1500);
        renderScenes(r.items || []);
      } else if (act === "apps") {
        const items = (await post("/api/scenes", {})).items || [];
        const cur = items.find((i) => i.name === name)?.apps || [];
        const v = await appsDialog(name, cur);
        if (v === null) return;
        const r = await post("/api/scenes", { link: name, apps: v });
        toast(r.ok ? (v.trim() ? `已关联：${v}` : "已取消关联") : "保存失败", 1800);
        renderScenes(r.items || []);
      } else if (act === "del") {
        if (!(await confirmDialog(`删除场景「${escHtml(name)}」？`))) return;
        const r = await post("/api/scenes", { delete: name });
        renderScenes(r.items || []);
      }
    });
    return sceneMenuEl;
  };
  const showSceneMenu = (x, y, name, custom, album, muted) => {
    const menu = ensureSceneMenu();
    menu.dataset.name = name;
    menu.dataset.custom = custom ? "1" : "0";
    menu.innerHTML = (album ? `<button data-act="album-edit">编辑相册…</button>` : `<button data-act="rename">重命名</button>`
      + `<button data-act="dup">复制场景</button>`
      + `<button data-act="export">导出场景…</button>`)
      + `<button data-act="mute">${muted ? "启用" : "停用（切换时跳过）"}</button>`
      + (custom || album ? "" : `<button data-act="apps">关联程序…</button>`)
      + (custom || album ? "" : `<button data-act="del">删除</button>`);
    menu.dataset.muted = muted ? "1" : "0";
    menu.hidden = false;
    menu.style.left = `${Math.min(x, window.innerWidth - 120)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - 120)}px`;
  };

  // 点菜单外面收起
  document.addEventListener("pointerdown", (e) => {
    if (sceneMenuEl && !sceneMenuEl.hidden && !e.target.closest(".key-menu")) {
      sceneMenuEl.hidden = true;
    }
  }, true);

  const startInlineRename = (name, custom) => {
    const row = [...document.querySelectorAll("#scene-list .scene-row")]
      .find((r) => r.dataset.name === name);
    if (!row) return;
    const label = row.querySelector("b");
    if (!label) return;
    const input = document.createElement("input");
    input.className = "scene-rename";
    input.value = name;
    label.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const commit = async (save) => {
      if (done) return;
      done = true;
      const next = input.value.trim();
      if (save && next && next !== name) {
        const r = await post("/api/scenes", custom
          ? { rename_custom: next }
          : { rename: next, old: name });
        renderScenes(r.items || []);
      } else {
        await reloadScenes();
      }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      else if (e.key === "Escape") { e.preventDefault(); commit(false); }
      e.stopPropagation();
    });
    input.addEventListener("blur", () => commit(true));
    input.addEventListener("click", (e) => e.stopPropagation());
  };

  const renderScenes = (items) => {
    const box = $("#scene-list");
    if (!box) return;
    sceneItems = items;
    box.innerHTML = items.length
      ? items.map((it) => {
        const name = typeof it === "string" ? it : it.name;
        const active = typeof it === "string" ? false : it.active;
        const custom = typeof it === "string" ? false : it.custom;
        const isOfficial = typeof it === "string" ? false : it.official;
        const picked = selectedScene === name;
        const album = !!it.album;
        const albumScene = !!it.albumScene;
        return `<div class="row scene-row${active ? " active" : ""}${custom ? " custom" : ""}${isOfficial ? " official" : ""}`
          + `${album ? " album" : ""}${albumScene ? " album-scene" : ""}${it.muted ? " muted" : ""}${picked ? " picked" : ""}"`
          + ` data-name="${escHtml(name)}" data-custom="${custom ? 1 : 0}" data-album="${album ? 1 : 0}"`
          + ` data-album-scene="${albumScene ? 1 : 0}">`
          + `<b title="${albumScene ? "相册场景：整屏轮播图片（拖动可调整顺序）" : album ? "整屏相册幻灯片（不可删除）" : isOfficial ? "来自官方驱动的场景" : "拖动可调整顺序"}">`
          + `${escHtml(name)}${album ? `<em>${it.count || 0}</em>` : ""}</b>`
          + ((it.apps || []).length ? `<small class="scene-apps" title="前台切到这些程序时自动切换">⇢ ${escHtml(it.apps.join(", "))}</small>` : "")
          + `<span class="scene-ops">${active ? "使用中" : ""}`
          + `<button class="scene-mute${it.muted ? "" : " on"}" data-mute="${escHtml(name)}" title="${it.muted ? "已停用：「下一场景」和关联程序都会跳过它（点击启用）" : "启用中（点击停用，切换场景时跳过）"}"><span></span></button></span>`
          + "</div>";
      }).join("")
      : "<div class='row'>还没有场景，点 ＋ 新建</div>";
    box.querySelectorAll(".scene-mute").forEach((btn) => btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const name = btn.dataset.mute;
      const r = await post("/api/scenes", { name, mute: btn.classList.contains("on") });
      renderScenes(r.items || []);
      toast(btn.classList.contains("on") ? `「${name}」已停用，切换场景时跳过` : `「${name}」已启用`, 1600);
    }));
    box.querySelectorAll(".scene-row").forEach((row) => {
      const name = row.dataset.name;
      row.addEventListener("click", async () => {
        if (Date.now() - sceneDragEndedAt < 450) return;        // 刚拖过排序
        if (row.querySelector(".scene-rename")) return;      // 正在改名
        if (row.dataset.album === "1") {
          // 相册场景：先选中并把编辑区换成图片编辑区（空相册也能直接拖图进去），再看要不要播。
          selectedScene = name;
          renderEditor();
          if (row.classList.contains("active")) {
            toast(`「${name}」已经是当前场景`, 1800);
            return;
          }
          const r = await post("/api/scenes", { apply: name });
          if (!r.ok) {
            // 切换失败也保持编辑区打开，让用户能往里加图片
            renderScenes(r.items || []);
            return toast(r.reason || "相册场景切不过去", 2600);
          }
          renderScenes(r.items || []);
          renderEditor();
          forceBoardReload();
          toast(`已切换到相册场景「${name}」`, 1800);
          await refreshState();
          return;
        }
        selectedScene = name;
        renderEditor();
        if (row.classList.contains("active") || row.classList.contains("switching")) return;
        box.querySelectorAll(".scene-row.switching").forEach((r) => r.classList.remove("switching"));
        row.classList.add("switching");
        const r = await post("/api/scenes", { apply: name });
        if (!r.ok) {
          row.classList.remove("switching");
          toast(r.reason || "切换失败", 2800);
          return;
        }
        forceBoardReload();
        toast(`已切换到「${name}」`, 1200);
        await refreshState();
        await reloadScenes();
      });
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showSceneMenu(e.clientX, e.clientY, name, row.dataset.custom === "1",
          row.dataset.album === "1" && row.dataset.albumScene !== "1",
          row.classList.contains("muted"));
      });

      // 长按 0.45 秒进入拖动排序（和按键拖动一致的手感）
      let holdTimer = 0;
      let dragging = false;
      const onMove = (e) => {
        if (!dragging) return;
        e.preventDefault();
        const others = [...box.querySelectorAll(".scene-row")].filter((r) => r !== row);
        const hit = others.find((r) => {
          const b = r.getBoundingClientRect();
          return e.clientY >= b.top && e.clientY <= b.bottom;
        });
        if (!hit) return;
        const b = hit.getBoundingClientRect();
        if (e.clientY < b.top + b.height / 2) hit.before(row);
        else hit.after(row);
      };
      const onUp = async () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
        document.body.classList.remove("no-select");
        if (!dragging) return;
        dragging = false;
        row.classList.remove("dragging");
        sceneDragEndedAt = Date.now();
        const order = [...box.querySelectorAll(".scene-row")].map((r) => r.dataset.name);
        const r = await post("/api/scenes", { reorder: order });
        if (!r.ok) return toast("排序没保存住", 2000);
        renderScenes(r.items || []);
        toast("场景顺序已保存", 1200);
      };
      row.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || e.target.closest(".scene-mute, .scene-rename")) return;
        holdTimer = setTimeout(() => {
          dragging = true;
          row.classList.add("dragging");
          document.body.classList.add("no-select");
          document.addEventListener("pointermove", onMove, { passive: false });
          document.addEventListener("pointerup", onUp);
          document.addEventListener("pointercancel", onUp);
        }, 450);
      });
      const cancelHold = () => {
        if (dragging) return;
        clearTimeout(holdTimer);
      };
      row.addEventListener("pointerup", cancelHold);
      row.addEventListener("pointercancel", cancelHold);
      row.addEventListener("pointerleave", cancelHold);
      row.addEventListener("pointermove", (e) => {
        if (dragging) return;
        // 移动了就先取消长按，免得想点一下却进了拖动模式
        if (Math.abs(e.movementX) + Math.abs(e.movementY) > 3) cancelHold();
      });
    });
  };
  const reloadScenes = async () => {
    const r = await post("/api/scenes", {});
    renderScenes(r.items || []);
    sceneNames = (r.items || []).map((i) => i.name);
    const c = $("#scene-count");
    if (c) c.textContent = String((r.items || []).length);
    renderEditor();          // 切到相册场景时编辑区要跟着换成图片编辑
  };
  window.m18ReloadScenes = reloadScenes;
  const importScene = async (file) => {
    if (!file) return;
    if (!/\.(m18scene|zip)$/i.test(file.name)) return toast("请选择 .m18scene 场景文件", 1800);
    const data = await readAsDataURL(file);
    const r = await post("/api/scenes/import", { data, name: file.name.replace(/\.(m18scene|zip)$/i, "") });
    if (!r.ok) return toast(r.error || "导入失败", 2200);
    selectedScene = r.name;
    renderScenes(r.items || []);
    sceneNames = (r.items || []).map((i) => i.name);
    toast(`已导入「${r.name}」（${r.images} 张图片${r.plugins ? `、${r.plugins} 个自制插件` : ""}），单击即可切换`, 3000);
  };
  $("#scene-import").addEventListener("click", () => $("#scene-import-file").click());
  $("#scene-import-file").addEventListener("change", (e) => {
    importScene(e.target.files?.[0]);
    e.target.value = "";
  });
  const sceneBox = $("#scene-list");
  sceneBox.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    e.stopPropagation();
  });
  sceneBox.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (!f || !/\.(m18scene|zip)$/i.test(f.name)) return;
    e.preventDefault();
    e.stopPropagation();
    importScene(f);
  });
  $("#scene-add").addEventListener("click", async () => {
    const pick = await newSceneDialog();
    if (!pick) return;
    const r = await post("/api/scenes", { create: pick.name, kind: pick.kind });
    if (!r.ok) return toast(r.reason || "新建失败", 2400);
    selectedScene = r.name;
    await refreshState();
    await reloadScenes();
    renderEditor();
    toast(pick.kind === "album"
      ? `已新建相册场景「${pick.name}」：在编辑区里拖入图片`
      : `已新建场景「${pick.name}」`, 2600);
  });
  $("#scene-del-btn").addEventListener("click", async () => {
    const name = selectedScene;
    if (!name) {
      toast("先在列表里选中一个场景", 1600);
      return;
    }
    const items = (await post("/api/scenes", {})).items || [];
    const target = items.find((i) => i.name === name);
    if (target?.custom || target?.builtin) {
      toast(target.builtin ? "内置相册不能删除" : "自定义场景不能删除", 1600);
      return;
    }
    if (!(await confirmDialog(`删除场景「${escHtml(name)}」？${target?.albumScene ? "<br>里面的图片文件不会被删掉。" : ""}`))) return;
    const r = await post("/api/scenes", { delete: name });
    selectedScene = null;
    renderScenes(r.items || []);
    renderEditor();
  });
  reloadScenes();


  const pushStandbyB = throttle((v) => post("/api/standby-brightness", { value: v }), 250);
  $("#standby-brightness").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#standby-brightness-val").textContent = String(v);
    pushStandbyB(v);
  });

  const pushStandby = throttle((v) => post("/api/standby", { sec: v }), 300);
  $("#standby-sec").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#standby-sec-val").textContent = `${v} 秒`;
    pushStandby(v);
  });

  $("#sel-reset-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    const names = { 16: "上一页", 17: "主页", 18: "下一页" };
    const name = names[selectedKey];
    if (!name) return;
    if (!(await confirmDialog(`把按键 ${selectedKey} 还原成默认的「${name}」？<br><small>长按 / 双击都会清掉，只保留短按。</small>`))) return;
    await post("/api/key/reset-nav", { key: selectedKey });
    formDrafts = {};
    await refreshState();
    renderEditor();
  });

  // 切到控制台窗口不会唤醒待机（只有设备按键或显式操作才会）
  $("#sel-clear-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    if (!(await confirmDialog(`清除按键 ${selectedKey} 的图片、动作和样式？`))) return;
    dropPendingForKey(selectedKey);       // 预览中的图也一起清，不然格子上还留着
    await post("/api/key/clear", { key: selectedKey });
    await post("/api/key/action/clear", { key: selectedKey });
    await post("/api/key/action/clear", { key: selectedKey, gesture: "long" });
    await post("/api/key/action/clear", { key: selectedKey, gesture: "double" });
    await post("/api/key/style", { key: selectedKey, text: "",
      color: "#e8eefc", bg: "#000000", opacity: 100, size: 20, pos: "center" });
    await refreshState();
  });

  $("#reconnect").addEventListener("click", async () => {
    if (!(await confirmDialog("重新连接并接管？<br><small>会断开 SDK 再重新枚举设备（约 3 秒），期间屏幕会闪一下。</small>"))) return;
    await post("/api/device/reconnect");
    toast("正在重新连接…", 1600);
    setTimeout(() => refreshState(), 3500);
  });

  /* 画面卡住（灯效正常、屏幕不刷新）时：清屏 + 背光重启 + 重推 15 个键 */
  $("#hard-reset").addEventListener("click", async () => {
    const r = await post("/api/device/hard-reset", { hard: false });
    toast(r?.ok === false ? (r.error || "设备没连上") : "已清屏重推，看看屏幕刷新没有", 2600);
  });

  /* 还不行就软件拔插：让 Windows 重新枚举设备（相当于拔了 USB 再插） */
  $("#usb-reset").addEventListener("click", async () => {
    if (!(await confirmDialog("让系统重新枚举这块 Dock？<br><small>相当于把 USB 线拔下来再插回去：屏幕会黑一下、约 3 秒，之后控制台会自动接管。不是管理员时会弹一次权限确认。</small>"))) return;
    await post("/api/device/usb-reset");
    toast("正在重新枚举设备…", 2200);
    setTimeout(() => refreshState(), 7000);
  });

  /* 动图错位时的手动对齐：整组重新加载后一起开始 */
  $("#gif-resync").addEventListener("click", async () => {
    const r = await post("/api/device/gif-resync");
    toast(r?.ok ? `已重播 ${(r.keys || []).length} 张动图` : "没有正在播的动图", 1600);
  });

  $("#quit").addEventListener("click", async () => {
    if (!confirm("退出后控制台会停止，设备灯光由官方软件或固件默认接管。确定退出？")) return;
    await post("/api/quit");
    document.body.innerHTML =
      '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:#8b98a9;font-size:14px">控制台已退出，可以关闭此窗口。</div>';
  });
}

/* ------------------------------------------------------------------ 相册 */
let albumState = null;
let albumSig = "";

function renderAlbum(a) {
  if (a && state) state.album = a;   // 相册消息是「最新事实」，先写进 state 再判断 body 类
  syncAlbumBody();
  if (a && a.scene) {
    // 相册场景播放中：只更新「按键编辑」里的图片区和键盘预览，待机相册的卡片只同步数字
    if (a.scene === activeAlbumScene()) renderAlbumEditor(a, a.scene);
    if (a.standby) {
      $("#album-count").textContent = String(a.standby.items);
      $("#album-interval").value = a.standby.interval;
      $("#album-interval-val").textContent = `${a.standby.interval} 秒`;
      setSwitch($("#album-slideshow"), a.standby.slideshow);
      const sp = $("#album-preview");
      if (a.standby.items && sp) {
        sp.src = `/api/album/preview.jpg?v=${Date.now()}`;
        sp.hidden = false;
        $("#album-empty").hidden = true;
      }
    }
    const board = document.querySelector(".ring-board");
    if (board) {
      board.classList.add("album-on");
      const url = `url("/api/album/tiles.png?scene=${encodeURIComponent(a.scene)}&v=${Date.now()}")`;
      document.querySelectorAll(".ring-board .cell").forEach((cell) => {
        const n = Number(cell.dataset.key);
        const col = (n - 1) % 5, row = 2 - Math.floor((n - 1) / 5);
        cell.style.setProperty("--album", url);
        cell.style.setProperty("--album-pos", `${col * 25}% ${row * 50}%`);
      });
    }
    return;
  }
  albumState = a;
  const items = a.items || [];
  $("#album-count").textContent = String(items.length);
  const play = $("#album-play");
  play.textContent = a.active ? "■ 停止播放" : "▶ 立即播放";
  play.classList.toggle("on", !!a.active);
  play.disabled = !items.length;
  $("#album-live").hidden = !a.active;
  setSwitch($("#album-slideshow"), a.slideshow);
  $("#album-interval").value = a.interval;
  $("#album-interval-val").textContent = `${a.interval} 秒`;
  document.querySelectorAll("#album-transition [data-tr]").forEach((b) => b.classList.toggle("on", b.dataset.tr === a.transition));
  document.querySelectorAll("#standby-source [data-src]").forEach((b) => b.classList.toggle("on", b.dataset.src === (a.source || "wallpaper")));
  const prev = $("#album-preview");
  const v = state?.view || {};
  const sig = `${items.join("|")}#${a.index}#${v.fit}#${v.zoom}#${state?.standbyBrightness}#${state?.rotation ?? 0}`;
  if (sig !== albumSig) {
    albumSig = sig;
    if (items.length) {
      prev.src = `/api/album/preview.jpg?v=${Date.now()}`;
      prev.hidden = false;
    } else {
      prev.hidden = true;
      prev.removeAttribute("src");
    }
    $("#album-empty").hidden = !!items.length;
    $("#album-strip").innerHTML = items.map((name, i) => `<div class="album-thumb${i === a.index ? " on" : ""}" data-i="${i}" draggable="true" title="单击：显示这张　拖动：调整顺序">
      <img src="/api/album/thumb/${i}.jpg?n=${encodeURIComponent(name)}" alt="" /><button class="album-del" data-del="${i}" title="移除">×</button></div>`).join("");
  } else {
    document.querySelectorAll("#album-strip .album-thumb").forEach((t) => t.classList.toggle("on", Number(t.dataset.i) === a.index));
  }
  // 播放中：控制台键盘上也显示相册画面（和设备一致）
  const board = document.querySelector(".ring-board");
  if (board) {
    board.classList.toggle("album-on", !!a.active);
    const url = a.active ? `url("/api/album/tiles.png?v=${encodeURIComponent(sig)}")` : "";
    document.querySelectorAll(".ring-board .cell").forEach((cell) => {
      const n = Number(cell.dataset.key);
      const col = (n - 1) % 5, row = 2 - Math.floor((n - 1) / 5);
      cell.style.setProperty("--album", url || "none");
      cell.style.setProperty("--album-pos", `${col * 25}% ${row * 50}%`);
    });
  }
}

async function albumAddFiles(files) {
  const list = [...files].filter((f) => /^image\//.test(f.type) || /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
  if (!list.length) return toast("请拖入图片文件", 1600);
  toast(`正在加入 ${list.length} 张图片…`, 1500);
  const ups = [];
  for (const f of list) {
    try { ups.push((await upload(f)).file); } catch (e) { toast(`${f.name}：${e.message}`, 2000); }
  }
  if (!ups.length) return;
  const r = await post("/api/album/add", { files: ups });
  renderAlbum(r);
  toast(r.ok ? `已加入 ${ups.length} 张` : "加入失败", 1400);
}

function bindAlbum() {
  const view = $("#album-view");
  view.addEventListener("click", () => $("#album-file").click());
  $("#album-add").addEventListener("click", () => $("#album-file").click());
  const showTab = (tab) => {
    document.querySelectorAll("#standby-tabs [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    document.querySelectorAll("#standby-card .sb-pane").forEach((p) => { p.hidden = p.dataset.pane !== tab; });
    try { localStorage.setItem("m18.standbyTab", tab); } catch (_) { /* ignore */ }
  };
  document.querySelectorAll("#standby-tabs [data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  try { showTab(localStorage.getItem("m18.standbyTab") || "wallpaper"); } catch (_) { showTab("wallpaper"); }
  window.m18ShowStandbyTab = showTab;
  document.querySelectorAll("#standby-source [data-src]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.src === "album" && !(albumState?.items || []).length) {
      showTab("album");
      return toast("幻灯片还是空的，先添加几张图片", 1800);
    }
    renderAlbum(await post("/api/album/settings", { source: b.dataset.src }));
    toast(b.dataset.src === "album" ? "闲置后播放幻灯片" : "闲置后显示壁纸", 1400);
  }));
  $("#album-file").addEventListener("change", (e) => {
    const files = e.target.files;
    if (files?.length) albumAddFiles(files);
    e.target.value = "";
  });
  const card = $("#album-card");
  // 拖到「幻灯片」页签上也能直接加图
  const tabBtn = document.querySelector('#standby-tabs [data-tab="album"]');
  tabBtn.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { e.preventDefault(); showTab("album"); } });
  card.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    e.stopPropagation();
    view.classList.add("drop-hot");
  });
  card.addEventListener("dragleave", (e) => { if (!card.contains(e.relatedTarget)) view.classList.remove("drop-hot"); });
  card.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    e.stopPropagation();
    view.classList.remove("drop-hot");
    albumAddFiles(e.dataTransfer.files);
  });
  $("#album-play").addEventListener("click", async () => {
    const r = await post(albumState?.active ? "/api/album/stop" : "/api/album/play", {});
    renderAlbum(r);
    if (!r.ok && !albumState?.active) toast("先加几张图片", 1400);
  });
  const strip = $("#album-strip");
  strip.addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      renderAlbum(await post("/api/album/remove", { index: Number(del.dataset.del) }));
      return;
    }
    const t = e.target.closest(".album-thumb");
    if (!t) return;
    const i = Number(t.dataset.i);
    renderAlbum(await post(albumState?.active ? "/api/album/goto" : "/api/album/play", { index: i }));
  });
  let dragFrom = -1;
  strip.addEventListener("dragstart", (e) => {
    const t = e.target.closest(".album-thumb");
    if (!t) return;
    dragFrom = Number(t.dataset.i);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "album");
  });
  strip.addEventListener("dragover", (e) => { if (dragFrom >= 0) { e.preventDefault(); e.stopPropagation(); } });
  strip.addEventListener("drop", async (e) => {
    const t = e.target.closest(".album-thumb");
    if (dragFrom < 0 || !t) return;
    e.preventDefault();
    e.stopPropagation();
    const to = Number(t.dataset.i);
    const from = dragFrom;
    dragFrom = -1;
    if (from !== to) renderAlbum(await post("/api/album/move", { from, to }));
  });
  strip.addEventListener("dragend", () => { dragFrom = -1; });
  $("#album-slideshow").addEventListener("click", async () => {
    renderAlbum(await post("/api/album/settings", { slideshow: !albumState?.slideshow }));
  });
  const pushInterval = throttle((v) => post("/api/album/settings", { interval: v }), 300);
  $("#album-interval").addEventListener("input", (e) => {
    $("#album-interval-val").textContent = `${e.target.value} 秒`;
    if (albumState) albumState.interval = Number(e.target.value);
    pushInterval(Number(e.target.value));
  });
  document.querySelectorAll("#album-transition [data-tr]").forEach((b) => b.addEventListener("click", async () => {
    renderAlbum(await post("/api/album/settings", { transition: b.dataset.tr }));
  }));

}

/* ===== 相册场景：编辑区里的图片集（和「待机画面 → 幻灯片」同一套操作） ===== */
let albumSceneState = null;
let albumSceneSig = "";
let albumSceneLoaded = "";

function activeAlbumScene() {
  const name = selectedScene || state?.album?.scene || state?.activeScene || "";
  const it = (sceneItems || []).find((i) => i.name === name);
  return it && it.sceneType === "album" && !it.builtin ? name : "";
}

/* 相册场景时给 body 挂个类：导航键显示 ◀ ⌂ ▶、翻页按钮收起。
   跟的是「现在设备上放的是不是相册场景」，不是左边列表点了谁，
   这样任何一次状态刷新都不会把类冲掉，切回普通场景也会自动去掉。 */
function syncAlbumBody() {
  const alb = state?.album || {};
  const applied = (alb.active && alb.scene) || (state?.activeScene || "");
  const name = applied || selectedScene || "";
  const it = (sceneItems || []).find((i) => i.name === name);
  document.body.classList.toggle("album-scene", !!(it && it.sceneType === "album" && !it.builtin));
}

function renderAlbumEditor(a, name = activeAlbumScene()) {
  if (!name || !$("#as-name")) return;
  albumSceneState = a;
  const items = a.items || [];
  $("#sel-label").textContent = `相册场景：${name}`;
  $("#as-name").textContent = name;
  $("#as-count").textContent = `${items.length} 张`;
  const del = $("#as-del");
  if (del) del.disabled = !items.length;
  $("#as-live").hidden = !a.active;
  setSwitch($("#as-slideshow"), a.slideshow);
  $("#as-interval").value = a.interval;
  $("#as-interval-val").textContent = `${a.interval} 秒`;
  document.querySelectorAll("#as-transition [data-tr]").forEach((b) => {
    b.classList.toggle("on", b.dataset.tr === a.transition);
  });
  document.querySelectorAll("#as-fit [data-fit]").forEach((b) => {
    b.classList.toggle("on", b.dataset.fit === (a.fit || "cover"));
  });
  const gap = a.gap == null ? 27 : a.gap;
  $("#as-gap").value = gap;
  $("#as-gap-val").textContent = gap;
  const sig = `${name}|${items.join("|")}#${a.index}`;
  if (sig === albumSceneSig) {
    document.querySelectorAll("#as-strip .album-thumb").forEach((t) => {
      t.classList.toggle("on", Number(t.dataset.i) === a.index);
    });
    return;
  }
  albumSceneSig = sig;
  const prev = $("#as-preview");
  if (items.length) {
    prev.src = `/api/album/preview.jpg?scene=${encodeURIComponent(name)}&v=${Date.now()}`;
    prev.hidden = false;
  } else {
    prev.hidden = true;
    prev.removeAttribute("src");
  }
  $("#as-empty").hidden = !!items.length;
  const q = encodeURIComponent(name);
  $("#as-strip").innerHTML = items.map((n, i) => `<div class="album-thumb${i === a.index ? " on" : ""}"`
    + ` data-i="${i}" draggable="true" title="单击：显示这张　拖动：调整顺序">`
    + `<img src="/api/album/thumb/${i}.jpg?scene=${q}&n=${encodeURIComponent(n)}" alt="" />`
    + `<button class="album-del" data-del="${i}" title="移除">×</button></div>`).join("");
}

async function reloadAlbumEditor() {
  const name = activeAlbumScene();
  if (!name) return;
  renderAlbumEditor(await post("/api/album", { scene: name }), name);
}

async function albumSceneAddFiles(files) {
  const name = activeAlbumScene();
  if (!name) return;
  const list = [...files].filter((f) => /^image\//.test(f.type) || /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
  if (!list.length) return toast("请拖入图片文件", 1600);
  toast(`正在加入 ${list.length} 张图片…`, 1500);
  const ups = [];
  for (const f of list) {
    try { ups.push((await upload(f)).file); } catch (e) { toast(`${f.name}：${e.message}`, 2000); }
  }
  if (!ups.length) return;
  const r = await post("/api/album/add", { files: ups, scene: name });
  albumSceneSig = "";
  renderAlbumEditor(r, name);
  toast(r.ok ? `已加入 ${ups.length} 张` : "加入失败", 1400);
  window.m18ReloadScenes?.();
}

function bindAlbumSceneEditor() {
  if (!$("#as-view") || $("#as-view").dataset.bound) return;
  $("#as-view").dataset.bound = "1";
  const view = $("#as-view");
  view.addEventListener("click", () => $("#as-file").click());
  $("#as-add").addEventListener("click", () => $("#as-file").click());
  $("#as-file").addEventListener("change", (e) => {
    if (e.target.files?.length) albumSceneAddFiles(e.target.files);
    e.target.value = "";
  });
  let depth = 0;
  view.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    depth += 1;
    view.classList.add("drop-hot");
  });
  view.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) view.classList.remove("drop-hot");
  });
  view.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    e.stopPropagation();
  });
  view.addEventListener("drop", (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    view.classList.remove("drop-hot");
    albumSceneAddFiles(e.dataTransfer.files);
  });
  $("#as-del").addEventListener("click", async () => {
    const name = activeAlbumScene();
    if (!name) return;
    const a = albumSceneState || {};
    const items = a.items || [];
    if (!items.length) return toast("这个相册场景里还没有图片", 1600);
    const idx = Number(a.index || 0);
    const ok = await confirmDialog(`删除预览框里的这张图片？<br><span class="muted">只从这个相册场景里移除，文件本身还留在上传目录里。</span>`);
    if (!ok) return;
    albumSceneSig = "";
    renderAlbumEditor(await post("/api/album/remove", { index: idx, scene: name }), name);
    window.m18ReloadScenes?.();
    toast("已删除这张图片", 1400);
  });
  const strip = $("#as-strip");
  strip.addEventListener("click", async (e) => {
    const name = activeAlbumScene();
    if (!name) return;
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      albumSceneSig = "";
      renderAlbumEditor(await post("/api/album/remove", { index: Number(del.dataset.del), scene: name }), name);
      window.m18ReloadScenes?.();
      return;
    }
    const t = e.target.closest(".album-thumb");
    if (!t) return;
    const i = Number(t.dataset.i);
    if (albumSceneState?.active) {
      await post("/api/album/goto", { index: i });
      albumSceneSig = "";
      renderAlbumEditor(await post("/api/album", { scene: name }), name);
    } else {
      albumSceneSig = "";
      renderAlbumEditor({ ...(albumSceneState || {}), index: i }, name);
    }
  });
  let from = -1;
  strip.addEventListener("dragstart", (e) => {
    const t = e.target.closest(".album-thumb");
    if (!t) return;
    from = Number(t.dataset.i);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "album-scene");
  });
  strip.addEventListener("dragover", (e) => { if (from >= 0) { e.preventDefault(); e.stopPropagation(); } });
  strip.addEventListener("drop", async (e) => {
    const t = e.target.closest(".album-thumb");
    if (from < 0 || !t) return;
    e.preventDefault();
    e.stopPropagation();
    const to = Number(t.dataset.i);
    const src = from;
    from = -1;
    const name = activeAlbumScene();
    if (!name || src === to) return;
    albumSceneSig = "";
    renderAlbumEditor(await post("/api/album/move", { from: src, to, scene: name }), name);
    window.m18ReloadScenes?.();
  });
  strip.addEventListener("dragend", () => { from = -1; });
  $("#as-slideshow").addEventListener("click", async () => {
    const name = activeAlbumScene();
    if (!name) return;
    renderAlbumEditor(await post("/api/album/settings", { scene: name, slideshow: !albumSceneState?.slideshow }), name);
  });
  const pushInterval = throttle((v) => {
    const name = activeAlbumScene();
    if (name) post("/api/album/settings", { scene: name, interval: v });
  }, 300);
  $("#as-interval").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#as-interval-val").textContent = `${v} 秒`;
    if (albumSceneState) albumSceneState.interval = v;
    pushInterval(v);
  });
  document.querySelectorAll("#as-transition [data-tr]").forEach((b) => b.addEventListener("click", async () => {
    const name = activeAlbumScene();
    if (!name) return;
    renderAlbumEditor(await post("/api/album/settings", { scene: name, transition: b.dataset.tr }), name);
  }));
  document.querySelectorAll("#as-fit [data-fit]").forEach((b) => b.addEventListener("click", async () => {
    const name = activeAlbumScene();
    if (!name) return;
    albumSceneSig = "";
    renderAlbumEditor(await post("/api/album/settings", { scene: name, fit: b.dataset.fit }), name);
  }));
  const pushGap = throttle((v) => {
    const name = activeAlbumScene();
    if (!name) return;
    post("/api/album/settings", { scene: name, gap: v }).then(() => refreshAlbumPreview(name));
  }, 300);
  $("#as-gap").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#as-gap-val").textContent = v;
    if (albumSceneState) albumSceneState.gap = v;
    pushGap(v);
  });
}

function refreshAlbumPreview(name) {
  const prev = $("#as-preview");
  if (!prev || prev.hidden || !name) return;
  prev.src = `/api/album/preview.jpg?scene=${encodeURIComponent(name)}&v=${Date.now()}`;
}


function bindDeselect() {
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !selectedKey) return;
    if (e.target.closest(".cell, .ring-btn, .dot, #key-editor, .sidebar, .modal, .key-menu, .toast, "
      + "button, input, select, textarea, label, a, [contenteditable], [draggable='true']")) return;
    selectKey(null);
  });
}

/* ------------------------------------------------------------------ SSE */
/* 设备那边整组动图重新开始播放了：把界面上同样的动图也从头播，避免越看越错位 */
function restartGifAnim(msg) {
  const t = Math.round((msg && msg.t ? msg.t : Date.now() / 1000) * 1000);
  const bump = (img) => {
    if (!img) return;
    const src = img.getAttribute("src") || "";
    if (!src) return;
    const base = src.split("&g=")[0].split("?g=")[0];
    img.src = `${base}${base.includes("?") ? "&" : "?"}g=${t}`;
  };
  // 控制台预览：事件里列出的键都是正在播动图的，直接给它们换一次地址重新起播
  for (const k of (msg && msg.keys) || []) {
    bump(document.querySelector(`.cell[data-key="${k}"] .cell-img`));
  }
  // 图标库里的动图缩略图（同一次重播，和屏幕上对得上）
  document.querySelectorAll("#icon-groups img").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (/\.gif(\?|$)/i.test(src)) bump(img);
  });
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "frame") {
      paintDots(msg.colors);
    } else if (msg.type === "input") {
      const cell = document.querySelector(`.cell[data-key="${msg.key}"], .ring-btn[data-key="${msg.key}"]`);
      if (cell) {
        if (msg.state === 1) cell.classList.add("pressed");
        else setTimeout(() => cell.classList.remove("pressed"), 120);
      }
      if (msg.state === 1 && state) {
        state.events = [msg, ...(state.events || [])].slice(0, 12);
        renderLog(state.events);
      }
    } else if (msg.type === "nav") {
      toast(`导航：${msg.name}`, 1200);
    } else if (msg.type === "page") {
      refreshState();
    } else if (msg.type === "pluginlog") {
      if (msg.plugin === cpCurrent.id) cpLog(msg.message, /错误|出错|失败/.test(msg.message) ? "err" : "");
    } else if (msg.type === "keyvisual") {
      const p = state?.keyPlugin?.[msg.key];
      if (p) {
        p.rev = msg.rev;
        refreshCells();
        if (selectedKey === msg.key) {
          const ti = $("#sel-thumb .thumb-img");
          if (ti) ti.src = `/api/key/${msg.key}/live.jpg?v=${msg.rev}`;
          else renderEditor();
        }
      } else {
        refreshState();
      }
    } else if (msg.type === "scene") {
      if (!msg.quiet) toast(msg.ok ? `场景：${msg.name}` : "切换场景失败", 1400);
      refreshState();
      window.m18ReloadScenes?.();
    } else if (msg.type === "status") {
      // 息屏 / 唤醒：先把屏幕开关立刻改对，再整体刷新
      if (typeof msg.screenOff === "boolean") {
        if (state) state.screenOff = msg.screenOff;
        setSwitch($("#sleep-btn"), !msg.screenOff);
      }
      refreshState();
    } else if (msg.type === "notice") {
      toast(msg.text || "", msg.ms || 4000);
    } else if (msg.type === "library") {
      libraryItems = msg.items || [];
      renderLibrary();
    } else if (msg.type === "lighting") {
      if (state) state.lighting = { ...(state.lighting || {}), ...(msg.lighting || {}) };
      applyLighting(msg.lighting);
    } else if (msg.type === "gif") {
      restartGifAnim(msg);
    } else if (msg.type === "album") {
      renderAlbum(msg);
      window.m18ReloadScenes?.();
    }
  };
  es.onopen = () => {
    // 断线期间（电脑睡眠 / 服务重启）漏掉的推送：重连上就整份补刷一次
    if (connectEvents.retry) refreshState();
    connectEvents.retry = 0;
  };
  es.onerror = () => {
    es.close();
    connectEvents.retry = (connectEvents.retry || 0) + 1;
    setTimeout(connectEvents, Math.min(10000, 1000 * connectEvents.retry));
  };
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  // 另一个版本已在运行时，后启动的那个会带着提示打开这里
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("notice")) {
      setTimeout(() => toast(q.get("notice"), 9000), 800);
      history.replaceState(null, "", location.pathname);
    }
  } catch (_) { /* ignore */ }
  bindAlbum();
  bindDeselect();
  hardenBrowser();
  buildBoard();
  buildRing();
  bindKeyGestures();
  buildSwatches();
  bindRingPainting();
  bindUI();
  applyRotation();
  try {
    applyState(await (await fetch("/api/state")).json());
  } catch (e) {
    console.error(e);
  }
  if (!location.search.includes("noevents")) connectEvents();
  // ?tab=xxx 是早期多页签留下的：现在只有一个主页面，带了反而会把界面切成空白，忽略
}

boot();
