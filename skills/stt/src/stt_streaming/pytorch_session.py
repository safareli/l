"""
PyTorch/NeMo-based streaming session for stt-streaming.

This module contains the PyTorch model loading and Session class.
It is imported only when --onnx is NOT used, keeping the ONNX code path
free of PyTorch/NeMo dependencies.
"""

import logging
import os
import sys
import time
import warnings
from typing import Optional

import numpy as np
import torch

SAMPLE_RATE = 16000

# Minimum PCM to accumulate before preprocessing (in ms).
MIN_PREPROCESS_MS = 200
MIN_PREPROCESS_SAMPLES = SAMPLE_RATE * MIN_PREPROCESS_MS // 1000  # 3200
MIN_PREPROCESS_BYTES = MIN_PREPROCESS_SAMPLES * 2

# Silence padding appended when finalizing a stream.
FLUSH_PAD_MS = 560
FLUSH_PAD_SAMPLES = SAMPLE_RATE * FLUSH_PAD_MS // 1000

STREAMING_MODELS = {
    "en": "nvidia/stt_en_fastconformer_hybrid_large_streaming_multi",
    "ka": "nvidia/stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc",
}

# Attention context sizes for multi-latency English model.
DEFAULT_ATT_CONTEXT_SIZE_EN = [70, 1]

# Default number of PyTorch CPU threads.
DEFAULT_NUM_THREADS = 4


def _suppress_logging():
    """Suppress NeMo's extremely verbose startup logging."""
    os.environ["NEMO_TESTING"] = "1"
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


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


def load_models(
    langs: list[str],
    att_context_size_en: Optional[list[int]] = None,
    num_threads: int = DEFAULT_NUM_THREADS,
) -> dict:
    """
    Load streaming ASR models for the specified languages.
    Returns {lang: model} dict.
    """
    torch.set_num_threads(num_threads)
    torch.set_num_interop_threads(1)
    _log(f"PyTorch threads: intra-op={num_threads}, inter-op=1")

    _suppress_logging()
    import nemo.collections.asr as nemo_asr

    models = {}
    for lang in langs:
        model_name = STREAMING_MODELS.get(lang)
        if not model_name:
            print(
                f"Unknown language '{lang}', skipping. Known: {list(STREAMING_MODELS.keys())}",
                file=sys.stderr,
            )
            continue

        print(f"Loading {lang} streaming model: {model_name} ...", file=sys.stderr)
        t0 = time.monotonic()

        model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(model_name)

        if lang == "en" and att_context_size_en:
            if hasattr(model.encoder, "set_default_att_context_size"):
                model.encoder.set_default_att_context_size(att_context_size_en)

        if not hasattr(model.encoder, "streaming_cfg") or model.encoder.streaming_cfg is None:
            model.encoder.setup_streaming_params()

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


class Session:
    """
    Per-connection streaming state (PyTorch/NeMo backend).

    Holds the NeMo CacheAwareStreamingAudioBuffer plus the encoder/decoder
    cache tensors that carry context from chunk to chunk.
    """

    def __init__(self, model, lang: str):
        from nemo.collections.asr.parts.utils.streaming_utils import CacheAwareStreamingAudioBuffer

        self.model = model
        self.lang = lang

        self.pcm_buffer = bytearray()
        self.streaming_buffer = CacheAwareStreamingAudioBuffer(model=model)

        (
            self.cache_last_channel,
            self.cache_last_time,
            self.cache_last_channel_len,
        ) = model.encoder.get_initial_cache_state(batch_size=1)

        self.previous_hypotheses = None
        self.pred_out = None
        self.step_num = 0
        self._has_audio = False

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[str]:
        """
        Feed raw PCM audio (16-bit signed LE, 16 kHz, mono).
        Returns a list of partial transcription strings.
        """
        self.pcm_buffer.extend(pcm_int16_bytes)

        if len(self.pcm_buffer) < MIN_PREPROCESS_BYTES:
            return []

        audio = (
            np.frombuffer(bytes(self.pcm_buffer), dtype=np.int16).astype(np.float32)
            * (1.0 / 32768.0)
        )
        self.pcm_buffer.clear()
        self._append_audio(audio)

        return self._process_chunks(is_final=False)

    def finalize(self) -> str:
        """Flush remaining audio and return final transcription."""
        if self.pcm_buffer:
            remaining = (
                np.frombuffer(bytes(self.pcm_buffer), dtype=np.int16).astype(np.float32)
                * (1.0 / 32768.0)
            )
            self.pcm_buffer.clear()
        else:
            remaining = np.array([], dtype=np.float32)

        pad = np.zeros(FLUSH_PAD_SAMPLES, dtype=np.float32)
        audio = np.concatenate([remaining, pad])
        self._append_audio(audio)

        results = self._process_chunks(is_final=True)
        return results[-1] if results else ""

    def _append_audio(self, audio: np.ndarray):
        if not self._has_audio:
            self.streaming_buffer.append_audio(audio, stream_id=-1)
            self._has_audio = True
        else:
            self.streaming_buffer.append_audio(audio, stream_id=0)

    def _process_chunks(self, is_final: bool) -> list[str]:
        results = []

        with torch.inference_mode():
            for chunk_audio, chunk_lengths in self.streaming_buffer:
                is_last_chunk = self.streaming_buffer.is_buffer_empty()

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
