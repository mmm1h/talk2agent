from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Mapping, Sequence

from acp import (
    PROTOCOL_VERSION,
    audio_block,
    embedded_blob_resource,
    embedded_text_resource,
    image_block,
    resource_block,
    spawn_agent_process,
    text_block,
)

from talk2agent.acp.bot_client import BotClient
from talk2agent.acp.permission import AutoApprovePermissionPolicy


@dataclass(frozen=True, slots=True)
class SessionChoice:
    value: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSelection:
    kind: str
    current_value: str
    choices: tuple[SessionChoice, ...]
    config_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSessionCapabilities:
    can_load: bool
    can_list: bool
    can_resume: bool
    supports_image_prompt: bool
    supports_audio_prompt: bool
    supports_embedded_context_prompt: bool


@dataclass(frozen=True, slots=True)
class SessionCommand:
    name: str
    description: str
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class PromptText:
    text: str


@dataclass(frozen=True, slots=True)
class PromptImage:
    data: str
    mime_type: str
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class PromptAudio:
    data: str
    mime_type: str
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class PromptTextResource:
    uri: str
    text: str
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class PromptBlobResource:
    uri: str
    blob: str
    mime_type: str | None = None


class UnsupportedPromptContentError(RuntimeError):
    def __init__(self, unsupported_content_types: Sequence[str]):
        normalized = tuple(dict.fromkeys(unsupported_content_types))
        self.unsupported_content_types = normalized
        joined = ", ".join(normalized)
        super().__init__(f"unsupported prompt content types: {joined}")


class SessionListingNotSupportedError(RuntimeError):
    pass


