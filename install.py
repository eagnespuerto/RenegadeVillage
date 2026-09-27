"""RenegadeVillage installer.

Installs dependencies, asks for the shared folder, connects to Ollama and lets you
choose which *cloud* models become villagers. Writes config.json. Re-run any time.
"""
from __future__ import annotations

import getpass
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or default


def pick_folder(default: Path) -> Path:
    print("\nPick the shared folder where villagers read, write and run files.")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="RenegadeVillage shared folder", initialdir=str(default.parent),
                                         mustexist=False)
        root.destroy()
        if chosen:
            print(f"  -> {chosen}")
            return Path(chosen)
    except Exception as e:  # no display, no tkinter, etc.
        print(f"  (folder picker unavailable: {e})")
    return Path(ask("Shared folder path", str(default))).expanduser()


def default_name(model: str) -> str:
    """Villager name from the model name, e.g. 'qwen3-coder:480b-cloud' -> 'qwen3-coder'."""
    base = model.split("/")[-1].split(":")[0]
    return re.sub(r"[^A-Za-z0-9-]", "-", base).strip("-") or "villager"


def choose(models: list[str]) -> list[str]:
    print("\nCloud models available:")
    for i, m in enumerate(models, 1):
        print(f"  {i:>2}. {m}")
    raw = ask("Which ones join the village? Numbers separated by commas, or 'all'", "all")
    if raw.lower() == "all":
        return models
    picked = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(models):
            picked.append(models[int(part) - 1])
    return picked


def main() -> None:
    print("== RenegadeVillage setup ==")
    if "--skip-deps" not in sys.argv:
        print("\nInstalling Python dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])

    from renegade_village import config as cfgmod
    from renegade_village.config import PALETTE, AgentSpec, Config
    from renegade_village.ollama_client import OllamaError, list_cloud_models_sync

    old = cfgmod.load() if cfgmod.CONFIG_PATH.exists() else None
    default_folder = Path(old.shared_folder) if old else Path.home() / "Documents" / "RenegadeVillage-shared"
    folder = pick_folder(default_folder)
    folder.mkdir(parents=True, exist_ok=True)

    print("\nHow should the village reach Ollama Cloud?")
    print("  1. Through the local Ollama app (run `ollama signin`, then pull *-cloud models,")
    print("     e.g. `ollama pull gpt-oss:120b-cloud`)")
    print("  2. Directly at ollama.com with an API key (https://ollama.com/settings/keys)")
    mode = "direct" if ask("Choose 1 or 2", "2" if old and old.mode == "direct" else "1") == "2" else "local"
    host, api_key = (old.host if old else "http://localhost:11434"), ""
    if mode == "direct":
        api_key = getpass.getpass("Ollama API key (leave blank to use the OLLAMA_API_KEY env var or the saved one): ").strip()
        if not api_key and old:
            api_key = old.api_key
    else:
        host = ask("Ollama host", host)

    import os
    try:
        models = list_cloud_models_sync(mode, host, api_key or os.environ.get("OLLAMA_API_KEY", ""))
    except OllamaError as e:
        raise SystemExit(f"\n{e}")
    if not models:
        raise SystemExit(
            "\nNo cloud models found. In local mode, sign in with `ollama signin` and pull at least one\n"
            "cloud model, e.g. `ollama pull gpt-oss:120b-cloud`, then run this again.")

    picked = choose(models)
    if not picked:
        raise SystemExit("No models picked.")

    previous = {a.model: a for a in (old.agents if old else [])}
    agents, used = [], set()
    for i, m in enumerate(picked):
        prev = previous.get(m)
        name = prev.name if prev else default_name(m)
        n, k = name, 2
        while n.lower() in used:
            n, k = f"{name}-{k}", k + 1
        used.add(n.lower())
        agents.append(AgentSpec(name=n, model=m, persona=prev.persona if prev else "",
                                color=prev.color if prev else PALETTE[i % len(PALETTE)]))

    user = ask("\nYour display name in the chat", old.user_name if old else "you")
    cfg = old or Config(shared_folder=str(folder))
    cfg.shared_folder, cfg.mode, cfg.host, cfg.api_key = str(folder.resolve()), mode, host, api_key
    cfg.user_name, cfg.agents = user, agents
    cfgmod.save(cfg)

    print(f"\nSaved {cfgmod.CONFIG_PATH}")
    print("Villagers: " + ", ".join(f"{a.name} ({a.model})" for a in agents))
    print("Edit config.json to rename villagers, give them personas, or tune turn_interval.")
    print("\nStart the village with:  python run.py")


if __name__ == "__main__":
    main()
