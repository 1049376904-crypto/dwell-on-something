"""dwell 后端统一启动入口。

所有功能通过 register_xxx_feature(server) 接入。PM2 必须运行本文件，
不要直接运行 server.py。
"""

import server
from compat import register_compat
from storage_feature import register_storage_feature
from auth_feature import register_auth_feature
from gateway_config import register_gateway_config
from models_feature import register_models_feature
from media_feature import register_media_feature
from icon_feature import register_icon_feature
from push_feature import register_push_feature
from frontend_feature import register_frontend_feature
from calendar_feature import register_calendar_feature
from whisper_feature import register_whisper_feature
from journal_feature import register_journal_feature
from repo_feature import register_repo_feature
from event_stream_feature import register_event_stream_feature
from transcript_feature import register_transcript_feature
from agent_tools_feature import register_agent_tools_feature
from busy_guard import register_busy_guard
from sticker_feature import register_sticker_feature
from music_feature import register_music_feature
from news_feature import register_news_feature
from nook_feature import register_nook_feature
from heartbeat_feature import register_heartbeat_feature
from wake_feature import register_wake_feature
from backup_feature import register_backup_feature
from panel_shell import register_panel_shell
from voice_feature import register_voice_feature
from voice_call import register_voice_call
from voice_prompt import register_voice_prompt


register_compat(server)
# 存储必须最先固定，否则其余模块会在错误的数据库里建表。
db_path = register_storage_feature(server)
# 口令闸门紧跟在存储之后：它要读写 settings，但必须在其余模块之前，
# 因为 Flask 的 before_request 按注册顺序执行——排在后面的话，
# 先注册的钩子会在没验身份的情况下先跑一遍。
register_auth_feature(server)
# 网关配置要在聊天代理之前生效。
register_gateway_config(server)
# 模型清单接管 /api/model，要在网关配置之后。
register_models_feature(server)
# 图片要在聊天代理之前：agent_tools 需要 server.build_multimodal。
register_media_feature(server)

# 图标要在推送和前端之前：manifest 和页面 head 都要拿它的链接。
register_icon_feature(server)
# 推送要在前端之前：前端需要拿到 push_client_script 注入页面。
register_push_feature(server)
register_frontend_feature(server)
register_calendar_feature(server)
register_whisper_feature(server)
register_journal_feature(server)
register_repo_feature(server)
# 先替换事件流，再注册会广播工具事件的聊天代理。
register_event_stream_feature(server)
# transcript 在 event_stream 之后接管 api_messages，并需在 agent_tools 之前
# 注册，后者依赖 server.save_transcript。
register_transcript_feature(server)
register_agent_tools_feature(server)
# busy 占位必须紧跟在 agent_tools 之后：那个模块会整体替换
# call_gateway，先包的话包住的是旧版、随后被覆盖，finally 就白写了。
# 反过来，它必须在心跳之前——心跳靠 call_gateway 的返回值
# 区分「没轮到」和「真说过」。
register_busy_guard(server)
# 下面这几个都要往 agent_tools 的 TOOLS 里追工具、
# 或者包一层 execute_tool / build_context_snapshot，所以都排在它之后。
# 它们各自包住的是「上一层」，所以顺序之间互不影响。
register_sticker_feature(server)
register_music_feature(server)
register_news_feature(server)
register_nook_feature(server)
# 心跳必须最后注册：它调用 server.call_gateway，要拿到带工具、
# 带占位的那一版，同时依赖 server.send_push 把主动说的话推到锁屏。
register_heartbeat_feature(server)
# /api/wake 是上游设置页那个开关的后端，要在心跳之后：
# 它借用 heartbeat_feature 的键名和 night_key()，
# 也需要那边先把默认值写进 settings。
register_wake_feature(server)
# 备份放在最末：只依赖 get_db 和 DB_PATH，但它的定时线程会读整个库，
# 等其余模块把表都建完再启动最省事。
register_backup_feature(server)
# 面板样式统一：靠 after_request 给 /push /models /stickers /backup
# 注一段共用样式，并往主页注入承载它们的浮层。
# 和注册顺序无关，排在所有面板之后读起来最清楚。
register_panel_shell(server)
# 语音条要在 frontend 之后：它包住那一层 index 视图，把客户端脚本注进去。
# 后端在 /api/voice（独立服务，nginx 反代到 127.0.0.1:8021），不经过 Flask。
register_voice_feature(server)
# 通话再包一层，要在 voice_feature 之后：那颗话筒键得先在，电话键才挨着它长。
register_voice_call(server)
# 告诉模型那行 [voice] 和 [通话中] 是什么。要在 agent_tools 之后：
# 它包的是那个模块的 build_context_snapshot。不接这一行，
# 语音条依旧能录能发，只是模型不知道那行标记是什么意思。
register_voice_prompt(server)


if __name__ == "__main__":
    print("dwell-backend 启动（模块化入口）")
    print(f"  gateway : {server.GATEWAY_URL}")
    print(f"  model   : {server.current_model()}")
    print(f"  port    : {server.PORT}")
    print(f"  db      : {db_path}")
    print("  frontend: ../web/index.html（动态读取，无需复制）")
    print("  modules : 聊天 / 待办 / 日历 / 悄悄话 / 日记 / 仓库 / 日报 / 共读")
    print("  tools   : 待办 / 日历 / 悄悄话 / 日记 / 摘录 / 表情 / 找歌 / 日报 / 共读")
    print("  events  : 可重放游标队列")
    print("  history : 思考与工具调用持久化重放")
    print("  config  : 设置 → 接入 API（地址/令牌/模型名）")
    print("  beat    : /api/heartbeat；开关也接进了设置页那一行")
    print("  push    : /push 面板（iOS 需 HTTPS + 添加到主屏幕）")
    print("  models  : /models 面板")
    print("  media   : /api/upload，图片存 data/uploads")
    print("  sticker : /stickers 面板，原图存 data/stickers")
    print("  music   : /api/music，网易云链接自动变卡片")
    print("  news    : /api/news，稿子存 data/news（默认关闭，隔几天一份）")
    print("  nook    : /books 传书，书稿存 data/books，划线进库")
    print("  icon    : /api/icon/status")
    print("  backup  : /backup 面板（默认关闭，只推文本库）")
    print("  panels  : 浮层内打开，配色取自上游 :root（/api/panel/vars）")
    print("  busy    : 原子占位，卡住会自动抢占（/api/busy）")
    print("  auth    : /auth 设口令；维护令牌在 data/admin_token")
    print("  voice   : 语音条；TTS 在 /api/voice（独立服务，不经过 Flask）")
    print("  call    : 语音通话·对讲机模式（回合制，一轮 5~8 秒）")
    print("  vprompt : [voice] 与 [通话中] 已接进 system 提示词")
    server.app.run(host="0.0.0.0", port=server.PORT, threaded=True)
