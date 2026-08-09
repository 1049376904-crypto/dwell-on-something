"""持久化思考过程与工具调用，让刷新后仍能重放。

server.py 的 messages 表只存 her / gu 两种正文，思考和工具调用此前只走广播，
刷新即丢。这里用独立的 transcript 表记录，再在 /api/messages 响应里按顺序
并回去。

刻意不写进 messages 表：api_send 组装上下文时读的是 messages，思考和工具
记录混进去会被当成 assistant 正文发给网关，污染上下文。

排序方式：每条 transcript 行记录写入那一刻 messages 表的最大 seq（after_seq）。
妍妍的消息在一轮开始前就已入库、沐的回复在一轮结束才入库，所以同一轮的
思考与工具行天然锚定在两者之间。排序键 (anchor, tier, id) 完全确定，不依赖
时间戳（同一秒内无法区分先后）。

与前端的字段约定（上游 renderSaid 的 tool 分支）：
    addToolUse({ name: m.text, input: JSON.parse(m.extra), id: tid })
工具名走 text，入参走 extra 的 JSON 字符串。不要用 name 字段，前端不读。
返回结果原本被上游写死成空字符串，配套的 frontend_feature 补丁改成读
m.result / m.is_error，所以这里把结果并进同一条 tool 记录，而不是发送
独立的 tool_result 条目（renderSaid 没有对应分支，会被静默丢弃）。

注意 upto 字段：前端会把它赋给 /api/poll 的 since，所以它必须是事件流游标，
不能是 messages 表的 seq（见 event_stream_feature 里的说明）。本模块在
event_stream 之后注册并接管 api_messages，因此要自己保持这个语义。
"""

import time

from flask import jsonify, request


# 合成 seq 的偏移量，保证与 messages 表真实 seq 不冲突。
SYNTH_OFFSET = 1_000_000_000

# 单条工具结果在历史里展示的上限，避免超长 JSON 把页面拖慢。
MAX_RESULT_CHARS = 4000


def register_transcript_feature(server_module):
    get_db = server_module.get_db

    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS transcript (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                after_seq INTEGER NOT NULL,
                kind      TEXT    NOT NULL,
                name      TEXT    NOT NULL DEFAULT '',
                text      TEXT    NOT NULL DEFAULT '',
                extra     TEXT    NOT NULL DEFAULT '',
                call_id   TEXT    NOT NULL DEFAULT '',
                is_error  INTEGER NOT NULL DEFAULT 0,
                at        INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_transcript_after
                ON transcript (after_seq, id);
        """)

    def save_transcript(kind, text="", name="", extra="", call_id="", is_error=False):
        """记一条思考或工具记录，锚定在当前最新的正文消息之后。"""
        with get_db() as db:
            row = db.execute("SELECT MAX(seq) AS m FROM messages").fetchone()
            after_seq = int(row["m"] or 0)
            cur = db.execute(
                "INSERT INTO transcript "
                "(after_seq,kind,name,text,extra,call_id,is_error,at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    after_seq, str(kind), str(name), str(text),
                    str(extra), str(call_id), int(bool(is_error)), int(time.time()),
                ),
            )
            return cur.lastrowid

    def merged_messages(limit, before):
        base = server_module.load_messages(limit, before)
        if not base:
            return base, False

        low = base[0]["seq"]
        high = base[-1]["seq"]
        with get_db() as db:
            rows = db.execute(
                "SELECT id,after_seq,kind,name,text,extra,call_id,is_error,at "
                "FROM transcript WHERE after_seq>=? AND after_seq<=? "
                "ORDER BY after_seq,id",
                (low, high),
            ).fetchall()
            more = db.execute(
                "SELECT 1 FROM messages WHERE seq<? LIMIT 1", (low,)
            ).fetchone() is not None

        # 先把工具结果按 call_id 收起来，稍后并进对应的 tool 条目。
        results = {}
        for row in rows:
            if row["kind"] == "tool_result" and row["call_id"]:
                results[row["call_id"]] = row

        # 排序键第二位是层级：正文在前，同一锚点下的 transcript 在后。
        items = [((m["seq"], 1, 0), m) for m in base]

        for row in rows:
            kind = row["kind"]

            # tool_result 已并入 tool 条目，单独发过去前端没有分支会丢掉。
            if kind == "tool_result":
                continue

            item = {
                "seq": SYNTH_OFFSET + row["id"],
                "kind": kind,
                "text": row["text"],
                "at": row["at"],
            }

            if kind == "tool":
                # 前端从 text 读工具名、从 extra 读入参 JSON。
                item["text"] = row["name"] or row["text"]
                item["extra"] = row["extra"]

                result_row = results.get(row["call_id"])
                if result_row is not None:
                    payload = result_row["text"] or ""
                    if len(payload) > MAX_RESULT_CHARS:
                        payload = payload[:MAX_RESULT_CHARS] + "…（已截断）"
                    item["result"] = payload
                    item["is_error"] = bool(result_row["is_error"])
                else:
                    # 生成中途断掉的调用没有结果行，如实留空。
                    item["result"] = ""
                    item["is_error"] = False

            items.append(((row["after_seq"], 2, row["id"]), item))

        items.sort(key=lambda pair: pair[0])
        return [item for _, item in items], more

    def api_messages_real():
        try:
            limit = max(1, min(1000, int(request.args.get("limit", 400))))
        except (TypeError, ValueError):
            limit = 400

        before_raw = request.args.get("before")
        before = None
        if before_raw:
            try:
                candidate = int(before_raw)
            except ValueError:
                candidate = 0
            # 前端可能把合成 seq 当游标传回来；那不是真实分页位置，忽略即可。
            if 0 < candidate < SYNTH_OFFSET:
                before = candidate

        msgs, more = merged_messages(limit, before)

        # upto 必须是事件流游标：前端拿它当 /api/poll 的 since。
        cursor = getattr(server_module, "event_cursor", None)
        upto = cursor() if callable(cursor) else 0

        return jsonify({"msgs": msgs, "upto": upto, "more": more})

    # /api/messages 与 /api/said 共用 endpoint api_messages。
    server_module.app.view_functions["api_messages"] = api_messages_real

    server_module.save_transcript = save_transcript
    return save_transcript
