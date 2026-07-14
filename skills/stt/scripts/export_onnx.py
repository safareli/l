#!/usr/bin/env python3
"""
Export NeMo streaming FastConformer models to ONNX format.

Produces per language:
  - encoder-model.onnx (~435 MB) — the FastConformer encoder with streaming cache I/O
  - decoder_joint-model.onnx (~20 MB) — the RNN-T decoder + joint network
  - metadata.json — vocab, blank_id, cache shapes, streaming config, preprocessor config
  - mel_filterbank.npy — precomputed mel filterbank matrix [n_mels, n_fft//2+1]

Output directory: ~/.cache/stt-streaming-onnx/<lang>/

The metadata.json and mel_filterbank.npy enable the ONNX runtime mode to run
WITHOUT loading PyTorch or NeMo at all — just numpy + onnxruntime.

Usage:
    cd skills/stt
    uv run --python python3.11 python scripts/export_onnx.py             # export English
    # uv run --python python3.11 python scripts/export_onnx.py --langs ka  # Georgian (TODO)
    # uv run --python python3.11 python scripts/export_onnx.py --langs en,ka  # both
"""

import argparse
import json
import os
import sys
import time
import warnings
import logging

# Suppress NeMo's verbose logging
os.environ["NEMO_TESTING"] = "1"
logging.disable(logging.WARNING)
warnings.filterwarnings("ignore")

import torch
import numpy as np

STREAMING_MODELS = {
    "en": "nvidia/stt_en_fastconformer_hybrid_large_streaming_multi",
    # "ka": "nvidia/stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc",  # TODO
}

DEFAULT_ATT_CONTEXT_SIZE_EN = [70, 1]
CACHE_BASE_DIR = os.path.expanduser("~/.cache/stt-streaming-onnx")

# Metadata version — bump when adding new required fields.
# v2 added preprocessor config + mel filterbank (no torch/nemo needed at runtime).
METADATA_VERSION = 2


def patch_torch_onnx_export():
    """
    Force legacy ONNX export mode.

    PyTorch 2.10+ changed torch.onnx.export to use the dynamo-based exporter
    by default, which fails on NeMo models due to data-dependent shape guards
    in rel_shift. Force legacy mode with dynamo=False.
    """
    _orig_export = torch.onnx.export

    def patched_export(*args, **kwargs):
        kwargs["dynamo"] = False
        return _orig_export(*args, **kwargs)

    torch.onnx.export = patched_export
    print("  Patched torch.onnx.export to use legacy mode (dynamo=False)", file=sys.stderr)


def _extract_preprocessor_config(model) -> dict:
    """
    Extract preprocessor configuration from a NeMo model.

    Returns a dict with all parameters needed to reproduce the mel spectrogram
    computation without NeMo at runtime.

    Note: The streaming buffer uses dither=0.0 and pad_to=0 (overriding the
    model's defaults). These streaming overrides are applied here.
    """
    pp = model.preprocessor.featurizer

    config = {
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
        "pad_value": float(pp.pad_value),
        # Streaming overrides (CacheAwareStreamingAudioBuffer.extract_preprocessor):
        "dither": 0.0,   # forced to 0 for streaming
        "pad_to": 0,     # forced to 0 for streaming
        # Original normalize setting (kept as-is when online_normalization=None):
        "normalize": str(model._cfg.preprocessor.get("normalize", "NA")),
    }

    return config


def _extract_mel_filterbank(model) -> np.ndarray:
    """
    Extract the mel filterbank matrix from a NeMo model.

    Returns numpy array of shape [n_mels, n_fft//2+1], float32.
    """
    fb = model.preprocessor.featurizer.fb  # [1, n_mels, n_fft//2+1]
    return fb.squeeze(0).cpu().numpy().astype(np.float32)


def _extract_sampling_frames(model) -> list[int] | None:
    """Extract sampling_frames from the encoder's pre_encode module."""
    if hasattr(model.encoder, "pre_encode") and hasattr(model.encoder.pre_encode, "get_sampling_frames"):
        sf = model.encoder.pre_encode.get_sampling_frames()
        if isinstance(sf, (list, tuple)):
            return [int(x) for x in sf]
        return [int(sf)]
    return None


