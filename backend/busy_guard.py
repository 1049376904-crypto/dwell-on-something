"""把「谁在跟网关说话」这件事做成原子的。

## 原来的两个问题

**一、检查和占位不是一步。** server.api_send 长这样：

    with state_lock:
        if state["busy"]: return 429
    save_message("her", text)                    # ← 锁已经放开了
    threading.Thread(target=call_gateway, ...)   # ← busy 是进了这里才设 True

从放锁到 call_gateway 真正把 busy 设成 True，中间有一段窗口，
第二个请求进来看到的仍然是 False。连点两次发送、
或者心跳刚好在这一刻醒过来，两路就会同时打网关：
消息顺序错乱、两份回复交叉写库。窗口很短，但不是不会发生。

心跳那边、表情那边也各写了一遍同样的「先查 busy 再起线程」，
所以同一个洞有三处。

**二、busy 可能永远卡住。** call_gateway 的收尾是这样的：

    except Exception as e:
        ...
        with state_lock: state["busy"] = False
        return
    broadcast(...)          # ← 在 try 之外
    save_message(...)
    with state_lock: state["busy"] = False

最后那几行任何一处抛异常（订阅队列出问题、写库失败），
就没人把 busy 清回 False。之后所有发送一律 429，界面上表现为
「他一直在说话」，只能重启后端。

## 修法

acquire_busy() 在同一把锁里完成「查 + 占」，占不到就返回 False，
调用方直接拒绝。release_busy() 无条件放。call_gateway 用
try/finally 包起来，无论怎么退出都释放。

顺带记下占位者和占位时间：卡住的时候能看出是谁占着、占了多久，
不用靠猜。/api/status 和 /api/busy 都会报。

超时兜底：占位超过 BUSY_MAX_SECONDS 视为泄漏，下一个请求可以抢占。
这是最后一道保险——上面的 finally 已经覆盖了正常路径，
但线程被杀、进程收到信号这类情况 finally 也不会执行。
网关本身 timeout=120，所以这个值必须明显大于它，
否则正常的长回复会被误判成泄漏而抢占。
"""

import time


# 占位超过这么久视为泄漏，允许抢占。
# 必须明显大于 server.call_gateway 的 requests timeout（120 秒），
# 否则一次慢回复就会被当成卡死。
BUSY_MAX_SECONDS = 300


def register_busy_guard(server_module):
    state = server_module.state
    state_lock = server_module.state_lock

    # 记录是谁占着、什么时候占的。诊断用。
    state.setdefault("busy_owner", "")
    state.setdefault("busy_since", 0)

    def acquire_busy(owner="chat"):
        """原子地占住网关。占到返回 True，已被占住返回 False。

        「查」和「占」必须在同一把锁里，这正是原来那个洞。
        """
        now = int(time.time())
        with state_lock:
            if state.get("busy"):
                since = int(state.get("busy_since") or 0)
                # 超时视为泄漏：finally 覆盖不到线程被杀这类情况。
                if since and now - since > BUSY_MAX_SECONDS:
                    print(
                        "[dwell] busy 占位超过 %d 秒（%s），视为泄漏并抢占"
                        % (BUSY_MAX_SECONDS, state.get("busy_owner") or "?")
                    )
                else:
                    return False
            state["busy"] = True
            state["busy_owner"] = str(owner)
            state["busy_since"] = now
            state["stop_flag"] = False
            return True

    def release_busy():
        with state_lock:
            state["busy"] = False
            state["busy_owner"] = ""
            state["busy_since"] = 0

    def busy_info():
        with state_lock:
            busy = bool(state.get("busy"))
            owner = state.get("busy_owner") or ""
            since = int(state.get("busy_since") or 0)
        return {
            "busy": busy,
            "owner": owner,
            "since": since,
            "held_seconds": int(time.time() - since) if busy and since else 0,
            "max_seconds": BUSY_MAX_SECONDS,
        }

    original_call_gateway = server_module.call_gateway

    def call_gateway_guarded(messages, model):
        """包一层，保证退出时一定释放。

        原函数内部自己也会设 busy=True 和 False，那些留着无害：
        调用方已经通过 acquire_busy 占过位，这里只保证一定释放。
        """
        try:
            return original_call_gateway(messages, model)
        finally:
            release_busy()

    server_module.acquire_busy = acquire_busy
    server_module.release_busy = release_busy
    server_module.busy_info = busy_info
    server_module.call_gateway = call_gateway_guarded

    def api_status():
        """替换 server.py 的 /api/status，多报占位者和占了多久。

        上游前端只读 alive 和 busy，多给几个字段不影响它。
        """
        from flask import jsonify

        info = busy_info()
        return jsonify({
            "alive": True,
            "busy": info["busy"],
            "busy_owner": info["owner"],
            "busy_held_seconds": info["held_seconds"],
            "since": int(time.time()),
        })

    def api_busy():
        """诊断用：现在是谁在跟网关说话、占了多久。"""
        from flask import jsonify

        payload = {"ok": True}
        payload.update(busy_info())
        return jsonify(payload)

    server_module.app.view_functions["api_status"] = api_status
    server_module.app.add_url_rule(
        "/api/busy", endpoint="api_busy", view_func=api_busy, methods=["GET"]
    )

    print("[dwell] busy 占位: 原子获取 + finally 释放（/api/busy 可查）")
    return acquire_busy
