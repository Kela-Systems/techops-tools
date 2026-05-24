#!/usr/bin/env python3
"""Deployment Time Tracker – interactive CLI for timing deployment steps."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ── Manifest / catalog merging ──────────────────────────────────────────────

def load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_catalog_path(manifest_path: str, catalog_rel: str) -> str:
    return str(Path(manifest_path).parent / catalog_rel)


def merge_manifest(manifest_path: str) -> dict:
    """Merge device catalog + deployment manifest into a resolved plan."""
    manifest = load_yaml(manifest_path)
    deployment = manifest.get("deployment", {})

    catalog_path = deployment.get("catalog")
    catalog = {}
    if catalog_path:
        abs_catalog = resolve_catalog_path(manifest_path, catalog_path)
        catalog = load_yaml(abs_catalog).get("device_types", {})

    resolved_devices: dict[str, dict] = {}

    for dev_key, dev_conf in manifest.get("devices", {}).items():
        dev_type = dev_conf.get("type")

        if dev_type:
            if dev_type not in catalog:
                raise ValueError(
                    f"Device '{dev_key}' references unknown catalog type '{dev_type}'"
                )
            base = copy.deepcopy(catalog[dev_type])
            name = dev_conf.get("name", base["name"])
            steps = base["steps"]
        else:
            name = dev_conf.get("name", dev_key)
            steps = copy.deepcopy(dev_conf.get("steps", []))
            dev_type = None

        # Apply overrides
        for step_id, overrides in dev_conf.get("overrides", {}).items():
            for step in steps:
                if step["id"] == step_id:
                    step.update(overrides)
                    break
            else:
                raise ValueError(
                    f"Device '{dev_key}': override references unknown step '{step_id}'"
                )

        # Remove steps
        remove_ids = set(dev_conf.get("remove_steps", []))
        steps = [s for s in steps if s["id"] not in remove_ids]

        # Extra steps
        for extra in dev_conf.get("extra_steps", []):
            after = extra.pop("after", None)
            if after:
                idx = next(
                    (i for i, s in enumerate(steps) if s["id"] == after), None
                )
                if idx is not None:
                    steps.insert(idx + 1, extra)
                else:
                    steps.append(extra)
            else:
                steps.append(extra)

        resolved_devices[dev_key] = {
            "name": name,
            "type": dev_type,
            "steps": steps,
        }

    # System-wide steps as a pseudo-device
    system_steps = manifest.get("system_steps", [])
    if system_steps:
        resolved_devices["system"] = {
            "name": "System-wide",
            "type": None,
            "steps": copy.deepcopy(system_steps),
        }

    return {
        "deployment": deployment,
        "devices": resolved_devices,
    }


# ── State tracking ───────────────────────────────────────────────────────────

class StepState:
    def __init__(self, device_key: str, step: dict, menu_num: int):
        self.device_key = device_key
        self.step_id: str = step["id"]
        self.name: str = step["name"]
        self.expected_minutes: float = step.get("expected_minutes", 0)
        self.menu_num = menu_num

        self.status: str = "pending"  # pending | in_progress | completed | skipped
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.notes: str = ""

    @property
    def actual_minutes(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds() / 60.0
        return 0.0

    @property
    def delta_minutes(self) -> float:
        return self.actual_minutes - self.expected_minutes


class BreakRecord:
    def __init__(self):
        self.started_at: datetime = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None
        self.note: str = ""

    @property
    def duration_minutes(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds() / 60.0


class DeploymentSession:
    def __init__(self, plan: dict, deployer: str, dry_run: bool):
        self.plan = plan
        self.deployer = deployer
        self.dry_run = dry_run

        self.deployment_info = plan["deployment"]
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None

        self.steps: list[StepState] = []
        self.breaks: list[BreakRecord] = []
        self.active_break: BreakRecord | None = None
        self.active_steps: list[StepState] = []

        menu_num = 1
        for dev_key, dev in plan["devices"].items():
            for step in dev["steps"]:
                self.steps.append(StepState(dev_key, step, menu_num))
                menu_num += 1

    @property
    def total_expected(self) -> float:
        return sum(s.expected_minutes for s in self.steps)

    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "completed")

    @property
    def sum_of_steps_minutes(self) -> float:
        return sum(s.actual_minutes for s in self.steps if s.status == "completed")

    @property
    def total_break_minutes(self) -> float:
        return sum(b.duration_minutes for b in self.breaks)

    @property
    def wall_clock_minutes(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds() / 60.0

    @property
    def active_wall_clock_minutes(self) -> float:
        return self.wall_clock_minutes - self.total_break_minutes

    @property
    def parallel_overlap_minutes(self) -> float:
        overlap = self.sum_of_steps_minutes - self.active_wall_clock_minutes
        return max(0.0, overlap)


# ── Display helpers ──────────────────────────────────────────────────────────

def fmt_minutes(m: float) -> str:
    if m < 0:
        return "-" + fmt_minutes(-m)
    h = int(m) // 60
    mins = int(m) % 60
    if h > 0:
        return f"{h}h {mins:02d}m"
    return f"{mins}m"


def fmt_delta(delta: float) -> str:
    if delta > 0:
        return f"+{fmt_minutes(delta)} over"
    elif delta < 0:
        return f"-{fmt_minutes(-delta)} under"
    return "on target"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_menu(session: DeploymentSession):
    info = session.deployment_info
    name = info.get("name", "Deployment")
    version = info.get("version", "?")
    total = len(session.steps)
    done = session.completed_count

    steps_str = fmt_minutes(session.sum_of_steps_minutes)
    break_str = fmt_minutes(session.total_break_minutes)

    print(f"\n{'=' * 70}")
    print(f"  {name} v{version} | {done}/{total} steps done | "
          f"Steps: {steps_str} | Breaks: {break_str}")
    print(f"{'=' * 70}")

    if session.active_steps:
        print(f"\n  ACTIVE TIMERS ({len(session.active_steps)}):")
        for ss in session.active_steps:
            elapsed = (datetime.now(timezone.utc) - ss.started_at).total_seconds() / 60.0
            exp = fmt_minutes(ss.expected_minutes)
            print(f"    [{ss.menu_num:>2}] {ss.name} "
                  f"(running {fmt_minutes(elapsed)}, exp: {exp})")
        print()

    current_device = None
    for ss in session.steps:
        if ss.device_key != current_device:
            current_device = ss.device_key
            dev = session.plan["devices"][current_device]
            dev_name = dev["name"]
            dev_done = sum(
                1 for s in session.steps
                if s.device_key == current_device and s.status == "completed"
            )
            dev_total = sum(
                1 for s in session.steps if s.device_key == current_device
            )
            print(f"\n  {current_device} ({dev_name})  [{dev_done}/{dev_total} done]")

        num = f"{ss.menu_num:>4}"
        if ss.status == "completed":
            actual = fmt_minutes(ss.actual_minutes)
            exp = fmt_minutes(ss.expected_minutes)
            print(f"  {num}. [DONE {actual:>5} / {exp}] {ss.name}")
        elif ss.status == "skipped":
            print(f"  {num}. [SKIP] {ss.name}")
        elif ss.status == "in_progress":
            elapsed = (datetime.now(timezone.utc) - ss.started_at).total_seconds() / 60.0
            print(f"  {num}. [>>> {fmt_minutes(elapsed):>5}] {ss.name}")
        else:
            exp = fmt_minutes(ss.expected_minutes)
            print(f"  {num}. [ ] {ss.name} (exp: {exp})")

    print()
    if session.active_break:
        elapsed = fmt_minutes(session.active_break.duration_minutes)
        print(f"  ** ON BREAK ({elapsed} so far) – press 'b' to end break **")
        print()

    print("  Commands: <number> start | d <number> [note] done | "
          "c <number> cancel")
    print("           b: break | s <number>: skip | q: finish")
    print()


# ── Interactive loop ─────────────────────────────────────────────────────────

def find_step_by_num(session: DeploymentSession, num: int) -> StepState | None:
    return next((s for s in session.steps if s.menu_num == num), None)


def complete_step(session: DeploymentSession, ss: StepState, note: str = ""):
    ss.finished_at = datetime.now(timezone.utc)
    ss.status = "completed"
    if note:
        ss.notes = note
    session.active_steps = [s for s in session.active_steps if s is not ss]

    actual = fmt_minutes(ss.actual_minutes)
    exp = fmt_minutes(ss.expected_minutes)
    delta = fmt_delta(ss.delta_minutes)
    print(f"\n  Done: \"{ss.name}\" in {actual} (expected {exp}, {delta})")


def run_interactive(session: DeploymentSession):
    session.started_at = datetime.now(timezone.utc)
    print(f"\nDeployment started at {session.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Total expected clean-run time: {fmt_minutes(session.total_expected)}")

    while True:
        clear_screen()
        print_menu(session)

        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Use 'q' to finish the deployment properly.")
            continue

        if not raw:
            continue

        cmd = raw.lower()

        # d <number> [note] — mark a running step as done
        if cmd.startswith("d "):
            parts = raw.split(None, 2)
            if len(parts) >= 2 and parts[1].isdigit():
                num = int(parts[1])
                note = parts[2] if len(parts) > 2 else ""
                target = find_step_by_num(session, num)
                if target and target.status == "in_progress":
                    complete_step(session, target, note)
                    if not session.dry_run:
                        time.sleep(1.2)
                else:
                    print(f"  Step {num} is not currently running.")
                    time.sleep(0.8)
            else:
                print("  Usage: d <number> [note]")
                time.sleep(0.8)
            continue

        # c <number> — cancel a running step back to pending
        if cmd.startswith("c "):
            parts = raw.split()
            if len(parts) == 2 and parts[1].isdigit():
                num = int(parts[1])
                target = find_step_by_num(session, num)
                if target and target.status == "in_progress":
                    target.status = "pending"
                    target.started_at = None
                    session.active_steps = [
                        s for s in session.active_steps if s is not target
                    ]
                    print(f"  Cancelled: {target.name}")
                    if not session.dry_run:
                        time.sleep(0.8)
                else:
                    print(f"  Step {num} is not currently running.")
                    time.sleep(0.8)
            else:
                print("  Usage: c <number>")
                time.sleep(0.8)
            continue

        # b — toggle break
        if cmd == "b":
            if session.active_break:
                brk = session.active_break
                brk.ended_at = datetime.now(timezone.utc)
                note = input("  Break note (optional): ").strip()
                if note:
                    brk.note = note
                session.breaks.append(brk)
                session.active_break = None
                dur = fmt_minutes(brk.duration_minutes)
                print(f"\n  Break ended ({dur})")
                if not session.dry_run:
                    time.sleep(1)
            else:
                session.active_break = BreakRecord()
                print("\n  Break started. Press 'b' again to end it.")
                if not session.dry_run:
                    time.sleep(1)
            continue

        # q — finish deployment
        if cmd == "q":
            if session.active_break:
                brk = session.active_break
                brk.ended_at = datetime.now(timezone.utc)
                brk.note = brk.note or "ended with deployment"
                session.breaks.append(brk)
                session.active_break = None

            # Auto-complete any still-running steps
            if session.active_steps:
                print(f"  {len(session.active_steps)} step(s) still running:")
                for ss in session.active_steps:
                    print(f"    [{ss.menu_num}] {ss.name}")
                action = input("  Complete them now? (y/n): ").strip().lower()
                if action == "y":
                    for ss in list(session.active_steps):
                        complete_step(session, ss)
                else:
                    continue

            pending = [s for s in session.steps if s.status == "pending"]
            if pending:
                confirm = input(
                    f"  {len(pending)} steps still pending. "
                    f"Finish anyway? (y/n): "
                ).strip().lower()
                if confirm != "y":
                    continue

            session.finished_at = datetime.now(timezone.utc)
            break

        # s <number> — skip a step
        if cmd.startswith("s "):
            parts = raw.split()
            if len(parts) == 2 and parts[1].isdigit():
                num = int(parts[1])
                target = find_step_by_num(session, num)
                if target and target.status == "pending":
                    reason = input(f"  Skip reason for \"{target.name}\": ").strip()
                    target.status = "skipped"
                    target.notes = reason or "skipped"
                    print(f"  Skipped: {target.name}")
                    if not session.dry_run:
                        time.sleep(0.8)
                else:
                    print("  Invalid step number or step not pending.")
                    time.sleep(0.8)
            else:
                print("  Usage: s <number>")
                time.sleep(0.8)
            continue

        # <number> — start a step timer
        if raw.isdigit():
            num = int(raw)
            target = find_step_by_num(session, num)
            if not target:
                print("  Invalid step number.")
                time.sleep(0.8)
                continue
            if target.status != "pending":
                print(f"  Step already {target.status}.")
                time.sleep(0.8)
                continue

            target.status = "in_progress"
            target.started_at = datetime.now(timezone.utc)
            session.active_steps.append(target)
            print(f"  Started: {target.name}")
            if not session.dry_run:
                time.sleep(0.8)
            continue

        print("  Unknown command. Try: <number>, d <num>, c <num>, b, s <num>, q")
        time.sleep(0.8)


# ── JSON report ──────────────────────────────────────────────────────────────

def generate_report(session: DeploymentSession) -> dict:
    info = session.deployment_info

    devices_report = {}
    for dev_key, dev in session.plan["devices"].items():
        dev_steps = [s for s in session.steps if s.device_key == dev_key]
        devices_report[dev_key] = {
            "name": dev["name"],
            "type": dev.get("type"),
            "steps": [
                {
                    "id": s.step_id,
                    "name": s.name,
                    "expected_minutes": s.expected_minutes,
                    "actual_minutes": round(s.actual_minutes, 2),
                    "delta_minutes": round(s.delta_minutes, 2),
                    "status": s.status,
                    "notes": s.notes,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                }
                for s in dev_steps
            ],
        }

    # Aggregate per device type
    type_stats: dict[str, dict] = {}
    for dev_key, dev_data in devices_report.items():
        dev_type = dev_data.get("type") or dev_data.get("name", dev_key)
        if dev_type not in type_stats:
            type_stats[dev_type] = {
                "name": dev_data["name"],
                "count": 0,
                "actual_minutes": 0.0,
                "expected_minutes": 0.0,
                "completed_steps": 0,
                "total_steps": 0,
            }
        stats = type_stats[dev_type]
        stats["count"] += 1
        for step in dev_data["steps"]:
            stats["total_steps"] += 1
            stats["expected_minutes"] += step.get("expected_minutes", 0)
            if step.get("status") == "completed":
                stats["completed_steps"] += 1
                stats["actual_minutes"] += step.get("actual_minutes", 0)

    for stats in type_stats.values():
        stats["actual_minutes"] = round(stats["actual_minutes"], 2)
        stats["expected_minutes"] = round(stats["expected_minutes"], 2)

    return {
        "deployment": info.get("name", "Unknown"),
        "version": info.get("version", "Unknown"),
        "deployer": session.deployer,
        "dry_run": session.dry_run,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "finished_at": session.finished_at.isoformat() if session.finished_at else None,
        "wall_clock_minutes": round(session.wall_clock_minutes, 2),
        "active_wall_clock_minutes": round(session.active_wall_clock_minutes, 2),
        "sum_of_steps_minutes": round(session.sum_of_steps_minutes, 2),
        "expected_total_minutes": round(session.total_expected, 2),
        "total_break_minutes": round(session.total_break_minutes, 2),
        "parallel_overlap_minutes": round(session.parallel_overlap_minutes, 2),
        "breaks": [
            {
                "started_at": b.started_at.isoformat(),
                "ended_at": b.ended_at.isoformat() if b.ended_at else None,
                "duration_minutes": round(b.duration_minutes, 2),
                "note": b.note,
            }
            for b in session.breaks
        ],
        "device_type_summary": type_stats,
        "devices": devices_report,
    }


def save_report(report: dict, dry_run: bool) -> str:
    base_dir = Path(__file__).parent / "reports"
    if dry_run:
        base_dir = base_dir / "dry_run"
    base_dir.mkdir(parents=True, exist_ok=True)

    name_slug = report["deployment"].lower().replace(" ", "_")
    version = report["version"].replace(".", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{name_slug}_{version}_{ts}.json"
    filepath = base_dir / filename

    with open(filepath, "w") as f:
        json.dump(report, f, indent=2)

    return str(filepath)


# ── Google Sheets export ─────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return load_yaml(str(config_path))
    return {}


def _get_or_create_worksheet(sh, title: str, rows: int = 100, cols: int = 30):
    """Get a worksheet by title, creating it if it doesn't exist."""
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def _last_data_row(worksheet) -> int:
    """Return the 1-based index of the last row that has content, or 0."""
    all_values = worksheet.get_all_values()
    for i in range(len(all_values) - 1, -1, -1):
        if any(str(cell).strip() for cell in all_values[i]):
            return i + 1
    return 0


