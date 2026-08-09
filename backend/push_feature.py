"""Web Push：让沐主动说的话在页面关着的时候也能被看到。

心跳不接推送等于对着空气说话，所以这两个模块是配套的。

关键取舍：
* VAPID 密钥首次运行自动生成并存进 settings，不用手工准备。
  换了密钥所有旧订阅立即失效，所以生成后就不再动它。
* pywebpush 是可选依赖。没装时接口如实报告缺失，其余功能照常，
  不让一个推送库拖垮整个后端。
* 推送内容只放摘要，最多 80 字。通知会出现在锁屏上，
  别人瞥一眼就看见全文不合适。
* 订阅端点唯一，重复订阅走 UPSERT，不会堆出一堆重复记录。
* 自带一个独立的开关面板（/push）。妍妍只有手机，
  iOS 上没有控制台，订阅必须能靠点按完成。
  面板把 CLIENT_SCRIPT 内联进自己的 HTML，不依赖 frontend_feature 的注入：
  那个注入只作用于 index.html，面板是另一个页面。

两道前提，顺序不能颠倒：
1. HTTPS。非安全上下文下浏览器压根不暴露 PushManager，
   跟是不是主屏模式无关。用 IP 直连 http:// 一定失败。
2. 添加到主屏幕。iOS（Safari 16.4+）只在 standalone 模式下
   允许申请通知权限。
面板会分别检测这两项并如实说明卡在哪一步。
"""

import json
import time

from flask import Response, jsonify, request


KEY_VAPID_PRIVATE = "push_vapid_private_pem"
KEY_VAPID_PUBLIC = "push_vapid_public_b64"
KEY_CONTACT = "push_contact"

# 通知正文上限。锁屏上会直接显示，不宜过长。
MAX_BODY_CHARS = 80

# 推送失败到这些状态码说明订阅已经作废，直接删掉。
DEAD_STATUS = {404, 410}


