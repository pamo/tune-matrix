#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
from io import BytesIO
import json
import os
import secrets
import shutil
import sys
import threading
import time
import tempfile
import urllib.parse
import urllib.request
from email.message import Message
from urllib.error import HTTPError, URLError
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import Any, NoReturn, Protocol

from PIL import Image, ImageDraw, ImageOps

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SCOPE = "user-read-currently-playing"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_PLACEHOLDER_ART_ID = "2a96cbd8b46e442fc41c2b86b821562f"
# 4 = auth failed, 6 = user not found, 10 = invalid key, 26 = suspended key.
LASTFM_AUTH_ERROR_CODES = frozenset({4, 6, 10, 26})
# 8 = operation failed, 11 = service offline, 16 = temporarily unavailable.
LASTFM_TRANSIENT_ERROR_CODES = frozenset({8, 11, 16})
LASTFM_RATE_LIMIT_ERROR_CODE = 29


# --------------------------------------------------------------------------------------
# Errors
#
# The split matters at startup: a wall display should exit loudly on a misconfiguration it
# can never recover from, and should fall through to the idle frame on anything the poll
# thread could plausibly retry its way out of (no Wi-Fi yet, 5xx, rate limit).
# --------------------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderAuthError(ProviderError):
    """Credentials are missing, wrong, or revoked. Retrying will not help."""


class ProviderUnavailableError(ProviderError):
    """Transient failure: network unreachable, server error, rate limit."""


class ProviderRateLimitError(ProviderUnavailableError):
    def __init__(self, provider_name: str, retry_after_seconds: int) -> None:
        self.provider_name = provider_name
        self.retry_after_seconds = max(retry_after_seconds, 1)
        super().__init__(
            f"{provider_name} API rate limited request; retry after {self.retry_after_seconds} seconds."
        )


@dataclass
class PlaybackArt:
    key: str
    image_url: str
    is_playing: bool
    # Human-readable "Artist - Track", for the terminal preview's status line. The key is
    # whatever the provider considers stable (often an opaque id), which is useless to read.
    title: str | None = None


@dataclass
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> HttpResponse:
    if params:
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers=headers or {},
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.headers, response.read())
    except HTTPError as exc:
        # An error status is still a response; callers classify it themselves.
        return HttpResponse(exc.code, exc.headers, exc.read())
    except (URLError, OSError) as exc:
        # No response at all: DNS failure, connection refused, timeout, Wi-Fi not up yet.
        raise ProviderUnavailableError(f"{method} {url} failed: {exc}") from exc


HttpFn = Callable[..., HttpResponse]


def raise_http_error(response: HttpResponse, context: str) -> NoReturn:
    body = response.body.decode("utf-8", errors="replace")
    message = f"{context} failed with HTTP {response.status}: {body}"
    if response.status in (401, 403):
        raise ProviderAuthError(message)
    if response.status >= 500:
        raise ProviderUnavailableError(message)
    raise ProviderError(message)


def retry_after_seconds(response: HttpResponse, default: int = 5) -> int:
    """Parse a Retry-After header, falling back to `default` on anything unparseable."""
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        return max(int(raw), 1)
    except ValueError:
        return default


class PlaybackProvider(Protocol):
    name: str

    def authorize(self) -> None:
        ...

    def get_playback_art(self) -> PlaybackArt | None:
        ...


# --------------------------------------------------------------------------------------
# Token persistence
# --------------------------------------------------------------------------------------


def atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    file_mode: int = 0o644,
    private_parent: bool = False,
) -> None:
    """Write JSON so a crash or power cut can never leave a half-written file behind.

    A wall display loses power at the wall, so every file it writes gets replaced
    atomically rather than truncated and rewritten in place.
    """
    parent = path.parent
    # Path("token.json").parent is Path("."), and chmod 0o700 on the working directory
    # under sudo would lock the deploy checkout to root only.
    named_parent = parent not in (Path("."), Path(""))
    if named_parent:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private_parent else 0o755)
    if named_parent and private_parent:
        os.chmod(parent, 0o700)

    temp_path: Path | None = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, path)
        replaced = True
        os.chmod(path, file_mode)
    finally:
        if not replaced and temp_path and temp_path.exists():
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()


class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        ...

    def save(self, token: dict[str, Any]) -> None:
        ...


