#!/usr/bin/env bash
# Print the hostPath that backs an application's datastore PVC.
#
# WHY A hostPath INSTEAD OF THE PVC ITSELF
# A PersistentVolumeClaim can only be mounted by a pod in the SAME
# namespace as the claim. The application datastores live in the
# `changedetection`, `linkding`, `navidrome` and `bookorbit` namespaces
# and these CronJobs run in the dedicated `backup` namespace, so the PVCs
# cannot be mounted directly.
#
# The cluster is a single-node k3s cluster and the PVCs are provisioned by
# the local-path provisioner, which stores each volume in a directory on
# the node. Mounting those directories through a hostPath gives the backup
# pod the live datastore with no cross-namespace hack and no second
# writable copy.
#
# The path contains the PVC's UID, so it CHANGES if the PVC is ever
# deleted and recreated. When that happens:
#
#   ./tools/backup/datastore-path.sh <app>
#
# and update the matching apps/base/backup/cronjob-<app>.yaml with the
# new value.
#
# The backup pod mounts it read-only and the engine fails loudly if the
# datastore or the expected file is missing, so a stale path shows up as a
# failed backup rather than a silent empty one.
#
# Usage:
#   ./tools/backup/datastore-path.sh <app>   one app's path
#   ./tools/backup/datastore-path.sh         every app's path
set -euo pipefail

# app:namespace:pvc
apps=(
  "changedetection:changedetection:changedetection-data-pvc"
  "linkding:linkding:linkding-data-pvc"
  "navidrome:navidrome:navidrome-data-pvc"
  "bookorbit:bookorbit:bookorbit-data"
  "karakeep:karakeep:data-karakeep-0"
)

resolve() {
  local app="$1" ns="$2" pvc="$3"
  local pv path
  pv="$(kubectl -n "$ns" get pvc "$pvc" -o jsonpath='{.spec.volumeName}')"
  if [ -z "$pv" ]; then
    echo "no PV for $ns/$pvc" >&2
    return 1
  fi
  path="$(kubectl get pv "$pv" -o jsonpath='{.spec.local.path}')"
  if [ -z "$path" ]; then
    echo "no local path for pv/$pv (is it still local-path provisioned?)" >&2
    return 1
  fi
  printf '%-16s %s\n' "$app" "$path"
}

if [ "$#" -ge 1 ]; then
  for entry in "${apps[@]}"; do
    IFS=: read -r app ns pvc <<< "$entry"
    if [ "$app" = "$1" ]; then
      resolve "$app" "$ns" "$pvc"
      exit 0
    fi
  done
  echo "unknown app '$1' (known: ${apps[*]%%:*})" >&2
  exit 1
fi

for entry in "${apps[@]}"; do
  IFS=: read -r app ns pvc <<< "$entry"
  resolve "$app" "$ns" "$pvc"
done
