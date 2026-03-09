"""
Continuous mode: auto-segmentation for long-running streams.

Wraps an OnnxSession (or Session) and automatically detects silence
boundaries, splitting the transcript into numbered segments. The inner
session is NEVER reset — the model keeps its warm caches and full
context, so accuracy is identical to non-continuous mode.

Segmentation is purely a text-level operation: at each silence boundary,
the current text since the last boundary is emitted as a Final, and
subsequent Partials show only the new text.

Silence detection uses RMS energy on small sub-chunks (100ms windows),
regardless of how audio is batched by the WebSocket processor.

Two-pass refinement (optional): when enabled, raw PCM audio is buffered
alongside streaming. After an utterance boundary (2s silence or 30s max),
the buffered audio is sent to Parakeet TDT for higher-quality offline
transcription. The refined text replaces the streaming segments in-place.
"""

import io
import struct
import numpy as np

from stt_streaming.onnx_session import _decode_tokens


# ---------------------------------------------------------------------------
# Result types returned by ContinuousSession.feed_audio()
# ---------------------------------------------------------------------------

class Partial:
    """Interim transcription within the current segment."""
    __slots__ = ("text", "seq")

    def __init__(self, text: str, seq: int):
        self.text = text
        self.seq = seq


class Final:
    """Completed segment (silence boundary detected or explicit end)."""
    __slots__ = ("text", "seq")

    def __init__(self, text: str, seq: int):
        self.text = text
        self.seq = seq


class RefinementRequest:
    """Request to refine a group of segments via Parakeet (async, fire-and-forget)."""
    __slots__ = ("wav_bytes", "seq_start", "seq_end")

    def __init__(self, wav_bytes: bytes, seq_start: int, seq_end: int):
        self.wav_bytes = wav_bytes
        self.seq_start = seq_start
        self.seq_end = seq_end


# ---------------------------------------------------------------------------
# WAV assembly helper
# ---------------------------------------------------------------------------

def _pcm_to_wav(pcm_chunks: list[bytes], sample_rate: int = 16000) -> bytes:
    """Assemble raw PCM int16 chunks into an in-memory WAV file."""
    buf = io.BytesIO()
    total_pcm = b"".join(pcm_chunks)
    num_samples = len(total_pcm) // 2
    data_size = num_samples * 2

    # WAV header (44 bytes)
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))           # chunk size
    buf.write(struct.pack("<H", 1))            # PCM format
    buf.write(struct.pack("<H", 1))            # mono
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * 2))  # byte rate
    buf.write(struct.pack("<H", 2))            # block align
    buf.write(struct.pack("<H", 16))           # bits per sample
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(total_pcm)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Continuous session wrapper
# ---------------------------------------------------------------------------

DEFAULT_SILENCE_THRESHOLD_RMS = 0.01  # Normalized RMS energy threshold
DEFAULT_SILENCE_BOUNDARY_MS = 400     # Consecutive silence ms to trigger boundary
DEFAULT_MAX_SEGMENT_MS = 60_000       # Force boundary after this duration (0 = no limit)
DEFAULT_COMMIT_TIMEOUT_MS = 1000      # Force-commit deferred boundary after this much silence

# Refinement (two-pass) defaults
DEFAULT_REFINEMENT_SILENCE_MS = 2000   # Trigger refinement after 2s silence (utterance end)
DEFAULT_REFINEMENT_MAX_MS = 30_000     # Force-refine after 30s of audio (~960KB PCM)

# Sub-chunk size for energy analysis (100ms = 1600 samples = 3200 bytes)
_ANALYSIS_CHUNK_BYTES = 3200


