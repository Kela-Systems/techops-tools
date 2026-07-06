# Kela Operator Machine — Build Runbook

**Target:** Panasonic Toughbook CF-33 (2-in-1 rugged tablet)
**OS:** Ubuntu 24.04 LTS Desktop (GNOME on **Xorg**)
**Role:** Locked **kiosk** operator terminal (Chrome `--kiosk` → `https://kela.local/`, remote-managed via Tailscale/AnyDesk)

---

## Kiosk-Mode Rugged Operator (for management)

The laptop boots straight into a locked, full-screen browser on the Kela
system — no address bar, tab strip, or bookmarks — with three fixed views
(hub / location-updater / camera) that operators switch between using
`Ctrl+Tab` or `Ctrl+1/2/3`, so they see only the intended apps and can't
navigate elsewhere. It
self-heals (relaunches if closed; a reboot always returns to kiosk), while a
hidden gesture (triple-tap **F2**, or triple-press the **A1** button in tablet
mode) lets a technician drop to the normal desktop and re-enter kiosk from a
single icon. The machine stays locked to internal systems only (no general
internet), remotely managed via Tailscale + AnyDesk, never sleeps and never
auto-shuts-down (it runs until the battery is depleted), with Bluetooth
disabled and the microphone muted by default. Net effect: a single-purpose,
tamper-resistant terminal that's simple for operators and fully recoverable
for support.

---



## 0. Pre-flight


| Item                        | Value                                                                                                                                                                                                                                                                              |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hostname                    | `<site-name>-operator` (e.g. `fob-12-operator`)                                                                                                                                                                                                                                    |
| Primary user                | `kela`                                                                                                                                                                                                                                                                             |
| Password                    | `Kelasys123!`                                                                                                                                                                                                                                                                      |
| AnyDesk unattended password | `Kelasys123!`                                                                                                                                                                                                                                                                      |
| Target URL                  | `https://kela.local/` → `192.168.88.10`                                                                                                                                                                                                                                            |
| Server TLS cert             | Fetched live from the LAN server (`kela.local` / `192.168.88.10`) via `openssl s_client`; **auto-converges** via the `kela-cert-ensure` 5-min timer (installs once the server appears, re-pins on cert rotation) and is re-runnable by hand with `sudo kela-install-cert` (see §3) |
| Tailscale auth              | Auth key + ACL tags supplied via env vars (see §3)                                                                                                                                                                                                                                 |
| Egress policy               | LAN-only, with Tailscale + AnyDesk carve-outs                                                                                                                                                                                                                                      |
| WiFi                        | Available (BIOS + OS)                                                                                                                                                                                                                                                              |
| Bluetooth                   | Blocked (rfkill + module blacklist + BlueZ purged)                                                                                                                                                                                                                                 |


You'll need: bootable Ubuntu 24.04 USB, wired ethernet during install (more reliable than WiFi for first-time apt), the `operator-setup.sh` script, a Tailscale **auth key** (reusable, tagged), and **LAN reachability to the hub** (`kela.local`) so the script can pull the server cert.

> **Recommended path: bake a turnkey install USB once and re-use it for every site.** See `[usb-builder/README.md](./usb-builder/README.md)`. Sections 2-4 below are the manual fallback for when you don't want to use the USB image, or for re-running parts of the setup on an already-installed machine.

---



## 1. BIOS configuration (Panasonic Toughbook CF-33)

Power on and **hold** `F2` at the Panasonic logo to enter the Setup Utility.
(The CF-33 has no Dell-style one-time F12 boot menu — boot device selection
lives inside Setup.) Menu labels vary slightly by firmware revision; confirm
on the actual unit.

- **Boot** → UEFI boot mode; internal SSD first. (During imaging only, move
USB ahead — see the ordering note below.)
- **Security → Secure Boot** → **Enabled** (stays on; no MOK needed — the
kiosk build installs no unsigned kernel modules).
- **Security → Security Chip (TPM)** → **Enabled**.
- **Advanced → Wireless (or Device Security)**:
  - **WLAN** → **Enabled**.
  - **Bluetooth** → **Disabled** (belt-and-braces; the OS also blocks it).
  - **WWAN / LTE** and **GPS** → **Disabled** if unused — a cellular modem is
  a second WAN uplink you don't want on a LAN-only kiosk (the per-user
  egress lock blocks non-LAN traffic regardless, so this is defence-in-depth).
