# Barcode scanner setup (Zebra DS2278)

The bench tools read a device's **factory label** instead of having the operator
retype the password off the sticker. This page is the one-time setup for the
scanner on a station, plus the self-test that proves it worked.

Written for the **DS2278** — cordless, sitting in a **CR2278** cradle. A corded
DS2208 works identically; it just skips the pairing step and has its own guide.

> **Why this page exists at all:** the scanner is a keyboard. A keyboard sends
> *key positions*, not characters, and the host decides which character each
> position means. Teltonika passwords are full of shift-dependent punctuation
> (`?`, `*`, `$`, `=`), so a station whose active keyboard layout is not the one
> the scanner assumes will silently deliver a *different* password — which shows
> up much later as an unexplained login failure. Everything below exists to
> close that gap and then prove it is closed.

## Use the cradle, not a direct Bluetooth pairing

A DS2278 can reach a host two ways. Take the first:

- **Scanner → cradle over Bluetooth, cradle → PC over USB.** The cradle appears
  to the PC as an ordinary USB keyboard, so the PC needs no pairing, no
  Bluetooth stack, and nothing to re-establish after a reboot. The scanner also
  charges whenever it is parked.
- **Scanner paired directly to the PC as a Bluetooth keyboard.** Avoid it: it has
  to be re-paired per host, survives reboots unreliably, and never charges.

## What the tools expect

| Setting | Value | Factory default | Why |
| --- | --- | --- | --- |
| USB device type | **HID Keyboard Emulation** | already this | No driver and no COM port. |
| Prefix | **`~`** (ASCII 126) | none | Marks the start of a scan so the page can tell a scan from typing, and route it away from whatever field has focus. |
| Suffix | **CR** (ASCII 13) | none | Terminates the scan. |
| Scan options | **`<PREFIX> <DATA> <SUFFIX>`** | data only | Actually transmits the two above. |
| USB Caps Lock Override | **Enable** | Disable | Stops a stray Caps Lock from inverting the password's case. |
| Keypad Emulation | **Enable** | already Enable | Sends characters as Alt+numpad sequences. See below. |
| Quick Keypad Emulation | **Disable** — on a scanner used with Windows | Enable | See below. This is the setting that actually makes the layout irrelevant, and the one that is not portable to macOS. |

Everything else stays at factory default. In particular leave **batch mode off**:
you want a scan to fail loudly when the scanner is out of range, not to be stored
and replayed later against whatever device happens to be on the bench. Leave
**Keypad Emulation with Leading Zero** at its default (Enable) too — that is the
`ALT 0 0 6 5` form, which is the one Windows reads through the ANSI codepage.

### Quick Keypad Emulation: the one setting that isn't portable

"Keypad Emulation" makes the scanner send each character as an **Alt + numeric
keypad** sequence instead of a key press. Windows composes that into the exact
character from the number alone, with the active keyboard layout never getting a
say — which is precisely the protection the bench PCs need: a station with Hebrew
(or any non-US) layout active still receives `?` as `?`.

It is enabled out of the box, so it is **not** the barcode you scan. The trap is
its companion:

> **"Quick" Keypad Emulation is also on by default, and it means Alt+numpad is
> used only for characters that are NOT on the keyboard.** Every character in a
> Teltonika password — letters, digits, `?` `$` `*` `=` `!` `-` `_` — *is* on the
> keyboard, so with Quick enabled they all still travel as ordinary key presses
> and are still mangled by a non-US layout. Table 8-1 lists both as Enable;
> page 8-13 says it outright: "disable Quick Keypad Emulation and enable Keypad
> Emulation".

So the Windows change is a single barcode: **Disable Quick Keypad Emulation**.

**macOS has no Alt+numpad composition, so once Quick is off the scanner produces
garbage there.** On a DS2278 that matters more than it looks, because:

> **Parameters live in the scanner, not the cradle.** Configure the scanner once
> and the settings travel with it to any cradle on any station — which is
> convenient across a fleet of Windows benches, and a trap if you carry the same
> scanner to a Mac.

So, with one scanner:

