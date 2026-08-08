"""部署层的个人化配置。

修改本文件即可，不用去改 web/index.html（那份保持上游原貌，方便以后同步）。
也可以用环境变量覆盖，例如：

    DWELL_USER_NAME=妍妍 DWELL_AI_NAME=沐 DWELL_TOGETHER_SINCE=2026-07-13
"""

import os


# 两人的称呼
USER_NAME = os.getenv("DWELL_USER_NAME", "妍妍")
AI_NAME = os.getenv("DWELL_AI_NAME", "沐")

# 首页标题与副标题
APP_TITLE = os.getenv("DWELL_APP_TITLE", AI_NAME)
APP_SUBTITLE = os.getenv("DWELL_APP_SUBTITLE", f"{USER_NAME} · {AI_NAME}")

# “在一起 N 天”的起算日，格式 YYYY-MM-DD
TOGETHER_SINCE = os.getenv("DWELL_TOGETHER_SINCE", "2026-07-13")

# 日记首页底部的一句话
DIARY_MOTTO = os.getenv("DWELL_DIARY_MOTTO", f"{USER_NAME} 和 {AI_NAME} 的日子")

# 小票页脚的店名
STORE_NAME = os.getenv("DWELL_STORE_NAME", "MU \u00b7 YAN GENERAL STORE")

# 锁屏上的英文提示
LOCK_WORD = os.getenv("DWELL_LOCK_WORD", "slide to unlock")