def _ensure_headers(worksheet, expected_headers: list[str]):
    """Ensure row 1 has the expected headers. Replace if stale."""
    all_values = worksheet.get_all_values()
    if all_values:
        current = all_values[0][: len(expected_headers)]
        if current == expected_headers:
            return
    worksheet.update(
        values=[expected_headers], range_name="A1", value_input_option="RAW",
    )


def _build_device_breakdown(report: dict) -> str:
    """Build a compact multi-line string summarizing per-device-type stats."""
    type_summary = report.get("device_type_summary", {})
    if not type_summary:
        return ""

    lines = []
    for stats in type_summary.values():
        name = stats["name"]
        count = stats["count"]
        actual = round(stats["actual_minutes"], 1)
        expected = round(stats["expected_minutes"], 1)
        completed = stats["completed_steps"]
        total = stats["total_steps"]
        lines.append(
            f"{name} x{count}: {actual} / {expected} min "
            f"({completed}/{total} steps)"
        )
    return "\n".join(lines)


def export_to_gsheet(report: dict, config: dict):
    sheets_config = config.get("google_sheets", {})
    key_file = sheets_config.get("key_file")
    sheet_id = sheets_config.get("sheet_id")

    if not key_file or not sheet_id:
        print("  Google Sheets not configured (missing key_file or sheet_id in config.yaml). Skipping.")
        return

    key_path = Path(__file__).parent / key_file
    if not key_path.exists():
        print(f"  Service account key not found at {key_path}. Skipping Google Sheets export.")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("  gspread / google-auth not installed. Skipping Google Sheets export.")
        print("  Install with: pip install gspread google-auth")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(key_path), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    # ── Summary tab ──────────────────────────────────────────────────────
    summary_ws = _get_or_create_worksheet(sh, "Summary")
    summary_headers = [
        "Date", "Deployer", "Site", "Version",
        "Expected (min)", "Wall-clock (min)", "Active (min)",
        "Steps Total (min)", "Break (min)", "Overlap (min)",
        "Delta (min)", "Delta %", "Completion", "Device Breakdown", "Notes",
    ]
    _ensure_headers(summary_ws, summary_headers)

    expected = report["expected_total_minutes"]
    active_wc = report.get("active_wall_clock_minutes", 0)
    delta = active_wc - expected
    delta_pct = round((delta / expected * 100) if expected > 0 else 0, 1)

    total_steps = sum(len(d["steps"]) for d in report["devices"].values())
    completed_steps = sum(
        1 for d in report["devices"].values()
        for s in d["steps"] if s.get("status") == "completed"
    )

    all_notes = []
    for dev_key, dev_data in report["devices"].items():
        dev_name = dev_data.get("name", dev_key)
        for step in dev_data["steps"]:
            if step.get("notes"):
                all_notes.append(f"{dev_name}/{step['name']}: {step['notes']}")

    summary_row = [
        report.get("started_at", "")[:10],
        report.get("deployer", ""),
        report.get("deployment", ""),
        report.get("version", ""),
        round(expected, 1),
        round(report.get("wall_clock_minutes", 0), 1),
        round(active_wc, 1),
        round(report.get("sum_of_steps_minutes", 0), 1),
        round(report.get("total_break_minutes", 0), 1),
        round(report.get("parallel_overlap_minutes", 0), 1),
        round(delta, 1),
        delta_pct,
        f"{completed_steps}/{total_steps}",
        _build_device_breakdown(report),
        "; ".join(all_notes),
    ]
    next_row = _last_data_row(summary_ws) + 1
    if next_row > summary_ws.row_count:
        summary_ws.add_rows(next_row - summary_ws.row_count)
    summary_ws.update(
        values=[summary_row], range_name=f"A{next_row}", value_input_option="RAW",
    )

    # ── Steps tab ────────────────────────────────────────────────────────
    steps_ws = _get_or_create_worksheet(sh, "Steps")
    steps_headers = [
        "Date", "Site", "Version", "Deployer",
        "Device Instance", "Device Type", "Step",
        "Expected (min)", "Actual (min)", "Delta (min)",
        "Status", "Notes",
    ]
    _ensure_headers(steps_ws, steps_headers)

    date = report.get("started_at", "")[:10]
    site = report.get("deployment", "")
    version = report.get("version", "")
    deployer = report.get("deployer", "")

    step_rows = []
    for dev_key, dev_data in report["devices"].items():
        dev_name = dev_data.get("name", dev_key)
        dev_type = dev_data.get("type") or dev_name
        for step in dev_data["steps"]:
            actual = round(step.get("actual_minutes", 0), 1) if step.get("status") == "completed" else ""
            delta_val = round(step.get("delta_minutes", 0), 1) if step.get("status") == "completed" else ""
            step_rows.append([
                date, site, version, deployer,
                dev_key, dev_type, step["name"],
                step.get("expected_minutes", 0),
                actual, delta_val,
                step.get("status", "pending"),
                step.get("notes", ""),
            ])

    if step_rows:
        next_row = _last_data_row(steps_ws) + 1
        needed = next_row + len(step_rows) - 1
        if needed > steps_ws.row_count:
            steps_ws.add_rows(needed - steps_ws.row_count)
        steps_ws.update(
            values=step_rows, range_name=f"A{next_row}", value_input_option="RAW",
        )

    print(f"  Exported to Google Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}")


