# BookOrbit

Self-hosted library and reading platform for ebooks, PDFs, audiobooks and
comics. AGPL-3.0.

## How it is deployed

Everything is driven by Flux from this repo. Nothing is applied by hand.

- `clusters/staging/flux-system/gotk-sync.yaml` declares this repo as the
  `flux-system` GitRepository (branch `main`).
- `clusters/staging/apps.yaml` makes Flux reconcile `./apps/staging` and
  decrypt SOPS-encrypted Secrets with the cluster's age key.
- `apps/staging/bookorbit/` contains the `HelmRelease` and the two
  SOPS-encrypted Secrets.
- `apps/staging/flux-charts/bookorbit/` contains the `HelmChart`.
- `charts/bookorbit/` is the chart itself. Flux resolves it from the same
  GitRepository, so there is no second source and no HelmRepository.

The `HelmChart` uses `reconcileStrategy: ChartVersion`, so a change to the
chart ships by bumping `version` in `charts/bookorbit/Chart.yaml`.

### Why the HelmChart lives in `flux-system`

This cluster runs source-controller v1.6.2, whose `HelmChart` CRD has no
`sourceRef.namespace` field. A `HelmChart` can therefore only reference a
source in its own namespace, and the `flux-system` `GitRepository` lives in
`flux-system`. So the `HelmChart` goes there and the `HelmRelease` — which
does support `chartRef.namespace` — points back at it from `bookorbit`.

## Topology

Upstream runs three things: the Node app, PostgreSQL with pgvector, and an
optional read-aloud worker. The chart ships the app and the database; the
read-aloud worker is left out because it needs its own token and an optional
Kokoro TTS sidecar.

| Component | Image | Notes |
|---|---|---|
| app | `ghcr.io/bookorbit/bookorbit` (digest-pinned) | runs `migrate.js` then the server |
| postgres | `pgvector/pgvector:pg18` | pgvector is required, not vanilla postgres |
| cloudflared | `cloudflare/cloudflared:latest` | tunnel connector |

The app image ships a health check at `/api/v1/health`, which the chart uses for
both probes.

The app container runs as `PUID:PGID` (1000:1000) with a read-only root
filesystem and a memory-backed `/tmp`, mirroring upstream's compose
`read_only: true` plus `tmpfs: /tmp`. An init container waits for
PostgreSQL to become healthy so migrations never race the database.

The app image is pinned by multi-arch index digest rather than a tag, so the
node architecture does not matter and the deployed bits are immutable.

### PostgreSQL and the fresh-volume problem

The official postgres image starts as root, chowns `PGDATA`, then drops to the
`postgres` user with `gosu`. Forcing `runAsUser` skips that chown and `initdb`
then refuses the root-owned fresh volume. The chart therefore keeps
`fsGroup: 999` for group write access but does **not** pin `runAsUser` /
`runAsGroup`, and it grants `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETUID`,
`SETGID` so the image can do its own privilege drop.

## External exposure

The `bookorbit` Cloudflare Tunnel (id `bf307688-9a6f-4981-b864-350c917c49ce`)
is locally managed: the ingress rules live in the chart's `ConfigMap` and the
connector reads `credentials.json` from the SOPS-encrypted `tunnel-credentials`
Secret. DNS for `bookorbit.silasgroskreutz.com` already points at the tunnel.

There is deliberately no Traefik `Ingress` for this app. TLS terminates at
Cloudflare's edge, which keeps the ingress path free of a cert-manager
dependency the rest of the cluster does not have.

## Secrets

`apps/staging/bookorbit/secret.yaml` is SOPS-encrypted and holds
`POSTGRES_PASSWORD`, `JWT_SECRET`, `SETUP_BOOTSTRAP_TOKEN` and
`ROTATION_ID`. Values are never written in plaintext, in Git history, or in
logs.

`apps/staging/bookorbit/tunnel-secret.yaml` is SOPS-encrypted and holds the
tunnel `credentials.json`.

### Rotating a credential

The Secret is applied by Flux, not by the chart, so Helm cannot checksum it
and the pod would otherwise keep the old value in its environment after a
rotation. The `ROTATION_ID` key solves this:

1. Change the credential value in `apps/staging/bookorbit/secret.yaml`.
2. Bump `ROTATION_ID` (for example `r2` -> `r3`).
3. Commit and push.

The HelmRelease pulls `ROTATION_ID` out of the Secret through `valuesFrom` and
passes it to the chart as `app.secretRotationId`. The Deployment annotates
its pod template with it, so the bump changes the pod spec, the Deployment
rolls, and the new pod reads the new credential.

Verify the roll landed:

```
kubectl -n bookorbit get deploy bookorbit \
  -o jsonpath='{.spec.template.metadata.annotations.bookorbit\.silasgroskreutz\.com/secret-rotation}'
```

### First-run setup

Setup is one-shot: the app refuses setup once a user exists. The bootstrap
token is checked against the `x-setup-token` header on `POST /api/v1/auth/setup`.

```
kubectl -n bookorbit get secret bookorbit-secret \
  -o jsonpath='{.data.SETUP_BOOTSTRAP_TOKEN}' | base64 -d
```

The admin account created during setup was `silas` with the password stored
outside the repo in `~/.bookorbit-credentials` (mode 600).

## Library storage

The library is the NAS share `//192.168.0.2/public/Books`, 257 files.

Prodesk mounts that share at two paths:

| Path | Mount | uid | Writable by the app |
|---|---|---|---|
| `/mnt/nas/books` | `//192.168.0.2/public/Books` | 0 | no |
| `/mnt/nas-roms/Books` | `//192.168.0.2/public/roms` | 1000 | yes |

The two paths hold byte-identical content (verified by md5 over the file
names and sizes), but only the second presents the files as uid 1000, which is
the uid the app container runs as. So the chart mounts `/mnt/nas-roms/Books`
at `/books` in the container.

`/data` (covers, book-bucket, read-aloud jobs) is a local-path PVC on Prodesk.
