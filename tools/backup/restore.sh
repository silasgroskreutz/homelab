#!/usr/bin/env bash
# Restore a changedetection.io backup artifact into a datastore directory.
#
# The procedure encoded here was proven by hand in the restore test
# (kanban t_c1345881) and it lives in this script so the next operator does
# not have to rediscover it at 03:00 with nobody watching.
#
# READ tools/backup/README.md BEFORE RUNNING THIS. The three non-obvious
# facts, all of which break a restore in ways that look like success:
#
#   1. The artifact is a .tar.gz of the datastore DIRECTORY, not a SQLite
#      file. changedetection.io (release 0.39.22.1) uses JSON/file storage
#      -- /app/changedetectionio/store.py:37 sets
#      json_store_path = "{}/url-watches.json".format(self.datastore_path).
#      There is no changedetection.db. This is an untar, not sqlite3 .restore.
#
#   2. The archive root member prefix is "datastore/" (the basename of the
#      source directory /datastore), and the datastore directory IS the PVC
#      mount root. So the untar MUST strip that prefix:
#          tar -xzf ARTIFACT -C TARGET --strip-components=1
#      Without it the files land in TARGET/datastore/ and url-watches.json
#      is missing from TARGET/. The app then comes up with ZERO watches
#      instead of erroring -- a restore that silently does nothing.
#
#   3. The snapshot .txt files carry the history, not the JSON. With
#      url-watches.json intact but the .txt snapshots missing, the app
#      still lists the watches and still reports history_n from
#      history.txt, but reading the history CONTENT returns HTTP 500.
#      So "the JSON parses and the watch count matches" is NOT a sufficient
#      restore check. This script verifies every snapshot file referenced
#      by every history.txt exists and is non-empty.
#
# The previous_md5 field in the JSON is the change-detection baseline: a
# restore that loses it makes the next recheck look like a first check and
# writes a spurious snapshot. This script reports the baseline per watch.
#
# secret.txt is the Flask session key. It is a credential and is excluded
# from the artifact by tools/backup/config.json. This script NEVER sources
# it from the artifact: if a secret.txt is present in the archive it is
# dropped on the floor and reported. A secret.txt in the target may only
# come from --secret-file or from a file that was already there.
#
# There are NO credentials in this script and none are added to any
# ConfigMap in Git. If a restore ever needs the live API token, it comes
# from a SOPS-encrypted Secret.
#
# The script needs no cluster access and no running instance. It works from
# a plain shell on the node with nothing but tar, python3 and sha256sum.
#
# Usage:
#   ./tools/backup/restore.sh --artifact ARTIFACT.tar.gz --target DIR
#
# See --help for the full option list.
set -euo pipefail

PROG="$(basename "$0")"

# ---------------------------------------------------------------------------
# Defaults and option state
# ---------------------------------------------------------------------------
ARTIFACT=""
TARGET=""
EXPECT_COUNT=""
LIVE_SOURCE=""            # datastore dir to count the live watches from
SECRET_FILE=""            # explicitly supplied secret.txt
ALLOW_LIVE_TARGET=0       # deliberate override for the live datastore
CLEAN_TARGET=0            # wipe the target's contents before restoring
VERIFY_ONLY=0             # verify an already-restored dir, change nothing
DRY_RUN=0                 # show what would happen, touch nothing
LIVE_PATH_HINT=""         # explicit path to the live datastore

FAILURES=0

usage() {
  cat <<EOF
$PROG - restore a changedetection.io backup artifact into a datastore directory

Usage:
  $PROG --artifact ARTIFACT.tar.gz --target DIR [options]

Required:
  --artifact PATH        the .tar.gz artifact to restore
  --target DIR           the datastore directory to restore INTO
                         (this is the PVC mount root, e.g. /datastore)

Options:
  --expect-count N       expected number of watches ('watching' entries).
                         Required for an offline/after-the-fact restore
                         where the live source is gone.
  --live-source DIR      datastore directory to count the live watches from
                         at restore time. Defaults to the detected live
                         datastore path. Mutually redundant with
                         --expect-count; --expect-count wins if both given.
  --live-path HINT       treat HINT as the live datastore path for the
                         refusal check (defaults to the auto-detected path).
  --secret-file PATH     copy this file into the target as secret.txt.
                         The ONLY accepted source of secret.txt. The
                         artifact's own secret.txt is never used.
  --clean                delete the target's existing contents before
                         restoring (recommended for a true restore).
  --allow-live-target    DELIBERATE override: permit restoring into the
                         live changedetection datastore.
  --verify-only          run the verification checks against --target
                         without extracting anything.
  --dry-run              print the plan and the archive contents, touch
                         nothing.
  -h, --help             this text.

Exit status is 0 only when every check passed.
EOF
}

