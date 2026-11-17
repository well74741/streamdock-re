/* 可信后台宿主：独立origin运行；用户代码仅在Worker，不把token交给Worker。 */
'use strict';
(() => {
  const token = location.hash.slice(1);
  history.replaceState(null, '', '/effects/host');
  const nativeFetch = fetch.bind(window);
  let worker, interval, deadline, uploadBusy = false, stopped = false, busy = true;
  let tickId = 0, seq = 0, pending = null, lastLog = 0, lastPreview = -Infinity;
  let imageTokens = 15, lastBudget = performance.now(), workerStarted = 0, fullValid = false, fullSeq = 0;
  let config, logs = [], colors = Array.from({length: 24}, () => [0, 0, 0]);
  // 只在服务端接受后推进缓存；排队帧被更新替换时不能假装它已经提交。
  const acceptedPixels = new Map();
  // 并行写屏（后端每键一条 SDK 连接，整板约57ms）时预算放宽到每秒300张；否则仍是60张。
  let parallel = false;
  const full = document.createElement('canvas'), preview = document.createElement('canvas');
  const tile = document.createElement('canvas');
  preview.width = 500; preview.height = 370; tile.width = tile.height = 64;
  const fullCtx = full.getContext('2d', {alpha: false, willReadFrequently: true});
  const previewCtx = preview.getContext('2d', {alpha: false, willReadFrequently: true});
  const tileCtx = tile.getContext('2d', {alpha: false, willReadFrequently: true});
  async function request(path, body) {
    const response = await nativeFetch('/effects/' + path, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store', credentials: 'omit',
      headers: {Authorization: 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: AbortSignal.timeout(1500)
    });
    if (!response.ok) throw new Error('灯效会话请求失败（' + response.status + '）');
    return response.json();
  }
  async function stop(message) {
    if (stopped) return;
    stopped = true;
    clearInterval(interval); clearTimeout(deadline); worker?.terminate();
    pending = null; logs = [];
    document.body.dataset.status = 'error'; document.body.textContent = String(message).slice(0, 2048);
    try { await request('error', {message: String(message).slice(0, 2048)}); } catch (_) { /* 后端也有看门狗 */ }
  }
  function crop(key) {
    tileCtx.drawImage(full, key.x - key.size / 2, key.y - key.size / 2,
      key.size, key.size, 0, 0, 64, 64);
  }
  function prepare(value) {
    const started = performance.now();
    const body = {seq: value.seq, colors: value.colors};
    const tiles = new Map();
    if (fullValid) {
      const now = performance.now();
      if (now - lastPreview >= 100) {   // 控制台预览约10fps
        lastPreview = now;
        previewCtx.drawImage(full, 0, 0, 500, 370);
        body.preview = preview.toDataURL('image/jpeg', 0.72);
      }
    }
    if (fullValid) {
      const now = performance.now();
      imageTokens = Math.min(15, imageTokens + (now - lastBudget) * (parallel ? 0.3 : 0.06));
      lastBudget = now;
      const changed = [];
      for (const key of config.keys.filter(p => p.screen)) {
        crop(key);
        const bytes = tileCtx.getImageData(0, 0, 64, 64).data;
        const pixels = new Uint32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
        const previous = acceptedPixels.get(key.key);
        if (!previous || !pixels.every((v, i) => v === previous[i])) changed.push({key, pixels});
      }
      // 一整份变化集来自同一个画布时刻；预算不足就等下一帧，不拆成跨时刻的几行。
      if (changed.length && changed.length <= imageTokens) {
        body.keys = {};
        body.screenSeq = fullSeq;
        body.screenColors = value.colors;
        for (const {key, pixels} of changed) {
          crop(key);
          body.keys[key.key] = tile.toDataURL('image/jpeg', 0.78);
          tiles.set(key.key, pixels);
        }
        imageTokens -= changed.length;
      }
    }
    document.body.dataset.encodeMs = String(Math.round(performance.now() - started));
    return {body, tiles};
  }
  async function upload() {
    if (uploadBusy || stopped) return;
    uploadBusy = true;
    try {
      while (pending && !stopped) {
        const value = pending; pending = null;
        // 不在Worker消息到达时提前编码排队：这里没有await，画布和颜色是同一快照。
        // 上一次请求返回后再比较最新画布，避免跨帧合并/撤回像素时丢失更新。
        const {body, tiles} = prepare(value);
        const started = performance.now();
        await request('frame', body);
        for (const [key, pixels] of tiles) acceptedPixels.set(key, pixels);
        document.body.dataset.uploadMs = String(Math.round(performance.now() - started));
        document.body.dataset.frames = String(body.seq);
      }
    } catch (error) { stop(error.message); }
    finally { uploadBusy = false; }
  }
  async function tick() {
    if (busy || stopped) return;
    busy = true;
    try {
      const started = performance.now();
      const data = await request('poll');
      document.body.dataset.pollMs = String(Math.round(performance.now() - started));
      if (stopped) return;
      parallel = data.parallel === true;
      document.body.dataset.parallel = String(parallel);
      delete data.parallel;
      workerStarted = performance.now();
      deadline = setTimeout(() => stop('灯效单帧超过 750ms，已终止 Worker'), 750);
      worker.postMessage({type: 'tick', id: ++tickId, ...data});
      const now = performance.now();
      if (logs.length && now - lastLog >= 1000) {
        const messages = logs.splice(0, 16); lastLog = now;
        request('log', {messages}).catch(() => {});
      }
    } catch (error) { stop(error.message); }
  }
  async function start() {
    if (!/^[A-Za-z0-9_-]{40,80}$/.test(token)) throw new Error('灯效会话凭证无效');
    config = await request('session');
    full.width = config.width; full.height = config.height;
    worker = new Worker('/effects/effect-worker.js');
    worker.onerror = event => { event.preventDefault(); stop(event.message || '灯效 Worker 错误'); };
    worker.onmessageerror = () => stop('灯效 Worker 消息错误');
    worker.onmessage = event => {
      const value = event.data;
      if (stopped) { value.bitmap?.close(); return; }
      if (value.type === 'error') { stop(value.message); return; }
      if (value.type === 'ready') {
        clearTimeout(deadline); busy = false; document.body.dataset.status = 'running';
        interval = setInterval(tick, 40); tick(); return;
      }
      if (!busy || value.id !== tickId || !['frame', 'idle'].includes(value.type)) {
        value.bitmap?.close(); stop('灯效 Worker 协议错误'); return;
      }
      clearTimeout(deadline);
      for (const message of (value.messages || []).slice(0, 16)) {
        if (logs.length < 64) logs.push(String(message).slice(0, 2048));
      }
      document.body.dataset.workerMs = String(Math.round(performance.now() - workerStarted));
      try {
        seq++;
        if (value.type === 'frame') {
          colors = value.colors;
          fullCtx.drawImage(value.bitmap, 0, 0);
          fullValid = true; fullSeq = seq;
        }
        pending = {seq, colors}; // 单槽，替换旧状态，不排队积累旧画面
        upload();
      } catch (error) { stop(error.message); }
      finally { value.bitmap?.close(); busy = false; }
    };
    deadline = setTimeout(() => stop('灯效初始化超过 2 秒，已终止 Worker'), 2000);
    worker.postMessage({type: 'init', config});
  }
  start().catch(error => stop(error.message));
})();
