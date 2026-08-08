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

    # 移除仓库自带的演示 fetch 拦截。演示模式存在时，所有 api 请求都会
    # 被假数据截获，真实后端永远收不到消息。
    start = html.find(DEMO_START)
    end = html.find(DEMO_END, start if start >= 0 else 0)
    if start >= 0 and end > start:
        html = html[:start] + html[end:]

    # 部署时的个人化文字。源文件仍保持上游原貌，避免以后同步上游时冲突。
    replacements = {
        r"\u987e\u5c7f": r"\u6c90",          # 顾屿 -> 沐
        r"\u6b23\u6b23": r"\u598d\u598d", # 欣欣 -> 妍妍
        r"\u8001\u5a46\u7684": r"\u598d\u598d\u7684", # 老婆的 -> 妍妍的
        r"\u8001\u516c": r"\u6c90",          # 老公 -> 沐
        "YU \\u00b7 XIN GENERAL STORE": "MU \\u00b7 YAN GENERAL STORE",
    }
    for old, new in replacements.items():
        html = html.replace(old, new)

    # 上游日历页面移除了生理周期卡片变量 p，但还残留 p.appendChild，
    # 会导致整个日历渲染抛错。
    html = html.replace(
        "  p.appendChild(moodRow);\n  box.appendChild(p);",
        "  box.appendChild(moodRow);",
    )

    # 悄悄话原本藏在“日记”主页里，而空日记数据会让入口根本渲染不出来。
    # 这里直接在侧栏加入稳定入口，不依赖日记模块是否已有数据。
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

    # server.py 已注册根路由；替换它，避免重复 URL。
    server_module.app.view_functions["index"] = index_real
    server_module.app.add_url_rule(
        "/api/version",
        endpoint="api_version",
        view_func=version_real,
        methods=["GET"],
    )
