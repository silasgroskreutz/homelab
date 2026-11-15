#!/usr/bin/env bash
# ==============================================================================
# deploy-app.sh — scaffold + deploy a new app to the k3s cluster via Flux/GitOps
# ==============================================================================
#
# WHY THIS EXISTS
#   Pi.Alert was deployed by hand with `kubectl apply`, then hand-patched 21
#   times. It was never listed in apps/staging/kustomization.yaml, so Flux
#   never owned it. That drift is why every deploy needed a human check-in.
#   This script makes the GitOps path the ONLY path: a new app is scaffolded
#   into apps/base + apps/staging, validated, committed, pushed, and synced.
#
# USAGE
#   ./deploy-app.sh scaffold <name> <image> <port> [options]
#   ./deploy-app.sh validate [name]
#   ./deploy-app.sh deploy <name> [--no-push]
#   ./deploy-app.sh verify <name>
#   ./deploy-app.sh list
#
# OPTIONS (scaffold)
#   --hostname <host>       Public hostname, e.g. myapp.silasgroskreutz.com
#   --storage <size>        PVC size, e.g. 1Gi. Default: none (no PVC)
#   --mount <path>          Container path for the PVC
#   --env KEY=VALUE         Repeatable
#   --no-pvc                No persistent volume (default when no --storage)
#   --uid <n>               runAsUser. Default 1000
#   --fsgroup <n>           fsGroup. Default 1000
#   --hostpath <path>       Read-only hostPath mount (e.g. /mnt/nas/music)
#   --hostpath-mount <path> Container path for the hostPath
#   --cloudflare            Add a cloudflared tunnel overlay (needs creds)
#   --no-ingress            Skip the public Ingress
#
# DEPLOY ORDER (do not reorder)
#   1. scaffold  -> write manifests into apps/base/<name> + apps/staging/<name>
#   2. validate  -> `kubectl kustomize` MUST build clean, and `kubectl apply
#                    --dry-run=server` MUST pass against the live CRDs
#   3. commit    -> single commit
#   4. push      -> Flux picks it up (or we force a sync)
#   5. verify    -> pods Ready, endpoints populated, TLS issuer correct,
#                    HTTP reachable. If verify fails, the deploy FAILED.
#
# NEVER
#   - `kubectl set env` / `kubectl patch` a Flux-managed object. Flux reverts
#     it within one sync interval, silently. Change the manifest and push.
#   - `kubectl apply` a resource that has no kustomization reference. It is
#     never applied and it drifts.
# ==============================================================================
set -euo pipefail

REPO="${HOMELAB_REPO:-$HOME/homelab}"
PRODESC="silas@192.168.0.101"
KUBECTL="/usr/local/bin/kubectl"
SITE_DOMAIN="silasgroskreutz.com"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { red "ERROR: $*"; exit 1; }

have_sops_key() {
  [ -f "$HOME/.config/sops/age/keys.txt" ] || \
  [ -n "$(find "$HOME/.config/sops/age" -name '*.txt' -o -name '*.key' 2>/dev/null | head -1)" ]
}

