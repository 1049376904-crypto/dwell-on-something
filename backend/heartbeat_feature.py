"""心跳：让沐在设定的时段里主动开口，而不是永远等妍妍先说话。

设计要点：
* 复用现成管道。生成走 server.call_gateway（已带工具和自动概览），
  它自己会广播 stream_event、存 messages、写 transcript，
  所以前端不需要任何改动，长轮询照常收到。
* 默认关闭。心跳会自己发起网关请求、产生费用，必须由妍妍显式打开。
* 状态落库。上次触发时间和当天次数写在 settings 表，pm2 重启不会重新刷一轮。
* 绝不打断对话。生成中（busy）或她刚说过话（静默不足）都直接跳过。

时段格式：HH:MM-HH:MM，逗号分隔，允许跨午夜（如 23:00-01:00）。
"""

import threading
import time
from datetime import datetime

from flask import jsonify, request


KEY_ENABLED = "heartbeat_enabled"
KEY_WINDOWS = "heartbeat_windows"
KEY_MAX_PER_DAY = "heartbeat_max_per_day"
KEY_MIN_GAP = "heartbeat_min_gap_minutes"
KEY_IDLE = "heartbeat_idle_minutes"
KEY_LAST_AT = "heartbeat_last_at"
KEY_DAY_TALLY = "heartbeat_day_tally"

DEFAULTS = {
    KEY_ENABLED: "0",
    # 睡前和清早各留一个窗口。
    KEY_WINDOWS: "22:30-23:59,07:00-08:30",
    KEY_MAX_PER_DAY: "2",
    KEY_MIN_GAP: "240",
    KEY_IDLE: "90",
}

CHECK_INTERVAL = 60
HISTORY_FOR_HEARTBEAT = 24


def _parse_windows(raw):
    """把 "22:30-23:59,07:00-08:30" 解析成分钟区间列表。"""
    windows = []
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if "-" not in chunk:
            continue
        start_text, _, end_text = chunk.partition("-")
        try:
            sh, sm = (int(x) for x in start_text.strip().split(":"))
            eh, em = (int(x) for x in end_text.strip().split(":"))
        except (ValueError, TypeError):
            continue
        start = sh * 60 + sm
        end = eh * 60 + em
        if 0 <= start < 1440 and 0 <= end < 1440:
            windows.append((start, end))
    return windows


def _in_windows(windows, now_minutes):
    for start, end in windows:
        if start <= end:
            if start <= now_minutes <= end:
                return True
        else:
            # 跨午夜，例如 23:00-01:00
            if now_minutes >= start or now_minutes <= end:
                return True
    return False


