#!/usr/bin/env python3
"""
Test two-pass refinement: stream audio to stt-streaming with refine=true,
verify that refined messages arrive for each utterance group.

Usage:
    cd ~/.config/home-manager/skills/stt
    uv run --python python3.11 python scripts/test_refinement.py
    uv run --python python3.11 python scripts/test_refinement.py --pace 1.0
    uv run --python python3.11 python scripts/test_refinement.py --refinement-silence-ms 1500
"""

import argparse
import asyncio
import json
import time
import wave

import websockets


def main():
    parser = argparse.ArgumentParser(description="Test two-pass refinement")
    parser.add_argument("--url", default="ws://127.0.0.1:6771/stream?lang=en")
    parser.add_argument("--wav", default="bench_data/english_16k.wav")
    parser.add_argument("--chunk-ms", type=int, default=100,
                        help="Audio chunk size in ms (default: 100)")
    parser.add_argument("--silence-ms", type=int, default=400,
                        help="Segment boundary silence (default: 400)")
    parser.add_argument("--refinement-silence-ms", type=int, default=1000,
                        help="Refinement trigger silence (default: 1000)")
    parser.add_argument("--refinement-max-ms", type=int, default=30000,
                        help="Max audio before forced refinement (default: 30000)")
    parser.add_argument("--pace", type=float, default=2.0,
                        help="Playback speed (1.0=realtime, 2.0=2x, 0=no pacing)")
    parser.add_argument("--recv-timeout", type=float, default=15.0,
                        help="Seconds to wait for final refined messages after stream ends")
    args = parser.parse_args()
    asyncio.run(run_test(args))


