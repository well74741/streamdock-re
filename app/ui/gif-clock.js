/* 图标库动图使用同一时钟；懒加载、拖拽副本都加入现有时间轴，不改原图地址。 */
(() => {
  "use strict";
  const epoch = performance.now();
  const entries = new Map();
  const images = new WeakMap();
  const views = new Set();
  const queue = [];
  const maxBytes = 96 * 1024 * 1024;
  let bytes = 0, jobs = 0, raf = 0;
  const supported = typeof ImageDecoder === "function";

  function release(entry) {
    for (const frame of entry.frames || []) frame.bitmap.close();
    bytes -= entry.bytes || 0;
    entries.delete(entry.url);
  }
  function trim(required = 0) {
    // 只清理没有展示者的缓存，不打断正在播放或拖拽的图。
    const idle = [...entries.values()].filter((e) => !e.refs && (e.frames || e.failed))
      .sort((a, b) => a.used - b.used);
    while (idle.length && (bytes + required > maxBytes || entries.size > 48)) release(idle.shift());
  }
  async function decode(entry) {
    let decoder;
    const frames = [];
    let size = 0;
    try {
      const response = await fetch(entry.url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      decoder = new ImageDecoder({ data: await response.arrayBuffer(), type: "image/gif" });
      await decoder.tracks.ready;
      const count = decoder.tracks.selectedTrack?.frameCount || 0;
      if (!count) throw new Error("未读到动图帧");
      if (count > 600) throw new Error("动图超过 600 帧，保留浏览器原生播放");
      let total = 0;
      for (let i = 0; i < count; i++) {
        const { image } = await decoder.decode({ frameIndex: i });
        try {
          // 缩略图无需保存大尺寸原帧；所有帧都由同一时间轴选取。
          const scale = Math.min(1, 96 / Math.max(image.displayWidth, image.displayHeight));
          const w = Math.max(1, Math.round(image.displayWidth * scale));
          const h = Math.max(1, Math.round(image.displayHeight * scale));
          const cost = w * h * 4;
          trim(cost);
          if (bytes + cost > maxBytes) throw new Error("动图缩略缓存已满，保留浏览器原生播放");
          const canvas = new OffscreenCanvas(w, h);
          canvas.getContext("2d").drawImage(image, 0, 0, w, h);
          const delay = (image.duration || 100000) / 1000;
          // GIF 的 0/10ms 延时按 Chromium 原生规则视为 100ms。
          const duration = delay < 20 ? 100 : delay;
          total += duration;
          frames.push({ bitmap: canvas.transferToImageBitmap(), end: total });
          size += cost;
          bytes += cost;
        } finally { image.close(); }
      }
      entry.frames = frames;
      entry.total = total;
      entry.bytes = size;
      entry.ready = true;
      for (const view of views) if (view.entry === entry) activate(view);
      wake();
    } catch (error) {
      for (const frame of frames) frame.bitmap.close();
      bytes -= size;
      entry.bytes = 0;
      delete entry.frames;
      entry.ready = false;
      entry.failed = true;
      for (const view of views) if (view.entry === entry && view.img) {
        view.img.classList.remove("gif-clock-source");
        view.canvas.hidden = true;
      }
      // 不能解码时原图仍正常显示，不用静止图伪装同步成功。
      console.warn("图标库同步动图：", error.message);
    } finally {
      decoder?.close();
      entry.loading = false;
      trim();
    }
  }
  function pump() {
    while (jobs < 2 && queue.length) {
      const entry = queue.shift();
      if (!entry.refs) { entries.delete(entry.url); continue; }
      jobs++;
      entry.loading = true;
      decode(entry).finally(() => { jobs--; pump(); });
    }
  }
  function frameAt(entry, now) {
    const phase = (now - epoch) % entry.total;
    // 不从「上一次绘制帧」递增，后台停顿和懒加载都不会积累时间偏差。
    let lo = 0, hi = entry.frames.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (phase < entry.frames[mid].end) hi = mid;
      else lo = mid + 1;
    }
    return lo;
  }
  function paint(view, now) {
    const entry = view.entry;
    if (!entry.ready) return;
    const index = frameAt(entry, now);
    if (index === view.frame) return;
    const bitmap = entry.frames[index].bitmap;
    if (view.canvas.width !== bitmap.width || view.canvas.height !== bitmap.height) {
      view.canvas.width = bitmap.width;
      view.canvas.height = bitmap.height;
    }
    const ctx = view.canvas.getContext("2d");
    ctx.clearRect(0, 0, view.canvas.width, view.canvas.height);
    ctx.drawImage(bitmap, 0, 0);
    view.frame = index;
  }
  function detach(view) {
    intersection.unobserve(view.img || view.canvas);
    if (view.entry) {
      view.entry.refs--;
      view.entry.used = performance.now();
    }
    if (view.img) images.delete(view.img);
    views.delete(view);
  }
  function tick(now) {
    raf = 0;
    let playing = false;
    for (const view of views) {
      if (!view.canvas.isConnected || (view.img && !view.img.isConnected)) { detach(view); continue; }
      if (!view.visible || !view.entry?.ready) continue;
      paint(view, now);
      if (view.entry.frames.length > 1) playing = true;
    }
    if (playing && !document.hidden) raf = requestAnimationFrame(tick);
  }
  function wake() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(tick);
  }
  function activate(view) {
    paint(view, performance.now());
    if (view.img) {
      view.img.classList.add("gif-clock-source");
      view.canvas.hidden = false;
    }
  }
  const intersection = new IntersectionObserver((items) => {
    for (const item of items) {
      const view = images.get(item.target);
      if (!view) continue;
      view.visible = item.isIntersecting;
      if (view.visible && !view.entry && supported) load(view);
      else if (!view.visible && view.entry) {
        // 离开视口后释放缓存引用；再出现时按公共时钟追上，不保留几千张隐藏动图。
        view.entry.refs--;
        view.entry.used = performance.now();
        view.entry = null;
        view.frame = -1;
        view.canvas.hidden = true;
        view.img.classList.remove("gif-clock-source");
        trim();
      }
    }
    wake();
  });
  function load(view) {
    const url = view.img.src;
    let entry = entries.get(url);
    if (!entry) {
      entry = { url, refs: 0, used: performance.now() };
      entries.set(url, entry);
      queue.push(entry);
    }
    entry.refs++;
    view.entry = entry;
    views.add(view);
    if (entry.ready) activate(view);
    pump();
  }
  function attach(img) {
    if (images.has(img) || !/\.gif(?:[?#]|$)/i.test(img.getAttribute("src") || "")) return;
    let wrapper = img.parentElement;
    if (!wrapper.matches(".tile-wrap, .lib-cell")) {
      wrapper = document.createElement("span");
      wrapper.className = "gif-clock-view";
      if (img.classList.contains("cover")) wrapper.classList.add("gif-clock-cover");
      img.before(wrapper);
      wrapper.appendChild(img);
    }
    wrapper.classList.add("gif-clock-view");
    const canvas = document.createElement("canvas");
    canvas.className = "gif-clock-canvas";
    canvas.hidden = true;
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.objectFit = getComputedStyle(img).objectFit;
    wrapper.appendChild(canvas);
    const view = { img, canvas, entry: null, visible: false, frame: -1 };
    images.set(img, view);
    views.add(view);
    intersection.observe(img);
  }
  function scan(root) {
    root.querySelectorAll("img").forEach(attach);
    for (const view of views) if (!view.img?.isConnected && !view.canvas.isConnected) detach(view);
    trim();
  }
  function observe(root) {
    if (!root || !supported) return;
    scan(root);
    // 重建图标列表时复用解码缓存及 epoch；不通过重设 src 触发重新起播。
    let scheduled = false;
    new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      queueMicrotask(() => { scheduled = false; scan(root); });
    }).observe(root, { childList: true, subtree: true });
  }
  function clone(source, target) {
    const originals = source.matches("img") ? [source] : [...source.querySelectorAll("img")];
    const copies = target.matches("img") ? [target] : [...target.querySelectorAll("img")];
    let result = target;
    originals.forEach((img, i) => {
      const original = images.get(img), copy = copies[i];
      if (!original || !copy) return;
      if (!original.entry) load(original);
      if (original.entry.failed) return;
      const canvas = document.createElement("canvas");
      // 刚展开图标包就拖动时，解码可能还没结束；副本先显示已有画面，
      // 随原图一起进入公共时钟，不留下独立起播的 GIF。
      if (img.complete && img.naturalWidth) {
        canvas.width = Math.min(96, img.naturalWidth);
        canvas.height = Math.max(1, Math.round(img.naturalHeight * canvas.width / img.naturalWidth));
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      }
      canvas.className = copy.className.replace("gif-clock-source", "").trim();
      canvas.style.cssText = copy.style.cssText;
      canvas.style.objectFit = getComputedStyle(img).objectFit;
      // 副本直接订阅原图的帧，不再创建一个浏览器 GIF 播放器。
      if (copy === target) result = canvas;
      else {
        const old = copy.parentElement.querySelector(".gif-clock-canvas");
        old?.remove();
        canvas.className = "gif-clock-canvas";
        copy.replaceWith(canvas);
      }
      const view = { canvas, img: null, entry: original.entry, visible: true, frame: -1 };
      view.entry.refs++;
      views.add(view);
      paint(view, performance.now());
    });
    wake();
    return result;
  }
  document.addEventListener("visibilitychange", wake);
  window.M18GifClock = { observe, clone };
})();
