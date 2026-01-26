---
name: yt-transcript
description: Download YouTube video transcripts and generate AI summaries with timestamped sections. Use when user wants to get transcript, summary, or understand content of a YouTube video.
---

# YouTube Transcript

Download YouTube transcripts, process them into timestamped and plain text formats, and generate AI summaries.

## Setup

```bash
cd ~/dev/yt-transcript
direnv allow
```

## Usage

In non-interactive shells (like when running from an agent), direnv hooks don't auto-load.
Use `eval "$(direnv export bash)"` to explicitly load the environment.

```bash
cd ~/dev/yt-transcript && eval "$(direnv export bash)"

# Basic usage (outputs to ./results/)
./yt_transcript.sh "https://www.youtube.com/watch?v=VIDEO_ID"

# Custom output directory
./yt_transcript.sh "https://www.youtube.com/watch?v=VIDEO_ID" ./my_output
```

## Output Files

All files are prefixed with `<video_id>-<video_title>`:

- `*_timestamped.txt` - Transcript with timestamps per line
- `*_text.txt` - Plain text transcript (no timestamps)
- `*_summary.txt` - AI-generated summary with sections and tl;dr
- `*.en.srt` - Original SRT subtitle file

## Summary Format

The summary includes:
- Header with video title and link
- Sections with timestamps (e.g., `## Section Title (00:00:00 - 00:02:30)`)
- tl;dr at the end
- Ads/sponsor segments are excluded
