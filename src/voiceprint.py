from __future__ import annotations

"""
Personal voiceprint markers for «ИА» and «прием».

Enrollment stores your spoken exemplars; runtime matches live audio
via log-mel embeddings + cosine similarity (no cloud, no Whisper for markers).
"""

import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "models" / "voiceprint"


def _hz_to_mel(hz: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    f_max = sr / 2.0
    mels = np.linspace(_hz_to_mel(np.array([0.0]))[0], _hz_to_mel(np.array([f_max]))[0], n_mels + 2)
    hz = _mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for i in range(n_mels):
        left, center, right = bins[i], bins[i + 1], bins[i + 2]
        if center == left:
            center += 1
        if right == center:
            right += 1
        for j in range(left, center):
            if 0 <= j < fb.shape[1]:
                fb[i, j] = (j - left) / max(1, center - left)
        for j in range(center, right):
            if 0 <= j < fb.shape[1]:
                fb[i, j] = (right - j) / max(1, right - center)
    return fb


def logmel_embed(
    audio: np.ndarray,
    sr: int = 16000,
    n_fft: int = 512,
    hop: int = 160,
    n_mels: int = 40,
) -> np.ndarray:
    """Fixed-size L2-normalized embedding from log-mel mean/std."""
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if x.size < n_fft:
        x = np.pad(x, (0, n_fft - x.size))
    # pre-emphasis
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])
    window = np.hanning(n_fft)
    frames = []
    for start in range(0, x.size - n_fft + 1, hop):
        frame = x[start : start + n_fft] * window
        spec = np.abs(np.fft.rfft(frame)) ** 2
        frames.append(spec)
    if not frames:
        frames = [np.abs(np.fft.rfft(x[:n_fft] * window)) ** 2]
    spec = np.stack(frames, axis=0)
    fb = _mel_filterbank(sr, n_fft, n_mels)
    mel = spec @ fb.T
    mel = np.log(mel + 1e-6)
    mean = mel.mean(axis=0)
    std = mel.std(axis=0)
    feat = np.concatenate([mean, std]).astype(np.float32)
    nrm = np.linalg.norm(feat) + 1e-9
    return feat / nrm


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) + 1e-9) * (np.linalg.norm(b) + 1e-9)))


