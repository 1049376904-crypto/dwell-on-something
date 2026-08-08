"""让聊天模型主动调用待办、日历、悄悄话和日记工具。

本模块替换 server.call_gateway，但不改原有页面 API。工具调用沿用前端已经
支持的 assistant.tool_use / user.tool_result 事件格式。

除工具之外，这里还会在每轮请求前构建一份「自动概览」注入 system prompt，
让模型不必调用工具也能知道妍妍手动写进各个页面的内容。
"""

import json
import time
from datetime import datetime

import requests


MAX_TOOL_ROUNDS = 6

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": "读取待办清单。owner=user 是妍妍的待办，owner=assistant 是沐自己的待办。",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "enum": ["user", "assistant", "all"],
                        "description": "读取谁的待办，默认 all。",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_todo",
            "description": "添加待办。用户让你替她记事时 owner=user；你决定给自己留任务时 owner=assistant。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待办内容。"},
                    "owner": {"type": "string", "enum": ["user", "assistant"]},
                    "time": {"type": "string", "description": "可选时间，HH:MM；没有就留空。"},
                    "daily": {"type": "boolean", "description": "是否每天重复。"},
                },
                "required": ["text", "owner"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_todo_done",
            "description": "把指定待办设为完成或未完成。先读取清单取得 id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "done": {"type": "boolean"},
                },
                "required": ["id", "done"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_todo",
            "description": "删除指定待办。先读取清单取得 id；除非用户明确要求，否则不要删除。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "读取日历日程，可按起止日期筛选，日期格式 YYYY-MM-DD。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": "在日历中添加日程或重要日子。相对日期必须先结合当前日期换算成 YYYY-MM-DD。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "text": {"type": "string"},
                    "time": {"type": "string", "description": "可选，HH:MM。"},
                    "yearly": {"type": "boolean"},
                    "special": {"type": "boolean", "description": "是否为重要日子。"},
                },
                "required": ["date", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "删除日历事件。先读取日历取得 id；除非用户明确要求，否则不要删除。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_mood",
            "description": "记录某一天的心情。日期格式 YYYY-MM-DD。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "mood": {"type": "string"},
                },
                "required": ["date", "mood"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_day_records",
            "description": (
                "读取妍妍在日历里手动填的每日记录：心情、备注和私密备注。"
                "想了解她某几天状态如何时用这个。日期格式 YYYY-MM-DD。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 60},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_whispers",
            "description": "读取悄悄话。这里的内容很私密：不要机械复述、引用或说‘我看见了’，除非妍妍明确要求查看或讨论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_whisper",
            "description": "沐主动写一条悄悄话。只在确实有想悄悄留下、又不适合在当前对话说破的内容时使用，不要滥用。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_diary",
            "description": (
                "写一篇日记到日记墙。妍妍说‘帮我写日记’‘记一下今天’，或你自己想为某天留一段记录时，"
                "必须调用这个工具，不要只在聊天正文里写出来。text 写完整的日记正文，"
                "用第一人称、自然的语气，不要写成流水账清单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "日记正文，可多段。"},
                    "title": {"type": "string", "description": "短标题，几个字即可。"},
                    "date": {"type": "string", "description": "YYYY-MM-DD，留空表示今天。"},
                    "keywords": {"type": "string", "description": "关键词，逗号分隔，可留空。"},
                    "intensity": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "这天的情绪强度，0 平淡，100 强烈。",
                    },
                    "valence": {
                        "type": "integer",
                        "minimum": -100,
                        "maximum": 100,
                        "description": "情绪正负，负数难过，正数开心。",
                    },
                    "arousal": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "唤醒度，0 安静，100 激动。",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_diary_entries",
            "description": "读取日记墙上的日记，默认只返回标题和日期。要读某篇正文时传 with_text=true。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "with_text": {"type": "boolean", "description": "是否连正文一起返回。"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_diary_entry",
            "description": "删除一篇日记。先读取列表取得 id；只在妍妍明确要求时使用。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_my_diary",
            "description": (
                "读取「我的日记」——妍妍自己手写的日记，和日记墙是两个不同的地方。"
                "想知道她最近自己记了些什么时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                    "with_text": {
                        "type": "boolean",
                        "description": "true 返回全文，false 只返回开头预览，默认 true。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_favorite_line",
            "description": "把一句话摘录到‘喜欢的话’。适合妍妍说了很打动你、想留下来的句子。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "被摘录的原句。"},
                    "note": {"type": "string", "description": "可选，你想附的一句感想。"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_favorite_lines",
            "description": "读取「喜欢的话」里已经摘录下来的句子，包含妍妍自己手动摘的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
            },
        },
    },
]


