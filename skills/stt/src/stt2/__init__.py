"""
stt2: Fast offline English speech-to-text using ONNX Runtime.

Uses the ONNX-exported FastConformer encoder + RNN-T decoding.
No PyTorch or NeMo at runtime — just numpy + onnxruntime.

Pipeline: audio → mel (numpy) → encoder (ONNX) → RNN-T decode (ONNX) → text

The _pc model provides punctuation and capitalization.
"""

import argparse
import json
import os
import sys
import time
import wave

import numpy as np
import onnxruntime as ort

# Reuse from stt_streaming
from stt_streaming.mel_preprocessor import MelPreprocessor
from stt_streaming.onnx_session import greedy_rnnt_decode

CACHE_DIR = os.path.expanduser("~/.cache/stt-onnx/en")


def _log(msg: str):
    print(msg, file=sys.stderr)


def _decode_tokens(token_ids: list[int], vocab: list[str]) -> str:
    """Decode token IDs via SentencePiece vocab. ▁ → space."""
    if not token_ids:
        return ""
    pieces = [vocab[tid] if tid < len(vocab) else f"<unk_{tid}>" for tid in token_ids]
    return "".join(pieces).replace("\u2581", " ").strip()


class Transcriber:
    """
    ONNX-based offline English speech-to-text.

    Uses RNN-T decoding (encoder + decoder_joint ONNX sessions).
    1.6% WER on benchmark audio, 0.03x RT on CPU.
    """

    def __init__(self, num_threads: int = 4):
        if not os.path.isdir(CACHE_DIR):
            _log(f"ONNX models not found at {CACHE_DIR}")
            _log("Run: uv run --python python3.11 python scripts/export_onnx_offline.py")
            raise FileNotFoundError(CACHE_DIR)

        t0 = time.monotonic()

        # Load metadata
        with open(os.path.join(CACHE_DIR, "metadata.json")) as f:
            self.metadata = json.load(f)

        self.vocab = self.metadata["vocab"]
        self.blank_id = self.metadata.get("rnnt_blank_id", self.metadata["blank_id"])

        # Load mel preprocessor
        filterbank = np.load(os.path.join(CACHE_DIR, "mel_filterbank.npy"))
        self.preprocessor = MelPreprocessor(
            config=self.metadata["preprocessor"],
            filterbank=filterbank,
        )

        # ONNX session options
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = num_threads
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # Load encoder
        self.encoder = ort.InferenceSession(
            os.path.join(CACHE_DIR, "encoder.onnx"),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        # RNN-T decoder+joint
        self.decoder_joint = ort.InferenceSession(
            os.path.join(CACHE_DIR, "decoder_joint.onnx"),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        lstm_hidden = self.metadata.get("lstm_hidden_size", 640)
        lstm_layers = self.metadata.get("lstm_num_layers", 1)
        self._lstm_init = (
            np.zeros([lstm_layers, 1, lstm_hidden], dtype=np.float32),
            np.zeros([lstm_layers, 1, lstm_hidden], dtype=np.float32),
        )

        elapsed = time.monotonic() - t0
        _log(f"stt2: loaded in {elapsed:.1f}s (ONNX, numpy-only)")

    def transcribe_pcm(self, pcm_int16: bytes) -> dict:
        """
        Transcribe raw PCM audio (16-bit signed LE, 16kHz, mono).

        Returns dict with keys: text, duration_s, elapsed_s, rtf
        """
        audio = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        return self._transcribe_audio(audio)

    def transcribe_wav(self, wav_path: str) -> str:
        """Transcribe a 16kHz mono WAV file. Returns text with punctuation."""
        with wave.open(wav_path, "rb") as wf:
            assert wf.getnchannels() == 1, f"Expected mono, got {wf.getnchannels()} channels"
            assert wf.getframerate() == 16000, f"Expected 16kHz, got {wf.getframerate()}Hz"
            assert wf.getsampwidth() == 2, f"Expected 16-bit, got {wf.getsampwidth()*8}-bit"
            pcm = wf.readframes(wf.getnframes())

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self._transcribe_audio(audio)["text"]

    def _transcribe_audio(self, audio: np.ndarray) -> dict:
        """Core transcription on float32 audio array. Returns result dict."""
        audio_len = len(audio)
        duration = audio_len / 16000

        _log(f"Audio: {duration:.1f}s ({audio_len} samples)")

        t0 = time.monotonic()

        # Mel spectrogram
        mel, mel_len = self.preprocessor(audio, audio_len)
        t_mel = time.monotonic() - t0

        # Encoder
        t1 = time.monotonic()
        enc_out, enc_len = self.encoder.run(
            None,
            {
                "audio_signal": mel,
                "length": np.array([mel_len], dtype=np.int64),
            },
        )
        t_enc = time.monotonic() - t1

        # RNN-T decode
        t2 = time.monotonic()
        tokens, _ = greedy_rnnt_decode(
            encoder_out=enc_out,
            decoder_joint_sess=self.decoder_joint,
            prev_tokens=[],
            lstm_states=(self._lstm_init[0].copy(), self._lstm_init[1].copy()),
            blank_id=self.blank_id,
        )
        text = _decode_tokens(tokens, self.vocab)
        t_dec = time.monotonic() - t2

        total = time.monotonic() - t0
        _log(f"Transcribed in {total:.2f}s ({total/duration:.2f}x RT) "
             f"[mel={t_mel:.2f}s enc={t_enc:.2f}s rnnt={t_dec:.2f}s]")

        return {
            "text": text,
            "duration_s": round(duration, 2),
            "elapsed_s": round(total, 3),
            "rtf": round(total / duration, 4) if duration > 0 else 0,
        }


def main():
    parser = argparse.ArgumentParser(
        prog="stt2",
        description="Fast offline English speech-to-text (ONNX)",
    )
    parser.add_argument("wav_path", help="Path to 16kHz mono WAV file")
    args = parser.parse_args()

    transcriber = Transcriber()
    text = transcriber.transcribe_wav(args.wav_path)
    print(text)
