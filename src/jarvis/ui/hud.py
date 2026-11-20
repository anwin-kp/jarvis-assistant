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
        self.on_stop = None  # called immediately (not queued) when you press Stop / type "stop" while JARVIS talks
        self.on_input = None  # called immediately for buttons and typed messages: they cut in on JARVIS talking
        self.memory_api = None  # (action, msg) -> facts list: the Memory panel (list / forget / teach)
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
        if type_ in ("state", "flags", "stats", "timing"):
            self.latest[type_] = event
        elif type_ not in ("level", "user_pending", "user_cancel"):
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
        from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        html = (WEB / "index.html").read_text(encoding="utf-8")

        @app.get("/")
        async def index(token: str = ""):
            if not secrets.compare_digest(token, self.token):
                return PlainTextResponse("Forbidden", status_code=403)
            return HTMLResponse(html)

        @app.post("/upload")
        async def upload(request: Request, token: str = "", name: str = "file"):
            """Attachment from the page (raw body). Saved to data/knowledge; the page sends the id with its message."""
            if not secrets.compare_digest(token, self.token):
                return PlainTextResponse("Forbidden", status_code=403)
            from jarvis.ui import attachments

            try:
                path = attachments.save(name, await request.body())
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=413)
            return JSONResponse({"id": path.name, "name": attachments.display_name(path)})

        @app.get("/file/{file_id}")
        async def file(file_id: str, token: str = ""):
            """Thumbnails of attached images in the chat (only files in data/knowledge)."""
            from fastapi.responses import FileResponse

            from jarvis.ui import attachments

            path = attachments.resolve(file_id) if secrets.compare_digest(token, self.token) else None
            if not path or path.suffix.lower() not in attachments.IMAGE_EXT:
                return PlainTextResponse("Not found", status_code=404)
            return FileResponse(path)

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
                    action = msg.get("action")
                    if action in ("approve", "deny"):  # approval buttons: no cut-in, never a chat turn
                        self.q.put(("answer", action == "approve"))
                        continue
                    if action in ("facts", "forget", "teach"):  # Memory panel: no turn, no cut-in
                        if self.memory_api:
                            facts = await asyncio.to_thread(self.memory_api, action, msg)
                            await ws.send_json({"type": "facts", "facts": facts})
                        continue
                    if action == "stop" or (self.on_stop and action == "text" and self.on_stop(text)):
                        if action == "stop" and self.on_stop:
                            self.on_stop("stop")
                        continue
                    if action in ("text", "command", "talk") and self.on_input:
                        self.on_input(text)  # buttons / typed messages cut in on JARVIS talking or thinking
                    if action == "text" and msg.get("files"):
                        self.q.put(("text", text, [str(f) for f in msg["files"]][:10]))
                    elif msg.get("action") in ("text", "command"):
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
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={self.url}", "--start-maximized",
                              f"--user-data-dir={profile}", "--no-first-run",
                              # no extensions (Edge syncs yours into new profiles: overlays like Blackbox)
                              "--disable-extensions", "--disable-sync"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            # Edge restores the size it saved last time, overriding --start-maximized: maximize the window itself
            threading.Thread(target=maximize_window, args=("J.A.R.V.I.S.",), daemon=True).start()
        except Exception:
            webbrowser.open(self.url)


def maximize_window(title: str, wait: float = 15) -> bool:
    import time

    import win32con
    import win32gui

    end = time.time() + wait
    while time.time() < end:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd and win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            return True
        time.sleep(0.3)
    return False
