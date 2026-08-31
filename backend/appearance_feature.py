"""dwell 外观：气泡拆分、头像、时间戳、日期条、引用、字体，和一个能实时调的面板。

不改 web/index.html。在 index 视图外面再包一层，把客户端脚本注进去。

字号是**分路**的：正文、语音条、工具行、引用、输入框各有自己的变量和滑块。
一开始做成一个滑块牵着全部按比例缩，实际不好看 —— 那几处本来就不是
一个量级的东西，同一个比例套上去总有一处别扭。

⚠️ CSS 变量挂在 `html` 上（`--apv-` 前缀撞不着别人），所以 `#log` 外面的
输入框、引用条也能用。但那几个 `apv-hide-*` 开关 class 仍然挂在 `#log`
上 —— 挂到 body 上有溢出风险。

⚠️ 样式选择器一律锁进 `#log`、`#apvSheet` 或 `.composer`，一条都不能漏。
`.row` 这个 class 名在 index.html 里被复用了至少四处（`.hd-write .row`、
`.hadd .row`、`.rc-add .row`，锁屏那层里也有）。写成裸 `.row{display:flex}`
会把锁屏的布局压掉 —— `track.clientWidth` 算错、`MAX()` 归零、滑块拖不动，
而锁屏靠 `setPointerCapture` 抓指针，布局一崩解锁手势就整个没了。

⚠️ 输入框字号别低于 16px：iOS Safari 聚焦一个字号小于 16px 的输入框时
会自动放大整个页面，回不去。滑块下限因此钉在 16。

⚠️ 要盖住 voice_feature 里写死的字号（`.vz-dur` 那些）得靠**特异性**：
那边是 `.vz-dur`（0,1,0），这边必须写成 `#log .row.apv .vz-dur`（1,2,0）。
只写 `.vz-dur` 是同特异性，而这段样式注入在它前面，会输。

⚠️ `@font-face` 的 `src` 一定要带 `format()`，Safari 少了它有时整条规则
都不认。字体文件的地址带 mtime 版本号，换同名字体才会重新拉。

⚠️ 上游 `.sheet` 自己**没有**左右内边距 —— 内容本该放进 `.sheet .body`。
往 `.sheet` 里直接塞东西必须自己补内边距，否则 `margin-left:auto` 的开关
会顶到屏幕外面。

⚠️ 气泡圆角别用 999px：单行是圆条，多行会撑成椭圆。用 20px。

⚠️ 拆气泡只拆定稿的（`el._final`）。流式那条边写边拆会一帧一个样。
代码块里的空行不算分割点 —— 按 ``` 的奇偶记状态，围栏内跳过。

⚠️ 引用的发送要走**捕获阶段**插队：上游是 `sendBtn.onclick = send`，
捕获阶段的监听比 onclick 先跑，在那儿把引用拼进 `box.value` 即可。

设置存在 settings 表里，不用 localStorage：换个设备就没了。

删掉 run.py 里那一行就完全没有这个功能，别的都不受影响。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from flask import jsonify, request, send_file

# 自由 CSS 那一栏的上限。写错了顶多这一页难看，但别让它把库撑大。
CSS_MAX = 20000

SETTINGS_KEY = "appearance"

DEFAULTS = {
    "avatarSize": 34,       # 头像直径 px
    "fontSize": 14.5,       # 气泡正文 px
    "voiceSize": 14.5,      # 语音条转写文字 px（时长自动取 .82 倍）
    "toolSize": 14,         # 工具行 px（Thought process 那种）
    "quoteSize": 13,        # 气泡里那段引用 px
    "composerSize": 16,     # 输入框字号 px（低于 16 iOS 会放大页面）
    "composerPad": 12,      # 输入框内边距 px
    "btnSize": 36,          # 输入框那排圆按钮 px
    "bubbleRadius": 18,     # 气泡圆角 px
    "rowGap": 16,           # 两条消息之间 px
    "splitGap": 6,          # 同一条消息拆出来的段之间 px
    "timeSize": 10.5,       # 时间戳 px
    "gapHours": 5,          # 隔这么久才插日期分隔条
    "showAvatar": True,
    "showTime": True,
    "showDay": True,
    "split": True,          # 空行拆成独立气泡
    "quote": True,          # 长按气泡能引用
    "toolMode": "align",    # 工具行：align 跟气泡对齐 / full 独占一行 / hide 藏起来
    "fontFamily": "",       # 传上来的字体名；空 = 用系统字体
    "css": "",              # 自由 CSS，原样注入
}

NUM_RANGE = {
    "avatarSize": (18, 72),
    "fontSize": (11, 22),
    "voiceSize": (10, 22),
    "toolSize": (9, 20),
    "quoteSize": (9, 20),
    # ⚠️ 下限 16：iOS Safari 聚焦小于 16px 的输入框会放大整个页面
    "composerSize": (16, 22),
    "composerPad": (4, 20),
    "btnSize": (28, 48),
    "bubbleRadius": (0, 28),
    "rowGap": (2, 40),
    "splitGap": (0, 24),
    "timeSize": (8, 16),
    "gapHours": (0.5, 24),
}

TOOL_MODES = ("align", "full", "hide")

ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}

# 中文字体动辄好几兆，所以比头像那条宽得多
FONT_MAX = 12 * 1024 * 1024
# ⚠️ format() 是 @font-face 的必需品，Safari 少了它有时整条规则不认
FONT_EXT = {
    ".ttf": ("font/ttf", "truetype"),
    ".otf": ("font/otf", "opentype"),
    ".woff": ("font/woff", "woff"),
    ".woff2": ("font/woff2", "woff2"),
    ".ttc": ("font/collection", "collection"),
}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")


def _clean(raw: dict) -> dict:
    """把传进来的设置钳到合法范围。坏值一律回落到默认，不报错。

    旧设置里没有新加的那几个键，这儿会用默认值补上 —— 升级后看着
    跟原来一样，想分开调再动滑块。
    """
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for key, (low, high) in NUM_RANGE.items():
        if key in raw:
            try:
                out[key] = max(low, min(high, float(raw[key])))
            except (TypeError, ValueError):
                pass
    for key in ("showAvatar", "showTime", "showDay", "split", "quote"):
        if key in raw:
            out[key] = bool(raw[key])
    if raw.get("toolMode") in TOOL_MODES:
        out["toolMode"] = raw["toolMode"]
    if isinstance(raw.get("fontFamily"), str):
        out["fontFamily"] = SAFE_NAME.sub("", raw["fontFamily"])[:80]
    if "css" in raw and isinstance(raw["css"], str):
        out["css"] = raw["css"][:CSS_MAX]
    return out


CLIENT_SCRIPT = r"""
<style id="apv-base">
/* ⚠️ 变量挂在 html 上，不是 #log —— 输入框和引用条在 #log 外面，
   也得能用。前缀 --apv- 撞不着别人的名字。 */
html{
  --apv-av:34px; --apv-font:14.5px; --apv-voice:14.5px;
  --apv-tool:14px; --apv-quote:13px;
  --apv-cfont:16px; --apv-cpad:12px; --apv-btn:36px;
  --apv-radius:18px; --apv-gap:16px; --apv-split:6px; --apv-time:10.5px;
  --apv-family:inherit;
}

/* ⚠️ 下面每一条都必须带 #log / .composer 前缀。
   .row 这个名字在 index.html 里被复用了至少四处，锁屏那层也有 ——
   写成裸 .row 会把锁屏滑块的布局压掉，解锁手势直接失效。 */
#log .row.apv{display:flex;align-items:flex-start;gap:9px;
  margin-bottom:var(--apv-gap)}
#log .row.me.apv{flex-direction:row-reverse;justify-content:flex-start}
#log .row.apv > .bubble,
#log .row.apv > .gu{min-width:0;font-size:var(--apv-font);
  font-family:var(--apv-family)}
#log .row.apv > .bubble{border-radius:var(--apv-radius)}
/* 同一条消息拆出来的几段之间收紧，跟"两条消息"区分开 */
#log .row.apv.apv-split{margin-bottom:var(--apv-split)}

#log .apv-side{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;
  gap:3px;width:var(--apv-av);padding-top:2px}
