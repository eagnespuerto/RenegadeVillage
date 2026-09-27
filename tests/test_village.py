import asyncio
import json

import pytest

from renegade_village.config import AgentSpec, Config
from renegade_village.ollama_client import is_cloud_entry, _filter_models
from renegade_village.store import Store, normalize_channel
from renegade_village.village import Village
from renegade_village.workspace import Workspace, WorkspaceError


class FakeClient:
    """Replays scripted assistant messages per model."""

    def __init__(self, scripts: dict[str, list[dict]]):
        self.scripts = scripts
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, model, messages, tools=None):
        self.calls.append((model, [dict(m) for m in messages]))
        script = self.scripts.get(model, [])
        return script.pop(0) if script else {"role": "assistant", "content": ""}

    async def aclose(self):
        pass


def call(tool, **args):
    return {"function": {"name": tool, "arguments": args}}


def make_village(tmp_path, scripts, **cfg_kw):
    cfg = Config(shared_folder=str(tmp_path / "shared"), turn_interval=0, typing_seconds_max=0, **cfg_kw)
    cfg.agents = [AgentSpec("alpha", "gpt-oss:120b-cloud"), AgentSpec("beta", "qwen3-coder:480b-cloud")]
    ws = Workspace(cfg.shared_path)
    store = Store(ws.internal / "village.db")
    events = []

    async def broadcast(ev):
        events.append(ev)

    client = FakeClient(scripts)
    return Village(cfg, store, ws, client, broadcast), client, events


def test_cloud_filter():
    local = {"models": [{"name": "llama3:8b"}, {"name": "gpt-oss:120b-cloud"},
                        {"name": "glm-4.6:cloud"}, {"name": "x:latest", "remote_host": "https://ollama.com"}]}
    assert _filter_models("local", local) == ["glm-4.6:cloud", "gpt-oss:120b-cloud", "x:latest"]
    assert _filter_models("direct", {"models": [{"name": "gpt-oss:120b"}]}) == ["gpt-oss:120b"]
    assert not is_cloud_entry({"name": "llama3:8b"})


def test_normalize_channel():
    assert normalize_channel("#Solar Sim_Results!") == "solar-sim-results"


def test_workspace_confinement(tmp_path):
    ws = Workspace(tmp_path / "shared")
    for bad in ["../x.txt", "..\\..\\x", ".village/village.db", "/../../etc"]:
        with pytest.raises(WorkspaceError):
            ws.write_file(bad, "nope")
    assert ws.write_file("/notes/plan.md", "# hi") == "notes/plan.md"
    assert ws.read_file("notes/plan.md") == "# hi"
    assert [f["path"] for f in ws.list_files()] == ["notes"]  # .village hidden


def test_run_python_reports_output_and_new_files(tmp_path):
    ws = Workspace(tmp_path / "shared")
    res = ws.run_python(code="open('out.csv','w').write('a,b\\n'); print(6*7)", timeout=30)
    assert res["returncode"] == 0 and res["stdout"].strip() == "42"
    assert res["files_changed"] == ["out.csv"]
    res = ws.run_python(code="import time; time.sleep(5)", timeout=1)
    assert res["timed_out"]


def test_turn_with_tools(tmp_path):
    scripts = {"gpt-oss:120b-cloud": [
        {"role": "assistant", "content": "", "tool_calls": [
            call("write_file", path="sim.py", content="print('orbits ok')"),
            call("run_python", path="sim.py")]},
        {"role": "assistant", "content": "", "tool_calls": [
            call("create_channel", name="Orbit Sim", topic="n-body"),
            call("post_message", channel="orbit-sim", content="Sim runs! @beta take a look",
                 attachments=["sim.py"]),
            call("update_memory", notes="built sim.py"),
            call("end_turn")]},
    ]}
    v, client, events = make_village(tmp_path, scripts)

    async def go():
        v.running = True  # so the @mention queues beta without spawning a task
        await v.take_turn(v.agent("alpha"))

    asyncio.run(go())
    msgs = v.store.get_messages("orbit-sim")
    assert [m["author"] for m in msgs] == ["system", "alpha"]
    assert msgs[1]["attachments"] == ["sim.py"]
    assert v.store.agent_state("alpha")["memory"] == "built sim.py"
    assert v._priority == ["beta"]
    tool_results = [m for m in client.calls[1][1] if m["role"] == "tool"]
    assert "orbits ok" in json.loads(tool_results[1]["content"])["stdout"]
    assert any(e["type"] == "files" for e in events)


