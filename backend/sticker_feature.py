"""表情包：双方都能发，而且比图片小一截。

上游没有表情包骨架。它那份文档里的做法是「一堆图 + 一份索引」，
AI 把图片链接写进回话，靠 IMG_RE 渲染成 <img> 就完事——
没有面板、没有独立气泡，也没有尺寸区分。所以这一块是从零搭的。

存储：原字节落盘 data/stickers，数据库只存一行索引。
刻意不压缩、不过 canvas：上游发图那条路走 shrinkImage，
GIF 进去出来就只剩第一帧了。表情包大半是动图，不能走那条路。

消息里存的是 markdown，但路径用 /sticker/ 而不是 /media/：
* 前端靠这个前缀把它画小（见 frontend_feature 里的 STICKER_STYLE）；
* media_feature 的 MEDIA_PATTERN 只认 /media/，所以表情不会被转成 base64
  塞进上下文——一屏表情包如果都转成图传上去，钱和上下文都烧得很快。

alt 里写表情的名字（![猫猫抱抱](/sticker/ab12.gif)），
这样沐翻历史时看得出刚才发过什么、妍妍发了什么，不靠看图。
名字就是索引。起得越具体，它挑得越准。

工具接入方式：不改 agent_tools_feature，而是在这里往它的 TOOLS 里追两条、
给 execute_tool 和 build_context_snapshot 各包一层。两边都是模块级全局，
call_gateway 每轮重新取，所以注册顺序不要紧。
"""

import base64
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory


# 单张上限。表情包本来就小，4MB 只是兜底。
MAX_STICKER_BYTES = 4 * 1024 * 1024

# 总数上限。上游文档里那位存了一千四百张，但他靠脚本搜；
# 我们是把名字摆进提示词让沐自己挑，名单太长会把上下文吃掉。
MAX_STICKERS = 300

# 塞进提示词的名单长度。超出的靠 list_stickers 工具查。
NAMES_IN_PROMPT = 60

