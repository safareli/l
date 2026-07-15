"""
stt_parakeet: Offline English STT using NVIDIA Parakeet TDT 0.6B v2 (ONNX).

Uses the onnx_asr library for preprocessing, encoder, and TDT decoding.
600M parameters, INT8 quantized (~652MB), ~30x real-time on CPU.
"""

import re
import sys
import time

import numpy as np
import onnx_asr
import onnxruntime as ort


def _log(msg: str) -> None:
    print(f"[stt_parakeet] {msg}", file=sys.stderr, flush=True)


MODEL_ID = "nemo-parakeet-tdt-0.6b-v2"
_DECODE_SPACE_PATTERN = re.compile(r"\A\s|\s\B|(\s)\b")


class ParakeetTranscriber:
    """Loads Parakeet TDT ONNX model once, transcribes audio on demand."""

    def __init__(self, num_threads: int = 4):
        _log(f"Loading {MODEL_ID} (INT8, ONNX)…")
        t0 = time.monotonic()

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = num_threads
        sess_options.inter_op_num_threads = 1
        sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._model = onnx_asr.load_model(
            MODEL_ID,
            quantization="int8",
            providers=["CPUExecutionProvider"],
            sess_options=sess_options,
        )
        # Timestamp-capable adapter for rolling chunk refinement.
        self._model_ts = self._model.with_timestamps()

        elapsed = time.monotonic() - t0
        _log(f"Model loaded in {elapsed:.1f}s")

    @staticmethod
    def decode_tokens(tokens: list[str]) -> str:
        """Decode token pieces to human-readable text."""
        return re.sub(_DECODE_SPACE_PATTERN, lambda x: " " if x.group(1) else "", "".join(tokens)).strip()

    def transcribe_pcm(self, pcm_bytes: bytes) -> dict:
        """Transcribe raw 16kHz mono 16-bit PCM bytes.

        Returns dict with text, duration_s, elapsed_s, rtf.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        duration_s = len(samples) / 16000.0

        t0 = time.monotonic()
        # model.recognize accepts numpy array directly (high-level API)
        text = self._model.recognize(samples, sample_rate=16000)
        elapsed_s = time.monotonic() - t0

        return {
            "text": text or "",
            "duration_s": round(duration_s, 2),
            "elapsed_s": round(elapsed_s, 3),
            "rtf": round(elapsed_s / duration_s, 4) if duration_s > 0 else 0,
            "model": "parakeet-tdt-0.6b-v2",
        }

    def transcribe_pcm_with_timestamps(self, pcm_bytes: bytes) -> dict:
        """Transcribe PCM and return token-level timestamps for center-chunk filtering."""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        duration_s = len(samples) / 16000.0

        t0 = time.monotonic()
        result = self._model_ts.recognize(samples, sample_rate=16000)
        elapsed_s = time.monotonic() - t0

        tokens = list(result.tokens or [])
        timestamps = [float(x) for x in (result.timestamps or [])]
        text = result.text or self.decode_tokens(tokens)

        return {
            "text": text,
            "tokens": tokens,
            "timestamps": timestamps,
            "duration_s": round(duration_s, 2),
            "elapsed_s": round(elapsed_s, 3),
            "rtf": round(elapsed_s / duration_s, 4) if duration_s > 0 else 0,
            "model": "parakeet-tdt-0.6b-v2",
        }
