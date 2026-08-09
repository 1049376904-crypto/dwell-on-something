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

# ICONS 表的锚点，上游第一条。
ICONS_ANCHOR = "const ICONS = {\n"

# cpu 不在上游那张表里，补一个 feather 风格的。
EXTRA_ICONS = (
    "  cpu: S('<rect x=\"4\" y=\"4\" width=\"16\" height=\"16\" rx=\"2\"/>"
    "<rect x=\"9\" y=\"9\" width=\"6\" height=\"6\"/>"
    "<line x1=\"9\" y1=\"1\" x2=\"9\" y2=\"4\"/>"
    "<line x1=\"15\" y1=\"1\" x2=\"15\" y2=\"4\"/>"
    "<line x1=\"9\" y1=\"20\" x2=\"9\" y2=\"23\"/>"
    "<line x1=\"15\" y1=\"20\" x2=\"15\" y2=\"23\"/>"
    "<line x1=\"20\" y1=\"9\" x2=\"23\" y2=\"9\"/>"
    "<line x1=\"20\" y1=\"14\" x2=\"23\" y2=\"14\"/>"
    "<line x1=\"1\" y1=\"9\" x2=\"4\" y2=\"9\"/>"
    "<line x1=\"1\" y1=\"14\" x2=\"4\" y2=\"14\"/>'),\n"
    "  paperclip: S('<path d=\"M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19"
    "a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48\"/>'),\n"
)

# 侧边栏锚点：上游的「日记」按钮。
NAV_WALL_BUTTON = (
    '<button class="item" id="navWall">'
    '<span class="ic" data-i="pen"></span>日记</button>'
)

# 悄悄话、通知、模型三个入口。都用 button 而不是 a：
# 上游 .item 的样式是给 button 写的，用 <a> 会吃到全局链接色，
# 在侧边栏里显示成一行蓝色带下划线的字，跟旁边几项完全不搭。
NAV_EXTRA_BUTTONS = (
    NAV_WALL_BUTTON
    + '\n      <button class="item" id="navWhisper">'
      '<span class="ic" data-i="note"></span>悄悄话</button>'
    + '\n      <button class="item" id="navPush">'
      '<span class="ic" data-i="bell"></span>通知</button>'
    + '\n      <button class="item" id="navModels">'
      '<span class="ic" data-i="cpu"></span>模型</button>'
)

# 旧版本插过的按钮组合，用于重新构建时先还原。
NAV_LEGACY_VARIANTS = (
    NAV_WALL_BUTTON
    + '\n      <button class="item" id="navWhisper">'
      '<span class="ic" data-i="note"></span>悄悄话</button>'
    + '\n      <button class="item" id="navPush">'
      '<span class="ic" data-i="bell"></span>通知</button>'
    + '\n      <button class="item" id="navModels">'
      '<span class="ic" data-i="box"></span>模型</button>',
)

NAV_WALL_HANDLER = (
    "document.getElementById('navWall').onclick = () => { closeDrawer(); "
    "sheets.wall.classList.add('open'); loadWall(); };"
)

NAV_EXTRA_HANDLERS = (
    NAV_WALL_HANDLER
    + "\ndocument.getElementById('navWhisper').onclick = () => { closeDrawer(); "
      "sheets.wall.classList.add('open'); renderWhisper(); };"
    + "\ndocument.getElementById('navPush').onclick = () => { location.href = '/push'; };"
    + "\ndocument.getElementById('navModels').onclick = () => { location.href = '/models'; };"
)

