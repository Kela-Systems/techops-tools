# QA labels: the printed pass certificate

Every tool prints one label when a run ends **verified OK**, and prints nothing
otherwise. That asymmetry is the feature (TEC-352):

> **No label, it doesn't ship.**

Before this, a unit that passed and a unit that failed were indistinguishable
objects on the same bench. The label makes the pass physical, and — because
there is no REJECT label — the *absence* of one is the reject. A failed unit
looks exactly like a unit nobody has touched yet, which is the correct thing
for it to look like.

That is also why the printer is not allowed to be a gate on the bench itself:
if a missing label blocked provisioning, the first jammed roll would stop the
line, and the pressure would be to ship unlabelled. Instead a printer problem
is a warning and the operator labels by hand — see
[Hand-labelling](#when-nothing-prints).

## The gate

```python
entry["status"] == "ok" and entry.get("verified") is True
```

`is True`, not truthiness. The run-record schema has **three** verification
outcomes and the third one is the reason this is spelled out:

| `verified` | meaning | label |
| --- | --- | --- |
| `True` | every in-scope check passed | printed |
| `False` | at least one check failed | none |
| `None` | nothing was in scope, or the checks could not run | none |

`None` is "configured but NOT verified", a real end state on these tools (see
`MagosBench.verified_note`). A label is a claim that somebody checked, so a
unit nobody could check must not carry one — and a `if entry["verified"]:`
would have printed for `None` about as often as it mattered.

Both `configure` and `verify` runs print. A QA sweep with the **Verify** button
(section 9 of the operator guide) therefore re-prints the label for a unit whose
original was damaged, peeled or never printed, without touching the device.

## Stock and printer

Zebra ZD421, 203 dpi, on **58 × 29 mm** stock — 464 × 232 dots, and the design
uses all of it.

**The printhead is 832 dots wide and that is the first thing to check against
any change of stock.** The head is 104 mm at 203 dpi and the widest media the
printer accepts is 118 mm. The original design was 15 × 5 cm and declared
`^PW1199`; the labels came back correct at the left edge and progressively
absent to the right, the QR missing entirely, because everything past dot 832
simply had no head over it. No stock could have rescued it. `MAX_HEAD_DOTS` is
that limit as an assertion, and `test_the_design_fits_the_printhead` is the
test that would have caught it.

464 is comfortably inside the head, so this design prints its long axis
**across** the head with no rotation: design coordinates are printer
coordinates, and `_fo` is a formatting call rather than a transform. A stock
whose long axis exceeded 832 would have to rotate, which is the one reason `_fo`
still exists as its own function.

### What 58 mm costs

464 × 232 is under a quarter of the area the first design had, and the faces are
what absorbed the difference. Each carries a header, one hero and **at most two
supporting fields** above the barcode. Three things were given up:

- **The QR.** There is no room for it beside a barcode that already needs up to
  444 of the 448 usable dots, and the barcode is the one the DS2278 actually
  reads in the warehouse. `LabelContent` has no QR payload at all now.
- **The serial as a field.** It was never lost: the Code 128's human-readable
  line prints it under every face. Dropping the duplicate is what bought each
  face a second real field, and `test_no_face_prints_the_serial_twice` keeps it
  that way.
- **`SITE` on the OTD and RUTM faces.** Costless — the hostname beside it
  already ends in the site (`otd-kela-fob-12` against `kela-fob-12`).

The barcode is the tightest constraint on this stock. `barcode_module_width`
picks the widest module that still fits, and the real serials already span its
range: a Teltonika `6010212527` gets 3 dots per module, a speaker's
`TM-CS20-000001-XX` drops to 2 — 0.25 mm, the practical floor for a handheld
reader. A serial materially longer than the speaker's would not overflow so much
as quietly stop scanning, which is the failure mode to watch for if a new device
family arrives with longer serials.

The vertical budget is named at the top of
[`bench_core/qa_label.py`](../bench-core/src/bench_core/qa_label.py)
(`HERO_KEY_Y`, `HERO_Y`, `ROW1_Y`, `ROW1_BIG_Y`, `ROW2_Y`, `FOOT_Y`) rather than
scattered through the faces, because there is no slack to absorb a face that
drifts. The tests assert nothing runs off the media, because ZPL clips silently
rather than scaling, and a clipped barcode still looks like a barcode.

### Staying centred on media that is not

Nothing is drawn against a physical edge, and every face is printed with the
**same clearance on all four sides** — 14 dots across the head, 10 along the
feed. That is not tidiness: small labels do not sit perfectly square on the
roll, so the media wanders from side to side under the head and any asymmetry
in the design reads as a crooked print. The two axes differ because they fail
differently — the gap sensor keeps the feed direction tight, while side-to-side
is where the wander actually is, and where the vertical budget could least
afford to give anything up.

Two things had to be handled to get there, both of which look like the design's
fault and are actually the printer's:

- **The barcode is centred, not left-aligned.** `barcode_module_width` picks the
  widest module that fits, so the barcode's width jumps with the length of the
  serial. Left-aligned at `MARGIN`, a short serial left a visibly heavier gap on
  the right. It is also the one element allowed nearer the edge than `MARGIN`
  (`BAR_EDGE`, 10 dots): the longest real serial needs 444 of the 464 available
  and there is nowhere else for it to come from.
- **The barcode is anchored to the bottom edge, not to `FOOT_Y`.** `^BC` draws
  its own human-readable line and sizes it from the **module width** — `^CF` has
  no effect on it whatsoever, which is worth knowing before trying to make it
  smaller. So the line grows whenever a shorter serial earns a wider module, and
  with the top anchored the label finished 3 dots from the edge on some faces
  and 10 on others. `barcode_y` measures back from the bottom instead;
  `BAR_TEXT_DROP` holds the per-module heights, measured off renders because
  there is no way to derive them. `FOOT_Y` is then the *highest* the bars can
  ever start, so a body that clears it clears the barcode for every serial.

Module width is capped at 3 for the same reason: a wider module drags the
interpretation line taller with it, and a scanner that reads 3 comfortably gains
nothing from 4.

## Print quality

Three things decide whether a label is readable, none of which touch the
layout: whether the printer thinks there is a ribbon in the path (`^MT`), how
hard the head burns (`~SD`), and how fast the media moves (`^PR`). All three are
optional, and **omitted entirely when the station has not set them**, so a bench
that has never configured any of this keeps printing exactly as its printer is
set today.

```json
{
  "printer": {
    "media": "direct",
    "darkness": 24,
    "speed": 3
  }
}
```

They are sent with every label rather than left on the printer because a
printer's stored settings are invisible to the bench. A station whose darkness
had drifted low would print pale labels for as long as it took a person to
notice, and nothing in the tools could see it. Sent per job, print quality is a
property of the config: swap the printer and the labels come out the same.

`media` is the one that is not a matter of degree. `direct` (`^MTD`) is
heat-sensitive paper and no ribbon; `transfer` (`^MTT`) melts a ribbon onto
plain stock. Getting it wrong is not subtle — thermal-transfer mode on direct
thermal media puts a ribbon between the head and the paper, which insulates it,
and every label comes out **uniformly pale**. If that is the symptom, this is
the first thing to check, ahead of any amount of darkness.

Darkness is `~SD`, not `^MD`, because `^MD` adjusts *relative* to whatever the
printer is already set to and so inherits the very drift it is meant to remove.
Out-of-range values are clamped rather than rejected: a fat-fingered `300` in a
station file should still print labels. A bad `media` or a non-numeric darkness
is ignored for the same reason — a pale label is much easier to notice than a
bench that has stopped.

Reach for **speed before darkness** when print is weak. Slowing the printer
darkens the result just as effectively and is considerably kinder to the head.

### Choosing the darkness

Don't guess one value at a time. Print the ladder:

```bash
.venv/bin/python -m bench_core.qa_label --ladder \
    -o ladder.zpl bench-core/tests/label-records/speaker.json
```

That is the same real label seven times, at darkness 12 through 30, each one
printing its own setting where the mode normally goes. Send it to the printer,
then pick **the darkest rung whose barcode still scans** and put that number in
the station file.

It prints the real face rather than a test pattern on purpose, because both
failure directions end in an unscannable barcode: too light and the bars are too
faint to read, too dark and they bleed into each other. The only way to judge
that is with the actual barcode carrying an actual serial — and the speaker
record is the one worth testing, since its long serial forces the narrowest
module.

To try a single setting without editing the station file, the same flags work on
an ordinary dump: `--media`, `--darkness`, `--speed`.

### When the settings run out

Measured on the first 58 mm roll, darkness buys about 25% more ink between 12
and 25 and then **stops** — 28 and 30 are indistinguishable from 25. Speed 2 is
the slowest the printer goes. So `{"darkness": 25, "speed": 2}` is roughly the
ceiling of what the bench can do, and a label still weak at those values is not
going to be rescued by a number.

Two symptoms tell you to stop tuning and look at the hardware:

- **Solid fills come out speckled while bold text is fine.** Heat that is merely
  insufficient makes everything uniformly grey. Patchy solids next to legible
  text means the ink is not *adhering* evenly, which is a media, ribbon or
  printhead-contact problem.
- **The darkness ladder is flat.** If every rung looks the same, the limiting
  factor is not heat.

In that order: clean the printhead (a stock change drags fresh adhesive and dust
across it, and this costs a cotton bud and some isopropyl); confirm what the
media actually is; then check the ribbon is the right chemistry for the label
face — wax wants a matte paper face, and a gloss or synthetic face needs resin
or wax-resin, which is exactly the pairing that prints speckled.

Note that **direct thermal is not the easy way out here.** It needs no ribbon
and prints beautifully, but it fades within a year or so of heat and light, and
these labels are meant to identify a unit for as long as the unit exists. A
thermal-transfer resin ribbon is the right technology for the job; the work is
in pairing it correctly, not in escaping it.

### A note on hyphens in the preview

Hostnames render in Labelary with conspicuously wide gaps around their hyphens —
`otd-kela-fob-12` looks like `otd — kela — fob — 12`. Measured, each hyphen takes
31 dots against a 14-dot average for the other characters in the same string, so
it is padding rather than a wide glyph, and it is not coming from anything in
this module: the same string renders identically with and without `^FB`, byte
for byte. It is the viewer's substitute for Zebra's font 0. **Check a printed
label before treating it as real** — nothing here has been changed to chase it,
because the alternatives (a bitmapped font, or an explicit `^A0N` width) both
look worse on actual hardware.

### Configuring it

Nothing is required. With no configuration at all, a USB ZD421 is found by
queue name. To pin it down, add a `printer` block to `.bench-station.json` in
the bench root — a printer is station hardware, the same category as
`station_id`:

```json
{
  "station_id": "bench-01",
  "printer": { "host": "192.168.88.20", "port": 9100, "queue": "ZDesigner ZD421" }
}
```

Both keys are optional and either may be used alone. Per-process overrides:
`BENCH_PRINTER_HOST`, `BENCH_PRINTER_PORT`, `BENCH_PRINTER_QUEUE`.

Transports are tried in this order, and the first to answer is cached:

1. **`host` on port 9100** — raw ZPL to a networked ZD421, no driver involved.
2. **The Windows print queue** — a USB ZD421 through the spooler, written as a
   **RAW** job. This needs the **ZDesigner** driver (or *Generic / Text Only*).
   A driver that *renders* the job will print the ZPL as a page of source
   instead of a label; that is the single most likely setup mistake.

A named `queue` that is not installed resolves to *nothing*, deliberately —
rather than falling through to the name match. If the station names a printer
and that printer is gone, quietly using a different one puts labels somewhere
nobody is looking.

A failed discovery is remembered for 20 seconds, so an unplugged network
printer costs one connect timeout per twenty seconds instead of one per run.

### A queue is asked before it is written to

Windows keeps a print queue when its printer is unplugged, so the spooler will
accept a job for a ZD421 that is not there: `WritePrinter` succeeds, the record
says `printed: true`, and the whole backlog comes out days later when somebody
reconnects the cable. That is the only way this feature can fail in the
*unsafe* direction — claiming a label nobody has — so `queue_fault()` reads the
queue's status first and refuses to hand over a job when it is offline, paused,
out of labels, jammed, open, or in an error state. The unplugged-USB case is
usually the `PRINTER_ATTRIBUTE_WORK_OFFLINE` attribute rather than the status
word, so both are checked.

It **fails open**: an unreadable status is not evidence of a fault, and a
driver that will not answer must not stop a printer that is working. States
that mean a healthy printer is merely busy — `PRINTING`, `WARMING_UP`, `BUSY`,
`POWER_SAVE` — are not faults either. The bias only ever runs one way, because
a label wrongly reported as unprinted costs a hand-written sticker, while one
wrongly reported as printed is exactly the hole the gate exists to close.

## Hands-free modes (auto / cycle)

The Magos radar and APU can run unattended: plug a unit in, it configures
itself, unplug, next one. Printing sits inside that loop, so it is explicitly
bounded rather than merely expected to be quick.

The loop is safe by structure. `state["busy"]` is set before the run and
cleared after the print, and `poll_step` returns immediately while busy — so
the printer cannot race the detection state, and a print cannot be interrupted
half-way. Cycle mode advances its channel index on `entry["status"] == "ok"`,
which a print failure never changes, so **a broken printer cannot cost a unit
its channel or skip one.** Raythink's IP-octet counter is the same story: it
advances from `on_run_recorded`, after the print, gated on the run result.

What is *not* safe by structure is duration, and this is the reason for the
timeouts in `label_printer.py`. Because `busy` is held across the print, a
transport that blocks forever does not cost one label — it freezes the run.
The page sits on "Configuring…", and cycle mode stops advancing with no error
anywhere. The Windows spooler API is exactly that hazard: `OpenPrinter`,
`WritePrinter` and `EnumPrinters` take no timeout and block for as long as the
spooler service is wedged or the queue is paused with a full buffer.

So every printer call has a ceiling (`_bounded`), and the worst case a run can
pay is bounded at roughly:

| Path | Ceiling |
| --- | --- |
| TCP probe | 3 s (`CONNECT_TIMEOUT_SEC`) |
| TCP send | 3 s connect + 10 s send |
| Spooler queue listing | 5 s (`SPOOLER_LIST_TIMEOUT_SEC`) |
| Spooler send | 10 s (`SPOOLER_SEND_TIMEOUT_SEC`) |

A timed-out call is *abandoned*, not killed — a blocked native call cannot be
interrupted. The thread is a daemon, so it cannot hold the process open, and
the bench reports the label unprinted and carries on. With the 20-second
negative cache a dead printer costs at most one ceiling per twenty seconds, on
runs that take minutes; for scale, the Magos idle auto-disarm is 10 minutes and
unplug detection is 3 polls at 2 s, so neither is anywhere near being tripped
by a print.

`test_apu_label.py` asserts the cycle keeps advancing with both a missing
printer and a blocking one; `test_raythink_label.py` does the same for the
octet counter.

## The seven faces

The tools do genuinely different things to a device, so the useful largest
field differs. Each face leads with the identity **that tool actually wrote**,
and carries at most two supporting fields under it — the serial is not among
them, because the barcode already prints it.

| Tool | Face | Hero | Also | Why the hero |
| --- | --- | --- | --- | --- |
| OTD500 | `hostname-imei` | hostname | IMEI, MAC | no unique LAN address is written |
| RUTM08 | `hostname-gateway` | hostname | MAC, LAN band | every RUTM lands on the same LAN address, so it is not an identity |
| TSW202 | `shared-ip` | mgmt IP | MAC | no site and no hostname to lead with |
| IP speaker | `shared-ip` | final IP | MAC | it arrives on DHCP; the static address is the result worth printing |
| Raythink | `unit-ip` | IP | HOST, MAC | the per-unit octet is how ONVIF and the NVR find it |
| Magos radar (AR-300, channel confirmed) | `channel` | RF channel | MODEL, MAC | the system diagram says "radar 1", not an address |
| Magos radar (any other) | `shared-ip` | IP | MAC | only the AR-300 line has a channel at all |
| Magos APU | `pairing` | APU + radars | — | the only unit whose label has to name *other* devices |
| any of TSW / speaker / Raythink left on DHCP | `dhcp-mac` | the MAC | HOST | there is no address to print, and the MAC is how it gets found again |

The DHCP face makes the MAC the hero rather than printing the word DHCP over
it: the header already says DHCP, and the MAC is the only thing on that label
worth reading from arm's length.

DHCP is detected from `device.ip_mode == "dhcp"`, falling back to an empty
`device.ip`. All three of these tools record `ip_mode` since TEC-848, so the
explicit mode is checked first; the empty address stays as the fallback for
records written before that field existed. The record spells the mode `"dhcp"`
or `"static"` — TEC-848's four modes (fixed / cycle / manual / dhcp) collapse to
those two before reaching a record, so the check compares against `"dhcp"`
rather than negating `"static"`.

The address printed is always `device.ip`, never `device.reached_at`. Under
DHCP the latter records where the unit happened to answer when the run
finished; it belongs to the site's DHCP server, which may hand out a different
lease tomorrow. Printing it would put an address on the sticker that nobody
promised to keep.

### The radar channel: two fields, and only one of them prints

**Not every radar has a channel.** Only the AR-300 line does. That single fact
drives the whole design here, because `set_channel` *skips the step without
complaint* on a radar with no variants — so a perfectly successful run can
record the operator's pick on a unit that has no such setting and never
received one.

So a radar record carries two different things, and they must not be confused:

| Field | Meaning | Written when | Printed? |
| --- | --- | --- | --- |
| `device.channel` | the **pick** — the bench's intent | every configure run, including `"other"` for a typed address | **never** |
| `device.rf_channel` | the channel the radar **confirmed** | configure or verify, only when the `RF channel` row came back green | yes, as the hero |

`channel` has to stay in the record even when it was never applied: it is what
`resolve_expected()` hands a later verify pass as the expectation to check
against. Dropping it would silently delete a real check. But it is intent, not
fact, so `_rf_channel()` in the label reads `rf_channel` and never `channel`.

`rf_channel` comes from `confirmed_channel()`, which reads the `RF channel` row
the run already produced — no extra API call. A configure run gets that row
from its closing re-read (`recheck_radar_at`), a verify run from the pass
itself. Green only, so all of these print the address instead:

* a radar with no RF channel (non-AR-300, or firmware with no `listVariants`);
* a manual-IP run — `resolve_target` stores `"other"`, and the run deliberately
  never touches the channel;
* an AR-300 that would not report what channel it is on (amber);
* an AR-300 found on a *different* channel than the record expects (red) —
  the case where printing the pick would be actively dangerous.

Note what `rf_channel` is and is not. Because the row is green only on an exact
match, its value always equals the expectation, which came from the pick. The
device read is a **gate on printing**, not the origin of the digit. That gate
is the entire point: it is what keeps a frequency off the sticker of a radar
that is not on it.

The tempting shortcut is deriving the channel from the address, since channel N
maps to `.5N`. Don't. A radar answering at `.51` need not be transmitting on
chan1 — it may have no channel setting at all. This is the same caution
`VARIANT_KEYS` is written with: "a wrong read here would put a green tick on
the wrong frequency."

The **APU** is a different story and reads `device.channel` happily, via
`_apu_index()`. An APU has no RF channel and no `RF channel` row; its 0/1 is
just the slot the operator picked, and the slot is what chose the IP. Nothing
about the hardware is being claimed by printing it. A manual-IP APU keeps the
`pairing` face when radar addresses were supplied, minus the index — those
addresses really were written.



### What is never printed

Not the password, `password_source`, the **firmware version**, the operator
name, the station id, `bench_version`, or anything Tailscale. The firmware one
is easy to miss and worth stating: it changes after the label is stuck on, so
printing it manufactures a document that is wrong later.
`test_qa_label.py` asserts every one of these against every face.

### The barcode

Code 128, carrying the bare serial, which the DS2278 already on the bench reads
in the warehouse. It is encoded by the **printer** (`^BC`), not in Python: a
placeholder encoder would have shipped labels whose codes look right and scan
as nothing, whereas handing the payload to printer firmware means the symbol is
either real or absent.

There is no QR. It did not survive the move to 58 mm stock — see
[What 58 mm costs](#what-58-mm-costs).

## Reviewing a face without hardware

The design lives in [`bench-label-designs.canvas.tsx`](bench-label-designs.canvas.tsx)
(committed here, not on a laptop — that was the durability gap the issue
flagged). To see what the printer will actually do:

```bash
.venv/bin/python -m bench_core.qa_label \
    bench-core/tests/label-records/magos-apu.json
```

Paste the output into <https://labelary.com/viewer.html> set to **8 dpmm** and
**2.28 × 1.14 in**, which is 58 × 29 mm. Nothing is rotated, so what the viewer
shows is the reading orientation.

To render every face at once, use the API:

```bash
curl -s -X POST --data-binary @label.zpl -H "Accept: image/png" \
    http://api.labelary.com/v1/printers/8dpmm/labels/2.28x1.14/0/ -o label.png
```

The trailing `/0/` is the label index, so `/1/` is the second label in a
multi-label dump.

There is one sample record per face in
[`bench-core/tests/label-records/`](../bench-core/tests/label-records/), and a
wildcard dumps all of them at once — expanded by the tool, not the shell, so
the same command works on the bench station:

```powershell
.venv\Scripts\python -m bench_core.qa_label -o all.zpl `
    bench-core\tests\label-records\*.json
```

Use `-o` rather than `>` on Windows. PowerShell 5.1 redirects as UTF-16 with a
BOM, and a ZD421 fed that prints a page of nothing recognisable — a confusing
failure to hit while checking whether the labels themselves are right. `-o`
writes ASCII, which is all ZPL ever is here.

To exercise the whole print path on a machine with no printer, point it at a
file:

```bash
BENCH_PRINTER_SINK=/tmp/labels.zpl ./start-bench.sh
```

Every label that would have printed is appended to that file, which is itself a
stack of `^XA…^XZ` labels that labelary renders.

> One preview artifact to ignore: labelary draws hyphens very wide at large
> font sizes, so `otd-kela-fob-12` previews as `otd — kela — fob — 12`. The ZPL
> contains a plain ASCII hyphen (the barcode's own text, set smaller from the
> same font, shows it normally). Worth confirming on the first real print.

## When nothing prints

Never fatal. A verified device is verified whether or not a printer answered,
and failing the run would throw away a good provision over a USB cable. So a
failure shows up in two places instead:

**In the run record**, as a `label` block:

```json
"label": {"printed": false, "face": "shared-ip", "target": "queue:ZDesigner ZD421",
          "error": "...", "at": "2026-08-30T11:26:47+00:00"}
```

An **absent** `label` key is different from `{"printed": false}`, and the
difference matters when reading records back: absent means the run did not earn
a label, `printed: false` means it earned one and did not get it. Only the
second needs someone to act.

**In the UI**, as an amber banner under the header, from `mountPrinterWarn()`
in the shared `bench.js` — so all seven tools get it without seven edits:

- at startup, if no printer answers: *"No label printer detected — units must
  be labelled by hand before they ship."*
- after a pass that did not print: *"SN GSAC657352 passed but no label was
  printed — label it by hand before shipping."*

The second names the serial, because by the time an operator reads it they may
have moved on to the next unit. It clears on the next successful print, and
never on its own — a banner that outlives its problem teaches people to ignore
banners. Failures also go to the tool's rolling log at WARNING.

The bracketed reason (`the print queue is offline`, `out of labels`, …) is
carried forward to the units that follow, via `_last_fault`. It has to be: a
failed send starts the 20-second negative cache, so the *next* unit finds no
transport at all and would otherwise be told the generic "no printer detected"
— which sends an operator to check the configuration when the real answer was
"check the cable". Over a batch they would see the useless message far more
often than the useful one. A successful print forgets it.

Hand-labelling is a legitimate fallback, not a workaround: write the serial and
the address (or `DHCP` + MAC) on a blank label. The rule the printed label
enforces is the one that has to hold either way — an unlabelled unit does not
leave the bench.
