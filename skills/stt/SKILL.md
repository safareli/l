---
name: stt
description: Speech-to-text transcription using whisper-cpp. Transcribes audio files (any format supported by ffmpeg) from local paths or HTTP URLs. Use when user wants to transcribe audio, convert speech to text, or get text from a voice/audio file.
---

# STT - Speech to Text

Transcribe audio files to text using whisper-cpp (small.en model). Supports any audio format that ffmpeg can decode (mp3, ogg, wav, m4a, flac, webm, etc.).

## Usage

`stt` is available on PATH via home-manager.

```bash
# Transcribe a local file
stt ./recording.mp3

# Transcribe from an HTTP URL
stt https://example.com/audio.ogg

# Custom output directory (default: /tmp/stt/)
stt ./recording.mp3 --outdir ./transcripts

# Include timestamps in output
stt ./recording.mp3 --timestamps
```

### Options

- `--outdir <dir>` - Output directory (default: `/tmp/stt/`)
- `--timestamps` - Include timestamps in transcript output

## Output

Writes transcript to `/tmp/stt/<basename>-<timestamp>.txt` and prints the path.

## After transcribing

- If the user just provided an audio file/URL with no other instructions, print the transcript content.
- If the user asked to transcribe and do something with the result (translate, summarize, etc.), read the transcript and perform the requested task.
