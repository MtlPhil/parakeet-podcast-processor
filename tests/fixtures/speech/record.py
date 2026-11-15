r"""Record what the speech engines return, for tests/test_recorded_speech.py.

Makes two clips with macOS's ``say`` (English in two voices, French in one),
runs each engine on them the way p3/transcriber.py calls it, and writes
their raw output here as JSON. On an Apple Silicon Mac, from the repo root:

    uv run --no-sync --with faster-whisper --with "av<19" \
        python tests/fixtures/speech/record.py

It needs ffmpeg, and downloads faster-whisper's ``small`` model (about
480 MB) if it isn't cached; the MLX models are the ones P3 uses.
"""

import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

HOST = (
    "Welcome to Workbench Radio, I'm Maya Okafor. Today we talk about fixing old bikes."
)
GUEST = (
    "Thanks Maya. Most old bikes only need a new chain, and a steel frame "
    "lasts for decades."
)
FRENCH = (
    "Bienvenue à l'atelier. Aujourd'hui, on parle de réparer les vieux vélos "
    "au Québec."
)


def _say(text: str, voice: str, out: Path) -> None:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(aiff),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout)


def _write(name: str, data) -> None:
    (HERE / f"{name}.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False, default=float) + "\n"
    )


def main() -> None:
    import mlx_whisper
    from faster_whisper import WhisperModel
    from parakeet_mlx import from_pretrained

    from p3.settings import DEFAULT_PARAKEET_MODEL
    from p3.transcriber import _CHUNK_OVERLAP_SECONDS, _CHUNK_SECONDS, WHISPER_MODELS

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        host, guest = tmp_path / "host.wav", tmp_path / "guest.wav"
        _say(HOST, "Samantha", host)
        _say(GUEST, "Daniel", guest)
        english = tmp_path / "english.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(host),
                "-i",
                str(guest),
                "-filter_complex",
                "[0:a][1:a]concat=n=2:v=0:a=1",
                str(english),
            ],
            check=True,
        )
        french = tmp_path / "french.wav"
        _say(FRENCH, "Amélie", french)
        _write(
            "clips",
            {"english": [HOST, GUEST], "switch": _duration(host), "french": FRENCH},
        )

        whisper = WHISPER_MODELS["turbo"]
        for name, clip, language in (
            ("mlx_whisper_en", english, None),
            ("mlx_whisper_fr", french, "fr"),
        ):
            _write(
                name,
                mlx_whisper.transcribe(
                    str(clip),
                    path_or_hf_repo=whisper,
                    language=language,
                    word_timestamps=True,
                    verbose=None,
                ),
            )

        result = from_pretrained(DEFAULT_PARAKEET_MODEL).transcribe(
            str(english),
            chunk_duration=_CHUNK_SECONDS,
            overlap_duration=_CHUNK_OVERLAP_SECONDS,
        )
        _write(
            "parakeet_en",
            {
                "sentences": [
                    {
                        "text": s.text,
                        "start": s.start,
                        "end": s.end,
                        "confidence": s.confidence,
                        "tokens": [
                            {"text": t.text, "start": t.start, "end": t.end}
                            for t in s.tokens
                        ],
                    }
                    for s in result.sentences
                ]
            },
        )

        model = WhisperModel("small", device="auto", compute_type="auto")
        segments, info = model.transcribe(str(french))
        _write(
            "faster_whisper_fr",
            {
                "language": info.language,
                "segments": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text,
                        "no_speech_prob": s.no_speech_prob,
                    }
                    for s in segments
                ],
            },
        )


if __name__ == "__main__":
    main()
