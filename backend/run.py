"""dwell 后端统一启动入口。

新增功能以 register_xxx_feature(server) 的形式接入，避免所有代码都堆进
server.py，也方便后续逐个启用悄悄话、音乐、仓库等模块。
"""

import server
from calendar_feature import register_calendar_feature


register_calendar_feature(server)


if __name__ == "__main__":
    print("dwell-backend 启动（模块化入口）")
    print(f"  gateway : {server.GATEWAY_URL}")
    print(f"  model   : {server.current_model()}")
    print(f"  port    : {server.PORT}")
    server.app.run(host="0.0.0.0", port=server.PORT, threaded=True)