async def run_test(args):
    # Load audio
    with wave.open(args.wav, "rb") as wf:
        assert wf.getsampwidth() == 2 and wf.getframerate() == 16000 and wf.getnchannels() == 1, \
            f"Expected 16kHz mono 16-bit WAV, got ch={wf.getnchannels()} rate={wf.getframerate()} sw={wf.getsampwidth()}"
        pcm = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()

    chunk_bytes = 16000 * 2 * args.chunk_ms // 1000

    ws_url = (
        f"{args.url}"
        f"&continuous=true"
        f"&silence_ms={args.silence_ms}"
        f"&refine=true"
        f"&refinement_silence_ms={args.refinement_silence_ms}"
        f"&refinement_max_ms={args.refinement_max_ms}"
    )

    print(f"Audio:       {args.wav} ({duration:.1f}s)")
    print(f"WebSocket:   {ws_url}")
    print(f"Chunk:       {args.chunk_ms}ms, pace={'none' if args.pace == 0 else f'{args.pace}x'}")
    print(f"Refinement:  silence={args.refinement_silence_ms}ms, max={args.refinement_max_ms}ms")
    print()

    # Tracking
    segments = {}        # seq -> {text, final}
    refined_msgs = []    # list of refined messages
    n_partials = 0
    n_finals = 0

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        ready_raw = await ws.recv()
        ready = json.loads(ready_raw)
        assert ready["type"] == "ready", f"Expected ready, got: {ready}"
        print(f"Server ready: lang={ready.get('lang')} continuous={ready.get('continuous')} refine={ready.get('refine')}")
        if not ready.get("refine"):
            print("\n❌ ERROR: Server did not acknowledge refine=true!")
            print("   Check that stt-streaming was restarted with the new code.")
            return
        print()

        t0 = time.monotonic()

        async def sender():
            for i in range(0, len(pcm), chunk_bytes):
                chunk = pcm[i : i + chunk_bytes]
                if len(chunk) < 100:
                    break
                await ws.send(chunk)
                if args.pace > 0:
                    await asyncio.sleep(args.chunk_ms / 1000.0 / args.pace)
            await ws.send(json.dumps({"type": "end"}))

        async def receiver():
            nonlocal n_partials, n_finals
            got_end_final = False
            while True:
                timeout = args.recv_timeout if got_end_final else 120.0
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    if got_end_final:
                        break  # Expected: waited for late refined msgs
                    print("⚠ Timeout waiting for server response")
                    break

                msg = json.loads(raw)
                elapsed = time.monotonic() - t0

                if msg["type"] == "partial":
                    n_partials += 1
                    seq = msg.get("seq", 0)
                    segments[seq] = {"text": msg.get("text", ""), "final": False}

                elif msg["type"] == "final":
                    n_finals += 1
                    seq = msg.get("seq", 0)
                    text = msg.get("text", "")
                    segments[seq] = {"text": text, "final": True}
                    print(f"  [{elapsed:6.1f}s] FINAL  seq={seq}: {text[:80]}{'…' if len(text) > 80 else ''}")
                    # The final after "end" signal won't have meaningful text sometimes
                    if not text and got_end_final:
                        break
                    # Check if this looks like the end-of-stream final
                    # (comes after we sent {"type":"end"})
                    if not got_end_final:
                        got_end_final = True  # any final after sender completes

                elif msg["type"] == "refined":
                    refined_msgs.append(msg)
                    print(f"  [{elapsed:6.1f}s] 🦜 REFINED seq={msg['seq_start']}-{msg['seq_end']}: "
                          f"{msg['text'][:80]}{'…' if len(msg['text']) > 80 else ''}")
                    print(f"           model={msg.get('model')} elapsed={msg.get('elapsed_s')}s")

                elif msg["type"] == "error":
                    print(f"  [{elapsed:6.1f}s] ❌ ERROR: {msg.get('message')}")

        # Run sender first, then keep receiving
        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        await asyncio.gather(send_task, recv_task)
        total_elapsed = time.monotonic() - t0

    # ── Summary ──────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Audio duration:  {duration:.1f}s")
    print(f"Total elapsed:   {total_elapsed:.1f}s")
    print(f"Partials:        {n_partials}")
    print(f"Finals:          {n_finals}")
    print(f"Refined msgs:    {len(refined_msgs)}")

    if not refined_msgs:
        print()
        print("❌ FAIL: No refined messages received!")
        print()
        print("Possible causes:")
        print("  - Parakeet server not running (check: curl http://127.0.0.1:6774/health)")
        print("  - Refinement trigger thresholds too high")
        print("  - Bug in refinement trigger logic")
        return

    print()
    # Show refined coverage
    all_final_seqs = sorted(seq for seq, s in segments.items() if s["final"])
    refined_seqs = set()
    for r in refined_msgs:
        for s in range(r["seq_start"], r["seq_end"] + 1):
            refined_seqs.add(s)

    print(f"Final segments:  {all_final_seqs}")
    print(f"Refined seqs:    {sorted(refined_seqs)}")
    unrefined = [s for s in all_final_seqs if s not in refined_seqs]
    if unrefined:
        print(f"Unrefined seqs:  {unrefined}")
    else:
        print(f"Coverage:        100% ✓ (all finals were refined)")

    # Compare streaming vs refined text
    print()
    print("── Streaming vs Refined ──")
    for r in refined_msgs:
        streaming_text = " ".join(
            segments[s]["text"]
            for s in range(r["seq_start"], r["seq_end"] + 1)
            if s in segments and segments[s]["text"]
        )
        print(f"\n  Streaming [{r['seq_start']}-{r['seq_end']}]:")
        print(f"    {streaming_text[:120]}{'…' if len(streaming_text) > 120 else ''}")
        print(f"  Refined ({r.get('elapsed_s', '?')}s):")
        print(f"    {r['text'][:120]}{'…' if len(r['text']) > 120 else ''}")

    # Compare combined refined text against reference if available
    ref_path = args.wav.replace("_16k.wav", ".txt").replace(".wav", ".txt")
    try:
        ref_text = open(ref_path).read().strip()
    except FileNotFoundError:
        ref_text = None

    if ref_text:
        import re

        def normalize(text):
            return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", text.lower())).strip()

        # Build combined refined text (in seq order)
        refined_by_start = sorted(refined_msgs, key=lambda r: r["seq_start"])
        combined_refined = " ".join(r["text"] for r in refined_by_start)

        # Build combined streaming text
        combined_streaming = " ".join(
            segments[s]["text"]
            for s in sorted(segments.keys())
            if segments[s]["final"] and segments[s]["text"]
        )

        ref_norm = normalize(ref_text)
        ref_words = ref_norm.split()

        print()
        print("── Word Error Rate ──")

        for label, text in [("Streaming", combined_streaming), ("Refined", combined_refined)]:
            hyp_norm = normalize(text)
            hyp_words = hyp_norm.split()
            errs, ref_len, ops = _compute_wer(ref_words, hyp_words)
            wer = errs / ref_len * 100 if ref_len else 0
            print(f"\n  {label}: WER={wer:.1f}% ({errs}/{ref_len})")
            if ops:
                for op in ops[:20]:
                    print(f"    {op}")
                if len(ops) > 20:
                    print(f"    ... and {len(ops) - 20} more")

        print()
        print(f"  Reference:       {ref_text[:120]}…")
        print(f"  Refined concat:  {combined_refined[:120]}…")

    print()
    print("✅ PASS: Refinement is working!")


def _compute_wer(ref_words, hyp_words):
    """Compute WER with edit operations."""
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
    ops = []
    i, j = r, h
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref_words[i - 1] == hyp_words[j - 1]:
            i -= 1; j -= 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            ops.append(f'SUB "{ref_words[i - 1]}" → "{hyp_words[j - 1]}"')
            i -= 1; j -= 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ops.append(f'INS "{hyp_words[j - 1]}"')
            j -= 1
        else:
            ops.append(f'DEL "{ref_words[i - 1]}"')
            i -= 1
    return d[r][h], r, list(reversed(ops))


if __name__ == "__main__":
    main()
