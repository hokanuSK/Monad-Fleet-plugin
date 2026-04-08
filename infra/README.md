# Infra

Infrastructure and deployment assets:

- `docker/elabimg/`: custom eLabFTW image build context
- `observability/`: Prometheus, Mimir, Grafana configuration

Legacy root paths are kept as symlinks for compatibility:

- `elabimg -> infra/docker/elabimg`
- `observability -> infra/observability`
