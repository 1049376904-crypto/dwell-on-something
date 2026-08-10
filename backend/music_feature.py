"""音乐卡片：把网易云链接变成能点的卡。

## 为什么不需要那个 MCP

上游前端已经把卡片写好了（`.songcard`，52px 封面加歌名歌手，
点一下开网易云），它只做一件事：在消息里发现网易云链接，
就拿歌曲 id 去问 `api/music?id=`，等五个字段回来填进卡片：

    {ok, id, name, artist, album, pic, sec}

这五个字段一次普通 HTTP 请求就能拿到，不需要常驻进程、不占内存。
那个带 Docker 容器的 MCP 是「一起听」、控制播放、登录账号才需要的东西。

## 数据来源

网易云的老公开接口，要带 UA 和 Referer，不然直接被挡：

    详情  https://music.163.com/api/song/detail?ids=[<id>]
    搜索  https://music.163.com/api/search/get?s=<关键词>&type=1

这些接口没有官方承诺，随时可能限流或改结构。所以：
* 查到就缓存进 songs 表，同一首歌只问一次；
* 解析写得宽容，字段缺了就留空，不让整张卡片崩掉；
* /api/music/status 如实报告通不通，坏了能一眼看出是网易云那边的事。

缓存还有个附带好处：哪天那些接口彻底不通了，
以前发过的卡片照样能显示。

## 封面图

卡片里的 `<img>` 直接指向网易云的图片 CDN（上游就是这么写的），
不经过我们的后端——省带宽，代价是那个 CDN 会看到访问者的 IP。
只是专辑封面，我判断不值得为此做一层代理。

## 给沐的工具

`find_song` 只负责找歌并把链接给它，不自己发消息。
卡片是由消息正文里的链接渲染出来的，所以链接必须写进它的回话里。
这跟表情包正好相反——那边明确要求不许把图片链接写进正文。
"""

import json
import re
import time
import urllib.parse
import urllib.request


UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
REFERER = "https://music.163.com/"

DETAIL_URL = "https://music.163.com/api/song/detail?ids=%5B{id}%5D"
SEARCH_URL = "https://music.163.com/api/search/get"

TIMEOUT = 12

# 缓存多久之后重新问一次。歌曲信息几乎不变，给一个月足够。
# 主要是为了万一某次拿到的是残缺数据，不至于永远错下去。
CACHE_DAYS = 30

# 搜索最多返回几条。给模型挑用，太多只会占上下文。
SEARCH_LIMIT = 5

ID_OK = re.compile(r"^\d{1,15}$")


