#!/usr/bin/env python3
"""
Export NeMo streaming FastConformer models to ONNX format.

Produces two ONNX files per language:
  - encoder-model.onnx (~435 MB) — the FastConformer encoder with streaming cache I/O
  - decoder_joint-model.onnx (~20 MB) — the RNN-T decoder + joint network

Also saves metadata.json with vocab, blank_id, cache shapes, and streaming config.

Output directory: ~/.cache/stt-streaming-onnx/<lang>/

Usage:
    cd skills/stt
    uv run --python python3.11 python scripts/export_onnx.py             # export English
    uv run --python python3.11 python scripts/export_onnx.py --langs ka  # export Georgian
    uv run --python python3.11 python scripts/export_onnx.py --langs en,ka  # export both
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
    "ka": "nvidia/stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc",
}

DEFAULT_ATT_CONTEXT_SIZE_EN = [70, 1]
CACHE_BASE_DIR = os.path.expanduser("~/.cache/stt-streaming-onnx")


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

    # Extract metadata before export
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
    # These are layers-first: [num_layers, B, T, D] and [num_layers, B, D, T_conv]
    # ONNX export uses batch-first: [B, num_layers, T, D]
    num_layers = cache_ch.shape[0]
    cache_ch_T = cache_ch.shape[2]  # e.g. 70
    cache_ch_D = cache_ch.shape[3]  # e.g. 512
    cache_t_D = cache_t.shape[2]    # e.g. 512
    cache_t_T = cache_t.shape[3]    # e.g. 8

    def _to_json_val(v):
        """Convert streaming config values (may be int or list) to JSON-safe types."""
        if isinstance(v, list):
            return [int(x) for x in v]
        return int(v)

    metadata = {
        "lang": lang,
        "model_name": model_name,
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
    }

    # Get decoder LSTM hidden size from the RNNTDecoder
    pred_net = model.decoder
    metadata["lstm_hidden_size"] = int(pred_net.pred_hidden)       # typically 640
    metadata["lstm_num_layers"] = int(pred_net.pred_rnn_layers)    # typically 1

    # Export ONNX
    # Set drop_extra_pre_encoded=0 before export. This value gets baked into the
    # ONNX graph. If left at the default (2), the first chunk (9 mel frames) would
    # subsample to ~2 frames, then drop 2, resulting in 0 frames — which crashes
    # the Conv node. With drop=0, all chunks produce valid output. The output is
    # still trimmed by valid_out_len (via streaming_post_process), so the extra
    # pre-encoded frames only add a slight overlap in the conformer context.
    model.encoder.streaming_cfg.drop_extra_pre_encoded = 0
    print(f"  Set drop_extra_pre_encoded=0 for ONNX compatibility", file=sys.stderr)

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

    # Save metadata
    metadata_path = os.path.join(output_dir, "metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"  ✓ metadata: {metadata_path}", file=sys.stderr)

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
        help="Attention context size for EN model as 'left,right' (default: 70,1)",
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
