# Docs Folder Map

Last update: 2026-02-23

This file maps all current files in `docs/` and explains what each one is for.

## Inventory

| File | Type | Scope | Use When |
| --- | --- | --- | --- |
| `docs/monad_fleet_grpc_interface_v2.tex` | Spec (LaTeX) | Canonical Fleet v2 protocol + implementation profile | You need exact protocol/behavior contracts for agent/server integration. |
| `docs/monad_fleet_v2_phase1_playbook.md` | Operations playbook | Phase-1 practical runbook for Raspberry Pi deployments | You need run commands, acceptance criteria, or troubleshooting during operations. |
| `docs/pi_artifacts_reference.md` | Reference guide | Artifact semantics, naming, dedupe, bundling, validation | You need to interpret uploaded artifacts and run-summary outcomes. |
| `docs/agent_handoff_single_radio_2026-02-23.md` | Operational handoff | Latest verified Pi single-radio status and blocker summary | You are continuing CSI stabilization work in the next Codex/agent window. |
| `docs/monad_fleet_analysis_sequences_only.tex` | Analysis/design draft (LaTeX) | Theoretical analysis + implementation concept for wireless sensing and eLabFTW integration | You need architecture rationale and academic-style analysis context. |
| `docs/RFSensingPaper.tex` | Research paper draft (LaTeX) | Deterministic RF sensing protocol for multi-day/multi-participant experiments | You need research framing, methodology, and dataset protocol context. |

## Recommended Read Order

1. `docs/monad_fleet_grpc_interface_v2.tex`
2. `docs/monad_fleet_v2_phase1_playbook.md`
3. `docs/agent_handoff_single_radio_2026-02-23.md`
4. `docs/pi_artifacts_reference.md`
5. `docs/monad_fleet_analysis_sequences_only.tex`
6. `docs/RFSensingPaper.tex`

## Practical Notes

- Protocol truth source for implementation work is `docs/monad_fleet_grpc_interface_v2.tex`.
- Runtime operations on Pi should be guided by `docs/monad_fleet_v2_phase1_playbook.md`.
- Artifact/result interpretation should be cross-checked with `docs/pi_artifacts_reference.md`.
- The two `.tex` drafts (`monad_fleet_analysis_sequences_only.tex`, `RFSensingPaper.tex`) are design/research context, not deployment runbooks.
