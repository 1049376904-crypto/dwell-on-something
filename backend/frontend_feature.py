"""统一托管 dwell 前端，并在响应时应用部署补丁。

后端直接读取仓库中的 web/index.html，不再依赖手动复制到 backend/index.html。
这样 git pull 后只需重启 PM2，新前端就会立即生效。
"""

from pathlib import Path
import subprocess

from flask import Response, jsonify


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


def _build_frontend(source: Path) -> str:
    html = source.read_text(encoding="utf-8")

    # 移除演示 fetch 拦截，让请求真正进入后端。
    start = html.find(DEMO_START)
    end = html.find(DEMO_END, start if start >= 0 else 0)
    if start >= 0 and end > start:
        html = html[:start] + html[end:]

    # 部署时个人化文字，源文件保持上游原貌。
    replacements = {
        r"\u987e\u5c7f": r"\u6c90",
        r"\u6b23\u6b23": r"\u598d\u598d",
        r"\u8001\u5a46\u7684": r"\u598d\u598d\u7684",
        r"\u8001\u516c": r"\u6c90",
        "YU \\u00b7 XIN GENERAL STORE": "MU \\u00b7 YAN GENERAL STORE",
    }
    for old, new in replacements.items():
        html = html.replace(old, new)

    # 修复上游日历残留的未定义变量 p。
    html = html.replace(
        "  p.appendChild(moodRow);\n  box.appendChild(p);",
        "  box.appendChild(moodRow);",
    )

    # 日记为空时，上游原代码会把空数组当成错误，并在 renderBento 中对
    # undefined 日期调用 slice。允许空日记正常进入主页。
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
        })

    server_module.app.view_functions["index"] = index_real
    server_module.app.add_url_rule(
        "/api/version",
        endpoint="api_version",
        view_func=version_real,
        methods=["GET"],
    )