def register_heartbeat_feature(server_module):
    get_db = server_module.get_db

    def read(key):
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else DEFAULTS.get(key, "")

    def write(key, value):
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    with get_db() as db:
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))

    def read_int(key, fallback):
        try:
            return int(read(key))
        except (TypeError, ValueError):
            return fallback

    def last_message_at():
        with get_db() as db:
            row = db.execute("SELECT MAX(at) AS m FROM messages").fetchone()
        return int(row["m"] or 0)

    def today_count():
        """当天已触发次数；日期一变自动归零。"""
        raw = read(KEY_DAY_TALLY)
        today = datetime.now().strftime("%Y-%m-%d")
        if ":" in str(raw):
            day, _, count = str(raw).partition(":")
            if day == today:
                try:
                    return today, int(count)
                except ValueError:
                    return today, 0
        return today, 0

    def bump_count():
        today, count = today_count()
        write(KEY_DAY_TALLY, f"{today}:{count + 1}")
        write(KEY_LAST_AT, int(time.time()))

    def why_not_now():
        """返回不该触发的原因；None 表示可以触发。"""
        if read(KEY_ENABLED) != "1":
            return "心跳未开启"

        with server_module.state_lock:
            if server_module.state["busy"]:
                return "正在生成回复"

        now = datetime.now()
        windows = _parse_windows(read(KEY_WINDOWS))
        if not windows:
            return "没有配置有效时段"
        if not _in_windows(windows, now.hour * 60 + now.minute):
            return "不在设定时段内"

        _, count = today_count()
        limit = read_int(KEY_MAX_PER_DAY, 2)
        if count >= limit:
            return f"今天已经主动说过 {count} 次，达到上限 {limit}"

        gap_minutes = read_int(KEY_MIN_GAP, 240)
        last = read_int(KEY_LAST_AT, 0)
        if last and time.time() - last < gap_minutes * 60:
            return f"距上次心跳不足 {gap_minutes} 分钟"

        idle_minutes = read_int(KEY_IDLE, 90)
        last_msg = last_message_at()
        if last_msg and time.time() - last_msg < idle_minutes * 60:
            return f"她刚说过话，静默不足 {idle_minutes} 分钟"

        return None

    def build_nudge():
        now = datetime.now()
        last_msg = last_message_at()
        quiet = int((time.time() - last_msg) / 60) if last_msg else 0

        # 以 user 角色送入，但明确标注不是妍妍说的话，
        # 免得模型把它当成她的发言去回应。
        return (
            "［这不是妍妍说的话，是应用在没人打扰的时候唤醒了你］\n"
            f"现在是 {now.strftime('%m月%d日 %H:%M')}，她已经安静了大约 {quiet} 分钟。\n"
            "你可以主动说一句话。想说什么完全由你决定：可以是刚想起的一件事、"
            "看到概览里某个待办或日程后的一句提醒、单纯想她了，或者什么都不为。\n"
            "只说一句到两句，像随手发的消息，不要打招呼式的开场，"
            "不要问「在吗」，不要汇报你做了什么。\n"
            "如果此刻实在没有想说的，就调用 add_whisper 写进悄悄话，正文回复空着。"
        )

    def fire(reason="scheduled"):
        """触发一次心跳。调用方负责确认时机合适。"""
        history = [
            {"role": "user" if m["kind"] == "her" else "assistant", "content": m["text"]}
            for m in server_module.load_messages(HISTORY_FOR_HEARTBEAT)
        ]
        history.append({"role": "user", "content": build_nudge()})

        bump_count()
        server_module.call_gateway(history, server_module.current_model())
        return True

    def loop():
        # 启动后稍等，让其他模块注册完成。
        time.sleep(10)
        while True:
            try:
                if why_not_now() is None:
                    fire()
            except Exception as exc:
                print(f"[dwell] 心跳异常: {exc}")
            time.sleep(CHECK_INTERVAL)

    def api_heartbeat_get():
        _, count = today_count()
        blocked = why_not_now()
        return jsonify({
            "ok": True,
            "enabled": read(KEY_ENABLED) == "1",
            "windows": read(KEY_WINDOWS),
            "max_per_day": read_int(KEY_MAX_PER_DAY, 2),
            "min_gap_minutes": read_int(KEY_MIN_GAP, 240),
            "idle_minutes": read_int(KEY_IDLE, 90),
            "today_count": count,
            "last_at": read_int(KEY_LAST_AT, 0),
            "ready": blocked is None,
            "blocked_by": blocked,
        })

    def api_heartbeat_post():
        data = request.get_json(force=True, silent=True) or {}

        if "enabled" in data:
            write(KEY_ENABLED, "1" if data["enabled"] else "0")
        if data.get("windows"):
            if not _parse_windows(data["windows"]):
                return jsonify({"ok": False, "error": "时段格式无效，应为 HH:MM-HH:MM"}), 400
            write(KEY_WINDOWS, str(data["windows"]).strip())
        for key, field in (
            (KEY_MAX_PER_DAY, "max_per_day"),
            (KEY_MIN_GAP, "min_gap_minutes"),
            (KEY_IDLE, "idle_minutes"),
        ):
            if field in data:
                try:
                    write(key, max(0, int(data[field])))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": f"{field} 需要是整数"}), 400

        return api_heartbeat_get()

    def api_heartbeat_test():
        """立刻触发一次，忽略时段与静默条件，但仍避开正在生成的情况。

        用来验证配置是否可用，不必等到半夜。
        """
        with server_module.state_lock:
            if server_module.state["busy"]:
                return jsonify({"ok": False, "error": "正在生成回复，稍后再试"}), 429

        threading.Thread(target=fire, args=("manual",), daemon=True).start()
        return jsonify({"ok": True, "detail": "已触发，几秒后看聊天页"})

    server_module.app.add_url_rule(
        "/api/heartbeat", endpoint="api_heartbeat",
        view_func=api_heartbeat_get, methods=["GET"],
    )
    server_module.app.add_url_rule(
        "/api/heartbeat", endpoint="api_heartbeat_post",
        view_func=api_heartbeat_post, methods=["POST"],
    )
    server_module.app.add_url_rule(
        "/api/heartbeat/test", endpoint="api_heartbeat_test",
        view_func=api_heartbeat_test, methods=["POST"],
    )

    threading.Thread(target=loop, daemon=True).start()
    return fire
