import argparse
import sys
import queue

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000   # Whisper expects 16 kHz mono
CHUNK_SECONDS = 5     # transcribe every N seconds of audio


def listen(
    model_size: str = "tiny",
    device: str = "auto",
    language: str | None = None,
    chunk_seconds: int = CHUNK_SECONDS,
) -> None:
    compute_type = "int8" if device in ("cpu", "auto") else "float16"
    print(f"Loading model '{model_size}'...", flush=True)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    audio_queue: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            print(f"[audio status] {status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    samples_needed = SAMPLE_RATE * chunk_seconds
    buffer: list[np.ndarray] = []

    print(f"Listening (chunk size: {chunk_seconds}s) — press Ctrl+C to stop.\n", flush=True)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback):
        try:
            while True:
                chunk = audio_queue.get()
                buffer.append(chunk)

                total = sum(c.shape[0] for c in buffer)
                if total < samples_needed:
                    continue

                audio = np.concatenate(buffer, axis=0).flatten()
                buffer = []

                segments, _ = model.transcribe(
                    audio,
                    language=language,
                    beam_size=5,
                    vad_filter=True,          # skip silent regions
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                text = " ".join(seg.text.strip() for seg in segments)
                if text:
                    print(text, flush=True)

        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live microphone transcription via faster-whisper")
    parser.add_argument(
        "-m", "--model",
        default="tiny",
        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
        help="Whisper model size (default: tiny)",
    )
    parser.add_argument(
        "-d", "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Device to run on (default: auto)",
    )
    parser.add_argument(
        "-l", "--language",
        default=None,
        help="Source language code, e.g. 'en'. Auto-detected if omitted.",
    )
    parser.add_argument(
        "-c", "--chunk",
        type=int,
        default=CHUNK_SECONDS,
        help=f"Seconds of audio per transcription chunk (default: {CHUNK_SECONDS})",
    )
    args = parser.parse_args()

    listen(
        model_size=args.model,
        device=args.device,
        language=args.language,
        chunk_seconds=args.chunk,
    )


if __name__ == "__main__":
    main()
