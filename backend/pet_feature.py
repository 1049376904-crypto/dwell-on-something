"""dwell 小家伙：照源码逻辑，五个状态，不自创。

源码只有五个状态：走动(WALK)、静止(IDLE)、爆发(BURST)、随机A(STARTUP_A)、
随机B(STARTUP_B)。这一版就五个，不多不少。

不碰上游 #pet。独立建一只，自己走、自己换图。
锚点用 .composer（聊天页才有，别的页面没有）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from flask import jsonify, request, send_file

SETTINGS_KEY = "pet"

# 源码只有五个状态
STATES = ("walk", "idle", "burst", "startupA", "startupB")

DEFAULTS = {
    "on": True,
    "walk": True,
    "speed": 15,
    "size": 96,
    "lift": 0,
    "idleChance": 0.35,
    "bounce": True,
}

NUM_RANGE = {
    "speed": (2, 120),
    "size": (32, 260),
    "lift": (-60, 160),
    "idleChance": (0, 1),
}

ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}

PIC_MAX = 4 * 1024 * 1024


def _clean(raw: dict) -> dict:
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for key, (low, high) in NUM_RANGE.items():
        if key in raw:
            try:
                out[key] = max(low, min(high, float(raw[key])))
            except (TypeError, ValueError):
                pass
    for key in ("on", "walk", "bounce"):
        if key in raw:
            out[key] = bool(raw[key])
    return out


CLIENT_SCRIPT = r"""
<style id="pet-base">
#petBottom{position:fixed;z-index:2147482000;pointer-events:none;
  display:none;will-change:left,top}
#petBottom img{display:block;pointer-events:auto;cursor:pointer;
  user-select:none;-webkit-user-drag:none;
  transition:transform .18s ease-out}
#petBottom.pet-flip img{transform:scaleX(-1)}
#petBottom.pet-away{display:none !important}

#petSheet .sheet{box-sizing:border-box;width:100%;max-width:100vw;
  overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding:0 20px calc(env(safe-area-inset-bottom,0px) + 18px)}
#petSheet .sheet::after{display:none}
#petSheet .sheet *{box-sizing:border-box}
#petSheet .grabber{width:38px;margin-left:auto;margin-right:auto}
#petSheet .pt-h{font-size:19px;font-weight:600;margin:2px 0 6px;
  color:var(--text,#2b2a27)}
#petSheet .pt-sub{font-size:12px;color:var(--dim,#8a867c);opacity:.85;
  line-height:1.65;margin:0 0 12px}
#petSheet .pt-grp{font-size:11px;letter-spacing:.12em;
  color:var(--dim,#8a867c);opacity:.7;margin:15px 0 2px}
#petSheet .pt-line{display:flex;align-items:center;gap:10px;padding:7px 0;
  min-width:0}
#petSheet .pt-line > label{flex:0 0 72px;min-width:0;font-size:13.5px;
  color:var(--dim,#8a867c);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#petSheet .pt-line input[type=range]{flex:1 1 0;min-width:0;width:auto;
  -webkit-appearance:none;appearance:none;height:26px;background:transparent;
  margin:0}
