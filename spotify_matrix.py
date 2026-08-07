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
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        parent = self.path.parent
        # Path("token.json").parent is Path("."), and chmod 0o700 on the working directory
        # under sudo would lock the deploy checkout to root only.
        if parent not in (Path("."), Path("")):
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(parent, 0o700)

        temp_path: Path | None = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as token_file:
                temp_path = Path(token_file.name)
                json.dump(token, token_file, indent=2)
                token_file.flush()
                os.fsync(token_file.fileno())

            os.replace(temp_path, self.path)
            replaced = True
            os.chmod(self.path, 0o600)
        finally:
            if not replaced and temp_path and temp_path.exists():
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()


class InMemoryTokenStore:
    """Non-persistent store, for tests and dry runs."""

    def __init__(self, token: dict[str, Any] | None = None) -> None:
        self.token = token

    def load(self) -> dict[str, Any] | None:
        return self.token

    def save(self, token: dict[str, Any]) -> None:
        self.token = token


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

    def _resolve_scale(self, frame_size: tuple[int, int]) -> int:
        if self.scale is not None:
            return self.scale
        size = self.terminal_size or tuple(shutil.get_terminal_size((80, 24)))
        return terminal_scale_for(frame_size, size, self.RESERVED_ROWS)

    def _warn_if_clipped(self, frame_size: tuple[int, int]) -> None:
        columns, rows = self.terminal_size or tuple(shutil.get_terminal_size((80, 24)))
        needed_rows = frame_size[1] // 2 + self.RESERVED_ROWS
        if columns < frame_size[0] or rows < needed_rows:
            # Warn on stderr before the alternate screen swallows it.
            print(
                f"Warning: terminal is {columns}x{rows}; an unscaled {frame_size[0]}x"
                f"{frame_size[1]} frame needs {frame_size[0]}x{needed_rows}. "
                "The preview will be clipped until you resize.",
                file=sys.stderr,
                flush=True,
            )

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
        if not self._started:
            self._warn_if_clipped(image.size)
            if self.alt_screen:
                self.stream.write("\x1b[?1049h")  # alternate screen, preserves scrollback
            self.stream.write("\x1b[2J\x1b[?25l")  # clear, hide cursor
            self._started = True

        self._measure_fps()
        frame = scale_for_preview(image, self._resolve_scale(image.size), self.grid)
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
        self.stream.write("\x1b[0m\x1b[?25h")  # reset colours, show cursor
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
# Frame sources and the display loop
# --------------------------------------------------------------------------------------


def advance_angle(angle: float, rpm: float, delta_seconds: float) -> float:
    return (angle - 360.0 * (rpm / 60.0) * delta_seconds) % 360.0


class RecordFrameSource:
    """Produces the next record frame, advancing the rotation by real elapsed time."""

    def __init__(
        self,
        renderer: FrameRenderer,
        state: PlaybackState,
        rpm: float,
        start_time: float | None = None,
    ) -> None:
        self.renderer = renderer
        self.state = state
        self.rpm = rpm
        self.angle = 0.0
        self._last_frame_at = start_time

    def __call__(self, now: float) -> Image.Image:
        art, is_playing = self.state.snapshot()

        delta = 0.0 if self._last_frame_at is None else now - self._last_frame_at
        self._last_frame_at = now

        if is_playing and art is not None and self.renderer.animated:
            self.angle = advance_angle(self.angle, self.rpm, delta)

        return self.renderer.render(art, self.angle) if art is not None else self.renderer.idle()


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


def build_image_preparer(
    args: argparse.Namespace, renderer: FrameRenderer
) -> Callable[[str], Image.Image]:
    """Return the poll thread's download-and-resize step.

    The resize happens here rather than in the frame loop because it is the expensive
    part. The demo provider short-circuits the download entirely.
    """
    if args.provider == "demo":
        art = demo_album_art(512)
        return lambda url: renderer.fit(art)
    return lambda url: renderer.fit(download_image(url))


def build_provider(args: argparse.Namespace) -> PlaybackProvider:
    spec = PROVIDERS[args.provider]
    env: dict[str, str | None] = {name: os.environ.get(name) for name in spec.required_env}

    missing = missing_env_vars(env)
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    return spec.factory(args, {name: value or "" for name, value in env.items()})


def validate_args(args: argparse.Namespace) -> None:
    if args.chain_length * args.parallel != 1:
        raise SystemExit(
            "--chain-length and --parallel must both be 1. Each frame is rendered as a "
            "single square panel, so a chained or parallel display would leave the rest "
            "of the chain black."
        )


def playback_status(provider_name: str, style: str, state: PlaybackState) -> str:
    """One-line summary of what the display is doing, for the terminal preview."""
    art, is_playing = state.snapshot()
    if art is None:
        playback = "idle"
    else:
        playback = "playing" if is_playing else "paused"

    parts = [provider_name, style, playback]
    # Prefer the human-readable title; the key is often an opaque id (Last.fm mbid,
    # Spotify track id) which tells you nothing about whether the right thing is playing.
    track = state.title or state.art_key
    if track:
        parts.append(track if len(track) <= 40 else track[:39] + "…")
    return " · ".join(parts)


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
            args.preview_frames, style=args.style, scale=args.preview_scale or 1
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

    renderer = build_renderer(args.style, size)
    state = PlaybackState()
    stop_event = threading.Event()

    with contextlib.ExitStack() as stack:
        # Entered before the thread starts, so a failure to start it still clears the panel.
        display = stack.enter_context(
            build_display(args, status=lambda: playback_status(provider.name, args.style, state))
        )

        poll_thread = threading.Thread(
            target=poll_provider,
            args=(
                provider,
                state,
                stop_event,
                args.poll_seconds,
                build_image_preparer(args, renderer),
            ),
            daemon=True,
        )
        poll_thread.start()

        def stop_polling() -> None:
            stop_event.set()
            poll_thread.join(timeout=1)

        stack.callback(stop_polling)

        if max_frames is not None:
            # A bounded render (--once, --record-gif) is almost always a preview, so let
            # the first poll land instead of capturing the idle frame. The live path does
            # not wait: it shows idle immediately and fills in when the poll returns.
            state.wait_for_first_update(timeout=max(2.0, args.poll_seconds * 2))

        drive_display(
            display,
            RecordFrameSource(renderer, state, args.rpm),
            fps=args.fps,
            max_frames=max_frames,
        )


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
        default="spotify",
        help="Music provider to use for album art. 'demo' needs no credentials.",
    )
    parser.add_argument(
        "--style",
        choices=tuple(RENDERER_STYLES),
        default="record",
        help=(
            "How to draw the album art: 'record' spins it as a vinyl disc, "
            "'art' shows it static and full-bleed."
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
    parser.add_argument("--rpm", type=positive_float, default=20.0)
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
