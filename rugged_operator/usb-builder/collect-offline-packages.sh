#!/usr/bin/env bash
# ============================================================================
#  collect-offline-packages.sh — build the offline .deb pool for the Kela
#  operator ISO (option "3a": fully offline install, no WAN during install).
#
#  Runs a clean ubuntu:24.04 amd64 container so the FULL dependency closure of
#  the desktop + tools is downloaded (not just the deltas vs. your host), then
#  adds the third-party debs (Tailscale, AnyDesk, Chrome) and builds a flat
#  apt repo index (Packages/Packages.gz). The result drops into ./offline-pool
#  and is embedded into the ISO by build-iso.sh.
#
#  Why a container: `apt-get install --download-only` on your own machine skips
#  anything already installed. A pristine ubuntu:24.04 base is a subset of the
#  Ubuntu **Server** base we install onto, so everything the container skips is
#  guaranteed already present on the target — making this closure sufficient
#  for an offline server-ISO install.
#
#  Requirements on the build host:
#    - Docker (Desktop on macOS, or engine on Linux)
#    - ~3 GB free disk for the pool
#
#  Usage:
#    ./collect-offline-packages.sh [output-dir]      # default ./offline-pool
#
#  Run this once; re-run only when you want to refresh package versions.
#  After it finishes, run build-iso.sh as usual — it auto-detects ./offline-pool.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$SCRIPT_DIR/offline-pool}"

# Packages we install on top of the Ubuntu Server base:
#  - desktop:  ubuntu-desktop-minimal + gdm3 + a terminal (matches user-data).
#              This closure already pulls the Xorg session bits (xserver-xorg,
#              xserver-xorg-input-libinput for the CF-33 touchscreen,
#              gnome-session) that the kiosk needs for WaylandEnable=false.
#  - tools:    everything operator-setup.sh §2 apt-installs, incl. onboard
#              (tablet on-screen keyboard) and evtest (A1 scancode capture).
DESKTOP_PKGS="ubuntu-desktop-minimal gdm3 gnome-terminal"
TOOL_PKGS="openssh-server curl wget ca-certificates apt-transport-https gnupg lsb-release ufw rfkill libnss3-tools power-profiles-daemon dconf-cli onboard evtest"
THIRDPARTY_PKGS="tailscale anydesk google-chrome-stable"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker not found. Install Docker Desktop (macOS) or docker engine (Linux)." >&2
  exit 1
}

mkdir -p "$OUT_DIR"
echo "==> Collecting offline package pool into: $OUT_DIR"
echo "    (clean ubuntu:24.04 amd64 container — this pulls ~2 GB, give it a few minutes)"

docker run --rm --platform linux/amd64 \
  -e DEBIAN_FRONTEND=noninteractive \
  -e DESKTOP_PKGS="$DESKTOP_PKGS" \
  -e TOOL_PKGS="$TOOL_PKGS" \
  -e THIRDPARTY_PKGS="$THIRDPARTY_PKGS" \
  -v "$OUT_DIR":/out \
  ubuntu:24.04 bash -euo pipefail -c '
    set -x
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl gnupg wget dpkg-dev

    install -m 0755 -d /etc/apt/keyrings

    # --- Tailscale repo (noble) ---
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
      -o /etc/apt/sources.list.d/tailscale.list

    # --- AnyDesk repo ---
    curl -fsSL https://keys.anydesk.com/repos/DEB-GPG-KEY \
      -o /etc/apt/keyrings/keys.anydesk.com.asc
    chmod a+r /etc/apt/keyrings/keys.anydesk.com.asc
    echo "deb [signed-by=/etc/apt/keyrings/keys.anydesk.com.asc] http://deb.anydesk.com/ all main" \
      > /etc/apt/sources.list.d/anydesk-stable.list

    # --- Google Chrome repo ---
    wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list

    apt-get update

    # Resolve + download the whole closure in one shot for version consistency.
    apt-get install -y --download-only \
      $DESKTOP_PKGS $TOOL_PKGS $THIRDPARTY_PKGS

    # Belt-and-braces: explicitly fetch the top-level tool debs too, in case any
    # are part of the container base (and therefore skipped above) but absent
    # from the target server base.
    cd /out
    apt-get download $TOOL_PKGS 2>/dev/null || true

    # Gather every fetched .deb into /out.
    cp -n /var/cache/apt/archives/*.deb /out/ 2>/dev/null || true

    # Build a flat apt repo index.
    cd /out
    rm -f Packages Packages.gz
    dpkg-scanpackages --multiversion . /dev/null > Packages
    gzip -kf Packages

    # Stamp the pool so every station records which pool built it
    # (/etc/kela/build-info) and skew vs the ISO point release is traceable.
    {
      echo "pool_built_at=$(date -u +%FT%TZ)"
      . /etc/os-release && echo "pool_base=ubuntu-${VERSION}"
    } > /out/POOL_INFO

    echo "POOL_DEB_COUNT=$(ls -1 /out/*.deb | wc -l)"
    echo "POOL_SIZE=$(du -sh /out | cut -f1)"
    chmod -R a+rX /out
  '

DEB_COUNT="$(find "$OUT_DIR" -maxdepth 1 -name '*.deb' | wc -l | tr -d ' ')"
POOL_SIZE="$(du -sh "$OUT_DIR" | cut -f1)"

cat <<EOF

============================================================================
  Offline pool ready: $OUT_DIR
    packages: ${DEB_COUNT} .debs   size: ${POOL_SIZE}
    index:    Packages + Packages.gz

  Next: build the ISO (it auto-detects this pool):
    ./build-iso.sh --input <server.iso> --output ./kela-operator-24.04.iso --secrets ./secrets.env

  IMPORTANT: after building, validate by installing ONE laptop with ethernet
  UNPLUGGED. If a package turns out to be missing, add it to TOOL_PKGS in this
  script and re-run.
============================================================================
EOF
