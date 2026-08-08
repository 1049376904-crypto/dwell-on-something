"""dwell 后端统一启动入口。

所有功能通过 register_xxx_feature(server) 接入。PM2 必须运行本文件，
不要直接运行 server.py。
"""

import server
from frontend_feature import register_frontend_feature
from calendar_feature import register_calendar_feature
from whisper_feature import register_whisper_feature
from journal_feature import register_journal_feature
from event_stream_feature import register_event_stream_feature
from agent_tools_feature import register_agent_tools_feature


register_frontend_feature(server)
register_calendar_feature(server)
register_whisper_feature(server)
register_journal_feature(server)
# 必须先替换事件流，再注册会广播工具事件的聊天代理。
register_event_stream_feature(server)
register_agent_tools_feature(server)


if __name__ == "__main__":
    print("dwell-backend 启动（模块化入口）")
    print(f"  gateway : {server.GATEWAY_URL}")
    print(f"  model   : {server.current_model()}")
    print(f"  port    : {server.PORT}")
    print("  frontend: ../web/index.html（动态读取，无需复制）")
    print("  modules : 聊天 / 待办 / 日历 / 悄悄话 / 日记")
    print("  tools   : 待办 / 日历 / 悄悄话")
    print("  events  : 可重放游标队列")
    server.app.run(host="0.0.0.0", port=server.PORT, threaded=True)