# 图片上传。刻意不改上游的发送逻辑：上传完把 markdown 插进输入框，
# 剩下的交给原有的发送流程，图片就跟着文字一起走 /api/send。
UPLOAD_SCRIPT = """
<script>
(function () {
  const MAX_EDGE = 1600;      // 长边上限
  const QUALITY = 0.8;        // JPEG 质量
  const SKIP_UNDER = 300000;  // 小于这个就不压了，省得白转一遍

  function findComposer() {
    return document.querySelector('#composer textarea')
      || document.querySelector('textarea#input')
      || document.querySelector('form textarea')
      || document.querySelector('textarea');
  }

  // iPhone 原图四五兆，1.6G 内存的机器连传几张就可能 OOM。
  async function shrink(file) {
    if (!/^image\\//.test(file.type)) return null;
    if (file.type === 'image/gif') return file;   // 动图压了就不动了
    if (file.size <= SKIP_UNDER) return file;

    const bitmap = await createImageBitmap(file).catch(() => null);
    if (!bitmap) return file;

    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    if (scale >= 1 && file.size <= 2000000) return file;

    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close && bitmap.close();

    const blob = await new Promise((res) => canvas.toBlob(res, 'image/jpeg', QUALITY));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], 'photo.jpg', { type: 'image/jpeg' });
  }

  async function upload(file) {
    const small = await shrink(file);
    if (!small) return null;
    const form = new FormData();
    form.append('file', small, small.name || 'photo.jpg');
    const resp = await (await fetch('/api/upload', { method: 'POST', body: form })).json();
    if (!resp.ok) throw new Error(resp.error || '上传失败');
    return resp;
  }

  async function handle(files) {
    const box = findComposer();
    if (!box) return;
    const images = [...files].filter((f) => /^image\\//.test(f.type));
    if (!images.length) return;

    const mark = document.getElementById('dwellUploadHint');
    if (mark) mark.textContent = '正在上传 ' + images.length + ' 张…';

    for (const file of images) {
      try {
        const r = await upload(file);
        if (!r) continue;
        box.value = (box.value ? box.value.replace(/\\s*$/, '') + '\\n' : '') + r.markdown + '\\n';
        box.dispatchEvent(new Event('input', { bubbles: true }));
      } catch (e) {
        if (mark) mark.textContent = '上传失败：' + e.message;
        return;
      }
    }
    if (mark) mark.textContent = '';
    box.focus();
  }

  function mount() {
    const box = findComposer();
    if (!box || document.getElementById('dwellPick')) return;

    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.multiple = true;
    input.id = 'dwellPick';
    input.style.display = 'none';
    input.onchange = () => { handle(input.files); input.value = ''; };

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'dwellPickBtn';
    btn.title = '发图片';
    btn.innerHTML = '<span class="ic" data-i="paperclip"></span>';
    btn.style.cssText = 'background:transparent;border:0;color:var(--dim);'
      + 'cursor:pointer;padding:6px 8px;line-height:1;';
    btn.onclick = () => input.click();

    const hint = document.createElement('span');
    hint.id = 'dwellUploadHint';
    hint.style.cssText = 'color:var(--dim);font-size:13px;margin-left:6px;';

    const bar = box.parentElement;
    bar.appendChild(input);
    bar.appendChild(btn);
    bar.appendChild(hint);

    // 上游的图标是靠 data-i 在启动时统一填充的，动态加的这个要自己画。
    if (window.ICONS && window.ICONS.paperclip) {
      btn.querySelector('.ic').innerHTML = window.ICONS.paperclip;
    }

    // 粘贴和拖拽都接上，手机上主要用选择器，桌面上这两个更顺手。
    box.addEventListener('paste', (ev) => {
      const files = ev.clipboardData && ev.clipboardData.files;
      if (files && files.length) { ev.preventDefault(); handle(files); }
    });
    ['dragover', 'drop'].forEach((type) => {
      box.addEventListener(type, (ev) => {
        ev.preventDefault();
        if (type === 'drop' && ev.dataTransfer) handle(ev.dataTransfer.files);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
  // 上游有些界面是后建的，兜底再试几次。
  setTimeout(mount, 800);
  setTimeout(mount, 2500);
})();
</script>
"""


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


