"""
The two AWS calls: Transcribe streaming (speech to text) and Polly (text to
speech). Credentials come from the environment, the same IAM keys embeddings
already use.

Streaming rather than batch Transcribe, deliberately: batch jobs read their
input from S3, which would mean a bucket holding recordings of people
describing their dismissal. Streaming takes the audio in the request and
keeps nothing.
"""

import asyncio
import logging
import threading
from collections import OrderedDict

import boto3

from app.config import settings
from app.voice.text import Pcm, mostly_arabic, speakable, split_for_polly

log = logging.getLogger("law_agent.voice")


class SpeechUnavailable(RuntimeError):
    """The speech provider failed or is not permitted for these credentials."""


# ── speech in ──────────────────────────────────────────────────────────────


async def transcribe(pcm: Pcm) -> str:
    """Text for a recording. Empty string when nothing intelligible was said."""
    # Imported here: the SDK pulls in awscrt, and the service should still
    # start (and serve text chat) on a machine where it is not installed.
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler

    s = settings()

    class _Collector(TranscriptResultStreamHandler):
        def __init__(self, stream):
            super().__init__(stream)
            self.parts: list[str] = []

        async def handle_transcript_event(self, transcript_event):
            for result in transcript_event.transcript.results:
                # Partials are revised as more audio arrives; only final
                # segments are the transcript.
                if not result.is_partial and result.alternatives:
                    self.parts.append(result.alternatives[0].transcript)

    try:
        client = TranscribeStreamingClient(region=s.speech_region)
        stream = await client.start_stream_transcription(
            language_code=s.transcribe_language,
            media_sample_rate_hz=pcm.sample_rate,
            media_encoding="pcm",
        )
        collector = _Collector(stream.output_stream)

        async def feed() -> None:
            # 100 ms per event. The recording already exists, so there is no
            # reason to pace it in real time.
            step = pcm.sample_rate * 2 // 10
            for i in range(0, len(pcm.frames), step):
                await stream.input_stream.send_audio_event(
                    audio_chunk=pcm.frames[i : i + step]
                )
            await stream.input_stream.end_stream()

        await asyncio.gather(feed(), collector.handle_events())
    except Exception as exc:  # the SDK raises its own and awscrt's types
        log.warning("transcribe failed: %s: %s", type(exc).__name__, exc)
        raise SpeechUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc

    return " ".join(p.strip() for p in collector.parts if p.strip())


# ── speech out ─────────────────────────────────────────────────────────────

_polly = None
_polly_lock = threading.Lock()


def _polly_client():
    global _polly
    with _polly_lock:
        if _polly is None:
            _polly = boto3.client("polly", region_name=settings().speech_region)
        return _polly


def synthesize(text: str) -> bytes:
    """MP3 for `text`. Long answers are split below Polly's per-request cap
    and the MP3 streams concatenated, which players handle as one file."""
    s = settings()
    spoken = speakable(text)
    if not spoken:
        return b""

    if mostly_arabic(spoken):
        voice, engine = s.polly_voice_id, s.polly_engine
    else:
        voice, engine = s.polly_voice_id_en, s.polly_engine_en

    client = _polly_client()
    parts: list[bytes] = []
    try:
        for chunk in split_for_polly(spoken):
            resp = client.synthesize_speech(
                Text=chunk, OutputFormat="mp3", VoiceId=voice, Engine=engine
            )
            parts.append(resp["AudioStream"].read())
    except Exception as exc:
        log.warning("polly failed: %s: %s", type(exc).__name__, exc)
        raise SpeechUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc
    return b"".join(parts)


class _AudioCache:
    """Recently spoken answers, in memory.

    In memory rather than on disk on purpose: a file cache of legal answers
    read aloud is one more copy of someone's case that erasure would have to
    find. This dies with the process, and one worker means one cache.
    """

    def __init__(self, max_entries: int = 48):
        self._max = max_entries
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


audio_cache = _AudioCache()
