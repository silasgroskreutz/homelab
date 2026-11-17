# KitchenOwl web frontend with the Brave-crashing autofill token corrected
# AND content-versioned asset URLs + a real cache policy, so patched bundles
# actually reach clients.
#
# WHY THIS IMAGE EXISTS
# ---------------------
# KitchenOwl v0.8.1 (and upstream main) declare the onboarding username
# field with:
#
#     autofillHints: [AutofillHints.newUsername, AutofillHints.username]
#
# "newUsername" is a Flutter-level constant that has NO valid HTML
# `autocomplete` token. The Flutter web engine writes the raw Dart string
# straight into the DOM as:
#
#     autocomplete="newUsername"
#
# That value is invalid per the HTML spec, so Chromium/Brave discard it and
# fall back to HEURISTIC autofill detection on that field. Brave's heuristic
# path can pop an address-autofill suggestion while the user types, and that
# popup crashes the entire browser process (EXC_BREAKPOINT / SIGTRAP, browser
# process, diagnostic key "autofill-suggestions-shown-on-typing"):
#
#     brave/brave-browser#60052
#     brave/brave-browser#59759
#       (same crash; disabling address autofill stops it)
#       (same crash on Win11; "Had to disable save and fill address in
#        brave://settings/contactInfo to resolve")
#
# The login page already emits the valid `autocomplete="username"` token and
# is not affected - only the onboarding (create-first-user) screen crashes.
#
# Stage 1 (patch_autofill_token.py) fixes the token in the compiled bundle.
#
# Stage 2 (patch_web_bundle.py) fixes DELIVERY. Three separate defects kept
# the Stage 1 fix from reaching clients:
#
#   1. v0.8.1 serves the bundle as an unhashed `main.dart.js`. Upstream main
#      builds with `flutter build web --web-content-hash`, so a new build
#      always yields a new URL. We reproduce that by publishing the bundle as
#      `main.dart.<sha256(bundle)[:12]>.js` and pointing the loader's build
#      config at it.
#
#   2. index.html loads the loader by a fixed name (`flutter_bootstrap.js`),
#      and THAT file is what points at the bundle. So the loader is versioned
#      too: index.html now references `flutter_bootstrap.js?v=<hash>`. The
#      chain index.html -> loader -> bundle is content-versioned end to end,
#      so no cache anywhere can serve a stale bundle.
#
#   3. The origin sends NO Cache-Control at all, so Cloudflare applies its
#      default `max-age=14400` (four hours) to .js assets and browsers apply
#      heuristic freshness to the HTML entry point. The patch installs
#      upstream main's cache rules (uwsgi `route ... addheader:`), extended
#      so the legacy unhashed `main.dart.js` is `no-cache` as well - a client
#      still resolving the OLD url revalidates and receives the PATCHED bytes.
#
# Both patch scripts are deliberately strict - they refuse to continue unless
# they find EXACTLY the expected content, so an upstream bundle change can
# never silently produce a half-patched image.
#
# Build and push (from a machine that can reach the internal registry):
#   docker build -t 192.168.0.101:5000/kitchenowl-test:v0.8.1-patched2 tools/kitchenowl-patch
#   docker push 192.168.0.101:5000/kitchenowl-test:v0.8.1-patched2
FROM tombursch/kitchenowl:v0.8.1

# The frontend bundle is owned by kitchenowl:kitchenowl (UID/GID 1000) and
# the upstream image runs as root with entrypoint.sh dropping privileges
# via gosu. Patching as root preserves that ownership model.
USER root

COPY patch_autofill_token.py /tmp/patch_autofill_token.py
COPY patch_web_bundle.py /tmp/patch_web_bundle.py

RUN python3 /tmp/patch_autofill_token.py /var/www/web/kitchenowl/main.dart.js \
    && python3 /tmp/patch_web_bundle.py \
         /var/www/web/kitchenowl \
         /usr/src/kitchenowl/wsgi.ini \
    && rm -f /tmp/patch_autofill_token.py /tmp/patch_web_bundle.py \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/main.dart.js \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/main.dart.*.js \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/flutter_bootstrap.js \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/flutter.js \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/index.html \
    && chown kitchenowl:kitchenowl /usr/src/kitchenowl/wsgi.ini

USER kitchenowl
