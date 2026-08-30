"""dwell 语音通话：对讲机模式。

点电话进全屏，形状跟着你的声音变；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
转写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层，那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

形状照 ChatGPT Voice Mode "Bloop" 的 GLSL 源码搬的（那份 shader 被人从
Android APK 里反编译出来了），走 isNewBloop == false 那一支：纯黑扁平，
不用任何纹理。SDF + opSmoothUnion 的公式和参数一个没改，只是从 GPU 的
逐像素并行改成 Canvas 2D 上逐像素串行。

调参：`dwellCall.preview('thinking')` 把形态定住看，`dwellCall.P` 是
那张参数表，改完立刻生效。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from voice_feature import _voice_token

CLIENT_SCRIPT = r"""
<style>
#vcall{position:fixed;inset:0;z-index:9999;display:none;
  background:#faf9f7;color:#15151a;-webkit-user-select:none;user-select:none;
  -webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif}
#vcall.on{display:grid;grid-template-rows:auto 1fr auto}

.vc-top{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap;
  padding:calc(env(safe-area-inset-top,0px) + 22px) 20px 0}
.vc-tag{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:999px;
  background:rgba(21,21,26,.032);border:1px solid rgba(21,21,26,.055);
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
  color:#15151a;background:rgba(21,21,26,.045);border:1px solid rgba(21,21,26,.07);
  transition:transform .42s cubic-bezier(.34,1.42,.5,1),background .3s ease,opacity .3s ease}
