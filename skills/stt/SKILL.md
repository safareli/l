---
name: stt
description: Speech-to-text transcription using NVIDIA NeMo FastConformer (English & Georgian) or whisper-cpp. Auto-detects spoken language via langid_ambernet. Transcribes audio files (any format supported by ffmpeg) from local paths or HTTP URLs. Use when user wants to transcribe audio, convert speech to text, or get text from a voice/audio file.
---

# STT - Speech to Text

Transcribe audio files to text. Supports any audio format that ffmpeg can decode (mp3, ogg, wav, m4a, flac, webm, etc.).

## Backends

| Backend | Languages | Speed (CPU) | Quality | Notes |
|---------|-----------|-------------|---------|-------|
| **NeMo FastConformer** (default) | English, Georgian | ~0.15x RTF | Excellent | NVIDIA model, 115M params, ~460MB each |
| **whisper-cpp** (`--whisper`) | English only | ~0.33x RTF | Good | OpenAI Whisper small.en, 244M params |

NeMo is **~2x faster** than whisper-cpp on CPU with comparable or better accuracy.

## Usage

`stt` is available on PATH via home-manager.

```bash
# Transcribe audio (auto-detects language by default)
stt ./recording.mp3

# Explicit language (skip auto-detection)
stt ./recording.mp3 --lang en
stt ./recording.mp3 --lang ka

# Transcribe from an HTTP URL
stt https://example.com/audio.ogg

# Use whisper-cpp instead of NeMo (English only)
stt ./recording.mp3 --whisper

# Include timestamps (whisper only)
stt ./recording.mp3 --whisper --timestamps

# Custom output directory (default: /tmp/stt/)
stt ./recording.mp3 --outdir ./transcripts
```

### Options

- `-l, --lang <auto|en|ka>` - Language (default: `auto`). `auto` detects spoken language via langid_ambernet, then routes to the matching STT model. Supported explicit values: `en` (English), `ka` (Georgian)
- `--whisper` - Use whisper-cpp backend instead of NeMo (English only)
- `--outdir <dir>` - Output directory (default: `/tmp/stt/`)
- `--timestamps` - Include timestamps in transcript output (whisper backend only)

## Architecture

- `stt` — thin wrapper script (handles ffmpeg conversion, URLs, output files), calls `stt-nemo`
- `stt-nemo` — shell wrapper that runs `uv run python -m stt_nemo` (uv manages the Python venv + deps from `pyproject.toml` + `uv.lock`)
- `whisper-cli` — nix package from nixpkgs (used with `--whisper` flag)

### Packaging

Nix (via home-manager) provides system deps: `uv`, `ffmpeg`, `python3.11`, `whisper-cpp`.
Python dependencies (PyTorch CPU, NeMo toolkit, etc.) are managed by **uv** using `pyproject.toml` + `uv.lock`. On first run, uv creates a `.venv/` and installs everything (~500MB cached in `~/.cache/uv`).

- **`pyproject.toml`** + **`uv.lock`** — Python dependency specification
- **`src/stt_nemo/`** — Python module for NeMo transcription

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

See `bench/bench.sh` for the full benchmark script. Run with: `cd skills/stt && bash bench/bench.sh`
