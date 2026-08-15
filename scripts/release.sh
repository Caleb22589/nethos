#!/bin/bash
# Publish a NETHOS image as a GitHub release.
#
#     scripts/release.sh v0.1
#     scripts/release.sh v0.1 --draft
#
# GitHub Releases rather than a web host, because it needs no domain, no
# server and no bill: a public repository gets the bandwidth, and the 2GB
# per-file limit is not a constraint once the image is compressed -- most of a
# disk image is zeroes and repeated text, so ~2GB becomes well under 1GB.
#
# The site lives on GitHub Pages at caleb22589.github.io/nethos, which is the
# same argument: no domain, HTTPS included.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
TAG="${1:-}"
shift || true
DRAFT=""
for arg in "$@"; do
    [ "$arg" = "--draft" ] && DRAFT="--draft"
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$TAG" ] || die "usage: scripts/release.sh vX.Y [--draft]"
command -v gh >/dev/null || die "gh is not installed: https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "gh is not logged in: gh auth login"

IMG="$BUILD/nethos-x86_64.img"
[ -f "$IMG" ] || die "no image at $IMG
Build and prepare it first:
  scripts/build-x86.sh && scripts/make-usb.sh"

# --------------------------------------------------------------------------
# Compress. zstd if it is here (much faster, same ballpark ratio), xz if not.
if command -v zstd >/dev/null; then
    OUT="$IMG.zst"
    say "Compressing with zstd (this is the slow part)"
    zstd -19 -T0 --long -f -o "$OUT" "$IMG"
elif command -v xz >/dev/null; then
    OUT="$IMG.xz"
    say "Compressing with xz"
    xz -9 -T0 -kf "$IMG"
else
    die "neither zstd nor xz is installed"
fi

raw=$(stat -c%s "$IMG" 2>/dev/null || stat -f%z "$IMG")
small=$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT")
say "$(( raw / 1048576 ))MB -> $(( small / 1048576 ))MB"
[ "$small" -lt 2000000000 ] || die "over GitHub's 2GB per-file limit.
Build with fewer sets, e.g.:
  scripts/build-x86.sh --sets \"base system kernel desktop firmware\""

# Checksums, because nobody should install an operating system from a download
# they cannot verify. Written next to the image and attached to the release.
say "Checksums"
SUMS="$BUILD/SHA256SUMS"
( cd "$BUILD" && sha256sum "$(basename "$OUT")" > SHA256SUMS ) 2>/dev/null \
  || ( cd "$BUILD" && shasum -a 256 "$(basename "$OUT")" > SHA256SUMS )
cat "$SUMS"

# --------------------------------------------------------------------------
say "Publishing $TAG"
NOTES=$(cat <<EOF
A bootable NETHOS image for x86-64.

**Install**

\`\`\`bash
zstd -d nethos-x86_64.img.zst          # or: xz -d nethos-x86_64.img.xz
sudo dd if=nethos-x86_64.img of=/dev/sdX bs=4M status=progress conv=fsync
\`\`\`

Boot with UEFI and **Secure Boot disabled** — this GRUB is not signed, and a
machine with Secure Boot on refuses it without explaining why.

The root filesystem grows to fill the disk on first boot, so a small write
still gives you the whole drive.

Log in as \`neth\` / \`nethos\`.

**Verify before you install**

\`\`\`bash
sha256sum -c SHA256SUMS
\`\`\`

Built $(date -u +%Y-%m-%d) from $(git -C "$ROOT" rev-parse --short HEAD).
EOF
)

if gh release view "$TAG" >/dev/null 2>&1; then
    say "Release exists; uploading assets to it"
    gh release upload "$TAG" "$OUT" "$SUMS" --clobber
else
    gh release create "$TAG" "$OUT" "$SUMS" \
        --title "NETHOS $TAG" --notes "$NOTES" $DRAFT
fi

say "Done"
gh release view "$TAG" --json url --jq .url
