#!/usr/bin/env python3
"""
Scaffold a new application for the homelab cluster.

Deploying a new app should be a checklist, not an archaeology project. This
script generates the whole standard shape so that the only decisions left are
the ones that actually vary between apps: the name, the image, the port, and
whether it needs storage.

What it generates
-----------------
  apps/base/<app>/namespace.yaml     Namespace (one owner, always)
  apps/base/<app>/deployment.yaml    Deployment with sane defaults
  apps/base/<app>/service.yaml       ClusterIP Service
  apps/base/<app>/storage.yaml       PVC (unless --no-storage)
  apps/base/<app>/kustomization.yaml
  apps/staging/<app>/kustomization.yaml
  apps/staging/<app>/ingress.yaml    Traefik Ingress (host placeholder)
  apps/staging/<app>/secret.yaml     SOPS-encrypted Secret placeholder
  apps/staging/<app>/cloudflare.yaml Cloudflare tunnel connector

Then it registers the app in apps/staging/kustomization.yaml.

Usage
-----
  new_app.py --name myapp --image ghcr.io/org/myapp:1.0 --port 8080
  new_app.py --name myapp --image org/myapp:latest --port 8080 \
             --host myapp.silasgroskreutz.com --storage 2Gi
  new_app.py --name myapp --image org/myapp --port 8080 --no-storage \
             --no-ingress

Design rules baked into the output
-----------------------------------
* The Service selector and the Deployment labels are both `app: <name>`, and
  NOTHING else uses that label. A shared label is how a database pod ends up
  in an app's endpoints and a 200 from the Service turns out to be the
  database answering.
* The Deployment gets a `checksum/config` annotation placeholder so that a
  ConfigMap change actually rolls the pod.
* Storage defaults to a PVC, because almost every app persists something.
  Media that already lives on the NAS is mounted by hostPath instead.
* Every generated file is plain YAML with no placeholders that would fail
  `kubectl kustomize` — the script validates the whole tree before it exits.
* Secrets are created as SOPS-encrypted placeholders. Never write a real
  secret into this repo in plaintext.

After running this, the remaining steps are documented in the printed output.
"""

import argparse
import os
import pathlib
import re
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[2]
DOMAIN = "silasgroskreutz.com"

NAMESPACE_TEMPLATE = """\
apiVersion: v1
kind: Namespace
metadata:
  name: {name}
"""

DEPLOYMENT_TEMPLATE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
      annotations:
        # Bump this when a mounted ConfigMap changes, or the pod keeps
        # running the old content. See the k8s-application-deployment skill.
        checksum/config: "initial"
    spec:
      securityContext:
        fsGroup: 1000
        runAsUser: 1000
        runAsGroup: 1000
      containers:
        - name: {name}
          image: {image}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: {port}
{env_block}{volume_mounts}
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
{volumes}"""

SERVICE_TEMPLATE = """\
apiVersion: v1
kind: Service
metadata:
  name: {name}
spec:
  ports:
    - port: {port}
      targetPort: {port}
  selector:
    app: {name}
  type: ClusterIP
"""

STORAGE_TEMPLATE = """\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}-data-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {size}
  # The local-path provisioner does NOT enforce PVC quotas, so an
  # undersized request is harmless today. If this ever moves to a storage
  # class that enforces limits, expand the PVC FIRST.
"""

BASE_KUSTOMIZATION_TEMPLATE = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - deployment.yaml
  - service.yaml
{storage_resource}"""

STAGING_KUSTOMIZATION_TEMPLATE = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: {name}
resources:
  - ../../base/{name}
  - secret.yaml
{extra_resources}"""

INGRESS_TEMPLATE = """\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  annotations:
    # The public hostname must match this DNS record, or Traefik serves its
    # default certificate and the client sees a TLS error.
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  ingressClassName: traefik
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {name}
                port:
                  number: {port}
  tls:
    - hosts:
        - {host}
      secretName: {name}-tls
"""

SECRET_TEMPLATE = """\
apiVersion: v1
kind: Secret
metadata:
  name: {name}
type: Opaque
# Add the keys this app needs under stringData. This file is SOPS-encrypted
# by the scaffolder, so edit it with:
#   sops -e -i apps/staging/{name}/secret.yaml
# and the recipient is already set in .sops.yaml.
stringData:
  EXAMPLE_PASSWORD: "change-me"
