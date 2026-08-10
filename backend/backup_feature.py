"""备份：把数据库快照推到私有仓库，也能直接下载到手机。

为什么需要这个：现在 /root/dwell-backup.sh 每天三点跑一次，十五份快照
全躺在同一块盘上。磁盘容量不是瓶颈（文本一年几十 MB，18G 够很多年），
真正的缺口是这台 ECS 哪天欠费、被回收或者盘坏了——本地快照会跟机器一起消失。

只推文本库（dwell.db），图片和表情的本体留在 VPS 上：
git 对二进制没有增量优势，每天一份照片会让仓库只涨不缩，
而且照片是大头。要不要一起推另说，这里刻意不做。

推之前先 VACUUM INTO 出一份快照，不直接读 .db：
WAL 模式下正在写的库随时可能被抓到半个事务，
VACUUM INTO 走的是一致性读，出来的文件可以直接打开。

三条安全约定：
1. 令牌只存库、不入代码。这个 fork 是公开仓库，硬编码等于公开发布，
   而且 git 删文件不等于删历史（网关 token 就是这么留在历史里的）。
2. 备份仓库必须是私有的。推之前查一次 visibility，公开就拒绝，
   不然等于把全部聊天、日记、悄悄话公开出版。
3. 下载接口强制口令。后端目前整体没有鉴权，任何人知道域名就能翻聊天记录；
   但「能翻」和「一键拖走整个库」不是一个量级。没设口令就不提供下载。
"""

import base64
import gzip
import hmac
import json
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, jsonify, request


# 固定 UTC+8。服务器时区可能是 UTC，用 now() 会让「每天三点」跑在别的时刻，
# 心跳那边已经踩过一次同样的坑。
CN = timezone(timedelta(hours=8))

GITHUB_API = "https://api.github.com"

# GitHub Contents API 单文件硬限 100MB，base64 之后还要涨三分之一。
# 40MB 留足余量；文本快照压缩后远小于这个数，撞上限说明有别的问题。
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

# 后台线程多久醒一次看看该不该备份。
TICK_SECONDS = 600

DEFAULTS = {
    "enabled": False,
    "repo": "1049376904-crypto/dwell-backups",
    "branch": "main",
    "token": "",
    "hour": 3,                # 每天几点（UTC+8）
    "download_password": "",  # 空表示禁用下载接口
    "last_at": 0,
    "last_result": "",
    "last_error": "",
    "last_path": "",
    "last_bytes": 0,
    "last_ok_at": 0,
}


def cn_now():
    return datetime.now(CN)