# ── End-of-deployment summary ────────────────────────────────────────────────

def print_summary(session: DeploymentSession):
    info = session.deployment_info
    name = info.get("name", "Deployment")

    wall = fmt_minutes(session.wall_clock_minutes)
    active_wc = fmt_minutes(session.active_wall_clock_minutes)
    steps_sum = fmt_minutes(session.sum_of_steps_minutes)
    brk = fmt_minutes(session.total_break_minutes)
    exp = fmt_minutes(session.total_expected)
    overlap = fmt_minutes(session.parallel_overlap_minutes)
    delta = session.active_wall_clock_minutes - session.total_expected

    print(f"\n{'=' * 70}")
    print(f"  Deployment \"{name}\" complete!")
    print(f"{'=' * 70}")
    print(f"  Wall-clock time:    {wall} (expected {exp}, {fmt_delta(delta)})")
    print(f"    Active work:      {active_wc} (breaks excluded)")
    print(f"    Cumulative steps: {steps_sum} ({overlap} of parallel overlap)")
    print(f"    Break time:       {brk} ({len(session.breaks)} break(s))")

    if session.breaks:
        print()
        for i, b in enumerate(session.breaks, 1):
            dur = fmt_minutes(b.duration_minutes)
            note = f" – {b.note}" if b.note else ""
            print(f"    Break {i}: {dur}{note}")

    # Bottlenecks
    overruns = sorted(
        [s for s in session.steps if s.status == "completed" and s.delta_minutes > 0],
        key=lambda s: s.delta_minutes,
        reverse=True,
    )
    if overruns:
        print(f"\n  Bottlenecks (sorted by overrun):")
        for i, s in enumerate(overruns[:10], 1):
            actual_str = fmt_minutes(s.actual_minutes)
            exp_str = fmt_minutes(s.expected_minutes)
            delta_str = fmt_minutes(s.delta_minutes)
            print(f"    {i}. {s.name:<40} +{delta_str}  ({actual_str} vs {exp_str})")

    skipped = [s for s in session.steps if s.status == "skipped"]
    if skipped:
        print(f"\n  Skipped steps ({len(skipped)}):")
        for s in skipped:
            note = f" – {s.notes}" if s.notes else ""
            print(f"    - {s.name}{note}")

    print()


