"""
ONNX Runtime-based streaming session for stt-streaming.

Replaces PyTorch encoder/decoder inference with ONNX Runtime for ~6.8x
speedup on the encoder forward pass (165ms → 24ms per chunk), enabling
comfortable real-time streaming on CPU.

The NeMo preprocessor and CacheAwareStreamingAudioBuffer are still used
for mel spectrogram extraction and chunk management — these are fast
numpy/torch operations, not the bottleneck.

Cache tensor ordering:
  - PyTorch (get_initial_cache_state): layers-first → [num_layers, B, T, D]
  - ONNX (forward_for_export): batch-first → [B, num_layers, T, D]
"""

import json
import os
import sys
import time
from typing import Optional

import numpy as np
import onnxruntime as ort

CACHE_BASE_DIR = os.path.expanduser("~/.cache/stt-streaming-onnx")


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


def load_onnx_sessions(
    langs: list[str],
    num_threads: int = 4,
    att_context_size_en: Optional[list[int]] = None,
) -> dict:
    """
    Load ONNX Runtime sessions for the specified languages.

    Returns {lang: {"encoder": ort.InferenceSession,
                    "decoder_joint": ort.InferenceSession,
                    "metadata": dict,
                    "model": NeMo model}} dict.

    Also loads the NeMo model for each language (needed for preprocessor
    and CacheAwareStreamingAudioBuffer).
    """
    sessions = {}

    for lang in langs:
        lang_dir = os.path.join(CACHE_BASE_DIR, lang)
        encoder_path = os.path.join(lang_dir, "encoder-model.onnx")
        decoder_path = os.path.join(lang_dir, "decoder_joint-model.onnx")
        metadata_path = os.path.join(lang_dir, "metadata.json")

        if not all(os.path.exists(p) for p in [encoder_path, decoder_path, metadata_path]):
            _log(f"ONNX models not found for {lang} in {lang_dir}")
            _log(f"  Run: uv run --python python3.11 python scripts/export_onnx.py --langs {lang}")
            continue

        _log(f"Loading ONNX sessions for {lang}...")
        t0 = time.monotonic()

        # Load metadata
        with open(metadata_path) as f:
            metadata = json.load(f)

        # Configure ONNX Runtime session options
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = num_threads
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # Load encoder session
        enc_sess = ort.InferenceSession(
            encoder_path,
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        # Load decoder+joint session
        dec_sess = ort.InferenceSession(
            decoder_path,
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        elapsed = time.monotonic() - t0
        _log(f"  ONNX sessions loaded for {lang} in {elapsed:.1f}s")

        # Now load the NeMo model for preprocessor + streaming buffer
        _log(f"  Loading NeMo model for {lang} preprocessor...")
        t0 = time.monotonic()

        # Suppress NeMo logging
        import warnings
        import logging
        os.environ["NEMO_TESTING"] = "1"
        logging.disable(logging.WARNING)
        warnings.filterwarnings("ignore")

        import torch
        torch.set_num_threads(num_threads)
        torch.set_num_interop_threads(1)

        import nemo.collections.asr as nemo_asr
        model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(
            metadata["model_name"]
        )

        # Configure attention context for English multi-latency model.
        # Use the CLI-provided att_context_size if given, otherwise use
        # the left context from the exported cache shape.
        if lang == "en" and hasattr(model.encoder, "set_default_att_context_size"):
            if att_context_size_en:
                model.encoder.set_default_att_context_size(att_context_size_en)
            else:
                left_ctx = metadata["cache_last_channel_shape"][2]  # e.g. 70
                model.encoder.set_default_att_context_size([left_ctx, 1])

        if not hasattr(model.encoder, "streaming_cfg") or model.encoder.streaming_cfg is None:
            model.encoder.setup_streaming_params()

        model.eval()

        elapsed = time.monotonic() - t0
        _log(f"  NeMo model loaded for {lang} preprocessor in {elapsed:.1f}s")

        sessions[lang] = {
            "encoder": enc_sess,
            "decoder_joint": dec_sess,
            "metadata": metadata,
            "model": model,
        }

    return sessions


def greedy_rnnt_decode(
    encoder_out: np.ndarray,
    decoder_joint_sess: ort.InferenceSession,
    prev_tokens: list[int],
    lstm_states: tuple[np.ndarray, np.ndarray],
    blank_id: int,
    max_symbols_per_step: int = 10,
) -> tuple[list[int], tuple[np.ndarray, np.ndarray]]:
    """
    Greedy RNN-T decoding loop using ONNX decoder+joint model.

    For each encoder time step:
      1. Run decoder+joint on (encoder[t], last_token, lstm_states) → logits
      2. argmax → if blank, move to next time step; else append token, repeat

    Args:
        encoder_out: [B, D, T'] encoder hidden states (B=1)
        decoder_joint_sess: ONNX Runtime session for decoder+joint
        prev_tokens: list of previously predicted token IDs
        lstm_states: tuple of (hidden, cell) each [num_layers, B, hidden_size]
        blank_id: blank token ID (typically 1024)
        max_symbols_per_step: safety limit to prevent infinite loops

    Returns:
        (updated_tokens, updated_lstm_states)
    """
    tokens = list(prev_tokens)
    B, D, T = encoder_out.shape  # [1, 512, T']

    for t in range(T):
        enc_t = encoder_out[:, :, t : t + 1]  # [1, 512, 1]
        symbols_emitted = 0

        while symbols_emitted < max_symbols_per_step:
            # Target: last predicted token (or blank_id for start-of-sequence)
            last_token = tokens[-1] if tokens else blank_id
            target = np.array([[last_token]], dtype=np.int32)
            target_len = np.array([1], dtype=np.int32)

            joint_out, _, new_s1, new_s2 = decoder_joint_sess.run(
                None,
                {
                    "encoder_outputs": enc_t,
                    "targets": target,
                    "target_length": target_len,
                    "input_states_1": lstm_states[0],
                    "input_states_2": lstm_states[1],
                },
            )

            # joint_out shape: [1, 1, 1, vocab_size+1]
            logit = joint_out[0, 0, 0, :]
            pred = int(np.argmax(logit))

            if pred == blank_id:
                break  # move to next encoder time step

            tokens.append(pred)
            lstm_states = (new_s1, new_s2)
            symbols_emitted += 1

    return tokens, lstm_states


class OnnxSession:
    """
    Per-connection ONNX-based streaming session.

    Drop-in replacement for the PyTorch Session class. Uses ONNX Runtime
    for encoder and decoder+joint inference, with NeMo's preprocessor and
    CacheAwareStreamingAudioBuffer for mel extraction and chunking.
    """

    SAMPLE_RATE = 16000
    MIN_PREPROCESS_MS = 200
    MIN_PREPROCESS_SAMPLES = SAMPLE_RATE * MIN_PREPROCESS_MS // 1000  # 3200
    MIN_PREPROCESS_BYTES = MIN_PREPROCESS_SAMPLES * 2
    FLUSH_PAD_MS = 560
    FLUSH_PAD_SAMPLES = SAMPLE_RATE * FLUSH_PAD_MS // 1000

    def __init__(self, session_data: dict, lang: str):
        """
        Args:
            session_data: dict with keys "encoder", "decoder_joint", "metadata", "model"
            lang: language code
        """
        import torch
        from nemo.collections.asr.parts.utils.streaming_utils import CacheAwareStreamingAudioBuffer

        self.encoder_sess = session_data["encoder"]
        self.decoder_joint_sess = session_data["decoder_joint"]
        self.metadata = session_data["metadata"]
        self.model = session_data["model"]
        self.lang = lang

        # Tokenizer from the NeMo model
        self.tokenizer = self.model.tokenizer

        # Blank ID
        self.blank_id = self.metadata["blank_id"]

        # Raw PCM accumulator
        self.pcm_buffer = bytearray()

        # NeMo's streaming buffer for mel extraction and chunking
        self.streaming_buffer = CacheAwareStreamingAudioBuffer(model=self.model)

        # Encoder caches — ONNX uses batch-first format: [B, num_layers, T, D]
        ch_shape = self.metadata["cache_last_channel_shape"]  # [1, 17, 70, 512]
        t_shape = self.metadata["cache_last_time_shape"]       # [1, 17, 512, 8]
        self.cache_last_channel = np.zeros(ch_shape, dtype=np.float32)
        self.cache_last_time = np.zeros(t_shape, dtype=np.float32)
        self.cache_last_channel_len = np.zeros([1], dtype=np.int64)

        # RNN-T decoder state (LSTM hidden + cell)
        lstm_hidden = self.metadata.get("lstm_hidden_size", 640)
        lstm_layers = self.metadata.get("lstm_num_layers", 1)
        self.lstm_states = (
            np.zeros([lstm_layers, 1, lstm_hidden], dtype=np.float32),
            np.zeros([lstm_layers, 1, lstm_hidden], dtype=np.float32),
        )

        # Predicted tokens so far
        self.predicted_tokens: list[int] = []

        self.step_num = 0
        self._has_audio = False

    def feed_audio(self, pcm_int16_bytes: bytes) -> list[str]:
        """
        Feed raw PCM audio (16-bit signed LE, 16 kHz, mono).
        Returns list of partial transcription strings.
        """
        self.pcm_buffer.extend(pcm_int16_bytes)

        if len(self.pcm_buffer) < self.MIN_PREPROCESS_BYTES:
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

        pad = np.zeros(self.FLUSH_PAD_SAMPLES, dtype=np.float32)
        audio = np.concatenate([remaining, pad])
        self._append_audio(audio)

        results = self._process_chunks(is_final=True)
        return results[-1] if results else ""

    def _append_audio(self, audio: np.ndarray):
        """Preprocess raw audio and append features to the streaming buffer."""
        if not self._has_audio:
            self.streaming_buffer.append_audio(audio, stream_id=-1)
            self._has_audio = True
        else:
            self.streaming_buffer.append_audio(audio, stream_id=0)

    def _process_chunks(self, is_final: bool) -> list[str]:
        """
        Iterate over all available model chunks and run ONNX inference.
        """
        results = []

        for chunk_audio, chunk_lengths in self.streaming_buffer:
            text = self._onnx_stream_step(chunk_audio, chunk_lengths)
            self.step_num += 1
            results.append(text)

        return results

    # Minimum mel frames the ONNX encoder can handle. The ONNX-exported
    # ConvSubsampling produces a 0-length intermediate for very short inputs
    # (e.g. 9 mel frames with subsampling_factor=8), crashing the Conv node.
    # 12 frames is the minimum that works. Shorter chunks are zero-padded.
    MIN_ONNX_MEL_FRAMES = 12

    def _onnx_stream_step(
        self,
        processed_signal,
        processed_signal_length,
    ) -> str:
        """
        One streaming step using ONNX Runtime.

        1. Run preprocessed mel features through ONNX encoder
        2. Run greedy RNN-T decoding through ONNX decoder+joint
        3. Decode tokens to text

        Note: The ONNX encoder already applies output trimming (valid_out_len)
        and drop_extra_pre_encoded internally — these are baked into the exported
        graph via forward_for_export → streaming_post_process(keep_all_outputs=False).
        We don't need to do additional trimming here. For the final chunk, the
        silence padding in finalize() ensures trailing content is pushed through.
        """
        import torch

        # Convert torch tensors to numpy for ONNX Runtime
        if isinstance(processed_signal, torch.Tensor):
            audio_signal = processed_signal.numpy()
        else:
            audio_signal = np.asarray(processed_signal, dtype=np.float32)

        if isinstance(processed_signal_length, torch.Tensor):
            length = processed_signal_length.numpy().astype(np.int64)
        else:
            length = np.asarray(processed_signal_length, dtype=np.int64)

        # Pad short chunks to minimum required by the ONNX encoder.
        # The first streaming chunk can be as small as 9 mel frames (per
        # streaming_cfg.chunk_size[0]), but the ONNX ConvSubsampling needs
        # at least MIN_ONNX_MEL_FRAMES to produce non-zero output.
        T = audio_signal.shape[2]
        if T < self.MIN_ONNX_MEL_FRAMES:
            pad_width = self.MIN_ONNX_MEL_FRAMES - T
            audio_signal = np.pad(
                audio_signal,
                ((0, 0), (0, 0), (0, pad_width)),
                mode="constant",
                constant_values=0.0,
            )
            # length stays the same — encoder uses it for masking

        # Run encoder
        enc_outputs, encoded_lengths, cache_ch_next, cache_t_next, cache_ch_len_next = (
            self.encoder_sess.run(
                None,
                {
                    "audio_signal": audio_signal,
                    "length": length,
                    "cache_last_channel": self.cache_last_channel,
                    "cache_last_time": self.cache_last_time,
                    "cache_last_channel_len": self.cache_last_channel_len,
                },
            )
        )

        # Update caches
        self.cache_last_channel = cache_ch_next
        self.cache_last_time = cache_t_next
        self.cache_last_channel_len = cache_ch_len_next

        # enc_outputs shape: [B, D, T'] — already trimmed by ONNX encoder

        # Skip if no encoder output
        if enc_outputs.shape[2] == 0:
            text = self.tokenizer.ids_to_text(self.predicted_tokens)
            return text

        # Greedy RNN-T decoding
        self.predicted_tokens, self.lstm_states = greedy_rnnt_decode(
            encoder_out=enc_outputs,
            decoder_joint_sess=self.decoder_joint_sess,
            prev_tokens=self.predicted_tokens,
            lstm_states=self.lstm_states,
            blank_id=self.blank_id,
        )

        # Decode tokens to text
        text = self.tokenizer.ids_to_text(self.predicted_tokens)
        return text
