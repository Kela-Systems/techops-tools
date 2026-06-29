# Kela Bench — Operator Guide

How to provision devices at the bench. Keep this open on the side screen.

> This is the operator-facing version of the guide. The same content is shown on the
> bench dashboard at **Operator guide** (`launcher/guide.html`). If something here
> doesn't match the screen, tell an engineer so it can be updated.

---

## 1. Starting the bench

1. Double-click `Start Bench Tools.bat` on the desktop.
2. A black window opens and stays open — **leave it open all day.** Closing it stops every tool.
3. After a few seconds the **dashboard** opens in the browser with five tool cards.
4. Click the card for whatever you're about to plug in. A **green dot** means that tool is ready.

> **First start of the day / after an update** can take a minute while it updates and
> installs — the dots stay grey until each tool is up. This is normal; just wait.

---



## 2. The basic routine

Every tool works the same way:

**plug a device in → it's detected → configure it → unplug it → next one**

1. Plug **one** device into the bench cable. The page detects it and shows its details.
2. Fill in what it asks (site name, channel, or IP — see the per-device sections).
3. Click **Configure** and watch the progress log. Green tick = done.
4. When it says so, **unplug it** and plug in the next one.

> ⚠️ **Always unplug the finished device before plugging in the next.** Two devices on the
> cable at once will confuse detection.

---



## 3. Golden rules

- Leave the black `Start Bench Tools.bat` window open the whole time.
- One device on the cable at a time.
- Wait for the green tick before unplugging.
- If a device is never detected, it's almost always plugged into the **wrong port / network**
for that device — Magos uses a different network from the Teltonika and camera tools.
- Each device ends up on a final `192.168.88.x` address. After that it will no longer answer on
the factory address — that's expected, it means the move worked.

---



## 4. Magos Radar & APU  *(network 192.168.40.x)*

Radar opens on its own card; APU on another. Plug the unit in, then choose its **channel** and
click configure. The tool sets the time and the device's permanent IP, then verifies it.

**Channel → address**


| Channel | Radar address | APU address   |
| ------- | ------------- | ------------- |
| 0       | 192.168.88.50 | 192.168.88.60 |
| 1       | 192.168.88.51 | 192.168.88.61 |
| 2       | 192.168.88.52 | 192.168.88.62 |
| 3       | 192.168.88.53 | 192.168.88.63 |


> **Manual ("other") IP:** if you type your own IP instead of picking a channel, the device goes
> to **exactly** that address and the RF channel is left unchanged. Double-check the number — a
> typo sends the unit to the wrong address.

**Note:** This workflow is optimized for the Magos AR300 model and the cUAS version, but it works for different Magos models as well. for SR-500 and SR-1000 models, use only the Manual workflow.

**Auto & Cycle (hands-free) modes**

- **Auto** — set one target once; every unit you plug in gets configured to it automatically.
- **Cycle** — configures units in groups of four, giving each the next channel (0 → 1 → 2 → 3,
then back to 0). Just keep plugging them in.

Both skip a unit that briefly re-appears (so unplug each one), and both **turn themselves off
after 10 minutes with nothing plugged in** — the page shows a message; just switch the mode back
on to continue.

---



## 5. Teltonika OTD500 & RUTM08  *(network 192.168.1.x)*

Plug the router in. The page reads it and asks for two things:

- **Site name** — e.g. `kela-fob-123`. The device is named after it (`otd-kela-fob-123` / `rut-kela-fob-123`).
- **Label password** — the factory password printed on the device's sticker.

Click **Configure**. The tool does everything else (password, name, time, firmware, RMS, Tailscale,
checks) and, for the RUTM08, **moves the LAN to** `192.168.88.1` **as the last step**.

> **Re-running a device that was already configured?** Leave the label-password field **empty** —
> it's already on the shared password. The RUTM08 page can also pick it up again on its new
> `192.168.88.1` address.

> ⚠️ **Firmware:** if the page warns the firmware image is missing, tell an engineer — the device
> can't be fully configured without it.

---



## 6. Raythink thermal camera  *(network 192.168.1.x)*

A fresh camera answers on `192.168.1.123`. Plug it in, then choose:

- **Profile** — `LAN` or `Cellular` (ask which the job needs).
- **IP** — either **Manual** (type the last number, 30–50) or **Cycle** (the tool picks the next
free number automatically: 30, 31, 32 …).

Click **Configure**. The tool sets the password, imports the profile, sets the time, the ONVIF
login, and **moves the camera to** `192.168.88.<number>` **as the last step**, then verifies it.

> **Cycle mode** remembers its place even after a restart, and only moves to the next number when a
> camera finishes cleanly — a failed camera keeps its number for the retry.

---



## 7. Known issues & quick fixes

**A tool card stays grey**
The tool isn't up yet. On the first start of the day it's still installing — wait a minute and it
turns green. If it never turns green, close the black window and run `Start Bench Tools.bat` again.

**Device plugged in but never detected**
Wrong network for that device. Magos units use the `192.168.40.x` port; Teltonika and camera units
use the `192.168.1.x` port. Make sure it's the right cable/port, fully seated, and the device has
finished booting (give it ~30 seconds). Try once more before asking for help.

**Configuration failed (red error)**
Read the message at the top — it usually says which step failed. Click **Dismiss**, leave the device
plugged in, and click **Configure** again to retry. Steps that need internet (firmware, RMS,
Tailscale) can fail if the SIM/uplink has no signal yet — wait a moment and retry.

**Device disappeared right after "Configured"**
That's normal. Most devices move to a new `192.168.88.x` address as the last step, so they stop
answering on the address they shipped with. The green tick means it worked — unplug and move on.

**I closed the black window by accident**
All tools stopped. Just double-click `Start Bench Tools.bat` again. (Closing a browser *tab* is fine — the
tools keep running; reopen the tab from the dashboard.)

---



## 8. When to call an engineer

- A tool card never turns green even after restarting `Start Bench Tools.bat`.
- The page reports a **missing firmware image** or missing configuration.
- The same device fails the same step twice after a retry.
- You're unsure which profile / channel / IP a job should use.

