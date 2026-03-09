"""
stt-streaming: WebSocket streaming speech-to-text using NeMo FastConformer.

Loads cache-aware streaming models at startup and processes audio chunks
in real-time over WebSocket connections.

Supports two backends:
  - PyTorch/NeMo (default): loads NeMo models directly (~2GB memory)
  - ONNX Runtime (--onnx): lightweight numpy-only mode (~600MB memory)

Protocol:
    Client → Server:
        - Binary frames: raw PCM audio (16-bit signed LE, 16kHz, mono)
        - Text frames:   JSON control messages
            {"type": "end"}   — end of stream, triggers final flush
            {"type": "reset"} — reset session for a new utterance

    Server → Client:
        - {"type": "ready", "lang": "en", ...}    — session initialized
        - {"type": "partial", "text": "hello wor"} — interim transcription
        - {"type": "final", "text": "hello world."} — final transcription
        - {"type": "error", "message": "..."}       — error

Usage:
    uv run python -m stt_streaming --langs en --port 6771
    uv run python -m stt_streaming --langs en,ka --port 6771
    uv run python -m stt_streaming --langs en --onnx --port 6771
"""

import argparse
import asyncio
import json
import sys
import time
from urllib.parse import parse_qs, urlparse

SAMPLE_RATE = 16000

# Default number of CPU threads for inference.
DEFAULT_NUM_THREADS = 4

# Sentinel objects for the audio queue (producer/consumer pattern).
_SENTINEL_END = object()
_SENTINEL_RESET = object()


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def _parse_query_params(path: str) -> dict:
    """Extract query parameters from the WebSocket URL path."""
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return {k: v[0] for k, v in params.items()}


# ---------------------------------------------------------------------------
# Session creation — dispatches to PyTorch or ONNX backend
# ---------------------------------------------------------------------------

def _create_session(models: dict, lang: str, use_onnx: bool):
    """Create a Session or OnnxSession depending on mode."""
    if use_onnx:
        from stt_streaming.onnx_session import OnnxSession
        return OnnxSession(models[lang], lang)
    else:
        from stt_streaming.pytorch_session import Session
        return Session(models[lang], lang)


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

async def _handle_connection(websocket, models: dict, use_onnx: bool = False):
    """
    Handle one WebSocket streaming session.

    Uses a producer/consumer pattern: the receiver task consumes WebSocket
    messages as fast as they arrive and queues them; the processor task
    drains accumulated audio from the queue and runs inference. This
    decouples message ingestion from model inference.

    Query parameters:
        lang=en          — language (default: en)
        continuous=true  — enable continuous mode with auto-segmentation
        silence_ms=800   — silence duration to trigger segment boundary (default: 800)
        max_segment_ms=60000 — force boundary after this duration (default: 60000, 0=off)
    """
    path = websocket.request.path
    params = _parse_query_params(path)
    lang = params.get("lang", "en")
    continuous = params.get("continuous", "").lower() in ("true", "1", "yes")
    silence_ms = int(params.get("silence_ms", "400"))
    max_segment_ms = int(params.get("max_segment_ms", "60000"))

    if lang not in models:
        await websocket.send(json.dumps({
            "type": "error",
            "message": f"Language '{lang}' not available. Loaded: {list(models.keys())}",
        }))
        await websocket.close()
        return

    if continuous:
        from stt_streaming.continuous import ContinuousSession
        create_fn = lambda: _create_session(models, lang, use_onnx)
        session = ContinuousSession(
            create_session_fn=create_fn,
            silence_boundary_ms=silence_ms,
            max_segment_ms=max_segment_ms,
        )
    else:
        session = _create_session(models, lang, use_onnx)

    ts_start = time.monotonic()
    mode_str = "onnx" if use_onnx else "pytorch"
    cont_str = " continuous" if continuous else ""
    _log(f"session open  lang={lang}  mode={mode_str}{cont_str}")

    await websocket.send(json.dumps({
        "type": "ready",
        "lang": lang,
        "continuous": continuous,
        "sample_rate": SAMPLE_RATE,
        "encoding": "pcm_s16le",
        "channels": 1,
    }))

    queue: asyncio.Queue = asyncio.Queue()

    async def receiver():
        """Consume WebSocket messages and route to queue."""
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    if message:
                        await queue.put(message)
                elif isinstance(message, str):
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "message": "Invalid JSON",
                        }))
                        continue

                    msg_type = data.get("type")
                    if msg_type == "end":
                        await queue.put(_SENTINEL_END)
                    elif msg_type == "reset":
                        await queue.put(_SENTINEL_RESET)
                    else:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "message": f"Unknown message type: {msg_type}",
                        }))
        except Exception as e:
            name = type(e).__name__
            if "ConnectionClosed" not in name:
                _log(f"receiver error lang={lang}: {name}: {e}")
        finally:
            await queue.put(None)

    async def processor():
        """Drain audio from queue, run inference, send results."""
        nonlocal session

        while True:
            item = await queue.get()
            if item is None:
                break

            if item is _SENTINEL_END:
                final_text = await asyncio.to_thread(session.finalize)
                try:
                    msg = {"type": "final", "text": final_text}
                    if continuous:
                        msg["seq"] = session._seq if hasattr(session, '_seq') else 0
                    await websocket.send(json.dumps(msg))
                except Exception:
                    pass
                continue

            if item is _SENTINEL_RESET:
                session = _create_session(models, lang, use_onnx)
                try:
                    await websocket.send(json.dumps({
                        "type": "ready",
                        "lang": lang,
                    }))
                except Exception:
                    pass
                continue

            # Drain any additional audio frames into a single batch
            all_audio = bytearray(item)
            while not queue.empty():
                try:
                    peek = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(peek, bytes):
                    all_audio.extend(peek)
                else:
                    await queue.put(peek)
                    break

            results = await asyncio.to_thread(session.feed_audio, bytes(all_audio))
            if continuous:
                from stt_streaming.continuous import Partial, Final
                for result in results:
                    try:
                        if isinstance(result, Final):
                            await websocket.send(json.dumps({
                                "type": "final", "text": result.text, "seq": result.seq,
                            }))
                        else:
                            await websocket.send(json.dumps({
                                "type": "partial", "text": result.text, "seq": result.seq,
                            }))
                    except Exception:
                        return
            else:
                for text in results:
                    try:
                        await websocket.send(json.dumps({"type": "partial", "text": text}))
                    except Exception:
                        return

    try:
        recv_task = asyncio.create_task(receiver())
        proc_task = asyncio.create_task(processor())
        await asyncio.gather(recv_task, proc_task)
    except Exception as e:
        name = type(e).__name__
        if "ConnectionClosed" not in name:
            _log(f"session error lang={lang}: {name}: {e}")
    finally:
        elapsed = time.monotonic() - ts_start
        _log(f"session close lang={lang}  chunks={session.step_num}  elapsed={elapsed:.1f}s")