#log .apv-av{width:var(--apv-av);height:var(--apv-av);border-radius:50%;
  background:var(--panel,#f0eee6) center/cover no-repeat;
  display:flex;align-items:center;justify-content:center;
  font-size:calc(var(--apv-av) * .42);color:var(--dim,#8a867c);
  font-weight:500;overflow:hidden;-webkit-user-select:none;user-select:none}
#log .apv-time{font-size:var(--apv-time);line-height:1.25;color:var(--dim,#8a867c);
  opacity:.72;white-space:nowrap;font-variant-numeric:tabular-nums;
  text-align:center;letter-spacing:.01em}

/* ── 语音条：自己一路字号 ──────────────────────────────────────
   ⚠️ voice_feature 那边把 .vz-dur 写死成 12px、.vz-txt 写死成 14.5px。
   要盖住它得靠特异性：那边是 (0,1,0)，这边 #log .row.apv .vz-dur 是
   (1,2,0)。只写 .vz-dur 是平手，而这段注入在它前面，会输。 */
#log .row.apv .vz{font-size:var(--apv-voice);
  min-width:calc(var(--apv-voice) * 7.2);
  gap:calc(var(--apv-voice) * .62);
  padding:calc(var(--apv-voice) * .48) calc(var(--apv-voice) * .9)
          calc(var(--apv-voice) * .48) calc(var(--apv-voice) * .76)}
#log .row.apv .vz-ico{width:calc(var(--apv-voice) * 1.04);
  height:calc(var(--apv-voice) * 1.04)}
#log .row.apv .vz-wave{height:calc(var(--apv-voice) * 1.04)}
#log .row.apv .vz-dur{font-size:calc(var(--apv-voice) * .82)}
#log .row.apv .vz-txt{font-size:var(--apv-voice);
  font-family:var(--apv-family);
  margin-top:calc(var(--apv-voice) * .48)}

/* ── 工具行：自己一路字号。上游写死 14.5px，这儿接过来 ── */
#log .row .toolline{font-size:var(--apv-tool);font-family:var(--apv-family)}
#log .row .toolline .ic,
#log .row .toolline .spin{width:calc(var(--apv-tool) * .94);
  height:calc(var(--apv-tool) * .94)}
#log .row .toolline .chev{font-size:calc(var(--apv-tool) * .9)}

/* 日期分隔条。是 #log 的直接子元素，不进 .row。 */
#log .apv-day{display:flex;justify-content:center;
  margin:calc(var(--apv-gap) + 6px) auto;max-width:720px}
#log .apv-day > span{font-size:calc(var(--apv-time) + .5px);
  color:var(--dim,#8a867c);opacity:.78;padding:3px 11px;border-radius:999px;
  border:1px solid var(--line,#e8e5dc);background:transparent;
  letter-spacing:.02em;white-space:nowrap;font-variant-numeric:tabular-nums}

/* ── 引用：自己一路字号（气泡里那段 + 输入框上方那条） ── */
#log .apv-qbox{border-left:2.5px solid var(--line,#e8e5dc);
  padding:1px 0 1px 10px;margin:0 0 7px;opacity:.78;
  font-size:var(--apv-quote);line-height:1.6;
  white-space:pre-wrap;word-break:break-word}
#log .apv-qbox b{display:block;font-weight:600;
  font-size:calc(var(--apv-quote) * .9);opacity:.7;margin-bottom:2px}

/* 工具行缩进：默认跟气泡左边对齐 */
#log.apv-tool-align .row:not(.apv) > .toolline{
  margin-left:calc(var(--apv-av) + 9px)}
#log.apv-tool-hide .row:not(.apv) > .toolline{display:none}

/* 开关那几个 class 挂在 #log 上，不挂 body（挂 body 有溢出风险） */
#log.apv-hide-av .apv-av{display:none}
#log.apv-hide-av .apv-side{width:auto}
#log.apv-hide-time .apv-time{display:none}
#log.apv-hide-av.apv-hide-time .apv-side{display:none}
#log.apv-hide-day .apv-day{display:none}

/* ── 输入框那一坨 ──────────────────────────────────────────────
   ⚠️ #box 的字号别低于 16px：iOS Safari 聚焦时会放大整个页面。
   滑块下限已经钉在 16，这儿只是接住那个值。 */
.composer{padding:var(--apv-cpad) calc(var(--apv-cpad) + 2px)
          calc(var(--apv-cpad) - 2px)}
.composer #box{font-size:var(--apv-cfont);font-family:var(--apv-family);
  padding:2px 2px calc(var(--apv-cpad) * .6)}
.composer .ctlrow{gap:calc(var(--apv-btn) * .22)}
.composer #plusBtn,
.composer #vzMic,
.composer #vcBtn{width:var(--apv-btn);height:var(--apv-btn);flex:0 0 auto}
.composer #plusBtn .ic,
.composer #vzMic svg,
.composer #vcBtn svg{width:calc(var(--apv-btn) * .47);
  height:calc(var(--apv-btn) * .47)}
.composer #send{width:calc(var(--apv-btn) * 1.06);
  height:calc(var(--apv-btn) * 1.06)}
.composer #send .ic{width:calc(var(--apv-btn) * .47);
  height:calc(var(--apv-btn) * .47)}
.composer .pill{font-size:calc(var(--apv-cfont) * .86);
  font-family:var(--apv-family);
  padding:calc(var(--apv-btn) * .2) calc(var(--apv-btn) * .38)}

/* ── 长按气泡弹出来那个小菜单 ── */
#apvMenu{position:fixed;z-index:12500;display:none;gap:2px;
  background:var(--card,#fff);border:1px solid var(--line,#e8e5dc);
  border-radius:14px;padding:5px;font-family:var(--apv-family);
  box-shadow:0 8px 28px rgba(0,0,0,.14)}
#apvMenu.on{display:flex}
#apvMenu button{border:0;background:transparent;color:var(--text,#2b2a27);
  font-family:inherit;font-size:14px;padding:9px 15px;border-radius:10px;
  cursor:pointer;white-space:nowrap}
#apvMenu button:active{background:var(--panel,#f0eee6)}

/* ── 输入框上方那条引用 ── */
#apvQuote{display:none;align-items:flex-start;gap:9px;
  margin:0 0 8px;padding:8px 10px 8px 0;
  border-left:2.5px solid var(--accent,#c96442);
  background:transparent}
#apvQuote.on{display:flex}
#apvQuote .qt{flex:1 1 auto;min-width:0;padding-left:10px;
  font-size:var(--apv-quote);font-family:var(--apv-family);
  line-height:1.55;color:var(--dim,#8a867c);
  max-height:3.4em;overflow:hidden}
#apvQuote .qt b{display:block;color:var(--text,#2b2a27);opacity:.8;
  font-weight:600;font-size:calc(var(--apv-quote) * .9);margin-bottom:1px}
#apvQuote .qx{flex:0 0 auto;width:28px;height:28px;border:0;padding:0;
  background:transparent;color:var(--dim,#8a867c);cursor:pointer;
  display:flex;align-items:center;justify-content:center}
#apvQuote .qx svg{width:15px;height:15px}

/* ── 面板 ─────────────────────────────────────────────────────
   ⚠️ 上游 .sheet 自己没有左右内边距（内容本该放进 .sheet .body）。
   这儿直接往 .sheet 里塞东西，所以必须自己补 20px —— 不补的话
   margin-left:auto 的开关会顶到屏幕外面，右半截被切掉。 */
#apvSheet .sheet{box-sizing:border-box;width:100%;max-width:100vw;
  overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding:0 20px calc(env(safe-area-inset-bottom,0px) + 18px)}