.vc-btn:active{transform:scale(.9)}
.vc-btn:focus-visible{outline:2px solid rgba(21,21,26,.45);outline-offset:3px}
.vc-btn.mute.off{opacity:.4}
.vc-btn.hang{background:#15151a;border-color:#15151a;color:#faf9f7}
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

  var E = 2.71828182846, PI = Math.PI;

  /* ═══ 参数：全部来自 voice_main.fsh，数字没改 ═══════════════════ */
  var P = {
    ink: [21, 21, 26],       // 形状颜色
    mainRadius: 0.49,        // args.mainRadius
    tol: 0.005,              // clampingTolerance，边缘软化 = 免费抗锯齿
    // speak
    barCount: 4,
    barScale: 0.44,          // w = (1/barCount) * barScale
    barSpread: 1.9,          // pos.x = (f-.5) * mainRadius * barSpread
    barGoo: 0.2,             // opSmoothUnion 系数 * (1 - amount)
    // think
    cloudCount: 5,
    ringFrac: 0.45,          // 环半径 = mainRadius * ringFrac
    blobFrac: 0.5,           // 每颗圆半径 = mainRadius * blobFrac（比环大，必然重叠）
    cloudWobble: 0.1,        // 环半径的正弦扰动幅度
    spinDiv: 3.0,            // 自转：time / spinDiv
    // listen
    listenBase: 0.38,
    listenMic: 0.05,
    listenBreath: 0.03,
    listenFade: 0.6,         // 音量越大越透明
    // idle
    idleMid: 0.12,
    idleMax: 0.3
  };

  /* ═══ 缓动：原样翻自 shader ═══════════════════════════════════ */
  function scaled(e0, e1, x) {
    return Math.max(0, Math.min(1, (x - e0) / (e1 - e0)));
  }
  function spring(t, d) {
    return 1 - Math.exp(-E * 2 * t) * Math.cos((1 - d) * 115 * t);
  }
  function fixedSpring(t, d) {
    var s0 = 1 - Math.exp(-E * 2 * t) * Math.cos((1 - d) * 115 * t);
    var s = s0 + (1 - s0) * scaled(0, 1, t);
    return s * (1 - t) + t;
  }
  function bounce(t, d) {
    return -Math.sin(PI * (1 - d) * t) * (1 - t) * Math.exp(-E * 2 * t) * t * 10;
  }
  function silkySmooth(t, k) {
    return (Math.atan(k * Math.sin((t - 0.5) * PI)) / Math.atan(k)) * 0.5 + 0.5;
  }
  function mix(a, b, h) { return a + (b - a) * h; }
  function smoothstep(e0, e1, x) {
    var t = Math.max(0, Math.min(1, (x - e0) / (e1 - e0)));
    return t * t * (3 - 2 * t);
  }

  /* SDF 平滑并集。之前用 metaball 场强凑，融出来的腰比这个胖一圈，
     形状特征就是那么丢的。k 是"胶水量"。 */
  function opSmoothUnion(d1, d2, k) {
    if (k <= 0) k = 1e-6;
    var h = Math.max(0, Math.min(1, 0.5 + 0.5 * (d2 - d1) / k));
    return mix(d2, d1, h) - k * h * (1 - h);
  }
  /* 圆角矩形 SDF。四颗方块用它，圆角半径 = 宽度时就是圆角正方块。 */
  function sdRoundedBox(px, py, bx, by, r) {
    var qx = Math.abs(px) - bx + r, qy = Math.abs(py) - by + r;
    return Math.min(Math.max(qx, qy), 0)
         + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - r;
  }

  /* ═══ 状态 ═══════════════════════════════════════════════════ */
  var LABEL = { idle: 'Idle', listening: 'Listening',
                thinking: 'Thinking', speaking: 'Speaking' };

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

  /* 四通道频谱 + 总音量，对应 shader 里的 avgMag 和 micLevel */
  var AM = [0, 0, 0, 0], micLevel = 0;
  // 每个态的 amount 和进入时刻，对应 stateXxx / xxxTimestamp
  var ST = {
    listening: { amount: 0, ts: 0 },
    thinking:  { amount: 0, ts: 0 },
    speaking:  { amount: 0, ts: 0 }
  };
  var T0 = 0, silenceAmount = 0, silenceTs = 0;
  var drawRaf = 0, lastT = 0, W = 0, H = 0, DPR = 1;

  /* SDF 是逐像素算的，全分辨率在 iPad 上跑不满 60fps，
     所以画在 1/SS 的离屏上再放大。边缘那点软化正好当抗锯齿。 */
  var SS = 2, off = null, octx = null, oimg = null;

  function fit() {
    if (!cv || !stage) return;
    var side = Math.min(window.innerWidth * 0.9, window.innerHeight * 0.46, 420);
    stage.style.width = side + 'px';
    stage.style.height = side + 'px';
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(side * DPR);
    cv.height = Math.round(side * DPR);
    W = cv.width; H = cv.height;
    var ow = Math.max(1, Math.round(W / SS)), oh = Math.max(1, Math.round(H / SS));
    if (!off) { off = document.createElement('canvas'); octx = off.getContext('2d'); }
    off.width = ow; off.height = oh;
    oimg = octx.createImageData(ow, oh);
  }
  var fitTimer = null;
  function fitLater() {
    if (fitTimer) return;
    fitTimer = setTimeout(function () { fitTimer = null; fit(); }, 140);
  }

  /* ── idle：t<=1 弹簧到 0.12，之后指数逼近 0.3。
     k = exp(-gamma)*omega 保证导数在 t=1 处连续（原版注释就是这么说的） */
  function idleRadius(time) {
    var gamma = 3.0, omega = PI / 2, t1 = 1.0;
    var k = Math.exp(-gamma) * omega;
    if (time <= t1) {
      var tp = time / t1;
      return P.idleMid * (1 - Math.exp(-gamma * tp) * Math.cos(omega * tp));
    }
    return P.idleMid + (P.idleMax - P.idleMid) * (1 - Math.exp(-k * (time - t1)));
  }

  /* ── 每帧先把形状参数算出来，逐像素那层循环里只做距离计算 ── */
  var SH = { idleR: 0.3, bars: [], clouds: [], dot: null,
             listenR: 0, listenA: 1, alpha: 1 };

  function shape(time) {
    var t = time;

    // idle 一直在（原版 idleArgs.amount = 1.0）
    SH.idleR = idleRadius(t);
    SH.alpha = Math.sin((PI / 0.7) * t) * 0.175 + 0.825;

    // listen
    var L = ST.listening;
    if (L.amount > 0.001) {
      var dur = t - L.ts;
      var breathing = Math.sin(t) * 0.5 + 0.5;
      var entry = fixedSpring(scaled(0, 3, dur), 0.9);
      var listenAnim = Math.max(0, Math.min(1, spring(scaled(0, 0.9, dur), 1)));
      var l1 = micLevel;
      var r = P.listenBase + l1 * P.listenMic + breathing * P.listenBreath;
      SH.listenR = r * (1 - (1 - entry) * 0.25);
      // 原版是靠 alpha 而不是形变来表达"在听"：音量越大越透明
      SH.listenA = mix(1, 1 - l1 * P.listenFade, listenAnim);
    }

    // think：五颗圆排在环上，圆比环大所以必然重叠成云朵
    var K = ST.thinking;
    SH.clouds.length = 0;
    SH.dot = null;
    if (K.amount > 0.001) {
      var dur2 = t - K.ts;
      var entry2 = spring(scaled(0, 1, dur2), 1);
      var dotEntry = spring(scaled(0.1, 1.1, dur2), 1);
      var dotR = mix(0.2, 0.06, dotEntry) * K.amount;
      var shiftX = dotR * 0.5 * dotEntry;       // 对齐光学中心
      var goo = 0.03 * scaled(0, 10, dur2) + 0.8 * (1 - entry2);

      for (var i = 0; i < P.cloudCount; i++) {
        var f = (i + 0.5) / P.cloudCount;
        var a = -f * PI * 2 + t / P.spinDiv + spring(scaled(0, 10, dur2), 1) * PI / 2;
        var ring = P.mainRadius * P.ringFrac * entry2;
        // 这条正弦扰动就是云朵不规则起伏的来源
        ring -= (Math.sin(entry2 * PI * 4 + a * PI * 2 + t * 3
                          - silkySmooth(t / 4, 2) * PI) * 0.5 + 0.5)
                * P.mainRadius * P.cloudWobble;
        SH.clouds.push({
          x: Math.cos(a) * ring - shiftX,
          y: Math.sin(a) * ring,
          r: P.mainRadius * P.blobFrac,
          goo: goo
        });
      }
      // 左上那颗独立点，自己绕小圈转
      var dotAngle = 0.5 / P.cloudCount * PI * 2;
      var a0 = -0.5 / P.cloudCount * PI * 2 + t / P.spinDiv;
      var dotRing = (Math.sin(dotEntry * PI * 4 + a0 * PI * 2 + t * 0.1 * PI * 4) * 0.5 + 0.5)
                  * dotR * 0.3;
      SH.dot = {
        x: -P.mainRadius * 0.8 * dotEntry + Math.cos(dotAngle + t) * dotRing - shiftX,
        y: P.mainRadius * 0.8 * dotEntry + Math.sin(dotAngle + t) * dotRing,
        r: dotR * 0.8,
        goo: (1 - Math.min(dotEntry, K.amount)) * dotR
      };
    }

    // speak：四颗圆角方块
    var S = ST.speaking;
    SH.bars.length = 0;
    if (S.amount > 0.001) {
      var dur3 = t - S.ts;
      var silenceDur = t - silenceTs;
      for (var j = 0; j < P.barCount; j++) {
        var f2 = (j + 0.5) / P.barCount;
        var w = (1 / P.barCount) * P.barScale;
        var h = w;
        var wave = Math.sin(f2 * PI * 0.8 + t) * 0.5 + 0.5;
        var entry3 = spring(scaled(0.1 + wave * 0.4, 1 + wave * 0.4, dur3), 0.98);
        var px = (f2 - 0.5) * P.mainRadius * P.barSpread;
        var py = 0.25 * (1 - entry3);
        // 安静时那点小摆动
        if (silenceAmount > 0) {
          var stagger = f2 / 5, delay = 0.6;
          var bt = scaled(delay, delay + 1,
                          ((silenceDur + stagger) / 2 % 1) * 2);
          py += bounce(bt, 6) * w * 0.25 * silenceAmount
              * Math.pow(entry3, 4) * Math.pow(S.amount, 4);
        }
        // 中间两颗的音频响应更强
        h += (AM[j] || 0) * (0.1 + (1 - Math.abs(f2 - 0.5) * 2) * 0.1);
        SH.bars.push({ x: px, y: py, w: w, h: h, r: w,
                       goo: P.barGoo * (1 - S.amount) });
      }
    }
  }

  /* 逐像素：跟 shader 的 main() 一个顺序，各态按 amount 依次 mix */
  function distAt(sx, sy) {
    var d = Math.hypot(sx, sy) - SH.idleR;      // idle 垫底
    var a;

    a = ST.listening.amount;
    if (a > 0.001) d = mix(d, Math.hypot(sx, sy) - SH.listenR, a);

    a = ST.thinking.amount;
    if (a > 0.001 && SH.clouds.length) {
      var dk = 1000;
      for (var i = 0; i < SH.clouds.length; i++) {
        var c = SH.clouds[i];
        dk = opSmoothUnion(dk, Math.hypot(sx - c.x, sy - c.y) - c.r, c.goo);
      }
      if (SH.dot) {
        dk = opSmoothUnion(dk, Math.hypot(sx - SH.dot.x, sy - SH.dot.y) - SH.dot.r,
                           SH.dot.goo);
      }
      d = mix(d, dk, a);
    }

    a = ST.speaking.amount;
    if (a > 0.001 && SH.bars.length) {
      var ds = 1000;
      for (var j = 0; j < SH.bars.length; j++) {
        var b = SH.bars[j];
        ds = opSmoothUnion(ds, sdRoundedBox(sx - b.x, sy - b.y, b.w, b.h, b.r), b.goo);
      }
      d = mix(d, ds, a);
    }
    return d;
  }

  function frame(now) {
    drawRaf = requestAnimationFrame(frame);
    if (!ctx || !live || !oimg) return;
    lastT = now;
    var time = (now - T0) / 1000;

    // 各态 amount 往目标滑：这就是 shader 里 stateXxx 的作用
    for (var k in ST) {
      var want = (phase === k) ? 1 : 0;
      ST[k].amount += (want - ST[k].amount) * 0.14;
    }

    // 安静程度：speaking 时用它驱动那点小摆动
    if (phase === 'speaking' && micLevel < 0.02) {
      if (silenceAmount === 0) silenceTs = time;
      silenceAmount = Math.min(1, silenceAmount + 0.02);
    } else if (phase !== 'speaking') {
      silenceAmount = 0;
    } else {
      silenceAmount = Math.max(0, silenceAmount - 0.06);
    }

    shape(time);

    var ow = off.width, oh = off.height, data = oimg.data;
    var ink = P.ink;
    // 坐标系：st ∈ [-0.5, 0.5]，跟 shader 一致
    var inv = 1 / oh, ox = (ow * inv) * 0.5;
    var alphaBase = SH.alpha * mix(1, SH.listenA, ST.listening.amount);
    var tol = P.tol;

    for (var y = 0, idx = 0; y < oh; y++) {
      var sy = (y + 0.5) * inv - 0.5;
      for (var x = 0; x < ow; x++, idx += 4) {
        var sx = (x + 0.5) * inv - ox;
        // smoothstep(tol, 0, d)：负距离(形状内)为 1，正距离淡出
        var cs = smoothstep(tol, 0, distAt(sx, sy));
        var al = cs * alphaBase;
        if (al > 0.002) {
          data[idx] = ink[0]; data[idx + 1] = ink[1]; data[idx + 2] = ink[2];
          data[idx + 3] = (al * 255) | 0;
        } else {
          data[idx + 3] = 0;
        }
      }
    }
    octx.putImageData(oimg, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(off, 0, 0, W, H);

    if (timeEl && dialT0) timeEl.textContent = fmt((Date.now() - dialT0) / 1000);
  }

  function startDraw() {
    if (drawRaf) return;
    T0 = performance.now();
    drawRaf = requestAnimationFrame(frame);
  }
  function stopDraw() {
    if (drawRaf) { cancelAnimationFrame(drawRaf); drawRaf = 0; }
  }

  /* ═══ 界面 ═══════════════════════════════════════════════════ */
  function setPhase(p, label) {
    if (p !== phase && ST[p]) ST[p].ts = (performance.now() - T0) / 1000;
    phase = p;
    if (!box) return;
    box.classList.remove('listening', 'thinking', 'speaking');
    if (p !== 'idle') box.classList.add(p);
    if (stateEl) stateEl.textContent = label != null ? label : (LABEL[p] || '');
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
      if (micStream) micStream.getTracks().forEach(function (tr) { tr.enabled = !muted; });
    };
    window.addEventListener('resize', fitLater);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', fitLater);
    fit();

    fetch('api/model', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (modelEl && d.model) modelEl.textContent = d.model; })
      .catch(function () {});
  }

  /* ═══ 音频：四段频谱喂 avgMag，总能量喂 micLevel ═══════════════ */
  function readBands(node, wave, fq) {
    node.getByteTimeDomainData(wave);
    var sum = 0;
    for (var i = 0; i < wave.length; i++) {
      var v = (wave[i] - 128) / 128;
      sum += v * v;
    }
    node.getByteFrequencyData(fq);
    var n = fq.length;
    // 四段：低 / 中低 / 中高 / 高，对应 shader 里 avgMag 的四个通道
    var edges = [0, (n * .06) | 0, (n * .18) | 0, (n * .42) | 0, n];
    for (var b = 0; b < 4; b++) {
      var s = 0, c = edges[b + 1] - edges[b];
      for (var k = edges[b]; k < edges[b + 1]; k++) s += fq[k];
      var v2 = c > 0 ? (s / c / 255) : 0;
      AM[b] = AM[b] * 0.68 + v2 * 0.32;
    }
    return Math.sqrt(sum / wave.length);
  }

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
        var lvl = readBands(an, wave, freqArr);
        // thinking 要"自己动"，speaking 那会儿麦克风收到的是喇叭回声
        if (phase === 'listening' || phase === 'idle') {
          micLevel = micLevel * 0.72 + lvl * 0.28;
        }
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
    AM[0] = AM[1] = AM[2] = AM[3] = 0;
    micLevel = 0;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
  }

  /* 模型说话时四颗方块跟着它的声音跳 */
  var pAn = null, pRaf = 0;
  function watchPlayer() {
    if (!ac || pAn || !player) return;
    try {
      var src = ac.createMediaElementSource(player);
      pAn = ac.createAnalyser();
      pAn.fftSize = 1024;
      pAn.smoothingTimeConstant = 0.66;
      src.connect(pAn);
      pAn.connect(ac.destination);
      var wave = new Uint8Array(pAn.fftSize);
      var fq = new Uint8Array(pAn.frequencyBinCount);
      var loop = function () {
        pRaf = requestAnimationFrame(loop);
        if (!pAn || phase !== 'speaking') return;
        micLevel = micLevel * 0.7 + readBands(pAn, wave, fq) * 0.3;
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
    var t0 = Date.now();
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
      var ms = Date.now() - t0;
      signal(ms < 900 ? 4 : ms < 2000 ? 3 : ms < 4000 ? 2 : 1);
    } catch (e) { signal(1); }

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
    micLevel = 0;
    AM[0] = AM[1] = AM[2] = AM[3] = 0;
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
    micLevel = 0;
    silenceAmount = 0;
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
      micStream.getTracks().forEach(function (tr) { try { tr.stop(); } catch (e) {} });
      micStream = null;
    }
    if (player) { try { player.pause(); } catch (e) {} }
    try { if (window.speechSynthesis) speechSynthesis.cancel(); } catch (e) {}
    dialT0 = 0;
    for (var k in ST) { ST[k].amount = 0; }
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
    // 调参时把形态定住：dwellCall.preview('thinking')
    preview: function (p) {
      build();
      box.classList.add('on');
      live = true;
      fit();
      startDraw();
      setPhase(p);
      if (ST[p]) ST[p].amount = 1;
    },
    P: P,
    // 手动喂音频，不打电话也能看四颗方块跳：dwellCall.fake(.6)
    fake: function (v) { micLevel = v; AM[0] = AM[1] = AM[2] = AM[3] = v; }
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