def _b64url_nopad(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _generate_vapid_keys():
    """生成 P-256 密钥对，返回 (PEM 私钥, base64url 公钥)。

    公钥是 65 字节未压缩点，浏览器 applicationServerKey 要的就是这个格式。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    public_point = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return pem, _b64url_nopad(public_point)


SERVICE_WORKER = """/* dwell service worker：只做推送展示和点击跳转，不碰缓存。
   刻意不做离线缓存：这个前端由后端动态构建，缓存会让补丁失效。 */

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (e) {
    payload = { body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || '沐';
  const options = {
    body: payload.body || '',
    tag: payload.tag || 'dwell',
    renotify: true,
    data: { url: payload.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
"""

# 客户端脚本。同时注入主应用和面板页面，所以不能依赖任何页面独有的元素。
CLIENT_SCRIPT = """
<script>
(function () {
  const PUSH = {
    // isSecureContext 是硬前提：为 false 时 PushManager 根本不存在。
    secure: window.isSecureContext === true,
    supported: 'serviceWorker' in navigator && 'PushManager' in window,
    standalone: window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true,
    ios: /iPhone|iPad|iPod/.test(navigator.userAgent),
  };
  window.dwellPush = PUSH;

  function urlBase64ToUint8Array(base64) {
    const padding = '='.repeat((4 - (base64.length % 4)) % 4);
    const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(normalized);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  PUSH.blockedBy = function () {
    if (!PUSH.secure) return '当前不是 HTTPS，浏览器不提供推送能力';
    if (!PUSH.supported) return '这个浏览器不支持推送';
    if (PUSH.ios && !PUSH.standalone) return 'iPhone 需要先添加到主屏幕，再从主屏图标打开';
    return null;
  };

  PUSH.register = async function () {
    return navigator.serviceWorker.register('/sw.js', { scope: '/' });
  };

  PUSH.enable = async function () {
    const blocked = PUSH.blockedBy();
    if (blocked) throw new Error(blocked);

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('通知权限被拒绝：' + permission);

    const reg = await PUSH.register();
    await navigator.serviceWorker.ready;

    const keyResp = await (await fetch('/api/push/key', { cache: 'no-store' })).json();
    if (!keyResp.ok) throw new Error(keyResp.error || '拿不到推送公钥');

    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyResp.public_key),
      });
    }

    const saved = await (await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    })).json();
    if (!saved.ok) throw new Error(saved.error || '订阅保存失败');
    return saved;
  };

  PUSH.disable = async function () {
    if (!PUSH.supported) return { ok: true };
    const reg = await navigator.serviceWorker.getRegistration('/');
    if (!reg) return { ok: true };
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return { ok: true };
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
    return { ok: true };
  };

  // 已经授权过的话，静默把 service worker 挂上，
  // 免得换了设备或清过数据后订阅悄悄失效。
  if (!PUSH.blockedBy() && window.Notification && Notification.permission === 'granted') {
    PUSH.enable().catch(() => {});
  }
})();
</script>
"""

# 独立面板。刻意不塞进上游那个设置页：那边结构复杂、
# 字符串补丁很容易打歪，而这个页面只需要能点。
# {client_script} 由 api_push_panel 填入，面板不依赖外部注入。
PANEL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>通知设置</title>
<link rel="manifest" href="/manifest.json">
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
  .card {
    border: 1px solid var(--line); border-radius: 14px;
    padding: 16px 18px; margin-bottom: 14px;
  }
  .row { display: flex; justify-content: space-between; gap: 12px; padding: 5px 0; }
  .row span:first-child { color: var(--dim); }
  .row span:last-child { text-align: right; }
  .ok { color: var(--accent); }
  .bad { color: #c26a4a; }
  button {
    width: 100%; padding: 14px; margin-top: 10px; font-size: 16px;
    border: 1px solid var(--line); border-radius: 11px;
    background: transparent; color: var(--fg); cursor: pointer;
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .45; }
  #log {
    white-space: pre-wrap; word-break: break-word; font-size: 14px;
    color: var(--dim); margin-top: 16px; min-height: 3em;
  }
  .warn {
    border: 1px solid #c98a3c; background: rgba(201,138,60,.09);
    padding: 13px 16px; border-radius: 11px; font-size: 14.5px;
    margin-bottom: 14px;
  }
  .warn code { font-size: 13px; word-break: break-all; }
  a { color: var(--accent); }
</style>
</head>
<body>
<h1>通知</h1>
<div class="sub">开启后，沐主动说的话会推到锁屏。</div>

<div id="guard"></div>

<div class="card">
  <div class="row"><span>安全上下文</span><span id="s-secure">检测中</span></div>
  <div class="row"><span>浏览器支持</span><span id="s-support">检测中</span></div>
  <div class="row"><span>主屏模式</span><span id="s-standalone">检测中</span></div>
  <div class="row"><span>通知权限</span><span id="s-perm">检测中</span></div>
  <div class="row"><span>已订阅设备</span><span id="s-count">…</span></div>
  <div class="row"><span>推送库</span><span id="s-lib">…</span></div>
</div>

<button id="btn-enable" class="primary">开启通知</button>
<button id="btn-test">发一条测试通知</button>
<button id="btn-disable">关闭通知</button>
<button id="btn-beat">让沐现在说句话</button>

<div id="log"></div>
<div class="sub" style="margin-top:20px"><a href="/">回到应用</a></div>

__CLIENT_SCRIPT__

<script>
const $ = (id) => document.getElementById(id);
const log = (text) => { $('log').textContent = text; };

function mark(id, good, text) {
  const el = $(id);
  el.textContent = text;
  el.className = good ? 'ok' : 'bad';
}

function refreshLocal() {
  const P = window.dwellPush || {};

  mark('s-secure', P.secure, P.secure ? 'HTTPS' : 'HTTP，不安全');
  mark('s-support', P.supported, P.supported ? '支持' : '不支持');
  mark('s-standalone', P.standalone, P.standalone ? '是' : '否（浏览器标签页）');
  $('s-perm').textContent = window.Notification ? Notification.permission : '不可用';

  // 两道前提分开提示：HTTPS 是硬门槛，主屏幕是 iOS 的额外要求。
  const blocked = P.blockedBy ? P.blockedBy() : '脚本未加载';
  $('btn-enable').disabled = !!blocked;

  if (!P.secure) {
    $('guard').innerHTML = '<div class="warn">现在是 <b>HTTP</b> 访问，'
      + '浏览器在非加密页面上不提供推送能力，这跟主屏幕无关。'
      + '请改用 HTTPS 域名打开这个页面：<br><code>'
      + 'https://dwell.yanyan081217.buzz/push</code></div>';
  } else if (P.ios && !P.standalone) {
    $('guard').innerHTML = '<div class="warn">HTTPS 已就绪。接下来在 Safari 里'
      + '点分享按钮，选<b>添加到主屏幕</b>，然后从主屏图标打开这个页面，'
      + '系统才允许开启通知。</div>';
  } else if (blocked) {
    $('guard').innerHTML = '<div class="warn">' + blocked + '</div>';
  } else {
    $('guard').innerHTML = '';
  }
}

async function refreshServer() {
  try {
    const d = await (await fetch('/api/push/status', { cache: 'no-store' })).json();
    $('s-count').textContent = d.count;
    $('s-lib').textContent = d.library_installed ? '已安装' : '未安装';
  } catch (e) {
    $('s-count').textContent = '读取失败';
  }
}

$('btn-enable').onclick = async () => {
  log('正在申请权限并订阅…');
  try {
    const r = await window.dwellPush.enable();
    log('订阅成功，当前 ' + r.count + ' 台设备。可以点下面的测试通知了。');
  } catch (e) {
    log('失败：' + e.message);
  }
  refreshLocal(); refreshServer();
};

$('btn-test').onclick = async () => {
  log('正在发送…');
  try {
    const d = await (await fetch('/api/push/test', { cache: 'no-store' })).json();
    log(d.ok
      ? '已发往 ' + d.sent + ' 台设备。锁屏或下拉通知看看。'
      : '没发出去：' + (d.error || JSON.stringify(d)));
  } catch (e) {
    log('失败：' + e.message);
  }
  refreshServer();
};

$('btn-disable').onclick = async () => {
  log('正在取消…');
  try {
    await window.dwellPush.disable();
    log('已取消订阅。系统通知权限需要在 iOS 设置里单独关。');
  } catch (e) {
    log('失败：' + e.message);
  }
  refreshLocal(); refreshServer();
};

$('btn-beat').onclick = async () => {
  log('已叫醒它，十几秒后应该会说话…');
  try {
    await fetch('/api/heartbeat/test', { cache: 'no-store' });
    setTimeout(async () => {
      const d = await (await fetch('/api/heartbeat', { cache: 'no-store' })).json();
      log('结果：' + (d.last_result || '未知')
        + (d.last_text ? '\\n它说：' + d.last_text : '')
        + (d.last_push ? '\\n推送：' + d.last_push : ''));
    }, 15000);
  } catch (e) {
    log('失败：' + e.message);
  }
};

refreshLocal();
refreshServer();
</script>
</body>
</html>
"""


