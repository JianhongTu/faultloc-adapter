# faultloc-adapter

`faultloc-adapter` converts [FLBench](https://github.com/JianhongTu/FLBench)
fault-localization instances into reproducible
[Harbor](https://github.com/harbor-framework/harbor) tasks. It is maintained as a
standalone adapter so FLBench does not need to fork or modify the Harbor framework.

## Status

The repository currently contains the Harbor `v0.20.0` adapter scaffold. The first
migration milestone is one fast FLBench instance whose vulnerable state and developer
patch reproduce the original verifier outcomes under Harbor.

## Boundary

The adapter will read FLBench instance metadata and materialize Harbor task directories.
Harbor remains responsible for running agents and verifiers; FLBench remains the source
of instance metadata, vulnerable images, and reference patches.

## Development

Requirements:

- Python 3.11 or newer
- `uv`
- Harbor `v0.20.0`
- Docker access to the FLBench/ARVO task images

Install the adapter package:

```bash
uv sync
```

Generate selected tasks after the adapter implementation is complete:

```bash
uv run faultloc-adapter \
  --task-ids <instance-id> \
  --output-dir datasets/faultloc-adapter
```

Generated datasets, Harbor jobs, and trial artifacts are intentionally excluded from
version control.
