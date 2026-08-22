#!/bin/bash
# Build and publish the NETHOS package repository.
#
#   scripts/publish-repo.sh              convert and index, locally
#   scripts/publish-repo.sh --publish    ...and upload it
#
# Converting Debian packages to npkg is decompression and repacking: identical
# on every machine, and pure CPU. Doing it during an install means a two-core
# 2012 laptop spends the best part of an hour redoing work that has already
# been done here. So do it once and serve the result.
#
# npkg already speaks this: Repository fetches an index.json, resolves against
# it, downloads by filename and verifies a sha256. A GitHub release is a flat
# set of files under one base URL, which is exactly the shape it wants, so
# nothing in npkg needs to change to be served from one.
set -euo pipefail

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/build/repo"
SETS="${NETHOS_REPO_SETS:-base system kernel desktop firmware net browser installer}"
TAG="${NETHOS_REPO_TAG:-repo-x86_64}"
PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

command -v docker >/dev/null || die "docker (colima) is needed"
mkdir -p "$OUT"

say "Converting: $SETS"
docker run --rm -v "$HERE":/nethos:ro -v "$OUT":/out -v nethos-repo-cache:/cache \
    -e SETS="$SETS" -w /work debian:trixie bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq python3 zstd xz-utils >/dev/null 2>&1
python3 -u - <<"PY"
import os, sys, json
sys.path.insert(0, "/nethos/pkg")
import npkg_bootstrap as nb
from npkg_convert import convert_deb
from npkg import build_index

sets = os.environ["SETS"].split()
seeds = []
for s in sets:
    seeds += nb.SETS.get(s, [])
seeds = sorted(set(seeds))
print(f"  {len(seeds)} seed packages from {len(sets)} sets")

archive = nb.DebianArchive(arch="amd64")
archive.load()
resolved = archive.resolve(seeds)
print(f"  {len(resolved)} packages after dependency resolution")

debs, npks = "/cache/debs", "/out"
os.makedirs(debs, exist_ok=True)

import concurrent.futures as futures
paths = [""] * len(resolved)
done = 0
with futures.ThreadPoolExecutor(max_workers=16) as pool:
    jobs = {pool.submit(archive.download, f, debs): i for i, f in enumerate(resolved)}
    for job in futures.as_completed(jobs):
        paths[jobs[job]] = job.result()
        done += 1
        if done % 100 == 0 or done == len(resolved):
            print(f"  downloaded {done}/{len(resolved)}")

done = 0
for npk, err in nb._convert_many(paths, npks):
    done += 1
    if err:
        print(f"  skipped {err}")
    if done % 100 == 0 or done == len(resolved):
        print(f"  converted {done}/{len(resolved)}")

idx = build_index(npks)
n = len(idx.get("packages", []))
print("  index: %d packages" % n)
PY
' 2>&1 | grep -vE "^\s*$"

count=$(ls -1 "$OUT"/*.npk 2>/dev/null | wc -l | tr -d ' ')
[ "$count" -gt 0 ] || die "no packages were produced"
size=$(du -sh "$OUT" | cut -f1)
say "Built: $count packages, $size, index at $OUT/index.json"

if [ "$PUBLISH" = 1 ]; then
    command -v gh >/dev/null || die "gh is needed to publish"
    say "Publishing as $TAG (this uploads $size)"
    gh release view "$TAG" --repo Caleb22589/nethos >/dev/null 2>&1 || \
        gh release create "$TAG" --repo Caleb22589/nethos \
            --title "NETHOS package repository (x86_64)" \
            --notes "Debian packages converted to npkg format, once, so that installing NETHOS does not mean converting six hundred of them on the machine being installed.

npkg reads index.json from here and downloads packages by name, verifying each against the sha256 in the index.

    npkg install <name>

Rebuilt with scripts/publish-repo.sh --publish." >/dev/null
    # Uploaded in batches: one gh call per file is a round trip each, and one
    # call with six hundred arguments is longer than a command line allows.
    # From inside the directory, with NUL separators. Piping ls into xargs
    # split the path at the space in "untitled folder" and uploaded nothing at
    # all, while reporting success.
    ( cd "$OUT" && find . -maxdepth 1 \( -name "*.npk" -o -name "index.json" \) \
        -printf "%f\0" | xargs -0 -n 40 \
        gh release upload "$TAG" --repo Caleb22589/nethos --clobber )
    uploaded=$(gh release view "$TAG" --repo Caleb22589/nethos --json assets \
        --jq ".assets | length" 2>/dev/null || echo 0)
    say "$uploaded assets on the release"
    [ "$uploaded" -gt 100 ] || die "the upload did not take"
    say "Published: https://github.com/Caleb22589/nethos/releases/tag/$TAG"
fi
