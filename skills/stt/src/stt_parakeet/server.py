"""
parakeet-server: HTTP API for offline English STT using Parakeet TDT 0.6B v2 (ONNX).

Endpoints:
    POST /transcribe
        Body: raw audio bytes (any format ffmpeg can decode, or 16kHz mono WAV)
        Response: {"text": "...", "duration_s": 81.2, "elapsed_s": 1.5, "rtf": 0.018, "model": "parakeet-tdt-0.6b-v2"}

    GET /health
        Response: {"status": "ok", "model": "parakeet-tdt-0.6b-v2"}

Usage:
    parakeet-server --port 6774
    curl -X POST http://localhost:6774/transcribe --data-binary @audio.wav
"""

import argparse
import asyncio
import io
import shutil
import subprocess
import time
import wave

from aiohttp import web

from stt_parakeet import ParakeetTranscriber, _log

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _pcm_from_wav_bytes(data: bytes) -> bytes | None:
    """Try to read as 16kHz mono WAV. Returns PCM bytes or None."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getframerate() == 16000 and wf.getsampwidth() == 2:
                return wf.readframes(wf.getnframes())
    except Exception:
        pass
    return None


def _pcm_from_ffmpeg(data: bytes) -> bytes:
    """Convert arbitrary audio bytes to 16kHz mono PCM via ffmpeg."""
    result = subprocess.run(
        [FFMPEG, "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "s16le",
         "-acodec", "pcm_s16le", "pipe:1"],
        input=data, capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        raise ValueError(f"ffmpeg failed: {result.stderr[:500].decode(errors='replace')}")
    if not result.stdout:
        raise ValueError("ffmpeg produced no output")
    return result.stdout


def _cors_middleware():
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


def create_app(transcriber: ParakeetTranscriber) -> web.Application:
    app = web.Application(client_max_size=200 * 1024 * 1024, middlewares=[_cors_middleware()])

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "model": "parakeet-tdt-0.6b-v2"})

    async def transcribe(request: web.Request) -> web.Response:
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

        pcm = _pcm_from_wav_bytes(data)
        if pcm is None:
            try:
                pcm = await asyncio.to_thread(_pcm_from_ffmpeg, data)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

        duration_s = len(pcm) / 2 / 16000
        if duration_s < 0.1:
            return web.json_response({"error": "Audio too short (< 0.1s)"}, status=400)
        if duration_s > 7200:
            return web.json_response({"error": "Audio too long (> 2h)"}, status=400)

        result = await asyncio.to_thread(transcriber.transcribe_pcm, pcm)
        _log(f"HTTP transcribe: {duration_s:.1f}s audio → {result['elapsed_s']:.2f}s ({result['model']})")

        return web.json_response(result)

    app.router.add_get("/health", health)
    app.router.add_post("/transcribe", transcribe)
    return app


def serve_main():
    parser = argparse.ArgumentParser(prog="parakeet-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6774)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    transcriber = ParakeetTranscriber(num_threads=args.threads)
    app = create_app(transcriber)

    _log(f"parakeet-server listening on http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    serve_main()
