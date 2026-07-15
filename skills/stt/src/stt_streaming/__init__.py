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
import io
import json
import sys
import time
import wave
from threading import Lock
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

def _create_session(
    models: dict,
    lang: str,
    use_onnx: bool,
    min_preprocess_ms: int | None = None,
):
    """Create a Session or OnnxSession depending on mode."""
    if use_onnx:
        from stt_streaming.onnx_session import OnnxSession
        return OnnxSession(models[lang], lang, min_preprocess_ms=min_preprocess_ms)
    else:
        from stt_streaming.pytorch_session import Session
        return Session(models[lang], lang)


# ---------------------------------------------------------------------------
# Two-pass refinement (in-process Parakeet)
# ---------------------------------------------------------------------------

# The WebSocket server can call Parakeet directly in-process.
# This removes the extra HTTP hop and avoids running a separate parakeet-server.
_PARAKEET_TRANSCRIBER = None
_PARAKEET_TRANSCRIBER_INIT_LOCK = Lock()
_PARAKEET_INFER_LOCK = Lock()
_PARAKEET_NUM_THREADS = DEFAULT_NUM_THREADS


def _wav_to_pcm_s16le_mono_16k(wav_bytes: bytes) -> bytes:
    """Decode WAV bytes into raw PCM s16le mono 16k bytes."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        if channels != 1 or sample_rate != 16000 or sample_width != 2:
            raise ValueError(
                "Expected WAV to be mono 16kHz 16-bit "
                f"(got channels={channels}, rate={sample_rate}, width={sample_width * 8}bit)"
            )
        return wf.readframes(wf.getnframes())


def _get_parakeet_transcriber_sync():
    """Lazy-init singleton ParakeetTranscriber (thread-safe)."""
    global _PARAKEET_TRANSCRIBER

    if _PARAKEET_TRANSCRIBER is not None:
        return _PARAKEET_TRANSCRIBER

    with _PARAKEET_TRANSCRIBER_INIT_LOCK:
        if _PARAKEET_TRANSCRIBER is not None:
            return _PARAKEET_TRANSCRIBER

        from stt_parakeet import ParakeetTranscriber

        _log(f"Loading in-process Parakeet transcriber (threads={_PARAKEET_NUM_THREADS})...")
        _PARAKEET_TRANSCRIBER = ParakeetTranscriber(num_threads=_PARAKEET_NUM_THREADS)
        _log("In-process Parakeet transcriber ready")
        return _PARAKEET_TRANSCRIBER


def _transcribe_pcm_locked(transcriber, pcm: bytes) -> dict:
    """Serialize Parakeet inference to keep CPU usage predictable."""
    with _PARAKEET_INFER_LOCK:
        return transcriber.transcribe_pcm(pcm)


async def _transcribe_wav_bytes_with_parakeet(wav_bytes: bytes) -> dict:
    """Run Parakeet transcription in-process for a WAV audio blob."""
    pcm = await asyncio.to_thread(_wav_to_pcm_s16le_mono_16k, wav_bytes)
    transcriber = await asyncio.to_thread(_get_parakeet_transcriber_sync)
    return await asyncio.to_thread(_transcribe_pcm_locked, transcriber, pcm)


def _transcribe_pcm_with_timestamps_locked(transcriber, pcm: bytes) -> dict:
    """Timestamp-capable Parakeet inference under a single CPU lock."""
    with _PARAKEET_INFER_LOCK:
        return transcriber.transcribe_pcm_with_timestamps(pcm)


async def _transcribe_wav_bytes_with_parakeet_timestamps(wav_bytes: bytes) -> dict:
    """Run timestamp-capable Parakeet transcription for a WAV audio blob."""
    pcm = await asyncio.to_thread(_wav_to_pcm_s16le_mono_16k, wav_bytes)
    transcriber = await asyncio.to_thread(_get_parakeet_transcriber_sync)
    return await asyncio.to_thread(_transcribe_pcm_with_timestamps_locked, transcriber, pcm)


def _pcm_to_wav_s16le_mono_16k(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM s16le mono audio in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class RollingRefineChunkRequest:
    """One rolling Parakeet refinement job for a fixed audio timeline chunk."""

    __slots__ = (
        "chunk_id",
        "start_ms",
        "end_ms",
        "window_start_s",
        "core_start_s",
        "core_end_s",
        "wav_bytes",
    )

    def __init__(
        self,
        chunk_id: int,
        start_ms: int,
        end_ms: int,
        window_start_s: float,
        core_start_s: float,
        core_end_s: float,
        wav_bytes: bytes,
    ):
        self.chunk_id = chunk_id
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.window_start_s = window_start_s
        self.core_start_s = core_start_s
        self.core_end_s = core_end_s
        self.wav_bytes = wav_bytes


class RollingParakeetRefiner:
    """Build rolling left/chunk/right refinement jobs from raw PCM stream."""

    def __init__(
        self,
        chunk_ms: int,
        left_s: float,
        right_s: float,
        sample_rate: int = SAMPLE_RATE,
    ):
        self.sample_rate = sample_rate
        self.chunk_samples = int(chunk_ms * sample_rate / 1000)
        self.left_samples = int(left_s * sample_rate)
        self.right_samples = int(right_s * sample_rate)

        self.total_samples = 0
        self.buffer = bytearray()
        self.buffer_start_sample = 0

        self.next_live_chunk_id = 0
        self.next_refine_chunk_id = 0

    def append_pcm(self, pcm: bytes) -> tuple[list[dict], list[RollingRefineChunkRequest]]:
        """Append stream PCM and return (live_chunk_events, refine_jobs)."""
        if not pcm:
            return [], []

        samples = len(pcm) // 2
        self.buffer.extend(pcm)
        self.total_samples += samples

        live_events = self._pop_live_events()
        refine_jobs = self._pop_ready_refine_jobs()
        self._trim_buffer()
        return live_events, refine_jobs

    def finalize(self) -> tuple[list[dict], list[RollingRefineChunkRequest]]:
        """Flush remaining chunk boundaries and refinement jobs at stream end."""
        live_events = self._pop_live_events(final=True)
        refine_jobs = self._pop_ready_refine_jobs(final=True)
        return live_events, refine_jobs

    def _pop_live_events(self, final: bool = False) -> list[dict]:
        events: list[dict] = []

        while (self.next_live_chunk_id + 1) * self.chunk_samples <= self.total_samples:
            chunk_id = self.next_live_chunk_id
            start_sample = chunk_id * self.chunk_samples
            end_sample = (chunk_id + 1) * self.chunk_samples
            events.append(
                {
                    "chunk_id": chunk_id,
                    "start_ms": int(start_sample * 1000 / self.sample_rate),
                    "end_ms": int(end_sample * 1000 / self.sample_rate),
                }
            )
            self.next_live_chunk_id += 1

        # Emit trailing partial chunk boundary when stream ends.
        if final and self.next_live_chunk_id * self.chunk_samples < self.total_samples:
            chunk_id = self.next_live_chunk_id
            start_sample = chunk_id * self.chunk_samples
            end_sample = self.total_samples
            events.append(
                {
                    "chunk_id": chunk_id,
                    "start_ms": int(start_sample * 1000 / self.sample_rate),
                    "end_ms": int(end_sample * 1000 / self.sample_rate),
                }
            )
            self.next_live_chunk_id += 1

        return events

    def _pop_ready_refine_jobs(self, final: bool = False) -> list[RollingRefineChunkRequest]:
        jobs: list[RollingRefineChunkRequest] = []

        while self.next_refine_chunk_id * self.chunk_samples < self.total_samples:
            chunk_id = self.next_refine_chunk_id
            core_start = chunk_id * self.chunk_samples
            core_end = min((chunk_id + 1) * self.chunk_samples, self.total_samples)

            if not final:
                # Need right-context audio before this chunk can be refined.
                required = (chunk_id + 1) * self.chunk_samples + self.right_samples
                if self.total_samples < required:
                    break

            window_start = max(0, core_start - self.left_samples)
            window_end = min(self.total_samples, core_end + self.right_samples)
            pcm = self._slice_pcm(window_start, window_end)
            wav_bytes = _pcm_to_wav_s16le_mono_16k(pcm)

            jobs.append(
                RollingRefineChunkRequest(
                    chunk_id=chunk_id,
                    start_ms=int(core_start * 1000 / self.sample_rate),
                    end_ms=int(core_end * 1000 / self.sample_rate),
                    window_start_s=window_start / self.sample_rate,
                    core_start_s=core_start / self.sample_rate,
                    core_end_s=core_end / self.sample_rate,
                    wav_bytes=wav_bytes,
                )
            )
            self.next_refine_chunk_id += 1

        return jobs

    def _slice_pcm(self, start_sample: int, end_sample: int) -> bytes:
        if start_sample < self.buffer_start_sample:
            raise ValueError("Requested start sample is no longer in rolling buffer")

        rel_start = start_sample - self.buffer_start_sample
        rel_end = end_sample - self.buffer_start_sample
        byte_start = rel_start * 2
        byte_end = rel_end * 2
        return bytes(self.buffer[byte_start:byte_end])

    def _trim_buffer(self):
        # Keep only history needed for future left context windows.
        keep_from = max(0, self.next_refine_chunk_id * self.chunk_samples - self.left_samples)
        if keep_from <= self.buffer_start_sample:
            return

        drop_samples = keep_from - self.buffer_start_sample
        drop_bytes = drop_samples * 2
        if drop_bytes <= 0:
            return

        del self.buffer[:drop_bytes]
        self.buffer_start_sample = keep_from


class SeqContextRefineJob:
    """One seq-based refinement job with extra left/right context."""

    __slots__ = ("req", "wav_bytes", "center_start_s", "center_end_s")

    def __init__(self, req, wav_bytes: bytes, center_start_s: float, center_end_s: float):
        self.req = req
        self.wav_bytes = wav_bytes
        self.center_start_s = center_start_s
        self.center_end_s = center_end_s


class SeqContextRefiner:
    """Assemble seq-based refinement windows: left context + center + right context."""

    def __init__(self, left_s: float, right_s: float, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.left_samples = int(left_s * sample_rate)
        self.right_samples = int(right_s * sample_rate)

        self._pending_req = None
        self._pending_pcm: bytes | None = None
        self._pending_left_pcm: bytes = b""

    def submit(self, req) -> list[SeqContextRefineJob]:
        """Push a new request. Returns jobs ready to run now (usually previous request)."""
        pcm = _wav_to_pcm_s16le_mono_16k(req.wav_bytes)
        jobs: list[SeqContextRefineJob] = []

        if self._pending_req is not None and self._pending_pcm is not None:
            jobs.append(self._build_job(self._pending_req, self._pending_pcm, self._pending_left_pcm, pcm))
            left_for_new = self._pending_pcm
        else:
            left_for_new = b""

        self._pending_req = req
        self._pending_pcm = pcm
        self._pending_left_pcm = left_for_new
        return jobs

    def finalize(self) -> list[SeqContextRefineJob]:
        """Flush trailing pending request without right context."""
        jobs: list[SeqContextRefineJob] = []
        if self._pending_req is not None and self._pending_pcm is not None:
            jobs.append(self._build_job(self._pending_req, self._pending_pcm, self._pending_left_pcm, b""))

        self._pending_req = None
        self._pending_pcm = None
        self._pending_left_pcm = b""
        return jobs

    def _build_job(self, req, center_pcm: bytes, left_pcm: bytes, right_pcm: bytes) -> SeqContextRefineJob:
        left_ctx = left_pcm[-self.left_samples * 2 :] if self.left_samples > 0 else b""
        right_ctx = right_pcm[: self.right_samples * 2] if self.right_samples > 0 else b""

        window_pcm = left_ctx + center_pcm + right_ctx
        wav_bytes = _pcm_to_wav_s16le_mono_16k(window_pcm)

        center_start_s = (len(left_ctx) / 2) / self.sample_rate
        center_end_s = center_start_s + ((len(center_pcm) / 2) / self.sample_rate)
        return SeqContextRefineJob(req=req, wav_bytes=wav_bytes, center_start_s=center_start_s, center_end_s=center_end_s)


def _build_live_transcript(
    final_by_seq: dict[int, str],
    partial_by_seq: dict[int, str],
) -> str:
    """Build current full live transcript from continuous seq fragments."""
    seqs = sorted(set(final_by_seq) | set(partial_by_seq))
    parts: list[str] = []
    for seq in seqs:
        if seq in final_by_seq:
            t = final_by_seq[seq].strip()
            if t:
                parts.append(t)
        elif seq in partial_by_seq:
            t = partial_by_seq[seq].strip()
            if t:
                parts.append(t)
    return " ".join(parts).strip()


def _live_chunk_delta(prev_text: str, cur_text: str) -> str:
    """Best-effort incremental text for one timeline live chunk."""
    prev = prev_text or ""
    cur = cur_text or ""
    if cur.startswith(prev):
        return cur[len(prev):].strip()

    # Handle rewrites: cut at longest common prefix.
    i = 0
    lim = min(len(prev), len(cur))
    while i < lim and prev[i] == cur[i]:
        i += 1
    return cur[i:].strip()


def _decode_center_tokens(tokens, timestamps, start_s: float, end_s: float) -> str:
    """Decode only tokens whose timestamps fall inside [start_s, end_s)."""
    filtered_tokens: list[str] = []
    for tok, ts in zip(tokens or [], timestamps or []):
        t = float(ts)
        if start_s <= t < end_s:
            filtered_tokens.append(tok)

    transcriber = _get_parakeet_transcriber_sync()
    return transcriber.decode_tokens(filtered_tokens)


async def _do_parakeet_rolling_chunk(websocket, req: RollingRefineChunkRequest):
    """Refine one fixed timeline chunk and send chunk-keyed patch event."""
    try:
        data = await _transcribe_wav_bytes_with_parakeet_timestamps(req.wav_bytes)

        tokens = data.get("tokens") or []
        timestamps = [req.window_start_s + float(ts) for ts in (data.get("timestamps") or [])]
        text = await asyncio.to_thread(
            _decode_center_tokens,
            tokens,
            timestamps,
            req.core_start_s,
            req.core_end_s,
        )

        msg = {
            "type": "refined_chunk",
            "chunk_id": req.chunk_id,
            "start_ms": req.start_ms,
            "end_ms": req.end_ms,
            "text": text,
            "model": data.get("model", "parakeet"),
            "elapsed_s": data.get("elapsed_s", 0),
            "source": "parakeet_rolling",
        }
        try:
            await websocket.send(json.dumps(msg))
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _log(f"Rolling refinement failed for chunk={req.chunk_id}: {type(e).__name__}: {e}")


async def _do_refinement_context_job(websocket, job: SeqContextRefineJob):
    """Run seq-based refinement with extra context and emit legacy `refined` message."""
    try:
        data = await _transcribe_wav_bytes_with_parakeet_timestamps(job.wav_bytes)
        tokens = data.get("tokens") or []
        timestamps = data.get("timestamps") or []

        text = await asyncio.to_thread(
            _decode_center_tokens,
            tokens,
            timestamps,
            job.center_start_s,
            job.center_end_s,
        )

        msg = {
            "type": "refined",
            "seq_start": job.req.seq_start,
            "seq_end": job.req.seq_end,
            "text": text,
            "model": data.get("model", "parakeet"),
            "elapsed_s": data.get("elapsed_s", 0),
        }
        try:
            await websocket.send(json.dumps(msg))
            _log(
                f"Context refinement sent: seq {job.req.seq_start}-{job.req.seq_end} "
                f"({data.get('elapsed_s', '?')}s)"
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _log(f"Context refinement failed: {type(e).__name__}: {e}")


async def _do_refinement(websocket, req, session):
    """Transcribe buffered audio with Parakeet, send refined text to WebSocket."""
    try:
        data = await _transcribe_wav_bytes_with_parakeet(req.wav_bytes)

        text = data.get("text", "").strip()
        if not text:
            _log("Refinement returned empty text, skipping")
            return

        msg = {
            "type": "refined",
            "seq_start": req.seq_start,
            "seq_end": req.seq_end,
            "text": text,
            "model": data.get("model", "parakeet"),
            "elapsed_s": data.get("elapsed_s", 0),
        }
        try:
            await websocket.send(json.dumps(msg))
            _log(
                f"Refinement sent: seq {req.seq_start}-{req.seq_end} "
                f"({data.get('elapsed_s', '?')}s, {data.get('duration_s', '?')}s audio)"
            )
        except Exception:
            pass  # WebSocket already closed
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _log(f"Refinement failed: {type(e).__name__}: {e}")
    finally:
        if session is not None and hasattr(session, 'refinement_done'):
            session.refinement_done()


async def _do_parakeet_segment(websocket, req):
    """Transcribe one chunk with Parakeet and emit a final segment message."""
    try:
        data = await _transcribe_wav_bytes_with_parakeet(req.wav_bytes)

        text = data.get("text", "").strip()
        if not text:
            return

        msg = {
            "type": "final",
            "seq": req.seq,
            "text": text,
            "model": data.get("model", "parakeet"),
            "elapsed_s": data.get("elapsed_s", 0),
            "source": "parakeet",
        }
        try:
            await websocket.send(json.dumps(msg))
            _log(
                f"Parakeet-only sent seq={req.seq} "
                f"({data.get('elapsed_s', '?')}s, {data.get('duration_s', '?')}s audio)"
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _log(f"Parakeet-only chunk failed: {type(e).__name__}: {e}")


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
        refine=true      — enable two-pass refinement via in-process Parakeet
        refine_with_context=true   — for seq-based refine, add left/right context window per chunk
        refine_left_s=10           — left context seconds for refine_with_context
        refine_right_s=5           — right context seconds for refine_with_context
        refinement_silence_ms=1000 — silence to trigger refinement (default: 1000)
        refinement_max_ms=30000    — max audio before forced refinement (default: 30000)
        pre_ms=450                 — ONNX PCM aggregation threshold in ms
                                     (continuous default: 450, non-cont default: 650)
        parakeet_only=true         — skip live model; silence-chunk audio and transcribe chunks via in-process Parakeet
        parakeet_silence_ms=1000   — silence for Parakeet chunk boundary (default: 1000)
        parakeet_max_ms=30000      — force Parakeet chunk after this duration
        rolling_refine=true        — emit rolling chunk-keyed refinement patches (continuous EN only)
        rolling_chunk_ms=2000      — timeline chunk size in ms for live/refined chunk ids
        rolling_left_s=10          — left context in seconds for Parakeet rolling windows
        rolling_right_s=5          — right context in seconds for Parakeet rolling windows
    """
    path = websocket.request.path
    params = _parse_query_params(path)
    lang = params.get("lang", "en")
    continuous = params.get("continuous", "").lower() in ("true", "1", "yes")
    silence_ms = int(params.get("silence_ms", "400"))
    max_segment_ms = int(params.get("max_segment_ms", "60000"))
    refine = params.get("refine", "").lower() in ("true", "1", "yes")
    refine_with_context = params.get("refine_with_context", "").lower() in ("true", "1", "yes")
    refine_left_s = float(params.get("refine_left_s", "10"))
    refine_right_s = float(params.get("refine_right_s", "5"))
    refinement_silence_ms = int(params.get("refinement_silence_ms", "1000"))
    refinement_max_ms = int(params.get("refinement_max_ms", "30000"))

    # Parakeet-only mode: chunk by silence/timeout and transcribe chunks via Parakeet,
    # skipping the live FastConformer model entirely for this connection.
    parakeet_only = params.get("parakeet_only", "").lower() in ("true", "1", "yes")
    parakeet_silence_ms = int(params.get("parakeet_silence_ms", str(silence_ms if continuous else 1000)))
    parakeet_max_ms = int(params.get("parakeet_max_ms", "30000"))

    # Rolling chunk-keyed refinement (time-indexed patches).
    rolling_refine = params.get("rolling_refine", "").lower() in ("true", "1", "yes")
    rolling_chunk_ms = int(params.get("rolling_chunk_ms", "2000"))
    rolling_left_s = float(params.get("rolling_left_s", "10"))
    rolling_right_s = float(params.get("rolling_right_s", "5"))

    # ONNX PCM aggregation threshold (latency vs stability tradeoff).
    # Lower = more live feel, higher = slightly more stable first-pass text.
    # Defaults chosen per mode:
    #   continuous (UI/live): 450ms
    #   non-continuous:       650ms
    pre_ms_default = "450" if continuous else "650"
    pre_ms = int(params.get("pre_ms", pre_ms_default))
    pre_ms = max(50, min(pre_ms, 5000))

    refine_left_s = max(0.0, min(refine_left_s, 60.0))
    refine_right_s = max(0.0, min(refine_right_s, 30.0))

    rolling_chunk_ms = max(200, min(rolling_chunk_ms, 60_000))
    rolling_left_s = max(0.0, min(rolling_left_s, 60.0))
    rolling_right_s = max(0.0, min(rolling_right_s, 30.0))

    if parakeet_only:
        # Current in-process Parakeet model is English-only.
        if lang != "en":
            await websocket.send(json.dumps({
                "type": "error",
                "message": "parakeet_only currently supports lang=en only",
            }))
            await websocket.close()
            return
        # parakeet_only does not use two-pass/rolling refinement (it is already Parakeet-only).
        refine = False
        if refine_with_context:
            refine_with_context = False
            _log("refine_with_context is ignored with parakeet_only=true")
        if rolling_refine:
            rolling_refine = False
            _log("rolling_refine is ignored with parakeet_only=true")
        # Force continuous semantics (segmented Finals with seq).
        continuous = True
    else:
        if lang not in models:
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Language '{lang}' not available. Loaded: {list(models.keys())}",
            }))
            await websocket.close()
            return

        # Refinement only works in continuous mode (and only for English)
        if refine and not continuous:
            refine = False
            _log("refine=true requires continuous=true, ignoring")
        if refine and lang != "en":
            refine = False
            _log(f"refine=true only supports lang=en (got {lang}), ignoring")

        if refine_with_context and not refine:
            refine_with_context = False
            _log("refine_with_context=true requires refine=true, ignoring")
        if refine_with_context and not continuous:
            refine_with_context = False
            _log("refine_with_context=true requires continuous=true, ignoring")
        if refine_with_context and lang != "en":
            refine_with_context = False
            _log(f"refine_with_context=true only supports lang=en (got {lang}), ignoring")

        # Rolling refinement currently supports continuous English live mode.
        if rolling_refine and not continuous:
            rolling_refine = False
            _log("rolling_refine=true requires continuous=true, ignoring")
        if rolling_refine and lang != "en":
            rolling_refine = False
            _log(f"rolling_refine=true only supports lang=en (got {lang}), ignoring")
        if rolling_refine:
            # Don't mix utterance-level refinement with chunk-keyed rolling patches.
            refine = False
            if refine_with_context:
                refine_with_context = False
                _log("refine_with_context disabled because rolling_refine=true")

    if parakeet_only:
        from stt_streaming.parakeet_only import ParakeetOnlySession

        session = ParakeetOnlySession(
            silence_boundary_ms=parakeet_silence_ms,
            max_segment_ms=parakeet_max_ms,
        )
    elif continuous:
        from stt_streaming.continuous import ContinuousSession
        create_fn = lambda: _create_session(
            models,
            lang,
            use_onnx,
            min_preprocess_ms=pre_ms if use_onnx else None,
        )
        session = ContinuousSession(
            create_session_fn=create_fn,
            silence_boundary_ms=silence_ms,
            max_segment_ms=max_segment_ms,
            refine=refine,
            refinement_silence_ms=refinement_silence_ms,
            refinement_max_ms=refinement_max_ms,
        )
    else:
        session = _create_session(
            models,
            lang,
            use_onnx,
            min_preprocess_ms=pre_ms if use_onnx else None,
        )

    rolling_refiner = (
        RollingParakeetRefiner(
            chunk_ms=rolling_chunk_ms,
            left_s=rolling_left_s,
            right_s=rolling_right_s,
        )
        if rolling_refine and not parakeet_only
        else None
    )

    seq_context_refiner = (
        SeqContextRefiner(left_s=refine_left_s, right_s=refine_right_s)
        if refine and refine_with_context and not parakeet_only
        else None
    )

    ts_start = time.monotonic()
    mode_str = "parakeet_only" if parakeet_only else ("onnx" if use_onnx else "pytorch")
    cont_str = " continuous" if continuous else ""
    refine_str = " refine" if refine else ""
    refine_ctx_str = (
        f" refine_ctx left={refine_left_s}s right={refine_right_s}s"
        if seq_context_refiner is not None
        else ""
    )
    rolling_str = (
        f" rolling_refine chunk_ms={rolling_chunk_ms} left={rolling_left_s}s right={rolling_right_s}s"
        if rolling_refiner is not None
        else ""
    )
    pre_str = f" pre_ms={pre_ms}" if (use_onnx and not parakeet_only) else ""
    po_str = (
        f" silence_ms={parakeet_silence_ms} max_ms={parakeet_max_ms}"
        if parakeet_only
        else ""
    )
    _log(
        f"session open  lang={lang}  mode={mode_str}"
        f"{cont_str}{refine_str}{refine_ctx_str}{rolling_str}{pre_str}{po_str}"
    )

    await websocket.send(json.dumps({
        "type": "ready",
        "lang": lang,
        "continuous": continuous,
        "refine": refine,
        "refine_with_context": seq_context_refiner is not None,
        "refine_left_s": refine_left_s if seq_context_refiner is not None else None,
        "refine_right_s": refine_right_s if seq_context_refiner is not None else None,
        "rolling_refine": rolling_refiner is not None,
        "rolling_chunk_ms": rolling_chunk_ms if rolling_refiner is not None else None,
        "rolling_left_s": rolling_left_s if rolling_refiner is not None else None,
        "rolling_right_s": rolling_right_s if rolling_refiner is not None else None,
        "parakeet_only": parakeet_only,
        "pre_ms": pre_ms if (use_onnx and not parakeet_only) else None,
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

    # Track refinement tasks so we can cancel them on disconnect
    refinement_tasks: set[asyncio.Task] = set()

    # Live transcript state for rolling chunk-keyed events.
    final_by_seq: dict[int, str] = {}
    partial_by_seq: dict[int, str] = {}
    last_live_snapshot = ""

    async def processor():
        """Drain audio from queue, run inference, send results."""
        nonlocal session, rolling_refiner, seq_context_refiner, last_live_snapshot

        while True:
            item = await queue.get()
            if item is None:
                break

            if item is _SENTINEL_END:
                if parakeet_only:
                    final_req = await asyncio.to_thread(session.finalize)
                    if final_req is not None:
                        task = asyncio.create_task(_do_parakeet_segment(websocket, final_req))
                        refinement_tasks.add(task)
                        task.add_done_callback(refinement_tasks.discard)

                    # Drain all outstanding Parakeet chunk tasks, then notify client.
                    pending = [t for t in refinement_tasks if not t.done()]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    try:
                        await websocket.send(json.dumps({"type": "parakeet_done"}))
                    except Exception:
                        pass
                else:
                    final_text = await asyncio.to_thread(session.finalize)
                    try:
                        msg = {"type": "final", "text": final_text}
                        if continuous:
                            msg["seq"] = session._seq if hasattr(session, '_seq') else 0
                        await websocket.send(json.dumps(msg))
                    except Exception:
                        pass

                    # Emit final refinement request for remaining buffered audio.
                    final_ref_req = None
                    if refine and hasattr(session, 'finalize_refinement'):
                        final_ref_req = session.finalize_refinement()

                    if seq_context_refiner is not None:
                        ready_jobs: list[SeqContextRefineJob] = []
                        if final_ref_req is not None:
                            if hasattr(session, 'refinement_done'):
                                session.refinement_done()
                            ready_jobs.extend(await asyncio.to_thread(seq_context_refiner.submit, final_ref_req))

                        # Flush trailing pending seq request without right context.
                        ready_jobs.extend(await asyncio.to_thread(seq_context_refiner.finalize))

                        for job in ready_jobs:
                            task = asyncio.create_task(_do_refinement_context_job(websocket, job))
                            refinement_tasks.add(task)
                            task.add_done_callback(refinement_tasks.discard)
                    elif final_ref_req is not None:
                        task = asyncio.create_task(
                            _do_refinement(websocket, final_ref_req, session)
                        )
                        refinement_tasks.add(task)
                        task.add_done_callback(refinement_tasks.discard)

                    # Finalize rolling chunk boundaries and enqueue trailing refinements.
                    if rolling_refiner is not None:
                        cur_live_text = _build_live_transcript(final_by_seq, partial_by_seq)
                        live_events, refine_jobs = rolling_refiner.finalize()

                        for ev in live_events:
                            chunk_text = _live_chunk_delta(last_live_snapshot, cur_live_text)
                            last_live_snapshot = cur_live_text
                            try:
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "live_chunk",
                                            "chunk_id": ev["chunk_id"],
                                            "start_ms": ev["start_ms"],
                                            "end_ms": ev["end_ms"],
                                            "text": chunk_text,
                                            "source": "live",
                                        }
                                    )
                                )
                            except Exception:
                                pass

                        for req in refine_jobs:
                            task = asyncio.create_task(_do_parakeet_rolling_chunk(websocket, req))
                            refinement_tasks.add(task)
                            task.add_done_callback(refinement_tasks.discard)

                        # Drain rolling refinement patches before done notification.
                        pending = [t for t in refinement_tasks if not t.done()]
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        try:
                            await websocket.send(json.dumps({"type": "rolling_done"}))
                        except Exception:
                            pass

                continue

            if item is _SENTINEL_RESET:
                if parakeet_only:
                    from stt_streaming.parakeet_only import ParakeetOnlySession

                    session = ParakeetOnlySession(
                        silence_boundary_ms=parakeet_silence_ms,
                        max_segment_ms=parakeet_max_ms,
                    )
                else:
                    session = _create_session(
                        models,
                        lang,
                        use_onnx,
                        min_preprocess_ms=pre_ms if use_onnx else None,
                    )
                # Reset rolling/live state.
                final_by_seq.clear()
                partial_by_seq.clear()
                last_live_snapshot = ""
                if rolling_refiner is not None:
                    rolling_refiner = RollingParakeetRefiner(
                        chunk_ms=rolling_chunk_ms,
                        left_s=rolling_left_s,
                        right_s=rolling_right_s,
                    )
                if seq_context_refiner is not None:
                    seq_context_refiner = SeqContextRefiner(
                        left_s=refine_left_s,
                        right_s=refine_right_s,
                    )

                try:
                    await websocket.send(json.dumps({
                        "type": "ready",
                        "lang": lang,
                        "continuous": continuous,
                        "refine": refine,
                        "refine_with_context": seq_context_refiner is not None,
                        "refine_left_s": refine_left_s if seq_context_refiner is not None else None,
                        "refine_right_s": refine_right_s if seq_context_refiner is not None else None,
                        "rolling_refine": rolling_refiner is not None,
                        "rolling_chunk_ms": rolling_chunk_ms if rolling_refiner is not None else None,
                        "rolling_left_s": rolling_left_s if rolling_refiner is not None else None,
                        "rolling_right_s": rolling_right_s if rolling_refiner is not None else None,
                        "parakeet_only": parakeet_only,
                        "pre_ms": pre_ms if (use_onnx and not parakeet_only) else None,
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
            if parakeet_only:
                for req in results:
                    task = asyncio.create_task(_do_parakeet_segment(websocket, req))
                    refinement_tasks.add(task)
                    task.add_done_callback(refinement_tasks.discard)
            elif continuous:
                from stt_streaming.continuous import Partial, Final, RefinementRequest

                for result in results:
                    try:
                        if isinstance(result, RefinementRequest):
                            if seq_context_refiner is not None:
                                # Release ContinuousSession's refine gate immediately;
                                # refinement itself is delayed until right context is available.
                                if hasattr(session, 'refinement_done'):
                                    session.refinement_done()

                                ready_jobs = await asyncio.to_thread(seq_context_refiner.submit, result)
                                for job in ready_jobs:
                                    task = asyncio.create_task(
                                        _do_refinement_context_job(websocket, job)
                                    )
                                    refinement_tasks.add(task)
                                    task.add_done_callback(refinement_tasks.discard)
                            else:
                                # Fire-and-forget async refinement
                                task = asyncio.create_task(
                                    _do_refinement(websocket, result, session)
                                )
                                refinement_tasks.add(task)
                                task.add_done_callback(refinement_tasks.discard)
                        elif isinstance(result, Final):
                            final_by_seq[result.seq] = result.text
                            partial_by_seq.pop(result.seq, None)
                            await websocket.send(json.dumps({
                                "type": "final", "text": result.text, "seq": result.seq,
                            }))
                        elif isinstance(result, Partial):
                            if result.seq not in final_by_seq:
                                partial_by_seq[result.seq] = result.text
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

            # Rolling chunk-keyed live/refined events (time-indexed).
            if rolling_refiner is not None:
                cur_live_text = _build_live_transcript(final_by_seq, partial_by_seq)
                live_events, refine_jobs = rolling_refiner.append_pcm(bytes(all_audio))

                for ev in live_events:
                    chunk_text = _live_chunk_delta(last_live_snapshot, cur_live_text)
                    last_live_snapshot = cur_live_text
                    try:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "live_chunk",
                                    "chunk_id": ev["chunk_id"],
                                    "start_ms": ev["start_ms"],
                                    "end_ms": ev["end_ms"],
                                    "text": chunk_text,
                                    "source": "live",
                                }
                            )
                        )
                    except Exception:
                        return

                for req in refine_jobs:
                    task = asyncio.create_task(_do_parakeet_rolling_chunk(websocket, req))
                    refinement_tasks.add(task)
                    task.add_done_callback(refinement_tasks.discard)

    try:
        recv_task = asyncio.create_task(receiver())
        proc_task = asyncio.create_task(processor())
        await asyncio.gather(recv_task, proc_task)
    except Exception as e:
        name = type(e).__name__
        if "ConnectionClosed" not in name:
            _log(f"session error lang={lang}: {name}: {e}")
    finally:
        # Cancel any pending refinement tasks
        for task in refinement_tasks:
            task.cancel()
        if refinement_tasks:
            await asyncio.gather(*refinement_tasks, return_exceptions=True)
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
        default="70,1",
        help=(
            "Attention context size for EN multi-latency model as 'left,right'. "
            "Default: 70,1 (80ms). Options: 70,0 (0ms) / 70,1 (80ms)"
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

    global _PARAKEET_NUM_THREADS
    _PARAKEET_NUM_THREADS = args.threads

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