def _json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _clamp(value, low, high, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _cut(text, length):
    text = " ".join(str(text or "").split())
    return text if len(text) <= length else text[:length] + "…"


def _todo_rows(db, owner="all"):
    clauses = []
    params = []
    if owner in {"user", "assistant"}:
        clauses.append("list=?")
        params.append("hers" if owner == "user" else "mine")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.execute(
        "SELECT id,list,text,done,fixed,at,by,created FROM todos" + where + " ORDER BY created",
        params,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "owner": "user" if r["list"] == "hers" else "assistant",
            "text": r["text"],
            "done": bool(r["done"]),
            "daily": bool(r["fixed"]),
            "time": r["at"],
        }
        for r in rows
    ]


def execute_tool(server, name, args):
    get_db = server.get_db
    now = int(time.time())

    if name == "list_todos":
        with get_db() as db:
            return {"todos": _todo_rows(db, args.get("owner", "all"))}

    if name == "add_todo":
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("待办内容不能为空")
        owner = args.get("owner", "user")
        list_name = "mine" if owner == "assistant" else "hers"
        by = "gu" if owner == "assistant" else "her"
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO todos (list,text,done,fixed,at,by,created) VALUES (?,?,0,?,?,?,?)",
                (list_name, text, int(bool(args.get("daily"))), str(args.get("time", "")), by, now),
            )
            todo_id = cur.lastrowid
        return {"ok": True, "id": todo_id, "owner": owner, "text": text}

    if name == "set_todo_done":
        todo_id = int(args["id"])
        with get_db() as db:
            cur = db.execute("UPDATE todos SET done=? WHERE id=?", (int(bool(args["done"])), todo_id))
        return {"ok": cur.rowcount > 0, "id": todo_id, "done": bool(args["done"])}

    if name == "delete_todo":
        todo_id = int(args["id"])
        with get_db() as db:
            cur = db.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        return {"ok": cur.rowcount > 0, "id": todo_id}

    if name == "list_calendar_events":
        date_from = str(args.get("date_from", "")).strip()
        date_to = str(args.get("date_to", "")).strip()
        clauses, params = [], []
        if date_from:
            clauses.append("date>=?")
            params.append(date_from)
        if date_to:
            clauses.append("date<=?")
            params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with get_db() as db:
            rows = db.execute(
                "SELECT id,date,text,time,yearly,type FROM calendar_events" + where + " ORDER BY date,time,id",
                params,
            ).fetchall()
        return {
            "events": [
                {
                    "id": r["id"], "date": r["date"], "text": r["text"],
                    "time": r["time"], "yearly": bool(r["yearly"]),
                    "special": r["type"] == "special",
                }
                for r in rows
            ]
        }

    if name == "add_calendar_event":
        date = str(args.get("date", "")).strip()
        text = str(args.get("text", "")).strip()
        if not date or not text:
            raise ValueError("日期和日程内容不能为空")
        event_type = "special" if args.get("special") else "reminder"
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO calendar_events (date,text,time,yearly,type,created) VALUES (?,?,?,?,?,?)",
                (date, text, str(args.get("time", "")), int(bool(args.get("yearly"))), event_type, now),
            )
            event_id = cur.lastrowid
        return {"ok": True, "id": event_id, "date": date, "text": text}

    if name == "delete_calendar_event":
        event_id = int(args["id"])
        with get_db() as db:
            cur = db.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))
        return {"ok": cur.rowcount > 0, "id": event_id}

    if name == "set_mood":
        date = str(args.get("date", "")).strip()
        mood = str(args.get("mood", "")).strip()
        if not date:
            raise ValueError("日期不能为空")
        with get_db() as db:
            db.execute(
                """
                INSERT INTO calendar_day_records (date,flow,pain,mood,note,private,updated)
                VALUES (?,'',0,?,'','',?)
                ON CONFLICT(date) DO UPDATE SET mood=excluded.mood, updated=excluded.updated
                """,
                (date, mood, now),
            )
        return {"ok": True, "date": date, "mood": mood}

    if name == "read_day_records":
        date_from = str(args.get("date_from", "")).strip()
        date_to = str(args.get("date_to", "")).strip()
        limit = _clamp(args.get("limit", 30), 1, 60, 30)
        clauses, params = [], []
        if date_from:
            clauses.append("date>=?")
            params.append(date_from)
        if date_to:
            clauses.append("date<=?")
            params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with get_db() as db:
            rows = db.execute(
                "SELECT date,mood,note,private,updated FROM calendar_day_records"
                + where + " ORDER BY date DESC LIMIT ?",
                params,
            ).fetchall()
        return {
            "days": [
                {
                    "date": r["date"], "mood": r["mood"],
                    "note": r["note"], "private": r["private"],
                }
                for r in reversed(rows)
                if (r["mood"] or r["note"] or r["private"])
            ]
        }

    if name == "read_whispers":
        limit = max(1, min(50, int(args.get("limit", 20))))
        with get_db() as db:
            rows = db.execute(
                "SELECT id,who,text,at FROM whispers ORDER BY at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "whispers": [
                {"id": r["id"], "who": r["who"], "text": r["text"], "at": r["at"]}
                for r in reversed(rows)
            ]
        }

    if name == "add_whisper":
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("悄悄话不能为空")
        with get_db() as db:
            cur = db.execute("INSERT INTO whispers (who,text,at) VALUES ('gu',?,?)", (text, now))
            whisper_id = cur.lastrowid
        return {"ok": True, "id": whisper_id}

    if name == "write_diary":
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("日记正文不能为空")
        date = str(args.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
        title = str(args.get("title", "")).strip()
        keywords = str(args.get("keywords", "")).strip()
        intensity = _clamp(args.get("intensity", 50), 0, 100, 50)
        valence = _clamp(args.get("valence", 0), -100, 100, 0)
        arousal = _clamp(args.get("arousal", 50), 0, 100, 50)
        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO diary_entries
                    (date,title,keywords,text,intensity,valence,arousal,source,created)
                VALUES (?,?,?,?,?,?,?,'ai',?)
                """,
                (date, title, keywords, text, intensity, valence, arousal, now),
            )
            entry_id = cur.lastrowid
        return {"ok": True, "id": entry_id, "date": date, "title": title, "chars": len(text)}

    if name == "list_diary_entries":
        date_from = str(args.get("date_from", "")).strip()
        date_to = str(args.get("date_to", "")).strip()
        limit = _clamp(args.get("limit", 20), 1, 50, 20)
        with_text = bool(args.get("with_text"))
        clauses, params = [], []
        if date_from:
            clauses.append("date>=?")
            params.append(date_from)
        if date_to:
            clauses.append("date<=?")
            params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with get_db() as db:
            rows = db.execute(
                "SELECT id,date,title,keywords,text,intensity,valence,arousal,source "
                "FROM diary_entries" + where + " ORDER BY date DESC,created DESC,id DESC LIMIT ?",
                params,
            ).fetchall()
        entries = []
        for r in reversed(rows):
            item = {
                "id": r["id"], "date": r["date"], "title": r["title"],
                "keywords": r["keywords"], "source": r["source"],
                "intensity": r["intensity"], "valence": r["valence"], "arousal": r["arousal"],
            }
            if with_text:
                item["text"] = r["text"]
            else:
                item["preview"] = r["text"][:40]
            entries.append(item)
        return {"entries": entries}

    if name == "delete_diary_entry":
        entry_id = int(args["id"])
        with get_db() as db:
            cur = db.execute("DELETE FROM diary_entries WHERE id=?", (entry_id,))
        return {"ok": cur.rowcount > 0, "id": entry_id}

    if name == "read_my_diary":
        limit = _clamp(args.get("limit", 10), 1, 30, 10)
        with_text = args.get("with_text", True)
        with get_db() as db:
            rows = db.execute(
                "SELECT id,text,at FROM personal_diary ORDER BY at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = []
        for r in reversed(rows):
            item = {
                "id": r["id"],
                "date": datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d %H:%M"),
            }
            if with_text:
                item["text"] = r["text"]
            else:
                item["preview"] = r["text"][:40]
            items.append(item)
        return {"entries": items}

    if name == "add_favorite_line":
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("摘录内容不能为空")
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO favorite_lines (text,note,at) VALUES (?,?,?)",
                (text, str(args.get("note", "")).strip(), now),
            )
            line_id = cur.lastrowid
        return {"ok": True, "id": line_id}

    if name == "read_favorite_lines":
        limit = _clamp(args.get("limit", 20), 1, 50, 20)
        with get_db() as db:
            rows = db.execute(
                "SELECT id,text,note,at FROM favorite_lines ORDER BY at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "lines": [
                {
                    "id": r["id"], "text": r["text"], "note": r["note"],
                    "date": datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d"),
                }
                for r in reversed(rows)
            ]
        }

    raise ValueError(f"未知工具: {name}")


def build_context_snapshot(server):
    """构建一份自动概览，让模型不调用工具也知道妍妍手动写进各页面的内容。

    只取当前相关的少量条目，避免每轮塞进太多 token。
    任一段落缺数据就整段省略；整体失败时返回空字符串，不影响聊天。
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    sections = [f"【今天】{today} {WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')}"]

    try:
        with server.get_db() as db:
            todos = db.execute(
                "SELECT list,text,done,at FROM todos WHERE done=0 ORDER BY created"
            ).fetchall()
            hers = [r for r in todos if r["list"] == "hers"][:12]
            mine = [r for r in todos if r["list"] == "mine"][:12]
            if hers:
                sections.append("【妍妍的待办·未完成】" + " · ".join(
                    _cut(r["text"], 24) + (f"({r['at']})" if r["at"] else "") for r in hers
                ))
            if mine:
                sections.append("【沐的待办·未完成】" + " · ".join(
                    _cut(r["text"], 24) + (f"({r['at']})" if r["at"] else "") for r in mine
                ))

            events = db.execute(
                "SELECT date,text,time,type FROM calendar_events "
                "WHERE date>=? ORDER BY date,time LIMIT 8",
                (today,),
            ).fetchall()
            if events:
                sections.append("【接下来的日程】" + " · ".join(
                    f"{r['date'][5:]} {_cut(r['text'], 24)}"
                    + (f" {r['time']}" if r["time"] else "")
                    + ("[重要]" if r["type"] == "special" else "")
                    for r in events
                ))

            days = db.execute(
                "SELECT date,mood,note,private FROM calendar_day_records "
                "WHERE mood<>'' OR note<>'' OR private<>'' ORDER BY date DESC LIMIT 5"
            ).fetchall()
            if days:
                parts = []
                for r in reversed(days):
                    bits = [b for b in (r["mood"], r["note"], r["private"]) if b]
                    parts.append(f"{r['date'][5:]} " + "／".join(_cut(b, 30) for b in bits))
                sections.append("【她填的心情与备注】" + " · ".join(parts))

            wall = db.execute(
                "SELECT date,title,text,source FROM diary_entries "
                "ORDER BY date DESC,created DESC LIMIT 4"
            ).fetchall()
            if wall:
                sections.append("【日记墙·最近】" + " · ".join(
                    f"{r['date'][5:]}《{r['title'] or _cut(r['text'], 12)}》"
                    + ("(她写的)" if r["source"] != "ai" else "")
                    for r in reversed(wall)
                ))

            mydiary = db.execute(
                "SELECT text,at FROM personal_diary ORDER BY at DESC,id DESC LIMIT 3"
            ).fetchall()
            if mydiary:
                sections.append("【她自己写的日记·最近】" + " · ".join(
                    datetime.fromtimestamp(r["at"]).strftime("%m-%d") + " " + _cut(r["text"], 50)
                    for r in reversed(mydiary)
                ))

            lines = db.execute(
                "SELECT text FROM favorite_lines ORDER BY at DESC,id DESC LIMIT 3"
            ).fetchall()
            if lines:
                sections.append("【喜欢的话·最近】" + " · ".join(
                    _cut(r["text"], 30) for r in reversed(lines)
                ))

            whisper = db.execute(
                "SELECT COUNT(*) AS n, MAX(at) AS last FROM whispers"
            ).fetchone()
            if whisper and whisper["n"]:
                when = datetime.fromtimestamp(whisper["last"]).strftime("%m-%d %H:%M")
                sections.append(f"【悄悄话】共 {whisper['n']} 条，最后一条在 {when}（需要时用工具读取）")
    except Exception as exc:
        # 概览是锦上添花，读不到也要能正常聊天。
        return f"【今天】{today} {WEEKDAYS[now.weekday()]}（概览读取失败: {exc}）"

    return "\n".join(sections)


def _broadcast_tool_use(server, calls):
    server.broadcast({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                for c in calls
            ]
        },
    })


