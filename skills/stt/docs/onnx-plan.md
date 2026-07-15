# ONNX Runtime Export Plan for stt-streaming

## Summary

Replace PyTorch eager inference in `stt-streaming` with ONNX Runtime for a **~6.8x speedup** on the encoder forward pass (165ms → 24ms per chunk), which would bring streaming from **1.34x real-time** (34% slower) to comfortably **under 1.0x real-time**.

## Benchmark Results (encoder forward, 16 mel frames, batch=1)

| Backend | Threads | Time/chunk | Speedup vs PyTorch |
|---------|---------|------------|-------------------|
| PyTorch eager | 4 | 165.4ms | 1.0x |
| ONNX Runtime | 1 | 60.3ms | 2.7x |
| ONNX Runtime | 2 | 39.0ms | 4.2x |
| ONNX Runtime | 3 | 29.9ms | 5.5x |
| **ONNX Runtime** | **4** | **24.3ms** | **6.8x** |
| ONNX Runtime | 6 | 39.5ms | 4.2x (worse — over-parallelized) |

## What NeMo Already Provides

NeMo's `EncDecHybridRNNTCTCBPEModel` extends `Exportable` and has built-in ONNX export with `cache_support=True`:

```python
model.set_export_config({'cache_support': True})
model.export('model.onnx')
```

This produces two ONNX files:

### 1. `encoder-model.onnx` (435.5 MB)

The FastConformer encoder with streaming cache I/O.

**Inputs** (cache format is **batch-first**, unlike PyTorch which is layers-first):

| Name | Shape | Type | Description |
|------|-------|------|-------------|
| `audio_signal` | `[B, 80, T]` | float32 | Mel spectrogram chunk |
| `length` | `[B]` | int64 | Number of mel frames in chunk |
| `cache_last_channel` | `[B, 17, 70, 512]` | float32 | Attention K/V cache (17 layers) |
| `cache_last_time` | `[B, 17, 512, 8]` | float32 | Convolution cache (17 layers) |
| `cache_last_channel_len` | `[B]` | int64 | How many cache entries are filled |

**Outputs:**

| Name | Shape | Type | Description |
|------|-------|------|-------------|
| `outputs` | `[B, 512, T']` | float32 | Encoder hidden states |
| `encoded_lengths` | `[B]` | int64 | Output sequence length |
| `cache_last_channel_next` | `[B, 17, T_new, 512]` | float32 | Updated attention cache |
| `cache_last_time_next` | `[B, 17, 512, 8]` | float32 | Updated convolution cache |
| `cache_last_channel_next_len` | `[B]` | int64 | Updated cache length |

### 2. `decoder_joint-model.onnx` (20.3 MB)

The RNN-T decoder (prediction network) + joint network combined.

**Inputs:**

| Name | Shape | Type | Description |
|------|-------|------|-------------|
| `encoder_outputs` | `[B, 512, T']` | float32 | Encoder output for this chunk |
| `targets` | `[B, U]` | int32 | Previously predicted token IDs |
| `target_length` | `[B]` | int32 | Number of target tokens |
| `input_states_1` | `[1, B, 640]` | float32 | LSTM hidden state |
| `input_states_2` | `[1, B, 640]` | float32 | LSTM cell state |

**Outputs:**

| Name | Shape | Type | Description |
|------|-------|------|-------------|
| `outputs` | `[B, T', U, 1025]` | float32 | Joint logits (1024 tokens + blank) |
| `prednet_lengths` | `[B]` | int32 | Output lengths |
| `output_states_1` | `[1, B, 640]` | float32 | Updated LSTM hidden state |
| `output_states_2` | `[1, B, 640]` | float32 | Updated LSTM cell state |

## Important: Export Compatibility Issue

PyTorch 2.10 changed `torch.onnx.export` to use the new dynamo-based exporter by default, which **fails** on NeMo models due to data-dependent shape guards in `rel_shift`. The fix is to force legacy mode:

```python
_orig_export = torch.onnx.export
def patched_export(*args, **kwargs):
    kwargs['dynamo'] = False
    return _orig_export(*args, **kwargs)
torch.onnx.export = patched_export
```

