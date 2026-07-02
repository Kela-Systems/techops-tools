# Kela Operator Install USB

Bake a turnkey USB stick that installs Ubuntu 24.04 + the Kela operator
kiosk configuration on a Panasonic Toughbook CF-33 with **one input total**:
the site identifier, typed either **at the GRUB menu at power-on** (then the
whole install + first-boot setup runs hands-off to completion) or, if left
empty there, at a **first-boot prompt** after the install.

Everything in `../setup.md` still applies — this directory just automates §2
(Ubuntu install) and §3 (post-install script) into a single bootable USB.

---

## What ends up on the USB

| Bucket | Contents |
|---|---|
| Stock Ubuntu 24.04 Desktop ISO | the entire vanilla casper + grub tree |
| `/autoinstall/user-data` | Subiquity autoinstall config (kela user, password hash, late-commands) |
| `/autoinstall/meta-data` | NoCloud datasource id |
| `/operator/operator-setup.sh` | exact copy of `../setup.sh` |
| `/operator/secrets.env` | `TS_AUTHKEY` + `TS_TAGS` (treat the ISO as a secret!) |
| `/operator/kela-first-boot-*` | UI prompt, root runner, autostart entry, sudoers drop-in |
| `/extras/` *(offline build only)* | local apt pool: desktop + Tailscale/AnyDesk/Chrome `.debs` + `Packages` index |
| `boot/grub/grub.cfg` | patched: default entry is "Auto-install (WILL ERASE DISK)" |

---

## Build host requirements

The builder runs on either Linux or macOS.

```bash
# Linux (Ubuntu / Debian)
sudo apt install xorriso openssl

# macOS (Homebrew)
brew install xorriso
# openssl already ships with macOS; on pre-Sonoma releases:
#   brew install openssl@3
#   export PATH="$(brew --prefix openssl@3)/bin:$PATH"
```

`xorriso` is used to both inspect and rebuild the ISO (preserving its
hybrid BIOS+UEFI boot setup verbatim via `-boot_image any replay`), so no
separate ISO extractor is needed.