async def serve(host: str, port: int, models: dict, use_onnx: bool = False):
    """Start the WebSocket server (runs forever)."""
    import websockets

    async with websockets.serve(
        lambda ws: _handle_connection(ws, models, use_onnx=use_onnx),
        host,
        port,
        max_size=10 * 1024 * 1024,
        ping_interval=30,
        ping_timeout=120,
    ):
        mode_str = "ONNX Runtime" if use_onnx else "PyTorch"
        _log(f"stt-streaming listening on ws://{host}:{port}  ({mode_str})")
        _log(f"  Languages: {list(models.keys())}")
        _log(f"  Connect:   ws://{host}:{port}/stream?lang=<lang>")
        await asyncio.Future()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="stt-streaming",
        description="WebSocket streaming speech-to-text server (NeMo FastConformer)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=6771, help="Bind port (default: 6771)")
    parser.add_argument(
        "--langs",
        default="en",
        help="Comma-separated languages to load (default: en). Options: en, ka",
    )
    parser.add_argument(
        "--att-context-size",
        default="70,0",
        help=(
            "Attention context size for EN multi-latency model as 'left,right'. "
            "Default: 70,0 (0ms). Options: 70,0 (0ms) / 70,1 (80ms)"
        ),
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_NUM_THREADS,
        help=(
            f"Number of CPU threads for inference (default: {DEFAULT_NUM_THREADS}). "
            "For batch_size=1 streaming, 2-4 is usually optimal."
        ),
    )
    parser.add_argument(
        "--onnx",
        action="store_true",
        help=(
            "Use ONNX Runtime for inference (~6.8x faster, ~3x less memory). "
            "No PyTorch/NeMo loaded — pure numpy + ONNX Runtime. "
            "Requires pre-exported ONNX models in ~/.cache/stt-streaming-onnx/<lang>/. "
            "Run: uv run --python python3.11 python scripts/export_onnx.py --langs <lang>"
        ),
    )
    args = parser.parse_args()

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    att_ctx = [int(x) for x in args.att_context_size.split(",")]

    if args.onnx:
        from stt_streaming.onnx_session import load_onnx_sessions

        models = load_onnx_sessions(langs, num_threads=args.threads, att_context_size_en=att_ctx)
        if not models:
            print("No ONNX models loaded. Exiting.", file=sys.stderr)
            print(
                "Export them first: uv run --python python3.11 python scripts/export_onnx.py --langs "
                + ",".join(langs),
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        from stt_streaming.pytorch_session import load_models

        models = load_models(langs, att_context_size_en=att_ctx, num_threads=args.threads)
        if not models:
            print("No models loaded. Exiting.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(serve(args.host, args.port, models, use_onnx=args.onnx))
