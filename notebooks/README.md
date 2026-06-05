# 5 GHz + BLE Measurement Notebooks

This folder starts the notebook-first analysis layer for `docs/measurement_5ghz_ble_plan.md`.

## Implementation Proposal

Build the thesis analysis in four small phases:

1. **Notebook contract first**
   - Keep notebooks offline: they read exported files from `artifacts/analysis/<run_id>/`, not directly from Raspberry Pis.
   - Use stable tabular files so the analysis is reproducible even if the live system changes.
   - First notebook: `01_measurement_5ghz_ble_power_analysis.ipynb`.

2. **Normalize run data**
   - Convert 5 GHz pcaps into `wifi_5g_frames.csv`.
   - Copy the agent channel schedule to `wifi_5g_channel_schedule.jsonl`.
   - Export BLE transmitter/receiver logs to `ble_tx.csv` and `ble_rx.csv`.
   - Export Mimir/Prometheus power telemetry to `power_timeseries.csv`.
   - Add run metadata in `metadata.json`.
   - Current helper: `scripts/analysis/prepare_measurement_dataset.py`.

3. **Fill the analysis**
   - Sanity checks: record counts, coverage windows, gaps, clock skew.
   - 5 GHz: devices, OUI/vendor mix, channel occupancy, channel changes, RSSI, frame types, AP inventory.
   - BLE: join transmissions/receptions by `adv_id`, compute delay and RSSI-derived distance.
   - Power: under-voltage events, AX210 rail power, correlation with sniffer activity.

4. **Export thesis artifacts**
   - Write figures to `artifacts/figures/<run_id>/`.
   - Write summary tables to `artifacts/tables/<run_id>/`.
   - Write key numbers to `artifacts/tables/<run_id>/key_numbers.json`.

## Expected Dataset Layout

Place one run under:

```text
artifacts/analysis/<run_id>/
  metadata.json
  events.ndjson
  wifi_5g_frames.csv
  wifi_5g_channel_schedule.jsonl
  ble_tx.csv
  ble_rx.csv
  power_timeseries.csv
  command_status.csv
  wifi_capture_summary.csv
  dataset_summary.json
```

Minimum useful schemas:

```text
wifi_5g_frames.csv:
  timestamp, pi_id, channel, freq_mhz, bandwidth_mhz,
  src_mac, dst_mac, bssid, src_oui, frame_type, frame_subtype,
  frame_len, rssi_dbm, ssid

ble_tx.csv:
  adv_id, tx_timestamp, tx_pi_id

ble_rx.csv:
  adv_id, rx_timestamp, rx_pi_id, rssi_dbm

power_timeseries.csv:
  timestamp, pi_id, core_voltage_v, pmic_3v3_sys_current_a,
  pmic_3v3_sys_voltage_v, under_voltage, cpu_temp_c
```

For the 5 GHz pcap parser, use `tshark` first because it already understands radiotap and 802.11 fields. The extractor can come after this notebook contract is stable.

## Jupyter Know-How

Jupyter notebooks are `.ipynb` files made of cells:

- **Markdown cells** are notes, headings, formulas, and thesis explanation.
- **Code cells** run Python and show tables/plots directly below the cell.
- Run a cell with `Shift+Enter`.
- Restart the kernel when state gets confusing, then use **Run All** to prove the notebook is reproducible from top to bottom.
- Keep raw data outside git under `artifacts/analysis/`; keep the notebook and helper docs in git.
- Save final figures/tables from code, not by screenshot, so thesis outputs can be regenerated.

Local setup:

```bash
python3 -m venv .venv-notebooks
. .venv-notebooks/bin/activate
pip install -r notebooks/requirements.txt
jupyter lab notebooks/
```

When Jupyter opens in the browser, choose the `.venv-notebooks` Python kernel if prompted.

Prepare the current local experiment-62 evidence bundles:

```bash
python3 scripts/analysis/prepare_measurement_dataset.py \
  --experiment-id exp62 \
  --dataset-id exp62-local-evidence \
  --output artifacts/analysis/exp62-local-evidence \
  --bundle artifacts/tmp/exp62_monad01_evidence.tar.gz \
  --bundle artifacts/tmp/exp62_monad02_evidence.tar.gz \
  --bundle artifacts/tmp/exp62_monad03_evidence.tar.gz \
  --bundle artifacts/tmp/exp62_monad04_evidence.tar.gz
```

Prepare downloaded experiment-78 results:

```bash
python3 scripts/analysis/prepare_measurement_dataset.py \
  --experiment-id exp78 \
  --dataset-id exp78-latest \
  --output artifacts/analysis/exp78-latest \
  --bundle artifacts/analysis/exp78-latest/raw/run-30805a6f__report-replay__ag-e36a07__wireless-run-evidence-bundle__fcdc4bea.tar.gz \
  --bundle artifacts/analysis/exp78-latest/raw/run-f28b4512__report-replay__ag-80f4bf__wireless-run-evidence-bundle__4fc15320.tar.gz \
  --pcap artifacts/analysis/exp78-latest/raw/run-30805a6f__report-replay__ag-e36a07__wifi-5g-monitor__45b51060.pcap \
  --pcap artifacts/analysis/exp78-latest/raw/run-f28b4512__report-replay__ag-80f4bf__wifi-5g-monitor__e4afb742.pcap
```
