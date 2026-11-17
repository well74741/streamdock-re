/* 默认灯效：深色科幻线条、电脑播放音频抖动、按键向外扩散彩色马赛克。 */
const {ctx, width, height, keys, leds} = M18FX;
const waves = [];
let now = 0;
const clamp = v => Math.max(0, Math.min(1, v));
function rgb(h, v) {
  const c = [0, 2 / 3, 1 / 3].map(offset => {
    const x = ((h + offset) % 1) * 6;
    return Math.round(255 * v * clamp(Math.abs(x - 3) - 1));
  });
  return c;
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
  ctx.fillStyle = '#030713'; ctx.fillRect(0, 0, width, height);
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#0a1a2d';
  for (let x = 70; x <= 930; x += 43) {
    ctx.beginPath(); ctx.moveTo(x, 70); ctx.lineTo(x, 690); ctx.stroke();
  }
  for (let y = 70; y <= 690; y += 31) {
    ctx.beginPath(); ctx.moveTo(70, y); ctx.lineTo(930, y); ctx.stroke();
  }
  for (const key of keys.filter(p => p.screen)) {
    const half = key.size / 2, band = audio.bands[(key.key - 1) % 8] || 0;
    ctx.strokeStyle = `rgba(20,170,230,${0.2 + band * 0.6})`;
    ctx.strokeRect(key.x - half + 3, key.y - half + 3, key.size - 6, key.size - 6);
    ctx.strokeStyle = '#38bfe6'; ctx.beginPath();
    for (let i = 0; i < 25; i++) {
      const x = key.x - half + 8 + i * (key.size - 16) / 24;
      const jitter = Math.sin(time * 13 + i * 1.9 + key.key) * (2 + band * 27 + level * 10);
      const y = key.y + jitter;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#1b7198';
    ctx.fillRect(key.x - half + 8, key.y + half - 13, (key.size - 16) * (0.06 + band * 0.94), 3);
  }
  // 每个空间单元固定一个块；以距离而非键号传播，导航键也能激发同一波纹。
  if (waves.length) for (let y = 82; y < 690; y += 24) for (let x = 82; x < 930; x += 24) {
    const wave = waveAt(x, y);
    if (wave.value < 0.015) continue;
    const c = rgb(wave.hue, wave.value);
    ctx.fillStyle = `rgb(${c.join(',')})`;
    const size = 8 + wave.value * 13;
    ctx.fillRect(x - size / 2, y - size / 2, size, size);
  }
  const colors = leds.map((led, i) => {
    const wave = waveAt(led.x, led.y), band = audio.bands[i % 8] || 0;
    const base = rgb(0.53 + 0.025 * Math.sin(time + i), 0.035 + band * 0.35 + level * 0.1);
    const pulse = rgb(wave.hue, wave.value);
    const color = base.map((v, ch) => Math.min(255, v + pulse[ch]));
    ctx.fillStyle = `rgb(${color.join(',')})`;
    ctx.fillRect(led.x - 5, led.y - 5, 10, 10);
    return color;
  });
  M18FX.render(colors);
});
M18FX.log('科幻音频波纹已加载：电脑播放声音驱动线条，按 1–18 键产生彩色马赛克。');
