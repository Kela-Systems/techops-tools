# Magos configurators (radar + APU)

Two bench tools for provisioning **Magos** devices one plug-in at a time, sharing
the same *plug in → detect → configure → unplug → repeat* workflow as the rest of
the [bench tools](../README.md):


| Tool  | Device                             | Port |
| ----- | ---------------------------------- | ---- |
| Radar | Magos AR-300 **radar**             | 8001 |
| APU   | Magos **AR Processing Unit (APU)** | 8002 |


Both run independently on their own ports, so you can provision a radar and an
APU side by side.

## For the operator

> **Network adapter:** your laptop must have an adapter on the device's factory
> subnet (`192.168.40.x`) or the device will never be detected. A fresh radar
> boots on `192.168.40.50` (or `.60`); a fresh APU boots on `192.168.40.60`.
> Both ship as `admin:password`.

1. Start the bench with the top-level launcher (`Start Bench Tools.bat` on Windows or
   `start-bench.command` on macOS). It sets up the shared `.venv` and starts
   every tool — radar on 8001, APU on 8002 — then opens the dashboard. First run
   installs dependencies (needs internet once).
2. Plug a device into the laptop. The page detects it on the factory IP.
3. Pick the **channel** (0–3) or enter a manual IP, then click configure. The
  tool sets NTP + timezone and the static IP (the APU also sets the controlled
   radar IP), verifies the device at its new address, and tells you to unplug it
   and plug in the next one.

### Channel mapping

Channel `N` maps to a fixed scheme on the `192.168.88.x` subnet:


| Channel | Radar IP        | APU IP          | APU controls radar |
| ------- | --------------- | --------------- | ------------------ |
| 0       | `192.168.88.50` | `192.168.88.60` | `192.168.88.50`    |
| 1       | `192.168.88.51` | `192.168.88.61` | `192.168.88.51`    |
| 2       | `192.168.88.52` | `192.168.88.62` | `192.168.88.52`    |
| 3       | `192.168.88.53` | `192.168.88.63` | `192.168.88.53`    |


### Picking a manual ("other") IP instead of a channel

If you enter your own IP rather than choosing a channel 0–3, the device is
treated as channel `other` and only the parts you actually gave it are applied:

- **Radar:** NTP + timezone and the static IP/gateway/DNS are set, but the **RF
channel is left untouched** — it's only changed when you pick a channel 0–3
(firmware ≥ 3.x). So a manual IP keeps whatever RF channel the radar already
has; it does *not* default to channel 0. The IP is combined with the configured
netmask, and the radar is then verified at that new address.
- **APU:** NTP + timezone and the static IP are set, but the **controlled radar
is only changed if you supply a radar IP** — leave it blank and the APU keeps
its existing `phoenix_ip` target.

In both cases nothing checks that the IP belongs to the expected `192.168.88.x`
scheme — the device goes to exactly the address you type, so a typo lands the
unit on the wrong address (and it'll only be reachable there afterwards).
Double-check a manual IP before configuring.

### Auto / Cycle modes (hands-free)

- **Auto:** arm a single target; every device detected on the factory IP is
configured to it automatically.
- **Cycle:** configure devices in groups of four, each taking the next channel
(0 → 1 → 2 → 3, wrapping). Just keep plugging them in.

Both modes refuse to reconfigure the same unit if it briefly reappears on the
factory IP (matched by serial number) — so unplug each one before the next.

Both modes also disarm themselves automatically after **10 minutes with nothing
plugged in**, so a mode left on by mistake won't silently reconfigure a unit
that gets plugged in much later. The dashboard shows a message when this
happens; just toggle the mode back on to resume.

## Command-line use

Both device clients also run standalone, without the web UI:

```bash
python3 magos_configure.py --interactive   # radar: asks the channel, picks the IP
python3 apu_configure.py --interactive      # APU: asks the channel, picks the IPs
```

Pass `--host`, `--ip`, `--ntp`, etc. to drive them explicitly, or set
`MAGOS_PASSWORD` / `APU_PASSWORD` to avoid the password prompt. See each script's
`--help` for the full list.

## Layout


| File                                   | Role                                                                                                             |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `app.py` / `apu_app.py`                | The radar / APU FastAPI apps (thin subclasses)                                                                   |
| `magos_bench.py`                       | Shared bench engine (`MagosBench`): detection loop, auto/cycle state machine, settings, per-unit logging, routes |
| `magos_configure.py`                   | Radar device client + standalone CLI                                                                             |
| `apu_configure.py`                     | APU device client + standalone CLI                                                                               |
| `static/index.html`, `static/apu.html` | The radar / APU dashboards                                                                                       |
| `logs/`                                | Rolling human log + per-unit JSON records                                                                        |


## For engineers

- The two apps are thin subclasses of `MagosBench` that fill in device-specific
hooks (`resolve_target`, `do_configure`, `build_entry`, `success_message`);
all the common machinery lives in `magos_bench.py`.
- The generic bench-UI helpers (`StepCollector`, `slug`) come from the shared
`bench-core` package, installed editable into the bench-root `.venv`
(`-e ./bench-core[ui]`), which is why this folder must stay a sibling of
`bench-core/` inside `bench/`.
- Both device clients log through the `magos` logger, so step lines render the
same in the CLI and the UI.
- Tests live in `tests/` (`test_radar_app.py` / `test_apu_app.py`); they exercise
the state machine and run with no hardware. From the bench root:
`.venv/bin/python -m pytest`.

