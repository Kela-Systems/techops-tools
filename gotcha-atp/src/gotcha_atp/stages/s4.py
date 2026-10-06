"""S4 — Versions vs release.yaml. What the unit runs, against the fleet pins.

Sources (all reads):
  * hub: SystemService.GetStatus (S4.1), VideoAnalyticsConfigService
    ListAvailableModels / ListConfigs (S4.11)
  * server: the node-published kela-integration-images ConfigMap, the pods'
    images and image volumes, the Gotcha manifest's hosting_requirements,
    `crictl inspecti` for the magos-agent image label, kela-node-controller
    --version, kubectl's server version
  * Fleet: the site's desired vs running release, the node's desired system
    configuration, package digest → tag (models)
  * the S0.6 inventory for every device firmware, bench-central for the
    bench record of every discovered serial

A pin left empty or "TODO" records the value and leaves that sub-check amber.
A device S0.6 could not identify is "not checked" in its own sub-check only,
so one unreachable part never hides the versions of the others.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Iterator, Optional

import httpx

from ..benchcentral import BenchCentral
from ..context import Context
from ..discovery import Part
from ..fleet import Fleet
from ..model import Checks, Row, RowSpec, StageSpec, amber
from ._common import grpc_error, guarded, hub
from .s5 import PSQL

STAGE = StageSpec(
    id="S4", name="Versions vs release.yaml", depends_on=("S0.3",),
    vantage="server + devices (S0.6 inventory) + hub gRPC + Fleet + bench-central (tailnet)",
    rows=(
        RowSpec("S4.1", "Hub release",
                "GetStatus.version == release.yaml.hub.image_tag; Fleet running == desired release",
                "critical", depends_on=("S3.5",)),
        RowSpec("S4.2", "Integration images",
                "staged bundle == Fleet's desired release; staged integration digests == the node's "
                "Fleet images report; int-gotcha runs the staged images; magos-agent (manifest and "
                "running image) == release.yaml.hub.magos_agent",
                "high", "read-back"),
        RowSpec("S4.3", "Node controller and k3s",
                "== Fleet desired system configuration (the node installs exactly that); "
                "≥ release.yaml.server.node_controller_floor / k3s_floor",
                "medium"),
        RowSpec("S4.4", "APU firmware identical and pinned",
                "same on both; == release.yaml.magos.apu_firmware", "high"),
        RowSpec("S4.5", "Radar firmware identical across the 4 radars",
                "one version; == release.yaml.magos.radar_firmware", "high"),
        RowSpec("S4.6", "Camera firmware and generation",
                "REST generation; build date ≥ release.yaml.camera.firmware floor", "medium"),
        RowSpec("S4.7", "Network gear firmware (warning only)",
                "RUTM/OTD == release.yaml.teltonika pins; TSW202 ≥ floor; IGS-4215 ≥ "
                "release.yaml.planet.firmware_floor — mismatch is a warning", "medium"),
        RowSpec("S4.8", "Operator station build", "== release.yaml.operator.setup_version",
                "medium", "read-back"),
        RowSpec("S4.9", "Every part passed the bench",
                "latest configure record per serial in bench-central has verified=true",
                "high", "read-back"),
        RowSpec("S4.10", "Speaker firmware", "== release.yaml.speaker.firmware — warning", "medium"),
        RowSpec("S4.11", "Default detection model",
                "default model == release.yaml.hub.detector.model; every mounted copy is "
                "detector.version — warning", "medium", "read-back", depends_on=("S3.5",)),
    ),
)

INTEGRATION_IMAGES = ("sudo -n k3s kubectl -n kela get cm kela-integration-images "
                      "-o jsonpath='{.data.images\\.json}'")
GOTCHA_MANIFEST_SQL = ("select coalesce(json_agg(hosting_requirements), '[]') from manifests "
                       "where name ilike '%gotcha%'")
# Per pod: containers (spec image + running imageID) and image volumes —
# filtered on the server so the transfer stays small.
PODS = r"""sudo -n k3s kubectl -n kela get pods -o json | python3 -c '
import json, sys
out = []
for p in json.load(sys.stdin)["items"]:
    st = {c["name"]: c.get("imageID", "") for c in p.get("status", {}).get("containerStatuses") or []}
    out.append({"name": p["metadata"]["name"],
                "containers": [{"name": c["name"], "image": c["image"], "image_id": st.get(c["name"], "")}
                               for c in p["spec"]["containers"]],
                "image_volumes": [{"name": v["name"], "ref": v["image"].get("reference", "")}
                                  for v in p["spec"].get("volumes") or [] if "image" in v]})