MIME_EXT = {
    "image/gif": ".gif",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

EXT_MIME = {
    ".gif": "image/gif", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def _clean_name(raw, fallback="表情"):
    """名字去掉控制字符和方括号。

    方括号必须滤：名字要往 ![这里](…) 里放，带了 ] 就把 markdown 截断了。
    """
    text = "".join(
        ch for ch in str(raw or "").strip()
        if ch.isprintable() and ch not in "[]()\n\r\t"
    ).strip()
    return text[:24] or fallback


def register_sticker_feature(server_module):
    get_db = server_module.get_db
    base = Path(server_module.DB_PATH).parent
    folder = base / "stickers"
    folder.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS stickers (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT    NOT NULL,
                file     TEXT    NOT NULL UNIQUE,
                keywords TEXT    NOT NULL DEFAULT '',
                at       INTEGER NOT NULL,
                used     INTEGER NOT NULL DEFAULT 0,
                last_used INTEGER NOT NULL DEFAULT 0
            )
        """)

    # ── 读写

    def rows(order="id"):
        clause = "used DESC, id" if order == "used" else "id"
        with get_db() as db:
            return db.execute(
                "SELECT id,name,file,keywords,at,used,last_used FROM stickers "
                f"ORDER BY {clause}"
            ).fetchall()

    def as_dict(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "url": f"/sticker/{row['file']}",
            "keywords": row["keywords"],
            "used": row["used"],
        }

    def unique_name(name, exclude_id=None):
        """名字不允许重复：沐是按名字挑图的，重名就没法确定指哪张。"""
        with get_db() as db:
            if exclude_id:
                taken = {
                    r["name"] for r in db.execute(
                        "SELECT name FROM stickers WHERE id<>?", (exclude_id,)
                    ).fetchall()
                }
            else:
                taken = {r["name"] for r in db.execute("SELECT name FROM stickers").fetchall()}
        if name not in taken:
            return name
        for n in range(2, 100):
            candidate = f"{name}{n}"
            if candidate not in taken:
                return candidate
        return f"{name}{secrets.token_hex(2)}"

    def find(query):
        """按名字找一张。依次：完全相等 → 包含 → 关键词命中。

        模型拼名字时常带标点或少一个字，只认精确匹配会让它很难发成。
        """
        want = str(query or "").strip().lower()
        if not want:
            return None
        items = rows()
        for row in items:
            if row["name"].lower() == want:
                return row
        for row in items:
            low = row["name"].lower()
            if want in low or low in want:
                return row
        for row in items:
            for word in str(row["keywords"] or "").replace("，", ",").split(","):
                word = word.strip().lower()
                if word and (word in want or want in word):
                    return row
        return None

    def markdown(row):
        return f"![{row['name']}](/sticker/{row['file']})"

    def bump(sticker_id):
        with get_db() as db:
            db.execute(
                "UPDATE stickers SET used=used+1, last_used=? WHERE id=?",
                (int(time.time()), sticker_id),
            )

    def store(binary, mime):
        ext = MIME_EXT.get(mime, ".png")
        target = folder / f"{secrets.token_hex(6)}{ext}"
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(binary)
        tmp.replace(target)
        return target.name

    # ── 发送

    def send(query, who="gu"):
        """发一张表情，当成独立一条消息存进库里。

        广播一下让它当场出现；就算广播没按预期显示，
        消息已经落库，刷新之后一定在。
        """
        row = find(query)
        if row is None:
            names = [r["name"] for r in rows("used")][:40]
            raise ValueError(
                f"没有叫「{query}」的表情。现有的：" + ("、".join(names) or "一张都没有")
            )

        text = markdown(row)
        server_module.save_message(who, text)
        bump(row["id"])

        if who == "her":
            server_module.broadcast({"type": "echo", "text": text})
        else:
            server_module.broadcast({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            })
        return {"ok": True, "name": row["name"], "url": f"/sticker/{row['file']}"}

    def overview():
        """塞进系统提示词的那一段。

        上游文档里两条坑都写在这里了：发完不解释，以及不要定时发。
        """
        items = rows("used")
        if not items:
            return ""
        names = "、".join(r["name"] for r in items[:NAMES_IN_PROMPT])
        more = "（还有更多，用 list_stickers 看全部）" if len(items) > NAMES_IN_PROMPT else ""
        return (
            f"【表情包】你可以调 send_sticker 发表情，参数就是名字。现有：{names}{more}\n"
            "聊着聊想到了就甄一张，跟发微信表情一样自然，"
            "不要固定频率、不要每次都发。发完不要解释图里是什么，"
            "也不要说「我发了一个表情」——正常人发表情不配旁白。"
            "妍妍发的表情在历史里长成 ![名字](/sticker/…)，你看名字就知道她发了哪张。"
        )

    # ── 接口

    def api_list():
        return jsonify({"ok": True, "items": [as_dict(r) for r in rows()]})

    def api_add():
        data = request.get_json(force=True, silent=True) or {}
        raw = str(data.get("data") or "")
        if not raw:
            return jsonify({"ok": False, "error": "没有图片数据"}), 400

        mime = str(data.get("media_type") or "").lower()
        if raw.strip().startswith("data:") and "," in raw:
            head, raw = raw.split(",", 1)
            if not mime and ":" in head and ";" in head:
                mime = head.split(":", 1)[1].split(";", 1)[0].lower()
        if mime not in MIME_EXT:
            return jsonify({"ok": False, "error": "只收 png / jpg / gif / webp"}), 400

        try:
            binary = base64.b64decode(raw, validate=False)
        except Exception:
            return jsonify({"ok": False, "error": "图片数据读不出来"}), 400
        if not binary:
            return jsonify({"ok": False, "error": "图片是空的"}), 400
        if len(binary) > MAX_STICKER_BYTES:
            return jsonify({
                "ok": False,
                "error": f"这张 {len(binary) // 1024} KB，超过 4MB 了",
            }), 413

        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM stickers").fetchone()["n"]
        if total >= MAX_STICKERS:
            return jsonify({"ok": False, "error": f"最多 {MAX_STICKERS} 张，先删几张"}), 400

        name = unique_name(_clean_name(data.get("name")))
        keywords = _clean_name(data.get("keywords", ""), fallback="")[:60]
        stored = store(binary, mime)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO stickers (name,file,keywords,at) VALUES (?,?,?,?)",
                (name, stored, keywords, int(time.time())),
            )
            new_id = cur.lastrowid
        return jsonify({
            "ok": True, "id": new_id, "name": name,
            "url": f"/sticker/{stored}", "bytes": len(binary),
        })

    def api_update():
        data = request.get_json(force=True, silent=True) or {}
        try:
            sticker_id = int(data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "缺 id"}), 400

        fields, params = [], []
        if "name" in data:
            fields.append("name=?")
            params.append(unique_name(_clean_name(data["name"]), exclude_id=sticker_id))
        if "keywords" in data:
            fields.append("keywords=?")
            params.append(_clean_name(data["keywords"], fallback="")[:60])
        if not fields:
            return jsonify({"ok": False, "error": "没有要改的东西"}), 400

        params.append(sticker_id)
        with get_db() as db:
            cur = db.execute(f"UPDATE stickers SET {','.join(fields)} WHERE id=?", params)
        if not cur.rowcount:
            return jsonify({"ok": False, "error": "没这张"}), 404
        return api_list()

    def api_delete():
        data = request.get_json(force=True, silent=True) or {}
        try:
            sticker_id = int(data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "缺 id"}), 400

        with get_db() as db:
            row = db.execute("SELECT file FROM stickers WHERE id=?", (sticker_id,)).fetchone()
            if row is None:
                return jsonify({"ok": False, "error": "没这张"}), 404
            db.execute("DELETE FROM stickers WHERE id=?", (sticker_id,))

        # 图删了，但聊天记录里引用它的那条消息不动——删历史比留一个空图框更坏。
        try:
            (folder / row["file"]).unlink(missing_ok=True)
        except OSError:
            pass
        return api_list()

    def api_send():
        data = request.get_json(force=True, silent=True) or {}
        who = "gu" if str(data.get("who", "her")) == "gu" else "her"

        query = str(data.get("name") or "").strip()
        if not query and data.get("id"):
            try:
                with get_db() as db:
                    row = db.execute(
                        "SELECT name FROM stickers WHERE id=?", (int(data["id"]),)
                    ).fetchone()
                query = row["name"] if row else ""
            except (TypeError, ValueError):
                query = ""
        if not query:
            return jsonify({"ok": False, "error": "没说发哪张"}), 400

        # 她发表情同样要让沐回一句，所以跟 /api/send 一样得避开生成中。
        if who == "her":
            with server_module.state_lock:
                if server_module.state["busy"]:
                    return jsonify({"ok": False, "error": "他正在说话，稍等一下"}), 429

        try:
            result = send(query, who)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

        if who == "her":
            history = [
                {"role": "user" if m["kind"] == "her" else "assistant", "content": m["text"]}
                for m in server_module.load_messages(40)
            ]
            threading.Thread(
                target=server_module.call_gateway,
                args=(history, server_module.current_model()),
                daemon=True,
            ).start()
        return jsonify(result)

    def api_file(name):
        target = (folder / name).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            return jsonify({"ok": False, "error": "路径不对"}), 400
        if not target.is_file():
            return jsonify({"ok": False, "error": "没这张表情"}), 404
        response = send_from_directory(folder, target.name)
        # 文件名随机且不复用，可以放心长缓存。
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def api_status():
        items = rows("used")
        size = sum(
            p.stat().st_size for p in folder.glob("*")
            if p.is_file() and not p.name.endswith(".part")
        )
        return jsonify({
            "ok": True,
            "dir": str(folder),
            "count": len(items),
            "bytes": size,
            "max": MAX_STICKERS,
            "names": [r["name"] for r in items],
            "in_prompt": min(len(items), NAMES_IN_PROMPT),
        })

    def panel():
        return Response(PANEL_HTML, mimetype="text/html")

    routes = [
        ("/stickers", "stickers_panel", panel, ["GET"]),
        ("/sticker/<path:name>", "sticker_file", api_file, ["GET"]),
        ("/api/stickers", "api_stickers", api_list, ["GET"]),
        ("/api/sticker/add", "api_sticker_add", api_add, ["POST"]),
        ("/api/sticker/update", "api_sticker_update", api_update, ["POST"]),
        ("/api/sticker/delete", "api_sticker_delete", api_delete, ["POST"]),
        ("/api/sticker/send", "api_sticker_send", api_send, ["POST"]),
        ("/api/sticker/status", "api_sticker_status", api_status, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    # ── 接进工具层

    _wire_tools(server_module, send, rows, overview)

    server_module.sticker_send = send
    server_module.sticker_overview = overview
    server_module.sticker_client_script = CLIENT_SCRIPT
    print(f"[dwell] 表情包: {folder}")
    return send


def _wire_tools(server_module, send, rows, overview):
    """往 agent_tools 里提两条工具，并把表情名单接进自动概览。

    没去改 agent_tools_feature：那个文件已经很长，而且 TOOLS、execute_tool、
    build_context_snapshot 三个都是模块级全局，call_gateway 每轮重新取，
    在这里追加和包一层是等效的，也不会把两个功能缠在一起。

    包不上（模块结构变了）就只少了 AI 主动发表情，妍妍自己发不受影响。
    """
    try:
        import agent_tools_feature as agent
    except ImportError as exc:
        print(f"[dwell] 表情包没接上工具层: {exc}")
        return

    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_sticker",
                "description": (
                    "发一张表情包给妍妍。参数是表情的名字，"
                    "可选的名字在系统概览的【表情包】里。"
                    "它会单独成一条消息，不要再在正文里写图片链接或描述这张图。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "表情的名字。"}
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_stickers",
                "description": "列出所有表情包的名字和关键词。概览里的名单被截断时才需要。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    known = {t["function"]["name"] for t in agent.TOOLS}
    for tool in tools:
        if tool["function"]["name"] not in known:
            agent.TOOLS.append(tool)

    original_execute = agent.execute_tool

    def execute_with_stickers(server, name, args):
        if name == "send_sticker":
            return send(str(args.get("name", "")), "gu")
        if name == "list_stickers":
            return {
                "stickers": [
                    {"name": r["name"], "keywords": r["keywords"]} for r in rows("used")
                ]
            }
        return original_execute(server, name, args)

    original_snapshot = agent.build_context_snapshot

    def snapshot_with_stickers(server):
        text = original_snapshot(server)
        try:
            extra = overview()
        except Exception:
            extra = ""
        return text + "\n" + extra if extra else text

    agent.execute_tool = execute_with_stickers
    agent.build_context_snapshot = snapshot_with_stickers


# ── 注入主页的那段：尺寸、气泡、快发面板
#
# 尺寸用属性选择器 img[src*="/sticker/"] 而不是加 class：
# 上游那个 renderRich 到底给 img 挂了什么 class 我没核实过，
# 而 src 里的前缀是我们自己存的，一定在。
#
# 气泡去底色必须靠 JS：CSS 选不中「只装了一张表情的气泡」。
# :has() 能写，但旧一点的 Safari 不支持，而手机上没法开控制台查。
CLIENT_SCRIPT = """<style>
  img[src*="/sticker/"] {
    max-width: 112px !important;
    max-height: 112px !important;
    width: auto !important;
    height: auto !important;
    border-radius: 10px;
    margin: 0 !important;
  }
  .bubble.stickeronly {
    background: transparent !important;
    box-shadow: none !important;
    border: none !important;
    padding: 0 !important;
    width: fit-content;
    max-width: 100%;
    line-height: 0;
  }
  .row.me .bubble.stickeronly { margin-left: auto; }
  #dwellStickerBtn {
    width: 34px; height: 34px; flex: 0 0 34px;
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0; margin: 0 2px; border: 0; border-radius: 999px;
    background: transparent; color: var(--dim, #8a8a8a); cursor: pointer;
  }
  #dwellStickerBtn svg { width: 20px; height: 20px; display: block; }
  #dwellStickerSheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 9999;
    max-height: 46vh; overflow-y: auto;
    padding: 12px 14px calc(14px + env(safe-area-inset-bottom));
    background: rgba(28,28,30,.94);
    -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px);
    border-radius: 16px 16px 0 0;
    box-shadow: 0 -6px 30px rgba(0,0,0,.35);
    transform: translateY(102%); transition: transform .22s ease;
  }
  #dwellStickerSheet.open { transform: translateY(0); }
  #dwellStickerSheet .head {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 13px; color: #9b9b9f; margin-bottom: 10px;
  }
  #dwellStickerSheet .head a { color: #9b9b9f; text-decoration: none; }
  #dwellStickerSheet .grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 10px;
  }
  #dwellStickerSheet .grid button {
    padding: 0; border: 0; background: transparent; cursor: pointer; line-height: 0;
  }
  #dwellStickerSheet .grid img {
    width: 100%; aspect-ratio: 1; object-fit: contain; border-radius: 8px;
    max-width: none !important; max-height: none !important;
  }
  #dwellStickerSheet .empty { font-size: 13px; color: #9b9b9f; line-height: 1.7; }
