#!/usr/bin/env bash
# Regenerate apps/base/backup/configmap.yaml from the engine and its config.
#
# The backup pod takes its logic from Git, not from a baked image: the
# engine (tools/backup/backup.py) and its JSON configuration
# (tools/backup/config.json) are packed into a ConfigMap that Flux mounts
# at /scripts. That keeps the backup logic reviewable and changeable
# without rebuilding an image.
#
# This script is the deterministic generator for that ConfigMap. Run it in
# the SAME commit as any change to backup.py or config.json:
#
#   ./tools/backup/gen-configmap.sh
#
# It uses `kubectl create configmap --from-file`, which produces exactly
# the structure the API server expects (one key per file, base64 in
# `data`). The result is committed to Git so that Flux applies it.
#
# There are NO credentials in these files. A future app that needs one must
# get it from a SOPS-encrypted Secret, never from a plaintext ConfigMap
# in Git.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

out="apps/base/backup/configmap.yaml"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

kubectl create configmap backup-scripts \
  --from-file=backup.py=tools/backup/backup.py \
  --from-file=config.json=tools/backup/config.json \
  --dry-run=client -o yaml > "$tmp"

python3 - "$tmp" "$out" <<'PY'
import sys

src, dst = sys.argv[1], sys.argv[2]
lines = open(src).read().splitlines()

header = [
    "# GENERATED FILE - do not edit by hand.",
    "# Source: tools/backup/backup.py, tools/backup/config.json",
    "# Regenerate with: ./tools/backup/gen-configmap.sh",
    "# The backup engine and its configuration come from Git this way so the",
    "# backup logic is reviewable and changeable without an image rebuild.",
]

out = []
for line in lines:
    # kubectl emits a null creationTimestamp; it is noise in a manifest that
    # lives in Git and changes nothing when applied.
    if line.strip() == "creationTimestamp: null":
        continue
    out.append(line)

with open(dst, "w") as handle:
    handle.write("\n".join(header + out) + "\n")
print(f"wrote {dst}")
PY
