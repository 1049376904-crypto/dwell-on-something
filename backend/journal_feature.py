"""dwell 日记功能：日记墙、我的日记和摘录内容的基础持久化。"""

import time
from datetime import datetime

from flask import jsonify, request


def register_journal_feature(server_module):
    """替换 server.py 中的日记相关占位接口。"""
    get_db = server_module.get_db

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS diary_entries (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT    NOT NULL,
                title     TEXT    NOT NULL DEFAULT '',
                keywords  TEXT    NOT NULL DEFAULT '',
                text      TEXT    NOT NULL,
                intensity INTEGER NOT NULL DEFAULT 0,
                valence   INTEGER NOT NULL DEFAULT 0,
                arousal   INTEGER NOT NULL DEFAULT 0,
                source    TEXT    NOT NULL DEFAULT 'ai',
                created   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS personal_diary (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                text    TEXT    NOT NULL,
                at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS favorite_lines (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                text    TEXT    NOT NULL,
                note    TEXT    NOT NULL DEFAULT '',
                at      INTEGER NOT NULL
            );
        """)

    def wall_payload(lite=False):
        with get_db() as db:
            rows = db.execute(
                "SELECT id,date,title,keywords,text,intensity,valence,arousal,source,created "
                "FROM diary_entries ORDER BY date,created,id"
            ).fetchall()

        bricks = []
        for row in rows:
            item = {
                "id": row["id"],
                "date": row["date"],
                "title": row["title"],
                "kw": row["keywords"],
                "s": row["intensity"],
                "v": row["valence"],
                "a": row["arousal"],
                "source": row["source"],
                "created": row["created"],
            }
            # 前端用 ?lite=1 初始化墙；读全文时再请求普通版本。
            if not lite:
                item["text"] = row["text"]
            else:
                item["text"] = ""
            bricks.append(item)

        return {"ok": True, "bricks": bricks}

    def api_wall_real():
        lite = request.args.get("lite") == "1"
        return jsonify(wall_payload(lite))

    def api_wall_post_real():
        data = request.get_json(force=True, silent=True) or {}
        action = str(data.get("action", "add"))

        with get_db() as db:
            if action == "add":
                text = str(data.get("text", "")).strip()
                if not text:
                    return jsonify({"ok": False, "error": "日记内容不能为空"}), 400
                date = str(data.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
                cur = db.execute(
                    """
                    INSERT INTO diary_entries
                        (date,title,keywords,text,intensity,valence,arousal,source,created)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        date,
                        str(data.get("title", "")).strip(),
                        str(data.get("keywords", data.get("kw", ""))).strip(),
                        text,
                        int(data.get("intensity", data.get("s", 0)) or 0),
                        int(data.get("valence", data.get("v", 0)) or 0),
                        int(data.get("arousal", data.get("a", 0)) or 0),
                        str(data.get("source", "ai")),
                        int(time.time()),
                    ),
                )
                entry_id = cur.lastrowid
            elif action == "delete":
                entry_id = int(data.get("id", 0))
                db.execute("DELETE FROM diary_entries WHERE id=?", (entry_id,))
            else:
                return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

        result = wall_payload(False)
        result["id"] = entry_id
        return jsonify(result)

    def personal_items():
        with get_db() as db:
            rows = db.execute(
                "SELECT id,text,at FROM personal_diary ORDER BY at DESC,id DESC"
            ).fetchall()
        return [{"id": row["id"], "text": row["text"], "at": row["at"]} for row in rows]

    def api_herdiary_real():
        return jsonify({"items": personal_items()})

    def api_herdiary_post_real():
        data = request.get_json(force=True, silent=True) or {}
        action = str(data.get("action", "add"))
        with get_db() as db:
            if action == "add":
                text = str(data.get("text", "")).strip()
                if not text:
                    return jsonify({"ok": False, "error": "日记内容不能为空"}), 400
                cur = db.execute(
                    "INSERT INTO personal_diary (text,at) VALUES (?,?)",
                    (text, int(time.time())),
                )
                item_id = cur.lastrowid
            elif action == "del":
                item_id = int(data.get("id", 0))
                db.execute("DELETE FROM personal_diary WHERE id=?", (item_id,))
            else:
                return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400
        return jsonify({"ok": True, "id": item_id, "items": personal_items()})

    def api_favlines_real():
        with get_db() as db:
            rows = db.execute(
                "SELECT text,note,at FROM favorite_lines ORDER BY at DESC,id DESC"
            ).fetchall()

        blocks = ["# 摘下来的话"]
        for row in rows:
            date = datetime.fromtimestamp(row["at"]).strftime("%Y-%m-%d")
            block = ["---", "", f"**{date}**", f"> {row['text']}"]
            if row["note"]:
                block.extend(["", row["note"]])
            blocks.extend(block)
        return jsonify({"ok": True, "text": "\n".join(blocks)})

    server_module.app.view_functions["api_wall"] = api_wall_real
    server_module.app.view_functions["api_herdiary"] = api_herdiary_real
    server_module.app.view_functions["api_herdiary_post"] = api_herdiary_post_real
    server_module.app.view_functions["api_favlines"] = api_favlines_real

    # server.py 没有 POST /api/wall，新增一个供未来 AI 工具和管理功能使用。
    server_module.app.add_url_rule(
        "/api/wall",
        endpoint="api_wall_post",
        view_func=api_wall_post_real,
        methods=["POST"],
    )
