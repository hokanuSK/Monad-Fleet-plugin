# Artifacts

Runtime state and generated artifacts that are environment-dependent:

- `data/`: local persistent service state and bind-mount data
- `tmp/`: temporary workspace files
- `output/`: generated reports, smoke context outputs, research artifacts

Legacy root paths are kept as symlinks for compatibility:

- `data -> artifacts/data`
- `tmp -> artifacts/tmp`
- `output -> artifacts/output`
