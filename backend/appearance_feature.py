"""dwell 外观：气泡头像、时间戳，以及一个能实时调的外观面板。

不改 web/index.html。在 index 视图外面再包一层，把客户端脚本注进去。

⚠️ 这一层比语音那套深：头像要插进上游 `.row` 的结构里（`row()` 建行、
`addMe()` 往里塞 `.bubble`、`ensureGu()` 塞 `.gu`）。上游哪天改了那几个
函数，头像就不显示了 —— 不会崩，但会静默失效。这是注入路线的代价。

设置存在 settings 表里，不用 localStorage：换个设备就没了，而且
iPad 和手机会看到不一样的样子。

面板是主页里的抽屉浮层，不是 iframe 面板。滑块要实时预览 —— 拖头像
大小的时候后面的聊天得立刻跟着变，隔着 iframe 看不见。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from flask import jsonify, request, send_file

# 自由 CSS 那一栏的上限。写错了顶多这一页难看，但别让它把库撑大。
CSS_MAX = 20000

SETTINGS_KEY = "appearance"

DEFAULTS = {
    "avatarSize": 34,       # 头像直径 px
    "fontSize": 14.5,       # 气泡正文 px
    "bubbleRadius": 18,     # 气泡圆角 px
    "rowGap": 16,           # 两条消息之间 px
    "timeSize": 10.5,       # 时间戳 px
    "showAvatar": True,
    "showTime": True,
    "css": "",              # 自由 CSS，原样注入
}

NUM_RANGE = {
    "avatarSize": (18, 72),
    "fontSize": (11, 22),
    "bubbleRadius": (0, 28),
    "rowGap": (2, 40),
    "timeSize": (8, 16),
}

ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}


def _clean(raw: dict) -> dict:
    """把传进来的设置钳到合法范围。坏值一律回落到默认，不报错。"""
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for key, (low, high) in NUM_RANGE.items():
        if key in raw:
            try:
                out[key] = max(low, min(high, float(raw[key])))
            except (TypeError, ValueError):
                pass
    for key in ("showAvatar", "showTime"):
        if key in raw:
            out[key] = bool(raw[key])
    if "css" in raw and isinstance(raw["css"], str):
        out["css"] = raw["css"][:CSS_MAX]
    return out


CLIENT_SCRIPT = r"""
<style id="apv-base">
:root{
  --apv-av:34px; --apv-font:14.5px; --apv-radius:18px;
  --apv-gap:16px; --apv-time:10.5px;
}
/* 挂了头像的行改成两列：头像那一列 + 气泡。
   上游 .row 是块级、.row.me 是 flex 靠右，这里两种都统一成 flex。 */
#log .row.apv{display:flex;align-items:flex-start;gap:9px;
  margin-bottom:var(--apv-gap)}
#log .row.me.apv{flex-direction:row-reverse;justify-content:flex-start}
#log .row.apv > .bubble,#log .row.apv > .gu{min-width:0}

.apv-side{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;
  gap:3px;width:var(--apv-av);padding-top:2px}