#apvSheet .sheet *{box-sizing:border-box}
#apvSheet .grabber{width:38px;margin-left:auto;margin-right:auto}
#apvSheet .apv-h{font-size:19px;font-weight:600;margin:2px 0 14px;
  color:var(--text,#2b2a27)}
/* 小节标题：十几个滑块堆一起找不到要调哪个，分成几组 */
#apvSheet .apv-grp{font-size:11px;letter-spacing:.12em;
  color:var(--dim,#8a867c);opacity:.7;margin:15px 0 2px}
#apvSheet .apv-line{display:flex;align-items:center;gap:10px;padding:7px 0;
  min-width:0}
#apvSheet .apv-line > label{flex:0 0 66px;min-width:0;font-size:13.5px;
  color:var(--dim,#8a867c);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#apvSheet .apv-line input[type=range]{flex:1 1 0;min-width:0;width:auto;
  -webkit-appearance:none;appearance:none;height:26px;background:transparent;
  margin:0}
#apvSheet .apv-line input[type=range]::-webkit-slider-runnable-track{height:3px;
  border-radius:2px;background:var(--line,#e8e5dc)}
#apvSheet .apv-line input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
  width:20px;height:20px;border-radius:50%;background:#fff;margin-top:-8.5px;
  border:1px solid var(--line,#e8e5dc);box-shadow:0 1px 4px rgba(0,0,0,.14)}
#apvSheet .apv-val{flex:0 0 38px;min-width:0;text-align:right;font-size:12.5px;
  color:var(--dim,#8a867c);font-variant-numeric:tabular-nums}
#apvSheet .apv-sw{flex:0 0 auto;margin-left:auto;-webkit-appearance:none;
  appearance:none;width:44px;height:26px;border-radius:999px;
  background:var(--line,#e8e5dc);position:relative;transition:background .2s ease;
  border:0;padding:0;cursor:pointer}
#apvSheet .apv-sw::after{content:'';position:absolute;top:3px;left:3px;
  width:20px;height:20px;border-radius:50%;background:#fff;
  transition:transform .2s ease;box-shadow:0 1px 3px rgba(0,0,0,.18)}
#apvSheet .apv-sw.on{background:var(--accent,#c96442)}
#apvSheet .apv-sw.on::after{transform:translateX(18px)}
/* 三档那个：一排小药丸 */
#apvSheet .apv-seg{flex:1 1 0;min-width:0;display:flex;gap:4px;padding:3px;
  background:var(--panel,#f0eee6);border-radius:999px}
#apvSheet .apv-seg button{flex:1 1 0;min-width:0;border:0;background:transparent;
  color:var(--dim,#8a867c);font-family:inherit;font-size:12.5px;padding:7px 4px;
  border-radius:999px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#apvSheet .apv-seg button.on{background:var(--card,#fff);color:var(--text,#2b2a27);
  font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.08)}
#apvSheet .apv-avs{display:flex;gap:10px;padding:6px 0 2px;min-width:0}
#apvSheet .apv-pick{flex:1 1 0;min-width:0;display:flex;align-items:center;
  gap:9px;padding:9px;border:1px dashed var(--line,#e8e5dc);border-radius:14px;
  cursor:pointer;font-size:12.5px;color:var(--dim,#8a867c)}
#apvSheet .apv-pick span.t{min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#apvSheet .apv-pick i{flex:0 0 auto;width:34px;height:34px;border-radius:50%;
  background:var(--panel,#f0eee6) center/cover no-repeat;
  display:flex;align-items:center;justify-content:center;font-style:normal;
  font-size:14px;color:var(--dim,#8a867c);overflow:hidden}
#apvSheet .apv-pick input{display:none}
/* 字体那一块 */
#apvSheet .apv-font{border:1px dashed var(--line,#e8e5dc);border-radius:14px;
  padding:11px 13px;margin-top:4px}
#apvSheet .apv-font .fname{font-size:13.5px;color:var(--text,#2b2a27);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:var(--apv-family)}
#apvSheet .apv-font .fsub{font-size:11.5px;color:var(--dim,#8a867c);
  opacity:.85;margin-top:3px;line-height:1.6}
#apvSheet .apv-font .frow{display:flex;gap:8px;margin-top:10px;min-width:0}
#apvSheet .apv-font label.up,
#apvSheet .apv-font button{flex:1 1 0;min-width:0;min-height:38px;
  border-radius:999px;border:0;background:var(--panel,#f0eee6);
  color:var(--text,#2b2a27);font-size:13px;font-family:inherit;cursor:pointer;
  display:flex;align-items:center;justify-content:center;padding:0 10px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#apvSheet .apv-font input[type=file]{display:none}
#apvSheet .apv-font .try{font-size:calc(var(--apv-font) * 1);
  font-family:var(--apv-family);color:var(--text,#2b2a27);
  margin-top:10px;line-height:1.7;opacity:.9}
#apvSheet .apv-css{display:block;width:100%;min-width:0;min-height:104px;
  resize:vertical;background:var(--panel,#f0eee6);border:1px solid transparent;
  border-radius:14px;padding:11px 13px;color:var(--text,#2b2a27);
  font-size:12.5px;line-height:1.6;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
#apvSheet .apv-note{font-size:11.5px;color:var(--dim,#8a867c);opacity:.85;
  margin:8px 0 0;line-height:1.75;word-break:break-word}
#apvSheet .apv-note code{background:var(--panel,#f0eee6);border-radius:5px;
  padding:1px 5px;font-size:11px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
#apvSheet .apv-note b{display:block;margin:10px 0 3px;font-weight:600;
  color:var(--text,#2b2a27);opacity:.72;font-size:11.5px}
#apvSheet .apv-note i{font-style:normal;opacity:.72}
#apvSheet .apv-btns{display:flex;gap:9px;margin-top:16px;min-width:0}
#apvSheet .apv-btns button{flex:1 1 0;min-width:0;min-height:44px;
  border-radius:999px;border:0;background:var(--panel,#f0eee6);
  color:var(--text,#2b2a27);font-size:14px;font-family:inherit;cursor:pointer;
  padding:0 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#apvSheet .apv-btns button.go{background:var(--accent,#c96442);color:#fff}
#apvSheet .apv-sep{height:1px;background:var(--line,#e8e5dc);margin:14px 0;
  opacity:.7}
</style>
<style id="apv-font"></style>
<style id="apv-user"></style>
<script>
(function () {
  if (window.dwellAppearance) return;

  var DEF = {
    avatarSize: 34, fontSize: 14.5, voiceSize: 14.5, toolSize: 14, quoteSize: 13,
    composerSize: 16, composerPad: 12, btnSize: 36,
    bubbleRadius: 18, rowGap: 16, splitGap: 6, timeSize: 10.5, gapHours: 5,
    showAvatar: true, showTime: true, showDay: true, split: true, quote: true,
    toolMode: 'align', fontFamily: '', css: ''
  };
  var cfg = JSON.parse(JSON.stringify(DEF));

  /* 滑块分三组。每项：[键, 标签, 最小, 最大, 步长] */
  var SIZE_META = [
    ['fontSize', '正文', 11, 22, 0.5],
    ['voiceSize', '语音', 10, 22, 0.5],
    ['toolSize', '工具行', 9, 20, 0.5],
    ['quoteSize', '引用', 9, 20, 0.5],
    ['timeSize', '时间', 8, 16, 0.5],
    ['avatarSize', '头像', 18, 72, 1]
  ];
  var GAP_META = [
    ['bubbleRadius', '圆角', 0, 28, 1],
    ['rowGap', '行距', 2, 40, 1],
    ['splitGap', '段距', 0, 24, 1],
    ['gapHours', '隔多久', 0.5, 24, 0.5]
  ];
  /* ⚠️ composerSize 最低 16：iOS Safari 聚焦更小的输入框会放大整页 */
  var BOX_META = [
    ['composerSize', '输入字号', 16, 22, 0.5],
    ['composerPad', '内边距', 4, 20, 1],
    ['btnSize', '按钮', 28, 48, 1]
  ];
  var META = SIZE_META.concat(GAP_META, BOX_META);
  var TOOLS = [['align', '对齐'], ['full', '整行'], ['hide', '藏起来']];
  var NAME = { me: '妍妍', gu: '沐' };
  var FONT_OK = '.ttf,.otf,.woff,.woff2,.ttc';

  var PRESET = [
    '/* 圆条气泡 · 不想要了就清空这一栏 */',
    '#log .row.apv > .bubble,',
    '#log .row.apv > .gu {',
    '  padding: 9px 15px;',
    '  border-radius: 20px;',
    '  line-height: 1.62;',
    '}',
    '',
    '/* 你说的 */',
    '#log .row.apv > .bubble {',
    '  background: rgba(201, 100, 66, .10);',
    '  border: 1px solid rgba(201, 100, 66, .16);',
    '  color: var(--text);',
    '}',
    '',
    '/* 他说的 */',
    '#log .row.apv > .gu {',
    '  background: var(--card);',
    '  border: 1px solid var(--line);',
    '}',
    '',
    '/* 表情包和图片：别包壳，也别被圆角切了 */',
    '#log .row.apv-img > .bubble,',
    '#log .row.apv-img > .gu {',
    '  padding: 0;',
    '  background: transparent;',
    '  border: 0;',
    '}',
    '#log .row.apv img { border-radius: 14px; }',
    '',
    '/* 拆出来的中间几段：头像淡一点，一组看着才连贯 */',
    '#log .row.apv-split:not(.apv-first) .apv-av { opacity: .45; }'
  ].join('\n');

  function logEl() { return document.getElementById('log'); }

  /* ── 时间戳来源 ────────────────────────────────────────────────
     DOM 里的行不带 at，所以启动时拉一次 api/messages，按 kind|text
     建队列去配。用队列而不是按序号对齐 —— 「看更早的消息」会往前插行，
     序号会错位。配不上的（刚发的）用当下时间。 */
  var stampQ = Object.create(null);
  var stamps = new WeakMap();
  var loaded = false;

  function keyOf(mine, text) {
    return (mine ? 'm|' : 'g|') + (text || '').trim().slice(0, 120);
  }

  function loadStamps() {
    return fetch('api/messages?limit=400', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var msgs = (d && d.msgs) || [];
        for (var i = 0; i < msgs.length; i++) {
          var m = msgs[i];
          var mine = m.kind === 'me' || m.kind === 'her';
          if (!mine && m.kind !== 'gu') continue;
          var k = keyOf(mine, m.text);
          (stampQ[k] || (stampQ[k] = [])).push(m.at);
        }
      })
      .catch(function () {})
      .then(function () { loaded = true; });
  }

  function stampFor(row, mine, text) {
    if (stamps.has(row)) return stamps.get(row);
    var q = stampQ[keyOf(mine, text)];
    var at = (q && q.length) ? q.shift() : Math.floor(Date.now() / 1000);
    stamps.set(row, at);
    return at;
  }

  function clock(at) {
    var d = new Date(at * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0') + ':' +
           String(d.getSeconds()).padStart(2, '0');
  }

  function dayLabel(at) {
    var d = new Date(at * 1000);
    var now = new Date();
    var hm = String(d.getHours()).padStart(2, '0') + ':' +
             String(d.getMinutes()).padStart(2, '0');
    if (d.toDateString() === now.toDateString()) return '今天 ' + hm;
    var y = new Date(now.getTime() - 86400000);
    if (d.toDateString() === y.toDateString()) return '昨天 ' + hm;
    var head = (d.getMonth() + 1) + '月' + d.getDate() + '日';
    if (d.getFullYear() !== now.getFullYear()) head = d.getFullYear() + '年' + head;
    return head + ' ' + hm;
  }

  /* ── 字体 ──────────────────────────────────────────────────────
     ⚠️ src 一定要带 format()，Safari 少了它有时整条 @font-face 都不认。
     地址带 v= 版本号（服务端给的 mtime），换同名字体才会重新拉。 */
  var fontInfo = { name: '', v: '', fmt: '' };

  function applyFont() {
    var tag = document.getElementById('apv-font');
    var use = cfg.fontFamily && fontInfo.name === cfg.fontFamily;
    if (tag) {
      tag.textContent = use ?
        '@font-face{font-family:"apvUser";' +
        'src:url("api/appearance/font?v=' + fontInfo.v + '")' +
        (fontInfo.fmt ? ' format("' + fontInfo.fmt + '")' : '') + ';' +
        'font-display:swap}' : '';
    }
    document.documentElement.style.setProperty(
      '--apv-family',
      use ? '"apvUser", -apple-system, "PingFang SC", sans-serif' : 'inherit');
  }

  /* ── 应用设置 ───────────────────────────────────────────────── */
  function apply() {
    var s = document.documentElement.style;
    s.setProperty('--apv-av', cfg.avatarSize + 'px');
    s.setProperty('--apv-font', cfg.fontSize + 'px');
    s.setProperty('--apv-voice', cfg.voiceSize + 'px');
    s.setProperty('--apv-tool', cfg.toolSize + 'px');
    s.setProperty('--apv-quote', cfg.quoteSize + 'px');
    s.setProperty('--apv-cfont', cfg.composerSize + 'px');
    s.setProperty('--apv-cpad', cfg.composerPad + 'px');
    s.setProperty('--apv-btn', cfg.btnSize + 'px');
    s.setProperty('--apv-radius', cfg.bubbleRadius + 'px');
    s.setProperty('--apv-gap', cfg.rowGap + 'px');
    s.setProperty('--apv-split', cfg.splitGap + 'px');
    s.setProperty('--apv-time', cfg.timeSize + 'px');

    var L = logEl();
    if (L) {
      L.classList.toggle('apv-hide-av', !cfg.showAvatar);
      L.classList.toggle('apv-hide-time', !cfg.showTime);
      L.classList.toggle('apv-hide-day', !cfg.showDay);
      L.classList.toggle('apv-tool-align', cfg.toolMode === 'align');
      L.classList.toggle('apv-tool-hide', cfg.toolMode === 'hide');
    }
    applyFont();
    var tag = document.getElementById('apv-user');
    if (tag) tag.textContent = cfg.css || '';
  }

  var avSrc = { me: '', gu: '' };
  function avatarUrl(who) {
    return 'api/appearance/avatar/' + who + '?v=' + (avSrc[who] || '0');
  }
  function paintAvatars() {
    var els = document.querySelectorAll('.apv-av[data-who],[data-pick]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var who = el.getAttribute('data-who') || el.getAttribute('data-pick');
      if (avSrc[who]) {
        el.style.backgroundImage = 'url("' + avatarUrl(who) + '")';
        el.textContent = '';
      } else {
        el.style.backgroundImage = '';
        el.textContent = who === 'me' ? '妍' : '沐';
      }
    }
  }

  /* ── 按空行切段 ────────────────────────────────────────────────
     ⚠️ 代码块里的空行不算分割点。按 ``` 的奇偶记状态，围栏内跳过 ——
     不然一段代码会被腰斩成两个气泡，语法高亮和复制按钮都跟着废。 */
  function segments(text) {
    var lines = String(text || '').split('\n');
    var out = [], cur = [], fence = false, blanks = 0;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (/^\s*```/.test(ln)) fence = !fence;
      if (!fence && !ln.trim()) { blanks++; continue; }
      if (blanks > 0 && cur.length) { out.push(cur.join('\n')); cur = []; }
      blanks = 0;
      cur.push(ln);
    }
    if (cur.length) out.push(cur.join('\n'));
    return out.filter(function (s) { return s.trim(); });
  }

  /* 开头连着的 > 那几行是引用，切出来单独渲染 */
  function splitQuote(text) {
    var lines = String(text || '').split('\n');
    var q = [], i = 0;
    while (i < lines.length && /^\s*>/.test(lines[i])) {
      q.push(lines[i].replace(/^\s*>\s?/, ''));
      i++;
    }
    if (!q.length) return null;
    while (i < lines.length && !lines[i].trim()) i++;
    return { quote: q.join('\n').trim(), rest: lines.slice(i).join('\n') };
  }

  /* 气泡里那段引用：包成左边一道竖线的块 */
  function dressQuote(bub, row) {
    if (bub._apvQ) return;
    var raw = bub._raw != null ? bub._raw : bub.textContent;
    var got = splitQuote(raw);
    if (!got) return;
    bub._apvQ = 1;
    bub._raw = got.rest;
    bub.textContent = got.rest;
    bub.removeAttribute('data-rich');
    try { renderRich(bub); } catch (e) {}
    var box = document.createElement('div');
    box.className = 'apv-qbox';
    var m = got.quote.match(/^([^\n：:]{1,12})[：:]\s*([\s\S]*)$/);
    if (m) {
      var who = document.createElement('b');
      who.textContent = m[1];
      box.appendChild(who);
      box.appendChild(document.createTextNode(m[2]));
    } else {
      box.textContent = got.quote;
    }
    bub.insertBefore(box, bub.firstChild);
    if (row) row.classList.add('apv-quoted');
  }

  /* ── 给一行挂上所有能当选择器用的钩子 ──────────────────────────
     原来只有 .apv / .apv-split 两个，能改的地方太少。 */
  function tagRow(row, bub, mine, at) {
    row.classList.add('apv', mine ? 'apv-me' : 'apv-gu');
    row.setAttribute('data-who', mine ? 'me' : 'gu');
    row.setAttribute('data-at', String(at));
    var raw = (bub._raw != null ? bub._raw : bub.textContent) || '';
    if (raw.indexOf('[voice') === 0) row.classList.add('apv-voice');
    if (bub.querySelector('img')) row.classList.add('apv-img');
    if (bub.querySelector('.cbk, pre')) row.classList.add('apv-code');
  }

  /* 建一个跟母气泡同款的行。第一段留在原位，其余走这条。 */
  function makeRow(mine, text, at) {
    var row = document.createElement('div');
    row.className = 'row' + (mine ? ' me' : '') + ' apv apv-split';
    row.setAttribute('data-apv', '1');
    var bub = document.createElement('div');
    bub.className = mine ? 'bubble' : 'gu';
    bub._raw = text;
    bub._final = true;
    bub.textContent = text;
    try { renderRich(bub); } catch (e) {}
    row.appendChild(bub);
    stamps.set(row, at);
    tagRow(row, bub, mine, at);
    dressRow(row, mine, at);
    return row;
  }

  /* 头像和时间戳那一列 */
  function dressRow(row, mine, at) {
    if (row.querySelector('.apv-side')) return;
    var who = mine ? 'me' : 'gu';
    var side = document.createElement('div');
    side.className = 'apv-side';
    var av = document.createElement('div');
    av.className = 'apv-av';
    av.setAttribute('data-who', who);
    av.setAttribute('aria-hidden', 'true');
    if (avSrc[who]) av.style.backgroundImage = 'url("' + avatarUrl(who) + '")';
    else av.textContent = mine ? '妍' : '沐';
    var tm = document.createElement('div');
    tm.className = 'apv-time';
    tm.textContent = clock(at);
    side.appendChild(av);
    side.appendChild(tm);
    row.insertBefore(side, row.firstChild);
  }

  /* ── 主循环：挂头像时间，顺带拆段、包引用、补日期条 ───────────── */
  function decorate() {
    var L = logEl();
    if (!L) return;
    var rows = L.querySelectorAll('.row:not([data-apv])');
    var touched = rows.length;

    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      // 只认直接挂着气泡的行。思考行和工具行没有 .bubble/.gu，跳过
      var bub = null, mine = false;
      for (var j = 0; j < row.children.length; j++) {
        var c = row.children[j];
        if (c.classList.contains('bubble')) { bub = c; mine = true; break; }
        if (c.classList.contains('gu')) { bub = c; break; }
      }
      if (!bub) continue;

      var text = (bub._raw != null ? bub._raw : bub.textContent) || '';
      // 他那条是流式写出来的，还没拉到时间表之前先别配
      if (!mine && !text.trim() && !loaded) continue;

      row.setAttribute('data-apv', '1');
      var at = stampFor(row, mine, text);
      tagRow(row, bub, mine, at);
      dressRow(row, mine, at);

      /* 拆段：只拆定稿的。流式那条边写边拆会一帧一个样。
         拆出来的段共用母消息的时间戳。 */
      if (!bub._final) { row.classList.add('apv-solo'); continue; }
      var segs = cfg.split ? segments(text) : [text];
      if (segs.length > 1) {
        bub._raw = segs[0];
        bub.textContent = segs[0];
        bub.removeAttribute('data-rich');
        try { renderRich(bub); } catch (e) {}
        row.classList.add('apv-split', 'apv-first');
        var anchor = row.nextSibling;
        for (var k = 1; k < segs.length; k++) {
          var nr = makeRow(mine, segs[k], at);
          if (k === segs.length - 1) {
            nr.classList.remove('apv-split');
            nr.classList.add('apv-last');
          }
          L.insertBefore(nr, anchor);
        }
      } else {
        row.classList.add('apv-solo');
      }
      dressQuote(bub, row);
    }
    if (touched) dayMarks();
  }

  /* 相邻两条间隔超过 gapHours 就在中间插一行。全量重算：行会被往前插。 */
  function dayMarks() {
    var L = logEl();
    if (!L) return;
    var old = L.querySelectorAll('.apv-day');
    for (var i = 0; i < old.length; i++) old[i].remove();
    var rows = L.querySelectorAll('.row[data-apv]');
    var gap = cfg.gapHours * 3600;
    var prev = null;
    for (var j = 0; j < rows.length; j++) {
      var row = rows[j];
      if (!stamps.has(row)) continue;
      var at = stamps.get(row);
      if (prev === null || at - prev > gap) {
        var d = document.createElement('div');
        d.className = 'apv-day';
        var sp = document.createElement('span');
        sp.textContent = dayLabel(at);
        d.appendChild(sp);
        L.insertBefore(d, row);
      }
      prev = at;
    }
  }

  /* MutationObserver 只置脏标记，rAF 合并后再扫 —— 流式时 DOM 一秒变几十次 */
  var dirty = false, pending = false;
  function markDirty() {
    dirty = true;
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      if (!dirty) return;
      dirty = false;
      decorate();
    });
  }

  /* ═══ 引用 ═══════════════════════════════════════════════════
     长按气泡 → 小菜单 → 引用条挂在输入框上方 → 发送时拼进 box.value。

     ⚠️ 长按不用 contextmenu：iOS Safari 上那个会连带弹系统的
     「复制/查找」菜单，两个叠在一起。自己数 touchstart→touchend 的
     时间，超过 480ms 且手指没动超过 10px 才算。 */
  var LONG_MS = 480, MOVE_TOL = 10;
  var menu = null, quoteBar = null, quoted = null;
  var lt = null;

  function bubbleOf(node) {
    if (!node || !node.closest) return null;
    return node.closest('#log .row.apv > .bubble, #log .row.apv > .gu') || null;
  }

  function rawOf(bub) {
    var t = bub._raw != null ? bub._raw : bub.textContent;
    return String(t || '').trim();
  }

  function buildMenu() {
    if (menu) return;
    menu = document.createElement('div');
    menu.id = 'apvMenu';
    menu.innerHTML = '<button type="button" data-q="quote">引用</button>' +
      '<button type="button" data-q="copy">复制</button>';
    document.body.appendChild(menu);
    menu.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-q]');
      if (!b) return;
      var act = b.getAttribute('data-q');
      var bub = menu._bub;
      hideMenu();
      if (!bub) return;
      if (act === 'quote') setQuote(bub);
      else if (act === 'copy') copyText(rawOf(bub));
    });
  }

  function showMenu(bub, x, y) {
    buildMenu();
    menu._bub = bub;
    menu.classList.add('on');
    var r = menu.getBoundingClientRect();
    var left = Math.max(8, Math.min(window.innerWidth - r.width - 8, x - r.width / 2));
    var top = y - r.height - 12;
    if (top < 8) top = y + 16;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
  }
  function hideMenu() {
    if (menu) { menu.classList.remove('on'); menu._bub = null; }
  }

  function copyText(t) {
    var done = function () { try { note('（复制好了）'); } catch (e) {} };
    if (navigator.clipboard) {
      navigator.clipboard.writeText(t).then(done).catch(function () {});
      return;
    }
    var ta = document.createElement('textarea');
    ta.value = t;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    ta.remove();
  }

  function buildQuoteBar() {
    if (quoteBar) return quoteBar;
    var composer = document.querySelector('.composer');
    if (!composer) return null;
    quoteBar = document.createElement('div');
    quoteBar.id = 'apvQuote';
    quoteBar.innerHTML = '<div class="qt"></div>' +
      '<button type="button" class="qx" aria-label="不引用了">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/>' +
      '<line x1="18" y1="6" x2="6" y2="18"/></svg></button>';
    composer.insertBefore(quoteBar, composer.firstChild);
    quoteBar.querySelector('.qx').onclick = clearQuote;
    return quoteBar;
  }

  function setQuote(bub) {
    var bar = buildQuoteBar();
    if (!bar) return;
    var mine = bub.classList.contains('bubble');
    var t = rawOf(bub);
    // 引用里再套引用会越滚越长，把对方引的那层剥掉
    var inner = splitQuote(t);
    if (inner) t = inner.rest.trim() || t;
    quoted = { who: mine ? NAME.me : NAME.gu, text: t };
    var qt = bar.querySelector('.qt');
    qt.textContent = '';
    var b = document.createElement('b');
    b.textContent = quoted.who;
    qt.appendChild(b);
    qt.appendChild(document.createTextNode(t.length > 90 ? t.slice(0, 90) + '…' : t));
    bar.classList.add('on');
    var box = document.getElementById('box');
    if (box) box.focus();
  }

  function clearQuote() {
    quoted = null;
    if (quoteBar) quoteBar.classList.remove('on');
  }

  /* 发送前把引用拼到正文前面。
     ⚠️ 走捕获阶段：上游是 sendBtn.onclick = send，直接读 box.value；
     捕获比 onclick 先跑，在这儿改完 value 上游就拿到带引用的那份。 */
  function injectQuote() {
    if (!quoted) return;
    var box = document.getElementById('box');
    if (!box) return;
    var body = (box.value || '').trim();
    if (!body) return;                 // 空的不发，也不消耗引用
    var head = quoted.text.split('\n')
      .map(function (l) { return '> ' + l; }).join('\n');
    box.value = '> ' + quoted.who + '：\n' + head.replace(/^> /, '') + '\n\n' + body;
    box.dispatchEvent(new Event('input', { bubbles: true }));
    clearQuote();
  }

  function bindQuote() {
    var L = logEl();
    if (!L || L._apvQuoteBound) return;
    L._apvQuoteBound = 1;

    L.addEventListener('touchstart', function (e) {
      if (!cfg.quote || e.touches.length !== 1) return;
      var bub = bubbleOf(e.target);
      if (!bub) return;
      var t = e.touches[0];
      lt = { x: t.clientX, y: t.clientY, bub: bub };
      lt.timer = setTimeout(function () {
        if (!lt) return;
        showMenu(lt.bub, lt.x, lt.y);
        try { if (navigator.vibrate) navigator.vibrate(12); } catch (e2) {}
      }, LONG_MS);
    }, { passive: true });

    L.addEventListener('touchmove', function (e) {
      if (!lt) return;
      var t = e.touches[0];
      if (Math.abs(t.clientX - lt.x) + Math.abs(t.clientY - lt.y) > MOVE_TOL) {
        clearTimeout(lt.timer);
        lt = null;
      }
    }, { passive: true });

    var end = function () {
      if (!lt) return;
      clearTimeout(lt.timer);
      lt = null;
    };
    L.addEventListener('touchend', end, { passive: true });
    L.addEventListener('touchcancel', end, { passive: true });

    // 鼠标那边（iPad 接键盘、或者电脑上看）走右键
    L.addEventListener('contextmenu', function (e) {
      if (!cfg.quote) return;
      var bub = bubbleOf(e.target);
      if (!bub) return;
      e.preventDefault();
      showMenu(bub, e.clientX, e.clientY);
    });

    document.addEventListener('click', function (e) {
      if (menu && menu.classList.contains('on') && !menu.contains(e.target)) hideMenu();
    }, true);

    var sendBtn = document.getElementById('send');
    if (sendBtn) sendBtn.addEventListener('click', injectQuote, true);
    var box = document.getElementById('box');
    if (box) {
      box.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) injectQuote();
      }, true);
    }
  }

  /* ── 面板 ───────────────────────────────────────────────────── */
  var sheet = null, saveTimer = null;

  function save() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      fetch('api/appearance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg)
      }).catch(function () {});
    }, 420);
  }

  function sliderRow(m) {
    return '<div class="apv-line"><label for="apv-' + m[0] + '">' + m[1] + '</label>' +
      '<input type="range" id="apv-' + m[0] + '" data-k="' + m[0] + '" min="' + m[2] +
      '" max="' + m[3] + '" step="' + m[4] + '">' +
      '<span class="apv-val" data-v="' + m[0] + '"></span></div>';
  }

  function buildSheet() {
    if (sheet) return;
    sheet = document.createElement('div');
    sheet.className = 'sheetWrap';
    sheet.id = 'apvSheet';

    var sizes = '', gaps = '', boxes = '', i;
    for (i = 0; i < SIZE_META.length; i++) sizes += sliderRow(SIZE_META[i]);
    for (i = 0; i < GAP_META.length; i++) gaps += sliderRow(GAP_META[i]);
    for (i = 0; i < BOX_META.length; i++) boxes += sliderRow(BOX_META[i]);

    var seg = '<div class="apv-line"><label>工具行</label><div class="apv-seg">';
    for (var t = 0; t < TOOLS.length; t++) {
      seg += '<button type="button" data-tool="' + TOOLS[t][0] + '">' + TOOLS[t][1] + '</button>';
    }
    seg += '</div></div>';

    sheet.innerHTML =
      '<div class="shade" data-apv-close="1"></div>' +
      '<div class="sheet" role="dialog" aria-label="外观">' +
        '<div class="grabber"></div>' +
        '<div class="apv-h">外观</div>' +
        '<div class="apv-avs">' +
          '<label class="apv-pick"><i data-pick="me"></i><span class="t">我的头像</span>' +
            '<input type="file" accept="image/*" data-up="me"></label>' +
          '<label class="apv-pick"><i data-pick="gu"></i><span class="t">他的头像</span>' +
            '<input type="file" accept="image/*" data-up="gu"></label>' +
        '</div>' +

        '<div class="apv-grp">字体</div>' +
        '<div class="apv-font">' +
          '<div class="fname" data-fname>还是系统字体</div>' +
          '<div class="fsub" data-fsub>传个 ttf / otf / woff 上来就换 —— ' +
            '不用直链，直接选文件。中文字体几兆是常事，上限 12MB。</div>' +
          '<div class="try">好看的字，说给你听的话。0123456789</div>' +
          '<div class="frow">' +
            '<label class="up">选字体文件' +
              '<input type="file" accept="' + FONT_OK + '" data-fontup></label>' +
            '<button type="button" data-fontoff>用系统字体</button>' +
          '</div>' +
        '</div>' +

        '<div class="apv-grp">多大</div>' + sizes +
        '<div class="apv-grp">多远</div>' + gaps +
        '<div class="apv-grp">输入框</div>' + boxes +

        '<div class="apv-sep"></div>' +
        '<div class="apv-line"><label>拆气泡</label>' +
          '<button type="button" class="apv-sw" data-sw="split" ' +
          'role="switch" aria-label="空行拆成独立气泡"></button></div>' +
        '<div class="apv-line"><label>长按引用</label>' +
          '<button type="button" class="apv-sw" data-sw="quote" ' +
          'role="switch" aria-label="长按气泡引用"></button></div>' +
        '<div class="apv-line"><label>头像</label>' +
          '<button type="button" class="apv-sw" data-sw="showAvatar" ' +
          'role="switch" aria-label="显示头像"></button></div>' +
        '<div class="apv-line"><label>时间</label>' +
          '<button type="button" class="apv-sw" data-sw="showTime" ' +
          'role="switch" aria-label="显示时间"></button></div>' +
        '<div class="apv-line"><label>日期条</label>' +
          '<button type="button" class="apv-sw" data-sw="showDay" ' +
          'role="switch" aria-label="显示日期分隔"></button></div>' +
        seg +

        '<div class="apv-sep"></div>' +
        '<textarea class="apv-css" spellcheck="false" autocapitalize="off" ' +
          'autocorrect="off" placeholder="在这儿写 CSS"></textarea>' +
        '<div class="apv-btns" style="margin-top:10px">' +
          '<button type="button" data-apv-preset="1">用一下圆条样式</button>' +
        '</div>' +
        '<p class="apv-note">' +
          '<b>行（这些 class 都在 .row 上）</b>' +
          '<code>.apv</code> 认过的行　<code>.apv-me</code> 你说的　' +
          '<code>.apv-gu</code> 他说的<br>' +
          '<code>.apv-first</code> 拆出来那组的头一段<br>' +
          '<code>.apv-last</code> 最后一段　<code>.apv-solo</code> 没拆的<br>' +
          '<code>.apv-split</code> 中间几段（不含最后一段）<br>' +
          '<code>.apv-voice</code> 语音　<code>.apv-img</code> 带图<br>' +
          '<code>.apv-code</code> 带代码块　<code>.apv-quoted</code> 带引用<br>' +
          '<code>[data-who="gu"]</code>　<code>[data-at]</code> 秒级时间戳' +

          '<b>行里面</b>' +
          '<code>#log .row.apv &gt; .bubble</code> 你的气泡<br>' +
          '<code>#log .row.apv &gt; .gu</code> 他的气泡<br>' +
          '<code>#log .apv-av</code> 头像　<code>#log .apv-time</code> 时间<br>' +
          '<code>#log .apv-side</code> 头像和时间那一列<br>' +
          '<code>#log .apv-qbox</code> 气泡里那段引用<br>' +
          '<code>#log .apv-day &gt; span</code> 中间那个日期条<br>' +
          '<code>#log .row .toolline</code> 思考那一行<br>' +
          '<code>#log .row.apv .vz</code> 语音条　' +
          '<code>.vz-txt</code> 转写　<code>.vz-dur</code> 时长<br>' +
          '<code>#log .row.apv img</code> 图片和表情包' +

          '<b>输入框那一坨</b>' +
          '<code>.composer</code> 整块　<code>.composer #box</code> 输入框<br>' +
          '<code>.composer .pill</code> 模型名那颗<br>' +
          '<code>#plusBtn</code> <code>#vzMic</code> <code>#vcBtn</code> ' +
          '<code>#send</code> 四颗按钮<br>' +
          '<code>#apvQuote</code> 引用条　<code>#apvMenu</code> 长按菜单' +

          '<b>变量（挂在 html 上，哪儿都能用）</b>' +
          '<code>--apv-font</code> 正文　<code>--apv-voice</code> 语音<br>' +
          '<code>--apv-tool</code> 工具行　<code>--apv-quote</code> 引用<br>' +
          '<code>--apv-cfont</code> 输入字号　<code>--apv-cpad</code> 内边距<br>' +
          '<code>--apv-btn</code> 按钮　<code>--apv-av</code> 头像　' +
          '<code>--apv-time</code> 时间<br>' +
          '<code>--apv-radius</code> 圆角　<code>--apv-gap</code> 行距　' +
          '<code>--apv-split</code> 段距<br>' +
          '<code>--apv-family</code> 你传的那个字体<br>' +
          '上游的：<code>--bg</code> <code>--card</code> <code>--panel</code> ' +
          '<code>--line</code> <code>--text</code> <code>--dim</code> ' +
          '<code>--accent</code>' +

          '<b>抄两句</b>' +
          '<i>他的气泡换个底色：</i><br>' +
          '<code>#log .apv-gu &gt; .gu{background:#f3efe6}</code><br>' +
          '<i>语音那颗药丸换色：</i><br>' +
          '<code>#log .apv-voice .vz{background:rgba(201,100,66,.12)}</code><br>' +
          '<i>拆出来的中间几段头像淡一点：</i><br>' +
          '<code>#log .apv-split:not(.apv-first) .apv-av{opacity:.45}</code>' +

          '<b>三个坑</b>' +
          '圆角别写 <code>999px</code> —— 单行好看，多行会撑成椭圆，' +
          '<code>20px</code> 上下都对。<br>' +
          '选择器前面那个 <code>#log</code> 别省 —— <code>.row</code> 这名字' +
          '页面里别处也在用，写宽了会把锁屏弄坏。<br>' +
          '<code>#box</code> 的字号别写到 16px 以下 —— iOS 一聚焦就会' +
          '把整页放大，退不回来。' +
        '</p>' +
        '<div class="apv-btns">' +
          '<button type="button" data-apv-reset="1">还原默认</button>' +
          '<button type="button" class="go" data-apv-close="1">好了</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(sheet);

    sheet.addEventListener('click', function (e) {
      var t = e.target;
      if (t.closest && t.closest('[data-apv-close]')) { closeSheet(); return; }
      if (t.closest && t.closest('[data-apv-reset]')) {
        var keepFont = cfg.fontFamily;
        cfg = JSON.parse(JSON.stringify(DEF));
        cfg.fontFamily = keepFont;      // 还原样式，但别把字体也退掉
        apply(); fillSheet(); dayMarks(); save();
        return;
      }
      if (t.closest && t.closest('[data-apv-preset]')) {
        cfg.css = PRESET;
        apply(); fillSheet(); save();
        return;
      }
      if (t.closest && t.closest('[data-fontoff]')) {
        cfg.fontFamily = '';
        apply(); fillSheet(); save();
        return;
      }
      var tl = t.closest && t.closest('[data-tool]');
      if (tl) {
        cfg.toolMode = tl.getAttribute('data-tool');
        apply(); fillSheet(); save();
        return;
      }
      var sw = t.closest && t.closest('[data-sw]');
      if (sw) {
        var k = sw.getAttribute('data-sw');
        cfg[k] = !cfg[k];
        apply(); fillSheet(); save();
        if (k === 'split') {
          if (cfg[k]) decorate();
          else { try { note('（改好了，刷一下就看到）'); } catch (e2) {} }
        }
      }
    });

    sheet.addEventListener('input', function (e) {
      var el = e.target;
      var k = el.getAttribute && el.getAttribute('data-k');
      if (k) {
        cfg[k] = parseFloat(el.value);
        var v = sheet.querySelector('[data-v="' + k + '"]');
        if (v) v.textContent = k === 'gapHours' ? (cfg[k] + 'h') : cfg[k];
        apply();
        if (k === 'gapHours') dayMarks();
        save();
        return;
      }
      if (el.classList && el.classList.contains('apv-css')) {
        cfg.css = el.value;
        apply(); save();
      }
    });

    sheet.addEventListener('change', function (e) {
      var el = e.target;
      // 头像
      var who = el.getAttribute && el.getAttribute('data-up');
      if (who && el.files && el.files[0]) {
        var fd = new FormData();
        fd.append('file', el.files[0]);
        fetch('api/appearance/avatar/' + who, { method: 'POST', body: fd })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d || !d.ok) throw new Error('bad');
            avSrc[who] = d.v;
            paintAvatars();
          })
          .catch(function () {
            try { note('（头像没传上去）', 'err'); } catch (e2) {}
          });
        el.value = '';
        return;
      }
      // 字体
      if (el.hasAttribute && el.hasAttribute('data-fontup') && el.files && el.files[0]) {
        var f = el.files[0];
        var sub = sheet.querySelector('[data-fsub]');
        if (sub) sub.textContent = '正在传 ' + f.name + '…';
        var ff = new FormData();
        ff.append('file', f);
        fetch('api/appearance/font', { method: 'POST', body: ff })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d || !d.ok) throw new Error((d && d.error) || 'bad');
            fontInfo = { name: d.name, v: d.v, fmt: d.fmt };
            cfg.fontFamily = d.name;
            apply(); fillSheet(); save();
            try { note('（换好了，字体是 ' + d.name + '）'); } catch (e2) {}
          })
          .catch(function (err) {
            if (sub) sub.textContent = '没传上去（' + (err.message || '不知道为什么')
              + '）。ttf / otf / woff / woff2，12MB 以内。';
          });
        el.value = '';
      }
    });
  }

  function fillSheet() {
    if (!sheet) return;
    for (var i = 0; i < META.length; i++) {
      var k = META[i][0];
      var el = sheet.querySelector('[data-k="' + k + '"]');
      if (el) el.value = cfg[k];
      var v = sheet.querySelector('[data-v="' + k + '"]');
      if (v) v.textContent = k === 'gapHours' ? (cfg[k] + 'h') : cfg[k];
    }
    var sws = sheet.querySelectorAll('[data-sw]');
    for (var j = 0; j < sws.length; j++) {
      var on = !!cfg[sws[j].getAttribute('data-sw')];
      sws[j].classList.toggle('on', on);
      sws[j].setAttribute('aria-checked', on ? 'true' : 'false');
    }
    var tls = sheet.querySelectorAll('[data-tool]');
    for (var n = 0; n < tls.length; n++) {
      tls[n].classList.toggle('on', tls[n].getAttribute('data-tool') === cfg.toolMode);
    }
    var fn = sheet.querySelector('[data-fname]');
    if (fn) {
      fn.textContent = (cfg.fontFamily && fontInfo.name === cfg.fontFamily)
        ? cfg.fontFamily : '还是系统字体';
    }
    var fs = sheet.querySelector('[data-fsub]');
    if (fs) {
      fs.textContent = fontInfo.name
        ? ('传上来的是 ' + fontInfo.name +
           (cfg.fontFamily ? '，正在用' : '，现在没在用'))
        : '传个 ttf / otf / woff 上来就换 —— 不用直链，直接选文件。中文字体几兆是常事，上限 12MB。';
    }
    var ta = sheet.querySelector('.apv-css');
    if (ta && ta.value !== cfg.css) ta.value = cfg.css || '';
    paintAvatars();
  }

  function openSheet() {
    buildSheet();
    fillSheet();
    try { if (typeof closeDrawer === 'function') closeDrawer(); } catch (e) {}
    requestAnimationFrame(function () { sheet.classList.add('open'); });
  }
  function closeSheet() {
    if (!sheet) return;
    sheet.classList.remove('open');
    try { if (typeof openDrawer === 'function') openDrawer(); } catch (e) {}
  }

  /* 侧边栏加一项，跟着 navWall 长。取不到就先不加，下一轮再试。 */
  function mountNav() {
    if (document.getElementById('navLook')) return;
    var anchor = document.getElementById('navWall');
    if (!anchor || !anchor.parentNode) return;
    var b = document.createElement('button');
    b.className = anchor.className;
    b.id = 'navLook';
    b.type = 'button';
    b.innerHTML = '<span class="ic"><svg width="19" height="19" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="3.2"/>' +
      '<path d="M12 3v2.2M12 18.8V21M4.6 7.8l1.9 1.1M17.5 15.1l1.9 1.1' +
      'M4.6 16.2l1.9-1.1M17.5 8.9l1.9-1.1"/></svg></span>外观';
    b.onclick = openSheet;
    anchor.parentNode.insertBefore(b, anchor.nextSibling);
  }

  /* ── 起 ─────────────────────────────────────────────────────── */
  function boot() {
    apply();

    fetch('api/appearance', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          cfg = d.cfg;
          avSrc.me = (d.avatars && d.avatars.me) || '';
          avSrc.gu = (d.avatars && d.avatars.gu) || '';
          if (d.font) fontInfo = d.font;
        }
        apply(); paintAvatars(); fillSheet();
        decorate();
      })
      .catch(function () {});

    loadStamps().then(decorate);

    mountNav();
    bindQuote();
    var L = logEl();
    if (L && window.MutationObserver) {
      new MutationObserver(markDirty).observe(L, {
        childList: true, subtree: true, characterData: true
      });
    }
    // 侧边栏和输入卡都是上游后来才画的；也兜住流式那条空气泡后来才有字
    setInterval(function () { mountNav(); bindQuote(); decorate(); }, 1400);
  }

  window.dwellAppearance = {
    open: openSheet,
    close: closeSheet,
    get: function () { return cfg; },
    preset: PRESET,
    split: segments,
    quote: setQuote,
    unquote: clearQuote,
    font: function () { return fontInfo; },
    redraw: function () { decorate(); dayMarks(); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


def register_appearance_feature(server_module):
    """接外观设置：六个接口 + 再包一层 index。

    要排在 frontend_feature 之后（它包的是那一层 index）。
    """
    app = server_module.app
    get_db = server_module.get_db

    data_dir = Path(server_module.DB_PATH).resolve().parent
    av_dir = data_dir / "avatars"
    av_dir.mkdir(parents=True, exist_ok=True)
    font_dir = data_dir / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)

    def read_cfg() -> dict:
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM settings WHERE key=?", (SETTINGS_KEY,)
                ).fetchone()
        except Exception:
            return dict(DEFAULTS)
        if not row:
            return dict(DEFAULTS)
        try:
            raw = row["value"] if hasattr(row, "keys") else row[0]
            return _clean(json.loads(raw))
        except Exception:
            return dict(DEFAULTS)

    def write_cfg(cfg: dict) -> None:
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (SETTINGS_KEY, json.dumps(cfg, ensure_ascii=False)),
            )

    def avatar_path(who: str):
        """找 who 的头像文件。没传过返回 None。"""
        for ext in ALLOWED_EXT:
            p = av_dir / (who + ext)
            if p.exists():
                return p
        return None

    def avatar_stamp(who: str) -> str:
        """给前端当缓存串用：换了头像这个数就变，浏览器才会重新拉。"""
        p = avatar_path(who)
        if p is None:
            return ""
        try:
            return str(int(p.stat().st_mtime))
        except OSError:
            return ""

    def font_path():
        """当前那个字体文件。只留一个 —— 传新的就把旧的顶掉。"""
        for ext in FONT_EXT:
            p = font_dir / ("user" + ext)
            if p.exists():
                return p
        return None

    def font_meta() -> dict:
        """给前端拼 @font-face 用：显示名、版本号、format()。"""
        p = font_path()
        if p is None:
            return {"name": "", "v": "", "fmt": ""}
        label = ""
        try:
            label = (font_dir / "user.name").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        try:
            v = str(int(p.stat().st_mtime))
        except OSError:
            v = ""
        return {
            "name": label or p.name,
            "v": v,
            "fmt": FONT_EXT.get(p.suffix.lower(), ("", ""))[1],
        }

    def api_get():
        return jsonify({
            "ok": True,
            "cfg": read_cfg(),
            "avatars": {"me": avatar_stamp("me"), "gu": avatar_stamp("gu")},
            "font": font_meta(),
        })

    def api_post():
        cfg = _clean(request.get_json(force=True, silent=True) or {})
        write_cfg(cfg)
        return jsonify({"ok": True, "cfg": cfg})

    def api_avatar_put(who):
        if who not in ("me", "gu"):
            return jsonify({"ok": False, "error": "who"}), 400
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"ok": False, "error": "no file"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return jsonify({"ok": False, "error": "type"}), 400
        # 换格式时把旧的那几个扩展名清掉，不然 avatar_path 会先撞上旧文件
        for old in ALLOWED_EXT:
            p = av_dir / (who + old)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        f.save(str(av_dir / (who + ext)))
        return jsonify({"ok": True, "v": str(int(time.time()))})

    def api_avatar_get(who):
        if who not in ("me", "gu"):
            return jsonify({"ok": False}), 404
        p = avatar_path(who)
        if p is None:
            return jsonify({"ok": False}), 404
        return send_file(str(p), mimetype=ALLOWED_EXT.get(p.suffix.lower()),
                         max_age=0, conditional=True)

    def api_font_put():
        """收一个字体文件。只留一份，传新的顶掉旧的。

        原始文件名单独存一行当显示名 —— 文件本身一律叫 user.xxx，
        免得中文名在路径上出岔子。
        """
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"ok": False, "error": "没选文件"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in FONT_EXT:
            return jsonify({"ok": False, "error": "只收 ttf/otf/woff/woff2"}), 400

        blob = f.read()
        if not blob:
            return jsonify({"ok": False, "error": "文件是空的"}), 400
        if len(blob) > FONT_MAX:
            return jsonify({"ok": False, "error": "超过 12MB"}), 413

        for old in FONT_EXT:
            p = font_dir / ("user" + old)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        (font_dir / ("user" + ext)).write_bytes(blob)
        label = SAFE_NAME.sub("", Path(f.filename).stem)[:80] or ("字体" + ext)
        try:
            (font_dir / "user.name").write_text(label, encoding="utf-8")
        except OSError:
            pass

        cfg = read_cfg()
        cfg["fontFamily"] = label
        write_cfg(cfg)

        meta = font_meta()
        meta["ok"] = True
        return jsonify(meta)

    def api_font_get():
        p = font_path()
        if p is None:
            return jsonify({"ok": False}), 404
        mime = FONT_EXT.get(p.suffix.lower(), ("font/ttf", ""))[0]
        # 带版本号的地址可以放心让浏览器缓存
        return send_file(str(p), mimetype=mime, max_age=86400, conditional=True)

    app.add_url_rule("/api/appearance", endpoint="api_appearance_get",
                     view_func=api_get, methods=["GET"])
    app.add_url_rule("/api/appearance", endpoint="api_appearance_post",
                     view_func=api_post, methods=["POST"])
    app.add_url_rule("/api/appearance/avatar/<who>", endpoint="api_appearance_avatar_put",
                     view_func=api_avatar_put, methods=["POST"])
    app.add_url_rule("/api/appearance/avatar/<who>", endpoint="api_appearance_avatar_get",
                     view_func=api_avatar_get, methods=["GET"])
    app.add_url_rule("/api/appearance/font", endpoint="api_appearance_font_put",
                     view_func=api_font_put, methods=["POST"])
    app.add_url_rule("/api/appearance/font", endpoint="api_appearance_font_get",
                     view_func=api_font_get, methods=["GET"])

    server_module.appearance_client_script = CLIENT_SCRIPT

    original = app.view_functions.get("index")
    if original is None:
        return

    def index_with_appearance(*args, **kwargs):
        resp = original(*args, **kwargs)
        try:
            if "text/html" not in (resp.headers.get("Content-Type") or ""):
                return resp
            html = resp.get_data(as_text=True)
        except Exception:
            return resp
        if "window.dwellAppearance" in html or "</body>" not in html:
            return resp
        resp.set_data(html.replace("</body>", CLIENT_SCRIPT + "</body>", 1))
        return resp

    app.view_functions["index"] = index_with_appearance
