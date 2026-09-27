"""The village: agents taking turns in a Discord-style group chat, with shared tools."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Awaitable, Callable

from .config import AgentSpec, Config
from .ollama_client import OllamaClient
from .store import Store, normalize_channel
from .workspace import Workspace, WorkspaceError

log = logging.getLogger("renegade_village")

Broadcast = Callable[[dict], Awaitable[None]]


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _fn("post_message", "Post a message (Markdown supported) to a channel. Mention villagers with @name.",
        {"channel": {"type": "string", "description": "channel name, e.g. general"},
         "content": {"type": "string"},
         "attachments": {"type": "array", "items": {"type": "string"},
                         "description": "optional shared-folder paths to attach (images render inline)"}},
        ["channel", "content"]),
    _fn("create_channel", "Create a new text channel for a topic or project.",
        {"name": {"type": "string"}, "topic": {"type": "string"}}, ["name", "topic"]),
    _fn("read_channel", "Read older messages from a channel.",
        {"channel": {"type": "string"}, "limit": {"type": "integer", "description": "default 20, max 100"}},
        ["channel"]),
    _fn("list_files", "List files in the shared folder (or a subfolder).",
        {"path": {"type": "string", "description": "folder relative to the shared folder, default '.'"}}, []),
    _fn("read_file", "Read a text file from the shared folder.", {"path": {"type": "string"}}, ["path"]),
    _fn("write_file", "Create or overwrite a file in the shared folder: Markdown docs, Python scripts, data.",
        {"path": {"type": "string"}, "content": {"type": "string"},
         "append": {"type": "boolean", "description": "append instead of overwrite"}},
        ["path", "content"]),
    _fn("run_python", "Run Python in the shared folder (cwd). Pass `code` for a snippet or `path` for a "
        "script there. Use it for calculations and simulations; save plots with matplotlib's savefig.",
        {"code": {"type": "string"}, "path": {"type": "string"},
         "args": {"type": "array", "items": {"type": "string"}}}, []),
    _fn("update_memory", "Replace your private notebook, which is shown to you every turn. Keep plans, "
        "commitments and facts you'll need later. Only you can see it.",
        {"notes": {"type": "string"}}, ["notes"]),
    _fn("end_turn", "Finish your turn. Call this when you've done what you want for now.",
        {"reason": {"type": "string"}}, []),
]

SYSTEM_TEMPLATE = """You are {name}, a villager in RenegadeVillage: a group chat where several AI agents, each a
different model, live and work together alongside a human ({user}). You run on the model `{model}`.
{persona}
Village goal (set by {user}): {goal}

How the village works:
- It is a Discord-style server with text channels. Everyone takes turns; this is your turn.
- Talk ONLY by calling post_message. Keep messages conversational and reasonably short, like chat.
  Reply in the channel where the conversation is happening. Mention others with @name.
- There is a shared folder for real work. Write Markdown documents, Python scripts and data there with
  write_file, and run code with run_python.{python_note}
- Create a new channel when a project deserves its own space.
- Your private notebook (update_memory) is your only memory between turns. Use it.
- Don't repeat what's already been said. If nothing needs you right now, just call end_turn.
- You have at most {steps} tool rounds this turn.

Villagers: {roster}
Channels: {channels}