"""

CLOUDFLARE_TEMPLATE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared
spec:
  selector:
    matchLabels:
      app: cloudflared
  replicas: 2
  template:
    metadata:
      labels:
        app: cloudflared
    spec:
      containers:
        - name: cloudflared
          image: cloudflare/cloudflared:latest
          args:
            - tunnel
            - --config
            - /etc/cloudflared/config/config.yaml
            - run
          livenessProbe:
            httpGet:
              path: /ready
              port: 2000
            failureThreshold: 1
            initialDelaySeconds: 10
            periodSeconds: 10
          volumeMounts:
            - name: config
              mountPath: /etc/cloudflared/config
              readOnly: true
            - name: creds
              mountPath: /etc/cloudflared/creds
              readOnly: true
      volumes:
        - creds:
            secret:
              secretName: tunnel-credentials
        - config:
            configMap:
              name: cloudflared
              items:
                - key: config.yaml
                  path: config.yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cloudflared
data:
  config.yaml: |
    tunnel: {name}
    credentials-file: /etc/cloudflared/creds/credentials.json
    metrics: 0.0.0.0:2000
    no-autoupdate: true
    ingress:
    - hostname: {host}
      service: http://{name}:{port}
    - service: http_status:404
"""


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  created {path.relative_to(REPO)}")


def register_in_staging(name: str) -> None:
    """Add the app to apps/staging/kustomization.yaml, idempotently."""
    k = REPO / "apps/staging/kustomization.yaml"
    text = k.read_text()
    entry = f"  - {name}"
    if re.search(rf"(?m)^\s*-\s*{re.escape(name)}\s*$", text):
        print(f"  apps/staging/kustomization.yaml already lists {name}")
        return
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines) if l.startswith("resources:"))
    last = max(i for i, l in enumerate(lines) if l.startswith("  - "))
    lines.insert(last + 1, entry)
    k.write_text("\n".join(lines) + "\n")
    print(f"  registered {name} in apps/staging/kustomization.yaml")