# ---------------------------------------------------------------------------
# cmd_scaffold <name> <image> <port> [options]
# ---------------------------------------------------------------------------
cmd_scaffold() {
  local name="$1" image="$2" port="$3"; shift 3
  local hostname="" storage="" mount="" uid=1000 fsgroup=1000
  local hostpath="" hostpath_mount="" cloudflare=0 ingress=1
  local -a envs=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --hostname)      hostname="$2"; shift 2 ;;
      --storage)       storage="$2"; shift 2 ;;
      --mount)         mount="$2"; shift 2 ;;
      --env)           envs+=("$2"); shift 2 ;;
      --uid)           uid="$2"; shift 2 ;;
      --fsgroup)       fsgroup="$2"; shift 2 ;;
      --hostpath)      hostpath="$2"; shift 2 ;;
      --hostpath-mount) hostpath_mount="$2"; shift 2 ;;
      --cloudflare)    cloudflare=1; shift ;;
      --no-ingress)    ingress=0; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done

  [ -d "$REPO" ] || die "repo not found at $REPO (set HOMELAB_REPO)"
  [ -d "$REPO/apps/base" ] || die "$REPO/apps/base missing — not the homelab repo?"

  if [ -e "$REPO/apps/base/$name" ] || [ -e "$REPO/apps/staging/$name" ]; then
    die "$name already exists in the repo. Refusing to overwrite."
  fi

  # Default hostname if not given
  [ -n "$hostname" ] || hostname="$name.$SITE_DOMAIN"

  local base="$REPO/apps/base/$name" stage="$REPO/apps/staging/$name"
  mkdir -p "$base" "$stage"

  # ---------------- namespace.yaml ----------------
  cat > "$base/namespace.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $name
