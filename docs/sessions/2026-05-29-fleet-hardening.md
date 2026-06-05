# Fleet hardening — 2026-05-29

A working session that turned an unreliable fleet (no successful new
experiment in the days since exp 174) back into one that captures and uploads
end-to-end. Three independent bugs and two configuration gaps were untangled
and fixed.

## Initial state

- Branch: `codex/agent-bootstrap-runtime`
- Fleet: 5 Pis (monad-02..06), each Pi 5 with Intel AX210 via M.2 HAT+; all
  Ubuntu 24.04 since the monad-02 reflash on 2026-05-21.
- The Pis had been relocated to a **new physical location** with a new uplink
  (`Guest` WiFi, library at a university) — 143 ms VPN RTT to EC2 instead of
  the previous network's much lower RTT.
- exp 174's 23 h captures had completed but uploads were partially stuck (~1
  GB of pcap/BLE-tx-log artifacts had to be pulled manually and re-uploaded
  to elabftw via the API in this same session).
- Every smoke and 19 h policy created in the session got 0 captures and 0
  uploads.

## Bug 1 — Agent treats future `measure_from` as immediately complete

### Symptom

`exp 175`, `exp 176`: fleet-service auto-provisioned the policy, all 5
agents received it within seconds, and within **1 second** every agent
logged:

```
Run <uuid> stored locally and queued for later upload
```

…then on the next poll cycle:

```
Skip execution for policy_id=… mode=once reason=execution already completed
```

Captures never started. The execution-index recorded `state=reported`,
`completed_runs=2`, `started_runs=1` despite no commands ever running.

### Root cause

In [src/device-sim/agent_v2/main.py](../../src/device-sim/agent_v2/main.py),
the per-group dispatch loop is gated by `in_window(group.from, group.to)`
(defined in [core.py](../../src/device-sim/agent_v2/core.py)):

```python
def in_window(start, end):
    now = now_utc()
    return ts_to_datetime(start) <= now <= ts_to_datetime(end)
```

`in_window` returns `False` for windows in the **past** *or the future*.
When a policy arrived with `measure_from` in the future (e.g. `+90 s` to
give the fleet-service time to ingest), every group was skipped, the run
was finalized as an empty `measured` report, marked `reported`, and
`execution_skip_reason` kept rejecting subsequent polls with
`"execution already completed"`.

`exp 174` had avoided this only because its `measure_from` was set 6 s in
the *past* (clock skew between scheduler creation time and what was
written), so `in_window` happened to return `True` immediately.

### Fix

Patch in `main.py` between the `skip_reason` check and the `AckPrepared`
call: if `measure_from > now()`, log a `Measure window not yet open:
deferring` message and `continue` to the next poll cycle instead of
proceeding to dispatch.

```python
measure_from_dt = parse_iso(measure_from_iso) if measure_from_iso else None
if measure_from_dt is not None and measure_from_dt > now_utc():
    wait_s = max(0.0, (measure_from_dt - now_utc()).total_seconds())
    log.info("Measure window not yet open: deferring (opens in %.0fs at %s)",
             wait_s, measure_from_iso)
    flush_pending_reports_guarded("waiting-for-measure-window", bypass_slot_gate=True)
    if reached_max_cycles(cycles, max_cycles):
        break
    time.sleep(poll)
    continue
```

This is the only code change in this PR.

## Bug 2 — Fleet-service silently ignores experiments without the `fleet` tag

### Symptom

`exp 177` (`measure_from` in the past) and `exp 178` were created with full
policy metadata but the fleet-service never logged a
`Grafana dashboard auto-provisioned` line for them. Agents polled and saw
`No assignment/work` the entire window.

### Root cause

The fleet-service container polls elabftw with a hard-coded filter:

```
GET /api/v2/experiments?limit=20&offset=0&tags[]=fleet
```

Earlier smoke scripts in this session created experiments via POST + PATCH
without adding any tags. Those experiments are simply invisible to the
fleet-service.

### Fix

Procedural — the smoke / 24 h launcher scripts now always POST the `fleet`
tag (and a small set of descriptive tags) right after the metadata PATCH.
No code change.