def export_lang(lang: str, att_context_size_en=None):
    """Export ONNX models for a single language."""
    import nemo.collections.asr as nemo_asr

    model_name = STREAMING_MODELS.get(lang)
    if not model_name:
        print(f"Unknown language '{lang}'. Known: {list(STREAMING_MODELS.keys())}", file=sys.stderr)
        return False

    output_dir = os.path.join(CACHE_BASE_DIR, lang)
    os.makedirs(output_dir, exist_ok=True)

    encoder_path = os.path.join(output_dir, "encoder-model.onnx")
    decoder_path = os.path.join(output_dir, "decoder_joint-model.onnx")

    if os.path.exists(encoder_path) and os.path.exists(decoder_path):
        print(f"ONNX models already exist for {lang} in {output_dir}", file=sys.stderr)
        print(f"  Delete them to re-export.", file=sys.stderr)
        return True

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Exporting {lang}: {model_name}", file=sys.stderr)
    print(f"Output: {output_dir}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    # Load model
    print(f"  Loading PyTorch model...", file=sys.stderr)
    t0 = time.monotonic()
    model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(model_name)

    # Configure attention context for English multi-latency model
    if lang == "en" and att_context_size_en:
        if hasattr(model.encoder, "set_default_att_context_size"):
            model.encoder.set_default_att_context_size(att_context_size_en)

    # Ensure streaming params are initialized
    if not hasattr(model.encoder, "streaming_cfg") or model.encoder.streaming_cfg is None:
        model.encoder.setup_streaming_params()

    model.eval()
    elapsed = time.monotonic() - t0
    print(f"  Model loaded in {elapsed:.1f}s", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Extract metadata (before export, while model is fully loaded)
    # -----------------------------------------------------------------------

    cfg = model.encoder.streaming_cfg
    tokenizer = model.tokenizer

    # Get vocab
    vocab = []
    for i in range(tokenizer.vocab_size):
        try:
            vocab.append(tokenizer.ids_to_tokens([i])[0])
        except Exception:
            vocab.append(f"<unk_{i}>")

    # Get blank_id from the decoding config
    blank_id = model.joint.num_classes_with_blank - 1  # typically 1024

    # Cache shapes from initial state
    cache_ch, cache_t, cache_ch_len = model.encoder.get_initial_cache_state(batch_size=1)
    num_layers = cache_ch.shape[0]
    cache_ch_T = cache_ch.shape[2]
    cache_ch_D = cache_ch.shape[3]
    cache_t_D = cache_t.shape[2]
    cache_t_T = cache_t.shape[3]

    # Extract preprocessor config and mel filterbank
    preprocessor_config = _extract_preprocessor_config(model)
    mel_filterbank = _extract_mel_filterbank(model)
    sampling_frames = _extract_sampling_frames(model)

    print(f"  Preprocessor config: {preprocessor_config}", file=sys.stderr)
    print(f"  Mel filterbank shape: {mel_filterbank.shape}", file=sys.stderr)
    print(f"  Sampling frames: {sampling_frames}", file=sys.stderr)

    def _to_json_val(v):
        """Convert streaming config values (may be int or list) to JSON-safe types."""
        if isinstance(v, list):
            return [int(x) for x in v]
        return int(v)

    metadata = {
        "version": METADATA_VERSION,
        "lang": lang,
        "model_name": model_name,
        "att_context_size": (
            [int(x) for x in att_context_size_en] if (lang == "en" and att_context_size_en) else None
        ),
        "blank_id": int(blank_id),
        "vocab_size": len(vocab),
        "vocab": vocab,
        "num_layers": int(num_layers),
        "cache_last_channel_shape": [1, int(num_layers), int(cache_ch_T), int(cache_ch_D)],
        "cache_last_time_shape": [1, int(num_layers), int(cache_t_D), int(cache_t_T)],
        "streaming_cfg": {
            "chunk_size": _to_json_val(cfg.chunk_size),
            "shift_size": _to_json_val(cfg.shift_size),
            "pre_encode_cache_size": _to_json_val(cfg.pre_encode_cache_size),
            "drop_extra_pre_encoded": _to_json_val(cfg.drop_extra_pre_encoded),
        },
        "sample_rate": 16000,
        "n_mels": 80,
        "preprocessor": preprocessor_config,
        "sampling_frames": sampling_frames,
    }

    # Get decoder LSTM hidden size from the RNNTDecoder
    pred_net = model.decoder
    metadata["lstm_hidden_size"] = int(pred_net.pred_hidden)
    metadata["lstm_num_layers"] = int(pred_net.pred_rnn_layers)

    # -----------------------------------------------------------------------
    # Export ONNX
    # -----------------------------------------------------------------------

    # Keep native drop_extra_pre_encoded from model config.
    # The ONNX graph bakes this slice in. For very short step-0 chunks,
    # runtime may need to skip the first step (handled in OnnxSession).
    print(f"  drop_extra_pre_encoded={cfg.drop_extra_pre_encoded} (native, baked into ONNX)", file=sys.stderr)

    print(f"  Configuring export with cache_support=True...", file=sys.stderr)
    model.set_export_config({"cache_support": True})

    # Patch torch.onnx.export for compatibility
    patch_torch_onnx_export()

    print(f"  Exporting to ONNX (this may take a few minutes)...", file=sys.stderr)
    t0 = time.monotonic()
    model.export(os.path.join(output_dir, "model.onnx"))
    elapsed = time.monotonic() - t0
    print(f"  Export completed in {elapsed:.1f}s", file=sys.stderr)

    # NeMo export creates encoder-model.onnx and decoder_joint-model.onnx
    # and also a model.onnx that we don't need. Clean up.
    model_onnx = os.path.join(output_dir, "model.onnx")
    if os.path.exists(model_onnx):
        os.remove(model_onnx)

    # Verify output files exist
    for name, path in [("encoder", encoder_path), ("decoder_joint", decoder_path)]:
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            print(f"  ✓ {name}: {path} ({size_mb:.1f} MB)", file=sys.stderr)
        else:
            print(f"  ✗ {name}: {path} NOT FOUND", file=sys.stderr)
            return False

    # -----------------------------------------------------------------------
    # Save metadata + mel filterbank
    # -----------------------------------------------------------------------

    metadata_path = os.path.join(output_dir, "metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"  ✓ metadata: {metadata_path}", file=sys.stderr)

    filterbank_path = os.path.join(output_dir, "mel_filterbank.npy")
    np.save(filterbank_path, mel_filterbank)
    fb_size_kb = os.path.getsize(filterbank_path) / 1024
    print(f"  ✓ filterbank: {filterbank_path} ({fb_size_kb:.1f} KB)", file=sys.stderr)

    print(f"\n  Export successful for {lang}!", file=sys.stderr)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Export NeMo streaming FastConformer models to ONNX format",
    )
    parser.add_argument(
        "--langs",
        default="en",
        help="Comma-separated languages to export (default: en). Options: en, ka",
    )
    parser.add_argument(
        "--att-context-size",
        default="70,1",
        help="Attention context size for EN model as 'left,right' (default: 70,1 = 80ms look-ahead)",
    )
    args = parser.parse_args()

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    att_ctx = [int(x) for x in args.att_context_size.split(",")]

    print(f"ONNX Export for stt-streaming", file=sys.stderr)
    print(f"  Languages: {langs}", file=sys.stderr)
    print(f"  Output dir: {CACHE_BASE_DIR}", file=sys.stderr)

    success = True
    for lang in langs:
        if not export_lang(lang, att_context_size_en=att_ctx):
            success = False

    if success:
        print(f"\nAll exports completed successfully.", file=sys.stderr)
    else:
        print(f"\nSome exports failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
