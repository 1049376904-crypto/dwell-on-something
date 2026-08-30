"""dwell 语音通话：对讲机模式。

点电话进全屏，形状跟着你的声音变；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
转写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层，那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

视觉是 2D 矢量扁平纯黑，四个形态：正圆 / 波浪圆 / 思想气泡 / 横排四点。
形态之间靠 metaball 场强融合 + 节点弹簧插值，连续变形不硬切。
调参：`dwellCall.preview('thinking')` 把形态定住，SHAPES 和 FIELD
是那两张参数表，改完立刻生效。

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
  var INK = [21, 21, 26];              // 纯黑那个黑

  /* ═══ metaball 场强参数 ═══════════════════════════════════════
     每颗圆贡献 w * r² / d² 的场强，超过 T 的像素填黑。圆靠近时场强
     叠加，边界自然消融 —— 这就是"平滑合并"，比手画贝塞尔靠谱。
     T 越小融得越狠（形状会胀），EDGE 是边缘软化宽度，当抗锯齿用。 */
  var FIELD = { T: 1.0, EDGE: 0.14, POW: 1.0 };

  /* ═══ 四个形态：每个都是一组节点 ═══════════════════════════════
     x/y 是相对半径 R 的偏移，r 是相对 R 的半径，w 是场强权重。
     节点数必须一样多（8 个），换态时一一对应做弹簧插值，
     所以圆分裂成四颗胶囊那一下是连续变形的。
     用不到的节点把 w 压到 0，它就"消失"但位置还在插值里。 */
  var SHAPES = {
    // 一颗正圆：主节点独大，其余缩在中心权重为 0
    idle: [
      { x: 0, y: 0, r: 1.00, w: 1 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }
    ],
    // 听：还是一颗圆，波浪加在边缘采样上（见 waveAt）
    listening: [
      { x: 0, y: 0, r: 0.96, w: 1 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }, { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }
    ],
    /* 思想气泡：上方四颗互相粘连成云朵，左下角一颗独立小点留空隙。
       四颗的圆心距要小于半径和，不然融不到一起。 */
    thinking: [
      { x: -0.30, y: -0.16, r: 0.58, w: 1 },
      { x:  0.26, y: -0.24, r: 0.50, w: 1 },
      { x:  0.40, y:  0.16, r: 0.42, w: 1 },
      { x: -0.10, y:  0.26, r: 0.52, w: 1 },
      { x: -0.62, y:  0.72, r: 0.17, w: 1 },   // 那颗独立小点
      { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 },
      { x: 0, y: 0, r: 0.30, w: 0 }
    ],
    /* 横排四点：左两颗偏宽（sx 拉横），右两颗趋正圆，互不粘连。
       间距要够大，不然会融成一条虫。 */
    speaking: [
      { x: -0.95, y: 0, r: 0.34, w: 1, sx: 1.34 },
      { x: -0.32, y: 0, r: 0.33, w: 1, sx: 1.20 },
      { x:  0.32, y: 0, r: 0.31, w: 1, sx: 1.04 },
      { x:  0.95, y: 0, r: 0.30, w: 1, sx: 1.00 },
      { x: 0, y: 0, r: 0.20, w: 0 }, { x: 0, y: 0, r: 0.20, w: 0 },
      { x: 0, y: 0, r: 0.20, w: 0 }, { x: 0, y: 0, r: 0.20, w: 0 }
    ]
  };
  var N = 8;

  /* ═══ 状态机：形态之外的运动参数 ═════════════════════════════
     k/d 形态插值的弹簧刚度与阻尼，breathe 呼吸幅度，period 周期，
     wave 边缘波浪幅度，spin 整体自转，wobble 内部起伏。 */
  var SPEC = {
    idle:      { k: 46,  d: 8.0, breathe: .034, period: 5200, wave: 0,
                 spin: 0,    wobble: .010, label: 'Idle' },
    listening: { k: 92,  d: 9.5, breathe: .008, period: 4200, wave: 1,
                 spin: 0,    wobble: .014, label: 'Listening' },
    thinking:  { k: 62,  d: 8.6, breathe: .016, period: 3000, wave: .35,
                 spin: .085, wobble: .040, label: 'Thinking' },
    speaking:  { k: 150, d: 7.2, breathe: .030, period: 1300, wave: .22,
                 spin: 0,    wobble: .022, label: 'Speaking' }
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

  /* ═══ 阻尼谐振弹簧 ═══════════════════════════════════════════
     lerp 永远追不过头，所以显得机械。弹簧会过冲目标再回弹 settling，
     重量感就是从这儿来的。a = (target - x) * k - v * d */
  function Spring(x0, k, d) { this.x = x0; this.v = 0; this.k = k; this.d = d; }
  Spring.prototype.to = function (t, dt) {
    dt = Math.min(dt, 0.032);          // 切后台回来那一下 dt 会很大，不钳会炸
    this.v += ((t - this.x) * this.k - this.v * this.d) * dt;
    this.x += this.v * dt;
    return this.x;
  };
  Spring.prototype.tune = function (k, d) { this.k = k; this.d = d; };

  /* 每个节点四根弹簧（x / y / r / w），加一根横向拉伸 */
  var nodes = [];
  for (var i0 = 0; i0 < N; i0++) {
    nodes.push({
      x: new Spring(0, 46, 8), y: new Spring(0, 46, 8),
      r: new Spring(i0 === 0 ? 1 : .3, 46, 8),
      w: new Spring(i0 === 0 ? 1 : 0, 46, 8),
      sx: new Spring(1, 46, 8)
    });
  }
  var sBreathe = new Spring(.034, 40, 9);
  var sWave = new Spring(0, 40, 9);
  var sSpin = new Spring(0, 30, 9);
  var sWob = new Spring(.01, 30, 9);

  /* 四通道频率：低 / 中 / 高 / 总能量 */
  var A = { lo: 0, mid: 0, hi: 0, all: 0 };
  var loud = 0;              // listening 用：涨得快落得慢
  var t = 0, spinA = 0;
  var drawRaf = 0, lastT = 0, W = 0, H = 0, R = 0, DPR = 1;

  /* 场强渲染是逐像素的，全分辨率在 iPad 上跑不满 60fps，
     所以画在 1/SS 分辨率的离屏上再放大。边缘那点软化正好当抗锯齿。 */
  var SS = 2;
  var off = null, octx = null, oimg = null;

  function fit() {
    if (!cv || !stage) return;
    var side = Math.min(window.innerWidth * 0.92, window.innerHeight * 0.5, 460);
    stage.style.width = side + 'px';
    stage.style.height = side + 'px';
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(side * DPR);
    cv.height = Math.round(side * DPR);
    W = cv.width; H = cv.height;
    R = Math.min(W, H) * 0.215;

    var ow = Math.max(1, Math.round(W / SS)), oh = Math.max(1, Math.round(H / SS));
    if (!off) {
      off = document.createElement('canvas');
      octx = off.getContext('2d');
    }
    off.width = ow; off.height = oh;
    oimg = octx.createImageData(ow, oh);
    ctx.imageSmoothingEnabled = true;
  }
  var fitTimer = null;
  function fitLater() {
    if (fitTimer) return;
    fitTimer = setTimeout(function () { fitTimer = null; fit(); }, 140);
  }

  /* 边缘波浪：listening 时跟着音量起伏，thinking 时慢慢自己动 */
  function waveAt(th, amp) {
    return 1
      + Math.sin(th * 3 + t * 1.15) * amp * 0.055
      + Math.sin(th * 5 - t * 0.82) * amp * 0.032
      + Math.sin(th * 2 + t * 0.47) * amp * 0.026;
  }

  function frame(now) {
    drawRaf = requestAnimationFrame(frame);
    if (!ctx || !live || !oimg) return;

    var dt = lastT ? (now - lastT) / 1000 : 0.016;
    lastT = now;
    t += dt;
    var s = SPEC[phase] || SPEC.idle;
    var want = SHAPES[phase] || SHAPES.idle;

    // listening：涨得快落得慢。同一根弹簧涨落对称，停下来会"弹回去"，
    // 那个手感不对 —— 要的是慢慢恢复，不猛缩。
    var lvl = A.all;
    loud = lvl > loud ? loud + (lvl - loud) * Math.min(1, dt * 9)
                      : loud + (lvl - loud) * Math.min(1, dt * 1.7);

    var breathe = sBreathe.to(s.breathe, dt);
    var wave = sWave.to(s.wave, dt);
    var spin = sSpin.to(s.spin, dt);
    var wob = sWob.to(s.wobble, dt);

    spinA += spin * dt;

    // 整体呼吸：idle/speaking 走正弦，listening 由音量推
    var puff = 1 + Math.sin(t / (s.period / 1000) * Math.PI * 2) * breathe;
    if (phase === 'listening') puff = 1 + Math.min(0.20, loud * 1.5);
    if (phase === 'speaking') puff += A.all * 0.10;

    // 节点插值到目标形态
    var pts = [];
    for (var i = 1, j = 0; j < N; j++) {
      var wn = want[j], nd = nodes[j];
      nd.x.tune(s.k, s.d); nd.y.tune(s.k, s.d);
      nd.r.tune(s.k, s.d); nd.w.tune(s.k, s.d); nd.sx.tune(s.k, s.d);

      // 说话时每颗胶囊各跟一段频谱：低频推左边，高频推右边
      var kick = 1;
      if (phase === 'speaking') {
        var band = [A.lo, A.mid, A.mid * .8 + A.hi * .2, A.hi][j] || 0;
        kick = 1 + band * 1.15;
      }
      // 思考时内部微微起伏，各节点错开相位
      var breath2 = 1 + Math.sin(t * 1.25 + j * 1.7) * wob;

      var nx = nd.x.to(wn.x, dt), ny = nd.y.to(wn.y, dt);
      var nr = nd.r.to(wn.r, dt) * kick * breath2;
      var nw = nd.w.to(wn.w, dt);
      var nsx = nd.sx.to(wn.sx || 1, dt);
      if (nw <= 0.004) continue;      // 权重没了就不参与场强，省一层循环

      // thinking 时整团绕中心转
      var ca = Math.cos(spinA), sa = Math.sin(spinA);
      pts.push({
        x: (nx * ca - ny * sa) * R * puff,
        y: (nx * sa + ny * ca) * R * puff,
        r: nr * R * puff,
        w: nw,
        sx: nsx
      });
    }

    // ── 场强扫描：超阈值填黑，边缘按距离软化
    var ow = off.width, oh = off.height;
    var data = oimg.data;
    var ocx = ow / 2, ocy = oh / 2, k = 1 / SS;
    var T = FIELD.T, EDGE = FIELD.EDGE;
    var np = pts.length;

    // 预乘：场强用 (w*r²)/d²，先把 w*r² 算出来
    var qx = new Float32Array(np), qy = new Float32Array(np);
    var qn = new Float32Array(np), qs = new Float32Array(np);
    for (var p = 0; p < np; p++) {
      qx[p] = ocx + pts[p].x * k;
      qy[p] = ocy + pts[p].y * k;
      qn[p] = pts[p].w * (pts[p].r * k) * (pts[p].r * k);
      qs[p] = 1 / (pts[p].sx * pts[p].sx);   // 横向拉伸：压 dx²
    }

    for (var y = 0, idx = 0; y < oh; y++) {
      for (var x = 0; x < ow; x++, idx += 4) {
        var f = 0;
        for (var q = 0; q < np; q++) {
          var dx = x - qx[q], dy = y - qy[q];
          var d2 = dx * dx * qs[q] + dy * dy;
          if (d2 < 0.35) { f += 40; continue; }   // 圆心附近别除爆
          f += qn[q] / d2;
        }
        var a;
        if (f >= T + EDGE) a = 255;
        else if (f <= T - EDGE) a = 0;
        else {
          // 阈值附近平滑过渡，当抗锯齿
          var u = (f - (T - EDGE)) / (EDGE * 2);
          a = Math.round(255 * u * u * (3 - 2 * u));
        }
        if (a) {
          data[idx] = INK[0]; data[idx + 1] = INK[1]; data[idx + 2] = INK[2];
        }
        data[idx + 3] = a;
      }
    }
    octx.putImageData(oimg, 0, 0);

    // ── 波浪：listening 时靠裁剪路径削一层，比塞进场强便宜得多
    ctx.clearRect(0, 0, W, H);
    var amp = wave * (0.35 + loud * 2.4);
    if (amp > 0.02 && !slow && (phase === 'listening' || phase === 'idle')) {
      ctx.save();
      ctx.beginPath();
      var rr = R * puff * 1.32;
      for (var i2 = 0; i2 <= 96; i2++) {
        var th = i2 / 96 * Math.PI * 2;
        var v = rr * waveAt(th, amp);
        var px = W / 2 + Math.cos(th) * v, py = H / 2 + Math.sin(th) * v;
        if (i2 === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.clip();
      ctx.drawImage(off, 0, 0, W, H);
      ctx.restore();
    } else {
      ctx.drawImage(off, 0, 0, W, H);
    }

    if (timeEl && dialT0) timeEl.textContent = fmt((Date.now() - dialT0) / 1000);
  }

  function startDraw() {
    if (drawRaf) return;
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

  /* ═══ 音频分析 ═══════════════════════════════════════════════ */
  function bands(node, wave, fq) {
    node.getByteTimeDomainData(wave);
    var sum = 0;
    for (var i = 0; i < wave.length; i++) {
      var v = (wave[i] - 128) / 128;
      sum += v * v;
    }
    node.getByteFrequencyData(fq);
    var n = fq.length, b1 = (n * .08) | 0, b2 = (n * .32) | 0;
    var s1 = 0, s2 = 0, s3 = 0;
    for (var j = 0; j < b1; j++) s1 += fq[j];
    for (var j2 = b1; j2 < b2; j2++) s2 += fq[j2];
    for (var j3 = b2; j3 < n; j3++) s3 += fq[j3];
    A.lo = A.lo * .7 + (s1 / b1 / 255) * .3;
    A.mid = A.mid * .7 + (s2 / (b2 - b1) / 255) * .3;
    A.hi = A.hi * .7 + (s3 / (n - b2) / 255) * .3;
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
        // thinking / speaking 时麦克风不驱动画面：thinking 要"自己动"，
        // speaking 那会儿麦克风收到的是喇叭回声
        var lvl = bands(an, wave, freqArr);
        if (phase === 'listening' || phase === 'idle') A.all = A.all * .72 + lvl * .28;

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
    loud = 0;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
  }

  /* 模型说话时让形状跟着它的声音跳：接一路分析器到播放器上 */
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
        A.all = A.all * .68 + bands(pAn, wave, fq) * .32;
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
    loud = 0;
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
      if (!SPEC[p]) return;
      build();
      box.classList.add('on');
      live = true;
      fit();
      startDraw();
      setPhase(p);
    },
    spec: SPEC,
    shapes: SHAPES,
    field: FIELD,
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
