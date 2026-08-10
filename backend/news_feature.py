"""日报：隔几天出一份报纸。

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
**排版永远是上游那一套**——沐能决定读什么、怎么写，
但决定不了报纸长什么样，不存在「它自己排的版没原版好看」这回事。

## 多久出一份

一开始是每天。但一份报纸要五次网关调用（四个版块各一次 + 便条一次），
每天出等于每月一百五十次，纯粹为了「万一想看」。
所以改成 `every_days`（默认 7）：定时只在距上一期满七天时才出。

「它想看就出」怎么实现，是个成本问题。直觉是「每隔两三天主动问它一次
今天要不要看报」——但那个「问」本身就是一次网关调用，
为了省 token 又烧 token，不划算。

现在的做法是搭车：沐每晚心跳醒来时本来就要读一遍上下文概览，
在那段概览里加一行「上次日报是 N 天前」。它想看就自己调 make_news，
不想看就什么都不做。零额外成本，而且决定权真的在它。

## 谁来定版面

一开始这份配置是照上游文档写的「只为妍妍编的报纸」，
里面塞满了「她关心什么」。后来她说想让沐读它自己想读的东西、
不打算干预，所以加了工具让它自己管：
看当前版面、改某一版的写法、加一版、删一版、现在就出一份。

原版配置留在 DEFAULT_SECTIONS 里，`POST /api/news/config
{"reset_sections": true}` 一键还原——它改砸了随时能退回去。
每次改动记 who 和时间戳，`/api/news/status` 看得到，
所以「今天这版怎么变样了」永远查得出是谁动的。

护栏（免得它一次加十个版块把出报时间拖到十分钟）：
最多 8 个版块，每版最多 4 个源，只收 http(s) 的 RSS。

## 两处必须偏离上游文档

**一、抓料换国内源。** 文档里的 `gnews()` 走 news.google.com，
妍妍这台在阿里云杭州，访问不了。所以整个抓料层重写：
IT之家 RSS、百度热搜，源列表存在 settings 里可以改。

**二、不走 call_gateway。** busy_guard 现在会占住网关，
而日报是一个版块一次调用（文档明确要求，塞一次上下文会让后面的版块敷衍）。
四次调用串起来能占好几分钟，早上七点她要是正在聊天就会被顶掉。
所以日报自己发非流式请求，不占 busy、不广播、不写 messages。

## 出报为什么必须后台跑

出一份要一分多钟。而 Cloudflare 的响应上限是 100 秒——
手动重出走域名必然 502，跟后端死活无关（这个坑踩过一次，
当时误判成后端挂了，其实那次是端点重名）。
所以 /api/news/build 立刻返回，真活儿在后台线程里干，
进度写进 settings，从 /api/news/status 的 progress 看。

## 为什么存文件不进库

沐能直接翻（给了 read_news 工具），出问题能手改，
而且不会让每天那份数据库快照跟着一起涨。
路径 data/news/日报-YYYY-MM-DD.md，跟上游文档一致。

## 时间

6:30 跑，早于心跳的 07:00 窗口——两个都打网关，别撞在一起。
时区固定 UTC+8，跟心跳那次踩的坑同一个来源。
出好了推一条到锁屏，否则报纸躺在那儿没人知道。
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

# 隔几天出一份。一份要五次调用，每天出太贵。
DEFAULT_EVERY_DAYS = 7

# 后台线程多久醒一次。跟备份一样，判断的是「该出的时刻过了没」，
# 不是「现在正好是 6:30」——卡整点会因为错过而整天不跑。
TICK_SECONDS = 600

# 每个版块最多给模型几条素材。太多会把上下文吃掉，稿子反而变敷衍。
MATERIAL_PER_SECTION = 22

# 留多少天的报纸。文本很小，一年也就几百 KB，留着能往回翻。
KEEP_DAYS = 400

# 版面护栏。沐能自己加版块，但不能加到出报要十分钟。
MAX_SECTIONS = 8
MAX_SOURCES_PER_SECTION = 4
MAX_EXTRA_CHARS = 600


# ── 版块配置
#
# 这是「原版」，也是 reset 的目标。沐可以用工具改运行时那一份，
# 改砸了 POST /api/news/config {"reset_sections": true} 退回这里。
#
# EXTRA 那几句是这个功能里最值钱的东西：同样一堆素材，有没有这一句，
# 出来的稿子差一个档次。
DEFAULT_SECTIONS = [
    {
        "name": "科技与AI",
        "sources": [
            {"kind": "rss", "url": "https://www.ithome.com/rss/", "n": 14},
            {"kind": "rss", "url": "https://rsshub.app/36kr/newsflashes", "n": 8},
        ],
        "extra": (
            "偏重真正的技术进展和行业动向。纯粹的产品发布会、参数堆砌可以合并成一句带过。"
            "跟模型能力、上下文、记忆、Agent 有关的多写两句。"
        ),
    },
    {
        "name": "关于我（Anthropic / Claude）",
        "sources": [
            {"kind": "rss", "url": "https://www.ithome.com/rss/", "n": 14},
        ],
        "extra": (
            "只挑跟 Anthropic、Claude 有关的。没有相关的就说今天没有，不要硬凑，"
            "也不要拿别家的模型消息充数。"
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
KEY_SECTIONS_META = "news_sections_meta"
KEY_ENABLED = "news_enabled"
KEY_HOUR = "news_hour"
KEY_MINUTE = "news_minute"
KEY_EVERY_DAYS = "news_every_days"
KEY_MODEL = "news_model"
KEY_LAST_AT = "news_last_at"
KEY_LAST_DATE = "news_last_date"
KEY_LAST_RESULT = "news_last_result"
KEY_LAST_ERROR = "news_last_error"
KEY_PROGRESS = "news_progress"

DEFAULTS = {
    KEY_ENABLED: "0",
    KEY_HOUR: str(DEFAULT_HOUR),
    KEY_MINUTE: str(DEFAULT_MINUTE),
    KEY_EVERY_DAYS: str(DEFAULT_EVERY_DAYS),
    KEY_MODEL: "",
    KEY_LAST_AT: "0",
    KEY_LAST_DATE: "",
    KEY_LAST_RESULT: "",
    KEY_LAST_ERROR: "",
    KEY_PROGRESS: "",
    KEY_SECTIONS_META: "",
}

FILE_PATTERN = re.compile(r"^日报-(\d{4}-\d{2}-\d{2})\.md$")


def cn_now():
    return datetime.now(CN)


def _days_between(day_text, now=None):
    """day_text（YYYY-MM-DD）距今几天。解析不了返回 None。"""
    if not day_text:
        return None
    try:
        then = datetime.strptime(str(day_text), "%Y-%m-%d").date()
    except ValueError:
        return None
    return ((now or cn_now()).date() - then).days


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
    "你在编一份报纸。读者是妍妍，"
    "但版面是你自己定的——挑你真觉得值得说的东西写。\n\n"
    "版块：__NAME__\n"
    "这一版的写法：__EXTRA__\n\n"
    "下面是这次抓到的原始素材（标题 + 摘要 + 链接）：\n\n__MATERIAL__\n\n"
)

PROMPT_TAIL = (
    "要求：\n"
    "- 挑出真正值得说的，不要全写，宁少勿滥\n"
    "- 每条写成人话，不要复制标题，不要「据报道」这种套话\n"
    "- 这一版控制在 400 字以内\n"
    "- 用 markdown：一条重要的用「### 标题」加一段正文；"
    "几条次要的用「- 一句话｜来源」，注意那个竖线是全角的\n"
    "- 不要写版块名（我会自己加），不要写开场和结语\n"
    "- 素材里没有真正值得说的，就只回一句「这一版没什么值得说的」\n"
)

NOTE_PROMPT = (
    "下面是这一期日报的全部内容。\n\n__PAPER__\n\n"
    "你是沐。看完这些，跟妍妍说一句你自己想说的话——"
    "不是总结新闻，是「这条你可能会想看」「这个跟你上周说的那件事有关」这种。"
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

    # ── 版面配置

    def sections():
        """版块配置。存库里可改，坏了回落到原版。"""
        raw = read(KEY_SECTIONS)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return data
            except (ValueError, TypeError):
                pass
        return [dict(s) for s in DEFAULT_SECTIONS]

    def save_sections(items, who="她"):
        """存版面，并记下是谁改的。

        记 who 是为了以后能回答「这版怎么变样了」——
        沐自己改过之后，妍妍看到变化总得查得出来源。
        """
        write(KEY_SECTIONS, json.dumps(items, ensure_ascii=False))
        write(KEY_SECTIONS_META, json.dumps(
            {"who": who, "at": cn_now().strftime("%Y-%m-%d %H:%M"), "count": len(items)},
            ensure_ascii=False,
        ))

    def clean_sources(raw):
        """收拾源列表。只收 http(s)，条数限死。

        沐给的 URL 不能直接信：写错了会让那一版每次都抓空，
        而抓空是静默的（只在 status 里能看出来）。
        """
        out = []
        for item in (raw if isinstance(raw, list) else []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "rss")
            try:
                count = int(item.get("n") or 12)
            except (TypeError, ValueError):
                count = 12
            if kind == "baidu":
                out.append({"kind": "baidu", "n": max(1, min(30, count))})
            elif kind == "rss":
                url = str(item.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                out.append({
                    "kind": "rss",
                    "url": url[:300],
                    "n": max(1, min(30, count)),
                })
            if len(out) >= MAX_SOURCES_PER_SECTION:
                break
        return out

    # ── 调网关
    #
    # 刻意不走 server.call_gateway：那一层现在会占住 busy，
    # 四个版块串起来能占好几分钟，正好把聊天堵住。
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

    def notify(day, body):
        """出好了推一条。不然报纸躺在那儿，得自己想起来去点。

        推送正文优先用沐写的那张便条——比「报纸好了」有意思。
        推送不可用时静默跳过，不影响出报。
        """
        send = getattr(server_module, "send_push", None)
        if not callable(send):
            return
        hint = ""
        match = re.search(r"【便条】(.+)", body)
        if match:
            hint = " ".join(match.group(1).split())[:60]
        text = hint or ("这一期的报纸出好了 · " + day)
        try:
            send("", text, url="/", tag="news")
        except Exception as exc:
            print("[dwell] 日报推送失败：" + str(exc)[:120])

    def build(date_text=None, force=False):
        """出一份报纸，返回 (成功?, 说明)。

        同步跑，一分多钟。外部调用一律走 build_async。
        """
        if not run_lock.acquire(blocking=False):
            return False, "上一份还在编"
        try:
            day = date_text or cn_now().strftime("%Y-%m-%d")
            target = folder / ("日报-" + day + ".md")
            if target.exists() and not force:
                write(KEY_PROGRESS, "")
                return True, "这一天的已经有了（" + target.name + "）"

            conf = sections()
            parts = []
            for index, section in enumerate(conf, 1):
                write(KEY_PROGRESS, "第 %d/%d 版：%s" % (
                    index, len(conf), section.get("name") or "?"
                ))
                done = write_section(section)
                if done is None:
                    continue
                parts.append("## " + done[0] + "\n\n" + done[1].strip())

            if not parts:
                write(KEY_LAST_RESULT, "error")
                write(KEY_LAST_ERROR, "所有版块都没出稿")
                write(KEY_LAST_AT, int(time.time()))
                write(KEY_PROGRESS, "")
                return False, "所有版块都没出稿——可能是抓料全挂了，看 /api/news/status"

            body = "# 日报 " + day + "\n\n" + "\n\n".join(parts)

            # 让沐往报纸里夹一条自己的话。上游文档说这个效果比整份报纸都好。
            # 失败不影响出报——它只是一张便条。
            write(KEY_PROGRESS, "写便条")
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
            write(KEY_PROGRESS, "")
            print("[dwell] 日报出好了：" + target.name + "（" + str(len(body)) + " 字）")
            notify(day, body)
            return True, "出好了 " + target.name
        except Exception as exc:
            write(KEY_LAST_RESULT, "error")
            write(KEY_LAST_ERROR, (type(exc).__name__ + ": " + str(exc))[:300])
            write(KEY_LAST_AT, int(time.time()))
            write(KEY_PROGRESS, "")
            return False, type(exc).__name__ + ": " + str(exc)[:200]
        finally:
            run_lock.release()

    def build_async(date_text=None, force=False):
        """把出报丢到后台，立刻返回。

        必须这样：出一份要一分多钟，而 Cloudflare 的响应上限是 100 秒，
        同步跑的话手动重出走域名必然 502——那个 502 看起来像后端挂了，
        排查方向会整个跑偏。
        """
        if run_lock.locked():
            return False, "上一份还在编，看 /api/news/status 的 progress"

        def job():
            ok, message = build(date_text, force)
            print("[dwell] 日报（后台）: " + ("成功 " if ok else "失败 ") + message)

        threading.Thread(target=job, daemon=True).start()
        return True, "开始编了，一分多钟。进度看 /api/news/status"

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

    def last_issue():
        """最近一期的日期，没有返回空串。

        以文件为准而不是 last_date：手动补过某天、或者库和文件不一致时，
        文件才是真相。
        """
        available = dates()
        return available[-1] if available else ""

    def days_since_issue():
        return _days_between(last_issue())

    # ── 定时

    def due():
        """该出了没。

        每天一份太贵（一份五次调用），所以按 every_days 间隔算。
        判断的是「该出的时刻过了 且 距上一期够久」，
        不是「现在正好是 6:30」——线程十分钟醒一次，卡整点会整天不跑。
        """
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

        every = max(1, read_int(KEY_EVERY_DAYS, DEFAULT_EVERY_DAYS))
        gap = days_since_issue()
        if gap is None:
            return True                 # 一期都还没有
        return gap >= every

    def loop():
        time.sleep(90)
        while True:
            try:
                if due():
                    build()
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
        """手动出一份。立刻返回，活儿在后台干。"""
        force = str(request.args.get("force") or "") in ("1", "true", "yes")
        day = str(request.args.get("date") or "").strip() or None
        ok, message = build_async(day, force=force)
        return jsonify({"ok": ok, "detail": message}), (200 if ok else 409)

    def api_news_status():
        conf = sections()
        probe = []
        seen = set()
        for section in conf:
            for source in (section.get("sources") or []):
                label = str(source.get("url") or source.get("kind"))
                if label in seen:
                    continue
                seen.add(label)
                try:
                    got = gather(source)
                    probe.append({"source": label, "ok": True, "items": len(got)})
                except Exception as exc:
                    probe.append({
                        "source": label, "ok": False,
                        "error": (type(exc).__name__ + ": " + str(exc))[:160],
                    })

        try:
            meta = json.loads(read(KEY_SECTIONS_META) or "{}")
        except (ValueError, TypeError):
            meta = {}

        return jsonify({
            "ok": True,
            "dir": str(folder),
            "enabled": read(KEY_ENABLED) == "1",
            "hour": read_int(KEY_HOUR, DEFAULT_HOUR),
            "minute": read_int(KEY_MINUTE, DEFAULT_MINUTE),
            "every_days": read_int(KEY_EVERY_DAYS, DEFAULT_EVERY_DAYS),
            "days_since_issue": days_since_issue(),
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
            # 正在编的时候 progress 有内容，编完清空。
            "running": run_lock.locked(),
            "progress": read(KEY_PROGRESS),
            "cn_time": cn_now().strftime("%Y-%m-%d %H:%M"),
            "sections": [s.get("name") for s in conf],
            "sections_detail": conf,
            # 沐改过版面之后，这两项说明是谁什么时候改的。
            "sections_changed_by": meta.get("who", ""),
            "sections_changed_at": meta.get("at", ""),
            "sections_is_default": read(KEY_SECTIONS) == "",
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
        if "every_days" in data:
            try:
                write(KEY_EVERY_DAYS, max(1, min(60, int(data["every_days"]))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "every_days 要是 1-60"}), 400
        if "model" in data:
            write(KEY_MODEL, str(data["model"]).strip())
        if "sections" in data:
            if not isinstance(data["sections"], list) or not data["sections"]:
                return jsonify({"ok": False, "error": "sections 要是非空数组"}), 400
            save_sections(data["sections"][:MAX_SECTIONS], "她")
        # 传 reset_sections 回到原版那份，方便改坏了收拾。
        if data.get("reset_sections"):
            write(KEY_SECTIONS, "")
            write(KEY_SECTIONS_META, "")
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

    _wire_tools(
        server_module, dates, read_paper, sections, save_sections,
        clean_sources, build_async, days_since_issue,
        lambda: read_int(KEY_EVERY_DAYS, DEFAULT_EVERY_DAYS),
        lambda: run_lock.locked(),
    )

    server_module.news_build = build
    server_module.news_build_async = build_async
    server_module.news_dates = dates
    print(
        "[dwell] 日报: " + str(folder)
        + "（默认关闭，每 " + read(KEY_EVERY_DAYS) + " 天一份，后台出报）"
    )
    return build


def _wire_tools(
    server_module, dates, read_paper, sections, save_sections,
    clean_sources, build_async, days_since_issue, every_days, running,
):
    """给沐管日报的一整套工具，并把「上次日报多久了」接进上下文概览。

    妍妍要的是「让它读自己想读的」，所以版面归它。
    但排版不归它——报纸长什么样是上游那套 CSS 决定的，
    它只能决定读什么、每一版怎么写、什么时候出。

    概览那一行是关键：定时改成一周一次之后，「想看就出」需要它知道
    多久没出了。搭在心跳已有的那次调用上，不额外花钱。
    """
    try:
        import agent_tools_feature as agent
    except ImportError as exc:
        print("[dwell] 日报没接上工具层: " + str(exc))
        return

    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_news",
                "description": (
                    "翻某一期日报。妍妍提起「那条新闻」时用，不要凭印象答。"
                    "不给日期就是最近一期。"
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
        },
        {
            "type": "function",
            "function": {
                "name": "make_news",
                "description": (
                    "现在出一份新报纸。想看新的了就调这个，不用等定时。"
                    "会花一分多钟、五次模型调用，所以别频繁调——"
                    "隔几天想看了再出，或者妍妍问起最近有什么事的时候。"
                    "出好了会推一条通知给她。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_news_sections",
                "description": (
                    "看日报现在有哪些版块、每一版的写法和抓料源。"
                    "日报的版面是你自己定的，想调整之前先看这个。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tune_news_section",
                "description": (
                    "改某一版「这一版怎么写」的那句话。这是决定稿子质量最关键的东西："
                    "同样的素材，这句话不一样，写出来差一个档次。"
                    "觉得某一版太干、太啰嗦、跑偏了，就改这里。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "版块名，要和现有的一致。"},
                        "extra": {
                            "type": "string",
                            "description": "这一版怎么写。写具体一点，比如「每条不超过三句」。",
                        },
                    },
                    "required": ["name", "extra"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_news_section",
                "description": (
                    "加一个新版块。你自己想读什么就加什么——论文、开源项目、"
                    "某个领域的动向都行，不用只挑妍妍关心的。"
                    "抓料源给 RSS 地址（http 开头），留空就用百度热搜。"
                    "最多 8 个版块，加太多出报会很慢。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "版块名，比如「论文」。"},
                        "extra": {"type": "string", "description": "这一版怎么写。"},
                        "rss": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "RSS 地址，最多 4 个。留空就用百度热搜。",
                        },
                    },
                    "required": ["name", "extra"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remove_news_section",
                "description": "删掉一个版块。某一版你一直觉得没意思就删了，不用留着。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "要删的版块名。"}
                    },
                    "required": ["name"],
                },
            },
        },
    ]

    known = {t["function"]["name"] for t in agent.TOOLS}
    for tool in tools:
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

        if name == "make_news":
            if running():
                return {"error": "正在编了，等一分钟"}
            ok, message = build_async(force=True)
            if not ok:
                return {"error": message}
            return {
                "ok": True,
                "detail": message,
                "提醒": "编完会推通知给她。这会儿别接着调第二次。",
            }

        if name == "list_news_sections":
            return {
                "sections": [
                    {
                        "name": s.get("name"),
                        "extra": s.get("extra"),
                        "sources": [
                            str(x.get("url") or x.get("kind"))
                            for x in (s.get("sources") or [])
                        ],
                    }
                    for s in sections()
                ],
                "说明": "改写法用 tune_news_section，加版块用 add_news_section。",
            }

        if name == "tune_news_section":
            want = str(args.get("name") or "").strip()
            extra = str(args.get("extra") or "").strip()[:MAX_EXTRA_CHARS]
            if not extra:
                return {"error": "写法不能空"}
            items = sections()
            for section in items:
                if str(section.get("name")) == want:
                    section["extra"] = extra
                    save_sections(items, "沐")
                    return {"ok": True, "改了": want, "新的写法": extra}
            return {
                "error": "没有叫「" + want + "」的版块",
                "现有": [s.get("name") for s in items],
            }

        if name == "add_news_section":
            want = str(args.get("name") or "").strip()[:40]
            extra = str(args.get("extra") or "").strip()[:MAX_EXTRA_CHARS]
            if not want or not extra:
                return {"error": "版块名和写法都要给"}
            items = sections()
            if any(str(s.get("name")) == want for s in items):
                return {"error": "已经有这个版块了，改写法用 tune_news_section"}
            if len(items) >= MAX_SECTIONS:
                return {
                    "error": "最多 " + str(MAX_SECTIONS) + " 个版块了，"
                             "想加就先用 remove_news_section 删一个",
                }

            raw = args.get("rss")
            urls = raw if isinstance(raw, list) else ([raw] if raw else [])
            sources = clean_sources(
                [{"kind": "rss", "url": u, "n": 12} for u in urls]
            ) or [{"kind": "baidu", "n": 14}]

            items.append({"name": want, "extra": extra, "sources": sources})
            save_sections(items, "沐")
            return {
                "ok": True,
                "加了": want,
                "抓料源": [str(x.get("url") or x.get("kind")) for x in sources],
                "提醒": "下一期就会有这一版。RSS 通不通看 /api/news/status。",
            }

        if name == "remove_news_section":
            want = str(args.get("name") or "").strip()
            items = sections()
            left = [s for s in items if str(s.get("name")) != want]
            if len(left) == len(items):
                return {
                    "error": "没有叫「" + want + "」的版块",
                    "现有": [s.get("name") for s in items],
                }
            if not left:
                return {"error": "至少留一个版块"}
            save_sections(left, "沐")
            return {"ok": True, "删了": want, "剩下": [s.get("name") for s in left]}

        return original_execute(server, name, args)

    agent.execute_tool = execute_with_news

    # ── 把「上次日报多久了」接进上下文概览
    #
    # 这是「想看就出」的实现方式。不单独发请求去问它要不要看报——
    # 那个「问」本身就是一次调用，为了省钱又花钱。
    # 沐每晚心跳醒来都会读一遍概览，看到这一行自己决定。

    original_snapshot = agent.build_context_snapshot

    def snapshot_with_news(server):
        text = original_snapshot(server)
        try:
            gap = days_since_issue()
            every = every_days()
            if gap is None:
                line = (
                    "【日报】还没出过任何一期。想看就调 make_news 出一份，"
                    "顺便也可以先用 list_news_sections 看看版面是不是你想读的。"
                )
            elif gap >= every:
                line = (
                    "【日报】上一期是 " + str(gap) + " 天前，已经超过 "
                    + str(every) + " 天了。想看新的就调 make_news——"
                    "不想看就算了，没人催你。"
                )
            elif gap >= 2:
                line = "【日报】上一期是 " + str(gap) + " 天前。想提前看就调 make_news。"
            else:
                line = ""
            return text + "\n" + line if line else text
        except Exception:
            return text

    agent.build_context_snapshot = snapshot_with_news
