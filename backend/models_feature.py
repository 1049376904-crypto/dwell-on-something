"""模型清单：多存几个模型名，随时切换。

以前 settings 里只有一个 model 键，换模型要手敲 SQL。这里在 settings
里额外存一份 JSON 清单（model_catalog），配一个面板 /models 用来点选。

顺带修一处兼容问题：上游前端读 /api/model 的 d.name，
而 server.py 返回的键叫 model，于是那个界面上模型名一直是空的。
这里两个键都给上。

清单只存名字，不存网关地址和令牌——那些在「接入 API」里配一次就够，
换模型不该动网关配置。
"""

import json

from flask import Response, jsonify, request


KEY_CATALOG = "model_catalog"
KEY_MODEL = "model"

# 首次运行时的默认清单。妍妍的网关用中文方括号做别名映射。
DEFAULT_CATALOG = [
    {"name": "【机械信使】claude-sonnet-4-6", "label": "Sonnet 4.6"},
]

MAX_MODELS = 40


PANEL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__HOME_NAME__">
<title>模型</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" sizes="180x180" href="/icons/icon-180.png">
<style>
  :root { --bg:#faf9f5; --fg:#262624; --dim:#8a8780; --line:#e6e3dc; --accent:#3d6b4f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#262624; --fg:#f0eee8; --dim:#9a968e; --line:#3a3a37; --accent:#7fae90; }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0 auto; padding: 28px 20px calc(28px + env(safe-area-inset-bottom));
    background: var(--bg); color: var(--fg);
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    max-width: 560px;
  }
  h1 { font-size: 21px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--dim); font-size: 14px; margin-bottom: 24px; }
  .list { border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  .item {
    display: flex; align-items: center; gap: 12px;
    padding: 14px 16px; cursor: pointer;
  }
  .item + .item { border-top: 1px solid var(--line); }
  .item.on { background: rgba(61,107,79,.08); }
  .item .txt { flex: 1; min-width: 0; }
  .item .label { font-weight: 600; }
  .item .name {
    font-size: 13px; color: var(--dim);
    word-break: break-all; line-height: 1.4;
  }
  .item .tick { color: var(--accent); font-weight: 600; }
  .item .del {
    border: 0; background: transparent; color: var(--dim);
    font-size: 20px; padding: 4px 6px; width: auto; margin: 0; line-height: 1;
  }
  form { margin-top: 20px; }
  label { display: block; font-size: 13.5px; color: var(--dim); margin: 12px 0 5px; }
  input {
    width: 100%; padding: 12px 14px; font-size: 15px;
    border: 1px solid var(--line); border-radius: 11px;
    background: transparent; color: var(--fg);
  }
  button {
    width: 100%; padding: 14px; margin-top: 14px; font-size: 16px;
    border: 1px solid var(--line); border-radius: 11px;
    background: transparent; color: var(--fg); cursor: pointer;
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  #log {
    white-space: pre-wrap; word-break: break-word; font-size: 14px;
    color: var(--dim); margin-top: 16px; min-height: 2.5em;
  }
  a { color: var(--accent); }
</style>
</head>
<body>
<h1>模型</h1>
<div class="sub">点一行就切换。名字要跟网关认的写法完全一致。</div>

<div class="list" id="list">读取中…</div>

<form id="add" onsubmit="return false">
  <label>模型名（网关认的那个，例如 【机械信使】claude-sonnet-4-6）</label>
  <input id="f-name" placeholder="必填" autocapitalize="off" autocorrect="off" spellcheck="false">
  <label>显示名（可留空，只影响这个列表怎么看）</label>
  <input id="f-label" placeholder="可选">
  <button id="btn-add" class="primary">加进清单</button>
  <button id="btn-probe">测一下当前模型通不通</button>
</form>

<div id="log"></div>
<div class="sub" style="margin-top:20px"><a href="/">回到应用</a></div>

<script>
const $ = (id) => document.getElementById(id);
const log = (t) => { $('log').textContent = t; };
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) }
    : { cache: 'no-store' };
  return (await fetch(path, opt)).json();
}

function render(d) {
  if (!d.models || !d.models.length) {
    $('list').innerHTML = '<div class="item"><div class="txt">'
      + '<div class="label">清单是空的</div>'
      + '<div class="name">在下面加一个模型名</div></div></div>';
    return;
  }
  $('list').innerHTML = d.models.map((m) => {
    const on = m.name === d.current;
    return '<div class="item' + (on ? ' on' : '') + '" data-name="' + esc(m.name) + '">'
      + '<div class="txt">'
      + '<div class="label">' + esc(m.label || m.name) + '</div>'
      + (m.label ? '<div class="name">' + esc(m.name) + '</div>' : '')
      + '</div>'
      + (on ? '<span class="tick">在用</span>'
            : '<button class="del" data-del="' + esc(m.name) + '">&times;</button>')
      + '</div>';
  }).join('');

  // 点整行切换；点叉号删除。删除按钮要先拦下来，免得顺带切过去。
  $('list').querySelectorAll('.item').forEach((el) => {
    el.onclick = async (ev) => {
      const del = ev.target.closest('[data-del]');
      if (del) {
        ev.stopPropagation();
        log('正在删除…');
        render(await api('/api/models', { action: 'remove', name: del.dataset.del }));
        log('删掉了。');
        return;
      }
      log('正在切换…');
      const r = await api('/api/models', { action: 'use', name: el.dataset.name });
      render(r);
      log(r.ok ? '已换成 ' + (r.current || '') + '，下一句话开始生效。'
               : ('切换失败：' + (r.error || '')));
    };
  });
}

