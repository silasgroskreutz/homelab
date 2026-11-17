#!/usr/bin/env python3
"""
Homelab app backup helper.

Runs inside a Kubernetes Job/CronJob pod as uid 1000. It stages every app's
state on the pod's local filesystem, verifies the staged artifact, and only
then copies it to the NAS over CIFS.

Three hard-won constraints are baked in here. Each was found by testing, not
by reading docs, and each silently produces a broken backup if ignored:

1. The NAS CIFS share (/mnt/nas-roms) is mounted with uid=1000,forceuid and
   dir_mode=0755. Only uid 1000 can create or write files there, so the pod
   must run as uid 1000.

2. SQLite's online backup API writes with random-access I/O and produces
   ZERO-BYTE files on this CIFS share. Every SQLite backup is therefore
   staged on local disk first and copied to the share with a plain sequential
   write afterwards.

3. Opening a WAL-mode SQLite database creates sibling -wal and -shm files
   next to it, and doing that on the CIFS share both pollutes the backup
   directory and misbehaves because SQLite's locking primitives do not
   work reliably over CIFS. The staged copy is therefore converted out of
   WAL mode into a single self-contained file, and verified LOCALLY, before
   it is copied to the share.

Usage:
    backup.py --config /scripts/config.json [--only NAME ...]

--only selects a subset of the configured apps. Every app has its own
schedule, so each CronJob runs the SAME config file but only the apps it
is responsible for. Without it, a run would back up every app at once.

Config file (JSON):
{
  "dest": "/backups/Backups",
  "keep": 14,
  "apps": [
    {"name": "changedetection", "kind": "tar", "source": "/datastore", "keep": 2},
    {"name": "bookorbit", "kind": "pgdump",
     "host": "bookorbit-postgres.bookorbit.svc.cluster.local",
     "user": "bookorbit", "database": "bookorbit", "keep": 3},
    {"name": "navidrome", "kind": "sqlite", "source": "/navidrome/navidrome.db", "keep": 3},
    {"name": "linkding", "kind": "sqlite", "source": "/linkding/db.sqlite3", "keep": 3}
  ]
}

The paths above are the mount points inside the BACKUP pod, not the paths
inside the application pod. Each application's PVC lives in the
application's own namespace and a PVC can only be mounted by a pod in the
same namespace, so the backup CronJob mounts the local-path directory of
that PVC instead (see tools/backup/datastore-path.sh).

5. The backup root is a SHARED tree. `dest` may point at a directory that
   also holds artifacts of other apps, so `prune()` only ever touches the
   per-app directories this configuration is responsible for. It must never
   walk the whole destination and delete what it does not own.

The "tar" kind (used for apps whose state is plain files rather than a
database) supports two extra keys:

  "exclude":       glob patterns, relative to the source root, that must not
                   enter the archive. changedetection.io keeps "secret.txt"
                   (the Flask session key) next to its datastore; that is a
                   credential and must not travel to a backup target.
  "verify":        checks applied to the STAGED archive, before it is copied:
                     "members":     paths that must be present in the archive
                     "json_member": archive member to parse as JSON
                     "json_key":    key in that JSON holding the record map
                     "min_count":   minimum number of records (default 1)
                     "compare_source": also count the live source and log
                     whether the two agree

  "redact":       credential keys stripped from a JSON member before it is
                  archived. Each spec names the member (relative to the
                  source root), the dotted path to the object holding the
                  credentials, and the keys to remove:

                    [{"member": "url-watches.json",
                      "path": "settings.application",
                      "keys": ["api_access_token", "rss_access_token"]}]

                  The point is that changedetection.io stores its API and
                  RSS bearer tokens INSIDE the datastore JSON. The API token
                  is accepted on the write routes too, and the backup target
                  is a CIFS share that also holds Books, Movies and Music -
                  not a secret store.

                  The removed VALUES are remembered and the finished archive
                  is scanned for them, so a token that also lives in another
                  member is caught rather than published. The redaction is
                  fail-CLOSED: an unreadable member, unparseable JSON, a
                  missing key path, or a spec that matches no member all
                  fail the whole backup run. A redaction that silently did
                  nothing would look exactly like one that worked.

                  A restored instance regenerates its own tokens on first
                  start - the app's store.py does this for a missing
                  api_access_token / rss_access_token, the same way it
                  generates secret.txt.

The "sqlite" kind takes a "verify" object as well:

  "verify":              checks applied to the STAGED copy, before it is
                         copied to the share:
                           "integrity_check": run PRAGMA integrity_check
                           "compare_tables":  tables whose row count in the
                                            staged copy must equal the row
                                            count in the live database

                         The table COUNT and the table NAMES are always
                         compared against the live database. The row counts
                         are only compared for the tables named here,
                         because a busy application writes to its database
                         while the backup runs and a volatile table would
                         then fail the run for the wrong reason. Name the
                         tables that hold the state worth protecting and
                         leave the churning ones out.

  "keep":                how many artifacts of this app to retain. The
                         top-level "keep" is the default for apps that do
                         not set their own.

Retention only ever deletes artifacts THIS ENGINE produced, which are the
files matching `<app>-<UTC stamp><suffix>`. A file that somebody else put
in the app's directory (a vendor's own backup, a manual test dump) is left
alone and logged. A retention policy that deletes files it does not own is
a data-loss bug wearing a housekeeping hat.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from typing import NoReturn

SHARE_MODE_NOTE = "cifs-forceuid-1000"


def log(msg: str) -> None:
    print(f"[backup] {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"[backup] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def ensure_dir(path: str) -> str:
    """Create a directory on the share, tolerating a pre-existing one.

    mkdir under CIFS with forceuid works for uid 1000, but the mode the share
    reports for a fresh directory can be restrictive for the group. Force 0755
    so a later stage can still list the contents.
    """
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        fail(f"cannot create {path} (need uid 1000 on the {SHARE_MODE_NOTE} share)")
    try:
        os.chmod(path, 0o755)
    except OSError:
        # Best effort: some CIFS servers reject chmod. Listing still works.
        pass
    return path


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_source(path: str, name: str, readonly: bool = False) -> sqlite3.Connection:
    """Open the live database for reading.

    Read-write is tried first because SQLite can then perform WAL recovery on
    a database whose previous writer crashed, which makes the backup a better
    artifact. It falls back to read-only when the file cannot be opened that
    way. Either way the backup API itself only reads from the source.

    `readonly` forces the read-only URI. That matters for a datastore that
    is mounted read-only: SQLite opens a WAL database read-write only if it
    can also write the -shm sibling, and on a read-only mount it cannot.
    """
    if readonly:
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error as exc:
            fail(f"{name}: cannot open source database {path} read-only: {exc}")
    try:
        return sqlite3.connect(path, timeout=30)
    except sqlite3.Error as exc:
        log(f"{name}: read-write open failed ({exc}); falling back to read-only")
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error as exc2:
            fail(f"{name}: cannot open source database {path}: {exc2}")


def _copy_source_for_snapshot(source: str, workdir: str, name: str) -> str:
    """Copy a source database and its WAL siblings onto local disk.

    A read-only mount blocks SQLite from CREATING the -shm file it needs
    before it can open a WAL-mode database. The failure is "unable to open
    database file" even though the .db file itself is perfectly readable,
    and it is what a WAL database without a -shm sibling hits (measured on
    linkding's tasks.sqlite3, 2026-11-16).

    Copying the .db and its -wal/-shm siblings to local disk gives SQLite a
    writable directory to work in. The copy is a plain sequential write, so
    it does not need the random-access I/O that the CIFS share cannot
    serve.
    """
    local = os.path.join(workdir, f"{name}-source.db")
    for leftover in (local, local + "-wal", local + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
    shutil.copyfile(source, local)
    copied = ["db"]
    for suffix in ("-wal", "-shm"):
        side = source + suffix
        if os.path.exists(side):
            shutil.copyfile(side, local + suffix)
            copied.append(suffix)
    log(f"{name}: copied the source to local disk ({', '.join(copied)}) "
        f"because the datastore mount cannot be opened by SQLite directly")
    return local


def snapshot_sqlite(source: str, dest: str, name: str,
                    workdir: str | None = None) -> None:
    """Copy a live SQLite database into `dest` with the online backup API.

    The API produces a transactionally consistent snapshot even while the
    application is writing in WAL mode. This is the recommended approach for
    hot SQLite databases; copying db+wal+shm by hand is not.

    A plain file copy of the .db is NOT equivalent: the .db file is only
    checkpointed occasionally, so a copy taken between checkpoints misses
    everything that is still in the -wal. Measured on this cluster's
    navidrome.db: the raw file held 1 user row while the live database held
    2, because the second user lived only in the WAL.

    Three attempts, in order:
      1. open the source read-write, because a WAL database whose writer
         crashed needs recovery to be readable and a read-only mount cannot
         recover it,
      2. open the source read-only,
      3. copy the source onto local disk and snapshot the copy, which works
         when the mount itself is the problem (read-only mount, or a WAL
         database with no -shm sibling to open).
    """
    last_error: Exception | None = None
    for readonly in (False, True):
        src = open_source(source, name, readonly=readonly)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
            return
        except sqlite3.Error as exc:
            last_error = exc
            log(f"{name}: backup API failed ({exc}); retrying read-only")
            for leftover in (dest, dest + "-wal", dest + "-shm"):
                if os.path.exists(leftover):
                    os.remove(leftover)
        finally:
            src.close()

    if workdir:
        local = _copy_source_for_snapshot(source, workdir, name)
        src = open_source(local, name, readonly=False)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
            return
        except sqlite3.Error as exc:
            last_error = exc
            log(f"{name}: backup API failed on the local copy ({exc})")
        finally:
            src.close()

    fail(f"{name}: sqlite backup API failed for {source}: {last_error}")


def _table_names(conn: sqlite3.Connection, name: str) -> list:
    """The user tables of a database, sorted."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    except sqlite3.Error as exc:
        fail(f"{name}: cannot read the table list: {exc}")
    return [row[0] for row in rows]


def _count_rows(conn: sqlite3.Connection, table: str, name: str) -> int:
    """Row count of one table, or a hard failure if it cannot be read."""
    try:
        return conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error as exc:
        fail(f"{name}: cannot count rows in table {table!r}: {exc}")


def _live_table_names(conn: sqlite3.Connection, name: str) -> "list | None":
    """The user tables of the LIVE database, or None if they cannot be read.

    The live side of a comparison is read softly on purpose: the artifact is
    still checked on its own, and a comparison that could not run is logged
    rather than quietly skipped.
    """
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    except sqlite3.Error as exc:
        log(f"{name}: cannot read the live table list ({exc}); live comparison skipped")
        return None
    return [row[0] for row in rows]


def open_live_soft(source: str, name: str) -> "sqlite3.Connection | None":
    """Open the live database read-only, or None when it cannot be read.

    This is only used for the live-vs-artifact comparison. A live database
    that cannot be opened (the application is down, or the WAL needs a
    recovery that a read-only mount cannot perform) must not abort the
    backup: the artifact is still verified on its own, and a comparison that
    could not run is logged rather than quietly skipped.
    """
    try:
        return sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        log(
            f"{name}: live source {source} cannot be opened read-only ({exc}); "
            f"live comparison skipped"
        )
        return None


def stage_sqlite(app: dict, workdir: str) -> str:
    """Produce a single self-contained, verified SQLite file on local disk.

    The artifact is checked against the LIVE database, not only for internal
    consistency. A database that passes integrity_check but holds different
    data than the running application is a backup of nothing, and it looks
    exactly like a good backup on the share.

    The table list and the table count are always compared. Row counts are
    compared for the tables named in "compare_tables" only, because a busy
    application writes while the backup runs and a volatile table would then
    fail the run for the wrong reason.
    """
    name = app["name"]
    source = app["source"]
    if not os.path.exists(source):
        fail(f"{name}: source database {source} does not exist")

    verify = app.get("verify", "integrity_check")
    if isinstance(verify, str):
        verify = {"integrity_check": verify == "integrity_check"}
    verify = verify or {}

    work = os.path.join(workdir, f"{name}.db")
    for leftover in (work, work + "-wal", work + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)

    # The online backup API produces a transactionally consistent snapshot even
    # while the application is writing in WAL mode. This is the recommended
    # approach for hot SQLite databases; copying db+wal+shm by hand is not.
    snapshot_sqlite(source, work, name, workdir)

    # The snapshot inherits the source's WAL journal mode. Convert it to a
    # rollback journal so the artifact is ONE file with no -wal/-shm siblings,
    # and so nothing later recreates those siblings next to it.
    conn = sqlite3.connect(work)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        if verify.get("integrity_check", True):
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                fail(f"{name}: integrity_check on staged copy returned {result!r}")
            log(f"verify: {name} integrity=ok")

        tables = _table_names(conn, name)
        if not tables:
            fail(f"{name}: the staged copy holds no tables at all")
        log(f"verify: {name} tables={len(tables)}")

        live = open_live_soft(source, name)
        if live is None:
            log(f"{name}: live comparison not performed")
        else:
            try:
                live_tables = _live_table_names(live, name)
                if live_tables is None:
                    pass
                elif live_tables != tables:
                    only_live = sorted(set(live_tables) - set(tables))
                    only_staged = sorted(set(tables) - set(live_tables))
                    fail(
                        f"{name}: the staged copy has {len(tables)} tables but the "
                        f"live database has {len(live_tables)} (only in live: "
                        f"{only_live}, only in staged: {only_staged}) - the artifact "
                        f"is not a copy of the live database"
                    )
                else:
                    log(f"verify: {name} table list matches the live database")

                    compare_tables = verify.get("compare_tables") or []
                    if isinstance(compare_tables, str):
                        compare_tables = [compare_tables]
                    if not isinstance(compare_tables, (list, tuple)):
                        fail(
                            f"{name}: compare_tables must be a list of table names, "
                            f"got {compare_tables!r}"
                        )
                    for table in list(compare_tables):
                        staged_rows = _count_rows(conn, table, name)
                        live_rows = _count_rows(live, table, name)
                        if staged_rows != live_rows:
                            fail(
                                f"{name}: table {table!r} holds {staged_rows} rows in the "
                                f"staged copy but {live_rows} in the live database"
                            )
                        log(
                            f"verify: {name} table {table} rows={staged_rows} "
                            f"matches the live database"
                        )
            finally:
                live.close()
    finally:
        conn.close()

    # Belt and braces: the staged file must stand alone.
    for leftover in (work + "-wal", work + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
            log(f"{name}: removed stray {os.path.basename(leftover)}")

    size = os.path.getsize(work)
    if size == 0:
        fail(f"{name}: staged backup is 0 bytes")
    return work


def stage_pgdump(app: dict, workdir: str) -> str:
    """pg_dump into a local file and verify it lists cleanly."""
    name = app["name"]
    work = os.path.join(workdir, f"{name}.dump")
    cmd = [
        "pg_dump",
        "-h", app["host"],
        "-p", str(app.get("port", 5432)),
        "-U", app["user"],
        "-d", app["database"],
        "--format=custom",
        f"--file={work}",
    ]
    # PGPASSWORD must already be in the environment (from a Secret).
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"{name}: pg_dump failed rc={result.returncode}: {result.stderr.strip()}")

    size = os.path.getsize(work)
    if size == 0:
        fail(f"{name}: pg_dump produced a 0-byte file")

    if app.get("verify", "pg_restore_list") == "pg_restore_list":
        check = subprocess.run(
            ["pg_restore", "--list", work], capture_output=True, text=True
        )
        if check.returncode != 0:
            fail(f"{name}: pg_restore --list failed: {check.stderr.strip()}")
        entries = len([ln for ln in check.stdout.splitlines() if ln.strip()])
        log(f"verify: {name} pg_restore_list_ok entries={entries}")
    return work


def stage_navidrome(app: dict, workdir: str) -> str:
    """Use Navidrome's own backup CLI, which is the upstream-supported path.

    NOTE: the shared backup image does not ship the navidrome CLI. Measured
    inside 192.168.0.101:5000/homelab-backup@sha256:42b1e010... (2026-11-16):
    `command -v navidrome` exits 127, `navidrome --version` reports "not
    found", and no navidrome binary exists anywhere in the image. This kind
    therefore CANNOT be used with that image - it fails with "navidrome
    backup create failed: [Errno 2] No such file or directory". Navidrome
    is backed up with kind "sqlite" instead, which is where this function
    ends up anyway: it re-snapshots the CLI's output through the SQLite
    backup API for consistency. Using this kind requires an image that
    carries the CLI.
    """
    name = app["name"]
    work = os.path.join(workdir, f"{name}-cli")
    os.makedirs(work, exist_ok=True)

    result = subprocess.run(
        ["navidrome", "backup", "create", "--backup-dir", work],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"{name}: navidrome backup create failed: {result.stderr.strip()}")

    created = [f for f in os.listdir(work) if f.endswith(".db")]
    if not created:
        fail(f"{name}: navidrome backup create produced no .db file")
    source = os.path.join(work, created[0])

    size = os.path.getsize(source)
    if size == 0:
        fail(f"{name}: navidrome backup produced a 0-byte file")

    # Navidrome's CLI copies the live database file. Re-snapshot it through
    # the SQLite backup API so the artifact is guaranteed transactionally
    # consistent rather than a byte copy of a database mid-write.
    final = os.path.join(workdir, f"{name}.db")
    src = open_source(source, name)
    try:
        dst = sqlite3.connect(final)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    conn = sqlite3.connect(final)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            fail(f"{name}: integrity_check on navidrome backup returned {result!r}")
        log(f"verify: {name} integrity=ok")
    finally:
        conn.close()

    shutil.move(final, os.path.join(workdir, f"{name}.db"))
    return os.path.join(workdir, f"{name}.db")


def _excluded(relpath: str, patterns: list) -> bool:
    """True when a source-relative path matches one of the exclude globs."""
    for pattern in patterns:
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(
            os.path.basename(relpath), pattern
        ):
            return True
    return False


def redact_json_member(path: str, rel: str, spec: dict, name: str) -> tuple[bytes, set]:
    """Return the member's bytes with the configured credential keys removed.

    The removed VALUES are returned as well, so the caller can prove they do
    not appear anywhere in the finished archive.

    This fails the whole run rather than falling back to the original file.
    A redaction that silently does not happen is worse than no redaction at
    all: it looks like it worked, and the credential lands on the share.
    So an unreadable member, unparseable JSON, or a missing key path is a
    hard error, not a warning.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        fail(f"{name}: cannot read {rel} for redaction: {exc}")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        fail(f"{name}: cannot parse {rel} as JSON for redaction: {exc}")

    parts = spec["path"].split(".")
    cursor = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            fail(
                f"{name}: redaction path {spec['path']!r} is not present in "
                f"{rel} (stops at {part!r}) - refusing to publish it "
                f"unredacted"
            )
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        fail(f"{name}: redaction path {spec['path']!r} in {rel} is not an object")

    removed: set = set()
    for key in spec["keys"]:
        if key not in cursor:
            # Already absent (the app may not have generated it yet). Nothing
            # to strip, and nothing to leak.
            continue
        value = cursor.pop(key)
        if isinstance(value, str) and value:
            removed.add(value.encode("utf-8"))

    return json.dumps(payload).encode("utf-8"), removed


def _source_files(source: str, excludes: list) -> list:
    """Every file under source that the archive would contain.

    The same exclusion and symlink rules are applied here as in the archive
    walk, so the number of files in the live source can be compared with the
    number of files that ended up in the staged archive.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, source)
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d for d in dirnames
            if not _excluded(os.path.join(rel_dir, d) if rel_dir else d, excludes)
        ]
        for filename in sorted(filenames):
            rel = os.path.join(rel_dir, filename) if rel_dir else filename
            if _excluded(rel, excludes):
                continue
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            found.append((full, rel))
    return found


