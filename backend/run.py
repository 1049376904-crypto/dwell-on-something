"""dwell 后端统一启动入口。

所有功能通过 register_xxx_feature(server) 接入。PM2 必须运行本文件，
不要直接运行 server.py。
"""

import server
from frontend_feature import register_frontend_feature
from calendar_feature import register_calendar_feature
from whisper_feature import register_whisper_feature


register_frontend_feature(server)
register_calendar_feature(server)
register_whisper_feature(server)


if __name__ == "__main__":
    print("dwell-backend 启动（模块化入口）")
    print(f"  gateway : {server.GATEWAY_URL}")
    print(f"  model   : {server.current_model()}")
    print(f"  port    : {server.PORT}")
    print("  frontend: ../web/index.html（动态读取，无需复制）")
    server.app.run(host="0.0.0.0", port=server.PORT, threaded=True)
