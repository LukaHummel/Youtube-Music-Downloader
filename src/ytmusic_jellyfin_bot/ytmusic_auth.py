from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

import requests

from .config import AppConfig

LOGGER = logging.getLogger(__name__)
CompletionCallback = Callable[[str], Awaitable[None]]


class YtMusicAuthStatus(StrEnum):
    DISABLED = "disabled"
    MISSING_CLIENT_CREDENTIALS = "missing client credentials"
    NOT_AUTHENTICATED = "not authenticated"
    AUTH_IN_PROGRESS = "auth in progress"
    AUTHENTICATED = "authenticated"
    AUTHENTICATED_REFRESH_FAILED = "authenticated but refresh failed"


class YtMusicAuthStartStatus(StrEnum):
    DISABLED = "disabled"
    MISSING_CLIENT_CREDENTIALS = "missing client credentials"
    STARTED = "started"
    ALREADY_IN_PROGRESS = "already in progress"


@dataclass(slots=True)
class YtMusicAuthFlow:
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float
    interval_seconds: int

    @property
    def verification_url_with_code(self) -> str:
        separator = "&" if "?" in self.verification_url else "?"
        return f"{self.verification_url}{separator}user_code={self.user_code}"

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))


@dataclass(slots=True)
class YtMusicAuthStartResult:
    status: YtMusicAuthStartStatus
    flow: YtMusicAuthFlow | None = None


class YtMusicAuthManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        oauth_credentials_factory: Callable[..., Any] | None = None,
    ):
        self.config = config
        self._oauth_credentials_factory = oauth_credentials_factory
        self._lock = asyncio.Lock()
        self._active_flow: YtMusicAuthFlow | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._refresh_failed = False

    @property
    def oauth_file(self) -> Path:
        return self.config.ytmusic_oauth_file

    @property
    def client_credentials_available(self) -> bool:
        return bool(self.config.ytmusic_oauth_client_id and self.config.ytmusic_oauth_client_secret)

    def mark_refresh_failed(self) -> None:
        self._refresh_failed = True

    def clear_refresh_failed(self) -> None:
        self._refresh_failed = False

    def clear_active_client_state(self) -> None:
        self.clear_refresh_failed()

    def status(self) -> YtMusicAuthStatus:
        if not self.config.ytmusic_metadata_enabled:
            return YtMusicAuthStatus.DISABLED
        if not self.client_credentials_available:
            return YtMusicAuthStatus.MISSING_CLIENT_CREDENTIALS
        if self._active_flow and self._active_flow.remaining_seconds > 0:
            return YtMusicAuthStatus.AUTH_IN_PROGRESS
        if self.oauth_file.is_file():
            if self._refresh_failed:
                return YtMusicAuthStatus.AUTHENTICATED_REFRESH_FAILED
            return YtMusicAuthStatus.AUTHENTICATED
        return YtMusicAuthStatus.NOT_AUTHENTICATED

    async def start_auth_flow(self, on_complete: CompletionCallback) -> YtMusicAuthStartResult:
        async with self._lock:
            if not self.config.ytmusic_metadata_enabled:
                return YtMusicAuthStartResult(YtMusicAuthStartStatus.DISABLED)
            if not self.client_credentials_available:
                return YtMusicAuthStartResult(YtMusicAuthStartStatus.MISSING_CLIENT_CREDENTIALS)
            if self._active_flow and self._active_flow.remaining_seconds > 0:
                return YtMusicAuthStartResult(YtMusicAuthStartStatus.ALREADY_IN_PROGRESS, self._active_flow)

            credentials = self._create_oauth_credentials()
            try:
                code = await asyncio.to_thread(credentials.get_code)
                flow = YtMusicAuthFlow(
                    device_code=str(code["device_code"]),
                    user_code=str(code["user_code"]),
                    verification_url=str(code.get("verification_url") or "https://www.google.com/device"),
                    expires_at=time.time() + int(code.get("expires_in") or 1800),
                    interval_seconds=max(1, int(code.get("interval") or 5)),
                )
            except Exception as exc:
                LOGGER.warning("Could not start ytmusic OAuth flow: %s", exc)
                return YtMusicAuthStartResult(YtMusicAuthStartStatus.MISSING_CLIENT_CREDENTIALS)

            self._active_flow = flow
            self._active_task = asyncio.create_task(
                self._poll_auth_flow(credentials, flow, on_complete),
                name="ytmusic-oauth-flow",
            )
            LOGGER.info(
                "Started ytmusic OAuth device flow: user_code=%s expires_in=%ss",
                flow.user_code,
                flow.remaining_seconds,
            )
            return YtMusicAuthStartResult(YtMusicAuthStartStatus.STARTED, flow)

    async def reset(self) -> None:
        async with self._lock:
            if self._active_task and not self._active_task.done():
                self._active_task.cancel()
            self._active_task = None
            self._active_flow = None
            self._refresh_failed = False
            try:
                self.oauth_file.unlink()
            except FileNotFoundError:
                pass

    def _create_oauth_credentials(self) -> Any:
        client_id = self.config.ytmusic_oauth_client_id
        client_secret = self.config.ytmusic_oauth_client_secret
        if not client_id or not client_secret:
            raise ValueError("ytmusic OAuth client credentials are not configured")
        factory = self._oauth_credentials_factory
        if factory is None:
            from ytmusicapi import OAuthCredentials

            factory = OAuthCredentials
        return factory(client_id=client_id, client_secret=client_secret, session=_timeout_session(self.config))

    async def _poll_auth_flow(
        self,
        credentials: Any,
        flow: YtMusicAuthFlow,
        on_complete: CompletionCallback,
    ) -> None:
        interval = flow.interval_seconds
        try:
            while flow.remaining_seconds > 0:
                await asyncio.sleep(interval)
                try:
                    token = await asyncio.to_thread(credentials.token_from_code, flow.device_code)
                except Exception as exc:
                    await self._finish_flow(flow, on_complete, f"YouTube Music OAuth failed: {exc}")
                    return

                if _has_refreshable_token(token):
                    self._write_token(token)
                    self.clear_refresh_failed()
                    await self._finish_flow(
                        flow,
                        on_complete,
                        "YouTube Music OAuth completed. Metadata enrichment will use the saved token.",
                    )
                    return

                error = str(token.get("error") or "") if isinstance(token, dict) else ""
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error:
                    await self._finish_flow(flow, on_complete, f"YouTube Music OAuth failed: {error}.")
                    return

                await self._finish_flow(flow, on_complete, "YouTube Music OAuth failed: unexpected token response.")
                return

            await self._finish_flow(flow, on_complete, "YouTube Music OAuth expired before authorization completed.")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Unhandled ytmusic OAuth flow failure")
            await self._finish_flow(flow, on_complete, "YouTube Music OAuth failed with an internal error.")

    async def _finish_flow(self, flow: YtMusicAuthFlow, on_complete: CompletionCallback, message: str) -> None:
        async with self._lock:
            if self._active_flow is flow:
                self._active_flow = None
                self._active_task = None
        try:
            await on_complete(message)
        except Exception:
            LOGGER.exception("Could not send ytmusic OAuth completion notification")

    def _write_token(self, token: dict[str, Any]) -> None:
        expires_in = int(token.get("expires_in") or 0)
        refresh_expires_in = int(token.get("refresh_token_expires_in") or expires_in)
        token_payload = {
            "scope": token["scope"],
            "token_type": token["token_type"],
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": int(time.time()) + expires_in,
            "expires_in": refresh_expires_in,
        }
        self.oauth_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.oauth_file.with_name(f"{self.oauth_file.name}.tmp")
        temp_path.write_text(json.dumps(token_payload, ensure_ascii=True), encoding="utf-8")
        if os.name == "posix":
            os.chmod(temp_path, 0o600)
        temp_path.replace(self.oauth_file)
        LOGGER.info("Saved ytmusic OAuth token to %s", self.oauth_file)


def _timeout_session(config: AppConfig) -> requests.Session:
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=config.ytmusic_request_timeout)  # type: ignore[method-assign]
    return session


def _has_refreshable_token(value: Any) -> bool:
    return isinstance(value, dict) and all(
        key in value for key in ("access_token", "refresh_token", "scope", "token_type")
    )
