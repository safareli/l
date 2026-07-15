#!/usr/bin/env bash
# STT Benchmark: Whisper (English) vs NeMo FastConformer (Georgian)
#
# Generates audio via TTS, then transcribes with Whisper and NeMo.
# Requires: tts, whisper-cli, stt-nemo (.venv), ffmpeg, ffprobe
#
# Usage: cd skills/stt && bash bench/bench.sh
#
# ┌──────────────────────────────────────────────────────────────────────────────────┐
# │                        STT Benchmark Results (2026-02-14)                        │
# │                        Machine: Ubuntu Server, no GPU, CPU only                  │
# ├──────────────┬──────────────────────┬──────────────────────┬─────────────────────┤
# │              │ EN: Whisper          │ EN: NeMo             │ KA: NeMo            │
# ├──────────────┼──────────────────────┼──────────────────────┼─────────────────────┤
# │ Audio dur.   │ 81.15s               │ 81.15s               │ 90.13s              │
# │ STT time     │ 27,036ms             │ 12,468ms             │ 15,432ms            │
# │ RTF          │ 0.33x               │ 0.15x                │ 0.17x               │
# │ Model        │ whisper small.en     │ fastconformer lg     │ fastconformer lg    │
# │ Params       │ 244M                 │ 115M                 │ 115M                │
# │ Runtime      │ whisper-cpp (CPU)    │ NeMo/PyTorch (CPU)   │ NeMo/PyTorch (CPU)  │
# ├──────────────┴──────────────────────┴──────────────────────┴─────────────────────┤
# │ Notes:                                                                           │
# │ - NeMo FastConformer is ~2x faster than whisper-cpp on CPU                       │
# │ - NeMo EN quality slightly better (fewer dropped words than Whisper)             │
# │ - NeMo KA quality excellent for 115M model (~5.7% WER on MCV test)              │
# │ - Whisper has no meaningful Georgian support                                     │
# │ - Times include model loading (~5s) but not first-time download (~460MB each)    │
# └──────────────────────────────────────────────────────────────────────────────────┘

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PARENT_DIR/bench_data"
STT_NEMO="$PARENT_DIR/.venv/bin/stt-nemo"
mkdir -p "$OUT_DIR"

# Colors
G='\033[0;32m' Y='\033[1;33m' C='\033[0;36m' R='\033[0m'

header() { echo -e "\n${Y}━━━ $1 ━━━${R}"; }
info()   { echo -e "${C}$1${R}"; }
ok()     { echo -e "${G}$1${R}"; }

time_cmd() {
    local start end
    start=$(date +%s%N)
    "$@"
    end=$(date +%s%N)
    echo $(( (end - start) / 1000000 ))
}

# ── Inline bench texts ───────────────────────────────────────────────

read -r -d '' EN_TEXT_CONTENT <<'EOF' || true
The history of artificial intelligence began in the nineteen fifties when researchers first explored the idea of creating machines that could think. Early pioneers like Alan Turing asked whether machines could truly reason, and proposed tests to measure their intelligence. Over the following decades, the field experienced cycles of excitement and disappointment, often called winters and summers. In the nineteen eighties, expert systems showed promise but eventually hit their limits. The real breakthrough came in the twenty tens with deep learning, when neural networks trained on massive datasets began to outperform humans on specific tasks. Today, large language models can write essays, translate languages, and even generate computer code. Speech recognition has improved dramatically, allowing people to dictate messages, control smart devices, and transcribe meetings with remarkable accuracy. Despite these advances, many challenges remain. Understanding context, reasoning about the world, and handling ambiguity are still difficult problems. Researchers continue to push the boundaries, working toward systems that can truly understand and interact with the world around them. The journey from simple rule based programs to modern neural networks has been long and fascinating, and the best may still be yet to come.
EOF

