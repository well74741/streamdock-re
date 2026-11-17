/* 此文件必须带 WORKER_CSP 响应头；不加载其它脚本，不开放任何网络。 */
'use strict';
(() => {
  const nativePost = self.postMessage.bind(self);
  const nativeListen = self.addEventListener.bind(self);
  const compile = Function;
  let api, canvas, ctx, config, handlers, colors, committed, frame = 0, previous = null;
  let failed = false, messages = [], tick;
  function send(value, transfer = []) { nativePost(value, transfer); }
  function fail(error) {
    if (failed) return;
    failed = true;
    let message = String(error && (error.stack || error.message) || error);
    const line = message.match(/m18-effect-user\.js:(\d+):\d+/);
    if (line) message = '代码第 ' + Math.max(1, Number(line[1]) - 3) + ' 行：' + message;
    send({type: 'error', message: message.slice(0, 2048)});
  }
  function safeAudio(value) {
    const number = v => Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0;
    return Object.freeze({level: number(value?.level),
      bands: Object.freeze(Array.from({length: 8}, (_, i) => number(value?.bands?.[i]))),
      available: value?.available === true, status: String(value?.status || 'unavailable').slice(0, 32)});
  }
  function emit(event, value) {
    for (const fn of [...handlers[event]]) {
      const result = fn(value);
      if (result && typeof result.then === 'function') throw new Error('M18FX 事件必须同步，不能返回 Promise');
    }
  }
  function init(data) {
    if (api) throw new Error('运行时只能初始化一次');
    config = data;
    canvas = new OffscreenCanvas(config.width, config.height);
    ctx = canvas.getContext('2d', {willReadFrequently: true, alpha: false});
    if (!ctx) throw new Error('浏览器不支持 OffscreenCanvas 2D');
    handlers = {key: [], frame: []};
    let audio = safeAudio(null);
    api = Object.freeze({
      version: 1, canvas, ctx, width: config.width, height: config.height,
      keys: Object.freeze(config.keys.map(p => Object.freeze({...p}))),
      leds: Object.freeze(config.leds.map(p => Object.freeze({...p}))),
      get audio() { return audio; },
      on(event, fn) {
        if (!['key', 'frame'].includes(event) || typeof fn !== 'function') throw new Error('on 只支持 key/frame');
        if (handlers[event].length >= 32) throw new Error('每种事件最多 32 个处理函数');
        handlers[event].push(fn);
        return () => { const i = handlers[event].indexOf(fn); if (i >= 0) handlers[event].splice(i, 1); };
      },
      render(rgb) {
        if (canvas.width !== config.width || canvas.height !== config.height) throw new Error('不能改变画布尺寸');
        if (rgb !== undefined) {
          if (!Array.isArray(rgb) || rgb.length !== config.leds.length) throw new Error('render 需要 24 组 RGB');
          colors = rgb.map(c => {
            if (!Array.isArray(c) || c.length !== 3 || !c.every(Number.isFinite)) throw new Error('RGB 格式错误');
            return c.map(v => Math.round(Math.max(0, Math.min(255, v))));
          });
        } else {
          colors = config.leds.map(p => {
            const x = Math.max(0, Math.min(canvas.width - 3, Math.round(p.x) - 1));
            const y = Math.max(0, Math.min(canvas.height - 3, Math.round(p.y) - 1));
            const pixels = ctx.getImageData(x, y, 3, 3).data, sum = [0, 0, 0];
            for (let i = 0; i < pixels.length; i += 4) for (let ch = 0; ch < 3; ch++) sum[ch] += pixels[i + ch];
            return sum.map(v => Math.round(v / 9));
          });
        }
        committed = true;
      },
      log(...parts) {
        if (messages.length >= 16) return;
        messages.push(parts.map(v => typeof v === 'string' ? v : JSON.stringify(v)).join(' ').slice(0, 2048));
      }
    });
    // CSP 的 script-src 只有 unsafe-eval（没有 self/远端来源），连 dynamic import 也被拒绝。
    const denied = () => { throw new Error('灯效不开放网络、嵌套 Worker、录音或自建定时器；请使用 on(frame)'); };
    for (const name of ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'WebTransport',
      'Worker', 'SharedWorker', 'importScripts', 'setTimeout', 'setInterval', 'postMessage']) {
      Object.defineProperty(self, name, {value: denied, writable: false, configurable: false});
    }
    Object.defineProperty(self, 'M18FX', {value: api, writable: false, configurable: false});
    const result = compile('M18FX', '"use strict";\n' + config.code + '\n//# sourceURL=m18-effect-user.js')(api);
    if (result && typeof result.then === 'function') throw new Error('灯效初始化必须同步');
    tick = data => {
      audio = safeAudio(data.audio);
      for (const event of data.events.slice(0, 128)) emit('key', Object.freeze({...event}));
      const seconds = data.time / 1000;
      const dt = previous === null ? 1 / config.fps : Math.max(0, Math.min(0.25, seconds - previous));
      previous = seconds;
      committed = false;
      emit('frame', Object.freeze({time: seconds, dt, frame: ++frame}));
      if (committed) {
        const bitmap = canvas.transferToImageBitmap();
        send({type: 'frame', id: data.id, bitmap, colors, messages}, [bitmap]);
      } else send({type: 'idle', id: data.id, messages});
      messages = [];
    };
    send({type: 'ready'});
  }
  nativeListen('message', event => {
    if (failed) return;
    try {
      if (event.data.type === 'init') init(event.data.config);
      else if (event.data.type === 'tick' && api) tick(event.data);
      else throw new Error('未知灯效消息');
    } catch (error) { fail(error); }
  });
  nativeListen('unhandledrejection', event => { event.preventDefault(); fail(event.reason); });
  nativeListen('error', event => { event.preventDefault(); fail(event.error || event.message); });
})();
