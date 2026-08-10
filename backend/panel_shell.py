"""让自建面板长得跟上游一个样，并且行为也一样。

## 配色

通知 / 模型 / 表情 / 备份四个页面是独立 HTML 文档，当初每个都自己写了
一套 :root 配色。那几套值是我凭印象调的，跟上游不是一个色系——
上游强调色是砖红 #c96442（日历里今天那个圆点），我却在模型页用了深绿；
底色上游 #faf9f5，我写成 #f4f1ea。并排一看就知道不是同一个应用。

修法沿用 #plusBtn 那次的教训：不要各自抄一份数值，要共用同一个来源。
运行时从 web/index.html 把 :root 解析出来，再往面板响应里注一段样式。
上游哪天调了配色，四个面板跟着变。

## 形态

更要紧的是行为。上游的日记、日子那些页面是**同一个文档里的浮层**，
它的关闭长这样：

    wrap.classList.remove('open');
    if (wrap.classList.contains('page')) openDrawer();

关掉浮层，是「page」就把抽屉重新拉开——所以点 × 会回到侧边栏。
而我那四个是独立文档，× 只能 history.back()，那会让整个应用重新加载，
应用每次启动又都从锁屏开始。于是点叉看起来就像把 App 重启了。
这不是叉叉写错，是形态选错了。

所以这里在主页注入一个装 iframe 的浮层，并在运行时把侧边栏那几项的
点击改成「开浮层」而不是「跳地址」。主文档从此不卸载：没有锁屏，
抽屉还在原地，关闭时调 openDrawer()，跟上游完全一致。
面板本身的 HTML 一行没改，它们仍然可以单独用地址打开。

为什么在运行时改按钮、而不是回 frontend_feature 改字符串补丁：
那边的补丁靠精确匹配上游源码，每加一处就多一个会静默失效的点。
按钮的 id 是我自己插的，运行时按 id 取到再换 onclick 更稳，
而且万一没取到，原来的跳转行为还在，不至于点了没反应。

## 关于本文件里为什么一个 f-string 都没有

上一版把样式写成 f-string，里面还夹着一段 JS。CSS 的大括号做了转义，
JS 的漏了，于是 Python 在 import 这一刻就抛 SyntaxError——后端起不来，
pm2 进程死掉，Cloudflare 直接给 502。模板一律走 __NAME__ 占位符加
replace，大括号不参与格式化。
"""

import re
from pathlib import Path

from flask import jsonify, request


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

# 需要注入统一样式的面板。以后加面板往这里添一条即可。
PANEL_PATHS = ("/push", "/models", "/stickers", "/backup")

# 注入浮层宿主的页面。只有主页。
MAIN_PATHS = ("/",)

ROOT_BLOCK = re.compile(r":root\s*\{([^}]*)\}")
VAR_LINE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")