# ── CLI entry point ──────────────────────────────────────────────────────────

def cmd_run(args):
    plan = merge_manifest(args.manifest)
    session = DeploymentSession(plan, args.deployer, args.dry_run)

    if args.dry_run:
        print("** DRY RUN MODE – steps complete instantly, no Sheet export **")

    run_interactive(session)
    clear_screen()
    print_summary(session)

    report = generate_report(session)
    filepath = save_report(report, args.dry_run)
    print(f"  Report saved: {filepath}")

    if not args.dry_run:
        config = load_config()
        if config.get("google_sheets"):
            try:
                export_to_gsheet(report, config)
            except Exception as e:
                print(f"  Google Sheets export failed: {e}")
                print(f"  You can retry later with: python deploy_tracker.py upload {filepath}")

    print()


def cmd_upload(args):
    """Upload a saved JSON report to Google Sheets."""
    report_path = Path(args.report)
    if not report_path.exists():
        print(f"Report file not found: {report_path}")
        sys.exit(1)

    with open(report_path) as f:
        report = json.load(f)

    if report.get("dry_run"):
        print("Warning: this is a dry-run report.")
        confirm = input("Upload anyway? (y/n): ").strip().lower()
        if confirm != "y":
            return

    config = load_config()
    if not config.get("google_sheets"):
        print("Google Sheets not configured. Add google_sheets section to config.yaml.")
        sys.exit(1)

    export_to_gsheet(report, config)


