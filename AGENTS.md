# AGENTS.md — OpenDDIL Logistics Sim

Guidelines and safety constraints for AI agents working in this repository.

## Repository Scope

This repo is a **stand-in simulator**, not a model of record. It discovers
assets, synthesises element-level telemetry for them, and publishes it so the
rest of OpenDDIL has something to fuse and project when the upstream sim is not
available. Code lives under `src/logistics_sim/`; the entry point is
`logistics-sim` (`main:main`). Runtime shape comes from `config/default.yaml`.

## What You CAN Do

- **Edit the synthesis logic** in `element_gen.py`, the discovery logic in
  `asset_discovery.py`, and the emit path in `publisher.py`.
- **Edit `config/default.yaml`** to change rates, fractions, or topic names.
- **Run the tests**: `uv sync` then `uv run pytest src/tests/`.
- **Run the sim locally** against a local broker.

## What You MUST NOT Do

- ❌ **Never name the upstream customer simulator**, or describe its internals,
  anywhere in this repo — code, comments, config, or commit messages. Use
  "upstream sim" / "upstream integrator". This repo is public.
- ❌ **Never paste operator console output** into code, comments, or a commit
  message. Live dumps carry real asset identifiers, locations, and ORBAT names.
  Substitute synthetic labels first.
- ❌ **Never work around the commit-msg guard.** It is installed from private
  tooling and enforces the two rules above. If it fails a commit, fix the
  message — do not rephrase to slip past it, and do not restate its terms.
- ❌ **Never push.** Commit locally; the user decides every push.
- ❌ **Never modify sibling repos** from this repo's context. Each has its own
  agent guidelines.

## Shape Compatibility — the recurring bug

Captured fixtures and docs-derived JSON **drift from what the live sim emits**.
`aliases.py` exists because of this and must keep accepting *both* shapes.

- When you add or rename a field, extend the alias map rather than switching it.
- A passing unit test is not evidence the wire is right. Verify against a real
  broker (`rpk topic consume ...`) before calling anything wired.

## Known Gap — element state is a boolean today

Synthesis currently collapses every degraded condition into one boolean plus a
fixed fraction. Anything downstream that appears to read a tier from this sim
is reading a flattening. Treat a per-state tier matrix as the intended
direction, not a refactor to be improvised mid-task.

## Documentation Maintenance

After ANY change to this repo, update:

1. `README.md` — keep the configuration table and run commands current.
2. This file (`AGENTS.md`) — update the constraints if a new dangerous
   operation becomes possible.
