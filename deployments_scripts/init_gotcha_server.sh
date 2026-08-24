#!/usr/bin/env bash
# init_gotcha_server.sh - Initial provisioning for a Gotcha NRU-230S server
#
# Neousys NRU-230S: NVIDIA Jetson AGX Orin 32GB SOM (arm64) running JetPack /
# L4T. The OS sits on the 64GB eMMC, which is nowhere near big enough to hold
# container images, so k3s data is relocated onto the M.2 NVMe (or 2.5" SATA)
# SSD before k3s is ever installed.
#
# Steps: hostname -> unique machine-id -> kela user with passwordless sudo ->
# format + mount the SSD at /mnt/data -> point /var/lib/rancher at the SSD ->
# Tailscale -> optionally claim the site's fixed address.
#
# Usage (over SSH on the LAN, as root):
#   scp init_gotcha_server.sh kela@192.168.88.20:/tmp/
#   ssh -t kela@192.168.88.20 'sudo bash /tmp/init_gotcha_server.sh \
#     --site kela-gotcha-01 --data-disk /dev/nvme0n1 --ts-authkey tskey-auth-... \
#     --static-ip 192.168.88.10/24'
#
# Or piped, with the config as env vars (no TTY, so --yes is required to wipe):
#   ssh kela@192.168.88.20 'sudo -E SITE_NAME=kela-gotcha-01 \
#     DATA_DISK=/dev/nvme0n1 TS_AUTHKEY=tskey-auth-... ASSUME_YES=1 bash -s' \
#     < init_gotcha_server.sh
#
# Safe to re-run: every step is idempotent, and the SSD is only formatted when
# it has no KELADATA filesystem yet, so a second run never eats site data.

set -Eeuo pipefail
trap 'echo "ERROR: failed at line $LINENO: $BASH_COMMAND" >&2' ERR

# Stamped into /etc/kela/build-info for fleet audits. Bump on every change.
SETUP_VERSION="2026-08-24.4"

# ---------- config (flags override these env vars) --------------------------
SITE_NAME="${SITE_NAME:-}"
DATA_DISK="${DATA_DISK:-}"
TS_AUTHKEY="${TS_AUTHKEY:-}"
TS_TAGS="${TS_TAGS:-}"
KELA_USER="${KELA_USER:-kela}"
KELA_PASSWORD_HASH="${KELA_PASSWORD_HASH:-}"
KELA_SSH_PUBKEY="${KELA_SSH_PUBKEY:-}"
STATIC_IP="${STATIC_IP:-}"
KEEP_MACHINE_ID="${KEEP_MACHINE_ID:-0}"
ASSUME_YES="${ASSUME_YES:-0}"

DATA_MOUNT=/mnt/data
DATA_LABEL=KELADATA
RANCHER_DIR="${DATA_MOUNT}/rancher"
DATA_UUID=""
DATA_PART=""
WILL_FORMAT=0
STATIC_IP_CLAIMED=0
MACHINE_ID_CHANGED=0
TOTAL_STEPS=8