class FileTokenStore:
    """Stores the OAuth token as private JSON, replaced atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None

        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            # Tightening permissions is best-effort. Some filesystems (and a cache written
            # under sudo but read as another user) reject chmod, and refusing to start over
            # that would strand the display on a working token.
            print(f"Warning: could not tighten permissions on {self.path}: {exc}", flush=True)

        try:
            with self.path.open("r", encoding="utf-8") as token_file:
                return json.load(token_file)
        except json.JSONDecodeError:
            corrupt_path = self.path.with_name(f"{self.path.name}.corrupt")
            self.path.replace(corrupt_path)
            print(
                f"Token cache was corrupt and moved to {corrupt_path}; re-authorizing.",
                flush=True,
            )
            return None

    def save(self, token: dict[str, Any]) -> None:
        atomic_write_json(self.path, token, file_mode=0o600, private_parent=True)


class InMemoryTokenStore:
    """Non-persistent store, for tests and dry runs."""

    def __init__(self, token: dict[str, Any] | None = None) -> None:
        self.token = token

    def load(self) -> dict[str, Any] | None:
        return self.token

    def save(self, token: dict[str, Any]) -> None:
        self.token = token


# --------------------------------------------------------------------------------------
# Runtime settings
#
# config.json is the single source of truth for anything adjustable while the display is
# running, and it is re-read whenever its mtime changes. That makes every way of changing
# settings equivalent: the web UI, an SSH edit, or anything else that writes the file.
# --------------------------------------------------------------------------------------


SCENE_NAMES = ("album", "blank", "clock", "photos", "photos+clock")


@dataclass(frozen=True)
class Config:
    style: str = "record"
    idle_scene: str = "blank"
    override_scene: str | None = None
    rpm: float = 20.0
    brightness: int | None = None
    clock_24_hour: bool = False
    photo_seconds: float = 30.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "idle_scene": self.idle_scene,
            "override_scene": self.override_scene,
            "rpm": self.rpm,
            "brightness": self.brightness,
            "clock_24_hour": self.clock_24_hour,
            "photo_seconds": self.photo_seconds,
        }


def coerce_config(values: dict[str, Any], defaults: Config | None = None) -> Config:
    """Build a Config from untrusted input, clamping rather than rejecting.

    The web UI and a hand-edited file are both untrusted. A bad value falls back to the
    default instead of crashing the display, because a wall clock that refuses to start
    over a typo is worse than one that ignores it.
    """
    base = defaults or Config()

    def choice(key: str, options: tuple[str, ...], fallback: str) -> str:
        value = values.get(key, fallback)
        return value if value in options else fallback

    def number(key: str, fallback: float, low: float, high: float) -> float:
        try:
            return min(max(float(values.get(key, fallback)), low), high)
        except (TypeError, ValueError):
            return fallback

    override = values.get("override_scene", base.override_scene)
    if override in ("", "none", "auto"):
        override = None
    if override is not None and override not in SCENE_NAMES:
        override = base.override_scene

    brightness: int | None
    raw_brightness = values.get("brightness", base.brightness)
    if raw_brightness is None:
        brightness = None
    else:
        try:
            brightness = min(max(int(raw_brightness), 1), 100)
        except (TypeError, ValueError):
            brightness = base.brightness

    return Config(
        style=choice("style", tuple(RENDERER_STYLES), base.style),
        idle_scene=choice("idle_scene", SCENE_NAMES, base.idle_scene),
        override_scene=override,
        rpm=number("rpm", base.rpm, 0.1, 300.0),
        brightness=brightness,
        clock_24_hour=bool(values.get("clock_24_hour", base.clock_24_hour)),
        photo_seconds=number("photo_seconds", base.photo_seconds, 1.0, 3600.0),
    )


class ConfigStore:
    """Reads config.json, reloading when it changes on disk."""

    def __init__(self, path: Path, defaults: Config | None = None) -> None:
        self.path = path
        self.defaults = defaults or Config()
        self._config = self.defaults
        self._signature: tuple[float, int] | None = None
        self._lock = threading.Lock()
        self._loaded_once = False

    def _signature_now(self) -> tuple[float, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime, stat.st_size

    def current(self) -> Config:
        """Cheap enough to call every frame: one stat unless the file actually changed."""
        signature = self._signature_now()
        with self._lock:
            if signature != self._signature or not self._loaded_once:
                self._signature = signature
                self._loaded_once = True
                self._config = self._read(signature)
            return self._config

    def _read(self, signature: tuple[float, int] | None) -> Config:
        if signature is None:
            return self.defaults
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read {self.path} ({exc}); using current settings.", flush=True)
            return self._config
        if not isinstance(values, dict):
            print(f"Warning: {self.path} is not a JSON object; using current settings.", flush=True)
            return self._config
        return coerce_config(values, self.defaults)

    def save(self, config: Config) -> None:
        atomic_write_json(self.path, config.as_dict())
        with self._lock:
            self._config = config
            self._signature = self._signature_now()
            self._loaded_once = True

    def update(self, changes: dict[str, Any]) -> Config:
        """Merge a partial update over the current settings and persist it."""
        merged = {**self.current().as_dict(), **changes}
        config = coerce_config(merged, self.defaults)
        self.save(config)
        return config


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_store: TokenStore,
        open_browser: bool,
        callback_timeout_seconds: float,
        http: HttpFn = http_request,
    ) -> None:
        self.name = "spotify"
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.open_browser = open_browser
        self.callback_timeout_seconds = callback_timeout_seconds
        self._store = token_store
        self._http = http
        self.token = token_store.load()

    def get_currently_playing(self) -> dict[str, Any] | None:
        refreshed_token = False
        while True:
            token = self._valid_access_token()
            response = self._http(
                "GET",
                CURRENTLY_PLAYING_URL,
                params={"additional_types": "track,episode"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )

            if response.status == 204:
                return None
            if response.status == 401:
                if refreshed_token:
                    raise_http_error(response, "Spotify currently-playing request")
                self._refresh_access_token()
                refreshed_token = True
                continue
            if response.status == 429:
                raise ProviderRateLimitError(self.name, retry_after_seconds(response))
            if response.status != 200:
                raise_http_error(response, "Spotify currently-playing request")

            return response.json()

    def get_playback_art(self) -> PlaybackArt | None:
        return playback_art_from_response(self.get_currently_playing())

    def authorize(self) -> None:
        self._valid_access_token()

    def _valid_access_token(self) -> str:
        if not self.token:
            self.token = self._authorize()

        if time.time() >= float(self.token.get("expires_at", 0)):
            self._refresh_access_token()

        return str(self.token["access_token"])

    def _save_token(self, token: dict[str, Any]) -> None:
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60

        previous_refresh_token = self.token.get("refresh_token") if self.token else None
        if previous_refresh_token and "refresh_token" not in token:
            token["refresh_token"] = previous_refresh_token

        self._store.save(token)
        self.token = token

    def _authorize(self) -> dict[str, Any]:
        state = secrets.token_urlsafe(18)
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
            raise ProviderAuthError("This script expects a localhost Spotify redirect URI.")

        with LocalCallbackServer(
            host=parsed_redirect.hostname or "127.0.0.1",
            port=parsed_redirect.port or 80,
            path=parsed_redirect.path or "/callback",
            expected_state=state,
        ) as callback:
            query = urllib.parse.urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "redirect_uri": self.redirect_uri,
                    "scope": SCOPE,
                    "state": state,
                }
            )
            auth_url = f"{AUTH_URL}?{query}"

            print("Authorize Spotify in your browser:")
            print(auth_url)
            if self.open_browser:
                webbrowser.open(auth_url)

            code = callback.wait_for_code(timeout_seconds=self.callback_timeout_seconds)

        token = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        self._save_token(token)
        return token

    def _refresh_access_token(self) -> None:
        refresh_token = self.token.get("refresh_token") if self.token else None
        if not refresh_token:
            self.token = self._authorize()
            return

        token = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._save_token(token)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        basic_auth = base64.b64encode(credentials).decode("ascii")
        response = self._http(
            "POST",
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        if response.status != 200:
            body = response.body.decode("utf-8", errors="replace")
            # Spotify reports a revoked refresh token or bad client as a 400, which would
            # otherwise be classified as a generic (retryable-looking) error.
            if "invalid_grant" in body or "invalid_client" in body:
                raise ProviderAuthError(
                    f"Spotify rejected the stored credentials ({body}). "
                    "Delete the token cache and re-run with --auth-only."
                )
            raise_http_error(response, "Spotify token request")
        return response.json()


class YouTubeMusicClient:
    """Best-effort provider built on playback *history*.

    YouTube Music exposes no live now-playing endpoint, so "is it playing?" is inferred
    from freshness: when the top history entry changes, the track is treated as playing
    and keeps spinning until `stale_after_seconds` passes with no further change. A track
    already at the top of history when the process starts counts as a change, so expect
    one stale spin window after a restart.
    """

    def __init__(
        self,
        auth_headers_path: Path,
        stale_after_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = "youtube-music"
        self.auth_headers_path = auth_headers_path
        self.stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._client: Any | None = None
        self._last_key: str | None = None
        self._last_change_at: float | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        if not self.auth_headers_path.exists():
            raise ProviderAuthError(
                f"YouTube Music auth headers file not found at {self.auth_headers_path}. "
                "Run ytmusicapi auth setup and set YTMUSIC_AUTH_HEADERS_PATH."
            )

        try:
            from ytmusicapi import YTMusic
        except ImportError as exc:
            raise ProviderAuthError(
                "The ytmusicapi package is required for provider=youtube-music. "
                'Install it with pip install "ytmusicapi>=1.8".'
            ) from exc

        self._client = YTMusic(str(self.auth_headers_path))
        return self._client

    def authorize(self) -> None:
        self._get_client()

    def get_playback_art(self) -> PlaybackArt | None:
        history = self._get_client().get_history()
        if not history:
            return None

        entry = history[0]
        thumbnails = entry.get("thumbnails") or []
        if not thumbnails:
            return None

        image = max(
            thumbnails,
            key=lambda candidate: (candidate.get("width") or 0) * (candidate.get("height") or 0),
        )
        image_url = image.get("url")
        if not image_url:
            return None

        track_key = str(entry.get("videoId") or entry.get("title") or image_url)

        name = entry.get("title") or ""
        artists = ", ".join(
            artist.get("name", "") for artist in (entry.get("artists") or []) if artist.get("name")
        )
        title = f"{artists} - {name}".strip(" -") if artists else name

        now = self._clock()
        if track_key != self._last_key or self._last_change_at is None:
            self._last_key = track_key
            self._last_change_at = now

        return PlaybackArt(
            key=track_key,
            image_url=str(image_url),
            is_playing=(now - self._last_change_at) <= self.stale_after_seconds,
            title=title or None,
        )


class DemoProvider:
    """Synthetic playback, for previewing the display without credentials or hardware.

    Cycles playing -> paused -> nothing playing on a fixed period so every state the panel
    can be in is reachable in one run. The art it reports is generated locally, so nothing
    is downloaded (see `build_image_preparer`).
    """

    DEMO_IMAGE_URL = "demo://album-art"

    def __init__(self, cycle_seconds: float = 6.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.name = "demo"
        self.cycle_seconds = cycle_seconds
        self._clock = clock
        self._start = clock()

    def authorize(self) -> None:
        return None

    def get_playback_art(self) -> PlaybackArt | None:
        elapsed = self._clock() - self._start
        phase = int(elapsed / self.cycle_seconds) % 3
        if phase == 2:
            return None
        return PlaybackArt(
            key="demo-track",
            image_url=self.DEMO_IMAGE_URL,
            is_playing=phase == 0,
            title="Demo Artist - Demo Track",
        )


def select_lastfm_image_url(images: list[dict[str, Any]]) -> str | None:
    size_rank = {"small": 0, "medium": 1, "large": 2, "extralarge": 3, "mega": 4}
    best_url: str | None = None
    best_rank = -1
    for image in images:
        url = (image.get("#text") or "").strip()
        if not url or LASTFM_PLACEHOLDER_ART_ID in url:
            # Last.fm serves a generic grey star for tracks it has no cover for. Treat it
            # as "no art" so the display goes idle instead of spinning the placeholder.
            continue
        rank = size_rank.get(image.get("size", ""), -1)
        if rank >= best_rank:
            best_rank = rank
            best_url = url
    return best_url


class LastFmClient:
    """Best-effort universal provider.

    Last.fm reports the track a user is currently scrobbling, regardless of which
    service plays it (Spotify, Apple Music, Tidal, Deezer, YouTube Music, ...). It is
    free, headless, and pollable from a remote device, which makes it the catch-all for
    services that expose no live now-playing endpoint of their own.
    """

    def __init__(self, api_key: str, user: str, http: HttpFn = http_request) -> None:
        self.name = "lastfm"
        self.api_key = api_key
        self.user = user
        self._http = http
        self.account_summary: str | None = None

    def authorize(self) -> None:
        # Verify the API key and username by issuing a recent-tracks request.
        self._fetch_recent_tracks()

    def _fetch_recent_tracks(self) -> dict[str, Any]:
        """Return the `recenttracks` object, recording which account answered.

        Last.fm only errors on a username that does not exist, so any real account
        verifies. Remembering who answered is what lets --auth-only show you whether it
        was *your* account -- a typo'd but valid username otherwise looks like success.
        """
        recent = self._request_recent_tracks()
        attributes = recent.get("@attr") or {}
        resolved_user = attributes.get("user")
        total = attributes.get("total")
        if resolved_user:
            scrobbles = f"{int(total):,} scrobbles" if str(total).isdigit() else "unknown scrobbles"
            self.account_summary = f"user {resolved_user}, {scrobbles}"
        return recent

    def _request_recent_tracks(self) -> dict[str, Any]:
        response = self._http(
            "GET",
            LASTFM_API_URL,
            params={
                "method": "user.getRecentTracks",
                "user": self.user,
                "api_key": self.api_key,
                "format": "json",
                "limit": "1",
            },
            timeout=10,
        )

        if response.status == 429:
            raise ProviderRateLimitError(self.name, retry_after_seconds(response))
        if response.status != 200:
            raise_http_error(response, "Last.fm recent-tracks request")

        payload = response.json()
        self._raise_for_payload_error(payload)
        return payload.get("recenttracks") or {}

    def get_playback_art(self) -> PlaybackArt | None:
        recent = self._fetch_recent_tracks()

        tracks = recent.get("track") or []
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks:
            return None

        track = tracks[0]
        now_playing = (track.get("@attr") or {}).get("nowplaying") == "true"
        if not now_playing:
            # The newest entry is a historical scrobble, not live; show idle rather
            # than spinning a stale track forever.
            return None

        image_url = select_lastfm_image_url(track.get("image") or [])
        if not image_url:
            return None

        artist = (track.get("artist") or {}).get("#text", "")
        title = f"{artist} - {track.get('name', '')}".strip(" -")
        track_key = track.get("mbid") or title
        return PlaybackArt(
            key=str(track_key) or image_url,
            image_url=image_url,
            is_playing=True,
            title=title or None,
        )

    def _raise_for_payload_error(self, payload: dict[str, Any]) -> None:
        """Last.fm reports failures in the body with HTTP 200."""
        error_code = payload.get("error")
        if error_code is None:
            return

        message = payload.get("message", "unknown error")
        if error_code in LASTFM_AUTH_ERROR_CODES:
            raise ProviderAuthError(
                f"Last.fm authorization failed ({message}). Check LASTFM_API_KEY and LASTFM_USER."
            )
        if error_code == LASTFM_RATE_LIMIT_ERROR_CODE:
            raise ProviderRateLimitError(self.name, 5)
        if error_code in LASTFM_TRANSIENT_ERROR_CODES:
            raise ProviderUnavailableError(f"Last.fm recent-tracks request failed: {message}")
        raise ProviderError(f"Last.fm recent-tracks request failed: {message}")


class LocalCallbackServer:
    def __init__(self, host: str, port: int, path: str, expected_state: str) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.state_error: str | None = None
        self.path = path
        self.expected_state = expected_state

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if parsed.path != parent.path:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Wrong callback path.")
                    return

                returned_state = params.get("state", [""])[0]
                if returned_state != parent.expected_state:
                    parent.state_error = "Spotify callback state did not match."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    return

                if "error" in params:
                    parent.error = params["error"][0]
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Spotify authorization failed.")
                    return

                parent.code = params.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Spotify authorization complete. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = HTTPServer((host, port), Handler)

    def __enter__(self) -> LocalCallbackServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # The constructor already bound the port, so anything that raises between
        # construction and wait_for_code would otherwise leave it bound for the life of
        # the process and make the next auth attempt fail with EADDRINUSE.
        # server_close is safe to call twice; shutdown() is not, so it stays in
        # wait_for_code where serve_forever is known to have started.
        self.server.server_close()

    def wait_for_code(self, timeout_seconds: float) -> str:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout_seconds
        try:
            while not self.code and not self.error and not self.state_error:
                if time.monotonic() >= deadline:
                    raise ProviderAuthError(
                        f"Timed out waiting for Spotify authorization callback after {timeout_seconds:.0f} seconds."
                    )
                time.sleep(0.1)
        finally:
            self.server.shutdown()
            self.server.server_close()

        if self.state_error:
            raise ProviderAuthError(self.state_error)
        if self.error:
            raise ProviderAuthError(f"Spotify authorization failed: {self.error}")
        if not self.code:
            raise ProviderAuthError("Spotify authorization did not return a code.")
        return self.code


# --------------------------------------------------------------------------------------
# Displays
# --------------------------------------------------------------------------------------


class MatrixDisplay:
    def __init__(
        self,
        *,
        rows: int,
        cols: int,
        chain_length: int,
        parallel: int,
        brightness: int,
        gpio_slowdown: int,
        hardware_mapping: str,
        pwm_bits: int,
        limit_refresh_rate_hz: int,
        disable_hardware_pulsing: bool,
    ) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as exc:
            raise RuntimeError(
                "The rgbmatrix Python bindings are not installed. Install "
                "hzeller/rpi-rgb-led-matrix on the Pi, or preview without hardware using "
                "--preview-terminal (live), --record-gif or --mock-output."
            ) from exc

        options = RGBMatrixOptions()
        options.rows = rows
        options.cols = cols
        options.chain_length = chain_length
        options.parallel = parallel
        options.brightness = brightness
        options.gpio_slowdown = gpio_slowdown
        options.hardware_mapping = hardware_mapping
        options.pwm_bits = pwm_bits
        options.limit_refresh_rate_hz = limit_refresh_rate_hz
        options.disable_hardware_pulsing = disable_hardware_pulsing

        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()

    def show(self, image: Image.Image) -> None:
        self.canvas.SetImage(image if image.mode == "RGB" else image.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self) -> None:
        self.matrix.Clear()

    def set_brightness(self, brightness: int) -> None:
        self.matrix.brightness = brightness

    def __enter__(self) -> MatrixDisplay:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.clear()


def scale_for_preview(image: Image.Image, scale: int, grid: bool = False) -> Image.Image:
    """Blow a 64x64 frame up to something you can actually look at.

    Nearest-neighbour on purpose: the point of a pixel-art display is the pixels, and any
    smoothing filter would hide exactly what you are trying to judge. `grid` draws the
    inter-pixel gutter, which approximates how the panel reads behind a diffuser.
    """
    if scale <= 1:
        return image

    scaled = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    if grid and scale >= 3:
        draw = ImageDraw.Draw(scaled)
        for x in range(0, scaled.width, scale):
            draw.line((x, 0, x, scaled.height), fill=(0, 0, 0))
        for y in range(0, scaled.height, scale):
            draw.line((0, y, scaled.width, y), fill=(0, 0, 0))
    return scaled


class MockDisplay:
    """Writes the current frame to a PNG."""

    def __init__(self, output: Path, scale: int = 1, grid: bool = False) -> None:
        self.output = output
        self.scale = scale
        self.grid = grid
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def show(self, image: Image.Image) -> None:
        # Replaced atomically: without --once this rewrites the file at the frame rate,
        # and a plain save lets readers observe a half-written PNG.
        temp_path = self.output.with_name(f".{self.output.name}.tmp")
        scale_for_preview(image, self.scale, self.grid).save(temp_path, format="PNG")
        os.replace(temp_path, self.output)

    def clear(self) -> None:
        return

    def __enter__(self) -> MockDisplay:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.clear()


def terminal_scale_for(
    frame_size: tuple[int, int],
    terminal_size: tuple[int, int],
    reserved_rows: int = 2,
) -> int:
    """Largest whole-pixel magnification of `frame_size` that fits the terminal.

    A magnified pixel is `scale` columns wide but only `scale / 2` character rows tall,
    because each row of cells carries two pixel rows.
    """
    frame_width, frame_height = frame_size
    columns, rows = terminal_size
    usable_rows = max(rows - reserved_rows, 1)

    by_width = columns // frame_width
    by_height = (usable_rows * 2) // frame_height
    return max(1, min(by_width, by_height))


class TerminalDisplay:
    """Live preview in the terminal, for when the panel has not arrived yet.

    Each character cell is two vertical pixels: the upper half block takes the foreground
    colour and the lower half the background, which also makes each pixel roughly square
    given typical cell proportions. So a 64x64 frame is 64 columns by 32 rows. Truecolour
    SGR codes only, no dependencies, and it works over SSH.

    By default it magnifies to fill the terminal and draws a status line underneath. It
    renders into the alternate screen buffer so it does not clobber scrollback, except in
    single-frame mode where leaving the alternate screen would erase the thing you asked
    to see.
    """

    HALF_BLOCK = "▀"
    RESERVED_ROWS = 2  # status line, plus one so the frame never touches the bottom edge

    def __init__(
        self,
        stream: Any = None,
        scale: int | None = None,
        grid: bool = False,
        status: Callable[[], str] | None = None,
        alt_screen: bool = True,
        terminal_size: tuple[int, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.scale = scale
        self.grid = grid
        self.status = status
        self.alt_screen = alt_screen
        self.terminal_size = terminal_size
        self._clock = clock
        self._started = False
        self._fps = 0.0
        self._last_shown_at: float | None = None
        self._warned: set[str] = set()

    def _current_terminal_size(self) -> tuple[int, int]:
        if self.terminal_size is not None:
            return self.terminal_size
        columns, rows = shutil.get_terminal_size((80, 24))
        return columns, rows

    def _required_size(self, frame_size: tuple[int, int], scale: int) -> tuple[int, int]:
        """Columns and rows needed to draw `frame_size` at `scale`, status line included."""
        return frame_size[0] * scale, (frame_size[1] * scale) // 2 + self.RESERVED_ROWS

    def _warn_once(self, message: str) -> None:
        # stderr, so the alternate screen does not swallow it.
        if message not in self._warned:
            self._warned.add(message)
            print(f"Warning: {message}", file=sys.stderr, flush=True)

    def _resolve_scale(self, frame_size: tuple[int, int]) -> int:
        """Pick a magnification that actually fits, and say so when it is not what was asked.

        An explicit --preview-scale is clamped rather than honoured blindly: overflowing the
        width makes every row line-wrap, which desynchronises the in-place redraw and turns
        the preview into confetti. A smaller picture beats a broken one.
        """
        columns, rows = self._current_terminal_size()
        largest_that_fits = terminal_scale_for(frame_size, (columns, rows), self.RESERVED_ROWS)
        chosen = largest_that_fits if self.scale is None else min(self.scale, largest_that_fits)

        if self.scale is not None and self.scale > chosen:
            need_columns, need_rows = self._required_size(frame_size, self.scale)
            self._warn_once(
                f"--preview-scale {self.scale} needs a {need_columns}x{need_rows} terminal, "
                f"but this one is {columns}x{rows}. Falling back to {chosen}x."
            )
            return chosen

        need_columns, need_rows = self._required_size(frame_size, chosen)
        if columns < need_columns or rows < need_rows:
            self._warn_once(
                f"terminal is {columns}x{rows} but the preview needs "
                f"{need_columns}x{need_rows}. It will be clipped until you resize."
            )
        return chosen

    @classmethod
    def frame_to_text(cls, image: Image.Image) -> str:
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        pixels = rgb.load()
        width, height = rgb.size
        lines = []
        for y in range(0, height, 2):
            parts: list[str] = []
            previous: tuple[tuple[int, ...], tuple[int, ...]] | None = None
            for x in range(width):
                top = pixels[x, y]
                # An odd-height frame has no bottom pixel for the final row.
                bottom = pixels[x, y + 1] if y + 1 < height else (0, 0, 0)
                if (top, bottom) != previous:
                    parts.append(
                        f"\x1b[38;2;{top[0]};{top[1]};{top[2]}"
                        f";48;2;{bottom[0]};{bottom[1]};{bottom[2]}m"
                    )
                    previous = (top, bottom)
                parts.append(cls.HALF_BLOCK)
            lines.append("".join(parts) + "\x1b[0m")
        return "\n".join(lines)

    def _measure_fps(self) -> None:
        now = self._clock()
        if self._last_shown_at is not None:
            delta = now - self._last_shown_at
            if delta > 0:
                instant = 1.0 / delta
                # Lightly smoothed, or the reading jitters too much to read.
                self._fps = instant if self._fps == 0.0 else self._fps * 0.8 + instant * 0.2
        self._last_shown_at = now

    def status_line(self) -> str:
        parts = [self.status()] if self.status else []
        if self._fps > 0:
            parts.append(f"{self._fps:.1f} fps")
        parts.append("ctrl-c to stop")
        return " · ".join(part for part in parts if part)

    def show(self, image: Image.Image) -> None:
        scale = self._resolve_scale(image.size)

        if not self._started:
            if self.alt_screen:
                self.stream.write("\x1b[?1049h")  # alternate screen, preserves scrollback
            # Wrapping is off so an over-wide row is truncated rather than pushed onto the
            # next line, which would desynchronise every subsequent redraw.
            self.stream.write("\x1b[?7l\x1b[2J\x1b[?25l")  # no wrap, clear, hide cursor
            self._started = True

        self._measure_fps()
        frame = scale_for_preview(image, scale, self.grid)
        # Home the cursor and overwrite in place rather than scrolling. \x1b[J at the end
        # erases anything left over from a larger previous frame.
        self.stream.write(
            "\x1b[H"
            + self.frame_to_text(frame)
            + f"\n\x1b[2m{self.status_line()}\x1b[0m\x1b[K\n\x1b[J"
        )
        self.stream.flush()

    def clear(self) -> None:
        if not self._started:
            return
        self.stream.write("\x1b[0m\x1b[?25h\x1b[?7h")  # reset colours, cursor, wrapping
        if self.alt_screen:
            self.stream.write("\x1b[?1049l")  # back to the shell, scrollback intact
        else:
            self.stream.write("\n")
        self.stream.flush()
        self._started = False

    def __enter__(self) -> TerminalDisplay:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.clear()


class GifRecorderDisplay:
    """Buffers frames and writes an animated GIF on exit.

    Useful for reviewing the spin speed, or for sharing what the panel will do. Note GIF
    is limited to 256 colours per frame, so album art is quantised in the recording but
    not on the real panel.
    """

    def __init__(self, output: Path, fps: float, scale: int = 1, grid: bool = False) -> None:
        self.output = output
        self.fps = fps
        self.scale = scale
        self.grid = grid
        self.frames: list[Image.Image] = []
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def show(self, image: Image.Image) -> None:
        self.frames.append(scale_for_preview(image, self.scale, self.grid).copy())

    def clear(self) -> None:
        return

    def save(self) -> None:
        if not self.frames:
            return
        first, *rest = self.frames
        first.save(
            self.output,
            format="GIF",
            save_all=True,
            append_images=rest,
            # GIF stores delays in hundredths of a second, so anything faster than 50 fps
            # cannot be represented.
            duration=max(20, round(1000.0 / self.fps)),
            loop=0,
        )
        print(f"Wrote {len(self.frames)} frames to {self.output}", flush=True)

    def __enter__(self) -> GifRecorderDisplay:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.save()


class ConfiguredDisplay:
    """Applies live settings to whatever display it wraps.

    Keeps brightness out of the scene logic: scenes produce pixels, this decides how
    brightly they are driven. Backends with no brightness control (the previews) simply
    have no set_brightness, so it is a no-op for them.
    """

    def __init__(self, inner: Any, config_store: ConfigStore) -> None:
        self.inner = inner
        self.config_store = config_store
        self._applied: int | None = None

    def show(self, image: Image.Image) -> None:
        brightness = self.config_store.current().brightness
        if brightness is not None and brightness != self._applied:
            setter = getattr(self.inner, "set_brightness", None)
            if setter is not None:
                setter(brightness)
            self._applied = brightness
        self.inner.show(image)

    def clear(self) -> None:
        self.inner.clear()

    def __enter__(self) -> ConfiguredDisplay:
        self.inner.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.inner.__exit__(*exc_info)


class Display(Protocol):
    def show(self, image: Image.Image) -> None:
        ...

    def clear(self) -> None:
        ...

    def __enter__(self) -> Any:
        ...

    def __exit__(self, *exc_info: object) -> None:
        ...


def matrix_options_from(args: argparse.Namespace) -> dict[str, Any]:
    """Translate CLI flags into MatrixDisplay keyword arguments."""
    return {
        "rows": args.rows,
        "cols": args.cols,
        "chain_length": args.chain_length,
        "parallel": args.parallel,
        "brightness": args.brightness,
        "gpio_slowdown": args.gpio_slowdown,
        "hardware_mapping": args.hardware_mapping,
        "pwm_bits": args.pwm_bits,
        "limit_refresh_rate_hz": args.limit_refresh_rate_hz,
        "disable_hardware_pulsing": args.no_hardware_pulse,
    }


def build_display(
    args: argparse.Namespace, status: Callable[[], str] | None = None
) -> Display:
    """Pick an output backend. The three preview backends are mutually exclusive.

    `--preview-scale` defaults to None, which means 1x for the file backends and
    fill-the-window for the terminal.
    """
    if args.mock_output:
        return MockDisplay(
            args.mock_output, scale=args.preview_scale or 1, grid=args.preview_grid
        )
    if args.preview_terminal:
        return TerminalDisplay(
            scale=args.preview_scale,
            grid=args.preview_grid,
            status=status,
            # Leaving the alternate screen would erase the single frame we just drew.
            alt_screen=not args.once,
        )
    if args.record_gif:
        return GifRecorderDisplay(
            args.record_gif, fps=args.fps, scale=args.preview_scale or 1, grid=args.preview_grid
        )
    return MatrixDisplay(**matrix_options_from(args))


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def demo_album_art(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size // 2, size // 2), fill=(238, 70, 60))
    draw.rectangle((size // 2, 0, size, size // 2), fill=(245, 180, 40))
    draw.rectangle((0, size // 2, size // 2, size), fill=(35, 150, 235))
    draw.rectangle((size // 2, size // 2, size, size), fill=(65, 185, 95))
    draw.line((0, 0, size, size), fill=(255, 255, 255), width=max(2, size // 18))
    draw.line((size, 0, 0, size), fill=(0, 0, 0), width=max(2, size // 22))
    return image


def playback_art_from_response(playback: dict[str, Any] | None) -> PlaybackArt | None:
    if not playback:
        return None

    item = playback.get("item")
    if not item:
        return None

    item_type = item.get("type")
    if item_type == "track":
        images = item.get("album", {}).get("images", [])
    else:
        images = item.get("images", [])

    if not images:
        return None

    image = max(images, key=lambda candidate: candidate.get("width") or 0)
    item_id = item.get("id") or item.get("uri") or image["url"]

    name = item.get("name") or ""
    artists = ", ".join(
        artist.get("name", "") for artist in (item.get("artists") or []) if artist.get("name")
    )
    title = f"{artists} - {name}".strip(" -") if artists else name

    return PlaybackArt(
        key=str(item_id),
        image_url=image["url"],
        is_playing=bool(playback.get("is_playing")),
        title=title or None,
    )


def download_image(url: str, http: HttpFn = http_request) -> Image.Image:
    response = http("GET", url, timeout=15)
    if response.status != 200:
        raise_http_error(response, f"Album art download from {url}")
    with Image.open(BytesIO(response.body)) as art:
        return art.convert("RGB")


def disc_geometry(size: int) -> tuple[int, int]:
    """Return (margin, disc_size) for a square frame of `size` pixels."""
    margin = max(2, size // 32)
    return margin, size - margin * 2


def fit_art_to_disc(art: Image.Image, disc_size: int) -> Image.Image:
    """Scale album art down to the disc. This is the expensive step (LANCZOS)."""
    return ImageOps.fit(art, (disc_size, disc_size), method=Image.Resampling.LANCZOS)


def build_disc_mask(disc_size: int) -> Image.Image:
    mask = Image.new("L", (disc_size, disc_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, disc_size - 1, disc_size - 1), fill=255)
    return mask


def draw_record_furniture(frame: Image.Image, size: int) -> None:
    """Draw the outer ring, centre label and spindle hole onto an RGBA frame.

    Deliberately not pre-rendered onto a transparent overlay and composited once: the
    label and its outline are semi-transparent, and PIL's "RGBA" draw mode blends against
    the destination RGB rather than in premultiplied space, so drawing onto transparency
    and compositing darkens them (measured up to 142 per channel off). The saving would
    have been ~0.02 ms/frame, which is not worth changing what the panel shows.
    """
    margin, _ = disc_geometry(size)
    draw = ImageDraw.Draw(frame, "RGBA")
    draw.ellipse(
        (margin, margin, size - margin - 1, size - margin - 1),
        outline=(6, 6, 6, 255),
        width=max(1, size // 32),
    )

    center = size // 2
    label_radius = max(5, size // 11)
    hole_radius = max(2, size // 25)
    draw.ellipse(
        (center - label_radius, center - label_radius, center + label_radius, center + label_radius),
        fill=(16, 16, 16, 210),
        outline=(220, 220, 220, 90),
    )
    draw.ellipse(
        (center - hole_radius, center - hole_radius, center + hole_radius, center + hole_radius),
        fill=(0, 0, 0, 255),
    )


def render_idle(size: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    margin, _ = disc_geometry(size)
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), outline=(55, 55, 55), width=2)
    center = size // 2
    radius = max(3, size // 18)
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=(18, 18, 18))
    return frame


class FrameRenderer(Protocol):
    """A display style.

    `fit` is called on the poll thread when new art arrives, `render` on the frame loop.
    `animated` tells the frame loop whether the rotation is worth advancing.
    """

    animated: bool

    def fit(self, art: Image.Image) -> Image.Image:
        ...

    def idle(self) -> Image.Image:
        ...

    def render(self, fitted_art: Image.Image, angle: float) -> Image.Image:
        ...


class AlbumArtRenderer:
    """Full-bleed static album art.

    Cover art is square and so is the panel, so this fills it edge to edge with no disc,
    label or rotation. `render` is effectively free: the art is already the right size and
    mode by the time it gets here, so there is nothing to recompute per frame.
    """

    animated = False

    def __init__(self, size: int) -> None:
        self.size = size
        self._idle = render_idle(size)

    def fit(self, art: Image.Image) -> Image.Image:
        fitted = ImageOps.fit(art, (self.size, self.size), method=Image.Resampling.LANCZOS)
        return fitted if fitted.mode == "RGB" else fitted.convert("RGB")

    def idle(self) -> Image.Image:
        return self._idle

    def render(self, fitted_art: Image.Image, angle: float) -> Image.Image:
        if fitted_art.size != (self.size, self.size) or fitted_art.mode != "RGB":
            fitted_art = self.fit(fitted_art)
        return fitted_art


class RecordRenderer:
    """Renders the spinning record.

    What depends only on frame size — the disc mask, the idle frame — is built once here.
    The album art resize is deliberately *not* done per frame: it is the expensive step
    (LANCZOS from a 640x640 download), so the poll thread does it via `fit` when a new
    track arrives, keeping it off the frame-critical path.
    """

    animated = True

    def __init__(self, size: int) -> None:
        self.size = size
        self.margin, self.disc_size = disc_geometry(size)
        self._mask = build_disc_mask(self.disc_size)
        self._idle = render_idle(size)

    def fit(self, art: Image.Image) -> Image.Image:
        return fit_art_to_disc(art, self.disc_size)

    def idle(self) -> Image.Image:
        return self._idle

    def render(self, fitted_art: Image.Image, angle: float) -> Image.Image:
        if fitted_art.size != (self.disc_size, self.disc_size):
            # Tolerate un-fitted art so previews and ad-hoc calls still work.
            fitted_art = self.fit(fitted_art)

        # The album art is the record surface: rotate it, then cut it into a circular disk.
        rotated = fitted_art.rotate(angle, resample=Image.Resampling.BICUBIC)
        frame = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 255))
        frame.paste(rotated, (self.margin, self.margin), self._mask)
        draw_record_furniture(frame, self.size)
        return frame.convert("RGB")


# --------------------------------------------------------------------------------------
# Clock
#
# A hand-drawn bitmap font rather than a TrueType one: at this size any antialiasing just
# smears pixels, and integer scaling of a 5x7 grid keeps every stroke exactly one pixel.
# --------------------------------------------------------------------------------------


CLOCK_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    ":": (".", ".", "#", ".", "#", ".", "."),
    " ": (".", ".", ".", ".", ".", ".", "."),
}
CLOCK_GLYPH_HEIGHT = 7
CLOCK_GLYPH_GAP = 1  # in unscaled pixels


def clock_text_mask(text: str, scale: int) -> Image.Image:
    """Render `text` as an L-mode mask at `scale`x, using the bitmap glyphs above."""
    glyphs = [CLOCK_GLYPHS[character] for character in text if character in CLOCK_GLYPHS]
    if not glyphs:
        return Image.new("L", (1, 1), 0)

    widths = [len(glyph[0]) for glyph in glyphs]
    total = sum(widths) + CLOCK_GLYPH_GAP * (len(glyphs) - 1)
    mask = Image.new("L", (total * scale, CLOCK_GLYPH_HEIGHT * scale), 0)
    draw = ImageDraw.Draw(mask)

    x = 0
    for glyph, width in zip(glyphs, widths):
        for row, line in enumerate(glyph):
            for column, pixel in enumerate(line):
                if pixel == "#":
                    left, top = (x + column) * scale, row * scale
                    draw.rectangle((left, top, left + scale - 1, top + scale - 1), fill=255)
        x += width + CLOCK_GLYPH_GAP
    return mask


def clock_scale_for(size: int, text: str) -> int:
    """Largest integer glyph scale that leaves a small margin on a `size` square panel."""
    mask_width = clock_text_mask(text, 1).width
    usable = max(size - 4, 1)
    by_width = usable // max(mask_width, 1)
    by_height = usable // CLOCK_GLYPH_HEIGHT
    return max(1, min(by_width, by_height))


def draw_clock_text(
    frame: Image.Image,
    text: str,
    scale: int,
    colour: tuple[int, int, int] = (255, 255, 255),
    outline: tuple[int, int, int] | None = (0, 0, 0),
    centre_y: int | None = None,
) -> None:
    """Draw `text` centred on `frame`, optionally haloed so it reads over a photo.

    The halo is a 1px outline rather than a darkened box behind the text: it costs far
    less of the picture while still keeping white digits legible on a pale image.
    """
    mask = clock_text_mask(text, scale)
    x = (frame.width - mask.width) // 2
    y = (frame.height - mask.height) // 2 if centre_y is None else centre_y - mask.height // 2

    if outline is not None:
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
            frame.paste(outline, (x + dx, y + dy), mask)
    frame.paste(colour, (x, y), mask)


def format_clock(moment: datetime, use_24_hour: bool) -> str:
    if use_24_hour:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}"


def render_test_pattern(size: int, offset: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    colors = (
        (255, 0, 0),
        (255, 160, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 120, 255),
        (80, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    )
    stripe_width = max(1, size // len(colors))
    for index, color in enumerate(colors):
        x0 = (index * stripe_width + offset) % size
        draw.rectangle((x0, 0, min(size - 1, x0 + stripe_width - 1), size - 1), fill=color)
        if x0 + stripe_width > size:
            draw.rectangle((0, 0, (x0 + stripe_width) % size, size - 1), fill=color)
    draw.rectangle((0, 0, size - 1, size - 1), outline=(255, 255, 255))
    return frame


RENDERER_STYLES: dict[str, Callable[[int], FrameRenderer]] = {
    "record": RecordRenderer,
    "art": AlbumArtRenderer,
}


def build_renderer(style: str, size: int) -> FrameRenderer:
    return RENDERER_STYLES[style](size)


def render_preview_frames(directory: Path, style: str = "record", scale: int = 1) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    renderer = build_renderer(style, 64)
    fitted = renderer.fit(demo_album_art(96))
    angles = (0, 45, 90, 135) if renderer.animated else (0,)
    for index, angle in enumerate(angles):
        frame = scale_for_preview(renderer.render(fitted, angle), scale)
        frame.save(directory / f"album-disk-{index:02d}.png")


# --------------------------------------------------------------------------------------
# Playback state and polling
# --------------------------------------------------------------------------------------


class PlaybackState:
    """Playback state shared between the poll thread and the render loop.

    The lock lives with the data it protects so no caller has to remember to take it.
    Images are always replaced wholesale, never mutated in place, so the render loop can
    hold a reference to one while the poll thread swaps in the next.
    """

    def __init__(
        self,
        art_key: str | None = None,
        image_url: str | None = None,
        image: Image.Image | None = None,
        is_playing: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._art_key = art_key
        self._image_url = image_url
        self._image = image
        self._is_playing = is_playing
        self._title: str | None = None
        # Set the first time the poll thread reports anything at all, success or idle.
        self._first_update = threading.Event()

    @property
    def art_key(self) -> str | None:
        with self._lock:
            return self._art_key

    @property
    def image_url(self) -> str | None:
        with self._lock:
            return self._image_url

    @property
    def image(self) -> Image.Image | None:
        with self._lock:
            return self._image

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing

    @property
    def title(self) -> str | None:
        with self._lock:
            return self._title

    def snapshot(self) -> tuple[Image.Image | None, bool]:
        """Read the art and playing flag together, so a frame never mixes two updates."""
        with self._lock:
            return self._image, self._is_playing

    def needs_download(self, art: PlaybackArt) -> bool:
        with self._lock:
            return art.key != self._art_key or art.image_url != self._image_url

    def wait_for_first_update(self, timeout: float) -> bool:
        """Block until the poll thread has reported once. Returns False on timeout."""
        return self._first_update.wait(timeout)

    def update(self, art: PlaybackArt, image: Image.Image | None) -> None:
        with self._lock:
            self._art_key = art.key
            self._image_url = art.image_url
            self._is_playing = art.is_playing
            self._title = art.title
            if image is not None:
                self._image = image
        self._first_update.set()

    def clear(self) -> None:
        with self._lock:
            self._art_key = None
            self._image_url = None
            self._image = None
            self._is_playing = False
            self._title = None
        self._first_update.set()


def poll_provider(
    provider: PlaybackProvider,
    state: PlaybackState,
    stop_event: threading.Event,
    poll_seconds: float,
    prepare_image: Callable[[str], Image.Image],
) -> None:
    last_status: str | None = None

    def report(status: str) -> None:
        nonlocal last_status
        if status != last_status:
            print(f"{provider.name}: {status}", flush=True)
            last_status = status

    while not stop_event.is_set():
        try:
            art = provider.get_playback_art()

            if art:
                image = prepare_image(art.image_url) if state.needs_download(art) else None
                state.update(art, image)
                report(f"art found, is_playing={art.is_playing}")
            else:
                state.clear()
                report("no playback item")
        except ProviderRateLimitError as exc:
            report(f"rate limited, retrying in {exc.retry_after_seconds}s")
            stop_event.wait(max(poll_seconds, float(exc.retry_after_seconds)))
            continue
        except Exception as exc:
            # Deduplicated: a sustained outage would otherwise spam the journal at the
            # poll rate. The display keeps showing the last art until the provider says
            # otherwise, so a transient failure is not visible on the panel.
            report(f"poll failed: {exc}")

        stop_event.wait(poll_seconds)


# --------------------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------------------


PHOTO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})


class PhotoLibrary:
    """Slideshow of local images, decoded off the frame thread.

    Decoding and downscaling a phone photo takes far longer than a frame budget on a Pi
    Zero, so a worker thread prepares the next image and publishes it when ready. The frame
    loop only ever reads an already-fitted 64x64 image.
    """

    def __init__(
        self,
        directory: Path,
        size: int,
        seconds: Callable[[], float],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.directory = directory
        self.size = size
        self.seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._current: Image.Image | None = None
        self._current_name: str | None = None
        self._paths: list[Path] = []
        self._index = 0
        self._shown_at: float | None = None
        self._listing_signature: tuple[float, int] | None = None
        # Set once a photo is decoded and ready, so a bounded preview can wait for it
        # rather than capturing the fallback frame.
        self._first_ready = threading.Event()

    def wait_for_first_photo(self, timeout: float) -> bool:
        return self._first_ready.wait(timeout)

    def available(self) -> list[str]:
        with self._lock:
            return [path.name for path in self._paths]

    def current(self) -> Image.Image | None:
        with self._lock:
            return self._current

    def current_name(self) -> str | None:
        with self._lock:
            return self._current_name

    def scan(self) -> list[Path]:
        """Re-list the directory when it changes, so new photos need no restart."""
        try:
            stat = self.directory.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            signature = None

        if signature is not None and signature == self._listing_signature and self._paths:
            return self._paths

        self._listing_signature = signature
        try:
            found = sorted(
                path
                for path in self.directory.iterdir()
                if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES
            )
        except OSError:
            found = []

        with self._lock:
            self._paths = found
            if self._index >= len(found):
                self._index = 0
        return found

    def _load(self, path: Path) -> Image.Image | None:
        try:
            with Image.open(path) as image:
                return ImageOps.fit(
                    image.convert("RGB"), (self.size, self.size), method=Image.Resampling.LANCZOS
                )
        except Exception as exc:  # unreadable, truncated, or not really an image
            print(f"photos: skipping {path.name} ({exc})", flush=True)
            return None

    def advance(self) -> bool:
        """Publish the next usable photo. Returns False when the directory has none."""
        paths = self.scan()
        if not paths:
            with self._lock:
                self._current = None
                self._current_name = None
            return False

        for _ in range(len(paths)):
            with self._lock:
                index = self._index % len(paths)
                self._index = index + 1
            path = paths[index]
            image = self._load(path)
            if image is not None:
                with self._lock:
                    self._current = image
                    self._current_name = path.name
                    self._shown_at = self._clock()
                self._first_ready.set()
                return True
        return False

    def tick(self) -> None:
        """Advance if the current photo has had its time. Safe to call from a worker."""
        with self._lock:
            shown_at = self._shown_at
            has_current = self._current is not None
        if not has_current or shown_at is None or self._clock() - shown_at >= self.seconds():
            self.advance()

    def run(self, stop_event: threading.Event, interval: float = 0.5) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"photos: {exc}", flush=True)
            stop_event.wait(interval)


# --------------------------------------------------------------------------------------
# Scenes
#
# A scene is "what the panel shows". Album scenes are driven by playback; the rest stand
# alone, which is what makes an idle state (clock, photos) possible.
# --------------------------------------------------------------------------------------


class Scene(Protocol):
    def frame(self, now: float) -> Image.Image:
        ...


class BlankScene:
    """The original idle frame: a dim ring and centre dot."""

    def __init__(self, size: int) -> None:
        self._frame = render_idle(size)

    def frame(self, now: float) -> Image.Image:
        return self._frame


class ClockScene:
    def __init__(
        self,
        size: int,
        use_24_hour: Callable[[], bool],
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.size = size
        self.use_24_hour = use_24_hour
        self._now = now
        self._cache: tuple[str, Image.Image] | None = None

    def text(self) -> str:
        return format_clock(self._now(), self.use_24_hour())

    def frame(self, now: float) -> Image.Image:
        text = self.text()
        # Re-render once a minute, not 20 times a second.
        if self._cache and self._cache[0] == text:
            return self._cache[1]

        frame = Image.new("RGB", (self.size, self.size), (0, 0, 0))
        draw_clock_text(frame, text, clock_scale_for(self.size, text), outline=None)
        self._cache = (text, frame)
        return frame


class PhotoScene:
    """Slideshow, optionally with the time drawn over it."""

    def __init__(
        self,
        library: PhotoLibrary,
        size: int,
        clock: ClockScene | None = None,
    ) -> None:
        self.library = library
        self.size = size
        self.clock = clock
        self._empty = render_idle(size)

    def frame(self, now: float) -> Image.Image:
        photo = self.library.current()
        if photo is None:
            # No photos yet: fall back to the clock if we have one, else the idle frame.
            return self.clock.frame(now) if self.clock else self._empty
        if self.clock is None:
            return photo

        frame = photo.copy()
        text = self.clock.text()
        # A quarter from the bottom, so faces in the middle stay visible.
        draw_clock_text(
            frame,
            text,
            max(1, clock_scale_for(self.size, text) - 1),
            centre_y=int(self.size * 0.78),
        )
        return frame


class AlbumScene:
    """Album art, drawn in whichever style the config currently asks for."""

    def __init__(
        self,
        renderers: dict[str, FrameRenderer],
        state: PlaybackState,
        config_store: ConfigStore,
        start_time: float | None = None,
    ) -> None:
        self.renderers = renderers
        self.state = state
        self.config_store = config_store
        self.angle = 0.0
        self._last_frame_at = start_time

    def frame(self, now: float) -> Image.Image:
        config = self.config_store.current()
        renderer = self.renderers[config.style]
        art, is_playing = self.state.snapshot()

        delta = 0.0 if self._last_frame_at is None else now - self._last_frame_at
        self._last_frame_at = now

        if is_playing and art is not None and renderer.animated:
            self.angle = advance_angle(self.angle, config.rpm, delta)

        if art is None:
            return renderer.idle()
        # Art arrives pre-fitted for whichever style was active when it was downloaded.
        # After a style change render() refits, which is cheap because the stored art is
        # already small; the next poll re-fits it properly.
        return renderer.render(art, self.angle)


class SceneDirector:
    """Chooses the scene each frame: an override, else album art, else the idle scene."""

    def __init__(
        self,
        config_store: ConfigStore,
        album: Scene,
        scenes: dict[str, Scene],
        state: PlaybackState,
    ) -> None:
        self.config_store = config_store
        self.album = album
        self.scenes = scenes
        self.state = state

    def scene_name(self, config: Config) -> str:
        if config.override_scene:
            return config.override_scene
        return "album" if self.state.image is not None else config.idle_scene

    def __call__(self, now: float) -> Image.Image:
        config = self.config_store.current()
        name = self.scene_name(config)
        scene = self.album if name == "album" else self.scenes.get(name)
        if scene is None:
            scene = self.album
        return scene.frame(now)


def build_scenes(
    size: int,
    config_store: ConfigStore,
    library: PhotoLibrary,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Scene]:
    clock = ClockScene(size, use_24_hour=lambda: config_store.current().clock_24_hour, now=now)
    return {
        "blank": BlankScene(size),
        "clock": clock,
        "photos": PhotoScene(library, size),
        "photos+clock": PhotoScene(library, size, clock=clock),
    }


# --------------------------------------------------------------------------------------
# Frame sources and the display loop
# --------------------------------------------------------------------------------------


def advance_angle(angle: float, rpm: float, delta_seconds: float) -> float:
    return (angle - 360.0 * (rpm / 60.0) * delta_seconds) % 360.0


class TestPatternFrameSource:
    def __init__(self, size: int) -> None:
        self.size = size
        self.offset = 0

    def __call__(self, now: float) -> Image.Image:
        frame = render_test_pattern(self.size, self.offset)
        self.offset = (self.offset + 1) % self.size
        return frame


def drive_display(
    display: Display,
    next_frame: Callable[[float], Image.Image],
    *,
    fps: float,
    max_frames: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Show frames at `fps` until interrupted, or until `max_frames` have been shown.

    Cleanup is the caller's, so it also happens on an early failure (see `run`).
    """
    shown = 0
    try:
        while True:
            frame_start = monotonic()
            display.show(next_frame(frame_start))
            shown += 1
            if max_frames is not None and shown >= max_frames:
                return
            sleep(max(0.0, (1.0 / fps) - (monotonic() - frame_start)))
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------------------
# Provider wiring and CLI
# --------------------------------------------------------------------------------------


