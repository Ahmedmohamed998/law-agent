import io
import wave

import pytest

from app.voice.text import (
    POLLY_MAX_CHARS,
    TABLE_NOTICE_AR,
    BadAudio,
    mostly_arabic,
    read_wav,
    speakable,
    split_for_polly,
)


# ── speakable ─────────────────────────────────────────────────────────────


def test_markdown_emphasis_and_lists_are_not_read_aloud():
    text = "**مدة الإجازة** السنوية:\n- 21 يوماً\n- 30 يوماً بعد خمس سنوات"
    spoken = speakable(text)
    assert "*" not in spoken
    assert "- " not in spoken
    assert "21 يوماً" in spoken and "30 يوماً" in spoken


def test_a_table_becomes_one_notice_not_a_minute_of_cells():
    text = (
        "جدول المخالفات:\n"
        "| المخالفة | الغرامة |\n"
        "|---|---|\n"
        "| تأخير | 1000 |\n"
        "| غياب | 2000 |\n"
        "هذه معلومات عامة."
    )
    spoken = speakable(text)
    assert spoken.count(TABLE_NOTICE_AR) == 1
    assert "1000" not in spoken and "|" not in spoken
    assert "هذه معلومات عامة." in spoken


def test_unsourced_tag_is_dropped_but_the_warning_line_is_kept():
    text = (
        "يحق للعامل مكافأة نهاية الخدمة (معرفة عامة — غير موثّقة في المستندات).\n"
        "قد يكون هذا الجزء غير محدَّث؛ يُرجى التحقق منه مع المحامي."
    )
    spoken = speakable(text)
    assert "معرفة عامة" not in spoken
    assert "قد يكون هذا الجزء غير محدَّث" in spoken


def test_citations_are_kept_because_they_are_the_point():
    text = "مدة الإجازة 21 يوماً (نظام العمل، المادة التاسعة بعد المائة)."
    assert "(نظام العمل، المادة التاسعة بعد المائة)" in speakable(text)


def test_empty_answer_speaks_nothing():
    assert speakable("") == ""
    assert speakable("|a|b|\n|---|---|") == TABLE_NOTICE_AR


# ── split_for_polly ───────────────────────────────────────────────────────


def test_short_text_is_one_request():
    assert split_for_polly("جملة واحدة.") == ["جملة واحدة."]


def test_long_text_splits_under_the_cap_at_sentence_ends():
    sentence = "هذه جملة عربية تشرح حقاً من حقوق العامل في نظام العمل السعودي. "
    text = sentence * 200
    chunks = split_for_polly(text)
    assert len(chunks) > 1
    assert all(len(c) <= POLLY_MAX_CHARS for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])
    # Nothing lost or duplicated.
    assert "".join(chunks).replace(" ", "") == text.strip().replace(" ", "")


def test_one_enormous_sentence_is_still_split():
    text = "كلمة " * 2000
    chunks = split_for_polly(text)
    assert all(len(c) <= POLLY_MAX_CHARS for c in chunks)
    assert sum(len(c.split()) for c in chunks) == 2000


def test_voice_follows_the_script():
    assert mostly_arabic("كم مدة الإجازة السنوية؟")
    assert not mostly_arabic("How long is annual leave?")


# ── read_wav ──────────────────────────────────────────────────────────────


def _wav(seconds: float, *, rate=16000, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buf.getvalue()


def test_accepts_16k_mono_16bit():
    pcm = read_wav(_wav(2.0), max_seconds=60)
    assert pcm.sample_rate == 16000
    assert pcm.seconds == pytest.approx(2.0)
    assert len(pcm.frames) == 2 * 16000 * 2


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"channels": 2}, "mono"),
        ({"width": 1}, "16-bit"),
        ({"rate": 44100}, "sample rate"),
    ],
)
def test_rejects_formats_transcribe_would_not_take(kwargs, message):
    with pytest.raises(BadAudio, match=message):
        read_wav(_wav(2.0, **kwargs), max_seconds=60)


def test_rejects_too_long_and_too_short():
    with pytest.raises(BadAudio, match="limit"):
        read_wav(_wav(61.0), max_seconds=60)
    with pytest.raises(BadAudio, match="too short"):
        read_wav(_wav(0.1), max_seconds=60)


def test_rejects_something_that_is_not_a_wav():
    with pytest.raises(BadAudio, match="not a readable WAV"):
        read_wav(b"this is not audio", max_seconds=60)
