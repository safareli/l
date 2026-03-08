"""
stt-streaming: WebSocket streaming speech-to-text using NeMo FastConformer.

Loads cache-aware streaming models at startup and processes audio chunks
in real-time over WebSocket connections.

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
import logging
import os
import sys
import time
import warnings
from typing import Optional
from urllib.parse import parse_qs, urlparse

import numpy as np
import torch

SAMPLE_RATE = 16000

# Minimum PCM to accumulate before preprocessing (in ms).
# Larger values → fewer mel boundary artifacts, but slightly higher latency.
MIN_PREPROCESS_MS = 200
MIN_PREPROCESS_SAMPLES = SAMPLE_RATE * MIN_PREPROCESS_MS // 1000  # 3200 samples

# Minimum bytes before we bother dispatching to the inference thread.
# 2 bytes per int16 sample.
MIN_PREPROCESS_BYTES = MIN_PREPROCESS_SAMPLES * 2

# Silence padding appended when finalizing a stream.
# Ensures the last partial chunk has enough data for the model.
FLUSH_PAD_MS = 560
FLUSH_PAD_SAMPLES = SAMPLE_RATE * FLUSH_PAD_MS // 1000

STREAMING_MODELS = {
    "en": "nvidia/stt_en_fastconformer_hybrid_large_streaming_multi",
    "ka": "nvidia/stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc",
}

# Attention context sizes for multi-latency English model.
# [70, 1] = 80ms look-ahead.
# Options: [70,0]=0ms  [70,1]=80ms  [70,6]=480ms  [70,13]=1040ms
DEFAULT_ATT_CONTEXT_SIZE_EN = [70, 1]

# Default number of PyTorch CPU threads for inference.
# For batch_size=1 streaming, fewer threads is better than the default
# (all cores) because thread synchronization overhead dominates.
# 4 threads is a good balance for most CPUs.
DEFAULT_NUM_THREADS = 4

# Sentinel objects for the audio queue (producer/consumer pattern).
_SENTINEL_END = object()
_SENTINEL_RESET = object()


def _suppress_logging():
    """Suppress NeMo's extremely verbose startup logging."""
    os.environ["NEMO_TESTING"] = "1"
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def load_models(
    langs: list[str],
    att_context_size_en: Optional[list[int]] = None,
    num_threads: int = DEFAULT_NUM_THREADS,
) -> dict:
    """
    Load streaming ASR models for the specified languages.
    Returns {lang: model} dict.
    """
    # Set PyTorch thread counts BEFORE loading models.
    # For batch_size=1 streaming inference, using all CPU cores causes
    # excessive thread synchronization overhead. 2-4 threads is optimal.
    torch.set_num_threads(num_threads)
    torch.set_num_interop_threads(1)
    _log(f"PyTorch threads: intra-op={num_threads}, inter-op=1")

    _suppress_logging()
    import nemo.collections.asr as nemo_asr

    models = {}
    for lang in langs:
        model_name = STREAMING_MODELS.get(lang)
        if not model_name:
            print(f"Unknown language '{lang}', skipping. Known: {list(STREAMING_MODELS.keys())}", file=sys.stderr)
            continue

        print(f"Loading {lang} streaming model: {model_name} ...", file=sys.stderr)
        t0 = time.monotonic()

        model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(model_name)

        # Configure attention context for multi-latency models (EN).
        if lang == "en" and att_context_size_en:
            if hasattr(model.encoder, "set_default_att_context_size"):
                model.encoder.set_default_att_context_size(att_context_size_en)

        # Ensure streaming params are initialized.
        if not hasattr(model.encoder, "streaming_cfg") or model.encoder.streaming_cfg is None:
            model.encoder.setup_streaming_params()

        # Configure RNN-T decoder for streaming.
        # Must use strategy="greedy" (not "greedy_batch") because only the
        # non-batched GreedyRNNTInfer supports partial_hypotheses, which is
        # needed to carry decoder state between streaming chunks.
        from nemo.collections.asr.parts.submodules.rnnt_decoding import RNNTDecodingConfig

        decoding_cfg = RNNTDecodingConfig(strategy="greedy", fused_batch_size=-1)
        model.change_decoding_strategy(decoding_cfg, decoder_type="rnnt")
        model.eval()

        cfg = model.encoder.streaming_cfg
        elapsed = time.monotonic() - t0
        print(
            f"  Loaded {lang} in {elapsed:.1f}s  "
            f"chunk_size={cfg.chunk_size}  shift_size={cfg.shift_size}  "
            f"pre_encode_cache={cfg.pre_encode_cache_size}",
            file=sys.stderr,
        )
        models[lang] = model

    return models


