"""语音转文字：收一段录音，返回文字。

给 voice_service.py 的 STT_URL 用，接口约定就是那边写的：
multipart 上传字段名 `file`，返回 {"text": ..., "emotion": ...}。

走阿里云 gummy-realtime-v1：多语种，中英混说也接得住。
没选 paraformer-realtime-v2 是因为那个要预先指定语种，突然说英文
会按中文去猜，出来是一串音近的乱码。也没选 8k 那个：那是电话
信道专用的 8kHz 模型，浏览器录出来的是 44.1k，降采样白白损质。

gummy 是 WebSocket 流式接口，而我们拿到的是录完的整段文件，所以
这里做的是「假装流式」：ffmpeg 转 16k 单声道 PCM，切块推进去，收完
返回。比要公网 URL 那条异步转写路子少绕一大圈（那条要先上 OSS）。

⚠️ 浏览器录出来的是 mp4/aac 或 webm/opus，采样率各不相同。不转格式
直接推，对面要么报错要么出乱码 —— ffmpeg 那一步不能省，转五秒的
音频不到一百毫秒。
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stt")

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MODEL = os.environ.get("STT_MODEL", "gummy-realtime-v1").strip()
CHUNK = 3200          # 每块 100ms @ 16kHz 单声道 16bit

app = FastAPI(docs_url=None, redoc_url=None)


def to_pcm(data: bytes, suffix: str) -> bytes:
    """任意格式 → 16kHz 单声道 16bit PCM。

    走临时文件而不是 stdin：mp4 的 moov box 在文件尾，ffmpeg 得能
    seek 才读得动，管道里会报 "moov atom not found"。
    """
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", tmp.name,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "16000",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr[:200].decode('utf-8', 'replace')}")
    return proc.stdout


def recognize(pcm: bytes) -> str:
    """把 PCM 推给 gummy，收回文字。"""
    import dashscope
    from dashscope.audio.asr import (
        TranslationRecognizerCallback,
        TranslationRecognizerRealtime,
    )

    dashscope.api_key = API_KEY
    parts: list[str] = []

    class Sink(TranslationRecognizerCallback):
        def on_event(self, request_id, transcription_result,
                     translation_result, usage) -> None:
            # 只收「说完了」的句子。中间结果是不断修正的全量文本，
            # 那些也拼进去就是一堆叠字。
            if transcription_result is None:
                return
            if getattr(transcription_result, "is_sentence_end", False):
                text = (transcription_result.text or "").strip()
                if text:
                    parts.append(text)

        def on_error(self, message) -> None:
            logger.warning("[stt] gummy error: %s", message)

    r = TranslationRecognizerRealtime(
        model=MODEL,
        format="pcm",
        sample_rate=16000,
        transcription_enabled=True,
        translation_enabled=False,
        callback=Sink(),
    )
    r.start()
    try:
        for i in range(0, len(pcm), CHUNK):
            r.send_audio_frame(pcm[i:i + CHUNK])
    finally:
        # stop() 会等到最后一句的 sentence_end 回来，不能省
        r.stop()
    return "".join(parts).strip()


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict:
    """失败一律软着陆：返回空文字加 error，别报 5xx。

    voice_service.transcribe_audio 那边拿到非 200 也只是记一行日志，
    但回 200 带 error 字段能让问题一直传到前端，好查得多。
    """
    if not API_KEY:
        return {"text": "", "emotion": "neutral", "error": "DASHSCOPE_API_KEY not set"}

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")

    suffix = Path(file.filename or "a.m4a").suffix.lower() or ".m4a"
    try:
        pcm = to_pcm(data, suffix)
    except Exception as exc:
        logger.warning("[stt] decode failed: %s", exc)
        return {"text": "", "emotion": "neutral", "error": f"decode: {exc}"}

    if len(pcm) < 3200:      # 不到 100ms，没什么可转的
        return {"text": "", "emotion": "neutral", "error": "too short"}

    try:
        text = recognize(pcm)
    except Exception as exc:
        logger.warning("[stt] recognize failed: %s", exc)
        return {"text": "", "emotion": "neutral", "error": str(exc)}

    logger.info("[stt] %d bytes → %r", len(data), text[:60])
    return {"text": text, "emotion": "neutral"}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": MODEL, "key": bool(API_KEY)}
