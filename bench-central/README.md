# Bench Central Collector

The durable home of bench run records (TEC-574, part of TEC-347). Every bench
station with `BENCH_CENTRAL_URL` set queues its per-run JSONs in a local
outbox and uploads them here over the tailnet (see `bench/README.md`,
"Central shipping"). Stations prune at 500 records per tool; **this database
never prunes** — it is the audit trail that answers "which devices were
configured last month, on which station, with what result".

- `collector.py` — the whole service: FastAPI + SQLite (WAL,
  `synchronous=FULL`). Ingest is idempotent on `run_id` (duplicate → 409, the
  uploader treats it as delivered), so uploader retries after a mid-flight
  network cut are free. The API contract is documented in the module
  docstring; the uploader's half lives in
  `bench/bench-core/src/bench_core/central.py`.
- No auth by design: the tailnet is the perimeter. The host has **no inbound
  security-group rules**; only Tailscale peers reach the port.

## Deploy (EC2 tailnet host, e.g. `techops-automations-host`)

Host decisions (from TEC-574): t3.micro (or t4g.micro — everything here runs
on ARM), 8–10 GB gp3, empty inbound security group, Tailscale installed and
logged in.

The checkout lives in ec2-user's home (`~/techops-tools`); the service runs as
`ec2-user` (see the unit for why) and the database lives OUTSIDE the checkout
in `/var/lib/bench-central`, so updating or even re-cloning the repo never
touches the data.

Python 3.10+ is required (`bench-core`'s floor — same as the bench). Amazon
Linux's stock `python3` is 3.9, and its bundled pip (21.x) predates modern
editable installs anyway, so install 3.11 and build the venv from it:

```bash
# as ec2-user
sudo dnf install -y python3.11
git clone https://github.com/Kela-Systems/techops-tools.git ~/techops-tools
cd ~/techops-tools/bench-central
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

sudo cp deploy/bench-central.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bench-central

curl http://127.0.0.1:8100/api/v1/health   # {"ok": true, "runs": 0, ...}
```

Then on each bench station set (before launching the bench):

```
BENCH_CENTRAL_URL=http://techops-automations-host:8100
```

To update the collector: `git -C ~/techops-tools pull`,
`sudo systemctl restart bench-central`.

## Data & backup

- The database lives at `/var/lib/bench-central/runs.db` (systemd
  `StateDirectory`), outside the repo checkout — a redeploy never touches it.
- Each row keeps the full record JSON verbatim plus queryable columns
  (station, tool, status, serial, operator, timestamps). Nothing is ever
  deleted by the service; there are no delete endpoints.
- Back up with SQLite's online backup (safe while the service runs):

```bash
sudo sqlite3 /var/lib/bench-central/runs.db ".backup /var/lib/bench-central/runs.backup.db"
```

## Browsing

**Dashboard (TEC-575):** open `http://techops-automations-host:8100/` in a
browser (any tailnet device). Read-only: search by serial/MAC/hostname/site/
operator, filter by station, device type, site, outcome and date range, and
click any run for its full detail — verification checks, warnings, and the
step log. No auth: reachability over the tailnet IS the access control.

The same data over the API:

```bash
curl 'http://techops-automations-host:8100/api/v1/runs?station_id=bench-1&status=error&limit=20'
curl 'http://techops-automations-host:8100/api/v1/runs?q=6008952944'
curl 'http://techops-automations-host:8100/api/v1/runs?since=2026-07-01&until=2026-08-01'
curl 'http://techops-automations-host:8100/api/v1/runs/<run_id>'   # full record, steps included
```

## Tests

```bash
cd bench-central
../bench/.venv/bin/python -m pytest        # needs httpx (see requirements-dev.txt)
```
