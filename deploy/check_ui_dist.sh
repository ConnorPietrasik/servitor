#!/usr/bin/env bash
# Servitor UI dist certification — the artifact, not the exit code.
#
# `vite build` exits 0 and can still leave a dist the browser cannot run: a
# half-written asset, an index.html pointing at a bundle that is not there, a
# `base` that does not match how the app serves it. The app then answers 200
# with a black screen, which on a phone behind the gate is indistinguishable
# from an API-only image. So the dist is certified the way the browser will
# read it: index.html plus every LOCAL asset it references, resolved against
# the dist root (the app serves the dist at `/`, so `/assets/x.js` is
# `<dist>/assets/x.js`).
#
# Called by deploy/guest/build.sh (before the dist is baked) and
# deploy/sync.sh (before it is pushed, every sync — a truncated dist left by
# an interrupted build is otherwise content-hashed and deployed as truth).
#
# Usage: deploy/check_ui_dist.sh <dist dir>
set -euo pipefail

DIST="${1:?usage: check_ui_dist.sh <dist dir>}"
[[ -d "$DIST" ]] || { echo "ui dist missing: $DIST (npm run build produced nothing)" >&2; exit 1; }
[[ -s "$DIST/index.html" ]] || { echo "ui dist has no index.html: $DIST" >&2; exit 1; }

BAD=()
# every src=/href= value in index.html, double- or single-quoted. Anything
# scheme-qualified, root-relative-to-another-host, data:, or a bare fragment is
# the browser's business, not the dist's.
REFS="$(
  { grep -oE '(src|href)[[:space:]]*=[[:space:]]*"[^"]*"' "$DIST/index.html" || true
    grep -oE "(src|href)[[:space:]]*=[[:space:]]*'[^']*'" "$DIST/index.html" || true; } \
    | sed -E 's/^[^=]*=[[:space:]]*//; s/^["'\'']//; s/["'\'']$//'
)"
while IFS= read -r ref; do
  case "$ref" in
    ""|http://*|https://*|data:*|"*//*"|\#*|mailto:*|tel:*) continue ;;
  esac
  path="${ref%%[?#]*}"                       # drop query/fragment
  [[ "$path" == /* ]] && path="${path#/}"     # served from the app root == dist root
  [[ "$path" == *..* ]] && { BAD+=("index.html references '$ref' — a path that escapes the dist"); continue; }
  [[ -s "$DIST/$path" ]] || BAD+=("index.html references '$ref' — '$DIST/$path' missing or empty")
done <<< "$REFS"

# a React bundle that exists is the difference between a SPA and a blank page
[[ -n "$(find "$DIST" -name '*.js' -size +1k -print -quit 2>/dev/null)" ]] \
  || BAD+=("no JS bundle over 1k in $DIST — vite produced an empty dist")

if (( ${#BAD[@]} )); then
  echo "ui dist is not servable ($DIST):" >&2
  for b in "${BAD[@]}"; do echo "    FAIL  $b" >&2; done
  exit 1
fi
echo "ui dist certified ($(find "$DIST" -type f | wc -l) files, index.html's references resolve)"
