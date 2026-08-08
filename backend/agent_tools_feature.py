"""让聊天模型主动调用待办、日历、悄悄话和日记工具。

本模块替换 server.call_gateway，但不改原有页面 API。工具调用沿用前端已经
支持的 assistant.tool_use / user.tool_result 事件格式。
"""

import json
import time
from datetime import datetime

import requests


MAX_TOOL_ROUNDS = 6

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
]


def _json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _clamp(value, low, high, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


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

    raise ValueError(f"未知工具: {name}")


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
        today = time.strftime("%Y-%m-%d %H:%M:%S %A", time.localtime())
        system = {
            "role": "system",
            "content": (
                f"当前服务器本地时间是 {today}。你是住在这个应用里的沐。"
                "你可以按需调用待办、日历、悄悄话和日记工具。"
                "涉及删除时必须遵从用户明确要求。"
                "悄悄话是私密空间：不要因读到它就机械复述或宣告你看见了。"
                "写日记必须调用 write_diary 工具存进日记墙，不要只把日记内容写在聊天正文里；"
                "存好之后只用一句话告诉妍妍写好了，不要把全文再念一遍。"
                "如果决定调用工具，这一轮不要先输出正文；先调用工具，拿到结果后再给妍妍一句简洁、自然、只回答当前请求的回复。"
                "不要继续回答历史中已经完成的问题，不要逐字复述工具返回的 JSON。"
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
