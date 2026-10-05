# arrow-lake-mesa

Fix software rendering on **Arrow Lake** operator stations (Intel GPU `8086:7d67`) running the
Ubuntu 22.04 station image (TEC-880). Mesa 23.2.1 on 22.04 doesn't support that GPU, so Chrome and
the desktop draw on the CPU: frozen or slow screens and stuck video, and a reboot doesn't help.
A newer Mesa from `ppa:kisak/turtle` moves the drawing back to the GPU.

**Support runbook (PDF):** [`Arrow-Lake-Station-Fix.pdf`](Arrow-Lake-Station-Fix.pdf), built from [`guide.html`](guide.html).

```bash
./arrow-lake-mesa.sh check   kela-fob-08-operator   # read-only snapshot: GPU, Mesa, CPU pressure, Chrome's GPU report
./arrow-lake-mesa.sh apply   kela-fob-08-operator   # before-snapshot, install Mesa, reboot (asks first)
./arrow-lake-mesa.sh verify  kela-fob-08-operator   # after-snapshot, compared with before → PASS / FAIL
./arrow-lake-mesa.sh rollback kela-fob-08-operator  # back to Ubuntu's Mesa
```

- Runs from a laptop (macOS bash 3.2 or Linux) over SSH as `KelaAdmin` (`ARROW_USER` to change).
  sudo asks for the KelaAdmin password on the station; the script never stores or passes it.
- `apply` refuses unless the GPU is `8086:7d67`, the OS is 22.04 and the kernel driver is `i915`,
  and does nothing on a station that already renders on the GPU.
- The reboot takes the station's screens down for ~2–3 min — coordinate with the site.
- Snapshots are saved to `./arrow-lake-reports/` (`ARROW_REPORT_DIR`); attach them to the ticket.
- Video decoding stays on the CPU (no Arrow Lake media driver on 22.04) — that comes with the
  24.04 image (TEC-910).

Rebuild the PDF after editing `guide.html`:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-pdf-header-footer \
  --print-to-pdf="$PWD/Arrow-Lake-Station-Fix.pdf" "file://$PWD/guide.html"
```