log()  { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
step() { echo; echo "==> [$1/${TOTAL_STEPS}] $2"; }
warn() { echo "WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage:
  sudo ./init_gotcha_server.sh --site <hostname> --data-disk <device> [options]

Required:
  --site <name>            Hostname for this box, used verbatim (e.g. kela-gotcha-01)
  --data-disk <device>     Whole SSD to format for /mnt/data (e.g. /dev/nvme0n1).
                           Refused if it holds /, /boot or /boot/firmware.

Options:
  --ts-authkey <key>       Tailscale auth key (tskey-auth-...). Omit to skip
                           `tailscale up` and bring the node up by hand later.
  --ts-tags <tags>         Tailscale ACL tags, comma-separated (e.g. tag:gotcha).
                           The auth key must be authorized for them.
  --ts-ssh                 Also enable Tailscale SSH on this node.
  --user <name>            Admin user to create (default: kela)
  --password-hash <hash>   crypt(3) hash for the user's password. Without it the
                           account has no password — SSH key or console only.
  --ssh-pubkey <key>       Public key to append to the user's authorized_keys.
  --keep-machine-id        Don't regenerate /etc/machine-id. By default it is
                           regenerated once, because every Jetson flashed from
                           the same image ships with an identical one, which
                           collides DHCP identities and journals across a fleet.
  --static-ip <cidr>       Additional fixed address to claim, e.g.
                           192.168.88.10/24 — the address the rest of the fleet
                           expects the site server on. Added *alongside* DHCP,
                           so the lease keeps providing the default route and
                           DNS, and the interface is never reactivated.
  --yes                    Don't prompt before wiping the data disk. Required
                           when running without a TTY (piped over SSH).
  -h, --help               Show this help

Every flag has an env-var equivalent (SITE_NAME, DATA_DISK, TS_AUTHKEY, TS_TAGS,
KELA_USER, KELA_PASSWORD_HASH, KELA_SSH_PUBKEY, KEEP_MACHINE_ID, STATIC_IP,
ASSUME_YES)
for use with `sudo -E ... bash -s`.
USAGE
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --site)          SITE_NAME="$2"; shift 2 ;;
      --data-disk)     DATA_DISK="$2"; shift 2 ;;
      --ts-authkey)    TS_AUTHKEY="$2"; shift 2 ;;
      --ts-tags)       TS_TAGS="$2"; shift 2 ;;
      --user)          KELA_USER="$2"; shift 2 ;;
      --password-hash) KELA_PASSWORD_HASH="$2"; shift 2 ;;
      --ssh-pubkey)    KELA_SSH_PUBKEY="$2"; shift 2 ;;
      --static-ip)     STATIC_IP="$2"; shift 2 ;;
      --keep-machine-id) KEEP_MACHINE_ID=1; shift ;;
      --yes|-y)        ASSUME_YES=1; shift ;;
      -h|--help)       usage ;;
      *)               die "Unknown option: $1" ;;
    esac
  done
}

# Resolve the whole-disk device backing a partition: /dev/nvme0n1p1 -> /dev/nvme0n1
disk_of() {
  local src="$1" parent
  parent="$(lsblk -nro pkname "$src" 2>/dev/null | grep -v '^$' | head -1 || true)"
  [[ -n "$parent" ]] && echo "/dev/${parent}" || echo "$src"
}

# The one thing this script must never get wrong.
guard_not_system_disk() {
  local target mp source
  target="$(basename "$DATA_DISK")"
  for mp in / /boot /boot/efi /boot/firmware; do
    source="$(findmnt -no SOURCE --target "$mp" 2>/dev/null || true)"
    [[ -n "$source" ]] || continue
    if [[ "$(basename "$(disk_of "$source")")" == "$target" ]]; then
      die "$DATA_DISK backs $mp — refusing to format the system disk"
    fi
  done
}

