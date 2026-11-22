# Changelog

## 3.3.0+swarm — 2026-11-21

### Added

- Vercel Board-compatible SQLite and loopback HTTP engines, adapter conformance,
  and the `fusion-board` MCP server.
- Ten executable swarm forms with dynamic planning, measured capacity, budget
  limits, cancellation, and journal replay for verified partial output.
- Structured peer calls, ACP sessions, and durable handoffs. Claude-host Codex
  seats keep the AndrewCode routing policy.

### Fixed

- HiveBoard preserves request IDs on errors, fences expired attempts and
  receipts, bounds redelivery, and avoids losing task wakeups (B1–B13).
- Provider dispatch supports the classic race, speculate, tournament, harden,
  and evolve CLI patterns and cancels subprocess groups.

### Changed

- Provider scripts carry board identity and record observed model, session,
  and usage when available. OpenCode uses its model variant selector.
- AndrewCode honors explicit seat models and effort and relays progress.
  Existing native team form IDs and HiveBoard tools remain available.
