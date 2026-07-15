# ONNX Runtime Mode for stt-streaming

## Overview

The `--onnx` flag replaces PyTorch eager inference with ONNX Runtime for the encoder and decoder, giving a **~6.8x speedup** on the encoder forward pass. Preprocessing was also rewritten in pure numpy (no PyTorch/NeMo at runtime), cutting startup from ~12s to ~1.4s and memory from ~1.8GB to ~500MB.

| | PyTorch | ONNX Runtime |
|---|---|---|
| Encoder per chunk | 165ms | ~23ms |
| Processing speed | 1.34x real-time (too slow) | 0.17x real-time (6x headroom) |
| Pipeline latency | N/A (couldn't keep up) | ~73ms typical, ~129ms worst |
| Startup | ~4s | ~1.4s |
| Memory | ~460MB | ~500MB |
| NeMo/PyTorch needed | Yes | No |

Currently English only. Georgian is commented out in the export script — needs investigation.

## Benchmarks

**Test audio:** 81.2s English speech (`bench_data/english_16k.wav`)
**Hardware:** aarch64 CPU, 4 threads
**MIN_PREPROCESS_MS:** 100

### Throughput (total processing time, no real-time pacing)

| Client chunk size | feed_audio calls | Encoder chunks | Per-call avg | Per-call p95 | Total time | Speed |
|---|---|---|---|---|---|---|
| 100ms | 812 | 562 | 16.8ms | 27.7ms | 13.8s | **0.17x RT** |
| 200ms | 406 | 537 | 79.4ms | 112.3ms | 32.4s | **0.40x RT** |
| 500ms | 163 | 521 | 133.4ms | 158.0ms | 21.9s | **0.27x RT** |

The 100ms client chunk is optimal — each `feed_audio` call processes fewer encoder chunks, giving more consistent latency and better throughput.

### Per-component breakdown (100ms client chunks)

| Component | avg | p50 | p95 | max |
|---|---|---|---|---|
| Mel preprocessing | 1.7ms | 1.8ms | 3.7ms | 8.3ms |
| ONNX encoder | 21.8ms | 20.7ms | 28.9ms | 67.7ms |
| RNN-T decode | 1.5ms | 1.2ms | 3.5ms | 8.3ms |
| **Total per chunk** | **25.0ms** | **23.8ms** | **34.1ms** | **68.5ms** |

The encoder is 87% of inference time. Mel preprocessing and RNN-T decoding are negligible.

### Pipeline latency (audio arrives → partial text sent)

| Stage | Time |
|---|---|
| PCM buffer accumulation | ≤100ms (MIN_PREPROCESS_MS) |
| Processing (mel + encoder + decode) | ~23ms median |
| **Typical end-to-end** | **~73ms** |
| **Worst case** | **~129ms** |

The PCM buffer (100ms) is now the dominant latency contributor, not inference. Reducing it further would reduce latency but increases mel-spectrogram boundary artifacts.

### Finalize overhead

After all audio is sent, `finalize()` pads 560ms of silence and flushes remaining chunks:
- **~140ms** (5 extra encoder chunks)
- Negligible compared to audio duration

## Usage

### 1. Export ONNX models (one-time)

```bash
cd ~/.config/home-manager/skills/stt
uv run --python python3.11 python scripts/export_onnx.py
```

Exports to `~/.cache/stt-streaming-onnx/en/`:
- `encoder-model.onnx` (435.5 MB)
- `decoder_joint-model.onnx` (20.3 MB)
- `metadata.json` (vocab, blank_id, cache shapes, streaming config, preprocessor config)
- `mel_filterbank.npy` (mel filterbank matrix)

### 2. Run the server

```bash
# ONNX mode (default in systemd service)
stt-streaming --port 6771 --onnx

# PyTorch mode (fallback)
stt-streaming --port 6771
```

## Architecture

```
scripts/export_onnx.py              # One-time export: PyTorch → ONNX + metadata
src/stt_streaming/
  __init__.py                        # --onnx flag, routes to OnnxSession or Session
  onnx_session.py                    # OnnxSession + greedy_rnnt_decode() + load_onnx_sessions()
  mel_preprocessor.py                # Pure numpy mel spectrogram extraction
  streaming_buffer.py                # Pure numpy streaming chunk management

~/.cache/stt-streaming-onnx/en/
  encoder-model.onnx                 # 435 MB — FastConformer encoder with streaming cache I/O
  decoder_joint-model.onnx           # 20 MB — RNN-T decoder + joint network
  metadata.json                      # vocab, blank_id, cache shapes, preprocessor config
  mel_filterbank.npy                 # mel filterbank matrix (extracted from NeMo at export time)
```

### What runs through ONNX Runtime

- **Encoder** (435 MB) — 87% of inference time, 6.8x faster than PyTorch
- **Decoder + Joint** (20 MB) — greedy RNN-T decoding loop, <2ms per chunk

### What runs in pure numpy

- **Mel preprocessing** — STFT + mel filterbank, ~1.8ms per chunk
- **Streaming buffer** — chunk management with pre-encode cache
- **Token decoding** — SentencePiece vocab lookup

No PyTorch or NeMo loaded at runtime.

## How it works

`OnnxSession` is a drop-in replacement for `Session` with the same `feed_audio()` / `finalize()` API:

1. **PCM accumulation** — bytearray, ≥100ms threshold before preprocessing
2. **Mel extraction** — pure numpy `MelPreprocessor` (STFT + mel filterbank)
3. **Chunk management** — `StreamingBuffer` handles pre-encode cache and chunk sizing
4. **Encoder** — ONNX Runtime inference
5. **Decoding** — custom `greedy_rnnt_decode()` loop via ONNX decoder+joint
6. **Text** — SentencePiece vocab lookup from saved vocabulary

### Greedy RNN-T decoding

For each encoder output time step:
1. Feed last predicted token + LSTM states into the ONNX decoder+joint model
2. Argmax over logits → if blank, advance to next time step; if token, append and repeat
3. Safety limit of 10 symbols per time step to prevent infinite loops

### Cache format

ONNX encoder uses **batch-first** cache tensors:

| Cache | Shape | Description |
|-------|-------|-------------|
| `cache_last_channel` | `[B, 17, 70, 512]` | Attention K/V cache (17 layers) |
| `cache_last_time` | `[B, 17, 512, 8]` | Convolution cache |
| `cache_last_channel_len` | `[B]` | Filled cache entries |
| LSTM hidden/cell | `[1, B, 640]` | RNN-T decoder state |

### Output trimming

The ONNX encoder bakes in `streaming_post_process(keep_all_outputs=False)`, which trims output to `valid_out_len=1` frame per chunk (for 0ms look-ahead). For the final chunk, the 560ms silence padding in `finalize()` pushes trailing content through.

## Export details

### `scripts/export_onnx.py`

1. Loads the NeMo streaming model (only needed at export time)
2. Configures attention context (`[70, 0]` = 0ms look-ahead)
3. Keeps native `drop_extra_pre_encoded=2` (baked into ONNX graph; step 0 skipped at runtime)
4. Patches `torch.onnx.export` with `dynamo=False` (PyTorch 2.10+ compat)
5. Calls `model.export()` with `cache_support=True`
6. Extracts mel filterbank matrix and preprocessor config from NeMo model
7. Saves metadata JSON + filterbank `.npy` alongside ONNX files

### `drop_extra_pre_encoded` and step 0

The native `drop_extra_pre_encoded=2` is **baked into the ONNX graph**. This is correct for all chunks except step 0: the first chunk has only `chunk_size[0]=1` mel frame, which after Conv subsampling + drop 2 would crash.

**Fix:** `OnnxSession._process_chunks()` simply **skips step 0** — it consumes the chunk from the streaming buffer (so the buffer state advances correctly) but doesn't call the encoder. This is safe because:
- PyTorch step 0 also uses `drop=0` (different from subsequent steps) and produces no output tokens
- The cache update from processing 1 mel frame is negligible (caches stay near-zero)

Previous approach used `drop_extra_pre_encoded=0` baked into the ONNX graph, which degraded WER from 1.6% to 3.1% — the 2 extra pre-encoded frames per step caused floating-point divergence that accumulated over hundreds of encoder steps.

### Minimum mel frame padding

The ONNX ConvSubsampling can't handle inputs shorter than 12 mel frames. `OnnxSession` pads short chunks to `MIN_ONNX_MEL_FRAMES=12` with zeros. In practice this is a safety net — with step 0 skipped, all subsequent chunks are 17 mel frames (8 new + 9 pre-encode cache).

## Accuracy vs look-ahead settings

The English streaming model (`stt_en_fastconformer_hybrid_large_streaming_multi`) supports multiple look-ahead modes via `--att-context-size`. Accuracy measured on 81.2s test audio, normalized WER (no punctuation, lowercase).

| Look-ahead | att_context_size | WER | Errors | Chunk size | ONNX speed | Status |
|---|---|---|---|---|---|---|
| **0ms (default)** | `70,0` | **1.6%** | 3/192 | 80ms (8 mel) | 0.28x RT | ✅ Matches offline accuracy |
| 80ms | `70,1` | 4.7% | 9/192 | 160ms (16 mel) | 0.16x RT | ✅ Alternative |
| 480ms | `70,6` | 86.5% | 166/192 | 560ms (56 mel) | — | ❌ Broken (hallucinations) |
| 1040ms | `70,13` | 94.3% | 181/192 | 1120ms (112 mel) | — | ❌ Broken (hallucinations) |
| **Offline** (ref) | N/A | **1.6%** | 3/192 | Full audio | N/A | Batch model (`_pc`) |

**Key findings:**
- **0ms look-ahead matches offline accuracy** (1.6% WER) — now the default. Produces 2x as many encoder chunks as 80ms, but still comfortably real-time with ONNX (0.28x RT). Was too slow with PyTorch (1.87x RT).
- **ONNX matches PyTorch exactly** at 1.6% WER. The native `drop_extra_pre_encoded=2` is baked into the ONNX graph with step 0 skipped at runtime (see export details below).
- **480ms and 1040ms are broken** — produce hallucinated garbage. The larger chunk modes don't work with chunk-by-chunk RNN-T streaming.

### Error breakdown (0ms, the default)

| Type | Count | Examples |
|---|---|---|
| Substitutions | 2 | "turing" → "touring", "datasets" → "sets" |
| Insertions | 1 | extra "data" (before "sets") |

Only 3 errors on 192 words — the same errors the offline batch model makes. The streaming model at 0ms look-ahead is effectively lossless vs offline. No punctuation or capitalization (the offline `_pc` model has these).

## Georgian language status

The infrastructure supports multiple languages, but Georgian is **commented out** in the export script. The models export identically (same architecture, sizes, cache shapes), but ONNX mode doesn't produce good transcription results for Georgian yet. Needs investigation.

## Future improvements

- **Fix Georgian** — investigate why ONNX mode produces poor results for the `ka` streaming model.
- **Make ONNX the default** — auto-detect ONNX models and use them without `--onnx` flag.
- **Reduce MIN_PREPROCESS_MS further** — currently 100ms, could go lower if mel boundary artifacts are acceptable.