def test_plain_text_reply_posts_to_focus_channel(tmp_path):
    scripts = {"qwen3-coder:480b-cloud": [{"role": "assistant", "content": "hello from beta"}]}
    v, client, _ = make_village(tmp_path, scripts)

    async def go():
        await v.human_post("projects", "anyone here?")
        await v.take_turn(v.agent("beta"))

    asyncio.run(go())
    assert [m["content"] for m in v.store.get_messages("projects")] == ["anyone here?", "hello from beta"]
    ctx = client.calls[0][1][1]["content"]
    assert "## #projects" in ctx and "(1 new)" in ctx and "NEW [" in ctx
    assert "Quiet channels" in ctx and "#simulations" in ctx
    # next turn: the human message is no longer marked new
    assert v.store.agent_state("beta")["last_seen"] >= 1


def test_bad_tool_args_become_errors_not_crashes(tmp_path):
    scripts = {"gpt-oss:120b-cloud": [
        {"role": "assistant", "content": "", "tool_calls": [
            call("post_message", channel="nowhere", content="hi"),
            call("read_file", path="../../secret"),
            call("fly_to_moon")]},
        {"role": "assistant", "content": "", "tool_calls": [call("end_turn")]},
    ]}
    v, client, events = make_village(tmp_path, scripts)
    asyncio.run(v.take_turn(v.agent("alpha")))
    results = [m["content"] for m in client.calls[1][1] if m["role"] == "tool"]
    assert all(r.startswith("error") for r in results)
    assert not any(e["type"] == "error" for e in events)


def test_rename_user_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from renegade_village.server import create_app

    cfg = Config(shared_folder=str(tmp_path / "shared"))
    cfg.agents = [AgentSpec("alpha", "gpt-oss:120b-cloud")]
    app = create_app(cfg, client=FakeClient({}), save_config=False)
    with TestClient(app) as c:
        c.post("/api/channels/general/messages", json={"content": "hi"})
        assert c.put("/api/user", json={"name": "  Agnes  P "}).json() == {"name": "Agnes P"}
        assert c.put("/api/user", json={"name": "ALPHA"}).status_code == 400
        assert c.put("/api/user", json={"name": "   "}).status_code == 400
        msgs = c.get("/api/channels/general/messages").json()
        assert msgs[0]["author"] == "Agnes P"
        assert "now known as" in msgs[-1]["content"]
        assert c.get("/api/state").json()["user_name"] == "Agnes P"


def test_out_of_rounds_forces_a_summary_post(tmp_path):
    busy = {"role": "assistant", "content": "", "tool_calls": [call("list_files")]}
    scripts = {"gpt-oss:120b-cloud": [dict(busy) for _ in range(6)] + [
        {"role": "assistant", "content": "", "tool_calls": [
            call("post_message", channel="simulations", content="Looked through the files, all good.")]}]}
    v, client, events = make_village(tmp_path, scripts)
    asyncio.run(v.take_turn(v.agent("alpha")))
    assert len(client.calls) == 7
    # the wrap-up call only offers post_message
    assert [m["content"] for m in v.store.get_messages("simulations")] == ["Looked through the files, all good."]
    # typing status always comes right before a message, and the turn ends idle
    statuses = [(e["status"], e.get("channel")) for e in events if e["type"] == "status"]
    assert statuses[0] == ("thinking", None) and statuses[-1] == ("idle", None)
    assert ("typing", "simulations") in statuses
    kinds = [e["type"] for e in events]
    typing_ev = [k for k, e in enumerate(events) if e.get("status") == "typing"][0]
    assert kinds[typing_ev + 1] == "message"