def register_backup_feature(server_module):
    get_db = server_module.get_db
    db_path = Path(server_module.DB_PATH)
    work_dir = db_path.parent / "backup"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 自己一张表，不挤 settings：这里存的是令牌和口令，
    # 将来要单独限制读取范围时不至于跟别的配置缠在一起。
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS backup_state (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
        """)

    # ── 配置读写

    def read_all():
        with get_db() as db:
            rows = db.execute("SELECT k, v FROM backup_state").fetchall()
        stored = {r["k"]: r["v"] for r in rows}
        conf = {}
        for key, fallback in DEFAULTS.items():
            raw = stored.get(key)
            if raw is None:
                conf[key] = fallback
            elif isinstance(fallback, bool):
                conf[key] = raw == "1"
            elif isinstance(fallback, int):
                try:
                    conf[key] = int(raw)
                except ValueError:
                    conf[key] = fallback
            else:
                conf[key] = raw
        return conf

    def write(values):
        with get_db() as db:
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                if isinstance(DEFAULTS[key], bool):
                    text = "1" if value else "0"
                else:
                    text = str(value)
                db.execute(
                    "INSERT INTO backup_state (k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (key, text),
                )

    # ── 快照

    def snapshot():
        """出一份一致性快照并 gzip，返回压缩后的路径。

        VACUUM INTO 需要 SQLite 3.27+。老版本上退回 Connection.backup，
        它同样是一致性拷贝，只是不顺手做整理。
        """
        stamp = cn_now().strftime("%Y%m%d-%H%M%S")
        raw = work_dir / f"snap-{stamp}.db"
        packed = work_dir / f"snap-{stamp}.db.gz"

        for path in (raw, packed):
            path.unlink(missing_ok=True)

        source = sqlite3.connect(str(db_path))
        try:
            try:
                source.execute("VACUUM INTO ?", (str(raw),))
            except sqlite3.OperationalError:
                target = sqlite3.connect(str(raw))
                try:
                    source.backup(target)
                finally:
                    target.close()
        finally:
            source.close()

        with open(raw, "rb") as src, gzip.open(packed, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        raw.unlink(missing_ok=True)
        return packed

    def latest_snapshot():
        files = sorted(work_dir.glob("snap-*.db.gz"))
        return files[-1] if files else None

    def prune(keep=3):
        """本地只留几份。远端在 git 历史里，本地不需要屯。"""
        files = sorted(work_dir.glob("snap-*.db.gz"))
        for path in files[:-keep] if len(files) > keep else []:
            path.unlink(missing_ok=True)

    # ── GitHub

    def call_github(conf, method, path, payload=None):
        url = f"{GITHUB_API}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {conf['token']}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "dwell-backup")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as response:
            text = response.read().decode("utf-8", "replace")
        return json.loads(text) if text.strip() else {}

    def check_repo(conf):
        """确认仓库存在、可写、而且是私有的。

        私有这一条是硬门槛：往公开仓库推等于把全部聊天记录公开发布，
        而且 git 删文件不等于删历史。
        """
        info = call_github(conf, "GET", f"/repos/{conf['repo']}")
        if not info.get("private", False):
            raise ValueError(
                f"{conf['repo']} 是公开仓库，不能往里推数据。"
                "先把它改成私有，或者换一个私有仓库。"
            )
        perms = info.get("permissions") or {}
        if not perms.get("push", False):
            raise ValueError("这个令牌对该仓库没有写权限。")
        return info

    def existing_sha(conf, path):
        try:
            info = call_github(
                conf, "GET", f"/repos/{conf['repo']}/contents/{path}?ref={conf['branch']}"
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return info.get("sha") if isinstance(info, dict) else None

    def upload(conf, packed):
        size = packed.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"快照 {size // 1024 // 1024} MB，超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB "
                "的上传上限。图片没进备份，撞到这个数说明该查一下库里存了什么。"
            )

        # 按日期命名。同一天重复备份就覆盖，不留一串同日文件；
        # 历史版本 git 自己有。
        day = cn_now().strftime("%Y-%m-%d")
        path = f"snapshots/{day}.db.gz"

        content = base64.b64encode(packed.read_bytes()).decode()
        payload = {
            "message": f"快照 {cn_now().strftime('%Y-%m-%d %H:%M')} (UTC+8)",
            "content": content,
            "branch": conf["branch"],
        }
        sha = existing_sha(conf, path)
        if sha:
            payload["sha"] = sha

        call_github(conf, "PUT", f"/repos/{conf['repo']}/contents/{path}", payload)
        return path, size

    # ── 跑一次

    run_lock = threading.Lock()

    def run_backup(manual=False):
        """出快照并推上去。返回 (成功?, 说明)。"""
        if not run_lock.acquire(blocking=False):
            return False, "上一次备份还在跑"
        try:
            conf = read_all()
            if not conf["token"]:
                write({"last_result": "error", "last_error": "还没填令牌"})
                return False, "还没填令牌"
            if "/" not in conf["repo"]:
                write({"last_result": "error", "last_error": "仓库要写成 用户名/仓库名"})
                return False, "仓库要写成 用户名/仓库名"

            try:
                check_repo(conf)
                packed = snapshot()
                path, size = upload(conf, packed)
                prune()
            except ValueError as exc:
                write({"last_result": "error", "last_error": str(exc), "last_at": int(time.time())})
                return False, str(exc)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = json.loads(exc.read().decode()).get("message", "")
                except Exception:
                    pass
                # 401/403 基本都是令牌过期或权限没给够，说清楚免得来回猜。
                hint = {
                    401: "令牌无效或已过期",
                    403: "令牌权限不够（需要该仓库的 Contents 读写）",
                    404: "仓库不存在，或令牌看不到它",
                }.get(exc.code, f"GitHub 返回 {exc.code}")
                message = f"{hint}{('：' + detail) if detail else ''}"
                write({"last_result": "error", "last_error": message, "last_at": int(time.time())})
                return False, message
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                write({"last_result": "error", "last_error": message, "last_at": int(time.time())})
                return False, message

            now = int(time.time())
            write({
                "last_result": "ok",
                "last_error": "",
                "last_at": now,
                "last_ok_at": now,
                "last_path": path,
                "last_bytes": size,
            })
            return True, f"推好了 {path}（{size // 1024} KB）"
        finally:
            run_lock.release()

    # ── 定时

    def due(conf):
        """到点了没。

        判断的是「今天该跑的那个时刻已经过了，而且今天还没成功过」，
        不是「现在正好是三点」——后台线程十分钟醒一次，
        卡在整点上会因为错过窗口而整天不跑。
        """
        if not conf["enabled"] or not conf["token"]:
            return False
        now = cn_now()
        target = now.replace(hour=max(0, min(23, conf["hour"])), minute=0, second=0, microsecond=0)
        if now < target:
            return False
        last_ok = conf["last_ok_at"]
        if not last_ok:
            return True
        return datetime.fromtimestamp(last_ok, CN) < target

    def loop():
        # 启动时先睡一会儿，别和 pm2 重启挤在一起。
        time.sleep(60)
        while True:
            try:
                if due(read_all()):
                    ok, message = run_backup()
                    print(f"[dwell] 定时备份: {'成功' if ok else '失败'} {message}")
            except Exception as exc:
                print(f"[dwell] 备份线程出错: {exc}")
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True).start()

    # ── 接口

    def public_state():
        conf = read_all()
        snap = latest_snapshot()
        return {
            "ok": True,
            "enabled": conf["enabled"],
            "repo": conf["repo"],
            "branch": conf["branch"],
            "hour": conf["hour"],
            # 令牌和口令一律不回显，只报有没有配。
            "token_set": bool(conf["token"]),
            "token_tail": conf["token"][-4:] if conf["token"] else "",
            "download_enabled": bool(conf["download_password"]),
            "last_result": conf["last_result"],
            "last_error": conf["last_error"],
            "last_path": conf["last_path"],
            "last_bytes": conf["last_bytes"],
            "last_at_cn": (
                datetime.fromtimestamp(conf["last_at"], CN).strftime("%Y-%m-%d %H:%M")
                if conf["last_at"] else ""
            ),
            "last_ok_at_cn": (
                datetime.fromtimestamp(conf["last_ok_at"], CN).strftime("%Y-%m-%d %H:%M")
                if conf["last_ok_at"] else ""
            ),
            "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "local_snapshot": snap.name if snap else "",
            "local_snapshot_bytes": snap.stat().st_size if snap else 0,
            "cn_time": cn_now().strftime("%Y-%m-%d %H:%M"),
            "server_local_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def api_status():
        return jsonify(public_state())

    def api_config():
        data = request.get_json(force=True, silent=True) or {}
        values = {}

        if "enabled" in data:
            values["enabled"] = bool(data["enabled"])
        if "repo" in data:
            values["repo"] = str(data["repo"]).strip().strip("/")
        if "branch" in data:
            values["branch"] = str(data["branch"]).strip() or "main"
        if "hour" in data:
            try:
                values["hour"] = max(0, min(23, int(data["hour"])))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "小时要是 0-23 的整数"}), 400
        # 传空串表示清掉；不传则保持原样，免得存一次配置把令牌抹了。
        if "token" in data:
            values["token"] = str(data["token"]).strip()
        if "download_password" in data:
            pwd = str(data["download_password"])
            if pwd and len(pwd) < 8:
                return jsonify({"ok": False, "error": "口令至少 8 位"}), 400
            values["download_password"] = pwd

        if not values:
            return jsonify({"ok": False, "error": "没有要改的东西"}), 400
        write(values)
        return jsonify(public_state())

    def api_run():
        ok, message = run_backup(manual=True)
        payload = public_state()
        payload["ok"] = ok
        payload["message"] = message
        return jsonify(payload), (200 if ok else 400)

    def api_check():
        """只测通不通，不推东西。填完令牌先点这个。"""
        conf = read_all()
        if not conf["token"]:
            return jsonify({"ok": False, "error": "还没填令牌"}), 400
        try:
            info = check_repo(conf)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except urllib.error.HTTPError as exc:
            return jsonify({"ok": False, "error": f"GitHub 返回 {exc.code}"}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({
            "ok": True,
            "repo": info.get("full_name"),
            "private": info.get("private"),
            "size_kb": info.get("size"),
        })

    def api_download():
        """下载最新快照。

        必须带口令。后端整体没有鉴权，这个接口给的是完整数据库，
        没有口令就等于把聊天、日记、悄悄话挂在公网上任人取走。
        """
        conf = read_all()
        if not conf["download_password"]:
            return jsonify({
                "ok": False,
                "error": "没设下载口令，这个接口是关着的。去 /backup 设一个。",
            }), 403

        given = request.args.get("p", "")
        # 常数时间比较，别让响应时间泄露口令。
        if not hmac.compare_digest(given, conf["download_password"]):
            return jsonify({"ok": False, "error": "口令不对"}), 403

        fresh = request.args.get("fresh") == "1"
        snap = None
        if fresh:
            try:
                snap = snapshot()
                prune()
            except Exception as exc:
                return jsonify({"ok": False, "error": f"出快照失败: {exc}"}), 500
        else:
            snap = latest_snapshot()
            if snap is None:
                try:
                    snap = snapshot()
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"出快照失败: {exc}"}), 500

        name = f"dwell-{cn_now().strftime('%Y%m%d-%H%M')}.db.gz"
        response = Response(snap.read_bytes(), mimetype="application/gzip")
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["Cache-Control"] = "no-store"
        return response

    def panel():
        return Response(PANEL_HTML, mimetype="text/html")

    routes = [
        ("/backup", "backup_panel", panel, ["GET"]),
        ("/api/backup/status", "api_backup_status", api_status, ["GET"]),
        ("/api/backup/config", "api_backup_config", api_config, ["POST"]),
        ("/api/backup/run", "api_backup_run", api_run, ["GET", "POST"]),
        ("/api/backup/check", "api_backup_check", api_check, ["GET", "POST"]),
        ("/api/backup/download", "api_backup_download", api_download, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    server_module.backup_run = run_backup
    print(f"[dwell] 备份: {work_dir}（面板 /backup）")
    return run_backup


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>备份</title>
<style>
  :root {
    --bg: #f4f1ea; --card: #fffdf8; --fg: #26241f; --dim: #8b867b;
    --line: #e5e0d5; --field: #faf8f3; --accent: #c2603f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #161514; --card: #1f1e1c; --fg: #ece9e3; --dim: #8e8a82;
      --line: #302e2b; --field: #262523; --accent: #d97a58;
    }
  }
  * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; }
  body {
    margin: 0; padding: 22px 16px calc(40px + env(safe-area-inset-bottom));
    background: var(--bg); color: var(--fg);
    font: 15px/1.6 -apple-system, "SF Pro Text", system-ui, sans-serif;
  }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  h2 { font-size: 15px; font-weight: 600; margin: 0 0 10px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 18px; }
  .sub a { color: var(--accent); }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 16px; padding: 14px; margin-bottom: 14px;
  }
  label { display: block; font-size: 13px; color: var(--dim); margin: 10px 0 4px; }
  input[type=text], input[type=password], input[type=number] {
    width: 100%; background: var(--field); border: 1px solid var(--line);
    border-radius: 10px; color: var(--fg); padding: 9px 11px; font-size: 15px;
  }
  button {
    font: inherit; font-size: 15px; min-height: 44px; padding: 0 16px;
    border: 1px solid var(--line); border-radius: 12px;
    background: var(--field); color: var(--fg); cursor: pointer;
    margin: 6px 6px 0 0;
  }
  button.go { background: var(--accent); border-color: var(--accent); color: #fff; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .row + .row { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
  .k { font-size: 13px; color: var(--dim); }
  .v { font-size: 14px; text-align: right; word-break: break-all; }
  #msg { min-height: 20px; font-size: 13px; color: var(--dim); margin: 10px 0 0; }
  #msg.warn { color: var(--accent); }
  .note {
    font-size: 12.5px; color: var(--dim); line-height: 1.7; margin: 10px 0 0;
  }
  .danger {
    border-color: var(--accent); background: rgba(194,96,63,.07);
  }
  code {
    font-family: ui-monospace, Menlo, monospace; font-size: 12.5px;
    background: var(--field); padding: 1px 5px; border-radius: 5px;
  }
</style>
</head>
<body>
<h1>备份</h1>
<div class="sub">每天一份数据库快照推到私有仓库。<a href="/">回聊天</a></div>

<div class="card">
  <h2>现在的状态</h2>
  <div id="state"></div>
</div>

<div class="card">
  <h2>推到哪里</h2>
  <label>私有仓库（用户名/仓库名）</label>
  <input type="text" id="repo" placeholder="1049376904-crypto/dwell-backups">
  <label>分支</label>
  <input type="text" id="branch" placeholder="main">
  <label>每天几点（0-23，北京时间）</label>
  <input type="number" id="hour" min="0" max="23">
  <label>GitHub 令牌（存好之后不再显示）</label>
  <input type="password" id="token" placeholder="留空表示不改" autocomplete="off">
  <p class="note">
    细粒度令牌只勾这一个仓库的 <code>Contents: Read and write</code> 就够。
    令牌只存在服务器数据库里，不进代码仓库。
  </p>
  <button class="go" onclick="save()">保存</button>
  <button onclick="check()">测一下通不通</button>
  <button onclick="runNow()">立刻备份一次</button>
  <div class="row" style="margin-top:14px">
    <span class="k">开启每天自动备份</span>
    <span><button onclick="toggle(true)">开</button><button onclick="toggle(false)">关</button></span>
  </div>
</div>

<div class="card danger">
  <h2>下载到手机</h2>
  <p class="note" style="margin-top:0">
    这个接口给出的是完整数据库，包含全部聊天、日记和悄悄话。
    后端本身没有登录，所以必须设口令，否则接口关着。口令至少 8 位。
  </p>
  <label>下载口令（留空表示不改，填 <code>-</code> 表示清掉并关闭下载）</label>
  <input type="password" id="pwd" placeholder="至少 8 位" autocomplete="off">
  <button class="go" onclick="savePwd()">保存口令</button>
  <div id="dl"></div>
</div>

<p id="msg"></p>

<script>
var msg = document.getElementById('msg');
function say(t, warn) { msg.textContent = t || ''; msg.className = warn ? 'warn' : ''; }

function kb(n) {
  n = Number(n || 0);
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return Math.round(n / 1024) + ' KB';
  return n + ' B';
}

function line(k, v) {
  return '<div class="row"><span class="k">' + k + '</span><span class="v">' + v + '</span></div>';
}

function paint(d) {
  var last = d.last_result === 'ok'
    ? '成功 · ' + (d.last_ok_at_cn || '')
    : (d.last_result === 'error' ? '失败 · ' + (d.last_error || '') : '还没跑过');

  var html = '';
  html += line('自动备份', d.enabled ? '开着' : '关着');
  html += line('仓库', d.repo || '—');
  html += line('令牌', d.token_set ? '已存 ···' + d.token_tail : '没填');
  html += line('每天', d.hour + ' 点（北京时间）');
  html += line('上次结果', last);
  if (d.last_path) html += line('上次文件', d.last_path + ' · ' + kb(d.last_bytes));
  html += line('数据库大小', kb(d.db_bytes));
  html += line('服务器时间', d.cn_time + '（北京）');
  if (d.cn_time.slice(0, 13) !== d.server_local_time.slice(0, 13)) {
    html += line('系统本地时间', d.server_local_time + '（和北京时间不一致，已按北京时间算）');
  }
  document.getElementById('state').innerHTML = html;

  document.getElementById('repo').value = d.repo || '';
  document.getElementById('branch').value = d.branch || 'main';
  document.getElementById('hour').value = d.hour;

  document.getElementById('dl').innerHTML = d.download_enabled
    ? '<button onclick="download()">下载最新快照</button>' +
      '<button onclick="download(1)">现做一份再下载</button>'
    : '<p class="note">还没设口令，下载是关着的。</p>';
}

function load() {
  fetch('/api/backup/status').then(function (r) { return r.json(); })
    .then(paint).catch(function () { say('读不到状态', true); });
}

function post(body, done) {
  fetch('/api/backup/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok === false) { say(d.error || '出错了', true); return; }
    paint(d); if (done) done();
  }).catch(function () { say('请求失败', true); });
}

function save() {
  var body = {
    repo: document.getElementById('repo').value,
    branch: document.getElementById('branch').value,
    hour: document.getElementById('hour').value
  };
  var t = document.getElementById('token').value;
  if (t) body.token = t;
  post(body, function () {
    document.getElementById('token').value = '';
    say('存好了');
  });
}

function savePwd() {
  var p = document.getElementById('pwd').value;
  if (!p) { say('没填，什么都没改'); return; }
  post({ download_password: p === '-' ? '' : p }, function () {
    document.getElementById('pwd').value = '';
    say(p === '-' ? '下载已关闭' : '口令存好了');
  });
}

function toggle(on) { post({ enabled: on }, function () { say(on ? '开了' : '关了'); }); }

function check() {
  say('连接中…');
  fetch('/api/backup/check', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) { say(d.error || '不通', true); return; }
      say('通了：' + d.repo + (d.private ? '（私有）' : '（公开，不能用）'), !d.private);
    }).catch(function () { say('请求失败', true); });
}

function runNow() {
  say('备份中，可能要十几秒…');
  fetch('/api/backup/run', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (d) { paint(d); say(d.message || '', !d.ok); })
    .catch(function () { say('请求失败', true); });
}

function download(fresh) {
  var p = prompt('下载口令');
  if (!p) return;
  var url = '/api/backup/download?p=' + encodeURIComponent(p) + (fresh ? '&fresh=1' : '');
  location.href = url;
}

load();
</script>
</body>
</html>
"""
