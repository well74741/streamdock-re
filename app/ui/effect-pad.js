/* 默认灯效 v5：星空闪烁背景 + 音频波形铺满三排屏幕（三股叠加）；彩色马赛克和灯珠使用相同的扩散颜色。 */
const {ctx, width, height, keys, leds} = M18FX;
const waves = [];
let now = 0;
const clamp = v => Math.max(0, Math.min(1, v));
const screens = keys.filter(p => p.screen);
const cell = screens[0].size;
const rowTop = Math.min(...screens.map(p => p.y)), rowBottom = Math.max(...screens.map(p => p.y));
const lineY = (rowTop + rowBottom) / 2;
// 最大振幅约为上一版的一半（够到上下两排屏幕的边缘），静音时是一条直线
const maxAmp = ((rowBottom - rowTop) / 2 + cell * 0.38) * 0.5;
const lineLeft = Math.min(...screens.map(p => p.x - p.size / 2)) + 6;
const lineRight = Math.max(...screens.map(p => p.x + p.size / 2)) - 6;
const POINTS = 220;
const smooth = Array(8).fill(0);   // 快起慢落，避免抖成碎线
// 星星：固定种子，每次加载位置一样；只撒在 15 块屏幕上（键缝被按键框挡住看不到）
let seed = 20261117;
const random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const stars = [];
for (const key of screens) for (let i = 0; i < 6; i++) {
  stars.push({x: key.x + (random() - 0.5) * key.size * 0.9, y: key.y + (random() - 0.5) * key.size * 0.9,
    speed: 1.2 + random() * 2.8, phase: random() * Math.PI * 2, size: 2.5 + random() * 3,
    tint: random() < 0.25 ? [255, 214, 150] : random() < 0.5 ? [170, 205, 255] : [235, 245, 255]});
}
function drawStars(time, level) {
  for (const star of stars) {
    // 尖峰型闪烁：大部分时间是暗淡小点，偶尔亮起带十字星芒
    const pulse = Math.max(0, Math.sin(time * star.speed + star.phase));
    const glow = 0.18 + 0.82 * pulse ** 6 + level * 0.25;
    const a = Math.min(1, glow), [r, g, b] = star.tint;
    ctx.fillStyle = `rgba(${r},${g},${b},${a.toFixed(3)})`;
    const s = star.size * (0.7 + 0.5 * pulse ** 6);
    ctx.fillRect(star.x - s / 2, star.y - s / 2, s, s);
    if (pulse ** 6 > 0.35) {
      const arm = s * (1.5 + 2.5 * pulse ** 6);
      ctx.fillStyle = `rgba(${r},${g},${b},${(a * 0.55).toFixed(3)})`;
      ctx.fillRect(star.x - arm, star.y - 1, arm * 2, 2);
      ctx.fillRect(star.x - 1, star.y - arm, 2, arm * 2);
    }
  }
}
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
function bandAt(u) {
  // 8 个频带在横向上线性插值，振幅沿整条线连续变化
  const f = u * 7, i = Math.min(6, Math.floor(f)), t = f - i;
  return smooth[i] * (1 - t) + smooth[i + 1] * t;
}
function strand(time, gain, speed, density, phase, color, lineWidth, level) {
  ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.beginPath();
  for (let i = 0; i <= POINTS; i++) {
    const u = i / POINTS, x = lineLeft + u * (lineRight - lineLeft);
    const envelope = 0.45 + 0.55 * Math.sin(Math.PI * u);
    const amp = clamp(bandAt(u) * 1.7 + level * 0.6) * maxAmp * gain * envelope;
    const y = lineY + Math.sin(time * speed + i * density + phase) * amp
      + Math.sin(time * speed * 0.6 + i * density * 2.3 + phase) * amp * 0.25;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
}
M18FX.on('frame', ({time}) => {
  now = time;
  while (waves.length && now - waves[0].start > 1.8) waves.shift();
  const audio = M18FX.audio, level = audio.available ? audio.level : 0;
  const bands = audio.available ? audio.bands : [0, 0, 0, 0, 0, 0, 0, 0];
  for (let i = 0; i < 8; i++) smooth[i] = Math.max(bands[i] || 0, smooth[i] * 0.82);
  ctx.fillStyle = '#030713'; ctx.fillRect(0, 0, width, height);
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#0a1a2d';
  for (let x = 70; x <= 930; x += 43) {
    ctx.beginPath(); ctx.moveTo(x, 70); ctx.lineTo(x, 690); ctx.stroke();
  }
  for (let y = 70; y <= 690; y += 31) {
    ctx.beginPath(); ctx.moveTo(70, y); ctx.lineTo(930, y); ctx.stroke();
  }
  drawStars(time, level);
  // 边框固定，不随音频变化。
  ctx.strokeStyle = 'rgba(20,170,230,0.22)';
  for (const key of screens) {
    const half = key.size / 2;
    ctx.strokeRect(key.x - half + 3, key.y - half + 3, key.size - 6, key.size - 6);
  }
  // 三股叠加的音频波形横穿三排屏幕：外层光晕 + 主线 + 两股反相细线（缩到 64px 屏上仍清楚）。
  strand(time, 1, 11, 0.252, 0, 'rgba(56,191,230,0.22)', 12, level);
  strand(time, 0.62, 15, 0.364, Math.PI, 'rgba(150,90,255,0.8)', 3.5, level);
  strand(time, 0.38, 19, 0.49, Math.PI / 2, 'rgba(120,255,220,0.7)', 2.5, level);
  strand(time, 1, 11, 0.252, 0, '#38bfe6', 5, level);
  ctx.lineWidth = 1;
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
M18FX.log('星空、三排音频波形与彩色马赛克已加载：按 1–18 键扩散，灯珠同色，无效果时关闭。');
