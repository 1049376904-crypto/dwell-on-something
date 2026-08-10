"""让自建面板长得跟上游一个样。

背景：通知 / 模型 / 表情 / 备份四个页面是独立 HTML 文档，
当初每个都自己写了一套 :root 配色。问题是那几套值是我凭印象调的，
跟上游不是一个色系——上游强调色是砖红 #c96442（日历里今天那个圆点），
我却在模型页用了深绿；底色上游 #faf9f5，我写成 #f4f1ea。
并排一看就知道不是同一个应用里的页面。

修法沿用 #plusBtn 那次的教训：不要各自抄一份数值，要共用同一个来源。
这里运行时从 web/index.html 里把 :root 那段解析出来，
再用 after_request 往面板响应里注一段样式。好处是上游哪天调了配色，
四个面板跟着变，不需要回头改四个文件。

为什么用 after_request 而不是改那四个文件：
它们的 PANEL_HTML 都是几百行的字符串模板，逐个插占位符改动面积大、
容易打歪；而且以后再加面板会忘记带上。挂在响应上是一次性的。

为什么样式里有 !important：
模型页那个绿色按钮和绿色链接是写死的十六进制，不是变量。
只覆盖变量压不住它，必须在这些属性上强制。

关于深色模式：那四个面板原本各自带了 prefers-color-scheme: dark，
这里的覆盖会把它压掉，面板从此固定浅色。这是有意的——
主应用本身是浅色的（--bg #faf9f5），面板跟着系统变深反而更不协调。
"""

import re

from flask import request


# 解析不到时的兜底，取自上游 web/index.html 的 :root。
# 只在源文件结构变了的时候才会用上，/api/panel/vars 会如实报告。
FALLBACK_VARS = {
    "bg": "#faf9f5",
    "card": "#ffffff",
    "panel": "#f0eee6",
    "line": "#e8e5dc",
    "text": "#2b2a27",
    "dim": "#8a867c",
    "accent": "#c96442",
    "accent-soft": "#d97757",
}

# 需要注样式的路径。以后加面板往这里添一条即可。
PANEL_PATHS = {"/push", "/models", "/stickers", "/backup"}

ROOT_BLOCK = re.compile(r":root\s*\{([^}]*)\}")
VAR_LINE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")


def _parse_vars(html):
    """从 index.html 里抠出 :root 的变量表。

    取第一个 :root 块。上游那块就在主样式表开头，
    后面若有别的 :root（主题变体之类）不该覆盖基准值。
    """
    match = ROOT_BLOCK.search(html)
    if not match:
        return dict(FALLBACK_VARS), False

    found = {}
    for name, value in VAR_LINE.findall(match.group(1)):
        found[name] = value.strip()
    if not found:
        return dict(FALLBACK_VARS), False

    merged = dict(FALLBACK_VARS)
    merged.update(found)
    return merged, True


