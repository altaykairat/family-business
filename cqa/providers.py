"""LLM provider abstraction (L.3). Every LLM step of the platform — evaluation prompts of a run and the
orchestrator's tasks — is a *prompt file* in and a *response file* out. A provider is what turns one into
the other:

  file     — manual mode (now): a human pastes the prompt into a fresh chat and saves the answer to the file;
  command  — a CLI that reads the prompt on stdin and prints the answer (`claude -p`, `codex exec -`,
             `gemini -p`, a local model wrapper); disabled until switched on in config/providers.json;
  http     — an HTTP API (OpenAI-compatible / Anthropic / local server): contract only, not implemented yet.

The pipeline never depends on which provider produced a file, so switching to API or a local model needs no
change in the pipeline. Providers run only when a human presses the button (decision 11).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from . import common
from .common import CQAError, load_json


def config() -> dict:
    p = common.CONFIG / "providers.json"
    return load_json(p) if p.exists() else {"default": "manual", "providers": {"manual": {"type": "file"}}}


def get(name: Optional[str] = None, agent: Optional[str] = None) -> "Provider":
    cfg = config()
    name = name or cfg.get("agents", {}).get(agent or "", None) or cfg.get("default", "manual")
    spec = cfg.get("providers", {}).get(name)
    if spec is None:
        raise CQAError(f"provider {name!r} is not configured in config/providers.json", "not_found")
    kind = spec.get("type")
    if kind == "file":
        return FileProvider(name, spec)
    if kind == "command":
        return CommandProvider(name, spec)
    if kind == "http":
        return HttpProvider(name, spec)
    raise CQAError(f"provider {name!r}: unknown type {kind!r}")


class Provider:
    manual = False

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec

    def describe(self) -> dict:
        return {"name": self.name, "type": self.spec.get("type"), "enabled": self.enabled(), "manual": self.manual,
                "model": self.spec.get("model")}

    def enabled(self) -> bool:
        return bool(self.spec.get("enabled", True))

    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        """Produce the response file for one prompt file. Returns {'status', 'seconds', ...}."""
        raise NotImplementedError


class FileProvider(Provider):
    """Manual mode: nothing to execute — the human is the transport."""

    manual = True

    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        if response_path.exists() and response_path.read_text(encoding="utf-8").strip():
            return {"status": "done", "response": common.rel(response_path)}
        return {"status": "waiting_for_human", "prompt": common.rel(prompt_path),
                "paste_response_to": common.rel(response_path)}


class CommandProvider(Provider):
    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        if not self.enabled():
            raise CQAError(f"provider {self.name} is disabled (config/providers.json → enabled: true)", "forbidden")
        cmd = self.spec.get("command")
        if not cmd:
            raise CQAError(f"provider {self.name}: no command configured")
        started = time.time()
        try:
            proc = subprocess.run(cmd, input=prompt_path.read_text(encoding="utf-8"), capture_output=True,
                                  text=True, timeout=float(self.spec.get("timeout_s", 300)),
                                  cwd=self.spec.get("cwd") or str(prompt_path.parent))
        except FileNotFoundError:
            raise CQAError(f"provider {self.name}: command {cmd[0]!r} not found")
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "seconds": round(time.time() - started, 1)}
        seconds = round(time.time() - started, 1)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"status": "failed", "seconds": seconds, "stderr": proc.stderr[-2000:]}
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(proc.stdout, encoding="utf-8")
        return {"status": "done", "seconds": seconds, "response": common.rel(response_path)}


class HttpProvider(Provider):
    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        raise CQAError(f"provider {self.name}: HTTP providers are not implemented yet (contract in "
                       "docs/team/implementation.md)", "not_implemented")
