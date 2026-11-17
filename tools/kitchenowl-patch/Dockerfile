# KitchenOwl web frontend with the invalid HTML autofill token corrected.
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
# The Flutter web bundle is what ships in the image, so the correction is
# applied to main.dart.js at build time: the hint list for that one field is
# reordered so the valid "username" token comes first.
#
# The patch script is deliberately strict - it refuses to patch unless it
# finds EXACTLY ONE occurrence of the target sequence, so an upstream bundle
# change can never silently produce an unpatched image.
#
# Build and push (from a machine that can reach the internal registry):
#   docker build -t 192.168.0.101:5000/kitchenowl-test:v0.8.1-patched1 tools/kitchenowl-patch
#   docker push 192.168.0.101:5000/kitchenowl-test:v0.8.1-patched1
FROM tombursch/kitchenowl:v0.8.1

# The frontend bundle is owned by kitchenowl:kitchenowl (UID/GID 1000) and
# the upstream image runs as root with entrypoint.sh dropping privileges
# via gosu. Patching as root preserves that ownership model.
USER root

COPY patch_autofill_token.py /tmp/patch_autofill_token.py

RUN python3 /tmp/patch_autofill_token.py /var/www/web/kitchenowl/main.dart.js \
    && rm -f /tmp/patch_autofill_token.py \
    && chown kitchenowl:kitchenowl /var/www/web/kitchenowl/main.dart.js

USER kitchenowl