- **Configure it for Windows (Quick off) and leave it that way.** The Windows
  benches are what actually provision devices.
- **Don't scan on the Mac.** You do not need to: the whole flow is testable
  without hardware — `.venv/bin/python -m pytest` plus
  `.venv/bin/python scripts/scan-smoke.py`, which drives real scans through both
  tools over HTTP with simulated devices.
- If you *do* want to scan on the Mac, re-enable Quick Keypad Emulation first and
  disable it again before the scanner returns to a bench. The charset self-test
  below is what catches you forgetting.

One consequence worth knowing when you read a passing self-test: with Quick
*enabled* (the factory state), a payload made entirely of on-keyboard ASCII never
exercises Alt+numpad at all. A green charset self-test on a US-layout host
therefore proves the prefix, the suffix and the character set, and says nothing
about keypad emulation. Only a run with a non-US layout active tests that.

This asymmetry is safe because nothing in the bench code decodes keystrokes. The
capture reads the character content the OS *committed* to the input field, so it
cannot tell which encoding produced it. The only thing that changes between the
two modes is whether the host mangles characters, which is exactly what the
self-test measures.

## Configuring the scanner

Zebra scanners are configured by scanning barcodes out of the Product Reference
Guide. Do it in this order; the scanner beeps twice on accepting each one.

1. Get the **DS2278 Digital Scanner Product Reference Guide** (Zebra support
   site, free).
2. **Pair the scanner to its cradle:** plug the cradle into the PC by USB, then
   scan the **pairing barcode printed on the cradle**. Wait for the pairing
   beeps and a steady link indicator. Do this before anything else — a scanner
   with no link cannot confirm that it accepted the settings below.
3. **Set Defaults** (front of the *User Preferences* chapter) — start from a
   known state, so a scanner someone else has already fiddled with behaves the
   same as a new one. This does not unpair it.
4. In the **USB Interface** chapter (chapter 8 — the cradle is a USB device, so
   this is the right chapter; the *Bluetooth* chapter has near-identically named
   parameters that apply only to a scanner paired straight to a host):
   - **USB Caps Lock Override → Enable** (page 8-8; default is Disable)
   - **Disable Quick Keypad Emulation** (page 8-13) *(see the section above
     before you do this on a scanner you also use with a Mac)*
   - **USB Device Type** is already **HID Keyboard Emulation** and **Keypad
     Emulation** is already **Enable**, so neither needs scanning after a Set
     Defaults. Scan them only to recover a scanner someone else changed.
5. Prefix and suffix, from the **Data Formatting / Scan Options** chapter. Zebra
   takes these as a four-digit number scanned from the *Numeric Barcodes*
   appendix, using its **ASCII value + 1000** convention:
   - **Scan Prefix**, then `1`, `1`, `2`, `6` — `~` is ASCII 126
   - **Scan Suffix 1**, then `1`, `0`, `1`, `3` — CR is ASCII 13
   - **Scan Options → `<PREFIX> <DATA> <SUFFIX>`**, then the **Enter** barcode at
     the end of that section to commit
   The guide also carries a single **Add an Enter Key (CR/LF)** shortcut barcode.
   It only covers the suffix — you still need the prefix steps above.
6. Run the self-test below before letting the scanner near a real device.

`123Scan`, Zebra's config utility, can do all of this from a saved profile and is
worth it once you are setting up several stations. It is **Windows-only**, which
is why the barcode route above is the documented path — it works from the Mac too.

## Self-test: prove it before you trust it

Two QR codes under [`docs/scanner/`](scanner/), plus a printable sheet:

| File | Payload | Tests |
| --- | --- | --- |
| [`scanner/selftest-label.svg`](scanner/selftest-label.svg) | A realistic Teltonika sticker | The whole path end to end |
| [`scanner/selftest-charset.svg`](scanner/selftest-charset.svg) | Every shift-dependent ASCII punctuation mark | Keyboard-layout mangling |
| [`scanner/selftest-sheet.html`](scanner/selftest-sheet.html) | Both of the above, with instructions | Print it, keep it at the station |

Run it like this:

