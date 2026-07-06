#!/usr/bin/env bash
# ============================================================================
#  collect-offline-packages.sh — build the offline .deb pool for the Kela
#  operator ISO (option "3a": fully offline install, no WAN during install).
#
#  Runs a clean ubuntu:24.04 amd64 container and downloads the COMPLETE
#  recursive dependency closure (Depends + Pre-Depends) of the desktop + tools,
#  then adds the third-party debs (Tailscale, AnyDesk, Chrome) and builds a flat
#  apt repo index (Packages/Packages.gz). The result drops into ./offline-pool
#  and is embedded into the ISO by build-iso.sh.
#
#  Why the full closure (not just `apt-get install --download-only`):
#  --download-only fetches only what the container is MISSING and silently skips
#  packages already present in the ubuntu:24.04 image. Those are NOT guaranteed
#  to exist on the target Server base, so they go missing from the pool and the
#  offline desktop install dies with "gnome-shell ... not installable" (plus the
#  cascade behind it). We therefore enumerate the whole hard-dependency closure
#  and download every piece, then VALIDATE that the set resolves from the pool
#  ALONE (empty base) — a check independent of whatever the target base ships.
#  If the pool can't satisfy the install, the collect FAILS instead of shipping
#  a half-pool that strands a field unit.
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
TOOL_PKGS="openssh-server curl wget ca-certificates apt-transport-https gnupg lsb-release ufw rfkill libnss3-tools power-profiles-daemon dconf-cli onboard evtest xdotool"
THIRDPARTY_PKGS="tailscale anydesk google-chrome-stable"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker not found. Install Docker Desktop (macOS) or docker engine (Linux)." >&2
  exit 1
}

mkdir -p "$OUT_DIR"

# Start every refresh from a clean pool. The container re-downloads the FULL
# closure on each run (fresh ubuntu:24.04, empty apt cache), so keeping old
# artifacts only lets stale package versions pile up — dpkg-scanpackages
# --multiversion would index every one, growing the pool + ISO monotonically
# until it no longer fits an 8 GB stick. Scoped to the artifacts we create, so
# it's safe even if OUT_DIR is a custom path.
echo "==> Clearing previous pool artifacts in: $OUT_DIR"
rm -f  "$OUT_DIR"/*.deb "$OUT_DIR"/Packages "$OUT_DIR"/Packages.gz "$OUT_DIR"/POOL_INFO
rm -rf "$OUT_DIR"/repo-config

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

    ALL_PKGS="$DESKTOP_PKGS $TOOL_PKGS $THIRDPARTY_PKGS"

    # (1) Resolve + download the delta the container is missing. Recommends ON,
    #     so a normal desktop`s recommended extras are captured where available.
    #     firefox- (trailing-dash exclusion): it is a snap-transitional deb
    #     whose postinst runs `snap install firefox` — a snap-store phone-home
    #     that can NEVER succeed during an offline install (it wedged a bench
    #     unit at "status half-installed firefox"). user-data excludes + pins
    #     it on the target too; keeping it out of the pool is the third belt.
    apt-get install -y --download-only $ALL_PKGS firefox-

    # (2) Force-download the ENTIRE hard-dependency closure (Depends +
    #     Pre-Depends), INCLUDING packages already in this container image that
    #     step (1) silently skips. This is the fix for the offline
    #     "gnome-shell not installable" cascade: those skipped base deps are
    #     absent from the target Server base too, so the pool must carry them.
    #     Tiny seed covers the rare Pre-Depends on dpkg/apt themselves.
    SEED="dpkg apt"
    CLOSURE="$(apt-cache depends --recurse \
                 --no-recommends --no-suggests --no-conflicts \
                 --no-breaks --no-replaces --no-enhances \
                 $ALL_PKGS 2>/dev/null \
               | awk "/^[a-zA-Z0-9]/ {print \$1}" | sort -u || true)"
    # Keep only real, downloadable packages (drop virtual/Provides-only names,
    # which have no Filename and would abort a batch download).
    REAL="$(apt-cache show $CLOSURE $SEED 2>/dev/null \
            | awk "/^Package: / {p=\$2} /^Filename: / {print p}" | sort -u || true)"
    cd /out
    apt-get download $REAL 2>/dev/null \
      || for p in $REAL; do apt-get download "$p" 2>/dev/null || true; done

    # Gather every fetched .deb into /out.
    cp -n /var/cache/apt/archives/*.deb /out/ 2>/dev/null || true

    # Build a flat apt repo index.
    cd /out
    rm -f Packages Packages.gz
    dpkg-scanpackages --multiversion . /dev/null > Packages
    gzip -kf Packages

    # --- Completeness gate: resolve from the pool ALONE, empty base ----------
    # Point apt at the pool only AND tell it nothing is installed
    # (Dir::State::status=/dev/null). If the whole set resolves, an offline
    # install on the (larger) Server base is guaranteed to resolve too. This is
    # the check that would have caught the original truncated pool. Recommends
    # off: we only guarantee the hard-dependency closure.
    echo "==> Validating offline closure (pool-only, empty base)"
    printf "deb [trusted=yes] file:/out ./\n" > /etc/apt/pool-only.list
    apt-get -o Dir::Etc::SourceList=/etc/apt/pool-only.list \
            -o Dir::Etc::SourceParts=/dev/null update >/dev/null 2>&1 || true
    if ! apt-get install -s -y \
            -o Dir::Etc::SourceList=/etc/apt/pool-only.list \
            -o Dir::Etc::SourceParts=/dev/null \
            -o Dir::State::status=/dev/null \
            -o APT::Install-Recommends=false \
            $ALL_PKGS >/tmp/kela-sim.log 2>&1; then
      echo "ERROR: offline pool INCOMPLETE — the package set does not resolve" >&2
      echo "       from the pool alone. The build is aborting so this does not" >&2
      echo "       strand a field unit. Unsatisfied:" >&2
      grep -E "Depends:|not installable|but it is not|Unable to correct" /tmp/kela-sim.log \
        | sort -u | sed "s/^/         /" >&2
      exit 1
    fi
    echo "    OK: desktop + tools + third-party resolve from the pool alone."

    # Ship the vendor repo config (keyrings + .list files) alongside the debs.
    # Offline installs pull Tailscale/AnyDesk/Chrome from the pool but never
    # configure their upstream repos; the pool is then deleted after first boot,
    # so without this they are frozen at pool-collection versions forever (a
    # Tailscale client aging out of control-plane compat = stranded fleet).
    # setup.sh lays these down (see install_pool_repos) so the station can be
    # pointed back at the upstreams at central patch time. Keyring filenames are
    # kept identical to the paths the .list files reference via signed-by=.
    mkdir -p /out/repo-config/keyrings /out/repo-config/lists
    cp /usr/share/keyrings/tailscale-archive-keyring.gpg /out/repo-config/keyrings/
    cp /etc/apt/keyrings/keys.anydesk.com.asc            /out/repo-config/keyrings/
    cp /etc/apt/keyrings/google-chrome.gpg               /out/repo-config/keyrings/
    cp /etc/apt/sources.list.d/tailscale.list      /out/repo-config/lists/
    cp /etc/apt/sources.list.d/anydesk-stable.list /out/repo-config/lists/
    cp /etc/apt/sources.list.d/google-chrome.list  /out/repo-config/lists/

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