.apv-av{width:var(--apv-av);height:var(--apv-av);border-radius:50%;
  background:var(--panel,#f0eee6) center/cover no-repeat;
  display:flex;align-items:center;justify-content:center;
  font-size:calc(var(--apv-av) * .42);color:var(--dim,#8a867c);
  font-weight:500;overflow:hidden;-webkit-user-select:none;user-select:none}
.apv-time{font-size:var(--apv-time);line-height:1.25;color:var(--dim,#8a867c);
  opacity:.72;white-space:nowrap;font-variant-numeric:tabular-nums;
  text-align:center;letter-spacing:.01em}
.apv-hide-av .apv-av{display:none}
.apv-hide-av .apv-side{width:auto}
.apv-hide-time .apv-time{display:none}
/* 两个都关掉时那一列就没用了，收掉免得留一道空隙 */
.apv-hide-av.apv-hide-time .apv-side{display:none}

#log .row.apv > .bubble,#log .row.apv > .gu{font-size:var(--apv-font)}
#log .row.apv > .bubble{border-radius:var(--apv-radius)}

/* ── 面板。借上游 .sheetWrap/.sheet 那套壳，样式跟日记那几页一致 ── */
#apvSheet .sheet{padding-bottom:calc(env(safe-area-inset-bottom,0px) + 18px)}
.apv-h{font-size:19px;font-weight:600;margin:2px 0 14px;color:var(--text,#2b2a27)}
.apv-row{display:flex;align-items:center;gap:12px;padding:9px 0}
.apv-row label{flex:0 0 78px;font-size:13.5px;color:var(--dim,#8a867c)}
.apv-row input[type=range]{flex:1 1 auto;-webkit-appearance:none;appearance:none;
  height:26px;background:transparent}
.apv-row input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:2px;
  background:var(--line,#e8e5dc)}
.apv-row input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
  width:20px;height:20px;border-radius:50%;background:#fff;margin-top:-8.5px;
  border:1px solid var(--line,#e8e5dc);box-shadow:0 1px 4px rgba(0,0,0,.14)}
.apv-val{flex:0 0 42px;text-align:right;font-size:12.5px;color:var(--dim,#8a867c);
  font-variant-numeric:tabular-nums}
.apv-sw{margin-left:auto;-webkit-appearance:none;appearance:none;width:44px;height:26px;
  border-radius:999px;background:var(--line,#e8e5dc);position:relative;
  transition:background .2s ease;flex:0 0 auto;border:0;cursor:pointer}
.apv-sw::after{content:'';position:absolute;top:3px;left:3px;width:20px;height:20px;
  border-radius:50%;background:#fff;transition:transform .2s ease;
  box-shadow:0 1px 3px rgba(0,0,0,.18)}
.apv-sw.on{background:var(--accent,#c96442)}
.apv-sw.on::after{transform:translateX(18px)}
.apv-avs{display:flex;gap:14px;padding:6px 0 2px}
.apv-pick{flex:1 1 0;display:flex;align-items:center;gap:10px;padding:10px;
  border:1px dashed var(--line,#e8e5dc);border-radius:14px;cursor:pointer;
  font-size:13px;color:var(--dim,#8a867c);min-width:0}
.apv-pick span.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.apv-pick i{flex:0 0 auto;width:38px;height:38px;border-radius:50%;
  background:var(--panel,#f0eee6) center/cover no-repeat;
  display:flex;align-items:center;justify-content:center;font-style:normal;
  font-size:15px;color:var(--dim,#8a867c)}
.apv-pick input{display:none}
.apv-css{width:100%;box-sizing:border-box;min-height:104px;resize:vertical;
  background:var(--panel,#f0eee6);border:1px solid transparent;border-radius:14px;
  padding:11px 13px;color:var(--text,#2b2a27);font-size:12.5px;line-height:1.6;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.apv-note{font-size:12px;color:var(--dim,#8a867c);opacity:.8;margin:7px 0 0;
  line-height:1.6}
.apv-btns{display:flex;gap:10px;margin-top:16px}
.apv-btns button{flex:1 1 0;min-height:44px;border-radius:999px;border:0;
  background:var(--panel,#f0eee6);color:var(--text,#2b2a27);font-size:14.5px;
  font-family:inherit;cursor:pointer}
.apv-btns button.go{background:var(--accent,#c96442);color:#fff}
.apv-sep{height:1px;background:var(--line,#e8e5dc);margin:14px 0;opacity:.7}
</style>
<style id="apv-user"></style>
<script>
(function () {
  if (window.dwellAppearance) return;

  var DEF = {
    avatarSize: 34, fontSize: 14.5, bubbleRadius: 18, rowGap: 16,
    timeSize: 10.5, showAvatar: true, showTime: true, css: ''
  };
  var cfg = null;
  var META = [
    ['avatarSize', '头像', 18, 72, 1],
    ['fontSize', '字号', 11, 22, 0.5],
    ['bubbleRadius', '圆角', 0, 28, 1],
    ['rowGap', '行距', 2, 40, 1],
    ['timeSize', '时间', 8, 16, 0.5]
  ];

  /* ── 时间戳来源 ────────────────────────────────────────────────
     DOM 里的 row 不带 at，所以启动时拉一次 api/messages，按
     kind|text 建队列去配。用队列而不是按序号对齐 —— 「看更早的消息」
     会往前插行，序号会错位。配不上的（刚发的）用当下时间。 */
  var stampQ = Object.create(null);
  var stamps = new WeakMap();
  var loaded = false;

  function keyOf(mine, text) {
    return (mine ? 'm|' : 'g|') + (text || '').trim().slice(0, 120);
  }

  function loadStamps() {
    return fetch('api/messages?limit=400', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var msgs = (d && d.msgs) || [];
        for (var i = 0; i < msgs.length; i++) {
          var m = msgs[i];
          var mine = m.kind === 'me' || m.kind === 'her';
          if (!mine && m.kind !== 'gu') continue;
          var k = keyOf(mine, m.text);
          (stampQ[k] || (stampQ[k] = [])).push(m.at);
        }
        loaded = true;
      })
      .catch(function () { loaded = true; });
  }

  function stampFor(row, mine, text) {
    if (stamps.has(row)) return stamps.get(row);
    var q = stampQ[keyOf(mine, text)];
    var at = (q && q.length) ? q.shift() : Math.floor(Date.now() / 1000);
    stamps.set(row, at);
    return at;
  }

  function fmt(at) {
    var d = new Date(at * 1000);
    var hm = String(d.getHours()).padStart(2, '0') + ':' +
             String(d.getMinutes()).padStart(2, '0');
    var now = new Date();
    if (d.toDateString() === now.toDateString()) return hm;
    return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
  }

  /* ── 应用设置 ───────────────────────────────────────────────── */
  function apply() {
    var s = document.documentElement.style;
    s.setProperty('--apv-av', cfg.avatarSize + 'px');
    s.setProperty('--apv-font', cfg.fontSize + 'px');
    s.setProperty('--apv-radius', cfg.bubbleRadius + 'px');
    s.setProperty('--apv-gap', cfg.rowGap + 'px');
    s.setProperty('--apv-time', cfg.timeSize + 'px');
    var b = document.body;
    if (b) {
      b.classList.toggle('apv-hide-av', !cfg.showAvatar);
      b.classList.toggle('apv-hide-time', !cfg.showTime);
    }
    var tag = document.getElementById('apv-user');
    if (tag) tag.textContent = cfg.css || '';
  }

  var avSrc = { me: '', gu: '' };
  function avatarUrl(who) {
    return 'api/appearance/avatar/' + who + '?v=' + (avSrc[who] || '0');
  }
  function paintAvatars() {
    var els = document.querySelectorAll('.apv-av[data-who]');
    for (var i = 0; i < els.length; i++) {
      var who = els[i].getAttribute('data-who');
      if (avSrc[who]) {
        els[i].style.backgroundImage = 'url("' + avatarUrl(who) + '")';
        els[i].textContent = '';
      } else {
        els[i].style.backgroundImage = '';
        els[i].textContent = who === 'me' ? '妍' : '沐';
      }
    }
  }

  /* ── 往每一行插头像和时间戳 ─────────────────────────────────── */
  function decorate() {
    var log = document.getElementById('log');
    if (!log) return;
    var rows = log.querySelectorAll('.row:not([data-apv])');
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      // 只认直接挂着气泡的行。思考行和工具行没有 .bubble/.gu，跳过
      var bub = null, mine = false;
      for (var j = 0; j < row.children.length; j++) {
        var c = row.children[j];
        if (c.classList.contains('bubble')) { bub = c; mine = true; break; }
        if (c.classList.contains('gu')) { bub = c; break; }
      }
      if (!bub) continue;
      // 他那条是流式写出来的，空的时候先别配时间 —— 配了就配到空串上
      var text = (bub._raw != null ? bub._raw : bub.textContent) || '';
      if (!mine && !text.trim() && !loaded) continue;

      row.setAttribute('data-apv', '1');
      row.classList.add('apv');

      var side = document.createElement('div');
      side.className = 'apv-side';
      var av = document.createElement('div');
      av.className = 'apv-av';
      av.setAttribute('data-who', mine ? 'me' : 'gu');
      av.setAttribute('aria-hidden', 'true');
      var tm = document.createElement('div');
      tm.className = 'apv-time';
      tm.textContent = fmt(stampFor(row, mine, text));
      side.appendChild(av);
      side.appendChild(tm);
      row.insertBefore(side, row.firstChild);
    }
    paintAvatars();
  }

  /* ── 面板 ───────────────────────────────────────────────────── */
  var sheet = null, saveTimer = null;

  function save() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      fetch('api/appearance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg)
      }).catch(function () {});
    }, 420);
  }

  function buildSheet() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.className = 'sheetWrap';
    sheet.id = 'apvSheet';

    var rows = '';
    for (var i = 0; i < META.length; i++) {
      var m = META[i];
      rows += '<div class="apv-row"><label for="apv-' + m[0] + '">' + m[1] + '</label>' +
        '<input type="range" id="apv-' + m[0] + '" data-k="' + m[0] + '" min="' + m[2] +
        '" max="' + m[3] + '" step="' + m[4] + '">' +
        '<span class="apv-val" data-v="' + m[0] + '"></span></div>';
    }

    sheet.innerHTML =
      '<div class="shade" data-apv-close="1"></div>' +
      '<div class="sheet" role="dialog" aria-label="外观">' +
        '<div class="grabber"></div>' +
        '<div class="apv-h">外观</div>' +
        '<div class="apv-avs">' +
          '<label class="apv-pick"><i data-pick="me"></i><span class="t">我的头像</span>' +
            '<input type="file" accept="image/*" data-up="me"></label>' +
          '<label class="apv-pick"><i data-pick="gu"></i><span class="t">他的头像</span>' +
            '<input type="file" accept="image/*" data-up="gu"></label>' +
        '</div>' +
        '<div class="apv-sep"></div>' +
        rows +
        '<div class="apv-row"><label>显示头像</label>' +
          '<button type="button" class="apv-sw" data-sw="showAvatar" ' +
          'role="switch" aria-label="显示头像"></button></div>' +
        '<div class="apv-row"><label>显示时间</label>' +
          '<button type="button" class="apv-sw" data-sw="showTime" ' +
          'role="switch" aria-label="显示时间"></button></div>' +
        '<div class="apv-sep"></div>' +
        '<textarea class="apv-css" spellcheck="false" ' +
          'placeholder="自己写点 CSS，比如&#10;#log .bubble { background: #eae6dc; }"></textarea>' +
        '<p class="apv-note">这一栏原样注进页面。写坏了页面会难看，清空就好。' +
        '气泡是 <code>#log .bubble</code>（你的）和 <code>#log .gu</code>（他的）。</p>' +
        '<div class="apv-btns">' +
          '<button type="button" data-apv-reset="1">还原默认</button>' +
          '<button type="button" class="go" data-apv-close="1">好了</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (e) {
      var t = e.target;
      if (t.closest && t.closest('[data-apv-close]')) { closeSheet(); return; }
      if (t.closest && t.closest('[data-apv-reset]')) {
        cfg = JSON.parse(JSON.stringify(DEF));
        apply(); fillSheet(); save();
      }
    });

    sheet.addEventListener('input', function (e) {
      var el = e.target;
      var k = el.getAttribute && el.getAttribute('data-k');
      if (k) {
        cfg[k] = parseFloat(el.value);
        var v = sheet.querySelector('[data-v="' + k + '"]');
        if (v) v.textContent = cfg[k];
        apply(); save();
        return;
      }
      if (el.classList && el.classList.contains('apv-css')) {
        cfg.css = el.value;
        apply(); save();
      }
    });

    sheet.addEventListener('change', function (e) {
      var el = e.target;
      var who = el.getAttribute && el.getAttribute('data-up');
      if (!who || !el.files || !el.files[0]) return;
      var fd = new FormData();
      fd.append('file', el.files[0]);
      fetch('api/appearance/avatar/' + who, { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) throw new Error('bad');
          avSrc[who] = d.v;
          paintAvatars(); fillSheet();
        })
        .catch(function () {
          try { note('（头像没传上去）', 'err'); } catch (e2) {}
        });
      el.value = '';
    });

    sheet.querySelectorAll('[data-sw]').forEach(function (b) {
      b.onclick = function () {
        var k = b.getAttribute('data-sw');
        cfg[k] = !cfg[k];
        apply(); fillSheet(); save();
      };
    });
  }

  function fillSheet() {
    if (!sheet) return;
    for (var i = 0; i < META.length; i++) {
      var k = META[i][0];
      var el = sheet.querySelector('[data-k="' + k + '"]');
      if (el) el.value = cfg[k];
      var v = sheet.querySelector('[data-v="' + k + '"]');
      if (v) v.textContent = cfg[k];
    }
    sheet.querySelectorAll('[data-sw]').forEach(function (b) {
      var on = !!cfg[b.getAttribute('data-sw')];
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    var ta = sheet.querySelector('.apv-css');
    if (ta && ta.value !== cfg.css) ta.value = cfg.css || '';
    sheet.querySelectorAll('[data-pick]').forEach(function (el) {
      var who = el.getAttribute('data-pick');
      if (avSrc[who]) {
        el.style.backgroundImage = 'url("' + avatarUrl(who) + '")';
        el.textContent = '';
      } else {
        el.style.backgroundImage = '';
        el.textContent = who === 'me' ? '妍' : '沐';
      }
    });
  }

  function openSheet() {
    buildSheet();
    fillSheet();
    try { if (typeof closeDrawer === 'function') closeDrawer(); } catch (e) {}
    requestAnimationFrame(function () { sheet.classList.add('open'); });
  }
  function closeSheet() {
    if (!sheet) return;
    sheet.classList.remove('open');
    // 跟上游浮层一个行为：关掉回到侧边栏
    try { if (typeof openDrawer === 'function') openDrawer(); } catch (e) {}
  }

  /* 侧边栏加一项。跟着 navWall 长，取不到就先不加，下一轮再试。 */
  function mountNav() {
    if (document.getElementById('navLook')) return;
    var anchor = document.getElementById('navWall');
    if (!anchor || !anchor.parentNode) return;
    var b = document.createElement('button');
    b.className = anchor.className;
    b.id = 'navLook';
    b.type = 'button';
    b.innerHTML = '<span class="ic"><svg width="19" height="19" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="3.2"/>' +
      '<path d="M12 3v2.2M12 18.8V21M4.6 7.8l1.9 1.1M17.5 15.1l1.9 1.1' +
      'M4.6 16.2l1.9-1.1M17.5 8.9l1.9-1.1"/></svg></span>外观';
    b.onclick = openSheet;
    anchor.parentNode.insertBefore(b, anchor.nextSibling);
  }

  /* ── 起 ─────────────────────────────────────────────────────── */
  function boot() {
    cfg = JSON.parse(JSON.stringify(DEF));
    apply();

    fetch('api/appearance', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          cfg = d.cfg;
          avSrc.me = d.avatars.me || '';
          avSrc.gu = d.avatars.gu || '';
        }
        apply(); paintAvatars(); fillSheet();
      })
      .catch(function () {});

    loadStamps().then(decorate);

    mountNav();
    var log = document.getElementById('log');
    if (log && window.MutationObserver) {
      new MutationObserver(function () { decorate(); })
        .observe(log, { childList: true, subtree: true, characterData: true });
    }
    // 侧边栏是上游后来才画的；顺带兜住流式那条空气泡后来才有字的情况
    setInterval(function () { mountNav(); decorate(); }, 1100);
  }

  window.dwellAppearance = {
    open: openSheet,
    close: closeSheet,
    get: function () { return cfg; },
    redraw: decorate
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_appearance_feature(server_module):
    """接外观设置：两个接口 + 再包一层 index。

    要排在 frontend_feature 之后（它包的是那一层 index）。
    """
    app = server_module.app
    get_db = server_module.get_db

    data_dir = Path(server_module.DB_PATH).resolve().parent
    av_dir = data_dir / "avatars"
    av_dir.mkdir(parents=True, exist_ok=True)

    def read_cfg() -> dict:
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)
                ).fetchone()
        except Exception:
            return dict(DEFAULTS)
        if not row:
            return dict(DEFAULTS)
        try:
            return _clean(json.loads(row[0] if not hasattr(row, "keys") else row["value"]))
        except Exception:
            return dict(DEFAULTS)

    def write_cfg(cfg: dict) -> None:
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (SETTINGS_KEY, json.dumps(cfg, ensure_ascii=False)),
            )

    def avatar_path(who: str):
        """找 who 的头像文件。没传过返回 None。"""
        for ext in ALLOWED_EXT:
            p = av_dir / (who + ext)
            if p.exists():
                return p
        return None

    def avatar_stamp(who: str) -> str:
        """给前端当缓存串用：换了头像这个数就变，浏览器才会重新拉。"""
        p = avatar_path(who)
        if p is None:
            return ""
        try:
            return str(int(p.stat().st_mtime))
        except OSError:
            return ""

    def api_get():
        return jsonify({
            "ok": True,
            "cfg": read_cfg(),
            "avatars": {"me": avatar_stamp("me"), "gu": avatar_stamp("gu")},
        })

    def api_post():
        cfg = _clean(request.get_json(force=True, silent=True) or {})
        write_cfg(cfg)
        return jsonify({"ok": True, "cfg": cfg})

    def api_avatar_put(who):
        if who not in ("me", "gu"):
            return jsonify({"ok": False, "error": "who"}), 400
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"ok": False, "error": "no file"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return jsonify({"ok": False, "error": "type"}), 400
        # 换格式时把旧的那几个扩展名清掉，不然 avatar_path 会先撞上旧文件
        for old in ALLOWED_EXT:
            p = av_dir / (who + old)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        f.save(str(av_dir / (who + ext)))
        return jsonify({"ok": True, "v": str(int(time.time()))})

    def api_avatar_get(who):
        if who not in ("me", "gu"):
            return jsonify({"ok": False}), 404
        p = avatar_path(who)
        if p is None:
            return jsonify({"ok": False}), 404
        return send_file(str(p), mimetype=ALLOWED_EXT.get(p.suffix.lower()),
                         max_age=0, conditional=True)

    app.add_url_rule("/api/appearance", endpoint="api_appearance_get",
                     view_func=api_get, methods=["GET"])
    app.add_url_rule("/api/appearance", endpoint="api_appearance_post",
                     view_func=api_post, methods=["POST"])
    app.add_url_rule("/api/appearance/avatar/<who>", endpoint="api_appearance_avatar_put",
                     view_func=api_avatar_put, methods=["POST"])
    app.add_url_rule("/api/appearance/avatar/<who>", endpoint="api_appearance_avatar_get",
                     view_func=api_avatar_get, methods=["GET"])

    server_module.appearance_client_script = CLIENT_SCRIPT

    original = app.view_functions.get("index")
    if original is None:
        return

    def index_with_appearance(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            if "text/html" not in (resp.headers.get("Content-Type") or ""):
                return resp
            html = resp.get_data(as_text=True)
        except Exception:
            return resp
        if "window.dwellAppearance" in html or "</body>" not in html:
            return resp
        resp.set_data(html.replace("</body>", CLIENT_SCRIPT + "</body>", 1))
        return resp

    app.view_functions["index"] = index_with_appearance
