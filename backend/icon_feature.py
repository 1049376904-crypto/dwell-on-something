"""应用图标：把妍妍指定的图片抓下来，自己托管成主屏和通知图标。

为什么不直接在 manifest 里写图床地址：
* 图床可能限时删除、换域名、挡外链，图标会某天悄悄变回灰色占位。
* iOS 会在添加到主屏那一刻去拉图标，网络抖一下就拿不到。
所以抓一次存在本地 data/icons 下，之后都由后端自己发。

Pillow 是可选依赖。装了就按 180/192/512 三档裁成 PNG；没装就原图直发，
manifest 里的 sizes 退化成 "any"，功能不受影响只是稍大一点。

图片来源存在 settings 表（app_icon_url），换图不用改代码：
    POST /api/icon/url  {"url": "https://..."}
    POST /api/icon/refresh
"""

import threading
import time
from pathlib import Path

import requests
from flask import Response, jsonify, request


KEY_ICON_URL = "app_icon_url"

# 妍妍 2026-08-01 上传的那张，1718x1718。
DEFAULT_ICON_URL = "https://s41.ax1x.com/2026/08/01/pm4eDAK.jpg"

# 图标尺寸：180 给 iOS apple-touch-icon，192/512 给 PWA manifest。
SIZES = (180, 192, 512)

# 下载上限。图标不该有这么大，超过就是拿错东西了。
MAX_BYTES = 8 * 1024 * 1024

ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp")