def register_push_feature(server_module):
    get_db = server_module.get_db

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT    NOT NULL UNIQUE,
                p256dh   TEXT    NOT NULL,
                auth     TEXT    NOT NULL,
                agent    TEXT    NOT NULL DEFAULT '',
                created  INTEGER NOT NULL,
                last_ok  INTEGER NOT NULL DEFAULT 0,
                fails    INTEGER NOT NULL DEFAULT 0
            );
        """)

    def read(key, default=""):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def write(key, value):
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def ensure_keys():
        """首次运行生成 VAPID 密钥；之后原样返回。

        密钥一换所有旧订阅立即失效，所以只在缺失时生成。
        """
        private_pem = read(KEY_VAPID_PRIVATE)
        public_b64 = read(KEY_VAPID_PUBLIC)
        if private_pem and public_b64:
            return private_pem, public_b64

        private_pem, public_b64 = _generate_vapid_keys()
        write(KEY_VAPID_PRIVATE, private_pem)
        write(KEY_VAPID_PUBLIC, public_b64)
        print("[dwell] 已生成 VAPID 密钥对")
        return private_pem, public_b64

    try:
        ensure_keys()
    except Exception as exc:
        # cryptography 缺失等极端情况：不要让整个后端起不来。
        print(f"[dwell] VAPID 密钥生成失败，推送不可用: {exc}")

    def subscriptions():
        with get_db() as db:
            return db.execute(
                "SELECT id,endpoint,p256dh,auth FROM push_subscriptions ORDER BY id"
            ).fetchall()

    def drop_subscription(endpoint):
        with get_db() as db:
            db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))

    def send_push(title, body, url="/", tag="dwell"):
        """向所有订阅推送一条通知，返回统计结果。

        任何一个订阅失败都不影响其他订阅；已作废的端点顺手清掉。
        """
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            return {"ok": False, "error": "未安装 pywebpush，运行 pip3 install pywebpush"}

        private_pem = read(KEY_VAPID_PRIVATE)
        if not private_pem:
            return {"ok": False, "error": "VAPID 密钥缺失"}

        targets = subscriptions()
        if not targets:
            return {"ok": False, "error": "还没有任何设备订阅推送"}

        payload = json.dumps({
            "title": title,
            "body": (body or "")[:MAX_BODY_CHARS],
            "url": url,
            "tag": tag,
        }, ensure_ascii=False)

        claims = {"sub": read(KEY_CONTACT, "mailto:dwell@localhost")}
        sent, removed, errors = 0, 0, []

        for row in targets:
            info = {
                "endpoint": row["endpoint"],
                "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            }
            try:
                webpush(
                    subscription_info=info,
                    data=payload,
                    vapid_private_key=private_pem,
                    vapid_claims=dict(claims),
                    timeout=15,
                )
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in DEAD_STATUS:
                    drop_subscription(row["endpoint"])
                    removed += 1
                else:
                    errors.append(f"{status or '?'}: {str(exc)[:120]}")
                    with get_db() as db:
                        db.execute(
                            "UPDATE push_subscriptions SET fails=fails+1 WHERE id=?",
                            (row["id"],),
                        )
                continue
            except Exception as exc:
                errors.append(str(exc)[:120])
                continue

            sent += 1
            with get_db() as db:
                db.execute(
                    "UPDATE push_subscriptions SET last_ok=?, fails=0 WHERE id=?",
                    (int(time.time()), row["id"]),
                )

        return {
            "ok": sent > 0,
            "sent": sent,
            "removed": removed,
            "errors": errors,
            "total": len(targets),
        }

    # ── 接口

    def api_push_panel():
        # 面板自带客户端脚本：frontend_feature 的注入只作用于 index.html，
        # 面板是独立页面，之前因此拿不到 window.dwellPush，永远显示「不支持」。
        html = PANEL_TEMPLATE.replace("__CLIENT_SCRIPT__", CLIENT_SCRIPT)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def api_push_key():
        public_b64 = read(KEY_VAPID_PUBLIC)
        if not public_b64:
            return jsonify({"ok": False, "error": "VAPID 公钥缺失"}), 500
        return jsonify({"ok": True, "public_key": public_b64})

    def api_push_subscribe():
        data = request.get_json(force=True, silent=True) or {}
        endpoint = str(data.get("endpoint", "")).strip()
        keys = data.get("keys") or {}
        p256dh = str(keys.get("p256dh", "")).strip()
        auth = str(keys.get("auth", "")).strip()

        if not endpoint or not p256dh or not auth:
            return jsonify({"ok": False, "error": "订阅信息不完整"}), 400

        with get_db() as db:
            db.execute(
                """
                INSERT INTO push_subscriptions (endpoint,p256dh,auth,agent,created)
                VALUES (?,?,?,?,?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh=excluded.p256dh,
                    auth=excluded.auth,
                    agent=excluded.agent,
                    fails=0
                """,
                (
                    endpoint, p256dh, auth,
                    str(request.headers.get("User-Agent", ""))[:200],
                    int(time.time()),
                ),
            )

        return jsonify({"ok": True, "count": len(subscriptions())})

    def api_push_unsubscribe():
        data = request.get_json(force=True, silent=True) or {}
        endpoint = str(data.get("endpoint", "")).strip()
        if endpoint:
            drop_subscription(endpoint)
        return jsonify({"ok": True, "count": len(subscriptions())})

    def api_push_status():
        try:
            import pywebpush  # noqa: F401
            library = True
        except ImportError:
            library = False

        with get_db() as db:
            rows = db.execute(
                "SELECT endpoint,agent,created,last_ok,fails "
                "FROM push_subscriptions ORDER BY id"
            ).fetchall()

        return jsonify({
            "ok": True,
            "library_installed": library,
            "has_keys": bool(read(KEY_VAPID_PUBLIC)),
            "count": len(rows),
            "devices": [
                {
                    # 端点是凭据的一部分，只回显尾段用于辨认设备。
                    "endpoint_tail": row["endpoint"][-24:],
                    "agent": row["agent"][:60],
                    "created": row["created"],
                    "last_ok": row["last_ok"],
                    "fails": row["fails"],
                }
                for row in rows
            ],
        })

    def api_push_test():
        result = send_push("沐", "这是一条测试通知，说明推送已经通了。")
        return jsonify(result)

    def api_service_worker():
        response = Response(SERVICE_WORKER, mimetype="application/javascript")
        # service worker 不能被缓存，否则改了推不下去。
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    def api_manifest():
        """PWA 清单。iOS 要「添加到主屏幕」后才允许推送，这个文件是前提。"""
        manifest = {
            "name": "沐",
            "short_name": "沐",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#faf9f5",
            "theme_color": "#faf9f5",
            # 图标资源随 fork 丢了，这里不写 icons：
            # iOS 会退回用页面截图，比引用 404 路径干净。
            "icons": [],
        }
        response = jsonify(manifest)
        response.headers["Cache-Control"] = "no-cache"
        return response

    routes = [
        ("/push", "api_push_panel", api_push_panel, ["GET"]),
        ("/sw.js", "api_service_worker", api_service_worker, ["GET"]),
        ("/manifest.json", "api_manifest", api_manifest, ["GET"]),
        ("/api/push/key", "api_push_key", api_push_key, ["GET"]),
        ("/api/push/status", "api_push_status", api_push_status, ["GET"]),
        ("/api/push/subscribe", "api_push_subscribe", api_push_subscribe, ["POST"]),
        ("/api/push/unsubscribe", "api_push_unsubscribe", api_push_unsubscribe, ["POST"]),
        # GET 也接受：手机浏览器直接打开就能测。
        ("/api/push/test", "api_push_test", api_push_test, ["GET", "POST"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    server_module.send_push = send_push
    server_module.push_client_script = CLIENT_SCRIPT
    return send_push
