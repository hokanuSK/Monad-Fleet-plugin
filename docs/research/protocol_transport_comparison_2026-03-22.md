# Fleet v2 Protocol Transport Comparison

**Date:** 2026-03-26  
**Scope:** Compare `protobuf-only` (unidirectional), `gRPC protobuf (HTTP/2 modeled)`, `REST/JSON`, and `JSON-RPC` for the Fleet v2 PREPARE/REPORT cycle.

## Executive Summary

Current ordering by transfer efficiency:

1. **protobuf-only** (best)
2. **gRPC protobuf (HTTP/2 modeled)**
3. **REST/JSON**
4. **JSON-RPC** (largest payload in this benchmark)

Key percentages (arithmetic mean of per-profile percentages across small/medium/large):

- `protobuf-only` is **53.39% smaller** than `REST/JSON`.
- `protobuf-only` is **54.60% smaller** than `JSON-RPC`.
- `gRPC protobuf (HTTP/2 modeled)` is **9.98% larger** than `protobuf-only`.
- `JSON-RPC` is **2.73% larger** than `REST/JSON`.

## Important Context About Bidirectional Communication

Current `fleet.v2` usage here is unidirectional RPC only (`Hello`, `GetPolicy`, `AckPrepared`, `PublishReport`, optional unidirectional `PublishEvents`).
With bidirectional streaming, percentages would likely shift because overhead amortization and flow-control behavior are different.

## Methodology

### Workload model

One PREPARE+REPORT cycle = 8 unidirectional messages:

1. `HelloRequest`
2. `HelloResponse`
3. `GetPolicyRequest`
4. `GetPolicyResponse`
5. `AckPreparedRequest`
6. `AckPreparedResponse`
7. `PublishReportRequest`
8. `PublishReportResponse`

Profiles:

- `small`: 10 report events
- `medium`: 50 report events
- `large`: 200 report events

### Transport model

- `protobuf-only`: raw protobuf body
- `gRPC protobuf (HTTP/2 modeled)`:
  - gRPC message frame: `8 * 5 B = 40 B`
  - HTTP/2 frame headers (modeled 5 frames per unidirectional RPC): `4 RPC * 5 * 9 B = 180 B`
  - HTTP/2 metadata (assumed): `4 RPC * 120 B = 480 B`
  - total modeled gRPC overhead per sequence: `700 B`
- `REST/JSON`: raw JSON body
- `JSON-RPC`: JSON-RPC envelope (`jsonrpc`, `id`, `method`, `params/result`) + JSON payload

### How HTTP/2 overhead was derived

The modeled gRPC overhead is built from protocol structure, not guessed:

- gRPC message frame: 5 B per message
- HTTP/2 frame header: 9 B per frame
- modeled unidirectional RPC flow: 5 frames per call
- modeled metadata after HPACK: 120 B per unidirectional RPC

For one PREPARE+REPORT sequence:

- `8 * 5 B` gRPC frames + `4 * 5 * 9 B` HTTP/2 frame headers + `4 * 120 B` metadata
- total = `700 B`

Sensitivity for metadata assumption:

| `H_meta` per RPC | Total overhead per sequence | small overhead | medium overhead | large overhead |
| ---: | ---: | ---: | ---: | ---: |
| 60 B | 460 B | 10.20% | 4.22% | 1.32% |
| 120 B | 700 B | 15.52% | 6.42% | 2.01% |
| 180 B | 1175 B | 26.06% | 10.77% | 3.37% |

### Measurement basis

- Same Fleet v2 message model for all variants.
- Protobuf + JSON derived from `proto/fleet_gateway_v2.proto`.
- CPU benchmark: medium payload, 5 runs x 8,000 iterations.

Raw artifact: `artifacts/output/research/protocol_transport_benchmark_2026-03-24_http2_modeled.json`.

## Payload Results

| Profile | protobuf-only [B] | gRPC protobuf (HTTP/2 modeled) [B] | REST/JSON [B] | JSON-RPC [B] |
| --- | ---: | ---: | ---: | ---: |
| small | 4,509 | 5,384 | 9,796 | 10,313 |
| medium | 10,909 | 11,784 | 23,346 | 23,863 |
| large | 34,910 | 35,785 | 74,159 | 74,676 |

## All-vs-All Percent Matrix (Medium Profile)

Interpretation: value = how much **row** is larger than **column**.
Negative means row is smaller.

