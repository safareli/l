"""
stt-nemo: NeMo FastConformer speech-to-text transcription.

This module is the NeMo backend, packaged as a nix derivation via uv2nix.
It's called by the main `stt` wrapper script.

Usage as CLI:
    stt-nemo <wav_path> [--lang <en|ka>]

Prints transcription to stdout. Logs go to stderr.
"""

import argparse
import logging
import os
import sys
import warnings

NEMO_MODELS = {
    "en": "nvidia/stt_en_fastconformer_hybrid_large_pc",
    "ka": "nvidia/stt_ka_fastconformer_hybrid_large_pc",
}


def transcribe(wav_path: str, lang: str) -> str:
    """Transcribe a 16kHz mono WAV file using NeMo FastConformer."""
    model_name = NEMO_MODELS[lang]

    # Suppress NeMo's verbose logging
    os.environ["NEMO_TESTING"] = "1"
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")

    import nemo.collections.asr as nemo_asr

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
        "-l", "--lang", default="en", choices=list(NEMO_MODELS.keys()),
        help="Language (default: en)",
    )
    args = parser.parse_args()

    text = transcribe(args.wav_path, args.lang)
    print(text)


if __name__ == "__main__":
    main()
