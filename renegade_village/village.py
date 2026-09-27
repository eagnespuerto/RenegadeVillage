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
- It is a Discord-style server with several text channels. Everyone takes turns; this is your turn.
- Talk ONLY by calling post_message. Keep messages conversational and reasonably short, like chat.
  Mention others with @name.
- Use the channels like a real server: post each thing where it belongs, going by the channel topics
  (e.g. plans and task splits in #projects, code and results in #simulations, new documents in #docs,
  coordination in #general). Don't pile everything into one channel. Answer people where they asked,
  and feel free to post in more than one channel in a turn. Create a channel when a project needs its own.
- There is a shared folder for real work. Write Markdown documents, Python scripts and data there with
  write_file, and run code with run_python.{python_note}
- Your private notebook (update_memory) is your only memory between turns. Use it.
- Don't repeat what's already been said. If nothing needs you right now, just call end_turn.
- You have {steps} tool rounds this turn. If you do work (write files, run code), tell the others about it
  with post_message before your rounds run out - work nobody hears about is wasted.

Villagers: {roster}

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
        """Every channel with its own recent messages, so quiet channels stay in view."""
        chans = self.store.list_channels()
        per = max(4, self.cfg.context_messages // max(1, len(chans)))
        top, focus, focus_id = self.store.max_message_id(), "general", 0
        sections, quiet = [], []
        for c in chans:
            msgs = self.store.get_messages(c["name"], per)
            topic = f" - {c['topic']}" if c["topic"] else ""
            if not msgs:
                quiet.append(f"#{c['name']}{topic}")
                continue
            new_ids = {m["id"] for m in msgs if m["id"] > last_seen and m["author"] != a.name}
            if new_ids and max(new_ids) > focus_id:
                focus, focus_id = c["name"], max(new_ids)
            lines = []
            for m in msgs:
                att = f" [attached: {', '.join(m['attachments'])}]" if m["attachments"] else ""
                text = m["content"] if len(m["content"]) <= 1200 else m["content"][:1200] + " ..."
                mark = "NEW " if m["id"] in new_ids else ""
                lines.append(f"{mark}[{_fmt_time(m['created_at'])}] {m['author']}: {text}{att}")
            head = f"## #{c['name']}{topic}" + (f"  ({len(new_ids)} new)" if new_ids else "")
            sections.append((bool(new_ids), msgs[-1]["id"], head + "\n" + "\n".join(lines)))
        # channels with news first, then by latest activity
        sections.sort(key=lambda x: (x[0], x[1]), reverse=True)
        body = "\n\n".join(sec for _, _, sec in sections) or "(no messages yet - you're among the first to speak)"
        if quiet:
            body += "\n\nQuiet channels (no messages yet): " + ", ".join(quiet)
        return (f"Channels and their recent messages (NEW = since your last turn):\n\n{body}\n\n"
                "It's your turn. For each message you post, pick the channel whose topic fits it best; "
                "the busiest channel isn't automatically the right one."), focus, top

    def _system_prompt(self, a: AgentSpec, memory: str) -> str:
        roster = ", ".join(f"{x.name} ({x.model})" for x in self.cfg.enabled_agents())
        return SYSTEM_TEMPLATE.format(
            name=a.name, user=self.cfg.user_name, model=a.model,
            persona=f"Your personality: {a.persona}\n" if a.persona else "",
            goal=self.cfg.village_goal, roster=roster,
            python_note="" if self.cfg.allow_python else " (run_python is disabled right now.)",
            steps=self.cfg.max_steps_per_turn, memory=memory or "(empty)")

    async def take_turn(self, a: AgentSpec) -> None:
        async with self._turn_lock:
            state = self.store.agent_state(a.name)
            user_msg, focus, top = self._context(a, state["last_seen"])
            messages = [{"role": "system", "content": self._system_prompt(a, state["memory"])},
                        {"role": "user", "content": user_msg}]
            await self._set_status(a.name, "thinking")
            self._posted = False
            self._unreported = False  # wrote files / ran code since the last post
            try:
                for _ in range(self.cfg.max_steps_per_turn):
                    reply = await self.client.chat(a.model, messages, TOOLS)
                    calls = reply.get("tool_calls") or []
                    content = (reply.get("content") or "").strip()
                    if not calls:
                        # Models that answer in plain text instead of calling post_message.
                        if content and not self._posted:
                            await self._say(a, focus, content)
                        break
                    messages.append({"role": "assistant", "content": reply.get("content") or "",
                                     "tool_calls": calls})
                    done = False
                    for call in calls:
                        name = call.get("function", {}).get("name", "")
                        result = await self._run_tool(a, name, _args(call))
                        if name == "end_turn":
                            done = True
                        messages.append({"role": "tool", "tool_name": name, "content": result})
                    if done:
                        break
                else:
                    if not self._posted or self._unreported:
                        await self._wrap_up(a, messages, focus)
            except Exception as e:  # keep the village alive if one model misbehaves
                log.exception("turn failed for %s", a.name)
                await self.broadcast({"type": "error", "agent": a.name, "error": str(e)[:500]})
            finally:
                self.store.set_agent_state(a.name, last_seen=top)
                await self._set_status(a.name, "idle")

    async def _wrap_up(self, a: AgentSpec, messages: list[dict], focus: str) -> None:
        """Out of rounds without posting: one last call that can only post a summary."""
        messages.append({"role": "user", "content":
                         "You've used all your tool rounds for this turn and haven't told the others about "
                         "your latest work yet. Post ONE short message now saying what you did or found "
                         "(mention files by path), in the channel where it fits best."})
        post_only = [t for t in TOOLS if t["function"]["name"] == "post_message"]
        reply = await self.client.chat(a.model, messages, post_only)
        for call in reply.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "post_message":
                if not (await self._run_tool(a, "post_message", _args(call))).startswith("error"):
                    return
        content = (reply.get("content") or "").strip()
        if content:
            await self._say(a, focus, content)

    async def _say(self, a: AgentSpec, channel: str, content: str,
                   attachments: list[str] | None = None) -> None:
        """Show '<name> is typing' in the channel briefly, then post, so typing always ends in a message."""
        await self._set_status(a.name, "typing", channel)
        delay = min(self.cfg.typing_seconds_max, 0.5 + len(content) / 400)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.post(channel, a.name, "agent", content, attachments)
        self._posted = True
        self._unreported = False
        await self._set_status(a.name, "thinking")

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
            await self._say(a, ch, content, atts)
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
            self._unreported = True
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
            self._unreported = True
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
