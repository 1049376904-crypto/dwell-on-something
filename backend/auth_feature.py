"""一道口令闸门，外加一个给命令行用的维护令牌。

## 为什么需要

到现在为止，后端所有接口都是裸的：知道 dwell.yanyan081217.buzz 就能读
全部聊天记录、日记、悄悄话，也能调用每一个写接口（加待办、删日记、
发表情、触发心跳烧网关额度）。备份下载那条我单独加过口令，
但那只是把最大的那个洞堵上了，其余几十条路还开着。

## 做法

一个口令 + 一个签名 cookie。验过一次，cookie 存 180 天，
PWA 里也只需要输一次，不影响推送和主屏安装。

口令不存明文，存 PBKDF2-HMAC-SHA256 派生值加随机盐。
比较走 hmac.compare_digest，不让响应时间泄露信息。

cookie 是 "过期时间戳.签名"，签名密钥由一个随机 secret 加上口令哈希
一起派生。这意味着改口令会让所有已登录设备立刻失效——
这个副作用是特意要的：口令泄露了，改一次就能把别人踢出去。

## 维护令牌：为什么不是「本机免鉴权」

闸门装上之后，在 VPS 上 curl 任何接口都会被挡，每次维护（手动出报、
跑备份、测心跳）都得先 POST 一次 /api/auth/login 换 cookie。很烦。

直觉的解法是「remote_addr 是 127.0.0.1 就放行」。**这条不能做。**
nginx 监听 8070、把请求 proxy_pass 到 127.0.0.1:8888，
所以公网进来的每一个请求，在 Flask 眼里 remote_addr 都是 127.0.0.1。
按来源 IP 放行等于把整道闸门废掉——而且表面上看不出来，
状态接口还会一直报「闸门生效」。

退一步想「那就要求没有 X-Forwarded-For 头」也不行：nginx 默认
并不添加那个头，要显式写 proxy_set_header 才有。这台机器的 nginx
到底配没配，我没读过它的 conf，不能拿整站安全去赌一个假设。

所以改成一个随机令牌：启动时生成，存在 data/admin_token（权限 0600）。
请求带 `X-Dwell-Token` 头（或 `?token=`）就放行。
能读到那个文件的人已经登上这台机器了，本来就有更高的权限，
所以这不额外降低安全性；而且它跟来源 IP 无关，从哪儿调都行。

## 三处刻意的取舍

**一、没设口令时不拦。** 直觉上应该默认拦住，但那样万一 cookie 或
反向代理哪里没配对，你连「设置口令」那个页面都进不去，只能 SSH 改库。
所以闸门只在设过口令之后才生效，没设时启动日志里打一行显眼的警告。

**二、Secure 只在 HTTPS 下加。** 妍妍有两个入口：
http://47.99.241.106:8070（日常，快）和 https://dwell.yanyan081217.buzz。
无条件加 Secure，浏览器会拒绝在 HTTP 下保存这个 cookie，
于是 HTTP 入口陷入「登录成功 → 下一个请求又要求登录」的死循环。
判断时要看 X-Forwarded-Proto：nginx 把请求转到 127.0.0.1:8888，
Flask 自己看到的永远是 http，看不出外层是 HTTPS。

**三、几条路径留开。** /sw.js、/manifest.json、/icons/*、
/apple-touch-icon.png 不拦。它们里面没有私密内容，
而 iOS 在「添加到主屏幕」那一刻会去拉 manifest 和图标，
service worker 注册也不带 cookie 语义保证；拦掉会让安装和推送
以很难排查的方式坏掉。这是安全和可用之间的取舍，我选了留开。

## 限速

15 分钟内错 8 次就锁 15 分钟。存在内存里，重启清零——
这不是抗大规模爆破的方案，是防止有人慢慢试出四位数字口令。
令牌不走限速：它是 32 字节随机数，猜不出来。
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Response, jsonify, redirect, request


KEY_HASH = "auth_password_hash"
KEY_SALT = "auth_password_salt"
KEY_SECRET = "auth_cookie_secret"
KEY_ADMIN_TOKEN = "auth_admin_token"

COOKIE_NAME = "dwell_auth"

# 命令行维护用的请求头。也接受 ?token=，但那个会进 werkzeug 的访问日志，
# 优先用头。
TOKEN_HEADER = "X-Dwell-Token"

# cookie 有效期。手机上不该反复输口令。
COOKIE_DAYS = 180

# PBKDF2 迭代次数。手机端只在登录时算一次，这个量级感知不到。
PBKDF2_ROUNDS = 200000

# 限速：窗口内失败超过上限就锁一段时间。
FAIL_WINDOW_SECONDS = 900
FAIL_LIMIT = 8
LOCK_SECONDS = 900

# 口令最短长度。太短的口令加了闸门也等于没加。
MIN_PASSWORD = 6

# 不需要口令就能访问的路径。
#
# manifest 和图标：iOS 在「添加到主屏幕」那一刻去拉，拦掉会让安装坏掉。
# sw.js：service worker 的注册和更新不保证带上 cookie。
# 这几个文件里没有任何私密内容。
OPEN_EXACT = {
    "/auth",
    "/sw.js",
    "/manifest.json",
    "/apple-touch-icon.png",
    "/favicon.ico",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/setup",
}

OPEN_PREFIX = (
    "/icons/",
)


def _derive(password, salt):
    return base64.b16encode(
        hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"),
            base64.b16decode(salt), PBKDF2_ROUNDS,
        )
    ).decode("ascii")


def register_auth_feature(server_module):
    get_db = server_module.get_db
    app = server_module.app
    token_path = Path(server_module.DB_PATH).parent / "admin_token"

    # 失败计数。进程内，重启清零。
    fails = {"count": 0, "first": 0, "locked_until": 0}
    fails_lock = threading.Lock()

    def read(key, default=""):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def write(key, value):
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def cookie_secret():
        """签名用的随机 secret，首次调用时生成并落库。"""
        value = read(KEY_SECRET)
        if not value:
            value = secrets.token_hex(32)
            write(KEY_SECRET, value)
        return value

    # ── 维护令牌

    def ensure_admin_token():
        """维护令牌：库里存一份，磁盘上放一份方便 cat。

        写文件用 0600：这台机器上别的用户读不到。
        文件丢了会从库里重新写出来，所以可以随便删。
        """
        value = read(KEY_ADMIN_TOKEN)
        if not value:
            value = secrets.token_urlsafe(32)
            write(KEY_ADMIN_TOKEN, value)
        try:
            if not token_path.exists() or token_path.read_text().strip() != value:
                token_path.write_text(value + "\n", encoding="ascii")
            os.chmod(token_path, 0o600)
        except OSError as exc:
            print("[dwell] 维护令牌写不进文件（" + str(exc)[:80] + "），可以从库里取")
        return value

    def token_ok():
        """请求带了正确的维护令牌吗。

        刻意不看来源 IP：nginx 转发之后所有公网请求的 remote_addr
        都是 127.0.0.1，按 IP 放行会把整道闸门废掉。
        """
        expected = read(KEY_ADMIN_TOKEN)
        if not expected:
            return False
        given = request.headers.get(TOKEN_HEADER) or request.args.get("token") or ""
        return bool(given) and hmac.compare_digest(str(given), expected)

    def configured():
        return bool(read(KEY_HASH)) and bool(read(KEY_SALT))

    def sign_key():
        """派生签名密钥。

        把口令哈希掺进来：改口令之后旧 cookie 的签名一律验不过，
        等于把所有已登录设备踢下线。
        """
        return (cookie_secret() + "|" + read(KEY_HASH)).encode("utf-8")

    def make_token():
        expires = int(time.time()) + COOKIE_DAYS * 86400
        payload = str(expires)
        sig = hmac.new(sign_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def token_valid(token):
        if not token or "." not in token:
            return False
        payload, _, sig = str(token).rpartition(".")
        expected = hmac.new(sign_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            return int(payload) > int(time.time())
        except ValueError:
            return False

    def is_https():
        """判断外层是不是 HTTPS。

        nginx 转发到 127.0.0.1:8888，Flask 看到的 scheme 永远是 http，
        所以必须看 X-Forwarded-Proto。判断错了会让 HTTP 入口
        存不上 cookie、陷入登录死循环。
        """
        forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if forwarded:
            return forwarded.lower() == "https"
        return request.scheme == "https"

    def set_cookie(response):
        response.set_cookie(
            COOKIE_NAME,
            make_token(),
            max_age=COOKIE_DAYS * 86400,
            httponly=True,
            samesite="Lax",
            secure=is_https(),
            path="/",
        )
        return response

    def locked_for():
        with fails_lock:
            left = fails["locked_until"] - int(time.time())
        return max(0, left)

    def note_failure():
        now = int(time.time())
        with fails_lock:
            if now - fails["first"] > FAIL_WINDOW_SECONDS:
                fails["count"] = 0
                fails["first"] = now
            fails["count"] += 1
            if fails["count"] >= FAIL_LIMIT:
                fails["locked_until"] = now + LOCK_SECONDS
                fails["count"] = 0

    def clear_failures():
        with fails_lock:
            fails["count"] = 0
            fails["first"] = 0
            fails["locked_until"] = 0

    def authed():
        return token_valid(request.cookies.get(COOKIE_NAME))

    def is_open(path):
        if path in OPEN_EXACT:
            return True
        for prefix in OPEN_PREFIX:
            if path.startswith(prefix):
                return True
        return False

    # ── 闸门

    @app.before_request
    def gate():
        # 没设口令就不拦：否则配置出问题时连设置页都进不去。
        if not configured():
            return None
        if request.method == "OPTIONS":
            return None
        if is_open(request.path):
            return None
        if authed():
            return None
        # 命令行维护走令牌，不用换 cookie。
        if token_ok():
            return None

        # 接口给 JSON，页面给跳转。前端 fetch 拿到 HTML 会莫名报错，
        # 401 加一句话更容易看懂。
        if request.path.startswith("/api/"):
            return jsonify({
                "ok": False,
                "error": "未登录",
                "auth": "required",
                "hint": "命令行加 -H '" + TOKEN_HEADER + ": <data/admin_token 里那串>'",
            }), 401
        return redirect("/auth?next=" + request.path)

    # ── 接口

    def api_status():
        return jsonify({
            "ok": True,
            "configured": configured(),
            # 没设口令时闸门是关着的，这一项如实报告。
            "gate_active": configured(),
            "authed": authed() if configured() else True,
            "by_token": token_ok(),
            "https": is_https(),
            "cookie_days": COOKIE_DAYS,
            "locked_seconds": locked_for(),
            # 只报路径，不回显令牌本身。
            "admin_token_file": str(token_path),
            "admin_token_header": TOKEN_HEADER,
            "open_paths": sorted(OPEN_EXACT) + [p + "*" for p in OPEN_PREFIX],
        })

    def api_setup():
        """首次设置口令。设过之后这条路走不通，改口令要走 /api/auth/change。"""
        if configured():
            return jsonify({"ok": False, "error": "已经设过口令了"}), 400

        data = request.get_json(force=True, silent=True) or {}
        password = str(data.get("password") or "")
        if len(password) < MIN_PASSWORD:
            return jsonify({"ok": False, "error": f"口令至少 {MIN_PASSWORD} 位"}), 400

        salt = base64.b16encode(secrets.token_bytes(16)).decode("ascii")
        write(KEY_SALT, salt)
        write(KEY_HASH, _derive(password, salt))
        cookie_secret()
        clear_failures()

        print("[dwell] 已设置访问口令，闸门生效")
        return set_cookie(jsonify({"ok": True, "detail": "口令已设置，这台设备已登录"}))

    def api_login():
        if not configured():
            return jsonify({"ok": False, "error": "还没设口令"}), 400

        left = locked_for()
        if left:
            return jsonify({
                "ok": False,
                "error": f"错太多次了，等 {left // 60 + 1} 分钟再试",
            }), 429

        data = request.get_json(force=True, silent=True) or {}
        password = str(data.get("password") or "")
        if not password:
            return jsonify({"ok": False, "error": "没输口令"}), 400

        expected = read(KEY_HASH)
        actual = _derive(password, read(KEY_SALT))
        if not hmac.compare_digest(actual, expected):
            note_failure()
            return jsonify({"ok": False, "error": "口令不对"}), 403

        clear_failures()
        return set_cookie(jsonify({"ok": True}))

    def api_change():
        """改口令。要给旧的。

        改完之后所有设备（包括这一台）的 cookie 全部失效，
        因为签名密钥掺了口令哈希。这里顺手给当前设备重新下发一个。
        """
        if not configured():
            return jsonify({"ok": False, "error": "还没设口令，用 /api/auth/setup"}), 400

        data = request.get_json(force=True, silent=True) or {}
        old = str(data.get("old") or "")
        new = str(data.get("new") or "")

        if not hmac.compare_digest(_derive(old, read(KEY_SALT)), read(KEY_HASH)):
            note_failure()
            return jsonify({"ok": False, "error": "旧口令不对"}), 403
        if len(new) < MIN_PASSWORD:
            return jsonify({"ok": False, "error": f"新口令至少 {MIN_PASSWORD} 位"}), 400

        salt = base64.b16encode(secrets.token_bytes(16)).decode("ascii")
        write(KEY_SALT, salt)
        write(KEY_HASH, _derive(new, salt))
        clear_failures()

        return set_cookie(jsonify({
            "ok": True,
            "detail": "改好了。其他设备需要重新登录。",
        }))

    def api_token_rotate():
        """换一个维护令牌。旧的立刻失效。

        要么已经登录，要么带着旧令牌，才能调——这一条不在放行名单里，
        所以闸门本身已经挡过一轮了。
        """
        write(KEY_ADMIN_TOKEN, "")
        value = ensure_admin_token()
        return jsonify({
            "ok": True,
            "detail": "换好了，旧令牌作废",
            "file": str(token_path),
            # 这条接口必须过闸门才能调到，回显一次省得再去 cat。
            "token": value,
        })

    def api_logout():
        response = jsonify({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    def page_auth():
        html = LOGIN_HTML.replace("__MIN__", str(MIN_PASSWORD))
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    routes = [
        ("/auth", "auth_page", page_auth, ["GET"]),
        ("/api/auth/status", "api_auth_status", api_status, ["GET"]),
        ("/api/auth/setup", "api_auth_setup", api_setup, ["POST"]),
        ("/api/auth/login", "api_auth_login", api_login, ["POST"]),
        ("/api/auth/change", "api_auth_change", api_change, ["POST"]),
        ("/api/auth/logout", "api_auth_logout", api_logout, ["POST"]),
        ("/api/auth/token", "api_auth_token", api_token_rotate, ["POST"]),
    ]
    for rule, endpoint, view, methods in routes:
        app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    ensure_admin_token()

    if configured():
        print("[dwell] 访问口令: 已设置，闸门生效")
    else:
        print("[dwell] 访问口令: 还没设！整站对公网敞开，请打开 /auth 设一个")
    print("[dwell] 维护令牌: " + str(token_path) + "（curl 加 " + TOKEN_HEADER + " 头）")

    return authed


# 登录页。占位符走 __NAME__，不用 f-string——
# 这里面有 JS 的大括号，f-string 漏一个转义就是 import 期的 SyntaxError。
# 配色照上游 :root 写死一份：这个页面在闸门之前，
# 不该依赖任何别的模块能不能加载。
LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>验证</title>
<style>
  :root {
    --bg: #faf9f5; --card: #ffffff; --panel: #f0eee6;
    --line: #e8e5dc; --text: #2b2a27; --dim: #8a867c; --accent: #c96442;
  }
  * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
    background: var(--bg); color: var(--text);
    font: 15px/1.6 -apple-system, "SF Pro Text", system-ui, sans-serif;
  }
  .box { width: 100%; max-width: 320px; }
  h1 {
    font-size: 28px; font-weight: 600; letter-spacing: -0.01em;
    margin: 0 0 6px;
  }
  p.sub { color: var(--dim); font-size: 13.5px; margin: 0 0 22px; }
  input {
    width: 100%; background: var(--panel);
    border: 1px solid transparent; border-radius: 14px;
    color: var(--text); padding: 13px 15px; font-size: 16px;
    font-family: inherit;
  }
  input::placeholder { color: var(--dim); }
  button {
    width: 100%; margin-top: 12px; min-height: 48px;
    border: 0; border-radius: 999px;
    background: var(--accent); color: #fff;
    font: inherit; font-size: 16px; cursor: pointer;
  }
  button:disabled { opacity: .5; }
  #msg { min-height: 22px; margin: 12px 0 0; font-size: 13px; color: var(--accent); }
  .hint { margin-top: 18px; font-size: 12.5px; color: var(--dim); line-height: 1.7; }
</style>
</head>
<body>
<div class="box">
  <h1 id="title">…</h1>
  <p class="sub" id="sub"></p>
  <input type="password" id="pwd" placeholder="口令" autocomplete="current-password"
         autocapitalize="off" autocorrect="off">
  <button id="go" onclick="submit()">进去</button>
  <p id="msg"></p>
  <p class="hint" id="hint"></p>
</div>

<script>
var setup = false;
var msg = document.getElementById('msg');
var pwd = document.getElementById('pwd');
var go = document.getElementById('go');

function nextUrl() {
  var m = location.search.match(/[?&]next=([^&]*)/);
  var raw = m ? decodeURIComponent(m[1]) : '/';
  // 只接受站内路径，避免被拿去做跳转跳板。
  return raw.charAt(0) === '/' && raw.charAt(1) !== '/' ? raw : '/';
}

function load() {
  fetch('/api/auth/status').then(function (r) { return r.json(); }).then(function (d) {
    setup = !d.configured;
    document.getElementById('title').textContent = setup ? '设个口令' : '验证';
    document.getElementById('sub').textContent = setup
      ? '这个站现在对公网敞开，设一个口令挡住别人。至少 __MIN__ 位。'
      : '输一次就好，这台设备会记住。';
    pwd.setAttribute('autocomplete', setup ? 'new-password' : 'current-password');
    go.textContent = setup ? '设好了' : '进去';
    if (setup) {
      document.getElementById('hint').textContent =
        '口令只存派生值，不存原文。以后改口令会让其他设备都退出登录。';
    } else if (!d.https) {
      document.getElementById('hint').textContent =
        '当前不是 HTTPS。用 https://dwell.yanyan081217.buzz 更稳妥。';
    }
    pwd.focus();
  }).catch(function () {
    msg.textContent = '问不到后端状态';
  });
}

function submit() {
  var value = pwd.value;
  if (!value) { msg.textContent = '还没输'; return; }
  go.disabled = true;
  msg.textContent = '';

  var url = setup ? '/api/auth/setup' : '/api/auth/login';
  var body = setup ? { password: value } : { password: value };

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); }).then(function (d) {
    go.disabled = false;
    if (d && d.ok) { location.href = nextUrl(); return; }
    msg.textContent = (d && d.error) || '不对';
    pwd.value = '';
    pwd.focus();
  }).catch(function () {
    go.disabled = false;
    msg.textContent = '请求失败';
  });
}

pwd.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') submit();
});

load();
</script>
</body>
</html>
"""