For an **offline-install ISO** (strongly recommended when imaging many
laptops in parallel — see [Offline install](#offline-install-no-wan-during-install))
you also need **Docker** (Desktop on macOS / engine on Linux) to run
`collect-offline-packages.sh`.

You also need:
- The **Ubuntu 24.04.x Live Server amd64 ISO** from <https://releases.ubuntu.com/24.04/>
  (`ubuntu-24.04.x-live-server-amd64.iso`, ~2.5 GB).

  **Do not use the Desktop ISO.** Ubuntu 24.04's new flutter desktop
  installer does not reliably honor autoinstall — cloud-init loads the
  config successfully but the installer still launches its GUI wizard.
  The Server ISO uses Subiquity, which honors autoinstall fully. The
  autoinstall config in this repo pulls in `ubuntu-desktop-minimal` +
  `gdm3` during the install, so the end result is still a GNOME kiosk.
- A Tailscale **reusable, ephemeral=false, pre-approved, tagged** auth key
  from <https://login.tailscale.com/admin/settings/keys>.
  **Record its expiry date in `secrets.env` as `TS_KEY_EXPIRY`** — keys live
  90 days max, and a stick built from a stale key installs fine and then
  fails `tailscale up` in the field. The builder refuses to build with an
  expired key and warns when <14 days remain.
- A USB stick of at least 8 GB (the customised ISO is ~5-6 GB)

---

## Build

1. Fill in the secrets:

   ```bash
   cp secrets.env.example secrets.env
   $EDITOR secrets.env          # paste TS_AUTHKEY, set TS_TAGS
   chmod 600 secrets.env
   ```

2. *(Recommended)* Build the offline package pool once — this is what makes
   parallel installs fast (zero WAN during install). Needs Docker:

   ```bash
   ./collect-offline-packages.sh        # → ./offline-pool/ (~2 GB, a few min)
   ```

   Skip this step to build an **online**-install ISO instead (each laptop
   downloads ~2 GB during install).

3. Run the builder. It **auto-detects `./offline-pool`** and embeds it:

   ```bash
   ./build-iso.sh \
       --input   ~/Downloads/ubuntu-24.04.4-live-server-amd64.iso \
       --output  ./kela-operator-24.04.iso \
       --secrets ./secrets.env
   ```

   The build log prints either `Embedding offline package pool … (fully
   offline install)` or `building an ONLINE-install ISO` so you know which
   you got.

   Optional flags:
   - `--offline-pool <dir>` — use a pool from a non-default location.
   - `--password '<plaintext>'` — override the kela password
     (default `Kelasys123!`, matching `setup.sh`).
   - `--volid 'My Label'` — change the ISO volume label.

4. Write the resulting ISO to the USB stick.

   **Linux**:

   ```bash
   lsblk                                       # identify the USB (e.g. /dev/sdb)
   sudo umount /dev/sdX*    2>/dev/null || true
   sudo dd if=./kela-operator-24.04.iso of=/dev/sdX bs=4M conv=fsync status=progress
   sync
   ```

   **macOS**:

   ```bash
   diskutil list                                # identify the USB (e.g. /dev/disk4)
   diskutil unmountDisk /dev/diskN              # unmount partitions, keep the device
   sudo dd if=./kela-operator-24.04.iso of=/dev/rdiskN bs=4m status=progress
   sync
   diskutil eject /dev/diskN
   ```

   Notes:
   - On macOS use `/dev/rdiskN` (the raw character device) not `/dev/diskN` —
     the raw device is an order of magnitude faster.
   - macOS `dd` uses lowercase suffixes: `bs=4m`, not `bs=4M`.
   - macOS `dd` supports `status=progress` on Monterey+. If your version
     doesn't, press **Ctrl+T** during the copy to print progress.

   **Triple-check** the `of=` device — `dd` will silently destroy whatever
   is on it.

---

## What happens during install

1. Boot the rugged from the USB — **hold `F2`** at the Panasonic logo → Setup → boot the USB (the CF-33 has no Dell-style one-time F12 menu). Keep the **keyboard docked**: the GRUB and first-boot site-name prompts need it. **Image with USB boot enabled and no Supervisor Password set**; apply the BIOS lockdown (Supervisor Password + disable USB/PXE boot — see [`../setup.md`](../setup.md) §1) **only after** a successful install, or you lock yourself out before imaging.
2. GRUB defaults to "Kela Operator — Auto-install (WILL ERASE DISK)" with a
   5 second timeout. The entry asks for the **site name** (e.g. `fob-12`):
   - **Type it now** → the entire install **and** first-boot setup run
     hands-off; one visit to the machine total.
   - **Just press Enter** → install proceeds; the site name is asked at a
     first-boot prompt instead (the old flow).
   The "Manual install (rescue / fallback)" entry is also present if you
   need to bail out.
3. Subiquity runs unattended:
   - Wipes the first internal disk (`storage.layout: direct`)
   - Creates the `kela` user with the baked password hash
   - Installs the GNOME desktop (`ubuntu-desktop-minimal` + `gdm3`) — from
     `/cdrom/extras` offline if the pool was embedded, else from the network
   - On offline builds, copies the pool to `/opt/kela-pool` so first boot can
     install Tailscale/AnyDesk/Chrome offline too
   - Drops the operator payload into `/usr/local/sbin/` + `/etc/kela/`
   - Configures GDM auto-login for `kela`
   - Reboots (`shutdown: reboot`)

4. **First boot**: GDM auto-logs `kela` in. A GNOME autostart entry pops
   open a terminal. If the site name was typed at GRUB it continues
   automatically with no input; otherwise it prompts (with a confirm step —
   the name becomes the permanent hostname/Tailscale name):

   ```
   Site name: fob-12
     Build as 'fob-12-operator' — correct? [y/N]
   ```

5. The first-boot runner:
   - Sources `/etc/kela/operator-secrets.env`
   - Runs `operator-setup.sh` with `SITE_NAME=fob-12 TS_AUTHKEY=… TS_TAGS=…`
   - **Gates on success**: refuses to finalize unless Tailscale actually has
     an IP **and** `kela-verify` (the §4.3 checklist as code) passes — a
     failed station keeps its retry wiring + auth key instead of stranding
   - Writes `/etc/kela/build-info` (site, Tailscale IP, **AnyDesk ID**,
     script/pool versions) and, if `KELA_CHECKIN_URL` is set in
     `secrets.env`, POSTs it to your inventory endpoint
   - Removes the autostart entry, the sudoers drop-in, and **shreds the
     secrets file** so the auth key isn't sitting on disk
   - Reboots

6. From that point on the machine boots straight into **Chrome `--kiosk`** on
   `https://kela.local/` (full-screen; no tabs, address bar, or bookmark bar —
   the locked "Kela" bookmarks are reachable from the "Chrome (Regular)"
   launcher), self-relaunching if closed, microphone muted by default, and
   reachable via Tailscale + AnyDesk. Escape to a normal desktop with
   triple-tap **F2** (docked) or triple-press **A1** (tablet); return via the
   "Kela Kiosk" app icon or a reboot.

Total wall-clock on a CF-33 with a wired SSD-class disk: ~15-25 min
(online build). With an **offline** ISO, no packages are downloaded at all —
each laptop is independent (no shared WAN/mirror bottleneck), so N laptops in
parallel cost the same network as one: zero. See below.
end-to-end depending on apt mirror speed.

---

## Offline install (no WAN during install)

The slow part of imaging a laptop isn't the USB write (you flash each stick
**once** and reuse it) — it's that every laptop otherwise re-downloads ~2 GB
of packages over the WAN, **twice**:

1. **Subiquity** pulls `ubuntu-desktop-minimal` + GNOME (~1.5–2 GB) — the
   Server ISO has no desktop on it.
2. **First boot** (`operator-setup.sh`) pulls Tailscale, AnyDesk, Chrome, and
   (previously) ran `apt upgrade`.

Run many laptops in parallel and they all fight one uplink + the apt mirrors,
which is why each takes ~30 min. The offline ISO eliminates **all** of that:

- `collect-offline-packages.sh` downloads the full dependency closure of the
  desktop + tools, plus the Tailscale/AnyDesk/Chrome `.debs`, into
  `./offline-pool/` (with a `Packages` index). It runs in a clean
  `ubuntu:24.04` amd64 container so the closure is complete for the Server
  target — and you only run it **once**.
- `build-iso.sh` embeds that pool at `/cdrom/extras`.
- During install, the desktop is installed from `/cdrom/extras` with apt
  pointed at a local `file://` repo (`[trusted=yes]`, network sources
  disabled). The pool is copied to `/opt/kela-pool` so first boot installs the
  apps from it too. `kela-first-boot-run` deletes `/opt/kela-pool` (~2 GB)
  after setup succeeds.
- `operator-setup.sh` auto-detects `/opt/kela-pool`: if present it installs
  from local debs; otherwise it falls back to the network exactly as before
  (so the script is still usable on a normally-installed machine). The
  redundant `apt upgrade` is now skipped by default (set `KELA_APT_UPGRADE=1`
  to force it on an online run).

What offline ISO **does not** remove: the per-machine dpkg unpack/configure
time (~10–15 min). But that's CPU+disk only, fully parallel, with no shared
bottleneck — so it scales flat. Use **USB 3 sticks** so reading the pool off
the stick isn't the new slow point.

> **Validate once:** after the first offline build, install a single laptop
> with **ethernet unplugged**. If a package turns out to be missing, add it to
> `TOOL_PKGS` in `collect-offline-packages.sh` and re-run it.

---

## Re-running after a failure

If something blows up during first-boot setup (network down, etc.), the
runner exits non-zero and **leaves the autostart + sudoers entries in
place** so you can re-run it without re-imaging. "Blows up" includes the
two end-of-run gates: **Tailscale not connected** (expired auth key, tag
mismatch, no network) and **`kela-verify` failures** — in both cases the
auth key is kept on disk for the retry instead of being shredded.

After any fix, `sudo kela-verify` re-checks the whole station in seconds.

> **Server cert when the site server doesn't exist yet:** building a
> station before its server is online is fine — the cert step is
> non-fatal and setup continues. Once the server is up, install its cert
> with `sudo kela-install-cert` (pulls from `192.168.88.10:443` by
> default, or `sudo kela-install-cert <host> [port]`), then restart
> Chrome.

The log lives at `/var/log/kela-first-boot.log`. After fixing the issue:

```bash
# Re-run the same SITE_NAME (the runner is idempotent — operator-setup.sh
# is too).
sudo /usr/local/sbin/kela-first-boot-run <site-name>
```

Or just reboot — the autostart prompt comes back.

---

## Security notes

- **The ISO contains a real Tailscale auth key.** Treat the `.iso` (and
  the USB stick) like the auth key itself. Don't commit it, don't email
  it, shred USBs after deployment.
- **The auth key only sits on the installed system until first-boot
  completes** — `kela-first-boot-run` shreds `/etc/kela/operator-secrets.env`
  after `operator-setup.sh` succeeds (i.e. after `tailscale up` has
  consumed the key).
- **NOPASSWD sudo is one-shot.** The drop-in at
  `/etc/sudoers.d/kela-first-boot` only allows running
  `/usr/local/sbin/kela-first-boot-run`. The runner removes the drop-in
  when it succeeds, so persistent privilege escalation is not granted.
- **GDM auto-login is permanent by design** — the rugged is a kiosk.
  If you ever stop using it as one, edit `/etc/gdm3/custom.conf`.
- **Physical boot lockdown matters.** The software kiosk lockdown (F2/A1 is
  the only escape) is only as strong as the firmware: set a **Supervisor
  Password** and **disable USB/PXE boot** in BIOS *after* imaging (see
  [`../setup.md`](../setup.md) §1), or the kiosk is trivially bypassed by
  booting external media.
- **Secure Boot stays on, no MOK enrollment needed.** The kiosk build installs
  no unsigned kernel modules (Xorg, the A1 hwdb remap, disabling
  iio-sensor-proxy, and onboard are all userspace), so the signed shim/kernel
  path is untouched.
- **The kela password is hashed with SHA-512 crypt (5000 rounds, the crypt
  default)** before baking. Hashing happens at build time on your host, not
  on the ISO.
