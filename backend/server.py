#!/usr/bin/env python3
"""
dwell-backend
对接 haven-gateway，为 dwell-on-something 前端提供聊天核心接口。

启动：
  cd backend
  pip install flask flask-cors requests
  python3 server.py
"""

import json
import os
import sqlite3
import threading
import time
import queue
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── 配置 ────────────────────────────────────────────────────────
GATEWAY_URL   = os.getenv("GATEWAY_URL",   "http://127.0.0.1:18003")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "sk-ebb1179c1f074daeb406c80efe203aca")
DEFAULT_MODEL = os.getenv("MODEL",         "claude-sonnet-4-5")
PORT          = int(os.getenv("PORT",       "8888"))
DB_PATH       = Path(os.getenv("DB_PATH",   "dwell.db"))

# ── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── 数据库 ─────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                seq   INTEGER PRIMARY KEY AUTOINCREMENT,
                kind  TEXT    NOT NULL,
                text  TEXT    NOT NULL,
                at    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES ('model', ?)",
            (DEFAULT_MODEL,)
        )

init_db()

# ── 全局状态 ─────────────────────────────────────────────────────────────────
state = {"busy": False, "stop_flag": False, "since": 0}
state_lock = threading.Lock()
subscribers: list = []
subs_lock = threading.Lock()

def broadcast(event: dict):
    with subs_lock:
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                subscribers.remove(q)
            except ValueError:
                pass

# ── 工具 ────────────────────────────────────────────────────────────────────
def current_model():
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='model'").fetchone()
        return row["value"] if row else DEFAULT_MODEL

def save_message(kind, text):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO messages (kind, text, at) VALUES (?, ?, ?)",
            (kind, text, int(time.time()))
        )
        return cur.lastrowid