- **Main → "Power On AC"** (label varies) → **Enabled**, so after a site power blip the unit powers itself back on →
auto-login → kiosk, with nobody pressing the button.
- **Security → Set Supervisor Password**, then **disable USB / removable /
network(PXE) boot** and gate the boot popup — this is what stops someone
bypassing the whole software lockdown by booting external media.

> **Ordering (important):** image the unit **while USB boot is still enabled
> and no Supervisor Password is set**, then apply the Supervisor Password +
> disable-USB-boot lockdown **after** a successful install. Otherwise you lock
> yourself out before you can image. Record the Supervisor Password centrally.

- Save and exit.

---



## 2. Ubuntu 24.04 install

### 2.0 Boot from the USB stick

The CF-33 has **no one-time boot menu** — boot device order lives inside the
Setup Utility. With the baked USB inserted (use a port on the tablet body, not
a hub), power on holding **`F2`** and open the **Boot** tab.

- **If the stick isn't listed** (you only see Windows Boot Manager / Recovery /
  PXE entries): open **`UEFI Boot Device Control`** on the Boot screen and make
  sure **USB / removable boot is allowed** (not "LAN only" / "internal only").
  If it still doesn't appear, temporarily set **Security → Secure Boot →
  Disabled** to test, try a different direct USB port, and **Save & Exit
  (`F10`)** then re-enter `F2` Setup so the firmware re-scans removable media.
- Once it shows up, the stick appears as **`UEFI: USB, Partition 2`** (Partition
  2 is the EFI boot partition — pick that one, not Partition 1).
- Set **Boot Option #1 → `UEFI: USB, Partition 2`** (or use a **Boot Override**
  on the Exit tab for a one-time boot), then **Save & Exit (`F10`)**.

The unit reboots into GRUB → choose **"Try or Install Ubuntu"**. After a
successful install, return to `F2` Setup and restore the lockdown (internal SSD
first, Supervisor Password, disable USB/removable/PXE boot) per the ordering
note in §1.

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

