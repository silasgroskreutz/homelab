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
    backup.py --config /scripts/config.json

Config file (JSON):
{
  "dest": "/backups",
  "keep": 14,
  "apps": [
    {"name": "linkding", "kind": "sqlite", "source": "/data/db.sqlite3"},
    {"name": "bookorbit", "kind": "pgdump",
     "host": "bookorbit-postgres", "user": "bookorbit", "database": "bookorbit"},
    {"name": "navidrome", "kind": "navidrome", "source": "/data/navidrome.db"},
    {"name": "komga", "kind": "tar", "source": "/config"}
  ]
}

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
"""

import argparse
import fnmatch
import hashlib
import json
import os
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


def open_source(path: str, name: str) -> sqlite3.Connection:
    """Open the live database for reading.

    Read-write is tried first because SQLite can then perform WAL recovery on
    a database whose previous writer crashed, which makes the backup a better
    artifact. It falls back to read-only when the file cannot be opened that
    way. Either way the backup API itself only reads from the source.
    """
    try:
        return sqlite3.connect(path, timeout=30)
    except sqlite3.Error as exc:
        log(f"{name}: read-write open failed ({exc}); falling back to read-only")
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error as exc2:
            fail(f"{name}: cannot open source database {path}: {exc2}")


def stage_sqlite(app: dict, workdir: str) -> str:
    """Produce a single self-contained, verified SQLite file on local disk."""
    name = app["name"]
    source = app["source"]
    if not os.path.exists(source):
        fail(f"{name}: source database {source} does not exist")

    work = os.path.join(workdir, f"{name}.db")
    for leftover in (work, work + "-wal", work + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)

    # The online backup API produces a transactionally consistent snapshot even
    # while the application is writing in WAL mode. This is the recommended
    # approach for hot SQLite databases; copying db+wal+shm by hand is not.
    src = open_source(source, name)
    try:
        dst = sqlite3.connect(work)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # The snapshot inherits the source's WAL journal mode. Convert it to a
    # rollback journal so the artifact is ONE file with no -wal/-shm siblings,
    # and so nothing later recreates those siblings next to it.
    conn = sqlite3.connect(work)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        if app.get("verify", "integrity_check") == "integrity_check":
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                fail(f"{name}: integrity_check on staged copy returned {result!r}")
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            log(f"verify: {name} integrity=ok tables={tables}")
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
    """Use Navidrome's own backup CLI, which is the upstream-supported path."""
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
    # on first start (precedent: secret.txt is excluded entirely).
    redact = app.get("redact", []) or []
    _redacted_secrets = set()
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

                # Redact JSON members that contain credentials
                redacted = False
                for spec in redact:
                    if rel == spec["member"]:
                        try:
                            with open(full) as fh:
                                payload = json.load(fh)
                            # Delete the listed keys at the dotted path
                            parts = spec["path"].split(".")
                            cur = payload
                            ok = True
                            for p in parts[:-1]:
                                if isinstance(cur, dict) and p in cur:
                                    cur = cur[p]
                                else:
                                    ok = False
                                    break
                            if ok and isinstance(cur, dict) and parts[-1] in cur:
                                removed = []
                                for k in spec["keys"]:
                                    if k in cur:
                                        removed.append(cur.pop(k))
                                # Write redacted JSON to a temp file in workdir
                                tmp_path = os.path.join(
                                    workdir, f".redacted_{os.path.basename(rel)}"
                                )
                                with open(tmp_path, "w") as fh:
                                    json.dump(payload, fh)
                                tar.add(tmp_path, arcname=os.path.join(arcroot, rel))
                                os.remove(tmp_path)
                                # Remember the original secret values for leak check
                                for v in removed:
                                    if isinstance(v, str) and v:
                                        _redacted_secrets.add(v.encode())
                            redacted = True
                            log(f"{name}: redacted {rel} keys={spec['keys']}")
                            break
                        except (json.JSONDecodeError, Exception) as e:
                            log(f"{name}: redaction warning for {rel}: {e}")
                            break
                if not redacted:
                    tar.add(full, arcname=os.path.join(arcroot, rel))
                added += 1

    if added == 0:
        fail(f"{name}: archive would contain no files")

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

    # Leak check: ensure redacted secret values do not appear anywhere in
    # the staged archive. The archive member bytes are scanned for the
    # original secret values that were removed from url-watches.json.
    if _redacted_secrets:
        with tarfile.open(work, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                data = f.read()
                for secret in _redacted_secrets:
                    if secret in data:
                        fail(
                            f"{name}: redacted secret value found in archive "
                            f"member {member.name}"
                        )

    return work


def publish(work: str, dest_dir: str, app_name: str, suffix: str) -> str:
    """Copy a staged artifact to the share and confirm it landed intact.

    A sequential copy is the only write mode that behaves on this share, so
    the file is copied rather than moved.

    The COPY is verified, not just the staged file: the artifact is read
    back from the share and its digest compared with the staged one. A
    backup that was never read back is an assumption.
    """
    size = os.path.getsize(work)
    staged_sha = sha256(work)
    target = os.path.join(dest_dir, f"{app_name}-{stamp()}{suffix}")
    log(f"{app_name}: staged {work} ({size} bytes, sha256={staged_sha[:16]})")

    shutil.copyfile(work, target)

    copied = os.path.getsize(target)
    if copied != size:
        fail(f"{app_name}: copy to share truncated ({copied} of {size} bytes)")
    copied_sha = sha256(target)
    if copied_sha != staged_sha:
        fail(
            f"{app_name}: copy to share is corrupt "
            f"(sha256 {copied_sha[:16]} != staged {staged_sha[:16]})"
        )
    log(f"{app_name}: published {target} ({copied} bytes on share)")
    log(f"{app_name}: copied: verified sha256={copied_sha[:16]} matches staged")
    return target


def prune(dest_root: str, managed: dict) -> None:
    """Keep the newest artifacts in the per-app directories this run owns.

    Only the directories named in `managed` are touched. The backup root is
    a shared NAS directory that also holds other apps' backups and unrelated
    content, so a walk over everything under it would prune data this job
    does not own. `managed` maps app name to its retention count.
    """
    for app, keep in sorted(managed.items()):
        if keep <= 0:
            continue
        app_dir = os.path.join(dest_root, app)
        if not os.path.isdir(app_dir):
            continue
        paths = [
            os.path.join(app_dir, name)
            for name in os.listdir(app_dir)
            if os.path.isfile(os.path.join(app_dir, name))
        ]
        paths.sort(key=os.path.getmtime, reverse=True)
        for stale in paths[keep:]:
            try:
                os.remove(stale)
                log(f"prune: removed {app}/{os.path.basename(stale)}")
            except OSError as exc:
                log(f"prune: could not remove {app}/{os.path.basename(stale)}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    with open(args.config) as handle:
        config = json.load(handle)

    dest_root = config["dest"]
    keep = int(config.get("keep", 14))
    workdir = config.get("workdir", "/tmp/backup-work")

    os.makedirs(workdir, exist_ok=True)
    ensure_dir(dest_root)

    results: list[str] = []
    for app in config["apps"]:
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
        results.append(publish(work, dest_dir, name, suffix))

    # Retention applies ONLY to the apps this configuration manages. The
    # backup root may be shared with other apps' backups, so pruning is
    # scoped rather than a walk over the whole destination.
    managed = {
        app["name"]: int(app.get("keep", keep)) for app in config["apps"]
    }
    prune(dest_root, managed)

    log(f"done: {len(results)} artifact(s) written under {dest_root}")
    for path in results:
        log(f"  {path} ({os.path.getsize(path)} bytes) sha256={sha256(path)[:16]}")


if __name__ == "__main__":
    main()
