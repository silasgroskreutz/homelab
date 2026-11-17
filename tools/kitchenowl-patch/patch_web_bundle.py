#!/usr/bin/env python3
"""Content-version the KitchenOwl web frontend and fix its cache policy.

WHY THIS EXISTS
---------------
Three independent delivery defects kept the Brave-crash fix from reaching
clients. All three are fixed here.

DEFECT 1 - the bundle URL is not content-addressed
    KitchenOwl v0.8.1 serves the compiled Flutter bundle as a fixed name,
    `main.dart.js`. Upstream `main` builds with
    `flutter build web --web-content-hash`, which emits `main.dart.<hash>.js`
    instead, so a new build always produces a new URL. v0.8.1 does not, so
    redeploying a fixed image does NOT invalidate any client cache: a browser
    that visited the site before the fix keeps executing the OLD bundle.

    We cannot rebuild the Flutter frontend here, so we reproduce the
    guarantee ourselves:

        main.dart.js  ->  main.dart.<sha256(bundle)[:12]>.js

    and point the loader's build config at that name. The version is derived
    from the bundle's own bytes, so identical content yields an identical
    name (no cache churn across rebuilds) and changed content always yields a
    new name (no cache can serve it stale).

DEFECT 2 - the loader is not versioned either
    index.html loads `flutter_bootstrap.js` by a fixed name, and THAT file is
    what points at the bundle. If the loader stays cached, the client keeps
    resolving the OLD bundle name even though a new one exists. So index.html
    is rewritten to reference the loader with a build-scoped query string:

        flutter_bootstrap.js?v=<sha256(patched loader)[:12]>

    A changed bundle therefore changes the loader, which changes the query
    string, which is a brand new cache key. The chain
    index.html -> loader -> bundle is content-versioned end to end.

DEFECT 3 - no cache policy on the origin
    v0.8.1's uwsgi config has no `addheader` rules, so the origin sends no
    `Cache-Control` at all. Cloudflare then applies its own default
    `max-age=14400` (four hours) to .js assets, and browsers apply
    heuristic freshness to the HTML entry point. A client can therefore hold
    a stale bundle for hours after a fix is deployed.

    Upstream `main` ships the correct rules in `backend/wsgi.ini`. This
    script installs the same rules, extended so that the legacy unhashed
    `main.dart.js` is served `no-cache` as well - a client still resolving
    the OLD URL then revalidates and receives the PATCHED bytes instead of
    the pre-fix ones.

    Final policy:
        index.html, flutter_bootstrap.js, flutter_service_worker.js,
        manifest.json, version.json, flutter.js   ->  no-cache
        main.dart.js (legacy name)                 ->  no-cache
        main.dart.<hash>.js, assets/, canvaskit/,
        icons/                                      ->  public, max-age=31536000, immutable
        SPA routes                                  ->  no-cache + index.html

Both loader files are patched:
  * flutter_bootstrap.js - carries the build config actually used
  * flutter.js           - the legacy loader, patched for consistency

The original `main.dart.js` is KEPT in place and now serves the same patched
bytes, so a client running an older cached bootstrap lands on patched code
the moment it revalidates.

The script is deliberately strict: it refuses to continue unless it finds
EXACTLY the expected content, so an upstream layout change can never
silently produce a half-patched image.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

WEB_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/www/web/kitchenowl")
WSGI_INI = Path(sys.argv[2] if len(sys.argv) > 2 else "/usr/src/kitchenowl/wsgi.ini")

BUNDLE = WEB_ROOT / "main.dart.js"
BOOTSTRAP = WEB_ROOT / "flutter_bootstrap.js"
FLUTTER_JS = WEB_ROOT / "flutter.js"
INDEX = WEB_ROOT / "index.html"

# A bare bundle reference. The lookahead keeps "main.dart.js.map" (source map)
# untouched and never matches a longer identifier.
BUNDLE_REF = re.compile(r"main\.dart\.js(?![.\w])")

# The loader reference inside index.html.
BOOTSTRAP_REF = re.compile(r'src="flutter_bootstrap\.js"')

# The single uwsgi route line shipped by v0.8.1. It is replaced wholesale.
OLD_ROUTE = r"route = ^\/(?!api)(?!mcp)[^\.]*$ static:/var/www/web/kitchenowl/index.html"

# The replacement block. `route` rules in uwsgi are evaluated in order and
# every matching rule contributes, so the rules are ordered most-specific
# first and the broad immutable rule no longer covers the entry points.
NEW_ROUTES = r"""route = ^\/(index\.html|flutter_bootstrap\.js|flutter_service_worker\.js|manifest\.json|version\.json|flutter\.js)$ addheader:Cache-Control: no-cache
route = ^\/main\.dart\.js$ addheader:Cache-Control: no-cache
route = ^\/main\.dart\.[0-9a-f]{12}\.js$ addheader:Cache-Control: public, max-age=31536000, immutable
route = ^\/(assets|canvaskit|icons)\/ addheader:Cache-Control: public, max-age=31536000, immutable
route = ^\/(?!api)(?!mcp)[^.]*$ addheader:Cache-Control: no-cache
route = ^\/(?!api)(?!mcp)[^.]*$ static:/var/www/web/kitchenowl/index.html"""


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def patch_bundle() -> str:
    """Publish the bundle under its content-hashed name; return that name."""
    bundle_bytes = BUNDLE.read_bytes()
    versioned_name = f"main.dart.{sha12(bundle_bytes)}.js"
    print(f"[patch] bundle content version: {sha12(bundle_bytes)}")
    print(f"[patch] versioned bundle name: {versioned_name}")

    target = WEB_ROOT / versioned_name
    if target.exists():
        fail(f"versioned bundle already exists: {target}")
    shutil.copyfile(BUNDLE, target)
    print(f"[patch] published {target.name}")

    for loader in (BOOTSTRAP, FLUTTER_JS):
        text = loader.read_text()
        count = len(BUNDLE_REF.findall(text))
        if count == 0:
            fail(f"no 'main.dart.js' reference found in {loader.name}")
        if versioned_name in text:
            fail(f"{loader.name} already points at a versioned bundle; refusing to double-patch")
        loader.write_text(BUNDLE_REF.sub(versioned_name, text))
        print(f"[patch] {loader.name}: rewrote {count} bundle reference(s)")

    boot = BOOTSTRAP.read_text()
    if f'"mainJsPath":"{versioned_name}"' not in boot:
        fail("flutter_bootstrap.js build config does not point at the versioned bundle")
    print("[patch] verified flutter_bootstrap.js build config")
    return versioned_name


def patch_index() -> None:
    """Version the loader reference in index.html."""
    text = INDEX.read_text()
    count = len(BOOTSTRAP_REF.findall(text))
    if count != 1:
        fail(f"expected exactly one flutter_bootstrap.js reference in index.html, found {count}")

    loader_version = sha12(BOOTSTRAP.read_bytes())
    INDEX.write_text(
        BOOTSTRAP_REF.sub(f'src="flutter_bootstrap.js?v={loader_version}"', text)
    )
    print(f"[patch] index.html: versioned loader reference (v={loader_version})")

    check = INDEX.read_text()
    if f'src="flutter_bootstrap.js?v={loader_version}"' not in check:
        fail("index.html loader reference was not rewritten")
    print("[patch] verified index.html")


def patch_wsgi_ini() -> None:
    """Install the cache-control rules into the uwsgi config."""
    text = WSGI_INI.read_text()

    if "addheader:Cache-Control" in text:
        fail("wsgi.ini already carries cache-control rules; refusing to double-patch")
    if text.count(OLD_ROUTE) != 1:
        fail(
            "expected exactly one occurrence of the v0.8.1 static route in "
            f"{WSGI_INI}, found {text.count(OLD_ROUTE)}"
        )

    WSGI_INI.write_text(text.replace(OLD_ROUTE, NEW_ROUTES))
    print(f"[patch] installed cache-control rules in {WSGI_INI}")

    check = WSGI_INI.read_text()
    if check.count("addheader:Cache-Control") != 5:
        fail("expected 5 cache-control rules after patching")
    if "static:/var/www/web/kitchenowl/index.html" not in check:
        fail("SPA fallback route was lost while patching")
    print("[patch] verified wsgi.ini")


def main() -> int:
    for path in (BUNDLE, BOOTSTRAP, FLUTTER_JS, INDEX, WSGI_INI):
        if not path.is_file():
            fail(f"expected file is missing: {path}")

    patch_bundle()
    patch_index()
    patch_wsgi_ini()

    print("[patch] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