def missing_env_vars(env_values: dict[str, str | None]) -> list[str]:
    return [name for name, value in env_values.items() if not value]


def _build_spotify(args: argparse.Namespace, env: dict[str, str]) -> PlaybackProvider:
    return SpotifyClient(
        client_id=env["SPOTIFY_CLIENT_ID"],
        client_secret=env["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI,
        token_store=FileTokenStore(args.token_cache),
        open_browser=not args.no_browser,
        callback_timeout_seconds=args.auth_timeout_seconds,
    )


def _build_lastfm(args: argparse.Namespace, env: dict[str, str]) -> PlaybackProvider:
    return LastFmClient(api_key=env["LASTFM_API_KEY"], user=env["LASTFM_USER"])


def _build_ytmusic(args: argparse.Namespace, env: dict[str, str]) -> PlaybackProvider:
    return YouTubeMusicClient(
        auth_headers_path=args.ytmusic_auth_headers,
        stale_after_seconds=args.ytmusic_stale_seconds,
    )


def _build_demo(args: argparse.Namespace, env: dict[str, str]) -> PlaybackProvider:
    return DemoProvider(cycle_seconds=args.demo_cycle_seconds)


def _lastfm_verified_message(args: argparse.Namespace, provider: Any) -> str:
    # Name the account that answered. Last.fm only rejects usernames that do not exist, so
    # a typo that happens to be somebody else's account verifies happily and then shows
    # their listening instead of yours.
    summary = getattr(provider, "account_summary", None)
    if summary:
        return f"Last.fm verified: {summary}. Check that is your account."
    return "Last.fm API key and user verified."


@dataclass(frozen=True)
class ProviderSpec:
    required_env: tuple[str, ...]
    factory: Callable[[argparse.Namespace, dict[str, str]], PlaybackProvider]
    verified_message: Callable[[argparse.Namespace, Any], str]


PROVIDERS: dict[str, ProviderSpec] = {
    "spotify": ProviderSpec(
        required_env=("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
        factory=_build_spotify,
        verified_message=lambda args, provider: f"Spotify token cached at {args.token_cache}",
    ),
    "youtube-music": ProviderSpec(
        required_env=(),
        factory=_build_ytmusic,
        verified_message=lambda args, provider: (
            f"YouTube Music auth headers verified at {args.ytmusic_auth_headers}"
        ),
    ),
    "lastfm": ProviderSpec(
        required_env=("LASTFM_API_KEY", "LASTFM_USER"),
        factory=_build_lastfm,
        verified_message=_lastfm_verified_message,
    ),
    "demo": ProviderSpec(
        required_env=(),
        factory=_build_demo,
        verified_message=lambda args, provider: "Demo provider needs no credentials.",
    ),
}

# Last.fm is the default: one free API key covers whatever service actually plays the
# music, and it needs no OAuth round-trip on a headless Pi.
DEFAULT_PROVIDER = "lastfm"


def build_image_preparer(
    args: argparse.Namespace, current_renderer: Callable[[], FrameRenderer]
) -> Callable[[str], Image.Image]:
    """Return the poll thread's download-and-resize step.

    The resize happens here rather than in the frame loop because it is the expensive
    part, and the renderer is resolved per call so a style change is picked up on the next
    poll. The demo provider short-circuits the download entirely.
    """
    if args.provider == "demo":
        art = demo_album_art(512)
        return lambda url: current_renderer().fit(art)
    return lambda url: current_renderer().fit(download_image(url))


def build_provider(args: argparse.Namespace) -> PlaybackProvider:
    spec = PROVIDERS[args.provider]
    env: dict[str, str | None] = {name: os.environ.get(name) for name in spec.required_env}

    missing = missing_env_vars(env)
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    return spec.factory(args, {name: value or "" for name, value in env.items()})


# Command-line flag -> config key. They differ where the flag reads better: --scene says
# "show this now", which is the override rather than the idle fallback.
CONFIGURABLE_FLAGS = {
    "style": "style",
    "scene": "override_scene",
    "idle_scene": "idle_scene",
    "rpm": "rpm",
    "clock_24_hour": "clock_24_hour",
    "photo_seconds": "photo_seconds",
}


def config_overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """Settings the user passed explicitly on the command line.

    These win over config.json and are written back to it, so a flag can never silently
    do nothing just because the file already had a different value, and the web UI never
    disagrees with what is on the panel. Flags left off default to None and are ignored.
    """
    return {
        key: getattr(args, flag)
        for flag, key in CONFIGURABLE_FLAGS.items()
        if getattr(args, flag, None) is not None
    }


def open_config_store(args: argparse.Namespace) -> ConfigStore:
    store = ConfigStore(args.config)
    overrides = config_overrides_from(args)
    if overrides or not args.config.exists():
        # Materialise the file on first run so it is there to edit or serve.
        store.update(overrides)
    return store


def validate_args(args: argparse.Namespace) -> None:
    if args.chain_length * args.parallel != 1:
        raise SystemExit(
            "--chain-length and --parallel must both be 1. Each frame is rendered as a "
            "single square panel, so a chained or parallel display would leave the rest "
            "of the chain black."
        )


def playback_status(
    provider_name: str, style: str, state: PlaybackState, scene: str | None = None
) -> str:
    """One-line summary of what the display is doing, for the terminal preview."""
    art, is_playing = state.snapshot()
    if art is None:
        playback = "idle"
    else:
        playback = "playing" if is_playing else "paused"

    # Show the scene, and the album style too when the scene is album art, so the HUD
    # never hides which of the two is actually in play.
    descriptor = f"{scene}:{style}" if scene == "album" else (scene or style)
    parts = [provider_name, descriptor, playback]
    # Prefer the human-readable title; the key is often an opaque id (Last.fm mbid,
    # Spotify track id) which tells you nothing about whether the right thing is playing.
    track = state.title or state.art_key
    if track:
        parts.append(track if len(track) <= 40 else track[:39] + "…")
    return " · ".join(parts)


def control_status(
    provider_name: str,
    state: PlaybackState,
    director: SceneDirector,
    library: PhotoLibrary,
) -> dict[str, Any]:
    """What the web UI shows back, so you can confirm the change took effect."""
    config = director.config_store.current()
    art, is_playing = state.snapshot()
    return {
        "provider": provider_name,
        "scene": director.scene_name(config),
        "playback": "idle" if art is None else ("playing" if is_playing else "paused"),
        "track": state.title or state.art_key,
        "photo": library.current_name(),
        "photo_count": len(library.available()),
        "scenes": list(SCENE_NAMES),
        "styles": list(RENDERER_STYLES),
    }


# --------------------------------------------------------------------------------------
# Web control UI
#
# Deliberately narrow: three fixed routes, no filesystem serving, and every write goes
# through coerce_config. It listens on the LAN with no authentication, which is the usual
# bargain for a home device -- see --web-host to restrict it.
# --------------------------------------------------------------------------------------


CONTROL_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tune Matrix</title>
<!-- Empty icon: stops the browser requesting /favicon.ico, which this server has no
     route for and deliberately will not serve from disk. -->
<link rel="icon" href="data:,">
<style>
  /* Neutral cool surfaces on purpose: the album art is the only saturated thing in this
     product, so the chrome around it stays quiet. One accent, used only for "this is on".
     Follows the system theme, because this panel gets adjusted in daylight and at night. */
  :root {
    color-scheme: light dark;
    --bg:#f4f6f8; --raised:#ffffff; --sunken:#eceff3; --line:#dfe4ea;
    --ink:#12161c; --muted:#5f6874; --accent:#0e8177; --accent-ink:#0a5f58;
    --glow:rgba(14,129,119,.20); --shadow:0 1px 2px rgba(16,22,30,.06);
    --radius:14px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0e1116; --raised:#171b22; --sunken:#12161c; --line:#242a33;
      --ink:#e7ebf1; --muted:#8d97a5; --accent:#4fd6c6; --accent-ink:#6fe3d5;
      --glow:rgba(79,214,198,.22); --shadow:none;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; padding:22px 18px 40px; background:var(--bg); color:var(--ink);
    font:400 16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  main { max-width:32rem; margin:0 auto; }
  h1 { font-size:.9rem; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
       color:var(--muted); margin:0 0 1rem; }

  /* The hero is the answer to "what is it showing right now" -- the only reason to open
     this page on a phone. Everything below is how you change it. */
  .now { margin:0 0 1.75rem; }
  .now-line { display:flex; align-items:center; gap:.6rem; }
  .dot { width:.5rem; height:.5rem; border-radius:50%; background:var(--accent);
         box-shadow:0 0 0 4px var(--glow); flex:none; }
  .dot.off { background:var(--muted); box-shadow:none; }
  @media (prefers-reduced-motion: no-preference) {
    .dot:not(.off) { animation:pulse 2.4s ease-in-out infinite; }
    @keyframes pulse { 50% { box-shadow:0 0 0 7px transparent; } }
  }
  .now-scene { font-size:1.6rem; font-weight:650; letter-spacing:-.02em; margin:0; }
  .now-meta { color:var(--muted); font-size:.9rem; margin:.3rem 0 0;
              font-variant-numeric:tabular-nums; }

  .group { margin:0 0 1.35rem; }
  .group > h2 { font-size:.7rem; font-weight:600; letter-spacing:.1em;
                text-transform:uppercase; color:var(--muted); margin:0 0 .5rem .15rem; }

  /* One control idiom at every arity. Two fixed columns rather than wrapping: every group
     here has 2, 4 or 6 options, so this is always balanced and never leaves one option
     orphaned on its own line. It also gives long labels a full cell instead of wrapping. */
  .seg { display:grid; grid-template-columns:repeat(2, 1fr);
         gap:.35rem; padding:.35rem;
         background:var(--sunken); border:1px solid var(--line); border-radius:var(--radius); }
  .seg button {
    padding:.65rem .7rem; cursor:pointer;
    font:inherit; font-size:.925rem; color:var(--muted);
    background:transparent; border:1px solid transparent; border-radius:10px;
    transition:color .15s, background .15s, box-shadow .15s;
  }
  .seg button:hover { color:var(--ink); }
  .seg button[aria-pressed="true"] {
    /* Lit, not filled: a soft ring of accent light, like a pixel behind the diffuser. */
    color:var(--accent-ink); font-weight:600;
    background:var(--raised); border-color:var(--accent);
    box-shadow:0 0 0 3px var(--glow), var(--shadow);
  }
  .seg button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

  .slider { display:block; margin:0 0 1.1rem; }
  .slider-label { display:flex; justify-content:space-between; align-items:baseline;
                  font-size:.9rem; color:var(--muted); margin-bottom:.35rem; }
  .slider-label .val { color:var(--ink); font-weight:600;
                       font-variant-numeric:tabular-nums; }
  input[type=range] { width:100%; accent-color:var(--accent); }
  input[type=range]:focus-visible { outline:2px solid var(--accent); outline-offset:4px; }
  .hint { color:var(--muted); font-size:.8rem; margin:.5rem 0 0 .15rem; }
</style>
</head><body><main>
<h1>Tune Matrix</h1>

<section class="now">
  <div class="now-line">
    <span class="dot off" id="dot"></span>
    <p class="now-scene" id="nowScene">Connecting</p>
  </div>
  <p class="now-meta" id="nowMeta">Reading the display</p>
</section>

<section class="group">
  <h2>Show</h2>
  <div class="seg" id="scene"></div>
  <p class="hint">Auto follows your music, then falls back to your idle choice.</p>
</section>

<section class="group">
  <h2>Album art</h2>
  <div class="seg" id="style"></div>
</section>

<section class="group">
  <h2>When nothing's playing</h2>
  <div class="seg" id="idle"></div>
</section>

<section class="group">
  <h2>Clock</h2>
  <div class="seg" id="clockfmt"></div>
</section>

<section class="group">
  <h2>Levels</h2>
  <label class="slider">
    <span class="slider-label">Brightness <span class="val" id="brightnessVal"></span></span>
    <input type="range" id="brightness" min="1" max="100" step="1">
  </label>
  <label class="slider">
    <span class="slider-label">Record speed <span class="val" id="rpmVal"></span></span>
    <input type="range" id="rpm" min="1" max="120" step="1">
  </label>
  <label class="slider">
    <span class="slider-label">New photo every <span class="val" id="photo_secondsVal"></span></span>
    <input type="range" id="photo_seconds" min="2" max="300" step="1">
  </label>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
// Scene keys are how the display thinks; these are how a person does.
const SCENE_LABEL = {album:"Album art", blank:"Off", clock:"Clock",
                     photos:"Photos", "photos+clock":"Photos + clock"};
const STYLE_LABEL = {record:"Record", art:"Full frame"};
const SLIDERS = {brightness:v => v + "%", rpm:v => v + " rpm",
                 photo_seconds:v => v + "s"};
const name = (map, key) => map[key] || key;
let config = {}, meta = {scenes:[], styles:[]};

function segment(host, options, current, onPick) {
  host.replaceChildren(...options.map(([value, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("aria-pressed", String(value === current));
    button.onclick = () => onPick(value);
    return button;
  }));
}

function paint() {
  segment($("scene"), [["", "Auto"], ...meta.scenes.map(s => [s, name(SCENE_LABEL, s)])],
          config.override_scene || "", v => save({override_scene: v}));
  segment($("style"), meta.styles.map(s => [s, name(STYLE_LABEL, s)]), config.style,
          v => save({style: v}));
  segment($("idle"), meta.scenes.filter(s => s !== "album")
                         .map(s => [s, name(SCENE_LABEL, s)]),
          config.idle_scene, v => save({idle_scene: v}));
  segment($("clockfmt"), [[false, "12 hour"], [true, "24 hour"]], config.clock_24_hour,
          v => save({clock_24_hour: v}));
  for (const key in SLIDERS) {
    const value = key === "brightness" ? (config.brightness ?? 65) : config[key];
    $(key).value = value;
    $(key + "Val").textContent = SLIDERS[key](value);
  }
}

async function save(changes) {
  const response = await fetch("/api/config", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(changes),
  });
  config = await response.json();
  paint();
  refresh();
}

for (const key in SLIDERS) {
  // Track while dragging, save once on release: one write per adjustment, not per pixel.
  $(key).addEventListener("input", e => {
    $(key + "Val").textContent = SLIDERS[key](e.target.value);
  });
  $(key).addEventListener("change", e => save({[key]: Number(e.target.value)}));
}

async function refresh() {
  try {
    const status = await (await fetch("/api/status")).json();
    meta = {scenes: status.scenes, styles: status.styles};
    const scene = name(SCENE_LABEL, status.scene);
    $("nowScene").textContent = status.scene === "album"
      ? `${scene} · ${name(STYLE_LABEL, config.style)}` : scene;
    $("dot").className = "dot" + (status.playback === "playing" ? "" : " off");
    // Only mention what is actually on the panel: naming a photo while album art is
    // showing would describe something the display is not doing.
    const detail = [];
    if (status.scene === "album" && status.track) detail.push(status.track);
    if (status.scene.startsWith("photos")) {
      detail.push(status.photo || "no photos found");
    }
    detail.push(`${status.provider} · ${status.playback}`);
    $("nowMeta").textContent = detail.join(" — ");
  } catch (error) {
    $("nowScene").textContent = "No display";
    $("nowMeta").textContent = "Nothing is answering on this address. Is it still running?";
    $("dot").className = "dot off";
  }
}

(async () => {
  config = await (await fetch("/api/config")).json();
  await refresh();
  paint();
  setInterval(refresh, 3000);
})();
</script></body></html>
"""


class ControlServer:
    """Tiny stdlib control panel. Use as a context manager; serves on a daemon thread."""

    def __init__(
        self,
        config_store: ConfigStore,
        status: Callable[[], dict[str, Any]],
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.config_store = config_store
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._respond(status, json.dumps(payload).encode("utf-8"), "application/json")

            def do_GET(self) -> None:
                route = urllib.parse.urlparse(self.path).path
                if route in ("/", "/index.html"):
                    self._respond(200, CONTROL_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif route == "/api/config":
                    self._json(200, outer.config_store.current().as_dict())
                elif route == "/api/status":
                    self._json(200, outer.status())
                else:
                    # No filesystem serving at all: only these routes exist.
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if urllib.parse.urlparse(self.path).path != "/api/config":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 8192:
                    self._json(400, {"error": "expected a small JSON body"})
                    return
                try:
                    changes = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid JSON"})
                    return
                if not isinstance(changes, dict):
                    self._json(400, {"error": "expected a JSON object"})
                    return
                # coerce_config clamps every field, so no input reaches the display raw.
                self._json(200, outer.config_store.update(changes).as_dict())

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self.thread.start()
        print(f"Control UI on {self.url}", flush=True)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> ControlServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def frame_budget(args: argparse.Namespace) -> int | None:
    """How many frames to show before stopping, or None to run until interrupted."""
    if args.once:
        return 1
    if args.record_gif:
        return max(1, round(args.fps * args.record_seconds))
    return None


def run(args: argparse.Namespace) -> None:
    if args.preview_frames:
        render_preview_frames(
            args.preview_frames,
            style=args.style or Config().style,
            scale=args.preview_scale or 1,
        )
        return

    validate_args(args)
    load_dotenv()

    size = min(args.rows, args.cols)
    max_frames = frame_budget(args)

    # --test-pattern is the hardware bring-up path: it must work before any provider
    # credentials exist, so build/authorize the provider only when we actually need it.
    if args.test_pattern:
        with build_display(args, status=lambda: f"test pattern · {size}x{size}") as display:
            drive_display(
                display,
                TestPatternFrameSource(size),
                fps=args.fps,
                max_frames=max_frames,
            )
        return

    provider = build_provider(args)

    try:
        provider.authorize()
    except ProviderAuthError as exc:
        raise SystemExit(str(exc)) from exc
    except (ProviderError, OSError) as exc:
        # Transient: a Pi that boots before Wi-Fi associates should show the idle frame
        # and let the poll thread recover, not exit with a traceback and a dark panel.
        if args.auth_only:
            raise SystemExit(str(exc)) from exc
        print(
            f"{provider.name}: unavailable at startup ({exc}); showing idle until it recovers",
            flush=True,
        )

    if args.auth_only:
        print(PROVIDERS[args.provider].verified_message(args, provider))
        return

    config_store = open_config_store(args)

    renderers = {name: build_renderer(name, size) for name in RENDERER_STYLES}
    state = PlaybackState()
    library = PhotoLibrary(
        args.photos, size, seconds=lambda: config_store.current().photo_seconds
    )
    scenes = build_scenes(size, config_store, library)
    album = AlbumScene(renderers, state, config_store)
    director = SceneDirector(config_store, album, scenes, state)
    stop_event = threading.Event()

    def status() -> str:
        config = config_store.current()
        return playback_status(
            provider.name, config.style, state, scene=director.scene_name(config)
        )

    with contextlib.ExitStack() as stack:
        # Entered before the threads start, so a failure to start one still clears the panel.
        display = stack.enter_context(
            ConfiguredDisplay(build_display(args, status=status), config_store)
        )

        workers = [
            threading.Thread(
                target=poll_provider,
                args=(
                    provider,
                    state,
                    stop_event,
                    args.poll_seconds,
                    build_image_preparer(
                        args, lambda: renderers[config_store.current().style]
                    ),
                ),
                daemon=True,
            ),
            threading.Thread(target=library.run, args=(stop_event,), daemon=True),
        ]
        for worker in workers:
            worker.start()

        def stop_workers() -> None:
            stop_event.set()
            for worker in workers:
                worker.join(timeout=1)

        stack.callback(stop_workers)

        # A bounded render is a one-shot preview, so it gets no control panel and cannot
        # collide with the port of a display already running on this machine.
        if args.web_port and max_frames is None:
            stack.enter_context(
                ControlServer(
                    config_store,
                    status=lambda: control_status(provider.name, state, director, library),
                    host=args.web_host,
                    port=args.web_port,
                )
            )

        if max_frames is not None:
            # A bounded render (--once, --record-gif) is almost always a preview, so let
            # the workers produce something instead of capturing the fallback frame. The
            # live path does not wait: it shows idle immediately and fills in as they land.
            state.wait_for_first_update(timeout=max(2.0, args.poll_seconds * 2))
            if "photos" in director.scene_name(config_store.current()):
                library.wait_for_first_photo(timeout=5.0)

        drive_display(display, director, fps=args.fps, max_frames=max_frames)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spin currently playing album art on a 64x64 RGB matrix.")
    parser.add_argument(
        "--provider",
        choices=tuple(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help=(
            f"Music provider to use for album art (default: {DEFAULT_PROVIDER}). "
            "'demo' needs no credentials."
        ),
    )
    parser.add_argument(
        "--style",
        choices=tuple(RENDERER_STYLES),
        default=None,
        help=(
            "How to draw the album art: 'record' spins it as a vinyl disc, "
            "'art' shows it static and full-bleed. Overrides and updates config.json."
        ),
    )
    parser.add_argument("--rows", type=int, default=64)
    parser.add_argument("--cols", type=int, default=64)
    parser.add_argument("--chain-length", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--brightness", type=int, default=65)
    parser.add_argument("--gpio-slowdown", type=int, default=2)
    parser.add_argument("--hardware-mapping", default="regular")
    parser.add_argument("--pwm-bits", type=int, default=11)
    parser.add_argument("--limit-refresh-rate-hz", type=int, default=120)
    parser.add_argument(
        "--no-hardware-pulse",
        action="store_true",
        help="Avoid Pi onboard sound conflict at the cost of more possible flicker.",
    )
    parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    parser.add_argument("--fps", type=positive_float, default=20.0)
    parser.add_argument("--rpm", type=positive_float, default=None)
    parser.add_argument("--token-cache", type=Path, default=Path(".cache/spotify_token.json"))
    parser.add_argument(
        "--ytmusic-auth-headers",
        type=Path,
        default=Path(os.environ.get("YTMUSIC_AUTH_HEADERS_PATH", ".cache/ytmusic_auth.json")),
        help="Path to ytmusicapi auth headers JSON file for YouTube Music provider.",
    )
    parser.add_argument(
        "--ytmusic-stale-seconds",
        type=positive_float,
        default=600.0,
        help=(
            "How long a YouTube Music history entry keeps spinning after it last changed. "
            "History has no live playing flag, so this bounds how long a finished track spins."
        ),
    )
    parser.add_argument(
        "--auth-timeout-seconds",
        type=positive_float,
        default=180.0,
        help="Maximum time to wait for Spotify OAuth callback before failing.",
    )
    parser.add_argument(
        "--demo-cycle-seconds",
        type=positive_float,
        default=6.0,
        help="How long the demo provider spends in each of playing / paused / idle.",
    )

    # Runtime settings. These seed config.json on first run; after that the file wins, so
    # the web UI and an SSH edit are interchangeable.
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Settings file, re-read whenever it changes. Written on first run.",
    )
    parser.add_argument(
        "--scene",
        choices=("auto", *SCENE_NAMES),
        default=None,
        help=(
            "Show this scene now, whatever is playing. 'auto' follows playback and falls "
            "back to --idle-scene. Use this to look at a scene straight away."
        ),
    )
    parser.add_argument(
        "--idle-scene",
        choices=tuple(name for name in SCENE_NAMES if name != "album"),
        default=None,
        help="What to show when nothing is playing.",
    )
    parser.add_argument(
        "--photos",
        type=Path,
        default=Path("photos"),
        help="Directory of images for the photo scenes. Re-scanned when it changes.",
    )
    parser.add_argument(
        "--photo-seconds", type=positive_float, default=None, help="Seconds per photo."
    )
    parser.add_argument(
        "--clock-24-hour",
        action="store_true",
        default=None,
        help="Show a 24-hour clock instead of 12-hour."
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Port for the control UI. 0 disables it.",
    )
    parser.add_argument(
        "--web-host",
        default="0.0.0.0",
        help="Interface for the control UI. Use 127.0.0.1 to keep it off the LAN.",
    )

    # Output backends. Exactly one destination, so argparse rejects combinations.
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--mock-output",
        type=Path,
        help="Write the current frame as a PNG instead of using RGB matrix hardware.",
    )
    output.add_argument(
        "--preview-terminal",
        action="store_true",
        help="Draw live frames in the terminal using truecolour half-blocks. No hardware needed.",
    )
    output.add_argument(
        "--record-gif",
        type=Path,
        help="Record the animation to a looping GIF and exit. See --record-seconds.",
    )
    parser.add_argument(
        "--record-seconds",
        type=positive_float,
        default=5.0,
        help="How long to record with --record-gif.",
    )
    parser.add_argument(
        "--preview-scale",
        type=positive_int,
        default=None,
        help=(
            "Nearest-neighbour magnification for preview output. "
            "Defaults to 1 for files and to filling the window for --preview-terminal."
        ),
    )
    parser.add_argument(
        "--preview-grid",
        action="store_true",
        help="Draw the inter-pixel gutter in scaled output, approximating the panel behind a diffuser.",
    )
    parser.add_argument("--preview-frames", type=Path, help="Render sample album-art frames and exit.")
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Authorize/verify provider credentials and exit without using the matrix.",
    )
    parser.add_argument("--test-pattern", action="store_true", help="Show a bright moving color test pattern.")
    parser.add_argument("--once", action="store_true", help="Render one frame and exit.")
    parser.add_argument("--no-browser", action="store_true", help="Print Spotify auth URL without trying to open a browser.")
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