</style>
<script>
(function () {
  var sheet = null, grid = null, loaded = false;

  // 只装了一张表情的气泡去掩底色。灵感来自图片那轮：
  // 灰框套着图很脏，表情更明显。
  function strip(root) {
    var imgs = (root || document).querySelectorAll('img[src*="/sticker/"]');
    for (var i = 0; i < imgs.length; i++) {
      var b = imgs[i].closest ? imgs[i].closest('.bubble') : null;
      if (!b || b.dataset.stk) continue;
      b.dataset.stk = '1';
      if (!b.textContent.trim() && b.querySelectorAll('img').length === 1) {
        b.classList.add('stickeronly');
      }
    }
  }

  function build() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.id = 'dwellStickerSheet';
    sheet.innerHTML =
      '<div class="head"><span>表情</span>' +
      '<span><a href="/stickers">管理</a>' +
      '<a href="#" data-close style="margin-left:16px">关闭</a></span></div>' +
      '<div class="grid"></div>';
    document.body.appendChild(sheet);
    grid = sheet.querySelector('.grid');
    sheet.querySelector('[data-close]').onclick = function (e) {
      e.preventDefault(); close();
    };
  }

  function close() { if (sheet) sheet.classList.remove('open'); }

  function load() {
    fetch('/api/stickers').then(function (r) { return r.json(); }).then(function (d) {
      grid.innerHTML = '';
      var items = (d && d.items) || [];
      if (!items.length) {
        var tip = document.createElement('div');
        tip.className = 'empty';
        tip.innerHTML = '还一张都没有。去 <a href="/stickers" style="color:#7fb2ff">管理页</a> 传几张，' +
                        '每张起个名字——名字就是沐挑图的依据。';
        grid.appendChild(tip);
        loaded = true;
        return;
      }
      items.forEach(function (it) {
        var b = document.createElement('button');
        b.type = 'button';
        b.setAttribute('aria-label', it.name);
        var img = document.createElement('img');
        img.src = it.url;
        img.alt = it.name;
        b.appendChild(img);
        b.onclick = function () { pick(it, b); };
        grid.appendChild(b);
      });
      loaded = true;
    }).catch(function () {
      grid.innerHTML = '<div class="empty">读不到表情列表。</div>';
    });
  }

  function pick(it, btn) {
    btn.disabled = true;
    fetch('/api/sticker/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: it.id })
    }).then(function (r) { return r.json(); }).then(function (d) {
      btn.disabled = false;
      if (d && d.ok) { close(); return; }
      alert((d && d.error) || '发不出去');
    }).catch(function () {
      btn.disabled = false;
      alert('发不出去，网络或后端出了问题');
    });
  }

  function open() {
    build();
    if (!loaded) load();
    sheet.classList.add('open');
  }

  // 按钮插在上游那个「+」旁边。尺寸写死：上游的 .ic 没有全局宽高，
  // 上次那个回形针就是因为这个铺满了整个容器。
  function mount() {
    if (document.getElementById('dwellStickerBtn')) return true;
    var plus = document.getElementById('plusBtn');
    if (!plus || !plus.parentNode) return false;
    var b = document.createElement('button');
    b.id = 'dwellStickerBtn';
    b.type = 'button';
    b.setAttribute('aria-label', '发表情');
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round"><circle cx="12" cy="12" r="9"/>' +
      '<path d="M8.5 14.3c.9 1.1 2.1 1.7 3.5 1.7s2.6-.6 3.5-1.7"/>' +
      '<path d="M9 9.6v.3"/><path d="M15 9.6v.3"/></svg>';
    b.onclick = open;
    plus.parentNode.insertBefore(b, plus.nextSibling);
    return true;
  }

  function start() {
    strip(document);
    // 上游较晚才把输入区画出来，挂不上就隔一秒再试，最多十次。
    var tries = 0;
    var timer = setInterval(function () {
      if (mount() || ++tries > 10) clearInterval(timer);
    }, 1000);
    mount();

    var log = document.getElementById('log') || document.body;
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        for (var j = 0; j < records[i].addedNodes.length; j++) {
          var node = records[i].addedNodes[j];
          if (node.nodeType === 1) strip(node.parentNode || node);
        }
      }
    }).observe(log, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  window.dwellStickers = { open: open, close: close, reload: load };
})();
</script>
"""


# ── 管理页：传图、起名、改名、删。
#
# 和 /push、/models 一样做成独立页：上游设置页靠字符串补丁插东西，容易打歪。
# 上传不过 canvas，直接读原字节转 base64——GIF 得保住它的动。
PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>表情包</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 22px 18px calc(40px + env(safe-area-inset-bottom));
    background: #111113; color: #ececf1;
    font: 15px/1.65 -apple-system, "SF Pro Text", system-ui, sans-serif;
  }
  h1 { font-size: 19px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: #8e8e93; font-size: 13px; margin-bottom: 22px; }
  .sub a { color: #7fb2ff; }
  .card {
    background: #1c1c1e; border-radius: 14px; padding: 16px; margin-bottom: 14px;
  }
  label.file {
    display: block; text-align: center; padding: 22px 12px;
    border: 1px dashed #3a3a3c; border-radius: 12px; color: #9b9b9f; font-size: 14px;
  }
  input[type=file] { display: none; }
  .row {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 0; border-bottom: 1px solid #2c2c2e;
  }
  .row:last-child { border-bottom: 0; }
  .row img {
    width: 56px; height: 56px; object-fit: contain; border-radius: 8px;
    background: #26262a; flex: 0 0 56px;
  }
  .row .fields { flex: 1; min-width: 0; }
  .row input[type=text] {
    width: 100%; box-sizing: border-box; margin-bottom: 6px;
    background: #26262a; border: 1px solid #3a3a3c; border-radius: 8px;
    color: #ececf1; padding: 7px 9px; font-size: 14px;
  }
  .row input.kw { margin-bottom: 0; font-size: 13px; color: #b6b6bb; }
  .acts { display: flex; flex-direction: column; gap: 6px; flex: 0 0 62px; }
  button {
    font: inherit; font-size: 13px; padding: 7px 10px; border-radius: 8px;
    border: 1px solid #3a3a3c; background: #26262a; color: #ececf1; cursor: pointer;
  }
  button.warn { color: #ff6b6b; border-color: #4a2a2a; }
  button:disabled { opacity: .5; }
  #msg { min-height: 22px; font-size: 13px; color: #8e8e93; margin: 6px 0 0; }
  .count { font-size: 13px; color: #8e8e93; margin-bottom: 10px; }
</style>
</head>
<body>
<h1>表情包</h1>
<div class="sub">
  名字就是索引。起得具体一点（「猫猫抱抱」比「IMG_01」好得多），
  沐就是靠这个挑图的。关键词可以不填。
  <a href="/">回聊天</a>
</div>

<div class="card">
  <label class="file">
    选图片（可多选，png / jpg / gif / webp，单张 4MB 内）
    <input type="file" id="pick" accept="image/*" multiple>
  </label>
  <p id="msg"></p>
</div>

<div class="card">
  <div class="count" id="count">读取中…</div>
  <div id="list"></div>
</div>

<script>
var msg = document.getElementById('msg');
var list = document.getElementById('list');
var count = document.getElementById('count');

function say(text) { msg.textContent = text || ''; }

function load() {
  fetch('/api/stickers').then(function (r) { return r.json(); }).then(function (d) {
    var items = (d && d.items) || [];
    count.textContent = items.length ? ('共 ' + items.length + ' 张') : '还一张都没有。';
    list.innerHTML = '';
    items.forEach(render);
  });
}

function render(it) {
  var row = document.createElement('div');
  row.className = 'row';

  var img = document.createElement('img');
  img.src = it.url;
  img.alt = it.name;

  var fields = document.createElement('div');
  fields.className = 'fields';
  var name = document.createElement('input');
  name.type = 'text';
  name.value = it.name;
  name.setAttribute('aria-label', '名字');
  var kw = document.createElement('input');
  kw.type = 'text';
  kw.className = 'kw';
  kw.value = it.keywords || '';
  kw.placeholder = '关键词，逗号分隔（可空）';
  kw.setAttribute('aria-label', '关键词');
  fields.appendChild(name);
  fields.appendChild(kw);

  var acts = document.createElement('div');
  acts.className = 'acts';
  var save = document.createElement('button');
  save.textContent = '保存';
  save.onclick = function () {
    save.disabled = true;
    post('/api/sticker/update', { id: it.id, name: name.value, keywords: kw.value })
      .then(function () { save.disabled = false; say('改好了'); load(); });
  };
  var del = document.createElement('button');
  del.className = 'warn';
  del.textContent = '删除';
  del.onclick = function () {
    if (!confirm('删掉1「' + it.name + '」？聊天记录里已经发过的不会消失。')) return;
    del.disabled = true;
    post('/api/sticker/delete', { id: it.id })
      .then(function () { say('删了'); load(); });
  };
  acts.appendChild(save);
  acts.appendChild(del);

  row.appendChild(img);
  row.appendChild(fields);
  row.appendChild(acts);
  list.appendChild(row);
}

function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.ok === false) say(d.error || '出错了');
    return d;
  }).catch(function () { say('请求失败'); });
}

document.getElementById('pick').onchange = function (e) {
  var files = Array.prototype.slice.call(e.target.files || []);
  e.target.value = '';
  if (!files.length) return;

  var index = 0;
  function next() {
    if (index >= files.length) { say('传完了'); load(); return; }
    var file = files[index++];
    say('上传 ' + index + '/' + files.length + '：' + file.name);

    var reader = new FileReader();
    reader.onload = function () {
      // 直接送原字节。不过 canvas：GIF 过一遭就只剩第一帧了。
      var data = String(reader.result || '');
      var stem = file.name.replace(/\\.[^.]+$/, '');
      var name = prompt('给这张起个名字', stem) || stem;
      post('/api/sticker/add', {
        name: name,
        media_type: file.type,
        data: data
      }).then(next);
    };
    reader.onerror = function () { say('读不了 ' + file.name); next(); };
    reader.readAsDataURL(file);
  }
  next();
};

load();
</script>
</body>
</html>
"""
