# Documentation Structure

This directory contains FleetManager documentation grouped by function.

## Directory Structure

### `specs/`
Protocol and interoperability specifications:
- `monad_fleet_grpc_interface_v2.tex` - Canonical `fleet.v2` protocol/profile specification

### `ops/`
Operational runbooks, handoffs, and execution workflow:
- `monad_fleet_v2_phase1_playbook.md` - Phase-1 deployment playbook
- `notion_ops_workflow.md` - Notion operations workflow
- `pi_artifacts_reference.md` - Raspberry Pi artifact semantics
- `agent_handoff_*.md` / `csi_future_work_*.md` - handoffs and forward plans

### `adr/`
Architecture Decision Records:
- `0001-repo-layout-normalization-2026-03-24.md` - repository normalization decision

### `runbooks/`
Cross-cutting operational runbooks:
- `repo-layout.md` - canonical repository path conventions and migration compatibility

### `research/`
Research and analysis drafts:
- `RFSensingPaper.tex`
- `monad_fleet_analysis_sequences_only.tex`
- `protocol_transport_comparison_2026-03-22.md` - quantified protobuf-only vs gRPC vs REST/JSON vs JSON-RPC transport analysis
- `bp_kapitola_analyza_transportov_2026-03-23.tex` - thesis-ready chapter analyzing protobuf/gRPC/REST/JSON-RPC tradeoffs

### `diagrams/`
Visual documentation:
- `*.mmd` - Mermaid source of truth
- `svg/*.svg` - rendered outputs for active diagrams
- `legacy/*.svg` - archived historical renders

## Key Documents

- **Protocol Specification**: `specs/monad_fleet_grpc_interface_v2.tex`
- **Operational Playbook**: `ops/monad_fleet_v2_phase1_playbook.md`
- **Notion Workflow**: `ops/notion_ops_workflow.md`
- **Diagram Guide**: `diagrams/README.md`
- **Layout ADR**: `adr/0001-repo-layout-normalization-2026-03-24.md`

## Build Commands

### LaTeX (specs)
```bash
cd docs/specs
latexmk -pdf -interaction=nonstopmode -halt-on-error monad_fleet_grpc_interface_v2.tex
```

### LaTeX (research)
```bash
cd docs/research
pdflatex -interaction=nonstopmode -halt-on-error RFSensingPaper.tex
```

### Diagrams
See `docs/diagrams/README.md`.
