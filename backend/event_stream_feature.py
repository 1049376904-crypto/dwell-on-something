"""可靠的可重放事件流。

原实现只把事件发给“此刻正在轮询”的请求；两个长轮询请求切换的空档里，
工具结果或最终回复会永久丢失。这里改成带递增游标的内存事件日志：前端传
since，服务端返回 since 之后的事件。刷新页面时 /api/messages 会同时给出
当前事件游标，避免把旧事件再次播放。
"""

import collections
import threading
import time

from flask import jsonify, request


MAX_EVENTS = 4096


def register_event_stream_feature(server_module):
    condition = threading.Condition()
    event_log = collections.deque(maxlen=MAX_EVENTS)
    cursor = 0

    def reliable_broadcast(event):
        nonlocal cursor
        with condition:
            cursor += 1
            event_log.append((cursor, event))
            condition.notify_all()
        return cursor

    def current_cursor():
        with condition:
            return cursor

    def api_poll_reliable():
        try:
            since = max(0, int(request.args.get("since", 0)))
        except (TypeError, ValueError):
            since = 0

        deadline = time.monotonic() + 25
        batch = []
        next_cursor = since

        with condition:
            while True:
                batch = [(seq, event) for seq, event in event_log if seq > since]
                if batch:
                    next_cursor = batch[-1][0]
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                condition.wait(timeout=remaining)

        return jsonify({
            "events": [event for _, event in batch[:128]],
            "next": batch[min(len(batch), 128) - 1][0] if batch else next_cursor,
        })

    def api_messages_reliable():
        try:
            limit = max(1, min(1000, int(request.args.get("limit", 400))))
        except (TypeError, ValueError):
            limit = 400

        before_raw = request.args.get("before")
        try:
            before = int(before_raw) if before_raw else None
        except (TypeError, ValueError):
            before = None

        msgs = server_module.load_messages(limit, before)
        more = False
        if msgs:
            with server_module.get_db() as db:
                more = db.execute(
                    "SELECT 1 FROM messages WHERE seq < ? LIMIT 1",
                    (msgs[0]["seq"],),
                ).fetchone() is not None

        # 这里返回的是事件流游标，而不是 messages 表的 seq。
        # 前端会把 upto 赋给 poll 的 since；两者混用正是旧回复串到下一轮的来源之一。
        return jsonify({
            "msgs": msgs,
            "upto": current_cursor(),
            "more": more,
        })

    server_module.broadcast = reliable_broadcast
    server_module.event_cursor = current_cursor
    server_module.app.view_functions["api_poll"] = api_poll_reliable
    server_module.app.view_functions["api_messages"] = api_messages_reliable
