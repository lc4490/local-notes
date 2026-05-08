import argparse
import sys
from pathlib import Path

from faster_whisper import WhisperModel


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def transcribe(
    audio_path: str,
    model_size: str = "base",
    device: str = "auto",
    language: str | None = None,
    output_file: str | None = None,
) -> None:
    print(f"Loading model '{model_size}'...", flush=True)
    compute_type = "int8" if device == "cpu" else "float16"
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    print(f"Transcribing: {audio_path}", flush=True)
    segments, info = model.transcribe(audio_path, language=language, beam_size=5)

    print(f"Detected language: {info.language} (probability: {info.language_probability:.2f})\n")

    lines = []
    for segment in segments:
        start = format_timestamp(segment.start)
        end = format_timestamp(segment.end)
        line = f"[{start} --> {end}]  {segment.text.strip()}"
        print(line)
        lines.append(line)

    if output_file:
        Path(output_file).write_text("\n".join(lines) + "\n")
        print(f"\nTranscript saved to: {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe audio using faster-whisper")
    parser.add_argument("audio", help="Path to the audio or video file")
    parser.add_argument(
        "-m", "--model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
        help="Whisper model size (default: base)",
    )
    parser.add_argument(
        "-d", "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Device to run inference on (default: auto)",
    )
    parser.add_argument(
        "-l", "--language",
        default=None,
        help="Source language code, e.g. 'en', 'es'. Auto-detected if omitted.",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Save transcript to this file path",
    )
    args = parser.parse_args()

    if not Path(args.audio).exists():
        print(f"Error: file not found: {args.audio}", file=sys.stderr)
        sys.exit(1)

    transcribe(
        audio_path=args.audio,
        model_size=args.model,
        device=args.device,
        language=args.language,
        output_file=args.output,
    )


if __name__ == "__main__":
    main()