| Row \\ Col | protobuf-only | gRPC protobuf (HTTP/2 modeled) | REST/JSON | JSON-RPC |
| --- | ---: | ---: | ---: | ---: |
| protobuf-only | 0.00% | -7.43% | -53.27% | -54.28% |
| gRPC protobuf (HTTP/2 modeled) | +8.02% | 0.00% | -49.52% | -50.62% |
| REST/JSON | +114.01% | +98.12% | 0.00% | -2.17% |
| JSON-RPC | +118.75% | +102.50% | +2.21% | 0.00% |

## All-vs-All Percent Matrix (Average Across Small+Medium+Large)

Interpretation: value = how much **row** is larger than **column**.
Negative means row is smaller.

| Row \\ Col | protobuf-only | gRPC protobuf (HTTP/2 modeled) | REST/JSON | JSON-RPC |
| --- | ---: | ---: | ---: | ---: |
| protobuf-only | 0.00% | -8.71% | -53.39% | -54.60% |
| gRPC protobuf (HTTP/2 modeled) | +9.98% | 0.00% | -48.77% | -50.16% |
| REST/JSON | +114.56% | +95.77% | 0.00% | -2.62% |
| JSON-RPC | +120.46% | +100.91% | +2.73% | 0.00% |

## gRPC vs protobuf (Main Focus)

From the benchmark:

- Overhead of `gRPC protobuf (HTTP/2 modeled)` vs `protobuf-only`: **2.51% to 19.41%** (profile-dependent).
- Average overhead: **9.98%**.

Conclusion: gRPC remains much better than JSON methods, but it is no longer “almost identical” to raw protobuf when HTTP/2 overhead is included.

## Different Number of Runs (gRPC vs protobuf-only)

Model-based (HTTP/2 estimate) for medium profile:

| Runs (`k`) | protobuf-only [B] | gRPC/HTTP2 [B] | Delta [B] | Delta [%] |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 10,909 | 11,784 | 875 | 8.02% |
| 5 | 54,545 | 58,920 | 4,375 | 8.02% |
| 10 | 109,090 | 117,840 | 8,750 | 8.02% |
| 50 | 545,450 | 589,200 | 43,750 | 8.02% |
| 100 | 1,090,900 | 1,178,400 | 87,500 | 8.02% |
| 1000 | 10,909,000 | 11,784,000 | 875,000 | 8.02% |

## Executed Unidirectional Wire Benchmark (Measured)

Measured artifact (executed): `artifacts/output/research/unidirectional_grpc_wire_benchmark_2026-03-24.json`.

Method:

- lightweight mock `FleetManager` server (same `fleet.v2` protobuf message shapes),
- real unidirectional gRPC transport bytes measured via local counting TCP proxy,
- protobuf-only baseline from serialized request/response payload bytes,
- fresh channel per scenario (`k=1,5,10,50,100,1000`).

Measured medium profile:

| Runs (`k`) | protobuf-only payload [B] | gRPC wire [B] | Delta [B] | Delta [%] |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 7,400 | 8,683 | 1,283 | 17.34% |
| 5 | 37,000 | 40,703 | 3,703 | 10.01% |
| 10 | 74,000 | 80,775 | 6,775 | 9.16% |
| 50 | 370,000 | 400,996 | 30,996 | 8.38% |
| 100 | 740,000 | 801,246 | 61,246 | 8.28% |
| 1000 | 7,400,000 | 8,006,146 | 606,146 | 8.19% |

Measured total transferred data for `k=1000` runs:

| Profile | protobuf-only payload [B] | gRPC wire [B] | Delta [B] | Delta [%] |
| --- | ---: | ---: | ---: | ---: |
| small | 1,858,000 | 2,464,120 | 606,120 | 32.62% |
| medium | 7,400,000 | 8,006,146 | 606,146 | 8.19% |
| large | 28,558,000 | 29,164,099 | 606,099 | 2.12% |

Interpretation:

- absolute byte delta grows approximately linearly with run count,
- relative percentage decreases as fixed setup overhead is amortized.

## CPU Results (Medium Payload)

| Operation | protobuf mean | JSON mean | JSON-RPC mean | protobuf advantage |
| --- | ---: | ---: | ---: | ---: |
| Serialize | 0.1438 s | 1.1904 s | 1.1827 s | 87.92% faster vs JSON, 87.84% faster vs JSON-RPC |
| Parse | 0.1355 s | 0.7879 s | 0.8300 s | 82.81% faster vs JSON, 83.68% faster vs JSON-RPC |

## Bottom Line

- If you want best transfer efficiency: **protobuf-only**.
- If you need RPC framework ergonomics and standard tooling: **gRPC protobuf** is still efficient, but carries measurable HTTP/2 overhead.
- `REST/JSON` is much heavier than protobuf variants.
- `JSON-RPC` is slightly heavier than REST/JSON in this workload.
