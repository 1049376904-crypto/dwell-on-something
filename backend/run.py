"""dwell 后端统一启动入口。

所有功能通过 register_xxx_feature(server) 接入。PM2 必须运行本文件，
不要直接运行 server.py。
"""

import server
from compat import register_compat
from storage_feature import register_storage_feature
from gateway_config import register_gateway_config
from frontend_feature import register_frontend_feature
from calendar_feature import register_calendar_feature
from whisper_feature import register_whisper_feature
from journal_feature import register_journal_feature
from event_stream_feature import register_event_stream_feature
from transcript_feature import register_transcript_feature
from agent_tools_feature import register_agent_tools_feature


register_compat(server)
# 存储必须最先固定，否则其余模块会在错误的数据库里建表。
db_path = register_storage_feature(server)
# 网关配置要在聊天代理之前生效。
register_gateway_config(server)

register_frontend_feature(server)
register_calendar_feature(server)
register_whisper_feature(server)
register_journal_feature(server)
# 先替换事件流，再注册会广播工具事件的聊天代理。
register_event_stream_feature(server)
# transcript 要在 agent_tools 之前，后者需要 server.save_transcript。
register_transcript_feature(server)
register_agent_tools_feature(server)


if __name__ == "__main__":
    print("dwell-backend 启动（模块化入口）")
    print(f"  gateway : {server.GATEWAY_URL}")
    print(f"  model   : {server.current_model()}")
    print(f"  port    : {server.PORT}")
    print(f"  db      : {db_path}")
    print("  frontend: ../web/index.html（动态读取，无需复制）")
    print("  modules : 聊天 / 待办 / 日历 / 悄悄话 / 日记")
    print("  tools   : 待办 / 日历 / 悄悄话 / 日记 / 摘录")
    print("  events  : 可重放游标队列")
    print("  history : 思考与工具调用持久化重放")
    print("  config  : 设置 → 接入 API（地址/令牌/模型名）")
    server.app.run(host="0.0.0.0", port=server.PORT, threaded=True)
