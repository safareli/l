#!/usr/bin/env python3
"""
Benchmark continuous mode WER — sends audio with real-time-ish pacing
so the server processes incrementally (not one giant batch).

Usage:
    uv run --python python3.11 python scripts/bench_continuous.py
    uv run --python python3.11 python scripts/bench_continuous.py --silence-ms 200
    uv run --python python3.11 python scripts/bench_continuous.py --silence-ms 400 --chunk-ms 200
"""

import argparse
import asyncio
import json
import re
import time
import wave


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", text.lower())).strip()


def compute_wer(ref_words, hyp_words):
    r, h = len(ref_words), len(hyp_words)
    d = [[0] * (h + 1) for _ in range(r + 1)]
    for i in range(r + 1): d[i][0] = i
    for j in range(h + 1): d[0][j] = j
    for i in range(1, r + 1):
        for j in range(1, h + 1):
            d[i][j] = d[i-1][j-1] if ref_words[i-1] == hyp_words[j-1] else 1 + min(d[i-1][j], d[i][j-1], d[i-1][j-1])
    ops = []; i, j = r, h
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref_words[i-1] == hyp_words[j-1]: i -= 1; j -= 1
        elif i > 0 and j > 0 and d[i][j] == d[i-1][j-1] + 1:
            ops.append(f'SUB "{ref_words[i-1]}" → "{hyp_words[j-1]}"'); i -= 1; j -= 1
        elif j > 0 and d[i][j] == d[i][j-1] + 1:
            ops.append(f'INS "{hyp_words[j-1]}"'); j -= 1
        else:
            ops.append(f'DEL "{ref_words[i-1]}"'); i -= 1
    return d[r][h], r, list(reversed(ops))


async def bench(url, wav_path, ref_path, chunk_ms, silence_ms, pace):
    import websockets

    with wave.open(wav_path, "rb") as wf:
        assert wf.getsampwidth() == 2 and wf.getframerate() == 16000
        pcm = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()

    ref = normalize(open(ref_path).read().strip())
    chunk_bytes = 16000 * 2 * chunk_ms // 1000

    ws_url = f"{url}&continuous=true&silence_ms={silence_ms}"
    print(f"Audio:      {wav_path} ({duration:.1f}s)")
    print(f"Server:     {ws_url}")
    pace_label = f"{pace}x" if pace > 0 else "no pacing"
    print(f"Chunk:      {chunk_ms}ms, silence_ms={silence_ms}, pace={pace_label}")
    print()

    segments = {}  # seq -> text
    n_partials = 0

    async with websockets.connect(ws_url) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready"

        t0 = time.monotonic()

        # Producer: send audio with ~real-time pacing
        async def sender():
            for i in range(0, len(pcm), chunk_bytes):
                chunk = pcm[i : i + chunk_bytes]
                if len(chunk) < 100:
                    break
                await ws.send(chunk)
                if pace > 0:
                    await asyncio.sleep(chunk_ms / 1000 / pace)
            await ws.send(json.dumps({"type": "end"}))

        # Consumer: collect all responses
        async def receiver():
            nonlocal n_partials
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    resp = json.loads(raw)
                    if resp["type"] == "final":
                        seq = resp.get("seq", 0)
                        segments[seq] = resp.get("text", "")
                        print(f"  [{seq}] FINAL: {resp.get('text', '')}")
                    elif resp["type"] == "partial":
                        n_partials += 1
                except asyncio.TimeoutError:
                    break

        await asyncio.gather(sender(), receiver())
        elapsed = time.monotonic() - t0

    # Combine segments
    combined = " ".join(segments[k] for k in sorted(segments.keys()) if segments[k].strip())
    hyp = normalize(combined)

    print()
    print(f"Elapsed:    {elapsed:.1f}s ({elapsed/duration:.2f}x RT)")
    print(f"Segments:   {len(segments)}")
    print(f"Partials:   {n_partials}")

    errs, ref_len, ops = compute_wer(ref.split(), hyp.split())
    print(f"WER:        {errs/ref_len*100:.1f}% ({errs}/{ref_len})")
    if ops:
        print("Errors:")
        for op in ops:
            print(f"  {op}")
    else:
        print("PERFECT ✓")


def main():
    parser = argparse.ArgumentParser(description="Benchmark stt-streaming continuous mode")
    parser.add_argument("--url", default="ws://127.0.0.1:6771/stream?lang=en")
    parser.add_argument("--wav", default="bench_data/english_16k.wav")
    parser.add_argument("--ref", default="bench_data/english.txt")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--silence-ms", type=int, default=400)
    parser.add_argument("--pace", type=float, default=2.0,
                        help="Playback speed multiplier (1.0 = real-time, 2.0 = 2x, 0 = no pacing)")
    args = parser.parse_args()
    asyncio.run(bench(args.url, args.wav, args.ref, args.chunk_ms, args.silence_ms, args.pace))


if __name__ == "__main__":
    main()
