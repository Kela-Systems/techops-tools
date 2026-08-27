# Kela Bench — Operator Guide

How to provision devices at the bench. Keep this open on the side screen.

> This is the operator-facing version of the guide. The same content is shown on the
> bench dashboard at **Operator guide** (`launcher/guide.html`). If something here
> doesn't match the screen, tell an engineer so it can be updated.

---

## 1. Starting the bench

1. Double-click **Kela Bench Tools** on the desktop (it may also appear as
   `Start Bench Tools.bat`).
2. A black window opens and stays open — **leave it open all day.** Closing it stops every tool.
   It first checks for a tools update — if the engineers released one, it installs
   itself here; no internet at the bench just means it starts with what it has.
3. After a few seconds the **dashboard** opens in the browser with seven tool cards.
4. Click the card for whatever you're about to plug in. A **green dot** means that tool is ready.
5. **Enter your name** in the **Operator** box at the top of the tool page (scan your badge or
   type it, then press Enter). Do this once at the start of the day — it's shared by all the
   tools and is recorded with every device you configure. The box glows amber until it's set.

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

Radar opens on its own card; APU on another. Plug the unit in, then choose its **channel**
(radar) or which **APU** it is (APU 0 or 1) and click configure. The tool sets the time and
the device's permanent IP, then verifies it.

**Radar: channel → address** (4 radars per system)


| Channel | Radar address |
| ------- | ------------- |
| 0       | 192.168.88.50 |
| 1       | 192.168.88.51 |
| 2       | 192.168.88.52 |
| 3       | 192.168.88.53 |


**APU: which APU → address + its two radars** (2 APUs per system — each controls two radars)


| APU | APU address   | Controls radars                                 |
| --- | ------------- | ----------------------------------------------- |
| 0   | 192.168.88.60 | radar_0 (192.168.88.50) + radar_1 (192.168.88.51) |
| 1   | 192.168.88.61 | radar_2 (192.168.88.52) + radar_3 (192.168.88.53) |


> **APU firmware:** APUs must already run firmware **3.1.2**. The tool refuses an older unit and
> asks you to upgrade it manually via its dashboard first — nothing is changed on a refused unit.

> **Manual ("other") IP:** if you type your own IP instead of picking a channel, the device goes
> to **exactly** that address and the RF channel is left unchanged. Double-check the number — a
> typo sends the unit to the wrong address.

**Note:** This workflow is optimized for the Magos AR300 model and the cUAS version, but it works for different Magos models as well. for SR-500 and SR-1000 models, use only the Manual workflow.

**Auto & Cycle (hands-free) modes**

- **Auto** — set one target once; every unit you plug in gets configured to it automatically.
- **Cycle** — radars are configured in groups of four, each taking the next channel (0 → 1 → 2
→ 3, then back to 0); APUs in groups of two (APU 0 → APU 1, then back to 0). Just keep
plugging them in.

Both skip a unit that briefly re-appears (so unplug each one), and both **turn themselves off
after 10 minutes with nothing plugged in** — the page shows a message; just switch the mode back
on to continue.

---



## 5. Teltonika OTD500 & RUTM08 routers  *(network 192.168.1.x)*

Plug the router in. The page reads it and asks for two things:

- **Site name** — e.g. `kela-fob-123`. The device is named after it (`otd-kela-fob-123` / `rut-kela-fob-123`).
- **Label password** — the factory password printed on the device's sticker.

Click **Configure**. The tool does everything else (password, name, time, firmware, RMS, Tailscale,
checks) and, for the RUTM08, **moves the LAN to** `192.168.88.1` **as the last step**.

### Scan the label instead of typing the password

If the station has a barcode scanner, **scan the QR code on the device's sticker** instead of
typing the password. The field turns green and says *"Password from the scanned label"* with the
serial number. You still type the site name and click **Configure** as usual.

You don't have to aim at the password box — scan whenever the device form is on screen.

> ⚠️ **"Scanned label rejected — scan the label on the device that is actually connected"**
> means the sticker you scanned belongs to a *different* device than the one plugged in. This is
> the scanner doing its job: check you scanned the right box, then scan again. Nothing was
> configured.

