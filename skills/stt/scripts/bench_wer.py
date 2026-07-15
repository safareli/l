#!/usr/bin/env python3
"""
Benchmark WER and latency of the stt-streaming WebSocket server.

Usage:
    uv run --python python3.11 python scripts/bench_wer.py
    uv run --python python3.11 python scripts/bench_wer.py --chunk-ms 200
    uv run --python python3.11 python scripts/bench_wer.py --url ws://127.0.0.1:6771/stream?lang=en
"""

import argparse
import asyncio
import json
import re
import time
import wave


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", text.lower())).strip()


def compute_wer(ref_words: list[str], hyp_words: list[str]) -> tuple[int, int, list[str]]:
    """
    Compute word error rate via Levenshtein distance.
    Returns (errors, ref_len, list_of_edit_ops).
    """
    r, h = len(ref_words), len(hyp_words)
    d = [[0] * (h + 1) for _ in range(r + 1)]
    for i in range(r + 1):
        d[i][0] = i
    for j in range(h + 1):
        d[0][j] = j
    for i in range(1, r + 1):
        for j in range(1, h + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j], d[i][j - 1], d[i - 1][j - 1])

    # Backtrace for edit operations
    ops = []
    i, j = r, h
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref_words[i - 1] == hyp_words[j - 1]:
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            ops.append(f'SUB "{ref_words[i-1]}" → "{hyp_words[j-1]}"')
            i -= 1
            j -= 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ops.append(f'INS "{hyp_words[j-1]}"')
            j -= 1
        else:
            ops.append(f'DEL "{ref_words[i-1]}"')
            i -= 1

    return d[r][h], r, list(reversed(ops))


async def bench(url: str, wav_path: str, ref_path: str, chunk_ms: int):
    import websockets

    with wave.open(wav_path, "rb") as wf:
        assert wf.getsampwidth() == 2 and wf.getframerate() == 16000 and wf.getnchannels() == 1
        pcm = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()

    ref = normalize(open(ref_path).read().strip())

    chunk_bytes = 16000 * 2 * chunk_ms // 1000  # 16kHz, 16-bit, mono

    async with websockets.connect(url) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", f"Expected ready, got: {ready}"

        t0 = time.monotonic()
        n_partials = 0
        n_chunks_sent = 0
        partial_latencies = []

        for i in range(0, len(pcm), chunk_bytes):
            chunk = pcm[i : i + chunk_bytes]
            if len(chunk) < 100:
                break
            t_send = time.monotonic()
            await ws.send(chunk)
            n_chunks_sent += 1

            # Drain available responses without blocking
            try:
                while True:
                    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.001))
                    if resp.get("type") == "partial":
                        n_partials += 1
                        partial_latencies.append(time.monotonic() - t_send)
            except (asyncio.TimeoutError, Exception):
                pass

        # Request finalization
        await ws.send(json.dumps({"type": "end"}))

        # Collect remaining partials + final
        final_text = ""
        while True:
            resp = json.loads(await ws.recv())
            if resp.get("type") == "final":
                final_text = resp["text"]
                break
            elif resp.get("type") == "partial":
                n_partials += 1

        elapsed = time.monotonic() - t0

    hyp = normalize(final_text)
    errs, ref_len, ops = compute_wer(ref.split(), hyp.split())

    print(f"Audio:     {wav_path} ({duration:.1f}s)")
    print(f"Server:    {url}")
    print(f"Chunk:     {chunk_ms}ms ({chunk_bytes} bytes)")
    print(f"Elapsed:   {elapsed:.1f}s ({elapsed/duration:.2f}x RT)")
    print(f"Chunks:    {n_chunks_sent} sent, {n_partials} partials received")
    if partial_latencies:
        import statistics

        print(
            f"Latency:   avg={statistics.mean(partial_latencies)*1000:.0f}ms "
            f"p50={statistics.median(partial_latencies)*1000:.0f}ms "
            f"p95={sorted(partial_latencies)[int(len(partial_latencies)*0.95)]*1000:.0f}ms"
        )
    print(f"WER:       {errs/ref_len*100:.1f}% ({errs}/{ref_len})")
    if ops:
        print("Errors:")
        for op in ops:
            print(f"  {op}")
    else:
        print("PERFECT ✓")


def main():
    parser = argparse.ArgumentParser(description="Benchmark stt-streaming WER and latency")
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:6771/stream?lang=en",
        help="WebSocket URL (default: ws://127.0.0.1:6771/stream?lang=en)",
    )
    parser.add_argument(
        "--wav",
        default="bench_data/english_16k.wav",
        help="Path to 16kHz mono WAV file (default: bench_data/english_16k.wav)",
    )
    parser.add_argument(
        "--ref",
        default="bench_data/english.txt",
        help="Path to reference transcript (default: bench_data/english.txt)",
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=100,
        help="Client chunk size in milliseconds (default: 100)",
    )
    args = parser.parse_args()
    asyncio.run(bench(args.url, args.wav, args.ref, args.chunk_ms))


if __name__ == "__main__":
    main()
