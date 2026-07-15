---
name: stt
description: Speech-to-text transcription using NVIDIA NeMo FastConformer (English & Georgian) or whisper-cpp. Auto-detects spoken language via langid_ambernet. Transcribes audio files (any format supported by ffmpeg) from local paths or HTTP URLs. Use when user wants to transcribe audio, convert speech to text, or get text from a voice/audio file.
---

# STT - Speech to Text

Transcribe audio files to text. Supports any audio format that ffmpeg can decode (mp3, ogg, wav, m4a, flac, webm, etc.).

## Backends

| Backend | Languages | Speed (CPU) | Quality | Notes |
|---------|-----------|-------------|---------|-------|
| **stt2 (ONNX)** ⭐ | English | **0.03x RTF** | Excellent + punctuation | ONNX Runtime, 3.5s startup, ~500MB RAM |
| **stt (NeMo/PyTorch)** | English, Georgian | ~0.15x RTF | Excellent + punctuation | PyTorch, ~14s startup, ~1.8GB RAM |
| **whisper-cpp** (`--whisper`) | English only | ~0.33x RTF | Good | OpenAI Whisper small.en, 244M params |

**For English, use `stt2`** — 4x faster than PyTorch NeMo, 4x less startup time, 3.5x less memory. Same model, same accuracy (1.6% WER).

## Usage

### stt2 (recommended for English)

```bash
# Transcribe audio file
stt2 ./recording.mp3

# Transcribe from URL
stt2 https://example.com/audio.ogg

# Custom output directory
stt2 ./recording.mp3 --outdir ./transcripts
```

### stt (multi-language, PyTorch)

```bash
# Transcribe audio (auto-detects language by default)
stt ./recording.mp3

# Explicit language (skip auto-detection)
stt ./recording.mp3 --lang en
stt ./recording.mp3 --lang ka

# Use whisper-cpp instead of NeMo (English only)
stt ./recording.mp3 --whisper
```

### Options

**stt2:**
- `--outdir <dir>` - Output directory (default: `/tmp/stt/`)

**stt:**
- `-l, --lang <auto|en|ka>` - Language (default: `auto`)
- `--whisper` - Use whisper-cpp backend (English only)
- `--outdir <dir>` - Output directory (default: `/tmp/stt/`)
- `--timestamps` - Include timestamps (whisper only)

## Architecture

### stt2 (ONNX, English-only)

- `stt2` — wrapper script (ffmpeg conversion, URLs, output files), calls `stt2-nemo`
- `stt2-nemo` — shell wrapper that runs `uv run python -m stt2`
- `src/stt2/` — ONNX Runtime transcriber: numpy mel preprocessing → ONNX encoder → RNN-T/CTC decode
- ONNX models: `~/.cache/stt-onnx/en/` (exported via `scripts/export_onnx_offline.py`)
- **No PyTorch or NeMo at runtime** — just numpy + onnxruntime (~500MB total)

### stt (PyTorch, multi-language)

- `stt` — wrapper script (ffmpeg conversion, URLs, output files), calls `stt-nemo`
- `stt-nemo` — shell wrapper that runs `uv run python -m stt_nemo`
- `whisper-cli` — nix package from nixpkgs (used with `--whisper` flag)

### Packaging

Nix (via home-manager) provides system deps: `uv`, `ffmpeg`, `python3.11`, `whisper-cpp`.
Python dependencies (PyTorch CPU, NeMo toolkit, etc.) are managed by **uv** using `pyproject.toml` + `uv.lock`.

- **`pyproject.toml`** + **`uv.lock`** — Python dependency specification
- **`src/stt2/`** — ONNX offline transcriber (English)
- **`src/stt_nemo/`** — PyTorch/NeMo transcriber (English + Georgian)
- **`src/stt_streaming/`** — ONNX streaming transcriber (WebSocket server)

## Output

Writes transcript to `/tmp/stt/<basename>-<timestamp>.txt` and prints the path to stderr.

## After transcribing

- If the user just provided an audio file/URL with no other instructions, print the transcript content.
- If the user asked to transcribe and do something with the result (translate, summarize, etc.), read the transcript and perform the requested task.

## Models

### Language Identification (auto-detect)

- **AmberNet:** [`langid_ambernet`](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/nemo/models/langid_ambernet) — compact spoken language ID model trained on VoxLingua107 (107 languages), 10x smaller than SOTA with comparable accuracy. Downloaded on first use to `~/.cache/torch/NeMo/`.

### NeMo FastConformer (default)

- **English:** [`nvidia/stt_en_fastconformer_hybrid_large_pc`](https://huggingface.co/nvidia/stt_en_fastconformer_hybrid_large_pc) — 115M params, ~460MB
- **Georgian:** [`nvidia/stt_ka_fastconformer_hybrid_large_pc`](https://huggingface.co/nvidia/stt_ka_fastconformer_hybrid_large_pc) — 115M params, ~460MB, 5.7% WER on MCV test

Models are downloaded on first use to `~/.cache/huggingface/hub/` (~460MB each).

### whisper-cpp (--whisper flag)

- **English:** OpenAI Whisper `small.en` — 244M params, via `whisper-cli`

## Benchmark

81.2s English audio, CPU:

| Backend | Total time | Startup | Inference | RTF | WER | Punctuation |
|---------|-----------|---------|-----------|-----|-----|-------------|
| **stt2 (ONNX)** | **3.5s** | 1.1s | 2.2s | 0.03x | 1.6% | ✅ |
| **stt2-server (HTTP)** | **2.1s** | 0 (preloaded) | 2.1s | 0.03x | 1.6% | ✅ |
| stt (PyTorch) | 13.8s | ~10s | ~3.5s | 0.17x | 1.6% | ✅ |
| stt --whisper | ~27s | ~1s | ~26s | 0.33x | — | ❌ |

See also `bench/bench.sh` for the full benchmark script.