log()  { printf '[restore] %s\n' "$*"; }
warn() { printf '[restore] WARN: %s\n' "$*" >&2; }
err()  { printf '[restore] ERROR: %s\n' "$*" >&2; }
fail() { err "$*"; FAILURES=$((FAILURES + 1)); }

die() { err "$*"; exit 2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --artifact)          ARTIFACT="${2:-}"; shift 2 ;;
    --target)            TARGET="${2:-}"; shift 2 ;;
    --expect-count)      EXPECT_COUNT="${2:-}"; shift 2 ;;
    --live-source)       LIVE_SOURCE="${2:-}"; shift 2 ;;
    --live-path)         LIVE_PATH_HINT="${2:-}"; shift 2 ;;
    --secret-file)       SECRET_FILE="${2:-}"; shift 2 ;;
    --clean)             CLEAN_TARGET=1; shift ;;
    --allow-live-target) ALLOW_LIVE_TARGET=1; shift ;;
    --verify-only)       VERIFY_ONLY=1; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "$ARTIFACT" ] || die "--artifact is required (try --help)"
[ -n "$TARGET" ]   || die "--target is required (try --help)"

[ -f "$ARTIFACT" ] || die "artifact does not exist: $ARTIFACT"
[ -r "$ARTIFACT" ] || die "artifact is not readable: $ARTIFACT"

