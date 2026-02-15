"""
stt-nemo: NeMo FastConformer speech-to-text transcription.

This module is the NeMo backend, packaged as a nix derivation via uv2nix.
It's called by the main `stt` wrapper script.

Usage as CLI:
    stt-nemo <wav_path> [--lang <auto|en|ka>]

Prints transcription to stdout. Logs go to stderr.
When --lang auto (the default), uses langid_ambernet to detect the spoken
language first, then routes to the appropriate STT model.
"""

import argparse
import logging
import os
import sys
import warnings

# TODO we can try parakeet-tdt-0.6b-v3 which should be better than existing en version as well as whisper.
# TODO upgrade to langid_pearlnet when available in NeMo (newer, lower error rate).
NEMO_MODELS = {
    "en": "nvidia/stt_en_fastconformer_hybrid_large_pc",
    "ka": "nvidia/stt_ka_fastconformer_hybrid_large_pc",
}

# langid_ambernet: compact spoken language ID model trained on VoxLingua107
# (107 languages). Available in NeMo 2.1.0. langid_pearlnet is better
# (5.34% vs ~7% error rate) but requires a newer NeMo version.
LANGID_MODEL = "langid_ambernet"

SUPPORTED_LANGS = list(NEMO_MODELS.keys())


def _suppress_logging():
    """Suppress NeMo's verbose logging."""
    os.environ["NEMO_TESTING"] = "1"
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")


def detect_language(wav_path: str) -> str:
    """Detect spoken language of a 16kHz mono WAV using langid_ambernet.

    Returns an ISO 639-1 language code (e.g. 'en', 'ka').
    """
    _suppress_logging()
    import nemo.collections.asr as nemo_asr

    print(f"Detecting language with {LANGID_MODEL}...", file=sys.stderr)
    langid_model = nemo_asr.models.EncDecSpeakerLabelModel.from_pretrained(
        model_name=LANGID_MODEL
    )
    lang = langid_model.get_label(wav_path)
    print(f"Detected language: {lang}", file=sys.stderr)
    return lang


def transcribe(wav_path: str, lang: str) -> str:
    """Transcribe a 16kHz mono WAV file using NeMo FastConformer.

    If lang is "auto", detects the spoken language first via langid_pearlnet,
    then routes to the matching STT model. Falls back to English if the
    detected language has no dedicated model.
    """
    if lang == "auto":
        detected = detect_language(wav_path)
        if detected in NEMO_MODELS:
            lang = detected
        else:
            print(
                f"No STT model for detected language '{detected}', "
                f"falling back to English.",
                file=sys.stderr,
            )
            lang = "en"

    model_name = NEMO_MODELS[lang]

    _suppress_logging()
    import nemo.collections.asr as nemo_asr

    print(f"Transcribing with {lang} model...", file=sys.stderr)
    model = nemo_asr.models.EncDecHybridRNNTCTCBPEModel.from_pretrained(
        model_name=model_name
    )
    output = model.transcribe([wav_path])
    result = output[0]
    if hasattr(result, "text"):
        return result.text
    elif isinstance(result, list):
        return result[0] if result else ""
    return str(result)


def main():
    parser = argparse.ArgumentParser(prog="stt-nemo")
    parser.add_argument("wav_path", help="Path to 16kHz mono WAV file")
    parser.add_argument(
        "-l", "--lang", default="auto",
        choices=["auto"] + SUPPORTED_LANGS,
        help="Language: auto (detect), en, ka (default: auto)",
    )
    args = parser.parse_args()

    text = transcribe(args.wav_path, args.lang)
    print(text)


if __name__ == "__main__":
    main()
