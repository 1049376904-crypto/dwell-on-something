"""dwell 语音通话：对讲机模式。

点电话进全屏，球跟着你的音量涨落；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
听写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层（流式输出 + 边出字边分句合成 +
边说边转写 + 打断处理），那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

白底黑球。黑球在亮底上不能靠发光立体（那样会像一个洞），靠的是
镜面高光 + 底缘反光 + 落地投影。球是 Canvas 2D 画的，四态之间弹簧插值。
改主题只动 THEMES；调参时 `dwellCall.preview('speaking')` 能把球定住看。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from voice_feature import _voice_token

CLIENT_SCRIPT = r"""
<style>
#vcall{position:fixed;inset:0;z-index:9999;display:none;
  background:
    radial-gradient(125% 95% at 50% 6%, #faf9f7 0%, #f2f0ec 42%, #e7e5df 100%);
  color:#1b1b20;-webkit-user-select:none;user-select:none;
  -webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif}
#vcall.on{display:grid;grid-template-rows:auto 1fr auto}

/* 顶部：细小的胶囊标签，稀疏地排一行 */
.vc-top{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap;
  padding:calc(env(safe-area-inset-top,0px) + 22px) 20px 0}
.vc-tag{display:inline-flex;align-items:center;gap:6px;
  padding:5px 11px;border-radius:999px;
  background:rgba(255,255,255,.55);border:1px solid rgba(24,24,28,.07);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  font-size:clamp(9.5px,2.4vw,11px);letter-spacing:.13em;text-transform:uppercase;
  color:rgba(27,27,32,.42);font-variant-numeric:tabular-nums;white-space:nowrap}
.vc-dot{width:5px;height:5px;border-radius:50%;background:#3f9e74;flex:0 0 auto;
  transition:background .5s ease}
#vcall.thinking .vc-dot{background:#b08d3c}
#vcall.speaking .vc-dot{background:#1b1b20}
.vc-bars{display:inline-flex;align-items:flex-end;gap:1.5px;height:9px}
.vc-bars i{width:2px;background:currentColor;opacity:.2;border-radius:1px}
.vc-bars i:nth-child(1){height:3px}
.vc-bars i:nth-child(2){height:5px}
.vc-bars i:nth-child(3){height:7px}
.vc-bars i:nth-child(4){height:9px}
.vc-bars.s1 i:nth-child(1),.vc-bars.s2 i:nth-child(-n+2),
.vc-bars.s3 i:nth-child(-n+3),.vc-bars.s4 i{opacity:.72}

/* 中间：球 + 字幕，慷慨留白 */
.vc-mid{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:clamp(16px,4vh,36px);min-height:0;padding:0 20px}
.vc-stage{position:relative;flex:0 0 auto;display:block}
.vc-stage canvas{display:block;width:100%;height:100%}

.vc-cap{text-align:center;max-width:min(74vw,430px);min-height:3.4em}
.vc-state{font-size:clamp(10px,2.6vw,11.5px);letter-spacing:.2em;text-transform:uppercase;
  color:rgba(27,27,32,.3);margin-bottom:11px;
  transition:color .5s cubic-bezier(.22,.61,.36,1)}
#vcall.speaking .vc-state{color:rgba(27,27,32,.58)}
.vc-said{font-size:clamp(14px,3.7vw,16.5px);line-height:1.85;font-weight:300;
  letter-spacing:.028em;color:rgba(27,27,32,.8);word-break:break-word}
.vc-said b{font-weight:300;opacity:0;animation:vcin .5s cubic-bezier(.22,.61,.36,1) forwards}
@keyframes vcin{to{opacity:1}}
.vc-said.dim{color:rgba(27,27,32,.34)}

/* 底部：两颗玻璃按钮 */
.vc-btns{display:flex;justify-content:center;align-items:center;
  gap:clamp(22px,7vw,34px);
  padding:0 20px calc(env(safe-area-inset-bottom,0px) + clamp(30px,6vh,54px))}
.vc-btn{-webkit-appearance:none;appearance:none;
  width:clamp(56px,15vw,64px);height:clamp(56px,15vw,64px);
  border-radius:50%;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  color:#1b1b20;background:rgba(255,255,255,.66);
  border:1px solid rgba(24,24,28,.08);
  box-shadow:0 3px 14px rgba(24,24,28,.06);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  transition:transform .42s cubic-bezier(.34,1.42,.5,1),
             background .3s ease,opacity .3s ease}