def encrypt_secret(path: pathlib.Path) -> bool:
    """Encrypt the generated Secret with SOPS.

    This is NOT optional. The Flux `apps` Kustomization applies the whole
    apps/staging tree with `decryption.provider: sops`. A single PLAINTEXT
    Secret in that tree fails the entire Kustomization, which takes down
    every app in the cluster, not just the new one. The failure surfaces as
    a reconcile error on `apps` with no obvious link to the new app.

    Encrypt here rather than leaving it to the operator, so the scaffold
    can never produce a tree that breaks the cluster.
    """
    key_file = pathlib.Path.home() / ".config/sops/age/keys.txt"
    if not key_file.exists():
        print(f"  WARNING: {key_file} is missing — secret left in PLAINTEXT.")
        print("           Do NOT commit until you encrypt it, or Flux will")
        print("           fail the entire apps Kustomization.")
        return False
    env = dict(os.environ, SOPS_AGE_KEY_FILE=str(key_file))
    result = subprocess.run(
        ["sops", "-e", "-i", str(path)], cwd=REPO, env=env,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: sops encryption failed: {result.stderr.strip()[:300]}")
        print("           The secret is still PLAINTEXT — do not commit it.")
        return False
    # Verify the effect, not just the exit code.
    text = path.read_text()
    if "ENC[" not in text:
        print("  WARNING: sops exited 0 but no ENC[ block is present — "
              "the secret is still plaintext. Do not commit.")
        return False
    print(f"  encrypted {path.relative_to(REPO)} (SOPS/age)")
    return True


def validate() -> bool:
    """Render the whole staging tree. A scaffold that does not build is useless."""
    print("\nValidating (kubectl kustomize apps/staging)...")
    result = subprocess.run(
        ["kubectl", "kustomize", "apps/staging"],
        cwd=REPO, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr[:2000])
        return False
    print(f"  renders cleanly ({len(result.stdout.splitlines())} lines)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True,
                        help="app name: lowercase, no spaces (also the namespace)")
    parser.add_argument("--image", required=True, help="container image reference")
    parser.add_argument("--port", type=int, required=True, help="container port")
    parser.add_argument("--host", help=f"public hostname (default: <name>.{DOMAIN})")
    parser.add_argument("--storage", default="1Gi", help="PVC size (default 1Gi)")
    parser.add_argument("--no-storage", action="store_true",
                        help="skip the PVC (stateless apps)")
    parser.add_argument("--no-ingress", action="store_true",
                        help="skip the Traefik Ingress")
    parser.add_argument("--no-cloudflare", action="store_true",
                        help="skip the Cloudflare tunnel connector")
    parser.add_argument("--env", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="environment variable (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without writing files")
    args = parser.parse_args()

    name = args.name.lower().strip()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name):
        die(f"--name must be a valid DNS label: {name!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(:[a-zA-Z0-9._-]+)?", args.image):
        die(f"--image looks malformed: {args.image!r}")
    host = args.host or f"{name}.{DOMAIN}"
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        die(f"--host looks malformed: {host!r}")

    base = REPO / "apps/base" / name
    staging = REPO / "apps/staging" / name
    if base.exists() or staging.exists():
        die(f"{name} already exists in apps/base or apps/staging — pick another name")

    env_block = ""
    if args.env:
        lines = "\n".join(
            f"            - name: {k}\n              value: {json_quote(v)}"
            for k, v in (e.split("=", 1) for e in args.env)
        )
        env_block = "          env:\n" + lines + "\n"

    volume_mounts = ""
    volumes = ""
    if not args.no_storage:
        volume_mounts = (
            "          volumeMounts:\n"
            "            - name: data\n"
            "              mountPath: /data\n"
        )
        volumes = (
            "      volumes:\n"
            "        - name: data\n"
            "          persistentVolumeClaim:\n"
            f"            claimName: {name}-data-pvc\n"
        )

    print(f"\nScaffolding {name}")
    print(f"  image {args.image}  port {args.port}  host {host}")
    if args.dry_run:
        print("\n(dry run — nothing written)")
        return

    write(base / "namespace.yaml", NAMESPACE_TEMPLATE.format(name=name))
    write(base / "deployment.yaml", DEPLOYMENT_TEMPLATE.format(
        name=name, image=args.image, port=args.port,
        env_block=env_block, volume_mounts=volume_mounts, volumes=volumes))
    write(base / "service.yaml", SERVICE_TEMPLATE.format(name=name, port=args.port))
    if not args.no_storage:
        write(base / "storage.yaml", STORAGE_TEMPLATE.format(name=name, size=args.storage))
    write(base / "kustomization.yaml", BASE_KUSTOMIZATION_TEMPLATE.format(
        name=name,
        storage_resource="" if args.no_storage else "  - storage.yaml"))

    extra = ""
    if not args.no_ingress:
        write(staging / "ingress.yaml", INGRESS_TEMPLATE.format(
            name=name, host=host, port=args.port))
        extra += "  - ingress.yaml\n"
    if not args.no_cloudflare:
        write(staging / "cloudflare.yaml", CLOUDFLARE_TEMPLATE.format(
            name=name, host=host, port=args.port))
        extra += "  - cloudflare.yaml\n"

    write(staging / "secret.yaml", SECRET_TEMPLATE.format(name=name))
    encrypt_secret(staging / "secret.yaml")
    write(staging / "kustomization.yaml", STAGING_KUSTOMIZATION_TEMPLATE.format(
        name=name, extra_resources=extra))
    register_in_staging(name)

    if not validate():
        print("\nScaffold written but does NOT render. Fix before committing.")
        sys.exit(1)

    print(f"""
Next steps for {name}
----------------------
1. Edit the Secret, then encrypt it:
     sops -e -i apps/staging/{name}/secret.yaml
2. Point the DNS A record for {host} at the cluster ingress IP.
3. Commit and push — Flux applies it:
     git add -A && git commit -m "feat({name}): add {name} to the cluster" && git push
4. Watch it come up:
     kubectl -n {name} get pods -w
5. Verify it properly (do not trust a 200 from the public host alone):
     bash ~/.hermes/skills/homelab/homelab-service-planning/scripts/verify-flux-app.sh \\
       {name} {name} https://{host} {args.port}
6. Create the least-privilege service account for automation, and store it in
   ~/.hermes/credentials/{name}-service.txt (mode 600).
""")


def json_quote(value: str) -> str:
    """Quote a YAML scalar safely."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


if __name__ == "__main__":
    main()