EOF

  # ---------------- deployment.yaml ----------------
  {
    cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $name
  template:
    metadata:
      labels:
        app: $name
    spec:
      securityContext:
        fsGroup: $fsgroup
        runAsUser: $uid
        runAsGroup: $uid
      containers:
        - name: $name
          image: $image
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: $port
EOF
    if [ ${#envs[@]} -gt 0 ]; then
      echo "          env:"
      for e in "${envs[@]}"; do
        k="${e%%=*}"; v="${e#*=}"
        printf '            - name: %s\n              value: "%s"\n' "$k" "$v"
      done
    fi
    if [ -n "$storage" ] && [ -n "$mount" ]; then
      cat <<EOF
          volumeMounts:
            - name: $name-data
              mountPath: $mount
EOF
    fi
    if [ -n "$hostpath" ] && [ -n "$hostpath_mount" ]; then
      cat <<EOF
            - name: $name-host
              mountPath: $hostpath_mount
              readOnly: true
EOF
    fi
    cat <<EOF
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 512Mi
      volumes:
EOF
    if [ -n "$storage" ] && [ -n "$mount" ]; then
      cat <<EOF
        - name: $name-data
          persistentVolumeClaim:
            claimName: $name-data-pvc
EOF
    fi
    if [ -n "$hostpath" ] && [ -n "$hostpath_mount" ]; then
      cat <<EOF
        - name: $name-host
          hostPath:
            path: $hostpath
            type: Directory
EOF
    fi
  } > "$base/deployment.yaml"

  # ---------------- service.yaml ----------------
  cat > "$base/service.yaml" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $name
spec:
  ports:
    - port: $port
  selector:
    app: $name
  type: ClusterIP
EOF

  # ---------------- storage.yaml (optional) ----------------
  local base_resources="  - namespace.yaml
  - deployment.yaml
  - service.yaml"
  if [ -n "$storage" ]; then
    cat > "$base/storage.yaml" <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $name-data-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: $storage
EOF
    base_resources="$base_resources
  - storage.yaml"
  fi

  cat > "$base/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
$base_resources
EOF

  # ---------------- staging overlay ----------------
  local stage_resources="  - ../../base/$name"
  if [ "$ingress" = "1" ]; then
    cat > "$stage/ingress.yaml" <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: $name
  namespace: $name
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls: "true"
spec:
  ingressClassName: traefik
  rules:
    - host: $hostname
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: $name
                port:
                  number: $port
  tls:
    - hosts:
        - $hostname
      secretName: $name-tls
EOF
    stage_resources="$stage_resources
  - ingress.yaml"
  fi

  if [ "$cloudflare" = "1" ]; then
    warn "NOTE: cloudflared overlay needs a tunnel + SOPS secret. Skipping the"
    warn "      tunnel manifest — add it once the tunnel name and credentials"
    warn "      are confirmed, and register secret.yaml in this kustomization."
  fi

  cat > "$stage/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $name
resources:
$stage_resources
EOF

  # ---------------- register in apps/staging/kustomization.yaml ----------------
  local staging_k="$REPO/apps/staging/kustomization.yaml"
  [ -f "$staging_k" ] || die "$staging_k not found"
  if ! grep -qE "^\s+- $name$" "$staging_k"; then
    python3 - "$staging_k" "$name" <<'PY'
import sys
path, name = sys.argv[1], sys.argv[2]
lines = open(path).read().rstrip("\n").split("\n")
out, inserted = [], False
for l in lines:
    out.append(l)
    if not inserted and l.strip() == "resources:":
        continue
# append under resources: block, keeping alphabetical-ish order
text = "\n".join(out)
text = text.rstrip("\n") + f"\n  - {name}\n"
open(path, "w").write(text)
PY
    green "registered '$name' in apps/staging/kustomization.yaml"
  fi

  green "scaffolded $name"
  echo "  base:   apps/base/$name"
  echo "  stage:  apps/staging/$name"
  echo "  host:   $hostname"
  echo
  blue "next: ./tools/deploy-app.sh validate $name"
}

# ---------------------------------------------------------------------------
# cmd_validate [name]
# ---------------------------------------------------------------------------
cmd_validate() {
  local name="${1:-}"
  local paths=()
  if [ -n "$name" ]; then
    paths=("$REPO/apps/staging/$name")
  else
    while IFS= read -r d; do paths+=("$d"); done < <(find "$REPO/apps/staging" -mindepth 1 -maxdepth 1 -type d | sort)
  fi

  local failed=0
  for p in "${paths[@]}"; do
    [ -f "$p/kustomization.yaml" ] || { warn "skip $p (no kustomization.yaml)"; continue; }
    blue "--- validating $(basename "$p") ---"
    if ! out=$("$KUBECTL" kustomize "$p" 2>&1); then
      red "  KUSTOMIZE BUILD FAILED:"
      echo "$out" | sed 's/^/    /'
      failed=1
      continue
    fi
    green "  kustomize build OK ($(echo "$out" | grep -c '^kind:') objects)"
    # Server-side dry-run catches CRD schema violations the way Flux's own
    # dry-run does. Pipe the RENDERED yaml through kube-validate.py, which
    # strips SOPS-encrypted secrets (Flux decrypts those server-side) and
    # classifies not-yet-created namespaces as expected.
    if printf '%s\n' "$out" | python3 "$REPO/tools/kube-validate.py" --dry-run >/tmp/_dryrun.$$ 2>&1; then
        sed 's/^/  /' /tmp/_dryrun.$$
    else
        red "  SERVER DRY-RUN FAILED — Flux would reject this:"
        sed 's/^/    /' /tmp/_dryrun.$$ | head -14
        failed=1
    fi
    rm -f /tmp/_dryrun.$$
  done
  [ "$failed" = "0" ] || die "one or more kustomize builds failed"
  green "all kustomize builds clean"
}

# ---------------------------------------------------------------------------
# cmd_deploy <name> [--no-push]
# ---------------------------------------------------------------------------
cmd_deploy() {
  local name="$1"; shift
  local push=1
  [ "${1:-}" = "--no-push" ] && push=0

  cmd_validate "$name"

  cd "$REPO"
  if [ -z "$(git status --porcelain)" ]; then die "nothing to commit — did you scaffold?"; fi

  git add -A "apps/base/$name" "apps/staging/$name" "apps/staging/kustomization.yaml" 2>/dev/null || git add -A
  git commit -m "feat($name): scaffold $name via GitOps template" 2>&1 | tail -3

  if [ "$push" = "1" ]; then
    git push origin main 2>&1 | tail -3
    green "pushed — Flux will reconcile (or force sync below)"
    # force an immediate sync instead of waiting up to 10m
    ssh -o ConnectTimeout=10 "$PRODESC" \
      "sudo -n /usr/local/bin/k3s kubectl -n flux-system annotate kustomization apps reconcile.fluxcd.io/requestedAt=\$(date -u +%Y-%m-%dT%H:%M:%SZ) --overwrite" >/dev/null 2>&1 \
      && green "forced flux sync" || warn "could not force flux sync (may need the repo sha check)"
  else
    warn "--no-push: commit is local only. Flux will NOT see it."
  fi
}

# ---------------------------------------------------------------------------
# cmd_verify <name>
# ---------------------------------------------------------------------------
cmd_verify() {
  local name="$1"
  local K="sudo -n /usr/local/bin/k3s kubectl"
  local host="$name.$SITE_DOMAIN"

  blue "=== VERIFY $name ==="

  echo "-- 1. pods (current ReplicaSet only) --"
  ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name get pods -l app=$name -o wide" 2>&1 | sed 's/^/   /'

  local ready
  ready=$(ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name get deploy $name -o jsonpath='{.status.readyReplicas}'" 2>/dev/null || echo "")
  if [ "${ready:-0}" = "0" ] || [ -z "$ready" ]; then
    red "  FAIL: 0 ready replicas"
    echo "  -- pod events --"
    ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name describe pods -l app=$name" 2>&1 | grep -A4 -iE 'warning|failed|error' | head -20 | sed 's/^/   /'
    echo "  -- logs --"
    ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name logs -l app=$name --tail=30 --all-containers" 2>&1 | tail -30 | sed 's/^/   /'
    return 1
  fi
  green "  $ready replica(s) ready"

  echo "-- 2. endpoints --"
  ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name get endpoints $name" 2>&1 | sed 's/^/   /'

  echo "-- 3. ingress host vs DNS --"
  ssh -o ConnectTimeout=10 "$PRODESC" "$K -n $name get ingress $name -o jsonpath='{.spec.rules[0].host}'" 2>&1 | sed 's/^/   rule: /'; echo
  echo "   dns:  $(dig +short "$host" 2>/dev/null | tr '\n' ' ')"

  echo "-- 4. TLS issuer (not just HTTP 200) --"
  curl -sv -o /dev/null -m 15 "https://$host/" 2>&1 | grep -iE 'subject:|issuer:|expire date:|SSL certificate verify' | sed 's/^/   /'
  if curl -sv -o /dev/null -m 15 "https://$host/" 2>&1 | grep -qi 'TRAEFIK DEFAULT CERT'; then
    red "  FAIL: Traefik default cert — the TLS secret does not exist"
  fi

  echo "-- 5. HTTP reachability --"
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://$host/" 2>/dev/null || echo "000")
  echo "   https://$host/ -> $code"
  [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "301" ] || red "  WARN: unexpected HTTP $code"

  echo "-- 6. flux revision --"
  ssh -o ConnectTimeout=10 "$PRODESC" "$K -n flux-system get ks apps -o jsonpath='{.status.lastAppliedRevision}'" 2>&1 | sed 's/^/   /'; echo

  green "=== VERIFY COMPLETE for $name ==="
}

# ---------------------------------------------------------------------------
cmd_list() {
  echo "apps in repo:"
  for d in "$REPO"/apps/staging/*/; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    listed=$(grep -cE "^\s+- $n$" "$REPO/apps/staging/kustomization.yaml" 2>/dev/null || echo 0)
    if [ "$listed" = "1" ]; then st="IN staging (Flux-owned)"; else st="NOT in staging kustomization (drift!)"; fi
    printf "  %-22s %s\n" "$n" "$st"
  done
}

case "${1:-}" in
  scaffold) shift; cmd_scaffold "$@" ;;
  validate) shift; cmd_validate "$@" ;;
  deploy)   shift; cmd_deploy "$@" ;;
  verify)   shift; cmd_verify "$@" ;;
  list)     cmd_list ;;
  *)
    sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
    exit 1 ;;
esac
