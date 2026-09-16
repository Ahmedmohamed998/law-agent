"""
Turning a written answer into something worth hearing, and a WAV upload into
PCM. Both pure, so both are tested without AWS.
"""

import io
import re
import wave
from dataclasses import dataclass

# The tag the model puts after every sentence it could not source. Right on the
# page, where the eye skips it; read aloud after every sentence it drowns the
# answer. The written answer keeps it, and the "may be out of date" line that
# accompanies it is still spoken.
_UNSOURCED_TAG = re.compile(r"\(\s*معرفة عامة\s*[—\-–]\s*غير موثّقة في المستندات\s*\)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_URL = re.compile(r"https?://\S+")

TABLE_NOTICE_AR = "يوجد جدول في الرد المكتوب."

# Polly bills and caps a single request by characters. Split below the cap, on
# sentence boundaries, so no request fails and no word is cut in two.
POLLY_MAX_CHARS = 2800


def speakable(markdown: str) -> str:
    """The text of an answer, as it should be spoken.

    Tables are replaced by a one-line notice rather than read cell by cell: a
    penalty schedule spoken aloud is a minute of numbers nobody can follow.
    """
    out: list[str] = []
    in_table = False
    for line in (markdown or "").splitlines():
        if _TABLE_ROW.match(line) or _TABLE_RULE.match(line):
            if not in_table:
                out.append(TABLE_NOTICE_AR)
                in_table = True
            continue
        in_table = False

        line = _UNSOURCED_TAG.sub("", line)
        line = _URL.sub("", line)
        line = re.sub(r"^\s*#{1,6}\s*", "", line)          # headings
        line = re.sub(r"^\s*(?:[-*·•]|\d+[.)])\s+", "", line)  # list markers
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", line)
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out).strip()


def split_for_polly(text: str, limit: int = POLLY_MAX_CHARS) -> list[str]:
    """Chunks no longer than `limit`, broken at sentence ends where possible."""
    text = text.strip()
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?؟؛\n])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:
            # A single "sentence" longer than the cap: break at the last space.
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def mostly_arabic(text: str) -> bool:
    """Pick the voice by script. The model answers in the question's language,
    and an Arabic voice reading English is barely intelligible."""
    arabic = len(re.findall(r"[؀-ۿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return arabic >= latin


class BadAudio(ValueError):
    """The upload is not audio this service accepts."""


@dataclass(frozen=True, slots=True)
class Pcm:
    frames: bytes
    sample_rate: int
    seconds: float


ACCEPTED_RATES = (8000, 16000)


def read_wav(data: bytes, *, max_seconds: int) -> Pcm:
    """Validate a WAV upload and return its PCM.

    The widget records 16 kHz mono 16-bit PCM because that is what Transcribe
    streaming takes without conversion, and it spares the server an ffmpeg
    dependency. Anything else is rejected rather than transcoded.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            nframes = w.getnframes()
            frames = w.readframes(nframes)
    except (wave.Error, EOFError) as exc:
        raise BadAudio(f"not a readable WAV file: {exc}") from exc

    if channels != 1:
        raise BadAudio(f"expected mono, got {channels} channels")
    if width != 2:
        raise BadAudio(f"expected 16-bit samples, got {width * 8}-bit")
    if rate not in ACCEPTED_RATES:
        raise BadAudio(f"expected a sample rate of {ACCEPTED_RATES}, got {rate}")

    seconds = nframes / float(rate)
    if seconds > max_seconds + 0.5:
        raise BadAudio(f"recording is {seconds:.0f}s; the limit is {max_seconds}s")
    if seconds < 0.3:
        raise BadAudio("recording is too short")
    return Pcm(frames=frames, sample_rate=rate, seconds=seconds)
