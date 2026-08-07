"""Unit tests for spotify_matrix.

Stdlib unittest only, so the Pi does not need extra packages to run these:

    python -m unittest -v

Nothing here touches the network, the real clock, or matrix hardware.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from email.message import Message
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image, ImageSequence

sys.argv = ["spotify_matrix"]
import spotify_matrix as sm

PLACEHOLDER_URL = (
    "https://lastfm.freetls.fastly.net/i/u/300x300/"
    "2a96cbd8b46e442fc41c2b86b821562f.png"
)


def http_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return sm.HttpResponse(status=status, headers=headers or {}, body=body)


def fake_http(*responses):
    """Return a stub http callable that yields the given responses in order."""
    queue = list(responses)
    calls: list[dict[str, Any]] = []

    def _request(method, url, params=None, headers=None, data=None, timeout=None):
        calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    _request.calls = calls  # type: ignore[attr-defined]
    return _request


@contextlib.contextmanager
def quiet():
    """Swallow argparse usage text and the script's own progress prints."""
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        yield


def png_bytes(colour=(1, 2, 3), size=(8, 8)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def lastfm_payload(images, now_playing=True, mbid=None, name="Teardrop", artist="Massive Attack"):
    track: dict[str, Any] = {"name": name, "artist": {"#text": artist}, "image": images}
    if now_playing:
        track["@attr"] = {"nowplaying": "true"}
    else:
        track["date"] = {"uts": "1600000000"}
    if mbid:
        track["mbid"] = mbid
    return {"recenttracks": {"track": [track]}}


def art_image(colour=(255, 0, 0), size=(300, 300)) -> Image.Image:
    return Image.new("RGB", size, colour)


# ======================================================================================
# Errors and HTTP
# ======================================================================================


class TestErrorTaxonomy(unittest.TestCase):
    def test_hierarchy_lets_callers_choose_their_granularity(self):
        self.assertTrue(issubclass(sm.ProviderAuthError, sm.ProviderError))
        self.assertTrue(issubclass(sm.ProviderUnavailableError, sm.ProviderError))
        self.assertTrue(issubclass(sm.ProviderRateLimitError, sm.ProviderUnavailableError))
        self.assertTrue(issubclass(sm.ProviderError, RuntimeError))

    def test_auth_errors_are_not_treated_as_transient(self):
        self.assertFalse(issubclass(sm.ProviderAuthError, sm.ProviderUnavailableError))

    def test_raise_http_error_classifies_by_status(self):
        cases = [
            (401, sm.ProviderAuthError),
            (403, sm.ProviderAuthError),
            (500, sm.ProviderUnavailableError),
            (503, sm.ProviderUnavailableError),
            (400, sm.ProviderError),
            (404, sm.ProviderError),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                with self.assertRaises(expected):
                    sm.raise_http_error(http_response(b"body", status=status), "ctx")

    def test_raise_http_error_keeps_status_and_body_in_the_message(self):
        with self.assertRaises(sm.ProviderError) as ctx:
            sm.raise_http_error(http_response(b"detail here", status=418), "Teapot request")
        self.assertIn("418", str(ctx.exception))
        self.assertIn("detail here", str(ctx.exception))
        self.assertIn("Teapot request", str(ctx.exception))

    def test_400_is_not_classified_as_transient(self):
        # A malformed request will fail identically forever; retrying it is pointless.
        self.assertNotIsInstance(
            self.capture(400), sm.ProviderUnavailableError
        )

    @staticmethod
    def capture(status):
        try:
            sm.raise_http_error(http_response(b"", status=status), "ctx")
        except sm.ProviderError as exc:
            return exc
        raise AssertionError("expected a ProviderError")


class TestHttpRequestFailureMapping(unittest.TestCase):
    def test_connection_failure_becomes_transient_not_a_bare_oserror(self):
        # The Pi may boot and start before Wi-Fi associates; that must be recoverable.
        import urllib.error

        with mock.patch.object(
            sm.urllib.request, "urlopen", side_effect=urllib.error.URLError("no route to host")
        ):
            with self.assertRaises(sm.ProviderUnavailableError) as ctx:
                sm.http_request("GET", "https://example.invalid/x")
        self.assertIn("no route to host", str(ctx.exception))

    def test_timeout_becomes_transient(self):
        with mock.patch.object(sm.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(sm.ProviderUnavailableError):
                sm.http_request("GET", "https://example.invalid/x")

    def test_http_error_status_is_returned_as_a_response(self):
        import urllib.error

        headers = Message()
        headers["Retry-After"] = "7"
        error = urllib.error.HTTPError("u", 429, "Too Many", headers, io.BytesIO(b"slow down"))
        self.addCleanup(error.close)
        with mock.patch.object(sm.urllib.request, "urlopen", side_effect=error):
            response = sm.http_request("GET", "https://example.invalid/x")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.body, b"slow down")
        self.assertEqual(sm.retry_after_seconds(response), 7)


class TestRetryAfterSeconds(unittest.TestCase):
    def test_parses_valid_header(self):
        self.assertEqual(sm.retry_after_seconds(http_response(b"", headers={"Retry-After": "30"})), 30)

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(sm.retry_after_seconds(http_response(b"", headers={"Retry-After": " 12 "})), 12)

    def test_missing_header_uses_default(self):
        self.assertEqual(sm.retry_after_seconds(http_response(b"")), 5)
        self.assertEqual(sm.retry_after_seconds(http_response(b""), default=9), 9)

    def test_unparseable_header_uses_default(self):
        for raw in ("soon", "", "1.5", "Wed, 21 Oct 2015 07:28:00 GMT"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    sm.retry_after_seconds(http_response(b"", headers={"Retry-After": raw})), 5
                )

    def test_never_returns_less_than_one_second(self):
        for raw in ("0", "-30"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    sm.retry_after_seconds(http_response(b"", headers={"Retry-After": raw})), 1
                )


class TestProviderRateLimitError(unittest.TestCase):
    def test_retry_after_is_clamped_to_at_least_one_second(self):
        for given in (0, -5):
            with self.subTest(given=given):
                self.assertEqual(sm.ProviderRateLimitError("p", given).retry_after_seconds, 1)

    def test_message_mentions_provider_and_delay(self):
        message = str(sm.ProviderRateLimitError("lastfm", 30))
        self.assertIn("lastfm", message)
        self.assertIn("30", message)


# ======================================================================================
# Token stores
# ======================================================================================


class TestFileTokenStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "cache" / "spotify_token.json"
        self.store = sm.FileTokenStore(self.path)

    def leftovers(self):
        return [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]

    def test_missing_file_loads_as_none(self):
        self.assertIsNone(self.store.load())

    def test_round_trip(self):
        self.store.save({"access_token": "at-1"})
        self.assertEqual(self.store.load(), {"access_token": "at-1"})

    def test_save_creates_a_private_parent_directory(self):
        self.store.save({"access_token": "at-1"})
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)

    def test_saved_file_is_private(self):
        self.store.save({"access_token": "at-1"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_save_leaves_no_temp_files(self):
        self.store.save({"access_token": "at-1"})
        self.assertEqual(self.leftovers(), [])

    def test_failed_replace_leaves_original_intact_and_cleans_up(self):
        self.store.save({"access_token": "original"})
        with mock.patch.object(sm.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save({"access_token": "replacement"})
        self.assertEqual(self.store.load(), {"access_token": "original"})
        self.assertEqual(self.leftovers(), [])

    def test_corrupt_json_is_moved_aside_and_reported_as_missing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with quiet():
            self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_name(f"{self.path.name}.corrupt").exists())

    def test_load_survives_a_chmod_failure(self):
        self.store.save({"access_token": "at-1"})
        with mock.patch.object(sm.os, "chmod", side_effect=OSError("read-only fs")), quiet():
            self.assertEqual(self.store.load(), {"access_token": "at-1"})

    def test_relative_bare_filename_does_not_chmod_the_working_directory(self):
        # Path("token.json").parent is Path("."). Under sudo, chmod 0o700 there would lock
        # the deploy checkout to root and break `git pull`.
        # Start from group/other-readable, since TemporaryDirectory already creates 0o700
        # and a no-op chmod would make this test pass either way.
        os.chmod(self.root, 0o755)
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            sm.FileTokenStore(Path("token.json")).save({"access_token": "at-1"})
        finally:
            os.chdir(cwd)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o755)
        self.assertTrue((self.root / "token.json").exists())

    def test_bare_filename_still_writes_a_private_file(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            sm.FileTokenStore(Path("token.json")).save({"access_token": "at-1"})
        finally:
            os.chdir(cwd)
        self.assertEqual(os.stat(self.root / "token.json").st_mode & 0o777, 0o600)


class TestInMemoryTokenStore(unittest.TestCase):
    def test_starts_empty(self):
        self.assertIsNone(sm.InMemoryTokenStore().load())

    def test_round_trip(self):
        store = sm.InMemoryTokenStore()
        store.save({"access_token": "at"})
        self.assertEqual(store.load(), {"access_token": "at"})

    def test_seeded_value(self):
        self.assertEqual(sm.InMemoryTokenStore({"a": 1}).load(), {"a": 1})


# ======================================================================================
# Spotify
# ======================================================================================


class SpotifyFixture(unittest.TestCase):
    """SpotifyClient backed by an in-memory store with a live token, so no OAuth or disk."""

    def build(self, *responses, token=None):
        self.store = sm.InMemoryTokenStore(
            token
            if token is not None
            else {"access_token": "at-1", "refresh_token": "rt-1", "expires_at": time.time() + 3600}
        )
        self.http = fake_http(*responses) if responses else fake_http(http_response({}))
        return sm.SpotifyClient(
            client_id="cid",
            client_secret="secret",
            redirect_uri=sm.DEFAULT_REDIRECT_URI,
            token_store=self.store,
            open_browser=False,
            callback_timeout_seconds=1.0,
            http=self.http,
        )


class TestSpotifyCurrentlyPlaying(SpotifyFixture):
    def test_204_means_nothing_playing(self):
        client = self.build(http_response(b"", status=204))
        self.assertIsNone(client.get_currently_playing())

    def test_200_returns_payload(self):
        client = self.build(http_response({"is_playing": True}))
        self.assertEqual(client.get_currently_playing(), {"is_playing": True})

    def test_401_refreshes_once_then_succeeds(self):
        client = self.build(http_response(b"nope", status=401), http_response({"ok": True}))
        refreshes = []
        with mock.patch.object(client, "_refresh_access_token", side_effect=lambda: refreshes.append(1)):
            self.assertEqual(client.get_currently_playing(), {"ok": True})
        self.assertEqual(len(refreshes), 1, "should refresh exactly once")

    def test_repeated_401_gives_up_as_an_auth_error(self):
        client = self.build(http_response(b"nope", status=401))
        with mock.patch.object(client, "_refresh_access_token"):
            with self.assertRaises(sm.ProviderAuthError) as ctx:
                client.get_currently_playing()
        self.assertIn("401", str(ctx.exception))

    def test_429_raises_rate_limit_with_header_value(self):
        client = self.build(http_response(b"", status=429, headers={"Retry-After": "12"}))
        with self.assertRaises(sm.ProviderRateLimitError) as ctx:
            client.get_currently_playing()
        self.assertEqual(ctx.exception.retry_after_seconds, 12)

    def test_429_garbage_header_falls_back_to_default(self):
        client = self.build(http_response(b"", status=429, headers={"Retry-After": "later"}))
        with self.assertRaises(sm.ProviderRateLimitError) as ctx:
            client.get_currently_playing()
        self.assertEqual(ctx.exception.retry_after_seconds, 5)

    def test_server_error_is_transient(self):
        client = self.build(http_response(b"oops", status=500))
        with self.assertRaises(sm.ProviderUnavailableError):
            client.get_currently_playing()

    def test_get_playback_art_maps_response(self):
        payload = {
            "is_playing": True,
            "item": {
                "type": "track",
                "id": "track-1",
                "album": {"images": [{"url": "big.png", "width": 640}, {"url": "small.png", "width": 64}]},
            },
        }
        client = self.build(http_response(payload))
        self.assertEqual(
            client.get_playback_art(),
            sm.PlaybackArt(key="track-1", image_url="big.png", is_playing=True),
        )

    def test_uses_the_injected_http_callable(self):
        client = self.build(http_response(b"", status=204))
        client.get_currently_playing()
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0]["url"], sm.CURRENTLY_PLAYING_URL)
        self.assertEqual(self.http.calls[0]["headers"]["Authorization"], "Bearer at-1")


class TestSpotifyTokenHandling(SpotifyFixture):
    def test_save_sets_expires_at_from_expires_in(self):
        client = self.build()
        before = time.time()
        client._save_token({"access_token": "at-2", "expires_in": 3600})
        expires_at = self.store.load()["expires_at"]
        self.assertGreater(expires_at, before)
        self.assertLessEqual(expires_at, before + 3600)

    def test_save_preserves_existing_refresh_token(self):
        client = self.build()
        client._save_token({"access_token": "at-2", "expires_in": 3600})
        self.assertEqual(self.store.load()["refresh_token"], "rt-1")

    def test_save_does_not_clobber_a_new_refresh_token(self):
        client = self.build()
        client._save_token({"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})
        self.assertEqual(self.store.load()["refresh_token"], "rt-2")

    def test_expired_token_triggers_a_refresh(self):
        client = self.build(
            http_response({"access_token": "at-new", "expires_in": 3600}),
            token={"access_token": "old", "refresh_token": "rt-1", "expires_at": time.time() - 1},
        )
        self.assertEqual(client._valid_access_token(), "at-new")

    def test_revoked_refresh_token_is_a_fatal_auth_error(self):
        client = self.build(
            http_response({"error": "invalid_grant"}, status=400),
            token={"access_token": "old", "refresh_token": "dead", "expires_at": time.time() - 1},
        )
        with self.assertRaises(sm.ProviderAuthError) as ctx:
            client.authorize()
        self.assertIn("--auth-only", str(ctx.exception))

    def test_token_endpoint_5xx_is_transient(self):
        client = self.build(
            http_response(b"bad gateway", status=502),
            token={"access_token": "old", "refresh_token": "rt-1", "expires_at": time.time() - 1},
        )
        with self.assertRaises(sm.ProviderUnavailableError):
            client.authorize()

    def test_non_localhost_redirect_is_rejected(self):
        client = sm.SpotifyClient(
            client_id="c",
            client_secret="s",
            redirect_uri="https://example.com/callback",
            token_store=sm.InMemoryTokenStore(),
            open_browser=False,
            callback_timeout_seconds=1.0,
            http=fake_http(http_response({})),
        )
        with self.assertRaises(sm.ProviderAuthError):
            client.authorize()


# ======================================================================================
# Last.fm
# ======================================================================================


class TestSelectLastFmImageUrl(unittest.TestCase):
    def test_prefers_largest_ranked_size(self):
        images = [
            {"size": "small", "#text": "s.png"},
            {"size": "mega", "#text": "mega.png"},
            {"size": "large", "#text": "l.png"},
        ]
        self.assertEqual(sm.select_lastfm_image_url(images), "mega.png")

    def test_skips_blank_urls(self):
        images = [{"size": "mega", "#text": "   "}, {"size": "small", "#text": "s.png"}]
        self.assertEqual(sm.select_lastfm_image_url(images), "s.png")

    def test_unknown_size_does_not_beat_known_size(self):
        images = [{"size": "extralarge", "#text": "xl.png"}, {"size": "bogus", "#text": "junk.png"}]
        self.assertEqual(sm.select_lastfm_image_url(images), "xl.png")

    def test_unknown_size_used_when_nothing_else_available(self):
        self.assertEqual(
            sm.select_lastfm_image_url([{"size": "bogus", "#text": "junk.png"}]), "junk.png"
        )

    def test_placeholder_is_ignored(self):
        self.assertIsNone(sm.select_lastfm_image_url([{"size": "extralarge", "#text": PLACEHOLDER_URL}]))

    def test_real_art_wins_over_placeholder_even_when_larger(self):
        images = [{"size": "mega", "#text": PLACEHOLDER_URL}, {"size": "small", "#text": "real.png"}]
        self.assertEqual(sm.select_lastfm_image_url(images), "real.png")

    def test_empty_list(self):
        self.assertIsNone(sm.select_lastfm_image_url([]))


class TestLastFmClient(unittest.TestCase):
    def client(self, payload, status=200, headers=None):
        self.http = fake_http(http_response(payload, status, headers))
        return sm.LastFmClient("key", "user", http=self.http)

    def test_now_playing_track(self):
        client = self.client(lastfm_payload([{"size": "extralarge", "#text": "xl.png"}], mbid="mb-1"))
        self.assertEqual(
            client.get_playback_art(),
            sm.PlaybackArt(key="mb-1", image_url="xl.png", is_playing=True),
        )

    def test_key_falls_back_to_artist_and_title(self):
        client = self.client(lastfm_payload([{"size": "large", "#text": "l.png"}]))
        self.assertEqual(client.get_playback_art().key, "Massive Attack - Teardrop")

    def test_historical_scrobble_renders_idle(self):
        client = self.client(lastfm_payload([{"size": "large", "#text": "l.png"}], now_playing=False))
        self.assertIsNone(client.get_playback_art())

    def test_single_track_object_instead_of_list(self):
        client = self.client(
            {
                "recenttracks": {
                    "track": {
                        "@attr": {"nowplaying": "true"},
                        "name": "Solo",
                        "artist": {"#text": "Artist"},
                        "image": [{"size": "mega", "#text": "m.png"}],
                    }
                }
            }
        )
        self.assertEqual(client.get_playback_art().image_url, "m.png")

    def test_empty_track_list(self):
        self.assertIsNone(self.client({"recenttracks": {"track": []}}).get_playback_art())

    def test_missing_recenttracks_key(self):
        self.assertIsNone(self.client({}).get_playback_art())

    def test_track_without_art(self):
        self.assertIsNone(self.client(lastfm_payload([])).get_playback_art())

    def test_placeholder_art_renders_idle(self):
        client = self.client(lastfm_payload([{"size": "extralarge", "#text": PLACEHOLDER_URL}]))
        self.assertIsNone(client.get_playback_art())

    def test_auth_error_codes_are_fatal_and_name_the_env_vars(self):
        for code in sorted(sm.LASTFM_AUTH_ERROR_CODES):
            with self.subTest(code=code):
                client = self.client({"error": code, "message": "nope"})
                with self.assertRaises(sm.ProviderAuthError) as ctx:
                    client.get_playback_art()
                self.assertIn("LASTFM_API_KEY", str(ctx.exception))

    def test_transient_error_codes_are_retryable(self):
        for code in sorted(sm.LASTFM_TRANSIENT_ERROR_CODES):
            with self.subTest(code=code):
                client = self.client({"error": code, "message": "service offline"})
                with self.assertRaises(sm.ProviderUnavailableError) as ctx:
                    client.get_playback_art()
                self.assertIn("service offline", str(ctx.exception))
                self.assertNotIn("LASTFM_API_KEY", str(ctx.exception))

    def test_payload_rate_limit_code_becomes_a_rate_limit_error(self):
        client = self.client({"error": sm.LASTFM_RATE_LIMIT_ERROR_CODE, "message": "too many"})
        with self.assertRaises(sm.ProviderRateLimitError):
            client.get_playback_art()

    def test_unknown_error_code_is_neither_fatal_auth_nor_transient(self):
        client = self.client({"error": 99, "message": "mystery"})
        with self.assertRaises(sm.ProviderError) as ctx:
            client.get_playback_art()
        self.assertNotIsInstance(ctx.exception, sm.ProviderAuthError)
        self.assertNotIsInstance(ctx.exception, sm.ProviderUnavailableError)

    def test_http_429_uses_retry_after(self):
        client = self.client({}, status=429, headers={"Retry-After": "30"})
        with self.assertRaises(sm.ProviderRateLimitError) as ctx:
            client.get_playback_art()
        self.assertEqual(ctx.exception.retry_after_seconds, 30)
        self.assertEqual(ctx.exception.provider_name, "lastfm")

    def test_http_server_error_is_transient(self):
        with self.assertRaises(sm.ProviderUnavailableError):
            self.client(b"boom", status=503).get_playback_art()

    def test_authorize_validates_credentials(self):
        client = self.client({"error": 10, "message": "Invalid API key"})
        with self.assertRaises(sm.ProviderAuthError):
            client.authorize()

    def test_uses_the_injected_http_callable(self):
        client = self.client(lastfm_payload([{"size": "large", "#text": "l.png"}]))
        client.get_playback_art()
        self.assertEqual(self.http.calls[0]["url"], sm.LASTFM_API_URL)
        self.assertEqual(self.http.calls[0]["params"]["user"], "user")


# ======================================================================================
# YouTube Music
# ======================================================================================


class FakeYTMusic:
    def __init__(self, history):
        self.history = history
        self.calls = 0

    def get_history(self):
        self.calls += 1
        return self.history


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestYouTubeMusicClient(unittest.TestCase):
    def build(self, history, stale_after_seconds=600.0):
        clock = FakeClock()
        client = sm.YouTubeMusicClient(
            auth_headers_path=Path("/nonexistent/auth.json"),
            stale_after_seconds=stale_after_seconds,
            clock=clock,
        )
        client._client = FakeYTMusic(history)
        return client, clock

    @staticmethod
    def entry(video_id="vid-1", thumbs=None, title="Song"):
        return {
            "videoId": video_id,
            "title": title,
            "thumbnails": thumbs if thumbs is not None else [{"url": "art.png", "width": 544, "height": 544}],
        }

    def test_empty_history(self):
        client, _ = self.build([])
        self.assertIsNone(client.get_playback_art())

    def test_entry_without_thumbnails(self):
        client, _ = self.build([self.entry(thumbs=[])])
        self.assertIsNone(client.get_playback_art())

    def test_thumbnail_without_url(self):
        client, _ = self.build([self.entry(thumbs=[{"width": 100, "height": 100}])])
        self.assertIsNone(client.get_playback_art())

    def test_picks_largest_thumbnail_by_area(self):
        client, _ = self.build([
            self.entry(thumbs=[
                {"url": "small.png", "width": 60, "height": 60},
                {"url": "big.png", "width": 544, "height": 544},
                {"url": "mid.png", "width": 120, "height": 120},
            ])
        ])
        self.assertEqual(client.get_playback_art().image_url, "big.png")

    def test_thumbnails_with_missing_dimensions_do_not_crash(self):
        client, _ = self.build([
            self.entry(thumbs=[{"url": "unknown.png"}, {"url": "known.png", "width": 10, "height": 10}])
        ])
        self.assertEqual(client.get_playback_art().image_url, "known.png")

    def test_key_prefers_video_id(self):
        client, _ = self.build([self.entry(video_id="abc")])
        self.assertEqual(client.get_playback_art().key, "abc")

    def test_key_falls_back_to_title_then_url(self):
        client, _ = self.build([{"title": "Only Title", "thumbnails": [{"url": "a.png", "width": 1, "height": 1}]}])
        self.assertEqual(client.get_playback_art().key, "Only Title")

        client2, _ = self.build([{"thumbnails": [{"url": "a.png", "width": 1, "height": 1}]}])
        self.assertEqual(client2.get_playback_art().key, "a.png")

    def test_first_observation_counts_as_playing(self):
        client, _ = self.build([self.entry()])
        self.assertTrue(client.get_playback_art().is_playing)

    def test_still_playing_inside_stale_window(self):
        client, clock = self.build([self.entry()], stale_after_seconds=600.0)
        client.get_playback_art()
        clock.advance(599)
        self.assertTrue(client.get_playback_art().is_playing)

    def test_goes_idle_once_entry_is_stale(self):
        client, clock = self.build([self.entry()], stale_after_seconds=600.0)
        client.get_playback_art()
        clock.advance(601)
        self.assertFalse(client.get_playback_art().is_playing)

    def test_boundary_is_inclusive(self):
        client, clock = self.build([self.entry()], stale_after_seconds=600.0)
        client.get_playback_art()
        clock.advance(600)
        self.assertTrue(client.get_playback_art().is_playing)

    def test_new_track_resets_the_window(self):
        client, clock = self.build([self.entry(video_id="first")], stale_after_seconds=600.0)
        client.get_playback_art()
        clock.advance(1200)
        self.assertFalse(client.get_playback_art().is_playing)

        client._client.history = [self.entry(video_id="second")]
        self.assertTrue(client.get_playback_art().is_playing, "a new track should spin again")

    def test_missing_auth_file_is_a_fatal_auth_error(self):
        client = sm.YouTubeMusicClient(auth_headers_path=Path("/nonexistent/auth.json"))
        with self.assertRaises(sm.ProviderAuthError) as ctx:
            client.authorize()
        self.assertIn("YTMUSIC_AUTH_HEADERS_PATH", str(ctx.exception))


# ======================================================================================
# Spotify response mapping
# ======================================================================================


class TestPlaybackArtFromResponse(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(sm.playback_art_from_response(None))
        self.assertIsNone(sm.playback_art_from_response({}))
        self.assertIsNone(sm.playback_art_from_response({"item": None}))

    def test_track_uses_album_images(self):
        art = sm.playback_art_from_response({
            "is_playing": True,
            "item": {
                "type": "track",
                "id": "t1",
                "album": {"images": [{"url": "a.png", "width": 64}, {"url": "b.png", "width": 640}]},
            },
        })
        self.assertEqual(art.image_url, "b.png")
        self.assertTrue(art.is_playing)

    def test_episode_uses_item_images(self):
        art = sm.playback_art_from_response({
            "is_playing": False,
            "item": {"type": "episode", "id": "e1", "images": [{"url": "ep.png", "width": 300}]},
        })
        self.assertEqual(art, sm.PlaybackArt(key="e1", image_url="ep.png", is_playing=False))

    def test_no_images(self):
        self.assertIsNone(
            sm.playback_art_from_response({"item": {"type": "track", "id": "t", "album": {"images": []}}})
        )

    def test_key_falls_back_to_uri(self):
        art = sm.playback_art_from_response({
            "item": {"type": "track", "uri": "spotify:track:x", "album": {"images": [{"url": "a.png", "width": 1}]}},
        })
        self.assertEqual(art.key, "spotify:track:x")


# ======================================================================================
# Image download
# ======================================================================================


class TestDownloadImage(unittest.TestCase):
    def test_decodes_a_png_via_the_injected_http_callable(self):
        http = fake_http(http_response(png_bytes((10, 20, 30), (12, 12))))
        image = sm.download_image("https://art.example/a.png", http=http)
        self.assertEqual(image.size, (12, 12))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.getpixel((0, 0)), (10, 20, 30))
        self.assertEqual(http.calls[0]["url"], "https://art.example/a.png")

    def test_missing_art_raises_with_the_url_in_the_message(self):
        http = fake_http(http_response(b"not found", status=404))
        with self.assertRaises(sm.ProviderError) as ctx:
            sm.download_image("https://art.example/gone.png", http=http)
        self.assertIn("gone.png", str(ctx.exception))

    def test_server_error_is_transient(self):
        http = fake_http(http_response(b"", status=502))
        with self.assertRaises(sm.ProviderUnavailableError):
            sm.download_image("https://art.example/a.png", http=http)

    def test_does_not_import_requests(self):
        # requests was a five-package dependency tree for a single GET; http_request
        # already does the job with stdlib urllib.
        self.assertNotIn("requests", (Path(sm.__file__).read_text()))


# ======================================================================================
# Rendering
# ======================================================================================


class TestDiscGeometry(unittest.TestCase):
    def test_margin_and_disc_size(self):
        self.assertEqual(sm.disc_geometry(64), (2, 60))
        self.assertEqual(sm.disc_geometry(32), (2, 28))
        self.assertEqual(sm.disc_geometry(128), (4, 120))

    def test_margin_never_below_two_pixels(self):
        self.assertEqual(sm.disc_geometry(16)[0], 2)


class TestFitArtToDisc(unittest.TestCase):
    def test_produces_exactly_the_disc_size(self):
        self.assertEqual(sm.fit_art_to_disc(art_image(size=(640, 640)), 60).size, (60, 60))

    def test_crops_non_square_art_rather_than_distorting_it(self):
        wide = Image.new("RGB", (200, 100), (0, 0, 0))
        wide.paste((255, 0, 0), (0, 0, 200, 50))
        fitted = sm.fit_art_to_disc(wide, 60)
        self.assertEqual(fitted.size, (60, 60))


class TestDiscMask(unittest.TestCase):
    def test_centre_is_opaque_and_corners_are_transparent(self):
        mask = sm.build_disc_mask(60)
        self.assertEqual(mask.mode, "L")
        self.assertEqual(mask.getpixel((30, 30)), 255)
        self.assertEqual(mask.getpixel((0, 0)), 0)
        self.assertEqual(mask.getpixel((59, 59)), 0)


class TestRecordRenderer(unittest.TestCase):
    SIZE = 64
    ART_PIXEL = (16, 32)  # inside the disc, clear of the centre label and spindle

    def setUp(self):
        self.renderer = sm.RecordRenderer(self.SIZE)

    def test_reuses_its_mask_and_idle_frame(self):
        self.assertIs(self.renderer.idle(), self.renderer.idle())
        first = self.renderer._mask
        self.renderer.render(self.renderer.fit(art_image()), 0.0)
        self.assertIs(self.renderer._mask, first)

    def test_renders_the_album_colour_onto_the_disc(self):
        frame = self.renderer.render(self.renderer.fit(art_image((255, 0, 0))), 0.0)
        self.assertEqual(frame.getpixel(self.ART_PIXEL), (255, 0, 0))
        self.assertEqual(frame.mode, "RGB")
        self.assertEqual(frame.size, (self.SIZE, self.SIZE))

    def test_switching_album_switches_what_is_drawn(self):
        red = self.renderer.render(self.renderer.fit(art_image((255, 0, 0))), 0.0)
        blue = self.renderer.render(self.renderer.fit(art_image((0, 0, 255))), 0.0)
        self.assertEqual(red.getpixel(self.ART_PIXEL), (255, 0, 0))
        self.assertEqual(blue.getpixel(self.ART_PIXEL), (0, 0, 255))

    def test_rotation_changes_the_frame(self):
        art = Image.new("RGB", (300, 300), (0, 0, 0))
        art.paste((255, 0, 0), (0, 0, 300, 150))
        fitted = self.renderer.fit(art)
        self.assertNotEqual(
            self.renderer.render(fitted, 0.0).tobytes(),
            self.renderer.render(fitted, 90.0).tobytes(),
        )

    def test_tolerates_unfitted_art(self):
        # Previews and ad-hoc calls should not have to pre-fit.
        direct = self.renderer.render(art_image((12, 200, 90), (640, 640)), 33.0)
        prefit = self.renderer.render(self.renderer.fit(art_image((12, 200, 90), (640, 640))), 33.0)
        self.assertEqual(direct.tobytes(), prefit.tobytes())

    def test_spindle_hole_is_black(self):
        frame = self.renderer.render(self.renderer.fit(art_image((255, 255, 255))), 0.0)
        self.assertEqual(frame.getpixel((self.SIZE // 2, self.SIZE // 2)), (0, 0, 0))

    def test_idle_frame_has_no_album_art(self):
        self.assertEqual(self.renderer.idle().getpixel(self.ART_PIXEL), (0, 0, 0))

    def test_fit_matches_the_renderers_disc_size(self):
        self.assertEqual(self.renderer.fit(art_image()).size, (self.renderer.disc_size,) * 2)


class TestRenderPreviewFrames(unittest.TestCase):
    def test_writes_four_distinct_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview"
            sm.render_preview_frames(out)
            files = sorted(out.iterdir())
            self.assertEqual([f.name for f in files], [f"album-disk-{i:02d}.png" for i in range(4)])
            frames = [Image.open(f).tobytes() for f in files]
            self.assertEqual(len(set(frames)), 4, "each preview angle should differ")


class TestRenderTestPattern(unittest.TestCase):
    def test_offset_shifts_the_stripes(self):
        self.assertNotEqual(
            sm.render_test_pattern(64, 0).tobytes(), sm.render_test_pattern(64, 5).tobytes()
        )

    def test_is_bright_enough_to_confirm_wiring(self):
        # Every channel must reach full scale, so a dead colour channel is visible.
        frame = sm.render_test_pattern(64, 0)
        self.assertEqual([high for _, high in frame.getextrema()], [255, 255, 255])


# ======================================================================================
# Playback state
# ======================================================================================


class TestPlaybackState(unittest.TestCase):
    def test_starts_empty(self):
        state = sm.PlaybackState()
        self.assertIsNone(state.art_key)
        self.assertIsNone(state.image_url)
        self.assertIsNone(state.image)
        self.assertFalse(state.is_playing)
        self.assertEqual(state.snapshot(), (None, False))

    def test_update_sets_everything(self):
        state = sm.PlaybackState()
        image = art_image()
        state.update(sm.PlaybackArt("k", "u.png", True), image)
        self.assertEqual(state.art_key, "k")
        self.assertEqual(state.image_url, "u.png")
        self.assertIs(state.image, image)
        self.assertTrue(state.is_playing)

    def test_update_with_no_image_keeps_the_current_one(self):
        state = sm.PlaybackState()
        image = art_image()
        state.update(sm.PlaybackArt("k", "u.png", True), image)
        state.update(sm.PlaybackArt("k", "u.png", False), None)
        self.assertIs(state.image, image, "a pause must not drop the art")
        self.assertFalse(state.is_playing)

    def test_needs_download_only_when_key_or_url_changes(self):
        state = sm.PlaybackState()
        art = sm.PlaybackArt("k", "u.png", True)
        self.assertTrue(state.needs_download(art))
        state.update(art, art_image())
        self.assertFalse(state.needs_download(art))
        self.assertTrue(state.needs_download(sm.PlaybackArt("k2", "u.png", True)))
        self.assertTrue(state.needs_download(sm.PlaybackArt("k", "u2.png", True)))

    def test_clear_resets_everything(self):
        state = sm.PlaybackState(art_key="k", image_url="u", image=art_image(), is_playing=True)
        state.clear()
        self.assertEqual(state.snapshot(), (None, False))
        self.assertIsNone(state.art_key)
        self.assertIsNone(state.image_url)

    def test_snapshot_never_mixes_two_updates(self):
        """A frame must never pair one track's art with another's playing flag."""
        state = sm.PlaybackState()
        red, blue = art_image((255, 0, 0)), art_image((0, 0, 255))
        stop = threading.Event()

        def writer():
            playing = True
            while not stop.is_set():
                state.update(sm.PlaybackArt("r", "r.png", playing), red if playing else blue)
                playing = not playing

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(4000):
                image, is_playing = state.snapshot()
                expected = red if is_playing else blue
                self.assertIs(image, expected)
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_owns_its_lock(self):
        self.assertIsInstance(sm.PlaybackState()._lock, type(threading.Lock()))

    def test_snapshot_reads_both_values_in_one_lock_acquisition(self):
        """Deterministic companion to the stress test above.

        Two separately-locked reads would pass the stress test almost always, but leave a
        window where a frame pairs one track's art with another's playing flag.
        """
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True), art_image())

        class CountingLock:
            def __init__(self, inner):
                self._inner = inner
                self.acquisitions = 0

            def __enter__(self):
                self.acquisitions += 1
                return self._inner.__enter__()

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        counting = CountingLock(threading.Lock())
        state._lock = counting
        state.snapshot()
        self.assertEqual(counting.acquisitions, 1)


# ======================================================================================
# Polling
# ======================================================================================


class ScriptedProvider:
    """Provider that plays back a script of results, stopping the poll loop as it goes."""

    name = "scripted"

    def __init__(self, results, stop_event):
        self.results = list(results)
        self.stop_event = stop_event
        self.calls = 0

    def authorize(self):
        pass

    def get_playback_art(self):
        self.calls += 1
        result = self.results.pop(0)
        if not self.results:
            # Stop after the last scripted result so the loop terminates promptly.
            self.stop_event.set()
        if isinstance(result, Exception):
            raise result
        return result


class TestPollProvider(unittest.TestCase):
    def run_poll(self, results, state=None, prepare=None):
        state = state or sm.PlaybackState()
        stop_event = threading.Event()
        provider = ScriptedProvider(results, stop_event)
        prepared: list[str] = []

        def default_prepare(url):
            prepared.append(url)
            return art_image((1, 2, 3), (10, 10))

        with quiet():
            sm.poll_provider(provider, state, stop_event, 0.0, prepare or default_prepare)
        return state, provider, prepared

    def test_art_updates_shared_state(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, _, prepared = self.run_poll([art])
        self.assertEqual(state.art_key, "k1")
        self.assertEqual(state.image_url, "u1.png")
        self.assertTrue(state.is_playing)
        self.assertIsNotNone(state.image)
        self.assertEqual(prepared, ["u1.png"])

    def test_no_art_clears_shared_state(self):
        seeded = sm.PlaybackState(art_key="old", image_url="old.png", image=art_image(), is_playing=True)
        state, _, _ = self.run_poll([None], state=seeded)
        self.assertEqual(state.snapshot(), (None, False))
        self.assertIsNone(state.art_key)

    def test_unchanged_track_is_not_reprepared(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, _, prepared = self.run_poll([art, art])
        self.assertEqual(prepared, ["u1.png"], "same key and url should reuse the fitted image")
        self.assertIsNotNone(state.image)

    def test_pause_updates_flag_without_repreparing(self):
        playing = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        paused = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=False)
        state, _, prepared = self.run_poll([playing, paused])
        self.assertEqual(len(prepared), 1)
        self.assertFalse(state.is_playing)
        self.assertIsNotNone(state.image, "paused art must stay on screen")

    def test_rate_limit_alone_does_not_blank_the_display(self):
        existing = art_image((9, 9, 9))
        seeded = sm.PlaybackState(art_key="k1", image_url="u1.png", image=existing, is_playing=True)
        state, _, _ = self.run_poll([sm.ProviderRateLimitError("scripted", 1)], state=seeded)
        self.assertEqual(state.art_key, "k1")
        self.assertIs(state.image, existing)
        self.assertTrue(state.is_playing)

    def test_transient_failure_does_not_blank_the_display(self):
        existing = art_image((9, 9, 9))
        seeded = sm.PlaybackState(art_key="k1", image_url="u1.png", image=existing, is_playing=True)
        state, provider, _ = self.run_poll(
            [sm.ProviderUnavailableError("network down"), sm.ProviderUnavailableError("still down")],
            state=seeded,
        )
        self.assertEqual(provider.calls, 2)
        self.assertIs(state.image, existing)

    def test_transient_exception_does_not_kill_the_loop(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, provider, _ = self.run_poll([RuntimeError("network down"), art])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(state.art_key, "k1")

    def test_prepare_failure_does_not_kill_the_loop(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)

        def flaky(url):
            raise sm.ProviderUnavailableError("404")

        _, provider, _ = self.run_poll([art, art], prepare=flaky)
        self.assertEqual(provider.calls, 2)

    def test_repeated_failures_are_logged_once(self):
        """A sustained outage must not spam the journal at the poll rate."""
        errors = [sm.ProviderUnavailableError("network down") for _ in range(5)]
        stop_event = threading.Event()
        provider = ScriptedProvider(errors, stop_event)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sm.poll_provider(provider, sm.PlaybackState(), stop_event, 0.0, lambda url: art_image())
        self.assertEqual(provider.calls, 5)
        self.assertEqual(buffer.getvalue().count("network down"), 1)

    def test_status_is_reported_again_after_it_changes_back(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        stop_event = threading.Event()
        provider = ScriptedProvider([art, None, art], stop_event)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sm.poll_provider(provider, sm.PlaybackState(), stop_event, 0.0, lambda url: art_image())
        self.assertEqual(buffer.getvalue().count("art found"), 2)
        self.assertEqual(buffer.getvalue().count("no playback item"), 1)

    def test_stops_promptly_when_the_stop_event_is_already_set(self):
        stop_event = threading.Event()
        stop_event.set()
        provider = ScriptedProvider([None], stop_event)
        sm.poll_provider(provider, sm.PlaybackState(), stop_event, 999.0, lambda url: art_image())
        self.assertEqual(provider.calls, 0)


# ======================================================================================
# Frame sources and the display loop
# ======================================================================================


class TestAdvanceAngle(unittest.TestCase):
    def test_rotates_clockwise(self):
        # 20 rpm for 3 s is one full turn; a third of that is 120 degrees, negated.
        self.assertAlmostEqual(sm.advance_angle(0.0, 20.0, 1.0), 240.0)

    def test_zero_delta_is_a_no_op(self):
        self.assertEqual(sm.advance_angle(37.0, 20.0, 0.0), 37.0)

    def test_wraps_into_zero_to_360(self):
        for delta in (0.1, 1.0, 3.0, 10.0, 100.0):
            with self.subTest(delta=delta):
                angle = sm.advance_angle(0.0, 33.0, delta)
                self.assertGreaterEqual(angle, 0.0)
                self.assertLess(angle, 360.0)

    def test_full_revolution_returns_to_the_start(self):
        self.assertAlmostEqual(sm.advance_angle(0.0, 20.0, 3.0) % 360.0, 0.0, places=6)


class TestRecordFrameSource(unittest.TestCase):
    def setUp(self):
        self.renderer = sm.RecordRenderer(64)
        self.state = sm.PlaybackState()

    def source(self, rpm=20.0):
        return sm.RecordFrameSource(self.renderer, self.state, rpm, start_time=0.0)

    def test_shows_idle_when_there_is_no_art(self):
        self.assertIs(self.source()(1.0), self.renderer.idle())

    def test_first_frame_does_not_jump_the_angle(self):
        self.state.update(sm.PlaybackArt("k", "u", True), self.renderer.fit(art_image()))
        source = sm.RecordFrameSource(self.renderer, self.state, 20.0)
        # 7.3 is deliberately not a whole number of revolutions at 20 rpm: treating the
        # monotonic clock's absolute value as elapsed time would land on 156 degrees.
        source(7.3)
        self.assertEqual(source.angle, 0.0, "elapsed time before the first frame is not real")

    def test_angle_advances_while_playing(self):
        self.state.update(sm.PlaybackArt("k", "u", True), self.renderer.fit(art_image()))
        source = self.source()
        source(1.0)
        self.assertAlmostEqual(source.angle, 240.0)

    def test_angle_frozen_while_paused(self):
        self.state.update(sm.PlaybackArt("k", "u", False), self.renderer.fit(art_image()))
        source = self.source()
        source(1.0)
        source(2.0)
        self.assertEqual(source.angle, 0.0)

    def test_paused_art_still_renders(self):
        self.state.update(sm.PlaybackArt("k", "u", False), self.renderer.fit(art_image((255, 0, 0))))
        frame = self.source()(1.0)
        self.assertEqual(frame.getpixel((16, 32)), (255, 0, 0))

    def test_returns_to_idle_when_art_is_cleared(self):
        self.state.update(sm.PlaybackArt("k", "u", True), self.renderer.fit(art_image()))
        source = self.source()
        source(1.0)
        self.state.clear()
        self.assertIs(source(2.0), self.renderer.idle())


class TestTestPatternFrameSource(unittest.TestCase):
    def test_offset_advances_and_wraps(self):
        source = sm.TestPatternFrameSource(8)
        for expected in range(1, 9):
            source(0.0)
            self.assertEqual(source.offset, expected % 8)

    def test_produces_moving_frames(self):
        source = sm.TestPatternFrameSource(64)
        self.assertNotEqual(source(0.0).tobytes(), source(0.0).tobytes())


class RecordingDisplay:
    def __init__(self):
        self.frames = []
        self.cleared = 0

    def show(self, image):
        self.frames.append(image)

    def clear(self):
        self.cleared += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.clear()


class TestDriveDisplay(unittest.TestCase):
    def test_once_shows_exactly_one_frame(self):
        display = RecordingDisplay()
        sm.drive_display(
            display, lambda now: art_image(), fps=20.0, max_frames=1, sleep=self.fail_on_sleep
        )
        self.assertEqual(len(display.frames), 1)

    @staticmethod
    def fail_on_sleep(seconds):
        raise AssertionError("--once must not sleep")

    def test_does_not_clear_the_display_itself(self):
        # Cleanup belongs to the context manager, so it also runs on an early failure.
        display = RecordingDisplay()
        sm.drive_display(display, lambda now: art_image(), fps=20.0, max_frames=1)
        self.assertEqual(display.cleared, 0)

    def test_paces_to_the_frame_budget(self):
        display = RecordingDisplay()
        clock = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0])
        slept: list[float] = []
        frames = 0

        def next_frame(now):
            nonlocal frames
            frames += 1
            if frames == 3:
                raise KeyboardInterrupt
            return art_image()

        sm.drive_display(
            display,
            next_frame,
            fps=4.0,
            max_frames=None,
            monotonic=lambda: next(clock),
            sleep=slept.append,
        )
        # 4 fps is a 0.25 s budget; the fake clock reports 0 s elapsed for the render.
        self.assertEqual(slept, [0.25, 0.25])

    def test_slow_frame_does_not_sleep_a_negative_duration(self):
        display = RecordingDisplay()
        clock = iter([0.0, 5.0, 5.0, 6.0])
        slept: list[float] = []
        frames = 0

        def next_frame(now):
            nonlocal frames
            frames += 1
            if frames == 2:
                raise KeyboardInterrupt
            return art_image()

        sm.drive_display(
            display, next_frame, fps=20.0, max_frames=None,
            monotonic=lambda: next(clock), sleep=slept.append,
        )
        self.assertEqual(slept, [0.0])

    def test_keyboard_interrupt_is_swallowed(self):
        def next_frame(now):
            raise KeyboardInterrupt

        sm.drive_display(RecordingDisplay(), next_frame, fps=20.0, max_frames=None)

    def test_passes_the_frame_start_time_to_the_source(self):
        seen: list[float] = []
        sm.drive_display(
            RecordingDisplay(),
            lambda now: (seen.append(now), art_image())[1],
            fps=20.0,
            max_frames=1,
            monotonic=lambda: 42.5,
        )
        self.assertEqual(seen, [42.5])


# ======================================================================================
# Displays
# ======================================================================================


class TestMockDisplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "frames" / "frame.png"

    def test_creates_the_parent_directory(self):
        sm.MockDisplay(self.output)
        self.assertTrue(self.output.parent.is_dir())

    def test_writes_a_readable_png(self):
        sm.MockDisplay(self.output).show(art_image((7, 8, 9), (16, 16)))
        with Image.open(self.output) as written:
            self.assertEqual(written.size, (16, 16))
            self.assertEqual(written.convert("RGB").getpixel((0, 0)), (7, 8, 9))

    def test_repeated_frames_leave_no_temp_files(self):
        display = sm.MockDisplay(self.output)
        for _ in range(5):
            display.show(art_image(size=(8, 8)))
        self.assertEqual([p.name for p in self.output.parent.iterdir()], [self.output.name])

    def test_replaces_atomically_so_readers_never_see_a_partial_file(self):
        display = sm.MockDisplay(self.output)
        display.show(art_image((1, 1, 1), (8, 8)))
        seen_sizes = set()
        real_replace = os.replace  # sm.os is the os module, so the patch below is global

        def check_before_replace(src, dst):
            # While the new frame is still in the temp file, the target must remain the
            # complete previous frame.
            with Image.open(dst) as current:
                seen_sizes.add(current.size)
            return real_replace(src, dst)

        with mock.patch.object(sm.os, "replace", side_effect=check_before_replace):
            display.show(art_image((2, 2, 2), (32, 32)))
        self.assertEqual(seen_sizes, {(8, 8)})

    def test_context_manager_clears(self):
        with sm.MockDisplay(self.output) as display:
            display.show(art_image(size=(8, 8)))


class TestMatrixOptions(unittest.TestCase):
    def test_maps_every_cli_flag_the_driver_needs(self):
        args = sm.build_parser().parse_args([
            "--rows", "32", "--cols", "16", "--brightness", "50",
            "--gpio-slowdown", "4", "--hardware-mapping", "adafruit-hat-pwm",
            "--pwm-bits", "8", "--limit-refresh-rate-hz", "90", "--no-hardware-pulse",
        ])
        self.assertEqual(sm.matrix_options_from(args), {
            "rows": 32,
            "cols": 16,
            "chain_length": 1,
            "parallel": 1,
            "brightness": 50,
            "gpio_slowdown": 4,
            "hardware_mapping": "adafruit-hat-pwm",
            "pwm_bits": 8,
            "limit_refresh_rate_hz": 90,
            "disable_hardware_pulsing": True,
        })

    def test_matches_the_matrix_display_signature(self):
        import inspect

        expected = {
            name
            for name, param in inspect.signature(sm.MatrixDisplay.__init__).parameters.items()
            if param.kind is inspect.Parameter.KEYWORD_ONLY
        }
        args = sm.build_parser().parse_args([])
        self.assertEqual(set(sm.matrix_options_from(args)), expected)

    def test_build_display_returns_a_mock_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = sm.build_parser().parse_args(["--mock-output", str(Path(tmp) / "f.png")])
            self.assertIsInstance(sm.build_display(args), sm.MockDisplay)


# ======================================================================================
# OAuth callback server
# ======================================================================================


class TestLocalCallbackServer(unittest.TestCase):
    def test_wait_for_code_times_out_as_an_auth_error(self):
        with sm.LocalCallbackServer("127.0.0.1", 0, "/callback", "state-1") as server:
            with self.assertRaises(sm.ProviderAuthError) as ctx:
                server.wait_for_code(timeout_seconds=0.2)
        self.assertIn("Timed out", str(ctx.exception))

    def test_exiting_the_context_releases_the_port(self):
        """Without this, a failure before wait_for_code leaves 8888 bound until restart."""
        server = sm.LocalCallbackServer("127.0.0.1", 0, "/callback", "state-1")
        port = server.server.server_address[1]
        with server:
            pass
        # Rebinding the same port proves it was released.
        rebound = HTTPServer(("127.0.0.1", port), sm.BaseHTTPRequestHandler)
        rebound.server_close()

    def test_port_is_released_even_when_the_body_raises(self):
        server = sm.LocalCallbackServer("127.0.0.1", 0, "/callback", "state-1")
        port = server.server.server_address[1]
        with contextlib.suppress(ValueError):
            with server:
                raise ValueError("boom")
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()


# ======================================================================================
# CLI and wiring
# ======================================================================================


class TestValidateArgs(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_single_panel_is_accepted(self):
        sm.validate_args(self.parse([]))

    def test_chained_panels_are_rejected_rather_than_silently_half_rendered(self):
        for argv in (["--chain-length", "2"], ["--parallel", "2"], ["--chain-length", "2", "--parallel", "3"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    sm.validate_args(self.parse(argv))
                self.assertIn("--chain-length", str(ctx.exception))

    def test_zero_is_rejected(self):
        with self.assertRaises(SystemExit):
            sm.validate_args(self.parse(["--chain-length", "0"]))


class TestCli(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_provider_defaults_to_spotify(self):
        self.assertEqual(self.parse([]).provider, "spotify")

    def test_choices_come_from_the_provider_table(self):
        action = next(a for a in sm.build_parser()._actions if a.dest == "provider")
        self.assertEqual(set(action.choices), set(sm.PROVIDERS))

    def test_known_providers_accepted(self):
        for provider in sm.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(self.parse(["--provider", provider]).provider, provider)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(SystemExit), quiet():
            self.parse(["--provider", "tidal"])

    def test_stale_window_defaults_and_overrides(self):
        self.assertEqual(self.parse([]).ytmusic_stale_seconds, 600.0)
        self.assertEqual(self.parse(["--ytmusic-stale-seconds", "90"]).ytmusic_stale_seconds, 90.0)

    def test_non_positive_values_rejected(self):
        for flag in ("--ytmusic-stale-seconds", "--auth-timeout-seconds", "--fps", "--rpm", "--poll-seconds"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit), quiet():
                    self.parse([flag, "0"])

    def test_missing_env_vars_helper(self):
        self.assertEqual(sm.missing_env_vars({"A": "x", "B": None, "C": ""}), ["B", "C"])


class TestProviderTable(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_every_provider_has_a_verified_message(self):
        args = self.parse([])
        for name, spec in sm.PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertTrue(spec.verified_message(args))

    def test_build_provider_reports_all_missing_lastfm_vars_at_once(self):
        args = self.parse(["--provider", "lastfm"])
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                sm.build_provider(args)
        self.assertIn("LASTFM_API_KEY", str(ctx.exception))
        self.assertIn("LASTFM_USER", str(ctx.exception))

    def test_build_provider_reports_missing_spotify_vars(self):
        args = self.parse([])
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                sm.build_provider(args)
        self.assertIn("SPOTIFY_CLIENT_ID", str(ctx.exception))
        self.assertIn("SPOTIFY_CLIENT_SECRET", str(ctx.exception))

    def test_spotify_redirect_uri_has_a_default_and_is_not_required(self):
        args = self.parse([])
        env = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertEqual(provider.redirect_uri, sm.DEFAULT_REDIRECT_URI)

    def test_spotify_redirect_uri_can_be_overridden(self):
        args = self.parse([])
        env = {
            "SPOTIFY_CLIENT_ID": "cid",
            "SPOTIFY_CLIENT_SECRET": "secret",
            "SPOTIFY_REDIRECT_URI": "http://localhost:9999/cb",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertEqual(provider.redirect_uri, "http://localhost:9999/cb")

    def test_spotify_gets_a_file_backed_token_store(self):
        args = self.parse(["--token-cache", "/tmp/nowhere/token.json"])
        env = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertIsInstance(provider._store, sm.FileTokenStore)
        self.assertEqual(provider._store.path, Path("/tmp/nowhere/token.json"))

    def test_lastfm_credentials_are_passed_through(self):
        args = self.parse(["--provider", "lastfm"])
        env = {"LASTFM_API_KEY": "key-1", "LASTFM_USER": "pam"}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertEqual((provider.api_key, provider.user), ("key-1", "pam"))

    def test_youtube_music_needs_no_env_and_gets_the_stale_window(self):
        args = self.parse(["--provider", "youtube-music", "--ytmusic-stale-seconds", "42"])
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = sm.build_provider(args)
        self.assertIsInstance(provider, sm.YouTubeMusicClient)
        self.assertEqual(provider.stale_after_seconds, 42.0)


# ======================================================================================
# Startup behaviour of run()
# ======================================================================================


class StubProvider:
    name = "stub"

    def __init__(self, authorize_error=None, art=None):
        self.authorize_error = authorize_error
        self.art = art
        self.authorize_calls = 0

    def authorize(self):
        self.authorize_calls += 1
        if self.authorize_error:
            raise self.authorize_error

    def get_playback_art(self):
        return self.art


class TestRunStartup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "frame.png"

    def run_with(self, provider, extra_argv=()):
        argv = ["--mock-output", str(self.output), "--once", *extra_argv]
        args = sm.build_parser().parse_args(argv)
        with mock.patch.object(sm, "build_provider", return_value=provider), \
             mock.patch.object(sm, "download_image", return_value=art_image(size=(64, 64))), \
             mock.patch.object(sm, "load_dotenv"), quiet():
            sm.run(args)

    def test_transient_startup_failure_still_shows_a_frame(self):
        """A Pi that boots before Wi-Fi associates must not end up with a dark panel."""
        provider = StubProvider(authorize_error=sm.ProviderUnavailableError("no route to host"))
        self.run_with(provider)
        self.assertTrue(self.output.exists())

    def test_bare_oserror_at_startup_is_also_survivable(self):
        provider = StubProvider(authorize_error=OSError("network is unreachable"))
        self.run_with(provider)
        self.assertTrue(self.output.exists())

    def test_rate_limit_at_startup_is_survivable(self):
        provider = StubProvider(authorize_error=sm.ProviderRateLimitError("stub", 30))
        self.run_with(provider)
        self.assertTrue(self.output.exists())

    def test_fatal_auth_failure_exits(self):
        provider = StubProvider(authorize_error=sm.ProviderAuthError("bad api key"))
        with self.assertRaises(SystemExit) as ctx:
            self.run_with(provider)
        self.assertIn("bad api key", str(ctx.exception))

    def test_auth_only_still_fails_loudly_on_a_transient_error(self):
        provider = StubProvider(authorize_error=sm.ProviderUnavailableError("no route"))
        with self.assertRaises(SystemExit):
            self.run_with(provider, extra_argv=["--auth-only"])

    def test_auth_only_reports_success_without_touching_the_display(self):
        args = sm.build_parser().parse_args(["--auth-only"])
        provider = StubProvider()
        buffer = io.StringIO()
        with mock.patch.object(sm, "build_provider", return_value=provider), \
             mock.patch.object(sm, "load_dotenv"), \
             mock.patch.object(sm, "build_display", side_effect=AssertionError("no display")), \
             contextlib.redirect_stdout(buffer):
            sm.run(args)
        self.assertIn("token cached", buffer.getvalue())

    def test_test_pattern_needs_no_provider_at_all(self):
        args = sm.build_parser().parse_args(
            ["--test-pattern", "--mock-output", str(self.output), "--once"]
        )
        with mock.patch.object(sm, "build_provider", side_effect=AssertionError("no provider")), \
             mock.patch.object(sm, "load_dotenv"), quiet():
            sm.run(args)
        self.assertTrue(self.output.exists())

    def test_test_pattern_honours_once(self):
        args = sm.build_parser().parse_args(
            ["--test-pattern", "--mock-output", str(self.output), "--once"]
        )
        display = RecordingDisplay()
        with mock.patch.object(sm, "build_display", return_value=display), \
             mock.patch.object(sm, "load_dotenv"), quiet():
            sm.run(args)
        self.assertEqual(len(display.frames), 1)
        self.assertEqual(display.cleared, 1, "the display must be cleared on the way out")

    def test_display_is_cleared_when_the_poll_thread_cannot_start(self):
        """Cleanup must be registered before the thread, or the panel keeps the last frame."""
        display = RecordingDisplay()
        args = sm.build_parser().parse_args(["--mock-output", str(self.output), "--once"])
        with mock.patch.object(sm, "build_provider", return_value=StubProvider()), \
             mock.patch.object(sm, "build_display", return_value=display), \
             mock.patch.object(sm, "load_dotenv"), \
             mock.patch.object(sm.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
             quiet():
            with self.assertRaises(RuntimeError):
                sm.run(args)
        self.assertEqual(display.cleared, 1)

    def test_chained_panel_config_is_rejected_before_touching_hardware(self):
        args = sm.build_parser().parse_args(["--chain-length", "2"])
        with mock.patch.object(sm, "build_display", side_effect=AssertionError("no display")):
            with self.assertRaises(SystemExit):
                sm.run(args)

    def test_preview_frames_needs_no_credentials_or_validation(self):
        directory = Path(self.tmp.name) / "preview"
        args = sm.build_parser().parse_args(["--preview-frames", str(directory)])
        with mock.patch.object(sm, "build_provider", side_effect=AssertionError("no provider")):
            sm.run(args)
        self.assertEqual(len(list(directory.iterdir())), 4)


# ======================================================================================
# Display styles
# ======================================================================================


class TestAlbumArtRenderer(unittest.TestCase):
    SIZE = 64

    def setUp(self):
        self.renderer = sm.AlbumArtRenderer(self.SIZE)

    def test_is_not_animated(self):
        self.assertFalse(self.renderer.animated)

    def test_fit_fills_the_whole_panel_not_just_the_disc(self):
        self.assertEqual(self.renderer.fit(art_image()).size, (self.SIZE, self.SIZE))

    def test_art_reaches_the_edges(self):
        frame = self.renderer.render(self.renderer.fit(art_image((255, 0, 0))), 0.0)
        for corner in ((0, 0), (63, 0), (0, 63), (63, 63)):
            with self.subTest(corner=corner):
                self.assertEqual(frame.getpixel(corner), (255, 0, 0))

    def test_no_disc_furniture_is_drawn(self):
        # Flat art must come out flat: no ring, no centre label, no spindle hole.
        frame = self.renderer.render(self.renderer.fit(art_image((255, 0, 0))), 0.0)
        self.assertEqual(frame.getextrema(), ((255, 255), (0, 0), (0, 0)))

    def test_angle_is_ignored(self):
        fitted = self.renderer.fit(sm.demo_album_art(256))
        self.assertEqual(
            self.renderer.render(fitted, 0.0).tobytes(),
            self.renderer.render(fitted, 137.0).tobytes(),
        )

    def test_render_is_a_no_op_for_already_fitted_art(self):
        fitted = self.renderer.fit(art_image())
        self.assertIs(self.renderer.render(fitted, 0.0), fitted)

    def test_tolerates_unfitted_art(self):
        frame = self.renderer.render(art_image((3, 4, 5), (640, 640)), 0.0)
        self.assertEqual(frame.size, (self.SIZE, self.SIZE))
        self.assertEqual(frame.mode, "RGB")

    def test_idle_is_stable_and_shared(self):
        self.assertIs(self.renderer.idle(), self.renderer.idle())


class TestRendererStyles(unittest.TestCase):
    def test_both_styles_are_registered(self):
        self.assertEqual(set(sm.RENDERER_STYLES), {"record", "art"})

    def test_build_renderer_picks_the_right_class(self):
        self.assertIsInstance(sm.build_renderer("record", 64), sm.RecordRenderer)
        self.assertIsInstance(sm.build_renderer("art", 64), sm.AlbumArtRenderer)

    def test_every_style_satisfies_the_frame_renderer_protocol(self):
        for style in sm.RENDERER_STYLES:
            with self.subTest(style=style):
                renderer = sm.build_renderer(style, 64)
                fitted = renderer.fit(art_image())
                frame = renderer.render(fitted, 12.0)
                self.assertEqual(frame.size, (64, 64))
                self.assertEqual(frame.mode, "RGB")
                self.assertEqual(renderer.idle().size, (64, 64))
                self.assertIsInstance(renderer.animated, bool)

    def test_cli_choices_match_the_style_table(self):
        action = next(a for a in sm.build_parser()._actions if a.dest == "style")
        self.assertEqual(set(action.choices), set(sm.RENDERER_STYLES))
        self.assertEqual(sm.build_parser().parse_args([]).style, "record")

    def test_static_style_does_not_advance_the_angle(self):
        renderer = sm.build_renderer("art", 64)
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True), renderer.fit(art_image()))
        source = sm.RecordFrameSource(renderer, state, 20.0, start_time=0.0)
        source(1.0)
        source(2.0)
        self.assertEqual(source.angle, 0.0, "no point spinning art that is not a disc")

    def test_record_style_does_advance_the_angle(self):
        renderer = sm.build_renderer("record", 64)
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True), renderer.fit(art_image()))
        source = sm.RecordFrameSource(renderer, state, 20.0, start_time=0.0)
        source(1.0)
        self.assertNotEqual(source.angle, 0.0)


class TestRenderPreviewFramesStyles(unittest.TestCase):
    def test_record_style_writes_four_angles(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p"
            sm.render_preview_frames(out, style="record")
            self.assertEqual(len(list(out.iterdir())), 4)

    def test_static_style_writes_one_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p"
            sm.render_preview_frames(out, style="art")
            self.assertEqual([f.name for f in out.iterdir()], ["album-disk-00.png"])

    def test_scale_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p"
            sm.render_preview_frames(out, style="art", scale=5)
            with Image.open(out / "album-disk-00.png") as image:
                self.assertEqual(image.size, (320, 320))


# ======================================================================================
# Preview backends
# ======================================================================================


class TestScaleForPreview(unittest.TestCase):
    def test_scale_of_one_is_a_no_op(self):
        image = art_image(size=(8, 8))
        self.assertIs(sm.scale_for_preview(image, 1), image)

    def test_magnifies_by_the_given_factor(self):
        self.assertEqual(sm.scale_for_preview(art_image(size=(8, 8)), 4).size, (32, 32))

    def test_uses_nearest_neighbour_so_pixels_stay_crisp(self):
        source = Image.new("RGB", (2, 1), (255, 0, 0))
        source.putpixel((1, 0), (0, 0, 255))
        scaled = sm.scale_for_preview(source, 8)
        # A smoothing filter would blend across the boundary; nearest must not.
        self.assertEqual(scaled.getpixel((3, 4)), (255, 0, 0))
        self.assertEqual(scaled.getpixel((12, 4)), (0, 0, 255))
        self.assertEqual({scaled.getpixel((x, 0)) for x in range(16)}, {(255, 0, 0), (0, 0, 255)})

    def test_grid_draws_the_inter_pixel_gutter(self):
        flat = sm.scale_for_preview(art_image((255, 255, 255), (4, 4)), 8, grid=False)
        gridded = sm.scale_for_preview(art_image((255, 255, 255), (4, 4)), 8, grid=True)
        self.assertEqual(flat.getpixel((8, 4)), (255, 255, 255), "no gutter without --preview-grid")
        self.assertEqual(gridded.getpixel((8, 4)), (0, 0, 0), "gutter at each pixel boundary")
        self.assertEqual(gridded.getpixel((4, 4)), (255, 255, 255), "pixel centre untouched")

    def test_grid_is_skipped_when_there_is_no_room_for_it(self):
        # At scale 2 a gutter would consume half of every pixel.
        with_grid = sm.scale_for_preview(art_image((255, 255, 255), (4, 4)), 2, grid=True)
        self.assertEqual(with_grid.getpixel((2, 2)), (255, 255, 255))


class TestTerminalDisplay(unittest.TestCase):
    def test_two_pixel_rows_per_character_row(self):
        text = sm.TerminalDisplay.frame_to_text(art_image(size=(64, 64)))
        self.assertEqual(len(text.split("\n")), 32)

    def test_one_cell_per_pixel_column(self):
        text = sm.TerminalDisplay.frame_to_text(art_image(size=(64, 64)))
        self.assertEqual(text.split("\n")[0].count(sm.TerminalDisplay.HALF_BLOCK), 64)

    def test_encodes_the_top_pixel_as_foreground_and_bottom_as_background(self):
        image = Image.new("RGB", (1, 2))
        image.putpixel((0, 0), (10, 20, 30))
        image.putpixel((0, 1), (40, 50, 60))
        text = sm.TerminalDisplay.frame_to_text(image)
        self.assertIn("38;2;10;20;30", text)
        self.assertIn("48;2;40;50;60", text)

    def test_repeated_colours_do_not_repeat_the_escape_code(self):
        """A 64x64 frame is 2048 cells; re-emitting SGR per cell would be needlessly fat."""
        flat = sm.TerminalDisplay.frame_to_text(art_image((5, 5, 5), (64, 64)))
        self.assertEqual(flat.count("\x1b[38;2;"), 32, "one colour change per row at most")

    def test_odd_height_does_not_crash_or_drop_a_row(self):
        text = sm.TerminalDisplay.frame_to_text(art_image(size=(4, 5)))
        self.assertEqual(len(text.split("\n")), 3)

    def test_accepts_non_rgb_frames(self):
        sm.TerminalDisplay.frame_to_text(Image.new("RGBA", (4, 4), (1, 2, 3, 255)))

    def test_show_homes_the_cursor_instead_of_scrolling(self):
        stream = io.StringIO()
        display = sm.TerminalDisplay(stream=stream)
        display.show(art_image(size=(4, 4)))
        first = stream.getvalue()
        display.show(art_image(size=(4, 4)))
        second = stream.getvalue()[len(first):]
        self.assertIn("\x1b[2J", first, "clears the screen once on the first frame")
        self.assertIn("\x1b[?25l", first, "hides the cursor")
        self.assertNotIn("\x1b[2J", second, "later frames overwrite in place")
        self.assertTrue(second.startswith("\x1b[H"))

    def test_clear_restores_the_cursor(self):
        stream = io.StringIO()
        display = sm.TerminalDisplay(stream=stream)
        display.show(art_image(size=(4, 4)))
        display.clear()
        self.assertIn("\x1b[?25h", stream.getvalue())

    def test_clear_is_a_no_op_before_any_frame(self):
        stream = io.StringIO()
        sm.TerminalDisplay(stream=stream).clear()
        self.assertEqual(stream.getvalue(), "")

    def test_context_manager_restores_the_cursor_on_exit(self):
        stream = io.StringIO()
        with sm.TerminalDisplay(stream=stream) as display:
            display.show(art_image(size=(4, 4)))
        self.assertIn("\x1b[?25h", stream.getvalue())


class TestGifRecorderDisplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "out" / "spin.gif"

    def frames_of(self, path):
        with Image.open(path) as gif:
            return [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(gif)]

    def test_nothing_is_written_before_exit(self):
        display = sm.GifRecorderDisplay(self.output, fps=10.0)
        display.show(art_image(size=(8, 8)))
        self.assertFalse(self.output.exists())

    def test_writes_an_animated_gif_on_exit(self):
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=10.0) as display:
                for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
                    display.show(art_image(colour, (8, 8)))
        frames = self.frames_of(self.output)
        self.assertEqual(len(frames), 3)
        self.assertEqual(len({f.tobytes() for f in frames}), 3)

    def test_no_file_when_no_frames_were_shown(self):
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=10.0):
                pass
        self.assertFalse(self.output.exists())

    def test_frame_delay_follows_fps(self):
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=10.0) as display:
                display.show(art_image(size=(8, 8)))
                display.show(art_image((9, 9, 9), (8, 8)))
        with Image.open(self.output) as gif:
            self.assertEqual(gif.info["duration"], 100)

    def test_frame_delay_is_clamped_to_what_gif_can_represent(self):
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=200.0) as display:
                display.show(art_image(size=(8, 8)))
                display.show(art_image((9, 9, 9), (8, 8)))
        with Image.open(self.output) as gif:
            self.assertGreaterEqual(gif.info["duration"], 20)

    def test_scale_is_applied_to_the_recording(self):
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=10.0, scale=4) as display:
                display.show(art_image(size=(8, 8)))
        self.assertEqual(self.frames_of(self.output)[0].size, (32, 32))

    def test_copies_frames_so_a_reused_buffer_is_not_captured_twice(self):
        """AlbumArtRenderer returns the same object every frame; the recorder must copy."""
        shared = art_image((1, 1, 1), (8, 8))
        with quiet():
            with sm.GifRecorderDisplay(self.output, fps=10.0) as display:
                display.show(shared)
                shared.paste((255, 255, 255), (0, 0, 8, 8))
                display.show(shared)
        frames = self.frames_of(self.output)
        self.assertEqual(len({f.tobytes() for f in frames}), 2)

    def test_creates_the_parent_directory(self):
        sm.GifRecorderDisplay(self.output, fps=10.0)
        self.assertTrue(self.output.parent.is_dir())


class TestBuildDisplayBackends(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_mock_output_selects_the_png_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            display = sm.build_display(self.parse(["--mock-output", str(Path(tmp) / "f.png")]))
        self.assertIsInstance(display, sm.MockDisplay)

    def test_preview_terminal_selects_the_terminal_backend(self):
        self.assertIsInstance(sm.build_display(self.parse(["--preview-terminal"])), sm.TerminalDisplay)

    def test_record_gif_selects_the_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            display = sm.build_display(self.parse(["--record-gif", str(Path(tmp) / "a.gif")]))
        self.assertIsInstance(display, sm.GifRecorderDisplay)

    def test_scale_and_grid_reach_the_png_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            display = sm.build_display(self.parse([
                "--mock-output", str(Path(tmp) / "f.png"), "--preview-scale", "6", "--preview-grid",
            ]))
        self.assertEqual((display.scale, display.grid), (6, True))

    def test_backends_are_mutually_exclusive(self):
        pairs = [
            ["--mock-output", "/tmp/a.png", "--preview-terminal"],
            ["--preview-terminal", "--record-gif", "/tmp/a.gif"],
            ["--mock-output", "/tmp/a.png", "--record-gif", "/tmp/a.gif"],
        ]
        for argv in pairs:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit), quiet():
                    self.parse(argv)

    def test_mock_display_scale_reaches_the_written_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "f.png"
            sm.MockDisplay(output, scale=3).show(art_image(size=(8, 8)))
            with Image.open(output) as written:
                self.assertEqual(written.size, (24, 24))


# ======================================================================================
# Demo provider
# ======================================================================================


class TestDemoProvider(unittest.TestCase):
    def build(self, cycle_seconds=6.0):
        clock = FakeClock()
        return sm.DemoProvider(cycle_seconds=cycle_seconds, clock=clock), clock

    def test_needs_no_authorization(self):
        provider, _ = self.build()
        self.assertIsNone(provider.authorize())

    def test_starts_playing(self):
        provider, _ = self.build()
        art = provider.get_playback_art()
        self.assertTrue(art.is_playing)
        self.assertEqual(art.image_url, sm.DemoProvider.DEMO_IMAGE_URL)

    def test_cycles_playing_then_paused_then_idle(self):
        provider, clock = self.build(cycle_seconds=6.0)
        observed = []
        for _ in range(3):
            art = provider.get_playback_art()
            observed.append(None if art is None else art.is_playing)
            clock.advance(6.0)
        self.assertEqual(observed, [True, False, None])

    def test_cycle_repeats(self):
        provider, clock = self.build(cycle_seconds=6.0)
        clock.advance(18.0)
        self.assertTrue(provider.get_playback_art().is_playing)

    def test_paused_state_keeps_the_same_track(self):
        provider, clock = self.build(cycle_seconds=6.0)
        first = provider.get_playback_art()
        clock.advance(6.0)
        second = provider.get_playback_art()
        self.assertEqual(first.key, second.key)
        self.assertFalse(second.is_playing)

    def test_is_registered_as_a_provider_needing_no_env(self):
        self.assertIn("demo", sm.PROVIDERS)
        self.assertEqual(sm.PROVIDERS["demo"].required_env, ())
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = sm.build_provider(sm.build_parser().parse_args(["--provider", "demo"]))
        self.assertIsInstance(provider, sm.DemoProvider)

    def test_cycle_length_comes_from_the_cli(self):
        args = sm.build_parser().parse_args(["--provider", "demo", "--demo-cycle-seconds", "2"])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sm.build_provider(args).cycle_seconds, 2.0)


class TestBuildImagePreparer(unittest.TestCase):
    def test_demo_provider_never_downloads(self):
        args = sm.build_parser().parse_args(["--provider", "demo"])
        renderer = sm.build_renderer("record", 64)
        with mock.patch.object(sm, "download_image", side_effect=AssertionError("no network")):
            image = sm.build_image_preparer(args, renderer)(sm.DemoProvider.DEMO_IMAGE_URL)
        self.assertEqual(image.size, (renderer.disc_size,) * 2)

    def test_real_provider_downloads_and_fits(self):
        args = sm.build_parser().parse_args([])
        renderer = sm.build_renderer("record", 64)
        with mock.patch.object(sm, "download_image", return_value=art_image(size=(640, 640))) as dl:
            image = sm.build_image_preparer(args, renderer)("https://art/a.png")
        dl.assert_called_once_with("https://art/a.png")
        self.assertEqual(image.size, (renderer.disc_size,) * 2)

    def test_fits_to_the_active_style(self):
        args = sm.build_parser().parse_args(["--provider", "demo"])
        art_renderer = sm.build_renderer("art", 64)
        image = sm.build_image_preparer(args, art_renderer)("x")
        self.assertEqual(image.size, (64, 64), "static art fills the panel, not just the disc")


# ======================================================================================
# Frame budget and preview end-to-end
# ======================================================================================


class TestFrameBudget(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_live_run_is_unbounded(self):
        self.assertIsNone(sm.frame_budget(self.parse([])))

    def test_once_is_a_single_frame(self):
        self.assertEqual(sm.frame_budget(self.parse(["--once"])), 1)

    def test_gif_length_is_fps_times_seconds(self):
        args = self.parse(["--record-gif", "/tmp/a.gif", "--fps", "10", "--record-seconds", "3"])
        self.assertEqual(sm.frame_budget(args), 30)

    def test_gif_records_at_least_one_frame(self):
        args = self.parse(["--record-gif", "/tmp/a.gif", "--fps", "1", "--record-seconds", "0.1"])
        self.assertEqual(sm.frame_budget(args), 1)

    def test_once_wins_over_gif_length(self):
        args = self.parse(["--record-gif", "/tmp/a.gif", "--once"])
        self.assertEqual(sm.frame_budget(args), 1)


class TestDriveDisplayFrameBudget(unittest.TestCase):
    def test_stops_after_max_frames(self):
        display = RecordingDisplay()
        sm.drive_display(
            display, lambda now: art_image(), fps=1000.0, max_frames=7, sleep=lambda s: None
        )
        self.assertEqual(len(display.frames), 7)

    def test_no_trailing_sleep_after_the_last_frame(self):
        slept = []
        sm.drive_display(
            RecordingDisplay(), lambda now: art_image(), fps=10.0, max_frames=3,
            monotonic=lambda: 0.0, sleep=slept.append,
        )
        self.assertEqual(len(slept), 2, "sleeps between frames, not after the last one")


class TestPlaybackStateFirstUpdate(unittest.TestCase):
    def test_not_set_before_the_first_poll(self):
        self.assertFalse(sm.PlaybackState().wait_for_first_update(timeout=0.01))

    def test_set_by_update(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True), art_image())
        self.assertTrue(state.wait_for_first_update(timeout=0.01))

    def test_set_by_clear_so_an_idle_provider_does_not_stall_a_preview(self):
        state = sm.PlaybackState()
        state.clear()
        self.assertTrue(state.wait_for_first_update(timeout=0.01))


class TestPreviewEndToEnd(unittest.TestCase):
    """The whole point: see what the panel would show, with no panel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, argv):
        with quiet():
            sm.run(sm.build_parser().parse_args(argv))

    def test_demo_provider_renders_real_art_not_the_idle_frame(self):
        output = self.dir / "f.png"
        self.run_cli(["--provider", "demo", "--mock-output", str(output), "--once"])
        with Image.open(output) as frame:
            colours = {frame.convert("RGB").getpixel((16, 32))}
        self.assertNotEqual(colours, {(0, 0, 0)}, "the poll should land before the frame")

    def test_static_style_fills_the_panel_end_to_end(self):
        output = self.dir / "art.png"
        self.run_cli(["--provider", "demo", "--style", "art", "--mock-output", str(output), "--once"])
        with Image.open(output) as frame:
            self.assertNotEqual(frame.convert("RGB").getpixel((0, 0)), (0, 0, 0))

    def test_record_style_leaves_the_corners_black_end_to_end(self):
        output = self.dir / "rec.png"
        self.run_cli(["--provider", "demo", "--style", "record", "--mock-output", str(output), "--once"])
        with Image.open(output) as frame:
            self.assertEqual(frame.convert("RGB").getpixel((0, 0)), (0, 0, 0))

    def test_gif_recording_captures_the_spin(self):
        output = self.dir / "spin.gif"
        self.run_cli([
            "--provider", "demo", "--record-gif", str(output), "--record-seconds", "1", "--fps", "8",
        ])
        with Image.open(output) as gif:
            frames = [f.copy().convert("RGB").tobytes() for f in ImageSequence.Iterator(gif)]
        self.assertGreaterEqual(len(frames), 2)
        self.assertGreater(len(set(frames)), 1, "a spinning record should differ frame to frame")

    def test_terminal_preview_writes_a_frame_without_hardware(self):
        stream = io.StringIO()
        args = sm.build_parser().parse_args(["--provider", "demo", "--preview-terminal", "--once"])
        real = sm.TerminalDisplay  # capture before patching, or the lambda recurses
        with mock.patch.object(sm, "TerminalDisplay", lambda: real(stream=stream)), quiet():
            sm.run(args)
        self.assertIn(sm.TerminalDisplay.HALF_BLOCK, stream.getvalue())
        self.assertIn("\x1b[38;2;", stream.getvalue())

    def test_scaled_grid_preview_is_written_at_the_requested_size(self):
        output = self.dir / "big.png"
        self.run_cli([
            "--provider", "demo", "--mock-output", str(output),
            "--preview-scale", "8", "--preview-grid", "--once",
        ])
        with Image.open(output) as frame:
            self.assertEqual(frame.size, (512, 512))

    def test_demo_needs_no_credentials(self):
        output = self.dir / "f.png"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.run_cli(["--provider", "demo", "--mock-output", str(output), "--once"])
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
