const $ = (sel, root = document) => root.querySelector(sel);

const MODE_NAMES = {
  solid: "常亮", rainbow: "彩虹", breathe: "呼吸",
  chase: "流光", gradient: "渐变", custom: "自定义", off: "关闭",
};

const MODE_DESC = {
  solid: "整圈灯珠显示同一颜色，颜色取自下方色板。",
  rainbow: "色相沿灯圈循环流动，速度由「速度」控制。",
  breathe: "当前颜色整体明暗呼吸。",
  chase: "单点亮光沿灯圈追逐移动。",
  gradient: "从当前颜色过渡到它的补色。",
  custom: "在灯圈上按住拖动逐颗上色，再涂一次覆盖。",
  off: "关闭灯带。",
};

const SWATCHES = [
  "#ff3b30", "#ff9500", "#ffcc00", "#34c759",
  "#00c7be", "#00aaff", "#5e5ce6", "#af52de",
  "#ff2d55", "#ffffff", "#8e8e93", "#0a84ff",
];

const NAV_PRESETS = [
  "上一页", "下一页", "主页",
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
    if (e.target.closest(".ring-board .cell, .ring-btn, .icon-group img, .key-menu")) return;
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
  document.addEventListener("wheel", (e) => {
    if (e.ctrlKey) e.preventDefault();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) e.preventDefault();
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
  const W = 5 * 64 + 4 * gap;
  const H = 3 * 64 + 2 * gap;
  let html = "";
  for (let r = 0; r < 3; r += 1) {
    for (let c = 0; c < 5; c += 1) {
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
      if (hasImg) {
        // 用原始文件（GIF 会实时播放），和控制台"所见即所得"
        if (imgEl.dataset.v !== `${n}-${hasImg}`) {
          imgEl.src = `/api/key/${n}/raw?v=${stamp}`;
          imgEl.dataset.v = `${n}-${hasImg}`;
        }
        imgEl.hidden = false;
      } else {
        imgEl.hidden = true;
        imgEl.removeAttribute("src");
        imgEl.dataset.v = "";
      }
    }
    cell.style.backgroundImage = "";
    cell.style.backgroundColor = hasImg ? "" : (st.bg || "#000");
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
      label.hidden = !st.text;
    }
    const badge = $(".badge", cell);
    if (badge) badge.hidden = !hasImg;
    const actEl = $(".cell-act", cell);
    if (actEl) {
      actEl.hidden = !act;
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
async function loadIcons() {
  const box = $("#icon-groups");
  if (!box) return;
  let items = [];
  try {
    items = (await post("/api/icons", {})).items || [];
  } catch (_) {
    items = [];
  }
  const cnt = $("#icon-count");
  if (cnt) cnt.textContent = String(items.length);
  const q = ($("#icon-search")?.value || "").toLowerCase().trim();
  const groups = new Map();
  items.forEach((name) => {
    if (q && !name.toLowerCase().includes(q)) return;
    const sep = name.indexOf("__");
    const group = sep > 0 ? name.slice(0, sep) : "通用";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(name);
  });
  const stamp = Date.now();
  let html = "";
  for (const [group, names] of groups) {
    const label = group.replace(/^com[._]/, "").replace(/[._]/g, " ");
    html += `<details class="icon-group" open><summary>${label}<em>${names.length}</em></summary><div class="tiles">`;
    html += names.map((n) =>
      `<img data-name="${n}" draggable="true" src="/api/icons/${encodeURIComponent(n)}?v=${stamp}" title="${n}" alt="" />`,
    ).join("");
    html += "</div></details>";
  }
  box.innerHTML = html || `<div class="muted" style="font-size:12px">没有图标，点「同步」从官方驱动抓取</div>`;
  box.querySelectorAll("img").forEach((img) => {
    img.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const start = { x: e.clientX, y: e.clientY };
      let dragging = false;
      const onMove = (ev) => {
        const dx = ev.clientX - start.x;
        const dy = ev.clientY - start.y;
        if (!dragging && Math.hypot(dx, dy) > 6) {
          dragging = true;
          startGhost(img, ev.clientX, ev.clientY);
        }
        if (!dragging) return;
        moveGhost(ev.clientX, ev.clientY);
        clearDropHover();
        const t = dropTargetAt(ev.clientX, ev.clientY);
        if (t && !t.classList.contains("icon-group")) t.classList.add("drop-hover");
      };
      const onUp = async (ev) => {
        document.removeEventListener("pointermove", onMove, true);
        document.removeEventListener("pointerup", onUp, true);
        endGhost();
        clearDropHover();
        const t = dropTargetAt(ev.clientX, ev.clientY);
        let key = 0;
        if (t?.dataset?.key) key = Number(t.dataset.key);
        else if (t?.classList.contains("thumb")) key = selectedKey;
        if (!dragging) {
          // 单击：应用到当前选中键
          if (!selectedKey) {
            toast("先选一个按键，或直接把图标拖到按键上", 1600);
            return;
          }
          key = selectedKey;
        }
        if (!key) return;
        const ok = await post("/api/key/icon", { key, name: img.dataset.name });
        toast(ok.ok ? `图标已应用到按键 ${key}` : "应用失败", 1300);
        if (ok.ok) await refreshState();
      };
      document.addEventListener("pointermove", onMove, true);
      document.addEventListener("pointerup", onUp, true);
    });
  });
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
  return el ? el.closest(".cell, .ring-btn, .thumb, .icon-group img") : null;
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

function selectKey(n) {
  selectedKey = n;
  refreshCells();
  renderEditor();
  // 设备上对应按键亮起内边缘；同时在控制台里操作也算"活跃"
  post("/api/key/select", { key: n || 0 }).catch(() => {});
  post("/api/activity", { wake: true }).catch(() => {});
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
      if (img) parts.push(`<img class="thumb-img" src="/api/key/${n}/raw?v=${Date.now()}" alt="" />`);
      const label = st.text || (img ? "" : String(n));
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

  $("#sel-action").textContent = act ? (type === "nav" ? `导航：${act}` : `打开：${act}`) : "未绑定";
  $("#sel-image").textContent = isNav ? "导航键无屏幕" : (img ? "单独图片 / 动图" : "无（标题 / 编号）");

  const nav = $("#sel-nav");
  const typeSel = $("#sel-type");
  const valueInput = $("#sel-value");
  const mediaSel = $("#sel-media");
  const actType = act ? (state?.keyActionTypes?.[n] || "launch") : "none";
  const actVal = state?.keyActionValues?.[n] || "";

  typeSel.value = actType;
  valueInput.value = actType === "nav" || actType === "media" ? "" : actVal;
  valueInput.disabled = actType === "nav" || actType === "none" || actType === "media"
    || actType === "macro";
  nav.hidden = actType !== "nav";
  if (mediaSel) {
    mediaSel.hidden = actType !== "media";
    if (actType === "media" && actVal) mediaSel.value = actVal;
  }
  const macroRow = $("#row-macro");
  if (macroRow) {
    macroRow.hidden = actType !== "macro";
    if (actType === "macro") $("#sel-macro").value = actVal;
  }
  const recBtn = $("#sel-record");
  if (recBtn) {
    recBtn.hidden = actType !== "hotkey";
    if (recBtn.hidden) {
      recBtn.classList.remove("on");
    }
  }
  const actionsRow = $("#editor-actions-row");
  if (actionsRow) {
    actionsRow.hidden = !["hotkey", "media", "nav"].includes(actType);
  }
  const valueRow = $("#row-value");
  if (valueRow) {
    valueRow.hidden = !["launch", "url", "hotkey"].includes(actType);
  }

  if (actType === "nav") {
    const jumps = Array.from({ length: state?.pageCount || 1 }, (_, i) => `跳到第 ${i + 1} 页`);
    const opts = [...NAV_PRESETS.slice(0, 3), ...jumps, ...NAV_PRESETS.slice(3)];
    nav.innerHTML = opts
      .map((p) => `<option value="${p}"${act === p ? " selected" : ""}>${p}</option>`)
      .join("");
  }

  $("#sel-hint").textContent = isNav
    ? "导航键默认：左=上一页，中=主页，右=下一页；也可以改成打开程序、网页或热键。"
    : "拖图片换画面；拖程序 / 快捷方式 / 脚本 / 文件夹绑定动作，按下即打开。";
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
}

function applyRotation() {
  const stage = $("#ring-stage");
  const wrap = $("#ring-wrap");
  if (!stage || !wrap) return;
  const rot = (state?.rotation ?? 0) % 360;
  stage.style.aspectRatio = "1.35";          // 尺寸固定，不随旋转变化
  wrap.style.width = "100%";
  const stageW = stage.clientWidth, stageH = stage.clientHeight;
  const wrapW = wrap.offsetWidth, wrapH = wrap.offsetHeight;
  if (!stageW || !stageH || !wrapW || !wrapH) return;
  const rad = (rot * Math.PI) / 180;
  const bboxW = Math.abs(wrapW * Math.cos(rad)) + Math.abs(wrapH * Math.sin(rad));
  const bboxH = Math.abs(wrapW * Math.sin(rad)) + Math.abs(wrapH * Math.cos(rad));
  const scale = Math.min(stageW / bboxW, stageH / bboxH, 1);
  wrap.style.transform = `translate(-50%, -50%) rotate(${rot}deg) scale(${scale})`;
  document.documentElement.style.setProperty("--rot", `${rot}deg`);
  const names = { 0: "正向", 90: "顺时针 90°", 180: "倒置 180°", 270: "逆时针 90°" };
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
  const standby = s.standby ?? 60;
  $("#standby-sec").value = standby;
  $("#standby-sec-val").textContent = fmtSec(standby);
  $("#reverse-led").classList.toggle("on", !!s.effectReverse);
  $("#mute-btn").classList.toggle("on", !!s.inputMuted);
  $("#repaint-btn").classList.toggle("on", !!s.repaintOnPress);
  $("#awake-btn").classList.toggle("on", !!s.keepAwake);
  $("#autostart-btn").classList.toggle("on", !!s.autostart);
  const wpBtn = $("#wallpaper-toggle");
  if (wpBtn) {
    const on = s.wallpaper?.enabled !== false;
    wpBtn.classList.toggle("on", on);
    wpBtn.setAttribute("aria-checked", on ? "true" : "false");
  }
  const sleepBtn = $("#sleep-btn");
  if (sleepBtn) {
    sleepBtn.classList.toggle("on", !s.screenOff);
    const lab = $("#screen-label");
    if (lab) lab.textContent = s.screenOff ? "灭" : "亮";
  }

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
  const page = (s.page ?? 0) + 1;
  const total = s.pageCount ?? 1;
  const names = s.pageNames || [];
  const nm = names[(s.page ?? 0)] || `第 ${page} 页`;
  $("#page-label").textContent = `${nm}（${page}/${total}）`;
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
  return `${v}s`;
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

  $("#reset-led").addEventListener("click", () => post("/api/lighting/reset"));
  $("#clear-led").addEventListener("click", () => fillAll([0, 0, 0]));
  $("#wake-btn").addEventListener("click", () => post("/api/device/wake"));

  $("#rotate-btn").addEventListener("click", async () => {
    const next = ((state?.rotation ?? 0) + 90) % 360;
    const r = await post("/api/rotation", { value: next });
    if (state) state.rotation = r.rotation;
    applyRotation();
    refreshBoard();
  });

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
    $("#reverse-led").classList.toggle("on", !!r.value);
    toast(r.value ? "灯效方向：反向" : "灯效方向：正向", 1200);
  });

  $("#sleep-btn").addEventListener("click", async () => {
    const off = !(state?.screenOff);
    if (state) state.screenOff = off;
    const btn = $("#sleep-btn");
    btn.classList.toggle("on", !off);
    const lab = $("#screen-label");
    if (lab) lab.textContent = off ? "灭" : "亮";
    const r = await post(off ? "/api/device/sleep" : "/api/device/wake");
    if (state) state.screenOff = r.screenOff ?? off;
    btn.classList.toggle("on", !(state?.screenOff));
    if (lab) lab.textContent = state?.screenOff ? "灭" : "亮";
    toast(off ? "已息屏" : "已亮屏", 1200);
  });

  const updateScreenButton = () => {
    const off = !!state?.screenOff;
    const btn = $("#sleep-btn");
    if (btn) {
      btn.textContent = off ? "亮屏" : "息屏";
      btn.classList.toggle("on", off);
    }
  };

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

  $("#page-del").addEventListener("click", async () => {
    if ((state?.pageCount ?? 1) <= 1) {
      toast("至少保留一页", 1400);
      return;
    }
    if (!(await confirmDialog("删除当前页？页面上的按键图片和动作会一起删除。"))) return;
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
    $("#mute-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "已屏蔽按键" : "按键已恢复", 1400);
  });

  $("#repaint-btn").addEventListener("click", async () => {
    const v = !(state?.repaintOnPress);
    const r = await post("/api/device/repaint-on-press", { value: v });
    if (state) state.repaintOnPress = r.value;
    $("#repaint-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "按键时会补画画面" : "按键时不再重绘", 1400);
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

  $("#sel-nav").addEventListener("change", async (e) => {
    if (!selectedKey) return;
    await post("/api/key/action", { key: selectedKey, type: "nav", name: e.target.value });
    await refreshState();
  });

  $("#sel-type").addEventListener("change", () => {
    const t = $("#sel-type").value;
    $("#sel-value").disabled = t === "nav" || t === "none" || t === "media" || t === "macro";
    $("#sel-nav").hidden = t !== "nav";
    const media = $("#sel-media");
    if (media) media.hidden = t !== "media";
    const macroRow = $("#row-macro");
    if (macroRow) macroRow.hidden = t !== "macro";
    const rec = $("#sel-record");
    if (rec) {
      rec.hidden = t !== "hotkey";
      if (rec.hidden) rec.classList.remove("on");
    }
    const row = $("#editor-actions-row");
    if (row) row.hidden = !["hotkey", "media", "nav"].includes(t);
    const vrow = $("#row-value");
    if (vrow) vrow.hidden = !["launch", "url", "hotkey"].includes(t);
  });

  $("#sel-save-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    const type = $("#sel-type").value;
    if (type === "none") {
      await post("/api/key/action/clear", { key: selectedKey });
      await refreshState();
      return;
    }
    const spec = { key: selectedKey, type };
    if (type === "nav") spec.name = $("#sel-nav").value;
    else if (type === "hotkey") spec.keys = $("#sel-value").value.trim();
    else if (type === "media") spec.action = $("#sel-media").value;
    else if (type === "macro") spec.macro = $("#sel-macro").value;
    else if (type === "url") spec.url = $("#sel-value").value.trim();
    else spec.path = $("#sel-value").value.trim();

    const r = await post("/api/key/action", spec);
    if (!r.ok) toast("保存失败：路径不存在或内容为空", 1800);
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
    const k = String(e.key).toLowerCase();
    if (!["control", "shift", "alt", "meta"].includes(k)) parts.push(k);
    if (parts.length < 2) {
      toast("至少要包含一个修饰键（Ctrl/Alt/Shift）", 1600);
      return;
    }
    $("#sel-value").value = parts.join("+");
    $("#sel-type").value = "hotkey";
    $("#sel-value").disabled = false;
    $("#sel-nav").hidden = true;
    recording = false;
    $("#sel-record").classList.remove("on");
  }, true);

  $("#awake-btn").addEventListener("click", async () => {
    const v = !(state?.keepAwake);
    const r = await post("/api/device/keep-awake", { value: v });
    if (state) state.keepAwake = r.value;
    $("#awake-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "保持唤醒：开" : "保持唤醒：关", 1200);
  });

  $("#reset-device").addEventListener("click", async () => {
    toast("正在复位设备…", 2000);
    applyState(await post("/api/device/reset"));
  });

  $("#mode-btn").addEventListener("click", async () => {
    await post("/api/device/mode", { mode: 0 });
    toast("已发送模式切换命令，按一下设备按键试试", 2200);
  });

  $("#probe-btn").addEventListener("click", async () => {
    await post("/api/device/probe");
    toast("探测开始：现在连按几次设备按键（约 10 秒）", 8000);
  });

  $("#pause-btn").addEventListener("click", async () => {
    const v = !(state?.writesPaused);
    const r = await post("/api/device/pause-writes", { value: v });
    if (state) state.writesPaused = r.value;
    $("#pause-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "已暂停所有写入，按几下设备键试试" : "写入已恢复", 2500);
  });

  $("#autostart-btn").addEventListener("click", async () => {
    const v = !(state?.autostart);
    const r = await post("/api/autostart", { value: v });
    if (state) state.autostart = r.value;
    $("#autostart-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "已设置开机自启" : "已关闭开机自启", 1600);
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

  $("#icon-sync").addEventListener("click", async () => {
    toast("正在同步图标库…", 1500);
    const r = await post("/api/icons/sync", {});
    toast(`图标库已更新：新增 ${r.added}，共 ${r.count} 个`, 2200);
    await loadIcons();
    await refreshState();
  });
  $("#icon-search").addEventListener("input", () => loadIcons());
  loadIcons();

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
      if (act === "rename") {
        startInlineRename(name, custom);
      } else if (act === "dup") {
        const r = await post("/api/scenes", { duplicate: name });
        toast(r.ok ? "已复制场景" : "复制失败", 1500);
        renderScenes(r.items || []);
      } else if (act === "del") {
        if (!(await confirmDialog(`删除场景「${name}」？`))) return;
        const r = await post("/api/scenes", { delete: name });
        renderScenes(r.items || []);
      }
    });
    return sceneMenuEl;
  };
  const showSceneMenu = (x, y, name, custom) => {
    const menu = ensureSceneMenu();
    menu.dataset.name = name;
    menu.dataset.custom = custom ? "1" : "0";
    menu.innerHTML = `<button data-act="rename">重命名</button>`
      + `<button data-act="dup">复制场景</button>`
      + (custom ? "" : `<button data-act="del">删除</button>`);
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
        const picked = selectedScene === name;
        return `<div class="row scene-row${active ? " active" : ""}${custom ? " custom" : ""}${picked ? " picked" : ""}"`
          + ` data-name="${name}" data-custom="${custom ? 1 : 0}">`
          + `<b>${name}</b>`
          + `<span class="scene-ops">${active ? "使用中" : (picked ? `<button class="scene-apply">切换</button>` : "")}</span>`
          + "</div>";
      }).join("")
      : "<div class='row'>还没有场景，点 ＋ 新建</div>";
    box.querySelectorAll(".scene-row").forEach((row) => {
      const name = row.dataset.name;
      row.addEventListener("click", (e) => {
        if (e.target.classList.contains("scene-apply")) return;
        selectedScene = name;
        renderScenes(items);
      });
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        showSceneMenu(e.clientX, e.clientY, name, row.dataset.custom === "1");
      });
      const apply = row.querySelector(".scene-apply");
      if (apply) {
        apply.addEventListener("click", async (e) => {
          e.stopPropagation();
          const r = await post("/api/scenes", { apply: name });
          toast(r.ok ? `已切换到「${name}」` : "切换失败", 1600);
          if (r.ok) {
            await refreshState();
            await reloadScenes();
          }
        });
      }
    });
  };
  const reloadScenes = async () => {
    const r = await post("/api/scenes", {});
    renderScenes(r.items || []);
  };
  $("#scene-add").addEventListener("click", async () => {
    const name = window.prompt("新场景名称", `场景 ${new Date().toLocaleDateString()}`);
    if (!name) return;
    const r = await post("/api/scenes", { save: name });
    toast(r.ok ? `已新建「${name}」（点它再点切换才会应用）` : "新建失败", 2200);
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
    if (target?.custom) {
      toast("自定义场景不能删除", 1600);
      return;
    }
    if (!(await confirmDialog(`删除场景「${name}」？`))) return;
    const r = await post("/api/scenes", { delete: name });
    selectedScene = null;
    renderScenes(r.items || []);
  });
  $("#scene-official").addEventListener("click", async () => {
    const r = await post("/api/scenes", { official: true });
    const items = r.items || [];
    const box = $("#scene-list");
    if (!items.length) {
      box.innerHTML = "<div class='row'>没找到官方场景</div>";
      return;
    }
    box.innerHTML = `<div class="row scene-back" id="scene-back">← 返回场景列表</div>`
      + items.map((it) =>
        `<div class="row scene-row official" data-id="${it.id}" data-name="${it.name}">` +
        `<b>${it.name}</b><span>${it.count} 键</span></div>`).join("");
    $("#scene-back").addEventListener("click", () => reloadScenes());
    box.querySelectorAll(".official").forEach((row) => {
      row.addEventListener("click", async () => {
        toast("正在导入官方场景…", 1500);
        const res = await post("/api/scenes", { import: row.dataset.id });
        if (res.ok) {
          const info = res.stats || {};
          toast(`已导入「${row.dataset.name}」：可执行 ${info.supported ?? "-"} / 共 ${info.total ?? "-"} 键（灰色键为官方控件，暂不支持）`, 4600);
        } else {
          toast("导入失败", 2200);
        }
        if (res.ok) {
          await refreshState();
          await reloadScenes();
        }
      });
    });
  });
  reloadScenes();

  $("#library-btn").addEventListener("click", async () => {
    const r = await post("/api/library", {});
    const plugins = r.plugins || [];
    const profiles = r.profiles || [];
    const head = document.querySelectorAll(".side-sec-head span")[2];
    if (head) head.textContent = `资源库 · ${plugins.length}`;
    const rows = plugins.slice(0, 80).map((p) =>
      `<div class="row"><b>${p.name || p.id}</b><span>${p.actions} 动作</span></div>`).join("")
      + profiles.slice(0, 30).map((n) =>
        `<div class="row"><b>${n}</b><span>场景</span></div>`).join("");
    $("#lib-list").innerHTML = rows || "<div class='row'>没有找到资源</div>";
  });

  $("#lightonly-btn").addEventListener("click", async () => {
    const v = !(state?.lightingOnly);
    const r = await post("/api/device/lighting-only", { value: v });
    if (state) state.lightingOnly = r.value;
    $("#lightonly-btn").classList.toggle("on", !!r.value);
    toast(r.value ? "仅灯光模式：不再写按键画面" : "已恢复按键画面", 2200);
  });

  const pushStandbyB = throttle((v) => post("/api/standby-brightness", { value: v }), 250);
  $("#standby-brightness").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#standby-brightness-val").textContent = String(v);
    pushStandbyB(v);
  });

  const pushStandby = throttle((v) => post("/api/standby", { sec: v }), 300);
  $("#standby-sec").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    $("#standby-sec-val").textContent = fmtSec(v);
    pushStandby(v);
  });

  // 切到控制台窗口不会唤醒待机（只有设备按键或显式操作才会）
  $("#sel-clear-top").addEventListener("click", async () => {
    if (!selectedKey) return;
    if (!(await confirmDialog(`清除按键 ${selectedKey} 的图片、动作和样式？`))) return;
    await post("/api/key/clear", { key: selectedKey });
    await post("/api/key/action/clear", { key: selectedKey });
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
    } else if (msg.type === "status") {
      refreshState();
    }
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectEvents, 1500);
  };
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
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
