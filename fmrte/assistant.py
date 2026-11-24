"""The AI assistant: the provider transport, the settings and the chat turn.

This is the part of the person's FM AI Assistant that belongs inside an editor.
It is deliberately small and honest about what it is:

* **Chat** talks to a third-party provider over its OpenAI-compatible
  ``/chat/completions`` endpoint. Command Code is the default (the same base URL
  and default model the assistant's own ``application.properties`` uses), and
  OpenRouter, any other OpenAI-compatible endpoint, or a local CLI can be
  selected instead.
* **The prompt** - instructions, the always-on findings, and the save's own
  facts - is assembled in :mod:`fmrte.aiprompt`; it was one of the three
  unrelated concerns this module used to hold at once.
* **The plans** - a tactic derived from the squad and a week of training - are
  generated in :mod:`fmrte.fmplans`.  The two helpers kept at the bottom of this
  module (:func:`build_tactic_plan`, :func:`build_training_plan`) are adapters
  that render the *object* shape an older caller imported; the panel and the
  model both use the generators themselves, so there is one plan per output.
* **The tools** are the catalogue in :mod:`fmrte.fmtools`.  This module calls
  ``fmtools.tool_definitions()`` and ``fmtools.execute_tool()`` directly - it no
  longer re-exports them under a second name, and it imports ``fmtools`` at
  module level rather than inside a function.

Nothing here writes into the game. The assistant proposes; the person applies.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import fmtools
from . import fmplans
from . import tacticfiles
from .aiprompt import (ALWAYS_ON, INSTRUCTIONS, build_prompt,  # noqa: F401
                       knowledge_context, resolve_active_club, save_context)
from .fmroles import POSITION_LABELS  # noqa: F401 - the plan output's labels

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

# The assistant's own defaults (application.properties / CommandCodeClient).
DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

# The bounds the assistant's own client uses, so a stalled or hostile provider
# cannot hang a chat or fill memory. CommandCodeClient: MAX_RESPONSE_BYTES and
# MAX_CATALOG_BYTES; CommandCodeKeyStore: MAX_STORED_BYTES;
# TacticFileService: MAX_TEMPLATE_BYTES. Written as the literals they are.
MAX_RESPONSE_BYTES = 2097152
MAX_CATALOG_BYTES = 8388608
MAX_KEY_BYTES = 65536
MAX_TEMPLATE_BYTES = tacticfiles.MAX_TEMPLATE_BYTES
REQUEST_TIMEOUT_SECONDS = 180.0

PROVIDERS: dict[str, dict[str, Any]] = {
    "commandcode": {
        "label": "Command Code",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "kind": "http",
        "note": "OpenAI-compatible. The assistant's default provider.",
    },
    "deepseek": {
        "label": "DeepSeek (Official API)",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "kind": "http",
        "note": "DeepSeek official API (deepseek-chat / deepseek-reasoner).",
    },
    "opencode_go": {
        "label": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "claude-sonnet-4-6",
        "kind": "http",
        "note": "OpenCode Go OpenAI-compatible gateway.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-sonnet",
        "kind": "http",
        "note": "Many models, including a free tier.",
    },
    "openai": {
        "label": "OpenAI-compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "kind": "http",
        "note": "Any endpoint that speaks /chat/completions.",
    },
    "gemini": {
        "label": "Google Gemini (OpenAI-compat)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "kind": "http",
        "note": "Gemini OpenAI-compatible endpoint using a Gemini API key.",
    },
    "claude_code": {
        "label": "Claude Code (local CLI)",
        "base_url": "",
        "model": "claude",
        "kind": "cli",
        "executable": "claude",
        "args": ["-p"],
        "note": "Runs the local Claude Code CLI (`claude -p`).",
    },
    "codex": {
        "label": "Codex (local CLI)",
        "base_url": "",
        "model": "codex",
        "kind": "cli",
        "executable": "codex",
        "args": ["exec", "--skip-git-repo-check"],
        "note": "Runs the local codex executable.",
    },
    "antigravity": {
        "label": "Antigravity (local CLI)",
        "base_url": "",
        "model": "agy",
        "kind": "cli",
        "executable": "agy",
        "args": ["exec", "--skip-git-repo-check"],
        "note": "Runs the local agy executable.",
    },
    "copilot": {
        "label": "GitHub Copilot (local CLI)",
        "base_url": "",
        "model": "copilot",
        "kind": "cli",
        "executable": "copilot",
        "args": ["-p"],
        "note": "Runs the local copilot executable; sign in first.",
    },
}

# The assistant's own status messages, so a failure reads the same way here.
STATUS_MESSAGES = {
    401: "Invalid API key. Disconnect and enter a valid provider key.",
    402: "The provider reports insufficient credits. Top up or pick a cheaper model.",
    403: "Access denied. Check that the key belongs to a provider plan.",
    404: "The provider has no model or endpoint matching this request.",
    429: "Rate limit reached. Wait before trying a new chat.",
}

# The statuses an OpenAI-compatible endpoint uses when it will not accept tool
# definitions: a malformed request (400), no such route or model (404), a
# request it will not process (422) and an unimplemented feature (501).
TOOLS_REJECTED_STATUSES = frozenset({400, 404, 422, 501})


class AssistantError(RuntimeError):
    """A failure safe to show in the chat; never contains a provider body.

    ``status`` is the HTTP status the provider answered with, when there was
    one: a caller that has to *decide* something about a failure (does this
    endpoint refuse tool definitions?) must read the number rather than search
    the human message for it, because a status message need not contain its own
    number at all - the 404 message does not.
    """

    def __init__(self, message: str, hint: str = "", status: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.status = status


# ---------------------------------------------------------------------------
# Settings and the key store
# ---------------------------------------------------------------------------

def data_directory() -> str:
    """Where the assistant keeps its own files, never the install folder."""
    base = os.environ.get("FMRTE_ASSISTANT_DIR", "")
    if base:
        return base
    home = os.path.expanduser("~")
    return os.path.join(home, ".fmrte-assistant")


def _settings_path() -> str:
    return os.path.join(data_directory(), "assistant.json")


def _key_path() -> str:
    return os.path.join(data_directory(), "provider.key")


@dataclass
class AssistantSettings:
    provider: str = "commandcode"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning: str = "Default"
    enabled: bool = True
    temperature: float = 0.4
    managed_club: str = ""

    @classmethod
    def load(cls) -> "AssistantSettings":
        settings = cls()
        try:
            with open(_settings_path(), encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return settings
        for name in ("provider", "base_url", "model", "reasoning", "managed_club"):
            value = stored.get(name)
            if isinstance(value, str) and value:
                setattr(settings, name, value)
        if isinstance(stored.get("enabled"), bool):
            settings.enabled = stored["enabled"]
        try:
            settings.temperature = float(stored.get("temperature", 0.4))
        except (TypeError, ValueError):
            pass
        return settings

    def save(self) -> None:
        os.makedirs(data_directory(), exist_ok=True)
        payload = {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "reasoning": self.reasoning,
            "enabled": self.enabled,
            "temperature": self.temperature,
            "managed_club": self.managed_club,
        }
        with open(_settings_path(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)

    def provider_info(self) -> dict[str, Any]:
        return PROVIDERS.get(self.provider, PROVIDERS["commandcode"])


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    """Windows DPAPI in the current-user scope, the way the assistant does it.

    The key is never written in plain text and never leaves this machine. On a
    platform without DPAPI nothing is stored, so the key stays in memory.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_string_buffer(data, len(data))
        source = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        target = DATA_BLOB()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if protect:
            ok = crypt32.CryptProtectData(ctypes.byref(source), None, None, None,
                                          None, flags, ctypes.byref(target))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None,
                                             None, None, flags,
                                             ctypes.byref(target))
        if not ok or not target.pbData or not target.cbData:
            return None
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)
    except Exception:  # noqa: BLE001 - no DPAPI means no stored key
        return None