def write_wav(path: Path, audio: np.ndarray, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    data = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
        ch = w.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return audio, sr


@dataclass
class MarkerModel:
    name: str
    embeddings: np.ndarray  # (N, D)
    threshold: float

    def score(self, audio: np.ndarray, sr: int = 16000) -> float:
        emb = logmel_embed(audio, sr=sr)
        sims = self.embeddings @ emb
        # max match against any exemplar (best for variable delivery)
        return float(np.max(sims))

    def match(self, audio: np.ndarray, sr: int = 16000) -> bool:
        return self.score(audio, sr=sr) >= self.threshold


@dataclass
class VoiceprintBank:
    ia: MarkerModel
    priem: MarkerModel
    sample_rate: int = 16000
    root: Path | None = None

    def score_clip(self, audio: np.ndarray) -> dict[str, float]:
        return {
            "ia": self.ia.score(audio, self.sample_rate),
            "priem": self.priem.score(audio, self.sample_rate),
        }

    def scan(
        self,
        audio: np.ndarray,
        win_s: float = 0.55,
        hop_s: float = 0.10,
    ) -> dict[str, float | int | None]:
        """
        Sliding-window scan for markers inside a longer utterance.
        Returns best scores and sample offsets (start index) for each marker.
        """
        sr = self.sample_rate
        win = max(1, int(win_s * sr))
        hop = max(1, int(hop_s * sr))
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size <= win:
            sc = self.score_clip(x)
            return {
                "ia": sc["ia"],
                "priem": sc["priem"],
                "ia_at": 0 if sc["ia"] >= self.ia.threshold else None,
                "priem_at": 0 if sc["priem"] >= self.priem.threshold else None,
            }

        best_ia, best_pr = -1.0, -1.0
        at_ia, at_pr = None, None
        for start in range(0, x.size - win + 1, hop):
            chunk = x[start : start + win]
            s_ia = self.ia.score(chunk, sr)
            s_pr = self.priem.score(chunk, sr)
            if s_ia > best_ia:
                best_ia, at_ia = s_ia, start
            if s_pr > best_pr:
                best_pr, at_pr = s_pr, start
        return {
            "ia": best_ia,
            "priem": best_pr,
            "ia_at": at_ia if best_ia >= self.ia.threshold else None,
            "priem_at": at_pr if best_pr >= self.priem.threshold else None,
        }

    def save(self, root: Path | None = None) -> None:
        root = root or self.root or DEFAULT_DIR
        root.mkdir(parents=True, exist_ok=True)
        meta = {
            "sample_rate": self.sample_rate,
            "ia_threshold": self.ia.threshold,
            "priem_threshold": self.priem.threshold,
            "ia_count": int(self.ia.embeddings.shape[0]),
            "priem_count": int(self.priem.embeddings.shape[0]),
        }
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        np.savez_compressed(
            root / "embeddings.npz",
            ia=self.ia.embeddings,
            priem=self.priem.embeddings,
        )
        log.info("Saved voiceprint bank → %s", root)

    @classmethod
    def load(cls, root: Path | None = None) -> "VoiceprintBank | None":
        root = root or DEFAULT_DIR
        npz_path = root / "embeddings.npz"
        meta_path = root / "meta.json"
        if not npz_path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        data = np.load(str(npz_path))
        ia = MarkerModel("ia", data["ia"], float(meta["ia_threshold"]))
        priem = MarkerModel("priem", data["priem"], float(meta["priem_threshold"]))
        return cls(ia=ia, priem=priem, sample_rate=int(meta.get("sample_rate", 16000)), root=root)


def build_bank_from_wavs(
    ia_wavs: list[Path],
    priem_wavs: list[Path],
    sr: int = 16000,
    margin: float = 0.08,
) -> VoiceprintBank:
    def _embs(paths: list[Path]) -> np.ndarray:
        out = []
        for p in paths:
            audio, file_sr = read_wav(p)
            if file_sr != sr:
                # naive resample
                duration = audio.size / file_sr
                n = max(1, int(duration * sr))
                audio = np.interp(np.linspace(0, 1, n, endpoint=False), np.linspace(0, 1, audio.size, endpoint=False), audio).astype(
                    np.float32
                )
            out.append(logmel_embed(audio, sr=sr))
        return np.stack(out, axis=0)

    ia_e = _embs(ia_wavs)
    pr_e = _embs(priem_wavs)

    def _auto_threshold(embs: np.ndarray) -> float:
        # Self-similarity floor: mean pairwise cosine among exemplars, minus margin
        sims = embs @ embs.T
        n = embs.shape[0]
        if n == 1:
            return 0.72
        mask = ~np.eye(n, dtype=bool)
        mean_self = float(sims[mask].mean())
        thr = max(0.55, min(0.92, mean_self - margin))
        return thr

    ia_thr = _auto_threshold(ia_e)
    pr_thr = _auto_threshold(pr_e)
    # Cross-talk: ensure thresholds aren't below opposite-class max too tightly
    cross_ia = float(np.max(pr_e @ ia_e.mean(axis=0)))
    cross_pr = float(np.max(ia_e @ pr_e.mean(axis=0)))
    ia_thr = max(ia_thr, cross_ia + 0.05)
    pr_thr = max(pr_thr, cross_pr + 0.05)
    # Cosine ≤ 1.0 — never set unreachable thresholds (bug after large enroll)
    ia_thr = float(min(0.92, max(0.55, ia_thr)))
    pr_thr = float(min(0.92, max(0.55, pr_thr)))

    return VoiceprintBank(
        ia=MarkerModel("ia", ia_e, ia_thr),
        priem=MarkerModel("priem", pr_e, pr_thr),
        sample_rate=sr,
    )