read -r -d '' KA_TEXT_CONTENT <<'EOF' || true
ხელოვნური ინტელექტის ისტორია მეოცე საუკუნის შუა წლებში დაიწყო, როდესაც მეცნიერებმა პირველად განიხილეს აზროვნების უნარის მქონე მანქანების შექმნის იდეა. ადრეულმა პიონერებმა, მაგალითად ალან ტურინგმა, იკითხეს, შეუძლიათ თუ არა მანქანებს ჭეშმარიტი მსჯელობა და შემოგვთავაზეს ტესტები მათი ინტელექტის გასაზომად. შემდგომი ათწლეულების განმავლობაში ეს სფერო აღფრთოვანებისა და იმედგაცრუების ციკლებს განიცდიდა. მეოცე საუკუნის ოთხმოციან წლებში ექსპერტულმა სისტემებმა პერსპექტივა აჩვენეს, მაგრამ საბოლოოდ თავიანთ შეზღუდვებს წააწყდნენ. ნამდვილი გარღვევა ოცდამეერთე საუკუნის მეორე ათწლეულში მოხდა ღრმა სწავლების გამოჩენით, როდესაც ნეირონულმა ქსელებმა, რომლებიც უზარმაზარ მონაცემთა ნაკრებებზე იყვნენ გაწვრთნილნი, კონკრეტულ ამოცანებში ადამიანებს გადააჭარბეს. დღეს დიდი ენობრივი მოდელები წერენ ესეებს, თარგმნიან ენებს და კომპიუტერის კოდსაც კი ქმნიან. მეტყველების ამოცნობა მნიშვნელოვნად გაუმჯობესდა, რაც ადამიანებს საშუალებას აძლევს შეტყობინებები კარნახით შეიტანონ, ჭკვიანი მოწყობილობები მართონ და შეხვედრები საოცარი სიზუსტით ჩაიწერონ. მიუხედავად ამ მიღწევებისა, ბევრი გამოწვევა რჩება. კონტექსტის გაგება, სამყაროს შესახებ მსჯელობა და ორაზროვნების დამუშავება კვლავ რთული პრობლემებია. მკვლევარები განაგრძობენ საზღვრების გაფართოებას და მუშაობენ სისტემებზე, რომლებსაც ჭეშმარიტად შეუძლიათ გარემომცველი სამყაროს გაგება და მასთან ურთიერთქმედება.
EOF

# ══════════════════════════════════════════════════════════════════════
header "Step 1: Generate audio with TTS"
# ══════════════════════════════════════════════════════════════════════

EN_TEXT_FILE="$OUT_DIR/english.txt"
KA_TEXT_FILE="$OUT_DIR/georgian.txt"
EN_WAV="$OUT_DIR/english.wav"
KA_WAV="$OUT_DIR/georgian.wav"

echo "$EN_TEXT_CONTENT" > "$EN_TEXT_FILE"
echo "$KA_TEXT_CONTENT" > "$KA_TEXT_FILE"

if [ ! -f "$EN_WAV" ]; then
    info "Generating English audio..."
    ms=$(time_cmd tts --file "$EN_TEXT_FILE" -o "$EN_WAV" --voice af_bella)
    ok "English TTS: ${ms}ms"
else
    info "English audio exists, skipping TTS"
fi

if [ ! -f "$KA_WAV" ]; then
    info "Generating Georgian audio..."
    ms=$(time_cmd tts --file "$KA_TEXT_FILE" -o "$KA_WAV")
    ok "Georgian TTS: ${ms}ms"
else
    info "Georgian audio exists, skipping TTS"
fi

# Show audio durations
for f in "$EN_WAV" "$KA_WAV"; do
    dur=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
    info "  $(basename "$f"): ${dur}s"
done

# ══════════════════════════════════════════════════════════════════════
header "Step 2: STT — Whisper (English)"
# ══════════════════════════════════════════════════════════════════════

WHISPER_OUT="$OUT_DIR/whisper_english.txt"

info "Transcribing English with whisper-cpp (small.en)..."
WHISPER_WAV="$OUT_DIR/english_16k.wav"
ffmpeg -y -i "$EN_WAV" -ar 16000 -ac 1 "$WHISPER_WAV" 2>/dev/null

