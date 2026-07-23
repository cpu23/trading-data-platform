# ADR 0002: Server-rendered progressive-disclosure UI

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

The operator needs to understand current conditions within seconds while still
being able to inspect evidence, history, failures, and configuration. A dense
terminal-style dashboard or deep menu hierarchy conflicts with that goal.

## Decision

Use FastAPI, Jinja templates, restrained JavaScript, HTMX-style partial
interactions, and server-sent quote updates. Keep primary navigation limited to
the dashboard, asset pages, settings, logs, and data quality. Present compact
summary views first and reveal detailed evidence or role disagreement in
context.

## Consequences

- One delivery stack serves HTML and JSON APIs.
- The browser has little application state and no separate build pipeline.
- Accessibility and responsive behavior can be verified in rendered pages.
- Highly interactive client-only workflows may require more deliberate design.

## Alternatives considered

- React/Vue single-page application.
- A permanently visible operations sidebar.
- Separate cards for every AI role.