def load_messages(limit=400, before=None):
    with get_db() as db:
        if before:
            rows = db.execute(
                "SELECT seq,kind,text,at FROM messages WHERE seq<? ORDER BY seq DESC LIMIT ?",
                (before, limit)
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = db.execute(
                "SELECT seq,kind,text,at FROM messages ORDER BY seq DESC LIMIT ?",
                (limit,)
            ).fetchall()
            rows = list(reversed(rows))
        return [{"seq": r["seq"], "kind": r["kind"], "text": r["text"], "at": r["at"]} for r in rows]

# ── 调 gateway 流式生成 ─────────────────────────────────────────────────
def call_gateway(messages, model):
    headers = {
        "Authorization": f"Bearer {GATEWAY_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {"model": model, "stream": True, "messages": messages}

    with state_lock:
        state["busy"]      = True
        state["stop_flag"] = False

    full_text  = ""
    think_text = ""

    try:
        resp = requests.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            headers=headers, json=payload, stream=True, timeout=120
        )
        resp.raise_for_status()

        for raw in resp.iter_lines():
            with state_lock:
                if state["stop_flag"]:
                    break
            if not raw:
                continue
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            thinking = delta.get("thinking", "")
            if thinking:
                think_text += thinking
                broadcast({"type": "stream_event",
                           "event": {"delta": {"type": "thinking_delta", "thinking": thinking}}})
                continue

            content = delta.get("content", "")
            if content:
                full_text += content
                broadcast({"type": "stream_event",
                           "event": {"delta": {"type": "text_delta", "text": content}}})

    except Exception as e:
        broadcast({"type": "stderr", "text": f"gateway 错误: {e}"})
        broadcast({"type": "result", "is_error": True, "result": str(e)})
        with state_lock:
            state["busy"] = False
        return

    # 生成完成
    content_parts = []
    if think_text:
        content_parts.append({"type": "thinking", "thinking": think_text})
    if full_text:
        content_parts.append({"type": "text", "text": full_text})
    broadcast({"type": "assistant", "message": {"content": content_parts}})

    if full_text:
        save_message("gu", full_text)

    broadcast({"type": "result", "is_error": False})
    with state_lock:
        state["busy"] = False

# ── 接口 ─────────────────────────────────────────────────────────────────────

@app.get("/api/messages")
@app.get("/api/said")
def api_messages():
    limit  = int(request.args.get("limit", 400))
    before = request.args.get("before")
    msgs   = load_messages(limit, int(before) if before else None)
    upto   = msgs[-1]["seq"] if msgs else 0
    return jsonify({"msgs": msgs, "upto": upto, "more": False})


@app.post("/api/send")
def api_send():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False}), 400
    with state_lock:
        if state["busy"]:
            return jsonify({"ok": False, "error": "busy"}), 429

    save_message("her", text)
    broadcast({"type": "echo", "text": text})

    msgs    = load_messages(40)
    history = [{"role": "user" if m["kind"] == "her" else "assistant",
                "content": m["text"]} for m in msgs]
    model   = current_model()

    threading.Thread(target=call_gateway, args=(history, model), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    with state_lock:
        state["stop_flag"] = True
    broadcast({"type": "system", "subtype": "stopped"})
    return jsonify({"ok": True})


@app.get("/api/poll")
def api_poll():
    q = queue.Queue(maxsize=64)
    with subs_lock:
        subscribers.append(q)

    events   = []
    deadline = time.time() + 25
    try:
        while time.time() < deadline:
            try:
                ev = q.get(timeout=min(1.0, deadline - time.time()))
                events.append(ev)
                while not q.empty() and len(events) < 32:
                    events.append(q.get_nowait())
                break
            except queue.Empty:
                continue
    finally:
        with subs_lock:
            try:
                subscribers.remove(q)
            except ValueError:
                pass

    with state_lock:
        cur = state["since"]
        state["since"] = cur + len(events)

    return jsonify({"events": events, "next": cur + len(events)})


@app.get("/api/model")
def api_get_model():
    return jsonify({"model": current_model(), "effort": "high"})


@app.post("/api/model")
def api_set_model():
    data  = request.get_json(force=True)
    model = (data.get("model") or "").strip()
    if model:
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('model',?)", (model,))
        broadcast({"type": "system", "subtype": "model", "model": model})
    return jsonify({"ok": True, "model": current_model()})


@app.get("/api/status")
def api_status():
    with state_lock:
        busy = state["busy"]
    return jsonify({"alive": True, "busy": busy, "since": int(time.time())})


@app.get("/api/context")
def api_context():
    msgs   = load_messages(400)
    used   = sum(len(m["text"]) for m in msgs) // 4
    window = 200000
    pct    = min(100, round(used / window * 100))
    return jsonify({"ok": True, "used": used, "window": window, "pct": pct, "model": current_model()})


@app.get("/api/chats")
def api_chats():
    return jsonify({"items": [{"id": "main", "name": "Claude",
                               "current": True, "last": int(time.time()), "preview": ""}]})


# 其余 stub
@app.post("/api/newchat")  
def api_newchat():     return jsonify({"ok": True})
@app.post("/api/chats")    
def api_chats_post():  return jsonify({"ok": True})
@app.get("/api/usage")     
def api_usage():       return jsonify({"ok": False})
@app.get("/api/cal")       
def api_cal():         return jsonify({"ok": False})
@app.post("/api/cal")      
def api_cal_post():    return jsonify({"ok": True})
@app.get("/api/wall")      
def api_wall():        return jsonify({"ok": False})
@app.get("/api/todos")     
def api_todos():       return jsonify({"ok": True, "mine": [], "hers": []})
@app.post("/api/todos")    
def api_todos_post():  return jsonify({"ok": True, "mine": [], "hers": []})
@app.get("/api/news")      
def api_news():        return jsonify({"ok": False})
@app.get("/api/nook/books")
def api_nook():        return jsonify([])
@app.get("/api/health")    
def api_health():      return jsonify({"ok": False})
@app.get("/api/repo/log")  
def api_repo_log():    return jsonify({"ok": False, "items": []})
@app.get("/api/whisper")   
def api_whisper():     return jsonify({"items": []})
@app.post("/api/whisper")  
def api_whisper_post(): return jsonify({"ok": True})
@app.get("/api/notes")     
def api_notes():       return jsonify({"gu": [], "her": []})
@app.post("/api/notes")    
def api_notes_post():  return jsonify({"gu": [], "her": []})
@app.get("/api/dreams")    
def api_dreams():      return jsonify({"items": []})
@app.get("/api/night")     
def api_night():       return jsonify({"days": []})
@app.get("/api/music")     
def api_music():       return jsonify({"ok": False})
@app.get("/api/gong")      
def api_gong():        return jsonify({"msgs": []})
@app.post("/api/gong")     
def api_gong_post():   return jsonify({"reply": ""})
@app.get("/api/find")      
def api_find():        return jsonify({"ok": True, "hits": []})
@app.get("/api/herdiary")  
def api_herdiary():    return jsonify({"items": []})
@app.post("/api/herdiary") 
def api_herdiary_post(): return jsonify({"ok": True})
@app.get("/api/favlines")  
def api_favlines():    return jsonify({"ok": True, "text": ""})
@app.get("/api/authmode")  
def api_authmode():    return jsonify({"mode": "subscription"})

# ── 启动 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"dwell-backend 启动")
    print(f"  gateway : {GATEWAY_URL}")
    print(f"  model   : {DEFAULT_MODEL}")
    print(f"  port    : {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
