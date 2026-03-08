# stt-streaming — WebSocket Streaming Speech-to-Text

Real-time streaming speech-to-text over WebSocket using NVIDIA NeMo cache-aware streaming FastConformer models.

Unlike the batch `stt` command (which requires the complete audio file upfront), `stt-streaming` processes audio as it arrives — sending back growing partial transcriptions in real time.

## How it works

The server loads **cache-aware streaming** variants of the same FastConformer models used by `stt-nemo`. These models are specifically trained to process audio in small chunks while maintaining internal state (attention K/V caches + convolution caches + RNN-T decoder state) between chunks. Each new chunk picks up where the last one left off, producing incrementally longer transcriptions.

```
Browser / Client                            stt-streaming server
─────────────────                           ─────────────────────────
                                            startup: load streaming model(s)

ws://host:port/stream?lang=en  ──────────►  new Session created
                                              • init encoder caches (attention + conv)
                                              • init RNN-T decoder state

binary frame (PCM bytes)       ──────────►  accumulate in sample buffer
                                              └─ enough for a model chunk?
                                                   └─ mel spectrogram extraction
                                                   └─ conformer_stream_step(chunk, caches)
                                                   └─ update caches + decoder state
                               ◄──────────  {"type":"partial","text":"hello wor"}

binary frame (PCM bytes)       ──────────►  same loop...
                               ◄──────────  {"type":"partial","text":"hello world"}

{"type":"end"}                 ──────────►  pad + flush remaining audio
                               ◄──────────  {"type":"final","text":"hello world. how are you?"}
```

### Streaming models