def stage_tar(app: dict, workdir: str) -> str:
    """Archive a directory of app state (no SQLite semantics required).

    The archive is built member by member rather than with a single tar.add()
    of the tree, so that excluded paths can be skipped. `exclude` patterns are
    matched against the path relative to the source root and against the bare
    filename, so "secret.txt" and "*.tmp" both do what they look like.

    The result is verified LOCALLY before it is published: the archive must
    reopen, contain the required members, and any declared JSON member must
    parse and hold a non-zero number of records. A backup of an empty
    datastore is a successful run of a useless backup, so that is a failure.
    """
    name = app["name"]
    source = app["source"]
    if not os.path.exists(source):
        fail(f"{name}: source path {source} does not exist")
    if not os.path.isdir(source):
        fail(f"{name}: source path {source} is not a directory")

    excludes = list(app.get("exclude", []))
    verify = app.get("verify", {}) or {}
    # Redaction config: memberwise JSON key removal at backup time.
    # When specified, the listed keys are deleted from the target JSON member
    # before archiving. The app stores these as credentials; keeping them out
    # of the published artifact lets restored instances generate fresh tokens
    # on first start (precedent: secret.txt is excluded entirely, and the
    # app's own store.py regenerates a missing token on startup).
    #
    # Keyed by source-relative member path so the lookup is exact. A spec
    # naming a member that is not in the source is a configuration error and
    # is checked below, so a typo cannot silently disable redaction.
    redact_specs = list(app.get("redact", []) or [])
    redact: dict = {}
    for spec in redact_specs:
        member = spec.get("member")
        if not member:
            fail(f"{name}: a redact spec has no 'member' key: {spec!r}")
        if member in redact:
            fail(f"{name}: two redact specs target the same member {member!r}")
        redact[member] = spec
    redacted_secrets: set = set()
    redacted_members: set = set()
    arcroot = os.path.basename(source.rstrip("/"))

    work = os.path.join(workdir, f"{name}.tar.gz")
    if os.path.exists(work):
        os.remove(work)

    added = 0
    with tarfile.open(work, "w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(source):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, source)
            if rel_dir == ".":
                rel_dir = ""
            # Prune excluded directories in place so os.walk never descends
            # into them.
            dirnames[:] = [
                d for d in dirnames
                if not _excluded(os.path.join(rel_dir, d) if rel_dir else d, excludes)
            ]
            for filename in sorted(filenames):
                rel = os.path.join(rel_dir, filename) if rel_dir else filename
                if _excluded(rel, excludes):
                    log(f"{name}: excluded {rel}")
                    continue
                full = os.path.join(dirpath, filename)
                if os.path.islink(full):
                    log(f"{name}: skipped symlink {rel}")
                    continue

                # Redact JSON members that carry credentials. A redaction
                # that cannot be applied is a hard failure of the whole
                # run: shipping the unredacted member would put the
                # credential back on the share, which is the exact thing
                # this code exists to prevent.
                spec = redact.get(rel)
                if spec is not None:
                    data, removed = redact_json_member(full, rel, spec, name)
                    redacted_secrets.update(removed)
                    redacted_members.add(rel)
                    tmp_path = os.path.join(
                        workdir, f".redacted_{os.path.basename(rel)}"
                    )
                    with open(tmp_path, "wb") as handle:
                        handle.write(data)
                    try:
                        tar.add(tmp_path, arcname=os.path.join(arcroot, rel))
                    finally:
                        os.remove(tmp_path)
                    log(
                        f"{name}: redacted {rel} "
                        f"keys={spec['keys']} removed={len(removed)}"
                    )
                else:
                    tar.add(full, arcname=os.path.join(arcroot, rel))
                added += 1

    min_files = int(verify.get("min_files", 1))
    allow_empty = bool(verify.get("allow_empty", False))
    if added == 0:
        if allow_empty:
            log(
                f"{name}: the source holds no files - publishing an empty archive "
                f"(allow_empty is set for this app)"
            )
        else:
            fail(
                f"{name}: archive would contain no files - a backup of an empty "
                f"datastore is a successful run of a useless backup"
            )
    elif added < min_files:
        fail(
            f"{name}: archive would contain {added} file(s), expected at least "
            f"{min_files} - the artifact does not match the live datastore"
        )

    # The archive must hold what the live source holds. A tar that silently
    # missed files (a walk that stopped early, an exclusion that matched
    # too much) looks exactly like a good backup.
    if verify.get("compare_source_files"):
        live_files = len(_source_files(source, excludes))
        if live_files != added:
            fail(
                f"{name}: the archive holds {added} file(s) but the live source holds "
                f"{live_files} - the artifact does not match the live datastore"
            )
        log(f"verify: {name} matches_live_source file_count={live_files}")

    size = os.path.getsize(work)
    if size == 0:
        fail(f"{name}: archive is 0 bytes")

    # Verify the STAGED archive locally. Nothing reaches the share until
    # these checks pass. "members" and "json_member" are given relative to
    # the source root; the archive stores them under the source's basename.
    members = set()
    with tarfile.open(work, "r:gz") as tar:
        for member in tar.getmembers():
            members.add(member.name)
        for required in verify.get("members", []):
            if os.path.join(arcroot, required) not in members:
                fail(
                    f"{name}: staged archive is missing required member "
                    f"{required!r} (has {sorted(members)})"
                )

        json_member = verify.get("json_member")
        if json_member:
            member_name = os.path.join(arcroot, json_member)
            if member_name not in members:
                fail(f"{name}: staged archive is missing {json_member!r}")
            extracted = tar.extractfile(member_name)
            if extracted is None:
                fail(f"{name}: could not read {json_member!r} from the archive")
            try:
                payload = json.loads(extracted.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                fail(f"{name}: {json_member!r} in the archive is not valid JSON: {exc}")

            json_key = verify.get("json_key")
            if json_key:
                records = payload.get(json_key)
                if not isinstance(records, dict):
                    fail(
                        f"{name}: {json_member!r} has no {json_key!r} object "
                        f"(keys: {sorted(payload)})"
                    )
                count = len(records)
                minimum = int(verify.get("min_count", 1))
                if count < minimum:
                    fail(
                        f"{name}: {json_member!r} holds {count} {json_key!r} "
                        f"records, expected at least {minimum}"
                    )
                log(f"verify: {name} {json_key}_count={count} (min {minimum})")

                if verify.get("compare_source"):
                    live_path = os.path.join(source, json_member)
                    try:
                        with open(live_path) as handle:
                            live_payload = json.load(handle)
                    except (OSError, ValueError) as exc:
                        fail(f"{name}: cannot read live source {live_path}: {exc}")
                    live_count = len(live_payload.get(json_key, {}))
                    if live_count != count:
                        fail(
                            f"{name}: archived {json_key} count {count} does not "
                            f"match live source count {live_count}"
                        )
                    log(f"verify: {name} matches_live_source {json_key}_count={live_count}")

    log(f"verify: {name} archive_ok members={len(members)} bytes={size}")

    # Every configured redact spec must have matched a member. A spec that
    # matched nothing means the config is wrong (wrong member name, wrong
    # source layout) and the credential would have shipped untouched.
    unmatched = sorted(set(redact) - set(redacted_members))
    if unmatched:
        fail(
            f"{name}: these redact specs matched no member in the source: "
            f"{unmatched} - the credential would not have been removed"
        )

    # Leak check. Two independent checks:
    #
    #   1. the exact secret values that were removed must not appear
    #      anywhere in the archive (e.g. copied into another member);
    #   2. no member may still carry a configured credential KEY with a
    #      non-empty string value. This catches the case where the same
    #      credential lives in a second file that redaction never touched.
    #
    # Both run on the STAGED archive, before anything reaches the share.
    credential_keys = {
        key
        for spec in redact.values()
        for key in spec.get("keys", [])
    }
    with tarfile.open(work, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            for secret in redacted_secrets:
                if secret in data:
                    fail(
                        f"{name}: redacted secret value found in archive "
                        f"member {member.name}"
                    )
            text = data.decode("utf-8", "replace")
            for key in credential_keys:
                for match in re.finditer(
                    r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', text
                ):
                    if match.group(1):
                        fail(
                            f"{name}: credential key {key!r} with a non-empty "
                            f"value is still present in archive member "
                            f"{member.name}"
                        )

    return work


def publish(work: str, dest_dir: str, app_name: str, suffix: str) -> tuple:
    """Copy a staged artifact to the share and confirm it landed intact.

    A sequential copy is the only write mode that behaves on this share, so
    the file is copied rather than moved.

    The COPY is verified, not just the staged file: the artifact is read
    back from the share and its digest compared with the staged one. A
    backup that was never read back is an assumption.

    Returns (path, digest, size). The digest is the one already computed
    from the copy ON THE SHARE, so the caller does not have to read the
    artifact back a second time. On this share that read is not cheap: the
    82 MB Navidrome artifact takes minutes to read back, and reading it
    twice doubled the runtime of the job.
    """
    size = os.path.getsize(work)
    staged_sha = sha256(work)
    target = os.path.join(dest_dir, f"{app_name}-{stamp()}{suffix}")
    # The copy lands under a temporary name and is renamed into place only
    # after it has been read back and verified. A pod killed mid-copy (the
    # CronJob's activeDeadlineSeconds does exactly that when the share
    # stalls) then leaves a file that is clearly marked incomplete, not a
    # file wearing a valid artifact's name that the next run's retention
    # would count as a good backup.
    partial = f"{target}.partial"
    log(f"{app_name}: staged {work} ({size} bytes, sha256={staged_sha[:16]})")

    try:
        shutil.copyfile(work, partial)
    except OSError as exc:
        # The copy itself died (I/O error, the mount going away). The
        # half-written file must not stay behind.
        _discard_partial(partial)
        fail(f"{app_name}: copy to share failed: {exc}")

    copied = os.path.getsize(partial)
    if copied != size:
        _discard_partial(partial)
        fail(f"{app_name}: copy to share truncated ({copied} of {size} bytes)")
    copied_sha = sha256(partial)
    if copied_sha != staged_sha:
        _discard_partial(partial)
        fail(
            f"{app_name}: copy to share is corrupt "
            f"(sha256 {copied_sha[:16]} != staged {staged_sha[:16]})"
        )
    try:
        os.replace(partial, target)
    except OSError as exc:
        _discard_partial(partial)
        fail(f"{app_name}: could not publish the verified copy: {exc}")
    log(f"{app_name}: published {target} ({copied} bytes on share)")
    log(f"{app_name}: copied: verified sha256={copied_sha[:16]} matches staged")
    return target, copied_sha, copied


def _discard_partial(target: str) -> None:
    """Remove a half-written artifact from the share.

    A copy that dies halfway leaves a file with the right NAME on the
    share. It looks like a valid artifact to anyone reading the directory,
    and the next run's retention counts it as one of this engine's own
    artifacts. Measured on this cluster: the NAS link degraded to
    multi-second round trips mid-copy (the node reaches the share over
    Wi-Fi only, with the wired interface down), which is exactly the kind
    of failure that produces a truncated file.

    Removing it is safe because a partial copy is never a valid artifact.
    """
    try:
        os.remove(target)
        log(f"removed the partial artifact {target}")
    except OSError as exc:
        log(f"could not remove the partial artifact {target}: {exc}")


def prune(dest_root: str, managed: dict, since: str | None = None) -> None:
    """Keep the newest artifacts in the per-app directories this run owns.

    Two scopes, both deliberate:

    1. Only the directories named in `managed` are touched. The backup root
       is a shared NAS directory that also holds other apps' backups and
       unrelated content, so a walk over everything under it would prune
       data this job does not own.

    2. Inside an app's directory, only the files THIS ENGINE produced are
       eligible for deletion: they match `<app>-<UTC stamp><suffix>`. A file
       that somebody else put there (a vendor's own backup, a manual test
       dump) is left alone and logged. Retention that deletes files it does
       not own is a data-loss bug wearing a housekeeping hat.

    Artifacts are ordered by the stamp in their name, not by mtime: mtime on
    a CIFS share is not trustworthy enough to decide what to delete.
    """
    for app, spec in sorted(managed.items()):
        keep = int(spec.get("keep", 0))
        suffix = spec.get("suffix", "")
        if keep <= 0:
            continue
        app_dir = os.path.join(dest_root, app)
        if not os.path.isdir(app_dir):
            continue
        pattern = re.compile(
            rf"^{re.escape(app)}-(\d{{8}}T\d{{6}}Z){re.escape(suffix)}$"
        )
        owned = []
        for name in sorted(os.listdir(app_dir)):
            full = os.path.join(app_dir, name)
            if not os.path.isfile(full):
                continue
            match = pattern.match(name)
            if match:
                owned.append((match.group(1), full))
            else:
                log(f"prune: kept {app}/{name} (not produced by this backup job)")
        owned.sort(reverse=True)
        for _, stale in owned[keep:]:
            try:
                os.remove(stale)
                log(f"prune: removed {app}/{os.path.basename(stale)}")
            except OSError as exc:
                log(f"prune: could not remove {app}/{os.path.basename(stale)}: {exc}")

        # Abandoned partial copies. publish() writes to "<name>.partial"
        # and renames it into place only after it has been read back and
        # verified, so a partial file is by definition an incomplete
        # artifact. A partial is only removed when it is certainly
        # abandoned: it predates both the current run (so no live copy is
        # writing it) and the newest complete artifact in the directory.
        # A partial that a concurrent job is still writing is left alone.
        newest = owned[0][0] if owned else None
        for name in sorted(os.listdir(app_dir)):
            if not name.endswith(".partial"):
                continue
            full = os.path.join(app_dir, name)
            if not os.path.isfile(full):
                continue
            stamp_match = re.match(
                rf"^{re.escape(app)}-(\d{{8}}T\d{{6}}Z){re.escape(suffix)}\.partial$", name
            )
            if not stamp_match:
                continue
            part_stamp = stamp_match.group(1)
            if since is not None and part_stamp >= since:
                continue
            if newest is not None and part_stamp >= newest:
                continue
            try:
                os.remove(full)
                log(f"prune: removed the abandoned partial {app}/{name}")
            except OSError as exc:
                log(f"prune: could not remove {app}/{name}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Back up only the named app(s). Repeatable. Retention is "
             "scoped to the selected apps as well, so one app's job never "
             "prunes another app's artifacts.",
    )
    args = parser.parse_args()

    with open(args.config) as handle:
        config = json.load(handle)

    dest_root = config["dest"]
    keep = int(config.get("keep", 14))
    workdir = config.get("workdir", "/tmp/backup-work")

    apps = list(config["apps"])
    if args.only:
        known = {app["name"] for app in apps}
        unknown = [name for name in args.only if name not in known]
        if unknown:
            fail(
                f"--only names apps that are not in the configuration: {unknown} "
                f"(configured apps: {sorted(known)})"
            )
        wanted = set(args.only)
        apps = [app for app in apps if app["name"] in wanted]
        log(f"selected apps: {', '.join(app['name'] for app in apps)}")

    os.makedirs(workdir, exist_ok=True)
    # The run's start stamp: a partial copy on the share that predates it
    # belongs to a run that died, not to one that is still writing.
    run_started = stamp()
    ensure_dir(dest_root)

    results: list[tuple] = []
    suffixes: dict = {}
    for app in apps:
        kind = app.get("kind")
        name = app["name"]
        dest_dir = ensure_dir(os.path.join(dest_root, name))
        if kind == "sqlite":
            work = stage_sqlite(app, workdir)
            suffix = ".db"
        elif kind == "pgdump":
            work = stage_pgdump(app, workdir)
            suffix = ".dump"
        elif kind == "navidrome":
            work = stage_navidrome(app, workdir)
            suffix = ".db"
        elif kind == "tar":
            work = stage_tar(app, workdir)
            suffix = ".tar.gz"
        else:
            fail(f"{name}: unknown kind {kind!r}")
        suffixes[name] = suffix
        results.append(publish(work, dest_dir, name, suffix))

    # Retention applies ONLY to the apps this run was responsible for. The
    # backup root is a shared NAS directory that also holds other apps'
    # backups, so pruning is scoped rather than a walk over the whole
    # destination - and inside an app's directory it only ever deletes the
    # artifacts this engine produced.
    managed = {
        app["name"]: {
            "keep": int(app.get("keep", keep)),
            "suffix": suffixes[app["name"]],
        }
        for app in apps
    }
    prune(dest_root, managed, since=run_started)

    log(f"done: {len(results)} artifact(s) written under {dest_root}")
    for path, digest, size in results:
        log(f"  {path} ({size} bytes) sha256={digest[:16]}")


if __name__ == "__main__":
    main()
