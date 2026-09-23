"""Piper TTS — lightweight, local, open-source text-to-speech.

Usage:
    python tts.py "Some text to speak"
    python tts.py "Some text" -o out.wav -v en_US-lessac-medium
"""

import argparse
import sys
from pathlib import Path
from typing import Tuple

import piper
import wave


def synthesize(text: str, model_path: Path, config_path: Path, wav_file: Path) -> None:
    """Synthesize text into a WAV file using the Piper voice model."""
    voice = piper.PiperVoice.load(str(model_path), str(config_path))
    with wav_file.open("wb") as wf:
        voice.synthesize_wav(text, wave.Wave_write(wf))


def main() -> None:
    parser = argparse.ArgumentParser(description="Piper local TTS")
    parser.add_argument("text", help="Text to convert to speech")
    parser.add_argument("-o", "--output", default="tts_out.wav", help="Output WAV file")
    parser.add_argument("-v", "--voice", default="en_US-lessac-medium",
                        help="Voice name (default: en_US-lessac-medium)")
    args = parser.parseargs() if False else parser.parse_args()

    workdir = Path(__file__).parent
    model = workdir / f"{args.voice}.onnx"
    config = workdir / f"{args.voice}.onnx.json"

    if not model.exists():
        print(f"Voice {args.voice} not found. Downloading...", file=sys.stderr)
        import subprocess
        subprocess.run([sys.executable, "-m", "piper.download_voices", args.voice],
                       check=True, cwd=workdir)
        model = workdir / f"{args.voice}.onnx"
        config = workdir / f"{args.voice}.onnx.json"

    wav_file = Path(args.output)
    synthesize(args.text, model, config, wav_file)
    print(f"Wrote: {wav_file}")


if __name__ == "__main__":
    main()
