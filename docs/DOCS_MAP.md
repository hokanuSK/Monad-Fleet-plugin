# Docs Folder Map

Last update: 2026-03-23

This file maps all current files in `docs/` and explains what each one is for.

## Inventory

| File | Type | Scope | Use When |
| --- | --- | --- | --- |
| `docs/specs/monad_fleet_grpc_interface_v2.tex` | Spec (LaTeX) | Canonical Fleet v2 protocol + implementation profile | You need exact protocol/behavior contracts for agent/server integration. |
| `docs/ops/monad_fleet_v2_phase1_playbook.md` | Operations playbook | Phase-1 practical runbook for Raspberry Pi deployments | You need run commands, acceptance criteria, or troubleshooting during operations. |
| `docs/ops/pi_artifacts_reference.md` | Reference guide | Artifact semantics, naming, dedupe, bundling, validation | You need to interpret uploaded artifacts and run-summary outcomes. |
| `docs/ops/notion_ops_workflow.md` | Operations workflow | Notion ops capture model, env flags, fallback behavior | You are enabling/operating Notion-backed execution memory from smoke flows. |
| `docs/ops/agent_handoff_notion_ops_2026-03-19.md` | Operational handoff | Latest Notion ops integration status + runtime verification results | You are continuing from the latest smoke/Notion flow implementation work. |
| `docs/ops/agent_handoff_single_radio_2026-02-23.md` | Operational handoff | Latest verified Pi single-radio status and blocker summary | You are continuing CSI stabilization work in the next Codex/agent window. |
| `docs/ops/csi_future_work_2026-03-18.md` | Future work note | CSI capture hardening backlog and near-term plan | You are planning next CSI reliability improvements. |
| `docs/research/monad_fleet_analysis_sequences_only.tex` | Analysis/design draft (LaTeX) | Theoretical analysis + implementation concept for wireless sensing and eLabFTW integration | You need architecture rationale and academic-style analysis context. |
| `docs/research/RFSensingPaper.tex` | Research paper draft (LaTeX) | Deterministic RF sensing protocol for multi-day/multi-participant experiments | You need research framing, methodology, and dataset protocol context. |
| `docs/research/bp_kapitola_analyza_transportov_2026-03-23.tex` | Thesis chapter (standalone LaTeX) | Bachelor-thesis chapter with measured analysis of protobuf-only, gRPC, REST/JSON, and JSON-RPC | You need a single-file chapter that compiles directly in Overleaf. |
| `docs/research/protocol_transport_comparison_2026-03-22.md` | Benchmark analysis note (Markdown) | Percentage all-vs-all comparison of protobuf-only, gRPC protobuf, REST/JSON, and JSON-RPC for Fleet v2 PREPARE/REPORT flows | You need quantitative transport/CPU tradeoffs to choose protocol direction. |
| `docs/diagrams/*.mmd` | Visual source | Source-of-truth sequence and architecture diagrams | You need to update protocol/process visualization. |
| `docs/diagrams/svg/*.svg` | Rendered output | Latest active rendered diagrams | You need embeddable static assets for specs/review. |

## Recommended Read Order

1. `docs/specs/monad_fleet_grpc_interface_v2.tex`
2. `docs/ops/monad_fleet_v2_phase1_playbook.md`
3. `docs/ops/agent_handoff_notion_ops_2026-03-19.md`
4. `docs/ops/notion_ops_workflow.md`
5. `docs/ops/agent_handoff_single_radio_2026-02-23.md`
6. `docs/ops/pi_artifacts_reference.md`
7. `docs/ops/csi_future_work_2026-03-18.md`
8. `docs/research/monad_fleet_analysis_sequences_only.tex`
9. `docs/research/RFSensingPaper.tex`
10. `docs/research/bp_kapitola_analyza_transportov_2026-03-23.tex`
11. `docs/research/protocol_transport_comparison_2026-03-22.md`

## Practical Notes

- Protocol truth source for implementation work is `docs/specs/monad_fleet_grpc_interface_v2.tex`.
- Runtime operations on Pi should be guided by `docs/ops/monad_fleet_v2_phase1_playbook.md`.
- Notion-linked smoke operations should follow `docs/ops/notion_ops_workflow.md`.
- Start new agent windows from `docs/ops/agent_handoff_notion_ops_2026-03-19.md`.
- Artifact/result interpretation should be cross-checked with `docs/ops/pi_artifacts_reference.md`.
- Research papers/analysis in `docs/research/` are design context, not deployment runbooks.
