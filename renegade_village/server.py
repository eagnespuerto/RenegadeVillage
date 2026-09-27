"""FastAPI app: REST for the UI, a WebSocket for live events."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as config_mod
from .config import Config
from .ollama_client import OllamaClient
from .store import Store, normalize_channel
from .village import Village
from .workspace import Workspace, WorkspaceError

STATIC = Path(__file__).parent / "static"


class Hub:
    def __init__(self):
        self.sockets: set[WebSocket] = set()

    async def broadcast(self, event: dict) -> None:
        dead = []
        for ws in list(self.sockets):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)


class PostBody(BaseModel):
    content: str


class ChannelBody(BaseModel):
    name: str
    topic: str = ""


class GoalBody(BaseModel):
    goal: str


class UserBody(BaseModel):
    name: str


class OrderBody(BaseModel):
    names: list[str]


def create_app(cfg: Config, client: OllamaClient | None = None, save_config: bool = True,
               autostart: bool = False) -> FastAPI:
    hub = Hub()
    workspace = Workspace(cfg.shared_path)
    store = Store(workspace.internal / "village.db")
    client = client or OllamaClient(cfg.mode, cfg.host, cfg.resolved_api_key)
    village = Village(cfg, store, workspace, client, hub.broadcast)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            await village.start()
        yield
        await village.pause()
        await client.aclose()

    app = FastAPI(title="RenegadeVillage", lifespan=lifespan)
    app.state.village = village

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # Always revalidate UI files so updates show up without a hard refresh (also in the Electron app).
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/state")
    async def state():
        return village.snapshot()

    @app.get("/api/channels/{channel}/messages")
    async def messages(channel: str, before: int | None = None, limit: int = 60):
        return store.get_messages(normalize_channel(channel), min(limit, 200), before)

    @app.post("/api/channels/{channel}/messages")
    async def post_message(channel: str, body: PostBody):
        ch = normalize_channel(channel)
        if not store.channel_exists(ch):
            raise HTTPException(404, f"no channel #{ch}")
        if not body.content.strip():
            raise HTTPException(400, "empty message")
        return await village.human_post(ch, body.content.strip())

    @app.post("/api/channels")
    async def create_channel(body: ChannelBody):
        try:
            ch, created = store.ensure_channel(body.name, body.topic, cfg.user_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if created:
            await hub.broadcast({"type": "channels", "channels": store.list_channels()})
        return {"name": ch, "created": created}

    @app.put("/api/channels/order")
    async def reorder_channels(body: OrderBody):
        try:
            store.reorder_channels([normalize_channel(n) for n in body.names])
        except ValueError as e:
            raise HTTPException(400, str(e))
        channels = store.list_channels()
        await hub.broadcast({"type": "channels", "channels": channels})
        return channels

    @app.post("/api/village/start")
    async def start():
        await village.start()
        return {"running": True}

    @app.post("/api/village/pause")
    async def pause():
        await village.pause()
        return {"running": False}

    @app.put("/api/village/goal")
    async def set_goal(body: GoalBody):
        cfg.village_goal = body.goal.strip() or cfg.village_goal
        if save_config:
            config_mod.save(cfg)
        await village.post("general", "system", "system", f"**New village goal:** {cfg.village_goal}")
        return {"goal": cfg.village_goal}

    @app.put("/api/user")
    async def set_user(body: UserBody):
        name = " ".join(body.name.split())[:32]
        if not name:
            raise HTTPException(400, "name can't be empty")
        if name.lower() in {"system", "village"} or village.agent(name):
            raise HTTPException(400, f"'{name}' is taken")
        old = cfg.user_name
        if name == old:
            return {"name": name}
        cfg.user_name = name
        if save_config:
            config_mod.save(cfg)
        store.rename_human(old, name)
        await hub.broadcast({"type": "user", "name": name, "old": old})
        await village.post("general", "system", "system", f"**{old}** is now known as **{name}**")
        return {"name": name}

    @app.get("/api/files")
    async def list_files(path: str = "."):
        try:
            return workspace.list_files(path)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/file")
    async def get_file(path: str):
        try:
            p = workspace.resolve(path)
        except WorkspaceError as e:
            raise HTTPException(400, str(e))
        if not p.is_file():
            raise HTTPException(404, "not found")
        media = "text/plain; charset=utf-8" if p.suffix.lower() in {".md", ".py", ".txt", ".csv", ".json", ".log"} \
            else None
        return FileResponse(p, media_type=media)

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        hub.sockets.add(sock)
        try:
            await sock.send_json({"type": "hello", "state": village.snapshot()})
            while True:
                await sock.receive_text()  # client doesn't send anything meaningful; keeps it open
        except WebSocketDisconnect:
            pass
        finally:
            hub.sockets.discard(sock)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main() -> None:
    import argparse
    import threading
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(description="Run RenegadeVillage")
    parser.add_argument("--config", type=Path, default=config_mod.CONFIG_PATH)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--start", action="store_true", help="start the village immediately")
    a = parser.parse_args()

    if not a.config.exists():
        raise SystemExit(f"No config at {a.config}. Run `python install.py` first.")
    cfg = config_mod.load(a.config)
    if not cfg.enabled_agents():
        raise SystemExit("No villagers enabled in config.json. Re-run `python install.py`.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = create_app(cfg, autostart=a.start)
    url = f"http://127.0.0.1:{cfg.port}"
    print(f"RenegadeVillage at {url}  (shared folder: {cfg.shared_path})")
    if not a.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")
