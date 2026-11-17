#!/usr/bin/env python3
"""Correct the invalid HTML autofill token in the compiled KitchenOwl web bundle.

KITCHENOWL DEFECT
-----------------
kitchenowl/lib/widgets/create_user_form_fields.dart declares the onboarding
username field with:

    autofillHints: [AutofillHints.newUsername, AutofillHints.username]

"newUsername" is a Flutter-level constant that has NO valid HTML
`autocomplete` token. The Flutter web engine therefore writes the raw Dart
string straight into the DOM as:

    autocomplete="newUsername"

That value is invalid per the HTML spec, so Chromium/Brave discard it and
fall back to HEURISTIC autofill detection on that field. Brave's heuristic
path can pop an address-autofill suggestion while the user types, and that
popup crashes the whole browser process (EXC_BREAKPOINT / SIGTRAP, browser
process, diagnostic key "autofill-suggestions-shown-on-typing"):

    brave/brave-browser#60052
    brave/brave-browser#59759  (same crash, disabling address autofill stops it)

The compiled Flutter bundle is what actually ships in the image, so the
correction is applied to main.dart.js: the hint list for that one field is
reordered so the valid "username" token comes first. The login page already
does this correctly and is unaffected.

This script is deliberately strict: it refuses to patch unless it finds
EXACTLY ONE occurrence of the target sequence, so an upstream bundle change
can never silently produce an unpatched image.
"""

import pathlib
import sys

OLD = '["newUsername","username"]'
NEW = '["username","newUsername"]'


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-main.dart.js>", file=sys.stderr)
        return 2

    path = pathlib.Path(sys.argv[1])
    if not path.is_file():
        print(f"ERROR: {path} is not a file", file=sys.stderr)
        return 2

    original = path.read_text(encoding="utf-8")
    count = original.count(OLD)
    if count != 1:
        print(
            f"ERROR: expected exactly 1 occurrence of {OLD} in {path}, "
            f"found {count}. Refusing to patch - the upstream bundle changed "
            f"and the fix needs to be re-derived.",
            file=sys.stderr,
        )
        return 1

    path.write_text(original.replace(OLD, NEW), encoding="utf-8")

    print(f"patched {path}: autofill hints {OLD} -> {NEW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
