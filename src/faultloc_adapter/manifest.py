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
    # `crash_output` is whatever the target printed, and a fuzzer input echoed back
    # can carry NUL bytes -- 4 of the 500 do. That text is interpolated into
    # instruction.md, which Harbor passes to the agent as an ARGV ELEMENT of
    # `docker compose exec`. execve cannot carry a NUL, so Python raises
    # `ValueError: embedded null byte` before the fork and the trial errors in the
    # agent phase, deterministically, on every attempt. Strip at load so the
    # generator and every checker read the same bytes; the manifest file itself is
    # frozen and stays as it is.
    # Conditional so a manifest missing the field still fails loudly at the point
    # of use, as it did before, rather than rendering an empty report section.
    if isinstance(manifest.get("crash_output"), str):
        manifest["crash_output"] = manifest["crash_output"].replace("\x00", "")
    return manifest
