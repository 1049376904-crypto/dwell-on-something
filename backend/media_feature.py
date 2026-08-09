"""图片与文件：让妍妍能给沐看东西。

上游前端本来就有一整套附件流程，之前没读到源码，白造了一个回形针。
现在改成对接它：

    plusBtn → addSheet → Camera / Photos / Add files
    → fCamera / fPhoto / fFile → takeFiles()
    → 图片走 shrinkImage()（长边 1568、JPEG 0.85、转 base64）塞进 attachments
    → renderStrip() 在输入框上方画缩略图
    → send() 把 {text, attachments} POST 给 api/send

所以后端要做两件事：
1. api_send 认 attachments，把 base64 图片落盘、正文补一段 markdown。
2. api/upload 认它的分块协议（裸 body + ?name=&idx=&done=），
   大文件搬进 data/files，再配一个 api/file?name= 供下载。

图片存文件、数据库只存路径：存进 SQLite 会让凌晨那个备份脚本迅速膨胀。

还有一处必须补的前端补丁。上游的图片正则写死了绝对地址：
    /!\\[[^\\]]*\\]\\((https?:\\/\\/[^\\s)]+)\\)|.../gi
我们插的是 ![](/media/2026-08/xxx.jpg)，相对路径匹配不上，
于是原样显示成一串「乱码」。见 frontend_feature 里的 IMG_RE 补丁。
"""

import base64
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory


# 单张图片上限。上游压完通常两三百 KB，这里只是兜底。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 分块上传的单块上限与总量上限。
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024

# 发给模型时单张的上限。base64 会让体积涨三分之一，
# 4MB 的图变成 5.3MB 的请求体，加上下文通常还撑得住。
MAX_INLINE_BYTES = 4 * 1024 * 1024

# 一轮最多带几张图。上下文里堆太多图会挤掉文字。
MAX_INLINE_IMAGES = 6

# 文本附件并进正文时的上限，太长会把上下文吃光。
MAX_TEXT_CHARS = 20000

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}

# 匹配消息文本里的图片路径。
MEDIA_PATTERN = re.compile(r"/media/(\d{4}-\d{2}/[0-9a-f]{8,32}\.[a-z]{3,4})")


def _safe_name(raw: str) -> str:
    """把用户给的文件名收拾干净。

    只取 basename，去掉开头的点，过滤控制字符和路径分隔符——
    别让 ../../etc/passwd 或者 .bashrc 这种东西落进柜子里。
    """
    name = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '/\\:*?"<>|')
    name = name.lstrip(".").strip()
    return name[:120] or "file"


