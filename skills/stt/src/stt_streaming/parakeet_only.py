"""
Parakeet-only streaming chunker.

Consumes PCM audio from the WebSocket stream, splits it by silence / max duration,
and emits chunk requests to be transcribed by Parakeet. No live FastConformer
inference is run for this mode.
"""

import io
import struct

import numpy as np

DEFAULT_SILENCE_THRESHOLD_RMS = 0.01
DEFAULT_SILENCE_BOUNDARY_MS = 1000
DEFAULT_MAX_SEGMENT_MS = 30_000

# 100ms analysis windows at 16kHz mono s16le
_ANALYSIS_CHUNK_BYTES = 3200


class ParakeetRequest:
    __slots__ = ("wav_bytes", "seq")

    def __init__(self, wav_bytes: bytes, seq: int):
        self.wav_bytes = wav_bytes
        self.seq = seq


def _pcm_to_wav(pcm_chunks: list[bytes], sample_rate: int = 16000) -> bytes:
    """Assemble raw PCM int16 chunks into an in-memory WAV blob."""
    total_pcm = b"".join(pcm_chunks)
    data_size = len(total_pcm)

    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))  # fmt chunk size
    buf.write(struct.pack("<H", 1))   # PCM
    buf.write(struct.pack("<H", 1))   # mono
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", sample_rate * 2))  # byte rate
    buf.write(struct.pack("<H", 2))   # block align
    buf.write(struct.pack("<H", 16))  # bits per sample
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(total_pcm)
    return buf.getvalue()


class ParakeetOnlySession:
    """
    Silence-chunking state machine for Parakeet-only WebSocket mode.

    Emits ParakeetRequest(seq, wav_bytes) whenever an utterance boundary
    is detected or max duration is reached.
    """

    def __init__(
        self,
        silence_boundary_ms: int = DEFAULT_SILENCE_BOUNDARY_MS,
        max_segment_ms: int = DEFAULT_MAX_SEGMENT_MS,
        silence_threshold_rms: float = DEFAULT_SILENCE_THRESHOLD_RMS,
    ):
        self.silence_boundary_ms = int(silence_boundary_ms)
        self.max_segment_ms = int(max_segment_ms)
        self.silence_threshold_rms = float(silence_threshold_rms)

        self._seq = 0
        self._silence_ms = 0.0
        self._segment_ms = 0.0
        self._has_speech = False
        self._audio_chunks: list[bytes] = []

        # For compatibility with outer logging.
        self.step_num = 0

    def reset(self):
        self._silence_ms = 0.0
        self._segment_ms = 0.0
        self._has_speech = False
        self._audio_chunks = []

    def _emit_segment(self) -> ParakeetRequest | None:
        if not self._has_speech or not self._audio_chunks:
            self.reset()
            return None

        wav_bytes = _pcm_to_wav(self._audio_chunks)
        req = ParakeetRequest(wav_bytes=wav_bytes, seq=self._seq)
        self._seq += 1
        self.reset()
        return req

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[ParakeetRequest]:
        results: list[ParakeetRequest] = []

        for offset in range(0, len(pcm_int16_bytes), _ANALYSIS_CHUNK_BYTES):
            sub_chunk = pcm_int16_bytes[offset : offset + _ANALYSIS_CHUNK_BYTES]
            if len(sub_chunk) < 2:
                break

            self.step_num += 1
            self._audio_chunks.append(sub_chunk)

            samples = (
                np.frombuffer(sub_chunk, dtype=np.int16).astype(np.float32)
                * (1.0 / 32768.0)
            )
            chunk_ms = len(samples) * 1000.0 / 16000.0
            rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) > 0 else 0.0

            if rms < self.silence_threshold_rms:
                self._silence_ms += chunk_ms
            else:
                self._silence_ms = 0.0
                self._has_speech = True

            self._segment_ms += chunk_ms

            should_emit = False
            if self._has_speech and self._silence_ms >= self.silence_boundary_ms:
                should_emit = True
            if self.max_segment_ms > 0 and self._has_speech and self._segment_ms >= self.max_segment_ms:
                should_emit = True

            if should_emit:
                req = self._emit_segment()
                if req is not None:
                    results.append(req)

        return results

    def finalize(self) -> ParakeetRequest | None:
        """Flush remaining buffered utterance at stream end."""
        return self._emit_segment()