def _patch_icons(html: str) -> str:
    """往 ICONS 表里补几个上游没有的图标，并把表挂到 window 上。"""
    if "  cpu: S(" not in html:
        html = html.replace(ICONS_ANCHOR, ICONS_ANCHOR + EXTRA_ICONS, 1)
    if "window.ICONS = ICONS;" not in html:
        # 动态添加的按钮要能自己取图标，所以暴露出来。
        html = html.replace(
            "/* ============ 线条图标",
            "window.__dwellIconsHook = 1;\n/* ============ 线条图标",
            1,
        )
        html = html.replace(
            "\nfunction basename(p)",
            "\nwindow.ICONS = ICONS;\nfunction basename(p)",
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


def _patch_nav(html: str) -> str:
    """补悄悄话、通知、模型三个侧边栏入口。"""
    # 清掉早期版本用 <a> 写的通知入口，避免重复和蓝色链接残留。
    html = html.replace(
        '\n      <a class="item" href="/push">'
        '<span class="ic" data-i="note"></span>通知</a>',
        "",
    )

    if NAV_EXTRA_BUTTONS not in html:
        # 先把历史版本的按钮组合还原成原始按钮，再统一插入当前版本。
        for legacy in NAV_LEGACY_VARIANTS:
            html = html.replace(legacy, NAV_WALL_BUTTON)
        html = html.replace(
            NAV_WALL_BUTTON
            + '\n      <button class="item" id="navWhisper">'
              '<span class="ic" data-i="note"></span>悄悄话</button>'
            + '\n      <button class="item" id="navPush">'
              '<span class="ic" data-i="bell"></span>通知</button>',
            NAV_WALL_BUTTON,
        )
        html = html.replace(NAV_WALL_BUTTON, NAV_EXTRA_BUTTONS, 1)

    if "getElementById('navModels')" not in html:
        html = html.replace(
            NAV_WALL_HANDLER
            + "\ndocument.getElementById('navWhisper').onclick = () => { closeDrawer(); "
              "sheets.wall.classList.add('open'); renderWhisper(); };"
            + "\ndocument.getElementById('navPush').onclick = () => { location.href = '/push'; };",
            NAV_WALL_HANDLER,
        )
        html = html.replace(NAV_WALL_HANDLER, NAV_EXTRA_HANDLERS, 1)

    return html


def _patch_head(html: str, icon_links: str) -> str:
    """补 PWA 清单、图标和主屏标题。

    清单是 iOS 的硬性前提：只有「添加到主屏幕」之后才允许申请通知权限。
    apple-mobile-web-app-title 决定主屏图标下面显示的名字，同时也是
    通知第二行「from X」里的 X——iOS 不给这两处分开，安装时手填的名字
    优先级最高。
    """
    head_bits = []

    if 'rel="manifest"' not in html:
        head_bits.append('  <link rel="manifest" href="/manifest.json">')
    if "apple-mobile-web-app-title" not in html:
        head_bits.append(
            '  <meta name="apple-mobile-web-app-title" content="'
            + personalize.HOME_SCREEN_NAME + '">'
        )
    if icon_links and "apple-touch-icon" not in html:
        head_bits.append(icon_links.rstrip("\n"))

    if head_bits:
        html = html.replace("</head>", "\n".join(head_bits) + "\n</head>", 1)
    return html


def _patch_tail(html: str, push_script: str) -> str:
    """把推送和图片上传脚本放进页面末尾。"""
    bits = []
    if push_script and "window.dwellPush" not in html:
        bits.append(push_script)
    if "dwellPickBtn" not in html:
        bits.append(UPLOAD_SCRIPT)
    if bits:
        html = html.replace("</body>", "".join(bits) + "</body>", 1)
    return html


def _build_frontend(source: Path, push_script: str = "", icon_links: str = "") -> str:
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

    html = _patch_icons(html)
    html = _patch_tool_labels(html)
    html = _patch_head(html, icon_links)
    html = _patch_tail(html, push_script)

    # 允许空日记正常进入主页。
    html = html.replace(
        "if (!d.ok || !d.bricks.length) throw 0;",
        "if (!d.ok) throw 0;",
    )
    html = html.replace(
        "const latestDate = boardDates[boardDates.length - 1];",
        "const latestDate = boardDates[boardDates.length - 1] || todayStr();",
    )

    html = _patch_nav(html)

    return html


def register_frontend_feature(server_module):
    repo_root = Path(__file__).resolve().parent.parent
    source = repo_root / "web" / "index.html"

    def push_script():
        # push_feature 可能还没注册（或注册失败），推送脚本就当不存在。
        return getattr(server_module, "push_client_script", "") or ""

    def icon_links():
        fn = getattr(server_module, "icon_html_links", None)
        return fn() if callable(fn) else ""

    def index_real():
        if not source.exists():
            return Response("找不到 web/index.html", status=500, mimetype="text/plain")
        html = _build_frontend(source, push_script(), icon_links())
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Dwell-Version"] = _git_version(repo_root)
        return response

    def version_real():
        source_text = source.read_text(encoding="utf-8") if source.exists() else ""
        built = _build_frontend(source, push_script(), icon_links()) if source.exists() else ""
        return jsonify({
            "ok": True,
            "version": _git_version(repo_root),
            "frontend_source": str(source),
            "frontend_exists": source.exists(),
            "entrypoint": "run.py",
            "user_name": personalize.USER_NAME,
            "ai_name": personalize.AI_NAME,
            "home_screen_name": personalize.HOME_SCREEN_NAME,
            "push_title": personalize.PUSH_TITLE,
            "together_since": personalize.TOGETHER_SINCE,
            # 补丁靠字符串匹配，上游一改就会静默失效；这里如实报告命中情况。
            "patches": {
                "demo_removed": DEMO_START not in built,
                "her_messages": "m.kind === 'me' || m.kind === 'her'" in built,
                "tool_result": "m.result || ''" in built,
                "tool_labels": "case 'write_diary'" in built,
                "verbof_anchor_found": VERBOF_ANCHOR in source_text,
                "push_script": "window.dwellPush" in built,
                "upload_script": "dwellPickBtn" in built,
                "cpu_icon": "  cpu: S(" in built,
                "icons_exposed": "window.ICONS = ICONS;" in built,
                "manifest_link": 'rel="manifest"' in built,
                "apple_icon": "apple-touch-icon" in built,
                "push_nav": 'id="navPush"' in built,
                "models_nav": 'id="navModels"' in built,
                "nav_anchor_found": NAV_WALL_BUTTON in source_text,
                "icons_anchor_found": ICONS_ANCHOR in source_text,
            },
        })

    server_module.app.view_functions["index"] = index_real
    server_module.app.add_url_rule(
        "/api/version",
        endpoint="api_version",
        view_func=version_real,
        methods=["GET"],
    )
