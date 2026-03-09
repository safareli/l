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
"""

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


# ---------------------------------------------------------------------------
# Continuous session wrapper
# ---------------------------------------------------------------------------

DEFAULT_SILENCE_THRESHOLD_RMS = 0.01  # Normalized RMS energy threshold
DEFAULT_SILENCE_BOUNDARY_MS = 400     # Consecutive silence ms to trigger boundary
DEFAULT_MAX_SEGMENT_MS = 60_000       # Force boundary after this duration (0 = no limit)
DEFAULT_COMMIT_TIMEOUT_MS = 1000      # Force-commit deferred boundary after this much silence

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

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[Partial | Final]:
        """
        Feed raw PCM audio. Returns mix of Partial and Final results.

        The inner session is never reset. Segmentation is text-level:
        we track the full transcript at each silence boundary and emit
        per-segment text by stripping the prefix.
        """
        results: list[Partial | Final] = []

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

            # Feed to inner session (never reset)
            partials = self._session.feed_audio(sub_chunk)

            # Check if a deferred boundary can now be committed
            # (new ▁-prefixed token appeared since silence was detected)
            split_idx = self._check_deferred_boundary()
            if split_idx is not None and split_idx > self._boundary_token_idx:
                seg_text = _decode_tokens(
                    self._session.predicted_tokens[self._boundary_token_idx : split_idx],
                    self._session.vocab,
                )
                if seg_text:
                    results.append(Final(text=seg_text, seq=self._seq))
                    self._seq += 1
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

        return results

    def finalize(self) -> str:
        """Flush current segment. Called on {"type":"end"} or connection close."""
        self._session.finalize()
        return self._segment_text()
