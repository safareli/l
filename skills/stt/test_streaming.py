#!/usr/bin/env python3
"""
Test client for stt-streaming WebSocket server.

Reads a 16kHz mono WAV file and streams it to the server in chunks,
simulating real-time audio delivery. Prints partial and final results.

Usage:
    # First, convert any audio to 16kHz mono WAV:
    ffmpeg -i input.mp3 -ar 16000 -ac 1 test.wav

    # Stream to server:
    uv run python test_streaming.py test.wav
    uv run python test_streaming.py test.wav ws://localhost:6771/stream?lang=en
    uv run python test_streaming.py test.wav ws://localhost:6771/stream?lang=ka --chunk-ms 300
"""

import argparse
import asyncio
import json
import sys
import time
import wave

import websockets


async def stream_file(uri: str, wav_path: str, chunk_ms: int):
    # Read WAV file
    with wave.open(wav_path, "rb") as wf:
        if wf.getsampwidth() != 2:
            print(f"Error: WAV must be 16-bit (got {wf.getsampwidth() * 8}-bit)", file=sys.stderr)
            sys.exit(1)
        if wf.getnchannels() != 1:
            print(f"Error: WAV must be mono (got {wf.getnchannels()} channels)", file=sys.stderr)
            sys.exit(1)
        if wf.getframerate() != 16000:
            print(f"Error: WAV must be 16kHz (got {wf.getframerate()}Hz)", file=sys.stderr)
            sys.exit(1)
        pcm = wf.readframes(wf.getnframes())
        duration_s = wf.getnframes() / wf.getframerate()

    chunk_bytes = 16000 * 2 * chunk_ms // 1000  # 2 bytes per sample (int16)
    n_chunks = (len(pcm) + chunk_bytes - 1) // chunk_bytes

    print(f"Audio: {wav_path} ({duration_s:.1f}s, {len(pcm)} bytes)", file=sys.stderr)
    print(f"Streaming {n_chunks} chunks of {chunk_ms}ms to {uri}", file=sys.stderr)
    print(file=sys.stderr)

    t0 = time.monotonic()

    async with websockets.connect(uri, ping_interval=30, ping_timeout=120) as ws:
        # Wait for ready
        msg = json.loads(await ws.recv())
        print(f"← {msg}", file=sys.stderr)

        if msg.get("type") == "error":
            print(f"Server error: {msg['message']}", file=sys.stderr)
            return

        # Send audio chunks, simulating real-time delivery
        for i in range(0, len(pcm), chunk_bytes):
            chunk = pcm[i : i + chunk_bytes]
            await ws.send(chunk)

            # Drain any partial results (non-blocking)
            try:
                while True:
                    result = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    data = json.loads(result)
                    print(f"  partial: {data.get('text', '')}", file=sys.stderr)
            except (asyncio.TimeoutError, TimeoutError):
                pass

            # Simulate real-time pacing
            await asyncio.sleep(chunk_ms / 1000)

        # Signal end of stream
        await ws.send(json.dumps({"type": "end"}))

        # Collect remaining partials and the final result
        while True:
            result = json.loads(await ws.recv())
            if result["type"] == "partial":
                print(f"  partial: {result.get('text', '')}", file=sys.stderr)
            elif result["type"] == "final":
                elapsed = time.monotonic() - t0
                print(f"", file=sys.stderr)
                print(f"  FINAL:   {result['text']}", file=sys.stderr)
                print(f"  Time:    {elapsed:.2f}s (audio: {duration_s:.1f}s)", file=sys.stderr)
                # Print final text to stdout for piping
                print(result["text"])
                break
            elif result["type"] == "error":
                print(f"  ERROR:   {result['message']}", file=sys.stderr)
                break


def main():
    parser = argparse.ArgumentParser(description="Test client for stt-streaming")
    parser.add_argument("wav", help="Path to 16kHz mono WAV file")
    parser.add_argument("uri", nargs="?", default="ws://localhost:6771/stream?lang=en",
                        help="WebSocket URI (default: ws://localhost:6771/stream?lang=en)")
    parser.add_argument("--chunk-ms", type=int, default=200,
                        help="Chunk duration in ms (default: 200)")
    args = parser.parse_args()

    asyncio.run(stream_file(args.uri, args.wav, args.chunk_ms))


if __name__ == "__main__":
    main()
