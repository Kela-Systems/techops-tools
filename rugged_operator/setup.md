# Kela Operator Machine — Build Runbook

**Target:** Dell Latitude 5420 Rugged
**OS:** Ubuntu 24.04 LTS Desktop
**Role:** Operator workstation (Chrome → `https://kela.local/`, remote-managed via Tailscale/AnyDesk)

---

## 0. Pre-flight

| Item | Value |
|---|---|
| Hostname | `<site-name>-operator` (e.g. `fob-12-operator`) |
| Primary user | `kela` |
| Password | `Kelasys123!` |
| AnyDesk unattended password | `Kelasys123!` |
| Target URL | `https://kela.local/` → `192.168.88.10` |
| Server TLS cert | Fetched live from the LAN server (`kela.local` / `192.168.88.10`) via `openssl s_client`; re-runnable with `sudo kela-install-cert` (see §3) |
| Tailscale auth | Auth key + ACL tags supplied via env vars (see §3) |
| Egress policy | LAN-only, with Tailscale + AnyDesk carve-outs |
| WiFi | Available (BIOS + OS) |
| Bluetooth | Blocked (rfkill + module blacklist + BlueZ purged) |

You'll need: bootable Ubuntu 24.04 USB, wired ethernet during install (more reliable than WiFi for first-time apt), the `operator-setup.sh` script, a Tailscale **auth key** (reusable, tagged), and **LAN reachability to the hub** (`kela.local`) so the script can pull the server cert.

> **Recommended path: bake a turnkey install USB once and re-use it for every site.** See [`usb-builder/README.md`](./usb-builder/README.md). Sections 2-4 below are the manual fallback for when you don't want to use the USB image, or for re-running parts of the setup on an already-installed machine.

---

## 1. BIOS configuration (Dell 5420 Rugged)

Boot, hit `F2` for BIOS setup.

- **Boot Sequence** → UEFI only; USB key first.
- **System Configuration → SATA Operation** → `AHCI`.
- **Security → Secure Boot** → can stay enabled.
- **Security → TPM 2.0** → Enabled.
- **Wireless → Bluetooth** → **Disabled** (belt-and-braces; the OS also blocks it).
- **Wireless → WLAN** → **Enabled**.
- **Power Management → AC Behavior** → "Wake on AC" enabled if the site loses power often.
- Save and exit.

---

## 2. Ubuntu 24.04 install

Boot from the USB, "Try or Install Ubuntu".

- **Updates and other software** → Minimal installation; uncheck "Download updates while installing" (script handles it).
- **Installation type** → Erase disk and install Ubuntu. Use LUKS if site policy requires it.
- **Who are you?**
  - Name: `kela`
  - Computer's name: `<site-name>-operator`
  - Username: `kela`
  - Password: `Kelasys123!`
  - **Log in automatically** → check.
- Finish, reboot, pull the USB.
- First boot: log in as `kela`, plug in ethernet, decline online-accounts prompts.

---

## 3. Run the post-install script

Copy `operator-setup.sh` onto the machine. The server certificate is pulled live from the site server on the LAN (`kela.local` / `192.168.88.10`) via `openssl s_client` (mirrors `kela-station-setup` phase 077) — no need to ship a CA file. **If the station is built before the site server exists, the cert step is skipped (non-fatal)** and you install it later with `sudo kela-install-cert` (see the note in §3.7).

```bash
chmod +x operator-setup.sh

SITE_NAME=fob-12 \
TS_AUTHKEY=tskey-auth-XXXXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXX \
TS_TAGS=tag:operator,tag:fob-12 \
sudo -E bash ./operator-setup.sh
```

Notes:

- `TS_AUTHKEY` — Tailscale auth key. Generate a **reusable, ephemeral=false, pre-approved, tagged** key from the Tailscale admin UI. If omitted, Tailscale is installed but not connected — and because the cert fetch in step 7 resolves the hub through Tailscale MagicDNS, it will warn-and-skip as well. The script prints the manual `tailscale up` command at the end so you can finish out-of-band.
- `TS_TAGS` — comma-separated ACL tags. `tag:` prefixes are added automatically if you forget them (so `operator,fob-12` and `tag:operator,tag:fob-12` are equivalent). The auth key itself must already be authorized to apply these tags.
- `CERT_FETCH_HOST` / `CERT_FETCH_PORT` — optional overrides. By default the script extracts the cert from `192.168.88.10:443` (the site server on the LAN, a.k.a. `kela.local`). The fetched cert just needs `kela.local` in its SAN list, since that's what Chrome opens.

