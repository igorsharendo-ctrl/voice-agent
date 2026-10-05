from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable


def normalize_text(text: str) -> str:
    """Normalize for matching; keep commas; keep dots inside tokens (e.g. main.py)."""
    t = text.strip().lower().replace("ё", "е")
    t = t.replace("\u00a0", " ")
    t = re.sub(r"[\"'`«»„“”]", "", t)
    # Keep commas in command text; flatten only dashes / ;:!?
    t = re.sub(r"[…\-—–]+", " ", t)
    t = re.sub(r"[;:!?]+", " ", t)
    # Whisper often puts «ИА.» / «прием.» — turn sentence-final dots into spaces,
    # but keep file-like tokens: main.py, v1.2
    t = re.sub(r"(?<![\w])\.(?!\w)", " ", t)  # lonely '.'
    t = re.sub(r"(?<=\w)\.(?=\s|$)", " ", t)  # word-final '.'
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _trim(text: str) -> str:
    return re.sub(r"^\.\s*", "", text).strip().strip(" .,;:!?…")


# Boundary after a wake/end token: space, end, or punctuation Whisper likes to add
_AFTER = r"(?=\s|$|[.,!?;:])"


def _skip_punct(text: str, i: int) -> int:
    while i < len(text) and text[i] in ".,!?;:":
        i += 1
    while i < len(text) and text[i] == " ":
        i += 1
    return i


# Whisper often hallucinates this on short/noisy clips.
_HALLUCINATIONS = (
    "субтитры сделал dimatorzok",
    "субтитры создавал dimatorzok",
    "продолжение следует",
    "спасибо за просмотр",
    "иа прием",
    "иа приём",
    "ia прием",
)


def is_hallucination(transcript: str) -> bool:
    n = normalize_text(transcript)
    return any(h in n for h in _HALLUCINATIONS) or n in _HALLUCINATIONS


@dataclass
class ParseResult:
    """Result of feeding one transcript into the wake/end protocol."""

    kind: str  # ignore | waiting | command
    command: str | None = None
    wake_phrase: str | None = None
    partial: str = ""


def _find_wake(norm: str, wake_phrases: list[str]) -> tuple[str, int, int] | None:
    """Return (phrase, start, end_index_after_phrase) for the earliest wake match."""
    best: tuple[str, int, int] | None = None
    for phrase in wake_phrases:
        if not phrase:
            continue
        pattern = rf"(?:^|\s)({re.escape(phrase)}){_AFTER}"
        for m in re.finditer(pattern, norm):
            start, end = m.start(1), _skip_punct(norm, m.end(1))
            if start > 12:
                continue
            if best is None or start < best[1] or (start == best[1] and end > best[2]):
                best = (phrase, start, end)
    return best


def _split_end(text: str, end_tokens: list[str]) -> tuple[str, bool]:
    """
    Split on the first end token.
    Returns (text_before_end, completed).
    """
    earliest = None
    for tok in end_tokens:
        if not tok:
            continue
        pattern = rf"(?:^|\s)({re.escape(tok)}){_AFTER}"
        m = re.search(pattern, text)
        if not m:
            continue
        if earliest is None or m.start(1) < earliest[0]:
            earliest = (m.start(1), m.end(1))
    if earliest is None:
        return _trim(text), False
    before = _trim(text[: earliest[0]])
    return before, True


def parse_utterance(
    transcript: str,
    wake_phrases: Iterable[str],
    end_tokens: Iterable[str],
) -> tuple[bool, str, bool, str | None]:
    """
    Parse a single utterance.
    Returns (wake_found, payload_after_wake, completed_with_end, wake_phrase).
    """
    raw = transcript.strip()
    if not raw:
        return False, "", False, None
    norm = normalize_text(raw)
    wakes = sorted({normalize_text(p) for p in wake_phrases if p}, key=len, reverse=True)
    ends = sorted({normalize_text(t) for t in end_tokens if t}, key=len, reverse=True)

    hit = _find_wake(norm, wakes)
    if not hit:
        payload, done = _split_end(norm, ends)
        return False, payload, done, None

    phrase, _start, end = hit
    after = _trim(norm[end:])
    payload, done = _split_end(after, ends)
    return True, payload, done, phrase


def extract_ptt_command(
    transcript: str,
    wake_phrases: Iterable[str] | None = None,
    end_tokens: Iterable[str] | None = None,
) -> str:
    """
    Push-to-talk: the whole clip is the command.
    Optionally strip habitual wake («ИА») / end («прием») if the user still says them.
    Returns empty string when nothing useful remains (do NOT fall back to raw transcript).
    """
    raw = (transcript or "").strip()
    if not raw:
        return ""
    wakes = list(wake_phrases or [])
    ends = list(end_tokens or ["прием", "приём", "прем"])
    wake_found, payload, _done, _phrase = parse_utterance(raw, wakes, ends)
    if wake_found:
        return payload.strip()
    # No wake word — still drop trailing end token if present
    text, _ = _split_end(normalize_text(raw), [normalize_text(t) for t in ends if t])
    return text.strip()


def is_empty_or_filler(
    transcript: str,
    wake_phrases: Iterable[str] | None = None,
    end_tokens: Iterable[str] | None = None,
) -> bool:
    """True when ASR text is blank, hallucination, or only wake/end fillers."""
    raw = (transcript or "").strip()
    if not raw:
        return True
    if is_hallucination(raw):
        return True
    cmd = extract_ptt_command(raw, wake_phrases, end_tokens)
    if not cmd:
        return True
    if len(cmd) < 2:
        return True
    return False