def cmd_validate(args):
    """Validate a manifest + catalog merge without running."""
    try:
        plan = merge_manifest(args.manifest)
    except Exception as e:
        print(f"Validation FAILED: {e}")
        sys.exit(1)

    total_steps = sum(len(d["steps"]) for d in plan["devices"].values())
    total_exp = sum(
        s.get("expected_minutes", 0)
        for d in plan["devices"].values()
        for s in d["steps"]
    )
    info = plan["deployment"]
    print(f"Manifest OK: {info.get('name')} v{info.get('version')}")
    print(f"  Devices: {len(plan['devices'])}")
    print(f"  Steps:   {total_steps}")
    print(f"  Expected clean-run: {fmt_minutes(total_exp)}")

    for dev_key, dev in plan["devices"].items():
        dev_exp = sum(s.get("expected_minutes", 0) for s in dev["steps"])
        print(f"    {dev_key} ({dev['name']}): {len(dev['steps'])} steps, {fmt_minutes(dev_exp)}")
        for step in dev["steps"]:
            print(f"      - {step['id']}: {step['name']} ({step.get('expected_minutes', 0)}m)")


def main():
    parser = argparse.ArgumentParser(
        description="Deployment Time Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run
    run_parser = subparsers.add_parser("run", help="Run a deployment session")
    run_parser.add_argument("manifest", help="Path to deployment manifest YAML")
    run_parser.add_argument("--deployer", default="Unknown", help="Name of the deployer")
    run_parser.add_argument("--dry-run", action="store_true", help="Dry-run mode (instant steps, no Sheet export)")
    run_parser.set_defaults(func=cmd_run)

    # upload
    upload_parser = subparsers.add_parser("upload", help="Upload a JSON report to Google Sheets")
    upload_parser.add_argument("report", help="Path to a saved JSON report file")
    upload_parser.set_defaults(func=cmd_upload)

    # validate
    val_parser = subparsers.add_parser("validate", help="Validate manifest + catalog without running")
    val_parser.add_argument("manifest", help="Path to deployment manifest YAML")
    val_parser.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