class KeyStore:
    """Stores one provider key, encrypted with DPAPI when Windows offers it."""

    def __init__(self, path: str | None = None):
        self.path = path or _key_path()
        self._memory = ""

    def available(self) -> bool:
        return os.name == "nt"

    def present(self) -> bool:
        return bool(self._memory) or (self.available()
                                      and os.path.isfile(self.path))

    def load(self) -> str:
        if self._memory:
            return self._memory
        if not self.available() or not os.path.isfile(self.path):
            return ""
        try:
            import base64

            if os.path.getsize(self.path) > MAX_KEY_BYTES:
                return ""
            with open(self.path, "r", encoding="ascii") as handle:
                blob = base64.b64decode(handle.read().strip())
            plain = _dpapi(blob, protect=False)
            if not plain:
                return ""
            self._memory = plain.decode("utf-8", "replace").strip()
            return self._memory
        except (OSError, ValueError):
            return ""

    def save(self, key: str) -> bool:
        key = (key or "").strip()
        if not key:
            return False
        self._memory = key
        if not self.available():
            return False
        blob = _dpapi(key.encode("utf-8"), protect=True)
        if not blob:
            return False
        try:
            import base64

            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="ascii") as handle:
                handle.write(base64.b64encode(blob).decode("ascii"))
            os.replace(temporary, self.path)
            return True
        except OSError:
            return False

    def clear(self) -> None:
        self._memory = ""
        try:
            os.remove(self.path)
        except OSError:
            pass

    def describe(self) -> str:
        if not self.available():
            return "kept in memory for this session only"
        return ("saved on this PC (Windows DPAPI, current user)"
                if os.path.isfile(self.path) else "not saved")


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

