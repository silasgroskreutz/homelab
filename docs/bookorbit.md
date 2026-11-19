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

The library is the NAS share `//192.168.0.2/public`. Two of its
subdirectories are mounted into the app: `Books` (ebooks) and `audiobooks`.

Prodesk mounts the NAS at four paths:

| Path | Mount | uid | Writable by the app |
|---|---|---|---|
| `/mnt/nas-books` | `//192.168.0.2/public/Books` | 0 | no |
| `/mnt/nas/books` | `//192.168.0.2/public/Books` | 0 | no |
| `/mnt/nas-roms` | `//192.168.0.2/public` | 1000 | yes |
| `/mnt/nas/music` | `//192.168.0.2/public/Music` | 0 | no |

`/mnt/nas-roms` is the **entire** `public` share, not `public/roms`. It has to
be: the chart mounts `/mnt/nas-roms/Books` and `/mnt/nas-roms/audiobooks`, and
the backup CronJobs write to `/mnt/nas-roms/Backups`. Only the `/mnt/nas-roms`
mount presents files as uid 1000, which is the uid the app container runs as,
so that is the one the chart uses.

### Two libraries, one browse root

`LIBRARY_BROWSE_ROOT` is a **single** root, and every library folder must sit
under it — the check is both lexical and canonical (`realpath`), so a folder
outside it cannot be created however it is spelled.

`Books` and `audiobooks` are **siblings** under `public/`, so their only
common ancestor is `/`, and the browse root is therefore `/`.

`/books` cannot move instead: 584 books and 5,188 `book_files` rows store
their absolute path, and `library_folders.path` is what reading progress and
annotations key off. Relocating it would mean rewriting every stored path for
no gain.

Widening the browse root does **not** expose the rest of the share. The two
libraries are mounted as **separate hostPath binds of the two
subdirectories**, so the container's mount namespace only ever contains
`/books` and `/audiobooks` — never `/mnt/nas-roms`, and never the share root
with `Music` and `Backups` in it. What `/` adds is the container's own
directories: `/app`, `/bin`, `/lib`, `/opt`, `/srv`, `/tmp`, `/usr` and `/var`
are `SYSTEM_FOLDERS` (hidden from the picker), `/etc`, `/root`, `/proc`,
`/sys`, `/dev` and `/run` are `BLOCKED_PATHS`, and `/data` is the app's own
data directory, which the picker marks `appData` and refuses.

Two more things fall out of having two roots:

- **The pod is pinned to `prodesk1`** (`nodeSelector`). The library volumes are
  hostPath binds of a CIFS mount that only exists there; without the pin a
  pod landing elsewhere comes up with an empty library.
- **Watch mode is off; auto-scan uses a cron instead.** inotify does not
  propagate through CIFS, so a file watcher would never fire.

### Audiobook library settings

Created through `POST /api/v1/libraries` (admin only — the `hermes_ops` API key
deliberately has no `manage_libraries`):

| Setting | Value | Why |
|---|---|---|
| `folders` | `["/audiobooks"]` | |
| `organizationMode` | `book_per_folder` | one book per directory, multi-track |
| `allowedFormats` | `m4b, mp3, m4a, opus, ogg, flac` | audio only |
| `formatPriority` | same list | |
| `coverAspectRatio` | `1/1` | audiobook covers are square |
| `watch` | `false` | inotify does not cross CIFS |
| `autoScanCronExpression` | `0 4 * * *` | daily, 5-field cron |
| `fileWriteEnabled` | `false` | never rewrite files on the NAS |

`allowedFormats` matters more than it looks. Roughly 19 of the audiobook
directories also contain an ebook (one has 74 mp3s plus a pdf). A folder
containing any audio file is grouped into a **single** candidate, so without
an audio-only format list the pdf/epub can win primary-file selection and
turn an audiobook into an ebook. Restricting the formats drops the ebook
companions from the candidates — which also means those 19 pdf / 11 epub /
3 mobi / 11 txt companions are in **no** library at all. They would need
their own folder to be catalogued.

### Mount table gotcha

`public/roms` contains only the 13 emulator folders (32X, Atari 2600,
gameboy, ...). `Books`, `Backups`, `Music` and `Movies` are **siblings** of
`roms`, not children. Mounting `//192.168.0.2/public/roms` at `/mnt/nas-roms`
makes `/mnt/nas-roms/Books` vanish and the pod fails with
`hostPath type check failed: /mnt/nas-roms/Books is not a directory`.

### Surviving a reboot

All four CIFS mounts use `_netdev,x-systemd.automount,nofail`. Prodesk boots
before the NAS answers, so a plain fstab mount races the network and fails
with `CIFS: VFS: Error connecting to socket. Aborting operation. (rc=-101)`.
`x-systemd.automount` makes the mount lazy — it attaches on first access,
after the network settles — and `nofail` keeps boot from blocking on it.

The mount table of the previous boot is recorded in `/home/silas/sweep.log` on
Prodesk. Check that first when a mount is missing after a reboot; it shows
what was actually mounted, not what was intended.

`/data` (covers, book-bucket, read-aloud jobs) is a local-path PVC on Prodesk.