def _pillow():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def register_icon_feature(server_module):
    # 跟数据库放一起，storage_feature 已经把 DB_PATH 固定成绝对路径。
    cache_dir = Path(server_module.DB_PATH).parent / "icons"
    cache_dir.mkdir(parents=True, exist_ok=True)
    source_path = cache_dir / "source"

    get_db = server_module.get_db
    lock = threading.Lock()
    state = {"ready": source_path.exists(), "error": "", "fetched_at": 0, "mime": ""}

    def read_url():
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (KEY_ICON_URL,)
            ).fetchone()
        return (row["value"] if row is not None else "") or DEFAULT_ICON_URL

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            (KEY_ICON_URL, DEFAULT_ICON_URL),
        )

    def fetch(force=False):
        """抓源图存本地。已经有了就直接返回，除非 force。"""
        with lock:
            if source_path.exists() and not force:
                state["ready"] = True
                return True

            url = read_url()
            try:
                resp = requests.get(url, timeout=25, stream=True)
                resp.raise_for_status()

                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                if content_type and content_type not in ALLOWED_TYPES:
                    raise ValueError(f"返回的不是图片: {content_type}")

                chunks, total = [], 0
                for chunk in resp.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("图片超过 8MB，没有继续下载")
                    chunks.append(chunk)

                data = b"".join(chunks)
                if len(data) < 512:
                    raise ValueError("下载到的内容太小，不像图片")

                # 先写临时文件再改名，避免半截文件被当成有效缓存。
                tmp = source_path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(source_path)

                # 换了源图，之前渲染的各档尺寸全部作废。
                for size in SIZES:
                    rendered = cache_dir / f"icon-{size}.png"
                    if rendered.exists():
                        rendered.unlink()

                state.update({
                    "ready": True,
                    "error": "",
                    "fetched_at": int(time.time()),
                    "mime": content_type or "image/jpeg",
                })
                print(f"[dwell] 应用图标已抓取: {url} ({len(data)} 字节)")
                return True

            except Exception as exc:
                state["ready"] = source_path.exists()
                state["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                print(f"[dwell] 应用图标抓取失败: {state['error']}")
                return False

    def render(size):
        """返回指定尺寸的图标字节和 mimetype。

        Pillow 缺席时原图直发：浏览器会自己缩放，不至于没图标。
        """
        if not source_path.exists() and not fetch():
            return None, ""

        Image = _pillow()
        if Image is None:
            return source_path.read_bytes(), state.get("mime") or "image/jpeg"

        target = cache_dir / f"icon-{size}.png"
        if target.exists():
            return target.read_bytes(), "image/png"

        try:
            with Image.open(source_path) as img:
                img = img.convert("RGB")
                # 非正方形先居中裁一刀，免得图标被压变形。
                width, height = img.size
                if width != height:
                    edge = min(width, height)
                    left = (width - edge) // 2
                    top = (height - edge) // 2
                    img = img.crop((left, top, left + edge, top + edge))
                img = img.resize((size, size), Image.LANCZOS)
                img.save(target, "PNG", optimize=True)
        except Exception as exc:
            state["error"] = f"渲染失败: {str(exc)[:160]}"
            return source_path.read_bytes(), state.get("mime") or "image/jpeg"

        return target.read_bytes(), "image/png"

    # ── 对外给别的模块用

    def manifest_entries():
        """manifest.json 的 icons 段。"""
        if not source_path.exists():
            return []
        has_pillow = _pillow() is not None
        return [
            {
                "src": f"/icons/icon-{size}.png",
                # 没有 Pillow 时实际尺寸不是这个，如实写 any 而不是撒谎。
                "sizes": f"{size}x{size}" if has_pillow else "any",
                "type": "image/png" if has_pillow else (state.get("mime") or "image/jpeg"),
                "purpose": "any",
            }
            for size in (192, 512)
        ]

    def html_links():
        """注入页面 head 的图标链接。iOS 认的是 apple-touch-icon。"""
        return (
            '  <link rel="apple-touch-icon" sizes="180x180" href="/icons/icon-180.png">\n'
            '  <link rel="icon" type="image/png" sizes="192x192" href="/icons/icon-192.png">\n'
        )

    # ── 路由

    def api_icon(size):
        try:
            wanted = int(size)
        except (TypeError, ValueError):
            wanted = 192
        if wanted not in SIZES:
            wanted = min(SIZES, key=lambda s: abs(s - wanted))

        data, mime = render(wanted)
        if not data:
            return jsonify({"ok": False, "error": state["error"] or "图标尚未就绪"}), 404

        response = Response(data, mimetype=mime)
        # 图标很少变，但换图后要能及时生效，给一天。
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    def api_icon_status():
        return jsonify({
            "ok": True,
            "url": read_url(),
            "cached": source_path.exists(),
            "bytes": source_path.stat().st_size if source_path.exists() else 0,
            "pillow": _pillow() is not None,
            "rendered": sorted(
                p.name for p in cache_dir.glob("icon-*.png")
            ),
            "fetched_at": state["fetched_at"],
            "error": state["error"],
        })

    def api_icon_refresh():
        ok = fetch(force=True)
        return jsonify({
            "ok": ok,
            "error": state["error"],
            "detail": "重新抓取完成，手机上需要把主屏图标删掉再添加一次" if ok else "抓取失败",
        })

    def api_icon_url():
        data = request.get_json(force=True, silent=True) or {}
        url = str(data.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": "需要 http 或 https 开头的图片直链"}), 400

        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (KEY_ICON_URL, url))

        ok = fetch(force=True)
        return jsonify({"ok": ok, "url": url, "error": state["error"]})

    routes = [
        ("/icons/icon-<size>.png", "api_icon", api_icon, ["GET"]),
        ("/apple-touch-icon.png", "api_icon_apple", lambda: api_icon(180), ["GET"]),
        ("/api/icon/status", "api_icon_status", api_icon_status, ["GET"]),
        ("/api/icon/refresh", "api_icon_refresh", api_icon_refresh, ["GET", "POST"]),
        ("/api/icon/url", "api_icon_url", api_icon_url, ["POST"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)

    server_module.icon_manifest_entries = manifest_entries
    server_module.icon_html_links = html_links

    # 首次抓取放后台：图床慢或不通都不该拖住后端启动。
    if not source_path.exists():
        threading.Thread(target=fetch, daemon=True).start()

    return fetch
