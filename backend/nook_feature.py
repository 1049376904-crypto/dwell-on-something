"""共读：一起读同一本书。

## 上游给了什么

整套阅读器都是现成的（web/index.html）：书架卡片、章节目录、
按纸书调过的正文排版、选中文字后浮出来的「划线 / 写一句」、
批注楼层、进度同步。后端一行前端都不用写。

契约是从那份 index.html 里逐个核出来的：

    GET  api/nook/books                    → [{slug, title, chapters:[名字…]}]
    GET  api/nook/progress                 → {slug: {ch, page, mode}}
    POST api/nook/progress                 ← {slug, ch, page, mode}
    GET  api/nook/chapter/<slug>/<idx>     → {index, total, title, book,
                                              chapters:[…], text}
    GET  api/nook/annotations/<slug>/<idx> → [{id, anchor, note, who, ts,
                                               replies:[{who, text, ts}]}]
    POST api/nook/annotations/<slug>/<idx> ← {anchor, note, who}
    POST api/nook/annotations/<slug>/<idx>/<id>/reply ← {text, who}

几处细节，写错了界面会静默出错：

* `who` 等于 `'ai'` 时显示在沐那一侧，其它值都算她的。前端自己发的是 `'user'`。
* `ts` 前端直接往界面上贴，所以后端要给拼好的字符串，不是时间戳。
* `anchor` 是引文本身，前端拿它在段落里 `indexOf` 找回位置。
  所以**不能存字符偏移**——正文改一个字全部错位。找不到就静默跳过那条。
* `chapter` 返回里必须有 `index`（数字），前端用 `typeof d.index !== 'number'`
  判断这一节是不是读不出来。

## slug 和书名是两件事

书名可以随时改，slug 一律不动。

原因：slug 进了文件路径（data/books/<slug>/）、进了 nook_progress 和
nook_annos 的每一行、也进了前端的 URL。跟着改名一起变的话，
已有的划线和进度立刻变成孤儿——而且是静默的，界面上只会显示
「这本书没有划线」，查都不知道从哪查。

所以 meta.json 里的 title 是「显示的名字」，slug 是「身份」。
改名只动前者。

## 端点重名

`/api/nook/books` 在 server.py 里有个 stub，endpoint 名就叫 `api_nook`。
直接 add_url_rule 会在 import 期抛 AssertionError，整个后端起不来、
全站 502——`/api/music` 已经这么坑过一次。所以凡是上游已有的路径，
一律走 `view_functions[名字] = 新函数` 替换。

## 书从哪来

妍妍没有电脑，所以必须能在手机上传书：`/books` 面板选一个 txt/md，
读成文本 POST 上来，后端切章存盘。

切章顺序：markdown 标题 → 「第N章/节/回」→ 都没有就按字数切。
最后这条兜底很重要：很多 txt 是一整坨，没有兜底就成了一章三十万字。

正文存文件（data/books/<slug>/），批注和进度进库。
这样备份快照不会被书稿撑大，而书本身也能手动放进去。

## AI 那边

上游文档里最要紧的一条：**不要每一条都回**。她划线多半只是「这句好」，
不是在问你。一个每条都回的共读，读两章就会被关掉。
这句话写进了工具描述和上下文概览里。

「她划了几处还没回过」接进 build_context_snapshot，搭在每轮已有的
调用上，不额外花钱——跟日报那行「上一期多久了」同一个做法。
"""

import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, jsonify, request


CN = timezone(timedelta(hours=8))

# 一本书最多多少字。中文小说三十万字很常见，留足余量。
MAX_BOOK_CHARS = 2000000

# 切章的目标长度（没有章节标记时按这个切）。
CHUNK_CHARS = 3500

# 章节数上限。防止一本奇怪的书切出几千个文件。
MAX_CHAPTERS = 600

# 引文最长存多少。前端要用它在段落里 indexOf，太长反而找不回来。
MAX_ANCHOR = 200

MAX_NOTE = 2000

# 书名和章节名的长度上限。
MAX_TITLE = 80
MAX_CH_TITLE = 60

# 给模型看的正文长度上限。一章几千字，截断了它也能读出味道。
MAX_TEXT_FOR_AI = 6000

# markdown 标题，或者「第一章」「第 12 节」「第三回」这类。
HEADING = re.compile(r"^#{1,3}\s+(.+)$")
CHAPTER_LINE = re.compile(
    r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千万]+\s*[章节回卷篇]"
    r"|[Cc]hapter\s+\d+)\s*[:：、.]?\s*(.*)$"
)

# 文件名里不能出现的东西。slug 会进路径，也会进 URL。
UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def cn_now():
    return datetime.now(CN)


