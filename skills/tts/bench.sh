#!/usr/bin/env bash
set -euo pipefail

# TTS Performance Benchmark: Kokoro vs Piper
#
# Compares wall-clock time and real-time factor (RTF) for both engines
# using the same 150-word English paragraph (~1 min of spoken audio).
#
# Results (2026-02-14, CPU-only, Ubuntu Server):
#
#   Engine       Wall(s)  Audio(s)     RTF
#   --------     -------  --------   -----
#   Kokoro         81.05     52.83   1.534
#   Piper           2.78     50.09   0.056
#
#   Piper is ~29x faster in wall-clock time.
#   Kokoro is slower than real-time (RTF > 1).
#   Piper generates 50s of audio in under 3s (RTF = 0.056).
#   Tradeoff: Kokoro produces more natural prosody; Piper is much faster.

# ~150 words ≈ 1 minute of spoken audio
TEXT="The art of storytelling has been a fundamental part of human civilization for thousands of years. Long before the invention of writing, people gathered around fires to share tales of adventure, wisdom, and wonder. These stories served not only as entertainment but also as a way to pass down knowledge from one generation to the next. Every culture on Earth has developed its own rich tradition of oral narrative, from the epic poems of ancient Greece to the folk tales of West Africa. Today, storytelling continues to evolve through books, films, podcasts, and digital media. Yet the core purpose remains the same: to connect us with each other, to help us make sense of the world around us, and to remind us of our shared humanity. Whether spoken aloud by a campfire or streamed across the globe, a good story has the power to inspire, to challenge, and to transform."

OUTDIR="/tmp/tts-perf-test"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

echo "============================================"
echo "  TTS Performance Test: Kokoro vs Piper"
echo "============================================"
echo ""
echo "Text length: $(echo "$TEXT" | wc -w) words"
echo ""

# --- Kokoro (default) ---
echo ">>> Kokoro (af_heart, default voice)..."
KOKORO_START=$(date +%s%N)
KOKORO_OUT=$(echo "$TEXT" | tts --outdir "$OUTDIR" -o "$OUTDIR/kokoro.wav" 2>/dev/null)
KOKORO_END=$(date +%s%N)
KOKORO_MS=$(( (KOKORO_END - KOKORO_START) / 1000000 ))
KOKORO_SEC=$(awk "BEGIN{printf \"%.2f\", $KOKORO_MS/1000}")
KOKORO_DUR=$(soxi -D "$OUTDIR/kokoro.wav" 2>/dev/null || ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTDIR/kokoro.wav")
echo "   Wall time : ${KOKORO_SEC}s"
echo "   Audio dur : ${KOKORO_DUR}s"
echo "   RTF       : $(awk "BEGIN{printf \"%.3f\", $KOKORO_SEC/$KOKORO_DUR}")"
echo ""

# --- Piper (en_US-lessac-medium) ---
echo ">>> Piper (en_US-lessac-medium)..."
PIPER_START=$(date +%s%N)
PIPER_OUT=$(echo "$TEXT" | tts --piper --piper-voice en_US-lessac-medium --outdir "$OUTDIR" -o "$OUTDIR/piper.wav" 2>/dev/null)
PIPER_END=$(date +%s%N)
PIPER_MS=$(( (PIPER_END - PIPER_START) / 1000000 ))
PIPER_SEC=$(awk "BEGIN{printf \"%.2f\", $PIPER_MS/1000}")
PIPER_DUR=$(soxi -D "$OUTDIR/piper.wav" 2>/dev/null || ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTDIR/piper.wav")
echo "   Wall time : ${PIPER_SEC}s"
echo "   Audio dur : ${PIPER_DUR}s"
echo "   RTF       : $(awk "BEGIN{printf \"%.3f\", $PIPER_SEC/$PIPER_DUR}")"
echo ""

# --- Summary ---
echo "============================================"
echo "  Summary"
echo "============================================"
printf "  %-10s  %8s  %8s  %6s\n" "Engine" "Wall(s)" "Audio(s)" "RTF"
printf "  %-10s  %8s  %8s  %6s\n" "--------" "-------" "-------" "-----"
printf "  %-10s  %8s  %8s  %6s\n" "Kokoro" "$KOKORO_SEC" "$KOKORO_DUR" "$(awk "BEGIN{printf \"%.3f\", $KOKORO_SEC/$KOKORO_DUR}")"
printf "  %-10s  %8s  %8s  %6s\n" "Piper" "$PIPER_SEC" "$PIPER_DUR" "$(awk "BEGIN{printf \"%.3f\", $PIPER_SEC/$PIPER_DUR}")"
echo ""
SPEEDUP=$(awk "BEGIN{printf \"%.1f\", $KOKORO_SEC/$PIPER_SEC}")
echo "  Piper is ${SPEEDUP}x faster in wall-clock time vs Kokoro"
echo ""
echo "  Output files:"
echo "    Kokoro: $OUTDIR/kokoro.wav"
echo "    Piper : $OUTDIR/piper.wav"
