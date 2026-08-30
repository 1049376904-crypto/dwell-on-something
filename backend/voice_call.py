"""dwell 语音通话：对讲机模式。

点电话进全屏，球跟着你的音量涨落；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
听写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层（流式输出 + 边出字边分句合成 +
边说边转写 + 打断处理），那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

球是 Canvas 2D 画的，四个状态之间弹簧插值。改主题只动 THEMES；
调参时可以在控制台 `dwellCall.phase('speaking')` 手动切态看效果。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from voice_feature import _voice_token

CLIENT_SCRIPT = r"""
<style>
#vcall{position:fixed;inset:0;z-index:9999;display:none;
  background:
    radial-gradient(130% 100% at 50% 8%, #14161d 0%, #0d0e14 45%, #0a0b0f 100%);
  color:#eceaf4;-webkit-user-select:none;user-select:none;
  -webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif}
#vcall.on{display:grid;grid-template-rows:auto 1fr auto}

/* 顶部：细小的胶囊标签，稀疏地排一行 */
.vc-top{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap;
  padding:calc(env(safe-area-inset-top,0px) + 22px) 20px 0}
.vc-tag{display:inline-flex;align-items:center;gap:6px;
  padding:5px 11px;border-radius:999px;
  background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.07);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  font-size:clamp(9.5px,2.4vw,11px);letter-spacing:.13em;text-transform:uppercase;
  color:rgba(236,234,244,.5);font-variant-numeric:tabular-nums;white-space:nowrap}
.vc-dot{width:5px;height:5px;border-radius:50%;background:#5ee0a8;flex:0 0 auto;
  box-shadow:0 0 7px rgba(94,224,168,.75);
  transition:background .5s ease,box-shadow .5s ease}
#vcall.thinking .vc-dot{background:#c9b26e;box-shadow:0 0 7px rgba(201,178,110,.75)}
#vcall.speaking .vc-dot{background:#8b7cff;box-shadow:0 0 7px rgba(139,124,255,.8)}
.vc-bars{display:inline-flex;align-items:flex-end;gap:1.5px;height:9px}
.vc-bars i{width:2px;background:currentColor;opacity:.26;border-radius:1px;
  transition:opacity .4s ease}
.vc-bars i:nth-child(1){height:3px}
.vc-bars i:nth-child(2){height:5px}
.vc-bars i:nth-child(3){height:7px}
.vc-bars i:nth-child(4){height:9px}
.vc-bars.s1 i:nth-child(1),.vc-bars.s2 i:nth-child(-n+2),
.vc-bars.s3 i:nth-child(-n+3),.vc-bars.s4 i{opacity:.9}

/* 中间：球 + 字幕，慷慨留白 */
.vc-mid{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:clamp(18px,4.6vh,42px);min-height:0;padding:0 20px}
.vc-stage{position:relative;flex:0 0 auto;display:block}
.vc-stage canvas{display:block;width:100%;height:100%}

.vc-cap{text-align:center;max-width:min(74vw,430px);min-height:3.6em}
.vc-state{font-size:clamp(10px,2.6vw,11.5px);letter-spacing:.2em;text-transform:uppercase;
  color:rgba(236,234,244,.34);margin-bottom:11px;
  transition:color .5s cubic-bezier(.22,.61,.36,1)}
#vcall.speaking .vc-state{color:rgba(155,141,255,.66)}
.vc-said{font-size:clamp(14px,3.7vw,16.5px);line-height:1.85;font-weight:200;
  letter-spacing:.028em;color:rgba(236,234,244,.72);word-break:break-word}
.vc-said b{font-weight:200;opacity:0;animation:vcin .5s cubic-bezier(.22,.61,.36,1) forwards}
@keyframes vcin{to{opacity:1}}
.vc-said.dim{color:rgba(236,234,244,.3)}

/* 底部：两颗玻璃按钮 */
.vc-btns{display:flex;justify-content:center;align-items:center;
  gap:clamp(22px,7vw,34px);
  padding:0 20px calc(env(safe-area-inset-bottom,0px) + clamp(30px,6vh,54px))}