# ---------------------------------------------------------------------------
# Session — one per WebSocket connection
# ---------------------------------------------------------------------------

class Session:
    """
    Per-connection streaming state.

    Holds the NeMo CacheAwareStreamingAudioBuffer plus the encoder/decoder
    cache tensors that carry context from chunk to chunk.
    """

    def __init__(self, model, lang: str):
        from nemo.collections.asr.parts.utils.streaming_utils import CacheAwareStreamingAudioBuffer

        self.model = model
        self.lang = lang

        # Raw PCM accumulator: bytearray of int16 LE bytes.
        # Using bytearray instead of numpy array avoids O(n²) np.concatenate
        # on every feed_audio call — bytearray.extend() is amortized O(1).
        self.pcm_buffer = bytearray()

        # NeMo's streaming buffer handles feature extraction and chunking.
        self.streaming_buffer = CacheAwareStreamingAudioBuffer(model=model)

        # Encoder caches (batch_size=1).
        (
            self.cache_last_channel,
            self.cache_last_time,
            self.cache_last_channel_len,
        ) = model.encoder.get_initial_cache_state(batch_size=1)

        # RNN-T decoder state.
        self.previous_hypotheses = None
        self.pred_out = None
        self.step_num = 0
        self._has_audio = False

    # -- public API ----------------------------------------------------------

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[str]:
        """
        Feed raw PCM audio (16-bit signed LE, 16 kHz, mono).

        Accumulates samples until MIN_PREPROCESS_SAMPLES, then preprocesses
        and runs inference on any complete model chunks.

        Returns a list of partial transcription strings
        (one per model chunk processed, may be empty).
        """
        self.pcm_buffer.extend(pcm_int16_bytes)

        if len(self.pcm_buffer) < MIN_PREPROCESS_BYTES:
            return []

        # Convert accumulated bytes to float32 audio, then clear buffer.
        audio = np.frombuffer(bytes(self.pcm_buffer), dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
        self.pcm_buffer.clear()
        self._append_audio(audio)

        return self._process_chunks(is_final=False)

    def finalize(self) -> str:
        """
        Flush remaining audio with silence padding and return the final
        transcription for this utterance.
        """
        if self.pcm_buffer:
            remaining = np.frombuffer(bytes(self.pcm_buffer), dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
            self.pcm_buffer.clear()
        else:
            remaining = np.array([], dtype=np.float32)

        # Pad with silence so the last partial chunk fills a full model chunk.
        pad = np.zeros(FLUSH_PAD_SAMPLES, dtype=np.float32)
        audio = np.concatenate([remaining, pad])
        self._append_audio(audio)

        results = self._process_chunks(is_final=True)
        return results[-1] if results else ""

    # -- internals -----------------------------------------------------------

    def _append_audio(self, audio: np.ndarray):
        """Preprocess raw audio and append features to the streaming buffer."""
        if not self._has_audio:
            self.streaming_buffer.append_audio(audio, stream_id=-1)
            self._has_audio = True
        else:
            self.streaming_buffer.append_audio(audio, stream_id=0)

    def _process_chunks(self, is_final: bool) -> list[str]:
        """
        Iterate over all available model chunks in the streaming buffer
        and run conformer_stream_step on each.

        Uses a single torch.inference_mode() context for all chunks
        (avoids per-chunk context manager overhead).
        """
        results = []

        with torch.inference_mode():
            for chunk_audio, chunk_lengths in self.streaming_buffer:
                is_last_chunk = self.streaming_buffer.is_buffer_empty()

                # On step 0 there's no pre-encode cache to drop.
                drop = (
                    0
                    if self.step_num == 0
                    else self.model.encoder.streaming_cfg.drop_extra_pre_encoded
                )

                (
                    self.pred_out,
                    transcribed_texts,
                    self.cache_last_channel,
                    self.cache_last_time,
                    self.cache_last_channel_len,
                    self.previous_hypotheses,
                ) = self.model.conformer_stream_step(
                    processed_signal=chunk_audio,
                    processed_signal_length=chunk_lengths,
                    cache_last_channel=self.cache_last_channel,
                    cache_last_time=self.cache_last_time,
                    cache_last_channel_len=self.cache_last_channel_len,
                    keep_all_outputs=(is_final and is_last_chunk),
                    previous_hypotheses=self.previous_hypotheses,
                    previous_pred_out=self.pred_out,
                    drop_extra_pre_encoded=drop,
                    return_transcription=True,
                )
                self.step_num += 1

                text = _extract_text(transcribed_texts)
                results.append(text)

        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(hyps) -> str:
    """Extract plain text from NeMo model output hypotheses."""
    from nemo.collections.asr.parts.utils.rnnt_utils import Hypothesis

    if not hyps:
        return ""
    h = hyps[0]
    if isinstance(h, Hypothesis):
        return h.text or ""
    if isinstance(h, str):
        return h
    if isinstance(h, list):
        return h[0] if h else ""
    return str(h)


def _parse_lang_from_path(path: str) -> str:
    """Extract ?lang= query parameter from the WebSocket URL path."""
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    values = params.get("lang", [])
    return values[0] if values else "en"


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

def _create_session(models: dict, lang: str, use_onnx: bool):
    """Create a Session or OnnxSession depending on mode."""
    if use_onnx:
        from stt_streaming.onnx_session import OnnxSession
        return OnnxSession(models[lang], lang)
    else:
        return Session(models[lang], lang)


async def _handle_connection(websocket, models: dict, use_onnx: bool = False):
    """
    Handle one WebSocket streaming session.

    Uses a producer/consumer pattern: the receiver task consumes WebSocket
    messages as fast as they arrive and queues them; the processor task
    drains accumulated audio from the queue and runs inference. This
    decouples message ingestion from model inference, so when inference is
    slower than real-time audio rate, multiple audio frames get batched
    into a single feed_audio() call — reducing per-call overhead (thread
    dispatch, numpy conversion, mel preprocessing).
    """
    path = websocket.request.path
    lang = _parse_lang_from_path(path)

    if lang not in models:
        await websocket.send(json.dumps({
            "type": "error",
            "message": f"Language '{lang}' not available. Loaded: {list(models.keys())}",
        }))
        await websocket.close()
        return

    session = _create_session(models, lang, use_onnx)
    ts_start = time.monotonic()
    mode_str = "onnx" if use_onnx else "pytorch"
    _log(f"session open  lang={lang}  mode={mode_str}")

    await websocket.send(json.dumps({
        "type": "ready",
        "lang": lang,
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
            # Signal processor to stop (connection closed or error).
            await queue.put(None)

    async def processor():
        """Drain audio from queue, run inference, send results."""
        nonlocal session

        while True:
            item = await queue.get()
            if item is None:
                # Connection closed.
                break

            if item is _SENTINEL_END:
                final_text = await asyncio.to_thread(session.finalize)
                try:
                    await websocket.send(json.dumps({
                        "type": "final",
                        "text": final_text,
                    }))
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

            # item is audio bytes. Drain any additional audio frames that
            # arrived while the previous inference was running, batching
            # them into a single feed_audio() call.
            all_audio = bytearray(item)
            while not queue.empty():
                try:
                    peek = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(peek, bytes):
                    all_audio.extend(peek)
                else:
                    # Non-audio sentinel — put it back and stop draining.
                    await queue.put(peek)
                    break

            results = await asyncio.to_thread(session.feed_audio, bytes(all_audio))
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
        max_size=10 * 1024 * 1024,  # 10 MB max message size
        ping_interval=30,            # send ping every 30s
        ping_timeout=120,            # allow 120s without pong (model inference can block)
    ):
        mode_str = "ONNX Runtime" if use_onnx else "PyTorch"
        _log(f"stt-streaming listening on ws://{host}:{port}  ({mode_str})")
        _log(f"  Languages: {list(models.keys())}")
        _log(f"  Connect:   ws://{host}:{port}/stream?lang=<lang>")
        await asyncio.Future()  # run forever


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
        default="70,1",
        help=(
            "Attention context size for EN multi-latency model as 'left,right'. "
            "Default: 70,1 (80ms). Options: 70,0 (0ms) / 70,1 (80ms) / 70,6 (480ms) / 70,13 (1040ms)"
        ),
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_NUM_THREADS,
        help=(
            f"Number of PyTorch CPU threads for inference (default: {DEFAULT_NUM_THREADS}). "
            "For batch_size=1 streaming, 2-4 is usually optimal. "
            "Using all CPU cores causes excessive synchronization overhead."
        ),
    )
    parser.add_argument(
        "--onnx",
        action="store_true",
        help=(
            "Use ONNX Runtime for encoder/decoder inference (~6.8x faster). "
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
        models = load_models(langs, att_context_size_en=att_ctx, num_threads=args.threads)
        if not models:
            print("No models loaded. Exiting.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(serve(args.host, args.port, models, use_onnx=args.onnx))
