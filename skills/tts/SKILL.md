---
name: tts
description: Text-to-speech synthesis using Kokoro-82M and Piper TTS. Converts text to natural-sounding speech audio (WAV). Use when user wants to generate audio from text, create voiceover, or convert text to speech. Supports Georgian via Piper.
---

# TTS - Text to Speech

Synthesize speech from text using two backends:
- **[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)** (default) — lightweight 82M param model, 9 languages, 50+ voices
- **[Piper TTS](https://github.com/OHF-Voice/piper1-gpl)** — fast ONNX-based TTS, alternative backend (use `--piper` to force)

## Usage

`tts` is available on PATH via home-manager.

```bash
# Basic usage - text as argument
tts "Hello world, this is a test."

# Choose a voice
tts "Good morning everyone." --voice am_adam

# Adjust speed
tts "Speaking quickly now." --speed 1.3

# Read from a file
tts --file article.txt --voice af_nova

# Pipe text in
echo "Piped text input" | tts

# Custom output path
tts "Hello" -o ~/output.wav

# Custom output directory (default: /tmp/tts/)
tts "Hello" --outdir ./audio

# List all available voices (both Kokoro and Piper)
tts --voices

# Georgian text (auto-detected → Piper)
tts "გამარჯობა, მე ვარ ქართველი"

# Georgian via Kokoro (experimental hack: espeak-ng G2P + Kokoro acoustic model)
tts "გამარჯობა" --kokoro
tts "გამარჯობა" --voice am_adam   # explicit voice also triggers Kokoro

# Force Piper backend for English (e.g., for speed testing)
tts "Hello world" --piper

# Specific Piper voice
tts "Hello world" --piper-voice en_US-lessac-medium
```

### Options

- `-v, --voice <name>` - Kokoro voice name (default: `af_heart`)
- `-s, --speed <float>` - Speech speed multiplier (default: `1.0`)
- `-f, --file <path>` - Read text from file instead of argument
- `-o, --output <path>` - Output WAV file path (default: auto-generated in `/tmp/tts/`)
- `--outdir <dir>` - Output directory (default: `/tmp/tts/`)
- `--voices` - List all available voices
- `--piper` - Force Piper backend instead of Kokoro
- `--piper-voice <name>` - Piper voice name (implies `--piper`; default: auto-select)
- `--kokoro` - Force Kokoro backend (e.g. for Georgian via espeak-ng G2P hack)

## Language Detection

Georgian text is **auto-detected** by checking for Georgian Unicode characters (U+10A0–U+10FF). When detected, the **Piper** backend with `ka_GE-natia-medium` voice is used by default (best quality).

Alternatively, Georgian can be routed through **Kokoro** using `--kokoro` or by specifying an explicit `--voice`. This uses an experimental hack: espeak-ng converts Georgian text to IPA phonemes, which are fed into Kokoro's acoustic model. All Georgian IPA phonemes happen to be in Kokoro's vocabulary. Quality is lower than Piper (accented, no Georgian prosody training) but allows using any of Kokoro's 50+ voices.

## Voices

### Kokoro (default)

Voice names follow the pattern `{lang}{gender}_{name}`:
- First letter = language: `a` (American English), `b` (British English), `e` (Spanish), `f` (French), `h` (Hindi), `i` (Italian), `j` (Japanese), `p` (Brazilian Portuguese), `z` (Mandarin Chinese)
- Georgian via Kokoro (`--kokoro`): uses any Kokoro voice with espeak-ng G2P (experimental, accented)
- Second letter = gender: `f` (female), `m` (male)

### Popular Kokoro voices

| Voice | Language | Gender | Description |
|-------|----------|--------|-------------|
| `af_heart` | American English | Female | Default, warm tone |
| `af_bella` | American English | Female | Clear, professional |
| `af_nova` | American English | Female | Bright, energetic |
| `am_adam` | American English | Male | Neutral, clear |
| `am_michael` | American English | Male | Deep, authoritative |
| `bf_emma` | British English | Female | British accent |
| `bm_george` | British English | Male | British accent |
| `ff_siwis` | French | Female | French |

### Piper voices

| Voice | Language | Quality |
|-------|----------|---------|
| `ka_GE-natia-medium` | Georgian | Medium (22kHz) |
| `en_US-lessac-medium` | American English | Medium (22kHz) |

## Output

Writes WAV audio to `/tmp/tts/tts-<voice>-<timestamp>.wav` and prints the path to stdout. Progress/status messages go to stderr.

- Kokoro (including Georgian): 24kHz, 16-bit mono
- Piper: 22.05kHz, 16-bit mono

## After generating

- If the user just asked to generate speech, print the output file path.
- If the user wants to do something with the audio (e.g., play it, convert format), use the output path accordingly.
- For long texts, the generation may take a moment — the model processes text in chunks.