## Bug 3 — RPC timeouts too tight for 143 ms VPN RTT

### Symptom

`exp 181` (the first run after the wait-fix landed): every agent
deferred correctly, then captures started and ran the full 5 min window.
After captures stopped, every agent logged `ReportCommandStatus failed …
DEADLINE_EXCEEDED` repeatedly, then:

```
PublishReport confirmation retry 1/2 run_id=… timeout=15s
PublishReport confirmation retry 2/2 run_id=… timeout=15s
PublishReport replay failed for run_id=…: DEADLINE_EXCEEDED
Run … stored locally and queued for later upload
```

11.8 MB pcaps sat in `data/pending/<run_id>/artifacts/`, never reaching
the fleet-service spool or elabftw.

### Root cause

The control / report gRPC timeouts were configured for the old network:

| env var | old value | applied via |
|---|---|---|
| `CONTROL_RPC_TIMEOUT_S` | 30 (where set) or 10 (default) | `.env` on each Pi |
| `HELLO_RPC_TIMEOUT_S` | 60 (where set) | `.env` on each Pi |
| `REPORT_RPC_TIMEOUT_S` | 90 (default) | sinks.py |
| `REPORT_RPC_CONFIRM_TIMEOUT_S` | **15** (`min(timeout_s, 15)`) | sinks.py |
| `REPORT_RPC_CONFIRM_RETRIES` | 2 (default) | sinks.py |
| `ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S` | 3 | `.env` on each Pi |

143 ms RTT × gRPC HTTP/2 handshake + body upload + fleet-side ingest
processing was exceeding the 15 s confirmation timeout for a 10 KB
`PublishReport`.

### Fix

Per-Pi `.env` raises (no code change):

```
ARTIFACT_UPLOAD_DURING_MEASURE_TIMEOUT_S=15   # was 3
CONTROL_RPC_TIMEOUT_S=120                      # was 30
HELLO_RPC_TIMEOUT_S=180                        # was 60
REPORT_RPC_TIMEOUT_S=180                       # was 90 (default)
REPORT_RPC_CONFIRM_TIMEOUT_S=90                # was 15 (default)
REPORT_RPC_CONFIRM_RETRIES=3                   # was 2 (default)
```

Three Pis (monad-03/05/06) lacked the `CONTROL_RPC_TIMEOUT_S` and
`HELLO_RPC_TIMEOUT_S` lines in `.env` entirely and were silently using
defaults — those were appended instead of sed-replaced.

## Bug 4 — monad-02 OOM crash loop (pre-existing, surfaced today)

### Symptom

When investigating why exp 174's uploads from monad-02 had stalled, found
the agent stuck in an infinite `signal=KILL` restart loop:
**1,072 restarts in 6 hours**, ~one every 20 s. `dmesg` showed
`Out of memory: Killed process … total-vm:2.6 GB rss-anon:1.7 GB` for the
agent process. The Pi has 1.9 GiB RAM and 0 swap.

### Root cause

monad-02's `data/pending/` had three stuck runs (exp 159, exp 169, exp 174)
totalling 415 MB of artifacts. On every restart the agent re-loaded the
state, allocated heap for every pending artifact's metadata + queued
upload buffers, and OOM-killed before completing the cycle.

### Fix

Procedural cleanup (no code change):
1. `mv data/pending/<run>/ data/failed/<run>/` for the 3 stuck runs on
   monad-02 (and stuck exp 174 / 159 runs on monad-03/05/06).
2. Set `state=failed_manual_cleanup` on the matching `in_progress`
   `data/execution-index.json` entries.
3. Delete orphaned `/tmp/wifi-5g-monitor-*.pcap` files (3.9 GB across the
   fleet — leftovers from interrupted captures).