This should be done in the export script.

## Important: Cache Tensor Ordering

- **PyTorch** (`get_initial_cache_state`): layers-first → `[17, B, T, 512]`
- **ONNX export** (`forward_for_export`): batch-first → `[B, 17, T, 512]`

The `forward_for_export` transposes internally. When using ONNX Runtime, caches must be batch-first.

## Implementation Plan

### Phase 1: Export Script (`scripts/export_onnx.py`)

A one-time script that:
1. Loads the PyTorch streaming model
2. Configures `att_context_size` (default `[70, 1]`)
3. Calls `model.export()` with `cache_support=True` and `dynamo=False` patch
4. Saves encoder + decoder_joint ONNX files to a known location
5. Also saves metadata (vocab, blank_id, cache shapes, streaming config) as JSON

Output directory: `~/.cache/stt-streaming-onnx/en/` (or `ka/`)

### Phase 2: ONNX Session Class (`OnnxSession`)

A new `Session`-like class that:
1. Loads ONNX encoder + decoder_joint sessions at startup (instead of PyTorch model)
2. **Reuses NeMo's preprocessor and `CacheAwareStreamingAudioBuffer`** for mel extraction and chunking — these are fast numpy/torch operations, not worth reimplementing
3. Replaces `conformer_stream_step` with:
   a. ONNX encoder inference (the big win — 6.8x faster)
   b. Custom greedy RNN-T decoding loop using ONNX decoder_joint

### Phase 3: Greedy RNN-T Decoding

NeMo's `conformer_stream_step` does greedy RNN-T decoding internally. With ONNX, we implement it ourselves:

```python
def greedy_rnnt_decode(encoder_out, decoder_joint_sess, prev_tokens, lstm_states, blank_id):
    """
    For each encoder time step:
      1. Run decoder on last predicted token → decoder hidden
      2. Run joint on (encoder[t], decoder hidden) → logits over vocab + blank
      3. argmax → if blank, move to next time step; else append token, repeat
    """
    tokens = list(prev_tokens)
    B, hidden, T = encoder_out.shape  # [1, 512, T']
    
    for t in range(T):
        enc_t = encoder_out[:, :, t:t+1]  # [1, 512, 1]
        
        while True:
            # targets = last token (or blank for start)
            target = np.array([[tokens[-1] if tokens else blank_id]], dtype=np.int32)
            target_len = np.array([1], dtype=np.int32)
            
            joint_out, _, new_s1, new_s2 = decoder_joint_sess.run(None, {
                'encoder_outputs': enc_t,
                'targets': target,
                'target_length': target_len,
                'input_states_1': lstm_states[0],
                'input_states_2': lstm_states[1],
            })
            
            # joint_out shape: [1, 1, 1, 1025]
            logit = joint_out[0, 0, 0, :]
            pred = int(np.argmax(logit))
            
            if pred == blank_id:
                break  # move to next encoder time step
            
            tokens.append(pred)
            lstm_states = (new_s1, new_s2)
    
    return tokens, lstm_states
```

This loop is lightweight — the decoder_joint model is only 20MB (vs 435MB encoder), so each call is fast (<1ms).

### Phase 4: Integration with `stt-streaming` Server

Add a `--onnx` flag (or make it default when ONNX models exist):

```bash
./stt-streaming --port 6771 --langs en              # PyTorch (current)
./stt-streaming --port 6771 --langs en --onnx       # ONNX Runtime
```

The server would:
1. Check if ONNX models exist in cache dir
2. If `--onnx` and models exist, use `OnnxSession`
3. Otherwise fall back to PyTorch `Session`

### What We Keep From NeMo (no reimplementation needed)

- **Preprocessor** (mel spectrogram extraction): `model.preprocessor` — fast, runs on numpy/torch, not the bottleneck
- **`CacheAwareStreamingAudioBuffer`**: handles chunking, pre-encode cache padding — complex logic we don't want to reimplement
- **Tokenizer** (BPE): for decoding token IDs → text. Can load from saved vocab file without full NeMo model

