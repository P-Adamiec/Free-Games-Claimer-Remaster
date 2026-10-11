"""The web interface: FastAPI + Jinja2, Dutch, no JavaScript build step.

Start it with:  uvicorn webui.app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from webui import jobqueue
from webui.app import botstate, config_store, help as storehelp, schema, security
from webui.app.users import UserError, UserStore, fingerprint

log = logging.getLogger("webui")
HERE = Path(__file__).resolve().parent
VNC_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
VNC_ENTRY = "/vnc/vnc.html?autoconnect=true&resize=scale&reconnect=true&path=vnc/websockify"
SESSION_HOURS = 12

CSP_APP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
# noVNC's own pages need a little more (inline styles, ws:// to ourselves).
CSP_VNC = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; "
           "base-uri 'self'; object-src 'none'")


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def create_app(environ=None) -> FastAPI:
    env = dict(os.environ if environ is None else environ)
    data_dir = Path(env.get("WEBUI_DATA_DIR") or "/webui-data")
    data_dir.mkdir(parents=True, exist_ok=True)
    users = UserStore(data_dir / "users.json")
    bots = botstate.bots_from_env(env)
    bots_by_name = {b.name: b for b in bots}

    outcome = users.bootstrap(env.get("WEBUI_ADMIN_USER"), env.get("WEBUI_ADMIN_PASSWORD"), list(bots_by_name))
    if outcome == "missing":
        raise SystemExit(
            "FOUT: er bestaan nog geen gebruikers. Zet WEBUI_ADMIN_USER en WEBUI_ADMIN_PASSWORD "
            "(minstens 10 tekens) in .env en start opnieuw: docker compose up -d")
    if outcome == "created":
        log.warning("Eerste beheerder '%s' aangemaakt uit WEBUI_ADMIN_USER. Haal WEBUI_ADMIN_PASSWORD "
                    "nu weg uit .env; het wordt niet meer gebruikt.", env.get("WEBUI_ADMIN_USER"))

    for bot in bots:
        try:
            if botstate.seed_config(bot):
                log.info("Standaard planning voor bot %s geschreven.", bot.name)
                if botstate.status(bot)["alive"]:  # already running on built-in defaults
                    botstate.request_restart(bot)
        except OSError as exc:
            log.warning("Kon geen startinstellingen schrijven voor %s: %s", bot.name, exc)

    queue_dir = Path(env.get("FGC_QUEUE_DIR") or "/queue")
    try:
        queue = jobqueue.JobQueue(queue_dir)
    except (OSError, sqlite3.Error) as exc:
        raise SystemExit(f"FOUT: de takenlijst in {queue_dir} kan niet geopend worden ({exc}). Is het volume "
                         "fgc_queue gekoppeld? Draai ./webui/bots.sh generate en daarna docker compose up -d.")
    prepare_queue(queue_dir)
    workers = jobqueue.workers_from_env(env)
    secure_cookies = _truthy(env.get("WEBUI_COOKIE_SECURE"), True)
    throttle = security.Throttle()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["store_names"] = botstate.STORE_NAMES
    templates.env.globals["store_help"] = storehelp.STORE_HELP
    templates.env.globals["help_modes"] = storehelp.MODES

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.users, app.state.bots, app.state.throttle, app.state.queue = users, bots, throttle, queue

    # ---------------------------------------------------------------- middleware

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy",
                     CSP_VNC if request.url.path.startswith("/vnc/") else CSP_APP)
        if request.url.scheme == "https":
            h.setdefault("Strict-Transport-Security", "max-age=15552000")
        if not request.url.path.startswith(("/static/", "/vnc/")):
            h.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=security.secret_key(env.get("WEBUI_SECRET_KEY"), data_dir / "secret.key"),
        session_cookie="fgc_webui",
        max_age=SESSION_HOURS * 3600,
        same_site="lax",
        https_only=secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ---------------------------------------------------------------- helpers

    def session_user(session) -> tuple[str, dict] | None:
        name = session.get("u")
        user = users.get(name) if name else None
        if not user or session.get("fp") != fingerprint(user):
            return None
        return name, user

    def require(request: Request, admin: bool = False) -> tuple[str, dict]:
        found = session_user(request.session)
        if not found:
            raise LoginRequired()
        if admin and found[1].get("role") != "admin":
            raise Forbidden()
        return found

    def bot_of(user: dict) -> botstate.Bot | None:
        return bots_by_name.get(user.get("bot") or "")

    def flash(request: Request, text: str, tone: str = "ok") -> None:
        request.session.setdefault("flash", []).append([tone, text])

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        found = session_user(request.session)
        ctx.update(request=request, me=found[0] if found else None, my=found[1] if found else None,
                   csrf=security.csrf_token(request.session), flashes=request.session.pop("flash", []))
        return templates.TemplateResponse(request, name, ctx, status_code=ctx.pop("status", 200))

    async def checked_form(request: Request):
        form = await request.form()
        if not security.csrf_ok(request.session, form.get("csrf")):
            raise Forbidden()
        return form

    def client_ip(request) -> str:
        # request.client is already the real address when uvicorn trusts the proxy (WEBUI_TRUSTED_PROXIES).
        return request.client.host if request.client else "?"

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc):
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc):
        return render(request, "error.html", status=403, title="Geen toegang",
                      text="Je hebt geen toegang tot deze pagina, of het formulier was verlopen. "
                           "Ga terug en probeer het opnieuw.")

    # ---------------------------------------------------------------- login

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/login")
    async def login_page(request: Request, next: str = "/"):
        return render(request, "login.html", next=_safe_next(next),
                      insecure=secure_cookies and request.url.scheme != "https")

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        name = str(form.get("username", "")).strip().lower()[:64]
        password = str(form.get("password", ""))[:256]
        nxt = _safe_next(str(form.get("next", "/")))
        keys = (f"ip:{client_ip(request)}", f"user:{name}")
        if not security.csrf_ok(request.session, form.get("csrf")):
            return render(request, "login.html", next=nxt, error="Het formulier was verlopen, probeer opnieuw.",
                          status=400)
        if throttle.blocked(*keys):
            log.warning("Login geblokkeerd voor %s / %s (te veel pogingen).", keys[0], name)
            return render(request, "login.html", next=nxt, status=429,
                          error="Te veel mislukte pogingen. Wacht een kwartier en probeer het opnieuw.")
        await asyncio.sleep(throttle.delay(*keys))
        user = await asyncio.to_thread(users.verify, name, password)
        if not user:
            throttle.fail(*keys)
            log.warning("Mislukte login voor '%s' vanaf %s.", name, keys[0][3:])
            return render(request, "login.html", next=nxt, status=401,
                          error="Onjuiste gebruikersnaam of wachtwoord.")
        throttle.success(f"user:{name}")
        request.session.clear()  # new session, no fixation
        request.session.update(u=name, fp=fingerprint(user))
        return RedirectResponse(nxt, status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        await checked_form(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---------------------------------------------------------------- dashboard

    @app.get("/")
    async def dashboard(request: Request):
        name, user = require(request)
        bot = bot_of(user)
        if not bot:
            return render(request, "nobot.html")
        qs = queue.bot_state(bot.name)
        st = botstate.status(bot, qs)
        games = botstate.games(bot)
        values = config_store.read_config(bot.config_file)
        last_job = botstate.job_view(qs["last"]) if qs["last"] else None
        per_store: dict[str, list] = {}
        for g in games:
            per_store.setdefault(g["store_name"], []).append(g)
        return render(request, "dashboard.html", bot=bot, st=st, per_store=per_store,
                      claimed=sum(1 for g in games if g["tone"] == "ok"),
                      failures=botstate.run_failures(bot, st, games), log=botstate.log_tail(bot),
                      schedule=botstate.schedule_text(values), last_job=last_job,
                      workers=workers, running_total=qs["running_total"],
                      vnc_password=botstate.env_value(bot, "VNC_PASSWORD"),
                      stores=config_store.selected_stores(values), configured=bool(values.get("STORES")))

    @app.get("/api/status")
    async def api_status(request: Request):
        name, user = require(request)
        bot = bot_of(user)
        if not bot:
            return JSONResponse({}, status_code=404)
        st = botstate.status(bot, queue.bot_state(bot.name))
        return {k: st[k] for k in ("state", "label", "running", "queued", "next_run", "restart_pending")}

    @app.post("/run")
    async def run_now(request: Request):
        name, user = require(request)
        await checked_form(request)
        bot = bot_of(user)
        if not bot:
            raise Forbidden()
        job, created = queue.enqueue(bot.name, source="manual", requested_by=name)
        if created:
            flash(request, "Run toegevoegd aan de takenlijst. Hij start zodra er een plek vrij is.")
        else:
            flash(request, "Er staat al een run van jouw bot in de wachtrij.", "bad")
        return RedirectResponse("/", status_code=303)

    # ---------------------------------------------------------------- job list

    @app.get("/taken")
    async def jobs_page(request: Request):
        name, user = require(request)
        admin = user.get("role") == "admin"
        mine = (user.get("bot") or "")
        active = [botstate.job_view(j) for j in queue.active()]
        history = [botstate.job_view(j) for j in queue.history(None if admin else mine or "-", 100)]
        if not admin:
            # Others' jobs only as an anonymous count: your place in line still makes sense.
            ahead = sum(1 for j in active if j["bot"] != mine)
            active = [j for j in active if j["bot"] == mine]
        else:
            ahead = 0
        return render(request, "jobs.html", active=active, history=history, admin=admin, workers=workers,
                      running_total=sum(1 for j in queue.active() if j["status"] == "running"),
                      others=ahead, alive=queue.bots_alive())

    @app.post("/taken/{job_id}/annuleer")
    async def job_cancel(request: Request, job_id: int):
        name, user = require(request)
        await checked_form(request)
        job = queue.get(job_id)
        if not job or (user.get("role") != "admin" and job["bot"] != user.get("bot")):
            raise Forbidden()
        if queue.cancel(job_id, by=name):
            flash(request, f"Taak {job_id} is geannuleerd.")
        else:
            flash(request, "Deze taak is al gestart of klaar en kan niet meer geannuleerd worden.", "bad")
        return RedirectResponse("/taken", status_code=303)

    # ---------------------------------------------------------------- settings

    def settings_page(request, bot, values, errors=None, status=200):
        locked = config_store.locked_keys(bot.env_file)
        return render(request, "settings.html", bot=bot, view=config_store.view(values, locked),
                      errors=errors or {}, timezones=schema.timezones(), status=status,
                      schedule=botstate.schedule_text(values), st=botstate.status(bot, queue.bot_state(bot.name)))

    @app.get("/instellingen")
    async def settings_get(request: Request):
        name, user = require(request)
        bot = bot_of(user)
        if not bot:
            return render(request, "nobot.html")
        return settings_page(request, bot, config_store.read_config(bot.config_file))

    @app.post("/instellingen")
    async def settings_post(request: Request):
        name, user = require(request)
        form = await checked_form(request)
        bot = bot_of(user)
        if not bot:
            raise Forbidden()
        current = config_store.read_config(bot.config_file)
        new, errors = config_store.apply_form(current, form, config_store.locked_keys(bot.env_file))
        if errors:
            flash(request, "Niet opgeslagen: controleer de rood gemarkeerde velden. "
                           "Ingevulde wachtwoorden moet je opnieuw invullen.", "bad")
            return settings_page(request, bot, new, errors, status=400)
        if new != current:
            config_store.write_config(bot.config_file, new)
            botstate.request_restart(bot)
            if form.get("run_after"):
                # Claimed by the bot once it has restarted with the new settings.
                queue.enqueue(bot.name, source="manual", requested_by=name)
            log.info("Instellingen van bot %s gewijzigd door %s.", bot.name, name)
            flash(request, "Opgeslagen. De bot herstart om de nieuwe instellingen te laden "
                           "(als er een run bezig is: zodra die klaar is).")
        else:
            flash(request, "Er was niets gewijzigd.")
        return RedirectResponse("/instellingen", status_code=303)

    # ---------------------------------------------------------------- own account

    @app.get("/uitleg")
    async def explain(request: Request):
        require(request)
        return render(request, "uitleg.html", stores=schema.STORES, general=storehelp.GENERAL)

    @app.get("/account")
    async def account_get(request: Request):
        require(request)
        return render(request, "account.html")

    @app.post("/account")
    async def account_post(request: Request):
        name, user = require(request)
        form = await checked_form(request)
        keys = (f"ip:{client_ip(request)}", f"user:{name}")
        if throttle.blocked(*keys) or not await asyncio.to_thread(users.verify, name, str(form.get("current", ""))):
            throttle.fail(*keys)
            flash(request, "Je huidige wachtwoord klopt niet.", "bad")
            return RedirectResponse("/account", status_code=303)
        new = str(form.get("new", ""))
        if new != str(form.get("repeat", "")):
            flash(request, "De twee nieuwe wachtwoorden zijn niet gelijk.", "bad")
            return RedirectResponse("/account", status_code=303)
        try:
            await asyncio.to_thread(users.set_password, name, new)
        except UserError as exc:
            flash(request, str(exc), "bad")
            return RedirectResponse("/account", status_code=303)
        request.session["fp"] = fingerprint(users.get(name))
        flash(request, "Je wachtwoord is gewijzigd.")
        return RedirectResponse("/account", status_code=303)

    # ---------------------------------------------------------------- admin

    @app.get("/beheer")
    async def admin_page(request: Request):
        require(request, admin=True)
        all_users = users.all()
        owner = {u.get("bot"): n for n, u in all_users.items() if u.get("bot")}
        configs = {b.name: config_store.read_config(b.config_file) for b in bots}
        rows = [{"bot": b, "st": botstate.status(b, queue.bot_state(b.name)), "owner": owner.get(b.name),
                 "schedule": botstate.schedule_text(configs[b.name]),
                 "claimed": sum(1 for g in botstate.games(b) if g["tone"] == "ok")} for b in bots]
        return render(request, "admin.html", users=all_users, rows=rows, bots=bots, workers=workers)

    async def admin_action(request: Request, action):
        require(request, admin=True)
        form = await checked_form(request)
        try:
            text = await asyncio.to_thread(action, form)
            flash(request, text)
        except UserError as exc:
            flash(request, str(exc), "bad")
        return RedirectResponse("/beheer", status_code=303)

    def _bot_choice(value) -> str | None:
        value = str(value or "")
        if value and value not in bots_by_name:
            raise UserError("Onbekende bot.")
        return value or None

    @app.post("/beheer/nieuw")
    async def admin_create(request: Request):
        def act(form):
            users.create(str(form.get("username", "")), str(form.get("password", "")),
                         str(form.get("role", "user")), _bot_choice(form.get("bot")))
            return "Gebruiker aangemaakt."
        return await admin_action(request, act)

    @app.post("/beheer/{target}/wachtwoord")
    async def admin_password(request: Request, target: str):
        def act(form):
            users.set_password(target, str(form.get("password", "")))
            return f"Wachtwoord van {target} is opnieuw ingesteld; oude sessies zijn uitgelogd."
        return await admin_action(request, act)

    @app.post("/beheer/{target}/wijzig")
    async def admin_update(request: Request, target: str):
        def act(form):
            users.update(target, role=str(form.get("role", "user")), bot=_bot_choice(form.get("bot")))
            return f"{target} is bijgewerkt."
        return await admin_action(request, act)

    @app.post("/beheer/{target}/verwijder")
    async def admin_delete(request: Request, target: str):
        me = require(request, admin=True)[0]

        def act(form):
            if target == me:
                raise UserError("Je kunt jezelf niet verwijderen.")
            users.delete(target)
            return f"{target} is verwijderd."
        return await admin_action(request, act)

    # ---------------------------------------------------------------- noVNC proxy

    def vnc_bot(session) -> botstate.Bot | None:
        found = session_user(session)
        return bot_of(found[1]) if found else None

    @app.get("/vnc")
    @app.get("/vnc/")
    async def vnc_entry(request: Request):
        require(request)
        return RedirectResponse(VNC_ENTRY, status_code=303)

    @app.get("/vnc/{path:path}")
    async def vnc_static(request: Request, path: str):
        require(request)
        bot = vnc_bot(request.session)
        if not bot:
            raise Forbidden()
        if not VNC_PATH_RE.match(path) or ".." in path:
            return PlainTextResponse("Niet gevonden", status_code=404)
        url = f"http://{bot.vnc_host}:{bot.vnc_port}/{path}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                upstream = await client.get(url)  # no cookies or headers of ours go to the bot
        except httpx.HTTPError:
            return render(request, "error.html", status=502, title="Scherm niet bereikbaar",
                          text="De bot is nu niet bereikbaar (misschien herstart hij). Probeer het over een minuut.")
        keep = {k: v for k, v in upstream.headers.items()
                if k.lower() in ("content-type", "last-modified", "etag")}
        keep["Cache-Control"] = "private, max-age=300"
        return Response(upstream.content, status_code=upstream.status_code, headers=keep)

    @app.websocket("/vnc/websockify")
    async def vnc_socket(ws: WebSocket):
        bot = vnc_bot(ws.session)
        if not bot or not _same_origin(ws):
            await ws.close(code=1008)
            return
        import websockets
        from websockets.asyncio.client import connect

        wanted = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        try:
            upstream = await connect(f"ws://{bot.vnc_host}:{bot.vnc_port}/websockify",
                                     subprotocols=wanted or None, max_size=None, open_timeout=10)
        except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol=upstream.subprotocol)

        async def to_bot():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
                if data is not None:
                    await upstream.send(data)

        async def to_browser():
            async for data in upstream:
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)

        tasks = [asyncio.create_task(to_bot()), asyncio.create_task(to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            for t in tasks:
                t.cancel()
            try:
                await upstream.close()
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:  # the browser already went away
                pass

    return app


def prepare_queue(folder: Path) -> None:
    """Let every bot write the shared job list, also bots that run as their own user (PUID)."""
    try:
        os.chmod(folder, 0o1777)
        for name in (jobqueue.DB_NAME, jobqueue.DB_NAME + "-wal", jobqueue.DB_NAME + "-shm"):
            path = folder / name
            if path.exists():
                os.chmod(path, 0o666)
    except OSError as exc:
        log.warning("Kon de takenlijst in %s niet voor alle bots openzetten: %s", folder, exc)


def _safe_next(value: str) -> str:
    """Only paths on this site, so a login link cannot send you elsewhere."""
    value = value or "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _same_origin(ws: WebSocket) -> bool:
    """Block other sites from opening a VNC socket with your cookie (cross-site WebSocket hijacking)."""
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    return (urlsplit(origin).netloc or "").lower() == host.lower()