def test_no_summary_when_agent_ends_turn_quietly(tmp_path):
    scripts = {"gpt-oss:120b-cloud": [{"role": "assistant", "content": "", "tool_calls": [call("end_turn")]}]}
    v, client, events = make_village(tmp_path, scripts)
    asyncio.run(v.take_turn(v.agent("alpha")))
    assert len(client.calls) == 1
    assert not any(e.get("status") == "typing" for e in events)


def test_context_lists_every_active_channel(tmp_path):
    v, _, _ = make_village(tmp_path, {})
    for i in range(30):
        v.store.add_message("general", "beta", "agent", f"general chatter {i}")
    v.store.add_message("docs", "beta", "agent", "wrote docs/plan.md")
    ctx, focus, _ = v._context(v.agent("alpha"), 0)
    assert "## #docs" in ctx and "wrote docs/plan.md" in ctx  # not crowded out by #general
    assert ctx.index("## #docs") < ctx.index("## #general")  # most recent news first
    assert focus == "docs"


def test_chat_retries_on_429(monkeypatch):
    import httpx
    from renegade_village import ollama_client as oc

    monkeypatch.setattr(oc, "RETRY_WAITS", [0, 0])
    hits = []

    def handler(request):
        hits.append(1)
        if len(hits) < 3:
            return httpx.Response(429, json={"error": "too many concurrent requests"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"}})

    async def go():
        c = oc.OllamaClient("local", "http://x", "")
        c._http = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
        return await c.chat("m", [])

    assert asyncio.run(go())["content"] == "hi" and len(hits) == 3


def test_work_after_last_post_gets_reported(tmp_path):
    scripts = {"gpt-oss:120b-cloud": [
        {"role": "assistant", "content": "", "tool_calls": [call("post_message", channel="general", content="on it")]},
        *[{"role": "assistant", "content": "", "tool_calls": [call("write_file", path=f"f{i}.md", content="x")]}
          for i in range(5)],
        {"role": "assistant", "content": "", "tool_calls": [
            call("post_message", channel="docs", content="Wrote f0.md..f4.md")]}]}
    v, client, _ = make_village(tmp_path, scripts)
    asyncio.run(v.take_turn(v.agent("alpha")))
    assert [m["content"] for m in v.store.get_messages("docs")] == ["Wrote f0.md..f4.md"]


def test_reorder_channels_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from renegade_village.server import create_app

    cfg = Config(shared_folder=str(tmp_path / "shared"))
    cfg.agents = [AgentSpec("alpha", "gpt-oss:120b-cloud")]
    app = create_app(cfg, client=FakeClient({}), save_config=False)
    with TestClient(app) as c:
        names = lambda: [ch["name"] for ch in c.get("/api/state").json()["channels"]]
        assert names() == ["general", "projects", "simulations", "docs", "random"]
        assert c.put("/api/channels/order", json={"names": ["docs", "#General"]}).status_code == 200
        assert names() == ["docs", "general", "projects", "simulations", "random"]
        c.post("/api/channels", json={"name": "new-one"})
        assert names()[-1] == "new-one"  # new channels go to the bottom
        assert c.put("/api/channels/order", json={"names": ["nope"]}).status_code == 400


def test_old_database_gets_channel_positions(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE channels (name TEXT PRIMARY KEY, topic TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT 'system', created_at REAL NOT NULL);
        INSERT INTO channels VALUES ('b', '', 'system', 2), ('a', '', 'system', 1);""")
    con.commit(); con.close()
    store = Store(db)
    assert [c["name"] for c in store.list_channels()] == ["a", "b"]
    store.reorder_channels(["b"])
    assert [c["name"] for c in store.list_channels()] == ["b", "a"]
