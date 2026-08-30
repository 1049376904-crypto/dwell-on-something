"""dwell 语音通话：对讲机模式。

点电话进全屏，球跟着你的声音活；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
听写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层，那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

球的实现见下面 CLIENT_SCRIPT 里的注释：阻尼弹簧 + 三层噪声流场 +
音频驱动坐标扭曲。白底黑球，内部流体是极克制的灰度层次。
调参：`dwellCall.preview('speaking')` 把球定在某态，`dwellCall.spec`
是那张参数表，改完立刻生效。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from voice_feature import _voice_token

CLIENT_SCRIPT = r"""
<style>
#vcall{position:fixed;inset:0;z-index:9999;display:none;
  background:radial-gradient(125% 95% at 50% 4%, #fbfaf8 0%, #f3f1ed 44%, #e8e6e0 100%);
  color:#15151a;-webkit-user-select:none;user-select:none;
  -webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif}
#vcall.on{display:grid;grid-template-rows:auto 1fr auto}

.vc-top{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap;
  padding:calc(env(safe-area-inset-top,0px) + 22px) 20px 0}
.vc-tag{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:999px;
  background:rgba(255,255,255,.5);border:1px solid rgba(21,21,26,.06);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  font-size:clamp(9.5px,2.4vw,11px);letter-spacing:.13em;text-transform:uppercase;
  color:rgba(21,21,26,.4);font-variant-numeric:tabular-nums;white-space:nowrap}
.vc-dot{width:5px;height:5px;border-radius:50%;background:#3f9e74;flex:0 0 auto;
  transition:background .6s ease}
#vcall.thinking .vc-dot{background:#ab8836}
#vcall.speaking .vc-dot{background:#15151a}
.vc-bars{display:inline-flex;align-items:flex-end;gap:1.5px;height:9px}
.vc-bars i{width:2px;background:currentColor;opacity:.18;border-radius:1px}
.vc-bars i:nth-child(1){height:3px}
.vc-bars i:nth-child(2){height:5px}
.vc-bars i:nth-child(3){height:7px}
.vc-bars i:nth-child(4){height:9px}
.vc-bars.s1 i:nth-child(1),.vc-bars.s2 i:nth-child(-n+2),
.vc-bars.s3 i:nth-child(-n+3),.vc-bars.s4 i{opacity:.68}

.vc-mid{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:clamp(14px,3.6vh,34px);min-height:0;padding:0 20px}
.vc-stage{position:relative;flex:0 0 auto;display:block}
.vc-stage canvas{display:block;width:100%;height:100%}

.vc-cap{text-align:center;max-width:min(74vw,430px);min-height:3.4em}
.vc-state{font-size:clamp(10px,2.6vw,11.5px);letter-spacing:.2em;text-transform:uppercase;
  color:rgba(21,21,26,.28);margin-bottom:11px;
  transition:color .6s cubic-bezier(.22,.61,.36,1)}
#vcall.speaking .vc-state{color:rgba(21,21,26,.56)}
.vc-said{font-size:clamp(14px,3.7vw,16.5px);line-height:1.85;font-weight:300;
  letter-spacing:.028em;color:rgba(21,21,26,.8);word-break:break-word}
.vc-said b{font-weight:300;opacity:0;animation:vcin .5s cubic-bezier(.22,.61,.36,1) forwards}
@keyframes vcin{to{opacity:1}}
.vc-said.dim{color:rgba(21,21,26,.32)}

.vc-btns{display:flex;justify-content:center;align-items:center;gap:clamp(22px,7vw,34px);
  padding:0 20px calc(env(safe-area-inset-bottom,0px) + clamp(28px,5.5vh,50px))}
.vc-btn{-webkit-appearance:none;appearance:none;
  width:clamp(56px,15vw,64px);height:clamp(56px,15vw,64px);border-radius:50%;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  color:#15151a;background:rgba(255,255,255,.66);border:1px solid rgba(21,21,26,.07);
  box-shadow:0 3px 14px rgba(21,21,26,.055);
  backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);
  transition:transform .42s cubic-bezier(.34,1.42,.5,1),background .3s ease,opacity .3s ease}
