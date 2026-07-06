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
- A USB stick of at least 4 GB (the customised ISO is ~2.6 GB)
- **Network at install time** (wired ethernet on the target) — the desktop
  environment downloads from the archive during the install. For parallel
  imaging, see [Parallel imaging](#parallel-imaging-bench-apt-cache).

---

## Build

1. Fill in the secrets:

   ```bash
   cp secrets.env.example secrets.env
   $EDITOR secrets.env          # paste TS_AUTHKEY, set TS_TAGS
   chmod 600 secrets.env
   ```

2. Run the builder:

   ```bash
   ./build-iso.sh \
       --input   ~/Downloads/ubuntu-24.04.4-live-server-amd64.iso \
       --output  ./kela-operator-24.04.iso \
       --secrets ./secrets.env
   ```

   Optional flags:
   - `--password '<plaintext>'` — override the kela password
     (default `Kelasys123!`, matching `setup.sh`).
   - `--volid 'My Label'` — change the ISO volume label.

3. Write the resulting ISO to the USB stick.

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
   - Installs the GNOME desktop (`ubuntu-desktop-minimal` + `gdm3`) from the
     archive — via the bench apt cache when `APT_PROXY` is set in secrets.env
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
     script version) and, if `KELA_CHECKIN_URL` is set in
     `secrets.env`, POSTs it to your inventory endpoint
   - Removes the autostart entry, the sudoers drop-in, and **shreds the
     secrets file** so the auth key isn't sitting on disk
   - Reboots

6. From that point on the machine boots straight into **Chrome `--kiosk`**
   showing the configured views (`TAB1`..`TAB3` in secrets.env; default is
   the hub alone) as tabs in one
   full-screen window (no address bar or bookmark bar — the locked "Kela"
   bookmarks are reachable from the "Chrome (Regular)" launcher),
   self-relaunching if closed, microphone muted by default, and reachable via
   Tailscale + AnyDesk. Switch views with **Ctrl+Tab** / **Ctrl+1/2/3** when
   docked, or the **A2** bezel button (cycles forward) in tablet mode. Escape
   to a normal desktop with triple-tap **F2** (docked) or triple-press **A1**
   (tablet); return via the "Kela Kiosk" app icon or a reboot.

Total wall-clock on a CF-33 with a wired SSD-class disk: ~30-45 min
end-to-end (download + unpack/configure + first boot), less with a bench
apt cache.

---

## Parallel imaging (bench apt cache)

Each install downloads ~2 GB from the archive (the desktop, then
Tailscale/AnyDesk/Chrome at first boot). Imaging many laptops in parallel
over one uplink multiplies that. The fix is a standard caching proxy on the
imaging bench — **not** baking packages into the ISO (we tried; the offline
pool bought ~5 min of downloads per unit at the cost of a Docker-based
dependency-closure pipeline, version-skew failures against the ISO base,
snap phone-home hangs, and a 5–6 GB ISO — see git history):

1. On any Linux box on the bench LAN (the VM test host is ideal):

   ```bash
   sudo apt install apt-cacher-ng     # listens on :3142
   ```

2. In `secrets.env`, set:

   ```bash
   APT_PROXY=http://<bench-ip>:3142
   ```

3. Rebuild the ISO. Both the install-time desktop download and the
   first-boot package installs route through the cache: the first unit
   populates it, every subsequent unit downloads at LAN speed. The proxy is
   deliberately **non-persistent** — nothing is written under `/etc/apt` on
   the station, so field units never carry a dead bench proxy.

Sticks built *without* `APT_PROXY` work anywhere with a network; the proxy
is purely a bench-throughput optimization.

> **Validate once per ISO revision:** install a single laptop end-to-end —
> through first boot and `sudo kela-verify` — before batch-imaging with it.

### If the install looks hung (disk LED quiet, no visible progress)

From the installer shell (`Ctrl+Alt+F2`, or Help → Enter shell):

```bash
journalctl --no-pager | grep 'kela:' | tail   # which phase are we in?
watch -n15 'chroot /target dpkg -l 2>/dev/null | grep -c "^ii"'   # count climbing = installing
ps aux | grep -E 'apt-get|dpkg|cp' | grep -v grep
```

> **WARNING — do not leave anything open under `/target`.** You can
> `tail -f /target/var/log/dpkg.log` for per-package detail, but you MUST
> Ctrl-C it before the install finishes: any process holding a file open
> under `/target` makes curtin's final unmount fail (`umount` exit 32) and
> **crashes an otherwise-successful install at teardown**. Same for shells
> `cd`'d into `/target`. The `watch` command above is safe (its probes are
> transient). If you hit this anyway — subiquity traceback with
> "returned non-zero exit status 32" — the install is fine: find the holder
> with `fuser -vm /target`, kill it, `umount -R /target`, reboot.

The desktop configure phase is CPU-bound and legitimately runs **15–25 min
with a quiet disk** — `kela: desktop install starting` followed by advancing
`dpkg.log` lines means it's healthy. `kela: desktop install DONE` marks
success. A `dpkg.log` frozen 10+ min with idle CPU is a real stall: check the
last package it logged. Known trap (fixed, kept here for recognition): the
**firefox** transitional deb's postinst runs `snap install firefox` — a snap
store phone-home that hangs when the store is unreachable; it is pinned out of
the build, and `api.snapcraft.io` is pointed at localhost during the install
so any similar phone-home fails in seconds instead of hanging silently.

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
> non-fatal and setup continues. The `kela-cert-ensure` timer then
> installs the cert automatically within ~5 min of the server coming
> online and restarts the kiosk for you (it also self-heals if the
> server is later rebuilt with a new cert). To skip the wait you can
> run `sudo kela-install-cert` by hand (pulls from `192.168.88.10:443`
> by default, or `sudo kela-install-cert <host> [port]`), then restart
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