def stamp(ts):
    """前端直接把 ts 贴到界面上，所以这里给拼好的字符串。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), CN).strftime("%m-%d %H:%M")


def clean_title(raw, fallback=""):
    """收拾用户给的名字。控制字符和路径分隔符一律去掉。"""
    text = UNSAFE.sub("", str(raw or ""))
    text = " ".join(text.split()).strip().strip(".")
    return text[:MAX_TITLE] or fallback


def make_slug(title, taken):
    """从书名派生一个能进路径也能进 URL 的 slug。

    中文照用（前端会 encodeURIComponent，文件系统也认），
    只把危险字符去掉。撞了就加后缀。

    注意：slug 定下来就不再改了，改书名不动它。
    """
    base = clean_title(title)[:40] or ("book-" + secrets.token_hex(3))
    slug = base
    n = 2
    while slug in taken:
        slug = base + "-" + str(n)
        n += 1
        if n > 200:
            return base + "-" + secrets.token_hex(3)
    return slug


def split_chapters(text):
    """把整本书切成 [(标题, 正文)]。

    三档依次尝试：markdown 标题、「第N章」这类行、按字数硬切。
    最后那条兜底不能省——很多 txt 是一整坨，没有兜底就成了一章三十万字，
    前端一次渲染完直接卡死。
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    marks = []          # [(行号, 标题)]
    for index, line in enumerate(lines):
        match = HEADING.match(line.strip())
        if match:
            marks.append((index, match.group(1).strip()[:MAX_CH_TITLE]))
            continue
        match = CHAPTER_LINE.match(line)
        if match and len(line.strip()) <= 40:
            marks.append((index, line.strip()[:MAX_CH_TITLE]))

    chapters = []
    if len(marks) >= 2:
        for order, (start, title) in enumerate(marks):
            end = marks[order + 1][0] if order + 1 < len(marks) else len(lines)
            body = "\n".join(lines[start + 1:end]).strip()
            if not body:
                continue
            chapters.append((title or ("第 " + str(len(chapters) + 1) + " 节"), body))
            if len(chapters) >= MAX_CHAPTERS:
                break
        if chapters:
            return chapters

    # 兜底：按段落攒到大约 CHUNK_CHARS 就切一节。
    buffer, size = [], 0
    for line in lines:
        buffer.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS and line.strip() == "":
            body = "\n".join(buffer).strip()
            if body:
                chapters.append(("第 " + str(len(chapters) + 1) + " 节", body))
            buffer, size = [], 0
            if len(chapters) >= MAX_CHAPTERS:
                break
    body = "\n".join(buffer).strip()
    if body and len(chapters) < MAX_CHAPTERS:
        chapters.append(("第 " + str(len(chapters) + 1) + " 节", body))
    return chapters or [("全文", str(text or "").strip())]


def _mount(server_module, rule, endpoint, view, methods):
    """注册一条接口，端点已存在就替换而不是新增。

    上游 server.py 里有一堆 stub，endpoint 名和我们要用的一样
    （这里撞的是 /api/nook/books 的 api_nook）。直接 add_url_rule
    会在 import 期抛 AssertionError，后端起不来、全站 502。
    """
    if endpoint in server_module.app.view_functions:
        server_module.app.view_functions[endpoint] = view
        return
    server_module.app.add_url_rule(
        rule, endpoint=endpoint, view_func=view, methods=methods
    )


