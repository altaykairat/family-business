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

import os
import shutil
import subprocess
import tempfile
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
    """A local agent CLI as a stand-in for an API call: one prompt → one fresh session → one answer.

    spec keys:
      command   argv; placeholders {workdir}, {output}, {prompt_file} are substituted
      stdin     send the prompt on stdin (default true)
      isolated  run in a fresh empty temp directory outside the repository (default true): the agent sees no
                project files, project instructions or project memory — only the prompt
      output    "stdout" (default) or "file" — read the answer from {output}
      detach    start the program and return at once (IDE chat window): the answer is pasted by a human
      env       extra environment variables
      home      {"env": "CODEX_HOME", "links": ["~/.codex/auth.json"]} — a fresh empty config home for the agent in a
                separate temp directory (not readable from the working directory): only the listed files are linked,
                so global instructions (AGENTS.md, CLAUDE.md), memories and plugins of the user's setup are not loaded
    """

    def describe(self) -> dict:
        d = super().describe()
        exe = (self.spec.get("command") or [None])[0]
        d.update({"available": bool(exe and (Path(os.path.expanduser(exe)).exists() or shutil.which(exe))), "detach": bool(self.spec.get("detach")),
                  "isolated": self.spec.get("isolated", True), "label": self.spec.get("label") or self.name})
        return d

    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        if not self.enabled():
            raise CQAError(f"provider {self.name} is disabled (config/providers.json → enabled: true)", "forbidden")
        cmd = self.spec.get("command")
        if not cmd:
            raise CQAError(f"provider {self.name}: no command configured")
        workdir = Path(tempfile.mkdtemp(prefix="cqa-agent-")) if self.spec.get("isolated", True) else prompt_path.parent
        output = workdir / "answer.txt"
        subst = {"{workdir}": str(workdir), "{output}": str(output), "{prompt_file}": str(prompt_path.resolve())}
        argv = [_subst(a, subst) for a in cmd]
        env = {**os.environ, **{k: _subst(str(v), subst) for k, v in (self.spec.get("env") or {}).items()}}
        home = None
        if self.spec.get("home"):
            home = Path(tempfile.mkdtemp(prefix="cqa-agent-home-"))
            for link in self.spec["home"].get("links", []):
                src = Path(os.path.expanduser(link))
                if src.exists():
                    (home / src.name).symlink_to(src)
            env[self.spec["home"]["env"]] = str(home)
        prompt = prompt_path.read_text(encoding="utf-8")
        started = time.time()
        try:
            if self.spec.get("detach"):
                subprocess.Popen(argv, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return {"status": "opened", "seconds": 0, "workdir": str(workdir),
                        "note": "chat opened in the IDE — paste the answer back (manual step)"}
            proc = subprocess.run(argv, input=prompt if self.spec.get("stdin", True) else None, capture_output=True,
                                  text=True, timeout=float(self.spec.get("timeout_s", 300)), cwd=str(workdir), env=env)
        except FileNotFoundError:
            raise CQAError(f"provider {self.name}: command {cmd[0]!r} not found — fix the path in config/providers.json")
        except subprocess.TimeoutExpired:
            shutil.rmtree(workdir, ignore_errors=True) if self.spec.get("isolated", True) else None
            shutil.rmtree(home, ignore_errors=True) if home else None
            return {"status": "timeout", "seconds": round(time.time() - started, 1)}
        seconds = round(time.time() - started, 1)
        answer = output.read_text(encoding="utf-8") if self.spec.get("output") == "file" and output.exists() else proc.stdout
        if self.spec.get("isolated", True):
            shutil.rmtree(workdir, ignore_errors=True)
        if home:
            shutil.rmtree(home, ignore_errors=True)
        if proc.returncode != 0 or not answer.strip():
            log = (proc.stderr or "") + "\n" + (proc.stdout or "")
            errors = list(dict.fromkeys(l.strip() for l in log.splitlines()
                                        if l.strip().lower().startswith(("error", "fatal")) or "usage limit" in l.lower()))
            return {"status": "failed", "seconds": seconds, "returncode": proc.returncode,
                    "error": " | ".join(errors)[:1000] or None, "stderr": log[-2000:]}
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(answer, encoding="utf-8")
        return {"status": "done", "seconds": seconds, "response": common.rel(response_path)}


def _subst(value: str, subst: dict) -> str:
    for k, v in subst.items():
        value = value.replace(k, v)
    return os.path.expanduser(value)


class HttpProvider(Provider):
    def complete(self, prompt_path: Path, response_path: Path) -> dict:
        raise CQAError(f"provider {self.name}: HTTP providers are not implemented yet (contract in "
                       "docs/team/implementation.md)", "not_implemented")
