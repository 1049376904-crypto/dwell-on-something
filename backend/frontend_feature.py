"""统一托管 dwell 前端，并在响应时应用部署补丁。

后端直接读取仓库中的 web/index.html，不再依赖手动复制到 backend/index.html。
个人化文字集中写在 personalize.py，源文件保持上游原貌。

所有补丁都必须可重复应用：后端每次响应都重新构建一遍 HTML，
补丁叠加会让页面逐渐损坏。
"""

from pathlib import Path
import subprocess

from flask import Response, jsonify

import personalize


DEMO_START = "(function () {\n  const T = 1786000000;"
DEMO_END = "/* ============ 线条图标"

# verbOf 里的锚点：上游第一条 case，我们把自建工具的词条插在它前面。
VERBOF_ANCHOR = (
    "    case 'Bash':   return ['terminal', 'Ran', "
    "input.description || (input.command || '').slice(0, 60)];"
)

# 自建工具的词条。沿用上游风格：图标名 + 英文动词 + 对象。
# 动词过去式、对象取具体内容，扫一眼就知道沐刚做了什么。
OUR_VERBS = """    case 'write_diary':           return ['pen', 'Wrote', brief(input.title || input.text)];
    case 'list_diary_entries':    return ['bookOpen', 'Read', 'the diary'];
    case 'delete_diary_entry':    return ['fileText', 'Removed', 'a diary entry'];
    case 'read_my_diary':         return ['bookOpen', 'Read', 'her diary'];
    case 'add_favorite_line':     return ['pen', 'Saved', brief(input.text)];
    case 'read_favorite_lines':   return ['bookOpen', 'Read', 'saved lines'];
    case 'add_todo':              return ['note', 'Noted', brief(input.text)];
    case 'list_todos':            return ['note', 'Checked', 'the list'];
    case 'set_todo_done':         return ['note', 'Ticked', 'an item'];
    case 'delete_todo':           return ['note', 'Removed', 'an item'];
    case 'add_calendar_event':    return ['note', 'Scheduled', brief(input.text)];
    case 'list_calendar_events':  return ['note', 'Checked', 'the calendar'];
    case 'delete_calendar_event': return ['note', 'Removed', 'an event'];
    case 'set_mood':              return ['note', 'Logged', brief(input.mood)];
    case 'read_day_records':      return ['bookOpen', 'Read', 'her notes'];
    case 'add_whisper':           return ['note', 'Whispered', brief(input.text)];
    case 'read_whispers':         return ['bookOpen', 'Read', 'the whispers'];
"""

# brief 用来压掉换行、限长，避免日记正文把整行撑开。
BRIEF_HELPER = (
    "  const brief = (s, n) => { s = String(s == null ? '' : s)"
    ".replace(/\\s+/g, ' ').trim(); n = n || 28; "
    "return s.length > n ? s.slice(0, n) + '…' : s; };"
)


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


def _patch_tool_labels(html: str) -> str:
    """给自建工具补 verbOf 词条。

    上游 verbOf 只认 Read / Write / Bash 这类 Claude Code 内置工具，
    我们的 write_diary、add_whisper 等会掉进 default 分支，
    直接把函数名摊在屏幕上（“Used add whisper”）。
    """
    if "case 'write_diary'" in html:
        return html

    # 先注入 brief 助手，再插词条；两者都锚在 verbOf 内部。
    html = html.replace(
        "function verbOf(name, input) {\n  input = input || {};",
        "function verbOf(name, input) {\n  input = input || {};\n" + BRIEF_HELPER,
        1,
    )
    html = html.replace(VERBOF_ANCHOR, OUR_VERBS + VERBOF_ANCHOR, 1)
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

    # 上游 renderSaid 只认 kind='me'，而后端（和上游自己的演示数据）用的是
    # kind='her'，导致刷新后妍妍的消息全部不渲染。先还原再替换，
    # 保证重复构建不会叠成一长串 || 条件。
    html = html.replace(
        "if (m.kind === 'me' || m.kind === 'her') {",
        "if (m.kind === 'me') {",
    )
    html = html.replace(
        "if (m.kind === 'me') {",
        "if (m.kind === 'me' || m.kind === 'her') {",
    )

    # 上游重放历史工具卡片时把结果写死成空字符串（原意是「别让它转圈」），
    # 于是刷新后返回值和错误状态全部消失。后端已把结果并进同一条 tool 记录，
    # 这里改成读 m.result / m.is_error。
    html = html.replace(
        "markToolDone(tid, false, '');",
        "markToolDone(tid, !!m.is_error, m.result || '');",
    )

    html = _patch_tool_labels(html)

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
        source_text = source.read_text(encoding="utf-8") if source.exists() else ""
        built = _build_frontend(source) if source.exists() else ""
        return jsonify({
            "ok": True,
            "version": _git_version(repo_root),
            "frontend_source": str(source),
            "frontend_exists": source.exists(),
            "entrypoint": "run.py",
            "user_name": personalize.USER_NAME,
            "ai_name": personalize.AI_NAME,
            "together_since": personalize.TOGETHER_SINCE,
            # 补丁靠字符串匹配，上游一改就会静默失效；这里如实报告命中情况。
            "patches": {
                "demo_removed": DEMO_START not in built,
                "her_messages": "m.kind === 'me' || m.kind === 'her'" in built,
                "tool_result": "m.result || ''" in built,
                "tool_labels": "case 'write_diary'" in built,
                "verbof_anchor_found": VERBOF_ANCHOR in source_text,
            },
        })

    server_module.app.view_functions["index"] = index_real
    server_module.app.add_url_rule(
        "/api/version",
        endpoint="api_version",
        view_func=version_real,
        methods=["GET"],
    )
