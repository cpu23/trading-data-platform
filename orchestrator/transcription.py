"""Local transcription of public webcast audio through faster-whisper.

faster-whisper runs entirely in-process: audio bytes are written to a
temporary file, decoded by PyAV and transcribed by local model weights.
There is deliberately no subprocess (no ffmpeg binary) and no HTTP usage in
this module: audio is never uploaded anywhere and transcription never
contacts a remote model service. Model weights are resolved by
faster-whisper itself -- ``model`` may be a size name (``tiny``, ``base``,
``small`` ...) downloaded once into the HuggingFace cache, or a local
directory path. ``model_dir`` pins the download root and
``local_files_only`` forbids any network access while loading weights.

The heavy dependency is imported lazily so the rest of the platform stays
importable (and testable) without it installed. When faster-whisper or the
model cannot be loaded, callers receive an explicit
:class:`TranscriptionUnavailable` error and can record a setup state
instead of crashing or inventing transcript content.

Strict production settings are on by default and are passed to
faster-whisper at transcribe time: ``vad_filter`` skips non-speech
segments and ``condition_on_previous_text`` is disabled so long webcasts
cannot hallucinate earlier context into later transcript text.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from logging_config import get_logger

logger = get_logger("transcription")

DEFAULT_MODEL = "small.en"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_LANGUAGE = "en"
DEFAULT_BEAM_SIZE = 5
DEFAULT_MAX_AUDIO_SECONDS = 7200
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_AUDIO_BYTES = 512 * 1024 * 1024
# Newsdata volume is mounted writable on the worker; weights land under it.
DEFAULT_MODEL_DIR = "/var/lib/trading-data/news/models/whisper"
# Strict anti-hallucination settings: VAD skips non-speech segments and
# condition_on_previous_text is disabled so a long webcast cannot drag
# earlier context forward into later transcript text.
DEFAULT_VAD_FILTER = True
DEFAULT_CONDITION_ON_PREVIOUS_TEXT = False

# A segment covers at most a few seconds of audio; a 10x headroom cap keeps
# runaway decoders bounded even if the duration probe is unavailable.
_MAX_SEGMENT_HEADROOM = 10
_MIN_SEGMENT_GUARD = 128

_SUPPORTED_SUFFIXES = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".wma", ".webm", ".mp4"}
)


class TranscriptionUnavailable(RuntimeError):
    """faster-whisper (or its runtime) cannot be loaded in this environment."""


class TranscriptionTimeout(RuntimeError):
    """Transcription did not finish within the configured deadline."""


class TranscriptionFailure(RuntimeError):
    """Transcription ran but produced no usable transcript."""


@dataclass(frozen=True)
class TranscriptionSegment:
    """One transcribed speech segment with source-time boundaries in seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    """Outcome of a local transcription run with full provenance."""

    text: str
    language: str | None
    language_probability: float | None
    duration_seconds: float | None
    segments: tuple[TranscriptionSegment, ...]
    model: str
    device: str
    compute_type: str
    beam_size: int
    vad_filter: bool
    condition_on_previous_text: bool
    elapsed_ms: int
    transcribed_at: str  # ISO 8601 UTC availability timestamp


def default_transcription_config() -> dict:
    """Return the bounded default transcription settings."""
    return {
        "model": DEFAULT_MODEL,
        "device": DEFAULT_DEVICE,
        "compute_type": DEFAULT_COMPUTE_TYPE,
        "beam_size": DEFAULT_BEAM_SIZE,
        "language": DEFAULT_LANGUAGE,
        "max_audio_seconds": DEFAULT_MAX_AUDIO_SECONDS,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "max_audio_bytes": DEFAULT_MAX_AUDIO_BYTES,
        "model_dir": DEFAULT_MODEL_DIR,
        "local_files_only": False,
        "cpu_threads": 0,  # 0 lets faster-whisper pick a safe default
        "vad_filter": DEFAULT_VAD_FILTER,
        "condition_on_previous_text": DEFAULT_CONDITION_ON_PREVIOUS_TEXT,
    }


