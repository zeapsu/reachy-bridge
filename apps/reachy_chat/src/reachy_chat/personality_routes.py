"""FastAPI routes for personality and voice management.

Exposes REST endpoints on the provided FastAPI app. Backend actions
(apply personality, fetch voices) are scheduled onto the running
LocalStream asyncio loop via the supplied get_loop callable.
"""

from __future__ import annotations
import asyncio
import inspect
import logging
from typing import Any, Callable, Optional, Awaitable

from fastapi import Query, FastAPI, Request
from pydantic import BaseModel

from .config import (
    LOCKED_PROFILE,
    config,
    get_default_voice_for_backend,
    get_available_voices_for_backend,
)
from .personality import (
    DEFAULT_OPTION,
    _sanitize_name,
    _write_profile,
    read_tools_for,
    list_personalities,
    available_tools_for,
    resolve_profile_dir,
    read_instructions_for,
)
from .conversation_handler import ConversationHandler


logger = logging.getLogger(__name__)


class ApplyPayload(BaseModel):
    """Body of POST /personalities/apply.

    Module-level: under postponed annotations, FastAPI can't resolve a
    function-local model and silently treats it as a query param.
    """

    name: str
    persist: bool = False


def mount_personality_routes(
    app: FastAPI,
    handler: ConversationHandler,
    get_loop: Callable[[], asyncio.AbstractEventLoop | None],
    *,
    persist_personality: Callable[[Optional[str], Optional[str]], None] | None = None,
    get_persisted_personality: Callable[[], Optional[str]] | None = None,
    apply_personality: Callable[[Optional[str]], Awaitable[str]] | None = None,
    get_available_voices: Callable[[], list[str] | Awaitable[list[str]]] | None = None,
    get_current_voice: Callable[[], str | Awaitable[str]] | None = None,
    change_voice: Callable[[str], str | Awaitable[str]] | None = None,
) -> None:
    """Register personality management endpoints on a FastAPI app."""
    from fastapi.responses import JSONResponse


    VOICE_TIMEOUT_SECONDS = 12

    async def _await_if_needed(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _voice_callback(name: str) -> Callable[..., Any] | None:
        callback = get_available_voices if name == "available" else get_current_voice
        if callback is not None:
            return callback
        fallback = getattr(handler, "get_available_voices" if name == "available" else "get_current_voice", None)
        return fallback if callable(fallback) else None

    async def _call_voice_callback(name: str) -> Any:
        callback = _voice_callback(name)
        if callback is None:
            raise RuntimeError("active handler voice API unavailable")
        return await _await_if_needed(callback())

    def _voice_fallback_list() -> list[str]:
        return get_available_voices_for_backend()

    def _voice_fallback_current() -> str:
        return get_default_voice_for_backend()
    def _startup_choice() -> Any:
        """Return the persisted startup personality or default."""
        try:
            if get_persisted_personality is not None:
                stored = get_persisted_personality()
                if stored:
                    return stored
            env_val = getattr(config, "REACHY_MINI_CUSTOM_PROFILE", None)
            if env_val:
                return env_val
        except Exception:
            pass
        return DEFAULT_OPTION

    def _current_choice() -> str:
        try:
            cur = getattr(config, "REACHY_MINI_CUSTOM_PROFILE", None)
            return cur or DEFAULT_OPTION
        except Exception:
            return DEFAULT_OPTION

    @app.get("/personalities")
    def _list() -> dict:  # type: ignore
        choices = [DEFAULT_OPTION, *list_personalities()]
        return {
            "choices": choices,
            "current": _current_choice(),
            "startup": _startup_choice(),
            "locked": LOCKED_PROFILE is not None,
            "locked_to": LOCKED_PROFILE,
        }

    @app.get("/personalities/load")
    def _load(name: str) -> dict:  # type: ignore
        instr = read_instructions_for(name)
        tools_txt = read_tools_for(name)
        voice = get_default_voice_for_backend()
        uses_default_voice = True
        if name != DEFAULT_OPTION:
            pdir = resolve_profile_dir(name)
            vf = pdir / "voice.txt"
            if vf.exists():
                v = vf.read_text(encoding="utf-8").strip()
                voice = v or get_default_voice_for_backend()
                uses_default_voice = not bool(v)
        avail = available_tools_for(name)
        enabled = [ln.strip() for ln in tools_txt.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        return {
            "instructions": instr,
            "tools_text": tools_txt,
            "voice": voice,
            "uses_default_voice": uses_default_voice,
            "available_tools": avail,
            "enabled_tools": enabled,
        }

    @app.post("/personalities/save")
    async def _save(request: Request) -> dict:  # type: ignore
        # Accept raw JSON only to avoid validation-related 422s
        try:
            raw = await request.json()
        except Exception:
            raw = {}
        name = str(raw.get("name", ""))
        instructions = str(raw.get("instructions", ""))
        tools_text = str(raw.get("tools_text", ""))
        voice = (
            str(raw.get("voice", get_default_voice_for_backend()))
            if raw.get("voice") is not None
            else get_default_voice_for_backend()
        )

        sanitized_name = _sanitize_name(name)
        if not sanitized_name:
            return JSONResponse({"ok": False, "error": "invalid_name"}, status_code=400)  # type: ignore
        try:
            logger.info(
                "save: name=%r voice=%r instr_len=%d tools_len=%d",
                sanitized_name,
                voice,
                len(instructions),
                len(tools_text),
            )
            _write_profile(sanitized_name, instructions, tools_text, voice or get_default_voice_for_backend())
            value = f"user_personalities/{sanitized_name}"
            choices = [DEFAULT_OPTION, *list_personalities()]
            return {"ok": True, "value": value, "choices": choices}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)  # type: ignore

    @app.post("/personalities/apply")
    async def _apply(payload: ApplyPayload) -> dict:  # type: ignore
        if LOCKED_PROFILE is not None:
            return JSONResponse(
                {"ok": False, "error": "profile_locked", "locked_to": LOCKED_PROFILE},
                status_code=403,
            )  # type: ignore
        loop = get_loop()
        if loop is None:
            return JSONResponse({"ok": False, "error": "loop_unavailable"}, status_code=503)  # type: ignore

        sel_name = payload.name or DEFAULT_OPTION
        persist_flag = bool(payload.persist)

        async def _do_apply() -> tuple[str, Optional[str]]:
            sel = None if sel_name == DEFAULT_OPTION else sel_name
            if apply_personality is not None:
                status = await apply_personality(sel)
            else:
                status = await handler.apply_personality(sel)
            current_voice_callback = get_current_voice or getattr(handler, "get_current_voice", None)
            voice_override = (
                await _await_if_needed(current_voice_callback()) if callable(current_voice_callback) else None
            )
            return status, voice_override

        try:
            logger.info("apply: requested name=%r", sel_name)
            fut = asyncio.run_coroutine_threadsafe(_do_apply(), loop)
            status, voice_override = fut.result(timeout=10)
            persisted_choice = _startup_choice()
            if persist_flag and persist_personality is not None:
                try:
                    persist_personality(None if sel_name == DEFAULT_OPTION else sel_name, voice_override)
                    persisted_choice = _startup_choice()
                except Exception as e:
                    logger.warning("Failed to persist startup personality: %s", e)
            return {"ok": True, "status": status, "startup": persisted_choice}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)  # type: ignore

    async def _voice_call_on_runtime(name: str) -> Any:
        loop = get_loop()
        if loop is None or loop is asyncio.get_running_loop():
            return await _call_voice_callback(name)
        future = asyncio.run_coroutine_threadsafe(_call_voice_callback(name), loop)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=VOICE_TIMEOUT_SECONDS)

    @app.get("/voices")
    async def _voices() -> list[str]:
        try:
            voices = await _voice_call_on_runtime("available")
            if not isinstance(voices, list) or not all(isinstance(voice, str) for voice in voices):
                raise TypeError("active handler returned an invalid voice list")
            return voices
        except Exception as exc:
            logger.warning("Unable to read active handler voices: %s", exc)
            return _voice_fallback_list()

    @app.get("/voices/current")
    async def _current_voice() -> dict[str, str]:
        try:
            current = await _voice_call_on_runtime("current")
            if not isinstance(current, str) or not current.strip():
                raise TypeError("active handler returned an invalid current voice")
            return {"voice": current.strip()}
        except Exception as exc:
            logger.warning("Unable to read active handler current voice: %s", exc)
            return {"voice": _voice_fallback_current()}

    @app.post("/voices/apply")
    async def _apply_voice(request: Request, voice: str | None = Query(None)) -> dict:  # type: ignore
        requested = str(voice or "").strip()
        if not requested:
            try:
                raw = await request.json()
            except Exception:
                raw = {}
            requested = str(raw.get("voice", "") or "").strip()
        if not requested:
            return JSONResponse({"ok": False, "error": "missing_voice"}, status_code=400)  # type: ignore
        loop = get_loop()
        if loop is None:
            return JSONResponse({"ok": False, "error": "loop_unavailable"}, status_code=503)  # type: ignore

        async def _do() -> Any:
            callback = change_voice or getattr(handler, "change_voice", None)
            if not callable(callback):
                raise RuntimeError("active handler voice API unavailable")
            return await _await_if_needed(callback(requested))

        try:
            future = asyncio.run_coroutine_threadsafe(_do(), loop)
            status = await asyncio.wait_for(asyncio.wrap_future(future), timeout=VOICE_TIMEOUT_SECONDS)
            if (
                status is None
                or status is False
                or (
                    isinstance(status, str)
                    and status.strip().lower().startswith(("voice change failed", "failed to change voice"))
                )
            ):
                raise RuntimeError(str(status or "voice change failed"))
            return {"ok": True, "status": status}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)  # type: ignore