If a scan doesn't work at all, or the password is rejected right after a scan, just type the
password by hand — typing always wins — and tell an engineer, because the scanner probably needs
re-setting up.

> **Re-running a device that was already configured?** Leave the label-password field **empty** —
> it's already on the shared password. Don't scan the label; the factory password no longer applies.
> The RUTM08 page can also pick it up again on its new `192.168.88.1` address.

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



## 7. Provision-ISR speaker  *(DHCP — no fixed address)*

The speaker gets its address from DHCP, so there's nothing to type: plug it in and the tool
**finds it by itself** (it scans the bench networks — this can take a few seconds longer than the
other tools). When the page shows *Speaker detected* with its address, just click **Configure**.

The tool sets the password, the time server, uploads the announcement audio file, and **moves the
speaker to** `192.168.88.70` **as the last step**, then verifies it.

> ⚠️ **One speaker at a time.** Every speaker ends up on the **same** final address
> (`192.168.88.70`), so finish and unplug one before connecting the next.

> **"Media file missing" on the page?** The announcement audio file isn't in place — tell an
> engineer. The speaker can't be fully configured without it.

---



## 8. Teltonika TSW202 switch  *(network 192.168.1.x)*

The simplest tool on the bench: **nothing to type but the password.** Plug the switch in, scan
the QR code on its sticker (or type the label password), and click **Configure**. There's no site
name — a switch isn't named after one.

The tool sets the password, checks the firmware, sets the time server and timezone, and **moves
the switch to** `192.168.88.2` **as the last step**, then verifies it.

> ⚠️ **One switch at a time.** Every switch ends up on the **same** final address
> (`192.168.88.2`), so finish and unplug one before connecting the next.

> **Re-running a switch that was already configured?** Leave the label-password field **empty** —
> it's already on the shared password. The page also picks it up again on its new `192.168.88.2`
> address.

> **Firmware** is different here from the routers: the switch only gets flashed if it arrived with
> an **older** version than the bench standard. A switch that shipped with a newer one is left
> alone, and the page says so in the *Firmware step* column. That's a normal, passing result — not
> something to report.

> ⚠️ **Switch never detected at all?** A few TSW202 units are the **PROFINET** variant, which ships
> with no address and can't be found by this tool. Check the box/label — if it says PROFINET, tell
> an engineer; it needs a one-off setup step first. Any other switch that isn't detected is the
> usual causes (wrong port, not booted yet).

---



## 9. Known issues & quick fixes

**A tool card stays grey**
The tool isn't up yet. On the first start of the day it's still installing — wait a minute and it
turns green. If it never turns green, close the black window and run `Start Bench Tools.bat` again.

**Device plugged in but never detected**
Wrong network for that device. Magos units use the `192.168.40.x` port; Teltonika and camera units
use the `192.168.1.x` port. Make sure it's the right cable/port, fully seated, and the device has
finished booting (give it ~30 seconds). Try once more before asking for help.
The **speaker** is different — it uses DHCP, so it just needs a port whose network hands out
addresses; give it up to a minute to get one after plugging in.

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

**The scanner beeps but nothing appears on the page**
The scanner read the code but couldn't send it. Put it back in its cradle for a few seconds — that
both charges it and re-establishes the link — then scan again. If it still does nothing, type the
password by hand and tell an engineer. **Park the scanner in its cradle whenever you're not using
it**; that's how it charges, and a flat scanner is the most common reason it goes quiet.

**The label lands in the site-name box, and the page starts configuring on its own**
The page is running an old copy of itself, left over from before the tools were updated. Press
**Ctrl+Shift+R** on that tab and scan again. Worth mentioning to an engineer if it happens on a
station more than once.

---



## 10. When to call an engineer

- A tool card never turns green even after restarting `Start Bench Tools.bat`.
- The page reports a **missing firmware image**, **missing media file**, or missing configuration.
- The same device fails the same step twice after a retry.
- You're unsure which profile / channel / IP a job should use.

