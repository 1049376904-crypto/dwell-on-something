"""表情包：双方都能发，而且比图片小一截。

上游没有表情包骨架。它那份文档里的做法是「一堆图 + 一份索引」，
AI 把图片链接写进回话，靠 IMG_RE 渲染成 <img> 就完事——
没有面板、没有独立气泡，也没有尺寸区分。所以这一块是从零搭的。

存储：原字节落盘 data/stickers，数据库只存一行索引。
刻意不压缩、不过 canvas：上游发图那条路走 shrinkImage，
GIF 进去出来就只剩第一帧了。表情包大半是动图，不能走那条路。

消息里存的是 markdown，但路径用 /sticker/ 而不是 /media/：
* 前端靠这个前缀把它画小（见下面 CLIENT_SCRIPT）；
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
import re
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

# 相机和微信导出的文件名当名字等于没名字。这种一律当空，
# 改成 表情1 这种可数的占位，让她事后在列表里改。
JUNK_NAME = re.compile(
    r"^(img|image|photo|pic|wx|mmexport|微信图片|screenshot|截屏|"
    r"iaic|gif|jpeg|jpg|png|webp|未命名|untitled)[\s_\-0-9()]*$",
    re.IGNORECASE,
)

PLACEHOLDER_NAME = re.compile(r"^表情\d+$")


def _clean_name(raw, fallback="表情"):
    """名字去掉控制字符和方括号。

    方括号必须滤：名字要往 ![这里](…) 里放，带了 ] 就把 markdown 截断了。
    """
    text = "".join(
        ch for ch in str(raw or "").strip()
        if ch.isprintable() and ch not in "[]()\n\r\t"
    ).strip()
    return text[:24] or fallback


def _looks_like_junk(name):
    """IMG_0423、微信图片20260810 这类等于没起名。"""
    text = str(name or "").strip()
    if not text:
        return True
    if JUNK_NAME.match(text) or PLACEHOLDER_NAME.match(text):
        return True
    # 纯十六进制串（时间戳、哈希）也算没名字。
    compact = text.replace("-", "").replace("_", "").replace(" ", "")
    return len(compact) >= 6 and all(c in "0123456789abcdefABCDEF" for c in compact)


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
            # 前端据此把没起名的标出来，提醒她改。
            "unnamed": _looks_like_junk(row["name"]),
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
        for n in range(2, 400):
            candidate = f"{name}{n}"
            if candidate not in taken:
                return candidate
        return f"{name}{secrets.token_hex(2)}"

    def auto_name():
        """没给名字时用「表情N」，N 顺着现有的往下排。"""
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM stickers").fetchone()["n"]
        return unique_name(f"表情{total + 1}")

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

    def decode(raw, mime):
        """把 data URL 或裸 base64 解成字节。返回 (bytes, mime, 错误说明)。"""
        raw = str(raw or "")
        if not raw:
            return None, mime, "没有图片数据"
        mime = str(mime or "").lower()
        if raw.strip().startswith("data:") and "," in raw:
            head, raw = raw.split(",", 1)
            if not mime and ":" in head and ";" in head:
                mime = head.split(":", 1)[1].split(";", 1)[0].lower()
        if mime not in MIME_EXT:
            return None, mime, "只收 png / jpg / gif / webp"
        try:
            binary = base64.b64decode(raw, validate=False)
        except Exception:
            return None, mime, "图片数据读不出来"
        if not binary:
            return None, mime, "图片是空的"
        if len(binary) > MAX_STICKER_BYTES:
            return None, mime, f"这张 {len(binary) // 1024} KB，超过 4MB 了"
        return binary, mime, None

    def insert(binary, mime, name, keywords=""):
        chosen = auto_name() if _looks_like_junk(name) else unique_name(_clean_name(name))
        stored = store(binary, mime)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO stickers (name,file,keywords,at) VALUES (?,?,?,?)",
                (chosen, stored, _clean_name(keywords, fallback="")[:60], int(time.time())),
            )
            new_id = cur.lastrowid
        return {
            "id": new_id, "name": chosen,
            "url": f"/sticker/{stored}", "bytes": len(binary),
        }

    def room_left():
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM stickers").fetchone()["n"]
        return MAX_STICKERS - total

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
        名字像「表情7」的不报给它——那种名字它没法判断该不该发。
        """
        items = [r for r in rows("used") if not _looks_like_junk(r["name"])]
        if not items:
            return ""
        names = "、".join(r["name"] for r in items[:NAMES_IN_PROMPT])
        more = "（还有更多，用 list_stickers 看全部）" if len(items) > NAMES_IN_PROMPT else ""
        return (
            f"【表情包】你可以调 send_sticker 发表情，参数就是名字。现有：{names}{more}\n"
            "聊着聊着想到了就甩一张，跟发微信表情一样自然，"
            "不要固定频率、不要每次都发。发完不要解释图里是什么，"
            "也不要说「我发了一个表情」——正常人发表情不配旁白。"
            "妍妍发的表情在历史里长成 ![名字](/sticker/…)，你看名字就知道她发了哪张。"
        )

    # ── 接口

    def api_list():
        return jsonify({
            "ok": True,
            "items": [as_dict(r) for r in rows()],
            "room": room_left(),
        })

    def api_add():
        """加表情。单张传 data，多张传 items:[{data,media_type,name}]。

        批量走一个请求：一次一个来回、逐张弹框问名字，
        传二十张就要点二十次，实测很折磨。
        """
        data = request.get_json(force=True, silent=True) or {}
        batch = data.get("items")

        if not isinstance(batch, list):
            batch = [{
                "data": data.get("data"),
                "media_type": data.get("media_type"),
                "name": data.get("name"),
                "keywords": data.get("keywords", ""),
            }]

        added, failed = [], []
        for item in batch:
            if not isinstance(item, dict):
                continue
            if room_left() <= 0:
                failed.append({"name": item.get("name") or "", "error": f"最多 {MAX_STICKERS} 张"})
                continue
            binary, mime, error = decode(item.get("data"), item.get("media_type"))
            if error:
                failed.append({"name": item.get("name") or "", "error": error})
                continue
            added.append(insert(binary, mime, item.get("name"), item.get("keywords", "")))

        if not added and failed:
            return jsonify({"ok": False, "error": failed[0]["error"], "failed": failed}), 400
        return jsonify({
            "ok": True, "added": added, "failed": failed,
            "count": len(added), "room": room_left(),
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
        named = [r["name"] for r in items if not _looks_like_junk(r["name"])]
        return jsonify({
            "ok": True,
            "dir": str(folder),
            "count": len(items),
            "bytes": size,
            "max": MAX_STICKERS,
            "names": [r["name"] for r in items],
            # 没起名的不报给模型，这里如实说有几张还等着改名。
            "named": len(named),
            "unnamed": len(items) - len(named),
            "in_prompt": min(len(named), NAMES_IN_PROMPT),
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
# 尺寸用属性选择器 img[src*="/sticker/"]，不加 class：
# 上游 renderRich 到底给 img 挂了什么 class 我没核实过，
# 而 src 里的前缀是我们自己存的，一定在。
#
# 两条颜色原则（上一版没做到，面板整片深色加蓝，跟这套暖白的皮完全脱节）：
# 一是主题色一律从页面上读，不写死；二是不用蓝色，链接和图标都走前景色。
# 读的办法是往上找第一个不透明的背景色，body 上可能是透明的。
#
# 输入区那个按钮不再自己写样式，直接克隆上游的「+」再换掉里面的图标：
# 底色、圆角、大小、颜色全部自动跟它一致，换主题也不会脱节。
CLIENT_SCRIPT = """<style>
  img[src*="/sticker/"] {
    max-width: 112px;
    max-height: 112px;
    width: auto;
    height: auto;
    border-radius: 10px;
    margin: 0;
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

  /* 点空白处也能关掉。 */
  #dwellStickerBackdrop {
    position: fixed; inset: 0; z-index: 9998;
    background: rgba(0,0,0,.28);
    opacity: 0; pointer-events: none; transition: opacity .2s ease;
  }
  #dwellStickerBackdrop.open { opacity: 1; pointer-events: auto; }

  #dwellStickerSheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 9999;
    display: flex; flex-direction: column;
    max-height: 52vh;
    /* 这两个值由 JS 从页面上读出来写进来。 */
    background: var(--dws-bg, #f5f2ec);
    color: var(--dws-fg, #26241f);
    border-radius: 18px 18px 0 0;
    box-shadow: 0 -8px 36px rgba(0,0,0,.18);
    transform: translateY(102%); transition: transform .22s ease;
  }
  #dwellStickerSheet.open { transform: translateY(0); }

  /* 头部固定，不跟着列表滚走——关闭键必须一直看得见。 */
  #dwellStickerSheet .head {
    position: relative;
    flex: 0 0 auto;
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 8px 6px 18px;
  }
  #dwellStickerSheet .head::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: 0;
    height: 1px; background: currentColor; opacity: .1;
  }
  #dwellStickerSheet .head .t { font-size: 16px; font-weight: 600; }
  #dwellStickerSheet .head .r { display: flex; align-items: center; gap: 2px; }
  #dwellStickerSheet .head button,
  #dwellStickerSheet .head a {
    font: inherit; font-size: 15px;
    min-width: 44px; min-height: 44px;
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0 10px; border: 0; border-radius: 12px;
    background: transparent; color: inherit; text-decoration: none; cursor: pointer;
  }
  #dwellStickerSheet .head a { opacity: .55; }
  #dwellStickerSheet .head .x { font-size: 21px; line-height: 1; }

  #dwellStickerSheet .body {
    flex: 1 1 auto; overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: 14px 16px calc(18px + env(safe-area-inset-bottom));
  }

  /* 固定像素的格子。上一版用 grid + 百分比 + aspect-ratio，
     列宽没生效时图片就没了上限。这版每一格宽高都写死。 */
  #dwellStickerSheet .grid { display: flex; flex-wrap: wrap; gap: 14px; }
  #dwellStickerSheet .grid .cell {
    width: 76px; flex: 0 0 76px;
    padding: 0; border: 0; background: transparent; cursor: pointer;
    display: flex; flex-direction: column; align-items: center; gap: 5px;
    color: inherit;
  }
  #dwellStickerSheet .grid .cell img {
    width: 72px !important; height: 72px !important;
    max-width: 72px !important; max-height: 72px !important;
    object-fit: contain; border-radius: 12px;
    background: rgba(128,128,128,.12);
    display: block;
  }
  #dwellStickerSheet .grid .cell span {
    font-size: 11.5px; line-height: 1.3; opacity: .6;
    max-width: 76px; text-align: center;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  #dwellStickerSheet .grid .cell:disabled { opacity: .45; }
  #dwellStickerSheet .empty { font-size: 13.5px; opacity: .6; line-height: 1.7; }
  #dwellStickerSheet .empty a { color: inherit; text-decoration: underline; }
</style>
<script>
(function () {
  var sheet = null, backdrop = null, grid = null;

  var FACE =
    '<circle cx="12" cy="12" r="9"/>' +
    '<path d="M8.8 14.2c.8.9 1.9 1.4 3.2 1.4s2.4-.5 3.2-1.4"/>' +
    '<circle cx="9.3" cy="10" r=".95" fill="currentColor" stroke="none"/>' +
    '<circle cx="14.7" cy="10" r=".95" fill="currentColor" stroke="none"/>';

  // 往上找第一个不透明的背景色。body 上经常是 transparent。
  function solidBg(el) {
    for (var n = el; n && n !== document.documentElement; n = n.parentElement) {
      var c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\\(\\s*0\\s*,\\s*0\\s*,\\s*0\\s*,\\s*0\\s*\\)/.test(c)) {
        return c;
      }
    }
    var h = getComputedStyle(document.documentElement).backgroundColor;
    return h || '#f5f2ec';
  }

  // 只装了一张表情的气泡去掉底色。灰框套着图很脏，表情更明显。
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

    backdrop = document.createElement('div');
    backdrop.id = 'dwellStickerBackdrop';
    backdrop.onclick = close;
    document.body.appendChild(backdrop);

    sheet = document.createElement('div');
    sheet.id = 'dwellStickerSheet';
    sheet.innerHTML =
      '<div class="head">' +
      '<span class="t">表情</span>' +
      '<span class="r"><a href="/stickers">管理</a>' +
      '<button type="button" class="x" data-close aria-label="关闭">\u00d7</button></span>' +
      '</div>' +
      '<div class="body"><div class="grid"></div></div>';
    document.body.appendChild(sheet);

    // 跟着页面主题走，不写死配色。
    var log = document.getElementById('log') || document.body;
    sheet.style.setProperty('--dws-bg', solidBg(log));
    sheet.style.setProperty('--dws-fg', getComputedStyle(document.body).color);

    grid = sheet.querySelector('.grid');
    sheet.querySelector('[data-close]').onclick = close;
  }

  function close() {
    if (sheet) sheet.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
  }

  function load() {
    fetch('/api/stickers').then(function (r) { return r.json(); }).then(function (d) {
      grid.innerHTML = '';
      var items = (d && d.items) || [];
      if (!items.length) {
        var tip = document.createElement('div');
        tip.className = 'empty';
        tip.innerHTML = '还一张都没有。去 <a href="/stickers">管理页</a> 传几张。';
        grid.appendChild(tip);
        return;
      }
      items.forEach(function (it) {
        var cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'cell';
        cell.setAttribute('aria-label', '发送 ' + it.name);

        var img = document.createElement('img');
        img.src = it.url;
        img.alt = '';
        cell.appendChild(img);

        // 名字写在图下面：一屏十几张缩略图，光看图分不清哪个是哪个。
        var label = document.createElement('span');
        label.textContent = it.name;
        cell.appendChild(label);

        cell.onclick = function () { pick(it, cell); };
        grid.appendChild(cell);
      });
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
    // 每次打开都重新拉：在管理页加完表情回来，不该还是旧的那几张。
    load();
    sheet.classList.add('open');
    backdrop.classList.add('open');
  }

  // 克隆上游的「+」按钮，只换里面的图标。
  // 上一版自己写样式，结果少了那层底色，一个光秃秃的笑脸挂在圆底按钮旁边很突兀。
  // 克隆之后 class 完全一致，底色圆角大小配色全部自动跟上，换主题也不会脱节。
  // cloneNode 不会复制 addEventListener 和 onclick 属性挂的处理器，
  // 但内联 onclick="" 会跟着复制，所以显式清掉。
  function mount() {
    if (document.getElementById('dwellStickerBtn')) return true;
    var plus = document.getElementById('plusBtn');
    if (!plus || !plus.parentNode) return false;

    // 先量原按钮里的图标，克隆出来的还没进文档，量不到。
    var size = 20, stroke = 1.6;
    var osvg = plus.querySelector('svg');
    if (osvg) {
      var box = osvg.getBoundingClientRect();
      if (box.width) size = Math.round(box.width);
      var sw = parseFloat(getComputedStyle(osvg).strokeWidth);
      if (sw) stroke = sw;
    }

    var b = plus.cloneNode(true);
    b.id = 'dwellStickerBtn';
    b.removeAttribute('onclick');
    b.setAttribute('aria-label', '发表情');
    b.title = '发表情';

    var svg =
      '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" ' +
      'fill="none" stroke="currentColor" stroke-width="' + stroke + '" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      FACE + '</svg>';

    // 上游图标可能包在 <span class="ic" data-i="plus"> 里，也可能是裸 svg。
    // 找得到那层就替换它的内容，顺手把 data-i 改掉，免得重绘时又变回加号。
    var holder = b.querySelector('.ic') || b.querySelector('svg');
    if (holder && holder.tagName.toLowerCase() === 'svg') {
      holder.innerHTML = FACE;
      holder.setAttribute('viewBox', '0 0 24 24');
    } else if (holder) {
      holder.setAttribute('data-i', 'smile');
      holder.innerHTML = svg;
    } else {
      b.innerHTML = svg;
    }

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


# ── 管理页：传图、改名、删。
#
# 和 /push、/models 一样做成独立页：上游设置页靠字符串补丁插东西，容易打歪。
# 上传不过 canvas，直接读原字节转 base64——GIF 得保住它的动。
#
# 配色跟着主应用那套暖白走（上一版是 #111113 加蓝，完全两码事）。
# 这是独立文档，读不到主应用的主题变量，所以写死一套暖色，
# 另外加一段 prefers-color-scheme: dark，系统切深色时自动跟着变。
PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>表情包</title>
<style>
  :root {
    --bg:     #f4f1ea;
    --card:   #fffdf8;
    --fg:     #26241f;
    --dim:    #8b867b;
    --line:   #e5e0d5;
    --field:  #faf8f3;
    --accent: #c2603f;
    --todo:   #f0e6d4;
    --todobg: #fdf8ee;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #161514; --card: #1f1e1c; --fg: #ece9e3; --dim: #8e8a82;
      --line: #302e2b; --field: #262523; --accent: #d97a58;
      --todo: #5a4a2c; --todobg: #2a2620;
    }
  }
  * { -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 22px 16px calc(40px + env(safe-area-inset-bottom));
    background: var(--bg); color: var(--fg);
    font: 15px/1.6 -apple-system, "SF Pro Text", system-ui, sans-serif;
  }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 18px; }
  .sub a { color: var(--accent); }
  .card {
    background: var(--card); border-radius: 16px; padding: 14px; margin-bottom: 14px;
    border: 1px solid var(--line);
  }
  label.file {
    display: block; text-align: center; padding: 22px 12px;
    border: 1px dashed var(--line); border-radius: 12px;
    color: var(--dim); font-size: 14px;
  }
  input[type=file] { display: none; }

  .item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 0; border-bottom: 1px solid var(--line);
  }
  .item:last-child { border-bottom: 0; }
  /* 尺寸写死，不用百分比：布局失效时也不会撑开。 */
  .item img {
    width: 60px !important; height: 60px !important;
    max-width: 60px !important; max-height: 60px !important;
    object-fit: contain; border-radius: 10px;
    background: rgba(128,128,128,.12); flex: 0 0 60px;
  }
  .item .fields { flex: 1 1 auto; min-width: 0; }
  .item input[type=text] {
    width: 100%; box-sizing: border-box;
    background: var(--field); border: 1px solid var(--line); border-radius: 10px;
    color: var(--fg); padding: 8px 10px; font-size: 15px;
  }
  .item input.todo { border-color: var(--todo); background: var(--todobg); }
  .item input.kw { margin-top: 6px; font-size: 13px; color: var(--dim); padding: 6px 10px; }
  .item .del {
    flex: 0 0 auto; min-width: 44px; min-height: 44px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 20px; color: var(--dim);
    border: 0; background: transparent; cursor: pointer;
  }
  #msg { min-height: 20px; font-size: 13px; color: var(--dim); margin: 8px 0 0; }
  #msg.warn { color: var(--accent); }
  .count { font-size: 13px; color: var(--dim); margin-bottom: 6px; }
  .hint { font-size: 12px; color: var(--dim); opacity: .8; margin: 0 0 10px; }
</style>
</head>
<body>
<h1>表情包</h1>
<div class="sub">
  名字就是沐挑图的依据，起得具体一点（「猫猫抱抱」比「表情3」有用得多）。
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
  <p class="hint">改完名字点一下别处就存了。浅色框的还没起名，沐看不到这些。</p>
  <div id="list"></div>
</div>

<script>
var msg = document.getElementById('msg');
var list = document.getElementById('list');
var count = document.getElementById('count');

function say(text, warn) {
  msg.textContent = text || '';
  msg.className = warn ? 'warn' : '';
}

function load() {
  fetch('/api/stickers').then(function (r) { return r.json(); }).then(function (d) {
    var items = (d && d.items) || [];
    var todo = items.filter(function (x) { return x.unnamed; }).length;
    count.textContent = items.length
      ? ('共 ' + items.length + ' 张' + (todo ? '，' + todo + ' 张还没起名' : ''))
      : '还一张都没有。';
    list.innerHTML = '';
    items.forEach(render);
  }).catch(function () { count.textContent = '读不到列表。'; });
}

function render(it) {
  var row = document.createElement('div');
  row.className = 'item';

  var img = document.createElement('img');
  img.src = it.url;
  img.alt = '';

  var fields = document.createElement('div');
  fields.className = 'fields';

  var name = document.createElement('input');
  name.type = 'text';
  name.value = it.name;
  name.placeholder = '起个名字';
  name.className = it.unnamed ? 'todo' : '';
  name.setAttribute('aria-label', '名字');

  var kw = document.createElement('input');
  kw.type = 'text';
  kw.className = 'kw';
  kw.value = it.keywords || '';
  kw.placeholder = '关键词，逗号分隔（可空）';
  kw.setAttribute('aria-label', '关键词');

  // 失焦即存。加个「保存」按钮只是多一次点击。
  function save() {
    if (name.value === it.name && kw.value === (it.keywords || '')) return;
    post('/api/sticker/update', { id: it.id, name: name.value, keywords: kw.value })
      .then(function (d) {
        if (d && d.ok) {
          it.name = name.value;
          it.keywords = kw.value;
          name.className = '';
          say('存好了');
          load();
        }
      });
  }
  name.onblur = save;
  kw.onblur = save;
  name.onkeydown = function (e) { if (e.key === 'Enter') name.blur(); };
  kw.onkeydown = function (e) { if (e.key === 'Enter') kw.blur(); };

  fields.appendChild(name);
  fields.appendChild(kw);

  var del = document.createElement('button');
  del.className = 'del';
  del.type = 'button';
  del.textContent = '\u00d7';
  del.setAttribute('aria-label', '删除 ' + it.name);
  del.onclick = function () {
    if (!confirm('删掉「' + it.name + '」？聊天记录里已经发过的不会消失。')) return;
    del.disabled = true;
    post('/api/sticker/delete', { id: it.id }).then(function () { say('删了'); load(); });
  };

  row.appendChild(img);
  row.appendChild(fields);
  row.appendChild(del);
  list.appendChild(row);
}

function post(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.ok === false) say(d.error || '出错了', true);
    return d;
  }).catch(function () { say('请求失败', true); });
}

function readOne(file) {
  return new Promise(function (resolve) {
    var reader = new FileReader();
    reader.onload = function () {
      // 直接送原字节。不过 canvas：GIF 过一遭就只剩第一帧了。
      resolve({
        data: String(reader.result || ''),
        media_type: file.type,
        name: file.name.replace(/\\.[^.]+$/, '')
      });
    };
    reader.onerror = function () { resolve(null); };
    reader.readAsDataURL(file);
  });
}

document.getElementById('pick').onchange = function (e) {
  var files = Array.prototype.slice.call(e.target.files || []);
  e.target.value = '';
  if (!files.length) return;

  say('读取 ' + files.length + ' 张…');

  // 一次读完一次传完。名字先用文件名，认不出的后端会给「表情N」，
  // 然后在下面列表里改——比每张弹一次输入框省事得多。
  Promise.all(files.map(readOne)).then(function (all) {
    var items = all.filter(function (x) { return x; });
    if (!items.length) { say('一张都没读出来', true); return; }

    say('上传中…');
    post('/api/sticker/add', { items: items }).then(function (d) {
      if (!d) return;
      var bad = (d.failed || []).length;
      say('传好 ' + (d.count || 0) + ' 张' + (bad ? '，' + bad + ' 张没成' : '') +
          '。下面改名字。', bad > 0);
      load();
    });
  });
};

load();
</script>
</body>
</html>
"""