class CommandSession:
    """
    Protocol: wake «ИА» …command… end «прием».

    Buffer survives pauses: speech chunks accumulate until «прием».
    If wake_required=False, first substantial chunk soft-starts a session
    even without hearing «ИА».
    """

    def __init__(
        self,
        wake_phrases: Iterable[str],
        end_tokens: Iterable[str] | None = None,
        ttl_s: float = 20.0,
        *,
        wake_required: bool = True,
        end_only_min_chars: int = 6,
    ) -> None:
        self.wake_phrases = list(wake_phrases)
        self.end_tokens = list(end_tokens or ["прием", "приём", "прем"])
        self.ttl_s = ttl_s
        self.wake_required = wake_required
        self.end_only_min_chars = end_only_min_chars
        self._until = 0.0
        self._buffer = ""
        self._wake: str | None = None

    @property
    def waiting(self) -> bool:
        """True while session is live (TTL not expired)."""
        return time.monotonic() < self._until

    @property
    def has_buffer(self) -> bool:
        return bool(self._buffer.strip())

    @property
    def partial(self) -> str:
        return self._buffer

    def clear(self) -> None:
        self._until = 0.0
        self._buffer = ""
        self._wake = None

    def _arm(self, wake: str | None, partial: str | None = None) -> None:
        self._until = time.monotonic() + self.ttl_s
        self._wake = wake or self._wake or "иа"
        if partial is not None:
            self._buffer = partial

    def _finish(self, extra: str = "", wake: str | None = None) -> ParseResult:
        parts = [p for p in (self._buffer, extra) if p]
        cmd = _trim(" ".join(parts))
        phrase = wake or self._wake
        self.clear()
        if not cmd:
            return ParseResult("ignore")
        return ParseResult("command", command=cmd, wake_phrase=phrase)

    def feed(
        self,
        transcript: str,
        *,
        wake_hint: bool = False,
        end_hint: bool = False,
    ) -> ParseResult:
        wake_found, payload, done, wake_phrase = parse_utterance(
            transcript, self.wake_phrases, self.end_tokens
        )

        if wake_hint and not wake_found:
            wake_found = True
            wake_phrase = wake_phrase or "иа"
            norm = normalize_text(transcript)
            if len(norm.split()) <= 2 and not end_hint:
                payload, done = "", False
            else:
                payload, done = _split_end(norm, [normalize_text(t) for t in self.end_tokens])

        if end_hint and not done:
            done = True

        if end_hint and transcript and is_hallucination(transcript):
            payload = ""

        # --- wake present ---
        if wake_found:
            if done:
                # «ИА … прием» in one clip
                if payload:
                    self.clear()
                    return ParseResult("command", command=payload, wake_phrase=wake_phrase)
                # «ИА. Прием.» with nothing between — keep / start waiting, don't wipe prior buffer
                if self.has_buffer:
                    return self._finish("", wake=wake_phrase)
                self._arm(wake_phrase, self._buffer)
                return ParseResult("waiting", wake_phrase=wake_phrase, partial=self._buffer)

            if self.waiting or self.has_buffer:
                # Second wake during dictation — keep buffer
                if payload:
                    self._buffer = _trim(f"{self._buffer} {payload}")
                self._arm(wake_phrase or self._wake, self._buffer)
                return ParseResult("waiting", wake_phrase=self._wake, partial=self._buffer)

            self._arm(wake_phrase, payload or "")
            return ParseResult("waiting", wake_phrase=wake_phrase, partial=self._buffer)

        # --- no wake; session open OR buffer still held after short TTL gap ---
        in_session = self.waiting or self.has_buffer
        if in_session:
            if done:
                return self._finish(payload, wake=self._wake)
            if payload:
                self._buffer = _trim(f"{self._buffer} {payload}")
            # Any continuation refreshes TTL (pauses don't kill the session)
            self._arm(self._wake, self._buffer)
            return ParseResult("waiting", wake_phrase=self._wake, partial=self._buffer)

        # --- soft-start without wake (multi-chunk dictation + later «прием») ---
        if not self.wake_required and payload and len(payload) >= self.end_only_min_chars:
            if done:
                self.clear()
                return ParseResult("command", command=payload, wake_phrase="(без иа)")
            self._arm("(soft)", payload)
            return ParseResult("waiting", wake_phrase="(soft)", partial=self._buffer)

        # lone «прием» with nothing buffered
        if done and not payload:
            return ParseResult("ignore")

        return ParseResult("ignore")


@dataclass
class WakeResult:
    matched: bool
    command: str | None
    phrase: str | None = None
    completed: bool = False


def extract_wake(
    transcript: str,
    wake_phrases: Iterable[str],
    end_tokens: Iterable[str] | None = None,
) -> WakeResult:
    ends = end_tokens or ["прием", "приём", "прем"]
    wake_found, payload, done, phrase = parse_utterance(transcript, wake_phrases, ends)
    if not wake_found:
        return WakeResult(False, None)
    if done:
        return WakeResult(True, payload or None, phrase, completed=True)
    return WakeResult(True, payload or None, phrase, completed=False)


def extract_command(
    transcript: str,
    wake_phrases: Iterable[str],
    end_tokens: Iterable[str] | None = None,
) -> str | None:
    r = extract_wake(transcript, wake_phrases, end_tokens)
    if not r.matched or not r.completed:
        return None
    return r.command