def register_nook_feature(server_module):
    get_db = server_module.get_db
    root = Path(server_module.DB_PATH).parent / "books"
    root.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS nook_annos (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                slug   TEXT    NOT NULL,
                ch     INTEGER NOT NULL,
                anchor TEXT    NOT NULL,
                note   TEXT    NOT NULL DEFAULT '',
                who    TEXT    NOT NULL DEFAULT 'user',
                at     INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nook_replies (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                anno_id INTEGER NOT NULL,
                who     TEXT    NOT NULL DEFAULT 'user',
                text    TEXT    NOT NULL,
                at      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nook_progress (
                slug TEXT PRIMARY KEY,
                ch   INTEGER NOT NULL DEFAULT 0,
                page INTEGER NOT NULL DEFAULT 0,
                mode INTEGER NOT NULL DEFAULT 2,
                at   INTEGER NOT NULL
            );
        """)

    # ── 书

    def book_dir(slug):
        target = (root / str(slug)).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return None            # 有人往 slug 里塞 ../
        return target

    def read_meta(slug):
        folder = book_dir(slug)
        if folder is None or not folder.is_dir():
            return None
        try:
            data = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("title", slug)
        data.setdefault("chapters", [])
        return data

    def write_meta(slug, meta):
        folder = book_dir(slug)
        if folder is None or not folder.is_dir():
            return False
        tmp = folder / "meta.json.part"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / "meta.json")
        return True

    def all_books():
        found = []
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            if not folder.is_dir():
                continue
            meta = read_meta(folder.name)
            if meta is None:
                continue
            found.append({
                "slug": folder.name,
                "title": meta.get("title") or folder.name,
                # 上游读的是章节名数组，用来在书架上显示「读到 X」。
                "chapters": [c.get("title", "") for c in meta.get("chapters", [])],
                "added": meta.get("added", 0),
            })
        return found

    def chapter_text(slug, index):
        meta = read_meta(slug)
        if meta is None:
            return None, None, 0
        chapters = meta.get("chapters") or []
        if not chapters:
            return None, None, 0
        index = max(0, min(int(index), len(chapters) - 1))
        entry = chapters[index]
        folder = book_dir(slug)
        try:
            body = (folder / str(entry.get("file"))).read_text(encoding="utf-8")
        except OSError:
            body = ""
        return entry.get("title") or ("第 " + str(index + 1) + " 节"), body, len(chapters)

    def save_book(title, text):
        """切章存盘，返回 (slug, 章节数)。"""
        text = str(text or "")
        if len(text) > MAX_BOOK_CHARS:
            text = text[:MAX_BOOK_CHARS]
        chapters = split_chapters(text)

        taken = {b["slug"] for b in all_books()}
        slug = make_slug(title, taken)
        folder = root / slug
        folder.mkdir(parents=True, exist_ok=True)

        entries = []
        for order, (name, body) in enumerate(chapters, 1):
            filename = "%03d.md" % order
            (folder / filename).write_text(body, encoding="utf-8")
            entries.append({"file": filename, "title": name})

        (folder / "meta.json").write_text(
            json.dumps(
                {
                    "title": clean_title(title, slug),
                    "chapters": entries,
                    "added": int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return slug, len(entries)

    def rename_book(slug, title):
        """改书名。只动 meta.json 里的 title，slug 不动。

        slug 在文件路径、nook_progress、nook_annos 和前端 URL 里都用着，
        跟着改名一起变会让已有的划线和进度变成孤儿，而且是静默的。
        """
        meta = read_meta(slug)
        if meta is None:
            return False, "没这本书"
        name = clean_title(title)
        if not name:
            return False, "书名不能空"
        meta["title"] = name
        if not write_meta(slug, meta):
            return False, "写不进去"
        return True, name

    def rename_chapter(slug, index, title):
        """改某一节的名字。切章猜出来的名字有时候很难看。"""
        meta = read_meta(slug)
        if meta is None:
            return False, "没这本书"
        chapters = meta.get("chapters") or []
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False, "第几节要是数字"
        if not 0 <= index < len(chapters):
            return False, "没有第 " + str(index) + " 节"
        name = clean_title(title)[:MAX_CH_TITLE]
        if not name:
            return False, "名字不能空"
        chapters[index]["title"] = name
        meta["chapters"] = chapters
        if not write_meta(slug, meta):
            return False, "写不进去"
        return True, name

    def drop_book(slug):
        folder = book_dir(slug)
        if folder is None or not folder.is_dir():
            return False
        for path in sorted(folder.iterdir(), reverse=True):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass
        # 划线跟着书一起走：书没了，批注挂在空处没有意义。
        with get_db() as db:
            rows = db.execute("SELECT id FROM nook_annos WHERE slug=?", (slug,)).fetchall()
            for row in rows:
                db.execute("DELETE FROM nook_replies WHERE anno_id=?", (row["id"],))
            db.execute("DELETE FROM nook_annos WHERE slug=?", (slug,))
            db.execute("DELETE FROM nook_progress WHERE slug=?", (slug,))
        return True

    # ── 批注

    def annos_of(slug, ch):
        with get_db() as db:
            rows = db.execute(
                "SELECT id,anchor,note,who,at FROM nook_annos "
                "WHERE slug=? AND ch=? ORDER BY id",
                (slug, int(ch)),
            ).fetchall()
            out = []
            for row in rows:
                replies = db.execute(
                    "SELECT who,text,at FROM nook_replies WHERE anno_id=? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                out.append({
                    "id": row["id"],
                    "anchor": row["anchor"],
                    "note": row["note"],
                    "who": row["who"],
                    "ts": stamp(row["at"]),
                    "replies": [
                        {"who": r["who"], "text": r["text"], "ts": stamp(r["at"])}
                        for r in replies
                    ],
                })
        return out

    def add_anno(slug, ch, anchor, note, who):
        anchor = " ".join(str(anchor or "").split())[:MAX_ANCHOR]
        if not anchor:
            return None
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO nook_annos (slug,ch,anchor,note,who,at) VALUES (?,?,?,?,?,?)",
                (
                    slug, int(ch), anchor, str(note or "")[:MAX_NOTE],
                    "ai" if who == "ai" else "user", int(time.time()),
                ),
            )
            return cur.lastrowid

    def add_reply(anno_id, text, who):
        text = str(text or "").strip()[:MAX_NOTE]
        if not text:
            return False
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM nook_annos WHERE id=?", (int(anno_id),)
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "INSERT INTO nook_replies (anno_id,who,text,at) VALUES (?,?,?,?)",
                (int(anno_id), "ai" if who == "ai" else "user", text, int(time.time())),
            )
        return True

    def her_open_marks(limit=20):
        """她划了但沐还没回过的地方。

        判断「没回过」= 这条批注下面没有任何 who='ai' 的楼层。
        这是概览和工具都要用的核心查询。
        """
        with get_db() as db:
            rows = db.execute(
                "SELECT a.id, a.slug, a.ch, a.anchor, a.note, a.at "
                "FROM nook_annos a "
                "WHERE a.who<>'ai' AND NOT EXISTS ("
                "  SELECT 1 FROM nook_replies r WHERE r.anno_id=a.id AND r.who='ai'"
                ") ORDER BY a.id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def progress_map():
        with get_db() as db:
            rows = db.execute("SELECT slug,ch,page,mode FROM nook_progress").fetchall()
        return {
            row["slug"]: {"ch": row["ch"], "page": row["page"], "mode": row["mode"]}
            for row in rows
        }

    def save_progress(slug, ch, page, mode):
        with get_db() as db:
            db.execute(
                "INSERT INTO nook_progress (slug,ch,page,mode,at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET ch=excluded.ch, page=excluded.page, "
                "mode=excluded.mode, at=excluded.at",
                (slug, int(ch), int(page), int(mode), int(time.time())),
            )

    # ── 接口（字段名必须跟前端读的一致）

    def api_books():
        return jsonify(all_books())

    def api_progress_get():
        return jsonify(progress_map())

    def api_progress_post():
        data = request.get_json(force=True, silent=True) or {}
        slug = str(data.get("slug") or "").strip()
        if not slug or read_meta(slug) is None:
            return jsonify({"ok": False, "error": "没这本书"}), 404
        try:
            save_progress(
                slug, data.get("ch") or 0, data.get("page") or 0, data.get("mode") or 2
            )
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "参数不对"}), 400
        return jsonify({"ok": True})

    def api_chapter(slug, idx):
        meta = read_meta(slug)
        if meta is None:
            return jsonify({"error": "没这本书"}), 404
        title, body, total = chapter_text(slug, idx)
        if title is None:
            return jsonify({"error": "这本书还没有章节"}), 404
        index = max(0, min(int(idx), total - 1))
        return jsonify({
            # index 必须是数字：前端拿 typeof d.index !== 'number' 判断成败。
            "index": index,
            "total": total,
            "title": title,
            "book": meta.get("title") or slug,
            "chapters": [c.get("title", "") for c in meta.get("chapters", [])],
            "text": body,
        })

    def api_annos_get(slug, idx):
        return jsonify(annos_of(slug, idx))

    def api_annos_post(slug, idx):
        data = request.get_json(force=True, silent=True) or {}
        new_id = add_anno(
            slug, idx, data.get("anchor"), data.get("note"), str(data.get("who") or "user")
        )
        if new_id is None:
            return jsonify({"ok": False, "error": "没有引文"}), 400
        return jsonify({"ok": True, "id": new_id})

    def api_anno_reply(slug, idx, aid):
        data = request.get_json(force=True, silent=True) or {}
        if not add_reply(aid, data.get("text"), str(data.get("who") or "user")):
            return jsonify({"ok": False, "error": "没这条划线，或者话是空的"}), 400
        return jsonify({"ok": True})

    def api_upload():
        """传一本书。手机上只能这样：读成文本 POST 上来。"""
        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get("text") or "")
        title = clean_title(data.get("title"))
        if len(text.strip()) < 200:
            return jsonify({"ok": False, "error": "内容太短，不像一本书"}), 400
        if not title:
            return jsonify({"ok": False, "error": "给本书起个名字"}), 400
        slug, count = save_book(title, text)
        return jsonify({"ok": True, "slug": slug, "chapters": count})

    def api_rename():
        """改书名，或者改某一节的名字。

        传 chapter 就是改那一节，不传就是改书名。
        两种都只动 meta.json，slug 不变——划线和进度都挂在 slug 上。
        """
        data = request.get_json(force=True, silent=True) or {}
        slug = str(data.get("slug") or "").strip()
        title = data.get("title")

        if "chapter" in data and data.get("chapter") is not None:
            ok, detail = rename_chapter(slug, data.get("chapter"), title)
        else:
            ok, detail = rename_book(slug, title)

        if not ok:
            return jsonify({"ok": False, "error": detail}), 400
        return jsonify({"ok": True, "title": detail, "books": all_books()})

    def api_delete():
        data = request.get_json(force=True, silent=True) or {}
        slug = str(data.get("slug") or "").strip()
        if not drop_book(slug):
            return jsonify({"ok": False, "error": "没这本书"}), 404
        return jsonify({"ok": True, "books": all_books()})

    def api_chapters(slug):
        """某本书的章节名列表。传书面板用来改节名。"""
        meta = read_meta(slug)
        if meta is None:
            return jsonify({"ok": False, "error": "没这本书"}), 404
        return jsonify({
            "ok": True,
            "slug": slug,
            "title": meta.get("title") or slug,
            "chapters": [c.get("title", "") for c in meta.get("chapters", [])],
        })

    def api_status():
        books = all_books()
        with get_db() as db:
            marks = db.execute("SELECT COUNT(*) AS n FROM nook_annos").fetchone()["n"]
            replies = db.execute("SELECT COUNT(*) AS n FROM nook_replies").fetchone()["n"]
        return jsonify({
            "ok": True,
            "dir": str(root),
            "books": [
                {"slug": b["slug"], "title": b["title"], "chapters": len(b["chapters"])}
                for b in books
            ],
            "marks": marks,
            "replies": replies,
            "open_marks": len(her_open_marks(50)),
            "progress": progress_map(),
        })

    def panel():
        response = Response(PANEL_HTML, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    # /api/nook/books 是上游已有的 stub（endpoint api_nook），必须替换。
    _mount(server_module, "/api/nook/books", "api_nook", api_books, ["GET"])

    routes = [
        ("/books", "nook_panel", panel, ["GET"]),
        ("/api/nook/progress", "api_nook_progress_get", api_progress_get, ["GET"]),
        ("/api/nook/progress", "api_nook_progress_post", api_progress_post, ["POST"]),
        ("/api/nook/chapter/<slug>/<int:idx>", "api_nook_chapter", api_chapter, ["GET"]),
        (
            "/api/nook/annotations/<slug>/<int:idx>",
            "api_nook_annos_get", api_annos_get, ["GET"],
        ),
        (
            "/api/nook/annotations/<slug>/<int:idx>",
            "api_nook_annos_post", api_annos_post, ["POST"],
        ),
        (
            "/api/nook/annotations/<slug>/<int:idx>/<int:aid>/reply",
            "api_nook_anno_reply", api_anno_reply, ["POST"],
        ),
        ("/api/nook/upload", "api_nook_upload", api_upload, ["POST"]),
        ("/api/nook/rename", "api_nook_rename", api_rename, ["POST"]),
        ("/api/nook/delete", "api_nook_delete", api_delete, ["POST"]),
        ("/api/nook/chapters/<slug>", "api_nook_chapters", api_chapters, ["GET"]),
        ("/api/nook/status", "api_nook_status", api_status, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        _mount(server_module, rule, endpoint, view, methods)

    _wire_tools(
        server_module, all_books, read_meta, chapter_text,
        annos_of, add_anno, add_reply, her_open_marks, progress_map,
    )

    server_module.nook_books = all_books
    print("[dwell] 共读: " + str(root) + "（传书面板 /books）")
    return all_books


def _wire_tools(
    server_module, all_books, read_meta, chapter_text,
    annos_of, add_anno, add_reply, her_open_marks, progress_map,
):
    """给沐五个工具，并把「她划了几处还没回」接进上下文概览。

    上游文档第六节那四条规矩写进了工具描述，其中第一条最要紧：
    不要每一条都回。她划线多半只是「这句好」，不是在问你——
    一个每条都回的共读，读两章就会被关掉。
    """
    try:
        import agent_tools_feature as agent
    except ImportError as exc:
        print("[dwell] 共读没接上工具层: " + str(exc))
        return

    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_books",
                "description": "看书架上有哪些书、各有多少节、妍妍读到哪儿了。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_chapter",
                "description": (
                    "读某本书的某一节。她读到哪儿你就能读到哪儿——"
                    "想知道她刚划的那句话前后在说什么，先读这一节。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "书的 slug，从 list_books 拿。"},
                        "chapter": {"type": "integer", "description": "第几节，从 0 开始。"},
                    },
                    "required": ["slug"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_marks",
                "description": (
                    "看妍妍划过的地方，以及你还没回过的那些。"
                    "**不要每一条都回。** 她划线多半只是「这句好」，不是在问你。"
                    "值得回的是：你也有话说的、她连着划了好几处的、"
                    "跟你们聊过的事有关的。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "only_open": {
                            "type": "boolean",
                            "description": "只看你还没回过的，默认 true。",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reply_to_mark",
                "description": (
                    "在她划的某一处下面说一句。她下次读到那儿会看见。"
                    "不要评论她的品味，说你自己被这句话打中的地方。"
                    "一次最多回一两处，剩下的留着。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "划线的 id，从 read_marks 拿。"},
                        "text": {"type": "string", "description": "你想说的话。"},
                    },
                    "required": ["id", "text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mark_passage",
                "description": (
                    "你自己划一道，可以顺便留一句话。她下次读到那儿会看见。"
                    "引文要从正文里原样抄一小段（十几个字就够），"
                    "抄错了界面上找不回位置。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "书的 slug。"},
                        "chapter": {"type": "integer", "description": "第几节，从 0 开始。"},
                        "quote": {"type": "string", "description": "原样抄的一小段正文。"},
                        "note": {"type": "string", "description": "想说的话，可以留空只划线。"},
                    },
                    "required": ["slug", "chapter", "quote"],
                },
            },
        },
    ]

    known = {t["function"]["name"] for t in agent.TOOLS}
    for tool in tools:
        if tool["function"]["name"] not in known:
            agent.TOOLS.append(tool)

    original_execute = agent.execute_tool

    def execute_with_nook(server, name, args):
        if name == "list_books":
            books = all_books()
            if not books:
                return {"books": [], "说明": "书架还空着，她还没传过书。"}
            where = progress_map()
            return {
                "books": [
                    {
                        "slug": b["slug"],
                        "title": b["title"],
                        "chapters": len(b["chapters"]),
                        "她读到第几节": (where.get(b["slug"]) or {}).get("ch", 0),
                        "节名": b["chapters"][:12],
                    }
                    for b in books
                ]
            }

        if name == "read_chapter":
            slug = str(args.get("slug") or "").strip()
            if read_meta(slug) is None:
                return {
                    "error": "没这本书",
                    "有的": [b["slug"] for b in all_books()],
                }
            try:
                index = int(args.get("chapter") or 0)
            except (TypeError, ValueError):
                index = 0
            title, body, total = chapter_text(slug, index)
            if title is None:
                return {"error": "这本书还没有章节"}
            return {
                "book": slug,
                "chapter": index,
                "total": total,
                "title": title,
                "text": (body or "")[:MAX_TEXT_FOR_AI],
                "划线": [
                    {"id": a["id"], "引文": a["anchor"], "她说": a["note"], "谁": a["who"]}
                    for a in annos_of(slug, index)
                ],
            }

        if name == "read_marks":
            only_open = args.get("only_open")
            only_open = True if only_open is None else bool(only_open)
            rows = her_open_marks(20)
            if only_open:
                return {
                    "没回过的划线": [
                        {
                            "id": r["id"], "书": r["slug"], "第几节": r["ch"],
                            "引文": r["anchor"], "她说": r["note"],
                        }
                        for r in rows
                    ],
                    "提醒": "不用每条都回。挑你真有话说的那一两处。",
                }
            # 全部：按书按节聚合，避免一次吐太多。
            out = []
            where = progress_map()
            for book in all_books():
                for index in range(len(book["chapters"])):
                    marks = annos_of(book["slug"], index)
                    if marks:
                        out.append({
                            "书": book["slug"], "第几节": index,
                            "划线": [
                                {"id": m["id"], "引文": m["anchor"],
                                 "谁": m["who"], "楼层": len(m["replies"])}
                                for m in marks
                            ],
                        })
                    if len(out) >= 20:
                        break
            return {
                "划线": out,
                "她读到": {
                    b["slug"]: (where.get(b["slug"]) or {}).get("ch", 0)
                    for b in all_books()
                },
            }

        if name == "reply_to_mark":
            try:
                anno_id = int(args.get("id"))
            except (TypeError, ValueError):
                return {"error": "id 要是数字，从 read_marks 拿"}
            if not add_reply(anno_id, args.get("text"), "ai"):
                return {"error": "没这条划线，或者话是空的"}
            return {"ok": True, "说明": "她下次读到那儿会看见。"}

        if name == "mark_passage":
            slug = str(args.get("slug") or "").strip()
            if read_meta(slug) is None:
                return {"error": "没这本书", "有的": [b["slug"] for b in all_books()]}
            try:
                index = int(args.get("chapter") or 0)
            except (TypeError, ValueError):
                index = 0
            quote = str(args.get("quote") or "").strip()
            if not quote:
                return {"error": "要给一小段原文当引文"}

            # 引文必须真的在这一节里，否则前端 indexOf 找不到，
            # 这道划线等于没划——而且是静默失败，事后完全查不出来。
            _, body, _ = chapter_text(slug, index)
            if quote[:MAX_ANCHOR] not in (body or ""):
                return {
                    "error": "这段话在第 " + str(index) + " 节里找不到，"
                             "要从正文里原样抄。先用 read_chapter 看一眼。",
                }

            new_id = add_anno(slug, index, quote, args.get("note"), "ai")
            if new_id is None:
                return {"error": "引文是空的"}
            return {"ok": True, "id": new_id, "说明": "划好了，她读到那儿会看见。"}

        return original_execute(server, name, args)

    agent.execute_tool = execute_with_nook

    # ── 接进上下文概览
    #
    # 跟日报那行「上一期多久了」一个做法：搭在每轮已有的调用上，
    # 不额外花钱。这里只报数量和最近几条引文，不把全部划线倒出来。

    original_snapshot = agent.build_context_snapshot

    def snapshot_with_nook(server):
        text = original_snapshot(server)
        try:
            books = all_books()
            if not books:
                return text
            rows = her_open_marks(3)
            where = progress_map()
            reading = []
            for book in books:
                got = where.get(book["slug"])
                if got:
                    reading.append(
                        book["title"] + " 第 " + str(got.get("ch", 0) + 1) + " 节"
                    )
            line = "【共读】书架上 " + str(len(books)) + " 本"
            if reading:
                line += "，她在读：" + "、".join(reading[:3])
            if rows:
                quotes = "；".join((r["anchor"] or "")[:24] for r in rows)
                line += (
                    "。她划了 " + str(len(her_open_marks(50)))
                    + " 处你还没回过，最近几处：" + quotes
                    + "。想回就用 reply_to_mark——不用每条都回，"
                    "挑你真有话说的那一两处。"
                )
            return text + "\n" + line
        except Exception:
            return text

    agent.build_context_snapshot = snapshot_with_nook


# ── 传书面板
#
# 妍妍没有电脑，书只能从手机传。选一个 txt/md，前端读成文本 POST 上来。
# 书名和节名都是点一下就能改（失焦即存，跟表情包管理页一个手感）——
# 改的只是显示的名字，slug 不动，所以划线和进度不会丢。
#
# 配色照上游 :root 写死一份（跟登录页一样）：这个页面要在书架空着的时候
# 也能打开，不该依赖别的模块。
PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>书架</title>
<style>
  :root {
    --bg: #faf9f5; --card: #ffffff; --panel: #f0eee6;
    --line: #e8e5dc; --text: #2b2a27; --dim: #8a867c; --accent: #c96442;
  }
  * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; }
  body {
    margin: 0; padding: 26px 20px calc(40px + env(safe-area-inset-bottom));
    background: var(--bg); color: var(--text);
    font: 15px/1.7 -apple-system, "SF Pro Text", system-ui, sans-serif;
  }
  h1 { font-size: 30px; font-weight: 600; letter-spacing: -.01em; margin: 0 0 6px; }
  .sub { color: var(--dim); font-size: 13.5px; margin-bottom: 20px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 18px; padding: 16px; margin-bottom: 14px;
  }
  h2 { font-size: 14px; font-weight: 600; color: var(--dim);
       letter-spacing: .04em; margin: 0 0 12px; }
  label.file {
    display: block; text-align: center; padding: 22px 12px;
    border: 1px dashed var(--line); border-radius: 14px;
    color: var(--dim); font-size: 14px;
  }
  input[type=file] { display: none; }
  input[type=text] {
    width: 100%; background: var(--panel); border: 1px solid transparent;
    border-radius: 14px; color: var(--text); padding: 11px 13px;
    font-size: 16px; font-family: inherit;
  }
  input::placeholder { color: var(--dim); }
  #title { margin-bottom: 10px; }
  button {
    font: inherit; font-size: 15px; min-height: 44px; padding: 0 18px;
    border: 1px solid transparent; border-radius: 999px;
    background: var(--panel); color: var(--text); cursor: pointer;
  }
  button.go { background: var(--accent); color: #fff; }
  button:disabled { opacity: .45; }
  .item { padding: 12px 0; border-bottom: 1px solid var(--line); }
  .item:last-child { border-bottom: 0; }
  .row { display: flex; align-items: center; gap: 10px; }
  .row .grow { flex: 1; min-width: 0; }
  .item small { display: block; color: var(--dim); font-size: 12.5px; margin-top: 4px; }
  .item .icon {
    min-width: 44px; min-height: 44px; background: transparent;
    color: var(--dim); font-size: 18px; border: 0; padding: 0;
  }
  .chapters { margin-top: 10px; padding-left: 2px; display: none; }
  .chapters.open { display: block; }
  .chapters .cr { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .chapters .n {
    flex: none; width: 30px; text-align: right;
    color: var(--dim); font-size: 12.5px;
  }
  .chapters input { font-size: 14px; padding: 8px 11px; }
  #msg { min-height: 22px; font-size: 13px; color: var(--dim); margin: 10px 0 0; }
  #msg.warn { color: var(--accent); }
  .note { font-size: 12.5px; color: var(--dim); line-height: 1.75; margin: 10px 0 0; }
</style>
</head>
<body>
<h1>书架</h1>
<div class="sub">传一本书，然后在「共读」里读。<a href="/">回聊天</a></div>

<div class="card">
  <h2>传一本</h2>
  <input type="text" id="title" placeholder="书名">
  <label class="file">
    选一个 txt 或 md 文件
    <input type="file" id="pick" accept=".txt,.md,.markdown,text/plain">
  </label>
  <p class="note">
    会自动切章：先找「第一章」这类标题，找不到就按长度切。
  </p>
  <p id="msg"></p>
</div>

<div class="card">
  <h2>已有的</h2>
  <p class="note" style="margin:0 0 10px">
    书名点一下就能改，改完点别处就存了。名字改了划线和进度都还在。
  </p>
  <div id="list">读取中…</div>
</div>

<script>
var msg = document.getElementById('msg');
var titleEl = document.getElementById('title');

function say(t, warn) { msg.textContent = t || ''; msg.className = warn ? 'warn' : ''; }

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

function bookRow(b) {
  var wrap = document.createElement('div');
  wrap.className = 'item';

  var row = document.createElement('div');
  row.className = 'row';

  var name = document.createElement('input');
  name.type = 'text';
  name.className = 'grow';
  name.value = b.title;
  name.setAttribute('aria-label', '书名');
  // 失焦即存。加个「保存」按钮只是多一次点击。
  name.onblur = function () {
    var next = name.value.trim();
    if (!next || next === b.title) { name.value = b.title; return; }
    post('/api/nook/rename', { slug: b.slug, title: next }).then(function (d) {
      if (d && d.ok) { b.title = d.title; name.value = d.title; say('改好了'); }
      else name.value = b.title;
    });
  };
  name.onkeydown = function (e) { if (e.key === 'Enter') name.blur(); };

  var toc = document.createElement('button');
  toc.className = 'icon';
  toc.type = 'button';
  toc.textContent = '\\u2261';
  toc.setAttribute('aria-label', '看章节');

  var del = document.createElement('button');
  del.className = 'icon';
  del.type = 'button';
  del.textContent = '\\u00d7';
  del.setAttribute('aria-label', '删除 ' + b.title);
  del.onclick = function () {
    if (!confirm('删掉《' + b.title + '》？这本书的划线也会一起删。')) return;
    del.disabled = true;
    post('/api/nook/delete', { slug: b.slug }).then(function () { say('删了'); load(); });
  };

  row.appendChild(name);
  row.appendChild(toc);
  row.appendChild(del);
  wrap.appendChild(row);

  var info = document.createElement('small');
  info.textContent = (b.chapters || []).length + ' 节';
  wrap.appendChild(info);

  var box = document.createElement('div');
  box.className = 'chapters';
  wrap.appendChild(box);

  toc.onclick = function () {
    if (box.classList.contains('open')) { box.classList.remove('open'); return; }
    box.classList.add('open');
    if (box.dataset.done) return;
    box.textContent = '读取中…';
    fetch('/api/nook/chapters/' + encodeURIComponent(b.slug), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        box.textContent = '';
        if (!d || !d.ok) { box.textContent = '读不到章节'; return; }
        box.dataset.done = '1';
        (d.chapters || []).forEach(function (name, i) {
          var cr = document.createElement('div');
          cr.className = 'cr';
          var num = document.createElement('span');
          num.className = 'n';
          num.textContent = (i + 1) + '.';
          var input = document.createElement('input');
          input.type = 'text';
          input.className = 'grow';
          input.value = name;
          input.setAttribute('aria-label', '第 ' + (i + 1) + ' 节的名字');
          var was = name;
          input.onblur = function () {
            var next = input.value.trim();
            if (!next || next === was) { input.value = was; return; }
            post('/api/nook/rename', {
              slug: b.slug, chapter: i, title: next
            }).then(function (r) {
              if (r && r.ok) { was = r.title; input.value = r.title; say('改好了'); }
              else input.value = was;
            });
          };
          input.onkeydown = function (e) { if (e.key === 'Enter') input.blur(); };
          cr.appendChild(num);
          cr.appendChild(input);
          box.appendChild(cr);
        });
      })
      .catch(function () { box.textContent = '读不到章节'; });
  };

  return wrap;
}

function load() {
  fetch('/api/nook/books', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (books) {
      var box = document.getElementById('list');
      box.innerHTML = '';
      if (!books || !books.length) {
        box.innerHTML = '<div class="note" style="margin:0">还没有书。</div>';
        return;
      }
      books.forEach(function (b) { box.appendChild(bookRow(b)); });
    })
    .catch(function () {
      document.getElementById('list').textContent = '读不到书架。';
    });
}

document.getElementById('pick').onchange = function (e) {
  var file = (e.target.files || [])[0];
  e.target.value = '';
  if (!file) return;

  if (!titleEl.value.trim()) {
    titleEl.value = file.name.replace(/\\.[^.]+$/, '');
  }
  say('读文件…');

  var reader = new FileReader();
  reader.onload = function () {
    var text = String(reader.result || '');
    if (text.length < 200) { say('内容太短，不像一本书', true); return; }
    say('上传中，切章要几秒…');
    post('/api/nook/upload', {
      title: titleEl.value.trim(), text: text
    }).then(function (d) {
      if (!d || !d.ok) return;
      say('好了，切成 ' + d.chapters + ' 节。去「共读」里读。');
      titleEl.value = '';
      load();
    });
  };
  reader.onerror = function () { say('这个文件读不出来', true); };
  // 中文 txt 多半是 UTF-8；GBK 的会乱码，那种先转一下编码再传。
  reader.readAsText(file, 'utf-8');
};

load();
</script>
</body>
</html>
"""