.vc-btn:active{transform:scale(.9)}
.vc-btn:focus-visible{outline:2px solid rgba(27,27,32,.5);outline-offset:3px}
.vc-btn.mute.off{opacity:.4;background:rgba(255,255,255,.4)}
.vc-btn.hang{background:#b8433b;border-color:rgba(24,24,28,.06);color:#fdfbf8;
  box-shadow:0 5px 20px rgba(184,67,59,.26)}

#vcBtn{display:inline-flex;align-items:center;justify-content:center}

@media (prefers-reduced-motion:reduce){
  .vc-said b{animation:none;opacity:1}
  .vc-btn,.vc-dot{transition:none}
}
</style>
<script>
(function () {
  if (window.dwellCall) return;
  var TOKEN = '__VOICE_TOKEN__';
  var CALL_HINT = '[通话中]';

  /* ── 主题：改色只动这里 ─────────────────────────────────────────
     hue 反光的色相偏向，body 球体明度基准，rim 底缘反光强度，
     shadow 投影浓度。黑球在亮底上全靠 rim 和 shadow 立住。 */
  var THEMES = {
    ink:   { hue: 232, sat: 9,  body: 13, rim: 0.30, shadow: 0.20 },
    slate: { hue: 214, sat: 15, body: 16, rim: 0.36, shadow: 0.18 }
  };
  var TH = THEMES.ink;

  /* ── 状态机：每态一组目标值，帧间弹簧插值，不许生硬跳变 ───────
     scale 基准大小，breathe 呼吸幅度，period 周期，lift 投影扩散，
     rim 反光倍率，emit 粒子量，ripple 涟漪开关，jitter 抖动，
     spin 高光游走速度。 */
  var SPEC = {
    idle:      { scale: 1.00, breathe: .038, period: 5500, lift: .30, rim: .70,
                 emit: .10, ripple: 0, jitter: 0, spin: .10, label: 'Idle' },
    listening: { scale: 0.945, breathe: .006, period: 4200, lift: .22, rim: .88,
                 emit: .38, ripple: 1, jitter: 0, spin: .16, label: 'Listening' },
    thinking:  { scale: 0.985, breathe: .010, period: 1500, lift: .34, rim: .80,
                 emit: .28, ripple: 0, jitter: .9, spin: .74, label: 'Thinking' },
    speaking:  { scale: 1.080, breathe: .088, period: 1150, lift: 1.0, rim: 1.0,
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
  var V = { scale: 1, lift: .3, rim: .7, emit: .1, ripple: 0,
            spin: .1, level: 0, drift: 0 };
  var parts = [], ripples = [], noiseTile = null;
  var drawRaf = 0, t0 = 0, lastRip = 0, W = 0, H = 0, R = 0, DPR = 1;

  function lerp(a, b, k) { return a + (b - a) * k; }

  /* 噪点：预渲染一小块反复铺，不每帧算随机数。
     亮底上要用 multiply 才压得出颗粒，overlay 会被冲掉。 */
  function buildNoise() {
    var n = document.createElement('canvas');
    n.width = n.height = 96;
    var c = n.getContext('2d');
    var img = c.createImageData(96, 96);
    for (var i = 0; i < img.data.length; i += 4) {
      var v = 190 + (Math.random() - .5) * 130;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
    c.putImageData(img, 0, 0);
    noiseTile = n;
  }

  function fit() {
    if (!cv || !stage) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    // 球占屏高 40~50%；画布比球大一圈，留给投影和粒子
    var side = Math.min(vw * 0.92, vh * 0.56, 560);
    stage.style.width = side + 'px';
    stage.style.height = side + 'px';
    DPR = Math.min(window.devicePixelRatio || 1, 2.5);
    cv.width = Math.round(side * DPR);
    cv.height = Math.round(side * DPR);
    W = cv.width; H = cv.height;
    R = Math.min(W, H) * 0.268;
  }
  var fitTimer = null;
  function fitLater() {                     // 节流 resize
    if (fitTimer) return;
    fitTimer = setTimeout(function () { fitTimer = null; fit(); }, 140);
  }

  function emitParticle(now) {
    parts.push({
      a: Math.random() * Math.PI * 2,
      r: R * (0.97 + Math.random() * 0.08),
      vr: R * (0.0020 + Math.random() * 0.0068),
      va: (Math.random() - .5) * 0.0040,
      sz: DPR * (0.45 + Math.random() * 1.35),
      born: now,
      life: 1200 + Math.random() * 2200
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
    if (phase === 'listening') want = s.scale + Math.min(0.24, V.level * 1.8);
    if (phase === 'thinking' && !slow) want += Math.sin(t / 92) * 0.006 * s.jitter;

    V.scale = lerp(V.scale, want, k);
    V.lift = lerp(V.lift, s.lift, k * .8);
    V.rim = lerp(V.rim, s.rim, k * .8);
    V.emit = lerp(V.emit, s.emit, k);
    V.ripple = lerp(V.ripple, s.ripple, k);
    V.spin = lerp(V.spin, s.spin, k);
    V.drift += V.spin * .05;

    var cx = W / 2, cy = H / 2, r = R * V.scale;
    var hue = TH.hue, sat = TH.sat;

    ctx.clearRect(0, 0, W, H);

    // ── 落地投影：球正下方一片椭圆，speaking 时扩散变软
    var sy = cy + r * (1.24 + V.lift * .1);
    var sw = r * (1.05 + V.lift * .5);
    var sh = r * (0.17 + V.lift * .11);
    var gs = ctx.createRadialGradient(cx, sy, 0, cx, sy, sw);
    gs.addColorStop(0, 'hsla(' + hue + ',' + sat + '%,14%,' +
      (TH.shadow * (1 - V.lift * .34)).toFixed(3) + ')');
    gs.addColorStop(0.55, 'hsla(' + hue + ',' + sat + '%,16%,' +
      (TH.shadow * .3 * (1 - V.lift * .3)).toFixed(3) + ')');
    gs.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,18%,0)');
    ctx.save();
    ctx.translate(cx, sy);
    ctx.scale(1, sh / sw);
    ctx.translate(-cx, -sy);
    ctx.fillStyle = gs;
    ctx.beginPath();
    ctx.arc(cx, sy, sw, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // ── 涟漪：listening 时一圈圈往外扩（亮底上是细深色环）
    if (V.ripple > .02 && !slow) {
      if (phase === 'listening' && now - lastRip > 780) {
        ripples.push(now);
        lastRip = now;
      }
      for (var i = ripples.length - 1; i >= 0; i--) {
        var age = (now - ripples[i]) / 2600;
        if (age >= 1) { ripples.splice(i, 1); continue; }
        ctx.beginPath();
        ctx.arc(cx, cy, r * (1 + age * 1.02), 0, Math.PI * 2);
        ctx.strokeStyle = 'hsla(' + hue + ',' + sat + '%,22%,' +
          ((1 - age) * 0.13 * V.ripple).toFixed(3) + ')';
        ctx.lineWidth = DPR * (1 - age * .5);
        ctx.stroke();
      }
    }

    // ── 边缘粒子：深灰半透明，从球面往外飘
    if (!slow) {
      var quota = V.emit * (2.0 + V.level * 8);
      while (quota-- > 0) if (Math.random() < .8) emitParticle(now);
      for (var p = parts.length - 1; p >= 0; p--) {
        var q = parts[p];
        var a2 = (now - q.born) / q.life;
        if (a2 >= 1) { parts.splice(p, 1); continue; }
        q.r += q.vr * (1 + V.level * 2.2);
        q.a += q.va;
        var fade = a2 < .18 ? a2 / .18 : (1 - a2) / .82;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(q.a) * q.r * V.scale,
                cy + Math.sin(q.a) * q.r * V.scale, q.sz, 0, Math.PI * 2);
        ctx.fillStyle = 'hsla(' + hue + ',' + (sat + 4) + '%,20%,' +
          (fade * 0.30 * (.5 + V.rim * .5)).toFixed(3) + ')';
        ctx.fill();
      }
    }

    // ── 接触阴影：球底那一小圈更浓的暗，把它压在"地面"上
    var gc = ctx.createRadialGradient(cx, cy + r * .92, 0, cx, cy + r * .92, r * .8);
    gc.addColorStop(0, 'hsla(' + hue + ',' + sat + '%,10%,' + (TH.shadow * .55).toFixed(3) + ')');
    gc.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,12%,0)');
    ctx.fillStyle = gc;
    ctx.beginPath();
    ctx.arc(cx, cy + r * .92, r * .8, 0, Math.PI * 2);
    ctx.fill();

    // ── 球体：深墨色，光源偏左上
    var lx = cx - r * .32, ly = cy - r * .38;
    var g1 = ctx.createRadialGradient(lx, ly, r * .05, cx, cy, r * 1.02);
    g1.addColorStop(0, 'hsl(' + hue + ',' + sat + '%,' + (TH.body + 17) + '%)');
    g1.addColorStop(0.36, 'hsl(' + hue + ',' + sat + '%,' + (TH.body + 4) + '%)');
    g1.addColorStop(0.78, 'hsl(' + hue + ',' + (sat + 3) + '%,' + Math.max(4, TH.body - 6) + '%)');
    g1.addColorStop(1, 'hsl(' + hue + ',' + (sat + 5) + '%,' + Math.max(3, TH.body - 9) + '%)');
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = g1;
    ctx.fill();

    ctx.save();
    ctx.clip();

    // 底缘反光：白色环境往上打的 bounce light。
    // 这道弯月是黑球在亮底上"有体积"的关键，没有它就是个洞。
    var by = cy + r * .58;
    var g2 = ctx.createRadialGradient(cx + r * .1, by, r * .04, cx + r * .1, by, r * .92);
    g2.addColorStop(0, 'hsla(' + (hue - 8) + ',' + (sat + 8) + '%,72%,' +
      (0.30 * V.rim).toFixed(3) + ')');
    g2.addColorStop(0.42, 'hsla(' + (hue - 4) + ',' + (sat + 6) + '%,56%,' +
      (0.10 * V.rim).toFixed(3) + ')');
    g2.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,40%,0)');
    ctx.fillStyle = g2;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    // 镜面高光：一片冷调折射，不是塑料球那种小圆点
    var g3 = ctx.createRadialGradient(lx, ly, 0, lx, ly, r * .62);
    g3.addColorStop(0, 'hsla(' + (hue + 6) + ',' + (sat + 10) + '%,90%,' +
      (0.28 + V.rim * .14).toFixed(3) + ')');
    g3.addColorStop(0.38, 'hsla(' + (hue + 4) + ',' + (sat + 6) + '%,74%,' +
      (0.07 * V.rim).toFixed(3) + ')');
    g3.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,60%,0)');
    ctx.fillStyle = g3;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    // thinking 时一颗光斑绕着球面游
    if (V.spin > .3) {
      var sa = t / 640, spr = r * .48;
      var px2 = cx + Math.cos(sa) * spr, py2 = cy + Math.sin(sa * 1.3) * spr * .7;
      var g4 = ctx.createRadialGradient(px2, py2, 0, px2, py2, r * .4);
      g4.addColorStop(0, 'hsla(' + (hue + 10) + ',' + (sat + 8) + '%,80%,' +
        (0.13 * Math.min(1, V.spin * 1.4)).toFixed(3) + ')');
      g4.addColorStop(1, 'hsla(' + hue + ',' + sat + '%,60%,0)');
      ctx.fillStyle = g4;
      ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
    }

    // 噪点铺满球面：压掉渐变过于平滑的塑料感
    if (noiseTile) {
      ctx.globalAlpha = 0.09;
      ctx.globalCompositeOperation = 'multiply';
      for (var nx = cx - r; nx < cx + r; nx += 96) {
        for (var ny = cy - r; ny < cy + r; ny += 96) ctx.drawImage(noiseTile, nx, ny);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }
    ctx.restore();

    // 顶缘那道细亮线：勾住球和背景的边界
    ctx.beginPath();
    ctx.arc(cx, cy, r - DPR * .5, Math.PI * 1.06, Math.PI * 1.94);
    ctx.strokeStyle = 'hsla(' + (hue + 4) + ',' + (sat + 8) + '%,82%,' +
      (0.14 + V.rim * .12).toFixed(3) + ')';
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
