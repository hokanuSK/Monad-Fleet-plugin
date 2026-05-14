# Raspberry Pi Agent Bootstrap Bundle

This workflow covers only:

1. stabilize the agent runtime
2. define a per-device bundle format
3. provide a one-command installer for freshly flashed Ubuntu

WireGuard provisioning is intentionally excluded. Use this only when the device
already has working network access and WireGuard is already configured.

## Files

- `scripts/rpi/generate_agent_bootstrap_bundle.sh`
- `scripts/rpi/install_flashed_ubuntu_agent.sh`
- `scripts/rpi/agent_bundle_devices.example.csv`

## Bundle Format

Generated output lives under:

```text
artifacts/output/rpi-agent-bootstrap/<timestamp>/<hostname>/
```

Each device directory contains:

- `agent.env`
  - device-specific deployment parameters
  - fleet manager address
  - control plane iface
  - Wi-Fi scan iface
  - artifact upload target
  - metrics sink defaults
  - Prometheus labels and remote_write target
- `README.md`
  - install example for that device

Top-level output contains:

- `manifest.csv`
- `README.md`

## Generate Bundles

Edit a CSV matching:

```text
hostname,agent_id,control_plane_iface,wifi_scan_iface,capabilities,artifact_upload_target,default_metrics_sinks,grpc_dns_resolver,prom_pi_id,prom_site,prom_instance,prom_metrics_port,prom_exposition_port,prom_remote_write_url
```

Then run:

```bash
DEVICE_CSV=path/to/devices.csv \
  scripts/rpi/generate_agent_bootstrap_bundle.sh
```

## One-Command Installer

For a freshly flashed Ubuntu Pi that is already reachable:

```bash
PI_HOST=<host-or-ip> \
SSH_PROXY_JUMP=ladamik@34.198.184.128 \
scripts/rpi/install_flashed_ubuntu_agent.sh \
  artifacts/output/rpi-agent-bootstrap/<timestamp>/<hostname>
```

What the installer does:

1. removes `DNS=` from `/etc/wireguard/wg0.conf` if requested by the bundle
2. installs runtime prerequisites:
   - `python3.12-venv`
   - `iw`
   - `bluez`
   - `wireless-tools`
   - `tcpdump`
   - `prometheus`
3. writes `/etc/prometheus/prometheus.yml` to:
   - scrape `127.0.0.1:<prom_metrics_port>`
   - label the device for Grafana/Mimir
   - `remote_write` to Mimir
4. deploys agent code with `scripts/rpi/deploy_agent.sh`
5. installs the systemd unit with `scripts/rpi/install_systemd_service.sh`
6. verifies:
   - `iw`
   - `bluetoothctl`
   - Python imports for `grpc`, `google.protobuf`, `prometheus_client`
   - active `prometheus`
   - active `monad-fleet-agent.service`
7. validates experiment readiness with `scripts/rpi/validate_bootstrap_install.sh`:
   - local metrics endpoint responds
   - configured Wi-Fi scan interface is present
   - monitor interface creation works when the measurement NIC differs from the control-plane NIC

You can also run the validation step directly after a deploy or after first boot:

```bash
PI_HOST=<host-or-ip> \
SSH_PROXY_JUMP=ladamik@34.198.184.128 \
scripts/rpi/validate_bootstrap_install.sh \
  artifacts/output/rpi-agent-bootstrap/<timestamp>/<hostname>
```

## Runtime Stabilization Included

The runtime side currently includes:

- command-level `WIFI_SCAN_IFACE` is respected in `src/device-sim/agent_v2/main.py`
- fresh Ubuntu package prerequisites are installed before deploy
- broken WireGuard-provided DNS can be stripped before package install
- local Prometheus scrape + remote_write is configured as part of bootstrap
- passive 5 GHz monitor capture dependencies are present on first boot, including `tcpdump`
- when the measurement NIC differs from the control-plane NIC, bootstrap can now force that interface `UP` before the agent starts

That combination addresses the failure mode we saw on `monad-01`, where the
agent fell back to `iw link` status collection and never produced a pcap
because `iw` was missing and DNS prevented package installation, and where
Grafana stayed empty because the node had no local Prometheus path into Mimir.