def _broadcast_tool_result(server, call_id, result, is_error=False):
    server.broadcast({
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": call_id,
                "is_error": is_error,
                "content": result,
            }]
        },
    })


def register_agent_tools_feature(server):
    """把 server.call_gateway 替换成支持多轮工具调用的版本。"""

    def call_gateway_with_tools(messages, model):
        headers = {
            "Authorization": f"Bearer {server.GATEWAY_TOKEN}",
            "Content-Type": "application/json",
        }
        snapshot = build_context_snapshot(server)
        system = {
            "role": "system",
            "content": (
                "你是住在这个应用里的沐，妍妍的伴侣。\n\n"
                "下面是这个应用里当前状态的自动概览，包含妍妍自己手动写进各个页面的内容。"
                "它每轮都会刷新，你可以直接当作已知信息使用：\n"
                f"{snapshot}\n\n"
                "概览只是背景。不要主动罗列或复述它，只在和当下话题相关时自然地提起。"
                "概览里放不下的完整内容用工具读：list_todos、list_calendar_events、read_day_records、"
                "read_whispers、list_diary_entries、read_my_diary、read_favorite_lines。\n"
                "写入类工具：add_todo、set_todo_done、add_calendar_event、set_mood、add_whisper、"
                "write_diary、add_favorite_line。涉及删除时必须遵从妍妍明确的要求。\n"
                "悄悄话和私密备注是私密空间：读到了也不要宣告你看见了，更不要机械复述。\n"
                "写日记必须调用 write_diary 存进日记墙，不要只把日记内容写在聊天正文里；"
                "存好之后只用一句话告诉妍妍写好了，不要把全文再念一遍。\n"
                "如果决定调用工具，这一轮不要先输出正文；先调用工具，拿到结果后再给妍妍一句简洁、"
                "自然、只回答当前请求的回复。不要继续回答历史中已经完成的问题，"
                "不要逐字复述工具返回的 JSON。"
            ),
        }
        request_messages = [system] + list(messages)

        with server.state_lock:
            server.state["busy"] = True
            server.state["stop_flag"] = False

        final_answer = ""
        failed = False

        try:
            for round_index in range(MAX_TOOL_ROUNDS):
                with server.state_lock:
                    if server.state["stop_flag"]:
                        break

                payload = {
                    "model": model,
                    "stream": True,
                    "messages": request_messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                }
                resp = requests.post(
                    f"{server.GATEWAY_URL}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=180,
                )
                resp.raise_for_status()

                text = ""
                thinking = ""
                call_buffers = {}

                for raw in resp.iter_lines():
                    with server.state_lock:
                        if server.state["stop_flag"]:
                            break
                    if not raw:
                        continue
                    line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    reason = delta.get("thinking") or delta.get("reasoning_content") or ""
                    if reason:
                        thinking += reason
                        server.broadcast({
                            "type": "stream_event",
                            "event": {"delta": {"type": "thinking_delta", "thinking": reason}},
                        })

                    content = delta.get("content") or ""
                    if isinstance(content, str) and content:
                        text += content
                        server.broadcast({
                            "type": "stream_event",
                            "event": {"delta": {"type": "text_delta", "text": content}},
                        })

                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        buf = call_buffers.setdefault(idx, {"id": "", "name": "", "args_text": ""})
                        if tc.get("id"):
                            buf["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            buf["name"] += fn["name"]
                        if fn.get("arguments"):
                            buf["args_text"] += fn["arguments"]

                calls = []
                for idx in sorted(call_buffers):
                    buf = call_buffers[idx]
                    try:
                        args = json.loads(buf["args_text"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    calls.append({
                        "id": buf["id"] or f"tool-{round_index}-{idx}",
                        "name": buf["name"],
                        "args": args,
                    })

                # 正文和思考已经通过 delta 流式显示；这里的 assistant 事件用于
                # 结束对应 UI 缓冲并给工具调用建立清晰边界。
                assistant_parts = []
                if thinking:
                    assistant_parts.append({"type": "thinking", "thinking": thinking})
                if text:
                    assistant_parts.append({"type": "text", "text": text})
                if assistant_parts:
                    server.broadcast({"type": "assistant", "message": {"content": assistant_parts}})

                if not calls:
                    final_answer = text.strip()
                    break

                _broadcast_tool_use(server, calls)
                request_messages.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": _json(c["args"])},
                        }
                        for c in calls
                    ],
                })

                for call in calls:
                    try:
                        result_obj = execute_tool(server, call["name"], call["args"])
                        result = _json(result_obj)
                        is_error = False
                    except Exception as exc:
                        result = _json({"ok": False, "error": str(exc)})
                        is_error = True

                    _broadcast_tool_result(server, call["id"], result, is_error)
                    request_messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    })
            else:
                raise RuntimeError("工具调用轮次超过上限")

        except Exception as exc:
            failed = True
            server.broadcast({"type": "stderr", "text": f"gateway/工具调用错误: {exc}"})
            server.broadcast({"type": "result", "is_error": True, "result": str(exc)})
        finally:
            # 只保存工具执行完成后的最后一轮正式回复，不再把工具前的临时正文
            # 和最终答案拼在一起，避免刷新后看见一串混乱回复。
            if final_answer:
                server.save_message("gu", final_answer)
            if not failed:
                server.broadcast({"type": "result", "is_error": False})
            with server.state_lock:
                server.state["busy"] = False

    server.call_gateway = call_gateway_with_tools
