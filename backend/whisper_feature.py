"""dwell 悄悄话功能：保存双方写下、但不出现在普通聊天里的话。"""

import time
from flask import jsonify, request


def register_whisper_feature(server_module):
    """替换 server.py 中原有的 /api/whisper 占位接口。"""
    get_db = server_module.get_db

    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS whispers (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                who  TEXT    NOT NULL,
                text TEXT    NOT NULL,
                at   INTEGER NOT NULL
            )
        """)

    def list_items():
        with get_db() as db:
            rows = db.execute(
                "SELECT id,who,text,at FROM whispers ORDER BY at,id"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "who": row["who"],
                "text": row["text"],
                "at": row["at"],
            }
            for row in rows
        ]

    def api_whisper_real():
        return jsonify({"items": list_items()})

    def api_whisper_post_real():
        # 原前端没有显式发送 Content-Type，因此使用 force=True 兼容它。
        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get("text", "")).strip()
        if not text:
            return jsonify({"ok": False, "error": "内容不能为空"}), 400

        # 前端写入默认属于妍妍；以后 AI 主动写入时可传 who='gu'。
        who = str(data.get("who", "her")).strip()
        if who not in {"her", "gu"}:
            who = "her"

        with get_db() as db:
            cur = db.execute(
                "INSERT INTO whispers (who,text,at) VALUES (?,?,?)",
                (who, text, int(time.time())),
            )
            item_id = cur.lastrowid

        return jsonify({"ok": True, "id": item_id, "items": list_items()})

    # server.py 已经注册了同名占位 endpoint；这里只替换处理函数，
    # 避免重复注册 URL，也不会影响聊天、待办和日历模块。
    server_module.app.view_functions["api_whisper"] = api_whisper_real
    server_module.app.view_functions["api_whisper_post"] = api_whisper_post_real
