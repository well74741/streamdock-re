/* 灯效库 + 多页灯效场景的界面。灯效代码只在这里编辑文本；用户代码由独立后台 Worker 执行，不进入本页面。
 *
 * - 左侧「灯效」分类 = 灯效库（和「相册」分类同样的网格）：点一下在下方编辑代码，拖到页面编辑区加入一页。
 * - 灯效场景：编辑区换成「页面编辑区」（布局同相册场景）：左边当前页预览，右边页列表（拖入 / 排序 / 移除 / 点选）。
 * - 选中上方按键时仍是普通按键编辑（只能设动作，不能放图片 / 图标）。
 */
(() => {
  let current = "", dirty = false, loading = "", request = 0, effect = {};
  let initialized = false, polling = false, logSeq = 0, logTicks = 0;
  let library = [], scene = null, sceneLoading = "", pagesBound = false;
  const drafts = new Map();
  const baselines = new Map();
  const normalized = (data) => JSON.stringify({ name: String(data.name || "").trim(),
    description: String(data.description || ""), code: String(data.code || "") });
  let baseline = normalized({});
  function updateDirty() {
    dirty = normalized(values()) !== baseline;
    q("#fx-save").classList.toggle("dirty", dirty);
    if (!dirty) drafts.delete(current || "__new__");
    return dirty;
  }
  const held = new Set();
  let inputChain = Promise.resolve();
  const q = (s) => document.querySelector(s);
  const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const api = (action, data = {}) => post("/api/effects", { action, ...data });
  const thumbUrl = (it) => `/api/effects/thumb/${encodeURIComponent(it.name)}?v=${it.thumb}`;
  const EFFECT_MIME = "application/x-m18-effect";
  const PAGE_MIME = "application/x-m18-effect-page";
  function values() {
    return { name: q("#fx-name").value.trim(), description: q("#fx-description").value, code: q("#fx-code").value };
  }
  function remember() { if (dirty) drafts.set(current || "__new__", values()); }
  function fill(data, changed = false) {
    q("#fx-name").value = data.name || "";
    q("#fx-description").value = data.description || "";
    q("#fx-code").value = data.code || "";
    if (!changed) {
      baseline = normalized(data);
      if (current) baselines.set(current, baseline);
    }
    updateDirty();
  }
  function show(scroll = true) {
    q("#fx-body").hidden = false;
    q("#fx-fold").textContent = "收起";
    if (scroll) q("#effect-lab").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---------------------------------------------------------------- 灯效库
  async function open(name, scroll = true) {
    if (!name) return;
    if (current === name || loading === name) { if (scroll) show(); return; }
    remember();
    const id = ++request;
    loading = name;
    const saved = drafts.get(name);
    const data = saved ? { ok: true, ...saved } : await api("get", { name });
    if (id !== request) return;
    loading = "";
    if (!data.ok) return toast(data.error || "读取灯效失败", 1800);
    current = name;
    baseline = baselines.get(name) || normalized(data);
    fill(data, !!saved);
    renderLibrary();
    if (scroll) show();
  }
  async function loadLibrary() {
    const data = await api("list");
    if (!data.ok) return;
    library = data.items || [];
    renderLibrary();
    if (scene) renderPages(scene.name, true);
  }
  function renderLibrary() {
    q("#fx-count").textContent = library.length;
    const box = q("#fx-list");
    box.innerHTML = library.length ? library.map((it) => `<span class="lib-cell fx-cell${it.active ? " running" : ""}${it.name === current ? " sel" : ""}"
        draggable="true" data-effect="${escape(it.name)}" title="${escape(it.description || it.name)}${it.scenes.length ? `\n用在：${escape(it.scenes.join("、"))}` : ""}\n点一下编辑代码 · 拖到灯效场景的页面编辑区加入一页 · 双击试运行">
        ${it.thumb ? `<img draggable="false" src="${thumbUrl(it)}" alt="" />` : '<span class="fx-ph">✦</span>'}
        <i>${escape(it.name)}</i></span>`).join("")
      : '<p class="side-empty small">灯效库还是空的，点 ＋ 新建一个。</p>';
    box.querySelectorAll(".fx-cell").forEach((cell) => {
      const name = cell.dataset.effect;
      cell.addEventListener("click", () => open(name));
      cell.addEventListener("dblclick", () => apply(name));
      cell.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData(EFFECT_MIME, name);
        e.dataTransfer.setData("text/plain", name);
        e.dataTransfer.effectAllowed = "copy";
        document.body.classList.add("fx-dragging");
      });
      cell.addEventListener("dragend", () => document.body.classList.remove("fx-dragging"));
    });
  }
  async function create() {
    remember();
    request++; loading = ""; current = ""; baseline = normalized({});
    const names = new Set(library.map((it) => it.name));
    let n = 1; while (names.has(`灯效 ${n}`)) n++;
    const data = await api("template");
    fill(drafts.get("__new__") || { name: `灯效 ${n}`, code: data.code || "", description: data.description }, true);
    renderLibrary();
    show();
  }
  async function save() {
    const data = values();
    if (!data.name || !data.code.trim()) { toast("请填名称和灯效代码", 1800); return false; }
    q("#fx-save").disabled = true;
    try {
      const r = await api("save", { ...data, original: current });
      if (!r.ok) { toast(r.error || "保存失败", 2600); return false; }
      drafts.delete(current || "__new__");
      current = r.name;
      fill({ ...data, name: current });
      await loadLibrary();
      toast(r.active ? "已保存并热重载正在运行的灯效" : "已保存到灯效库：拖到灯效场景的页面编辑区即可加入一页", 2400);
      return true;
    } finally { q("#fx-save").disabled = false; }
  }
  async function apply(name = current) {
    // 试运行：当前是灯效场景就临时放这一段（不改页列表），否则单独试运行，「退出灯效」回到原场景
    if (name === current && dirty) {
      if (!(await save())) return;
      name = current;
    }
    if (!name) return toast("请先保存到灯效库", 1600);
    const result = await api("run", { name });
    if (!result.ok) return toast(result.error || "灯效启动失败", 2400);
    toast(result.trial ? `试运行「${name}」：点「退出灯效」回到原场景` : `当前灯效场景临时播放「${name}」`, 2200);
    await refreshState();
  }

  // ---------------------------------------------------------------- 灯效场景（页面编辑区）
  function sceneName() {
    // 选中 / 正在运行的灯效场景（试运行时不算：那时没有页面可编辑）
    const name = selectedScene || state?.activeScene || "";
    return (sceneItems || []).find((it) => it.name === name && it.sceneType === "effect") ? name : "";
  }
  async function renderPages(name, force = false) {
    bindPages();
    if (!name) return;
    if (!force && (sceneLoading === name || (scene && scene.name === name && !scene.stale))) {
      paintPages();
      return;
    }
    sceneLoading = name;
    const data = await api("scene", { scene: name });
    if (sceneLoading !== name) return;
    sceneLoading = "";
    if (!data.ok) return;
    scene = data;
    paintPages();
  }
  function paintPages() {
    if (!scene) return;
    const items = scene.items || [];
    const cur = items[scene.index];
    q("#ep-name").textContent = scene.name;
    q("#ep-count").textContent = `${items.length} 页`;
    q("#ep-current").textContent = cur ? `第 ${scene.index + 1} 页 · ${cur.name}${cur.missing ? "（灯效库里已删除）" : ""}` : "—";
    q("#ep-live").hidden = !scene.active;
    const libItem = cur && library.find((it) => it.name === cur.name);
    const img = q("#ep-preview");
    if (libItem && libItem.thumb) {
      img.src = thumbUrl(libItem); img.hidden = false;
      q("#ep-empty").hidden = true;
    } else {
      img.hidden = true; img.removeAttribute("src");
      q("#ep-empty").hidden = false;
      q("#ep-empty").innerHTML = items.length
        ? `${escape(cur?.name || "")}<br><span>运行一次后自动生成预览图</span>`
        : "从左侧「灯效」分类拖入灯效<br><span>每个灯效是一页，◀ ▶ 长按切换</span>";
    }
    q("#ep-prev").disabled = q("#ep-next").disabled = items.length < 2;
    q("#ep-del").disabled = q("#ep-edit").disabled = !cur;
    q("#ep-strip").innerHTML = items.map((it, i) => {
      const lib = library.find((x) => x.name === it.name);
      return `<div class="album-thumb fx-page${i === scene.index ? " on" : ""}${it.missing ? " missing" : ""}" draggable="true" data-i="${i}"
          title="第 ${i + 1} 页：${escape(it.name)}\n点一下切到这一页 · 拖动排序">
        ${lib && lib.thumb ? `<img draggable="false" src="${thumbUrl(lib)}" alt="" />` : '<span class="fx-ph">✦</span>'}
        <i>${i + 1}. ${escape(it.name)}</i>
        <button class="album-del" data-del="${i}" title="移除这一页">×</button></div>`;
    }).join("");
  }
  async function pageOp(action, data) {
    if (!scene) return;
    const r = await api(action, { scene: scene.name, ...data });
    if (!r.ok) return toast(r.error || "操作失败", 2000);
    scene = r;
    paintPages();
    window.m18ReloadScenes?.();
  }
  function dropIndex(e) {
    const tile = e.target.closest?.(".fx-page");
    return tile ? Number(tile.dataset.i) : undefined;
  }
  function bindPages() {
    if (pagesBound || !q("#effect-pages")) return;
    pagesBound = true;
    const zone = q("#effect-pages");
    zone.addEventListener("dragover", (e) => {
      const types = Array.from(e.dataTransfer.types || []);
      if (!types.includes(EFFECT_MIME) && !types.includes(PAGE_MIME)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = types.includes(EFFECT_MIME) ? "copy" : "move";
      zone.classList.add("lib-drop-hot");
    });
    zone.addEventListener("dragleave", (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("lib-drop-hot"); });
    zone.addEventListener("drop", (e) => {
      zone.classList.remove("lib-drop-hot");
      const name = e.dataTransfer.getData(EFFECT_MIME);
      const from = e.dataTransfer.getData(PAGE_MIME);
      if (!name && from === "") return;
      e.preventDefault();
      const at = dropIndex(e);
      if (name) pageOp("pageAdd", at === undefined ? { name } : { name, index: at });
      else if (at !== undefined && Number(from) !== at) pageOp("pageMove", { from: Number(from), to: at });
    });
    const strip = q("#ep-strip");
    strip.addEventListener("dragstart", (e) => {
      const tile = e.target.closest(".fx-page");
      if (!tile) return;
      e.dataTransfer.setData(PAGE_MIME, tile.dataset.i);
      e.dataTransfer.effectAllowed = "move";
    });
    strip.addEventListener("click", (e) => {
      const del = e.target.closest("[data-del]");
      if (del) { e.stopPropagation(); pageOp("pageRemove", { index: Number(del.dataset.del) }); return; }
      const tile = e.target.closest(".fx-page");
      if (tile) pageOp("pageGoto", { index: Number(tile.dataset.i) });
    });
    const step = (d) => scene && scene.items.length && pageOp("pageGoto", { index: (scene.index + d + scene.items.length) % scene.items.length });
    q("#ep-prev").onclick = () => step(-1);
    q("#ep-next").onclick = () => step(1);
    q("#ep-del").onclick = () => scene && scene.items.length && pageOp("pageRemove", { index: scene.index });
    q("#ep-edit").onclick = () => { const cur = scene?.items?.[scene.index]; if (cur && !cur.missing) open(cur.name); };
    q("#ep-view").onclick = () => q("#ep-edit").click();
  }

  // ---------------------------------------------------------------- 运行状态
  async function copy(text, ok) {
    try { await navigator.clipboard.writeText(text); toast(ok, 1800); }
    catch (_) {
      const modal = document.createElement("div");
      modal.className = "modal confirm-modal";
      modal.innerHTML = '<div class="modal-card"><h2>复制内容</h2><textarea class="cp-code" style="width:100%;height:300px"></textarea><button class="seg">关闭</button></div>';
      modal.querySelector("textarea").value = text;
      modal.querySelector("button").onclick = () => modal.remove();
      document.body.append(modal);
      modal.querySelector("textarea").select();
    }
  }
  function renderState(value = {}) {
    const wasActive = !!effect.active, wasName = effect.name;
    effect = value;
    document.body.classList.toggle("effect-scene", !!value.active);
    q("#fx-stop").disabled = !value.active;
    const audio = value.audio || {};
    const fps = value.fps || {screens: 4, leds: 25, imageBudget: 60};
    const screens = value.parallel
      ? `15键并行写屏≤${fps.screens}fps（整板约${fps.fullBoard}fps）`
      : `逐键写屏：变化屏幕≤${fps.screens}fps / 总预算${fps.imageBudget || 60}图每秒`;
    q("#fx-status").textContent = value.active
      ? `运行：${value.name} · ${value.status} · 音频：${audio.status || (audio.available ? "已连接" : "不可用")} · ${screens} / 灯珠≤${fps.leds}fps`
      : `未运行灯效${value.status === "error" ? "：代码或宿主出错，请查看日志" : ""}`;
    let image = q("#fx-board-preview");
    if (!image) {
      image = document.createElement("img"); image.id = "fx-board-preview"; image.alt = "灯效整板预览"; image.hidden = true;
      q("#ring-wrap").append(image);
    }
    if (!value.active) {
      image.hidden = true; image.removeAttribute("src");
      if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = ""; }
    } else if (typeof value.previewRev === "number") {
      q(".ring-board").classList.remove("album-on");
      if (!previewLoop) previewLoop = setTimeout(previewTick, 0);
    }
    // 换页 / 启停时刷新库里「运行中」标记和页面编辑区
    if (wasActive !== !!value.active || wasName !== value.name) {
      if (scene) scene.stale = true;
      loadLibrary();
    }
  }
  // 预览自己按 ~10fps 拉取，和状态轮询解耦；新图解码好才替换（双缓冲），不会闪也不会排队。
  let previewLoop = 0, previewUrl = "";
  async function previewTick() {
    previewLoop = 0;
    if (!effect.active) return;
    const started = performance.now();
    if (!document.hidden) {
      try {
        const response = await fetch(`/api/effect/preview.jpg?r=${effect.revision}-${effect.previewRev}-${Date.now()}`, { cache: "no-store" });
        if (response.status === 200 && effect.active) {
          const url = URL.createObjectURL(await response.blob());
          const next = new Image(); next.src = url; await next.decode();
          const image = q("#fx-board-preview");
          if (image && effect.active) {
            image.src = url; image.hidden = false;
            if (previewUrl) URL.revokeObjectURL(previewUrl);
            previewUrl = url;
          } else URL.revokeObjectURL(url);
        }
      } catch (_) { /* 下一次再拉 */ }
    }
    if (effect.active) previewLoop = setTimeout(previewTick, Math.max(20, 100 - (performance.now() - started)));
  }
  async function poll() {
    if (polling || document.hidden || (!effect.active && effect.status !== "error")) return;
    polling = true;
    try {
      const data = await api("status");
      if (data.ok) renderState(data);
      if (++logTicks % 4 === 0) {
        const response = await api("logs", { since: logSeq, limit: 30 });
        for (const line of response.items || []) {
          logSeq = Math.max(logSeq, line.seq || 0);
          const div = document.createElement("div");
          div.textContent = `[${line.level}] ${line.message}`;
          q("#fx-log").append(div);
        }
        while (q("#fx-log").children.length > 100) q("#fx-log").firstChild.remove();
      }
    } finally { polling = false; }
  }
  function input(key, down) {
    const revision = effect.revision;
    inputChain = inputChain.then(() => {
      if (effect.active && revision === effect.revision) return post("/api/effects/input", { key, down });
    }).catch(() => {}); // 下/上请求串行，切换后不把旧鼠标事件送进新灯效
  }
  function guideDialog(path) {
    const modal = document.createElement("div");
    modal.className = "modal confirm-modal";
    modal.innerHTML = `<div class="modal-card fx-guide-card"><h2>灯效技术文档已打开</h2>
      <p>资源管理器里已选中 <b>M18FX 灯效技术文档.md</b>（同目录还有可运行的「示例-打击垫.js」）。</p>
      <ol>
        <li>把这个文件<b>直接拖进</b>其他智能体（ChatGPT、Claude、DeepSeek、豆包等）的对话框。</li>
        <li>用一两句话说清想要的效果，例如「星空背景，按键时从按下的位置炸开彩色粒子，灯带跟着变色」。</li>
        <li>让它<b>只返回 JavaScript</b>，复制回本页「自制灯效」编辑区，点「保存到灯效库」，再「试运行」。</li>
        <li>满意后把左侧「灯效」分类里的它拖进灯效场景的页面编辑区，就多了一页。</li>
      </ol>
      <p class="muted">文档位置：<code>${escape(path)}</code><br>支持 MCP 的智能体也可以点「接入 AI / MCP」直接开发和测试。</p>
      <div class="modal-actions"><button class="seg" id="fx-guide-copy">复制路径</button><button class="seg on" id="fx-guide-ok">知道了</button></div></div>`;
    document.body.append(modal);
    modal.querySelector("#fx-guide-ok").onclick = () => modal.remove();
    modal.querySelector("#fx-guide-copy").onclick = () => copy(path, "文档路径已复制");
  }
  function init() {
    if (initialized || !q("#effect-lab")) return;
    initialized = true;
    for (const id of ["fx-name", "fx-description", "fx-code"]) q(`#${id}`).addEventListener("input", () => {
      updateDirty(); remember();
    });
    q("#fx-new").onclick = create;
    q("#fx-new-side").onclick = create;
    q("#fx-template").onclick = async () => {
      if (dirty && !(await confirmDialog("用打击垫示例替换当前编辑内容？"))) return;
      const data = await api("template");
      if (data.ok) fill({ ...values(), code: data.code }, true);
    };
    q("#fx-fold").onclick = () => { const body = q("#fx-body"); body.hidden = !body.hidden; q("#fx-fold").textContent = body.hidden ? "展开" : "收起"; };
    q("#fx-save").onclick = save;
    q("#fx-apply").onclick = () => apply();
    q("#fx-stop").onclick = async () => { await api("stop"); await refreshState(); await window.m18ReloadScenes?.(); };
    q("#fx-agent").onclick = agentDialog;
    q("#fx-guide").onclick = async () => {
      const data = await api("revealGuide");
      if (!data.ok) return toast(data.error || "打开技术文档失败", 2400);
      guideDialog(data.path);
    };
    q("#fx-prompt").onclick = () => copy("请根据 M18FX 灯效技术文档（我会把文档文件一起发给你），为 Stream Dock M18 编写一个灯效。只返回可直接粘贴的 JavaScript，不要 HTML/Node.js/Python，不使用网络、不读写文件。联合控制15块屏幕和24颗灯珠，使用文档中的真实物理坐标，响应1-18键的按下/松开和电脑音频能量/8频带。效果需求：（在这里写你想要的效果）。若接入MCP：先m18_effect_api/m18_status，再m18_save_effect保存到灯效库、m18_run_effect试运行，用m18_effect_input和m18_effect_logs测试，最后m18_stop_effect；不改现有按键配置。", "智能体指引已复制");
    q("#fx-delete").onclick = async () => {
      if (!current || !(await confirmDialog(`从灯效库删除「${escape(current)}」？用到它的灯效页会一并移除。`))) return;
      const data = await api("delete", { name: current });
      if (!data.ok) return toast(data.error || "删除失败", 1800);
      drafts.delete(current); current = ""; fill({});
      await loadLibrary(); await refreshState(); await window.m18ReloadScenes?.();
    };
    // 灯效运行时，在预览板上点按键：照常选中按键（可编辑这个键的动作），同时把按下/松开交给灯效测试互动
    document.addEventListener("pointerdown", (event) => {
      if (!effect.active || event.button !== 0) return;
      const target = event.target.closest(".ring-board .cell, .ring-btn");
      if (!target) return;
      const key = Number(target.dataset.key);
      held.add(key); input(key, true);
    }, true);
    const release = () => { for (const key of held) input(key, false); held.clear(); };
    document.addEventListener("pointerup", release, true);
    document.addEventListener("pointercancel", release, true);
    window.addEventListener("blur", release);
    setInterval(poll, 250);
    loadLibrary();
  }
  async function guard() {
    if (!updateDirty()) return true;
    return confirmDialog("灯效编辑内容尚未保存。切换场景会保留草稿，但不会应用这些修改，仍要切换？");
  }
  function onMessage(msg) {
    if (msg.type === "effectlibrary") loadLibrary();
    else if (msg.type === "effectscene" && scene && msg.scene === scene.name) renderPages(scene.name, true);
  }
  // selectedName 保留旧名字：app.js 用它判断「当前是不是灯效场景」
  window.M18EffectUI = { init, open, selectedName: sceneName, sceneName, renderPages, renderLibrary: () => renderLibrary(),
    renderState, guard, onMessage, reload: loadLibrary };
})();
