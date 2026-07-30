"""Frozen instance manifests.

A manifest is the single frozen input for one FLBench instance. It carries
everything needed to generate a Harbor task, so generation is reproducible
without access to FLBench's database, S3, or the network.

The manifests are shipped, not built: `data/eval500.tar.gz` holds all 500 of them
alongside the gold reports, and `manifests/` is gitignored because it is the
extracted copy. The freezing tool that originally produced them was removed once
the instance set was locked -- it needed a FLBench checkout and the network, which
is exactly what this repo is not supposed to depend on. Recover it from git
history if the set ever has to change.

    sha256sum -c data/eval500.tar.gz.sha256
    tar -xzf data/eval500.tar.gz
"""

import json
from pathlib import Path

MANIFEST_VERSION = 2


def load(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path}: manifest_version {manifest.get('manifest_version')} "
            f"!= supported {MANIFEST_VERSION}"
        )
    return manifest
