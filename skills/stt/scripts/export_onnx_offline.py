#!/usr/bin/env python3
"""
Export the offline NeMo FastConformer _pc model (English) to ONNX.

Produces:
  - encoder.onnx — full-attention encoder (no streaming caches)
  - ctc_weight.npy, ctc_bias.npy — CTC head (Conv1d → matmul at runtime)
  - metadata.json — vocab, preprocessor config
  - mel_filterbank.npy — precomputed mel filterbank matrix

Output: ~/.cache/stt-onnx/en/

Usage:
    cd skills/stt
    uv run --python python3.11 python scripts/export_onnx_offline.py
"""

import json
import os
import sys
import time
import warnings
import logging

os.environ["NEMO_TESTING"] = "1"
logging.disable(logging.WARNING)
warnings.filterwarnings("ignore")

import torch
import numpy as np

MODEL_NAME = "nvidia/stt_en_fastconformer_hybrid_large_pc"
CACHE_DIR = os.path.expanduser("~/.cache/stt-onnx/en")
METADATA_VERSION = 1


def patch_torch_onnx_export():
    """Force legacy ONNX export (dynamo=False) for PyTorch 2.10+."""
    _orig = torch.onnx.export
    def patched(*args, **kwargs):
        kwargs["dynamo"] = False
        return _orig(*args, **kwargs)
    torch.onnx.export = patched
    print("  Patched torch.onnx.export (dynamo=False)", file=sys.stderr)


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    encoder_path = os.path.join(CACHE_DIR, "encoder.onnx")

    if os.path.exists(encoder_path):
        print(f"ONNX model already exists: {encoder_path}", file=sys.stderr)
        print(f"  Delete {CACHE_DIR} to re-export.", file=sys.stderr)
        return

    print(f"Exporting offline model: {MODEL_NAME}", file=sys.stderr)
    print(f"Output: {CACHE_DIR}", file=sys.stderr)

    # Load model
    print("  Loading PyTorch model...", file=sys.stderr)
    t0 = time.monotonic()
    import nemo.collections.asr as nemo_asr
    model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(MODEL_NAME)
    model.eval()
    print(f"  Loaded in {time.monotonic() - t0:.1f}s", file=sys.stderr)

    # ---- Extract metadata ----
    tokenizer = model.tokenizer
    vocab = []
    for i in range(tokenizer.vocab_size):
        try:
            vocab.append(tokenizer.ids_to_tokens([i])[0])
        except Exception:
            vocab.append(f"<unk_{i}>")

    blank_id = tokenizer.vocab_size  # 1024 (CTC blank = vocab_size)

    # Preprocessor config
    pp = model.preprocessor.featurizer
    preprocessor_config = {
        "sample_rate": int(model._cfg.preprocessor.sample_rate),
        "n_fft": int(pp.n_fft),
        "hop_length": int(pp.hop_length),
        "win_length": int(pp.win_length),
        "n_mels": int(pp.nfilt),
        "preemph": float(pp.preemph) if pp.preemph is not None else None,
        "mag_power": float(pp.mag_power),
        "log": bool(pp.log),
        "log_zero_guard_type": str(pp.log_zero_guard_type),
        "log_zero_guard_value": float(pp.log_zero_guard_value),
        "exact_pad": bool(pp.exact_pad),
        "normalize": str(model._cfg.preprocessor.get("normalize", "NA")),
    }

    # Mel filterbank
    mel_filterbank = model.preprocessor.featurizer.fb.squeeze(0).cpu().numpy().astype(np.float32)

    print(f"  Preprocessor: {preprocessor_config}", file=sys.stderr)
    print(f"  Filterbank: {mel_filterbank.shape}", file=sys.stderr)
    print(f"  Vocab: {len(vocab)} tokens, blank_id={blank_id}", file=sys.stderr)

    # ---- Export encoder to ONNX ----
    # The encoder forward: (audio_signal [B, n_mels, T], length [B]) → (encoded [B, D, T'], encoded_len [B])
    # Full attention [-1,-1], no caches.
    #
    # NeMo's typed method wrapper requires kwargs, but torch.onnx.export
    # passes positional args. Wrap encoder in a simple module.

    patch_torch_onnx_export()

    class EncoderWrapper(torch.nn.Module):
        def __init__(self, encoder):
            super().__init__()
            self.encoder = encoder

        def forward(self, audio_signal, length):
            return self.encoder(audio_signal=audio_signal, length=length)

    wrapper = EncoderWrapper(model.encoder)
    wrapper.eval()

    # Create dummy input
    dummy_mel = torch.randn(1, 80, 500, dtype=torch.float32)
    dummy_len = torch.tensor([500], dtype=torch.long)

    print("  Exporting encoder to ONNX...", file=sys.stderr)
    t0 = time.monotonic()

    torch.onnx.export(
        wrapper,
        (dummy_mel, dummy_len),
        encoder_path,
        input_names=["audio_signal", "length"],
        output_names=["encoded", "encoded_len"],
        dynamic_axes={
            "audio_signal": {0: "batch", 2: "time"},
            "length": {0: "batch"},
            "encoded": {0: "batch", 2: "time_enc"},
            "encoded_len": {0: "batch"},
        },
        opset_version=17,
    )

    elapsed = time.monotonic() - t0
    size_mb = os.path.getsize(encoder_path) / (1024 * 1024)
    print(f"  ✓ encoder.onnx ({size_mb:.1f} MB) in {elapsed:.1f}s", file=sys.stderr)

    # ---- Verify ONNX output matches PyTorch ----
    print("  Verifying ONNX output...", file=sys.stderr)
    import onnxruntime as ort
    sess = ort.InferenceSession(encoder_path, providers=["CPUExecutionProvider"])

    with torch.no_grad():
        pt_enc, pt_len = model.encoder(audio_signal=dummy_mel, length=dummy_len)
        pt_enc_np = pt_enc.cpu().numpy()

    onnx_enc, onnx_len = sess.run(None, {
        "audio_signal": dummy_mel.numpy(),
        "length": dummy_len.numpy(),
    })

    max_diff = np.max(np.abs(pt_enc_np - onnx_enc))
    print(f"  Max encoder diff: {max_diff:.2e} (should be < 1e-4)", file=sys.stderr)

    # ---- Export RNN-T decoder+joint to ONNX ----
    # NeMo's built-in export handles decoder_joint. We use model.export()
    # in a temp dir, then move the decoder_joint ONNX.
    import tempfile
    decoder_joint_path = os.path.join(CACHE_DIR, "decoder_joint.onnx")
    if not os.path.exists(decoder_joint_path):
        print("  Exporting RNN-T decoder+joint...", file=sys.stderr)
        t0 = time.monotonic()

        # Use NeMo's export which handles the decoder_joint correctly
        with tempfile.TemporaryDirectory() as tmpdir:
            model.set_export_config({"cache_support": False})
            model.export(os.path.join(tmpdir, "model.onnx"))
            # NeMo creates: encoder-model.onnx, decoder_joint-model.onnx
            tmp_dj = os.path.join(tmpdir, "decoder_joint-model.onnx")
            if os.path.exists(tmp_dj):
                import shutil
                shutil.move(tmp_dj, decoder_joint_path)
                size_mb = os.path.getsize(decoder_joint_path) / (1024 * 1024)
                print(f"  ✓ decoder_joint.onnx ({size_mb:.1f} MB) in {time.monotonic() - t0:.1f}s", file=sys.stderr)
            else:
                print(f"  ✗ decoder_joint-model.onnx not found in export output", file=sys.stderr)

    # Get RNN-T decoder metadata
    pred_net = model.decoder
    lstm_hidden_size = int(pred_net.pred_hidden)
    lstm_num_layers = int(pred_net.pred_rnn_layers)
    rnnt_blank_id = model.joint.num_classes_with_blank - 1  # typically 1024

    print(f"  RNN-T decoder: lstm_hidden={lstm_hidden_size}, layers={lstm_num_layers}, blank={rnnt_blank_id}", file=sys.stderr)

    # ---- Save everything ----
    metadata = {
        "version": METADATA_VERSION,
        "model_name": MODEL_NAME,
        "lang": "en",
        "blank_id": int(blank_id),
        "vocab_size": len(vocab),
        "vocab": vocab,
        "n_mels": 80,
        "preprocessor": preprocessor_config,
        "lstm_hidden_size": lstm_hidden_size,
        "lstm_num_layers": lstm_num_layers,
        "rnnt_blank_id": int(rnnt_blank_id),
    }

    with open(os.path.join(CACHE_DIR, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"  ✓ metadata.json", file=sys.stderr)

    np.save(os.path.join(CACHE_DIR, "mel_filterbank.npy"), mel_filterbank)
    print(f"  ✓ mel_filterbank.npy", file=sys.stderr)

    print(f"\nExport complete: {CACHE_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