### What We Replace

- **Encoder forward pass**: PyTorch → ONNX Runtime (the 6.8x speedup)
- **`conformer_stream_step`**: replaced by ONNX encoder + custom greedy RNN-T loop
- **RNN-T decoder + joint**: PyTorch → ONNX Runtime (small model, minor speedup)

### Memory Impact

- PyTorch model: ~460MB in memory (model weights + PyTorch overhead)
- ONNX Runtime: ~456MB on disk, ~460MB in memory (weights + ORT overhead)
- But: **no PyTorch runtime overhead** — ORT is much lighter than torch. Total process memory would be lower

### Loading Time Impact

- PyTorch: ~4s to load model
- ONNX Runtime: should be faster (no Python model construction, just load weights)
- Preprocessor still needs NeMo/torch for mel extraction — this adds a few seconds
- Could potentially pre-compute mel filterbank and use pure numpy (future optimization)

### Dependencies

Already added to `pyproject.toml`:
- `onnx` (for export script)  
- `onnxruntime` (for inference)
- `onnxscript` (for export, needed by torch.onnx with newer PyTorch)

### Open Question: Can We Avoid Loading the Full PyTorch Model?

Currently, we need NeMo to:
1. Create `CacheAwareStreamingAudioBuffer` (requires model object)
2. Run the preprocessor (requires model.preprocessor)

Options:
- **Short-term**: Still load the PyTorch model for preprocessor + streaming buffer, but run encoder/decoder through ONNX. This is the simplest path and still gives the 6.8x speedup on the bottleneck.
- **Long-term**: Export the preprocessor to ONNX too, and reimplement `CacheAwareStreamingAudioBuffer` logic in pure numpy. This would eliminate the NeMo/PyTorch dependency entirely but is more work.

**Recommendation**: Start with short-term approach. The preprocessor + buffer setup takes <5s at startup and is not a per-chunk bottleneck. The encoder is where 95%+ of inference time is spent.

## Expected End-to-End Performance

Current: 78.7s audio → 105.3s processing (1.34x real-time)

With ONNX encoder (6.8x faster on encoder, decoder loop is lightweight):
- Encoder was ~95% of inference time
- New encoder time: ~95% × (1/6.8) + ~5% ≈ 19% of original
- Estimated: 78.7s audio → ~78.7 × 0.25 ≈ 20s processing overhead after audio finishes
- Real-time streaming: each chunk processes in ~24ms, chunk represents ~160ms of audio → **7x faster than real-time**

The system should go from **34% slower than real-time** to **comfortably real-time with latency to spare**.

## Georgian Model

The Georgian model (`stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc`) exports **identically** — same architecture, same cache shapes, same file sizes. Verified:

| | English | Georgian |
|---|---|---|
| Model class | `EncDecHybridRNNTCTCBPEModel` | Same |
| Encoder ONNX | 435.5 MB | 435.5 MB |
| Decoder+Joint ONNX | 20.3 MB | 20.3 MB |
| Cache shapes | `[B, 17, 70, 512]` / `[B, 17, 512, 8]` | Same |
| Blank index | 1024 | 1024 |
| Vocab size | 1024 (English BPE) | 1024 (Georgian BPE) |
| Punctuation in output | No | Yes (`_pc` model) |
| att_context_size | Configurable | Fixed 80ms |

The export script and `OnnxSession` class are **fully generic** — same code for both languages. Only the ONNX files and vocab differ per language.

## File Structure

```
skills/stt/
  scripts/export_onnx.py          # One-time export script (handles both en/ka)
  src/stt_streaming/
    __init__.py                    # Add --onnx flag, route to OnnxSession
    onnx_session.py                # New: ONNX-based Session implementation
  ~/.cache/stt-streaming-onnx/
    en/
      encoder-model.onnx           # 435 MB
      decoder_joint-model.onnx     # 20 MB
      metadata.json                # vocab, blank_id, cache shapes, streaming config
    ka/
      encoder-model.onnx           # 435 MB
      decoder_joint-model.onnx     # 20 MB
      metadata.json                # Georgian vocab, same structure
```