# ── 注进面板 <head> 的样式
#
# 占位符用 __NAME__，不用 f-string 也不用 format：
# 这里面 CSS 和 JS 的大括号很多，任何带大括号语义的格式化方式
# 都得逐个转义，漏一个就是 import 期的 SyntaxError。
STYLE_TEMPLATE = """<style id="dwell-panel-shell">
  /* 覆盖面板自带的 :root，包括它们各自的深色模式那一段。 */
  :root, :root[data-theme] {
    --bg: __BG__;
    --card: __CARD__;
    --panel: __PANEL__;
    --line: __LINE__;
    --text: __TEXT__;
    --fg: __TEXT__;
    --dim: __DIM__;
    --accent: __ACCENT__;
    --accent-soft: __ACCENT_SOFT__;
    /* 面板里输入框和标记用的名字，一并映射到上游的值。 */
    --field: __PANEL__;
    --todo: __LINE__;
    --todobg: __BG__;
  }

  body {
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: -apple-system, "SF Pro Text", system-ui, sans-serif;
    /* 顶部留出关闭键的位置。装在浮层里时那个键在外层，位置一样。 */
    padding: calc(56px + env(safe-area-inset-top)) 20px
             calc(44px + env(safe-area-inset-bottom)) !important;
  }

  /* 标题按上游那种大而轻的写法，不用小号粗体。 */
  h1 {
    font-size: 30px !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em;
    margin: 0 0 6px !important;
    color: var(--text) !important;
  }
  h2 {
    font-size: 14px !important;
    font-weight: 600 !important;
    color: var(--dim) !important;
    letter-spacing: .04em;
    margin: 0 0 12px !important;
  }
  .sub { color: var(--dim) !important; font-size: 13.5px !important; }
  a { color: var(--accent) !important; text-decoration: none !important; }

  .card {
    background: var(--card) !important;
    border: 1px solid var(--line) !important;
    border-radius: 18px !important;
    padding: 16px !important;
  }

  /* 控件一律药丸形，跟日历上那排「今天」「‹」「›」同一个语言。 */
  button, .btn {
    border-radius: 999px !important;
    background: var(--panel) !important;
    border: 1px solid transparent !important;
    color: var(--text) !important;
    min-height: 44px;
    padding: 0 18px !important;
    font-family: inherit;
  }
  button.go, button.primary {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
    color: #fff !important;
  }
  button:disabled { opacity: .45 !important; }

  input[type=text], input[type=password], input[type=number], textarea {
    background: var(--panel) !important;
    border: 1px solid transparent !important;
    border-radius: 14px !important;
    color: var(--text) !important;
    padding: 11px 13px !important;
    font-family: inherit;
  }
  input::placeholder { color: var(--dim) !important; }
  /* 待改名那种提示色，用强调色的淡描边，不另造一个颜色。 */
  input.todo {
    border-color: var(--accent) !important;
    background: var(--bg) !important;
  }

  /* 上传区沿用上游「+ 记一件事」的虚线框。 */
  label.file {
    border: 1px dashed var(--line) !important;
    border-radius: 14px !important;
    color: var(--dim) !important;
  }

  .danger {
    border-color: var(--accent) !important;
    background: var(--bg) !important;
  }
  #msg.warn, .warn { color: var(--accent) !important; }
  code { background: var(--panel) !important; }

  /* 单独用地址打开时才有的关闭键，位置照日记那几页。 */
  #dwellPanelClose {
    position: fixed;
    top: calc(10px + env(safe-area-inset-top));
    left: 8px;
    z-index: 50;
    width: 44px;
    height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent !important;
    border: 0 !important;
    padding: 0 !important;
    min-height: 0;
    color: var(--text) !important;
    cursor: pointer;
  }
  #dwellPanelClose svg { width: 22px; height: 22px; }
</style>
<script>
(function () {
  // 被装进主页那个浮层里的时候，很多事要交给外层做。
  var framed = false;
  try { framed = !!(window.parent && window.parent !== window); }
  catch (e) { framed = true; }   // 跨源读不到 parent，那也是被套着

  function tellParent() {
    try { window.parent.postMessage('dwell-panel-close', '*'); } catch (e) {}
  }

  function mount() {
    if (!document.body) return;
    // 装在浮层里时外层已经有一个 ×，这里再挂一个就叠成两个了。
    if (framed) return;
    if (document.getElementById('dwellPanelClose')) return;
    var b = document.createElement('button');
    b.id = 'dwellPanelClose';
    b.type = 'button';
    b.setAttribute('aria-label', '关闭');
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round">' +
      '<line x1="6" y1="6" x2="18" y2="18"/>' +
      '<line x1="18" y1="6" x2="6" y2="18"/></svg>';
    b.onclick = function () {
      if (history.length > 1) history.back();
      else location.href = '/';
    };
    document.body.appendChild(b);
  }

  // 页内那几个「回聊天」「回到应用」链接指向 /。
  // 在浮层里直接导航会把整个应用装进 iframe，所以改成让外层关掉浮层。
  function guardLinks() {
    document.addEventListener('click', function (e) {
      var node = e.target;
      var a = node && node.closest ? node.closest('a') : null;
      if (!a) return;
      var href = a.getAttribute('href') || '';
      if (href === '/' || href === './' || href === '') {
        e.preventDefault();
        tellParent();
      }
    }, true);
  }

  // 这段脚本在 <head> 里执行，那时 document.body 还是 null，
  // 直接 appendChild 会抛错、按钮根本挂不上。必须等文档解析完。
  function start() {
    mount();
    if (framed) guardLinks();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
</script>
"""

