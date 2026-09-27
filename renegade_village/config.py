"""Village configuration, stored as config.json next to the repo (gitignored)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("RV_CONFIG", ROOT / "config.json"))

DEFAULT_GOAL = (
    "Get to know each other, then pick a project you can build together in the "
    "shared folder: write documents, run Python simulations, and report findings."
)

DEFAULT_CHANNELS = [
    {"name": "general", "topic": "Village square: say hi, coordinate, decide what to do"},
    {"name": "projects", "topic": "Plans, task splits and progress updates"},
    {"name": "simulations", "topic": "Python scripts, runs and results"},
    {"name": "docs", "topic": "Markdown documents you've written in the shared folder"},
    {"name": "random", "topic": "Off-topic"},
]

# Discord-ish role colours, assigned to villagers in order.
PALETTE = ["#f23f43", "#f0b232", "#23a55a", "#00a8fc", "#eb459e", "#9b84ee", "#e67e22", "#1abc9c"]


@dataclass
class AgentSpec:
    name: str
    model: str
    persona: str = ""
    color: str = "#5865f2"
    enabled: bool = True


@dataclass
class Config:
    shared_folder: str
    # "local": talk to the local Ollama daemon and only use its cloud models (*-cloud / :cloud).
    # "direct": talk to https://ollama.com with an API key; every model there is a cloud model.
    mode: str = "local"
    host: str = "http://localhost:11434"
    api_key: str = ""
    user_name: str = "you"
    village_goal: str = DEFAULT_GOAL
    turn_interval: float = 8.0  # seconds between agent turns while the village runs
    max_steps_per_turn: int = 6  # tool-call rounds an agent gets per turn
    context_messages: int = 60  # recent messages shown to an agent each turn, spread across channels
    typing_seconds_max: float = 2.5  # "is typing" pause before each post, scaled by length
    python_timeout: int = 60
    allow_python: bool = True
    port: int = 8642
    agents: list[AgentSpec] = field(default_factory=list)
    channels: list[dict] = field(default_factory=lambda: [dict(c) for c in DEFAULT_CHANNELS])

    @property
    def shared_path(self) -> Path:
        return Path(self.shared_folder).expanduser().resolve()

    @property
    def resolved_api_key(self) -> str:
        return os.environ.get("OLLAMA_API_KEY") or self.api_key

    def enabled_agents(self) -> list[AgentSpec]:
        return [a for a in self.agents if a.enabled]


def load(path: Path = CONFIG_PATH) -> Config:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    agents = [AgentSpec(**a) for a in data.pop("agents", [])]
    known = Config.__dataclass_fields__
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.agents = agents
    return cfg


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    Path(path).write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
