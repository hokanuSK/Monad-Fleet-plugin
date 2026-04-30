# Agent Handoff: Diagram Validity + Protocol Evidence (2026-04-09)

## User constraints (active)
- Focus on diagrams validity for protocol proposal.
- Do not modify/upload diagrams during validation.
- Continue downloading protocol-relevant papers into repo docs folder.

## Knowledge-base status
- Folder: `docs/knowledge-base/`
- Total PDFs now: 25
- Newly added in this pass:
  - `Routing_in_a_Delay_Tolerant_Network_SIGCOMM_2004.pdf`
  - `RFC_4838_Delay-Tolerant_Networking_Architecture.pdf`
  - `RFC_5050_Bundle_Protocol_Specification_legacy.pdf`
  - `RFC_9171_Bundle_Protocol_Version_7.pdf`
  - `RFC_9172_Bundle_Protocol_Security_BPSec.pdf`
  - `A_Survey_on_DTN_Routing_Protocols_2015.pdf`
  - `Routing_based_Protocols_in_Delay_Tolerant_Networks_Survey_2019.pdf`

## Most relevant evidence captured
- DTN model and constraints:
  - store-carry-forward / intermittent connectivity / finite buffers
  - source: `Routing_in_a_Delay_Tolerant_Network_SIGCOMM_2004.pdf`
- DTN architecture baseline and assumptions:
  - retransmission assumptions and security notes
  - source: `RFC_4838_Delay-Tolerant_Networking_Architecture.pdf`
- Bundle protocol standards:
  - BPv7 capabilities + explicit note that BP itself does not ensure end-to-end delivery assurance
  - source: `RFC_9171_Bundle_Protocol_Version_7.pdf`
- Bundle protocol security:
  - integrity/confidentiality services at protocol layer
  - source: `RFC_9172_Bundle_Protocol_Security_BPSec.pdf`
- Legacy custody-based retransmission context:
  - source: `RFC_5050_Bundle_Protocol_Specification_legacy.pdf`

## Diagram check highlights (no edits performed)
- Canonical upload outcomes in lifecycle:
  - `ACCEPTED`, `DUPLICATE`, `RPC fail` only
  - file: `docs/diagrams/sequence-lifecycle-v2-normalized.mmd`
- Mismatch found in scheduler flowchart:
  - includes `DUPLICATE or REJECTED -> sent`
  - file: `docs/diagrams/flowchart-windowed-upload-scheduler.mmd`
- Potentially lossy/ambiguous area:
  - maintenance split diagram compresses report response to `ACK/err` instead of canonical tri-state
  - file: `docs/diagrams/sequence-maintenance-runtime.mmd`

## Current conclusion state
- Proposal structure is strong for implementation direction (prepare/execute/maintenance with local pending spool).
- Not yet protocol-complete for a robust new protocol without clarifying:
  - `REJECTED` semantics (currently conflated with successful sent path in one diagram)
  - explicit delivery assurance/idempotency rules per payload/report
  - security/authentication/integrity/confidentiality path in runtime diagrams
  - queue/buffer/backpressure and retry policy details

## Operational notes
- Temporary browser-based full Mermaid render in `/tmp` hit Puppeteer timeout; semantic validation proceeded independently.
- One accidental state-run render earlier was reverted in prior step; diagrams left unchanged in this pass.
