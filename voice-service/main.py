"""voice 服务入口：把 voice_routes 的三个端点挂起来，加一层 token 鉴权。

跟 dwell 后端（Flask，8888）是两个进程，只通过 HTTP 说话。
跑在 127.0.0.1:8021，nginx 以 /api/voice 反代过来。

为什么不合进 Flask：TTS 和 STT 都要等外部服务好几秒，Flask 同步
模型下会把请求线程占住，聊天跟着卡。

⚠️ 这里在 include_router **之前**注册了自己的 `/api/voice/tts`，
给合成结果做磁盘缓存。FastAPI 按注册顺序匹配、先注册的赢，所以上游
那条（注释里明确写了"不做缓存"，因为 iOS 客户端自己落盘）被盖住 ——
上游文件一个字没改。网页端不缓存的话，同一句话听三遍按三倍字符收钱。
"""
from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import voice_service as vs
from voice_routes import router as voice_router

TOKEN = os.environ.get("VOICE_TOKEN", "").strip()

# 合成好的音频存这儿。一句话几十 KB，不做自动清理 ——
# 真涨起来了手动删掉就行，删了只是下次重新合成。
TTS_DIR = Path(os.environ.get("TTS_CACHE_DIR", "uploads/tts")).resolve()


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


class TTSBody(BaseModel):
    text: str = ""


def _voice_sig() -> str:
    """当前声音的指纹。换了音色或模型，缓存必须作废。"""
    parts = [
        getattr(vs, "ELEVENLABS_VOICE_ID", "") or "",
        getattr(vs, "ELEVENLABS_MODEL", "") or "",
        getattr(vs, "EDGE_TTS_VOICE", "") or "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _key_of(text: str) -> str:
    return hashlib.sha1((_voice_sig() + "\x00" + text).encode("utf-8")).hexdigest()


def _cached(key: str):
    """命中就返回 (bytes, mime)，没有返回 None。"""
    for ext, mime in ((".mp3", "audio/mpeg"), (".m4a", "audio/mp4"),
                      (".wav", "audio/wav"), (".ogg", "audio/ogg")):
        p = TTS_DIR / (key + ext)
        if p.is_file():
            try:
                return p.read_bytes(), mime
            except OSError:
                return None
    return None


def _ext_of(mime: str) -> str:
    m = (mime or "").lower()
    if "mpeg" in m or "mp3" in m:
        return ".mp3"
    if "wav" in m:
        return ".wav"
    if "ogg" in m or "opus" in m:
        return ".ogg"
    return ".m4a"


# ⚠️ 必须在 include_router 之前注册，才能盖住上游那条同路径的。
@app.post("/api/voice/tts")
async def voice_tts_cached(body: TTSBody) -> Response:
    """把一句话念出来，顺手在磁盘上留一份。

    返回头里带 X-TTS-Key，客户端存下来就能用
    GET /api/voice/say/{key} 复听，不再花钱。
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    key = _key_of(text)
    hit = _cached(key)
    if hit is not None:
        audio, mime = hit
        return Response(content=audio, media_type=mime,
                        headers={"X-TTS-Cache": "hit", "X-TTS-Key": key})

    out = await vs.synthesize_speech(text)
    if not out:
        # 三档全哑。客户端该掉到设备自带的合成器上，
        # 别在这儿假装成功回一段空音频。
        raise HTTPException(status_code=503, detail="tts unavailable")
    audio, mime = out

    try:
        TTS_DIR.mkdir(parents=True, exist_ok=True)
        (TTS_DIR / (key + _ext_of(mime))).write_bytes(audio)
    except OSError:
        pass          # 存不下就算了，别耽误这一次播放

    return Response(content=audio, media_type=mime,
                    headers={"X-TTS-Cache": "miss", "X-TTS-Key": key})


@app.get("/api/voice/say/{key}")
async def voice_say(key: str) -> Response:
    """按缓存键取合成好的音频。

    `<audio src>` 带不了 header，所以走 GET + ?t= 传 token
    （中间件那边已经认了查询参数）。
    """
    safe = "".join(c for c in key.lower() if c in "0123456789abcdef")
    if len(safe) != 40:
        raise HTTPException(status_code=400, detail="bad key")
    hit = _cached(safe)
    if hit is None:
        raise HTTPException(status_code=404, detail="not cached")
    audio, mime = hit
    return Response(content=audio, media_type=mime)


app.include_router(voice_router)


@app.get("/healthz")
async def healthz() -> dict:
    n = 0
    try:
        n = sum(1 for _ in TTS_DIR.iterdir())
    except OSError:
        pass
    return {
        "ok": True,
        "elevenlabs": bool(vs.ELEVENLABS_API_KEY and vs.ELEVENLABS_VOICE_ID),
        "edge_tts": bool(vs.EDGE_TTS_VOICE),
        "stt": vs.stt_available(),
        "tts_cached": n,
        "voice_sig": _voice_sig(),
    }
