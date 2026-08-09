"""图片：让妍妍能给沐看东西。

设计要点：
* 图片存文件，数据库只存 markdown 文本里的路径。存进 SQLite 会让
  每天凌晨那个备份脚本迅速膨胀到几百兆。
* 前端上传前先压缩（长边 1600、JPEG 0.8）。iPhone 原图四五兆，
  1.6G 内存的机器连传几张就可能 OOM。
* 文件名是随机十六进制，猜不出来。但这只是「不容易被发现」，
  不是「访问受控」——拿到链接的人能看到图。
* 发给模型时转 base64 内联，不给链接。给链接要求上游能访问这个域名，
  多一层不确定；内联请求体大一些，但一定送得到。

消息里的图片用标准 markdown 记录：
    ![](/media/2026-08/ab12cd34ef56.jpg)
这样历史记录仍是纯文本，前端的 renderRich 本来就会渲染图片，
不需要给 messages 表加字段。
"""

import base64
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory


# 单张上限。前端已经压过，这里只是兜底。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# 发给模型时单张的上限。太大的直接跳过，免得请求体爆掉。
MAX_INLINE_BYTES = 1_500_000

# 一轮最多带几张图。上下文里堆太多图会挤掉文字。
MAX_INLINE_IMAGES = 6

ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# 匹配消息文本里的图片路径，两种写法都认：
#   ![](/media/2026-08/xxxx.jpg)
#   /media/2026-08/xxxx.jpg
MEDIA_PATTERN = re.compile(r"/media/(\d{4}-\d{2}/[0-9a-f]{8,32}\.[a-z]{3,4})")


def register_media_feature(server_module):
    root = Path(server_module.DB_PATH).parent / "uploads"
    root.mkdir(parents=True, exist_ok=True)

    def month_dir():
        name = datetime.now().strftime("%Y-%m")
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        return name, path

    def store(data: bytes, mime: str):
        ext = ALLOWED.get(mime, ".jpg")
        name, folder = month_dir()
        # 随机文件名：原始文件名可能带中文、空格，也可能泄露信息。
        stem = secrets.token_hex(6)
        target = folder / f"{stem}{ext}"

        # 先写临时文件再改名，避免半截文件被当成有效图片读出去。
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)

        return f"/media/{name}/{target.name}"

    def resolve(rel: str):
        """把 /media/... 相对路径解析成真实文件，越界返回 None。"""
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            # 路径穿越，例如 ../../etc/passwd
            return None
        return candidate if candidate.is_file() else None

    def inline_images(text: str):
        """把消息文本里的图片路径读成多模态 image_url 片段。"""
        parts = []
        for rel in MEDIA_PATTERN.findall(text or ""):
            path = resolve(rel)
            if path is None:
                continue
            try:
                if path.stat().st_size > MAX_INLINE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue

            suffix = path.suffix.lower()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
            }.get(suffix, "image/jpeg")

            encoded = base64.b64encode(raw).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })
        return parts

    def build_multimodal(history):
        """把带图片的历史消息转成多模态格式。

        只处理 user 消息里的图片：assistant 不会发图。
        没有图片的消息保持纯字符串，避免无谓地改变请求结构。
        总量超过 MAX_INLINE_IMAGES 时只留最近的几张。
        """
        # 先数一遍，从后往前保留额度。
        budget = MAX_INLINE_IMAGES
        keep = {}
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str) or "/media/" not in content:
                continue
            if budget <= 0:
                keep[index] = []
                continue
            images = inline_images(content)[:budget]
            budget -= len(images)
            keep[index] = images

        if not keep:
            return history

        rebuilt = []
        for index, message in enumerate(history):
            images = keep.get(index)
            if not images:
                rebuilt.append(message)
                continue
            text = str(message.get("content", ""))
            blocks = []
            # 文字放前面：模型先读到「这是什么」再看图，比反过来自然。
            stripped = MEDIA_PATTERN.sub("", text).replace("![]()", "").strip()
            if stripped:
                blocks.append({"type": "text", "text": stripped})
            blocks.extend(images)
            rebuilt.append({"role": message["role"], "content": blocks})
        return rebuilt

    # ── 接口

    def api_media_file(rel):
        path = resolve(rel)
        if path is None:
            return jsonify({"ok": False, "error": "找不到这张图"}), 404
        response = send_from_directory(path.parent, path.name)
        # 文件名随机且不复用，可以放心长缓存。
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def api_upload():
        uploaded = request.files.get("file") or request.files.get("image")
        if uploaded is None:
            return jsonify({"ok": False, "error": "没有收到文件"}), 400

        mime = (uploaded.mimetype or "").split(";")[0].strip().lower()
        if mime not in ALLOWED:
            return jsonify({
                "ok": False,
                "error": f"只支持 JPG / PNG / WebP / GIF，收到的是 {mime or '未知类型'}",
            }), 400

        data = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": "图片超过 8MB"}), 413
        if len(data) < 64:
            return jsonify({"ok": False, "error": "文件太小，不像图片"}), 400

        url = store(data, mime)
        return jsonify({
            "ok": True,
            "url": url,
            "markdown": f"![]({url})",
            "bytes": len(data),
        })

    def api_media_status():
        files, total = 0, 0
        for path in root.rglob("*"):
            if path.is_file() and not path.name.endswith(".part"):
                files += 1
                total += path.stat().st_size
        usage = os.statvfs(root)
        return jsonify({
            "ok": True,
            "dir": str(root),
            "files": files,
            "bytes": total,
            "disk_free_bytes": usage.f_bavail * usage.f_frsize,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_inline_images": MAX_INLINE_IMAGES,
        })

    routes = [
        ("/media/<path:rel>", "api_media_file", api_media_file, ["GET"]),
        ("/api/upload", "api_upload", api_upload, ["POST"]),
        ("/api/media/status", "api_media_status", api_media_status, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    server_module.build_multimodal = build_multimodal
    server_module.media_root = root
    print(f"[dwell] 图片目录: {root}")
    return build_multimodal
