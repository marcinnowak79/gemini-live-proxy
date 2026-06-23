#!/usr/bin/env python3
"""Small offline speaker-identification PoC for captured Voice PE WAV files.

Expected input layout:

  speaker_samples/
    Marcin/*.wav
    Ania/*.wav
    Gosia/*.wav

The script computes one centroid embedding per speaker and then prints
leave-one-out matches for each sample.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"


@dataclass
class SampleEmbedding:
    speaker: str
    path: Path
    embedding: np.ndarray


def import_speechbrain():
    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency. Install with:\n"
            "  venv/bin/python -m pip install speechbrain torchaudio"
        ) from exc
    return EncoderClassifier


def normalize(vec: np.ndarray) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(normalize(a), normalize(b)))


def embed_file(classifier, path: Path) -> np.ndarray:
    emb = classifier.encode_file(str(path))
    return normalize(emb.detach().cpu().numpy())


def load_samples(root: Path, classifier) -> list[SampleEmbedding]:
    samples: list[SampleEmbedding] = []
    for speaker_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        speaker = speaker_dir.name
        for path in sorted(speaker_dir.glob("*.wav")):
            samples.append(SampleEmbedding(speaker=speaker, path=path, embedding=embed_file(classifier, path)))
    return samples


def centroid(samples: list[SampleEmbedding]) -> np.ndarray:
    return normalize(np.mean([s.embedding for s in samples], axis=0))


def leave_one_out(samples: list[SampleEmbedding]) -> list[dict]:
    rows: list[dict] = []
    for sample in samples:
        speakers = sorted({s.speaker for s in samples})
        scores: dict[str, float] = {}
        for speaker in speakers:
            enroll = [s for s in samples if s.speaker == speaker and s.path != sample.path]
            if not enroll:
                continue
            scores[speaker] = cosine(sample.embedding, centroid(enroll))

        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_speaker, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else -1.0
        rows.append(
            {
                "path": str(sample.path),
                "actual": sample.speaker,
                "predicted": best_speaker,
                "score": best_score,
                "margin": best_score - second_score,
                "ok": best_speaker == sample.speaker,
            }
        )
    return rows


def write_profiles(samples: list[SampleEmbedding], output: Path) -> None:
    profiles = {}
    for speaker in sorted({s.speaker for s in samples}):
        profiles[speaker] = centroid([s for s in samples if s.speaker == speaker]).tolist()
    output.write_text(json.dumps({"model": MODEL_ID, "profiles": profiles}, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("speaker_samples"))
    parser.add_argument("--profiles", type=Path, default=Path("speaker_profiles.json"))
    args = parser.parse_args()

    if not args.samples.exists():
        raise SystemExit(f"Missing samples directory: {args.samples}")

    EncoderClassifier = import_speechbrain()
    classifier = EncoderClassifier.from_hparams(source=MODEL_ID, savedir="models/spkrec-ecapa-voxceleb")
    samples = load_samples(args.samples, classifier)
    if len({s.speaker for s in samples}) < 2:
        raise SystemExit("Need samples for at least two speakers.")

    rows = leave_one_out(samples)
    ok = sum(1 for row in rows if row["ok"])
    total = len(rows)
    for row in rows:
        status = "OK" if row["ok"] else "MISS"
        print(
            f"{status:4} actual={row['actual']:<12} predicted={row['predicted']:<12} "
            f"score={row['score']:.3f} margin={row['margin']:.3f} file={Path(row['path']).name}"
        )

    print(f"\nAccuracy: {ok}/{total} ({(ok / total * 100.0) if total else 0.0:.1f}%)")
    write_profiles(samples, args.profiles)
    print(f"Wrote profiles: {args.profiles}")


if __name__ == "__main__":
    main()
