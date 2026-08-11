"""把「谁在跟网关说话」这件事做成原子的，并保证一定会释放。

## 原来的三个问题

**一、检查和占位不是一步。** server.api_send 长这样：

    with state_lock:
        if state["busy"]: return 429
    save_message("her", text)                    # ← 锁已经放开了
    threading.Thread(target=call_gateway, ...)   # ← busy 是进了这里才设 True

从放锁到 call_gateway 真正把 busy 设成 True，中间有一段窗口，
第二个请求进来看到的仍然是 False。连点两次发送、
或者心跳刚好在这一刻醒过来，两路就会同时打网关：
消息顺序错乱、两份回复交叉写库。窗口很短，但不是不会发生。

同样的「先查 busy 再起线程」在发送、表情、心跳三处各写了一遍，
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

**三、刚按下的停止会被吃掉。** /api/stop 设 stop_flag=True，
但 call_gateway 开头又设回 False。发送之后立刻点停止，
那一下正好落在线程启动前的几毫秒里，进到生成函数就被清掉了——
界面上表现为「点了停止但它还在说」。

## 修法：把占位和停止都收敛到一处

第一版想的是在那三个入口各加一次 acquire。放弃了：以后再加入口
（音乐、日报、共读都要打网关）还是会漏掉一个，而漏掉的那个正是
最难复现的那种 bug。

现在 call_gateway 自己负责占位——它在同一把锁里查+占，占不到就
根本不发请求，返回 False。于是那三个调用点一行都不用改，
将来新加的入口也自动被管住。

停止同理：server.py 里那句 `stop_flag = False` 已经删掉，
改由这里的 acquire_busy 决定新的一代该不该停。
判断依据是 stop_armed_at——停止刚按下（10 秒内），
说明那一下是冲着这一代来的，直接让它停。

调用方原有的「先查 busy」留着，当预检用：那样界面能立刻收到 429，
而不是等线程起来才发现白跑一趟。真正的原子性在这一层。

超时抢占是最后一道保险。上面的 finally 覆盖了正常路径，
但线程被杀、进程收信号这类情况 finally 也不执行。
BUSY_MAX_SECONDS 必须明显大于网关自己的 timeout（120 秒），
否则一次慢回复会被误判成泄漏而被抢占。

注册顺序：必须在 agent_tools_feature 之后。那个模块会整体替换
server.call_gateway，先包的话包住的是旧版，之后被覆盖掉，
finally 就白写了。
"""

import time


# 占位超过这么久视为泄漏，允许抢占。
# 必须明显大于 server.call_gateway 的 requests timeout（120 秒），
# 否则一次慢回复就会被当成卡死。
BUSY_MAX_SECONDS = 300

# 「停止」按下之后多久之内起来的那一代算被停掉。
#
# 这个窗口是取舍：太长会让「停完隔一会儿再发」被误停，
# 太短则在网关慢、线程起得晚的时候仍然漏掉。
# 10 秒够覆盖线程启动和网关握手，也短于任何人重新想好话再发的时间。
STOP_ARM_SECONDS = 10


def register_busy_guard(server_module):
    state = server_module.state
    state_lock = server_module.state_lock

    # 记录是谁占着、什么时候占的。卡住时不用靠猜。
    state.setdefault("busy_owner", "")
    state.setdefault("busy_since", 0)
    # 停止按下的时刻。给「还没起来的那一代」用。
    state.setdefault("stop_armed_at", 0)

    def acquire_busy(owner="chat"):
        """原子地占住网关。占到返回 True，已被占住返回 False。

        「查」和「占」在同一把锁里完成，这正是原来那个洞。

        顺便决定这一代的 stop_flag：停止刚按下（STOP_ARM_SECONDS 内）
        说明那一下是冲着这一代来的，直接置 True 让它别开口。
        """
        now = time.time()
        with state_lock:
            if state.get("busy"):
                since = int(state.get("busy_since") or 0)
                if since and now - since > BUSY_MAX_SECONDS:
                    print(
                        "[dwell] busy 占位超过 %d 秒（%s），视为泄漏并抢占"
                        % (BUSY_MAX_SECONDS, state.get("busy_owner") or "?")
                    )
                else:
                    return False

            armed = float(state.get("stop_armed_at") or 0)
            stop_now = bool(armed and now - armed <= STOP_ARM_SECONDS)

            state["busy"] = True
            state["busy_owner"] = str(owner)
            state["busy_since"] = int(now)
            state["stop_flag"] = stop_now
            # 消耗掉，别让同一次「停止」把接下来好几代都停了。
            state["stop_armed_at"] = 0

        if stop_now:
            print("[dwell] 停止是刚按下的，这一代（%s）直接停" % owner)
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
            armed = float(state.get("stop_armed_at") or 0)
        return {
            "busy": busy,
            "owner": owner,
            "since": since,
            "held_seconds": int(time.time() - since) if busy and since else 0,
            "max_seconds": BUSY_MAX_SECONDS,
            "stop_armed": bool(armed and time.time() - armed <= STOP_ARM_SECONDS),
        }

    original_call_gateway = server_module.call_gateway

    def call_gateway_guarded(messages, model, owner="chat"):
        """打网关，但先把位子占住，并保证退出时释放。

        返回 False 表示压根没发请求（有人正占着）。心跳靠这个返回值
        决定要不要计入当夜次数——不然一次「没轮到」也会被算成醒过。

        原函数内部自己也会设 busy=True，那行留着无害：
        这里的 finally 是最终保证。
        """
        if not acquire_busy(owner):
            info = busy_info()
            print(
                "[dwell] %s 想说话，但 %s 已经占着 %d 秒，这次跳过"
                % (owner, info["owner"] or "?", info["held_seconds"])
            )
            # 让界面知道这条没有回复，而不是一直转圈等。
            try:
                server_module.broadcast({
                    "type": "stderr",
                    "text": "上一条还在生成，这一条没有重复发请求",
                })
                server_module.broadcast({
                    "type": "result", "is_error": True, "result": "busy",
                })
            except Exception:
                pass
            return False

        try:
            original_call_gateway(messages, model)
            return True
        finally:
            release_busy()

    server_module.acquire_busy = acquire_busy
    server_module.release_busy = release_busy
    server_module.busy_info = busy_info
    server_module.call_gateway = call_gateway_guarded

    def api_stop():
        """替换 server.py 的 /api/stop，额外记下「停止按在什么时候」。

        只置 stop_flag 是不够的：那一代可能还没开始，
        acquire_busy 会重新决定 stop_flag，把这一下覆盖掉。
        stop_armed_at 就是留给它看的。
        """
        from flask import jsonify

        with state_lock:
            state["stop_flag"] = True
            state["stop_armed_at"] = time.time()
        try:
            server_module.broadcast({"type": "system", "subtype": "stopped"})
        except Exception:
            pass
        return jsonify({"ok": True})

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
        """诊断用：现在是谁在跟网关说话、占了多久。

        held_seconds 一直涨且远超一次回复的时间，说明卡住了；
        以前只能重启，现在超过 max_seconds 会被自动抢占。
        """
        from flask import jsonify

        payload = {"ok": True}
        payload.update(busy_info())
        return jsonify(payload)

    server_module.app.view_functions["api_status"] = api_status
    server_module.app.view_functions["api_stop"] = api_stop
    server_module.app.add_url_rule(
        "/api/busy", endpoint="api_busy", view_func=api_busy, methods=["GET"]
    )

    print("[dwell] busy 占位: 原子获取 + finally 释放（/api/busy 可查）")
    return acquire_busy
