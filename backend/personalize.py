"""部署层的个人化配置。

修改本文件即可，不用去改 web/index.html（那份保持上游原貌，方便以后同步）。
也可以用环境变量覆盖，例如：

    DWELL_USER_NAME=妍妍 DWELL_AI_NAME=沐 DWELL_TOGETHER_SINCE=2026-07-13

关于 iOS 通知头部那两行，实测规律如下：

    第一行  = 服务端 showNotification 的 title；留空时系统填应用名
    第二行  = 固定的 "from " + 应用名，无法去掉
    应用名  = <meta name="apple-mobile-web-app-title">，
              同时也是主屏图标下面显示的文字

所以第一行由 PUSH_TITLE 控制，第二行和桌面标签共用 HOME_SCREEN_NAME。
想让桌面显示的字跟 "from" 后面不一样，只能在「添加到主屏幕」的弹窗里
手动改名——那一步的输入框会覆盖桌面标签。
"""

import os


# 两人的称呼
USER_NAME = os.getenv("DWELL_USER_NAME", "妍妍")
AI_NAME = os.getenv("DWELL_AI_NAME", "沐")

# 首页标题与副标题（应用内的表头，不影响系统层面的名字）
APP_TITLE = os.getenv("DWELL_APP_TITLE", AI_NAME)
APP_SUBTITLE = os.getenv("DWELL_APP_SUBTITLE", f"{USER_NAME} · {AI_NAME}")

# 应用名，来自 <meta name="apple-mobile-web-app-title">。
# 它同时决定主屏图标下的文字和通知第二行的 "from X"。
# 设成「沐」是为了让通知读作「予妍 / from 沐」；
# 桌面上想显示 Luminae，在添加到主屏幕时手动改那个名字。
HOME_SCREEN_NAME = os.getenv("DWELL_HOME_SCREEN_NAME", AI_NAME)

# 推送通知第一行。send_push 的 title 为空时回落到这个值。
PUSH_TITLE = os.getenv("DWELL_PUSH_TITLE", "予妍")

# manifest 的 name。iOS 主屏那套流程实际不读它，
# 留着是为了标准完整性和其他浏览器的安装提示。
PWA_NAME = os.getenv("DWELL_PWA_NAME", PUSH_TITLE)

# “在一起 N 天”的起算日，格式 YYYY-MM-DD
TOGETHER_SINCE = os.getenv("DWELL_TOGETHER_SINCE", "2026-07-13")

# 日记首页底部的一句话
DIARY_MOTTO = os.getenv("DWELL_DIARY_MOTTO", f"{USER_NAME} 和 {AI_NAME} 的日子")

# 小票页脚的店名
STORE_NAME = os.getenv("DWELL_STORE_NAME", "MU \u00b7 YAN GENERAL STORE")

# 锁屏上的英文提示
LOCK_WORD = os.getenv("DWELL_LOCK_WORD", "slide to unlock")
