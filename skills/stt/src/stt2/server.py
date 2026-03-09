"""
stt2-server: HTTP API for offline English speech-to-text.

Loads the ONNX model once at startup. Each request is pure inference (~2s for 81s audio).

Endpoints:
    POST /transcribe
        Body: raw audio bytes (any format ffmpeg can decode, or 16kHz mono WAV)
        Content-Type: application/octet-stream (or audio/wav, audio/*, multipart/form-data)
        Response: {"text": "...", "duration_s": 81.2, "elapsed_s": 2.19, "rtf": 0.027}

    GET /health
        Response: {"status": "ok", "model": "en", "decoder": "rnnt"}

Usage:
    stt2-server --port 6773
    curl -X POST http://localhost:6773/transcribe --data-binary @audio.wav
    curl -X POST http://localhost:6773/transcribe -F "file=@recording.mp3"
"""

import argparse
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

from aiohttp import web

from stt2 import Transcriber, _log

# Find ffmpeg — may not be on PATH in systemd context
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _pcm_from_wav_bytes(data: bytes) -> bytes | None:
    """Try to read as 16kHz mono WAV. Returns PCM bytes or None if not valid."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getframerate() == 16000 and wf.getsampwidth() == 2:
                return wf.readframes(wf.getnframes())
    except Exception:
        pass
    return None


def _pcm_from_ffmpeg(data: bytes) -> bytes:
    """Convert arbitrary audio bytes to 16kHz mono PCM via ffmpeg (stdin → stdout)."""
    result = subprocess.run(
        [
            FFMPEG, "-i", "pipe:0",
            "-ar", "16000", "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise ValueError(f"ffmpeg failed: {result.stderr[:500].decode(errors='replace')}")
    if not result.stdout:
        raise ValueError("ffmpeg produced no output")
    return result.stdout


def _cors_middleware():
    """Allow cross-origin requests (stt-live on :6772 calls us on :6773)."""
    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    return middleware


def create_app(transcriber: Transcriber) -> web.Application:
    app = web.Application(client_max_size=200 * 1024 * 1024, middlewares=[_cors_middleware()])

    async def health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "model": "en",
        })

    async def transcribe(request: web.Request) -> web.Response:
        t0 = time.monotonic()

        # Read audio data from request body or multipart form
        content_type = request.content_type or ""

        if "multipart" in content_type:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"error": "No file in multipart form"}, status=400)
            data = await field.read(decode=False)
        else:
            data = await request.read()

        if not data:
            return web.json_response({"error": "Empty request body"}, status=400)

        # Convert to 16kHz mono PCM
        pcm = _pcm_from_wav_bytes(data)
        if pcm is None:
            try:
                pcm = await asyncio.to_thread(_pcm_from_ffmpeg, data)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

        duration_s = len(pcm) / 2 / 16000  # 16-bit = 2 bytes per sample
        if duration_s < 0.1:
            return web.json_response({"error": "Audio too short (< 0.1s)"}, status=400)
        if duration_s > 7200:
            return web.json_response({"error": "Audio too long (> 2h)"}, status=400)

        # Transcribe (in thread pool to not block event loop)
        result = await asyncio.to_thread(transcriber.transcribe_pcm, pcm)

        upload_time = t0  # could track upload vs inference separately
        _log(f"HTTP transcribe: {duration_s:.1f}s audio → {result['elapsed_s']:.2f}s inference")

        return web.json_response(result)

    app.router.add_get("/health", health)
    app.router.add_post("/transcribe", transcribe)

    return app


def serve_main():
    parser = argparse.ArgumentParser(prog="stt2-server", description="HTTP API for offline English STT (ONNX)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=6773, help="Bind port (default: 6773)")
    parser.add_argument("--threads", type=int, default=4, help="ONNX Runtime threads (default: 4)")
    args = parser.parse_args()

    transcriber = Transcriber(num_threads=args.threads)
    app = create_app(transcriber)

    _log(f"stt2-server listening on http://{args.host}:{args.port}")
    _log(f"  POST /transcribe — upload audio, get transcript")
    _log(f"  GET  /health")

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    serve_main()