def register_media_feature(server_module):
    base = Path(server_module.DB_PATH).parent
    images = base / "uploads"
    files = base / "files"
    parts = files / ".part"
    for path in (images, files, parts):
        path.mkdir(parents=True, exist_ok=True)

    def month_dir():
        name = datetime.now().strftime("%Y-%m")
        path = images / name
        path.mkdir(parents=True, exist_ok=True)
        return name, path

    def store_image(data: bytes, mime: str):
        ext = MIME_EXT.get(mime, ".jpg")
        name, folder = month_dir()
        # 随机文件名：原始名可能带中文、空格，也可能泄露信息。
        target = folder / f"{secrets.token_hex(6)}{ext}"

        # 先写临时文件再改名，避免半截文件被当成有效图片读出去。
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return f"/media/{name}/{target.name}"

    def resolve_image(rel: str):
        candidate = (images / rel).resolve()
        try:
            candidate.relative_to(images.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def inline_images(text: str):
        """把消息文本里的图片路径读成多模态 image_url 片段。

        跳过的图片会插一句说明：让沐知道「有张图没送过来」，
        而不是闷着装作没看见。
        """
        blocks = []
        for rel in MEDIA_PATTERN.findall(text or ""):
            path = resolve_image(rel)
            if path is None:
                blocks.append({"type": "text", "text": "［有一张图找不到了］"})
                continue
            try:
                size = path.stat().st_size
                if size > MAX_INLINE_BYTES:
                    blocks.append({
                        "type": "text",
                        "text": f"［这里有一张图，{size // 1024} KB，太大了没送过来，"
                                f"妍妍看得到，你看不到］",
                    })
                    continue
                raw = path.read_bytes()
            except OSError:
                blocks.append({"type": "text", "text": "［有一张图读不出来］"})
                continue

            mime = EXT_MIME.get(path.suffix.lower(), "image/jpeg")
            encoded = base64.b64encode(raw).decode("ascii")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })
        return blocks

    def build_multimodal(history):
        """把带图片的历史消息转成多模态格式。

        只处理 user 消息：assistant 不会发图。
        没有图片的消息保持纯字符串，避免无谓地改变请求结构。
        额度从最近的消息往前分配，老图先被挤掉。
        """
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
            blocks = inline_images(content)
            picked = []
            for block in blocks:
                if block["type"] == "image_url":
                    if budget <= 0:
                        continue
                    budget -= 1
                picked.append(block)
            keep[index] = picked

        if not keep:
            return history

        rebuilt = []
        for index, message in enumerate(history):
            blocks = keep.get(index)
            if not blocks:
                rebuilt.append(message)
                continue
            text = str(message.get("content", ""))
            # 文字放前面：模型先读到「这是什么」再看图，比反过来自然。
            stripped = MEDIA_PATTERN.sub("", text).replace("![]()", "").strip()
            merged = []
            if stripped:
                merged.append({"type": "text", "text": stripped})
            merged.extend(blocks)
            rebuilt.append({"role": message["role"], "content": merged})
        return rebuilt

    def save_attachments(items):
        """处理前端送来的 attachments，返回追加进正文的文本片段。

        上游的两种形态：
            {kind:'image', media_type:'image/jpeg', data:'<base64>', name:'x.jpg'}
            {kind:'text',  name:'a.md', text:'...'}
        """
        chunks = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()

            if kind == "image":
                raw = str(item.get("data", ""))
                if not raw:
                    continue
                # 有些客户端会连 data URL 前缀一起送过来。
                if "," in raw and raw.strip().startswith("data:"):
                    raw = raw.split(",", 1)[1]
                try:
                    binary = base64.b64decode(raw, validate=False)
                except Exception:
                    continue
                if not binary or len(binary) > MAX_IMAGE_BYTES:
                    continue
                mime = str(item.get("media_type", "image/jpeg")).lower()
                if mime not in MIME_EXT:
                    mime = "image/jpeg"
                chunks.append(f"![]({store_image(binary, mime)})")

            elif kind == "text":
                text = str(item.get("text", ""))
                if not text.strip():
                    continue
                name = _safe_name(item.get("name") or "附件.txt")
                clipped = text[:MAX_TEXT_CHARS]
                if len(text) > MAX_TEXT_CHARS:
                    clipped += "\n…（太长了，后面截断了）"
                chunks.append(f"［附件 {name}］\n{clipped}")

        return chunks

    # ── 接管 /api/send，让它认 attachments

    original_send = server_module.app.view_functions.get("api_send")

    def api_send_with_media():
        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get("text") or "").strip()
        extra = save_attachments(data.get("attachments"))

        if not text and not extra:
            return jsonify({"ok": False, "error": "没有内容"}), 400

        with server_module.state_lock:
            if server_module.state["busy"]:
                return jsonify({"ok": False, "error": "busy"}), 429

        # 图片的 markdown 放前面，跟着的文字是她对这张图说的话。
        full = "\n".join(extra + ([text] if text else []))

        server_module.save_message("her", full)
        server_module.broadcast({"type": "echo", "text": full})

        msgs = server_module.load_messages(40)
        history = [
            {"role": "user" if m["kind"] == "her" else "assistant", "content": m["text"]}
            for m in msgs
        ]

        import threading

        threading.Thread(
            target=server_module.call_gateway,
            args=(history, server_module.current_model()),
            daemon=True,
        ).start()
        return jsonify({"ok": True})

    server_module.app.view_functions["api_send"] = api_send_with_media

    # ── 分块上传（上游 bigUpload 用的协议）

    def api_upload():
        name = _safe_name(request.args.get("name") or "file")
        try:
            index = max(0, int(request.args.get("idx", 0)))
        except (TypeError, ValueError):
            index = 0
        done = str(request.args.get("done", "0")) in ("1", "true", "yes")

        blob = request.get_data(cache=False)
        if len(blob) > MAX_CHUNK_BYTES:
            return jsonify({"ok": False, "error": "单块超过 8MB"}), 413

        # 同名文件用一个稳定的桶：名字里可能有中文，先摘要一下。
        import hashlib

        bucket = parts / hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / f"{index:06d}").write_bytes(blob)

        if not done:
            return jsonify({"ok": True, "idx": index})

        pieces = sorted(bucket.glob("[0-9]" * 6))
        total = sum(p.stat().st_size for p in pieces)
        if total > MAX_FILE_BYTES:
            for p in pieces:
                p.unlink(missing_ok=True)
            bucket.rmdir()
            return jsonify({"ok": False, "error": "文件超过 200MB"}), 413

        target = files / name
        # 同名不覆盖：加一个短后缀，免得把上次搬的那份冲掉。
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = files / f"{stem}-{secrets.token_hex(2)}{suffix}"

        with target.open("wb") as out:
            for piece in pieces:
                out.write(piece.read_bytes())
                piece.unlink(missing_ok=True)
        try:
            bucket.rmdir()
        except OSError:
            pass

        # 图片顺带塞进聊天：上游走这条路的多是大图或原图。
        suffix = target.suffix.lower()
        note = f"api/file?name={target.name}"
        if suffix in EXT_MIME:
            moved = store_image(target.read_bytes(), EXT_MIME[suffix])
            note = moved

        return jsonify({
            "ok": True,
            "name": target.name,
            "bytes": total,
            "url": note,
        })

    def api_file():
        name = _safe_name(request.args.get("name") or "")
        if not name:
            return jsonify({"ok": False, "error": "缺少文件名"}), 400
        target = (files / name).resolve()
        try:
            target.relative_to(files.resolve())
        except ValueError:
            return jsonify({"ok": False, "error": "路径不对"}), 400
        if not target.is_file():
            return jsonify({"ok": False, "error": "柜子里没这个文件"}), 404
        return send_from_directory(files, target.name, as_attachment=True)

    # ── 图片托管

    def api_media_file(rel):
        path = resolve_image(rel)
        if path is None:
            return jsonify({"ok": False, "error": "找不到这张图"}), 404
        response = send_from_directory(path.parent, path.name)
        # 文件名随机且不复用，可以放心长缓存。
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def api_media_status():
        def measure(root):
            count = size = 0
            for path in root.rglob("*"):
                if path.is_file() and not path.name.endswith(".part"):
                    count += 1
                    size += path.stat().st_size
            return count, size

        img_n, img_b = measure(images)
        file_n, file_b = measure(files)
        usage = os.statvfs(base)
        return jsonify({
            "ok": True,
            "images_dir": str(images),
            "files_dir": str(files),
            "images": img_n,
            "images_bytes": img_b,
            "files": file_n,
            "files_bytes": file_b,
            "disk_free_bytes": usage.f_bavail * usage.f_frsize,
            "max_inline_bytes": MAX_INLINE_BYTES,
            "max_inline_images": MAX_INLINE_IMAGES,
        })

    routes = [
        ("/media/<path:rel>", "api_media_file", api_media_file, ["GET"]),
        ("/api/upload", "api_upload", api_upload, ["POST"]),
        ("/api/file", "api_file", api_file, ["GET"]),
        ("/api/media/status", "api_media_status", api_media_status, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    server_module.build_multimodal = build_multimodal
    server_module.media_images = images
    server_module.media_files = files
    print(f"[dwell] 图片: {images}  文件: {files}")
    return build_multimodal
