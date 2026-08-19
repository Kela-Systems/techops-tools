#!/usr/bin/env bash
# ============================================================================
#  stage-bundle.sh — copy a received Kela drive to local storage and verify it,
#  so that the drive itself can then be erased and rebuilt as the bootable
#  install stick.
#
#  Use this when you have ONE physical stick that arrives carrying the bundle
#  and is also the stick you want to make bootable. make-usb-macos.sh erases its
#  target, so it cannot read the bundle off that same disk.
#
#  Usage:
#    ./stage-bundle.sh "/Volumes/<received drive>" ~/kela-staging
#
#  Then build from the staged copy:
#    sudo ./make-usb-macos.sh disk4 ~/kela-staging/<bundle folder>
#
#  No sudo needed. The received drive is only ever read.
#
#  IMPORTANT: the staged copy carries live cluster-CA slot keys — same custody
#  rules as the drive. Delete it once the built stick verifies, and never build a
#  second stick from a stale staging copy: it would resurrect slot keys that the
#  first stick has already consumed and hand two boxes the same cluster CA.
# ============================================================================

set -euo pipefail

SRC="${1:?usage: $0 \"/Volumes/<received drive>\" /path/to/staging}"
DEST="${2:?usage: $0 \"/Volumes/<received drive>\" /path/to/staging}"

# shellcheck source=lib/common.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

[ -d "$SRC/02-kela" ] || die "$SRC doesn't look like a Kela drive (no 02-kela/)"
[ -r "$SRC/02-kela/MANIFEST.json" ] || die "no MANIFEST.json under $SRC/02-kela"

NAME=$(basename "$SRC")
TARGET="$DEST/$NAME"
[ -e "$TARGET" ] && die "$TARGET already exists — remove it or pick another staging path"

echo "== preflight =="
mkdir -p "$DEST"
SRC_BYTES=$(( $(du -sk "$SRC" | cut -f1) * 1024 ))
FREE_BYTES=$(( $(df -k "$DEST" | awk 'NR==2{print $4}') * 1024 ))
[ "$FREE_BYTES" -ge "$(( SRC_BYTES + 1000000000 ))" ] \
  || die "only $(human "$FREE_BYTES") free at $DEST; need $(human "$SRC_BYTES") plus slack"
echo "  bundle $(human "$SRC_BYTES"), free at $DEST $(human "$FREE_BYTES") — ok"

export COPYFILE_DISABLE=1

echo "== copying (this is the long part) =="
mkdir -p "$TARGET"
if ! cp -RX "$SRC/." "$TARGET/"; then
  echo "  note: cp reported errors (usually macOS sidecar files) — verifying below"
fi
find "$TARGET" \( -name '.DS_Store' -o -name '._*' \) -delete 2>/dev/null || true

echo "== repairing scanner-mangled names on the staged copy =="
cd "$TARGET/02-kela"
python3 "$LIBDIR/repair-names.py"

echo "== verifying the staged copy against the signed manifest =="
python3 "$LIBDIR/verify-manifest.py" \
  || die "the staged copy does not match the manifest — do NOT erase the drive"
cd /

cat <<EOF

Staged and verified at:
  $TARGET

The received drive was only read, so it is still intact. Now build the bootable
stick — this ERASES the stick, which is safe because the verified copy above is
the source:

  sudo ./make-usb-macos.sh <diskN> "$TARGET"

Find the disk id with:  diskutil list external

Afterwards, delete the staging copy:

  rm -rf "$TARGET"

It holds live cluster-CA slot keys, and rebuilding from a stale copy would reuse
slots that the first stick has already consumed.
EOF
