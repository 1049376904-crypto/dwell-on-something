"""网关接入配置：地址、令牌、模型名。

以前这三项只能改环境变量或手动改 SQLite，现在统一存在 settings 表，
并以它为运行时真相；环境变量只作为首次初始值。
"""

import requests


KEY_BASE = "gateway_base"
KEY_TOKEN = "gateway_token"
KEY_MODEL = "model"


def normalize_base(raw: str) -> str:
    """把用户可能输入的各种形式归一为不带 /v1 的根地址。

    允许输入：
        http://ip:18003
        http://ip:18003/
        http://ip:18003/v1
        http://ip:18003/v1/chat/completions
    """
    base = (raw or "").strip()
    if not base:
        return ""
    base = base.rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            base = base.rstrip("/")
    return base


def register_gateway_config(server_module):
    get_db = server_module.get_db

    def read(key, default=""):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def write(key, value):
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    # 首次运行：把当前环境变量里的值写进数据库作为初始值。
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            (KEY_BASE, normalize_base(server_module.GATEWAY_URL)),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            (KEY_TOKEN, server_module.GATEWAY_TOKEN),
        )

    def apply_runtime():
        """把数据库里的配置同步到运行中的模块属性。"""
        base = normalize_base(read(KEY_BASE, server_module.GATEWAY_URL))
        token = read(KEY_TOKEN, server_module.GATEWAY_TOKEN)
        if base:
            server_module.GATEWAY_URL = base
        if token:
            server_module.GATEWAY_TOKEN = token
        return base, token

    apply_runtime()

    def probe(base, token, model):
        """向网关发一次最小非流式请求，真实验证是否可用。"""
        url = f"{base}/v1/chat/completions"
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            return {"ok": False, "url": url, "detail": str(exc)}

        if resp.status_code != 200:
            return {
                "ok": False,
                "url": url,
                "code": resp.status_code,
                "detail": resp.text[:400],
            }

        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "url": url, "detail": "返回不是 JSON"}

        return {"ok": True, "url": url, "model": data.get("model") or model}

    # ── 接口

    def api_authmode():
        base = normalize_base(read(KEY_BASE, server_module.GATEWAY_URL))
        token = read(KEY_TOKEN, "")
        model = server_module.current_model()
        return server_module.jsonify_compat({
            "mode": "api" if base else "subscription",
            "base": base or "未配置",
            "has_token": bool(token),
            "models": {"model_opus": model},
        })

    def api_apitest():
        data = server_module.request_json()
        base = normalize_base(data.get("base")) or normalize_base(read(KEY_BASE, server_module.GATEWAY_URL))
        token = (data.get("token") or "").strip() or read(KEY_TOKEN, server_module.GATEWAY_TOKEN)
        model = (data.get("model_opus") or "").strip() or server_module.current_model()

        if not base:
            return server_module.jsonify_compat({"ok": False, "detail": "接口地址不能为空"})
        if not token:
            return server_module.jsonify_compat({"ok": False, "detail": "令牌不能为空"})

        return server_module.jsonify_compat(probe(base, token, model))

    def api_apiconf():
        data = server_module.request_json()

        if data.get("clear"):
            write(KEY_BASE, "")
            apply_runtime()
            return server_module.jsonify_compat({"ok": True, "mode": "subscription", "base": ""})

        base = normalize_base(data.get("base"))
        token = (data.get("token") or "").strip()
        model = (data.get("model_opus") or "").strip()

        if base:
            write(KEY_BASE, base)
        if token:
            write(KEY_TOKEN, token)
        if model:
            write(KEY_MODEL, model)

        new_base, _ = apply_runtime()
        return server_module.jsonify_compat({
            "ok": True,
            "mode": "api" if new_base else "subscription",
            "base": new_base,
            "model": server_module.current_model(),
        })

    server_module.app.view_functions["api_authmode"] = api_authmode
    server_module.app.add_url_rule(
        "/api/apitest", endpoint="api_apitest", view_func=api_apitest, methods=["POST"]
    )
    server_module.app.add_url_rule(
        "/api/apiconf", endpoint="api_apiconf", view_func=api_apiconf, methods=["POST"]
    )

    return apply_runtime
