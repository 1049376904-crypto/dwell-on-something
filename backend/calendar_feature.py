"""dwell 日历功能：日程、重要日子和每日心情记录。"""

import time
from flask import jsonify, request


def register_calendar_feature(server_module):
    """替换 server.py 里原先的 /api/cal 占位实现。"""
    get_db = server_module.get_db

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                date    TEXT NOT NULL,
                text    TEXT NOT NULL,
                time    TEXT NOT NULL DEFAULT '',
                yearly  INTEGER NOT NULL DEFAULT 0,
                type    TEXT NOT NULL DEFAULT 'reminder',
                created INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS calendar_day_records (
                date    TEXT PRIMARY KEY,
                flow    TEXT NOT NULL DEFAULT '',
                pain    INTEGER NOT NULL DEFAULT 0,
                mood    TEXT NOT NULL DEFAULT '',
                note    TEXT NOT NULL DEFAULT '',
                private TEXT NOT NULL DEFAULT '',
                updated INTEGER NOT NULL
            );
        """)

    def calendar_payload():
        with get_db() as db:
            event_rows = db.execute(
                "SELECT id,date,text,time,yearly,type,created "
                "FROM calendar_events ORDER BY date,time,created"
            ).fetchall()
            record_rows = db.execute(
                "SELECT date,flow,pain,mood,note,private,updated "
                "FROM calendar_day_records ORDER BY date"
            ).fetchall()

        events = [
            {
                "id": row["id"],
                "date": row["date"],
                "text": row["text"],
                "time": row["time"],
                "yearly": bool(row["yearly"]),
                "type": row["type"],
                "created": row["created"],
            }
            for row in event_rows
        ]

        days = {
            row["date"]: {
                "flow": row["flow"],
                "pain": row["pain"],
                "mood": row["mood"],
                "note": row["note"],
                "private": row["private"],
                "updated": row["updated"],
            }
            for row in record_rows
        }

        return {
            "ok": True,
            "cal": {
                "events": events,
                "period": {"days": days},
            },
            "predict": None,
        }

    def api_cal_real():
        return jsonify(calendar_payload())

    def api_cal_post_real():
        data = request.get_json(force=True, silent=True) or {}
        action = data.get("action", "")

        with get_db() as db:
            if action == "add_event":
                date = str(data.get("date", "")).strip()
                text = str(data.get("text", "")).strip()
                if not date or not text:
                    return jsonify({"ok": False, "error": "date 和 text 不能为空"}), 400

                event_type = "special" if data.get("special") else "reminder"
                db.execute(
                    "INSERT INTO calendar_events "
                    "(date,text,time,yearly,type,created) VALUES (?,?,?,?,?,?)",
                    (
                        date,
                        text,
                        str(data.get("time", "")).strip(),
                        int(bool(data.get("yearly"))),
                        event_type,
                        int(time.time()),
                    ),
                )

            elif action == "del_event":
                event_id = data.get("id")
                if event_id is None:
                    return jsonify({"ok": False, "error": "缺少事件 id"}), 400
                db.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))

            elif action == "day_record":
                date = str(data.get("date", "")).strip()
                if not date:
                    return jsonify({"ok": False, "error": "缺少日期"}), 400

                db.execute(
                    """
                    INSERT INTO calendar_day_records
                        (date,flow,pain,mood,note,private,updated)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET
                        flow=excluded.flow,
                        pain=excluded.pain,
                        mood=excluded.mood,
                        note=excluded.note,
                        private=excluded.private,
                        updated=excluded.updated
                    """,
                    (
                        date,
                        str(data.get("flow", "")),
                        int(data.get("pain", 0) or 0),
                        str(data.get("mood", "")),
                        str(data.get("note", "")),
                        str(data.get("private", "")),
                        int(time.time()),
                    ),
                )

            else:
                return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

        return jsonify(calendar_payload())

    # server.py 已注册相同 URL 的占位 endpoint；直接替换 view function，
    # 不重复增加 URL rule，也不影响聊天和待办接口。
    server_module.app.view_functions["api_cal"] = api_cal_real
    server_module.app.view_functions["api_cal_post"] = api_cal_post_real