| Language | Model | Look-ahead | Params | WER |
|----------|-------|-----------|--------|-----|
| English | [`stt_en_fastconformer_hybrid_large_streaming_multi`](https://huggingface.co/nvidia/stt_en_fastconformer_hybrid_large_streaming_multi) | configurable (0 / 80 / 480 / 1040ms) | ~114M | 5.4-7.0% (LS test-other) |
| Georgian | [`stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc`](https://huggingface.co/nvidia/stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc) | 80ms fixed | ~115M | 7.44% (MCV-test) |

These are the streaming counterparts of the offline models used by `stt-nemo`. Same architecture (`EncDecHybridRNNTCTCBPEModel`), same size (~460MB each), but trained with cache-aware streaming so they can process chunks incrementally without needing to see the full audio.

The English model supports multiple latency modes via attention context size configuration. Lower look-ahead = lower latency but slightly worse accuracy. Default is 80ms (`--att-context-size 70,1`).

### Offline vs streaming model comparison

| | Offline (`stt-nemo`) | Streaming (`stt-streaming`) |
|---|---|---|
| **Input** | Complete audio file | Audio chunks as they arrive |
| **Output** | Single final transcription | Incremental partials + final |
| **Latency** | Must wait for entire file | ~200ms per partial update |
| **Attention** | Full-context (sees everything) | Cache-aware (limited look-ahead) |
| **Decoder** | CTC or RNN-T (batch) | RNN-T greedy (stateful) |
| **Accuracy** | Slightly better | Slightly worse (limited context) |
| **EN WER** | ~5.4% | ~5.4-7.0% (depends on look-ahead) |
| **KA WER** | 5.73% | 7.44% |

## Usage

### Start the server

```bash
cd ~/.config/home-manager/skills/stt

# English only (default)
./stt-streaming --port 6771

# Both languages (~920MB memory)
./stt-streaming --langs en,ka --port 6771

# Lower latency English (0ms look-ahead, slightly worse accuracy)
./stt-streaming --langs en --att-context-size 70,0
```

### CLI options

```
--host HOST              Bind host (default: 0.0.0.0)
--port PORT              Bind port (default: 6771)
--langs LANGS            Comma-separated: en, ka (default: en)
--att-context-size L,R   EN model attention context (default: 70,1 = 80ms)
                         Options: 70,0 (0ms) / 70,1 (80ms) / 70,6 (480ms) / 70,13 (1040ms)
```

### Test with a WAV file

```bash
# Convert any audio to 16kHz mono WAV first:
ffmpeg -i recording.mp3 -ar 16000 -ac 1 recording.wav

# Stream to server (simulates real-time delivery):
uv run python test_streaming.py recording.wav
uv run python test_streaming.py recording.wav ws://localhost:6771/stream?lang=en
uv run python test_streaming.py recording.wav ws://localhost:6771/stream?lang=ka --chunk-ms 300
```

## WebSocket Protocol

### Connecting

```
ws://host:port/stream?lang=en
ws://host:port/stream?lang=ka
```

Language is specified as a query parameter. Client specifies the language upfront — the server needs to know which model to use before audio arrives.

### Client → Server

| Frame type | Content | Description |
|-----------|---------|-------------|
| Binary | Raw PCM bytes | Audio samples: **16-bit signed little-endian, 16kHz, mono**. Send in any chunk size (recommended: 100-500ms per frame). |
| Text | `{"type":"end"}` | Signal end of audio. Server flushes remaining buffer and sends final transcription. |
| Text | `{"type":"reset"}` | Reset session. Clears all caches and buffers for a new utterance on the same connection. |

### Server → Client

| Message | Description |
|---------|-------------|
| `{"type":"ready","lang":"en","sample_rate":16000,"encoding":"pcm_s16le","channels":1}` | Session initialized, ready to receive audio. |
| `{"type":"partial","text":"hello wor"}` | Interim transcription. Sent after each model chunk is processed. The text grows incrementally as more audio arrives. May update/correct earlier words. |
| `{"type":"final","text":"hello world."}` | Final transcription after `{"type":"end"}` signal. Includes any remaining audio that was buffered. |
| `{"type":"error","message":"..."}` | Error message. |

### Audio format

Raw PCM: **16-bit signed little-endian integers, 16kHz sample rate, mono channel**.

This is the simplest possible audio format — just raw amplitude samples, no headers or codec. It's what browser `AudioWorklet` produces natively. Each sample is 2 bytes, so 1 second of audio = 32,000 bytes.

In a browser:
```javascript
// AudioWorklet processor sends Float32 samples
// Convert to Int16 before sending over WebSocket:
const int16 = new Int16Array(float32Samples.length);
for (let i = 0; i < float32Samples.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, float32Samples[i] * 32768));
}
ws.send(int16.buffer);
```

## Architecture

```
src/stt_streaming/
  __init__.py     # Model loading, Session class, WebSocket server
  __main__.py     # Entry point

stt-streaming     # Shell wrapper: cd to project dir, exec uv run python -m stt_streaming
test_streaming.py # Test client: streams a WAV file to the server
```

### Key components

**`load_models(langs)`** — Loads streaming FastConformer models at startup. Configures the RNN-T decoder with `strategy="greedy"` (required — only the non-batched `GreedyRNNTInfer` supports `partial_hypotheses` for carrying decoder state between chunks; the default `greedy_batch` raises `NotImplementedError`).

**`Session`** — One per WebSocket connection. Holds:
- `pcm_buffer` — Accumulates raw PCM samples (minimum 200ms before preprocessing to reduce mel-spectrogram boundary artifacts)
- `streaming_buffer` — NeMo's `CacheAwareStreamingAudioBuffer`, handles mel feature extraction and chunking
- `cache_last_channel` / `cache_last_time` / `cache_last_channel_len` — Encoder attention + convolution caches
- `previous_hypotheses` — RNN-T decoder state (partial token sequence + hidden state)
- `pred_out` — Previous encoder output (for incremental decoding)

**`Session.feed_audio(pcm_bytes)`** — Accumulates PCM, preprocesses when ≥200ms available, iterates over any complete model chunks via `conformer_stream_step`, returns partial transcriptions.

**`Session.finalize()`** — Pads remaining audio with 560ms of silence (ensures last partial chunk fills a complete model chunk), processes with `keep_all_outputs=True`, returns final text.

**`serve()`** — asyncio + `websockets` server. Model inference runs in `asyncio.to_thread()` to avoid blocking the event loop. Each connection gets an independent `Session` with its own caches.

### Processing pipeline (per chunk)

```
1. PCM int16 bytes → float32 numpy array (÷ 32768)
2. Accumulate until ≥ 200ms (3200 samples)
3. NeMo preprocessor: raw audio → mel spectrogram features
4. CacheAwareStreamingAudioBuffer chunks features with pre-encode cache
5. conformer_stream_step(
       chunk_features,         # this chunk's mel frames
       cache_last_channel,     # attention K/V cache from prior chunks
       cache_last_time,        # convolution cache from prior chunks
       previous_hypotheses,    # RNN-T decoder state
       keep_all_outputs=False, # drop uncertain right-edge outputs (True only on final chunk)
   )
6. Updated caches + new partial transcription text
7. Send {"type":"partial","text":"..."} to client
```

### Dependencies

Same as `stt-nemo` plus `websockets>=13.0` (added to `pyproject.toml`, managed by uv).

## TODO

### Georgian language support

The code already supports `--langs en,ka`. Just needs testing with the Georgian streaming model (`stt_ka_fastconformer_hybrid_transducer_ctc_large_streaming_80ms_pc`). The model downloads on first use (~460MB to `~/.cache/huggingface/hub/`). Loading both models costs ~920MB memory total.

### Language auto-detection

Currently the client must specify `?lang=en` or `?lang=ka` upfront. For an `auto` mode, the approach would be:

1. Buffer the first ~2-3 seconds of audio silently (don't process yet)
2. Run `langid_ambernet` (the same small language-ID model used by `stt`) on that buffer
3. Select the matching streaming model
4. Replay the buffered audio through the selected model, then continue streaming normally

This adds a 2-3 second delay before the first partial result. `langid_ambernet` works well on short clips (trained on VoxLingua107). For most real use cases the client knows the language upfront, so this is a nice-to-have rather than essential.

A variant: keep both models loaded and run langid on the initial buffer to pick which one to route to — avoids model-loading delay but costs 2x memory.

### Continuous mode (long-running streams)

Currently each WebSocket session handles **one utterance**: audio comes in, partials stream back, client sends `{"type":"end"}`, server sends final. Good for push-to-talk / voice messages.

For **continuous mode** (live captioning, meeting transcription, always-on mic), the stream stays open indefinitely. The server needs to automatically detect utterance boundaries and reset model caches between them, because:

1. **Cache quality degrades** — The streaming models are optimized for utterance-length audio (5-30s). Caches grow stale over minutes.
2. **Memory grows** — The internal feature buffer grows indefinitely.
3. **UX** — Users want completed sentences/phrases delivered as discrete units, not one infinite string.

Implementation approach:

- Add **energy-based silence detection** on the server side: track RMS energy of incoming audio chunks. When energy drops below a threshold for N milliseconds (e.g., 800ms of silence), treat it as an utterance boundary.
- On boundary: send `{"type":"final"}` for the current utterance, reset `Session` caches, start fresh for the next utterance.
- Add a sequence counter so the client can track which utterance each partial/final belongs to:
  ```json
  {"type":"partial", "text":"hello world", "seq":0}
  {"type":"final",   "text":"hello world.", "seq":0}
  {"type":"partial", "text":"how are",     "seq":1}
  {"type":"final",   "text":"how are you?","seq":1}
  ```
- Could optionally use **Silero VAD** (a small, fast voice activity detection model) instead of simple energy thresholds for more robust boundary detection, but energy-based detection is simpler and good enough for v1.

### Punctuation and capitalization

The offline models (`stt_en_fastconformer_hybrid_large_pc`) include punctuation and capitalization in their output — the `_pc` suffix. The English streaming model (`stt_en_fastconformer_hybrid_large_streaming_multi`) does **not** produce punctuation or capitalization. The Georgian streaming model does have `_pc`.

Options:
- Use a lightweight punctuation restoration model as a post-processing step (e.g., NeMo has `punctuation_en_bert` or similar)
- Accept unpunctuated output for now (the text is still accurate, just lowercase without periods/commas)
- For the English model, could try the 1040ms look-ahead setting which may have slightly better formatting

### Home-manager integration

Add `stt-streaming` to PATH like `stt` and `stt-nemo`. The wrapper script already exists at `skills/stt/stt-streaming`. Needs to be wired into `home.nix` to symlink into `~/.local/bin/`.

Could also add a systemd user service to keep the server running in the background:
```nix
systemd.user.services.stt-streaming = {
  description = "Streaming STT WebSocket server";
  wantedBy = [ "default.target" ];
  serviceConfig.ExecStart = "${stt-streaming-wrapper} --langs en --port 6771";
  serviceConfig.Restart = "on-failure";
};
```

### Browser client

Build a minimal HTML/JS client for testing from a browser:
- `getUserMedia()` to capture microphone
- `AudioWorklet` to get raw PCM samples at 16kHz
- WebSocket to stream samples to the server
- Display partial results in real time

This would make it easy to demo and test without needing WAV files.