def normalize_transcription_config(raw: Mapping | None) -> dict:
    """Validate and clamp a transcription configuration dict.

    Malformed or out-of-range values fall back to bounded defaults so an
    operator typo can never disable the safety limits. The strict
    anti-hallucination switches (``vad_filter`` true,
    ``condition_on_previous_text`` false) stay on unless an explicit bool
    is supplied.
    """
    settings = default_transcription_config()
    if not isinstance(raw, Mapping):
        return settings
    for key in ("model", "device", "compute_type", "language", "model_dir"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
    for key in ("vad_filter", "condition_on_previous_text"):
        value = raw.get(key)
        if isinstance(value, bool):
            settings[key] = value
    for key in ("beam_size", "max_audio_seconds", "timeout_seconds", "cpu_threads"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and value > 0:
            settings[key] = int(value)
    value = raw.get("max_audio_bytes")
    if isinstance(value, (int, float)) and value > 0:
        settings["max_audio_bytes"] = int(value)
    settings["beam_size"] = max(1, min(settings["beam_size"], 10))
    settings["max_audio_seconds"] = max(
        1, min(settings["max_audio_seconds"], 24 * 3600)
    )
    settings["timeout_seconds"] = max(1, min(settings["timeout_seconds"], 24 * 3600))
    settings["local_files_only"] = bool(raw.get("local_files_only", False))
    return settings


def _import_faster_whisper():
    """Import the faster-whisper package lazily (importable for tests to patch)."""
    import faster_whisper  # noqa: F401

    return faster_whisper


def transcription_available() -> bool:
    """True when faster-whisper can be imported in this environment."""
    try:
        _import_faster_whisper()
        return True
    except ImportError:
        return False


def _probe_duration_seconds(path: str) -> float | None:
    """Best-effort source duration in seconds via PyAV; None when unprobeable."""
    try:
        import av

        with av.open(path) as container:
            duration = container.duration
            if duration is None:
                return None
            return float(duration) / 1_000_000
    except Exception:  # noqa: BLE001 - probing is best effort; deadline still bounds
        return None


def _run_with_deadline(
    fn: Callable[[], object],
    timeout_seconds: float,
) -> object:
    """Run ``fn`` on a daemon thread with a hard wall-clock deadline.

    Mirrors the bounded-http pattern: a stuck decoder never blocks the
    collector forever. The worker keeps running after the deadline (Python
    cannot kill threads); the caller receives a :class:`TranscriptionTimeout`
    and must not persist any partial output.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
            box["error"] = exc

    thread = threading.Thread(
        target=target, name="local-transcription", daemon=True
    )
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TranscriptionTimeout(
            f"transcription exceeded {timeout_seconds:g}s deadline"
        )
    if "error" in box:
        raise box["error"]
    return box.get("result")


_MODEL_CACHE: dict[tuple, object] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _model_cache_key(settings: dict) -> tuple:
    return (
        settings["model"],
        settings["device"],
        settings["compute_type"],
        settings["model_dir"],
        settings["local_files_only"],
        settings["cpu_threads"],
    )


def _load_model(settings: dict):
    """Load (or reuse) the WhisperModel; downloads weights only when allowed."""
    try:
        fw = _import_faster_whisper()
    except ImportError as exc:
        raise TranscriptionUnavailable(
            "faster-whisper is not installed; cannot transcribe audio locally"
        ) from exc
    key = _model_cache_key(settings)
    with _MODEL_CACHE_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is not None:
            return model
        kwargs: dict = {"download_root": settings["model_dir"]} if settings[
            "model_dir"
        ] else {}
        if settings["local_files_only"]:
            kwargs["local_files_only"] = True
        if settings["cpu_threads"]:
            kwargs["cpu_threads"] = settings["cpu_threads"]
        model = fw.WhisperModel(
            settings["model"],
            device=settings["device"],
            compute_type=settings["compute_type"],
            **kwargs,
        )
        _MODEL_CACHE[key] = model
        return model


def _write_audio_tempfile(audio: bytes, source_url: str = "") -> str:
    """Write audio bytes to a private temp file; returns the path."""
    suffix = ""
    from urllib.parse import urlsplit

    path = urlsplit(source_url).path.lower()
    for candidate in _SUPPORTED_SUFFIXES:
        if path.endswith(candidate):
            suffix = candidate
            break
    fd, path = tempfile.mkstemp(prefix="issuer_audio_", suffix=suffix or ".audio")
    with open(fd, "wb", closefd=True) as handle:
        handle.write(audio)
    return path


def _transcribe_path(
    path: str,
    settings: dict,
    *,
    deadline_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> TranscriptionResult:
    """Load the model and transcribe one local audio file within the deadline."""
    started_at = clock()
    max_segments = (
        max(settings["max_audio_seconds"] * _MAX_SEGMENT_HEADROOM, _MIN_SEGMENT_GUARD)
        + _MIN_SEGMENT_GUARD
    )

    duration = _probe_duration_seconds(path)
    if duration is not None and duration > settings["max_audio_seconds"]:
        raise TranscriptionFailure(
            "audio duration exceeds the configured maximum "
            f"({duration:.0f}s > {settings['max_audio_seconds']}s)"
        )

    model = _load_model(settings)
    transcribe_kwargs = {
        "language": settings["language"] or None,
        "beam_size": settings["beam_size"],
        "task": "transcribe",
        "vad_filter": settings["vad_filter"],
        "condition_on_previous_text": settings["condition_on_previous_text"],
    }
    segments_iter, info = model.transcribe(path, **transcribe_kwargs)

    segments: list[TranscriptionSegment] = []
    for raw in segments_iter:
        if clock() - started_at >= deadline_seconds:
            raise TranscriptionTimeout(
                f"transcription exceeded {deadline_seconds:g}s deadline"
            )
        text = (getattr(raw, "text", None) or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptionSegment(
                start=float(getattr(raw, "start", 0.0) or 0.0),
                end=float(getattr(raw, "end", 0.0) or 0.0),
                text=text,
            )
        )
        if len(segments) > max_segments:
            raise TranscriptionFailure(
                "transcription produced an implausible number of segments"
            )

    content = "\n".join(segment.text for segment in segments)
    if not content:
        raise TranscriptionFailure("no speech was transcribed from the audio")

    return TranscriptionResult(
        text=content,
        language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
        duration_seconds=getattr(info, "duration", None),
        segments=tuple(segments),
        model=settings["model"],
        device=settings["device"],
        compute_type=settings["compute_type"],
        beam_size=settings["beam_size"],
        vad_filter=settings["vad_filter"],
        condition_on_previous_text=settings["condition_on_previous_text"],
        elapsed_ms=max(0, int((clock() - started_at) * 1000)),
        transcribed_at=datetime.now(UTC).isoformat(),
    )


def transcribe_audio(
    audio: bytes,
    config: Mapping | None = None,
    correlation_id: str | None = None,
    *,
    source_url: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> TranscriptionResult:
    """Transcribe public webcast audio bytes fully locally.

    Raises:
        TranscriptionUnavailable: faster-whisper cannot be imported.
        TranscriptionTimeout: the configured deadline was exceeded (no
            partial output is ever returned).
        TranscriptionFailure: the payload is empty, oversized, overlong or
            contains no transcribable speech.
    """
    settings = normalize_transcription_config(config)
    if not transcription_available():
        raise TranscriptionUnavailable(
            "faster-whisper is not installed; cannot transcribe audio locally"
        )
    if not audio:
        raise TranscriptionFailure("empty audio payload")
    if len(audio) > settings["max_audio_bytes"]:
        raise TranscriptionFailure(
            f"audio payload exceeds {settings['max_audio_bytes']} bytes"
        )

    path = _write_audio_tempfile(audio, source_url)
    try:
        result = _run_with_deadline(
            lambda: _transcribe_path(
                path,
                settings,
                deadline_seconds=float(settings["timeout_seconds"]),
                clock=clock,
            ),
            float(settings["timeout_seconds"]),
        )
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "transcription_tempfile_cleanup_failed",
                action="transcribe_audio",
                path=path,
                correlation_id=correlation_id or "none",
            )
    if not isinstance(result, TranscriptionResult):
        raise TranscriptionFailure("transcription produced no result")
    return result


def audio_sha256(audio: bytes) -> str:
    """Deterministic content identity for audio bytes (source content hash)."""
    return hashlib.sha256(audio).hexdigest()


__all__ = [
    "DEFAULT_BEAM_SIZE",
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_CONDITION_ON_PREVIOUS_TEXT",
    "DEFAULT_DEVICE",
    "DEFAULT_LANGUAGE",
    "DEFAULT_MAX_AUDIO_BYTES",
    "DEFAULT_MAX_AUDIO_SECONDS",
    "DEFAULT_MODEL",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_VAD_FILTER",
    "TranscriptionFailure",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionTimeout",
    "TranscriptionUnavailable",
    "audio_sha256",
    "default_transcription_config",
    "normalize_transcription_config",
    "transcribe_audio",
    "transcription_available",
]