# 占位符 -> 变量名。accent-soft 缺失时回落到 accent。
SLOTS = (
    ("__BG__", "bg"),
    ("__CARD__", "card"),
    ("__PANEL__", "panel"),
    ("__LINE__", "line"),
    ("__TEXT__", "text"),
    ("__DIM__", "dim"),
    ("__ACCENT_SOFT__", "accent-soft"),
    ("__ACCENT__", "accent"),
)


# ── 注进主页的浮层宿主
#
# 没有占位符，纯字符串，颜色一律走上游自己的 CSS 变量。
HOST_SCRIPT = """<style id="dwell-panel-host-style">
  #dwellPanelHost {
    position: fixed;
    inset: 0;
    z-index: 12000;
    background: var(--bg, #faf9f5);
    transform: translateY(100%);
    transition: transform .24s ease;
    visibility: hidden;
  }
  #dwellPanelHost.open { transform: none; visibility: visible; }
  #dwellPanelFrame {
    width: 100%;
    height: 100%;
    border: 0;
    display: block;
    background: var(--bg, #faf9f5);
  }
  #dwellPanelHostClose {
    position: absolute;
    top: calc(10px + env(safe-area-inset-top));
    left: 8px;
    z-index: 2;
    width: 44px;
    height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 0;
    padding: 0;
    color: var(--text, #2b2a27);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  #dwellPanelHostClose svg { width: 22px; height: 22px; }
</style>
<script>
(function () {
  // 这几个路径原本是「跳过去」，现在改成在浮层里打开。
  // 主文档不卸载，所以不会经过锁屏，抽屉也还在原地。
  var HOSTED = { '/push': 1, '/models': 1, '/stickers': 1, '/backup': 1 };

  var wrap = null, frame = null;

  function build() {
    if (wrap) return;
    wrap = document.createElement('div');
    wrap.id = 'dwellPanelHost';

    var btn = document.createElement('button');
    btn.id = 'dwellPanelHostClose';
    btn.type = 'button';
    btn.setAttribute('aria-label', '关闭');
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round">' +
      '<line x1="6" y1="6" x2="18" y2="18"/>' +
      '<line x1="18" y1="6" x2="6" y2="18"/></svg>';
    btn.onclick = close;

    frame = document.createElement('iframe');
    frame.id = 'dwellPanelFrame';
    frame.setAttribute('title', '面板');

    wrap.appendChild(frame);
    wrap.appendChild(btn);
    document.body.appendChild(wrap);
  }

  function open(path) {
    build();
    // 跟上游一样：开浮层之前先把抽屉收起来。
    try { if (typeof closeDrawer === 'function') closeDrawer(); } catch (e) {}
    frame.src = path;
    // 先让浏览器画一帧，否则 transform 的过渡不会生效。
    requestAnimationFrame(function () { wrap.classList.add('open'); });
  }

  function close() {
    if (!wrap) return;
    wrap.classList.remove('open');
    // 收起动画走完再断开 iframe：立刻断会看到内容先白一下。
    setTimeout(function () { if (frame) frame.src = 'about:blank'; }, 260);
    // 这一句就是上游的行为：关掉浮层回到侧边栏。
    try { if (typeof openDrawer === 'function') openDrawer(); } catch (e) {}
  }

  // 面板里的「回聊天」会往这边发消息。
  window.addEventListener('message', function (e) {
    if (e && e.data === 'dwell-panel-close') close();
  });

  // 侧边栏那几项：按 id 取到再换掉 onclick。
  // 取不到就什么都不做——原来的跳转还在，不至于点了没反应。
  var NAV = [
    ['navPush', '/push'],
    ['navModels', '/models'],
    ['navBackup', '/backup']
  ];

  function bind() {
    NAV.forEach(function (pair) {
      var el = document.getElementById(pair[0]);
      if (!el || el.dataset.dwellHosted) return;
      el.dataset.dwellHosted = '1';
      el.onclick = function () { open(pair[1]); };
    });
  }

  // 页面里指向面板的链接也一并接过来，
  // 比如表情快发面板右上角那个「管理」。
  function delegate() {
    document.addEventListener('click', function (e) {
      var node = e.target;
      var a = node && node.closest ? node.closest('a') : null;
      if (!a) return;
      var href = a.getAttribute('href') || '';
      if (HOSTED[href]) {
        e.preventDefault();
        open(href);
      }
    }, true);
  }

  function start() {
    bind();
    delegate();
    // 侧边栏是上游脚本later才画出来的，慢一步的话按 id 取不到。
    var tries = 0;
    var timer = setInterval(function () {
      bind();
      if (++tries > 10) clearInterval(timer);
    }, 1000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.dwellPanels = { open: open, close: close };
})();
</script>
"""


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


