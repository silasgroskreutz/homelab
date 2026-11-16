# Backup engine and restore procedures

This directory holds the backup engine, its configuration, and the restore
procedures for the four applications in the backup set.

- `backup.py` — the backup engine. Runs as a Kubernetes Job from the
  `backup` namespace. Logic comes from Git via a ConfigMap, not from the
  image.
- `config.json` — which apps to back up, what to exclude, what to verify.
- `datastore-path.sh` — prints the hostPath that backs each app's
  datastore PVC.
- `gen-configmap.sh` — regenerates `apps/base/backup/configmap.yaml` from
  `backup.py` and `config.json`. Run it in the SAME commit as any change to
  either file.
- `restore.sh` — restores a backup artifact into a datastore directory and
  verifies the result. See [Restoring](#restoring) below. It covers the
  changedetection.io artifact; the other three apps have their own
  restore notes in [Restoring the other apps](#restoring-the-other-apps).

## The backup set

One CronJob per application, all in the `backup` namespace, all running the
same engine and the same configuration with a different `--only` selection.
An app's job can therefore never back up another app's data and never
prune another app's artifacts.

| App | Kind | Schedule (America/Chicago) | Artifact | `keep` |
|---|---|---|---|---|
| Linkding | `sqlite` + `tar` | `37 0 * * *` | `Backups/linkding/linkding-<stamp>.db`, `Backups/linkding-data/linkding-data-<stamp>.tar.gz` | 3 |
| BookOrbit | `pgdump` + `tar` | `17 2 * * *` | `Backups/bookorbit/bookorbit-<stamp>.dump`, `Backups/bookorbit-covers/bookorbit-covers-<stamp>.tar.gz` | 3 / 2 |
| changedetection.io | `tar` | `23 4 * * *` | `Backups/changedetection/changedetection-<stamp>.tar.gz` | 2 |
| Navidrome | `sqlite` | `43 5 * * *` | `Backups/navidrome/navidrome-<stamp>.db` | 3 |

The schedules are deliberately spread out: no two of them are on the same
CIFS share at the same minute, and none of them sit on the hour. The
changedetection artifact is ~66 KB; the Navidrome artifact is ~82 MB and
the BookOrbit cover archive is ~288 MB, so the small backups are not
scheduled next to the large ones. `concurrencyPolicy: Forbid` only
prevents one CronJob from overlapping itself — it does NOT stop two
different CronJobs from running at the same time. The schedules are what
prevent that.

Retention is per app (`keep` in `config.json`). `prune()` only ever
touches the directories of the apps the running job is responsible for,
and inside those directories only deletes files this engine produced
(`<app>-<UTC stamp><suffix>`). A vendor's own backup or a manual dump in
the same directory is kept and logged as kept.

### Where each app's data actually lives

| App | Live location | Backup reads |
|---|---|---|
| Linkding | `/etc/linkding/data` on the `linkding` PVC | `db.sqlite3` (SQLite backup API) plus a tar of `assets/`, `favicons/`, `previews/` |
| BookOrbit | PostgreSQL in the `bookorbit` namespace, cover images in the `bookorbit` PVC | `pg_dump` of the `bookorbit` database plus a tar of `covers/` |
| changedetection.io | JSON store on the `changedetection` PVC | tar of the whole datastore |
| Navidrome | `navidrome.db` (+ WAL) on the `navidrome` PVC | SQLite backup API snapshot of the database |

Notes that are easy to get wrong:

- **Navidrome's `.db` file is stale.** Navidrome checkpoints rarely, so
  the `.db` on disk can be days old while a 127 MB `-wal` holds the current
  rows. Copying the `.db` loses them. The SQLite backup API reads the WAL
  and captures the live state. Measured 2026-11-16: the raw file held 1
  `user` row, the live database held 2.
- **Navidrome's native CLI is not in the backup image.** The pinned image
  has no `navidrome` binary (`command -v navidrome` → exit 127), so the
  engine's `navidrome` kind cannot run against it. Navidrome therefore
  uses `kind: sqlite`, which is where the native kind ends up anyway — it
  re-snapshots the CLI's output through the SQLite backup API.
- **Linkding's scope follows upstream's own `full_backup`**, read from the
  running container: `db.sqlite3` (through the same backup API),
  `assets/`, `favicons/`, `previews/`. Upstream deliberately leaves out
  `tasks.sqlite3` (the transient huey queue) and `secretkey.txt`
  (Django's SECRET_KEY — a credential, and this share is not a secret
  store; the app generates a fresh key when the file is missing).
- **BookOrbit's book files are not in the backup set.** They already live
  on the NAS (`/mnt/nas-roms/Books`), mounted into the app as `/books`.
  The cover tree is derived from the books' own artwork but is backed up
  anyway, because a rescan does not reproduce the stored metadata.

### Why the apps are mounted with a hostPath

A PersistentVolumeClaim can only be mounted by a pod in the SAME namespace
as the claim. These CronJobs live in the dedicated `backup` namespace and
the datastores live in `linkding`, `bookorbit`, `changedetection` and
`navidrome`, so the PVCs cannot be mounted directly.

The cluster is a single-node k3s cluster and the PVCs are provisioned by
the local-path provisioner, so each datastore is a directory on the node.
Each CronJob mounts that directory read-only through a hostPath. The path
contains the PVC's UID, so it changes if the PVC is recreated:

```sh
./tools/backup/datastore-path.sh            # every app
./tools/backup/datastore-path.sh navidrome  # one app
```

Update the matching `apps/base/backup/cronjob-<app>.yaml` when the path
changes. A stale path makes the job fail loudly; it does not silently
archive an empty directory.

### Credentials

BookOrbit's `pg_dump` needs the PostgreSQL password. It comes from the
SOPS-encrypted Secret `bookorbit-postgres` in the `backup` namespace
(`apps/staging/backup/secret.yaml`) — Kubernetes Secrets are
namespace-scoped, so the credential cannot be read out of the `bookorbit`
namespace. The job runs as the app-specific ServiceAccount
`backup-bookorbit`. No credential is ever in a ConfigMap.

### The rules the CIFS share imposes

- Every pod that writes to `/mnt/nas-roms` runs as `runAsUser: 1000`,
  `runAsGroup: 0`, `fsGroup: 1000`. The share is mounted
  `uid=1000,forceuid,gid=0,noforcegid`; a UID 1000 pod cannot overwrite a
  file created by root, and root cannot create files a UID 1000 pod can
  then delete.
- **SQLite is never written directly onto the share.** SQLite's online
  backup API does random-access I/O and produces ZERO-BYTE files on this
  mount. Every SQLite backup is staged on local disk, verified there, and
  then copied to the share with a plain sequential write.
- The copy is verified after writing: the artifact is read back from the
  share and its sha256 compared with the staged file.

---

## Restoring the other apps

`restore.sh` covers the changedetection.io artifact only. The other three
have different artifact formats, and each one has its own recovery path.

### Linkding (`linkding-<stamp>.db` + `linkding-data-<stamp>.tar.gz`)

The `.db` artifact is a single self-contained SQLite file: the WAL state
was folded in and the journal mode was converted to `DELETE`, so there is
no `-wal`/`-shm` sibling to restore alongside it. To restore:

1. Scale the app down: `kubectl scale deploy/linkding -n linkding --replicas=0`
2. Stop it writing to the old database first. **Do not** copy the artifact
   over a live `db.sqlite3`: a WAL-mode database with a stale `-wal` next
   to it will replay that WAL on top of the restored file. Remove the
   live `db.sqlite3`, `db.sqlite3-wal` and `db.sqlite3-shm` and then copy
   the artifact in as `db.sqlite3`.
3. Extract `linkding-data-<stamp>.tar.gz` over the data directory for the
   `assets/`, `favicons/` and `previews/` trees (the archive is rooted at
   the data directory's basename, so strip one component).
4. `secretkey.txt` is not in the artifact by design. Leave the live one in
   place if it exists; if it is missing, Linkding generates a fresh one on
   start.
5. Scale back up and confirm the bookmarks are listed.

### BookOrbit (`bookorbit-<stamp>.dump`)

A custom-format `pg_dump`. To restore:

```sh
kubectl scale deploy/bookorbit -n bookorbit --replicas=0
kubectl -n bookorbit exec sts/bookorbit-postgres -- \
  pg_restore --clean --if-exists --no-owner \
  -U bookorbit -d bookorbit < /path/to/bookorbit-<stamp>.dump
kubectl scale deploy/bookorbit -n bookorbit --replicas=1
```

The password comes from the same SOPS-encrypted Secret (`PGPASSWORD`).
The cover images are not in the dump — they are in
`bookorbit-covers-<stamp>.tar.gz`, which untars over the `covers/`
directory on the BookOrbit PVC. The book files themselves are not in
either artifact: they live on `/mnt/nas-roms/Books`.

### Navidrome (`navidrome-<stamp>.db`)

A single self-contained SQLite file, same WAL situation as Linkding:
remove the live `navidrome.db`, `navidrome.db-wal` and `navidrome.db-shm`
before copying the artifact in, or Navidrome will replay the stale WAL.
`artwork/`, `cache/` and `backup/` are deliberately not backed up — they
are regenerable caches.

## Restoring

The restore procedure below is the **changedetection.io** one.

The restore procedure was proven by hand in kanban task `t_c1345881`
against a scratch namespace with a deliberately damaged datastore. This
script encodes that procedure so the next operator does not have to
rediscover it at 03:00 with nobody watching.

```sh
./tools/backup/restore.sh \
  --artifact /mnt/nas-roms/Backups/changedetection/changedetection-20261116T210133Z.tar.gz \
  --target /path/to/scratch/datastore
```

The script needs only `tar`, `python3` and `sha256sum`. It does **not**
need a running changedetection instance, a reachable cluster, or the
`backup` namespace. It works from a plain shell on the node.

### Getting the script onto the node

The script is self-contained: it does not import anything from the repo,
and the only repo file it optionally consults is
`tools/backup/datastore-path.sh` (for the live-path detection), which it
degrades gracefully without. So any copy of it runs.

```sh
# from the machine that holds the repo
scp tools/backup/restore.sh silas@192.168.0.101:/tmp/restore.sh
ssh silas@192.168.0.101 'chmod +x /tmp/restore.sh'
```

The node's own `~/homelab` clone is a separate, older checkout that
cannot pull from GitHub (no SSH key for it on that host), so do not
assume `git pull` there will bring the script over.

On the node, `kubectl` from `PATH` is a snap build with no working
kubeconfig; `sudo -n /usr/local/bin/k3s kubectl` is the one that reaches
the API server. The script tries both, plus a direct glob of the
local-path provisioner directory, so it detects the live datastore either
way.

### The three things that break a restore

These are the non-obvious details. Each of them produces a restore that
*looks* like it worked.

**1. The artifact is not SQLite — it is JSON.**

changedetection.io release 0.39.22.1 uses JSON/file storage. In the
running container, `/app/changedetectionio/store.py` line 37 sets:

```python
self.json_store_path = "{}/url-watches.json".format(self.datastore_path)
```

There is no `changedetection.db` anywhere in the PVC. A restore means
untarring a directory, not `sqlite3 .restore`. The datastore holds
`url-watches.json` plus one subdirectory per watch containing the
last-seen content snapshots (`.txt`) and `history.txt`.

**2. The archive root prefix must be stripped.**

The archive members are rooted at `datastore/` — the basename of the
source directory `/datastore` at backup time. The datastore directory IS
the PVC mount root, so the untar must strip that prefix:

```sh
tar -xzf ARTIFACT -C /datastore --strip-components=1
```

Without `--strip-components=1` the files land in `/datastore/datastore/`
and `url-watches.json` is missing from `/datastore/`. **The app then comes
up with zero watches instead of erroring.** This is the single most likely
way to get a restore wrong. `restore.sh` detects the archive's root
prefix, refuses to restore an archive whose layout it does not recognise,
and strips one component on extraction.

**3. The `.txt` snapshots carry the history, not the JSON.**

Proven by A/B in `t_c1345881`: with `url-watches.json` intact but the
`.txt` snapshot files removed, the app still listed both watches and still
reported `history_n = 2` / `1` (it reads the count from `history.txt`) —
but reading the history **content** returned HTTP 500. Restoring the `.txt`
files brought it back to HTTP 200.

So a restore check that only verifies "the JSON parses and the watch count
matches" can pass while the history is broken. `restore.sh` therefore
verifies that **every file referenced by every `<uuid>/history.txt` exists
on disk and is non-empty**.

### `previous_md5` is the change-detection baseline

`previous_md5` in `url-watches.json` is what changedetection compares
against to decide whether a page changed. With the restored baseline
present, a recheck against an unchanged URL produces NO new snapshot. With
`previous_md5` blanked, the same recheck is treated as a first check and
writes a new snapshot (`history_n` 1 → 2).

A correct restore must preserve `previous_md5`. `restore.sh` reports the
baseline per watch so you can see it survived. The end-to-end confirmation
is still a live recheck against an unchanged URL after the app is back up.

### `secret.txt` is never restored from the artifact

`secret.txt` is the Flask session key — a credential. It is excluded from
the artifact by `config.json` on purpose. The app generates its own on
first start via `init_app_secret` in
`/app/changedetectionio/__init__.py`. Sessions do **not** carry over from
the live instance to a restored one; that is expected and correct.

`restore.sh` never sources `secret.txt` from the artifact. If an archive
somehow contains one, the script drops it during staging and says so. A
`secret.txt` in the target may only come from `--secret-file` (explicitly
supplied by the caller) or from a file that was already in the target
before the restore.

### Known issue: API tokens ride along in `url-watches.json`

The artifact contains `url-watches.json`, and that JSON carries
`api_access_token` and `rss_access_token` when API access is enabled. The
`/mnt/nas-roms` CIFS share has `password = false`, so those tokens are the
instance's only authentication, and the API token is accepted on write
routes. This was found during the restore test and filed separately as
`t_0b444f02`; the redaction-vs-inject-vs-accept decision is a human's to
make. `restore.sh` does not add, remove or log those tokens — it does not
print the contents of `url-watches.json`.

### Options

| Option | Meaning |
|---|---|
| `--artifact PATH` | the `.tar.gz` artifact to restore (required) |
| `--target DIR` | the datastore directory to restore **into** (required) |
| `--expect-count N` | expected number of watches. Required for an offline/after-the-fact restore where the live source is gone. |
| `--live-source DIR` | datastore directory to count the live watches from at restore time. Defaults to the detected live datastore path. |
| `--live-path HINT` | treat `HINT` as the live datastore path for the refusal check. |
| `--secret-file PATH` | copy this file into the target as `secret.txt`. The only accepted source. |
| `--clean` | delete the target's existing contents before restoring. |
| `--allow-live-target` | deliberate override permitting a restore into the live datastore. |
| `--verify-only` | run the checks against `--target` without extracting. |
| `--dry-run` | print the plan and the archive contents, touch nothing. |

### Safety rails

`restore.sh` refuses to restore into the live `changedetection`
datastore unless you pass `--allow-live-target`. It detects the live path
by, in order:

1. `--live-path`, if given;
2. `./tools/backup/datastore-path.sh` (needs `kubectl` and a reachable
   cluster);
3. a glob over `/var/lib/rancher/k3s/storage/*_changedetection_changedetection-data-pvc`.

The refusal is a hard exit (status 3) with no changes made. If the live
path cannot be detected at all, the script says so loudly rather than
pretending the guard is active.

The extraction goes to a staging directory first and is copied into the
target afterwards, so a truncated or corrupt archive cannot leave the
target half-restored.

### What it verifies

After restoring, the script fails loudly (exit 1) if any of these do not
hold:

- `url-watches.json` exists in the target and parses as JSON;
- its `watching` count matches the count in the live source at restore
  time, or the count the caller supplied with `--expect-count`;
- every file referenced by every `<uuid>/history.txt` exists on disk and
  is non-empty;
- every watch has a `previous_md5` field (reported per watch, so you can
  see the baseline);
- `secret.txt` is absent, pre-existing, or explicitly supplied — never
  taken from the artifact.

### What it prints

The artifact path and its sha256, the artifact size and member count, the
archive root prefix that was stripped, the target directory, the expected
watch count and where that count came from, the watch count, one line per
watch (url, history count, `previous_md5`), the total history entries, and
the full list of restored files. A restore nobody can audit is not a
restore.

### The full procedure

`restore.sh` covers the file-level restore and its verification. The
cluster-level steps around it are:

1. **Never restore under a running app.** Scale the app down first:

   ```sh
   kubectl scale deploy/changedetection -n changedetection --replicas=0
   ```

2. Restore into the datastore directory:

   ```sh
   ./tools/backup/restore.sh --artifact ARTIFACT --target /path/to/datastore
   ```

   For the live instance, `--target` is the PVC mount root and
   `--allow-live-target` is required. The target directory on the node is
   root-owned; run the restore with `sudo -n` if a plain read fails.

3. Bring the app back up:

   ```sh
   kubectl scale deploy/changedetection -n changedetection --replicas=1
   ```

4. Confirm the app comes up and lists the watches, and that a recheck
   against an **unchanged** URL does **not** append a new snapshot. That
   last check is the end-to-end proof that the `previous_md5` baseline
   survived the restore.

To rehearse the whole thing without touching the live instance, restore
into a scratch directory and point a scratch changedetection Deployment
at it, as `t_c1345881` did. Never restore over the live namespace's
datastore to test the procedure.
