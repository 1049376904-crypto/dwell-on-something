"""把所有模块的数据库读写固定到同一个文件。

必须在注册其他模块之前执行，否则它们会把表建在旧的相对路径数据库里。
"""

import sqlite3

from flask import jsonify

from paths import resolve_db_path


TABLES = [
    "messages",
    "todos",
    "calendar_events",
    "calendar_day_records",
    "whispers",
    "diary_entries",
]


def register_storage_feature(server_module):
    db_path = resolve_db_path()
    server_module.DB_PATH = db_path

    def get_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        # 开启 WAL，减少长轮询与写入同时发生的锁冲突。
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=4000")
        return db

    server_module.get_db = get_db
    server_module.init_db()

    def api_storage():
        counts = {}
        with get_db() as db:
            for table in TABLES:
                try:
                    row = db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                    counts[table] = row["n"]
                except sqlite3.Error:
                    # 对应模块还没注册时，表可能不存在。
                    counts[table] = None
        return jsonify({
            "ok": True,
            "db_path": str(db_path),
            "exists": db_path.exists(),
            "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "counts": counts,
        })

    server_module.app.add_url_rule(
        "/api/storage",
        endpoint="api_storage",
        view_func=api_storage,
        methods=["GET"],
    )

    print(f"[dwell] 数据库: {db_path}")
    return db_path
