"""Desktop HUD: a local web page (127.0.0.1 + per-launch token) shown in an Edge app window.
The core publishes events here; the page sends typed text, approvals and commands back."""

import asyncio
import secrets
import subprocess
import threading
import webbrowser
from collections import deque
from pathlib import Path

from loguru import logger

WEB = Path(__file__).parent / "web"
URL_FILE = Path(__file__).resolve().parents[3] / "data" / "hud-url.txt"


class Hud:
    def __init__(self, app_queue, port: int = 8765):
        self.q = app_queue
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.clients: set = set()
        self.history: deque = deque(maxlen=150)
        self.latest: dict = {}  # last state/flags/stats, replayed to new windows
        self.loop: asyncio.AbstractEventLoop | None = None
        self.url = f"http://127.0.0.1:{port}/?token={self.token}"

    # ------------------------------------------------------------ publishing (any thread)
    def publish(self, type_: str, **data) -> None:
        event = {"type": type_, **data}
        if type_ in ("state", "flags", "stats"):
            self.latest[type_] = event
        elif type_ != "level":
            self.history.append(event)
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast, event)

    def _broadcast(self, event: dict) -> None:
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, event))

    async def _send(self, ws, event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:
            self.clients.discard(ws)

    # ------------------------------------------------------------ server
    def _make_app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, PlainTextResponse

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        html = (WEB / "index.html").read_text(encoding="utf-8")

        @app.get("/")
        async def index(token: str = ""):
            if not secrets.compare_digest(token, self.token):
                return PlainTextResponse("Forbidden", status_code=403)
            return HTMLResponse(html)

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            if not secrets.compare_digest(ws.query_params.get("token", ""), self.token):
                await ws.close(code=4401)
                return
            await ws.accept()
            self.clients.add(ws)
            for event in [*self.history, *self.latest.values()]:
                await ws.send_json(event)
            try:
                while True:
                    msg = await ws.receive_json()
                    text = str(msg.get("text", ""))[:2000]
                    if msg.get("action") in ("text", "command"):
                        self.q.put(("text", text))
                    elif msg.get("action") == "talk":
                        self.q.put(("text", ""))
            except WebSocketDisconnect:
                pass
            finally:
                self.clients.discard(ws)

        return app

    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(self._make_app(), host="127.0.0.1", port=self.port, log_level="warning",
                                ws="websockets", lifespan="off")
        server = uvicorn.Server(config)

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(server.serve())
            except Exception:
                logger.exception("HUD server stopped")

        threading.Thread(target=run, daemon=True, name="hud").start()
        # Lets start-jarvis / the desktop shortcut reopen the HUD if JARVIS is already running.
        try:
            URL_FILE.parent.mkdir(parents=True, exist_ok=True)
            URL_FILE.write_text(self.url, encoding="utf-8")
        except OSError:
            pass

    def open(self) -> None:
        """Open as a standalone app window (Edge --app), falling back to the default browser."""
        try:
            # Own Edge profile: a separate process that kill-jarvis.ps1 can close without touching your browser.
            profile = Path(__file__).resolve().parents[3] / "data" / "hud-profile"
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={self.url}", "--window-size=1100,760",
                              f"--user-data-dir={profile}", "--no-first-run"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            webbrowser.open(self.url)
