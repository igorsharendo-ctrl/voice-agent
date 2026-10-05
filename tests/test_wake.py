from __future__ import annotations

import time

from src.wake import CommandSession, extract_command, extract_wake, is_hallucination, parse_utterance

PHRASES = ["иа", "и а", "ia", "йа", "ия"]
ENDS = ["прием", "приём", "прем"]


def test_full_phrase_with_end() -> None:
    assert extract_command("ИА открой файл main.py прием", PHRASES, ENDS) == "открой файл main.py"


def test_wake_only_waits() -> None:
    r = extract_wake("ИА", PHRASES, ENDS)
    assert r.matched and not r.completed and r.command is None


def test_without_end_not_ready() -> None:
    assert extract_command("ИА открой файл", PHRASES, ENDS) is None
    r = extract_wake("ИА открой файл", PHRASES, ENDS)
    assert r.matched and not r.completed and r.command == "открой файл"


def test_session_two_steps() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    assert s.feed("ИА").kind == "waiting"
    assert s.feed("найди баг в main.py прием").kind == "command"
    assert s.feed("найди баг в main.py прием").command is None  # cleared, no wake
    s2 = CommandSession(PHRASES, ENDS, ttl_s=30)
    s2.feed("ИА")
    r = s2.feed("найди баг прием")
    assert r.kind == "command" and r.command == "найди баг"


def test_session_partial_then_end() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    assert s.feed("ИА открой логи").kind == "waiting"
    r = s.feed("прием")
    assert r.kind == "command" and r.command == "открой логи"


def test_ignore_plain() -> None:
    assert parse_utterance("просто текст", PHRASES, ENDS)[0] is False
    s = CommandSession(PHRASES, ENDS)
    assert s.feed("просто текст").kind == "ignore"


def test_voiceprint_wake_hint() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    # Whisper misheard «ИА» as something else — voiceprint arms session
    r = s.feed("Слово", wake_hint=True)
    assert r.kind == "waiting"
    r2 = s.feed("открой файл прием")
    assert r2.kind == "command" and r2.command == "открой файл"


def test_voiceprint_end_hint() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    s.feed("ИА открой логи")
    r = s.feed("продолжение следует", end_hint=True)
    assert r.kind == "command" and r.command == "открой логи"


def test_punctuated_wake_and_end() -> None:
    """Whisper often emits «ИА.» / «Прием.» with periods — must still parse."""
    text = "ИА. Сейчас мы выполним команду, протестируем ее. Прием."
    r = extract_wake(text, PHRASES, ENDS)
    assert r.matched and r.completed
    assert r.command is not None
    assert "выполним команду" in r.command
    assert extract_command(text, PHRASES, ENDS) is not None
    # filename dots preserved
    assert extract_command("ИА открой main.py прием", PHRASES, ENDS) == "открой main.py"


def test_second_wake_keeps_buffer() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    assert s.feed("ИА").kind == "waiting"
    s.feed("открой файл")
    r = s.feed("ИА")  # second wake must not wipe
    assert r.kind == "waiting"
    assert "открой файл" in (r.partial or "")
    r2 = s.feed("прием")
    assert r2.kind == "command" and "открой файл" in (r2.command or "")


def test_end_only_without_wake() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30, wake_required=False)
    r = s.feed("Я хочу сказать команду. Прием.")
    assert r.kind == "command"
    assert "команду" in (r.command or "")
    r2 = CommandSession(PHRASES, ENDS, wake_required=False).feed("текст команды прием")
    assert r2.kind == "command"


def test_soft_session_across_pauses() -> None:
    """Without ИА, chunks accumulate until прием (wake_required=False)."""
    s = CommandSession(PHRASES, ENDS, ttl_s=30, wake_required=False)
    assert s.feed("Итак мы тестируем длинный текст").kind == "waiting"
    assert s.feed("вторая часть фразы").kind == "waiting"
    r = s.feed("прием")
    assert r.kind == "command"
    assert "длинный текст" in (r.command or "")
    assert "вторая часть" in (r.command or "")


def test_ia_priem_empty_keeps_waiting() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=30)
    r = s.feed("ИА. Прием.")
    assert r.kind == "waiting"


def test_buffer_survives_ttl_for_priem() -> None:
    s = CommandSession(PHRASES, ENDS, ttl_s=0.05, wake_required=False)
    s.feed("накопили текст команды")
    time.sleep(0.08)  # TTL expired, buffer remains
    assert not s.waiting and s.has_buffer
    r = s.feed("прием")
    assert r.kind == "command" and "накопили" in (r.command or "")


def test_hallucination() -> None:
    assert is_hallucination("Субтитры сделал DimaTorzok")


if __name__ == "__main__":
    test_full_phrase_with_end()
    test_wake_only_waits()
    test_without_end_not_ready()
    test_session_two_steps()
    test_session_partial_then_end()
    test_ignore_plain()
    test_voiceprint_wake_hint()
    test_voiceprint_end_hint()
    test_punctuated_wake_and_end()
    test_second_wake_keeps_buffer()
    test_end_only_without_wake()
    test_soft_session_across_pauses()
    test_ia_priem_empty_keeps_waiting()
    test_buffer_survives_ttl_for_priem()
    test_hallucination()
    print("all wake tests passed")
