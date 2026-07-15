---
name: yt-transcript
description: Download YouTube video transcripts and generate AI summaries with timestamped sections. Use when user wants to get transcript, summary, or understand content of a YouTube video.
---

# YouTube Transcript

Download YouTube transcripts and process them into timestamped and plain text formats.

## Usage

`yt-transcript` is available on PATH via home-manager.

```bash
# Basic usage (outputs to /tmp/yt-transcript/)
yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID"

# Custom output directory
yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" ./my_output

# Specify subtitle language (default: en)
yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --lang ka
```

## Output Files

All files are prefixed with `<video_id>-<video_title>`:

- `*_timestamped.txt` - Transcript with timestamps per line
- `*_text.txt` - Plain text transcript (no timestamps)
- `*.<lang>.srt` - Original SRT subtitle file

## After downloading

- If the user just dropped a YouTube link with no other instructions, or explicitly asked for a summary, read the `*_timestamped.txt` file and summarize it (see format below).
- If the user asked a specific question about the video, read the transcript and answer that question directly — don't summarize.

## Summary format

- For each major topic/section:
  ```
  ## Section Title (HH:MM:SS - HH:MM:SS)
  Brief description of what's covered
  ```
- Skip any ads, sponsor segments, or promotional content
- Write in a direct way — no clickbait or mystery language, give all spoilers upfront
- End with a **tl;dr** that captures the key takeaways in 2-3 sentences
- Output only the sections and tl;dr — no preamble, no title, no meta-commentary
