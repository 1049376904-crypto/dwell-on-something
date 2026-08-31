"""dwell 语音条：录音发出去、他那条点一下念出来。

不改 web/index.html，也不改 frontend_feature.py——在 index 视图外面包一层，
把客户端脚本注在 </body> 前。服务端是独立的 voice 服务（127.0.0.1:8021，
由 nginx 以 /api/voice 反代），跟 Flask 这边只通过 HTTP 说话。

消息格式契约（两边别改岔了）：`[voice · 0:05] 转写文字`

⚠️ 自己那条语音的音频文件名存在浏览器 localStorage 里 —— 那行标记里不能
带文件名，否则模型会读到一串乱码。清了浏览器数据，旧语音就只剩文字。

⚠️ 两张表跟 voice_call.py 是共用的，键名和取键方式都得一致：
- `dwellVoiceFiles`：标记原文（含转写文字）→ 服务器上的录音文件名
- `dwellVoiceSaid` ：他说的正文 → TTS 缓存键（复听不用重新合成）
通话那边上传完会往第一张表写，所以退出通话后那条语音这边直接能播。
差一个空格就配不上，那条语音就只剩文字。
"""

import os
from pathlib import Path

VOICE_ENV = Path("/etc/voice.env")


def _voice_token() -> str:
    """鉴权 token：先看环境变量，再去 /etc/voice.env 捞。

    故意不写进仓库 —— 这个文件是要提交的，key 一个都不能进来。
    """
    tok = os.environ.get("VOICE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in VOICE_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("VOICE_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


CLIENT_SCRIPT = r"""
<style>
.vz{display:inline-flex;align-items:center;gap:9px;padding:7px 13px 7px 11px;
  border-radius:16px;background:rgba(127,127,127,.14);cursor:pointer;
  -webkit-touch-callout:none;-webkit-user-select:none;user-select:none;
  vertical-align:middle;min-width:104px}
.vz-ico{width:15px;height:15px;flex:0 0 auto;opacity:.85}
.vz-wave{display:flex;align-items:center;gap:2px;height:15px;flex:1 1 auto}
.vz-wave i{display:block;width:2px;border-radius:1px;background:currentColor;
  opacity:.5;height:4px;transition:height .12s linear}
.vz.on .vz-wave i{animation:vzb .9s ease-in-out infinite alternate}
.vz.on .vz-wave i:nth-child(2n){animation-delay:.15s}
.vz.on .vz-wave i:nth-child(3n){animation-delay:.3s}
@keyframes vzb{from{height:4px;opacity:.4}to{height:13px;opacity:.9}}
.vz-dur{font-size:12px;opacity:.6;font-variant-numeric:tabular-nums;flex:0 0 auto}
.vz-txt{margin-top:7px;font-size:14.5px;line-height:1.7;opacity:.82}
.vz-busy{opacity:.45}
.vzrec{display:flex;align-items:center;gap:10px;padding:8px 12px;margin-bottom:8px;
  border-radius:16px;background:rgba(127,127,127,.14);font-size:13px}
.vzrec-dot{width:8px;height:8px;border-radius:50%;background:#e2564a;flex:0 0 auto;
  animation:vzp 1.1s ease-in-out infinite}
@keyframes vzp{0%,100%{opacity:1}50%{opacity:.25}}
.vzrec-wave{display:flex;align-items:center;gap:2px;height:20px;flex:1 1 auto}
.vzrec-wave i{display:block;width:2px;border-radius:1px;background:currentColor;
  opacity:.55;height:3px}
.vzrec-t{font-variant-numeric:tabular-nums;opacity:.7;flex:0 0 auto}
.vzrec button{background:none;border:0;color:inherit;font-size:13px;padding:4px 8px;
  cursor:pointer;flex:0 0 auto}
.vzrec button.ok{font-weight:600}
#vzMic{display:inline-flex;align-items:center;justify-content:center}
#vzMic.rec{color:#e2564a}
</style>
<script>
(function () {
  if (window.dwellVoice) return;
  var TOKEN = '__VOICE_TOKEN__';
  var MARK = /^\[voice(?:\s*·\s*(\d+:\d+))?(?:\s*·\s*([a-z]+))?\]\s*/;
  /* ⚠️ 这两个键名跟 voice_call.py 是共用约定，改了两边就对不上。
     LSKEY : 标记原文（含转写文字）→ 服务器上的录音文件名
     LSSAID: 他说的正文           → TTS 缓存键 */
  var LSKEY = 'dwellVoiceFiles';
  var LSSAID = 'dwellVoiceSaid';

  function hdr(extra) {
    var h = extra || {};
    if (TOKEN) h['X-Voice-Token'] = TOKEN;
    return h;
  }
  function qtok() { return TOKEN ? '?t=' + encodeURIComponent(TOKEN) : ''; }
  function fmt(s) {
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function toast(msg) {
    try { if (typeof note === 'function') { note(msg, 'err'); return; } } catch (e) {}
    console.warn('[voice]', msg);
  }

  /* 自己那条语音的音频文件名存本地：那行标记里不能带文件名，
     否则模型会读到一串乱码。清了浏览器数据就只剩文字。 */
  function lsGet(k) {
    try { return JSON.parse(localStorage.getItem(k) || '{}'); } catch (e) { return {}; }
  }
  function lsPut(k, key, val) {
    if (!key || !val) return;
    var m = lsGet(k);
    m[key] = val;
    var ks = Object.keys(m);
    while (ks.length > 400) { delete m[ks.shift()]; }
    try { localStorage.setItem(k, JSON.stringify(m)); } catch (e) {}
  }
  function fileMap() { return lsGet(LSKEY); }
  function remember(key, name) { lsPut(LSKEY, key, name); }

  /* ── 录音 ───────────────────────────────────────────── */
  var rec = null, chunks = [], micStream = null, t0 = 0, tick = null;
  var recog = null, heard = '', strip = null, ac = null, an = null, raf = 0;

  /* iOS Safari 的 MediaRecorder 不一定支持 webm，实际多是 mp4/aac。
     服务端 AUDIO_EXTENSIONS 六种都收，所以探到哪个用哪个。 */
  function pickMime() {
    var cands = ['audio/mp4', 'audio/mp4;codecs=mp4a.40.2', 'audio/aac',
                 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < cands.length; i++) {
      if (MediaRecorder.isTypeSupported(cands[i])) return cands[i];
    }
    return '';
  }
  function extOf(mime) {
    mime = mime || '';
    if (mime.indexOf('webm') >= 0) return '.webm';
    if (mime.indexOf('ogg') >= 0) return '.ogg';
    if (mime.indexOf('mpeg') >= 0) return '.mp3';
    if (mime.indexOf('wav') >= 0) return '.wav';
    return '.m4a';
  }

  /* 本机听写。服务端没配 STT 时靠它出字；没有它就只发时长。 */
  function startRecog() {
    var R = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!R) return;
    try {
      recog = new R();
      recog.lang = 'zh-CN';
      recog.continuous = true;
      recog.interimResults = true;
      heard = '';
      recog.onresult = function (e) {
        var out = '';
        for (var i = 0; i < e.results.length; i++) {
          if (e.results[i].isFinal) out += e.results[i][0].transcript;
        }
        if (out) heard = out;
      };
      recog.onerror = function () {};
      recog.start();
    } catch (e) { recog = null; }
  }
  function stopRecog() {
    if (!recog) return;
    try { recog.stop(); } catch (e) {}
    recog = null;
  }

  function showStrip() {
    var composer = document.querySelector('.composer');
    if (!composer) return;
    strip = document.createElement('div');
    strip.className = 'vzrec';
    var bars = '';
    for (var i = 0; i < 22; i++) bars += '<i></i>';
    strip.innerHTML = '<span class="vzrec-dot"></span>' +
      '<span class="vzrec-wave">' + bars + '</span>' +
      '<span class="vzrec-t">0:00</span>' +
      '<button type="button" data-vz="cancel">取消</button>' +
      '<button type="button" class="ok" data-vz="ok">完成</button>';
    composer.insertBefore(strip, composer.firstChild);
    strip.querySelector('[data-vz="cancel"]').onclick = cancel;
    strip.querySelector('[data-vz="ok"]').onclick = finish;
    var tEl = strip.querySelector('.vzrec-t');
    tick = setInterval(function () {
      tEl.textContent = fmt((Date.now() - t0) / 1000);
      if (Date.now() - t0 > 120000) finish();   // 两分钟封顶
    }, 250);
  }
  function hideStrip() {
    if (tick) { clearInterval(tick); tick = null; }
    if (strip && strip.parentNode) strip.parentNode.removeChild(strip);
    strip = null;
  }

  /* 波形：取时域数据算音量，新的插最前面，旧的往后挤。 */
  function meter() {
    if (!micStream || !window.AudioContext) return;
    try {
      ac = new AudioContext();
      an = ac.createAnalyser();
      an.fftSize = 512;
      ac.createMediaStreamSource(micStream).connect(an);
      var buf = new Uint8Array(an.fftSize);
      var loop = function () {
        if (!an || !strip) return;
        an.getByteTimeDomainData(buf);
        var sum = 0;
        for (var i = 0; i < buf.length; i++) {
          var v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        var lvl = Math.min(1, Math.sqrt(sum / buf.length) * 3.4);
        var bars = strip.querySelectorAll('.vzrec-wave i');
        for (var j = bars.length - 1; j > 0; j--) {
          bars[j].style.height = bars[j - 1].style.height || '3px';
        }
        if (bars[0]) bars[0].style.height = (3 + lvl * 16).toFixed(1) + 'px';
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    } catch (e) {}
  }

  function cleanup() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    an = null;
    if (ac) { try { ac.close(); } catch (e) {} ac = null; }
    if (micStream) {
      micStream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) {} });
      micStream = null;
    }
    rec = null;
    hideStrip();
    var btn = document.getElementById('vzMic');
    if (btn) btn.classList.remove('rec');
  }

  async function start() {
    if (rec) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('（这个浏览器不给录音）'); return;
    }
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast('（没拿到麦克风权限）'); return;
    }
    var mime = pickMime();
    try {
      rec = mime ? new MediaRecorder(micStream, { mimeType: mime })
                 : new MediaRecorder(micStream);
    } catch (e) {
      try { rec = new MediaRecorder(micStream); }
      catch (e2) { toast('（录不了）'); cleanup(); return; }
    }
    chunks = [];
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    try { rec.start(200); } catch (e) { toast('（录不了）'); cleanup(); return; }
    t0 = Date.now();
    startRecog();
    showStrip();
    meter();
    var btn = document.getElementById('vzMic');
    if (btn) btn.classList.add('rec');
    // iOS Safari 上 navigator.vibrate 不存在，判断了再调
    if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) {} }
  }

  function cancel() {
    stopRecog();
    if (rec && rec.state !== 'inactive') { try { rec.stop(); } catch (e) {} }
    chunks = [];
    cleanup();
  }

  async function finish() {
    if (!rec) return;
    var dur = Math.max(1, Math.round((Date.now() - t0) / 1000));
    var mime = rec.mimeType || '';
    stopRecog();
    await new Promise(function (res) {
      if (!rec || rec.state === 'inactive') return res();
      rec.onstop = res;
      try { rec.stop(); } catch (e) { res(); }
    });
    var blob = new Blob(chunks, { type: mime || 'audio/mp4' });
    chunks = [];
    cleanup();
    if (!blob.size) { toast('（没录到声音）'); return; }

    var fd = new FormData();
    fd.append('file', blob, 'voice' + extOf(mime));
    fd.append('duration', String(dur));
    var msg = '[voice · ' + fmt(dur) + ']';
    var name = '';
    try {
      var r = await fetch('api/voice/message', { method: 'POST', headers: hdr(), body: fd });
      if (r.ok) {
        var d = await r.json();
        name = d.name || '';
        msg = d.message || msg;
        // 服务端没配 STT（默认路径），用本机听写兜底
        if (!d.text && heard) msg = ('[voice · ' + fmt(dur) + '] ' + heard).trim();
      } else {
        toast('（语音没传上去 ' + r.status + '）');
        return;
      }
    } catch (e) {
      toast('（语音没传上去）');
      return;
    }
    if (name) remember(msg.trim(), name);

    // 拿到那行标记，当普通消息发出去 —— 走上游自己的 send()，
    // 落库、重试、搜索全都照旧能用。
    var box = document.getElementById('box');
    var sendBtn = document.getElementById('send');
    if (!box || !sendBtn) return;
    box.value = msg;
    box.dispatchEvent(new Event('input', { bubbles: true }));
    sendBtn.click();
  }

  function mountMic() {
    if (document.getElementById('vzMic')) return;
    var row = document.querySelector('.composer .ctlrow');
    var sendBtn = document.getElementById('send');
    if (!row || !sendBtn) return;
    var b = document.createElement('button');
    b.id = 'vzMic';
    b.type = 'button';
    b.title = '说一句';
    b.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">' +
      '<rect x="9" y="3" width="6" height="11" rx="3"></rect>' +
      '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"></path></svg>';
    b.onclick = function (e) {
      e.preventDefault();
      if (rec) finish(); else start();
    };
    row.insertBefore(b, sendBtn);
  }

  /* ── 语音气泡 ────────────────────────────────────────── */
  var ttsCache = new Map();
  var playing = null;

  /* 他那条的音频地址。
     先看本地有没有存过 TTS 缓存键 —— 有就走 GET /api/voice/say/{key}，
     服务端直接吐磁盘上那份，一个字符都不花。
     没有才调合成，顺手把响应头里的键记下来，下次就免费了。 */
  async function saidUrl(text) {
    var key = lsGet(LSSAID)[text.trim()];
    if (key) return 'api/voice/say/' + key + qtok();
    if (ttsCache.has(text)) return ttsCache.get(text);
    var r = await fetch('api/voice/tts', {
      method: 'POST',
      headers: hdr({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text: text })
    });
    if (!r.ok) throw new Error('tts ' + r.status);
    var got = r.headers.get('X-TTS-Key') || '';
    if (got) lsPut(LSSAID, text.trim(), got);
    var url = URL.createObjectURL(await r.blob());
    ttsCache.set(text, url);
    return url;
  }

  /* 服务端那几档全哑时的兜底：让设备自己念，完全不联网。 */
  function deviceSay(text) {
    try {
      if (!window.speechSynthesis) return false;
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      speechSynthesis.speak(u);
      return true;
    } catch (e) { return false; }
  }

  function makePill(el, dur, transcript, mine) {
    var wrap = document.createElement('span');
    wrap.className = 'vz';
    var bars = '';
    for (var i = 0; i < 16; i++) bars += '<i></i>';
    wrap.innerHTML = '<svg class="vz-ico" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="1.9" stroke-linecap="round">' +
      '<path d="M11 5 6 9H3v6h3l5 4V5z"></path>' +
      '<path d="M15.5 8.5a5 5 0 0 1 0 7"></path></svg>' +
      '<span class="vz-wave">' + bars + '</span>' +
      '<span class="vz-dur">' + fmt(dur) + '</span>';

    var txt = document.createElement('div');
    txt.className = 'vz-txt';
    txt.textContent = transcript || '（没有文字）';
    txt.hidden = true;

    el.textContent = '';
    el.appendChild(wrap);
    el.appendChild(txt);

    var audio = null;
    async function play() {
      if (playing && playing !== audio) { try { playing.pause(); } catch (e) {} }
      if (audio) {
        if (!audio.paused) { audio.pause(); wrap.classList.remove('on'); return; }
        playing = audio; audio.play(); wrap.classList.add('on'); return;
      }
      wrap.classList.add('vz-busy');
      try {
        var url;
        if (mine) {
          var name = fileMap()[el._vzKey];
          if (!name) { toast('（这条的录音在这台设备上找不到了）'); return; }
          // <audio src> 带不了 header，token 只能走查询参数
          url = 'api/voice/file/' + encodeURIComponent(name) +
                (TOKEN ? '?t=' + encodeURIComponent(TOKEN) : '');
        } else {
          if (!transcript) { toast('（没有文字，念不出来）'); return; }
          url = await saidUrl(transcript);
        }
        audio = new Audio(url);
        audio.onloadedmetadata = function () {
          if (isFinite(audio.duration) && audio.duration > 0) {
            wrap.querySelector('.vz-dur').textContent = fmt(audio.duration);
          }
        };
        audio.onended = function () { wrap.classList.remove('on'); playing = null; };
        audio.onerror = function () { wrap.classList.remove('on'); toast('（播不出来）'); };
        playing = audio;
        await audio.play();
        wrap.classList.add('on');
      } catch (e) {
        if (!mine && transcript && deviceSay(transcript)) { /* 掉到设备自己念 */ }
        else toast('（念不出来）');
      } finally {
        wrap.classList.remove('vz-busy');
      }
    }

    /* 长按 420ms 看字。别挂 contextmenu —— 系统那个长按菜单会把
       语音条自己的长按整个吃掉，结果只弹出「拷贝」。 */
    var timer = null, longed = false;
    wrap.addEventListener('pointerdown', function () {
      longed = false;
      timer = setTimeout(function () {
        longed = true;
        txt.hidden = !txt.hidden;
        if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
      }, 420);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
      wrap.addEventListener(ev, function () {
        if (timer) { clearTimeout(timer); timer = null; }
      });
    });
    wrap.addEventListener('click', function (e) {
      e.preventDefault();
      if (longed) { longed = false; return; }
      play();
    });
  }

  function convert(el) {
    var mine = el.classList.contains('bubble');
    var raw = (el._raw != null ? el._raw : el.textContent) || '';
    raw = raw.trim();
    var m = MARK.exec(raw);
    if (!m) return false;
    var transcript = raw.slice(m[0].length).trim();
    var dur = 0;
    if (m[1]) {
      var p = m[1].split(':');
      dur = parseInt(p[0], 10) * 60 + parseInt(p[1], 10);
    } else if (transcript) {
      // 他不知道自己要念多少秒，先按 4.5 字/秒估，播的时候再校准
      dur = Math.max(1, Math.round(transcript.length / 4.5));
    }
    el.dataset.voice = '1';
    el.dataset.rich = '1';        // 别让上游的 renderRich 再动它
    el._vzKey = raw;
    makePill(el, dur, transcript, mine);
    return true;
  }

  function scan() {
    var els = document.querySelectorAll(
      '#log .gu:not([data-voice]), #log .bubble:not([data-voice])');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var raw = ((el._raw != null ? el._raw : el.textContent) || '').trim();
      if (raw.indexOf('[voice') !== 0) continue;
      // 自己那条一到就转；他那条要等说完，不然会把正在流的字截断
      if (el.classList.contains('bubble') || el._final === true) { convert(el); continue; }
      if (el._vzLen === raw.length && Date.now() - (el._vzAt || 0) > 1500) { convert(el); continue; }
      if (el._vzLen !== raw.length) { el._vzLen = raw.length; el._vzAt = Date.now(); }
    }
  }

  function boot() {
    mountMic();
    scan();
    var log = document.getElementById('log');
    if (log && window.MutationObserver) {
      new MutationObserver(function () { scan(); })
        .observe(log, { childList: true, subtree: true, characterData: true });
    }
    // 兜底轮询：面板来回切换会把输入区重建，话筒得能自己长回去
    setInterval(function () { mountMic(); scan(); }, 900);
  }

  window.dwellVoice = {
    start: start, finish: finish, cancel: cancel, scan: scan,
    // 查复听配没配上：files 是录音，said 是合成缓存键
    tables: function () { return { files: lsGet(LSKEY), said: lsGet(LSSAID) }; }
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_voice_feature(server_module):
    """包住 frontend_feature 注册的 index，把语音脚本注进去。

    必须排在 register_frontend_feature 之后 —— 它包的是那一层。
    排在 panel_shell 之后也行：那个走 after_request，跟这里不冲突。
    """
    app = server_module.app
    script = CLIENT_SCRIPT.replace("__VOICE_TOKEN__", _voice_token())
    server_module.voice_client_script = script

    original = app.view_functions.get("index")
    if original is None:
        # frontend_feature 没注册成功，语音就当不存在，别把首页拖下水
        return

    def index_with_voice(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            if "text/html" not in (resp.headers.get("Content-Type") or ""):
                return resp
            html = resp.get_data(as_text=True)
        except Exception:
            return resp
        if "window.dwellVoice" in html or "</body>" not in html:
            return resp
        resp.set_data(html.replace("</body>", script + "</body>", 1))
        return resp

    app.view_functions["index"] = index_with_voice
