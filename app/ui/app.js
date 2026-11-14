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
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json().catch(() => ({}));
}

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
  const noMouseButton = (e) => { if (e.button !== 0) e.preventDefault(); };

  document.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".ring-board .cell, .ring-btn, .key-menu, #sel-thumb, .lib-menu")) return;
    e.preventDefault();
  }, { capture: true });
  document.addEventListener("selectstart", noDefault, { capture: true });
  document.addEventListener("dragstart", (e) => {
    // 只允许显式标记 draggable 的元素拖动（图标库瓦片）
    if (!e.target.closest("[draggable='true']")) e.preventDefault();
  }, { capture: true });
  document.addEventListener("auxclick", noDefault, { capture: true });
  ["pointerdown", "pointerup", "mousedown", "mouseup"].forEach((type) => {
    document.addEventListener(type, noMouseButton, { capture: true });
  });
  // 右键按住拖动：不给浏览器留任何「拖动 / 选区 / 自动滚动」的机会
  let rightHeld = false;
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 2) return;
    rightHeld = true;
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
    if (imgEl) {
      const plug = state?.keyPlugin?.[n];
      if (plug && plug.rev > 0) {
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
        const rev = `${n}-${state?.keyImageRev?.[n] || imgs[n]}`;
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
      label.innerHTML = splitLabel(st.text, st.size ?? 20).join("<br>");
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
const packIconUrl = (pack, file) =>
  `/api/iconpack/${encodeURIComponent(pack)}/${encodeURIComponent(file)}`;
const pluginIconUrl = (id, idx) => `/api/plugin-icon/${encodeURIComponent(id)}/${idx}`;

function iconTile(pack, file) {
  const label = file.replace(/\.[^.]+$/, "");
  return `<img loading="lazy" draggable="false" data-name="pack:${escHtml(pack)}/${escHtml(file)}"`
    + ` src="${packIconUrl(pack, file)}" title="${escHtml(label)}" alt="" />`;
}

function fillPackTiles(details) {
  const tiles = details.querySelector(".tiles");
  if (!tiles || tiles.dataset.filled) return;
  const pack = iconPacks.find((p) => p.id === details.dataset.pack);
  if (!pack) return;
  tiles.innerHTML = pack.files.map((f) => iconTile(pack.id, f)).join("");
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
  box.innerHTML = iconPacks.map((p) => {
    const open = sideState.openPacks.includes(p.id);
    const cover = p.files[0] ? `<img class="cover" src="${packIconUrl(p.id, p.files[0])}" alt="" />` : "";
    return `<details class="icon-group" data-pack="${escHtml(p.id)}"${open ? " open" : ""}>`
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

/* 图标瓦片 / 插件动作图标：单击 = 应用到选中键，拖动 = 放到目标键（事件委托，几千个图标只绑一次） */
function bindIconDrag(container) {
  container.addEventListener("pointerdown", (e) => {
    const img = e.target.closest("img[data-name]");
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
      const t = dropTargetAt(ev.clientX, ev.clientY);
      if (t && !t.closest(".side-scroll")) t.classList.add("drop-hover");
    };
    const onUp = async (ev) => {
      document.removeEventListener("pointermove", onMove, true);
      document.removeEventListener("pointerup", onUp, true);
      endGhost();
      clearDropHover();
      let key = 0;
      if (dragging) {
        const t = dropTargetAt(ev.clientX, ev.clientY);
        if (t?.classList.contains("cp-icon")) {
          setCpIcon(img.dataset.name, img.src);
          toast("已设为自制插件的图标（保存后生效）", 1400);
          return;
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
        const r = m ? await post("/api/key/plugin", { key, plugin: m[1], index: Number(m[2]) }) : { ok: false };
        const name = img.closest(".pl-act")?.querySelector("span")?.textContent || "插件动作";
        if (!r.ok) {
          toast(r.error || "绑定失败", 2600);
          return;
        }
        await refreshState();
        if (r.native) {
          // 官方内置功能换成了控制台原生动作；要填值的直接选中这个键去编辑
          if (r.needsInput) {
            selectKey(key);
            $("#key-editor")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
          toast(r.needsInput ? `「${name}」已换成控制台的「${r.label}」动作，请在下方补充内容后点「保存」`
            : `按键 ${key}：${r.label}`, r.needsInput ? 3600 : 1800);
        } else {
          toast(`按键 ${key} 已绑定「${name}」，按下由插件处理`, 2200);
        }
        return;
      }
      const ok = await post("/api/key/icon", { key, name: img.dataset.name });
      toast(ok.ok ? `图标已应用到按键 ${key}` : "应用失败", 1300);
      if (ok.ok) await refreshState();
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
  $("#cp-agent").addEventListener("click", () => agentDialog().catch((e) => toast(`打开失败：${e.message || e}`)));
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
  $("#cp-new").addEventListener("click", () => openCustom(null));
  $("#custom-new-side").addEventListener("click", () => {
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
  frame.addEventListener("load", () => {
    if (frame.dataset.registered) return;
    const w = frame.contentWindow;
    const fn = w && (w.connectElgatoStreamDeckSocket || w.connectSocket);
    if (typeof fn !== "function") {
      toast("这个插件的设置页不兼容", 1800);
      return;
    }
    try {
      fn.call(w, r.port, r.uuid, "registerPropertyInspector", r.info, r.actionInfo);
      frame.dataset.registered = "1";
    } catch (e) {
      toast(`插件设置页出错：${e}`, 2200);
    }
  });
  frame.src = r.url;
  const close = async () => {
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

/* 接入 AI：把 MCP 配置给 Codex / DeepSeek Harness / Claude / Cursor 等智能体 */
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
      <div class="confirm-actions choice-actions">
        ${choices.map((c) => `<button class="seg${c.primary ? " on" : ""}" data-v="${c.value}">${c.label}</button>`).join("")}
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
  show("#sel-window", t === "window");
  show("#sel-record", t === "hotkey" || t === "toggle");
  show("#sel-pick", t === "launch" || t === "sound");
  const fv = selectedKey ? gestureView(selectedKey, editGesture) : null;
  show("#sel-open-folder", t === "folder" && fv?.type === "folder");
  show("#row-macro", t === "macro");
  show("#row-text-opts", t === "text");
  show("#editor-actions-row", ["hotkey", "toggle", "media", "nav", "mouse", "scene", "launch", "sound", "window", "folder"].includes(t));
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
      else if (img) parts.push(`<img class="thumb-img" src="/api/key/${n}/raw?v=${Date.now()}" alt="" />`);
      const label = plugLive ? "" : (st.text || (img ? "" : String(n)));
      if (label) {
        const tw = thumb.clientWidth || 96;
        const fs = Math.max(6, (st.size ?? 20) * (tw / 64));
        const align = st.pos === "top" ? "flex-start" : (st.pos === "bottom" ? "flex-end" : "center");
        parts.push(`<span class="thumb-label" style="color:${st.color || "#e8eefc"};`
          + `opacity:${(st.opacity ?? 100) / 100};font-size:${fs}px;line-height:1.15;`
          + `padding:${Math.max(1, (3 * tw) / 64)}px;align-items:${align}">`
          + `${splitLabel(label, st.size ?? 20).join("<br>")}</span>`);
      }
      thumb.innerHTML = parts.join("");
    }
  }
  $("#sel-text").value = st.text || "";
  $("#sel-fg").value = st.color || "#e8eefc";
  $("#sel-bg").value = st.bg || "#000000";
  $("#sel-opacity").value = st.opacity ?? 100;
  $("#sel-opacity-val").textContent = `${st.opacity ?? 100}%`;
  $("#sel-size").value = st.size ?? 20;
  $("#sel-size-val").textContent = `${st.size ?? 20}`;
  $("#sel-pos").value = st.pos || "center";

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
  $("#sel-image").textContent = isNav ? "导航键无屏幕" : (img ? "单独图片 / 动图" : "无（标题 / 编号）");

  const actType = fillActionForm(n);
  const curDraft = formDrafts[editGesture];
  if (editGesture !== "press") {
    const shown = curDraft ? { label: draftLabel(curDraft) } : gestureView(n, editGesture);
    $("#sel-action").textContent = `${gestureLabel(editGesture)}：${shown?.label || "未设置"}${curDraft ? "（未保存）" : ""}`;
  } else if (curDraft) {
    $("#sel-action").textContent = "短按：未保存的改动";
  }

  let hint = editGesture !== "press"
    ? `${gestureLabel(editGesture)}（${editGesture === "long" ? "按住 0.5 秒" : "0.3 秒内按两下"}）时执行。设置后，短按会在松开${editGesture === "double" ? "约 0.3 秒" : ""}后才触发。`
    : actType === "plugin"
    ? "插件动作：按下由插件处理，画面也由插件更新；点「插件设置」配置。改成别的类型即解除绑定。"
    : isNav
      ? "导航键默认：左=上一页，中=主页，右=下一页；也可以改成打开程序、网页或热键。"
      : "拖图片换画面；拖程序 / 快捷方式 / 脚本 / 文件夹绑定动作，按下即打开。";
  if (Object.keys(formDrafts).length) hint = "有未保存的改动 —— 三种触发方式改完一起点右上角「保存」生效。" + hint;
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

async function handleFile(key, file) {
  const isImage = /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name);
  if (isImage) {
    const up = await upload(file);
    const r = await post("/api/key/image", { key, file: up.file });
    if (!r.ok) toast("图片设置失败");
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
      await handleFile(key, file);
    } else if (dt.files?.[0]) {
      await handleFile(key, dt.files[0]);
    }
  } catch (err) {
    toast("拖入失败：" + err.message);
  }
  await refreshState();
}

/* ------------------------------------------------------------------ 状态 */
async function refreshState() {
  applyState(await (await fetch("/api/state")).json());
}

function applyMode(mode) {
  document.querySelectorAll("#modes .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.mode === mode);
  });
  $("#mode-label").textContent = MODE_NAMES[mode] || mode;
  $("#mode-desc").textContent = MODE_DESC[mode] || "";
  const clr = $("#clear-led");
  if (clr) clr.hidden = mode !== "custom";
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

async function refreshState() {
  try {
    applyState(await fetchState());
  } catch (err) {
    console.error("刷新状态失败", err);
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
  currentColor = L.color || currentColor;
  ledBrightness = L.brightness ?? 100;
  if (s.frame) paintDots(s.frame);
  $("#color").value = currentColor;
  $("#color-hex").textContent = currentColor.toUpperCase();
  syncSwatch();
  $("#led-brightness").value = ledBrightness;
  $("#led-brightness-val").textContent = ledBrightness;
  $("#speed").value = L.speed;
  $("#speed-val").textContent = Number(L.speed).toFixed(1) + "×";
  applyMode(L.mode);
  if (Array.isArray(L.custom) && L.custom.length === 24) {
    localCustom = L.custom.map((c) => [...c]);
  }

  $("#screen-brightness").value = s.screenBrightness;
  $("#screen-brightness-val").textContent = s.screenBrightness;

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
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

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

function fmtSec(v) {
  const m = Math.floor(v / 60), sec = v % 60;
  if (!m) return `${sec} 秒`;
  return sec ? `${m} 分 ${sec} 秒` : `${m} 分钟`;
}

function updateGapLabel(gap) {
  $("#wallpaper-gap-val").textContent = gap;
  const pct = (gap / 64) * 100;
  const ratioEl = $("#gap-ratio");
  if (ratioEl) {
    ratioEl.textContent = (pct % 1 === 0 ? pct.toFixed(0) : pct.toFixed(1)) + "%";
  }
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
function activateTab(name) {
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("active", p.id === "tab-" + name);
  });
  const btn = document.querySelector(`.nav-item[data-tab="${name}"]`);
  if (btn) $("#page-title").textContent = btn.textContent.trim();
  applyRotation();
}

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

  $("#screen-brightness").addEventListener("input", (e) => {
    $("#screen-brightness-val").textContent = e.target.value;
    throttle((v) => post("/api/screen-brightness", { value: v }), 120)(Number(e.target.value));
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
  bindView("#wp-ox", "ox", (v) => String(v));

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
      const oxEl = $("#wp-ox");
      const oyEl = $("#wp-oy");
      if (oxEl) { oxEl.value = ox; $("#wp-ox-val").textContent = ox; }
      if (oyEl) { oyEl.value = oy; $("#wp-oy-val").textContent = oy; }
      pushView({ ox, oy });
    };
    wpHolder.addEventListener("pointerup", finishDrag);
    wpHolder.addEventListener("pointercancel", finishDrag);
  }
  bindView("#wp-oy", "oy", (v) => String(v));

  document.querySelectorAll(".fit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".fit-btn").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      // 换填充方式时把偏移归零，避免上一张图的位移带过来导致画面歪掉
      const oxEl = $("#wp-ox");
      const oyEl = $("#wp-oy");
      if (oxEl) { oxEl.value = 0; $("#wp-ox-val").textContent = "0"; }
      if (oyEl) { oyEl.value = 0; $("#wp-oy-val").textContent = "0"; }
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
    toast(r.value ? "待机壁纸已启用" : "待机壁纸已停用", 1400);
  });

  const goPage = async (delta) => {
    applyState(await post("/api/page", { delta }));
    toast(`第 ${(state?.page ?? 0) + 1} 页`, 900);
  };
  $("#page-prev").addEventListener("click", () => goPage(-1));
  $("#page-next").addEventListener("click", () => goPage(1));
  $("#page-add").addEventListener("click", async () => {
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
    await post("/api/key/clear", { key: selectedKey });
    toast("已清除该按键的图片", 1200);
    await refreshState();
  });

  $("#key-file").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f || !selectedKey) return;
    try {
      const up = await upload(f);
      const r = await post("/api/key/image", { key: selectedKey, file: up.file });
      if (!r.ok) toast("图片设置失败");
      await refreshState();
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
      if (selectedKey) formDrafts[editGesture] = readFormDraft();
      editGesture = b.dataset.g;
      renderEditor();
    });
  });
  // 表单里任何改动都记进草稿：这样服务端推送刷新画面时，正在填的内容不会被冲掉
  ["#sel-value", "#sel-macro", "#sel-media", "#sel-mouse", "#sel-window", "#sel-scene", "#sel-enter", "#sel-secret", "#sel-nav"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const snap = () => { if (selectedKey) formDrafts[editGesture] = readFormDraft(); };
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
    const n = selectedKey;
    // 当前页签的内容也进草稿，这样三种触发方式可以一起提交
    formDrafts[editGesture] = readFormDraft();
    const order = ["press", "long", "double"];
    const todo = order.filter((g) => formDrafts[g]);
    if (!todo.length) return;

    // 文件夹被替换：先问一次
    for (const g of todo) {
      const d = formDrafts[g];
      const before = gestureView(n, g);
      if (before?.type === "folder" && d.type !== "folder"
        && !(await confirmDialog(`按键 ${n} 的${gestureLabel(g)}原来是文件夹「${before.value}」，替换后文件夹和里面的按键会一起删除。继续？`))) return;
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

  const pushStyle = throttle(async () => {
    if (!selectedKey) return;
    await post("/api/key/style", {
      key: selectedKey,
      text: $("#sel-text").value,
      color: $("#sel-fg").value,
      bg: $("#sel-bg").value,
      opacity: Number($("#sel-opacity").value),
      size: Number($("#sel-size").value),
      pos: $("#sel-pos").value,
    });
    await refreshState();
  }, 320);
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
    const mode = await choiceDialog(
      "从官方 Stream Dock 驱动重新抓取：<br>· 图标包、插件：总是重新读取<br>· 场景：选择处理方式",
      [{ value: "new", label: "只添加新场景（保留已改动）", primary: true },
       { value: "all", label: "全部重新导入（覆盖已导入的官方场景）" }]);
    if (!mode) return;
    if (mode === "all" && !(await confirmDialog("已导入的官方场景里你改过的按键会被覆盖，继续？"))) return;
    const withPlugins = await confirmDialog(
      "同时把官方插件（含 Node 运行环境，约 500 MB）复制到本地？<br>"
      + "<span class='muted'>复制后即使卸载官方 Stream Dock 软件，插件也能继续使用。图标包总是会备份。</span>");
    toast(withPlugins ? "正在同步并备份插件，可能要一两分钟…" : "正在同步官方资源…", 2500);
    const r = await post("/api/official/sync", { mode, plugins: withPlugins });
    iconPacks = null;
    pluginItems = null;
    await loadIcons(true);
    await loadPlugins(true);
    await window.m18ReloadScenes?.();
    await refreshState();
    const n = (r.scenes || []).length;
    toast(`同步完成：${mode === "all" ? "重导" : "新增"}场景 ${n} 个 · 图标包 ${r.packs ?? "-"} 个（${r.icons ?? "-"} 个图标）`
      + `${withPlugins ? ` · 已备份插件 ${r.plugins ?? 0} 个` : ""}`, 4200);
  });
  let searchTimer = null;
  $("#icon-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { loadIcons(); loadPlugins(); }, 160);
  });
  bindSidebar();
  bindCustomLab();
  loadIcons();
  loadPlugins();

  let selectedScene = null;
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
        if (!(await confirmDialog(`删除场景「${name}」？`))) return;
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
    box.innerHTML = items.length
      ? items.map((it) => {
        const name = typeof it === "string" ? it : it.name;
        const active = typeof it === "string" ? false : it.active;
        const custom = typeof it === "string" ? false : it.custom;
        const isOfficial = typeof it === "string" ? false : it.official;
        const picked = selectedScene === name;
        const album = !!it.album;
        return `<div class="row scene-row${active ? " active" : ""}${custom ? " custom" : ""}${isOfficial ? " official" : ""}`
          + `${album ? " album" : ""}${it.muted ? " muted" : ""}${picked ? " picked" : ""}"`
          + ` data-name="${escHtml(name)}" data-custom="${custom ? 1 : 0}" data-album="${album ? 1 : 0}">`
          + `<b title="${album ? "整屏相册幻灯片（不可删除）" : isOfficial ? "来自官方驱动的场景" : ""}">${escHtml(name)}${album ? `<em>${it.count || 0}</em>` : ""}</b>`
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
        if (row.querySelector(".scene-rename")) return;      // 正在改名
        if (row.dataset.album === "1" && !row.classList.contains("active")) {
          const r = await post("/api/scenes", { apply: name });
          if (!r.ok) {
            window.m18ShowStandbyTab?.("album");
            document.querySelector("#standby-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
            return toast("相册是空的，先在右侧「待机画面 → 相册」里加几张图片", 2400);
          }
          renderScenes(r.items || []);
          toast("相册播放中：◀ ▶ 翻照片，按其他键回到原场景", 2200);
          return;
        }
        selectedScene = name;
        if (row.classList.contains("active") || row.classList.contains("switching")) return;
        box.querySelectorAll(".scene-row.switching").forEach((r) => r.classList.remove("switching"));
        row.classList.add("switching");
        const r = await post("/api/scenes", { apply: name });
        if (!r.ok) {
          row.classList.remove("switching");
          toast(r.reason || "切换失败", 2800);
          return;
        }
        toast(`已切换到「${name}」`, 1200);
        await refreshState();
        await reloadScenes();
      });
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showSceneMenu(e.clientX, e.clientY, name, row.dataset.custom === "1", row.dataset.album === "1",
          row.classList.contains("muted"));
      });
    });
  };
  const reloadScenes = async () => {
    const r = await post("/api/scenes", {});
    renderScenes(r.items || []);
    sceneNames = (r.items || []).map((i) => i.name);
    const c = $("#scene-count");
    if (c) c.textContent = String((r.items || []).length);
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
    const name = window.prompt("新场景名称", `场景 ${new Date().toLocaleDateString()}`);
    if (!name) return;
    const r = await post("/api/scenes", { save: name });
    toast(r.ok ? `已新建「${name}」（单击即可切换过去）` : "新建失败", 2200);
    selectedScene = name;
    renderScenes(r.items || []);
  });
  $("#scene-del-btn").addEventListener("click", async () => {
    const name = selectedScene;
    if (!name) {
      toast("先在列表里选中一个场景", 1600);
      return;
    }
    const items = (await post("/api/scenes", {})).items || [];
    const target = items.find((i) => i.name === name);
    if (target?.custom || target?.album) {
      toast(target.album ? "相册场景不能删除" : "自定义场景不能删除", 1600);
      return;
    }
    if (!(await confirmDialog(`删除场景「${name}」？`))) return;
    const r = await post("/api/scenes", { delete: name });
    selectedScene = null;
    renderScenes(r.items || []);
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

  // 切到控制台窗口不会唤醒待机（只有设备按键或显式操作才会）
  $("#sel-clear-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    if (!(await confirmDialog(`清除按键 ${selectedKey} 的图片、动作和样式？`))) return;
    await post("/api/key/clear", { key: selectedKey });
    await post("/api/key/action/clear", { key: selectedKey });
    await post("/api/key/action/clear", { key: selectedKey, gesture: "long" });
    await post("/api/key/action/clear", { key: selectedKey, gesture: "double" });
    await post("/api/key/style", { key: selectedKey, text: "",
      color: "#e8eefc", bg: "#000000", opacity: 100, size: 20, pos: "center" });
    await refreshState();
  });

  $("#reconnect").addEventListener("click", async () => {
    applyState(await post("/api/device/reconnect"));
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
      return toast("相册还是空的，先添加几张图片", 1800);
    }
    renderAlbum(await post("/api/album/settings", { source: b.dataset.src }));
    toast(b.dataset.src === "album" ? "闲置后播放相册幻灯片" : "闲置后显示壁纸", 1400);
  }));
  $("#album-file").addEventListener("change", (e) => {
    const files = e.target.files;
    if (files?.length) albumAddFiles(files);
    e.target.value = "";
  });
  const card = $("#album-card");
  // 拖到「相册」页签上也能直接加图
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

/* 点空白处取消按键选中（点按钮、输入框、编辑区、侧栏等不算） */
function bindDeselect() {
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !selectedKey) return;
    if (e.target.closest(".cell, .ring-btn, .dot, #key-editor, .sidebar, .modal, .key-menu, .toast, "
      + "button, input, select, textarea, label, a, [contenteditable], [draggable='true']")) return;
    selectKey(null);
  });
}

/* ------------------------------------------------------------------ SSE */
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
      refreshState();
    } else if (msg.type === "album") {
      renderAlbum(msg);
      window.m18ReloadScenes?.();
    }
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectEvents, 1500);
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
  window.addEventListener("resize", applyRotation);
  applyRotation();
  try {
    applyState(await (await fetch("/api/state")).json());
  } catch (e) {
    console.error(e);
  }
  if (!location.search.includes("noevents")) connectEvents();
  const tab = new URLSearchParams(location.search).get("tab");
  if (tab) activateTab(tab);
}

boot();