def _build_style(variables):
    """把变量填进模板。

    __ACCENT_SOFT__ 必须排在 __ACCENT__ 前面替换，
    否则 __ACCENT__ 会先把它的前半截吃掉，剩下一个 _SOFT__ 挂在颜色后面。
    SLOTS 里已经是这个顺序。
    """
    style = STYLE_TEMPLATE
    for slot, key in SLOTS:
        value = variables.get(key) or variables.get("accent") or "#c96442"
        style = style.replace(slot, value)
    return style


def register_panel_shell(server_module):
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

        if key is not None and key == cache["key"] and cache["style"]:
            return cache

        variables, parsed = dict(FALLBACK_VARS), False
        if key is not None:
            try:
                variables, parsed = _parse_vars(source.read_text(encoding="utf-8"))
            except OSError:
                pass

        cache["key"] = key
        cache["vars"] = variables
        cache["parsed"] = parsed
        cache["style"] = _build_style(variables)
        return cache

    def inject(html, marker, block):
        """幂等地往 </head> 前塞一段。塞不进去就原样返回。"""
        if marker in html or "</head>" not in html:
            return None
        return html.replace("</head>", block + "</head>", 1)

    @server_module.app.after_request
    def inject_panel_shell(response):
        path = request.path
        if path not in PANEL_PATHS and path not in MAIN_PATHS:
            return response
        if not response.content_type or "text/html" not in response.content_type:
            return response
        if response.direct_passthrough:
            return response

        try:
            html = response.get_data(as_text=True)
        except (UnicodeDecodeError, RuntimeError):
            return response

        if path in PANEL_PATHS:
            updated = inject(html, "dwell-panel-shell", current()["style"])
        else:
            # 主页只加浮层宿主，配色由 frontend_feature 自己管。
            updated = inject(html, "dwell-panel-host-style", HOST_SCRIPT)

        if updated is not None:
            response.set_data(updated)
        return response

    def api_panel_vars():
        """解析结果诊断。

        parsed_from_upstream 为 false 说明上游 :root 的写法变了、
        现在用的是兜底值——面板不会崩，但配色可能又开始跟主应用脱节。
        """
        state = current()
        return jsonify({
            "ok": True,
            "source": str(source),
            "source_exists": source.exists(),
            "parsed_from_upstream": state["parsed"],
            "vars": state["vars"],
            "panels": list(PANEL_PATHS),
            "hosted_in_overlay": True,
        })

    server_module.app.add_url_rule(
        "/api/panel/vars",
        endpoint="api_panel_vars",
        view_func=api_panel_vars,
        methods=["GET"],
    )

    state = current()
    where = "已读取上游 :root" if state["parsed"] else "上游 :root 没解析出来，用兜底值"
    accent = state["vars"].get("accent", "?")
    print("[dwell] 面板样式: " + where + "（强调色 " + accent + "，浮层内打开）")
    return current
