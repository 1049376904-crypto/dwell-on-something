"""voice 服务入口：把 voice_routes 的三个端点挂起来，加一层 token 鉴权。

跟 dwell 后端（Flask，8888）是两个进程，只通过 HTTP 说话。
跑在 127.0.0.1:8021，nginx 以 /api/voice 反代过来。

为什么不合进 Flask：TTS 和 STT 都要等外部服务好几秒，Flask 同步
模型下会把请求线程占住，聊天跟着卡。
"""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import voice_service as vs
from voice_routes import router as voice_router

TOKEN = os.environ.get("VOICE_TOKEN", "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 只有走 edge-tts 才需要预热（它冷启动要 14~31s）；
    # ElevenLabs 没这个问题，现在走的是它。
    if vs.EDGE_TTS_VOICE:
        import asyncio
        asyncio.create_task(vs.warm_tts())
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if TOKEN and request.url.path.startswith("/api/voice"):
        # <audio src> 带不了 header，所以也认 ?t= 查询参数
        sent = request.headers.get("x-voice-token") or request.query_params.get("t", "")
        # 按字节比：compare_digest 对 str 参数要求纯 ASCII，
        # 别人往 header 里塞中文会直接抛 TypeError 打成 500。
        if not secrets.compare_digest(sent.encode("utf-8", "replace"), TOKEN.encode("utf-8")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


app.include_router(voice_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "elevenlabs": bool(vs.ELEVENLABS_API_KEY and vs.ELEVENLABS_VOICE_ID),
        "edge_tts": bool(vs.EDGE_TTS_VOICE),
        "stt": vs.stt_available(),
    }
