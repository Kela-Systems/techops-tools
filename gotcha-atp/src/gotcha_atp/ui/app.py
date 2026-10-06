"""The local web UI (http://127.0.0.1:8190): one process, one page, one SSE stream.

    GET  /                    the page (static/index.html)
    GET  /api/home            laptop status, release, tailnet units, credential presence
    POST /api/connect         open both hops to a unit, read its identity for confirmation
    POST /api/disconnect
    POST /api/run             start a run on the connected unit {stages?, soak?}
    POST /api/rerun-failed    re-run the stages that failed last time (never stamp-eligible)
    POST /api/answer          one manual-step answer {id, answer: yes|no|skip, note}
    POST /api/cancel
    GET  /api/events          SSE: the current run's events, replayed from the start on connect
    GET  /api/state           connection + last result (for a page reload)
    GET  /api/report/{kind}   the last run's json / html / pdf
    POST /api/upload/retry    drain the bench-central outbox

Passwords never leave this process: /api/home only says whether one is set,
and a typed password is held in memory for the connected session only.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import release as release_mod
from ..access import creds as creds_mod
from ..access import tailnet
from ..access.exec import AccessError
from ..access.session import Session
from ..benchcentral import BenchCentral
from ..context import Context
from ..record import RUNS_DIR, load as load_record, rows_of
from ..runner import Answers, Runner, RunOptions, selection_error
from ..stages import s0

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8190
# Home lists only these units; the tailnet carries the whole fleet. Any other
# site can still be typed by name.
UNIT_FILTER = "gotcha"


class EventBus:
    """Run events fanned out to every open page. History is kept for the
    current run so a reload (or a second tab) replays it from the start."""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self.subs: set[asyncio.Queue] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.history = []

    def publish(self, event: dict) -> None:
        with self.lock:
            self.history.append(event)
            subs = list(self.subs)
        if self.loop is not None:
            for q in subs:
                self.loop.call_soon_threadsafe(q.put_nowait, event)

    async def stream(self):
        q: asyncio.Queue = asyncio.Queue()
        with self.lock:
            backlog = list(self.history)
            self.subs.add(q)
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with self.lock:
                self.subs.discard(q)


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bus = EventBus()
        self.session: Optional[Session] = None
        self.connection: Optional[dict] = None
        self.password = ""
        self.engineer = ""
        self.thread: Optional[threading.Thread] = None
        self.answers = Answers()
        self.cancel = threading.Event()
        self.last: Optional[dict] = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def creds(self) -> creds_mod.Credentials:
        c = creds_mod.load()
        return replace(c, ssh_password=self.password or c.ssh_password,
                       engineer=self.engineer or c.engineer)

    def central(self, c: creds_mod.Credentials) -> BenchCentral:
        return BenchCentral(c.bench_central_url)


def create_app() -> FastAPI:
    st = State()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        st.bus.loop = asyncio.get_running_loop()
        threading.Thread(target=lambda: st.central(st.creds()).drain(), daemon=True).start()
        yield
        st.cancel.set()
        if st.session is not None:
            st.session.close()

    app = FastAPI(title="Gotcha ATP", lifespan=lifespan)
    app.state.atp = st

    @app.middleware("http")
    async def no_cache(request, call_next):
        # The page is tiny and local; a cached app.js/style.css after an update
        # shows the old UI against the new server.
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/home")
    def home() -> dict:
        c = st.creds()
        try:
            rel = release_mod.load()
            release = {"ok": True, **rel.info()}
        except release_mod.ReleaseError as e:
            release = {"ok": False, "error": str(e)}
        ts = tailnet.status()
        central = st.central(c)
        return {
            "release": release,
            "tailnet": {"ok": "error" not in ts and tailnet.self_online(ts),
                        "error": ts.get("error", "")},
            "units": [u.to_dict() for u in tailnet.units(ts) if UNIT_FILTER in u.site]
            if "error" not in ts else [],
            "bench_central": {"url": central.url, "ok": central.health(), "outbox": central.pending()},
            "fleet": {"configured": bool(c.fleet_url and c.fleet_token)},
            "creds": c.public(),
            "connection": st.connection,
            "running": st.running,
        }

    @app.post("/api/connect")
    def connect(body: dict = Body(...)) -> dict:
        site = str(body.get("site") or "").strip().lower()
        if not site:
            raise HTTPException(400, "pick a unit or type a site name")
        with st.lock:
            if st.running:
                raise HTTPException(409, "a run is in progress")
            if st.session is not None:
                st.session.close()
                st.session, st.connection = None, None
            st.password = str(body.get("password") or "")
            st.engineer = str(body.get("engineer") or "").strip()
            c = st.creds()
            redact = creds_mod.Redactor(c.secrets())
            ts = tailnet.status()
            if "error" in ts:
                raise HTTPException(502, ts["error"])
            operator = str(body.get("operator") or "").strip().lower() or None
            peers = tailnet.unit(ts, site, operator)
            if operator and peers.operator is None:
                raise HTTPException(400, f"{operator} is not a peer on the tailnet")
            session = Session(s0.unit_for(peers), user=c.ssh_user, password=c.ssh_password)
            try:
                info = session.open()
                identity = s0.read_identity(session)
            except AccessError as e:
                session.close()
                raise HTTPException(502, redact(str(e)))
            st.session = session
            st.connection = redact.scrub({"site": peers.site, "route": session.route, "session": info,
                                          "identity": identity, "peers": peers.to_dict()})
            return st.connection

    @app.post("/api/disconnect")
    def disconnect() -> dict:
        with st.lock:
            if st.running:
                raise HTTPException(409, "a run is in progress")
            if st.session is not None:
                st.session.close()
            st.session, st.connection, st.password = None, None, ""
        return {"ok": True}

    def _start(stages: Optional[list[str]], soak: bool, prior: dict) -> dict:
        with st.lock:
            if st.running:
                raise HTTPException(409, "a run is already in progress")
            if st.session is None or not st.session.opened:
                raise HTTPException(400, "connect to a unit first")
            c = st.creds()
            redact = creds_mod.Redactor(c.secrets())
            ctx = Context(unit=st.session.unit, creds=c, redact=redact, session=st.session,
                          prior=prior)
            try:
                ctx.release = release_mod.load()
            except release_mod.ReleaseError as e:
                ctx.release_error = str(e)
            st.cancel = ctx.cancel
            st.answers = Answers()
            st.bus.reset()
            runner = Runner(ctx, emit=st.bus.publish, answers=st.answers,
                            options=RunOptions(stages=stages, soak=soak, engineer=c.engineer),
                            out_dir=RUNS_DIR, central=st.central(c))

            def work() -> None:
                try:
                    st.last = runner.run()
                except Exception as e:  # noqa: BLE001 — surface it on the page, keep the server up
                    st.bus.publish({"type": "run_finished", "error": redact(f"{type(e).__name__}: {e}"),
                                    "summary": None})

            st.thread = threading.Thread(target=work, name="atp-run", daemon=True)
            st.thread.start()
            return {"run_id": runner.run_id, "stages": stages, "soak": soak}

    @app.post("/api/run")
    def run(body: dict = Body(default={})) -> dict:
        stages = body.get("stages") or None
        if stages is not None:
            stages = [str(s).strip().upper() for s in stages if str(s).strip()]
        soak = bool(body.get("soak"))
        if (why := selection_error(stages, soak)):
            raise HTTPException(400, why)
        return _start(stages, soak, {})

    @app.post("/api/rerun-failed")
    def rerun_failed() -> dict:
        last = st.last or {}
        summary = last.get("summary") or {}
        ids = summary.get("failed") or [i for i in summary.get("critical_amber") or []]
        stages = sorted({i.split(".")[0] for i in ids if i.split(".")[0] != "S0"},
                        key=lambda s: int(s[1:]))
        if not stages:
            raise HTTPException(400, "nothing to re-run — the last run has no failed stage")
        prior = rows_of(load_record(Path(last["record"]))) if last.get("record") else {}
        return _start(stages, False, prior)

    @app.post("/api/answer")
    def answer(body: dict = Body(...)) -> dict:
        try:
            st.answers.answer(str(body.get("id")), str(body.get("answer") or ""),
                              str(body.get("note") or ""))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        return {"pending": st.answers.pending()}

    @app.post("/api/cancel")
    def cancel() -> dict:
        st.cancel.set()
        return {"ok": True}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(st.bus.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/state")
    def state() -> dict:
        return {"connection": st.connection, "running": st.running,
                "pending": st.answers.pending(), "last": st.last}

    @app.get("/api/report/{kind}")
    def report(kind: str):
        last = st.last or {}
        key = {"json": "record", "html": "html", "pdf": "pdf"}.get(kind)
        path = last.get(key) if key else None
        if not path or not Path(path).is_file():
            raise HTTPException(404, f"no {kind} report for the last run"
                                + (f" — {last.get('pdf_note')}" if kind == "pdf" and last.get("pdf_note") else ""))
        media = {"json": "application/json", "html": "text/html", "pdf": "application/pdf"}[kind]
        return FileResponse(path, media_type=media, filename=Path(path).name)

    @app.post("/api/upload/retry")
    def upload_retry() -> dict[str, Any]:
        central = st.central(st.creds())
        results = central.drain()
        return {"results": results, "pending": central.pending()}

    return app