def _build_style(v):
    """拼出注进面板的那段样式。

    变量名同时给两套：上游用 --text，我那几个面板用 --fg，
    两个都定义，面板里已有的 var() 引用不用改。
    """
    return f"""<style id="dwell-panel-shell">
  /* 覆盖面板自带的 :root，包括它们各自的深色模式那一段。 */
  :root, :root[data-theme] {{
    --bg: {v['bg']};
    --card: {v['card']};
    --panel: {v['panel']};
    --line: {v['line']};
    --text: {v['text']};
    --fg: {v['text']};
    --dim: {v['dim']};
    --accent: {v['accent']};
    --accent-soft: {v.get('accent-soft', v['accent'])};
    /* 面板里输入框和标记用的名字，一并映射到上游的值。 */
    --field: {v['panel']};
    --todo: {v['line']};
    --todobg: {v['bg']};
  }}

  body {{
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: -apple-system, "SF Pro Text", system-ui, sans-serif;
    /* 顶部留出关闭键的位置。 */
    padding: calc(56px + env(safe-area-inset-top)) 20px
             calc(44px + env(safe-area-inset-bottom)) !important;
  }}

  /* 标题按上游那种大而轻的写法，不用小号粗体。 */
  h1 {{
    font-size: 30px !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em;
    margin: 0 0 6px !important;
    color: var(--text) !important;
  }}
  h2 {{
    font-size: 14px !important;
    font-weight: 600 !important;
    color: var(--dim) !important;
    letter-spacing: .04em;
    margin: 0 0 12px !important;
  }}
  .sub {{ color: var(--dim) !important; font-size: 13.5px !important; }}
  a {{ color: var(--accent) !important; text-decoration: none !important; }}

  .card {{
    background: var(--card) !important;
    border: 1px solid var(--line) !important;
    border-radius: 18px !important;
    padding: 16px !important;
  }}

  /* 控件一律药丸形，跟日历上那排「今天」「‹」「›」同一个语言。 */
  button, .btn {{
    border-radius: 999px !important;
    background: var(--panel) !important;
    border: 1px solid transparent !important;
    color: var(--text) !important;
    min-height: 44px;
    padding: 0 18px !important;
    font-family: inherit;
  }}
  button.go, button.primary {{
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #fff !important;
  }}
  button:disabled {{ opacity: .45 !important; }}

  input[type=text], input[type=password], input[type=number], textarea {{
    background: var(--panel) !important;
    border: 1px solid transparent !important;
    border-radius: 14px !important;
    color: var(--text) !important;
    padding: 11px 13px !important;
    font-family: inherit;
  }}
  input::placeholder {{ color: var(--dim) !important; }}
  /* 待改名那种提示色，用强调色的淡描边，不另造一个颜色。 */
  input.todo {{
    border-color: var(--accent) !important;
    background: var(--bg) !important;
  }}

  /* 上传区沿用上游「+ 记一件事」的虚线框。 */
  label.file {{
    border: 1px dashed var(--line) !important;
    border-radius: 14px !important;
    color: var(--dim) !important;
  }}

  .danger {{
    border-color: var(--accent) !important;
    background: {v['bg']} !important;
  }}
  #msg.warn, .warn {{ color: var(--accent) !important; }}
  code {{ background: var(--panel) !important; }}

  /* 左上角的关闭键，位置和粗细照日记那几页。 */
  #dwellPanelClose {{
    position: fixed;
    top: calc(10px + env(safe-area-inset-top));
    left: 8px;
    z-index: 50;
    width: 44px; height: 44px;
    display: flex; align-items: center; justify-content: center;
    background: transparent !important;
    border: 0 !important;
    padding: 0 !important;
    color: var(--text) !important;
    cursor: pointer;
  }}
  #dwellPanelClose svg {{ width: 22px; height: 22px; }}
</style>
<script>
(function () {
  // 面板都是独立文档，没有上游那个 sheet 的关闭键。
  // 补一个 ×：能回上一页就回，直接输地址进来的就回聊天页。
  if (document.getElementById('dwellPanelClose')) return;
  var b = document.createElement('button');
  b.id = 'dwellPanelClose';
  b.type = 'button';
  b.setAttribute('aria-label', '关闭');
  b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/>' +
    '<line x1="18" y1="6" x2="6" y2="18"/></svg>';
  b.onclick = function () {
    if (history.length > 1) history.back();
    else location.href = '/';
  };
  document.body.appendChild(b);
})();
</script>
"""


def register_panel_shell(server_module):
    from pathlib import Path

    from flask import jsonify

    source = Path(__file__).resolve().parent.parent / "web" / "index.html"
    cache = {"key": None, "vars": dict(FALLBACK_VARS), "parsed": False, "style": ""}

    def current():
        """读一次源文件解析变量，按 mtime 缓存。

        每个请求重新读 293KB 并跑正则没必要，
        但也不能只读一次——git pull 之后要能跟上。
        """
        try:
            stat = source.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None

        if key is not None and key == cache["key"]:
            return cache

        variables, parsed = dict(FALLBACK_VARS), False
        if key is not None:
            try:
                variables, parsed = _parse_vars(source.read_text(encoding="utf-8"))
            except OSError:
                pass

        cache.update(key=key, vars=variables, parsed=parsed, style=_build_style(variables))
        return cache

    @server_module.app.after_request
    def inject_panel_shell(response):
        # 只碰自建面板。主页由 frontend_feature 负责，别插手。
        if request.path not in PANEL_PATHS:
            return response
        if not response.content_type or "text/html" not in response.content_type:
            return response
        if response.direct_passthrough:
            return response

        html = response.get_data(as_text=True)
        # 幂等：重复注入会叠出一堆同名 style。
        if 'id="dwell-panel-shell"' in html or "</head>" not in html:
            return response

        state = current()
        response.set_data(html.replace("</head>", state["style"] + "</head>", 1))
        return response

    def api_panel_vars():
        """解析结果诊断。

        parsed 为 false 说明上游 :root 的写法变了、现在用的是兜底值——
        面板不会崩，但配色可能又开始跟主应用脱节。
        """
        state = current()
        return jsonify({
            "ok": True,
            "source": str(source),
            "source_exists": source.exists(),
            "parsed_from_upstream": state["parsed"],
            "vars": state["vars"],
            "panels": sorted(PANEL_PATHS),
        })

    server_module.app.add_url_rule(
        "/api/panel/vars",
        endpoint="api_panel_vars",
        view_func=api_panel_vars,
        methods=["GET"],
    )

    state = current()
    print(
        "[dwell] 面板样式: "
        + ("已读取上游 :root" if state["parsed"] else "上游 :root 没解析出来，用兜底值")
        + f"（强调色 {state['vars']['accent']}）"
    )
    return current
