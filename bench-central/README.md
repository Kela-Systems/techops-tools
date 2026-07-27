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

```bash
# as ec2-user
sudo git clone https://github.com/Kela-Systems/techops-tools.git /opt/techops-tools
cd /opt/techops-tools/bench-central
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo useradd --system --home /var/lib/bench-central bench-central 2>/dev/null || true
sudo cp deploy/bench-central.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bench-central

curl http://127.0.0.1:8100/api/v1/health   # {"ok": true, "runs": 0, ...}
```

Then on each bench station set (before launching the bench):

```
BENCH_CENTRAL_URL=http://techops-automations-host:8100
```

To update the collector: `sudo git -C /opt/techops-tools pull`,
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

## Browsing without a UI (for now)

```bash
curl 'http://techops-automations-host:8100/api/v1/runs?station_id=bench-1&status=error&limit=20'
curl 'http://techops-automations-host:8100/api/v1/runs?since=2026-07-01&until=2026-08-01'
curl 'http://techops-automations-host:8100/api/v1/runs/<run_id>'   # full record, steps included
```

## Tests

```bash
cd bench-central
../bench/.venv/bin/python -m pytest        # needs httpx (see requirements-dev.txt)
```
