"""dwell 语音通话：对讲机模式。

点电话进全屏，球跟着你的音量涨落；你停下来一秒半就算说完，自动上传、
自动发出去、模型回话自动念出来、念完自动接着听。整通电话不用碰屏幕。

⚠️ 这是**回合制**，不是 ChatGPT 那种实时双向流。一轮的等待是：
听写 ~1s + 模型写完 2~4s + ElevenLabs 合成 1~3s，加网络大概 5~8 秒，
中间打断不了。要做到真通话得换整条底层（流式输出 + 边出字边分句合成 +
边说边转写 + 打断处理），那是另一件事。

⚠️ 每一句都要合成，ElevenLabs 按字符收钱。界面上那个计数就是这通电话
花掉的字符数，别当装饰看。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from voice_feature import _voice_token

CLIENT_SCRIPT = r"""
<style>
#vcall{position:fixed;inset:0;z-index:9999;display:none;flex-direction:column;
  align-items:center;justify-content:center;gap:0;
  background:radial-gradient(120% 90% at 50% 12%,#232326 0%,#0d0d0f 62%,#050506 100%);
  color:#f2f1ec;-webkit-user-select:none;user-select:none}
#vcall.on{display:flex}
.vc-orb-box{position:relative;width:212px;height:212px;display:flex;
  align-items:center;justify-content:center}
.vc-orb{width:150px;height:150px;border-radius:50%;
  background:radial-gradient(38% 34% at 34% 28%,#6f6f78 0%,#33333a 42%,#111114 78%,#08080a 100%);
  box-shadow:0 0 60px rgba(150,150,170,.20),inset 0 0 34px rgba(0,0,0,.62);
  transform:scale(1);transition:transform .1s ease-out}
.vc-halo{position:absolute;width:150px;height:150px;border-radius:50%;
  border:1px solid rgba(220,220,235,.16);opacity:0;transform:scale(1)}
#vcall.listening .vc-halo{animation:vch 2.4s ease-out infinite}
@keyframes vch{0%{opacity:.5;transform:scale(1)}100%{opacity:0;transform:scale(1.42)}}
#vcall.thinking .vc-orb{animation:vcs 1.5s ease-in-out infinite alternate}
@keyframes vcs{from{transform:scale(.97);filter:brightness(.85)}
  to{transform:scale(1.03);filter:brightness(1.12)}}
#vcall.speaking .vc-orb{box-shadow:0 0 92px rgba(178,178,205,.34),inset 0 0 34px rgba(0,0,0,.55)}
.vc-state{margin-top:26px;font-size:15px;letter-spacing:.08em;opacity:.62;min-height:22px}
.vc-said{margin-top:14px;max-width:min(78vw,420px);text-align:center;font-size:14px;
  line-height:1.75;opacity:.42;min-height:24px}
.vc-meta{position:absolute;top:calc(env(safe-area-inset-top,0px) + 20px);left:0;right:0;
  text-align:center;font-size:11.5px;letter-spacing:.06em;opacity:.3;
  font-variant-numeric:tabular-nums}
.vc-btns{position:absolute;bottom:calc(env(safe-area-inset-bottom,0px) + 48px);
  display:flex;gap:20px;align-items:center}
.vc-btn{width:60px;height:60px;border-radius:50%;border:0;cursor:pointer;
  display:flex;align-items:center;justify-content:center;color:#f2f1ec;
  background:rgba(255,255,255,.10);backdrop-filter:blur(8px)}
.vc-btn.hang{background:#c8453c}
.vc-btn.mute.off{opacity:.42}
#vcBtn{display:inline-flex;align-items:center;justify-content:center}
</style>
<script>
(function () {
  if (window.dwellCall) return;
  var TOKEN = '__VOICE_TOKEN__';
  var CALL_HINT = '[通话中]';

  var box, orb, stateEl, saidEl, metaEl, muteBtn;
  var live = false, phase = 'idle', muted = false;
  var micStream = null, rec = null, chunks = [], recT0 = 0;
  var ac = null, an = null, raf = 0, silentSince = 0, spoke = false;
  var player = null, chars = 0, turns = 0, stopAll = false;

  var MIN_MS = 700;          // 太短的当噪音扔掉
  var MAX_MS = 30000;        // 一轮封顶，别让它录到天亮
  var HUSH_MS = 1400;        // 安静这么久就算说完了
  var HUSH_LVL = 0.055;      // 低于这个音量算安静

  function hdr(extra) {
    var h = extra || {};
    if (TOKEN) h['X-Voice-Token'] = TOKEN;
    return h;
  }
  function setPhase(p, label) {
    phase = p;
    if (!box) return;
    box.classList.remove('listening', 'thinking', 'speaking');
    if (p !== 'idle') box.classList.add(p);
    if (stateEl && label != null) stateEl.textContent = label;
  }
  function meta() {
    if (metaEl) metaEl.textContent = turns + ' 轮 · ' + chars + ' 字符';
  }
  function said(t) { if (saidEl) saidEl.textContent = t || ''; }

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
  function fmt(s) {
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  /* ── 界面 ─────────────────────────────────────────────── */
  function build() {
    if (box) return;
    box = document.createElement('div');
    box.id = 'vcall';
    box.innerHTML =
      '<div class="vc-meta"></div>' +
      '<div class="vc-orb-box"><span class="vc-halo"></span><span class="vc-orb"></span></div>' +
      '<div class="vc-state"></div>' +
      '<div class="vc-said"></div>' +
      '<div class="vc-btns">' +
        '<button type="button" class="vc-btn mute" data-vc="mute" title="静音">' +
          '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.8" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3"/>' +
          '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/></svg></button>' +
        '<button type="button" class="vc-btn hang" data-vc="hang" title="挂断">' +
          '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="1.8" stroke-linecap="round"><path d="M3 8.5c6-4 12-4 18 0l-2.6 3.2-3.5-1.2' +
          '-.6-2.4c-2.5-.7-5.1-.7-7.6 0l-.6 2.4-3.5 1.2z"/></svg></button>' +
      '</div>';
    document.body.appendChild(box);
    orb = box.querySelector('.vc-orb');
    stateEl = box.querySelector('.vc-state');
    saidEl = box.querySelector('.vc-said');
    metaEl = box.querySelector('.vc-meta');
    muteBtn = box.querySelector('[data-vc="mute"]');
    box.querySelector('[data-vc="hang"]').onclick = hang;
    muteBtn.onclick = function () {
      muted = !muted;
      muteBtn.classList.toggle('off', muted);
      if (micStream) micStream.getTracks().forEach(function (t) { t.enabled = !muted; });
    };
  }

  /* ── 音量表：听的时候球跟着涨落，同时用它判断你说完了没 ── */
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
        an.getByteTimeDomainData(buf);
        var sum = 0;
        for (var i = 0; i < buf.length; i++) {
          var v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        var lvl = Math.sqrt(sum / buf.length);
        if (phase === 'listening' && orb) {
          orb.style.transform = 'scale(' + (1 + Math.min(0.34, lvl * 2.1)).toFixed(3) + ')';
        }
        if (phase === 'listening' && !muted) {
          var now = Date.now();
          if (lvl > HUSH_LVL) { spoke = true; silentSince = 0; }
          else if (spoke) {
            if (!silentSince) silentSince = now;
            else if (now - silentSince > HUSH_MS) turnDone();
          }
          if (now - recT0 > MAX_MS) turnDone();
        }
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    } catch (e) {}
  }
  function stopMeter() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    an = null;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
  }

  /* ── 一轮：听 → 传 → 发 → 等 → 念 ──────────────────── */
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
    if (orb) orb.style.transform = 'scale(1)';
    setPhase('listening', '在听…');
    startMeter();
  }

  function turnDone() {
    if (phase !== 'listening') return;
    setPhase('thinking', '');
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
      var m = (d.msgs || []);
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
    setPhase('thinking', '想…');
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
    } catch (e) {}
    // 手机自带的听写在通话里不跑（它跟录音抢同一个音频会话），
    // 所以这行多半只有时长。模型看得懂 [通话中] 那行，会问回来。
    said(line.replace(/^\[voice[^\]]*\]\s*/, '') || '（说了 ' + fmt(dur) + '）');

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
    said(reply);
    await speak(reply);
    if (live && !stopAll) listen();
  }

  async function speak(text) {
    setPhase('speaking', '');
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

  /* ── 进出 ─────────────────────────────────────────────── */
  async function dial() {
    if (live) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    build();
    // iOS 只在用户手势里放开播放权限：借这一下点击把播放器解锁，
    // 之后整通电话都复用它，不然模型第一句就哑在那儿。
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
    muteBtn.classList.remove('off');
    meta();
    said('');
    setPhase('thinking', '接通中…');
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch (e) {
      setPhase('idle', '没拿到麦克风权限');
      setTimeout(hang, 1400);
      return;
    }
    listen();
  }

  function hang() {
    stopAll = true;
    live = false;
    stopMeter();
    if (rec && rec.state !== 'inactive') { try { rec.stop(); } catch (e) {} }
    rec = null;
    chunks = [];
    if (micStream) {
      micStream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) {} });
      micStream = null;
    }
    if (player) { try { player.pause(); } catch (e) {} }
    try { if (window.speechSynthesis) speechSynthesis.cancel(); } catch (e) {}
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
    b.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M6.5 3h3l1.5 4-2 1.5a11 11 0 0 0 5.5 5.5L16 12l4 1.5v3a2 2 0 0 1-2.2 2A16 16 0 0 1 4 6.2' +
      'A2 2 0 0 1 6 4z"/></svg>';
    b.onclick = function (e) { e.preventDefault(); dial(); };
    row.insertBefore(b, mic || sendBtn);
  }

  window.dwellCall = { dial: dial, hang: hang };
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