4. Raise `ARTIFACT_UPLOAD_MAX_BYTES` from 524 288 000 (500 MB) to
   2 143 289 343 (≈ 2 GB) to match the fleet-service's `ARTIFACT_MAX_BYTES`,
   so over-size pcaps (e.g. monad-03's 560 MB in exp 174) are no longer
   refused.

After cleanup, monad-02's agent steady-state RSS dropped from **1.7 GB**
to **36 MB**.

## Bug 5 — Stuck data from exp 174 was never finalized to elabftw

Two of the five Pis (monad-02, monad-03) had ended exp 174 with artifacts
in `data/pending/` instead of `data/sent/`. monad-02 because of the OOM
crash loop above; monad-03 because its pcap (560 MB) was over the old 500
MB upload cap and the agent refused to send it.

### Fix

Pulled the artifacts directly off the Pis via SSH/scp and uploaded them
to elabftw via the API:

- monad-02: `wifi-5g-monitor.pcap` (405 MB), `ble-tx-log.json` (10 MB),
  `wifi-5g-channel-schedule.log` (19 MB), `run-summary.json`
- monad-03: `wifi-5g-monitor.pcap` (560 MB)

All five Pi sets are now complete on elabftw experiment 174. The full
exp 174 bundle (2.1 GB across 26 artifacts) is also staged locally under
`artifacts/analysis/exp174/raw/` with a `manifest.json` describing per-Pi
roles, run IDs, sizes, and SHA-256s.

## End-to-end validation (smoke 182)

After the wait-fix patch + raised RPC timeouts + `fleet` tag, a 4 min
capture / 5 min upload smoke ran cleanly on all 5 Pis:

| Pi | pcap size | RX bytes | DEADLINE_EXCEEDED |
|---|---|---|---|
| monad-02 | 11.88 MB | OK | 0 |
| monad-03 | 10.36 MB | OK | 0 |
| monad-04 | 9.31 MB | OK | 0 |
| monad-05 | (similar) | OK | 0 |
| monad-06 | (similar) | OK | 0 |

15 files on elabftw exp 182 within ~3 min of capture stop:
each Pi's `wifi-5g-monitor.pcap`, `run-summary.json`, and
`wireless-run-evidence-bundle.tar.gz`.

## Active state: exp 183 (24 h)

Launched at 16:30 UTC 2026-05-29:

- Captures: 2026-05-29 16:30Z → 2026-05-30 16:30Z (24 h)
- Upload window: 2026-05-30 16:30Z → 18:30Z (2 h)
- All 5 Pis as listeners (`WIFI_SCAN passive_monitor` on wlp1s0 +
  `BLE_SCAN continuous`). No advertiser.

Expected artifact sizes (extrapolated from exp 174): 350–600 MB pcap per
Pi, fleet total 1.8–3 GB. The 2 h upload window with the raised
RPC timeouts should fit comfortably — smoke 182's effective fleet
throughput was ~0.3–0.5 MB/s parallel, putting 24 h uploads at roughly
60–100 min realistic, 100–170 min worst case.

## Open issues filed

- **#24 — Agent ignores mid-flight policy patches to `measure_window.to`
  (no early-cancel for in-progress commands).** Bug filed earlier in
  this session. Workaround: edit the policy *before* any agent has
  dispatched, or accept that running tcpdumps run to their original
  command-level timeout.

- **#25 — Live pcap streaming during measurement (via wlan0 / 2.4 GHz /
  WireGuard).** Feature request. Today's design buffers the full pcap on
  the Pi's SD card during measurement, then bursts the whole thing through
  the 2 h upload window. Streaming chunks during measurement would smooth
  the load and reduce data loss risk on a Pi crash.

## Notes for next session

- The wait-fix lands in this PR. Apply to all 5 Pis manually until the PR
  merges (the deployed `.py` files in `~monad/monad-fleet-agent/agent_v2/`
  already have it from this session, but a fresh flash needs to install
  this version of `main.py`).
- The other uncommitted changes on `codex/agent-bootstrap-runtime`
  (ble_advertise.py, collectors.py, sinks.py, etc.) are unrelated WIP and
  should land in their own PR.
- The fleet-service `fleet`-tag requirement is undocumented in the
  fleet-service codebase (per the env vars and behaviour observed here);
  consider documenting it or adding a healthcheck that warns when an
  experiment has fleet policy but lacks the tag.
- Guest WiFi RTT is 143 ms today but is a public AP — expect variability.
  If it gets significantly worse, raise `REPORT_RPC_CONFIRM_RETRIES` to 5
  and `REPORT_RPC_TIMEOUT_S` to 300.
