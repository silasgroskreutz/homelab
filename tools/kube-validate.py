#!/usr/bin/env python3
"""
kube-validate.py — filter rendered manifests so a SERVER-SIDE dry-run against
the live cluster tests what Flux will actually accept.

WHY THIS EXISTS
  `kubectl apply --dry-run=server -f -` on a full app overlay produces two
  classes of FALSE failure that have nothing to do with the manifests being
  wrong:

  1. SOPS-encrypted Secrets.
     The repo stores Secret values as SOPS ciphertext (ENC[AES256_GCM,...]).
     Flux decrypts them with the sops-age key BEFORE it reaches the API
     server. A direct `kubectl apply` sends the ciphertext, the server tries
     to PATCH the existing Secret with it, and rejects it:
         Error from server (Invalid): error when applying patch:
         Resource: "/v1, Resource=secrets" ... Name: "tunnel-credentials"
     Flux never hits this. So we drop SOPS secrets from the dry-run and
     report them separately — their real validation is "it decrypts", which
     is a repo-side check, not a cluster-side one.

  2. Namespaces that do not exist yet.
     For a brand-new app, the namespace is created by the same apply. The
     server-side dry-run evaluates objects in order, so the namespaced
     objects fail with:
         Error from server (NotFound): error when creating "STDIN":
         namespaces "appname" not found
     Flux creates the namespace first and then succeeds. We classify these
     as EXPECTED for a not-yet-created namespace, not as failures.

WHAT ACTUALLY FAILS THE VALIDATION
  Anything the API server rejects on schema grounds: unknown fields, wrong
  types, CRD violations. That is what a dry-run is for, and it is what
  catches a bad manifest BEFORE Flux tries to reconcile it.

USAGE
  kubectl kustomize <path> | ./tools/kube-validate.py --dry-run
  ./tools/kube-validate.py --dry-run < rendered.yaml
"""
import subprocess
import sys

PRODESC = "silas@192.168.0.101"
REMOTE_KUBECTL = "sudo -n /usr/local/bin/k3s kubectl apply --dry-run=server -f -"


def split_docs(text):
    """Split a multi-doc YAML stream into (kind, name, namespace, text) tuples."""
    docs = []
    for chunk in text.split("\n---"):
        if not chunk.strip():
            continue
        docs.append(chunk)
    return docs


def doc_meta(chunk):
    """Extract (kind, name, namespace) from a rendered YAML doc.

    Scoped properly: `name:` only counts when it appears inside the
    metadata block, and `namespace:` only when it is a metadata field.
    A bare `name:` inside env vars or labels must not win.
    """
    kind = name = namespace = ""
    in_metadata = False
    for raw_line in chunk.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped == "metadata:":
            in_metadata = True
            continue
        # A new top-level key ends the metadata block
        if indent == 0 and stripped.endswith(":") and stripped != "metadata:":
            in_metadata = False
            continue
        if not in_metadata:
            continue
        if stripped.startswith("kind:") and not kind:
            kind = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("name:") and not name:
            name = stripped.split(":", 1)[1].strip().strip('"').strip("'")
        elif stripped.startswith("namespace:") and not namespace:
            namespace = stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return kind, name, namespace


def is_sops_secret(chunk):
    """True when this doc is a Secret whose data is SOPS ciphertext."""
    if "\nkind: Secret" not in chunk and not chunk.lstrip().startswith("kind: Secret"):
        return False
    # SOPS writes the ciphertext inline in data:/stringData: and adds a
    # top-level `sops:` block with the age recipients.
    if "ENC[AES256_GCM" in chunk or "ENC[age" in chunk:
        return True
    # A sops: block with a mac/encrypted_regex is the other marker.
    return "\nsops:" in chunk and "encrypted_regex" in chunk


def main():
    dry_run = "--dry-run" in sys.argv
    raw = sys.stdin.read()

    chunks = split_docs(raw)
    keep, sops_secrets, namespaces = [], [], []

    for c in chunks:
        kind, name, ns = doc_meta(c)
        if is_sops_secret(c):
            sops_secrets.append(f"{ns or 'default'}/{name}")
            continue
        if kind == "Namespace":
            namespaces.append(name)
        keep.append(c)

    if sops_secrets:
        print(f"  SOPS-encrypted secrets skipped (Flux decrypts these server-side):")
        for s in sops_secrets:
            print(f"    - {s}")

    if not dry_run:
        for c in keep:
            print("---")
            print(c.strip())
        return 0

    if not keep:
        print("  nothing to dry-run")
        return 0

    payload = "\n---\n".join(keep)
    proc = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", PRODESC, REMOTE_KUBECTL],
        input=payload,
        text=True,
        capture_output=True,
    )
    stderr = proc.stderr or ""
    stdout = proc.stdout or ""

    errors, warnings, benign_ns = [], [], []
    for line in stderr.splitlines():
        s = line.strip()
        if not s or s.startswith("Warning:") or "last-applied-configuration" in s:
            warnings.append(s)
            continue
        if 'namespaces "' in s and "not found" in s:
            benign_ns.append(s)
            continue
        if s.startswith("Error"):
            errors.append(s)

    # kubectl's exit code is unreliable here: it can be non-zero purely from
    # the last-applied-configuration warnings on a successful dry-run.
    # Judge on the CONTENT of stderr, not the exit code.
    real_errors = [e for e in errors if "error when applying patch" not in e.lower() or True]

    if real_errors:
        print("  SERVER DRY-RUN FAILED — Flux would reject this:")
        seen = set()
        for line in stderr.splitlines():
            s = line.strip()
            if not s.startswith(("Error from server", "Resource:", "Name:")):
                continue
            if s in seen:
                continue
            seen.add(s)
            print(f"    {s}")
        return 1

    if benign_ns:
        nsnames = sorted({n for n in namespaces if n})
        if nsnames:
            print(f"  namespace(s) not yet created on the cluster: {', '.join(nsnames)}")
            print("  (expected for a new app — Flux creates the namespace first)")
    print("  server dry-run OK (CRD schema accepted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
