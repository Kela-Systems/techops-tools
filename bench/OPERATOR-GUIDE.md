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
4. **A label prints.** Stick it on the device.
5. When it says so, **unplug it** and plug in the next one.

> ⚠️ **Always unplug the finished device before plugging in the next.** Two devices on the
> cable at once will confuse detection.

> ⚠️ **No label, it doesn't ship.** A label only prints when the device passed
> its checks, so an unlabelled unit is not a unit that hasn't been labelled yet
> — it's a unit that hasn't passed. There is no "failed" label to look for; the
> missing label *is* the fail. If the page shows an amber **Label printer**
> warning, the printer is the problem, not the device: write the serial and the
> address on a blank label by hand before that unit goes in the shipping pile.

---



## 3. Golden rules

- Leave the black `Start Bench Tools.bat` window open the whole time.
- One device on the cable at a time.
- Wait for the green tick before unplugging.
- **Nothing leaves the bench without a label on it.**
- If a device is never detected, it's almost always plugged into the **wrong port / network**
for that device — Magos uses a different network from the Teltonika and camera tools.
- Each device ends up on a final `192.168.88.x` address. After that it will no longer answer on
the factory address — that's expected, it means the move worked. The tools also *look* for
finished units on those addresses, which is what makes **Verify** (section 9) work; on the radar
and the APU that means the bench adapter needs to reach `192.168.88.x` for a QA sweep.


### Choosing the address  *(camera, speaker, switch)*

Those three pages have an **Address assignment** box at the top. Pick a mode **once** and it stays
on for every device after it, including after a restart — set it at the start of a batch and then
just plug units in.

| Mode | What each device gets |
|---|---|
| **Same IP every time** | The one address you set. Every unit in the batch is identical — right when each one goes to a site that takes only one of them. |
| **Cycle** | The next address in the range, wrapping round at the top — for a site taking several. It only moves on when a device finishes cleanly, so a failed one keeps its number for the retry. |
| **Manual** | The address you type for that one unit. Type either the last number (`70`) or the whole thing (`192.168.88.70`). |
| **DHCP** | Nothing. The device is left asking the site's own network for an address. |

Not every tool offers all four — the switch has no **Cycle**, because a site takes one switch.

> **DHCP:** the bench doesn't know where the device will end up, so it finds it again by its MAC
> address to check it. **Your laptop has to be on the same network the device gets its address
> from**, or the tool will report that it couldn't find it again. If that happens, the device is
> almost certainly fine — but tell an engineer rather than shipping it unchecked.

> Whichever you pick is written into the device's record, so **Verify** later checks it against
> what that unit was actually given — not against the bench default.

---



## 4. Magos Radar & APU  *(network 192.168.40.x)*

Radar opens on its own card; APU on another. Plug the unit in, then choose its **channel**
(radar) or which **APU** it is (APU 0 or 1) and click configure. The tool sets the time and
the device's permanent IP, then reads the unit back on its new address and shows a table of
checks — the same table the **Verify** button produces (section 9).

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

**The label password is kept, so get it right.** On the OTD500, RUTM08 and TSW202 the bench sends
each unit's factory password to bench-central, filed under its serial. If that device is ever
factory-reset out in the field it goes back to that password, and this is the only place anyone
can look it up. Scanning is the reliable way to get it right — a mistyped password is stored as
typed, and only shows up as wrong months later when someone tries it on a reset unit. (If two
different passwords ever get recorded for one serial, the dashboard flags the row with an amber
`?` so an engineer can check the actual sticker.)

> ⚠️ **Firmware:** if the page warns the firmware image is missing, tell an engineer — the device
> can't be fully configured without it.

---



## 6. Raythink thermal camera  *(network 192.168.1.x)*

A fresh camera answers on `192.168.1.123`. Plug it in, then choose:

- **Profile** — `LAN` or `Cellular` (ask which the job needs).
- **Address** — see *[Choosing the address](#choosing-the-address)* below. Cameras offer all four
modes; **Cycle** is the usual one for a site taking several cameras, **Same IP every time** for a
batch of single-camera sites.

Click **Configure**. The tool sets the password, imports the profile, sets the time, the ONVIF
login, and **moves the camera to** `192.168.88.<number>` **as the last step**, then verifies it.

---



## 7. Provision-ISR speaker  *(DHCP — no fixed address)*

The speaker gets its address from DHCP, so there's nothing to type: plug it in and the tool
**finds it by itself** (it scans the bench networks — this can take a few seconds longer than the
other tools). When the page shows *Speaker detected* with its address, just click **Configure**.

The tool sets the password, the time server, uploads the announcement audio file, and **moves the
speaker to** `192.168.88.70` **as the last step**, then verifies it.

Where it ends up is your choice — see *[Choosing the address](#choosing-the-address)* below.
Speakers offer all four modes; `.70` is the default, and **Cycle** alternates `.70`/`.71` for a
site taking two.

> ⚠️ **One speaker at a time.** Unless you're cycling, every speaker ends up on the **same** final
> address, so finish and unplug one before connecting the next.

> **"Media file missing" on the page?** The announcement audio file isn't in place — tell an
> engineer. The speaker can't be fully configured without it.

---



## 8. Teltonika TSW202 switch  *(network 192.168.1.x)*

The simplest tool on the bench: **nothing to type but the password.** Plug the switch in, scan
the QR code on its sticker (or type the label password), and click **Configure**. There's no site
name — a switch isn't named after one.

The tool sets the password, checks the firmware, sets the time server and timezone, and **moves
the switch to** `192.168.88.2` **as the last step**, then verifies it.

Where it ends up is your choice — see *[Choosing the address](#choosing-the-address)* below.
Switches offer three of the four modes (no **Cycle**: a site takes one switch).

> ⚠️ **One switch at a time.** Every switch ends up on the **same** final address, so finish and
> unplug one before connecting the next.

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



## 9. Checking a finished device — **Verify**  *(nothing is changed)*

Next to **Configure** on every tool page there is a **Verify** button. It
checks a device that has **already** been configured and **changes nothing** — no password, no IP,
no reboot. Use it when you want to be sure a unit is right before it goes in the box.

**When to use it**

- **End of a batch.** Before boxing, plug each finished unit back in and press Verify.
- **A unit you're not sure about** — came back from a job, sat on the shelf, someone else did it.
- **After a failed run that you retried** and want a clean second opinion on.

**How to do it**

1. Plug the device in as usual, exactly as if you were going to configure it.
2. Wait for it to be **detected**. A device that was already provisioned lives on its final
   `192.168.88.x` address, and the tools look for it there too — the page will say so
   (*"already provisioned"* on the switch and router, *"already configured"* on the camera,
   *"finished radar / finished APU detected"* on the two Magos tools). On the radar and the APU the
   **Configure** button is greyed out for a finished unit, so a QA pass can't re-provision one by
   accident; only Verify is offered.
3. Click **Verify**. **There is nothing to type** — no site name, no label password. The tool
   looks up what this exact unit was configured to be and checks it against that.
4. Read the check table. **Verified ✓** at the top and all-green rows means ready to ship.

**Reading the result**

| What you see | What it means |
| --- | --- |
| Green **PASS** rows, header *Verified ✓* | The unit is what it should be. Unplug and box it. |
| Any red **FAIL** row | **Don't ship it.** See below. |
| Amber *skip* / *cannot confirm* rows | Not a failure. The tool is telling you it couldn't check that one thing (usually no signal at the bench) rather than pretending it's fine. |

The session counters keep the two kinds apart: **verified** / **verify failed** are their own
pills, and the history table's **Run** column says `verify` or `configure`. A verify pass does not
add to the day's **done** count — it isn't another device provisioned.

**If a row fails**

1. Leave the device plugged in and click **Configure** (that one *is* allowed to change things).
   On the Teltonika tools leave the label-password field **empty** — the unit is already on the
   shared password.
2. When it finishes, press **Verify** again.
3. Still red? Set that unit aside and tell an engineer which row failed. Don't ship it.

On the **radar and the APU** step 1 isn't available: those tools only configure a unit found on the
factory `192.168.40.x` address, so **Configure** stays greyed out for a finished one. Tell an
engineer instead of trying to force it — putting the unit back on its factory address means a
factory reset, which is their call, not a bench step.

**Two things worth knowing**

- **"prior run — none found on this station or in bench-central"** means nobody can say what this
  unit was supposed to be: it was never provisioned by us, or it was done on a station whose
  records never reached bench-central. Tell an engineer rather than guessing.
- **OTD500 without an antenna** will report the 4G row as *cannot confirm* — the modem hasn't
  attached to a network, so the tool won't claim it's on 4G. Expected at the bench.
- Verify needs the bench adapter to reach `192.168.88.x` (same as the last step of a normal run).
  If a finished unit is never detected, that's the usual cause.

---



## 10. Known issues & quick fixes

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

**Amber "Label printer" warning, and no label came out**
The device is fine — it passed, and it's configured. Only the printing failed. Check the
label printer is powered on, has a roll in it, and its cable is seated; the warning clears
by itself the next time a label prints. Until then, **label the units by hand** — serial and
address (or `DHCP` and the MAC) on a blank label. If the warning names a serial, that's the
one already done and waiting for a label. Tell an engineer if it doesn't come back.

The warning often says exactly what's wrong in brackets — *offline* (usually the USB cable
or the printer being switched off), *out of labels*, *paused*, *jammed*, *open*. Those five
you can fix yourself and carry on. Anything else, tell an engineer.

**Which roll goes in the printer**
**58 × 29 mm labels.** That's the only size the layout is built for. A different roll
won't jam, but it will print labels that are cut off at the edge or lost in white space —
either way not something to stick on a unit that's shipping.

**Labels come out as pages of code instead of labels**
The printer is installed with the wrong driver — it's printing the instructions rather than
following them. Stop and tell an engineer; it's a one-time setup fix, not something that
changes per device.

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



## 11. When to call an engineer

- A tool card never turns green even after restarting `Start Bench Tools.bat`.
- The page reports a **missing firmware image**, **missing media file**, or missing configuration.
- The same device fails the same step twice after a retry.
- A device **fails Verify** twice, or Verify says there's no record of it having been configured.
- You're unsure which profile / channel / IP a job should use.