#petSheet .pt-line input[type=range]::-webkit-slider-runnable-track{height:3px;
  border-radius:2px;background:var(--line,#e8e5dc)}
#petSheet .pt-line input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
  width:20px;height:20px;border-radius:50%;background:#fff;margin-top:-8.5px;
  border:1px solid var(--line,#e8e5dc);box-shadow:0 1px 4px rgba(0,0,0,.14)}
#petSheet .pt-val{flex:0 0 44px;min-width:0;text-align:right;font-size:12.5px;
  color:var(--dim,#8a867c);font-variant-numeric:tabular-nums}
#petSheet .pt-sw{flex:0 0 auto;margin-left:auto;-webkit-appearance:none;
  appearance:none;width:44px;height:26px;border-radius:999px;
  background:var(--line,#e8e5dc);position:relative;transition:background .2s ease;
  border:0;padding:0;cursor:pointer}
#petSheet .pt-sw::after{content:'';position:absolute;top:3px;left:3px;
  width:20px;height:20px;border-radius:50%;background:#fff;
  transition:transform .2s ease;box-shadow:0 1px 3px rgba(0,0,0,.18)}
#petSheet .pt-sw.on{background:var(--accent,#c96442)}
#petSheet .pt-sw.on::after{transform:translateX(18px)}
#petSheet .pt-pics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
  gap:9px;margin-top:4px}
#petSheet .pt-pic{display:flex;align-items:center;gap:9px;padding:9px;
  border:1px dashed var(--line,#e8e5dc);border-radius:14px;cursor:pointer;
  font-size:12.5px;color:var(--dim,#8a867c);min-width:0}
#petSheet .pt-pic i{flex:0 0 auto;width:38px;height:38px;border-radius:10px;
  background:var(--panel,#f0eee6) center/contain no-repeat;font-style:normal}
#petSheet .pt-pic span{min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#petSheet .pt-pic.has{border-style:solid}
#petSheet .pt-pic input{display:none}
#petSheet .pt-pic .pt-x{flex:0 0 auto;width:22px;height:22px;border:0;
  background:transparent;color:var(--dim,#8a867c);cursor:pointer;padding:0;
  display:flex;align-items:center;justify-content:center}
#petSheet .pt-pic .pt-x svg{width:13px;height:13px}
#petSheet .pt-btns{display:flex;gap:9px;margin-top:16px;min-width:0}
#petSheet .pt-btns button{flex:1 1 0;min-width:0;min-height:44px;
  border-radius:999px;border:0;background:var(--panel,#f0eee6);
  color:var(--text,#2b2a27);font-size:14px;font-family:inherit;cursor:pointer;
  padding:0 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#petSheet .pt-btns button.go{background:var(--accent,#c96442);color:#fff}
#petSheet .pt-note{font-size:11.5px;color:var(--dim,#8a867c);opacity:.85;
  margin:12px 0 0;line-height:1.75}
#petSheet .pt-sep{height:1px;background:var(--line,#e8e5dc);margin:14px 0;
  opacity:.7}
</style>
<script>
(function () {
  if (window.dwellPet) return;

  var DEF = {
    on: true, walk: true, speed: 15, size: 96, lift: 0,
    idleChance: 0.35, bounce: true
  };
  var cfg = JSON.parse(JSON.stringify(DEF));
  var pics = {};

  var WALK_SRC = "https://zkaicc.huilan.com/aicc/api/aicc-file/miniofile/preViewPicture/aicc/71G55kRR_1786448391994.png";
  var IDLE_SRC = "https://cac.opple.com/yc-media/getFile?id=e01f949c613e4add8621fdaba4ba3e5f#.png";
  var BURST_SRC = "https://www.lnjubao.cn/minio-jbpt/upload/20260811/edac7c48967dab963cd16afcfe731abb.png";
  var STARTUP_A = "https://cdncs.ykt.cbern.com.cn/v0.1/download?path=/zxx_feedback/qdqqd/1786448054276.png";
  var STARTUP_B = "https://cac.opple.com/yc-media/getFile?id=593531468e8f4650801b54d795a32985#.png";

  var BUILTIN = {
    walk: WALK_SRC,
    idle: IDLE_SRC,
    burst: BURST_SRC,
    startupA: STARTUP_A,
    startupB: STARTUP_B
  };

  var STATES = [
    ['walk', '走动'], ['idle', '静止'], ['burst', '爆发'],
    ['startupA', '随机A'], ['startupB', '随机B']
  ];
  var META = [
    ['speed', '走多快', 2, 120, 1],
    ['size', '多大', 32, 260, 2],
    ['lift', '离底栏', -60, 160, 1],
    ['idleChance', '爱发呆', 0, 1, 0.05]
  ];

  function urlOf(state) {
    return pics[state]
      ? 'api/pet/pic/' + state + '?v=' + pics[state]
      : (BUILTIN[state] || '');
  }

  var wrap = null, img = null;

  function build() {
    if (wrap) return;
    wrap = document.createElement('div');
    wrap.id = 'petBottom';
    img = document.createElement('img');
    img.alt = '小家伙';
    img.src = urlOf('walk');
    wrap.appendChild(img);
    document.body.appendChild(wrap);

    var clickCount = 0, lastClickAt = 0;
    img.addEventListener('click', function () {
      var now = performance.now();
      if (now - lastClickAt > 1500) clickCount = 0;
      lastClickAt = now;
      clickCount++;
      bounce();
      if (clickCount >= 4) {
        clickCount = 0;
        setTemp(urlOf('burst'), 2500);
      }
    });
  }

  var tempSrc = null, tempUntil = 0;
  function setTemp(src, ms) {
    tempSrc = src;
    tempUntil = performance.now() + ms;
  }

  var kbUp = false;
  var vv = window.visualViewport;
  function checkKb() { if (vv) kbUp = vv.height < window.innerHeight * 0.8; }
  if (vv) {
    vv.addEventListener('resize', checkKb);
    vv.addEventListener('scroll', checkKb);
    checkKb();
  }
  function typing() {
    var box = document.getElementById('box');
    var focused = !!(box && document.activeElement === box);
    return vv ? (focused && kbUp) : focused;
  }

  var x = 0, dir = 1, raf = 0;
  var restUntil = 0, nextRest = 0, lastT = 0;

  function bounce() {
    if (!img || img._hop) return;
    img._hop = 1;
    var t0 = performance.now(), dur = 520, peak = -cfg.size * 0.5;
    var step = function (t) {
      var p = Math.min(1, (t - t0) / dur);
      var y = Math.sin(p * Math.PI * 2) * peak * (1 - p);
      var flip = dir === -1 ? ' scaleX(-1)' : '';
      img.style.transform = 'translateY(' + y.toFixed(1) + 'px)' + flip;
      if (p < 1) requestAnimationFrame(step);
      else { img.style.transform = ''; img._hop = 0; }
    };
    requestAnimationFrame(step);
  }

  var nextRandomAt = 0;

  function frame(now) {
    raf = requestAnimationFrame(frame);
    build();
    if (!wrap || !img) return;

    var sheetOpen = !!document.querySelector('.sheetWrap.open');
    var lock = document.getElementById('lockWrap');
    if (sheetOpen || lock || !cfg.on) {
      wrap.classList.add('pet-away');
      return;
    }
    wrap.classList.remove('pet-away');

    var c = document.querySelector('.composer');
    if (!c) { wrap.classList.add('pet-away'); return; }

    wrap.style.display = 'block';
    img.style.height = cfg.size + 'px';

    var cr = c.getBoundingClientRect();
    var minX = cr.left + 6;
    var maxX = cr.right - cfg.size - 6;
    if (maxX < minX) maxX = minX;
    if (!x) x = minX + (maxX - minX) * 0.3;

    wrap.style.top = (cr.top - cfg.size + cfg.lift) + 'px';

    var dt = lastT ? Math.min(0.05, (now - lastT) / 1000) : 0.016;
    lastT = now;

    var typingNow = typing();
    var want;

    if (typingNow) {
      want = urlOf('idle');
      restUntil = 0;
      nextRest = now + 1500;
    } else if (tempSrc && now < tempUntil) {
      want = tempSrc;
    } else {
      if (now >= nextRandomAt) {
        if (Math.random() < 0.5) {
          setTemp(urlOf(Math.random() < 0.5 ? 'startupA' : 'startupB'), 3500);
        }
        nextRandomAt = now + 6000 + Math.random() * 4000;
      }
      if (tempSrc && now < tempUntil) {
        want = tempSrc;
      } else {
        tempSrc = null;
        want = urlOf('walk');
      }
    }

    if (img.getAttribute('src') !== want) img.src = want;

    if (typingNow || (cfg.bounce && img._hop)) {
      if (x < minX) x = minX;
      if (x > maxX) x = maxX;
    } else if (cfg.walk && !(tempSrc && now < tempUntil)) {
      if (now < restUntil) {
        // 发呆中
      } else {
        if (nextRest && now > nextRest && Math.random() < cfg.idleChance) {
          restUntil = now + 2000 + Math.random() * 3000;
          nextRest = 0;
        } else {
          x += dir * cfg.speed * dt * 3.2;
          if (x <= minX) { x = minX; dir = 1; nextRest = now + 600; }
          if (x >= maxX) { x = maxX; dir = -1; nextRest = now + 600; }
          if (!nextRest) nextRest = now + 2500 + Math.random() * 3000;
        }
      }
    }

    if (x < minX) x = minX;
    if (x > maxX) x = maxX;
    wrap.style.left = x + 'px';
    wrap.classList.toggle('pet-flip', dir === -1);
  }

  var sheet = null, saveTimer = null;

  function save() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      fetch('api/pet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg)
      }).catch(function () {});
    }, 420);
  }

  function row(m) {
    return '<div class="pt-line"><label for="pt-' + m[0] + '">' + m[1] + '</label>' +
      '<input type="range" id="pt-' + m[0] + '" data-k="' + m[0] + '" min="' + m[2] +
      '" max="' + m[3] + '" step="' + m[4] + '">' +
      '<span class="pt-val" data-v="' + m[0] + '"></span></div>';
  }

  function buildSheet() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.className = 'sheetWrap';
    sheet.id = 'petSheet';

    var lines = '';
    for (var i = 0; i < META.length; i++) lines += row(META[i]);

    var grid = '';
    for (var j = 0; j < STATES.length; j++) {
      grid += '<label class="pt-pic" data-pic="' + STATES[j][0] + '">' +
        '<i data-thumb="' + STATES[j][0] + '"></i>' +
        '<span>' + STATES[j][1] + '</span>' +
        '<button type="button" class="pt-x" data-picdel="' + STATES[j][0] + '" ' +
          'title="清掉，用回外链图" style="display:none">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-width="2" stroke-linecap="round">' +
          '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>' +
          '</svg></button>' +
        '<input type="file" accept="image/*" data-picup="' + STATES[j][0] + '"></label>';
    }

    sheet.innerHTML =
      '<div class="shade" data-pt-close="1"></div>' +
      '<div class="sheet" role="dialog" aria-label="小家伙">' +
        '<div class="grabber"></div>' +
        '<div class="pt-h">小家伙</div>' +
        '<p class="pt-sub">聊天输入栏上方一只左右走的小宠物，撞到边翻身，' +
          '你打字他停下，戳一下跳两下，连续点会爆发。</p>' +

        '<div class="pt-line"><label>让他在</label>' +
          '<button type="button" class="pt-sw" data-sw="on" role="switch"></button></div>' +
        '<div class="pt-line"><label>会走路</label>' +
          '<button type="button" class="pt-sw" data-sw="walk" role="switch"></button></div>' +
        '<div class="pt-line"><label>戳了跳</label>' +
          '<button type="button" class="pt-sw" data-sw="bounce" role="switch"></button></div>' +

        '<div class="pt-grp">调一调</div>' + lines +

        '<div class="pt-grp">换图</div>' +
        '<div class="pt-pics">' + grid + '</div>' +
        '<p class="pt-note">默认用五张外链图。传了哪个换哪个，' +
          '点格子右边那个 × 就清掉、用回外链。png / gif / svg 都行，4MB 以内。</p>' +

        '<div class="pt-btns">' +
          '<button type="button" data-pt-reset="1">还原默认</button>' +
          '<button type="button" class="go" data-pt-close="1">好了</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (e) {
      var t = e.target;
      if (t.closest && t.closest('[data-pt-close]')) { closeSheet(); return; }
      if (t.closest && t.closest('[data-pt-reset]')) {
        cfg = JSON.parse(JSON.stringify(DEF));
        fillSheet(); save();
        return;
      }
      var del = t.closest && t.closest('[data-picdel]');
      if (del) {
        e.preventDefault();
        e.stopPropagation();
        var st = del.getAttribute('data-picdel');
        fetch('api/pet/pic/' + st, { method: 'DELETE' })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d || !d.ok) throw new Error('bad');
            delete pics[st];
            fillSheet();
            try { note('（清掉了，用回外链图）'); } catch (e2) {}
          })
          .catch(function () {
            try { note('（没清掉）', 'err'); } catch (e2) {}
          });
        return;
      }
      var sw = t.closest && t.closest('[data-sw]');
      if (sw) {
        var k = sw.getAttribute('data-sw');
        cfg[k] = !cfg[k];
        fillSheet(); save();
      }
    });

    sheet.addEventListener('input', function (e) {
      var k = e.target.getAttribute && e.target.getAttribute('data-k');
      if (!k) return;
      cfg[k] = parseFloat(e.target.value);
      var v = sheet.querySelector('[data-v="' + k + '"]');
      if (v) v.textContent = k === 'idleChance'
        ? Math.round(cfg[k] * 100) + '%' : cfg[k];
      save();
    });

    sheet.addEventListener('change', function (e) {
      var el = e.target;
      var st = el.getAttribute && el.getAttribute('data-picup');
      if (!st || !el.files || !el.files[0]) return;
      var fd = new FormData();
      fd.append('file', el.files[0]);
      fetch('api/pet/pic/' + st, { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) throw new Error((d && d.error) || 'bad');
          pics[st] = d.v;
          fillSheet();
          try { note('（换好了）'); } catch (e2) {}
        })
        .catch(function () {
          try { note('（这张没传上去）', 'err'); } catch (e2) {}
        });
      el.value = '';
    });
  }

  function fillSheet() {
    if (!sheet) return;
    for (var i = 0; i < META.length; i++) {
      var k = META[i][0];
      var el = sheet.querySelector('[data-k="' + k + '"]');
      if (el) el.value = cfg[k];
      var v = sheet.querySelector('[data-v="' + k + '"]');
      if (v) v.textContent = k === 'idleChance'
        ? Math.round(cfg[k] * 100) + '%' : cfg[k];
    }
    var sws = sheet.querySelectorAll('[data-sw]');
    for (var j = 0; j < sws.length; j++) {
      var on = !!cfg[sws[j].getAttribute('data-sw')];
      sws[j].classList.toggle('on', on);
      sws[j].setAttribute('aria-checked', on ? 'true' : 'false');
    }
    var th = sheet.querySelectorAll('[data-thumb]');
    for (var n = 0; n < th.length; n++) {
      var st = th[n].getAttribute('data-thumb');
      var box = th[n].parentNode;
      th[n].style.backgroundImage = 'url("' + urlOf(st) + '")';
      if (box) box.classList.toggle('has', !!pics[st]);
    }
    var dels = sheet.querySelectorAll('[data-picdel]');
    for (var m = 0; m < dels.length; m++) {
      var st2 = dels[m].getAttribute('data-picdel');
      dels[m].style.display = pics[st2] ? '' : 'none';
    }
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
    try { if (typeof openDrawer === 'function') openDrawer(); } catch (e) {}
  }

  function mountNav() {
    if (document.getElementById('navPet')) return;
    var anchor = document.getElementById('navLook')
              || document.getElementById('navWall');
    if (!anchor || !anchor.parentNode) return;
    var b = document.createElement('button');
    b.className = anchor.className;
    b.id = 'navPet';
    b.type = 'button';
    b.innerHTML = '<span class="ic"><svg width="19" height="19" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M5.5 11.5a2 2 0 1 1 2.6-2.9"/>' +
      '<path d="M18.5 11.5a2 2 0 1 0-2.6-2.9"/>' +
      '<path d="M12 20c-3.6 0-6-1.9-6-4.4C6 12.9 8.7 10 12 10s6 2.9 6 5.6' +
      'c0 2.5-2.4 4.4-6 4.4z"/>' +
      '<path d="M10 15.4h.01M14 15.4h.01"/></svg></span>小家伙';
    b.onclick = openSheet;
    anchor.parentNode.insertBefore(b, anchor.nextSibling);
  }

  function boot() {
    fetch('api/pet', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          cfg = d.cfg;
          pics = d.pics || {};
        }
        fillSheet();
      })
      .catch(function () {});

    mountNav();
    raf = requestAnimationFrame(frame);
    setInterval(function () { mountNav(); }, 1200);
  }

  window.dwellPet = {
    open: openSheet,
    close: closeSheet,
    get: function () { return cfg; },
    pics: function () { return pics; }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_pet_feature(server_module):
    """接小家伙：五个接口 + 再包一层 index。"""
    app = server_module.app
    get_db = server_module.get_db

    data_dir = Path(server_module.DB_PATH).resolve().parent
    pic_dir = data_dir / "pet"
    pic_dir.mkdir(parents=True, exist_ok=True)

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
            raw = row["value"] if hasattr(row, "keys") else row[0]
            return _clean(json.loads(raw))
        except Exception:
            return dict(DEFAULTS)

    def write_cfg(cfg: dict) -> None:
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (SETTINGS_KEY, json.dumps(cfg, ensure_ascii=False)),
            )

    def pic_path(state: str):
        for ext in ALLOWED_EXT:
            p = pic_dir / (state + ext)
            if p.exists():
                return p
        return None

    def pic_stamps() -> dict:
        out = {}
        for st in STATES:
            p = pic_path(st)
            if p is None:
                continue
            try:
                out[st] = str(int(p.stat().st_mtime))
            except OSError:
                out[st] = "1"
        return out

    def api_get():
        return jsonify({"ok": True, "cfg": read_cfg(), "pics": pic_stamps()})

    def api_post():
        cfg = _clean(request.get_json(force=True, silent=True) or {})
        write_cfg(cfg)
        return jsonify({"ok": True, "cfg": cfg})

    def api_pic_put(state):
        if state not in STATES:
            return jsonify({"ok": False, "error": "state"}), 400
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"ok": False, "error": "没选文件"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return jsonify({"ok": False, "error": "只收 png/jpg/webp/gif/svg"}), 400
        blob = f.read()
        if not blob:
            return jsonify({"ok": False, "error": "文件是空的"}), 400
        if len(blob) > PIC_MAX:
            return jsonify({"ok": False, "error": "超过 4MB"}), 413
        for old in ALLOWED_EXT:
            p = pic_dir / (state + old)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        (pic_dir / (state + ext)).write_bytes(blob)
        return jsonify({"ok": True, "v": str(int(time.time()))})

    def api_pic_get(state):
        if state not in STATES:
            return jsonify({"ok": False}), 404
        p = pic_path(state)
        if p is None:
            return jsonify({"ok": False}), 404
        return send_file(str(p), mimetype=ALLOWED_EXT.get(p.suffix.lower()),
                         max_age=0, conditional=True)

    def api_pic_del(state):
        """清掉这个状态传过的图，回到外链。"""
        if state not in STATES:
            return jsonify({"ok": False, "error": "state"}), 400
        removed = False
        for old in ALLOWED_EXT:
            p = pic_dir / (state + old)
            if p.exists():
                try:
                    p.unlink()
                    removed = True
                except OSError:
                    pass
        return jsonify({"ok": True, "removed": removed})

    app.add_url_rule("/api/pet", endpoint="api_pet_get",
                     view_func=api_get, methods=["GET"])
    app.add_url_rule("/api/pet", endpoint="api_pet_post",
                     view_func=api_post, methods=["POST"])
    app.add_url_rule("/api/pet/pic/<state>", endpoint="api_pet_pic_put",
                     view_func=api_pic_put, methods=["POST"])
    app.add_url_rule("/api/pet/pic/<state>", endpoint="api_pet_pic_get",
                     view_func=api_pic_get, methods=["GET"])
    app.add_url_rule("/api/pet/pic/<state>", endpoint="api_pet_pic_del",
                     view_func=api_pic_del, methods=["DELETE"])

    server_module.pet_client_script = CLIENT_SCRIPT

    original = app.view_functions.get("index")
    if original is None:
        return

    def index_with_pet(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            if "text/html" not in (resp.headers.get("Content-Type") or ""):
                return resp
            html = resp.get_data(as_text=True)
        except Exception:
            return resp
        if "window.dwellPet" in html or "</body>" not in html:
            return resp
        resp.set_data(html.replace("</body>", CLIENT_SCRIPT + "</body>", 1))
        return resp

    app.view_functions["index"] = index_with_pet