def _get(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": REFERER}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _artists(song):
    """歌手名。这些接口的字段名换过好几版，几个都试一下。"""
    for key in ("artists", "ar"):
        people = song.get(key)
        if isinstance(people, list) and people:
            names = [str(p.get("name", "")).strip() for p in people if isinstance(p, dict)]
            names = [n for n in names if n]
            if names:
                return " / ".join(names)
    return ""


def _album(song):
    for key in ("album", "al"):
        album = song.get(key)
        if isinstance(album, dict):
            return str(album.get("name", "")).strip(), str(
                album.get("picUrl") or album.get("pic_str") or ""
            ).strip()
    return "", ""


def _duration_seconds(song):
    """时长。detail 接口给的是 duration（毫秒），有的版本叫 dt。"""
    for key in ("duration", "dt"):
        value = song.get(key)
        try:
            ms = int(value)
        except (TypeError, ValueError):
            continue
        if ms > 0:
            return ms // 1000
    return 0


def _parse_song(song):
    if not isinstance(song, dict):
        return None
    name = str(song.get("name", "")).strip()
    if not name:
        return None
    album_name, pic = _album(song)
    # 有的返回把封面挂在歌曲级别。
    if not pic:
        pic = str(song.get("picUrl") or "").strip()
    return {
        "id": str(song.get("id", "")),
        "name": name,
        "artist": _artists(song),
        "album": album_name,
        "pic": pic,
        "sec": _duration_seconds(song),
    }


def register_music_feature(server_module):
    from flask import jsonify, request

    get_db = server_module.get_db

    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id     TEXT PRIMARY KEY,
                name   TEXT NOT NULL DEFAULT '',
                artist TEXT NOT NULL DEFAULT '',
                album  TEXT NOT NULL DEFAULT '',
                pic    TEXT NOT NULL DEFAULT '',
                sec    INTEGER NOT NULL DEFAULT 0,
                at     INTEGER NOT NULL DEFAULT 0
            )
        """)

    state = {"last_error": "", "last_ok_at": 0, "last_try_at": 0}

    def cached(song_id):
        with get_db() as db:
            row = db.execute(
                "SELECT id,name,artist,album,pic,sec,at FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
        if row is None:
            return None
        if row["at"] and time.time() - row["at"] > CACHE_DAYS * 86400:
            return None
        return {
            "id": row["id"], "name": row["name"], "artist": row["artist"],
            "album": row["album"], "pic": row["pic"], "sec": row["sec"],
        }

    def remember(song):
        with get_db() as db:
            db.execute(
                "INSERT INTO songs (id,name,artist,album,pic,sec,at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, artist=excluded.artist, "
                "album=excluded.album, pic=excluded.pic, sec=excluded.sec, at=excluded.at",
                (
                    song["id"], song["name"], song["artist"], song["album"],
                    song["pic"], song["sec"], int(time.time()),
                ),
            )

    def detail(song_id):
        """查一首歌。先看缓存，没有再问网易云。"""
        hit = cached(song_id)
        if hit:
            return hit, ""

        state["last_try_at"] = int(time.time())
        try:
            data = _get(DETAIL_URL.format(id=song_id))
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:160]}"
            state["last_error"] = message
            # 过期的缓存也比什么都没有强：卡片显示旧信息，
            # 总比变成「这首歌读不到了」好。
            with get_db() as db:
                row = db.execute(
                    "SELECT id,name,artist,album,pic,sec FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
            if row is not None:
                return dict(row), ""
            return None, message

        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            state["last_error"] = "网易云没返回这首歌"
            return None, "没找到这首歌"

        parsed = _parse_song(songs[0])
        if parsed is None:
            state["last_error"] = "返回结构看不懂"
            return None, "返回的数据解析不了"

        parsed["id"] = str(song_id)
        remember(parsed)
        state["last_ok_at"] = int(time.time())
        state["last_error"] = ""
        return parsed, ""

    def search(keyword, limit=SEARCH_LIMIT):
        """按关键词搜歌，返回列表。"""
        query = urllib.parse.urlencode({
            "s": str(keyword or "").strip(),
            "type": 1,
            "limit": max(1, min(20, int(limit))),
            "offset": 0,
        })
        state["last_try_at"] = int(time.time())
        try:
            data = _get(SEARCH_URL + "?" + query)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:160]}"
            state["last_error"] = message
            return [], message

        result = data.get("result") or {}
        songs = result.get("songs")
        if not isinstance(songs, list):
            return [], "没有结果"

        found = []
        for item in songs:
            parsed = _parse_song(item)
            if parsed is None:
                continue
            # 搜索结果里顺手缓存一份，之后渲染卡片就不用再问一次。
            if parsed["id"]:
                remember(parsed)
            found.append(parsed)

        state["last_ok_at"] = int(time.time())
        state["last_error"] = ""
        return found, ""

    # ── 接口

    def api_music():
        """上游卡片调的就是这条。字段名必须跟它读的一致。"""
        song_id = str(request.args.get("id", "")).strip()
        if not ID_OK.match(song_id):
            return jsonify({"ok": False, "error": "id 不对"}), 400

        song, error = detail(song_id)
        if song is None:
            return jsonify({"ok": False, "error": error}), 404

        payload = {"ok": True}
        payload.update(song)
        response = jsonify(payload)
        # 前端本来就用 force-cache，这里再给一层。歌曲信息不会变。
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    def api_music_search():
        keyword = str(request.args.get("q", "")).strip()
        if not keyword:
            return jsonify({"ok": False, "error": "没给关键词"}), 400
        found, error = search(keyword)
        return jsonify({
            "ok": bool(found),
            "error": error,
            "items": found,
            "links": [
                "https://music.163.com/song?id=" + s["id"] for s in found if s["id"]
            ],
        })

    def api_music_status():
        """诊断。网易云那些接口没有官方承诺，坏了要能一眼看出来。"""
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM songs").fetchone()["n"]
        probe, probe_error = search("周杰伦", 1)
        return jsonify({
            "ok": True,
            "cached_songs": total,
            "netease_reachable": bool(probe),
            "probe_error": probe_error,
            "last_error": state["last_error"],
            "last_ok_at": state["last_ok_at"],
            "detail_api": DETAIL_URL.format(id="<id>"),
        })

    routes = [
        ("/api/music", "api_music", api_music, ["GET"]),
        ("/api/music/search", "api_music_search", api_music_search, ["GET"]),
        ("/api/music/status", "api_music_status", api_music_status, ["GET"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(
            rule, endpoint=endpoint, view_func=view, methods=methods
        )

    _wire_tools(server_module, search)

    server_module.music_search = search
    server_module.music_detail = detail
    print("[dwell] 音乐卡片: /api/music（缓存在 songs 表）")
    return search


def _wire_tools(server_module, search):
    """给沐一个找歌的工具。

    刻意不做成「自动发一条消息」：卡片是靠消息正文里的链接渲染的，
    所以链接必须出现在它自己的回话里。工具只把链接交给它。
    这跟表情包相反——那边要求不许把图片链接写进正文。
    """
    try:
        import agent_tools_feature as agent
    except ImportError as exc:
        print(f"[dwell] 音乐没接上工具层: {exc}")
        return

    tool = {
        "type": "function",
        "function": {
            "name": "find_song",
            "description": (
                "在网易云上找一首歌，拿到可以分享的链接。"
                "想给妍妍推荐歌、或者她说起某首歌想看看是哪一首时用。"
                "拿到链接后要把链接原样写进你的回话里，"
                "界面会自动把它变成一张能点的卡片；不要只说歌名。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "歌名，最好带上歌手，比如「晴天 周杰伦」。",
                    }
                },
                "required": ["query"],
            },
        },
    }

    known = {t["function"]["name"] for t in agent.TOOLS}
    if tool["function"]["name"] not in known:
        agent.TOOLS.append(tool)

    original_execute = agent.execute_tool

    def execute_with_music(server, name, args):
        if name == "find_song":
            found, error = search(str(args.get("query", "")), 3)
            if not found:
                return {"error": error or "没找到这首歌"}
            return {
                "songs": [
                    {
                        "name": s["name"],
                        "artist": s["artist"],
                        "album": s["album"],
                        "link": "https://music.163.com/song?id=" + s["id"],
                    }
                    for s in found
                ],
                "怎么用": "把 link 原样写进回话里，界面会渲染成卡片。",
            }
        return original_execute(server, name, args)

    agent.execute_tool = execute_with_music