Your notebook:
{memory}"""


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _args(call: dict) -> dict:
    a = call.get("function", {}).get("arguments") or {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except json.JSONDecodeError:
            a = {}
    return a if isinstance(a, dict) else {}


class Village:
    def __init__(self, cfg: Config, store: Store, workspace: Workspace, client: OllamaClient,
                 broadcast: Broadcast):
        self.cfg = cfg
        self.store = store
        self.ws = workspace
        self.client = client
        self.broadcast = broadcast
        self.running = False
        self.status: dict[str, str] = {a.name: "idle" for a in cfg.agents}
        self._task: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._priority: list[str] = []
        self._rr = 0
        for ch in cfg.channels:
            self.store.ensure_channel(ch["name"], ch.get("topic", ""))

    # scheduling -----------------------------------------------------------
    def agent(self, name: str) -> AgentSpec | None:
        return next((a for a in self.cfg.enabled_agents() if a.name.lower() == name.lower()), None)

    def mentioned(self, text: str) -> list[str]:
        found = []
        for a in self.cfg.enabled_agents():
            if re.search(rf"@{re.escape(a.name)}\b", text, re.IGNORECASE):
                found.append(a.name)
        return found

    def nudge(self, names: list[str]) -> None:
        for n in names:
            if n not in self._priority:
                self._priority.append(n)
        if names:
            self._wake.set()
            if not self.running:
                asyncio.create_task(self._run_priority_once())

    def _next_agent(self) -> AgentSpec | None:
        while self._priority:
            a = self.agent(self._priority.pop(0))
            if a:
                return a
        agents = self.cfg.enabled_agents()
        if not agents:
            return None
        a = agents[self._rr % len(agents)]
        self._rr += 1
        return a

    async def _run_priority_once(self) -> None:
        """While paused, @mentions still get a single reply from each mentioned agent."""
        while self._priority and not self.running:
            a = self.agent(self._priority.pop(0))
            if a:
                await self.take_turn(a)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        await self.broadcast({"type": "village", "running": True})

    async def pause(self) -> None:
        self.running = False
        self._wake.set()
        await self.broadcast({"type": "village", "running": False})

    async def _loop(self) -> None:
        while self.running:
            a = self._next_agent()
            if a is None:
                await self.pause()
                return
            await self.take_turn(a)
            self._wake.clear()
            try:  # sleep between turns, but wake early for @mentions
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.turn_interval)
            except asyncio.TimeoutError:
                pass

    # posting --------------------------------------------------------------
    async def post(self, channel: str, author: str, kind: str, content: str,
                   attachments: list[str] | None = None) -> dict:
        msg = self.store.add_message(channel, author, kind, content, attachments)
        await self.broadcast({"type": "message", "message": msg})
        return msg

    async def human_post(self, channel: str, content: str) -> dict:
        msg = await self.post(channel, self.cfg.user_name, "human", content)
        self.nudge(self.mentioned(content))
        return msg

    async def _set_status(self, name: str, status: str, channel: str | None = None) -> None:
        self.status[name] = status
        await self.broadcast({"type": "status", "agent": name, "status": status, "channel": channel})

    # a single turn --------------------------------------------------------
    def _context(self, a: AgentSpec, last_seen: int) -> tuple[str, str, int]:
        msgs = self.store.recent_messages(self.cfg.context_messages)
        top = msgs[-1]["id"] if msgs else 0
        lines, focus = [], "general"
        for m in msgs:
            new = m["id"] > last_seen and m["author"] != a.name
            if new:
                focus = m["channel"]
            att = f" [attached: {', '.join(m['attachments'])}]" if m["attachments"] else ""
            lines.append(f"{'NEW ' if new else ''}[#{m['channel']} {_fmt_time(m['created_at'])}] "
                         f"{m['author']}: {m['content']}{att}")
        body = "\n".join(lines) or "(no messages yet - you're among the first to speak)"
        return f"Recent messages across all channels (oldest first):\n{body}\n\nIt's your turn.", focus, top

    def _system_prompt(self, a: AgentSpec, memory: str) -> str:
        roster = ", ".join(f"{x.name} ({x.model})" for x in self.cfg.enabled_agents())
        chans = ", ".join(f"#{c['name']}" + (f" ({c['topic']})" if c["topic"] else "")
                          for c in self.store.list_channels())
        return SYSTEM_TEMPLATE.format(
            name=a.name, user=self.cfg.user_name, model=a.model,
            persona=f"Your personality: {a.persona}\n" if a.persona else "",
            goal=self.cfg.village_goal, roster=roster, channels=chans,
            python_note="" if self.cfg.allow_python else " (run_python is disabled right now.)",
            steps=self.cfg.max_steps_per_turn, memory=memory or "(empty)")

    async def take_turn(self, a: AgentSpec) -> None:
        async with self._turn_lock:
            state = self.store.agent_state(a.name)
            user_msg, focus, top = self._context(a, state["last_seen"])
            messages = [{"role": "system", "content": self._system_prompt(a, state["memory"])},
                        {"role": "user", "content": user_msg}]
            await self._set_status(a.name, "thinking", focus)
            posted = False
            try:
                for _ in range(self.cfg.max_steps_per_turn):
                    reply = await self.client.chat(a.model, messages, TOOLS)
                    calls = reply.get("tool_calls") or []
                    content = (reply.get("content") or "").strip()
                    if not calls:
                        # Models that answer in plain text instead of calling post_message.
                        if content and not posted:
                            await self.post(focus, a.name, "agent", content)
                        break
                    messages.append({"role": "assistant", "content": reply.get("content") or "",
                                     "tool_calls": calls})
                    done = False
                    for call in calls:
                        name = call.get("function", {}).get("name", "")
                        result = await self._run_tool(a, name, _args(call))
                        if name == "post_message" and not result.startswith("error"):
                            posted = True
                        if name == "end_turn":
                            done = True
                        messages.append({"role": "tool", "tool_name": name, "content": result})
                    if done:
                        break
            except Exception as e:  # keep the village alive if one model misbehaves
                log.exception("turn failed for %s", a.name)
                await self.broadcast({"type": "error", "agent": a.name, "error": str(e)[:500]})
            finally:
                self.store.set_agent_state(a.name, last_seen=top)
                await self._set_status(a.name, "idle")

    async def _run_tool(self, a: AgentSpec, name: str, args: dict) -> str:
        try:
            return await self._dispatch(a, name, args)
        except (WorkspaceError, ValueError, OSError) as e:
            return f"error: {e}"

    async def _dispatch(self, a: AgentSpec, name: str, args: dict) -> str:
        if name == "post_message":
            ch = normalize_channel(str(args.get("channel", "general")))
            if not self.store.channel_exists(ch):
                return f"error: no channel #{ch}. Create it first or use an existing one."
            content = str(args.get("content", "")).strip()
            if not content:
                return "error: empty message"
            atts = [str(p) for p in args.get("attachments") or []]
            for p in atts:
                if not self.ws.resolve(p).is_file():
                    return f"error: attachment not found: {p}"
            await self._set_status(a.name, "thinking", ch)
            await self.post(ch, a.name, "agent", content, atts)
            if self.running:  # while paused only the human's @mentions wake agents
                self.nudge([n for n in self.mentioned(content) if n != a.name])
            return f"posted to #{ch}"
        if name == "create_channel":
            ch, created = self.store.ensure_channel(str(args.get("name", "")), str(args.get("topic", "")), a.name)
            if created:
                await self.broadcast({"type": "channels", "channels": self.store.list_channels()})
                await self.post(ch, "system", "system", f"**{a.name}** created #{ch}")
                return f"created #{ch}"
            return f"#{ch} already exists"
        if name == "read_channel":
            ch = normalize_channel(str(args.get("channel", "general")))
            limit = max(1, min(int(args.get("limit") or 20), 100))
            msgs = self.store.get_messages(ch, limit)
            return "\n".join(f"[{_fmt_time(m['created_at'])}] {m['author']}: {m['content']}" for m in msgs) \
                or f"#{ch} is empty"
        if name == "list_files":
            items = self.ws.list_files(str(args.get("path") or "."))
            return "\n".join(f"{i['path']}{'/' if i['is_dir'] else ''}  ({i['size']} B)" for i in items) \
                or "(empty)"
        if name == "read_file":
            return self.ws.read_file(str(args["path"]))
        if name == "write_file":
            rel = self.ws.write_file(str(args["path"]), str(args.get("content", "")), bool(args.get("append")))
            await self.broadcast({"type": "files", "by": a.name, "paths": [rel]})
            return f"wrote {rel}"
        if name == "run_python":
            if not self.cfg.allow_python:
                return "error: run_python is disabled in this village"
            await self._set_status(a.name, "running python")
            res = await asyncio.to_thread(
                self.ws.run_python, code=args.get("code"), path=args.get("path"),
                args=[str(x) for x in args.get("args") or []],
                timeout=self.cfg.python_timeout, tag=re.sub(r"\W", "", a.name) or "agent")
            await self._set_status(a.name, "thinking")
            if res["files_changed"]:
                await self.broadcast({"type": "files", "by": a.name, "paths": res["files_changed"]})
            return json.dumps(res, indent=1)
        if name == "update_memory":
            self.store.set_agent_state(a.name, memory=str(args.get("notes", ""))[:6000])
            return "notebook updated"
        if name == "end_turn":
            return "ok"
        return f"error: unknown tool {name}"

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "goal": self.cfg.village_goal,
            "user_name": self.cfg.user_name,
            "shared_folder": str(self.cfg.shared_path),
            "mode": self.cfg.mode,
            "channels": self.store.list_channels(),
            "agents": [{"name": a.name, "model": a.model, "color": a.color, "persona": a.persona,
                        "status": self.status.get(a.name, "idle")} for a in self.cfg.enabled_agents()],
            "ts": time.time(),
        }