start=$(date +%s%N)
whisper-cli -m "$WHISPER_MODEL_PATH" -f "$WHISPER_WAV" --no-timestamps -np > "$WHISPER_OUT" 2>/dev/null
end=$(date +%s%N)
WHISPER_MS=$(( (end - start) / 1000000 ))

WHISPER_RESULT=$(cat "$WHISPER_OUT" | sed 's/^[[:space:]]*//' | tr -s ' ')
ok "Whisper done: ${WHISPER_MS}ms"

# ══════════════════════════════════════════════════════════════════════
header "Step 3: STT — NeMo FastConformer (English)"
# ══════════════════════════════════════════════════════════════════════

NEMO_EN_OUT="$OUT_DIR/nemo_english.txt"

info "Transcribing English with NeMo FastConformer (large, 115M)..."

start=$(date +%s%N)
"$STT_NEMO" "$EN_WAV" --lang en > "$NEMO_EN_OUT"
end=$(date +%s%N)
NEMO_EN_MS=$(( (end - start) / 1000000 ))

NEMO_EN_RESULT=$(cat "$NEMO_EN_OUT")
ok "NeMo EN done: ${NEMO_EN_MS}ms"

# ══════════════════════════════════════════════════════════════════════
header "Step 4: STT — NeMo FastConformer (Georgian)"
# ══════════════════════════════════════════════════════════════════════

NEMO_OUT="$OUT_DIR/nemo_georgian.txt"

info "Transcribing Georgian with NeMo FastConformer (large, 115M)..."

start=$(date +%s%N)
"$STT_NEMO" "$KA_WAV" --lang ka > "$NEMO_OUT"
end=$(date +%s%N)
NEMO_MS=$(( (end - start) / 1000000 ))

NEMO_RESULT=$(cat "$NEMO_OUT")
ok "NeMo done: ${NEMO_MS}ms"

# ══════════════════════════════════════════════════════════════════════
header "Results"
# ══════════════════════════════════════════════════════════════════════

EN_DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$EN_WAV" 2>/dev/null)
KA_DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$KA_WAV" 2>/dev/null)

EN_WHISPER_RTF=$(python3 -c "print(f'{$WHISPER_MS / 1000 / $EN_DUR:.2f}')")
EN_NEMO_RTF=$(python3 -c "print(f'{$NEMO_EN_MS / 1000 / $EN_DUR:.2f}')")
KA_RTF=$(python3 -c "print(f'{$NEMO_MS / 1000 / $KA_DUR:.2f}')")

echo ""
echo "┌──────────────────────────────────────────────────────────────────────────────────┐"
echo "│                            STT Benchmark Results                                 │"
echo "├──────────────┬──────────────────────┬──────────────────────┬─────────────────────┤"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "" "EN: Whisper" "EN: NeMo" "KA: NeMo"
echo "├──────────────┼──────────────────────┼──────────────────────┼─────────────────────┤"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "Audio dur." "${EN_DUR}s" "${EN_DUR}s" "${KA_DUR}s"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "STT time" "${WHISPER_MS}ms" "${NEMO_EN_MS}ms" "${NEMO_MS}ms"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "RTF" "${EN_WHISPER_RTF}x" "${EN_NEMO_RTF}x" "${KA_RTF}x"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "Model" "whisper small.en" "fastconformer lg" "fastconformer lg"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "Params" "244M" "115M" "115M"
printf "│ %-12s │ %-20s │ %-20s │ %-19s │\n" "Runtime" "whisper-cpp (CPU)" "NeMo/PyTorch (CPU)" "NeMo/PyTorch (CPU)"
echo "└──────────────┴──────────────────────┴──────────────────────┴─────────────────────┘"

echo ""
header "Transcriptions"

echo -e "\n${C}── English (original):${R}"
echo "$EN_TEXT_CONTENT"
echo -e "\n${G}── English (Whisper):${R}"
echo "$WHISPER_RESULT"
echo -e "\n${G}── English (NeMo):${R}"
echo "$NEMO_EN_RESULT"

echo -e "\n${C}── Georgian (original):${R}"
echo "$KA_TEXT_CONTENT"
echo -e "\n${G}── Georgian (NeMo):${R}"
echo "$NEMO_RESULT"
