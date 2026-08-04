#!/usr/bin/env python3
"""Validate tracked YAML and deterministic demo fixture invariants."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
YAML_FILES = [
    ROOT / "config" / "config.yaml",
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.demo.yml",
    ROOT / ".github" / "workflows" / "ci.yml",
]
DEMO_SEED = ROOT / "db" / "demo" / "900_demo_seed.sql"
REQUIRED_DEMO_MARKERS = (
    "demo/deterministic",
    "33333333-3333-4333-8333-333333333333",
    "Deterministic fictional demo briefing.",
    "'demo-us-cpi'",
)


def main() -> None:
    for path in YAML_FILES:
        with path.open(encoding="utf-8") as handle:
            document = yaml.load(handle, Loader=yaml.BaseLoader)
        if not isinstance(document, dict):
            raise SystemExit(f"{path.relative_to(ROOT)} must contain a YAML mapping")

    seed = DEMO_SEED.read_text(encoding="utf-8")
    missing = [marker for marker in REQUIRED_DEMO_MARKERS if marker not in seed]
    if missing:
        raise SystemExit(f"demo fixture is missing markers: {', '.join(missing)}")
    if "api_key" in seed.lower() or "password" in seed.lower():
        raise SystemExit("demo fixture must not contain credentials")

    print(f"validated {len(YAML_FILES)} YAML files and deterministic demo fixture markers")


if __name__ == "__main__":
    main()
