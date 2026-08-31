"""dwell 小家伙：让上游那只 #pet 沿输入栏走起来，顺便能换图。

上游 web/index.html 里已经有一只 `#pet`：能拖、能戳、会跟着聊天状态换图
（idle / thinking / typing / happy / doze / sleeping / react）。它缺的只有
一件事 —— 走动。这一层补上走动，再把「换图」做成能自己上传。

不改 web/index.html。在 index 视图外面再包一层注脚本。

⚠️ 打字判定用 `visualViewport` 的高度，不是"输入框有没有焦点"。移动端
软键盘会挤压可视视口高度；只看焦点的话，收起键盘但光标还在时他会一直
站着不动。桌面端没有这个收缩，退回看焦点。

⚠️ 只在聊天页出现。任何浮层（`.sheetWrap.open`）打开就藏起来 —— 他的
z-index 比浮层高，不藏会浮在日记、设置那些页面上面。

⚠️ 拖过之后就不走了。上游把拖动位置记在 `localStorage.petPos` 里，既然
你亲手摆过，那个位置就是你要的。面板上有「让他重新走起来」清掉它。

⚠️ 换图不能只改 `src`：上游 `petSet()` 每次状态变化都会重写 `petImg.src`。
所以这里包住 `petSet` —— 在它设完之后再覆盖一次。取不到那个函数就退回
用 MutationObserver 盯着 src 改回来。

删掉 run.py 里那一行就完全没有这个功能，上游那只螃蟹照旧能拖能戳。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from flask import jsonify, request, send_file

SETTINGS_KEY = "pet"

# 上游 pet/ 目录里那七张图对应的状态名
STATES = ("idle", "thinking", "typing", "happy", "doze", "sleeping", "react")

DEFAULTS = {
    "on": True,             # 总开关
    "walk": True,           # 走不走
    "speed": 15,            # 走动速度（px/秒 的基准，越小越慢）
    "size": 128,            # 图片高度 px（上游原本是 128）
    "lift": 4,              # 离输入卡顶边多高 px
    "idleChance": 0.35,     # 每走一段路停下发呆的概率
    "bounce": True,         # 戳一下跳两下
}

NUM_RANGE = {
    "speed": (2, 120),
    "size": (32, 260),
    "lift": (-40, 120),
    "idleChance": (0, 1),
}

ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}

PIC_MAX = 4 * 1024 * 1024


def _clean(raw: dict) -> dict:
    """钳到合法范围。坏值一律回落默认，不报错。"""
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
/* 走动时贴在输入卡正上方。上游给 #pet 的是 position:fixed + right/bottom，
   这里改成挂在 footer 里、用 left 定位 —— 键盘弹起时 footer 会被顶上去，
   他就跟着一起上去，不用自己算键盘高度。 */
#pet.petwalk{position:absolute;right:auto;bottom:auto;
  transition:none;will-change:transform,left}
#pet.petwalk img{transition:transform .18s ease-out}
/* 翻身：朝左走的时候整个镜像 */
#pet.petwalk.pet-flip img{transform:scaleX(-1)}
#pet.pet-away{display:none !important}

/* 面板：借上游 .sheetWrap/.sheet 的壳。
   ⚠️ .sheet 自己没有左右内边距（内容本该进 .sheet .body），得自己补；
   ⚠️ .sheet::after 是贴在底下的 80px 挡板，这里 .sheet 变成滚动容器了，
      那个绝对定位的伪元素会跟着内容滚到中间去，拿背景色盖掉一条。 */
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
/* 七个状态的换图格子 */
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
    on: true, walk: true, speed: 15, size: 128, lift: 4,
    idleChance: 0.35, bounce: true
  };
  var cfg = JSON.parse(JSON.stringify(DEF));
  var pics = {};          // state -> 版本号（有就是传过图）

  var STATES = [
    ['idle', '闲着'], ['thinking', '在想'], ['typing', '在写'],
    ['happy', '高兴'], ['doze', '打盹'], ['sleeping', '睡着'],
    ['react', '被戳']
  ];
  var META = [
    ['speed', '走多快', 2, 120, 1],
    ['size', '多大', 32, 260, 2],
    ['lift', '离底栏', -40, 120, 1],
    ['idleChance', '爱发呆', 0, 1, 0.05]
  ];

  function petEl() { return document.getElementById('pet'); }
  function petImg() { return document.getElementById('petImg'); }
  function composer() { return document.querySelector('.composer'); }

  /* ── 换图 ──────────────────────────────────────────────────────
     ⚠️ 不能只改 src：上游 petSet() 每次状态变化都会重写它。
     所以包住 petSet，在它设完之后再覆盖一次。 */
  function urlOf(state) {
    return pics[state] ? ('api/pet/pic/' + state + '?v=' + pics[state]) : '';
  }

  function overrideNow() {
    var im = petImg();
    if (!im) return;
    // 上游把文件名写进 src，从里头认出当前是哪个状态
    var src = im.getAttribute('src') || '';
    var hit = '';
    if (src.indexOf('idle-follow') >= 0) hit = 'idle';
    else if (src.indexOf('thinking') >= 0) hit = 'thinking';
    else if (src.indexOf('typing') >= 0) hit = 'typing';
    else if (src.indexOf('happy') >= 0) hit = 'happy';
    else if (src.indexOf('idle-doze') >= 0) hit = 'doze';
    else if (src.indexOf('sleeping') >= 0) hit = 'sleeping';
    else if (src.indexOf('double-jump') >= 0) hit = 'react';
    if (!hit) return;
    var want = urlOf(hit);
    if (want && src.indexOf('api/pet/pic/' + hit) < 0) im.src = want;
  }

  var wrapped = false;
  function wrapPetSet() {
    if (wrapped) return;
    if (typeof window.petSet !== 'function') return;
    var orig = window.petSet;
    window.petSet = function () {
      orig.apply(this, arguments);
      overrideNow();
    };
    wrapped = true;
  }

  /* petSet 是上游脚本里的局部函数，window 上可能取不到。
     那就退回盯着 src：它一被改写就立刻覆盖回来。 */
  function watchImg() {
    var im = petImg();
    if (!im || im._petWatched || !window.MutationObserver) return;
    im._petWatched = 1;
    new MutationObserver(function () { overrideNow(); })
      .observe(im, { attributes: true, attributeFilter: ['src'] });
  }

  /* ── 键盘起没起 ────────────────────────────────────────────────
     ⚠️ 用可视视口高度判断，不是"输入框有没有焦点"。移动端软键盘会
     挤压 visualViewport.height；只看焦点的话，收起键盘但光标还在时
     他会一直站着不走。桌面端没这个收缩，退回看焦点。 */
  var kbUp = false;
  var vv = window.visualViewport;
  function checkKb() {
    if (vv) kbUp = vv.height < window.innerHeight * 0.8;
  }
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

  /* 拖过就不走了 —— 那个位置是你亲手摆的 */
  function wasDragged() {
    try { return !!localStorage.getItem('petPos'); } catch (e) { return false; }
  }

  /* ── 走 ──────────────────────────────────────────────────────── */
  var x = 20, dir = 1, raf = 0;
  var restUntil = 0, nextRest = 0, lastT = 0;
  var mounted = false;

  /* 挂进 footer：键盘弹起时 footer 被顶上去，他跟着一起上去，
     不用自己算键盘高度。 */
  function mount() {
    var p = petEl(), f = document.querySelector('footer');
    if (!p || !f) return false;
    if (p.parentNode !== f) {
      f.insertBefore(p, f.firstChild);
      p.style.right = 'auto';
      p.style.bottom = 'auto';
      p.style.top = 'auto';
    }
    p.classList.add('petwalk');
    mounted = true;
    return true;
  }

  function unmount() {
    var p = petEl();
    if (!p) return;
    p.classList.remove('petwalk', 'pet-flip');
    if (p.parentNode !== document.body) document.body.appendChild(p);
    p.style.left = '';
    p.style.top = '';
    p.style.right = '';
    p.style.bottom = '';
    mounted = false;
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    var p = petEl();
    if (!p) return;

    wrapPetSet();
    watchImg();

    // ⚠️ 浮层开着就藏起来：他 z-index 比浮层高，不藏会浮在日记、设置上面
    var sheetOpen = !!document.querySelector('.sheetWrap.open');
    var lock = document.getElementById('lockWrap');
    if (sheetOpen || lock) {
      p.classList.add('pet-away');
      return;
    }
    p.classList.remove('pet-away');

    // 关了、或者你亲手拖过：回到上游那套（fixed 定位，不走）
    if (!cfg.on || !cfg.walk || wasDragged()) {
      if (mounted) unmount();
      if (!cfg.on) p.classList.add('pet-away');
      return;
    }
    if (!mounted && !mount()) return;

    var im = petImg();
    if (im) im.style.height = cfg.size + 'px';
    p.style.width = cfg.size + 'px';
    p.style.height = cfg.size + 'px';

    var c = composer();
    if (!c) return;
    var f = document.querySelector('footer');
    var cr = c.getBoundingClientRect();
    var fr = f.getBoundingClientRect();
    // 贴着输入卡顶边站
    p.style.top = (cr.top - fr.top - cfg.size + cfg.lift) + 'px';

    var minX = cr.left - fr.left + 4;
    var maxX = cr.right - fr.left - cfg.size - 4;
    if (maxX < minX) maxX = minX;

    var dt = lastT ? Math.min(0.05, (now - lastT) / 1000) : 0.016;
    lastT = now;

    if (typing()) {
      // 你在打字，他站着看
      restUntil = 0;
      nextRest = now + 1500;
    } else if (now < restUntil) {
      // 发呆中
    } else {
      if (nextRest && now > nextRest && Math.random() < cfg.idleChance) {
        restUntil = now + 2000 + Math.random() * 3000;   // 停 2~5 秒
        nextRest = 0;
      } else {
        x += dir * cfg.speed * dt * 3.2;
        if (x <= minX) { x = minX; dir = 1; nextRest = now + 600; }
        if (x >= maxX) { x = maxX; dir = -1; nextRest = now + 600; }
        if (!nextRest) nextRest = now + 2500 + Math.random() * 3000;
      }
    }

    if (x < minX) x = minX;
    if (x > maxX) x = maxX;
    p.style.left = x + 'px';
    p.classList.toggle('pet-flip', dir === -1);
  }

  /* 戳一下跳两下。上游只换图，没有跳。 */
  function bindBounce() {
    var p = petEl();
    if (!p || p._petBounce) return;
    p._petBounce = 1;
    p.addEventListener('click', function () {
      if (!cfg.bounce || !mounted) return;
      var im = petImg();
      if (!im || im._hop) return;
      im._hop = 1;
      var t0 = performance.now(), dur = 560, peak = -cfg.size * 0.22;
      var flip = dir === -1 ? ' scaleX(-1)' : '';
      var step = function (t) {
        var k = Math.min(1, (t - t0) / dur);
        var y = Math.sin(k * Math.PI * 2) * peak * (1 - k);
        im.style.transform = 'translateY(' + y.toFixed(1) + 'px)' + flip;
        if (k < 1) requestAnimationFrame(step);
        else { im.style.transform = ''; im._hop = 0; }
      };
      requestAnimationFrame(step);
    });
  }

  /* ── 面板 ───────────────────────────────────────────────────── */
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
        '<input type="file" accept="image/*" data-picup="' + STATES[j][0] + '"></label>';
    }

    sheet.innerHTML =
      '<div class="shade" data-pt-close="1"></div>' +
      '<div class="sheet" role="dialog" aria-label="小家伙">' +
        '<div class="grabber"></div>' +
        '<div class="pt-h">小家伙</div>' +
        '<p class="pt-sub">他会沿着输入栏来回走，撞到边就翻个身；' +
          '你打字的时候他站着等。戳一下会跳。</p>' +

        '<div class="pt-line"><label>让他在</label>' +
          '<button type="button" class="pt-sw" data-sw="on" role="switch"></button></div>' +
        '<div class="pt-line"><label>会走路</label>' +
          '<button type="button" class="pt-sw" data-sw="walk" role="switch"></button></div>' +
        '<div class="pt-line"><label>戳了跳</label>' +
          '<button type="button" class="pt-sw" data-sw="bounce" role="switch"></button></div>' +

        '<div class="pt-grp">调一调</div>' + lines +

        '<div class="pt-grp">换他的样子</div>' +
        '<div class="pt-pics">' + grid + '</div>' +
        '<p class="pt-note">不传就用原来那七张。传了哪个换哪个，' +
          '点同一格再选一次就是替换。png / gif / svg 都行，4MB 以内。</p>' +

        '<div class="pt-sep"></div>' +
        '<div class="pt-btns">' +
          '<button type="button" data-pt-free="1">让他重新走起来</button>' +
        '</div>' +
        '<p class="pt-note">把他拖到别处之后就不走了 —— 那个位置是你摆的。' +
          '按上面这颗把他放回输入栏上。</p>' +

        '<div class="pt-btns">' +
          '<button type="button" data-pt-reset="1">还原默认</button>' +
          '<button type="button" class="go" data-pt-close="1">好了</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (e) {
      var t = e.target;
      if (t.closest && t.closest('[data-pt-close]')) { closeSheet(); return; }
      if (t.closest && t.closest('[data-pt-free]')) {
        try { localStorage.removeItem('petPos'); } catch (e2) {}
        var p = petEl();
        if (p) { p.style.left = ''; p.style.top = ''; }
        try { note('（他回输入栏上去了）'); } catch (e3) {}
        return;
      }
      if (t.closest && t.closest('[data-pt-reset]')) {
        cfg = JSON.parse(JSON.stringify(DEF));
        fillSheet(); save();
        return;
      }
      var sw = t.closest && t.closest('[data-sw]');
      if (sw) {
        var k = sw.getAttribute('data-sw');
        cfg[k] = !cfg[k];
        fillSheet(); save();
        if (k === 'walk' && !cfg[k]) unmount();
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
          overrideNow();
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
      if (pics[st]) {
        th[n].style.backgroundImage = 'url("' + urlOf(st) + '")';
        if (box) box.classList.add('has');
      } else {
        th[n].style.backgroundImage = '';
        if (box) box.classList.remove('has');
      }
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

  /* 侧边栏加一项，跟在「外观」后面（没有外观就跟着日记） */
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

  /* ── 起 ─────────────────────────────────────────────────────── */
  function boot() {
    fetch('api/pet', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          cfg = d.cfg;
          pics = d.pics || {};
        }
        fillSheet();
        overrideNow();
      })
      .catch(function () {});

    mountNav();
    bindBounce();
    raf = requestAnimationFrame(frame);
    // 侧边栏和输入卡都是上游后来才画的
    setInterval(function () { mountNav(); bindBounce(); wrapPetSet(); watchImg(); }, 1200);
  }

  window.dwellPet = {
    open: openSheet,
    close: closeSheet,
    get: function () { return cfg; },
    pics: function () { return pics; },
    free: function () {
      try { localStorage.removeItem('petPos'); } catch (e) {}
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_pet_feature(server_module):
    """接小家伙：三个接口 + 再包一层 index。

    要排在 frontend_feature 之后；排在 appearance 之后能让侧边栏那项
    跟在「外观」后面（取不到也会退回跟着「日记」）。
    """
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
        """哪些状态传过图，各自的版本号。没传的不出现在这张表里。"""
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
        # 换格式时把旧的清掉，不然 pic_path 会先撞上旧文件
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

    app.add_url_rule("/api/pet", endpoint="api_pet_get",
                     view_func=api_get, methods=["GET"])
    app.add_url_rule("/api/pet", endpoint="api_pet_post",
                     view_func=api_post, methods=["POST"])
    app.add_url_rule("/api/pet/pic/<state>", endpoint="api_pet_pic_put",
                     view_func=api_pic_put, methods=["POST"])
    app.add_url_rule("/api/pet/pic/<state>", endpoint="api_pet_pic_get",
                     view_func=api_pic_get, methods=["GET"])

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
