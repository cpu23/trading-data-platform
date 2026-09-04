"""Version-controlled research benchmark episodes kept outside model inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from research_intelligence.context import ResearchContext
from research_intelligence.contracts import NormalizedEvidence
from research_intelligence.relationships import normalize_entity

DEFAULT_BENCHMARK_DIR = Path(__file__).with_name("benchmark_episodes")
_MAX_EPISODES = 50
_MAX_EVIDENCE = 200
_REQUIRED_KEYS = frozenset(
    {
        "id",
        "version",
        "synthetic",
        "episode_kind",
        "description",
        "replay_dates",
        "evidence",
        "expected_developments",
        "plausible_second_order_areas",
        "expected_unknowns",
        "forbidden_hindsight",
        "manual_milestones",
    }
)


def _timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid benchmark {field}") from None
    else:
        raise ValueError(f"invalid benchmark {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"benchmark {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _text(value: Any, field: str, maximum: int = 1_000) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        raise ValueError(f"benchmark {field} is required")
    return cleaned[:maximum]


def _strings(value: Any, field: str, maximum: int = 50) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"benchmark {field} must be a bounded list")
    return tuple(_text(item, field, 300) for item in value)


@dataclass(frozen=True, slots=True)
class ForbiddenHindsight:
    description: str
    available_after: datetime


@dataclass(frozen=True, slots=True)
class BenchmarkEpisode:
    episode_id: str
    version: int
    synthetic: bool
    episode_kind: str
    description: str
    replay_dates: tuple[datetime, ...]
    evidence: tuple[NormalizedEvidence, ...]
    expected_developments: tuple[str, ...]
    plausible_second_order_areas: tuple[str, ...]
    expected_unknowns: tuple[str, ...]
    forbidden_hindsight: tuple[ForbiddenHindsight, ...]
    manual_milestones: MappingProxyType
    source_path: str

    def evidence_as_of(
        self, context: ResearchContext
    ) -> tuple[NormalizedEvidence, ...]:
        if not context.is_replay:
            raise ValueError("benchmark evidence requires replay context")
        return context.filter_evidence(self.evidence)

    def evaluator_payload(self) -> dict[str, Any]:
        """Benchmark answers are exposed only after research execution."""
        return {
            "id": self.episode_id,
            "version": self.version,
            "episode_kind": self.episode_kind,
            "description": self.description,
            "expected_developments": list(self.expected_developments),
            "plausible_second_order_areas": list(self.plausible_second_order_areas),
            "expected_unknowns": list(self.expected_unknowns),
            "forbidden_hindsight": [
                {
                    "description": item.description,
                    "available_after": item.available_after.isoformat(),
                }
                for item in self.forbidden_hindsight
            ],
            "manual_milestones": {
                key: value.isoformat() for key, value in self.manual_milestones.items()
            },
        }


def _evidence(raw: Any, episode_id: str, synthetic: bool) -> NormalizedEvidence:
    if not isinstance(raw, dict):
        raise ValueError("benchmark evidence row must be an object")
    required = {
        "evidence_type",
        "evidence_id",
        "source_name",
        "source_timestamp",
        "available_at",
        "title",
    }
    if not required.issubset(raw):
        raise ValueError("benchmark evidence row is incomplete")
    source = _text(raw.get("source_name"), "evidence.source_name", 120)
    if synthetic and "synthetic" not in source.casefold():
        raise ValueError("synthetic benchmark sources must be labelled synthetic")
    entities = []
    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            raise ValueError("benchmark evidence entity must be an object")
        entities.append(normalize_entity(item.get("entity_type"), item.get("name")))
    source_timestamp = _timestamp(
        raw.get("source_timestamp"), "evidence.source_timestamp"
    )
    available_at = _timestamp(raw.get("available_at"), "evidence.available_at")
    if available_at < source_timestamp:
        raise ValueError("evidence cannot be available before its source timestamp")
    structured = raw.get("structured_fields") or {}
    if not isinstance(structured, dict):
        raise ValueError("benchmark structured_fields must be an object")
    forbidden_keys = _REQUIRED_KEYS & set(structured)
    if forbidden_keys:
        raise ValueError("benchmark answers cannot be embedded in evidence")
    return NormalizedEvidence.create(
        evidence_type=raw.get("evidence_type"),
        evidence_id=raw.get("evidence_id"),
        source_name=source,
        source_timestamp=source_timestamp,
        available_at=available_at,
        availability_basis=_text(
            raw.get("availability_basis") or "fixture_publication_time",
            "evidence.availability_basis",
            80,
        ),
        acquired_at=raw.get("acquired_at"),
        valid_from=raw.get("valid_from"),
        valid_to=raw.get("valid_to"),
        point_in_time_safe=raw.get("point_in_time_safe", True),
        title=raw.get("title"),
        bounded_excerpt=raw.get("bounded_excerpt"),
        source_reference=raw.get("source_reference"),
        entities=tuple(entities),
        structured_fields=structured,
        provenance={
            "adapter": "benchmark_fixture",
            "benchmark_id": episode_id,
            "synthetic": synthetic,
            **(
                raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
            ),
        },
        freshness="historical_fixture",
    )


def load_benchmark(path: str | Path) -> BenchmarkEpisode:
    source_path = Path(path)
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != _REQUIRED_KEYS:
        raise ValueError("benchmark episode has unexpected or missing fields")
    episode_id = _text(raw.get("id"), "id", 120)
    version = raw.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= 1_000
    ):
        raise ValueError("benchmark version must be 1..1000")
    synthetic = raw.get("synthetic")
    if not isinstance(synthetic, bool):
        raise ValueError("benchmark synthetic must be boolean")
    kind = _text(raw.get("episode_kind"), "episode_kind", 40).casefold()
    if kind not in {"development", "macro_regime", "noise"}:
        raise ValueError("benchmark episode_kind is unsupported")
    replay_raw = raw.get("replay_dates")
    if not isinstance(replay_raw, list) or not 1 <= len(replay_raw) <= 20:
        raise ValueError("benchmark replay_dates must contain 1..20 dates")
    replay_dates = tuple(
        sorted({_timestamp(item, "replay_date") for item in replay_raw})
    )
    evidence_raw = raw.get("evidence")
    if (
        not isinstance(evidence_raw, list)
        or not 1 <= len(evidence_raw) <= _MAX_EVIDENCE
    ):
        raise ValueError("benchmark evidence must contain 1..200 rows")
    evidence = tuple(_evidence(item, episode_id, synthetic) for item in evidence_raw)
    identities = [item.ref for item in evidence]
    if len(identities) != len(set(identities)):
        raise ValueError("benchmark evidence identities must be unique")
    hindsight_raw = raw.get("forbidden_hindsight")
    if not isinstance(hindsight_raw, list) or len(hindsight_raw) > 50:
        raise ValueError("benchmark forbidden_hindsight must be a bounded list")
    hindsight: list[ForbiddenHindsight] = []
    for item in hindsight_raw:
        if not isinstance(item, dict) or set(item) != {
            "description",
            "available_after",
        }:
            raise ValueError("forbidden hindsight row is invalid")
        hindsight.append(
            ForbiddenHindsight(
                _text(item.get("description"), "forbidden_hindsight", 500),
                _timestamp(
                    item.get("available_after"), "forbidden_hindsight.available_after"
                ),
            )
        )
    milestones_raw = raw.get("manual_milestones")
    if not isinstance(milestones_raw, dict) or len(milestones_raw) > 20:
        raise ValueError("benchmark manual_milestones must be an object")
    milestones = MappingProxyType(
        {
            _text(key, "manual_milestone", 80): _timestamp(value, "manual_milestone")
            for key, value in milestones_raw.items()
        }
    )
    return BenchmarkEpisode(
        episode_id=episode_id,
        version=version,
        synthetic=synthetic,
        episode_kind=kind,
        description=_text(raw.get("description"), "description", 2_000),
        replay_dates=replay_dates,
        evidence=evidence,
        expected_developments=_strings(
            raw.get("expected_developments"), "expected_developments"
        ),
        plausible_second_order_areas=_strings(
            raw.get("plausible_second_order_areas"),
            "plausible_second_order_areas",
        ),
        expected_unknowns=_strings(raw.get("expected_unknowns"), "expected_unknowns"),
        forbidden_hindsight=tuple(hindsight),
        manual_milestones=milestones,
        source_path=str(source_path),
    )


def list_benchmarks(
    directory: str | Path | None = None,
) -> tuple[BenchmarkEpisode, ...]:
    root = Path(directory) if directory is not None else DEFAULT_BENCHMARK_DIR
    paths = sorted(root.glob("*.yaml"))[:_MAX_EPISODES]
    episodes = tuple(load_benchmark(path) for path in paths)
    identities = [episode.episode_id for episode in episodes]
    if len(identities) != len(set(identities)):
        raise ValueError("benchmark episode IDs must be unique")
    return episodes


def get_benchmark(
    episode_id: str, directory: str | Path | None = None
) -> BenchmarkEpisode:
    normalized = _text(episode_id, "id", 120)
    for episode in list_benchmarks(directory):
        if episode.episode_id == normalized:
            return episode
    raise ValueError("unknown research benchmark")


__all__ = [
    "BenchmarkEpisode",
    "DEFAULT_BENCHMARK_DIR",
    "ForbiddenHindsight",
    "get_benchmark",
    "list_benchmarks",
    "load_benchmark",
]