Copy `operator-setup.sh` onto the machine. The server certificate is pulled live from the site server on the LAN (`kela.local` / `192.168.88.10`) via `openssl s_client` (mirrors `kela-station-setup` phase 077) — no need to ship a CA file. **If the station is built before the site server exists, the cert step is skipped (non-fatal)** and the `kela-cert-ensure` timer installs it automatically within ~5 min of the server coming online (see §3.7b) — running `sudo kela-install-cert` by hand is only needed if you don't want to wait.

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
2. `apt update` + base tools (including `libnss3-tools` for `certutil`). All installs come from the network; if `APT_PROXY` is set (secrets.env or env), apt routes through the bench cache — non-persistent, so field units never carry a dead proxy. `apt upgrade` is skipped by default (set `KELA_APT_UPGRADE=1` to force it); patch centrally over Tailscale instead.
3. **Tailscale** — installs, enables `tailscaled`, and if `TS_AUTHKEY` is set, runs `tailscale up --reset --authkey=… --hostname=<site>-operator --accept-routes [--advertise-tags=…]` and waits for an IPv4
4. **AnyDesk** (installs, sets unattended password to `Kelasys123!`)
5. **Google Chrome** + managed policy that disables telemetry, pins the homepage / new-tab / startup to `https://kela.local/`, hardens the browser (DevTools off, incognito off, printing off, downloads blocked), and installs managed bookmarks (locked "Kela" folder): מערכת קלע → `https://kela.local/`, שינוי מיקום אתר → `https://kela.local/location-updater`, ממשק מצלמה → `http://192.168.88.210:6010`. `RestoreOnStartupURLs` is set to the same three views. **Note:** the kiosk opens those three views as tabs directly (§8), so in-page links aren't required; the bookmarks matter mainly from the **Chrome (Regular)** launcher, where the bookmark bar is shown.
6. `/etc/hosts` entry `192.168.88.10  kela.local`
7. **Server cert fetch** — installs the standalone, re-runnable `kela-install-cert` command and runs it once (via `openssl s_client`, mirrors phase 077):
  - extracts the cert presented on `192.168.88.10:443` (the LAN server, a.k.a. `kela.local`); override with `CERT_FETCH_HOST`
  - auto-detects CA vs leaf and picks NSS trust flags (`C,,` vs `P,,`)
  - drops it at `/usr/local/share/ca-certificates/kela/kela-server.crt` + `update-ca-certificates` (system trust → curl/wget/etc.)
  - `certutil -A` into `/home/kela/.pki/nssdb` under nickname `"Kela Server"` (Chrome's NSS store)
  - writes `/etc/opt/chrome/policies/managed/kela-ssl-policy.json` with `SSLErrorOverrideAllowedForOrigins` as a defence-in-depth fallback
  - retries 5× with 5s backoff; **if the server doesn't exist yet, the step is non-fatal** — the station converges on its own (see §7b), so no manual follow-up is needed

7b. **Self-healing cert convergence** — installs `/usr/local/sbin/kela-cert-ensure` and a `kela-cert-ensure.timer` (`OnBootSec=2min`, `OnUnitActiveSec=5min`). Every 5 min it probes the served cert's SHA-256 fingerprint and compares it to the installed one: server absent → silent no-op; fingerprints equal → no-op; **missing or changed** → runs `kela-install-cert` and `try-restart`s the kiosk so Chrome re-reads NSS. A station built before its server exists picks up the cert within ~5 min of the server coming online, and **self-heals if the server is rebuilt with a new cert** (fingerprint mismatch → re-pin → kiosk restart). To skip the wait you can still run `sudo kela-install-cert` by hand.
8. **Kiosk launch** — a `kela-kiosk` **systemd user service** (`Restart=always`) runs Chrome `--kiosk` via `/usr/local/bin/kela-kiosk`, opening **three tabs in one window**: `https://kela.local/` (1), `https://kela.local/location-updater` (2), `http://192.168.88.210:6010/` (camera, 3). A GNOME autostart entry starts it at login; if Chrome is closed or crashes it relaunches in ~2s. Operators switch tabs with **Ctrl+Tab** or **Ctrl+1/2/3** (`Ctrl+0` and `Ctrl+4..9` are swallowed so stray number keys can't reset zoom or jump to a phantom tab). In **tablet mode** (no keyboard), the two bezel buttons drive the kiosk: **A2** cycles the three views forward (injects Ctrl+Tab via `kela-kiosk-next-tab`), and **A1** triple-press escapes → `kela-kiosk-escape` stops the service and drops to plain GNOME (docked, **triple-tap F2** does the same). Both buttons are wired via GNOME `XF86Launch1`/`XF86Launch2` bindings, with an hwdb fallback for firmware that emits raw scancodes (§9f). Re-enter from the **Kela Kiosk** app icon or a reboot.
9. **Operator session hardening**:

- GDM auto-login as `kela`, and `WaylandEnable=false` — the kiosk runs on **Xorg** (GNOME's Wayland touchscreen shell-gestures are an escape hole; Xorg has none and gives `xinput`/`DontVTSwitch` control)
- `systemctl mask` of `sleep.target`, `suspend.target`, `hibernate.target`, `hybrid-sleep.target` — nothing on the system can trigger a sleep state
- `systemctl mask getty@tty2…tty6` — closes the `Ctrl+Alt+F#` VT-switch escape (kernel-level, so not covered by GNOME keybindings)
- `systemctl mask iio-sensor-proxy.service` — **orientation lock** (no auto-rotate when the slate is handled; keeps the landscape UI and touch mapping fixed). Touch input is unaffected.
- `/etc/systemd/logind.conf.d/kela-no-sleep.conf` — lid switch, power button, suspend key, hibernate key, and idle action all set to `ignore`
- **System dconf db** (`/etc/dconf/db/local.d/00-kela-power`, with locks): `idle-delay 0` (**Screen Blank: Never**), screensaver/lock off, `sleep-inactive-{ac,battery}-type=nothing` + timeouts 0, `power-button-action=nothing`, idle-dim off, plus **always-on**: `critical-battery-action='nothing'` + `/etc/UPower/UPower.conf` `CriticalPowerAction=Ignore` so it never auto-shuts-down — it runs until the battery is physically depleted.
- **Kiosk input lockdown** (`/etc/dconf/db/local.d/10-kela-kiosk`, with locks): `F2` and `XF86Launch1` (A1) → `kela-kiosk-escape`, `XF86Launch2` (A2) → `kela-kiosk-next-tab`; `Ctrl+W/Ctrl+Shift+W/Ctrl+Q/Ctrl+T/Ctrl+N` swallowed to `/bin/true`; `Ctrl+0` and `Ctrl+4..9` also swallowed (so only the three real tabs are reachable via `Ctrl+1/2/3`, `Ctrl+Tab` left intact); overview key, `Alt+Tab`/window-switch, workspace-switch, close/minimize/show-desktop all unbound — so F2/A1 is the only way out. On-screen keyboard (`onboard`) auto-shows for tablet use; HiDPI text scaling set for the 3:2 panel.
- `/usr/local/bin/kela-session-init` + autostart `.desktop` — on every login: **Power Mode → performance** (`powerprofilesctl`, since it resets to balanced each boot), reasserts screen-never-blank (`gsettings` + `xset -dpms`), default sink to 100% + unmuted output, and **mutes every input source (microphone off by default)**

1. **UFW**: deny in+out by default; allow LAN (RFC1918 + `100.64.0.0/10`), `tailscale0`, plus DNS / 443/tcp / 41641/udp / 3478/udp outbound for Tailscale control plane, **80/tcp** outbound (apt mirrors are plain http — without it fleet patching over Tailscale dies at the apt step; Chrome as `kela` stays LAN-only via the per-user egress chain) and **123/udp** (NTP fallback)
2. **Cleanup + Bluetooth block**:
  - purge GNOME games, LibreOffice, Thunderbird, Rhythmbox, Shotwell, Cheese, Remmina, Transmission, Déjà Dup
    - `rfkill block bluetooth` + persistent `rfkill-block-bluetooth.service`
    - `systemctl mask bluetooth.service`
    - `/etc/modprobe.d/blacklist-bluetooth.conf` (bluetooth, btusb, btbcm, btintel, btrtl, btmtk, hci_uart)
    - `update-initramfs -u`
    - `apt purge bluez bluez-cups bluez-obexd gnome-bluetooth*`
3. `kela-verify` **+ build-info**:
  - installs `/usr/local/sbin/kela-verify` — the §4.3 checklist as code (remote access, firewall, kiosk wiring, sleep masking, bluetooth, NTP; PASS/FAIL per check, non-zero exit on any required failure). The USB first-boot runner refuses to finalize unless it passes; re-run any time with `sudo kela-verify`
    - writes `/etc/kela/build-info`: site, hostname, setup version, build date, Tailscale IP, **AnyDesk ID** (`anydesk --get-id` — no manual GUI step)
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

- **Per-site addressing (single source of truth)**: hub IP/host, camera URL, NTP source, and cert host are **not** hardcoded across scripts — they resolve from env overrides at build time (set them in `secrets.env`; defaults match the standard `192.168.88.0/24` layout) and are written to `/etc/kela/station.conf`. `kela-kiosk`, `kela-install-cert`, and `kela-verify` all source that file at runtime, so re-pointing a station in the field is a one-file edit (`sudoedit /etc/kela/station.conf`) followed by `sudo kela-verify` + `systemctl --user restart kela-kiosk` (as `kela`) — no script surgery. `kela-verify` asserts `/etc/hosts` actually maps the configured hub host → hub IP (a mismatched site now **fails** the check instead of finalizing green) and WARNs if the hub/camera aren't reachable. The one exception: the Chrome managed policy (homepage/bookmarks) is generated from these values at build time, so changing `station.conf` on an existing box updates the live kiosk tabs but not the policy bookmarks — re-run `operator-setup.sh` to regenerate the policy.
- **Installing the cert later / cert rotation**: this is now automatic. The `kela-cert-ensure` timer (§3.7b) runs every 5 min, and when the served fingerprint differs from what's installed (server first appears, or its cert rotates) it re-pins and restarts the kiosk. If you don't want to wait for the next tick you can still run `sudo kela-install-cert` by hand — it pulls from `192.168.88.10:443` by default (or `sudo kela-install-cert <host> [port]`), is idempotent (deletes the old `Kela Server` nickname before re-adding), and `openssl s_client` always grabs whatever the server is currently serving.
- **NSS vs system trust store**: Chrome on Linux ignores `/etc/ssl/certs` and uses NSS at `~/.pki/nssdb`. `kela-install-cert` installs the cert in *both* places so curl/wget *and* Chrome trust kela.local.
- **Chrome homepage + bookmarks**: pinned via the managed policy `/etc/opt/chrome/policies/managed/kela-policy.json` (`HomepageLocation`, `RestoreOnStartupURLs`, `ManagedBookmarks`). The bookmarks live in a locked "Kela" folder on the bookmark bar and can't be deleted by the operator. Edit that JSON (and re-launch Chrome) to change them. **In kiosk mode the bookmark bar is not shown**, but the three views (hub / location-updater / camera) are opened as tabs by `/usr/local/bin/kela-kiosk`, so operators reach them with `Ctrl+Tab` or `Ctrl+1/2/3`. To change the three addresses, edit `/etc/kela/station.conf` (`KELA_LOCAL_URL` / `LOCATION_UPDATER_URL` / `CAMERA_URL`) and restart the kiosk — the launcher sources it at runtime; to add/remove tabs beyond three, edit the URL list in `/usr/local/bin/kela-kiosk`. Keep `ManagedBookmarks` in the policy in sync if you want them to match in the **Chrome (Regular)** launcher, where the bookmark bar is shown.
- **Kiosk self-healing (crash + error-page)**: `kela-kiosk.service` is `Restart=always` with `StartLimitIntervalSec=0` (systemd's default 5-starts/10s limit is disabled, so a crash-loop after a hard power-off never lands in permanent `failed`) and the launcher scrubs Chrome crash state + stale `Singleton`* locks on each start. Because `Restart=always` only fires on a *crash* — not on an `ERR_CONNECTION_REFUSED` page, which Chrome caches and never retries — two extra guards handle a down/slow hub: the launcher waits up to ~30s for the hub to answer before opening Chrome, and `kela-kiosk-watch.service` polls the hub every 20s and restarts the kiosk on an unreachable→reachable transition, so a tablet with no keyboard recovers on its own once the hub is back (no manual `Ctrl+R`). Tune the wait/poll in `/usr/local/bin/kela-kiosk` and `/usr/local/bin/kela-kiosk-watch`.
- **Kiosk escape / return**: the kiosk is a `kela-kiosk` **systemd user service** (`Restart=always`). Escape to a normal desktop: **triple-tap F2** (keyboard docked) or **triple-press A1** (tablet) — `kela-kiosk-escape` runs `systemctl --user stop kela-kiosk`. From the desktop, launch **Chrome (Regular)** or **Kela Kiosk** from the app grid (mouse/touch via the dock's "Show Applications"), or over Tailscale/AnyDesk: `systemctl --user stop|start kela-kiosk` (from SSH: `sudo -u kela XDG_RUNTIME_DIR=/run/user/$(id -u kela) systemctl --user …`). A `reboot` always returns to the kiosk. F2/A1 is the **only** local way out — the overview key, `Alt+Tab`, workspace-switch, `Ctrl+W/Q/T/N`, `Alt+F4`, and the spare VT consoles are all disabled.
- **Tablet mode (touch + bezel buttons)**: touch works on Xorg via libinput (tap/scroll/zoom unaffected by the gesture lockdown, which only blanks GNOME *shell* keybindings). The `onboard` on-screen keyboard auto-shows for text entry. The two bezel buttons control the kiosk with no keyboard: **A2** cycles the three views **forward only** (each press = Ctrl+Tab), so the worst case to reach any view is two presses; **A1** triple-press escapes to GNOME (same script as F2). The buttons work out of the box: setup.sh ships an hwdb remap captured on real CF-33 hardware (device "Panasonic Laptop Support": A1 = scan `09`, firmware default `KEY_BATTERY` → `f2`; A2 = scan `0a`, firmware default `KEY_SUSPEND` → `prog2`), feeding the F2-escape and `XF86Launch2` GNOME bindings. If a different firmware revision leaves a button inert, re-capture with `sudo evtest` (press the button, note the `MSC_SCAN` value), update `/etc/udev/hwdb.d/70-kela-cf33-buttons.hwdb`, and apply with `sudo systemd-hwdb update` + `sudo udevadm trigger` (then update setup.sh §9f so the fix ships). `--force-device-scale-factor` in `/usr/local/bin/kela-kiosk` tunes text size for the 3:2 panel.
- **Always-on / battery**: the box never sleeps, never blanks, and **never auto-shuts-down** — `critical-battery-action='nothing'` (dconf) + `CriticalPowerAction=Ignore` (`/etc/UPower/UPower.conf`) mean it runs until the battery is physically depleted. To restore normal low-battery behaviour, revert those two.
- **Screen still blanks / power mode resets?** Screen-blank is enforced two ways: a locked system dconf db (`idle-delay 0`) *and* `kela-session-init` reasserting it (`gsettings` + `xset -dpms`) each login. Power Mode = performance is applied per-login via `powerprofilesctl` because `power-profiles-daemon` reverts to `balanced` on every boot. Don't rely on `sudo -u kela dbus-launch gsettings set ...` at build time — it frequently fails to persist, which was the original cause of the monitor still sleeping. To allow blanking/balanced again, remove the dconf locks + `00-kela-power`, run `dconf update`, and drop the relevant lines from `kela-session-init`.
- **Microphone muted by default**: `kela-session-init` mutes every input source on each login. It's reversible (the operator can unmute for a call), but resets to muted on the next login/reboot. To allow the mic permanently, remove the `set-source-mute` lines from `/usr/local/bin/kela-session-init`.
- **Cert trust flags**: the script auto-detects whether the hub is presenting a CA cert (`CA:TRUE` in basic constraints → NSS `C,,`) or a leaf cert (NSS `P,,`). Don't hand-edit the trust flags unless you know which one applies.
- **AnyDesk over Tailscale**: connect using the machine's Tailscale IP or MagicDNS name; UFW allows full inbound on `tailscale0`.
- **Chrome telemetry**: `/etc/opt/chrome/policies/managed/kela-policy.json` is what suppresses metrics accumulation. Don't delete it.
- **Bluetooth re-enable** (if ever needed): remove `/etc/modprobe.d/blacklist-bluetooth.conf`, `systemctl unmask bluetooth.service`, `apt install bluez`, `update-initramfs -u`, reboot. `rfkill unblock bluetooth` alone won't do it.
- **Re-enable sleep** (if you ever repurpose the box): `systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target`, delete `/etc/systemd/logind.conf.d/kela-no-sleep.conf`, `systemctl restart systemd-logind`, and revert the relevant `org.gnome.settings-daemon.plugins.power` gsettings.
- **Site-specific LAN**: the UFW config allows all RFC1918, not just `192.168.88.0/24` — keeps the image portable across sites. Tighten if a site requires stricter posture. Per-site *addressing* (hub/camera/NTP/cert) is parametrized via `secrets.env` → `/etc/kela/station.conf` (see the "Per-site addressing" note above).
- **Egress test nuance**: the §4.3 `curl https://example.com` test must FAIL **as the kela user** (per-user egress chain). Root processes (apt, tailscaled, AnyDesk) are allowed out on 53/80/443/123/41641/3478 — that's deliberate, so central patching works.
- **MOK / Secure Boot**: irrelevant for this base build; only matters if you later install unsigned kernel modules (e.g. some Magos/SiriusDriver pieces).



---



## 6. USB-based unattended install

For new ruggeds, prefer the install USB built from `[usb-builder/](./usb-builder/)`. It collapses §2-§4 into:

1. Power on the rugged with the USB inserted (**hold** `F2` at the Panasonic logo → Setup → boot the USB; the CF-33 has no one-time F12 menu). Image with the keyboard docked — the GRUB / first-boot site-name prompt needs it.
2. **Type the site name at the GRUB menu** (e.g. `fob-12`) and walk away —
  install + first-boot setup run hands-off to completion (~20-30 min) and
   the machine reboots to Chrome on `https://kela.local/`. One visit total.
3. (Fallback: press Enter at GRUB without a name → the site name is asked
  at a first-boot prompt after the ~15 min install instead.)

The first-boot runner only finalizes (and only shreds the baked auth key)
after Tailscale is connected **and** `sudo kela-verify` passes — a failed
station keeps its retry wiring instead of stranding unreachable.

Build prerequisites, secrets format, and re-run / failure recovery are in `[usb-builder/README.md](./usb-builder/README.md)`. Treat the resulting `.iso` (and the USB stick) as a secret — the Tailscale auth key is baked in until first-boot finishes, at which point `kela-first-boot-run` shreds it.

**Installing many laptops in parallel?** Run `apt-cacher-ng` on the imaging bench and set `APT_PROXY` in `secrets.env` — the first unit populates the cache, the rest download at LAN speed. See [Parallel imaging](./usb-builder/README.md#parallel-imaging-bench-apt-cache).