The script handles:

1. Hostname (`${SITE_NAME}-operator`, updates `/etc/hosts`)
   - **Timezone** `Asia/Jerusalem` + **NTP** from the site server `192.168.88.10` (`/etc/systemd/timesyncd.conf.d/kela-ntp.conf`), with `ntp.ubuntu.com` as fallback (123/udp is allowed out) so a drifted RTC recovers even when the hub is down
   - Waits up to 45s for a default route first — first boot can race DHCP
2. `apt update` + base tools (including `libnss3-tools` for `certutil`). If an offline pool is present at `/opt/kela-pool` (baked by the offline-install USB), all installs — base tools, Tailscale, AnyDesk, Chrome — come from there with **no network**; otherwise they come from the network as usual. `apt upgrade` is skipped by default (set `KELA_APT_UPGRADE=1` to force it on an online run); patch centrally over Tailscale instead.
3. **Tailscale** — installs, enables `tailscaled`, and if `TS_AUTHKEY` is set, runs `tailscale up --reset --authkey=… --hostname=<site>-operator --accept-routes [--advertise-tags=…]` and waits for an IPv4
4. **AnyDesk** (installs, sets unattended password to `Kelasys123!`)
5. **Google Chrome** + managed policy that disables telemetry, pins the homepage / new-tab / startup to `https://kela.local/`, and installs managed bookmarks (locked "Kela" folder on the bookmark bar): מערכת קלע → `https://kela.local/`, שינוי מיקום אתר → `https://kela.local/location-updater`, ממשק מצלמה → `http://192.168.88.210:6010`
6. `/etc/hosts` entry `192.168.88.10  kela.local`
7. **Server cert fetch** — installs the standalone, re-runnable `kela-install-cert` command and runs it once (via `openssl s_client`, mirrors phase 077):
   - extracts the cert presented on `192.168.88.10:443` (the LAN server, a.k.a. `kela.local`); override with `CERT_FETCH_HOST`
   - auto-detects CA vs leaf and picks NSS trust flags (`C,,` vs `P,,`)
   - drops it at `/usr/local/share/ca-certificates/kela/kela-server.crt` + `update-ca-certificates` (system trust → curl/wget/etc.)
   - `certutil -A` into `/home/kela/.pki/nssdb` under nickname `"Kela Server"` (Chrome's NSS store)
   - writes `/etc/opt/chrome/policies/managed/kela-ssl-policy.json` with `SSLErrorOverrideAllowedForOrigins` as a defence-in-depth fallback
   - retries 5× with 5s backoff; **if the server doesn't exist yet, the step is non-fatal** — once it's online run `sudo kela-install-cert` (defaults to `192.168.88.10`, or pass `sudo kela-install-cert <host> [port]`)
8. Chrome autostart `.desktop` → `https://kela.local/` maximized
9. **Operator session hardening**:
   - GDM auto-login as `kela`
   - `systemctl mask` of `sleep.target`, `suspend.target`, `hibernate.target`, `hybrid-sleep.target` — nothing on the system can trigger a sleep state
   - `/etc/systemd/logind.conf.d/kela-no-sleep.conf` — lid switch, power button, suspend key, hibernate key, and idle action all set to `ignore`
   - **System dconf db** (`/etc/dconf/db/local.d/00-kela-power`, with locks): `idle-delay 0` (**Screen Blank: Never**), screensaver/lock off, `sleep-inactive-{ac,battery}-type=nothing` + timeouts 0, `power-button-action=nothing`, idle-dim off. Replaces the old `sudo -u kela gsettings` calls, which silently failed to persist (why the screen still blanked).
   - `/usr/local/bin/kela-session-init` + autostart `.desktop` — on every login: **Power Mode → performance** (`powerprofilesctl`, since it resets to balanced each boot), reasserts screen-never-blank (`gsettings` + `xset -dpms`), default sink to 100% + unmuted output, and **mutes every input source (microphone off by default)**
10. **UFW**: deny in+out by default; allow LAN (RFC1918 + `100.64.0.0/10`), `tailscale0`, plus DNS / 443/tcp / 41641/udp / 3478/udp outbound for Tailscale control plane, **80/tcp** outbound (apt mirrors are plain http — without it fleet patching over Tailscale dies at the apt step; Chrome as `kela` stays LAN-only via the per-user egress chain) and **123/udp** (NTP fallback)
11. **Cleanup + Bluetooth block**:
    - purge GNOME games, LibreOffice, Thunderbird, Rhythmbox, Shotwell, Cheese, Remmina, Transmission, Déjà Dup
    - `rfkill block bluetooth` + persistent `rfkill-block-bluetooth.service`
    - `systemctl mask bluetooth.service`
    - `/etc/modprobe.d/blacklist-bluetooth.conf` (bluetooth, btusb, btbcm, btintel, btrtl, btmtk, hci_uart)
    - `update-initramfs -u`
    - `apt purge bluez bluez-cups bluez-obexd gnome-bluetooth*`
12. **`kela-verify` + build-info**:
    - installs `/usr/local/sbin/kela-verify` — the §4.3 checklist as code (remote access, firewall, kiosk wiring, sleep masking, bluetooth, NTP; PASS/FAIL per check, non-zero exit on any required failure). The USB first-boot runner refuses to finalize unless it passes; re-run any time with `sudo kela-verify`
    - writes `/etc/kela/build-info`: site, hostname, setup version, build date, Tailscale IP, **AnyDesk ID** (`anydesk --get-id` — no manual GUI step), offline-pool stamp
    - if `KELA_CHECKIN_URL` is set, POSTs build-info there (best-effort) so fleet inventory builds itself

---

## 4. Manual finishing steps

### 4.1 Tailscale auth (only if `TS_AUTHKEY` was not supplied)

If you ran the script without `TS_AUTHKEY`, finish auth now:

```bash
sudo tailscale up --hostname=<site-name>-operator --accept-routes \
                  --advertise-tags=tag:operator,tag:<site-name>
```

Open the link, authenticate via Google Workspace SSO. In the Tailscale admin UI: disable key expiry, confirm tags applied.

### 4.2 AnyDesk

The **AnyDesk-ID** is recorded in `/etc/kela/build-info` (and checked in via
`KELA_CHECKIN_URL` if configured) — no need to open the GUI. Verify the
unattended password is set:

```bash
sudo grep -i pwd_hash /etc/anydesk/system.conf   # non-empty hash
```

### 4.3 Reboot and verify

```bash
sudo reboot
```

**Quick check:** `sudo kela-verify` runs the machine-checkable subset of the
list below (and is what the USB first-boot flow already gated on). The full
manual list, for deep verification over Tailscale from a jumphost:

```bash
ssh kela@<site-name>-operator

# UFW
sudo ufw status verbose

# Bluetooth must be off, WiFi must work
rfkill list                          # bluetooth: Soft blocked: yes
lsmod | grep -iE 'blue|btusb'        # should be empty
nmcli radio wifi                     # enabled
nmcli dev                            # wlan0/wlp* visible

# Chrome trusts kela.local
sudo -u kela curl -sI https://kela.local/    # 200 OK, no -k needed
sudo -u kela certutil -L -d sql:/home/kela/.pki/nssdb | grep "Kela Server"

# Tailscale is up and tagged
tailscale status
tailscale ip -4

# Chrome process is up pointing at the right URL
pgrep -af chrome | grep kela.local

# Sleep is fully masked (all four should print "masked")
systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target

# logind ignores physical events
grep -E 'HandleLidSwitch|HandlePowerKey|IdleAction' /etc/systemd/logind.conf.d/kela-no-sleep.conf

# Default sink is unmuted at 100%, mic is muted (run as kela in the session)
sudo -u kela pactl get-sink-volume   @DEFAULT_SINK@     # → 100% / 100%
sudo -u kela pactl get-sink-mute     @DEFAULT_SINK@     # → Mute: no
sudo -u kela pactl get-source-mute   @DEFAULT_SOURCE@   # → Mute: yes

# Power Mode is performance, screen never blanks
powerprofilesctl get                                    # → performance
sudo -u kela gsettings get org.gnome.desktop.session idle-delay   # → uint32 0

# Timezone + NTP
timedatectl status                                      # Time zone: Asia/Jerusalem; NTP service: active
timedatectl show-timesync --property=ServerName --property=ServerAddress  # → 192.168.88.10

# Egress test — these should FAIL:
curl -m 5 https://example.com
# These should SUCCEED:
curl -m 5 https://login.tailscale.com
ping -c2 192.168.88.10
```

---

## 5. Notes / known gotchas

- **Installing the cert later / cert rotation**: if the station was built before the server existed (or the cert rotates), run `sudo kela-install-cert` once the server is up — it pulls from `192.168.88.10:443` by default (or `sudo kela-install-cert <host> [port]`). The command is idempotent (deletes the old `Kela Server` nickname before re-adding) and `openssl s_client` always grabs whatever the server is currently serving.
- **NSS vs system trust store**: Chrome on Linux ignores `/etc/ssl/certs` and uses NSS at `~/.pki/nssdb`. `kela-install-cert` installs the cert in *both* places so curl/wget *and* Chrome trust kela.local.
- **Chrome homepage + bookmarks**: pinned via the managed policy `/etc/opt/chrome/policies/managed/kela-policy.json` (`HomepageLocation`, `RestoreOnStartupURLs`, `ManagedBookmarks`). The bookmarks live in a locked "Kela" folder on the bookmark bar and can't be deleted by the operator. Edit that JSON (and re-launch Chrome) to change them.
- **Screen still blanks / power mode resets?** Screen-blank is enforced two ways: a locked system dconf db (`idle-delay 0`) *and* `kela-session-init` reasserting it (`gsettings` + `xset -dpms`) each login. Power Mode = performance is applied per-login via `powerprofilesctl` because `power-profiles-daemon` reverts to `balanced` on every boot. Don't rely on `sudo -u kela dbus-launch gsettings set ...` at build time — it frequently fails to persist, which was the original cause of the monitor still sleeping. To allow blanking/balanced again, remove the dconf locks + `00-kela-power`, run `dconf update`, and drop the relevant lines from `kela-session-init`.
- **Microphone muted by default**: `kela-session-init` mutes every input source on each login. It's reversible (the operator can unmute for a call), but resets to muted on the next login/reboot. To allow the mic permanently, remove the `set-source-mute` lines from `/usr/local/bin/kela-session-init`.
- **Cert trust flags**: the script auto-detects whether the hub is presenting a CA cert (`CA:TRUE` in basic constraints → NSS `C,,`) or a leaf cert (NSS `P,,`). Don't hand-edit the trust flags unless you know which one applies.
- **AnyDesk over Tailscale**: connect using the machine's Tailscale IP or MagicDNS name; UFW allows full inbound on `tailscale0`.
- **Chrome telemetry**: `/etc/opt/chrome/policies/managed/kela-policy.json` is what suppresses metrics accumulation. Don't delete it.
- **Bluetooth re-enable** (if ever needed): remove `/etc/modprobe.d/blacklist-bluetooth.conf`, `systemctl unmask bluetooth.service`, `apt install bluez`, `update-initramfs -u`, reboot. `rfkill unblock bluetooth` alone won't do it.
- **Re-enable sleep** (if you ever repurpose the box): `systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target`, delete `/etc/systemd/logind.conf.d/kela-no-sleep.conf`, `systemctl restart systemd-logind`, and revert the relevant `org.gnome.settings-daemon.plugins.power` gsettings.
- **Site-specific LAN**: the UFW config allows all RFC1918, not just `192.168.88.0/24` — keeps the image portable across sites. Tighten if a site requires stricter posture.
- **Egress test nuance**: the §4.3 `curl https://example.com` test must FAIL **as the kela user** (per-user egress chain). Root processes (apt, tailscaled, AnyDesk) are allowed out on 53/80/443/123/41641/3478 — that's deliberate, so central patching works.
- **MOK / Secure Boot**: irrelevant for this base build; only matters if you later install unsigned kernel modules (e.g. some Magos/SiriusDriver pieces).

---

## 6. USB-based unattended install

For new ruggeds, prefer the install USB built from [`usb-builder/`](./usb-builder/). It collapses §2-§4 into:

1. Power on the rugged with the USB inserted (F12 → USB).
2. **Type the site name at the GRUB menu** (e.g. `fob-12`) and walk away —
   install + first-boot setup run hands-off to completion (~20-30 min) and
   the machine reboots to Chrome on `https://kela.local/`. One visit total.
3. (Fallback: press Enter at GRUB without a name → the site name is asked
   at a first-boot prompt after the ~15 min install instead.)

The first-boot runner only finalizes (and only shreds the baked auth key)
after Tailscale is connected **and** `sudo kela-verify` passes — a failed
station keeps its retry wiring instead of stranding unreachable.

Build prerequisites, secrets format, and re-run / failure recovery are in [`usb-builder/README.md`](./usb-builder/README.md). Treat the resulting `.iso` (and the USB stick) as a secret — the Tailscale auth key is baked in until first-boot finishes, at which point `kela-first-boot-run` shreds it.

**Installing many laptops in parallel?** Build an **offline** ISO so no laptop touches the WAN during install — run `usb-builder/collect-offline-packages.sh` once (needs Docker), then `build-iso.sh` auto-embeds the pool. See the [Offline install](./usb-builder/README.md#offline-install-no-wan-during-install) section.