if [ "$VERIFY_ONLY" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  # Deliberately NOT checked here: the target's existence is verified after
  # the live-datastore refusal check below, so that a restore aimed at the
  # live datastore is refused for the RIGHT reason (it is the live
  # datastore) rather than for a permission error on the path.
  :
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

# Canonicalize a path without requiring it to exist. `realpath -m` is GNU;
# the macOS `realpath` has no -m, so fall back to python.
canon_path() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1 && realpath -m "$p" >/dev/null 2>&1; then
    realpath -m "$p"
  else
    python3 - "$p" <<'PY'
import os, sys
print(os.path.normpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
  fi
}

# is TARGET inside (or equal to) DIR?  Both must already be canonical.
is_within() {
  local target="$1" dir="$2"
  [ "$target" = "$dir" ] && return 0
  case "$target" in
    "$dir"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Detect the live datastore path (best effort, never required)
# ---------------------------------------------------------------------------
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
detected_live_path=""

# Ask the cluster for the PV's local path. There is more than one way to
# reach the API on this node and the obvious one (`kubectl` from PATH) is a
# snap build with no working kubeconfig, so try each in turn.
live_path_from_cluster() {
  local ns="${NAMESPACE:-changedetection}"
  local pvc="${PVC:-changedetection-data-pvc}"
  local k
  for k in "kubectl" "sudo -n /usr/local/bin/k3s kubectl" "sudo -n k3s kubectl"; do
    local pv path
    pv="$($k -n "$ns" get pvc "$pvc" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
    [ -n "$pv" ] || continue
    path="$($k get pv "$pv" -o jsonpath='{.spec.local.path}' 2>/dev/null || true)"
    [ -n "$path" ] || continue
    printf '%s\n' "$path"
    return 0
  done
  return 1
}

detect_live_path() {
  # 1. explicit hint wins
  if [ -n "$LIVE_PATH_HINT" ]; then
    detected_live_path="$LIVE_PATH_HINT"
    return
  fi
  # 2. the repo helper (needs kubectl + a reachable cluster)
  local helper="$repo_root/tools/backup/datastore-path.sh"
  if [ -x "$helper" ] && command -v kubectl >/dev/null 2>&1; then
    local p
    p="$("$helper" 2>/dev/null || true)"
    if [ -n "$p" ]; then
      detected_live_path="$p"
      return
    fi
  fi
  # 3. the cluster, queried directly (the helper may have failed while the
  #    cluster is reachable through the k3s kubectl instead)
  local p
  if p="$(live_path_from_cluster 2>/dev/null)"; then
    detected_live_path="$p"
    return
  fi
  # 4. glob the local-path provisioner directory on the node
  local candidate
  for candidate in /var/lib/rancher/k3s/storage/*_changedetection_changedetection-data-pvc; do
    if [ -d "$candidate" ]; then
      detected_live_path="$candidate"
      return
    fi
  done
  # 4b. the same glob, but via sudo: on the node the storage directory is
  # root-only, so a plain glob cannot even see the candidate, and the glob
  # has to be expanded by ROOT's shell (`sh -c`), not by this one.
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    candidate="$(sudo -n sh -c 'ls -d /var/lib/rancher/k3s/storage/*_changedetection_changedetection-data-pvc 2>/dev/null' | head -n1 || true)"
    if [ -n "$candidate" ]; then
      detected_live_path="$candidate"
      return
    fi
  fi
}

detect_live_path
if [ -n "$detected_live_path" ]; then
  detected_live_path="$(canon_path "$detected_live_path")"
fi

target_canon="$(canon_path "$TARGET")"

# ---------------------------------------------------------------------------
# Refuse to restore into the live datastore by accident
# ---------------------------------------------------------------------------
if [ -n "$detected_live_path" ]; then
  if is_within "$target_canon" "$detected_live_path"; then
    if [ "$ALLOW_LIVE_TARGET" -eq 1 ]; then
      warn "target $target_canon is INSIDE the live changedetection datastore ($detected_live_path)"
      warn "--allow-live-target was passed. Never restore under a running app:"
      warn "  kubectl scale deploy/changedetection -n changedetection --replicas=0"
    else
      err "refusing to restore into the LIVE changedetection datastore:"
      err "  target:     $target_canon"
      err "  live path:  $detected_live_path"
      err "Restore into a scratch datastore instead, or pass --allow-live-target"
      err "if you really mean to overwrite the live data (scale the app down first)."
      exit 3
    fi
  else
    log "live datastore detected at $detected_live_path -- target is outside it, OK"
  fi
else
  warn "could not detect the live datastore path (no kubectl, no local-path"
  warn "directory visible). The --allow-live-target guard is inactive."
fi

if [ "$VERIFY_ONLY" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  [ -d "$TARGET" ] || die "target directory does not exist: $TARGET"
  [ -w "$TARGET" ] || die "target directory is not writable: $TARGET"
fi

# ---------------------------------------------------------------------------
# Inspect the archive
# ---------------------------------------------------------------------------
ARTIFACT_SHA="$(sha256_of "$ARTIFACT")"
ARTIFACT_SIZE="$(wc -c < "$ARTIFACT" | tr -d ' ')"

members_file="$(mktemp)"
trap 'rm -f "$members_file"' EXIT
tar -tzf "$ARTIFACT" > "$members_file"

member_count="$(grep -c . "$members_file" || true)"
if [ "$member_count" -eq 0 ]; then
  die "artifact contains no members: $ARTIFACT"
fi

# Every member must live under a single root directory. That root is the
# basename of the datastore directory at backup time ("datastore"), and it
# is exactly what --strip-components=1 removes.
root_prefixes="$(sed 's|^\./||; s|/.*$||' "$members_file" | sort -u)"
root_count="$(printf '%s\n' "$root_prefixes" | grep -c . || true)"
if [ "$root_count" -ne 1 ]; then
  err "artifact has $root_count root prefixes, expected exactly 1:"
  printf '  %s\n' "$root_prefixes" >&2
  die "refusing to restore an archive whose layout is not understood"
fi
root_prefix="$(printf '%s\n' "$root_prefixes" | head -n1)"

if [ "$root_prefix" = "datastore" ]; then
  log "archive root prefix: $root_prefix/ (will be stripped with --strip-components=1)"
else
  warn "archive root prefix is '$root_prefix/', not the expected 'datastore/'."
  warn "The strip is still applied, but check that $TARGET/ ends up holding"
  warn "url-watches.json directly."
fi

artifact_has_secret=0
# -E (ERE): in a BRE the "|" is a literal, so the alternation must be
# expressed with -E or the pattern never matches.
if grep -qE '(^|/)secret\.txt$' "$members_file"; then
  artifact_has_secret=1
  warn "the artifact contains a secret.txt member. That is the Flask session"
  warn "key -- a credential. It will NOT be restored. secret.txt must come"
  warn "from --secret-file or from a file already present in the target."
fi

log "artifact:     $ARTIFACT"
log "size:         $ARTIFACT_SIZE bytes"
log "sha256:       $ARTIFACT_SHA"
log "members:      $member_count"
log "target:       $target_canon"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--- archive contents ---"
  sed 's/^/  /' "$members_file"
  echo "--- dry run: nothing was written ---"
  exit 0
fi

# ---------------------------------------------------------------------------
# Resolve the expected watch count
# ---------------------------------------------------------------------------
count_watches_in() {
  # $1 = a datastore directory containing url-watches.json
  # The live datastore directory is root-owned on the node, so a plain read
  # by the `silas` user fails. The node has passwordless sudo, so fall back
  # to that rather than forcing the operator to re-derive the count by hand.
  python3 - "$1" <<'PY'
import json, sys, os
p = os.path.join(sys.argv[1], "url-watches.json")
try:
    with open(p) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        print("ERR:not-a-json-object")
    else:
        w = data.get("watching")
        print("ERR:not-an-object" if not isinstance(w, dict) else str(len(w)))
except Exception as exc:
    print("ERR:" + str(exc))
PY
}

count_watches_in_with_sudo() {
  local dir="$1" out=""
  out="$(count_watches_in "$dir")"
  case "$out" in
    ERR:*)
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        out="$(sudo -n python3 - "$dir" <<'PY'
import json, sys, os
p = os.path.join(sys.argv[1], "url-watches.json")
try:
    with open(p) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        print("ERR:not-a-json-object")
    else:
        w = data.get("watching")
        print("ERR:not-an-object" if not isinstance(w, dict) else str(len(w)))
except Exception as exc:
    print("ERR:" + str(exc))
PY
)"
      fi
      ;;
  esac
  printf '%s\n' "$out"
}

live_source_dir="$LIVE_SOURCE"
if [ -z "$live_source_dir" ] && [ -n "$detected_live_path" ]; then
  live_source_dir="$detected_live_path"
fi

resolved_expected=""
expected_origin=""
if [ -n "$EXPECT_COUNT" ]; then
  resolved_expected="$EXPECT_COUNT"
  expected_origin="--expect-count"
elif [ -n "$live_source_dir" ]; then
  live_count="$(count_watches_in_with_sudo "$live_source_dir")"
  case "$live_count" in
    ERR:*) warn "cannot count the live watches in $live_source_dir ($live_count)" ;;
    *)
      resolved_expected="$live_count"
      expected_origin="live source $live_source_dir"
      ;;
  esac
fi

if [ -z "$resolved_expected" ]; then
  err "no expected watch count available: the live source is unreadable and"
  err "--expect-count was not given. For an offline/after-the-fact restore"
  err "pass --expect-count N (the number of watches the artifact should hold)."
  FAILURES=$((FAILURES + 1))
else
  log "expected watch count: $resolved_expected (from $expected_origin)"
fi

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
pre_existing_secret=0
if [ -f "$TARGET/secret.txt" ]; then
  pre_existing_secret=1
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  staging="$(mktemp -d)"
  trap 'rm -rf "$staging"; rm -f "$members_file"' EXIT

  log "extracting to a staging directory first (so a bad archive cannot leave"
  log "the target half-restored), then copying into the target"

  # --strip-components=1 removes the "datastore/" root prefix. Without it
  # the files land in TARGET/datastore/ and the app finds no watches.
  tar -xzf "$ARTIFACT" -C "$staging" --strip-components=1

  if [ "$artifact_has_secret" -eq 1 ]; then
    find "$staging" -name 'secret.txt' -type f -delete
  fi

  if [ "$CLEAN_TARGET" -eq 1 ]; then
    log "--clean: removing the existing contents of $target_canon"
    find "$target_canon" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  fi

  # Copy the restored tree over the target. cp -a keeps the mode and
  # timestamps; the app runs as root and owns the datastore, so anything
  # the restore creates must stay writable by it.
  cp -a "$staging/." "$target_canon/"

  if [ -n "$SECRET_FILE" ]; then
    [ -f "$SECRET_FILE" ] || die "--secret-file does not exist: $SECRET_FILE"
    log "installing the explicitly supplied secret.txt from $SECRET_FILE"
    cp -a "$SECRET_FILE" "$target_canon/secret.txt"
  fi

  rm -rf "$staging"
  trap 'rm -f "$members_file"' EXIT
else
  log "--verify-only: not extracting anything, verifying $target_canon as it is"
fi

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
log "verifying the restored datastore"

if [ ! -f "$target_canon/url-watches.json" ]; then
  fail "url-watches.json is missing from $target_canon"
  if [ -d "$target_canon/$root_prefix" ]; then
    fail "  (found $target_canon/$root_prefix/ instead -- the archive root prefix"
    fail "   was not stripped. This is the --strip-components=1 failure mode.)"
  fi
fi

CHECK_COUNT_FILE="$(mktemp)"
trap 'rm -f "$members_file" "$CHECK_COUNT_FILE"' EXIT

python3 - "$target_canon" "$resolved_expected" "$expected_origin" "$SECRET_FILE" "$CHECK_COUNT_FILE" <<'PY' || FAILURES=$((FAILURES + 1))
import json, os, sys

target, expected, origin = sys.argv[1], sys.argv[2], sys.argv[3]
secret_file = sys.argv[4] if len(sys.argv) > 4 else ""
count_file = sys.argv[5] if len(sys.argv) > 5 else ""
failures = []
notes = []

def check(cond, msg):
    if not cond:
        failures.append(msg)

watches_json = os.path.join(target, "url-watches.json")
watching = {}
if os.path.isfile(watches_json):
    try:
        with open(watches_json) as fh:
            data = json.load(fh)
    except Exception as exc:
        failures.append("url-watches.json does not parse as JSON: %s" % exc)
        data = None
    if data is not None:
        if not isinstance(data, dict):
            failures.append("url-watches.json is not a JSON object")
            data = None
        else:
            watching = data.get("watching")
            if not isinstance(watching, dict):
                failures.append(
                    "url-watches.json has no 'watching' object (keys: %s)"
                    % sorted(data)
                )
                watching = {}

count = len(watching) if isinstance(watching, dict) else 0
print("[restore] watch count: %d" % count)
if expected:
    try:
        exp = int(expected)
    except ValueError:
        failures.append("expected watch count is not an integer: %r" % expected)
        exp = None
    if exp is not None:
        if count != exp:
            failures.append(
                "watch count mismatch: restored %d, expected %d (from %s)"
                % (count, exp, origin)
            )
        else:
            print("[restore] watch count matches the expectation (%d, from %s)"
                  % (count, origin))

if not isinstance(watching, dict):
    watching = {}

total_history = 0
for uuid, watch in sorted(watching.items()):
    if not isinstance(watch, dict):
        failures.append("watch %s is not an object" % uuid)
        continue
    url = watch.get("url", "?")

    # The change-detection baseline. Losing it makes the next recheck look
    # like a first check and write a spurious snapshot.
    if "previous_md5" not in watch:
        failures.append("watch %s has no previous_md5 field" % uuid)
        baseline = "<missing>"
    elif watch.get("previous_md5"):
        baseline = watch["previous_md5"]
    else:
        baseline = "<empty>"

    hist_path = os.path.join(target, uuid, "history.txt")
    hist_n = 0
    if os.path.isfile(hist_path):
        with open(hist_path) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                hist_n += 1
                # Format is "<epoch>,<snapshot file>.txt"
                parts = line.split(",")
                if len(parts) != 2:
                    failures.append(
                        "watch %s: history.txt line %d is malformed: %r"
                        % (uuid, lineno, line))
                    continue
                snap = parts[1].strip()
                snap_path = os.path.join(target, uuid, snap)
                if not os.path.isfile(snap_path):
                    # This is the failure that a "JSON parses + count matches"
                    # check misses: the app lists the watch and reports the
                    # history length, but reading the history content 500s.
                    failures.append(
                        "watch %s: history.txt references %s but the file is "
                        "missing from %s" % (uuid, snap, os.path.join(target, uuid)))
                    continue
                if os.path.getsize(snap_path) == 0:
                    failures.append(
                        "watch %s: history snapshot %s is empty" % (uuid, snap))
    else:
        failures.append(
            "watch %s: history.txt is missing from %s" % (uuid, os.path.join(target, uuid)))

    total_history += hist_n
    print("[restore] watch %s  url=%s  history_n=%d  previous_md5=%s"
          % (uuid, url, hist_n, baseline))

print("[restore] total history entries: %d" % total_history)

# secret.txt: absent (the app generates one) or explicitly supplied. It is
# never taken from the artifact.
secret = os.path.join(target, "secret.txt")
if os.path.exists(secret):
    if secret_file:
        notes.append("secret.txt is present in the target (installed from the "
                     "explicitly supplied --secret-file)")
    else:
        notes.append("secret.txt is present in the target (a file that was "
                     "already there before the restore; it was NOT taken "
                     "from the artifact)")
else:
    notes.append("secret.txt is absent from the target -- the app will "
                 "generate its own on first start (init_app_secret)")

for note in notes:
    print("[restore] %s" % note)

for f in failures:
    print("[restore] CHECK FAILED: %s" % f, file=sys.stderr)
print("[restore] %d verification check(s) failed" % len(failures), file=sys.stderr)
if count_file:
    with open(count_file, "w") as fh:
        fh.write(str(len(failures)))
sys.exit(1 if failures else 0)
PY

# The python block reports how many individual checks failed. The shell's
# FAILURES counter only knows that the block failed as a whole, so read the
# real number back for the final report.
CHECK_FAILURES=0
if [ -f "$CHECK_COUNT_FILE" ]; then
  CHECK_FAILURES="$(cat "$CHECK_COUNT_FILE" 2>/dev/null || echo 0)"
fi

# The secret.txt policy is enforced by the shell too: the artifact's
# secret.txt is dropped during staging, so anything left here came from
# --secret-file or was already there before the restore.
if [ -f "$target_canon/secret.txt" ]; then
  if [ -n "$SECRET_FILE" ]; then
    log "secret.txt: installed from the explicitly supplied --secret-file"
  elif [ "$pre_existing_secret" -eq 1 ]; then
    # A secret.txt that was already in the target before the restore is
    # left alone. It is not from the artifact.
    log "secret.txt: pre-existing file left untouched (not taken from the artifact)"
  elif [ "$artifact_has_secret" -eq 1 ]; then
    # The artifact's secret.txt was dropped during staging, so a secret.txt
    # here cannot have come from the artifact -- but if it did, that is a
    # bug in this script and must not pass silently.
    fail "secret.txt is present in the target even though the artifact's copy was dropped."
    fail "  Refusing to accept a secret.txt of unknown provenance."
  else
    log "secret.txt: present in the target"
  fi
else
  log "secret.txt: absent -- the app will generate one on first start"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo
echo "--- restore report ---"
echo "artifact:        $ARTIFACT"
echo "artifact sha256: $ARTIFACT_SHA"
echo "artifact size:   $ARTIFACT_SIZE bytes"
echo "archive root:    $root_prefix/ (stripped)"
echo "target:          $target_canon"
echo "members:         $member_count"
echo "expected count:  ${resolved_expected:-unknown} (from ${expected_origin:-none})"
echo "--- restored file list ---"
( cd "$target_canon" && find . -type f | sort | sed 's|^\./|  |' )

if [ "$FAILURES" -ne 0 ]; then
  echo
  if [ "$CHECK_FAILURES" -gt 0 ]; then
    err "RESTORE VERIFICATION FAILED ($CHECK_FAILURES check(s) failed). See the CHECK FAILED lines above."
  else
    err "RESTORE VERIFICATION FAILED. See the CHECK FAILED lines above."
  fi
  exit 1
fi

echo
log "restore verified OK"
if [ "$VERIFY_ONLY" -eq 0 ]; then
  cat <<EOF
Next steps (the app must be scaled down before and up after a restore):
  kubectl scale deploy/changedetection -n changedetection --replicas=0
  # ... restore ...
  kubectl scale deploy/changedetection -n changedetection --replicas=1
Then confirm the app lists the watches and that a recheck against an
UNCHANGED url does NOT append a new snapshot (that proves previous_md5
survived the restore).
EOF
fi
exit 0