1. Open **`bench/scripts/scan-probe.html`** in a browser on the host you are
   setting up (straight off disk is fine).
2. Click the scan box and scan each code.
3. Paste the code's expected payload into the probe's *Expected payload* field.
4. Both must report **EXACT MATCH**.

The probe also reports which browser event carried the characters and the worst
inter-character gap — useful when the capture behaves oddly on a new host.

A mismatch is almost always one of these:

| Symptom | Cause |
| --- | --- |
| Punctuation wrong, letters fine | Host layout is being applied. On Windows, **Disable Quick Keypad Emulation**. On macOS, make sure it is *enabled*. |
| Letters come out Hebrew/Cyrillic | Non-Latin layout active. **Disable Quick Keypad Emulation** (Windows), or switch the layout to English. |
| Case inverted | Caps Lock is on and Caps Lock Override is not enabled. |
| Dropped characters, worse on longer payloads | Host cannot keep up. Set **USB Keystroke Delay → Medium** in the USB Interface chapter. |
| No `~` at the start | Prefix or Scan Options did not commit. Re-do step 5, ending with the **Enter** barcode. |
| Nothing happens, no Enter reported | Suffix missing. Re-do the Scan Suffix 1 steps. |

Cordless adds a few of its own:

| Symptom | Cause |
| --- | --- |
| Scanner beeps as if it read the code, but nothing arrives | No link to the cradle. Park it in the cradle, re-scan the cradle's pairing barcode, and check the cradle's USB cable. |
| Worked this morning, dead now | Flat battery. Park it — the cradle charges it. |
| A scan arrives much later, or several arrive at once | Batch mode is on. Turn it off: a queued scan can be replayed against a device that is no longer the one on the bench. |
| Intermittent drops at the far end of the bench | Out of Bluetooth range. Move the cradle, or scan closer to it. |

None of these can mis-provision a device — a lost scan arms nothing, and a
replayed one is caught by the MAC cross-check — but they all waste time.

The `~` prefix is a convenience, not a requirement — the tools also recognise a
payload that simply starts with `SN:`, so a scanner that has been reset still
works. You lose the ability to reliably distinguish a scan from typing, which is
what the prefix buys.

## Regenerating the self-test codes

The SVGs are committed; you only need this if a payload changes.

```bash
python3 -m venv /tmp/qrgen && /tmp/qrgen/bin/pip install segno
/tmp/qrgen/bin/python bench/docs/scanner/make-selftest-qr.py
```

`segno` is intentionally **not** a bench dependency — nothing at runtime encodes
QR codes, so it stays out of `requirements.txt`. If you change
`LABEL_PAYLOAD`, update the matching default in `bench/scripts/scan-probe.html`
too.

## What the label looks like

Teltonika stickers carry a semicolon-delimited key/value string. Two real ones:

```
OTD500   SN:6008219573;I:864088065513384;M:2097272B00F7;U:admin;PW:zZ?40*kA;B:015;
RUTM08   SN:6010212527;M:20972732F638;U:admin;PW:mL$7=b6N;B:039;
```

| Key | Meaning |
| --- | --- |
| `SN` | Serial number |
| `I` | IMEI — cellular devices only, so its absence distinguishes a RUTM08 from an OTD500 |
| `M` | LAN MAC address |
| `U` | Username (`admin`) |
| `PW` | Factory label password |
| `B` | Batch number |

`bench_core.device_label` parses this. Unknown keys are ignored rather than
rejected, so a new model that adds a field still scans.

## How a scan is used

Scan the label of the device you have just plugged in, on the OTD500 or RUTM08
tool page. Nothing else changes about the workflow: you still enter the site
name and press Configure.

- The password field switches to **"from scanned label"** and you do not type it.
- The scanned password stays on the server. It is never put into the page, and it
  is never written to a run record or shipped to bench-central.
- The label's MAC is checked against the MAC the tool independently read off the
  plugged-in device over ARP. **If they disagree the scan is refused** — that is
  the case where you have scanned the box next to the one that is plugged in.
- You can still type the password by hand; a typed value always wins.