class AgentSession:
    def __init__(
        self,
        command: str,
        args: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        mcp_servers=None,
        permission_policy=None,
        spawn_agent_process=spawn_agent_process,
    ):
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.env = dict(os.environ if env is None else env)
        self.mcp_servers = [] if mcp_servers is None else list(mcp_servers)
        self.last_used_at = time.monotonic()
        self.session_id = None

        self._spawn_agent_process = spawn_agent_process
        self._permission_policy = (
            AutoApprovePermissionPolicy() if permission_policy is None else permission_policy
        )
        self._client = BotClient(
            on_update=self._handle_update,
            permission_policy=self._permission_policy,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._context_manager = None
        self._conn = None
        self._process = None
        self._active_sink = None
        self._capabilities = AgentSessionCapabilities(
            can_load=False,
            can_list=False,
            can_resume=False,
            supports_image_prompt=False,
            supports_audio_prompt=False,
            supports_embedded_context_prompt=False,
        )
        self._model_selection: SessionSelection | None = None
        self._mode_selection: SessionSelection | None = None
        self._available_commands: tuple[SessionCommand, ...] = ()
        self._available_commands_event = asyncio.Event()

    @property
    def capabilities(self) -> AgentSessionCapabilities:
        return self._capabilities

    @property
    def available_commands(self) -> tuple[SessionCommand, ...]:
        return self._available_commands

    def get_selection(self, kind: str) -> SessionSelection | None:
        if kind == "model":
            return self._model_selection
        if kind == "mode":
            return self._mode_selection
        raise ValueError(f"unsupported selection kind: {kind}")

    async def wait_for_available_commands(self, timeout_seconds: float) -> tuple[SessionCommand, ...]:
        async with self._startup_lock:
            await self._ensure_started_unlocked()
            if self._available_commands_event.is_set():
                return self._available_commands

        try:
            await asyncio.wait_for(self._available_commands_event.wait(), timeout_seconds)
        except asyncio.TimeoutError:
            return self._available_commands
        return self._available_commands

    async def ensure_started(self):
        async with self._startup_lock:
            await self._ensure_started_unlocked()

    async def list_sessions(self, cursor: str | None = None):
        async with self._lifecycle_lock:
            async with self._startup_lock:
                await self._ensure_connected_unlocked()
                if not self._capabilities.can_list:
                    raise SessionListingNotSupportedError(
                        "session listing not supported by current provider"
                    )
                return await self._conn.list_sessions(cursor=cursor, cwd=self.cwd)

    async def load_session(self, session_id: str, *, prefer_resume: bool = True):
        async with self._lifecycle_lock:
            await self._close_locked()
            async with self._startup_lock:
                await self._ensure_connected_unlocked()
                response = await self._restore_session_unlocked(
                    session_id,
                    prefer_resume=prefer_resume,
                )
                self._apply_session_state(session_id, response)
            self.last_used_at = time.monotonic()

    async def set_selection(self, kind: str, value: str) -> SessionSelection:
        async with self._lifecycle_lock:
            async with self._startup_lock:
                await self._ensure_started_unlocked()
                selection = self.get_selection(kind)
                if selection is None:
                    raise ValueError(f"{kind} selection not supported")
                if selection.config_id is not None:
                    response = await self._conn.set_config_option(
                        config_id=selection.config_id,
                        session_id=self.session_id,
                        value=value,
                    )
                    self._apply_config_options(getattr(response, "config_options", None))
                elif kind == "model":
                    await self._conn.set_session_model(model_id=value, session_id=self.session_id)
                    self._model_selection = SessionSelection(
                        kind="model",
                        current_value=value,
                        choices=selection.choices,
                    )
                else:
                    await self._conn.set_session_mode(mode_id=value, session_id=self.session_id)
                    self._mode_selection = SessionSelection(
                        kind="mode",
                        current_value=value,
                        choices=selection.choices,
                    )

                updated = self.get_selection(kind)
                if updated is None:
                    raise RuntimeError(f"{kind} selection disappeared after update")
                self.last_used_at = time.monotonic()
                return updated

    async def run_turn(self, prompt_text, sink):
        return await self.run_prompt([PromptText(prompt_text)], sink)

    async def run_prompt(
        self,
        prompt_items: Sequence[
            PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource
        ],
        sink,
    ):
        if not prompt_items:
            raise ValueError("prompt_items must not be empty")

        async with self._lifecycle_lock:
            async with self._startup_lock:
                await self._ensure_started_unlocked()
                self._validate_prompt_items(prompt_items)
            self._active_sink = sink
            try:
                return await self._conn.prompt(
                    [self._prompt_item_to_block(item) for item in prompt_items],
                    session_id=self.session_id,
                )
            finally:
                self._active_sink = None
                self.last_used_at = time.monotonic()

    async def _handle_update(self, session_id, update):
        if session_id != self.session_id:
            return

        session_update = getattr(update, "session_update", None)
        if session_update == "current_mode_update" and self._mode_selection is not None:
            self._mode_selection = SessionSelection(
                kind="mode",
                current_value=getattr(update, "current_mode_id", self._mode_selection.current_value),
                choices=self._mode_selection.choices,
                config_id=self._mode_selection.config_id,
            )
        elif session_update == "config_option_update":
            self._apply_config_options(getattr(update, "config_options", None))
        elif session_update == "available_commands_update":
            self._apply_available_commands(
                getattr(
                    update,
                    "available_commands",
                    getattr(update, "availableCommands", None),
                )
            )

        if self._active_sink is None:
            return
        await self._active_sink.on_update(update)

    async def close(self):
        async with self._lifecycle_lock:
            await self._close_locked()

    async def _close_locked(self):
        async with self._startup_lock:
            await self._close_unlocked()

    async def _close_unlocked(self):
        if self._context_manager is None:
            return

        context_manager = self._context_manager
        self._context_manager = None
        self._conn = None
        self._process = None
        self.session_id = None
        self._active_sink = None
        self._model_selection = None
        self._mode_selection = None
        self._available_commands = ()
        self._available_commands_event = asyncio.Event()

        await context_manager.__aexit__(None, None, None)

    async def reset(self):
        async with self._lifecycle_lock:
            await self._close_locked()
            async with self._startup_lock:
                await self._ensure_started_unlocked()

    async def _ensure_connected_unlocked(self):
        if self._conn is not None:
            return

        context_manager = self._spawn_agent_process(
            lambda _agent: self._client,
            self.command,
            *self.args,
            env=self.env,
            cwd=self.cwd,
        )

        try:
            conn, process = await context_manager.__aenter__()
            initialize_response = await conn.initialize(protocol_version=PROTOCOL_VERSION)
        except Exception:
            await context_manager.__aexit__(None, None, None)
            raise

        self._context_manager = context_manager
        self._conn = conn
        self._process = process
        self._apply_capabilities(initialize_response)

    async def _ensure_started_unlocked(self):
        await self._ensure_connected_unlocked()
        if self.session_id is not None:
            return

        response = await self._conn.new_session(cwd=self.cwd, mcp_servers=self.mcp_servers)
        self._apply_session_state(response.session_id, response)

    async def _restore_session_unlocked(self, session_id: str, *, prefer_resume: bool):
        if prefer_resume and self._capabilities.can_resume:
            return await self._conn.resume_session(
                cwd=self.cwd,
                session_id=session_id,
                mcp_servers=self.mcp_servers,
            )
        if self._capabilities.can_load:
            return await self._conn.load_session(
                cwd=self.cwd,
                session_id=session_id,
                mcp_servers=self.mcp_servers,
            )
        if self._capabilities.can_resume:
            return await self._conn.resume_session(
                cwd=self.cwd,
                session_id=session_id,
                mcp_servers=self.mcp_servers,
            )
        raise RuntimeError("session restore not supported by current provider")

    def _apply_capabilities(self, initialize_response: Any) -> None:
        agent_capabilities = getattr(initialize_response, "agent_capabilities", None)
        session_capabilities = getattr(agent_capabilities, "session_capabilities", None)
        prompt_capabilities = getattr(agent_capabilities, "prompt_capabilities", None)
        self._capabilities = AgentSessionCapabilities(
            can_load=bool(getattr(agent_capabilities, "load_session", False)),
            can_list=session_capabilities is not None
            and getattr(session_capabilities, "list", None) is not None,
            can_resume=session_capabilities is not None
            and getattr(session_capabilities, "resume", None) is not None,
            supports_image_prompt=bool(getattr(prompt_capabilities, "image", False)),
            supports_audio_prompt=bool(getattr(prompt_capabilities, "audio", False)),
            supports_embedded_context_prompt=bool(
                getattr(prompt_capabilities, "embedded_context", False)
            ),
        )

    def _apply_session_state(self, session_id: str, response: Any) -> None:
        self.session_id = session_id
        config_options = getattr(response, "config_options", None)
        self._apply_config_options(config_options)
        if config_options is not None:
            return

        self._model_selection = self._selection_from_models(getattr(response, "models", None))
        self._mode_selection = self._selection_from_modes(getattr(response, "modes", None))

    def _apply_available_commands(self, raw_commands: Any) -> None:
        commands: list[SessionCommand] = []
        if raw_commands is not None:
            for raw_command in raw_commands:
                input_spec = getattr(raw_command, "input", None)
                input_spec = getattr(input_spec, "root", input_spec)
                commands.append(
                    SessionCommand(
                        name=getattr(raw_command, "name"),
                        description=getattr(raw_command, "description"),
                        hint=None if input_spec is None else getattr(input_spec, "hint", None),
                    )
                )
        self._available_commands = tuple(commands)
        self._available_commands_event.set()

    def _apply_config_options(self, config_options: Any) -> None:
        selections: dict[str, SessionSelection] = {}
        if config_options is not None:
            for raw_option in config_options:
                option = getattr(raw_option, "root", raw_option)
                category = getattr(option, "category", None)
                if category not in {"model", "mode"}:
                    continue
                current_value = getattr(option, "current_value", None)
                if current_value is None:
                    continue
                selections[category] = SessionSelection(
                    kind=category,
                    current_value=current_value,
                    choices=self._flatten_option_choices(getattr(option, "options", [])),
                    config_id=getattr(option, "id", None),
                )

        self._model_selection = selections.get("model", self._model_selection)
        self._mode_selection = selections.get("mode", self._mode_selection)

        if "model" not in selections and config_options is not None:
            self._model_selection = None
        if "mode" not in selections and config_options is not None:
            self._mode_selection = None

    def _flatten_option_choices(self, raw_values: Any) -> tuple[SessionChoice, ...]:
        if raw_values is None:
            return ()

        choices: list[SessionChoice] = []
        for raw_value in raw_values:
            if hasattr(raw_value, "value"):
                choices.append(
                    SessionChoice(
                        value=raw_value.value,
                        label=raw_value.name,
                        description=getattr(raw_value, "description", None),
                    )
                )
                continue
            for grouped_value in getattr(raw_value, "options", []):
                group_name = getattr(raw_value, "name", None)
                label = grouped_value.name
                if group_name:
                    label = f"{group_name} / {label}"
                choices.append(
                    SessionChoice(
                        value=grouped_value.value,
                        label=label,
                        description=getattr(grouped_value, "description", None),
                    )
                )
        return tuple(choices)

    def _selection_from_models(self, models: Any) -> SessionSelection | None:
        if models is None:
            return None
        choices = tuple(
            SessionChoice(
                value=model.model_id,
                label=model.name,
                description=getattr(model, "description", None),
            )
            for model in getattr(models, "available_models", [])
        )
        current_value = getattr(models, "current_model_id", None)
        if not current_value or not choices:
            return None
        return SessionSelection(kind="model", current_value=current_value, choices=choices)

    def _selection_from_modes(self, modes: Any) -> SessionSelection | None:
        if modes is None:
            return None
        choices = tuple(
            SessionChoice(
                value=mode.id,
                label=mode.name,
                description=getattr(mode, "description", None),
            )
            for mode in getattr(modes, "available_modes", [])
        )
        current_value = getattr(modes, "current_mode_id", None)
        if not current_value or not choices:
            return None
        return SessionSelection(kind="mode", current_value=current_value, choices=choices)

    def _prompt_item_to_block(
        self,
        item: PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource,
    ):
        if isinstance(item, PromptText):
            return text_block(item.text)
        if isinstance(item, PromptImage):
            return image_block(item.data, item.mime_type, uri=item.uri)
        if isinstance(item, PromptAudio):
            return audio_block(item.data, item.mime_type)
        if isinstance(item, PromptTextResource):
            return resource_block(
                embedded_text_resource(
                    item.uri,
                    item.text,
                    mime_type=item.mime_type,
                )
            )
        if isinstance(item, PromptBlobResource):
            return resource_block(
                embedded_blob_resource(
                    item.uri,
                    item.blob,
                    mime_type=item.mime_type,
                )
            )
        raise TypeError(f"unsupported prompt item: {type(item)!r}")

    def _validate_prompt_items(
        self,
        prompt_items: Sequence[
            PromptText | PromptImage | PromptAudio | PromptTextResource | PromptBlobResource
        ],
    ) -> None:
        unsupported_types: list[str] = []
        for item in prompt_items:
            if isinstance(item, PromptImage) and not self._capabilities.supports_image_prompt:
                unsupported_types.append("image")
            elif isinstance(item, PromptAudio) and not self._capabilities.supports_audio_prompt:
                unsupported_types.append("audio")
            elif isinstance(item, (PromptTextResource, PromptBlobResource)) and not self._capabilities.supports_embedded_context_prompt:
                unsupported_types.append("embedded_context")

        if unsupported_types:
            raise UnsupportedPromptContentError(unsupported_types)