validate() {
  [[ $EUID -eq 0 ]] || die "run as root (sudo bash $0 ...)"

  [[ -n "$SITE_NAME" ]] || die "--site is required (e.g. --site kela-gotcha-01)"
  [[ "$SITE_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || die "--site '$SITE_NAME' is not a valid DNS label (lowercase, digits, hyphens)"

  [[ -n "$DATA_DISK" ]] || die "--data-disk is required (e.g. --data-disk /dev/nvme0n1)"
  [[ -b "$DATA_DISK" ]] || die "$DATA_DISK is not a block device"
  # Reject partitions and LVM/md/crypt members: this script owns the whole disk.
  local disk_type
  disk_type="$(lsblk -ndro TYPE "$DATA_DISK")"
  case "$disk_type" in
    disk|loop) ;;
    *) die "$DATA_DISK is a '$disk_type', not a whole disk — pass e.g. /dev/nvme0n1" ;;
  esac

  if [[ -n "$TS_AUTHKEY" && "$TS_AUTHKEY" != tskey-* ]]; then
    die "TS_AUTHKEY does not look like a Tailscale auth key"
  fi

  if [[ -n "$STATIC_IP" && ! "$STATIC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
    die "--static-ip '$STATIC_IP' must be address/prefix, e.g. 192.168.88.10/24"
  fi

  local cmd
  for cmd in hostnamectl useradd usermod visudo sfdisk mkfs.ext4 blkid lsblk findmnt systemctl \
             systemd-machine-id-setup; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is required but missing"
  done

  guard_not_system_disk
}

# ---------- 1. hostname -----------------------------------------------------
set_hostname() {
  step 1 "Setting hostname to $SITE_NAME"
  hostnamectl set-hostname "$SITE_NAME"

  # sed exits 0 even when nothing matched, so decide with grep first. Rewrite by
  # truncating rather than `sed -i`, which swaps the inode and so fails whenever
  # /etc/hosts is a bind mount.
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    local rewritten
    rewritten="$(sed "s/^127\.0\.1\.1.*/127.0.1.1\t${SITE_NAME}/" /etc/hosts)"
    printf '%s\n' "$rewritten" > /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$SITE_NAME" >> /etc/hosts
  fi
  log "hostname set; /etc/hosts maps 127.0.1.1 -> $SITE_NAME"
}

# ---------- 2. machine-id ---------------------------------------------------
# Every Jetson flashed from the same JetPack image carries an identical
# /etc/machine-id. systemd derives the DHCP DUID/client-id from it, so two boxes
# on one LAN present the same DHCP identity and fight over leases — which would
# undermine the reserved address this script relies on — and their journals
# collide. Regenerated once and then left alone: churning it on every re-run
# would keep resetting the box's DHCP identity and journal lineage.
regenerate_machine_id() {
  step 2 "Giving the box a unique machine-id"

  local stamp=/var/lib/kela/.machine-id-regenerated

  if [[ "$KEEP_MACHINE_ID" == 1 ]]; then
    log "--keep-machine-id given — leaving $(cat /etc/machine-id 2>/dev/null) in place"
    return 0
  fi
  if [[ -e "$stamp" ]]; then
    log "already regenerated ($(cat /etc/machine-id 2>/dev/null)) — leaving it alone"
    return 0
  fi

  local before after
  before="$(cat /etc/machine-id 2>/dev/null || true)"

  # Both files have to go. systemd-machine-id-setup prefers to seed the new ID
  # from /var/lib/dbus/machine-id, which on JetPack is a real file holding the
  # same factory ID — so removing only /etc/machine-id hands that ID straight
  # back ("Initializing machine ID from D-Bus machine ID") and nothing changes.
  rm -f /etc/machine-id /var/lib/dbus/machine-id
  systemd-machine-id-setup >/dev/null

  # Restore dbus's copy as a symlink before any check can bail out, so it tracks
  # /etc/machine-id from here on and is never left missing. The usual snippets
  # delete it and never put it back, because systemd-machine-id-setup only
  # writes /etc/machine-id.
  if [[ -d /var/lib/dbus ]]; then
    ln -sfn /etc/machine-id /var/lib/dbus/machine-id
  fi

  after="$(cat /etc/machine-id 2>/dev/null || true)"
  [[ "$after" =~ ^[0-9a-f]{32}$ ]] || die "machine-id is not a valid ID: '${after}'"
  # A valid-looking ID isn't enough: it has to actually differ, or the box is
  # still carrying the fleet-wide duplicate this step exists to remove.
  if [[ "$after" == "$before" ]]; then
    die "machine-id came back unchanged ($after) — something is still seeding it; check for a stale /var/lib/dbus/machine-id"
  fi

  MACHINE_ID_CHANGED=1
  install -d -m 0755 /var/lib/kela
  printf '%s regenerated %s -> %s\n' "$(date -Iseconds)" "${before:-none}" "$after" > "$stamp"
  log "machine-id ${before:-none} -> $after (DHCP identity changes on next boot)"
}

# ---------- 3. admin user ---------------------------------------------------
create_admin_user() {
  step 3 "Creating $KELA_USER with passwordless sudo"

  if id "$KELA_USER" &>/dev/null; then
    log "$KELA_USER already exists — leaving the account alone"
  else
    useradd --create-home --shell /bin/bash --comment 'Kela' "$KELA_USER"
    log "created $KELA_USER"
  fi
  usermod -aG sudo "$KELA_USER"

  if [[ -n "$KELA_PASSWORD_HASH" ]]; then
    usermod -p "$KELA_PASSWORD_HASH" "$KELA_USER"
    log "password hash applied"
  fi

  # Validate the drop-in before installing it: a malformed file under
  # /etc/sudoers.d locks sudo out of the whole box, and this is a field server.
  local drop_in tmp
  drop_in="/etc/sudoers.d/90-${KELA_USER}-nopasswd"
  tmp="$(mktemp)"
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$KELA_USER" > "$tmp"
  if ! visudo -cf "$tmp" >/dev/null; then
    rm -f "$tmp"
    die "generated sudoers drop-in failed validation — not installing it"
  fi
  install -m 0440 -o root -g root "$tmp" "$drop_in"
  rm -f "$tmp"
  log "installed $drop_in"

  if [[ -n "$KELA_SSH_PUBKEY" ]]; then
    local home
    home="$(getent passwd "$KELA_USER" | cut -d: -f6)"
    install -d -m 0700 -o "$KELA_USER" -g "$KELA_USER" "${home}/.ssh"
    touch "${home}/.ssh/authorized_keys"
    grep -qxF "$KELA_SSH_PUBKEY" "${home}/.ssh/authorized_keys" \
      || echo "$KELA_SSH_PUBKEY" >> "${home}/.ssh/authorized_keys"
    chown "${KELA_USER}:${KELA_USER}" "${home}/.ssh/authorized_keys"
    chmod 0600 "${home}/.ssh/authorized_keys"
    log "authorized_keys updated"
  fi
}

confirm_wipe() {
  if [[ "$ASSUME_YES" == 1 ]]; then
    log "--yes given — wiping $DATA_DISK without prompting"
    return 0
  fi
  echo
  echo "About to ERASE $DATA_DISK:"
  lsblk -o NAME,SIZE,MODEL,TRAN,FSTYPE,LABEL,MOUNTPOINT "$DATA_DISK" || true
  echo
  [[ -t 0 ]] || die "no TTY to confirm on — re-run with --yes, or use 'ssh -t'"
  printf 'Type the disk path (%s) to confirm: ' "$DATA_DISK"
  local answer
  read -r answer
  [[ "$answer" == "$DATA_DISK" ]] || die "aborted"
}

# The kernel/udev can take a moment to publish the new partition node, so poll
# rather than assuming it exists the instant sfdisk returns.
first_partition() {
  local disk="$1" name
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    name="$(lsblk -nro NAME,TYPE "$disk" | awk '$2=="part"{print $1; exit}')"
    if [[ -n "$name" && -b "/dev/${name}" ]]; then
      echo "/dev/${name}"
      return 0
    fi
    sleep 1
  done
  die "no partition node appeared on $disk after partitioning"
}

# Work out whether the SSD needs formatting *before* touching anything, so the
# one destructive question gets asked up front and the rest runs unattended.
plan_data_disk() {
  local existing owner present
  existing="$(blkid -L "$DATA_LABEL" 2>/dev/null || true)"

  if [[ -n "$existing" ]]; then
    owner="$(disk_of "$existing")"
    if [[ "$owner" != "$DATA_DISK" ]]; then
      die "a $DATA_LABEL filesystem already exists on $existing (disk $owner), not on $DATA_DISK — sort that out by hand before re-running"
    fi
    DATA_PART="$existing"
    WILL_FORMAT=0
    return 0
  fi

  WILL_FORMAT=1
  present="$(lsblk -nro FSTYPE "$DATA_DISK" 2>/dev/null | grep -v '^$' | paste -sd, - || true)"
  if [[ -n "$present" ]]; then
    warn "$DATA_DISK already carries filesystems: $present"
  fi
}

# ---------- 4. data SSD -----------------------------------------------------
prepare_data_disk() {
  step 4 "Preparing $DATA_DISK as $DATA_MOUNT"
  install -d -m 0755 "$DATA_MOUNT"

  if [[ "$WILL_FORMAT" == 0 ]]; then
    # Already provisioned. Re-running must not cost the site its data, so only
    # the mount and fstab entry get refreshed below.
    log "found an existing $DATA_LABEL filesystem at $DATA_PART — not reformatting"
  else
    log "partitioning $DATA_DISK — GPT, one partition spanning the disk"
    printf ',\n' | sfdisk --label gpt --wipe always --wipe-partitions always "$DATA_DISK" >/dev/null
    partprobe "$DATA_DISK" 2>/dev/null || true
    udevadm settle 2>/dev/null || true

    DATA_PART="$(first_partition "$DATA_DISK")"
    # -m 1: ext4 reserves 5% for root by default, which is a lot of a large
    # SSD to hand back for a volume that only ever holds k3s data.
    log "formatting $DATA_PART as ext4, label $DATA_LABEL"
    mkfs.ext4 -F -m 1 -L "$DATA_LABEL" "$DATA_PART" >/dev/null
  fi

  DATA_UUID="$(blkid -s UUID -o value "$DATA_PART")"
  [[ -n "$DATA_UUID" ]] || die "could not read the UUID of $DATA_PART"

  # By UUID rather than LABEL, so a spare KELADATA disk plugged in later can't
  # shadow this one. nofail keeps the box bootable if the SSD ever drops out.
  sed -i -E "\|^[^#]*[[:space:]]${DATA_MOUNT}[[:space:]]|d" /etc/fstab
  printf 'UUID=%s\t%s\text4\tdefaults,noatime,nofail,x-systemd.device-timeout=15s\t0\t2\n' \
    "$DATA_UUID" "$DATA_MOUNT" >> /etc/fstab
  systemctl daemon-reload

  mountpoint -q "$DATA_MOUNT" || mount "$DATA_MOUNT"
  mountpoint -q "$DATA_MOUNT" || die "$DATA_MOUNT did not mount"
  log "$DATA_PART mounted at $DATA_MOUNT (UUID=$DATA_UUID)"
}

# ---------- 5. k3s data on the SSD -----------------------------------------
relocate_rancher() {
  step 5 "Pointing /var/lib/rancher at $RANCHER_DIR"

  # Settle the already-correct case before the k3s guard below. There is no data
  # to move and no path to swap, so a box that was provisioned earlier and has
  # since had k3s installed must not be blocked on a step with nothing to do.
  local current=""
  if [[ -L /var/lib/rancher ]]; then
    current="$(readlink -f /var/lib/rancher)"
    if [[ "$current" == "$RANCHER_DIR" ]]; then
      install -d -m 0755 "$RANCHER_DIR"
      log "/var/lib/rancher already points at $RANCHER_DIR"
      return 0
    fi
  fi

  # Everything past here either moves data or repoints a path k3s has open.
  local svc
  for svc in k3s k3s-agent; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
      die "$svc is running — 'systemctl stop $svc' first, then re-run"
    fi
  done

  install -d -m 0755 "$RANCHER_DIR"

  if [[ -L /var/lib/rancher ]]; then
    log "repointing /var/lib/rancher (was $current)"
  elif [[ -d /var/lib/rancher ]]; then
    if [[ -n "$(ls -A /var/lib/rancher)" ]]; then
      # An earlier k3s install put real data on the eMMC. Move it onto the SSD
      # rather than hiding it behind the symlink, where it would waste eMMC.
      local backup
      backup="/var/lib/rancher.bak-$(date '+%Y%m%d%H%M%S')"
      log "copying existing /var/lib/rancher onto the SSD"
      cp -a /var/lib/rancher/. "${RANCHER_DIR}/"
      mv /var/lib/rancher "$backup"
      warn "previous /var/lib/rancher kept at $backup — remove it once k3s is healthy"
    else
      rmdir /var/lib/rancher
    fi
  fi

  ln -sfn "$RANCHER_DIR" /var/lib/rancher
  log "/var/lib/rancher -> $RANCHER_DIR (k3s will land on the SSD when installed)"
}

# ---------- 6. Tailscale ----------------------------------------------------
install_tailscale() {
  step 6 "Installing Tailscale"

  if ! command -v curl >/dev/null 2>&1; then
    log "installing curl"
    apt-get update -qq && apt-get install -y -qq curl
  fi

  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL --retry 5 --retry-delay 15 --retry-all-errors https://tailscale.com/install.sh | sh
  fi
  systemctl enable --now tailscaled

  # Wait for tailscaled to come up before calling `tailscale up`.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    systemctl is-active --quiet tailscaled && break
    sleep 1
  done

  if [[ -z "$TS_AUTHKEY" ]]; then
    log "no auth key given — leaving 'tailscale up' as a manual step"
    return 0
  fi

  local args=( --reset
               --authkey="$TS_AUTHKEY"
               --hostname="$SITE_NAME"
               --timeout=120s
               --ssh
               --accept-routes )

  if [[ -n "$TS_TAGS" ]]; then
    local tags
    tags="$(echo "$TS_TAGS" \
      | sed 's/tag://g' \
      | tr ',' '\n' \
      | sed '/^[[:space:]]*$/d; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^/tag:/' \
      | paste -sd ',' -)"
    args+=( --advertise-tags="$tags" )
    log "tags: $tags"
  fi

  log "bringing Tailscale up as $SITE_NAME"
  if ! tailscale up "${args[@]}"; then
    warn "'tailscale up' failed — check the auth key and its tag authorization, then re-run by hand"
    return 0
  fi
  log "tailscale IPv4: $(tailscale ip -4 2>/dev/null | head -1 || echo 'not assigned yet')"
}

# ---------- 7. site address -------------------------------------------------
# The fleet addresses this box by IP, not by name: operator stations map
# kela.local -> 192.168.88.10 and pull their TLS cert from it, and the cameras,
# radars and APUs all use it as their NTP server. So the box should answer on
# that address even if the router's DHCP reservation is ever lost.
#
# It goes on as an *additional* address on the existing DHCP profile: the lease
# still supplies the default route and DNS, no gateway is set on the static, and
# the connection is deliberately never reactivated — so the SSH session this is
# running over, and the internet access the rest of the install needs, both stay
# up. On any other subnet the extra address is simply inert.
claim_static_ip() {
  step 7 "Claiming the site address"

  if [[ -z "$STATIC_IP" ]]; then
    log "no --static-ip given — leaving addressing to DHCP alone"
    return 0
  fi

  local addr iface con
  addr="${STATIC_IP%%/*}"
  iface="$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)"
  if [[ -z "$iface" ]]; then
    warn "no default route, so no interface to address — skipping $STATIC_IP"
    return 0
  fi

  if ip -4 addr show dev "$iface" | grep -qw "$addr"; then
    log "$addr is already on $iface"
    STATIC_IP_CLAIMED=1
  elif ping -c1 -W1 "$addr" >/dev/null 2>&1; then
    # Someone else already answers there. Claiming it would start an address
    # fight and could knock the real server off the LAN.
    warn "$addr already answers on this LAN and it is not us — skipping to avoid an IP conflict"
    return 0
  elif ip addr add "$STATIC_IP" dev "$iface" 2>/dev/null; then
    log "added $STATIC_IP to $iface, live"
    STATIC_IP_CLAIMED=1
  else
    warn "could not add $STATIC_IP to $iface"
    return 0
  fi

  # Persist it on the NetworkManager profile so it comes back after a reboot.
  # `nmcli connection modify` only rewrites the stored profile; without a
  # matching `connection up` nothing is torn down now.
  if ! command -v nmcli >/dev/null 2>&1; then
    warn "nmcli not found — $STATIC_IP is live but will not survive a reboot"
    return 0
  fi
  con="$(nmcli -t -g GENERAL.CONNECTION device show "$iface" 2>/dev/null || true)"
  if [[ -z "$con" ]]; then
    warn "no NetworkManager profile for $iface — $STATIC_IP is live but will not survive a reboot"
    return 0
  fi
  # nmcli renders multiple addresses as a comma-separated list, sometimes with
  # spaces after the commas, so trim before comparing.
  if nmcli -t -g ipv4.addresses connection show "$con" 2>/dev/null \
       | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -qx "$STATIC_IP"; then
    log "profile '$con' already carries $STATIC_IP"
  else
    nmcli connection modify "$con" +ipv4.addresses "$STATIC_IP"
    log "profile '$con' now carries $STATIC_IP alongside its DHCP lease"
  fi
}

# ---------- 8. record + verify ---------------------------------------------
PASS=0; FAIL=0; WARNED=0
req() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $d"; PASS=$((PASS+1)); else echo "FAIL  $d"; FAIL=$((FAIL+1)); fi; }
opt() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $d"; PASS=$((PASS+1)); else echo "WARN  $d"; WARNED=$((WARNED+1)); fi; }

record_and_verify() {
  step 8 "Recording build info and verifying"

  local ts_ip
  ts_ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"

  install -d -m 0755 /etc/kela
  cat > /etc/kela/build-info <<EOF
site=${SITE_NAME}
hostname=${SITE_NAME}
role=gotcha
model=NRU-230S
setup_version=${SETUP_VERSION}
built_at=$(date -Iseconds)
machine_id=$(cat /etc/machine-id 2>/dev/null || true)
admin_user=${KELA_USER}
data_disk=${DATA_DISK}
data_uuid=${DATA_UUID}
data_mount=${DATA_MOUNT}
rancher_dir=${RANCHER_DIR}
static_ip=${STATIC_IP}
tailscale_ip=${ts_ip}
EOF
  chmod 0644 /etc/kela/build-info
  echo

  req "hostname is $SITE_NAME"              test "$(hostname)" = "$SITE_NAME"
  req "machine-id is a valid uuid"          grep -qE '^[0-9a-f]{32}$' /etc/machine-id
  req "user $KELA_USER exists"              id "$KELA_USER"
  req "$KELA_USER has passwordless sudo"    sudo -u "$KELA_USER" sudo -n true
  req "$DATA_MOUNT is mounted"              mountpoint -q "$DATA_MOUNT"
  req "$DATA_MOUNT survives reboot"         grep -q "UUID=${DATA_UUID}" /etc/fstab
  req "$DATA_MOUNT is writable"             touch "${DATA_MOUNT}/.write-test"
  req "/var/lib/rancher -> $RANCHER_DIR"    test "$(readlink -f /var/lib/rancher)" = "$RANCHER_DIR"
  req "tailscaled is running"               systemctl is-active --quiet tailscaled
  opt "tailscale is connected"              test -n "$ts_ip"
  rm -f "${DATA_MOUNT}/.write-test"

  if [[ -n "$STATIC_IP" ]]; then
    # `ip` exits 0 on empty output, so both of these have to go through grep.
    # A deliberate skip (address already taken, no NM profile) has already been
    # warned about loudly, so it degrades to a warning rather than failing the
    # run. Losing the default route never does — that means we broke the box.
    if [[ "$STATIC_IP_CLAIMED" == 1 ]]; then
      req "${STATIC_IP%%/*} is live"  bash -c "ip -4 -o addr show | grep -qw '${STATIC_IP%%/*}'"
    else
      opt "${STATIC_IP%%/*} is live"  bash -c "ip -4 -o addr show | grep -qw '${STATIC_IP%%/*}'"
    fi
    req "default route still present" bash -c "ip -4 route show default | grep -q ."
  fi

  echo
  echo "${PASS} pass, ${FAIL} fail, ${WARNED} warn"
}

main() {
  parse_args "$@"
  validate

  echo "==> Initializing $SITE_NAME (NRU-230S, setup version ${SETUP_VERSION})"

  # Ask about the wipe before anything has changed, so an abort here is a no-op
  # and an approved run needs no further attention.
  plan_data_disk
  if [[ "$WILL_FORMAT" == 1 ]]; then
    confirm_wipe
  fi

  set_hostname
  regenerate_machine_id
  create_admin_user
  prepare_data_disk
  relocate_rancher
  install_tailscale
  # Last of the mutating steps: everything above needs working internet, and
  # this is the only one that touches the live network configuration.
  claim_static_ip
  record_and_verify
  [[ "$FAIL" -eq 0 ]] || die "$FAIL check(s) failed — see the list above"

  cat <<EOF

============================================================================
  $SITE_NAME initialized.

  Data SSD:      $DATA_DISK -> $DATA_MOUNT (UUID=$DATA_UUID)
  k3s data:      /var/lib/rancher -> $RANCHER_DIR
  Addresses:     $(ip -4 -o addr show scope global | awk '{print $4}' | paste -sd ' ' -)
  Station info:  cat /etc/kela/build-info

  Next: install k3s. It picks up $RANCHER_DIR through the symlink, so no
  --data-dir flag is needed.
============================================================================
EOF

  if [[ "$MACHINE_ID_CHANGED" == 1 ]]; then
    cat <<'EOF'
  Reboot before putting this box on a LAN with other Jetsons: the new
  machine-id only becomes its DHCP identity on the next boot.

EOF
  fi
}

main "$@"