$('btn-add').onclick = async () => {
  const name = $('f-name').value.trim();
  if (!name) { log('模型名不能为空。'); return; }
  log('正在保存…');
  const r = await api('/api/models', {
    action: 'add', name, label: $('f-label').value.trim(),
  });
  if (r.ok) {
    $('f-name').value = ''; $('f-label').value = '';
    log('加好了。点那一行就能换过去。');
  } else {
    log('失败：' + (r.error || ''));
  }
  render(r);
};

$('btn-probe').onclick = async () => {
  log('正在向网关发一次最小请求…');
  try {
    const r = await api('/api/apitest', {});
    log(r.ok ? '通了，网关回的模型是 ' + (r.model || '未知')
             : ('不通：' + (r.detail || JSON.stringify(r))));
  } catch (e) {
    log('失败：' + e.message);
  }
};

api('/api/models').then(render).catch(() => log('读取清单失败'));
</script>
</body>
</html>
"""


def register_models_feature(server_module):
    get_db = server_module.get_db

    def read(key, default=""):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def write(key, value):
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def load_catalog():
        raw = read(KEY_CATALOG)
        try:
            items = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            items = []

        cleaned = []
        seen = set()
        for item in items:
            if isinstance(item, str):
                item = {"name": item, "label": ""}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append({"name": name, "label": str(item.get("label", "")).strip()})

        # 当前在用的模型一定要在清单里，否则界面上看不到它。
        current = server_module.current_model()
        if current and current not in seen:
            cleaned.insert(0, {"name": current, "label": ""})

        if not cleaned:
            cleaned = [dict(entry) for entry in DEFAULT_CATALOG]
        return cleaned[:MAX_MODELS]

    def save_catalog(items):
        write(KEY_CATALOG, json.dumps(items, ensure_ascii=False))

    # 首次运行把默认清单落库。
    if not read(KEY_CATALOG):
        save_catalog(load_catalog())

    def payload(ok=True, error=""):
        data = {
            "ok": ok,
            "models": load_catalog(),
            "current": server_module.current_model(),
        }
        if error:
            data["error"] = error
        return data

    # ── 接口

    def api_models_get():
        return jsonify(payload())

    def api_models_post():
        data = request.get_json(force=True, silent=True) or {}
        action = str(data.get("action", "")).strip()
        name = str(data.get("name", "")).strip()

        if action == "add":
            if not name:
                return jsonify(payload(False, "模型名不能为空")), 400
            items = load_catalog()
            if any(m["name"] == name for m in items):
                return jsonify(payload(False, "这个模型已经在清单里了")), 400
            if len(items) >= MAX_MODELS:
                return jsonify(payload(False, f"清单最多 {MAX_MODELS} 条")), 400
            items.append({"name": name, "label": str(data.get("label", "")).strip()})
            save_catalog(items)
            return jsonify(payload())

        if action == "remove":
            items = load_catalog()
            if len(items) <= 1:
                return jsonify(payload(False, "至少要留一个模型")), 400
            if name == server_module.current_model():
                return jsonify(payload(False, "不能删掉正在用的模型")), 400
            save_catalog([m for m in items if m["name"] != name])
            return jsonify(payload())

        if action == "use":
            if not any(m["name"] == name for m in load_catalog()):
                return jsonify(payload(False, "清单里没有这个模型")), 400
            write(KEY_MODEL, name)
            server_module.broadcast({"type": "system", "subtype": "model", "model": name})
            return jsonify(payload())

        return jsonify(payload(False, f"未知操作: {action}")), 400

    def api_model_compat():
        """替换 server.py 的 /api/model GET。

        上游前端读 d.name，server.py 返回的是 model；两个键都给上，
        免得界面上模型名空着。
        """
        current = server_module.current_model()
        label = ""
        for item in load_catalog():
            if item["name"] == current:
                label = item["label"]
                break
        return jsonify({
            "ok": True,
            "name": current,
            "model": current,
            "label": label,
            "effort": "high",
            "models": load_catalog(),
        })

    def api_models_panel():
        import personalize

        html = PANEL_TEMPLATE.replace("__HOME_NAME__", personalize.HOME_SCREEN_NAME)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    server_module.app.view_functions["api_get_model"] = api_model_compat

    routes = [
        ("/models", "api_models_panel", api_models_panel, ["GET"]),
        ("/api/models", "api_models_get", api_models_get, ["GET"]),
        ("/api/models", "api_models_post", api_models_post, ["POST"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    return load_catalog
