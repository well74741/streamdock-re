/* 默认灯效 v2：整板中央一条音频波形；彩色马赛克和灯珠使用相同的扩散颜色。 */
const {ctx, width, height, keys, leds} = M18FX;
const waves = [];
let now = 0;
const clamp = v => Math.max(0, Math.min(1, v));
const middle = keys.filter(p => p.screen && p.key >= 6 && p.key <= 10);
const lineY = middle.reduce((sum, p) => sum + p.y, 0) / middle.length;
const lineLeft = Math.min(...middle.map(p => p.x - p.size / 2)) + 8;
const lineRight = Math.max(...middle.map(p => p.x + p.size / 2)) - 8;
function rgb(h, v) {
  return [0, 2 / 3, 1 / 3].map(offset => {
    const x = ((h + offset) % 1) * 6;
    return Math.round(255 * v * clamp(Math.abs(x - 3) - 1));
  });
}
M18FX.on('key', event => {
  if (!event.down) return;
  waves.push({x: event.x, y: event.y, start: event.time / 1000, hue: (event.key * 0.137 + now * 0.05) % 1});
  if (waves.length > 8) waves.shift();
});
function waveAt(x, y) {
  let best = 0, hue = 0.55;
  for (const wave of waves) {
    const age = Math.max(0, now - wave.start), radius = age * 650;
    const distance = Math.hypot(x - wave.x, y - wave.y);
    const value = Math.exp(-(((distance - radius) / 65) ** 2)) * clamp(1 - age / 1.8);
    if (value > best) { best = value; hue = wave.hue + distance / 1800; }
  }
  return {value: best, hue};
}
M18FX.on('frame', ({time}) => {
  now = time;
  while (waves.length && now - waves[0].start > 1.8) waves.shift();
  const audio = M18FX.audio, level = audio.available ? audio.level : 0;
  const bands = audio.available ? audio.bands : [0, 0, 0, 0, 0, 0, 0, 0];
  ctx.fillStyle = '#030713'; ctx.fillRect(0, 0, width, height);
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#0a1a2d';
  for (let x = 70; x <= 930; x += 43) {
    ctx.beginPath(); ctx.moveTo(x, 70); ctx.lineTo(x, 690); ctx.stroke();
  }
  for (let y = 70; y <= 690; y += 31) {
    ctx.beginPath(); ctx.moveTo(70, y); ctx.lineTo(930, y); ctx.stroke();
  }
  // 边框固定，不让15个屏幕都跟随音频变化；音频只影响中间一排。
  ctx.strokeStyle = 'rgba(20,170,230,0.22)';
  for (const key of keys.filter(p => p.screen)) {
    const half = key.size / 2;
    ctx.strokeRect(key.x - half + 3, key.y - half + 3, key.size - 6, key.size - 6);
  }
  // 只有一条连续波形，穿过整板中央五块屏幕；静音时是静止直线。
  ctx.strokeStyle = '#38bfe6'; ctx.beginPath();
  for (let i = 0; i <= 96; i++) {
    const x = lineLeft + i * (lineRight - lineLeft) / 96;
    const band = bands[Math.min(7, Math.floor(i * 8 / 97))] || 0;
    const y = lineY + Math.sin(time * 13 + i * 0.48) * (band * 27 + level * 10);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  if (waves.length) for (let y = 82; y < 690; y += 24) for (let x = 82; x < 930; x += 24) {
    const wave = waveAt(x, y);
    if (wave.value < 0.015) continue;
    const c = rgb(wave.hue, wave.value);
    ctx.fillStyle = `rgb(${c.join(',')})`;
    const size = 8 + wave.value * 13;
    ctx.fillRect(x - size / 2, y - size / 2, size, size);
  }
  const colors = leds.map(led => {
    const wave = waveAt(led.x, led.y);
    // 与马赛克同色、同一扩散函数；不叠加常亮底色或独立的音频灯效。
    const color = wave.value >= 0.015 ? rgb(wave.hue, wave.value) : [0, 0, 0];
    ctx.fillStyle = `rgb(${color.join(',')})`;
    ctx.fillRect(led.x - 5, led.y - 5, 10, 10);
    return color;
  });
  M18FX.render(colors);
});
M18FX.log('中央音频波形与彩色马赛克已加载：按 1–18 键扩散，灯珠同色，无效果时关闭。');
