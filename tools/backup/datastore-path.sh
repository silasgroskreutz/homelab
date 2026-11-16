#!/usr/bin/env bash
# Print the hostPath that backs the changedetection datastore PVC.
#
# WHY A hostPath INSTEAD OF THE PVC ITSELF
# A PersistentVolumeClaim can only be mounted by a pod in the SAME
# namespace as the claim. The changedetection datastore PVC lives in the
# `changedetection` namespace and this CronJob runs in the dedicated
# `backup` namespace, so the PVC cannot be mounted directly.
#
# The cluster is a single-node k3s cluster and the PVC is provisioned by
# the local-path provisioner, which stores the volume in a directory on
# the node. Mounting that directory through a hostPath gives the backup
# pod the live datastore with no cross-namespace hack and no second
# writable copy.
#
# The path contains the PVC's UID, so it CHANGES if the PVC is ever
# deleted and recreated. When that happens:
#
#   ./tools/backup/datastore-path.sh
#
# and update apps/base/backup/cronjob.yaml with the new value.
#
# The backup pod mounts it read-only and the engine fails loudly if the
# datastore or its url-watches.json is missing, so a stale path shows up
# as a failed backup rather than a silent empty one.
set -euo pipefail

ns="${NAMESPACE:-changedetection}"
pvc="${PVC:-changedetection-data-pvc}"

pv="$(kubectl -n "$ns" get pvc "$pvc" -o jsonpath='{.spec.volumeName}')"
path="$(kubectl get pv "$pv" -o jsonpath='{.spec.local.path}')"

if [ -z "$path" ]; then
  echo "no local path for pv/$pv (is it still local-path provisioned?)" >&2
  exit 1
fi

echo "$path"
