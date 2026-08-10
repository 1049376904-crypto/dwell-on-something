"""/api/wake：接上上游设置页里那个「夜里让他自己醒」开关。

上游前端一直在调 `api/wake`，我们没实现，所以：
* 日志里每次开页面都有一条 404；
* 设置页那一行的开关永远停在「问不到」。

它读的字段很具体（从上游 wakeState() 反推）：
    GET  /api/wake  → {"on": bool, "count": n, "max": n, "room": str}
    POST /api/wake  {"on": bool}

room 是上游用来说明额度的：'停' 显示「额度到顶了不醒」，
'省' 显示「额度紧，只干便宜的」，其它值不显示后缀。
我们没有额度概念，但「这一夜已经说满了」正好对应 '停' 的语义，
所以借它的词来表达我们自己的闸门，而不是另造一套说法。

为什么单独一个文件、而不是塞进 heartbeat_feature：
那个文件已经不短，而这里只是给上游前端补一层薄薄的适配。
但键名和「一夜」的算法必须从那边导入——同一个概念有两处定义，
迟早会算出不一样的结果（跨午夜那次就是这么出问题的）。
"""

from flask import jsonify, request

import heartbeat_feature as hb


def register_wake_feature(server_module):
    get_db = server_module.get_db

    def read(key):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else hb.DEFAULTS.get(key, "")

    def read_int(key, fallback):
        try:
            return int(read(key))
        except (TypeError, ValueError):
            return fallback

    def night_count():
        """当夜已触发次数。

        算法必须和 heartbeat_feature 一致，所以「哪一夜」直接用它的
        night_key()——那边把一夜的起点挪到了中午，跨零点才不会把计数刷掉。
        """
        raw = str(read(hb.KEY_DAY_TALLY) or "")
        key = hb.night_key()
        if ":" in raw:
            day, _, count = raw.partition(":")
            if day == key:
                try:
                    return int(count)
                except ValueError:
                    return 0
        return 0

    def state():
        on = read(hb.KEY_ENABLED) == "1"
        count = night_count()
        limit = read_int(hb.KEY_MAX_PER_DAY, 2)

        # 借上游的词说我们的事：这一夜说满了就是 '停'，
        # 还剩最后一次就 '省'，让那一行能看出还有没有余量。
        if count >= limit:
            room = "停"
        elif limit - count <= 1:
            room = "省"
        else:
            room = ""

        return {
            "ok": True,
            "on": on,
            "count": count,
            "max": limit,
            "room": room,
            # 下面几个上游用不到，但 curl 的时候有用。
            "windows": read(hb.KEY_WINDOWS),
            "idle_minutes": read_int(hb.KEY_IDLE, 45),
            "last_result": read(hb.KEY_LAST_RESULT),
            "detail": "细节看 /api/heartbeat",
        }

    def api_wake_get():
        return jsonify(state())

    def api_wake_post():
        data = request.get_json(force=True, silent=True) or {}
        if "on" in data:
            with get_db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?,?)",
                    (hb.KEY_ENABLED, "1" if data["on"] else "0"),
                )
        return jsonify(state())

    server_module.app.add_url_rule(
        "/api/wake", endpoint="api_wake_get", view_func=api_wake_get, methods=["GET"]
    )
    server_module.app.add_url_rule(
        "/api/wake", endpoint="api_wake_post", view_func=api_wake_post, methods=["POST"]
    )

    print("[dwell] 心跳开关: /api/wake（上游设置页那一行）")
    return state
