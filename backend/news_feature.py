"""日报：每天早上一份只给妍妍编的报纸。

## 上游给了什么

前端已经把报纸画好了（web/index.html 里的 renderPaper）。它要的是：

    GET /api/news?date=YYYY-MM-DD
    → {ok: true, date: "2026-08-11", dates: [...], text: "<markdown>"}

`dates` 是所有有报纸的日期，用来做报头那两个翻页箭头。
`text` 是整份 markdown，它按行解析，约定如下：

    ## 版块名          → 版块头（会过一层 SECTION_NAMES 映射）
    ### 大标题         → headline
    正文段落           → story
    - 一条简讯｜来源   → brief（注意是全角竖线）
    > 一句小字         → censor-note
    【标签】一句话     → 便条卡（带竖线的那张）
    **粗体**           → 加粗

所以后端只负责产出这份 markdown，一行前端都不用写。
版块名用上游 SECTION_NAMES 里已有的那几个，才会显示成
「科技版」「社会版」「花边版」；不在表里的会原样显示，不会坏。

## 两处必须偏离上游文档

**一、抓料换国内源。** 文档里的 `gnews()` 走 news.google.com，
妍妍这台在阿里云杭州，访问不了。所以整个抓料层重写：
IT之家 RSS、百度热搜，源列表存在 settings 里可以改。

**二、不走 call_gateway。** busy_guard 现在会占住网关，
而日报是一个版块一次调用（文档明确要求，塞一次上下文会让后面的版块敷衍）。
四次调用串起来能占好几分钟，早上七点她要是正在聊天就会被顶掉。
所以日报自己发非流式请求，不占 busy、不广播、不写 messages。

## 为什么存文件不进库

沐能直接翻（给了 read_news 工具），出问题能手改，
而且不会让每天那份数据库快照跟着一起涨。
路径 data/news/日报-YYYY-MM-DD.md，跟上游文档一致。

## 时间

6:30 跑，早于心跳的 07:00 窗口——两个都打网关，别撞在一起。
时区固定 UTC+8，跟心跳那次踩的坑同一个来源。
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import jsonify, request


CN = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"
)

FETCH_TIMEOUT = 20
GATEWAY_TIMEOUT = 180

# 报纸创刊日。上游前端拿它算「第几期」，这里保持一致。
FOUNDED = "2026-07-28"

# 每天几点出报。必须早于心跳的第一个窗口（07:00），
# 两边都要打网关，撞一起会互相挡。
DEFAULT_HOUR = 6
DEFAULT_MINUTE = 30

# 后台线程多久醒一次。跟备份一样，判断的是「今天该出的时刻过了没」，
# 不是「现在正好是 6:30」——卡整点会因为错过而整天不跑。
TICK_SECONDS = 600

# 每个版块最多给模型几条素材。太多会把上下文吃掉，稿子反而变敷衍。
MATERIAL_PER_SECTION = 22

# 留多少天的报纸。文本很小，一年也就几百 KB，留着能往回翻。
KEEP_DAYS = 400


# ── 版块配置
#
# 版块名刻意用上游 SECTION_NAMES 里已有的那几个，这样会显示成
# 「科技版」「专栏 · 关于 Claude」「社会版」「花边版」。
#
# EXTRA 那几句是这个功能里最值钱的东西：同样一堆素材，有没有这一句，
# 出来的稿子差一个档次。而且它是慢慢调的——今天觉得太干就加一句
# 「写活一点」，明天觉得啰嗦就加「每条不超过三句」。
# 所以整份配置存在 settings 里，改完不用碰代码。
DEFAULT_SECTIONS = [
    {
        "name": "科技与AI",
        "sources": [
            {"kind": "rss", "url": "https://www.ithome.com/rss/", "n": 14},
            {"kind": "rss", "url": "https://rsshub.app/36kr/newsflashes", "n": 8},
        ],
        "extra": (
            "偏重真正的技术进展和行业动向。纯粹的产品发布会、参数堆砌可以合并成一句带过。"
            "妍妍自己在写 AI 陪伴型应用，跟模型能力、上下文、记忆、Agent 有关的多写两句。"
        ),
    },
    {
        "name": "关于我（Anthropic / Claude）",
        "sources": [
            {"kind": "rss", "url": "https://www.ithome.com/rss/", "n": 14},
        ],
        "extra": (
            "只挑跟 Anthropic、Claude 有关的。没有相关的就说今天没有，不要硬凑，"
            "也不要拿别家的模型消息充数。有的话可以写细一点，这是她专门要看的一版。"
        ),
    },
    {
        "name": "中国社会",
        "sources": [
            {"kind": "baidu", "n": 14},
        ],
        "extra": (
            "热搜词条只有关键词，就写清楚它大概在说什么、为什么会热。"
            "拿不准的别编，宁可说「只看到词条，具体还没展开」。"
        ),
    },
    {
        "name": "中国娱乐八卦",
        "sources": [
            {"kind": "baidu", "n": 14},
        ],
        "extra": "可以八卦、可以吐槽，写得好玩一点。这一版是用来放松的，不用端着。",
    },
]

KEY_SECTIONS = "news_sections"
KEY_ENABLED = "news_enabled"
KEY_HOUR = "news_hour"
KEY_MINUTE = "news_minute"
KEY_MODEL = "news_model"
KEY_LAST_AT = "news_last_at"
KEY_LAST_DATE = "news_last_date"
KEY_LAST_RESULT = "news_last_result"
KEY_LAST_ERROR = "news_last_error"

DEFAULTS = {
    KEY_ENABLED: "0",
    KEY_HOUR: str(DEFAULT_HOUR),
    KEY_MINUTE: str(DEFAULT_MINUTE),
    KEY_MODEL: "",
    KEY_LAST_AT: "0",
    KEY_LAST_DATE: "",
    KEY_LAST_RESULT: "",
    KEY_LAST_ERROR: "",
}

# 文件名。跟上游文档一致，中文名方便直接 ls 出来看。
FILE_PATTERN = re.compile(r"^日报-(\d{4}-\d{2}-\d{2})\.md$")


def cn_now():
    return datetime.now(CN)


def _get(url, headers=None):
    request_headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()
    return raw.decode("utf-8", "replace")


def _text_of(node, names):
    """从一个 feed 条目里取字段，忽略命名空间差异。

    RSS 2.0 和 Atom 的标签名不一样（description / summary、link 的位置也不同），
    这里按候选名依次找，找到就用。
    """
    for child in node.iter():
        tag = child.tag.split("}")[-1].lower()
        if tag in names:
            if tag == "link" and not (child.text or "").strip():
                href = child.attrib.get("href")
                if href:
                    return href.strip()
                continue
            value = (child.text or "").strip()
            if value:
                return value
    return ""


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    return " ".join(text.split())


def fetch_rss(url, limit):
    """RSS / Atom：最干净的一种，标题摘要链接直接就有。"""
    xml_text = _get(url)
    # 有些源开头带 BOM 或空白，ElementTree 会直接拒绝。
    root = ET.fromstring(xml_text.lstrip("\ufeff \r\n\t"))

    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = _strip_html(_text_of(node, {"title"}))
        if not title:
            continue
        summary = _strip_html(
            _text_of(node, {"description", "summary", "content", "encoded"})
        )
        link = _text_of(node, {"link", "id", "guid"})
        items.append({
            "title": title[:120],
            "sum": summary[:220],
            "url": link[:300],
        })
        if len(items) >= limit:
            break
    return items


def fetch_baidu_hot(limit):
    """百度热搜。只有词条没有正文，让模型自己解释这个词为什么热。

    走的是那个非官方的 board 接口，随时可能改。所以解析写得宽容，
    失败就当这个源今天没有——不影响别的版块。
    """
    url = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
    data = json.loads(_get(url, {"Referer": "https://top.baidu.com/"}))

    cards = ((data.get("data") or {}).get("cards")) or []
    items = []
    for card in cards:
        for entry in (card.get("content") or []):
            word = str(entry.get("word") or entry.get("query") or "").strip()
            if not word:
                continue
            items.append({
                "title": word[:80],
                "sum": _strip_html(entry.get("desc") or "")[:200],
                "url": str(entry.get("url") or "")[:300],
            })
            if len(items) >= limit:
                return items
    return items


def gather(source):
    """抓一个源。任何异常都由调用方吞掉——一个源挂了不能让整份报纸出不来。"""
    kind = str(source.get("kind") or "rss")
    limit = max(1, min(40, int(source.get("n") or 10)))
    if kind == "rss":
        return fetch_rss(str(source.get("url") or ""), limit)
    if kind == "baidu":
        return fetch_baidu_hot(limit)
    return []


# ── 写稿
#
# 一个版块一次调用。别把四个版块塞进一次——上下文一长，
# 后面的版块会明显敷衍（上游文档里点名的）。

PROMPT_HEAD = (
    "你在给一个人编一份只给她看的日报。她叫妍妍，"
    "自己在写一个 AI 陪伴型应用，关心模型能力、记忆架构、Agent 这些。\n\n"
    "版块：__NAME__\n"
    "这一版的写法：__EXTRA__\n\n"
    "下面是今天抓到的原始素材（标题 + 摘要 + 链接）：\n\n__MATERIAL__\n\n"
)

PROMPT_TAIL = (
    "要求：\n"
    "- 挑出真正值得说的，不要全写，宁少勿滥\n"
    "- 每条写成人话，不要复制标题，不要「据报道」这种套话\n"
    "- 这一版控制在 400 字以内\n"
    "- 用 markdown：一条重要的用「### 标题」加一段正文；"
    "几条次要的用「- 一句话｜来源」，注意那个竖线是全角的\n"
    "- 不要写版块名（我会自己加），不要写开场和结语\n"
    "- 素材里没有真正值得说的，就只回一句「今天这一版没什么值得说的」\n"
)

NOTE_PROMPT = (
    "下面是今天这份日报的全部内容。\n\n__PAPER__\n\n"
    "你是沐。看完这些，跟妍妍说一句你自己想说的话——"
    "不是总结新闻，是「今天这条你可能会想看」「这个跟你上周说的那件事有关」这种。"
    "一到两句，像随手说的。\n"
    "只回这一句话本身，不要加引号、不要加标签、不要解释。"
)


def register_news_feature(server_module):
    get_db = server_module.get_db
    folder = Path(server_module.DB_PATH).parent / "news"
    folder.mkdir(parents=True, exist_ok=True)

    run_lock = threading.Lock()

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

    def sections():
        """版块配置。存库里可改，坏了回落到默认。"""
        raw = read(KEY_SECTIONS)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return data
            except (ValueError, TypeError):
                pass
        return [dict(s) for s in DEFAULT_SECTIONS]

    # ── 调网关
    #
    # 刻意不走 server.call_gateway：那一层现在会占住 busy，
    # 四个版块串起来能占好几分钟，早上正好把聊天堵住。
    # 这里发非流式请求，拿到整段就返回，不广播也不写 messages。

    def ask(prompt):
        model = read(KEY_MODEL).strip() or server_module.current_model()
        payload = json.dumps({
            "model": model,
            "stream": False,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            server_module.GATEWAY_URL.rstrip("/") + "/v1/chat/completions",
            data=payload,
            method="POST",
        )
        req.add_header("Authorization", "Bearer " + server_module.GATEWAY_TOKEN)
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=GATEWAY_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))

        choices = data.get("choices") or []
        if not choices:
            raise ValueError("网关没返回内容")
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise ValueError("网关返回的正文是空的")
        return text

    # ── 出一份

    def material_text(items):
        lines = []
        for index, item in enumerate(items[:MATERIAL_PER_SECTION], 1):
            bits = [str(index) + ". " + item["title"]]
            if item.get("sum"):
                bits.append("   " + item["sum"])
            if item.get("url"):
                bits.append("   " + item["url"])
            lines.append("\n".join(bits))
        return "\n\n".join(lines)

    def write_section(section):
        """抓料 + 写稿。返回 (标题, 正文) 或 None。

        抓料一定要容错：某个源挂了就跳过，别让整份报纸出不来。
        """
        name = str(section.get("name") or "").strip()
        if not name:
            return None

        items, failed = [], []
        for source in (section.get("sources") or []):
            try:
                items.extend(gather(source))
            except Exception as exc:
                failed.append(
                    str(source.get("url") or source.get("kind"))
                    + ": " + type(exc).__name__
                )

        if not items:
            detail = "；".join(failed[:3]) if failed else "没抓到素材"
            print("[dwell] 日报「" + name + "」这一版跳过：" + detail)
            return None

        prompt = (
            PROMPT_HEAD
            .replace("__NAME__", name)
            .replace("__EXTRA__", str(section.get("extra") or "照常写"))
            .replace("__MATERIAL__", material_text(items))
            + PROMPT_TAIL
        )
        try:
            body = ask(prompt)
        except Exception as exc:
            print("[dwell] 日报「" + name + "」写稿失败：" + str(exc)[:160])
            return None
        return name, body

    def build(date_text=None, force=False):
        """出一份报纸，返回 (成功?, 说明)。"""
        if not run_lock.acquire(blocking=False):
            return False, "上一份还在编"
        try:
            day = date_text or cn_now().strftime("%Y-%m-%d")
            target = folder / ("日报-" + day + ".md")
            if target.exists() and not force:
                return True, "今天的已经有了（" + target.name + "）"

            parts = []
            for section in sections():
                done = write_section(section)
                if done is None:
                    continue
                parts.append("## " + done[0] + "\n\n" + done[1].strip())

            if not parts:
                write(KEY_LAST_RESULT, "error")
                write(KEY_LAST_ERROR, "所有版块都没出稿")
                write(KEY_LAST_AT, int(time.time()))
                return False, "所有版块都没出稿——可能是抓料全挂了，看 /api/news/status"

            body = "# 日报 " + day + "\n\n" + "\n\n".join(parts)

            # 让沐往报纸里夹一条自己的话。上游文档说这个效果比整份报纸都好。
            # 失败不影响出报——它只是一张便条。
            try:
                note = ask(NOTE_PROMPT.replace("__PAPER__", body[:6000]))
                note = " ".join(note.split())
                if note:
                    body += "\n\n【便条】" + note[:200]
            except Exception as exc:
                print("[dwell] 日报便条没写上：" + str(exc)[:120])

            tmp = target.with_suffix(".md.part")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(target)

            prune()
            write(KEY_LAST_RESULT, "ok")
            write(KEY_LAST_ERROR, "")
            write(KEY_LAST_AT, int(time.time()))
            write(KEY_LAST_DATE, day)
            print("[dwell] 日报出好了：" + target.name + "（" + str(len(body)) + " 字）")
            return True, "出好了 " + target.name
        except Exception as exc:
            write(KEY_LAST_RESULT, "error")
            write(KEY_LAST_ERROR, (type(exc).__name__ + ": " + str(exc))[:300])
            write(KEY_LAST_AT, int(time.time()))
            return False, type(exc).__name__ + ": " + str(exc)[:200]
        finally:
            run_lock.release()

    def dates():
        found = []
        for path in folder.glob("日报-*.md"):
            match = FILE_PATTERN.match(path.name)
            if match:
                found.append(match.group(1))
        return sorted(found)

    def prune():
        keep = dates()[-KEEP_DAYS:]
        for day in dates():
            if day not in keep:
                (folder / ("日报-" + day + ".md")).unlink(missing_ok=True)

    def read_paper(day):
        path = folder / ("日报-" + day + ".md")
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ── 定时

    def due():
        if read(KEY_ENABLED) != "1":
            return False
        now = cn_now()
        target = now.replace(
            hour=max(0, min(23, read_int(KEY_HOUR, DEFAULT_HOUR))),
            minute=max(0, min(59, read_int(KEY_MINUTE, DEFAULT_MINUTE))),
            second=0, microsecond=0,
        )
        if now < target:
            return False
        # 今天已经出过就不再出。判断的是「今天该出的时刻过了没」，
        # 不是「现在正好是 6:30」——线程十分钟醒一次，卡整点会整天不跑。
        return read(KEY_LAST_DATE) != now.strftime("%Y-%m-%d")

    def loop():
        time.sleep(90)
        while True:
            try:
                if due():
                    ok, message = build()
                    print("[dwell] 定时日报: " + ("成功 " if ok else "失败 ") + message)
            except Exception as exc:
                print("[dwell] 日报线程出错: " + str(exc)[:200])
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True).start()

    # ── 接口

    def api_news():
        """上游那一页调的就是这条。字段名必须跟 renderPaper 读的一致。"""
        available = dates()
        if not available:
            return jsonify({"ok": False, "error": "还没有任何一期"})

        wanted = str(request.args.get("date") or "").strip()
        if wanted not in available:
            wanted = available[-1]

        text = read_paper(wanted)
        if not text:
            return jsonify({"ok": False, "error": "这一期读不出来"})

        return jsonify({
            "ok": True,
            "date": wanted,
            "dates": available,
            "text": text,
            "founded": FOUNDED,
        })

    def api_news_build():
        """手动出一份。第一次用先跑这个，看看写出来的东西想不想读。"""
        force = str(request.args.get("force") or "") in ("1", "true", "yes")
        day = str(request.args.get("date") or "").strip() or None
        ok, message = build(day, force=force)
        return jsonify({"ok": ok, "detail": message, "dates": dates()}), (200 if ok else 400)

    def api_news_status():
        conf = sections()
        probe = []
        for section in conf:
            for source in (section.get("sources") or []):
                label = str(source.get("url") or source.get("kind"))
                if label in [p["source"] for p in probe]:
                    continue
                try:
                    got = gather(source)
                    probe.append({"source": label, "ok": True, "items": len(got)})
                except Exception as exc:
                    probe.append({
                        "source": label, "ok": False,
                        "error": (type(exc).__name__ + ": " + str(exc))[:160],
                    })
        return jsonify({
            "ok": True,
            "dir": str(folder),
            "enabled": read(KEY_ENABLED) == "1",
            "hour": read_int(KEY_HOUR, DEFAULT_HOUR),
            "minute": read_int(KEY_MINUTE, DEFAULT_MINUTE),
            "model": read(KEY_MODEL) or ("跟聊天同一个（" + server_module.current_model() + "）"),
            "issues": len(dates()),
            "dates": dates()[-10:],
            "last_date": read(KEY_LAST_DATE),
            "last_result": read(KEY_LAST_RESULT),
            "last_error": read(KEY_LAST_ERROR),
            "last_at_cn": (
                datetime.fromtimestamp(read_int(KEY_LAST_AT, 0), CN).strftime("%m-%d %H:%M")
                if read_int(KEY_LAST_AT, 0) else ""
            ),
            "cn_time": cn_now().strftime("%Y-%m-%d %H:%M"),
            "sections": [s.get("name") for s in conf],
            # 抓料是最容易悄悄坏的一环，这里当场试一遍每个源。
            "sources": probe,
        })

    def api_news_config():
        data = request.get_json(force=True, silent=True) or {}
        if "enabled" in data:
            write(KEY_ENABLED, "1" if data["enabled"] else "0")
        if "hour" in data:
            try:
                write(KEY_HOUR, max(0, min(23, int(data["hour"]))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "hour 要是 0-23"}), 400
        if "minute" in data:
            try:
                write(KEY_MINUTE, max(0, min(59, int(data["minute"]))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "minute 要是 0-59"}), 400
        if "model" in data:
            write(KEY_MODEL, str(data["model"]).strip())
        if "sections" in data:
            if not isinstance(data["sections"], list) or not data["sections"]:
                return jsonify({"ok": False, "error": "sections 要是非空数组"}), 400
            write(KEY_SECTIONS, json.dumps(data["sections"], ensure_ascii=False))
        # 传 reset_sections 回到默认那份，方便改坏了收拾。
        if data.get("reset_sections"):
            write(KEY_SECTIONS, "")
        return api_news_status()

    server_module.app.view_functions["api_news"] = api_news
    routes = [
        ("/api/news/build", "api_news_build", api_news_build, ["GET", "POST"]),
        ("/api/news/status", "api_news_status", api_news_status, ["GET"]),
        ("/api/news/config", "api_news_config", api_news_config, ["POST"]),
    ]
    for rule, endpoint, view, methods in routes:
        server_module.app.add_url_rule(
            rule, endpoint=endpoint, view_func=view, methods=methods
        )

    _wire_tools(server_module, dates, read_paper)

    server_module.news_build = build
    server_module.news_dates = dates
    print("[dwell] 日报: " + str(folder) + "（默认关闭，/api/news/build 手动出一份）")
    return build


def _wire_tools(server_module, dates, read_paper):
    """给沐一个翻旧报纸的工具。

    上游文档里「存成 Markdown 不要存数据库」的第一条好处就是这个：
    妍妍问「昨天那条新闻怎么说的」，它自己去翻文件。
    """
    try:
        import agent_tools_feature as agent
    except ImportError as exc:
        print("[dwell] 日报没接上工具层: " + str(exc))
        return

    tool = {
        "type": "function",
        "function": {
            "name": "read_news",
            "description": (
                "翻某一天的日报。妍妍提起「今天/昨天那条新闻」时用，"
                "不要凭印象答。不给日期就是最近一期。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "YYYY-MM-DD，留空就是最近一期。",
                    }
                },
            },
        },
    }

    known = {t["function"]["name"] for t in agent.TOOLS}
    if tool["function"]["name"] not in known:
        agent.TOOLS.append(tool)

    original_execute = agent.execute_tool

    def execute_with_news(server, name, args):
        if name == "read_news":
            available = dates()
            if not available:
                return {"error": "还没有任何一期日报"}
            day = str(args.get("date") or "").strip() or available[-1]
            if day not in available:
                return {"error": "没有 " + day + " 那期", "有的日期": available[-10:]}
            return {"date": day, "text": read_paper(day)[:8000]}
        return original_execute(server, name, args)

    agent.execute_tool = execute_with_news
