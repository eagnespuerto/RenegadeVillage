"""The shared folder: file access confined to it, plus Python execution inside it."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

MAX_READ = 20_000
MAX_OUTPUT = 8_000
INTERNAL_DIR = ".village"  # db + run scripts; hidden from agents' listings


class WorkspaceError(ValueError):
    pass


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.internal = self.root / INTERNAL_DIR
        self.internal.mkdir(exist_ok=True)

    def resolve(self, rel: str) -> Path:
        rel = (rel or ".").replace("\\", "/").lstrip("/")
        p = (self.root / rel).resolve()
        if p != self.root and not p.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the shared folder: {rel}")
        if p == self.internal or p.is_relative_to(self.internal):
            raise WorkspaceError(f"{INTERNAL_DIR}/ is reserved")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    # files ----------------------------------------------------------------
    def list_files(self, path: str = ".") -> list[dict]:
        base = self.resolve(path)
        if not base.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        out = []
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            st = p.stat()
            out.append({"name": p.name, "path": self.rel(p), "is_dir": p.is_dir(),
                        "size": st.st_size, "mtime": st.st_mtime})
        return out

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise WorkspaceError(f"no such file: {path}")
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"[binary file, {len(data)} bytes]"
        return _truncate(text, MAX_READ)

    def write_file(self, path: str, content: str, append: bool = False) -> str:
        p = self.resolve(path)
        if p == self.root or p.is_dir():
            raise WorkspaceError(f"is a directory: {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return self.rel(p)

    def _snapshot(self) -> dict[str, float]:
        snap = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    snap[self.rel(p)] = p.stat().st_mtime
                except OSError:
                    pass
        return snap

    # python ---------------------------------------------------------------
    def run_python(self, *, code: str | None = None, path: str | None = None,
                   args: list[str] | None = None, timeout: int = 60, tag: str = "run") -> dict:
        """Run a snippet or a script from the shared folder, cwd = shared folder.

        Not a security sandbox: code runs as the current user. The folder confinement
        only applies to the file tools, not to what Python itself can do.
        """
        if bool(code) == bool(path):
            raise WorkspaceError("give exactly one of `code` or `path`")
        if code:
            runs = self.internal / "runs"
            runs.mkdir(exist_ok=True)
            script = runs / f"{tag}_{int(time.time() * 1000)}.py"
            script.write_text(code, encoding="utf-8")
        else:
            script = self.resolve(path)
            if not script.is_file():
                raise WorkspaceError(f"no such script: {path}")
        env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8"}
        before = self._snapshot()
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *(args or [])],
                cwd=self.root, env=env, capture_output=True, timeout=timeout,
                text=True, encoding="utf-8", errors="replace",
            )
            rc, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired as e:
            rc, timed_out = None, True
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        after = self._snapshot()
        changed = sorted(k for k, m in after.items() if before.get(k) != m)
        return {
            "returncode": rc,
            "timed_out": timed_out,
            "seconds": round(time.time() - t0, 2),
            "stdout": _truncate(out, MAX_OUTPUT),
            "stderr": _truncate(err, MAX_OUTPUT),
            "files_changed": changed,
        }