.vc-btn:active{transform:scale(.9)}
.vc-btn:focus-visible{outline:2px solid rgba(21,21,26,.45);outline-offset:3px}
.vc-btn.mute.off{opacity:.4;background:rgba(255,255,255,.4)}
.vc-btn.hang{background:#b4413a;border-color:rgba(21,21,26,.05);color:#fdfbf8;
  box-shadow:0 5px 20px rgba(180,65,58,.24)}
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

  /* ═══ 主题：黑球在白底上，颜色只是外壳 ═══════════════════════
     main 主色，low/mid/high 是内部流体那三层的灰度 —— 差异故意做得
     极小，不仔细看看不见，但能感觉到里面在动。调大就不黑了。 */
  var THEMES = {
    ink: {
      main: [10, 10, 12], low: [22, 22, 27], mid: [34, 33, 41], high: [52, 50, 62],
      hue: 236, halo: 0.062, shade: 0.20
    },
    slate: {
      main: [10, 12, 15], low: [21, 25, 31], mid: [31, 37, 46], high: [46, 55, 68],
      hue: 212, halo: 0.058, shade: 0.185
    }
  };
  var TH = THEMES.ink;

  /* ═══ 状态机 ═══════════════════════════════════════════════════
     scale 目标大小，k/d 弹簧刚度与阻尼，flow 流速，warp 形变幅度,
     tension 边缘张力（越低越"液体"），lum 内部流体亮度，
     swirl 漩涡强度，halo 辉光倍率。 */
  var SPEC = {
    idle:      { scale: 1.00, k: 42,  d: 7.4, flow: .16, warp: .022, tension: .82,
                 lum: .52, swirl: .04, halo: .70, label: 'Idle' },
    listening: { scale: 0.94, k: 78,  d: 8.2, flow: .40, warp: .034, tension: .74,
                 lum: .78, swirl: .10, halo: .88, label: 'Listening' },
    thinking:  { scale: 0.97, k: 120, d: 6.2, flow: .82, warp: .046, tension: .70,
                 lum: .88, swirl: 1.0, halo: .80, label: 'Thinking' },
    speaking:  { scale: 1.09, k: 165, d: 5.4, flow: 1.0, warp: .090, tension: .52,
                 lum: 1.0,  swirl: .18, halo: 1.0, label: 'Speaking' }
  };

  var box, cv, ctx, stage, stateEl, saidEl, metaEl, timeEl, barsEl, muteBtn, modelEl;
  var live = false, phase = 'idle', muted = false;
  var micStream = null, rec = null, chunks = [], recT0 = 0;
  var ac = null, an = null, freqArr = null, meterRaf = 0, silentSince = 0, spoke = false;
  var player = null, chars = 0, turns = 0, stopAll = false, dialT0 = 0;

  var MIN_MS = 700, MAX_MS = 30000, HUSH_MS = 1400, HUSH_LVL = 0.055;

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

  /* ═══════════════════════════════════════════════════════════════
     阻尼谐振弹簧。lerp 永远追不过头，所以显得机械；弹簧会过冲目标
     再回弹 settling，重量感就是从这儿来的。
       a = (target - x) * k - v * d
     k 刚度（多急），d 阻尼（多快停）。d 小了会晃很久，大了就没弹性。
     ═════════════════════════════════════════════════════════════ */
  function Spring(x0, k, d) {
    this.x = x0; this.v = 0; this.k = k; this.d = d;
  }
  Spring.prototype.to = function (target, dt) {
    // dt 钳在 32ms：切后台回来那一下 dt 会很大，不钳的话直接炸开
    dt = Math.min(dt, 0.032);
    var a = (target - this.x) * this.k - this.v * this.d;
    this.v += a * dt;
    this.x += this.v * dt;
    return this.x;
  };
  Spring.prototype.tune = function (k, d) { this.k = k; this.d = d; };

  /* ═══ 三层 FBM 噪声：离屏预渲染，主循环只采样合成 ═══════════════
     值噪声 + 分形布朗运动。三层频率不同、流速不同，叠起来才是"翻涌"，
     单层只会看着像整片平移。 */
  var NZ = 168;                       // 每层 tile 边长
  var layers = [];                    // 三张离屏 canvas

  function valueNoise(seed) {
    var g = new Float32Array(NZ * NZ);
    var s = seed * 9301;
    function rnd() { s = (s * 9301 + 49297) % 233280; return s / 233280; }
    var grid = 12, gv = [];
    for (var i = 0; i <= grid; i++) {
      gv[i] = [];
      for (var j = 0; j <= grid; j++) gv[i][j] = rnd();
    }
    function smooth(t) { return t * t * (3 - 2 * t); }
    function at(gx, gy) {                // 双线性 + 平滑插值
      var x = gx * grid, y = gy * grid;
      var xi = Math.floor(x) % grid, yi = Math.floor(y) % grid;
      var xf = smooth(x - Math.floor(x)), yf = smooth(y - Math.floor(y));
      var a = gv[xi][yi], b = gv[xi + 1][yi], c = gv[xi][yi + 1], dd = gv[xi + 1][yi + 1];
      return (a * (1 - xf) + b * xf) * (1 - yf) + (c * (1 - xf) + dd * xf) * yf;
    }
    for (var y2 = 0; y2 < NZ; y2++) {
      for (var x2 = 0; x2 < NZ; x2++) {
        // FBM：四个八度，振幅逐层减半
        var v = 0, amp = .5, f = 1;
        for (var o = 0; o < 4; o++) {
          v += at((x2 / NZ * f) % 1, (y2 / NZ * f) % 1) * amp;
          amp *= .5; f *= 2;
        }
        g[y2 * NZ + x2] = v;
      }
    }
    return g;
  }

  function buildLayers() {
    if (layers.length) return;
    var cfg = [
      { seed: 3,  lo: TH.low,  a: 1.00 },
      { seed: 17, lo: TH.mid,  a: 0.72 },
      { seed: 41, lo: TH.high, a: 0.46 }
    ];
    for (var n = 0; n < 3; n++) {
      var g = valueNoise(cfg[n].seed);
      var c = document.createElement('canvas');
      c.width = c.height = NZ;
      var cc = c.getContext('2d');
      var img = cc.createImageData(NZ, NZ);
      var base = TH.main, tip = cfg[n].lo;
      for (var i = 0, p = 0; i < g.length; i++, p += 4) {
        // 对比度拉一把，中间调压掉，只留丝状的亮脉
        var v = Math.min(1, Math.max(0, (g[i] - .28) * 2.1));
        v = v * v * (3 - 2 * v);
        img.data[p]     = base[0] + (tip[0] - base[0]) * v;
        img.data[p + 1] = base[1] + (tip[1] - base[1]) * v;
        img.data[p + 2] = base[2] + (tip[2] - base[2]) * v;
        img.data[p + 3] = Math.round(255 * v * cfg[n].a);
      }
      cc.putImageData(img, 0, 0);
      layers.push(c);
    }
  }

  /* ═══ 渲染状态 ═══════════════════════════════════════════════ */
  var sScale = new Spring(1, 42, 7.4);      // 大小
  var sWarp = new Spring(0, 60, 9);         // 形变幅度
  var sTens = new Spring(.82, 50, 9);       // 边缘张力
  var sLum = new Spring(.52, 40, 9);        // 内部亮度
  var sSwirl = new Spring(0, 44, 9);        // 漩涡
  var sHalo = new Spring(.7, 36, 9);        // 辉光
  var sFlow = new Spring(.16, 30, 8);       // 流速

  // 四通道频率：低 / 中 / 高 / 总能量。分别驱动不同视觉参数
  var A = { lo: 0, mid: 0, hi: 0, all: 0 };
  var cumAudio = 0;                         // 累计音频能量，扭曲噪声坐标
  var flowT = 0, waddleT = 0, silence = 0;
  var drawRaf = 0, lastT = 0, W = 0, H = 0, R = 0, DPR = 1;
  var SAMPLES = 128;                        // 边缘采样点数

  function fit() {
    if (!cv || !stage) return;
    var side = Math.min(window.innerWidth * 0.92, window.innerHeight * 0.56, 560);
    stage.style.width = side + 'px';
    stage.style.height = side + 'px';
    DPR = Math.min(window.devicePixelRatio || 1, 2.5);
    cv.width = Math.round(side * DPR);
    cv.height = Math.round(side * DPR);
    W = cv.width; H = cv.height;
    R = Math.min(W, H) * 0.268;
  }
  var fitTimer = null;
  function fitLater() {
    if (fitTimer) return;
    fitTimer = setTimeout(function () { fitTimer = null; fit(); }, 140);
  }

  /* 液体边缘：不画正圆，r(θ) 三条不同频率的正弦叠加。
     speaking 时幅度顶上去就是表面张力那种融化感。
     （Canvas 2D 没有 SDF，opSmoothUnion 做不了，这是同等观感的替代） */
  function edge(th, warp, tens, t) {
    var w = warp * (0.55 + A.all * 1.5);
    return 1
      + Math.sin(th * 3 + t * 0.9 + cumAudio * 0.6) * w * (1.15 - tens * .5)
      + Math.sin(th * 5 - t * 1.35 + cumAudio * 0.35) * w * 0.62
      + Math.sin(th * 2 + t * 0.52) * w * 0.48 * (1 + A.lo * 1.2);
  }

  function frame(now) {
    drawRaf = requestAnimationFrame(frame);
    if (!ctx || !live) return;

    var dt = lastT ? (now - lastT) / 1000 : 0.016;
    lastT = now;
    var s = SPEC[phase] || SPEC.idle;

    sScale.tune(s.k, s.d);
    var target = s.scale;
    if (phase === 'listening') target = s.scale + Math.min(0.22, A.all * 1.5);
    if (phase === 'speaking') target = s.scale + A.all * 0.05;

    var sc = sScale.to(target, dt);
    var warp = sWarp.to(s.warp, dt);
    var tens = sTens.to(s.tension, dt);
    var lum = sLum.to(s.lum, dt);
    var swirl = sSwirl.to(s.swirl, dt);
    var halo = sHalo.to(s.halo, dt);
    var flow = sFlow.to(s.flow, dt);

    // 累计音频能量：说得响流体转得急，安静下来慢慢淌
    cumAudio += (A.all * 2.6 + 0.12) * flow * dt * 4;
    flowT += dt * (0.24 + flow * 0.75 + A.mid * 0.9);

    // Waddle：安静久了微微摇摆，像活物待着而不是完全静止
    if (phase === 'idle' || (phase === 'listening' && A.all < 0.02)) {
      silence = Math.min(1, silence + dt * 0.55);
    } else {
      silence = Math.max(0, silence - dt * 2.2);
    }
    waddleT += dt;
    var wx = Math.sin(waddleT * 0.72) * R * 0.020 * silence;
    var wy = Math.sin(waddleT * 0.47 + 1.1) * R * 0.014 * silence;

    var cx = W / 2 + wx, cy = H / 2 + wy, r = R * sc;
    ctx.clearRect(0, 0, W, H);

    // ── 落地投影
    var shY = cy + r * 1.28, shW = r * (1.02 + (halo - .7) * 1.1), shH = shW * 0.17;
    var gs = ctx.createRadialGradient(cx, shY, 0, cx, shY, shW);
    gs.addColorStop(0, 'rgba(21,21,26,' + (TH.shade * (1.15 - halo * .35)).toFixed(3) + ')');
    gs.addColorStop(0.6, 'rgba(21,21,26,' + (TH.shade * .26).toFixed(3) + ')');
    gs.addColorStop(1, 'rgba(21,21,26,0)');
    ctx.save();
    ctx.translate(cx, shY);
    ctx.scale(1, shH / shW);
    ctx.beginPath();
    ctx.arc(0, 0, shW, 0, Math.PI * 2);
    ctx.fillStyle = gs;
    ctx.fill();
    ctx.restore();

    // ── 辉光：白底上是极淡的黑色晕开
    var gh = ctx.createRadialGradient(cx, cy, r * .88, cx, cy, r * 2.3);
    gh.addColorStop(0, 'rgba(21,21,26,' + (TH.halo * halo).toFixed(4) + ')');
    gh.addColorStop(0.5, 'rgba(21,21,26,' + (TH.halo * halo * .3).toFixed(4) + ')');
    gh.addColorStop(1, 'rgba(21,21,26,0)');
    ctx.fillStyle = gh;
    ctx.fillRect(0, 0, W, H);

    // ── 球体轮廓：形变后的闭合曲线
    ctx.beginPath();
    for (var i = 0; i <= SAMPLES; i++) {
      var th = i / SAMPLES * Math.PI * 2;
      var rr = r * edge(th, slow ? 0 : warp, tens, flowT);
      var x = cx + Math.cos(th) * rr, y = cy + Math.sin(th) * rr;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();

    ctx.save();
    ctx.clip();

    // 球体底色：光源偏左上
    var lx = cx - r * .30, ly = cy - r * .36;
    var g1 = ctx.createRadialGradient(lx, ly, r * .05, cx, cy, r * 1.14);
    var m = TH.main;
    g1.addColorStop(0, 'rgb(' + (m[0] + 26) + ',' + (m[1] + 25) + ',' + (m[2] + 30) + ')');
    g1.addColorStop(0.42, 'rgb(' + (m[0] + 9) + ',' + (m[1] + 9) + ',' + (m[2] + 12) + ')');
    g1.addColorStop(1, 'rgb(' + m[0] + ',' + m[1] + ',' + m[2] + ')');
    ctx.fillStyle = g1;
    ctx.fillRect(cx - r * 1.3, cy - r * 1.3, r * 2.6, r * 2.6);

    // ── 三层流体：各自流速方向不同，叠起来才是翻涌
    if (!slow && layers.length) {
      var spd = [1.0, -0.62, 0.38], zoom = [1.0, 1.62, 2.45];
      var alpha = [0.55, 0.42, 0.34];
      for (var n = 0; n < 3; n++) {
        var side = r * 2.9 / zoom[n];
        // thinking 时坐标绕中心转，能量在漩涡里打转
        var ang = swirl * (flowT * 0.5 + n * 0.7) + cumAudio * 0.012 * (n + 1);
        var ox = Math.cos(flowT * spd[n] * 0.5 + n * 2.1) * r * 0.30
               + Math.sin(cumAudio * 0.05 * (n + 1)) * r * 0.13;
        var oy = Math.sin(flowT * spd[n] * 0.42 + n * 1.3) * r * 0.26
               + Math.cos(cumAudio * 0.04 * (n + 1)) * r * 0.11;
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(ang);
        ctx.globalAlpha = alpha[n] * lum * (0.6 + A.all * 0.7);
        ctx.globalCompositeOperation = n === 0 ? 'source-over' : 'lighter';
        ctx.drawImage(layers[n], -side / 2 + ox, -side / 2 + oy, side, side);
        ctx.restore();
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }

    // 底缘反光：白底往上打的 bounce light。
    // 这道弯月是黑球"有体积"的关键，没有它就是个洞。
    var by = cy + r * .62;
    var g2 = ctx.createRadialGradient(cx + r * .08, by, r * .04, cx + r * .08, by, r * .95);
    g2.addColorStop(0, 'hsla(' + TH.hue + ',12%,74%,' + (0.26 + halo * .08).toFixed(3) + ')');
    g2.addColorStop(0.44, 'hsla(' + TH.hue + ',10%,56%,0.085)');
    g2.addColorStop(1, 'hsla(' + TH.hue + ',10%,44%,0)');
    ctx.fillStyle = g2;
    ctx.fillRect(cx - r * 1.3, cy - r * 1.3, r * 2.6, r * 2.6);

    // 镜面高光：一片冷调折射，随高频轻微抖动
    var hi = 0.26 + halo * .12 + A.hi * .10;
    var g3 = ctx.createRadialGradient(lx, ly, 0, lx, ly, r * .60);
    g3.addColorStop(0, 'hsla(' + (TH.hue + 6) + ',16%,92%,' + hi.toFixed(3) + ')');
    g3.addColorStop(0.4, 'hsla(' + TH.hue + ',12%,76%,0.055)');
    g3.addColorStop(1, 'hsla(' + TH.hue + ',10%,60%,0)');
    ctx.fillStyle = g3;
    ctx.fillRect(cx - r * 1.3, cy - r * 1.3, r * 2.6, r * 2.6);

    ctx.restore();

    // 顶缘那道细亮线，勾住球和背景的边界
    ctx.beginPath();
    for (var j = 0; j <= SAMPLES; j++) {
      var th2 = Math.PI * 1.04 + (j / SAMPLES) * Math.PI * 0.92;
      var rr2 = r * edge(th2, slow ? 0 : warp, tens, flowT) - DPR * .5;
      var x2 = cx + Math.cos(th2) * rr2, y2 = cy + Math.sin(th2) * rr2;
      if (j === 0) ctx.moveTo(x2, y2); else ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = 'hsla(' + TH.hue + ',14%,84%,' + (0.13 + halo * .1).toFixed(3) + ')';
    ctx.lineWidth = DPR;
    ctx.stroke();

    if (timeEl && dialT0) timeEl.textContent = fmt((Date.now() - dialT0) / 1000);
  }

  function startDraw() {
    if (drawRaf) return;
    buildLayers();
    lastT = 0;
    drawRaf = requestAnimationFrame(frame);
  }
  function stopDraw() {
    if (drawRaf) { cancelAnimationFrame(drawRaf); drawRaf = 0; }
  }

  /* ═══ 界面 ═══════════════════════════════════════════════════ */
  function setPhase(p, label) {
    phase = p;
    if (!box) return;
    box.classList.remove('listening', 'thinking', 'speaking');
    if (p !== 'idle') box.classList.add(p);
    if (stateEl) stateEl.textContent = label != null ? label : (SPEC[p] || SPEC.idle).label;
  }

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
        '<span class="vc-tag"><span class="vc-dot"></span><span>Connected</span></span>' +
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

  /* ═══ 音频分析：四通道频率驱动视觉，同时判断你说完了没 ═══════ */
  function startMeter() {
    if (!micStream || !window.AudioContext || an) return;
    try {
      ac = new AudioContext();
      an = ac.createAnalyser();
      an.fftSize = 1024;
      an.smoothingTimeConstant = 0.72;
      ac.createMediaStreamSource(micStream).connect(an);
      var wave = new Uint8Array(an.fftSize);
      freqArr = new Uint8Array(an.frequencyBinCount);
      var loop = function () {
        if (!an) return;
        meterRaf = requestAnimationFrame(loop);
        an.getByteTimeDomainData(wave);
        var sum = 0;
        for (var i = 0; i < wave.length; i++) {
          var v = (wave[i] - 128) / 128;
          sum += v * v;
        }
        var lvl = Math.sqrt(sum / wave.length);

        // 频谱分三段：低 / 中 / 高，各驱动不同参数
        an.getByteFrequencyData(freqArr);
        var n = freqArr.length, b1 = (n * .08) | 0, b2 = (n * .32) | 0;
        var s1 = 0, s2 = 0, s3 = 0;
        for (var j = 0; j < b1; j++) s1 += freqArr[j];
        for (var j2 = b1; j2 < b2; j2++) s2 += freqArr[j2];
        for (var j3 = b2; j3 < n; j3++) s3 += freqArr[j3];
        A.lo = A.lo * .7 + (s1 / b1 / 255) * .3;
        A.mid = A.mid * .7 + (s2 / (b2 - b1) / 255) * .3;
        A.hi = A.hi * .7 + (s3 / (n - b2) / 255) * .3;
        A.all = A.all * .72 + lvl * .28;

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
    A.lo = A.mid = A.hi = A.all = 0;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
  }

  /* 模型说话时让球也跟着动：接一路分析器到播放器上 */
  var pAn = null, pRaf = 0;
  function watchPlayer() {
    if (!ac || pAn || !player) return;
    try {
      var src = ac.createMediaElementSource(player);
      pAn = ac.createAnalyser();
      pAn.fftSize = 1024;
      pAn.smoothingTimeConstant = 0.7;
      src.connect(pAn);
      pAn.connect(ac.destination);
      var wave = new Uint8Array(pAn.fftSize);
      var fq = new Uint8Array(pAn.frequencyBinCount);
      var loop = function () {
        pRaf = requestAnimationFrame(loop);
        if (!pAn || phase !== 'speaking') return;
        pAn.getByteTimeDomainData(wave);
        var sum = 0;
        for (var i = 0; i < wave.length; i++) {
          var v = (wave[i] - 128) / 128;
          sum += v * v;
        }
        pAn.getByteFrequencyData(fq);
        var n = fq.length, b1 = (n * .08) | 0, b2 = (n * .32) | 0;
        var s1 = 0, s2 = 0, s3 = 0;
        for (var j = 0; j < b1; j++) s1 += fq[j];
        for (var j2 = b1; j2 < b2; j2++) s2 += fq[j2];
        for (var j3 = b2; j3 < n; j3++) s3 += fq[j3];
        A.lo = A.lo * .7 + (s1 / b1 / 255) * .3;
        A.mid = A.mid * .7 + (s2 / (b2 - b1) / 255) * .3;
        A.hi = A.hi * .7 + (s3 / (n - b2) / 255) * .3;
        A.all = A.all * .7 + Math.sqrt(sum / wave.length) * .3;
      };
      pRaf = requestAnimationFrame(loop);
    } catch (e) { pAn = null; }   // 有的浏览器不给接，那就只按时间驱动
  }

  /* ═══ 一轮：听 → 传 → 发 → 等 → 念 ═════════════════════════ */
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
      watchPlayer();
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
    A.all = A.lo = A.mid = A.hi = 0;
  }

  /* ═══ 进出 ═══════════════════════════════════════════════════ */
  async function dial() {
    if (live) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    build();
    // iOS 只在用户手势里放开播放权限：借这一下点击把播放器解锁，
    // 之后整通电话复用它，不然模型第一句会哑在那儿。
    if (!player) {
      player = new Audio();
      player.setAttribute('playsinline', '');
      player.crossOrigin = 'anonymous';
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
    silence = 0;
    cumAudio = 0;
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
    if (pRaf) { cancelAnimationFrame(pRaf); pRaf = 0; }
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
    theme: function (name) {
      if (!THEMES[name]) return;
      TH = THEMES[name];
      layers = [];          // 流体那三层跟着重建
      buildLayers();
    },
    // 调参时把球定在某态：dwellCall.preview('speaking')
    preview: function (p) {
      if (!SPEC[p]) return;
      build();
      box.classList.add('on');
      live = true;
      fit();
      startDraw();
      setPhase(p);
    },
    spec: SPEC,
    themes: THEMES,
    audio: A
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
