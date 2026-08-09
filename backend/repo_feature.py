"""仓库时间线：把本地 git 提交记录喂给前端的「仓库」页面。

前端调用 api/repo/log?n=&skip=，期望的结构（从上游演示数据推出）：
    {ok, total, skip, items: [{h, t, s, b, f: [{s, p}]}]}
其中 h=短 hash，t=unix 时间，s=标题，b=正文，f=改动文件列表，
文件项 s=状态字母（M/A/D/R…），p=路径。

git 调用一律用列表参数，不经过 shell；n / skip 强制转成整数并限幅，
避免把外部输入拼进命令行。
"""

import subprocess
from pathlib import Path

from flask import jsonify, request


# 单条提交里最多列出的改动文件数，防止一次巨型合并把响应撑爆。
MAX_FILES_PER_COMMIT = 40
MAX_PAGE = 100

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"

LOG_FORMAT = (
    RECORD_SEP
    + FIELD_SEP.join(["%h", "%at", "%s", "%b"])
    + FIELD_SEP
)


def _git(repo_root: Path, args, timeout=10):
    return subprocess.check_output(
        ["git", "-C", str(repo_root)] + args,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def _parse_name_status(tail: str):
    """解析 --name-status 尾部，返回 [{s, p}]。"""
    files = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1].upper()
        # 重命名/复制是 R100 old new，取新路径更有意义。
        path = parts[-1]
        files.append({"s": status, "p": path})
        if len(files) >= MAX_FILES_PER_COMMIT:
            break
    return files


def register_repo_feature(server_module):
    repo_root = Path(__file__).resolve().parent.parent

    def commit_total():
        try:
            return int(_git(repo_root, ["rev-list", "--count", "HEAD"]).strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            return 0

    def read_log(count, skip):
        raw = _git(
            repo_root,
            [
                "log",
                f"--skip={skip}",
                f"-n{count}",
                f"--format={LOG_FORMAT}",
                "--name-status",
                "--no-color",
            ],
            timeout=15,
        )

        items = []
        for record in raw.split(RECORD_SEP):
            if not record.strip():
                continue
            fields = record.split(FIELD_SEP)
            if len(fields) < 4:
                continue

            short_hash = fields[0].strip()
            try:
                at = int(fields[1].strip())
            except (ValueError, IndexError):
                at = 0
            subject = fields[2].strip()
            body = fields[3].strip()
            tail = fields[4] if len(fields) > 4 else ""

            items.append({
                "h": short_hash,
                "t": at,
                "s": subject,
                "b": body,
                "f": _parse_name_status(tail),
            })
        return items

    def api_repo_log_real():
        try:
            count = max(1, min(MAX_PAGE, int(request.args.get("n", 20))))
        except (TypeError, ValueError):
            count = 20
        try:
            skip = max(0, int(request.args.get("skip", 0)))
        except (TypeError, ValueError):
            skip = 0

        if not (repo_root / ".git").exists():
            return jsonify({
                "ok": False,
                "total": 0,
                "skip": skip,
                "items": [],
                "detail": "这里不是 git 仓库",
            })

        try:
            items = read_log(count, skip)
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "total": 0, "skip": skip, "items": [],
                            "detail": "读取 git 记录超时"})
        except (subprocess.SubprocessError, OSError) as exc:
            return jsonify({"ok": False, "total": 0, "skip": skip, "items": [],
                            "detail": f"读取 git 记录失败: {exc}"})

        return jsonify({
            "ok": True,
            "total": commit_total(),
            "skip": skip,
            "items": items,
        })

    server_module.app.view_functions["api_repo_log"] = api_repo_log_real
