# ADR 0009: Private state and signed-session authentication

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

The installation needs resumable setup, editable credentials, and secure local
or private-network access. Repository configuration must remain public-safe,
and the former browser authentication flow must not coexist with a second
operator login mechanism.

## Decision

Store activated operator configuration, credential values, administrator
authentication data, activation marker, and session secret in a private
persistent state volume. Create private files with `0600` permissions. Use
signed, HTTP-only, SameSite-strict sessions with expiry, CSRF validation,
same-origin mutation checks, and trusted-host enforcement. Lock setup routes
after activation.

## Consequences

- Repository files and images remain free of deployment secrets.
- Setup can be interrupted and resumed before activation.
- Backups must include both database and private state volumes.
- Network exposure still requires an operator-controlled reverse proxy or
  private-network boundary.

## Alternatives considered

- HTTP Basic authentication for every request.
- Credentials stored directly in repository YAML.
- Automatic Tailscale configuration by the application.