print(json.dumps(out))'"""
K3S_VERSION = ("sudo -n k3s kubectl version -o json 2>/dev/null | python3 -c "
               "'import json,sys; print(json.load(sys.stdin)[\"serverVersion\"][\"gitVersion\"])'")
OCI_VERSION = "org.opencontainers.image.version"


# ── pure helpers (covered by tests/test_s4.py) ──────────────────────────────

def version_digits(s: str) -> tuple:
    """'TSW2_R_00.01.07.1' → (0, 1, 7, 1); () when there is no dotted version."""
    m = re.search(r"\d+(?:\.\d+)+", s or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def versions_match(a: str, b: str) -> bool:
    """Same dotted version, whatever the prefix ('RUTM_R_00.07.24.3' vs '00.07.24.3')."""
    va, vb = version_digits(a), version_digits(b)
    return bool(va) and va == vb


def version_at_least(current: str, floor: str) -> bool:
    """Ordered by the dotted numbers; False when either side has none — "could
    not tell" must not read as "new enough"."""
    a, b = version_digits(current), version_digits(floor)
    return bool(a) and bool(b) and a >= b


def k3s_key(version: str) -> tuple:
    """'v1.36.2+k3s1' → (1, 36, 2, 1): the +k3sN build counts too."""
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)(?:\+k3s(\d+))?", version or "")
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def planet_key(version: str) -> tuple:
    """IGS-4215 '1.305b260324' → (1, 305, 260324): the build date after the 'b'
    is the ordering key (every 1.305bNNNNNN build has the same dotted part)."""
    m = re.search(r"(\d+)\.(\d+)b(\d+)", version or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def planet_at_least(current: str, floor: str) -> bool:
    a, b = planet_key(current), planet_key(floor)
    return bool(a) and bool(b) and a >= b


def fw_build_date(version: str) -> Optional[date]:
    """The build date a Raythink firmware string ends with (both generations)."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", version or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def camera_legacy(version: str) -> bool:
    """The RPC2 generation's firmware reads like '1.000.General 00.0.T, build: 2025-04-09'."""
    return bool(re.search(r"General|build:", version or ""))


def digest_of(ref: str) -> str:
    """'repo/path@sha256:abc' → 'sha256:abc'; '' when the ref carries no digest."""
    return ref.rsplit("@", 1)[1] if "@" in (ref or "") else ""


def repo_path(ref: str) -> str:
    """'<registry>/integrations/gotcha@sha256:…' → 'integrations/gotcha' (the
    key the node-controller publishes images under)."""
    path = (ref or "").split("@", 1)[0]
    path = path.split("/", 1)[1] if "/" in path and "." in path.split("/", 1)[0] else path
    return path.rsplit(":", 1)[0] if ":" in path.rsplit("/", 1)[-1] else path


def manifest_image_tag(hosting: list, repo: str) -> Optional[str]:
    """The tag the manifest asks for, e.g. 'kela-integration://vendor/magos-agent:3.25.6'."""
    for h in hosting or []:
        for svc in (h or {}).get("services") or []:
            m = re.fullmatch(rf"kela-integration://{re.escape(repo)}:(\S+)", str(svc.get("image") or ""))
            if m:
                return m.group(1)
    return None


def short(digest: str) -> str:
    return digest[:19] + "…" if len(digest) > 20 else digest or "—"


# ── data, read once per stage ───────────────────────────────────────────────

def _server(ctx: Context) -> dict:
    if "_s4" in ctx.facts:
        return ctx.facts["_s4"]
    s = ctx.server_sections({
        "images": INTEGRATION_IMAGES,
        "pods": PODS,
        "manifest": f"{PSQL} \"{GOTCHA_MANIFEST_SQL}\"",
        "node_controller": "kela-node-controller --version",
        "k3s": K3S_VERSION,
    }, timeout=90)
    d: dict = {"errors": {}}
    for key, parse in (("images", json.loads), ("pods", json.loads), ("manifest", json.loads)):
        r = s[key]
        try:
            d[key] = parse(r.out) if r.ok else None
        except ValueError:
            d[key] = None
        if d[key] is None:
            d["errors"][key] = ctx.redact((r.text.splitlines() or [f"rc {r.rc}"])[-1][:160]) or "no output"
    m = re.search(r"(\d+\.\d+\.\d+\S*)", s["node_controller"].text)
    d["node_controller"] = m.group(1) if s["node_controller"].ok and m else ""
    d["k3s"] = s["k3s"].text if s["k3s"].ok else ""
    ctx.facts["_s4"] = d
    return d


def _fleet(ctx: Context) -> Fleet:
    return Fleet(ctx.creds.fleet_url, ctx.creds.fleet_token)


def _fleet_node(ctx: Context) -> Optional[dict]:
    """The node S0.5 found in Fleet, or None (Fleet not configured / not adopted)."""
    return ctx.facts.get("fleet_node")


def _inventory(ctx: Context) -> Optional[list[Part]]:
    inv = ctx.facts.get("inventory")
    if inv is None:
        return None
    fields = Part.__dataclass_fields__
    return [Part(**{k: v for k, v in p.items() if k in fields}) for p in inv]


def _parts(ctx: Context, spec: RowSpec, *kinds: str) -> tuple[list[Part], Optional[Row]]:
    inv = _inventory(ctx)
    if inv is None:
        return [], amber(spec, "device discovery (S0.6) did not run")
    return [p for p in inv if p.kind in kinds], None


def _unreadable(p: Part) -> Optional[str]:
    """Why S0.6 has no firmware for this part, or None when it has one."""
    if p.error:
        return f"not identified in S0.6 ({p.error})"
    if p.firmware in ("", "unknown", None):
        return "reported no firmware in S0.6"
    return None


# ── rows ────────────────────────────────────────────────────────────────────

def _s41(ctx: Context) -> Row:
    spec = STAGE.spec("S4.1")
    c = Checks()
    version = (ctx.facts.get("hub_status") or {}).get("version")
    if version is None:
        from ..access.grpc import pb
        try:
            st = hub(ctx).stub("kela.system.v1alpha1.system_pb2_grpc", "SystemServiceStub").GetStatus(
                pb("kela.system.v1alpha1.system_pb2").GetStatusRequest(), timeout=15)
            version = st.version
        except Exception as e:  # noqa: BLE001
            return amber(spec, "GetStatus: " + ctx.redact(grpc_error(e)))
    want = ctx.release.pin("hub.image_tag")
    c.add("hub version", (version == want) if want else None,
          f"{version or '—'}" + ("" if version == want else f" (want {want or 'unpinned'})"))
    fleet, node = _fleet(ctx), _fleet_node(ctx)
    if not fleet.enabled or node is None:
        c.items.append({"label": "Fleet release", "ok": True,
                        "actual": "not compared — Fleet not configured or node not found (S0.5)"})
        return c.row(spec, version=version)
    try:
        rel = fleet.site_release(node.get("siteId") or ctx.unit.site)
    except (httpx.HTTPError, ValueError) as e:
        c.add("Fleet release", None, ctx.redact(f"Fleet unreachable: {type(e).__name__}: {e}"))
        return c.row(spec, version=version)
    desired, running = rel.get("desired") or {}, rel.get("running") or {}
    same = bool(desired.get("digest")) and desired.get("digest") == running.get("digest")
    label = desired.get("tag") or desired.get("branch") or "—"
    c.add("Fleet running == desired", same,
          f"{label} ({short(running.get('digest') or '')})" if same else
          f"running {short(running.get('digest') or '')}, desired {label} {short(desired.get('digest') or '')}")
    return c.row(spec, version=version, fleet_release={"desired": {k: desired.get(k) for k in
                                                                    ("tag", "variant", "channel", "commit", "branch", "digest")},
                                                       "running": running})


def _images_report(ctx: Context) -> tuple[Optional[dict], str]:
    """({bundle, desired_tag, images {repo: digest}} | None, why) from Fleet:
    the bundle the node staged its images from, the release Fleet wants the
    site on, and the integration image digests the node reported."""
    fleet, node = _fleet(ctx), _fleet_node(ctx)
    if not fleet.enabled or node is None:
        return None, "not compared — Fleet not configured or node not found (S0.5)"
    try:
        detail = fleet.node(node["id"])
        desired = (fleet.site_release(node.get("siteId") or ctx.unit.site).get("desired") or {}).get("tag")
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return None, ctx.redact(f"Fleet unreachable: {type(e).__name__}")
    rep = detail.get("imagesReport") or {}
    images = {i["name"]: digest_of(i.get("image", "")) for i in rep.get("images") or []
              if i.get("role") == "integration" and i.get("name")}
    return {"bundle": rep.get("bundleTag") or "", "desired_tag": desired or "", "images": images}, ""


def _s42(ctx: Context) -> Row:
    spec = STAGE.spec("S4.2")
    d = _server(ctx)
    c = Checks()
    staged = d.get("images")
    if staged is None:
        c.add("kela-integration-images", None, d["errors"].get("images", "unreadable"))
        staged = {}
    report, note = _images_report(ctx)
    if report is not None:
        desired = report["desired_tag"]
        c.add("staged bundle", (report["bundle"] == desired) if desired else None,
              f"{report['bundle'] or '—'}" + ("" if report["bundle"] == desired else
                                             f" (Fleet desired release {desired or 'unknown'})"))
    integration_repos = sorted(r for r in staged if r.startswith(("integrations/", "vendor/")))
    if staged and not any(r.startswith("integrations/gotcha") for r in integration_repos):
        c.add("integrations/gotcha", False, "not staged on this node")
    for repo in integration_repos:
        got = digest_of(staged[repo])
        if report is None:
            c.add(repo, None, f"{short(got)} — {note}")
        elif repo not in report["images"]:
            c.add(repo, None, f"{short(got)} — not in Fleet's images report")
        else:
            want = report["images"][repo]
            c.add(repo, got == want, short(got) + ("" if got == want else
                                                  f" (the node reported {short(want)} to Fleet)"))
    pods = [p for p in (d.get("pods") or []) if p["name"].startswith("int-gotcha-")]
    if d.get("pods") is None:
        c.add("int-gotcha images", None, d["errors"].get("pods", "pods unreadable"))
    elif not pods:
        c.add("int-gotcha images", False, "no int-gotcha pod")
    elif staged:
        for p in pods:
            for ct in p["containers"]:
                running = digest_of(ct["image_id"]) or digest_of(ct["image"])
                repo = repo_path(ct["image"])
                want = digest_of(staged.get(repo, ""))
                c.add(f"{ct['name']} {repo}", bool(want) and running == want,
                      "runs the staged image" if want and running == want else
                      f"runs {short(running)}, staged {short(want)}" if want else f"{repo} is not staged")
    agent_pin = ctx.release.pin("hub.magos_agent")
    tag = manifest_image_tag(d.get("manifest") or [], "vendor/magos-agent")
    if d.get("manifest") is None:
        c.add("magos-agent (manifest)", None, d["errors"].get("manifest", "manifest unreadable"))
    else:
        c.add("magos-agent (manifest)", (tag == agent_pin) if agent_pin and tag else False if agent_pin else None,
              (tag or "not in the Gotcha manifest") + ("" if tag == agent_pin else f" (want {agent_pin or 'unpinned'})"))
    running_refs = [ct["image_id"] or ct["image"] for p in pods for ct in p["containers"]
                    if repo_path(ct["image"]) == "vendor/magos-agent"]
    ref = running_refs[0] if running_refs else staged.get("vendor/magos-agent", "")
    if ref:
        r = ctx.server(f"sudo -n k3s crictl inspecti {ref}", timeout=30)
        try:
            info = json.loads(r.out) if r.ok else {}
        except ValueError:
            info = {}
        cfg = ((info.get("info") or {}).get("imageSpec") or {}).get("config") or {}
        label = (cfg.get("Labels") or {}).get(OCI_VERSION)
        if label is None:
            c.add("magos-agent (running image)", None,
                  "image not on the node" if not r.ok else f"image has no {OCI_VERSION} label")
        else:
            c.add("magos-agent (running image)", (label == agent_pin) if agent_pin else None,
                  label + ("" if label == agent_pin else f" (want {agent_pin or 'unpinned'})"))
    return c.row(spec, staged={k: digest_of(v) for k, v in staged.items()})


def _s43(ctx: Context) -> Row:
    spec = STAGE.spec("S4.3")
    d = _server(ctx)
    desired, note = None, ""
    fleet, node = _fleet(ctx), _fleet_node(ctx)
    if not fleet.enabled or node is None:
        note = "Fleet not configured or node not found (S0.5)"
    else:
        try:
            desired = fleet.system_configuration(node.get("model") or "", node.get("role") or "",
                                                 node.get("desiredSystemConfigurationVersion"))
            if desired is None:
                note = f"no system configuration {node.get('desiredSystemConfigurationVersion')} for {node.get('model')}/{node.get('role')}"
        except (httpx.HTTPError, ValueError) as e:
            note = ctx.redact(f"Fleet unreachable: {type(e).__name__}: {e}")
    c = Checks()
    for label, got, floor_key, fleet_key in (
            ("node-controller", d["node_controller"], "server.node_controller_floor", "nodeController"),
            ("k3s", d["k3s"], "server.k3s_floor", "k3s")):
        if not got:
            c.add(label, None, "version not readable on the server")
            continue
        target = ((desired or {}).get(fleet_key) or {}).get("version")
        if desired is None:
            c.add(f"{label} == Fleet desired", None, f"{got} — {note}")
        else:
            c.add(f"{label} == Fleet desired", (got == target) if target else None,
                  got + ("" if got == target else f" (Fleet desired {target or 'not set'})"))
        floor = ctx.release.pin(floor_key)
        if not floor:
            at_least = None
        elif label == "k3s":
            at_least = bool(k3s_key(got)) and bool(k3s_key(floor)) and k3s_key(got) >= k3s_key(floor)
        else:
            at_least = version_at_least(got, floor)
        c.add(f"{label} ≥ release.yaml floor", at_least,
              f"floor {floor}" if at_least else f"{got} is below the {floor} floor" if floor else "floor unpinned")
    return c.row(spec, installed={"node_controller": d["node_controller"], "k3s": d["k3s"]},
                 fleet_desired={k: (desired or {}).get(k) for k in ("k3s", "nodeController")} if desired else None)


def _identical_and_pinned(ctx: Context, spec: RowSpec, kind: str, pin_key: str) -> Row:
    parts, missing = _parts(ctx, spec, kind)
    if missing:
        return missing
    want = ctx.release.pin(pin_key)
    c = Checks()
    known = {p.firmware for p in parts if not _unreadable(p)}
    for p in parts:
        why = _unreadable(p)
        if why:
            c.add(p.label, None, why)
        else:
            c.add(p.label, (p.firmware == want) if want else None,
                  p.firmware + ("" if p.firmware == want else f" (want {want or 'unpinned'})"))
    if len(known) > 1:
        c.add("identical", False, "mixed: " + ", ".join(sorted(known)))
    return c.row(spec)


def _s44(ctx: Context) -> Row:
    return _identical_and_pinned(ctx, STAGE.spec("S4.4"), "apu", "magos.apu_firmware")


def _s45(ctx: Context) -> Row:
    return _identical_and_pinned(ctx, STAGE.spec("S4.5"), "radar", "magos.radar_firmware")


def _s46(ctx: Context) -> Row:
    spec = STAGE.spec("S4.6")
    parts, missing = _parts(ctx, spec, "camera")
    if missing:
        return missing
    c = Checks()
    floor = ctx.release.pin("camera.firmware")
    for p in parts:
        why = _unreadable(p)
        if why:
            c.add(p.label, None, why)
            continue
        legacy = camera_legacy(p.firmware)
        c.add("generation", not legacy, "legacy RPC2 firmware — replace or reflash" if legacy else "REST (/v1)")
        have, need = fw_build_date(p.firmware), fw_build_date(floor or "")
        if not floor:
            c.add("firmware", None, f"{p.firmware} (floor unpinned)")
        elif have is None or need is None:
            c.add("firmware", False, f"{p.firmware} — no build date to compare (floor {floor})")
        else:
            c.add("firmware", have >= need, p.firmware + ("" if have >= need else f" — older than the {floor} floor"))
    return c.row(spec)


def _s47(ctx: Context) -> Row:
    spec = STAGE.spec("S4.7")
    parts, missing = _parts(ctx, spec, "router", "modem", "switch", "poe_switch")
    if missing:
        return missing
    rel = ctx.release
    rules = {
        "router": ("teltonika.rutm08", versions_match, "want"),
        "modem": ("teltonika.otd500", versions_match, "want"),
        "switch": ("teltonika.tsw202_floor", version_at_least, "floor"),
        "poe_switch": ("planet.firmware_floor", planet_at_least, "floor"),
    }
    c = Checks()
    for p in parts:
        key, ok, word = rules[p.kind]
        why = _unreadable(p)
        want = rel.pin(key)
        if why:
            c.add(p.label, None, why)
        elif not want:
            c.add(p.label, None, f"{p.firmware} ({key} unpinned)")
        else:
            good = ok(p.firmware, want)
            c.add(p.label, good, p.firmware + ("" if good else f" ({word} {want})"))
    return c.row(spec)


def _s48(ctx: Context) -> Row:
    spec = STAGE.spec("S4.8")
    op = (ctx.facts.get("identity") or {}).get("operator")
    if op is None:
        return amber(spec, "no operator session (S0.4) — operator build not read")
    bi = op.get("build_info") or {}
    if not bi:
        return Checks().add("setup_version", False, "/etc/kela/build-info not found on the operator").row(spec)
    got, want = bi.get("setup_version", ""), ctx.release.pin("operator.setup_version")
    ok = None if not want else got == want
    return Checks().add("setup_version", ok,
                        f"{got or 'missing'}" + ("" if got == want else f" (want {want or 'unpinned'})")).row(spec)


def _s49(ctx: Context) -> Row:
    spec = STAGE.spec("S4.9")
    inv = _inventory(ctx)
    if inv is None:
        return amber(spec, "device discovery (S0.6) did not run")
    bc = BenchCentral(ctx.creds.bench_central_url)
    if not bc.enabled:
        return amber(spec, "bench-central URL not set ([bench_central] url in config.toml)")
    c = Checks()
    records: dict[str, dict] = {}
    for p in inv:
        if p.serial in ("", "unknown", None):
            c.add(p.label, None, "no serial — " + (_unreadable(p) or "not identified in S0.6"))
            continue
        try:
            runs = bc.runs_for_serial(p.serial, kind="configure", limit=1)
        except (httpx.HTTPError, ValueError) as e:
            return amber(spec, ctx.redact(f"bench-central unreachable: {type(e).__name__}: {e}"),
                         detail={"checks": c.items})
        if not runs:
            c.add(p.label, False, f"SN {p.serial} — no configure record in bench-central")
            continue
        last = runs[0]
        records[p.serial] = {k: last.get(k) for k in ("run_id", "timestamp", "tool", "status", "verified")}
        when = str(last.get("timestamp") or "")[:10]
        c.add(p.label, last.get("verified") is True,
              f"SN {p.serial} — {last.get('tool')} {when}" +
              ("" if last.get("verified") is True else f", not verified (status {last.get('status')})"))
    return c.row(spec, records=records)


def _s410(ctx: Context) -> Row:
    spec = STAGE.spec("S4.10")
    parts, missing = _parts(ctx, spec, "speaker")
    if missing:
        return missing
    want = ctx.release.pin("speaker.firmware")
    c = Checks()
    for p in parts:
        why = _unreadable(p)
        if why:
            c.add(p.label, None, why)
        else:
            same = bool(want) and p.firmware.strip().lower() == want.lower()
            c.add(p.label, same if want else None,
                  p.firmware + ("" if same else f" (want {want or 'unpinned'})"))
    return c.row(spec)


def _s411(ctx: Context) -> Row:
    spec = STAGE.spec("S4.11")
    from ..access.grpc import pb
    want = ctx.release.get("hub.detector") or {}
    try:
        va = pb("kela.video_analytics.v1alpha1.video_analytics_pb2")
        stub = hub(ctx).stub("kela.video_analytics.v1alpha1.video_analytics_pb2_grpc",
                             "VideoAnalyticsConfigServiceStub")
        models = stub.ListAvailableModels(va.ListAvailableModelsRequest(), timeout=15)
        configs = stub.ListConfigs(va.ListConfigsRequest(), timeout=15).configs
    except Exception as e:  # noqa: BLE001
        return amber(spec, "VideoAnalyticsConfigService: " + ctx.redact(grpc_error(e)))
    c = Checks()
    default = models.default_model
    c.add("default model", default == want.get("model") if default else False,
          (default or "none declared") + ("" if default == want.get("model") else f" (want {want.get('model')})"))
    pinned_cfgs = [f"{x.name or x.sensor_id}→{x.model}" for x in configs if x.model and (x.enabled or not x.HasField("enabled"))]
    if pinned_cfgs:
        c.items.append({"label": "configs pinning another model (recorded)", "ok": True,
                        "actual": ", ".join(pinned_cfgs)})
    package = str(want.get("package") or "")
    d = _server(ctx)
    mounts = sorted({(p["name"].rsplit("-", 2)[0], digest_of(v["ref"]))
                     for p in d.get("pods") or [] for v in p["image_volumes"]
                     if repo_path(v["ref"]) == f"models/{package}"})
    if d.get("pods") is None:
        c.add("mounted model", None, d["errors"].get("pods", "pods unreadable"))
        return c.row(spec)
    if not mounts:
        c.add("mounted model", False, f"no pod mounts models/{package}")
        return c.row(spec)
    tags: dict[str, str] = {}
    fleet = _fleet(ctx)
    note = "Fleet not configured — digest only"
    if fleet.enabled:
        try:
            tags, note = fleet.package_tags("model", package), ""
        except (httpx.HTTPError, ValueError) as e:
            note = ctx.redact(f"Fleet unreachable: {type(e).__name__}")
    version = str(want.get("version") or "")
    for workload, dig in mounts:
        tag = tags.get(dig)
        if tag is None:
            c.add(workload, None, f"{short(dig)} — " + (note or "digest not in Fleet's package list"))
        else:
            c.add(workload, tag == version, tag + ("" if tag == version else f" (want {version})"))
    return c.row(spec, mounts=[{"workload": w, "digest": d_, "tag": tags.get(d_)} for w, d_ in mounts])


def run(ctx: Context) -> Iterator[Row]:
    try:
        yield from guarded(ctx, STAGE, (_s41, _s42, _s43, _s44, _s45, _s46, _s47, _s48, _s49, _s410, _s411))
    finally:
        ctx.facts.pop("_s4", None)
