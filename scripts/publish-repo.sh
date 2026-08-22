#!/bin/bash
# Build and publish the NETHOS package repository.
#
#   scripts/publish-repo.sh              convert and index, locally
#   scripts/publish-repo.sh --publish    ...and upload to a GitHub release
#   scripts/publish-repo.sh --r2         ...and upload to Cloudflare R2
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
R2=0
case "${1:-}" in
    --publish) PUBLISH=1 ;;
    --r2)      R2=1 ;;
esac

# Uploading and serving are two different hostnames on R2.
#
# The S3 API endpoint signs every request, so an anonymous GET to it returns
# 400 and npkg -- which fetches with a plain GET -- cannot read the repository
# from it at all. Packages are served from the public bucket URL instead, and
# that is what goes in repos.json.
R2_REMOTE="${NETHOS_R2_REMOTE:-r2}"
R2_BUCKET="${NETHOS_R2_BUCKET:-nethos}"
R2_PUBLIC="${NETHOS_R2_PUBLIC:-}"

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

# Debian Essential packages, which nothing declares a dependency on.
#
# Being Essential means every other package may assume it is present without
# saying so, so dependency resolution never reaches them and they were absent
# from the repository entirely. libc-bin is the one that showed: it owns
# /usr/bin/ldd, mkinitramfs calls ldd to find the libraries a binary needs,
# and without it update-initramfs failed with "no ldd around" after the whole
# system had already been installed.
#
# The Debian bootstrap path already adds these (archive.base_seeds()); the
# repository was built without them, so the two produced different systems.
essential = archive.base_seeds()
extra = sorted(set(essential) - set(seeds))
if extra:
    print(f"  + {len(extra)} essential/required packages Debian assumes present")
seeds = sorted(set(seeds) | set(essential))
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
print("  index: %d packages" % len(idx.get("packages", [])))

# Close the repository under npkg resolution, not Debian resolution.
#
# The two do not agree. Debian satisfies some dependencies through virtual
# packages and alternatives that npkg resolves differently, so a set that
# apt considers complete can still leave npkg saying "no package satisfies
# gir1.2-cairo-1.0" halfway through an install -- on the target machine, with
# the disk already formatted.
#
# So ask npkg itself what is missing, fetch exactly that, and repeat until it
# has nothing left to ask for.
from npkg import Repository, Database, DependencyError, Solver
import re, tempfile

for attempt in range(1, 26):
    fake = tempfile.mkdtemp()
    repo = Repository("local", npks, fake)
    repo.fetch_index()
    solver = Solver(Database(fake), [repo])
    have = [n for n in seeds if repo.best(n)]
    try:
        plan = solver.resolve(have)
        print("  closure: %d packages resolve with nothing unmet" % len(plan))
        break
    except DependencyError as exc:
        text = str(exc)
        if "satisfies" not in text:
            print("  closure: %s" % text)
            break
        # chr(39) is a single quote. Writing one here would end the shell
        # string this whole script is embedded in, which has cost four builds.
        q = chr(39)
        token = text.split("satisfies ", 1)[1].strip()
        token = token.replace(q, "").strip()
        want = re.split(r"[<>=(\s]", token)[0]
        print("  closure pass %d: adding %s" % (attempt, want))
        # The name may not be a package at all. gir1.2-cairo-1.0 is a virtual
        # name that gir1.2-freedesktop declares in Provides, and asking Debian
        # to resolve it directly finds nothing -- which is how this loop span
        # eight times adding the same missing thing.
        if want in archive.packages:
            extra = archive.resolve([want])
        else:
            providers = [f for f in archive.packages.values()
                         if want in [p.strip().split(" ")[0]
                                     for p in (f.get("Provides") or "").split(",")]]
            if not providers:
                print("    nothing in Debian provides %s; giving up on it" % want)
                break
            print("    provided by %s" % providers[0].get("Package"))
            extra = archive.resolve([providers[0]["Package"]])
        new_paths = []
        for f in extra:
            npk_name = None
            try:
                new_paths.append(archive.download(f, debs))
            except Exception as e:
                print("    could not fetch %s: %s" % (f.get("Package"), e))
        added = 0
        for npk, err in nb._convert_many(new_paths, npks):
            if not err:
                added += 1
        print("    +%d packages" % added)
        build_index(npks)
else:
    print("  closure: gave up after 25 passes")

idx = build_index(npks)
n = len(idx.get("packages", []))
print("  index: %d packages" % n)
PY
' 2>&1 | grep -vE "^\s*$"

count=$(ls -1 "$OUT"/*.npk 2>/dev/null | wc -l | tr -d ' ')
[ "$count" -gt 0 ] || die "no packages were produced"
size=$(du -sh "$OUT" | cut -f1)
say "Built: $count packages, $size, index at $OUT/index.json"

if [ "$R2" = 1 ]; then
    command -v rclone >/dev/null || die "rclone is needed for R2 (brew install rclone)"
    rclone listremotes 2>/dev/null | grep -q "^${R2_REMOTE}:" || die \
        "no rclone remote called '$R2_REMOTE'.

  Create one without putting the secret in your shell history:
      rclone config
    n) new remote   name: $R2_REMOTE   storage: s3   provider: Cloudflare
    endpoint: https://3710ed8f24d77f61a3aea82883bb1a9f.r2.cloudflarestorage.com"

    say "Uploading $size to $R2_REMOTE:$R2_BUCKET"
    # --checksum, not timestamps: re-running should upload what changed and
    # nothing else, and object stores have no useful mtime.
    rclone copy "$OUT" "$R2_REMOTE:$R2_BUCKET" --checksum --transfers 16 \
        --progress --s3-no-check-bucket
    remote_count=$(rclone lsf "$R2_REMOTE:$R2_BUCKET" 2>/dev/null | grep -c "\.npk$" || echo 0)
    say "$remote_count packages in the bucket"
    [ "$remote_count" -ge "$count" ] || die "the upload did not take"

    if [ -n "$R2_PUBLIC" ]; then
        say "Checking it is readable without credentials"
        code=$(curl -s -o /dev/null -w "%{http_code}" "$R2_PUBLIC/index.json" --max-time 20)
        if [ "$code" = 200 ]; then
            say "  $R2_PUBLIC/index.json -> 200"
            say "  put this in /etc/npkg/repos.json as the url"
        else
            say "  $R2_PUBLIC/index.json -> $code"
            say "  the bucket is not public yet: enable the Public Development"
            say "  URL in the R2 dashboard, or bind a custom domain."
        fi
    else
        say "Set NETHOS_R2_PUBLIC to the bucket public URL to verify it serves."
    fi
fi

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