.vc-btn{-webkit-appearance:none;appearance:none;
  width:clamp(56px,15vw,64px);height:clamp(56px,15vw,64px);
  border-radius:50%;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  color:#eceaf4;background:rgba(255,255,255,.055);
  border:1px solid rgba(255,255,255,.085);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  transition:transform .42s cubic-bezier(.34,1.42,.5,1),
             background .3s ease,opacity .3s ease}
.vc-btn:active{transform:scale(.9)}
.vc-btn:focus-visible{outline:2px solid rgba(139,124,255,.75);outline-offset:3px}
.vc-btn.mute.off{opacity:.36;background:rgba(255,255,255,.025)}
.vc-btn.hang{background:rgba(214,71,62,.9);border-color:rgba(255,255,255,.12);
  box-shadow:0 6px 26px rgba(214,71,62,.3)}

#vcBtn{display:inline-flex;align-items:center;justify-content:center}

@media (prefers-reduced-motion:reduce){
  .vc-said b{animation:none;opacity:1}
  .vc-btn,.vc-dot,.vc-bars i{transition:none}
}
</style>
<script>
(function () {
  if (window.dwellCall) return;
  var TOKEN = '__VOICE_TOKEN__';
  var CALL_HINT = '[通话中]';

  /* ── 主题：改色只动这里 ─────────────────────────────────────────
     h 起始色相，h2 漂移到哪，s 饱和，rim 边缘亮度。 */
  var THEMES = {
    violet: { h: 252, h2: 274, s: 62, rim: 188 },
    teal:   { h: 188, h2: 205, s: 58, rim: 190 }
  };
  var TH = THEMES.violet;

  /* ── 状态机：每态一组目标值，帧间弹簧插值，不许生硬跳变 ───────
     调参就改这张表：scale 基准大小，breathe 呼吸幅度，period 周期，
     glow 辉光，sat 饱和倍率，emit 粒子量，ripple 涟漪开关，
     jitter 抖动，spin 光斑转速。 */
  var SPEC = {
    idle:      { scale: 1.00, breathe: .040, period: 5500, glow: .30, sat: .52,
                 emit: .10, ripple: 0, jitter: 0, spin: .10, label: 'Idle' },
    listening: { scale: 0.945, breathe: .006, period: 4200, glow: .48, sat: .66,
                 emit: .40, ripple: 1, jitter: 0, spin: .16, label: 'Listening' },
    thinking:  { scale: 0.985, breathe: .010, period: 1500, glow: .46, sat: .60,
                 emit: .30, ripple: 0, jitter: .9, spin: .70, label: 'Thinking' },
    speaking:  { scale: 1.085, breathe: .092, period: 1150, glow: 1.0, sat: .92,
                 emit: 1.0, ripple: 0, jitter: 0, spin: .30, label: 'Speaking' }
  };

  var box, cv, ctx, stage, stateEl, saidEl, metaEl, timeEl, barsEl, muteBtn, modelEl;
  var live = false, phase = 'idle', muted = false;
  var micStream = null, rec = null, chunks = [], recT0 = 0;
  var ac = null, an = null, meterRaf = 0, silentSince = 0, spoke = false;
  var player = null, chars = 0, turns = 0, stopAll = false, dialT0 = 0;

  var MIN_MS = 700;      // 太短的当噪音扔掉
  var MAX_MS = 30000;    // 一轮封顶，别让它录到天亮
  var HUSH_MS = 1400;    // 安静这么久就算说完了
  var HUSH_LVL = 0.055;  // 低于这个音量算安静

  var slow = false;
  try { slow = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}

  function hdr(extra) {
    var h = extra || {};
    if (TOKEN) h['X-Voice-Token'] = TOKEN;
    return h;
  }
  function fmt(s) {
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  /* ══ 渲染层 ═══════════════════════════════════════════════════ */
  var V = { scale: 1, glow: .3, sat: .52, emit: .1, ripple: 0,
            spin: .1, level: 0, hue: 0 };
  var parts = [], ripples = [], noiseTile = null;
  var drawRaf = 0, t0 = 0, lastRip = 0, W = 0, H = 0, R = 0, DPR = 1;

  function lerp(a, b, k) { return a + (b - a) * k; }

  /* 噪点：预渲染一小块反复铺，不每帧算随机数。
     这一层是"不塑料"的关键 —— 压掉渐变过于平滑的塑料感。 */
  function buildNoise() {
    var n = document.createElement('canvas');
    n.width = n.height = 96;
    var c = n.getContext('2d');
    var img = c.createImageData(96, 96);
    for (var i = 0; i < img.data.length; i += 4) {
      var v = 128 + (Math.random() - .5) * 168;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 26;
    }
    c.putImageData(img, 0, 0);
    noiseTile = n;
  }

  function fit() {
    if (!cv || !stage) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    // 球占屏高 40~50%；画布比球大一圈，给辉光和粒子留地方
    var side = Math.min(vw * 0.92, vh * 0.56, 560);
    stage.style.width = side + 'px';
    stage.style.height = side + 'px';
    DPR = Math.min(window.devicePixelRatio || 1, 2.5);
    cv.width = Math.round(side * DPR);
    cv.height = Math.round(side * DPR);
    W = cv.width; H = cv.height;
    R = Math.min(W, H) * 0.276;
  }
  var fitTimer = null;
  function fitLater() {                     // 节流 resize
    if (fitTimer) return;
    fitTimer = setTimeout(function () { fitTimer = null; fit(); }, 140);
  }

  function emitParticle(now) {
    parts.push({
      a: Math.random() * Math.PI * 2,
      r: R * (0.96 + Math.random() * 0.10),
      vr: R * (0.0022 + Math.random() * 0.0072),
      va: (Math.random() - .5) * 0.0042,
      sz: DPR * (0.5 + Math.random() * 1.55),
      born: now,
      life: 1100 + Math.random() * 2100,
      hue: (Math.random() - .5) * 26
    });
  }

  function frame(now) {
    drawRaf = requestAnimationFrame(frame);
    if (!ctx || !live) return;

    var s = SPEC[phase] || SPEC.idle;
    var k = slow ? 1 : 0.075;                  // 弹簧插值系数
    var t = now - t0;

    // 呼吸：正弦叠在基准 scale 上；listening 时改由音量驱动
    var want = s.scale + Math.sin(t / s.period * Math.PI * 2) * s.breathe;
    if (phase === 'listening') want = s.scale + Math.min(0.26, V.level * 1.9);
    if (phase === 'thinking' && !slow) want += Math.sin(t / 92) * 0.006 * s.jitter;

    V.scale = lerp(V.scale, want, k);
    V.glow = lerp(V.glow, s.glow, k * .8);
    V.sat = lerp(V.sat, s.sat, k * .8);
    V.emit = lerp(V.emit, s.emit, k);
    V.ripple = lerp(V.ripple, s.ripple, k);
    V.spin = lerp(V.spin, s.spin, k);
    V.hue += V.spin * (phase === 'speaking' ? .22 : .05);

    var cx = W / 2, cy = H / 2, r = R * V.scale;
    var hue = TH.h + Math.sin(V.hue / 42) * (TH.h2 - TH.h) * .5;
    var sat = Math.round(TH.s * V.sat);

    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';

    // ── 外辉光：大而弱，两段
    var g0 = ctx.createRadialGradient(cx, cy, r * .55, cx, cy, r * 2.75);
    g0.addColorStop(0, 'hsla(' + hue + ',' + sat + '%,64%,' + (0.20 * V.glow).toFixed(3) + ')');
    g0.addColorStop(0.42, 'hsla(' + (hue + 14) + ',' + sat + '%,52%,' + (0.075 * V.glow).toFixed(3) + ')');
    g0.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,42%,0)');
    ctx.fillStyle = g0;
    ctx.fillRect(0, 0, W, H);

    // ── 涟漪：listening 时一圈圈往外扩
    if (V.ripple > .02 && !slow) {
      if (phase === 'listening' && now - lastRip > 760) {
        ripples.push(now);
        lastRip = now;
      }
      for (var i = ripples.length - 1; i >= 0; i--) {
        var age = (now - ripples[i]) / 2500;
        if (age >= 1) { ripples.splice(i, 1); continue; }
        ctx.beginPath();
        ctx.arc(cx, cy, r * (1 + age * 1.06), 0, Math.PI * 2);
        ctx.strokeStyle = 'hsla(' + (hue + 8) + ',' + sat + '%,72%,' +
          ((1 - age) * 0.24 * V.ripple).toFixed(3) + ')';
        ctx.lineWidth = DPR * (1 - age * .55);
        ctx.stroke();
      }
    }

    // ── 边缘粒子：从球面往外飘，两头淡
    if (!slow) {
      var quota = V.emit * (2.2 + V.level * 9);
      while (quota-- > 0) if (Math.random() < .82) emitParticle(now);
      for (var p = parts.length - 1; p >= 0; p--) {
        var q = parts[p];
        var a2 = (now - q.born) / q.life;
        if (a2 >= 1) { parts.splice(p, 1); continue; }
        q.r += q.vr * (1 + V.level * 2.4);
        q.a += q.va;
        var fade = a2 < .16 ? a2 / .16 : (1 - a2) / .84;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(q.a) * q.r * V.scale,
                cy + Math.sin(q.a) * q.r * V.scale, q.sz, 0, Math.PI * 2);
        ctx.fillStyle = 'hsla(' + (hue + q.hue) + ',' + (sat + 12) + '%,' +
          (68 + V.glow * 16) + '%,' + (fade * 0.52 * (.4 + V.glow * .6)).toFixed(3) + ')';
        ctx.fill();
      }
    }

    ctx.globalCompositeOperation = 'source-over';

    // ── 球体：四段径向渐变，光源偏左上
    var lx = cx - r * .34, ly = cy - r * .40;
    var g1 = ctx.createRadialGradient(lx, ly, r * .04, cx, cy, r);
    g1.addColorStop(0, 'hsl(' + (hue + 10) + ',' + Math.round(sat * .5) + '%,' +
      Math.round(30 + V.glow * 26) + '%)');
    g1.addColorStop(0.34, 'hsl(' + hue + ',' + Math.round(sat * .62) + '%,' +
      Math.round(17 + V.glow * 12) + '%)');
    g1.addColorStop(0.74, 'hsl(' + (hue + 6) + ',' + Math.round(sat * .5) + '%,8%)');
    g1.addColorStop(1, 'hsl(' + (hue + 12) + ',30%,4%)');
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = g1;
    ctx.fill();

    ctx.save();
    ctx.clip();

    // 内发光：贴着边缘往里收，给它体积
    var g2 = ctx.createRadialGradient(cx, cy, r * .52, cx, cy, r);
    g2.addColorStop(0, 'hsla(' + hue + ',' + sat + '%,60%,0)');
    g2.addColorStop(0.82, 'hsla(' + (hue + 10) + ',' + sat + '%,' +
      Math.round(TH.rim / 3) + '%,' + (0.16 * V.glow).toFixed(3) + ')');
    g2.addColorStop(1, 'hsla(' + (hue + 16) + ',' + (sat + 14) + '%,74%,' +
      (0.34 * V.glow).toFixed(3) + ')');
    ctx.fillStyle = g2;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    // 高光：一片折射，不是塑料球那种小圆点
    var g3 = ctx.createRadialGradient(lx, ly, 0, lx, ly, r * .66);
    g3.addColorStop(0, 'hsla(' + (hue + 22) + ',60%,92%,' + (0.20 + V.glow * .16).toFixed(3) + ')');
    g3.addColorStop(0.5, 'hsla(' + (hue + 18) + ',55%,80%,' + (0.05 * V.glow).toFixed(3) + ')');
    g3.addColorStop(1, 'hsla(' + hue + ',50%,70%,0)');
    ctx.fillStyle = g3;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    // thinking 时一颗光斑绕着球面游
    if (V.spin > .3) {
      var sa = t / 620, sr = r * .5;
      var sx = cx + Math.cos(sa) * sr, sy = cy + Math.sin(sa * 1.3) * sr * .72;
      var g4 = ctx.createRadialGradient(sx, sy, 0, sx, sy, r * .44);
      g4.addColorStop(0, 'hsla(' + (hue + 30) + ',70%,86%,' +
        (0.20 * Math.min(1, V.spin * 1.4)).toFixed(3) + ')');
      g4.addColorStop(1, 'hsla(' + hue + ',60%,70%,0)');
      ctx.fillStyle = g4;
      ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }

    // 噪点铺满球面
    if (noiseTile) {
      ctx.globalAlpha = 0.5;
      ctx.globalCompositeOperation = 'overlay';
      for (var nx = cx - r; nx < cx + r; nx += 96) {
        for (var ny = cy - r; ny < cy + r; ny += 96) ctx.drawImage(noiseTile, nx, ny);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }
    ctx.restore();

    // 边缘那道细亮线
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = 'hsla(' + (hue + 14) + ',' + (sat + 10) + '%,78%,' +
      (0.13 + V.glow * .17).toFixed(3) + ')';
    ctx.lineWidth = DPR;
    ctx.stroke();

    if (timeEl && dialT0) timeEl.textContent = fmt((Date.now() - dialT0) / 1000);
  }

  function startDraw() {
    if (drawRaf) return;
    if (!noiseTile) buildNoise();
    t0 = performance.now();
    drawRaf = requestAnimationFrame(frame);
  }
  function stopDraw() {
    if (drawRaf) { cancelAnimationFrame(drawRaf); drawRaf = 0; }
    parts = [];
    ripples = [];
  }

  /* ══ 界面 ═════════════════════════════════════════════════════ */
  function setPhase(p, label) {
    phase = p;
    if (!box) return;
    box.classList.remove('listening', 'thinking', 'speaking');
    if (p !== 'idle') box.classList.add(p);
    if (stateEl) stateEl.textContent = label != null ? label : (SPEC[p] || SPEC.idle).label;
  }

  /* 字幕逐字浮现。整段一次性塞进去只是"出现"，不是"说出来" */
  var typeTimer = null;
  function say(text, dim) {
    if (!saidEl) return;
    if (typeTimer) { clearInterval(typeTimer); typeTimer = null; }
    saidEl.classList.toggle('dim', !!dim);
    saidEl.textContent = '';
    text = text || '';
    if (!text) return;
    if (slow) { saidEl.textContent = text; return; }
    var i = 0;
    typeTimer = setInterval(function () {
      if (i >= text.length) { clearInterval(typeTimer); typeTimer = null; return; }
      var b = document.createElement('b');
      b.textContent = text[i++];
      saidEl.appendChild(b);
    }, 34);
  }

  function meta() {
    if (metaEl) metaEl.textContent = turns + ' 轮 · ' + chars + ' 字符';
  }
  function signal(n) {
    if (barsEl) barsEl.className = 'vc-bars s' + Math.max(1, Math.min(4, n));
  }

  function build() {
    if (box) return;
    box = document.createElement('div');
    box.id = 'vcall';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', '语音通话');
    box.innerHTML =
      '<div class="vc-top">' +
        '<span class="vc-tag"><span class="vc-dot"></span><span class="vc-conn">Connected</span></span>' +
        '<span class="vc-tag vc-model">—</span>' +
        '<span class="vc-tag"><span class="vc-time">0:00</span></span>' +
        '<span class="vc-tag"><span class="vc-meta">0 轮 · 0 字符</span>' +
          '<span class="vc-bars s3"><i></i><i></i><i></i><i></i></span></span>' +
      '</div>' +
      '<div class="vc-mid">' +
        '<div class="vc-stage"><canvas aria-hidden="true"></canvas></div>' +
        '<div class="vc-cap">' +
          '<div class="vc-state" aria-live="polite"></div>' +
          '<div class="vc-said"></div>' +
        '</div>' +
      '</div>' +
      '<div class="vc-btns">' +
        '<button type="button" class="vc-btn mute" data-vc="mute" aria-label="静音">' +
          '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.6" stroke-linecap="round" aria-hidden="true">' +
          '<rect x="9" y="3" width="6" height="11" rx="3"/>' +
          '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/></svg></button>' +
        '<button type="button" class="vc-btn hang" data-vc="hang" aria-label="挂断">' +
          '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.6" stroke-linecap="round" aria-hidden="true">' +
          '<path d="M3 8.5c6-4 12-4 18 0l-2.6 3.2-3.5-1.2-.6-2.4c-2.5-.7-5.1-.7-7.6 0' +
          'l-.6 2.4-3.5 1.2z"/></svg></button>' +
      '</div>';
    document.body.appendChild(box);

    stage = box.querySelector('.vc-stage');
    cv = box.querySelector('canvas');
    ctx = cv.getContext('2d');
    stateEl = box.querySelector('.vc-state');
    saidEl = box.querySelector('.vc-said');
    metaEl = box.querySelector('.vc-meta');
    timeEl = box.querySelector('.vc-time');
    barsEl = box.querySelector('.vc-bars');
    modelEl = box.querySelector('.vc-model');
    muteBtn = box.querySelector('[data-vc="mute"]');

    box.querySelector('[data-vc="hang"]').onclick = hang;
    muteBtn.onclick = function () {
      muted = !muted;
      muteBtn.classList.toggle('off', muted);
      muteBtn.setAttribute('aria-label', muted ? '取消静音' : '静音');
      if (micStream) micStream.getTracks().forEach(function (t) { t.enabled = !muted; });
    };
    window.addEventListener('resize', fitLater);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', fitLater);
    fit();

    fetch('api/model', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (modelEl && d.model) modelEl.textContent = d.model; })
      .catch(function () {});
  }

  /* ══ 音量表：球跟着涨落，同时用它判断你说完了没 ══════════════ */
  function startMeter() {
    if (!micStream || !window.AudioContext || an) return;
    try {
      ac = new AudioContext();
      an = ac.createAnalyser();
      an.fftSize = 512;
      ac.createMediaStreamSource(micStream).connect(an);
      var buf = new Uint8Array(an.fftSize);
      var loop = function () {
        if (!an) return;
        meterRaf = requestAnimationFrame(loop);
        an.getByteTimeDomainData(buf);
        var sum = 0;
        for (var i = 0; i < buf.length; i++) {
          var v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        var lvl = Math.sqrt(sum / buf.length);
        V.level = V.level * .72 + lvl * .28;
        if (phase !== 'listening' || muted) return;
        var now = Date.now();
        if (lvl > HUSH_LVL) { spoke = true; silentSince = 0; }
        else if (spoke) {
          if (!silentSince) silentSince = now;
          else if (now - silentSince > HUSH_MS) turnDone();
        }
        if (now - recT0 > MAX_MS) turnDone();
      };
      meterRaf = requestAnimationFrame(loop);
    } catch (e) {}
  }
  function stopMeter() {
    if (meterRaf) { cancelAnimationFrame(meterRaf); meterRaf = 0; }
    an = null;
    V.level = 0;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
  }

  /* ══ 一轮：听 → 传 → 发 → 等 → 念 ═══════════════════════════ */
  function pickMime() {
    var c = ['audio/mp4', 'audio/mp4;codecs=mp4a.40.2', 'audio/aac',
             'audio/webm;codecs=opus', 'audio/webm'];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < c.length; i++) if (MediaRecorder.isTypeSupported(c[i])) return c[i];
    return '';
  }
  function extOf(m) {
    m = m || '';
    if (m.indexOf('webm') >= 0) return '.webm';
    if (m.indexOf('ogg') >= 0) return '.ogg';
    return '.m4a';
  }

  function listen() {
    if (!live || stopAll) return;
    var mime = pickMime();
    try {
      rec = mime ? new MediaRecorder(micStream, { mimeType: mime })
                 : new MediaRecorder(micStream);
    } catch (e) {
      try { rec = new MediaRecorder(micStream); } catch (e2) { hang(); return; }
    }
    chunks = [];
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    try { rec.start(200); } catch (e) { hang(); return; }
    recT0 = Date.now();
    spoke = false;
    silentSince = 0;
    setPhase('listening');
    say('', true);
    startMeter();
  }

  function turnDone() {
    if (phase !== 'listening') return;
    setPhase('thinking');
    var dur = Math.round((Date.now() - recT0) / 1000);
    var mime = rec ? (rec.mimeType || '') : '';
    var r = rec;
    rec = null;
    new Promise(function (res) {
      if (!r || r.state === 'inactive') return res();
      r.onstop = res;
      try { r.stop(); } catch (e) { res(); }
    }).then(function () {
      var blob = new Blob(chunks, { type: mime || 'audio/mp4' });
      chunks = [];
      if (Date.now() - recT0 < MIN_MS || !blob.size) { listen(); return; }
      return sendTurn(blob, mime, Math.max(1, dur));
    }).catch(function () { if (live) listen(); });
  }

  async function lastSeq() {
    try {
      var r = await fetch('api/messages?limit=1', { cache: 'no-store' });
      var d = await r.json();
      var m = d.msgs || [];
      return m.length ? m[m.length - 1].seq : 0;
    } catch (e) { return 0; }
  }

  async function waitReply(base) {
    var deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      if (stopAll || !live) return '';
      await new Promise(function (r) { setTimeout(r, 900); });
      try {
        var r = await fetch('api/messages?limit=6', { cache: 'no-store' });
        var d = await r.json();
        var msgs = d.msgs || [];
        for (var i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].kind === 'gu' && msgs[i].seq > base) return msgs[i].text || '';
        }
      } catch (e) {}
    }
    return '';
  }

  async function sendTurn(blob, mime, dur) {
    setPhase('thinking');
    var t = Date.now();
    var fd = new FormData();
    fd.append('file', blob, 'voice' + extOf(mime));
    fd.append('duration', String(dur));
    var line = '[voice · ' + fmt(dur) + ']';
    try {
      var up = await fetch('api/voice/message', { method: 'POST', headers: hdr(), body: fd });
      if (up.ok) {
        var d = await up.json();
        if (d.message) line = d.message;
      }
      var ms = Date.now() - t;
      signal(ms < 500 ? 4 : ms < 1200 ? 3 : ms < 2600 ? 2 : 1);
    } catch (e) { signal(1); }

    // 手机自带的听写在通话里不跑（它跟录音抢同一个音频会话），
    // 所以这行多半只有时长。模型看得懂 [通话中] 那行，会问回来。
    var mine = line.replace(/^\[voice[^\]]*\]\s*/, '');
    say(mine || '说了 ' + fmt(dur), true);

    var base = await lastSeq();
    var boxEl = document.getElementById('box');
    var sendBtn = document.getElementById('send');
    if (!boxEl || !sendBtn) { hang(); return; }
    boxEl.value = line + '\n' + CALL_HINT;
    boxEl.dispatchEvent(new Event('input', { bubbles: true }));
    sendBtn.click();
    turns += 1;
    meta();

    var reply = await waitReply(base);
    if (!live || stopAll) return;
    reply = (reply || '').replace(/^\[voice[^\]]*\]\s*/, '').trim();
    if (!reply) { listen(); return; }
    await speak(reply);
    if (live && !stopAll) listen();
  }

  async function speak(text) {
    setPhase('speaking');
    say(text);
    chars += text.length;
    meta();
    try {
      var r = await fetch('api/voice/tts', {
        method: 'POST',
        headers: hdr({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ text: text })
      });
      if (!r.ok) throw new Error('tts ' + r.status);
      var url = URL.createObjectURL(await r.blob());
      await new Promise(function (res) {
        player.onended = res;
        player.onerror = res;
        player.src = url;
        var p = player.play();
        if (p && p.catch) p.catch(function () { res(); });
      });
      try { URL.revokeObjectURL(url); } catch (e) {}
    } catch (e) {
      // 合成挂了就让设备自己念，别把电话弄断
      try {
        if (window.speechSynthesis) {
          await new Promise(function (res) {
            var u = new SpeechSynthesisUtterance(text);
            u.lang = 'zh-CN';
            u.onend = res;
            u.onerror = res;
            speechSynthesis.speak(u);
          });
        }
      } catch (e2) {}
    }
  }

  /* ══ 进出 ═════════════════════════════════════════════════════ */
  async function dial() {
    if (live) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    build();
    // iOS 只在用户手势里放开播放权限：借这一下点击把播放器解锁，
    // 之后整通电话复用它，不然模型第一句会哑在那儿。
    if (!player) {
      player = new Audio();
      player.setAttribute('playsinline', '');
    }
    try {
      player.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
      var p0 = player.play();
      if (p0 && p0.catch) p0.catch(function () {});
    } catch (e) {}

    box.classList.add('on');
    live = true;
    stopAll = false;
    turns = 0;
    chars = 0;
    muted = false;
    dialT0 = Date.now();
    muteBtn.classList.remove('off');
    meta();
    signal(3);
    fit();
    startDraw();
    setPhase('thinking', 'Connecting');
    say('', true);
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch (e) {
      setPhase('idle', 'No microphone');
      say('没拿到麦克风权限', true);
      setTimeout(hang, 1800);
      return;
    }
    listen();
  }

  function hang() {
    stopAll = true;
    live = false;
    stopMeter();
    stopDraw();
    if (typeTimer) { clearInterval(typeTimer); typeTimer = null; }
    if (rec && rec.state !== 'inactive') { try { rec.stop(); } catch (e) {} }
    rec = null;
    chunks = [];
    if (micStream) {
      micStream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) {} });
      micStream = null;
    }
    if (player) { try { player.pause(); } catch (e) {} }
    try { if (window.speechSynthesis) speechSynthesis.cancel(); } catch (e) {}
    dialT0 = 0;
    setPhase('idle', '');
    if (box) box.classList.remove('on');
  }

  function mountBtn() {
    if (document.getElementById('vcBtn')) return;
    var row = document.querySelector('.composer .ctlrow');
    var mic = document.getElementById('vzMic');
    var sendBtn = document.getElementById('send');
    if (!row || !sendBtn) return;
    var b = document.createElement('button');
    b.id = 'vcBtn';
    b.type = 'button';
    b.title = '打电话';
    b.setAttribute('aria-label', '打电话');
    b.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' +
      '<path d="M6.5 3h3l1.5 4-2 1.5a11 11 0 0 0 5.5 5.5L16 12l4 1.5v3a2 2 0 0 1-2.2 2A16 16 0 0 1 4 6.2' +
      'A2 2 0 0 1 6 4z"/></svg>';
    b.onclick = function (e) { e.preventDefault(); dial(); };
    row.insertBefore(b, mic || sendBtn);
  }

  window.dwellCall = {
    dial: dial,
    hang: hang,
    theme: function (name) { if (THEMES[name]) TH = THEMES[name]; },
    // 调参时手动切态看效果：dwellCall.preview('speaking')
    preview: function (p) {
      if (!SPEC[p]) return;
      build();
      box.classList.add('on');
      live = true;
      startDraw();
      setPhase(p);
    },
    spec: SPEC,
    themes: THEMES
  };

  function boot() {
    mountBtn();
    setInterval(mountBtn, 1200);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_voice_call(server_module):
    """再包一层 index，把通话那套注进去。要排在 register_voice_feature 之后。"""
    app = server_module.app
    script = CLIENT_SCRIPT.replace("__VOICE_TOKEN__", _voice_token())
    server_module.voice_call_script = script

    original = app.view_functions.get("index")
    if original is None:
        return

    def index_with_call(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            if "text/html" not in (resp.headers.get("Content-Type") or ""):
                return resp
            html = resp.get_data(as_text=True)
        except Exception:
            return resp
        if "window.dwellCall" in html or "</body>" not in html:
            return resp
        resp.set_data(html.replace("</body>", script + "</body>", 1))
        return resp

    app.view_functions["index"] = index_with_call