def _endpoint(base_url: str) -> str:
    """Normalise a pasted base URL to one ``/chat/completions`` endpoint."""
    value = (base_url or DEFAULT_BASE_URL).strip()
    if not value.startswith(("https://", "http://")):
        raise AssistantError("The provider address must start with https://")
    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value + "/chat/completions"


def _models_endpoint(base_url: str) -> str:
    """Normalise a base URL to its OpenAI-compatible ``/models`` endpoint."""
    value = (base_url or DEFAULT_BASE_URL).strip()
    if not value.startswith(("https://", "http://")):
        raise AssistantError("The provider address must start with https://")
    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value + "/models"


def list_models(settings: AssistantSettings, key: str = "",
                timeout: float = 15.0) -> list[dict[str, str]]:
    """Fetch available model IDs from an OpenAI-compatible ``/models`` endpoint."""
    info = settings.provider_info()
    if info.get("kind") == "cli":
        model_id = info.get("model") or settings.model or settings.provider
        return [{"id": model_id, "name": f"{info.get('label', model_id)} (local CLI)"}]

    headers = {
        "Accept": "application/json",
        "User-Agent": "FM27-Studio/1.0",
    }
    if key and key.strip():
        headers["Authorization"] = f"Bearer {key.strip()}"
    request = urllib.request.Request(
        _models_endpoint(settings.base_url), method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:
            pass
        raise AssistantError(_status_message(exc.code), detail[:200],
                             status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise AssistantError(
            "Could not reach the provider's /models endpoint.",
            str(getattr(exc, "reason", ""))[:200]) from exc
    except TimeoutError as exc:
        raise AssistantError(
            f"Fetching models timed out after {int(timeout)} seconds.") from exc

    if len(raw) > MAX_CATALOG_BYTES:
        raise AssistantError("The provider's model catalog is too large to parse.")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise AssistantError("The provider returned an invalid JSON model list.") from exc

    items = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
    elif isinstance(payload, list):
        items = payload

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for entry in items:
        if isinstance(entry, str):
            mid, label = entry.strip(), entry.strip()
        elif isinstance(entry, dict):
            mid = str(entry.get("id") or entry.get("name") or "").strip()
            label = str(entry.get("name") or mid).strip()
        else:
            continue
        if mid and mid not in seen:
            seen.add(mid)
            out.append({"id": mid, "name": label})
    out.sort(key=lambda item: item["id"].lower())
    return out


def _status_message(status: int) -> str:
    return STATUS_MESSAGES.get(
        status,
        f"The provider request failed (HTTP {status}). Check the account or "
        "try again later.")


def _http_chat(settings: AssistantSettings, key: str,
               messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None,
               timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    if not key:
        raise AssistantError(
            "No API key is set for this provider.",
            "Open Settings → AI Assistant and paste a key.")
    payload_dict: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": settings.temperature,
        "stream": False,
    }
    if tools:
        payload_dict["tools"] = tools
        payload_dict["tool_choice"] = "auto"
    body = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(settings.base_url), data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": "FM27-Studio/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1024).decode("utf-8", "replace")
        except Exception:
            pass
        raise AssistantError(_status_message(exc.code), detail[:200],
                             status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise AssistantError(
            "The provider could not be reached. Check the connection.",
            str(getattr(exc, "reason", ""))[:200]) from exc
    except TimeoutError as exc:
        raise AssistantError(
            f"The provider did not answer within {int(timeout)} seconds.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AssistantError(
            "The provider's reply is too large. Ask a narrower question.")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise AssistantError("The provider sent a reply this app cannot read.") from exc
    if isinstance(payload, dict) and payload.get("error"):
        message = payload["error"]
        if isinstance(message, dict):
            message = message.get("message") or message.get("code") or "error"
        raise AssistantError(f"The provider reported: {message}")
    return payload if isinstance(payload, dict) else {}


def chat_completion(settings: AssistantSettings, key: str, prompt: str,
                    timeout: float = REQUEST_TIMEOUT_SECONDS) -> str:
    """One blocking completion. Returns the assistant's text."""
    info = settings.provider_info()
    if info.get("kind") == "cli":
        return _cli_completion(settings, info, prompt, timeout)
    payload = _http_chat(
        settings, key,
        [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        tools=None, timeout=timeout)
    return _extract_text(payload)


def _rejected_tool_definitions(exc: AssistantError) -> bool:
    """Whether a failure is the endpoint refusing the ``tools`` field itself.

    A provider that does not implement tool calling answers with one of
    :data:`TOOLS_REJECTED_STATUSES`; the status is authoritative whenever the
    transport recorded one, because the messages for 404 and 422 carry no
    number to search for.  Only a failure with no status at all falls back to
    reading the text, and then only to keep a prose-only failure degrading to a
    plain completion instead of dead-ending the turn.
    """
    if exc.status is not None:
        return exc.status in TOOLS_REJECTED_STATUSES
    text = f"{exc} {exc.hint or ''}"
    return any(str(code) in text for code in TOOLS_REJECTED_STATUSES)


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _extract_reasoning(payload: Any) -> str:
    """Extract provider reasoning text (``reasoning_content`` / ``reasoning`` / ``<think>``)."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = message.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        content = message.get("content")
        if isinstance(content, str):
            found = _THINK_RE.findall(content)
            if found:
                return "\n".join(part.strip() for part in found if part.strip())
    return ""


def _strip_think_tags(text: str) -> str:
    if not text or "<think" not in text.lower():
        return text
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned or text.strip()


def chat_with_tools(settings: AssistantSettings, key: str, db,
                    user_message: str,
                    history: list[dict[str, str]] | None = None,
                    club_name: str = "",
                    on_status: Callable[[str], None] | None = None,
                    on_event: Callable[[dict[str, Any]], None] | None = None,
                    timeout: float = REQUEST_TIMEOUT_SECONDS) -> str:
    """Run an FM AI Assistant tool-calling conversation turn against FMRTE data.

    ``history`` holds the turns *before* this one: the current turn is
    ``user_message`` and this function appends it, so the list passed here must
    not already contain it.  A caller that keeps the conversation itself (the
    panel does) must take the history snapshot before it records the message it
    is asking about; passing it again would end the request in two identical
    consecutive ``user`` messages, which wastes tokens and is rejected outright
    by strict gateways.  The duplicate is dropped here as well, so one caller
    cannot break the wire format, but the contract is the caller's.

    Optional ``on_event`` receives structured trace dictionaries for rich UIs
    (``step``, ``thinking``, ``tool_start``, ``tool_done``).
    """
    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 - UI hooks must never break chat
                pass

    info = settings.provider_info()
    if info.get("kind") == "cli" or db is None:
        t0 = time.perf_counter()
        prompt = build_prompt(db, user_message, club_name=club_name) if db is not None else user_message
        emit({"type": "step", "stage": "context", "label": "Prepared context",
              "ms": int((time.perf_counter() - t0) * 1000)})
        t1 = time.perf_counter()
        answer = chat_completion(settings, key, prompt, timeout=timeout)
        emit({"type": "step", "stage": "answer", "label": "Completed",
              "ms": int((time.perf_counter() - t1) * 1000)})
        return answer

    t_ctx = time.perf_counter()
    active_club = resolve_active_club(db, club_name)
    system_prompt = (
        f"{INSTRUCTIONS}\n\n"
        f"{knowledge_context()}\n\n"
        f"{save_context(db, club_name=active_club)}\n\n"
        "You have FM27 tools to query live/snapshot players, clubs, staff, "
        "fixtures, lineup optimization, squad-based tactic planning, training "
        "schedules, transfer shortlists, replacements, and Match Lab studies. "
        "Use tools instead of guessing. Keep tool results concise."
    )
    emit({"type": "step", "stage": "context", "label": f"Loaded context ({active_club})",
          "ms": int((time.perf_counter() - t_ctx) * 1000)})

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if history:
        for turn in history[-10:]:
            role = turn.get("role")
            text = turn.get("text")
            if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                messages.append({"role": role, "content": text})
    if (len(messages) > 1 and messages[-1]["role"] == "user"
            and str(messages[-1]["content"]).strip() == user_message.strip()):
        # The caller's history already ended with this turn (the panel records
        # the message it is asking about, then builds its history): one user
        # turn, not two.
        messages.pop()
    messages.append({"role": "user", "content": user_message})

    tools = fmtools.tool_definitions()
    total_calls = 0
    for _round in range(10):
        t_llm = time.perf_counter()
        try:
            payload = _http_chat(settings, key, messages, tools=tools, timeout=timeout)
        except AssistantError as exc:
            # If the endpoint rejects tool definitions at all, retry once
            # without them: a plain answer beats an error on a provider that
            # does not implement function calling.
            if _round == 0 and _rejected_tool_definitions(exc):
                emit({"type": "step", "stage": "fallback",
                      "label": "Endpoint rejected tools; retrying plain completion",
                      "ms": int((time.perf_counter() - t_llm) * 1000)})
                return chat_completion(
                    settings, key,
                    build_prompt(db, user_message, club_name=active_club),
                    timeout=timeout)
            raise

        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        reasoning = _extract_reasoning(payload)
        if reasoning:
            emit({"type": "thinking", "text": reasoning, "round": _round + 1, "ms": llm_ms})

        choices = payload.get("choices") or []
        first = (choices[0] if choices else {}) or {}
        message = first.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            text = _strip_think_tags(_extract_text(payload))
            emit({"type": "step", "stage": "answer", "label": "Drafted response",
                  "ms": llm_ms})
            return text or "(no answer)"

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        }
        messages.append(assistant_msg)

        for call in tool_calls:
            total_calls += 1
            if total_calls > 24:
                return _strip_think_tags(_extract_text(payload)) or (
                    "Stopped after 24 tool calls in one turn; ask a narrower question.")
            call_id = str(call.get("id") or f"call_{total_calls}")
            fn = call.get("function") or {}
            fn_name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            if on_status:
                on_status(f"Running {fn_name}…")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
            except ValueError:
                parsed_args = {}
            emit({"type": "tool_start", "id": call_id, "name": fn_name, "args": parsed_args})
            t_tool = time.perf_counter()
            result_obj = fmtools.execute_tool(
                db, fn_name, parsed_args, default_club=active_club)
            tool_ms = int((time.perf_counter() - t_tool) * 1000)
            ok = not (isinstance(result_obj, dict) and result_obj.get("error"))
            summary = ""
            if isinstance(result_obj, dict):
                if result_obj.get("error"):
                    summary = str(result_obj["error"])[:120]
                elif "count" in result_obj:
                    summary = f"{result_obj['count']} items"
                elif "squad_size" in result_obj:
                    summary = f"{result_obj['squad_size']} players"
                elif "name" in result_obj:
                    summary = str(result_obj["name"])[:80]
            emit({"type": "tool_done", "id": call_id, "name": fn_name,
                  "args": parsed_args, "ms": tool_ms, "ok": ok, "summary": summary})
            try:
                result_str = json.dumps(result_obj, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                result_str = json.dumps(
                    {"error": "the tool's result could not be serialised"})
            if len(result_str.encode("utf-8")) > 32768:
                result_str = json.dumps(
                    {"truncated": True, "preview": result_str[:16000],
                     "guidance": "Result exceeded 32 KiB; narrow your filter or limit."},
                    ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result_str,
            })

    return "Reached the tool-calling round limit; please ask a more specific follow-up."


def model_catalog(settings: AssistantSettings, key: str,
                  timeout: float = REQUEST_TIMEOUT_SECONDS) -> list[str]:
    """The provider's public model list, bounded the way its own client is."""
    if not key:
        raise AssistantError("No API key is set for this provider.")
    base = _endpoint(settings.base_url)
    url = base[: -len("/chat/completions")] + "/models"
    request = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {key}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AssistantError(_status_message(exc.code), status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AssistantError("The model list could not be read.") from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise AssistantError("The provider's model list is too large.")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise AssistantError("The model list is not readable.") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload if isinstance(payload, list) else []
    names = []
    for row in rows:
        if isinstance(row, dict):
            name = row.get("id") or row.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        elif isinstance(row, str):
            names.append(row)
    return names


def _extract_text(payload: Any) -> str:
    """Read the text out of the shapes OpenAI-compatible providers return."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts).strip()
        if isinstance(first.get("text"), str):
            return first["text"].strip()
    for name in ("output_text", "text", "content"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _cli_completion(settings: AssistantSettings, info: dict[str, Any],
                    prompt: str, timeout: float) -> str:
    """Run a local coding-agent CLI and read its answer from stdout."""
    import shutil
    import subprocess

    executable = info.get("executable") or settings.provider
    path = shutil.which(executable)
    if not path:
        raise AssistantError(
            f"The {info.get('label')} program was not found on this computer.",
            "Install it, or pick another provider in Settings.")
    try:
        cli_args = list(info.get("args") or ["exec", "--skip-git-repo-check"])
        result = subprocess.run(
            [path, *cli_args, prompt],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise AssistantError(
            f"{info.get('label')} did not answer within {int(timeout)} seconds."
        ) from exc
    except OSError as exc:
        raise AssistantError(f"{info.get('label')} could not be started.") from exc
    text = (result.stdout or "").strip()
    if not text:
        detail = (result.stderr or "").strip().splitlines()
        raise AssistantError(
            f"{info.get('label')} returned nothing."
            + (f" {detail[-1][:200]}" if detail else ""))
    return text


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

@dataclass
class Message:
    role: str          # "user" or "assistant"
    text: str
    failed: bool = False


@dataclass
class Conversation:
    """One chat, kept in memory and optionally on disk."""

    messages: list[Message] = field(default_factory=list)
    key: str = "default"

    def add(self, role: str, text: str, failed: bool = False) -> Message:
        message = Message(role, text, failed)
        self.messages.append(message)
        return message

    def clear(self) -> None:
        self.messages.clear()

    def transcript(self, limit: int = 12) -> str:
        lines = []
        for message in self.messages[-limit:]:
            who = "User" if message.role == "user" else "Assistant"
            lines.append(f"{who}: {message.text}")
        return "\n".join(lines)


class ChatWorker:
    """Runs one completion (with FM27 tool calling when db is given) off the UI thread."""

    def __init__(self, settings: AssistantSettings, key: str, prompt: str,
                 on_done: Callable[[str | None, str | None], None],
                 *,
                 db: Any = None,
                 history: list[dict[str, str]] | None = None,
                 club_name: str = "",
                 on_status: Callable[[str], None] | None = None):
        self._settings = settings
        self._key = key
        self._prompt = prompt
        self._on_done = on_done
        self._db = db
        self._history = history
        self._club_name = club_name
        self._on_status = on_status
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            if self._db is not None:
                text = chat_with_tools(
                    self._settings, self._key, self._db, self._prompt,
                    history=self._history, club_name=self._club_name,
                    on_status=self._on_status)
            else:
                text = chat_completion(self._settings, self._key, self._prompt)
            self._on_done(text, None)
        except AssistantError as exc:
            self._on_done(None, str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
            self._on_done(None, f"The assistant failed: {exc}")


# ---------------------------------------------------------------------------
# The plans: one generator each, and the object API kept for old callers
# ---------------------------------------------------------------------------

# The rule tables and the template reader are declared once, in ``fmplans`` and
# ``tacticfiles``; these names are the same objects, kept because the panel, the
# tests and older callers import them from here.
TRAINING_ACTIVITIES = fmplans.TRAINING_ACTIVITIES
ACTIVITY_BY_ID = fmplans.ACTIVITY_BY_ID
DAYS = fmplans.DAYS
SESSIONS_PER_DAY = fmplans.SESSIONS_PER_DAY
TEAM_INSTRUCTIONS = fmplans.TEAM_INSTRUCTIONS
DUTIES = fmplans.DUTIES
ROLE_CATALOGUE = fmplans.ROLE_CATALOGUE
formation_label = fmplans.formation_label
activity_label = fmplans.activity_label
activity_localised = fmplans.activity_localised
tactic_templates = tacticfiles.tactic_templates
game_data_directory = tacticfiles.game_data_directory
load_templates = tacticfiles.load_templates


@dataclass
class TacticSlot:
    number: int
    position: str
    role: str
    duty: str

    def line(self) -> str:
        return f"{self.number:>2}. {self.position:<4} {self.role} ({self.duty})"


@dataclass
class TacticPlan:
    name: str
    template: str
    formation: str
    style: str
    slots: list[TacticSlot]
    instructions: dict[str, str]

    def sheet(self) -> str:
        lines = [
            f"{self.name}",
            f"Shape: {self.formation}   ·   Template: {self.template}",
            f"Style: {self.style}",
            "",
            "Eleven roles:",
        ]
        lines.extend(slot.line() for slot in self.slots)
        lines.append("")
        lines.append("Team instructions:")
        for key, value in self.instructions.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


@dataclass
class TrainingPlan:
    name: str
    focus: str
    days: list[list[int]]          # seven days, three activity ids each
    notes: list[str] = field(default_factory=list)

    def sheet(self) -> str:
        # The same renderer the training tool prints, so the panel's week and
        # the model's week cannot drift apart.
        return fmplans.training_sheet(self.name, self.focus, self.days,
                                      self.notes)


def build_tactic_plan(templates: list[dict[str, Any]], template_name: str,
                      style: str, name: str) -> TacticPlan:
    """A plan for one of the game's templates, as the older object API.

    The real generator is :func:`fmrte.fmplans.build_tactic_from_squad` - the
    panel and the model both call that, because a plan should be derived from
    the eleven who will actually play.  This adapter is kept because it is
    imported by name; it renders a template through the same picker, position
    labels and instruction table.
    """
    try:
        sheet = fmplans.build_template_plan(templates, template_name, style, name)
    except fmplans.NoTemplateError as exc:
        raise AssistantError(
            "No tactic template was found.",
            "The game's Tactics folder is not readable from here.") from exc
    return TacticPlan(
        name=sheet["name"],
        template=sheet["template"],
        formation=sheet["formation"],
        style=sheet["style"],
        slots=[TacticSlot(row["number"], row["position"], row["role"],
                          row["duty"]) for row in sheet["slots"]],
        instructions=dict(sheet["instructions"]),
    )


def build_training_plan(focus: str, name: str = "") -> TrainingPlan:
    """A week that matches the focus, as the older object API."""
    week = fmplans.training_week(focus, name)
    return TrainingPlan(
        name=week["name"],
        focus=week["focus"],
        days=[[session["id"] for session in day["sessions"]]
              for day in week["days"]],
        notes=list(week["notes"]),
    )


def suggestion_prompts() -> list[str]:
    """The four starter questions the original's empty state offers."""
    return [
        "Who are the best young prospects in my squad?",
        "Find a left-back under £23 million I can afford",
        "Which contracts expire this season?",
        "Where is my squad weakest?",
    ]


def looks_like_question(text: str) -> bool:
    return bool(re.search(r"\w", text or ""))
