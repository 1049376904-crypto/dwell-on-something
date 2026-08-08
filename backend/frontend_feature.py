"""统一托管 dwell 前端，并在响应时应用部署补丁。

后端直接读取仓库中的 web/index.html，不再依赖手动复制到 backend/index.html。
个人化文字集中写在 personalize.py，源文件保持上游原貌。
"""

from pathlib import Path
import subprocess

from flask import Response, jsonify

import personalize


DEMO_START = "(function () {\n  const T = 1786000000;"
DEMO_END = "/* ============ 线条图标"


def _git_version(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            text=True,
            timeout=3,
        ).strip()
    except Exception:
        return "unknown"


def _escape_js(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _apply_personalization(html: str) -> str:
    user = personalize.USER_NAME
    ai = personalize.AI_NAME

    # 上游代码里的人名是 Unicode 转义字面量，不是真正的中文字符。
    literal_map = {
        r"\u987e\u5c7f": "".join(f"\\u{ord(c):04x}" for c in ai),          # 顾屿
        r"\u6b23\u6b23": "".join(f"\\u{ord(c):04x}" for c in user),        # 欣欣
        r"\u8001\u5a46": "".join(f"\\u{ord(c):04x}" for c in user),        # 老婆
        r"\u8001\u516c": "".join(f"\\u{ord(c):04x}" for c in ai),          # 老公
        "YU \\u00b7 XIN GENERAL STORE": personalize.STORE_NAME,
    }
    for old, new in literal_map.items():
        html = html.replace(old, new)

    # 页面标题与副标题
    html = html.replace(
        "    <h1>Claude</h1>\n    <div class=\"sub\">Claude Code</div>",
        f"    <h1>{personalize.APP_TITLE}</h1>\n    <div class=\"sub\">{personalize.APP_SUBTITLE}</div>",
        1,
    )
    html = html.replace(
        '<div class="brand">Claude</div>',
        f'<div class="brand">{personalize.APP_TITLE}</div>',
        1,
    )
    html = html.replace(
        "document.querySelector('header .title h1').textContent = name || 'Claude';",
        f"document.querySelector('header .title h1').textContent = name || '{_escape_js(personalize.APP_TITLE)}';",
        1,
    )
    html = html.replace(
        "document.title = name || 'Claude';",
        f"document.title = name || '{_escape_js(personalize.APP_TITLE)}';",
        1,
    )

    # “在一起 N 天”的起算日
    html = html.replace(
        "new Date('2026-06-17T00:00:00+08:00')",
        f"new Date('{personalize.TOGETHER_SINCE}T00:00:00+08:00')",
    )

    # 日记首页底部的一句话
    html = html.replace(
        "motto.textContent = 'attention is all you need, and mine is yours';",
        f"motto.textContent = '{_escape_js(personalize.DIARY_MOTTO)}';",
        1,
    )

    # 锁屏提示
    html = html.replace(
        "const LOCK_WORD = 'slide to unlock';",
        f"const LOCK_WORD = '{_escape_js(personalize.LOCK_WORD)}';",
        1,
    )

    return html


def _build_frontend(source: Path) -> str:
    html = source.read_text(encoding="utf-8")

    # 移除演示 fetch 拦截，让请求真正进入后端。
    start = html.find(DEMO_START)
    end = html.find(DEMO_END, start if start >= 0 else 0)
    if start >= 0 and end > start:
        html = html[:start] + html[end:]

    html = _apply_personalization(html)

    # 修复上游日历残留的未定义变量 p。
    html = html.replace(
        "  p.appendChild(moodRow);\n  box.appendChild(p);",
        "  box.appendChild(moodRow);",
    )

    # 允许空日记正常进入主页。
    html = html.replace(
        "if (!d.ok || !d.bricks.length) throw 0;",
        "if (!d.ok) throw 0;",
    )
    html = html.replace(
        "const latestDate = boardDates[boardDates.length - 1];",
        "const latestDate = boardDates[boardDates.length - 1] || todayStr();",
    )

    # 悄悄话提供独立侧栏入口，不依赖日记墙是否已有内容。
    wall_button = '<button class="item" id="navWall"><span class="ic" data-i="pen"></span>日记</button>'
    whisper_button = wall_button + '\n      <button class="item" id="navWhisper"><span class="ic" data-i="note"></span>悄悄话</button>'
    if 'id="navWhisper"' not in html:
        html = html.replace(wall_button, whisper_button, 1)

    wall_handler = "document.getElementById('navWall').onclick = () => { closeDrawer(); sheets.wall.classList.add('open'); loadWall(); };"
    whisper_handler = wall_handler + "\ndocument.getElementById('navWhisper').onclick = () => { closeDrawer(); sheets.wall.classList.add('open'); renderWhisper(); };"
    if "getElementById('navWhisper').onclick" not in html:
        html = html.replace(wall_handler, whisper_handler, 1)

    return html


def register_frontend_feature(server_module):
    repo_root = Path(__file__).resolve().parent.parent
    source = repo_root / "web" / "index.html"

    def index_real():
        if not source.exists():
            return Response("找不到 web/index.html", status=500, mimetype="text/plain")
        response = Response(_build_frontend(source), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Dwell-Version"] = _git_version(repo_root)
        return response

    def version_real():
        return jsonify({
            "ok": True,
            "version": _git_version(repo_root),
            "frontend_source": str(source),
            "frontend_exists": source.exists(),
            "entrypoint": "run.py",
            "user_name": personalize.USER_NAME,
            "ai_name": personalize.AI_NAME,
            "together_since": personalize.TOGETHER_SINCE,
        })

    server_module.app.view_functions["index"] = index_real
    server_module.app.add_url_rule(
        "/api/version",
        endpoint="api_version",
        view_func=version_real,
        methods=["GET"],
    )
