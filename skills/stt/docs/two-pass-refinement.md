# Two-Pass ASR Refinement Plan

## Overview

Add a second-pass refinement to the streaming STT pipeline: the existing cache-aware
FastConformer (RNN-T) provides instant real-time partials, then Parakeet TDT 0.6B v2
re-transcribes the raw audio asynchronously to produce a higher-quality replacement.
The user sees text appear instantly, then it silently upgrades in-place moments later
with better technical terms, punctuation, and capitalization.

## Background & Research

This is a well-established pattern in production ASR systems:

### Google Two-Pass E2E ASR (2019)
[arxiv.org/abs/1908.10992](https://arxiv.org/abs/1908.10992)
- First pass: streaming RNN-T for real-time text
- Second pass: non-streaming LAS (Listen, Attend, Spell) rescores the hypothesis
  using the full audio with bidirectional attention
- Result: **17-22% WER reduction** over RNN-T alone
- Used in Pixel on-device speech recognition

### Google Deliberation Model (2020)
[arxiv.org/abs/2003.07962](https://arxiv.org/abs/2003.07962)
- Second pass attends to **both** the audio AND the first-pass text
- **23% WER reduction on proper nouns** — exactly our problem (JSON, MD5, Turing)
- 12% overall WER reduction vs LAS rescoring alone

### WeNet Unified Two-Pass / U2 (2020)
[arxiv.org/abs/2012.05481](https://arxiv.org/abs/2012.05481)
- CTC decoder streams partial transcripts
- On endpoint detection, attention decoder rescores the CTC output
- Single model with two decoders: CTC for speed, attention for quality
- 5.6% relative CER reduction from the second pass

### Deepgram Commercial API
Three-tier result system:
- `interim_results`: unstable partials (real-time)
- `is_final`: stable segment within an utterance
- `speech_final` / `utterance_end`: natural endpoint (longer silence) → finalization
- `utterance_end_ms` is configurable (default: 1000ms)

### Audio Splitting Strategies (2024)
[arxiv.org/abs/2409.05674](https://arxiv.org/html/2409.05674v1)
- Fixed intervals: lowest latency, worst quality
- VAD-based: best quality, highest latency
- Feedback algorithm: 2-4% WER increase for 1.5-2s latency reduction
- Conclusion: VAD/silence-based splitting is optimal for quality

### Qwen-ASR Streaming (antirez)
[github.com/antirez/qwen-asr](https://github.com/antirez/qwen-asr)
- 2-second chunks with prefix rollback
- Keeps last decoded tokens as context for next chunk
- Later chunks naturally refine earlier ones

### Key Takeaways
1. **Send raw audio, not text** — the second model needs to hear what the first missed
2. **Trigger on utterance boundaries** (1-2s silence), not per-segment
3. **Proper noun improvement is real** — 23% reduction on names/technical terms
4. **Async second pass** — never block streaming
5. **Graceful fallback** — if second pass fails, keep first-pass text

## Architecture

```
                           ┌──────────────────────────────────────┐
                           │        stt-streaming server          │
Audio ──WebSocket──►       │                                      │
                           │  ┌────────────────────────────┐      │
                           │  │  FastConformer (streaming)  │      │
                           │  │  400ms silence → segment    │      │
                           │  └──────────┬─────────────────┘      │
                           │             │                        │
                           │    partial/final messages             │
                           │             │                        │
                           │  ┌──────────▼─────────────────┐      │
                           │  │  Refinement Tracker         │      │
                           │  │                             │      │
                           │  │  - Buffers PCM audio        │      │
                           │  │  - Groups segments          │      │
                           │  │  - Detects utterance end    │      │
                           │  │    (1s silence OR 30s max)  │      │
                           │  │                             │      │
                           │  │  On trigger:                │      │
                           │  │  ┌─────────────────────┐    │      │
                           │  │  │ async POST audio to │    │      │
                           │  │  │ Parakeet (localhost) │    │      │
                           │  │  └─────────┬───────────┘    │      │
                           │  │            │                │      │
                           │  │    refined message          │      │
                           │  └────────────┼────────────────┘      │
                           │               │                       │
                           └───────────────┼───────────────────────┘
                                           │
                           ◄───WebSocket───┘
                           Client
```

### Message Types (WebSocket → Client)

Existing:
```json
{"type": "partial",  "seq": 5, "text": "we should use jason stringify"}
{"type": "final",    "seq": 5, "text": "we should use jason stringify"}
```

New:
```json
{"type": "refined",  "seq_start": 3, "seq_end": 5,
 "text": "We should use JSON.stringify instead of the manual approach.",
 "model": "parakeet-tdt-0.6b-v2", "elapsed_s": 0.42}
```

`refined` replaces streaming segments `seq_start` through `seq_end` (inclusive)
with a single block of higher-quality text.

## Detailed Design

### Server-Side: `continuous.py` Changes

#### New State
```python
# Refinement tracking
self._refinement_audio_chunks: list[bytes] = []   # PCM audio since last refinement
self._refinement_seq_start: int = 0                # first segment seq in current group
self._refinement_audio_ms: float = 0.0             # audio duration in current group
self._refinement_pending: bool = False              # waiting for Parakeet response
```

#### Configuration
```python
REFINEMENT_SILENCE_MS = 1000    # trigger refinement after 1s of silence
REFINEMENT_MAX_AUDIO_MS = 30000 # force-refine after 30s of audio (~960KB PCM)
```

These are **layered on top of** the existing 400ms segment boundary silence.
The two thresholds serve different purposes:

| Threshold | Default | Purpose | Triggers |
|-----------|---------|---------|----------|
| `silence_ms` | 400ms | Streaming segment boundary | `Final` message |
| `refinement_silence_ms` | 1000ms | Utterance end (thought boundary) | `refined` message (batched) |
| `refinement_max_ms` | 30000ms | Long speech without pauses | `refined` at next `Final` |

The refinement trigger **always waits for a streaming segment boundary** first.
It never fires mid-segment. This means:
- User always sees streaming text appear normally
- Refinement only replaces finalized segments, never partials
- No text disappears or jumps during active speech

Configurable via WebSocket URL params: `refinement_silence_ms`, `refinement_max_ms`.

#### Audio Buffering
Every PCM chunk fed to `feed_audio()` is also appended to
`_refinement_audio_chunks` (only when refinement mode is enabled).

#### Trigger Logic

Refinement only triggers **after a streaming segment boundary has been committed**
(i.e., after a `Final` has been emitted). It never interrupts mid-speech or
mid-segment. The silence thresholds work in layers:

```
Speech → 400ms silence → streaming segment Final → more speech → 400ms silence → Final
                                                                                    │
         1s total silence (no new segment Finals) ──────────────────────────────────►│
                                                                                    ▼
                                                                            Refinement trigger
                                                                            (covers all Finals
                                                                             since last refinement)
```

For long conversations with short pauses (only 400ms breathing pauses), the
**max duration fallback** kicks in. But it still waits for the next streaming
segment boundary before triggering — never mid-segment:

```
Final[3] → Final[4] → Final[5] → ... → Final[12] (30s of audio accumulated)
                                                    │
                                            next segment Final triggers refinement
                                            for segments 3-12
```

After each sub-chunk, check:
```python
should_refine = False

# Only consider refinement after a segment Final was just emitted
# (ensures we never interrupt mid-segment)
if not just_emitted_final:
    pass  # skip refinement check entirely

# Utterance end: long silence after speech, and at a segment boundary
elif (self._has_had_speech_in_group
      and self._silence_ms >= REFINEMENT_SILENCE_MS
      and not self._refinement_pending):
    should_refine = True

# Max duration: force-refine at next segment boundary
elif (self._refinement_audio_ms >= REFINEMENT_MAX_AUDIO_MS
      and self._has_had_speech_in_group
      and not self._refinement_pending):
    should_refine = True
```

#### Async Refinement
When triggered:
1. Merge `_refinement_audio_chunks` into a WAV blob (in-memory)
2. Record `seq_start` and `seq_end` (current seq)
3. Set `_refinement_pending = True`
4. Fire-and-forget async POST to `http://127.0.0.1:6774/transcribe`
5. On response: emit `refined` message, set `_refinement_pending = False`
6. Reset audio buffer, start new group

Since `continuous.py` runs in a sync context (called from the WebSocket handler),
the async POST should be dispatched via `asyncio.create_task()` from the WebSocket
handler that wraps `ContinuousSession`.

#### Flow in `__init__.py` WebSocket Handler
```python
# In the WebSocket message loop:
results = session.feed_audio(pcm_chunk)
for result in results:
    if isinstance(result, Partial):
        await ws.send_json({"type": "partial", ...})
    elif isinstance(result, Final):
        await ws.send_json({"type": "final", ...})
    elif isinstance(result, RefinementRequest):
        # Dispatch async refinement
        asyncio.create_task(do_refinement(ws, result))
```

```python
async def do_refinement(ws, req: RefinementRequest):
    """POST audio to Parakeet, send refined message back on WebSocket."""
    try:
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                "http://127.0.0.1:6774/transcribe",
                data=req.wav_bytes,
                headers={"Content-Type": "audio/wav"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
            data = await resp.json()
        await ws.send_json({
            "type": "refined",
            "seq_start": req.seq_start,
            "seq_end": req.seq_end,
            "text": data["text"],
            "model": data.get("model", "parakeet"),
            "elapsed_s": data.get("elapsed_s", 0),
        })
    except Exception as e:
        # Refinement failure is non-fatal — streaming text stays as-is
        log(f"Refinement failed: {e}")
```

### Client-Side: `stt-live.html` Changes

#### Handling `refined` Messages
```javascript
case 'refined':
    handleRefined(msg);
    break;
```

```javascript
function handleRefined(msg) {
    // Find segments in range [seq_start, seq_end]
    const start = msg.seq_start;
    const end = msg.seq_end;

    // Remove old segments in range
    segments = segments.filter(s => s.seq < start || s.seq > end);

    // Insert single refined segment
    // Use seq_start as the seq so it sorts correctly
    const refined = {
        seq: start,
        text: msg.text,
        final: true,
        refined: true,   // for styling
    };

    // Insert in correct position
    const insertIdx = segments.findIndex(s => s.seq > start);
    if (insertIdx === -1) {
        segments.push(refined);
    } else {
        segments.splice(insertIdx, 0, refined);
    }

    render();
}
```

#### Visual Treatment
Refined segments get a subtle green left border and a brief fade-in animation
to indicate they've been upgraded, without being distracting:

```css
.segment .refined {
    border-left: 3px solid #16a34a;
    padding-left: 0.5em;
    animation: refined-fade 0.6s ease-out;
}
@keyframes refined-fade {
    from { background: #f0fdf4; }
    to   { background: transparent; }
}
```

#### Enabling Refinement
Add a checkbox: `☑ Refine (Parakeet)` — when checked, the WebSocket URL
includes `&refine=true`. This tells the server to enable two-pass mode.

Optionally show the refinement latency below each refined block:
`"🦜 refined in 0.42s"` in small gray text.

### URL Parameters (WebSocket)

New params when `refine=true`:
```
ws://host:6771/stream?lang=en&continuous=true&silence_ms=400&refine=true&refinement_silence_ms=1000&refinement_max_ms=30000
```

## Implementation Steps

### Phase 1: Server Plumbing
1. [x] Add `RefinementRequest` dataclass to `continuous.py`
   - Fields: `wav_bytes`, `seq_start`, `seq_end`
2. [x] Add audio buffering to `ContinuousSession.feed_audio()`
   - Append every PCM chunk to `_refinement_audio_chunks`
   - Track `_refinement_audio_ms`
3. [x] Add utterance-end detection (separate from 400ms segment boundary)
   - 1s silence threshold after speech
   - 30s max duration fallback
4. [x] Emit `RefinementRequest` when triggered
5. [x] Add WAV assembly helper (PCM chunks → in-memory WAV bytes)

### Phase 2: WebSocket Integration
6. [x] Update `__init__.py` WebSocket handler to detect `refine=true` param
7. [x] Handle `RefinementRequest` results from `feed_audio()`
8. [x] Implement `do_refinement()` async task (POST to Parakeet, send `refined` msg)
9. [x] Handle edge cases:
   - WebSocket closes before refinement completes → cancel task
   - Parakeet server unavailable → log warning, skip refinement
   - Overlapping refinements → `_refinement_pending` flag prevents overlap

### Phase 3: Client
10. [x] Add `☑ Refine (Parakeet)` checkbox to controls
11. [x] Include `&refine=true` in WebSocket URL when checked
12. [x] Handle `refined` message type in JS
13. [x] Implement segment replacement logic
14. [x] Add CSS for refined segments (green border, fade animation)
15. [x] Show refinement metadata (model name, latency)

### Phase 4: Testing & Polish
16. [ ] Test with bench_continuous.py (verify segments align correctly)
17. [ ] Test real-time with microphone (verify no flicker/jump on refinement)
18. [ ] Test edge cases: rapid speech (no long pauses), very long pauses
19. [ ] Test Parakeet server restart during streaming
20. [ ] Tune thresholds: refinement_silence_ms (1000?), max_ms (30000?)

## Memory & Performance Budget

| Resource | Per connection | Notes |
|----------|---------------|-------|
| Audio buffer | ~960KB max | 30s × 16kHz × 2 bytes, freed on refinement |
| Parakeet latency | ~0.3-0.5s | For 10-15s audio chunk, localhost |
| Parakeet memory | ~652MB | INT8, already running as separate service |
| Network | 0 | localhost HTTP, no external calls |

## Non-Goals (for now)
- Unified two-pass model (Google's approach) — requires training
- Deliberation network (attending to both audio + first-pass text) — requires training
- Speaker diarization in refined output
- Refinement for non-continuous mode (single utterance, not needed)
- Refinement with Georgian (Parakeet is English-only)