class ContinuousSession:
    """
    Wraps a streaming session with silence-based auto-segmentation.

    The inner session runs continuously — no resets. Segmentation is
    text-level only: we track the full transcript at each boundary and
    emit per-segment text by stripping the prefix.

    This means:
    - Encoder caches stay warm (no cold-start accuracy loss)
    - LSTM decoder state maintains full context
    - Accuracy is identical to non-continuous mode
    """

    def __init__(
        self,
        create_session_fn,
        silence_threshold_rms: float = DEFAULT_SILENCE_THRESHOLD_RMS,
        silence_boundary_ms: int = DEFAULT_SILENCE_BOUNDARY_MS,
        max_segment_ms: int = DEFAULT_MAX_SEGMENT_MS,
        refine: bool = False,
        refinement_silence_ms: int = DEFAULT_REFINEMENT_SILENCE_MS,
        refinement_max_ms: int = DEFAULT_REFINEMENT_MAX_MS,
    ):
        self._session = create_session_fn()
        self._seq: int = 0

        # Silence detection state
        self._silence_threshold = silence_threshold_rms
        self._silence_boundary_ms = silence_boundary_ms
        self._max_segment_ms = max_segment_ms
        self._silence_ms: float = 0.0
        self._segment_ms: float = 0.0
        self._has_speech: bool = False

        # Token boundary tracking (no session reset needed).
        # Boundaries are deferred until the next word-start (▁-prefixed)
        # token, avoiding both mid-word splits AND backward jumps.
        self._boundary_token_idx: int = 0
        self._pending_boundary: bool = False  # silence detected, waiting for word start
        self._pending_token_idx: int = 0       # token count when silence was detected
        self._pending_silence_ms: float = 0.0  # silence accumulated since pending was set
        self._commit_timeout_ms = DEFAULT_COMMIT_TIMEOUT_MS

        # Two-pass refinement state
        self._refine: bool = refine
        self._refinement_silence_ms: int = refinement_silence_ms
        self._refinement_max_ms: int = refinement_max_ms
        self._refinement_audio_chunks: list[bytes] = []   # PCM audio since last refinement
        self._refinement_seq_start: int = 0                # first segment seq in current group
        self._refinement_audio_ms: float = 0.0             # audio duration in current group
        self._refinement_pending: bool = False              # waiting for Parakeet response
        self._refinement_has_speech: bool = False           # had speech in current group
        # Separate silence counter for refinement — not affected by
        # segment boundary logic which resets _silence_ms at 400ms.
        # Tracks consecutive silence since last speech, used to detect
        # utterance boundaries (2s silence).
        self._refinement_cont_silence_ms: float = 0.0
        # Audio split tracking: record the chunk index in
        # _refinement_audio_chunks at each segment boundary trigger.
        # This lets us split audio at the silence point (where the
        # boundary was detected) rather than at the current sub-chunk
        # (which may be past the next segment's speech onset due to
        # the deferred boundary mechanism).
        self._refinement_boundary_chunk_idx: int = 0

    @property
    def step_num(self) -> int:
        return self._session.step_num

    def _segment_text(self) -> str:
        """Decode current segment tokens (from boundary to latest)."""
        tokens = self._session.predicted_tokens
        if len(tokens) <= self._boundary_token_idx:
            return ""
        seg_tokens = tokens[self._boundary_token_idx:]
        return _decode_tokens(seg_tokens, self._session.vocab)

    def _check_deferred_boundary(self) -> int | None:
        """
        Check if a deferred boundary can now be committed.

        When silence triggers a boundary, we record the current token
        count and set _pending_boundary=True. We then search FORWARD
        from that point for the FIRST ▁-prefixed token (word start).
        We split RIGHT BEFORE it, so:

        - Everything the user already saw stays in the current segment
        - The next segment starts at a clean word boundary
        - No words jump backward between segments
        - No extra words get pulled into the current segment

        If silence continues for _commit_timeout_ms after the boundary
        was triggered (nobody is speaking), we force-commit at the
        current token end — no point waiting for a ▁ that may never come.

        Returns the token index to split at, or None if not ready.
        """
        if not self._pending_boundary:
            return None

        tokens = self._session.predicted_tokens
        vocab = self._session.vocab

        # Search forward from where silence was detected
        for i in range(self._pending_token_idx, len(tokens)):
            tid = tokens[i]
            if tid < len(vocab) and vocab[tid].startswith("\u2581"):
                self._pending_boundary = False
                self._pending_silence_ms = 0.0
                return i

        # Force-commit if silence continues past the timeout
        if self._pending_silence_ms >= self._commit_timeout_ms:
            self._pending_boundary = False
            self._pending_silence_ms = 0.0
            return len(tokens)

        return None

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[Partial | Final | RefinementRequest]:
        """
        Feed raw PCM audio. Returns mix of Partial, Final, and
        RefinementRequest results.

        The inner session is never reset. Segmentation is text-level:
        we track the full transcript at each silence boundary and emit
        per-segment text by stripping the prefix.

        When refinement is enabled, RefinementRequest is emitted after
        utterance boundaries (2s silence or 30s max audio). The caller
        should dispatch these asynchronously to Parakeet.
        """
        results: list[Partial | Final | RefinementRequest] = []

        for offset in range(0, len(pcm_int16_bytes), _ANALYSIS_CHUNK_BYTES):
            sub_chunk = pcm_int16_bytes[offset : offset + _ANALYSIS_CHUNK_BYTES]
            if len(sub_chunk) < 2:
                break

            # RMS energy of this sub-chunk
            samples = (
                np.frombuffer(sub_chunk, dtype=np.int16).astype(np.float32)
                * (1.0 / 32768.0)
            )
            chunk_ms = len(samples) * 1000.0 / 16000.0
            rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) > 0 else 0.0

            if rms < self._silence_threshold:
                self._silence_ms += chunk_ms
                if self._pending_boundary:
                    self._pending_silence_ms += chunk_ms
            else:
                self._silence_ms = 0.0
                self._has_speech = True
                if self._pending_boundary:
                    self._pending_silence_ms = 0.0

            self._segment_ms += chunk_ms

            # Buffer audio for refinement + track silence independently
            if self._refine:
                self._refinement_audio_chunks.append(sub_chunk)
                self._refinement_audio_ms += chunk_ms
                if rms >= self._silence_threshold:
                    self._refinement_has_speech = True
                    self._refinement_cont_silence_ms = 0.0
                else:
                    self._refinement_cont_silence_ms += chunk_ms

            # Feed to inner session (never reset)
            partials = self._session.feed_audio(sub_chunk)

            # Check if a deferred boundary can now be committed
            # (new ▁-prefixed token appeared since silence was detected)
            just_emitted_final = False
            split_idx = self._check_deferred_boundary()
            if split_idx is not None and split_idx > self._boundary_token_idx:
                seg_text = _decode_tokens(
                    self._session.predicted_tokens[self._boundary_token_idx : split_idx],
                    self._session.vocab,
                )
                if seg_text:
                    results.append(Final(text=seg_text, seq=self._seq))
                    self._seq += 1
                    just_emitted_final = True
                self._boundary_token_idx = split_idx
                self._segment_ms = 0.0
                self._has_speech = False

            for _text in partials:
                seg_text = self._segment_text()
                if seg_text:
                    results.append(Partial(text=seg_text, seq=self._seq))

            # Check for silence boundary trigger
            if not self._pending_boundary:
                should_segment = False
                if self._has_speech and self._silence_ms >= self._silence_boundary_ms:
                    should_segment = True
                if (
                    self._max_segment_ms > 0
                    and self._segment_ms >= self._max_segment_ms
                    and self._has_speech
                ):
                    should_segment = True

                if should_segment:
                    self._pending_boundary = True
                    self._pending_token_idx = len(self._session.predicted_tokens)
                    self._silence_ms = 0.0
                    # Record the audio split point for refinement.
                    # This is the silence point — before any next-segment
                    # speech has been fed. The deferred boundary may not
                    # commit until later (after the model sees the next
                    # word), but the audio should split HERE.
                    if self._refine:
                        self._refinement_boundary_chunk_idx = len(self._refinement_audio_chunks)

            # Check for refinement trigger.
            # Runs every sub-chunk (not just after Finals) because the
            # silence-based trigger needs to fire DURING silence — after
            # the last Final was emitted and 2s of silence accumulated.
            # The check itself requires at least one finalized segment
            # in the current refinement group (seq > seq_start).
            if self._refine and self._seq > self._refinement_seq_start:
                refinement_req = self._check_refinement_trigger(just_emitted_final)
                if refinement_req is not None:
                    results.append(refinement_req)

        return results

    def _check_refinement_trigger(self, just_emitted_final: bool = False) -> RefinementRequest | None:
        """
        Check if refinement should be triggered.

        Called on every sub-chunk (not just after Finals). Triggers when:
        1. Utterance end: continuous silence >= refinement_silence_ms
           (uses separate counter not affected by segment boundary logic)
        2. Max duration: audio >= refinement_max_ms AND a Final was just
           emitted (so we don't cut mid-segment)

        Returns a RefinementRequest or None.
        """
        if not self._refine or self._refinement_pending:
            return None
        if not self._refinement_has_speech:
            return None
        if not self._refinement_audio_chunks:
            return None

        should_refine = False

        # Utterance end: long continuous silence after speech.
        # Uses _refinement_cont_silence_ms which is independent of
        # the segment boundary _silence_ms (that one resets at 400ms).
        if self._refinement_cont_silence_ms >= self._refinement_silence_ms:
            should_refine = True

        # Max duration: force-refine, but only at a segment boundary
        # (just_emitted_final) to avoid cutting mid-segment.
        if (just_emitted_final
            and self._refinement_audio_ms >= self._refinement_max_ms):
            should_refine = True

        if not should_refine:
            return None

        # Build WAV and create request.
        # seq_end is the last finalized seq (_seq - 1, since _seq was
        # incremented after the most recent Final).
        seq_end = self._seq - 1
        seq_start = self._refinement_seq_start

        if seq_end < seq_start:
            return None

        # Split audio at the SILENCE POINT where the last segment
        # boundary was detected, not at the current sub-chunk.
        # This avoids including next-segment speech in the current
        # group (the deferred boundary mechanism means the Final
        # emits AFTER the model has ingested the next word's audio).
        split_idx = self._refinement_boundary_chunk_idx
        if split_idx <= 0:
            # No boundary recorded yet — use all audio
            split_idx = len(self._refinement_audio_chunks)

        send_chunks = self._refinement_audio_chunks[:split_idx]
        keep_chunks = self._refinement_audio_chunks[split_idx:]

        if not send_chunks:
            return None

        wav_bytes = _pcm_to_wav(send_chunks)
        req = RefinementRequest(
            wav_bytes=wav_bytes,
            seq_start=seq_start,
            seq_end=seq_end,
        )

        # Reset refinement state for next group.
        # Keep the audio AFTER the split point for the next group.
        self._refinement_pending = True
        self._refinement_audio_chunks = keep_chunks
        keep_ms = sum(len(c) / 2 / 16000 * 1000 for c in keep_chunks)
        self._refinement_audio_ms = keep_ms
        self._refinement_seq_start = self._seq
        self._refinement_has_speech = False
        self._refinement_cont_silence_ms = 0.0
        self._refinement_boundary_chunk_idx = 0

        return req

    def refinement_done(self):
        """Called when an async refinement completes (success or failure)."""
        self._refinement_pending = False

    def finalize(self) -> str:
        """Flush current segment. Called on {"type":"end"} or connection close."""
        self._session.finalize()
        return self._segment_text()

    def finalize_refinement(self) -> RefinementRequest | None:
        """
        Create a final refinement request for any remaining buffered audio.

        Called after finalize() when the stream ends, to refine the last
        group of segments that didn't hit the silence/max threshold.
        """
        if not self._refine:
            return None
        if not self._refinement_audio_chunks:
            return None
        if not self._refinement_has_speech:
            return None

        # The final segment was just emitted by finalize() at current _seq.
        # But finalize doesn't increment _seq, so seq_end = _seq.
        seq_end = self._seq
        seq_start = self._refinement_seq_start

        if seq_end < seq_start:
            return None

        wav_bytes = _pcm_to_wav(self._refinement_audio_chunks)
        req = RefinementRequest(
            wav_bytes=wav_bytes,
            seq_start=seq_start,
            seq_end=seq_end,
        )

        self._refinement_audio_chunks = []
        self._refinement_audio_ms = 0.0
        self._refinement_has_speech = False

        return req
