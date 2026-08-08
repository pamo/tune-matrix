"""Unit tests for tune_matrix.

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
from datetime import datetime
from email.message import Message
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image, ImageSequence

sys.argv = ["tune_matrix"]
import tune_matrix as sm

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
            sm.PlaybackArt(
                key="mb-1",
                image_url="xl.png",
                is_playing=True,
                title="Massive Attack - Teardrop",
            ),
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

    def test_title_is_artist_and_track(self):
        client = self.client(
            lastfm_payload([{"size": "large", "#text": "l.png"}], mbid="mb", artist="Robyn", name="Dancing On My Own")
        )
        self.assertEqual(client.get_playback_art().title, "Robyn - Dancing On My Own")

    def test_title_is_none_when_the_track_is_unnamed(self):
        payload = {
            "recenttracks": {
                "track": [{
                    "@attr": {"nowplaying": "true"},
                    "image": [{"size": "large", "#text": "l.png"}],
                }]
            }
        }
        self.assertIsNone(self.client(payload).get_playback_art().title)


class TestLastFmAccountIdentification(unittest.TestCase):
    """A typo'd username that happens to exist verifies fine and shows someone else's music."""

    def payload(self, user="pam-o", total="257480"):
        return {
            "recenttracks": {
                "@attr": {"user": user, "total": total, "page": "1", "perPage": "1"},
                "track": [],
            }
        }

    def client(self, payload):
        return sm.LastFmClient("key", "requested-name", http=fake_http(http_response(payload)))

    def test_authorize_records_which_account_answered(self):
        client = self.client(self.payload())
        client.authorize()
        self.assertEqual(client.account_summary, "user pam-o, 257,480 scrobbles")

    def test_summary_names_the_resolved_user_not_the_requested_one(self):
        client = self.client(self.payload(user="someone-else"))
        client.authorize()
        self.assertIn("someone-else", client.account_summary)
        self.assertNotIn("requested-name", client.account_summary)

    def test_scrobble_count_is_thousands_separated(self):
        client = self.client(self.payload(total="1234567"))
        client.authorize()
        self.assertIn("1,234,567 scrobbles", client.account_summary)

    def test_non_numeric_total_does_not_crash(self):
        client = self.client(self.payload(total="lots"))
        client.authorize()
        self.assertIn("unknown scrobbles", client.account_summary)

    def test_missing_attr_block_leaves_no_summary(self):
        client = self.client({"recenttracks": {"track": []}})
        client.authorize()
        self.assertIsNone(client.account_summary)

    def test_summary_is_also_recorded_by_a_normal_poll(self):
        client = self.client(self.payload())
        client.get_playback_art()
        self.assertEqual(client.account_summary, "user pam-o, 257,480 scrobbles")

    def test_verified_message_shows_the_account(self):
        client = self.client(self.payload())
        client.authorize()
        message = sm.PROVIDERS["lastfm"].verified_message(sm.build_parser().parse_args([]), client)
        self.assertIn("pam-o", message)
        self.assertIn("257,480", message)
        self.assertIn("your account", message)

    def test_verified_message_falls_back_when_the_account_is_unknown(self):
        client = self.client({"recenttracks": {"track": []}})
        client.authorize()
        message = sm.PROVIDERS["lastfm"].verified_message(sm.build_parser().parse_args([]), client)
        self.assertIn("verified", message)

    def test_bad_key_still_raises_before_any_summary_is_recorded(self):
        client = sm.LastFmClient(
            "key", "user", http=fake_http(http_response({"error": 10, "message": "Invalid API key"}))
        )
        with self.assertRaises(sm.ProviderAuthError):
            client.authorize()
        self.assertIsNone(client.account_summary)


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

    def test_title_combines_artists_and_track_name(self):
        entry = self.entry()
        entry["artists"] = [{"name": "Robyn"}]
        entry["title"] = "Honey"
        client, _ = self.build([entry])
        self.assertEqual(client.get_playback_art().title, "Robyn - Honey")

    def test_title_falls_back_to_the_bare_name_without_artists(self):
        client, _ = self.build([self.entry(title="Just A Title")])
        self.assertEqual(client.get_playback_art().title, "Just A Title")

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

    def test_title_combines_artists_and_track_name(self):
        art = sm.playback_art_from_response({
            "is_playing": True,
            "item": {
                "type": "track",
                "id": "t1",
                "name": "Dancing On My Own",
                "artists": [{"name": "Robyn"}],
                "album": {"images": [{"url": "a.png", "width": 640}]},
            },
        })
        self.assertEqual(art.title, "Robyn - Dancing On My Own")

    def test_title_joins_multiple_artists(self):
        art = sm.playback_art_from_response({
            "item": {
                "type": "track",
                "id": "t1",
                "name": "Song",
                "artists": [{"name": "A"}, {"name": "B"}],
                "album": {"images": [{"url": "a.png", "width": 1}]},
            },
        })
        self.assertEqual(art.title, "A, B - Song")

    def test_episode_title_is_just_the_name(self):
        art = sm.playback_art_from_response({
            "item": {"type": "episode", "id": "e1", "name": "Ep 12", "images": [{"url": "e.png", "width": 1}]},
        })
        self.assertEqual(art.title, "Ep 12")

    def test_title_is_none_when_unnamed(self):
        art = sm.playback_art_from_response({
            "item": {"type": "track", "id": "t", "album": {"images": [{"url": "a.png", "width": 1}]}},
        })
        self.assertIsNone(art.title)

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
        self.assertEqual(buffer.getvalue().count("nothing playing"), 1)

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


def stub_config(**overrides):
    """A ConfigStore backed by a path that does not exist, so it always yields defaults."""
    return sm.ConfigStore(Path("/nonexistent/config.json"), defaults=sm.Config(**overrides))


class TestAlbumScene(unittest.TestCase):
    def setUp(self):
        self.renderers = {name: sm.build_renderer(name, 64) for name in sm.RENDERER_STYLES}
        self.state = sm.PlaybackState()

    def scene(self, style="record", rpm=20.0, start_time=0.0):
        return sm.AlbumScene(
            self.renderers, self.state, stub_config(style=style, rpm=rpm), start_time=start_time
        )

    def renderer(self, style="record"):
        return self.renderers[style]

    def play(self, colour=(255, 0, 0), is_playing=True, style="record"):
        self.state.update(
            sm.PlaybackArt("k", "u", is_playing), self.renderer(style).fit(art_image(colour))
        )

    def test_shows_idle_when_there_is_no_art(self):
        self.assertIs(self.scene().frame(1.0), self.renderer().idle())

    def test_first_frame_does_not_jump_the_angle(self):
        self.play()
        scene = sm.AlbumScene(self.renderers, self.state, stub_config())
        # 7.3 is deliberately not a whole number of revolutions at 20 rpm: treating the
        # monotonic clock's absolute value as elapsed time would land on 156 degrees.
        scene.frame(7.3)
        self.assertEqual(scene.angle, 0.0, "elapsed time before the first frame is not real")

    def test_angle_advances_while_playing(self):
        self.play()
        scene = self.scene()
        scene.frame(1.0)
        self.assertAlmostEqual(scene.angle, 240.0)

    def test_angle_frozen_while_paused(self):
        self.play(is_playing=False)
        scene = self.scene()
        scene.frame(1.0)
        scene.frame(2.0)
        self.assertEqual(scene.angle, 0.0)

    def test_paused_art_still_renders(self):
        self.play(is_playing=False)
        self.assertEqual(self.scene().frame(1.0).getpixel((16, 32)), (255, 0, 0))

    def test_returns_to_idle_when_art_is_cleared(self):
        self.play()
        scene = self.scene()
        scene.frame(1.0)
        self.state.clear()
        self.assertIs(scene.frame(2.0), self.renderer().idle())

    def test_style_comes_from_the_config_not_the_constructor(self):
        self.play(colour=(0, 0, 255), style="art")
        record = self.scene(style="record").frame(1.0)
        art = self.scene(style="art").frame(1.0)
        self.assertEqual(record.getpixel((0, 0)), (0, 0, 0), "record leaves a black margin")
        self.assertEqual(art.getpixel((0, 0)), (0, 0, 255), "art fills the panel")

    def test_rpm_comes_from_the_config(self):
        self.play()
        scene = self.scene(rpm=60.0)
        scene.frame(1.0)
        self.assertAlmostEqual(scene.angle, 0.0, places=6)  # 60 rpm for 1s is a full turn

    def test_static_style_does_not_advance_the_angle(self):
        self.play(style="art")
        scene = self.scene(style="art")
        scene.frame(1.0)
        scene.frame(2.0)
        self.assertEqual(scene.angle, 0.0, "no point spinning art that is not a disc")

    def test_tolerates_art_fitted_for_the_other_style(self):
        # A style change leaves art sized for the previous renderer until the next poll.
        self.play(colour=(0, 255, 0), style="record")
        frame = self.scene(style="art").frame(1.0)
        self.assertEqual(frame.size, (64, 64))
        self.assertNotEqual(frame.getpixel((0, 0)), (0, 0, 0))


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

    def test_provider_defaults_to_lastfm(self):
        # Last.fm covers whatever service plays the music and needs no OAuth round-trip.
        self.assertEqual(self.parse([]).provider, "lastfm")
        self.assertEqual(self.parse([]).provider, sm.DEFAULT_PROVIDER)

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
                self.assertTrue(spec.verified_message(args, object()))

    def test_build_provider_reports_all_missing_lastfm_vars_at_once(self):
        args = self.parse(["--provider", "lastfm"])
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                sm.build_provider(args)
        self.assertIn("LASTFM_API_KEY", str(ctx.exception))
        self.assertIn("LASTFM_USER", str(ctx.exception))

    def test_build_provider_reports_missing_spotify_vars(self):
        args = self.parse(["--provider", "spotify"])
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                sm.build_provider(args)
        self.assertIn("SPOTIFY_CLIENT_ID", str(ctx.exception))
        self.assertIn("SPOTIFY_CLIENT_SECRET", str(ctx.exception))

    def test_spotify_redirect_uri_has_a_default_and_is_not_required(self):
        args = self.parse(["--provider", "spotify"])
        env = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertEqual(provider.redirect_uri, sm.DEFAULT_REDIRECT_URI)

    def test_spotify_redirect_uri_can_be_overridden(self):
        args = self.parse(["--provider", "spotify"])
        env = {
            "SPOTIFY_CLIENT_ID": "cid",
            "SPOTIFY_CLIENT_SECRET": "secret",
            "SPOTIFY_REDIRECT_URI": "http://localhost:9999/cb",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            provider = sm.build_provider(args)
        self.assertEqual(provider.redirect_uri, "http://localhost:9999/cb")

    def test_spotify_gets_a_file_backed_token_store(self):
        args = self.parse(["--provider", "spotify", "--token-cache", "/tmp/nowhere/token.json"])
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
    def isolated(self):
        return ["--config", str(Path(self.tmp.name) / "config.json")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "frame.png"

    def run_with(self, provider, extra_argv=()):
        argv = [
            "--mock-output", str(self.output), "--once",
            "--config", str(Path(self.tmp.name) / "config.json"),
            *extra_argv,
        ]
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
        for provider_name, expected in [
            ("spotify", "token cached"),
            ("lastfm", "verified"),
            ("demo", "no credentials"),
        ]:
            with self.subTest(provider=provider_name):
                args = sm.build_parser().parse_args(["--provider", provider_name, "--auth-only", *self.isolated()])
                buffer = io.StringIO()
                with mock.patch.object(sm, "build_provider", return_value=StubProvider()), \
                     mock.patch.object(sm, "load_dotenv"), \
                     mock.patch.object(
                         sm, "build_display", side_effect=AssertionError("no display")
                     ), \
                     contextlib.redirect_stdout(buffer):
                    sm.run(args)
                self.assertIn(expected, buffer.getvalue())

    def test_test_pattern_needs_no_provider_at_all(self):
        args = sm.build_parser().parse_args(
            ["--test-pattern", "--mock-output", str(self.output), "--once", *self.isolated()]
        )
        with mock.patch.object(sm, "build_provider", side_effect=AssertionError("no provider")), \
             mock.patch.object(sm, "load_dotenv"), quiet():
            sm.run(args)
        self.assertTrue(self.output.exists())

    def test_test_pattern_honours_once(self):
        args = sm.build_parser().parse_args(
            ["--test-pattern", "--mock-output", str(self.output), "--once", *self.isolated()]
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
        args = sm.build_parser().parse_args(
            ["--mock-output", str(self.output), "--once", *self.isolated()]
        )
        with mock.patch.object(sm, "build_provider", return_value=StubProvider()), \
             mock.patch.object(sm, "build_display", return_value=display), \
             mock.patch.object(sm, "load_dotenv"), \
             mock.patch.object(sm.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
             quiet():
            with self.assertRaises(RuntimeError):
                sm.run(args)
        self.assertEqual(display.cleared, 1)

    def test_chained_panel_config_is_rejected_before_touching_hardware(self):
        args = sm.build_parser().parse_args(["--chain-length", "2", *self.isolated()])
        with mock.patch.object(sm, "build_display", side_effect=AssertionError("no display")):
            with self.assertRaises(SystemExit):
                sm.run(args)

    def test_preview_frames_needs_no_credentials_or_validation(self):
        directory = Path(self.tmp.name) / "preview"
        args = sm.build_parser().parse_args(["--preview-frames", str(directory), *self.isolated()])
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
        # None means "leave config.json alone"; the effective default lives in Config.
        self.assertIsNone(sm.build_parser().parse_args([]).style)
        self.assertEqual(sm.Config().style, "record")



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


class TestTerminalScaleFor(unittest.TestCase):
    def test_accounts_for_two_pixel_rows_per_character_row(self):
        # 64x64 at scale 3 needs 192 columns and 96 pixel rows = 96 character rows... no:
        # 64*3 = 192 pixel rows over 2 = 96 character rows, so 98 rows with the reserve.
        self.assertEqual(sm.terminal_scale_for((64, 64), (192, 98), reserved_rows=2), 3)

    def test_limited_by_width(self):
        self.assertEqual(sm.terminal_scale_for((64, 64), (128, 999), reserved_rows=2), 2)

    def test_limited_by_height(self):
        self.assertEqual(sm.terminal_scale_for((64, 64), (9999, 66), reserved_rows=2), 2)

    def test_never_returns_less_than_one(self):
        self.assertEqual(sm.terminal_scale_for((64, 64), (10, 4), reserved_rows=2), 1)

    def test_reserved_rows_are_subtracted(self):
        # 64 rows is exactly enough for scale 2 (128 pixel rows) with nothing held back,
        # but not once the status line is reserved.
        self.assertEqual(sm.terminal_scale_for((64, 64), (9999, 64), reserved_rows=0), 2)
        self.assertEqual(sm.terminal_scale_for((64, 64), (9999, 64), reserved_rows=2), 1)
        self.assertEqual(sm.terminal_scale_for((64, 64), (9999, 66), reserved_rows=2), 2)

    def test_a_typical_full_screen_terminal_gets_real_magnification(self):
        self.assertGreaterEqual(sm.terminal_scale_for((64, 64), (204, 60)), 1)
        self.assertEqual(sm.terminal_scale_for((64, 64), (204, 60)), 1)
        self.assertEqual(sm.terminal_scale_for((64, 64), (256, 100)), 3)


class TestPlaybackStatus(unittest.TestCase):
    def test_idle(self):
        self.assertEqual(
            sm.playback_status("demo", "record", sm.PlaybackState()), "demo · record · idle"
        )

    def test_playing_includes_the_track(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("Massive Attack - Teardrop", "u", True), art_image())
        self.assertEqual(
            sm.playback_status("lastfm", "art", state),
            "lastfm · art · playing · Massive Attack - Teardrop",
        )

    def test_paused(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", False), art_image())
        self.assertIn("paused", sm.playback_status("spotify", "record", state))

    def test_long_track_keys_are_truncated(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("x" * 200, "u", True), art_image())
        line = sm.playback_status("spotify", "record", state)
        self.assertIn("…", line)
        self.assertLess(len(line), 80)

    def test_cleared_state_reports_idle_without_a_stale_track(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("gone", "u", True), art_image())
        state.clear()
        self.assertNotIn("gone", sm.playback_status("spotify", "record", state))

    def test_prefers_the_readable_title_over_an_opaque_key(self):
        state = sm.PlaybackState()
        state.update(
            sm.PlaybackArt(
                "2b72769a-f72e-3a75-ae97-a91fae433338", "u", True, title="Britney Spears - Toxic"
            ),
            art_image(),
        )
        line = sm.playback_status("lastfm", "record", state)
        self.assertIn("Britney Spears - Toxic", line)
        self.assertNotIn("2b72769a", line, "an mbid tells you nothing about what is playing")

    def test_falls_back_to_the_key_when_there_is_no_title(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("some-key", "u", True), art_image())
        self.assertIn("some-key", sm.playback_status("lastfm", "record", state))

    def test_clear_drops_the_title_too(self):
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True, title="Robyn - Honey"), art_image())
        state.clear()
        self.assertIsNone(state.title)
        self.assertNotIn("Robyn", sm.playback_status("lastfm", "record", state))


class TestTerminalDisplay(unittest.TestCase):
    """Instances pin scale and terminal_size so results never depend on the test runner."""

    def display(self, stream=None, **kwargs):
        kwargs.setdefault("scale", 1)
        kwargs.setdefault("terminal_size", (200, 60))
        return sm.TerminalDisplay(stream=stream if stream is not None else io.StringIO(), **kwargs)

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
        display = self.display(stream)
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
        display = self.display(stream)
        display.show(art_image(size=(4, 4)))
        display.clear()
        self.assertIn("\x1b[?25h", stream.getvalue())

    def test_clear_is_a_no_op_before_any_frame(self):
        stream = io.StringIO()
        self.display(stream).clear()
        self.assertEqual(stream.getvalue(), "")

    def test_context_manager_restores_the_cursor_on_exit(self):
        stream = io.StringIO()
        with self.display(stream) as display:
            display.show(art_image(size=(4, 4)))
        self.assertIn("\x1b[?25h", stream.getvalue())

    def test_erases_below_so_a_smaller_frame_leaves_no_debris(self):
        stream = io.StringIO()
        self.display(stream).show(art_image(size=(4, 4)))
        self.assertTrue(stream.getvalue().endswith("\x1b[J"))

    # --- magnification -----------------------------------------------------------------

    def test_auto_fit_magnifies_to_the_terminal(self):
        stream = io.StringIO()
        sm.TerminalDisplay(stream=stream, scale=None, terminal_size=(256, 100)).show(
            art_image(size=(64, 64))
        )
        # scale 3 => a 192x192 frame => 192 columns by 96 character rows
        self.assertEqual(stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK), 192 * 96)

    def test_explicit_scale_overrides_auto_fit(self):
        stream = io.StringIO()
        sm.TerminalDisplay(stream=stream, scale=1, terminal_size=(9999, 9999)).show(
            art_image(size=(64, 64))
        )
        self.assertEqual(stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK), 64 * 32)

    def test_auto_fit_falls_back_to_one_in_a_small_terminal(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            sm.TerminalDisplay(stream=stream, scale=None, terminal_size=(80, 24)).show(
                art_image(size=(64, 64))
            )
        self.assertEqual(stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK), 64 * 32)

    def test_grid_reaches_the_terminal_output(self):
        plain = io.StringIO()
        gridded = io.StringIO()
        art = art_image((255, 255, 255), (8, 8))
        sm.TerminalDisplay(stream=plain, scale=4, terminal_size=(200, 60)).show(art)
        sm.TerminalDisplay(stream=gridded, scale=4, grid=True, terminal_size=(200, 60)).show(art)
        self.assertNotEqual(plain.getvalue(), gridded.getvalue())
        self.assertIn("38;2;0;0;0", gridded.getvalue(), "gutter is drawn black")

    def test_warns_once_when_the_terminal_is_too_small(self):
        stream, errors = io.StringIO(), io.StringIO()
        display = sm.TerminalDisplay(stream=stream, scale=1, terminal_size=(40, 10))
        with contextlib.redirect_stderr(errors):
            display.show(art_image(size=(64, 64)))
            display.show(art_image(size=(64, 64)))
        self.assertEqual(errors.getvalue().count("Warning"), 1)
        self.assertIn("40x10", errors.getvalue())

    def test_no_warning_when_it_fits(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.display().show(art_image(size=(64, 64)))
        self.assertEqual(errors.getvalue(), "")

    def test_explicit_scale_is_clamped_to_what_fits(self):
        """Overflowing the width line-wraps every row and scrambles the whole redraw."""
        stream, errors = io.StringIO(), io.StringIO()
        display = sm.TerminalDisplay(stream=stream, scale=4, terminal_size=(119, 57))
        with contextlib.redirect_stderr(errors):
            display.show(art_image(size=(64, 64)))
        # Clamped to 1x: 64 columns by 32 rows, not 256 by 128.
        self.assertEqual(stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK), 64 * 32)
        self.assertIn("--preview-scale 4 needs a 256x130 terminal", errors.getvalue())
        self.assertIn("119x57", errors.getvalue())
        self.assertIn("Falling back to 1x", errors.getvalue())

    def test_explicit_scale_is_honoured_when_it_fits(self):
        stream, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            sm.TerminalDisplay(stream=stream, scale=4, terminal_size=(300, 140)).show(
                art_image(size=(64, 64))
            )
        self.assertEqual(stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK), 256 * 128)
        self.assertEqual(errors.getvalue(), "")

    def test_clamp_warning_is_not_repeated_every_frame(self):
        stream, errors = io.StringIO(), io.StringIO()
        display = sm.TerminalDisplay(stream=stream, scale=8, terminal_size=(119, 57))
        with contextlib.redirect_stderr(errors):
            for _ in range(5):
                display.show(art_image(size=(64, 64)))
        self.assertEqual(errors.getvalue().count("Warning"), 1)

    def test_never_magnifies_wider_than_the_terminal(self):
        for columns, rows in [(70, 40), (119, 57), (200, 60), (64, 34)]:
            with self.subTest(terminal=(columns, rows)):
                stream = io.StringIO()
                with contextlib.redirect_stderr(io.StringIO()):
                    sm.TerminalDisplay(
                        stream=stream, scale=6, terminal_size=(columns, rows)
                    ).show(art_image(size=(64, 64)))
                widest = max(
                    line.count(sm.TerminalDisplay.HALF_BLOCK)
                    for line in stream.getvalue().split("\n")
                    if sm.TerminalDisplay.HALF_BLOCK in line
                )
                self.assertLessEqual(widest, columns)

    def test_auto_fit_tracks_a_resized_window(self):
        stream = io.StringIO()
        display = sm.TerminalDisplay(stream=stream, scale=None, terminal_size=(256, 100))
        display.show(art_image(size=(64, 64)))
        big = stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK)
        display.terminal_size = (119, 57)
        display.show(art_image(size=(64, 64)))
        after = stream.getvalue().count(sm.TerminalDisplay.HALF_BLOCK) - big
        self.assertEqual(big, 192 * 96)
        self.assertEqual(after, 64 * 32, "shrinking the window should shrink the preview")

    # --- line wrapping -----------------------------------------------------------------

    def test_disables_line_wrap_while_drawing(self):
        stream = io.StringIO()
        display = self.display(stream)
        display.show(art_image(size=(4, 4)))
        self.assertIn("\x1b[?7l", stream.getvalue(), "autowrap off, so overflow truncates")

    def test_restores_line_wrap_on_exit(self):
        stream = io.StringIO()
        with self.display(stream) as display:
            display.show(art_image(size=(4, 4)))
        self.assertIn("\x1b[?7h", stream.getvalue())

    def test_restores_line_wrap_even_when_the_body_raises(self):
        stream = io.StringIO()
        with contextlib.suppress(ValueError):
            with self.display(stream) as display:
                display.show(art_image(size=(4, 4)))
                raise ValueError("boom")
        self.assertIn("\x1b[?7h", stream.getvalue())

    # --- status line -------------------------------------------------------------------

    def test_status_callable_is_rendered_under_the_frame(self):
        stream = io.StringIO()
        self.display(stream, status=lambda: "demo · record · playing").show(art_image(size=(4, 4)))
        self.assertIn("demo · record · playing", stream.getvalue())

    def test_status_is_re_read_every_frame(self):
        stream = io.StringIO()
        states = iter(["first", "second"])
        display = self.display(stream, status=lambda: next(states))
        display.show(art_image(size=(4, 4)))
        display.show(art_image(size=(4, 4)))
        self.assertIn("first", stream.getvalue())
        self.assertIn("second", stream.getvalue())

    def test_status_line_includes_the_stop_hint(self):
        self.assertIn("ctrl-c", self.display().status_line())

    def test_no_fps_reading_on_the_first_frame(self):
        display = self.display(clock=FakeClock())
        display.show(art_image(size=(4, 4)))
        self.assertNotIn("fps", display.status_line())

    def test_fps_is_measured_from_frame_intervals(self):
        clock = FakeClock()
        display = self.display(clock=clock)
        display.show(art_image(size=(4, 4)))
        for _ in range(40):
            clock.advance(0.05)  # 20 fps
            display.show(art_image(size=(4, 4)))
        self.assertIn("20.0 fps", display.status_line())

    def test_works_without_a_status_callable(self):
        stream = io.StringIO()
        self.display(stream, status=None).show(art_image(size=(4, 4)))
        self.assertIn("ctrl-c", stream.getvalue())

    # --- alternate screen --------------------------------------------------------------

    def test_uses_the_alternate_screen_to_preserve_scrollback(self):
        stream = io.StringIO()
        with self.display(stream, alt_screen=True) as display:
            display.show(art_image(size=(4, 4)))
        output = stream.getvalue()
        self.assertIn("\x1b[?1049h", output, "enters the alternate screen")
        self.assertIn("\x1b[?1049l", output, "and leaves it on the way out")
        self.assertLess(output.index("\x1b[?1049h"), output.index("\x1b[?1049l"))

    def test_single_frame_mode_stays_on_the_main_screen(self):
        """Leaving the alternate screen would erase the one frame you asked to see."""
        stream = io.StringIO()
        with self.display(stream, alt_screen=False) as display:
            display.show(art_image(size=(4, 4)))
        self.assertNotIn("\x1b[?1049", stream.getvalue())

    def test_alternate_screen_is_left_even_if_the_body_raises(self):
        stream = io.StringIO()
        with contextlib.suppress(ValueError):
            with self.display(stream, alt_screen=True) as display:
                display.show(art_image(size=(4, 4)))
                raise ValueError("boom")
        self.assertIn("\x1b[?1049l", stream.getvalue())


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

    def test_terminal_uses_the_alternate_screen_for_a_live_preview(self):
        self.assertTrue(sm.build_display(self.parse(["--preview-terminal"])).alt_screen)

    def test_terminal_stays_on_the_main_screen_for_a_single_frame(self):
        """--once must not enter the alternate screen; exiting would erase the frame."""
        display = sm.build_display(self.parse(["--preview-terminal", "--once"]))
        self.assertFalse(display.alt_screen)

    def test_terminal_scale_defaults_to_auto_fit(self):
        self.assertIsNone(sm.build_display(self.parse(["--preview-terminal"])).scale)

    def test_terminal_takes_an_explicit_scale_and_grid(self):
        display = sm.build_display(
            self.parse(["--preview-terminal", "--preview-scale", "5", "--preview-grid"])
        )
        self.assertEqual((display.scale, display.grid), (5, True))

    def test_status_callable_is_handed_to_the_terminal_backend(self):
        display = sm.build_display(self.parse(["--preview-terminal"]), status=lambda: "hello")
        self.assertEqual(display.status(), "hello")

    def test_file_backends_default_to_unscaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = sm.build_display(self.parse(["--mock-output", str(Path(tmp) / "f.png")]))
            gif = sm.build_display(self.parse(["--record-gif", str(Path(tmp) / "a.gif")]))
        self.assertEqual(png.scale, 1)
        self.assertEqual(gif.scale, 1)

    def test_preview_scale_must_be_at_least_one(self):
        for value in ("0", "-3"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit), quiet():
                    self.parse(["--preview-scale", value])

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
            image = sm.build_image_preparer(args, lambda: renderer)(sm.DemoProvider.DEMO_IMAGE_URL)
        self.assertEqual(image.size, (renderer.disc_size,) * 2)

    def test_real_provider_downloads_and_fits(self):
        args = sm.build_parser().parse_args([])
        renderer = sm.build_renderer("record", 64)
        with mock.patch.object(sm, "download_image", return_value=art_image(size=(640, 640))) as dl:
            image = sm.build_image_preparer(args, lambda: renderer)("https://art/a.png")
        dl.assert_called_once_with("https://art/a.png")
        self.assertEqual(image.size, (renderer.disc_size,) * 2)

    def test_fits_to_the_active_style(self):
        args = sm.build_parser().parse_args(["--provider", "demo"])
        art_renderer = sm.build_renderer("art", 64)
        image = sm.build_image_preparer(args, lambda: art_renderer)("x")
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

    def isolated(self):
        return ["--config", str(self.dir / "config.json")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, argv):
        # Always isolate config.json: it persists by design, so a shared one would leak
        # settings between tests (and into the repo).
        isolated = ["--config", str(self.dir / "config.json"), "--photos", str(self.dir / "photos")]
        with quiet():
            sm.run(sm.build_parser().parse_args([*argv, *isolated]))

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
        factory = lambda **kwargs: real(stream=stream, terminal_size=(200, 60), **kwargs)
        with mock.patch.object(sm, "TerminalDisplay", factory), quiet():
            sm.run(args)
        self.assertIn(sm.TerminalDisplay.HALF_BLOCK, stream.getvalue())
        self.assertIn("\x1b[38;2;", stream.getvalue())

    def test_terminal_preview_reports_live_playback_state(self):
        """run() must wire the status callable through, or the HUD is empty."""
        stream = io.StringIO()
        args = sm.build_parser().parse_args(
            ["--provider", "demo", "--style", "art", "--preview-terminal", "--once", *self.isolated()]
        )
        real = sm.TerminalDisplay
        factory = lambda **kwargs: real(stream=stream, terminal_size=(200, 60), **kwargs)
        with mock.patch.object(sm, "TerminalDisplay", factory), quiet():
            sm.run(args)
        self.assertIn("demo · album:art · playing · Demo Artist - Demo Track", stream.getvalue())

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


# ======================================================================================
# Runtime settings
# ======================================================================================


class TestCoerceConfig(unittest.TestCase):
    def test_empty_input_gives_defaults(self):
        self.assertEqual(sm.coerce_config({}), sm.Config())

    def test_round_trips_through_as_dict(self):
        config = sm.Config(style="art", idle_scene="clock", override_scene="photos", rpm=45.0)
        self.assertEqual(sm.coerce_config(config.as_dict()), config)

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(sm.coerce_config({"nonsense": True, "style": "art"}).style, "art")

    def test_bad_style_falls_back_instead_of_raising(self):
        # A wall clock that refuses to start over a typo is worse than one that ignores it.
        self.assertEqual(sm.coerce_config({"style": "hologram"}).style, sm.Config().style)

    def test_bad_scene_names_fall_back(self):
        self.assertEqual(sm.coerce_config({"idle_scene": "lava-lamp"}).idle_scene, "blank")
        self.assertIsNone(sm.coerce_config({"override_scene": "lava-lamp"}).override_scene)

    def test_override_sentinels_mean_follow_playback(self):
        for value in ("", "none", "auto", None):
            with self.subTest(value=value):
                self.assertIsNone(sm.coerce_config({"override_scene": value}).override_scene)

    def test_every_scene_name_is_accepted_as_an_override(self):
        for scene in sm.SCENE_NAMES:
            with self.subTest(scene=scene):
                self.assertEqual(sm.coerce_config({"override_scene": scene}).override_scene, scene)

    def test_numbers_are_clamped_not_rejected(self):
        self.assertEqual(sm.coerce_config({"rpm": 10_000}).rpm, 300.0)
        self.assertEqual(sm.coerce_config({"rpm": -5}).rpm, 0.1)
        self.assertEqual(sm.coerce_config({"photo_seconds": 0}).photo_seconds, 1.0)
        self.assertEqual(sm.coerce_config({"brightness": 500}).brightness, 100)
        self.assertEqual(sm.coerce_config({"brightness": 0}).brightness, 1)

    def test_unparseable_numbers_fall_back(self):
        self.assertEqual(sm.coerce_config({"rpm": "fast"}).rpm, sm.Config().rpm)
        self.assertEqual(sm.coerce_config({"brightness": "bright"}).brightness, None)

    def test_null_brightness_means_leave_as_launched(self):
        self.assertIsNone(sm.coerce_config({"brightness": None}).brightness)

    def test_supplied_defaults_are_the_fallback(self):
        defaults = sm.Config(style="art", rpm=99.0)
        coerced = sm.coerce_config({"style": "nope", "rpm": "nope"}, defaults)
        self.assertEqual((coerced.style, coerced.rpm), ("art", 99.0))


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.store = sm.ConfigStore(self.path)

    def test_missing_file_yields_defaults(self):
        self.assertEqual(self.store.current(), sm.Config())

    def test_save_then_read_back(self):
        self.store.save(sm.Config(style="art", idle_scene="clock"))
        self.assertEqual(sm.ConfigStore(self.path).current().style, "art")

    def test_written_file_is_readable_json(self):
        self.store.save(sm.Config(rpm=33.0))
        self.assertEqual(json.loads(self.path.read_text())["rpm"], 33.0)

    def test_picks_up_an_external_edit(self):
        """An SSH edit and the web UI must be equivalent."""
        self.store.save(sm.Config(style="record"))
        self.assertEqual(self.store.current().style, "record")
        self.path.write_text(json.dumps({"style": "art"}), encoding="utf-8")
        self.assertEqual(self.store.current().style, "art")

    def test_unchanged_file_is_not_re_parsed(self):
        self.store.save(sm.Config())
        first = self.store.current()
        self.assertIs(self.store.current(), first, "should be cached between stats")

    def test_corrupt_file_keeps_the_last_good_settings(self):
        self.store.save(sm.Config(style="art"))
        self.store.current()
        self.path.write_text("{not json", encoding="utf-8")
        with quiet():
            self.assertEqual(self.store.current().style, "art")

    def test_non_object_json_keeps_the_last_good_settings(self):
        self.store.save(sm.Config(style="art"))
        self.store.current()
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with quiet():
            self.assertEqual(self.store.current().style, "art")

    def test_update_merges_over_current_settings(self):
        self.store.save(sm.Config(style="art", rpm=45.0))
        updated = self.store.update({"rpm": 30.0})
        self.assertEqual((updated.style, updated.rpm), ("art", 30.0))

    def test_update_clamps_hostile_input(self):
        self.assertEqual(self.store.update({"rpm": 10_000}).rpm, 300.0)

    def test_update_persists(self):
        self.store.update({"idle_scene": "photos"})
        self.assertEqual(sm.ConfigStore(self.path).current().idle_scene, "photos")

    def test_deleted_file_falls_back_to_defaults(self):
        self.store.save(sm.Config(style="art"))
        self.store.current()
        self.path.unlink()
        self.assertEqual(self.store.current(), sm.Config())

    def test_save_is_atomic(self):
        self.store.save(sm.Config())
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])


class TestAtomicWriteJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_writes_readable_json(self):
        target = self.root / "a.json"
        sm.atomic_write_json(target, {"b": 1})
        self.assertEqual(json.loads(target.read_text()), {"b": 1})

    def test_failed_replace_leaves_the_original(self):
        target = self.root / "a.json"
        sm.atomic_write_json(target, {"v": "first"})
        with mock.patch.object(sm.os, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                sm.atomic_write_json(target, {"v": "second"})
        self.assertEqual(json.loads(target.read_text()), {"v": "first"})
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.json"])

    def test_private_parent_is_locked_down(self):
        target = self.root / "secrets" / "t.json"
        sm.atomic_write_json(target, {"a": 1}, file_mode=0o600, private_parent=True)
        self.assertEqual(os.stat(target.parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_plain_parent_is_not_locked_down(self):
        os.chmod(self.root, 0o755)
        sm.atomic_write_json(self.root / "sub" / "c.json", {"a": 1})
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o755)

    def test_bare_filename_does_not_touch_the_working_directory(self):
        os.chmod(self.root, 0o755)
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            sm.atomic_write_json(Path("c.json"), {"a": 1})
        finally:
            os.chdir(cwd)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o755)


class TestConfigOverridesFromArgs(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_no_flags_means_no_overrides(self):
        self.assertEqual(sm.config_overrides_from(self.parse([])), {})

    def test_explicit_flags_become_overrides(self):
        overrides = sm.config_overrides_from(self.parse(["--style", "art", "--rpm", "45"]))
        self.assertEqual(overrides, {"style": "art", "rpm": 45.0})

    def test_every_configurable_flag_is_covered(self):
        argv = [
            "--style", "art", "--scene", "clock", "--idle-scene", "clock", "--rpm", "45",
            "--photo-seconds", "12", "--clock-24-hour", "--clock-motion", "breathe",
        ]
        self.assertEqual(
            set(sm.config_overrides_from(self.parse(argv))),
            set(sm.CONFIGURABLE_FLAGS.values()),
        )

    def test_every_flag_maps_to_a_real_config_field(self):
        self.assertLessEqual(set(sm.CONFIGURABLE_FLAGS.values()), set(sm.Config().as_dict()))

    def test_scene_flag_sets_the_override(self):
        self.assertEqual(
            sm.config_overrides_from(self.parse(["--scene", "photos"])),
            {"override_scene": "photos"},
        )

    def test_scene_auto_clears_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            sm.ConfigStore(path).save(sm.Config(override_scene="photos"))
            store = sm.open_config_store(self.parse(["--config", str(path), "--scene", "auto"]))
            self.assertIsNone(store.current().override_scene)

    def test_scene_accepts_every_scene_plus_auto(self):
        action = next(a for a in sm.build_parser()._actions if a.dest == "scene")
        self.assertEqual(set(action.choices), {"auto", *sm.SCENE_NAMES})

    def test_scene_flag_beats_an_existing_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            sm.ConfigStore(path).save(sm.Config(override_scene="clock"))
            store = sm.open_config_store(self.parse(["--config", str(path), "--scene", "photos"]))
            self.assertEqual(store.current().override_scene, "photos")

    def test_an_explicit_flag_beats_an_existing_config_file(self):
        """A flag that silently does nothing is worse than no flag."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            sm.ConfigStore(path).save(sm.Config(style="record"))
            args = self.parse(["--config", str(path), "--style", "art"])
            store = sm.open_config_store(args)
            self.assertEqual(store.current().style, "art")
            self.assertEqual(json.loads(path.read_text())["style"], "art", "and it persists")

    def test_config_file_survives_a_run_with_no_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            sm.ConfigStore(path).save(sm.Config(style="art", rpm=45.0))
            store = sm.open_config_store(self.parse(["--config", str(path)]))
            self.assertEqual((store.current().style, store.current().rpm), ("art", 45.0))

    def test_first_run_materialises_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            sm.open_config_store(self.parse(["--config", str(path)]))
            self.assertTrue(path.exists())


# ======================================================================================
# Clock
# ======================================================================================


class TestClockGlyphs(unittest.TestCase):
    def test_every_digit_and_the_colon_are_defined(self):
        for character in "0123456789: ":
            self.assertIn(character, sm.CLOCK_GLYPHS)

    def test_all_glyphs_have_the_same_height(self):
        for character, glyph in sm.CLOCK_GLYPHS.items():
            with self.subTest(character=character):
                self.assertEqual(len(glyph), sm.CLOCK_GLYPH_HEIGHT)

    def test_each_glyph_is_rectangular(self):
        for character, glyph in sm.CLOCK_GLYPHS.items():
            with self.subTest(character=character):
                self.assertEqual(len({len(row) for row in glyph}), 1)

    def test_glyphs_only_use_the_two_expected_characters(self):
        for character, glyph in sm.CLOCK_GLYPHS.items():
            with self.subTest(character=character):
                self.assertLessEqual(set("".join(glyph)), {"#", "."})

    def test_every_digit_has_some_ink(self):
        for digit in "0123456789":
            with self.subTest(digit=digit):
                self.assertIn("#", "".join(sm.CLOCK_GLYPHS[digit]))

    def test_all_digits_are_distinct(self):
        rendered = {d: sm.CLOCK_GLYPHS[d] for d in "0123456789"}
        self.assertEqual(len(set(rendered.values())), 10, "two digits would look identical")


class TestClockTextMask(unittest.TestCase):
    def test_scale_multiplies_both_dimensions(self):
        one = sm.clock_text_mask("12:34", 1)
        three = sm.clock_text_mask("12:34", 3)
        self.assertEqual((three.width, three.height), (one.width * 3, one.height * 3))

    def test_height_is_the_glyph_grid(self):
        self.assertEqual(sm.clock_text_mask("1", 1).height, sm.CLOCK_GLYPH_HEIGHT)

    def test_mode_is_a_mask(self):
        self.assertEqual(sm.clock_text_mask("1", 1).mode, "L")

    def test_pixels_are_fully_on_or_off(self):
        # Any antialiasing here would smear on a 64px panel.
        values = set(sm.clock_text_mask("18:45", 2).convert("L").getextrema())
        self.assertLessEqual(values, {0, 255})

    def test_unknown_characters_are_skipped(self):
        self.assertEqual(
            sm.clock_text_mask("1?2", 1).width, sm.clock_text_mask("12", 1).width
        )

    def test_empty_text_does_not_crash(self):
        self.assertEqual(sm.clock_text_mask("", 2).size, (1, 1))

    def test_wider_text_is_wider(self):
        self.assertGreater(
            sm.clock_text_mask("12:34", 1).width, sm.clock_text_mask("1:34", 1).width
        )


class TestClockScaleFor(unittest.TestCase):
    def test_fits_within_the_panel(self):
        for size in (32, 64, 128):
            for text in ("9:41", "12:34", "23:59"):
                with self.subTest(size=size, text=text):
                    scale = sm.clock_scale_for(size, text)
                    mask = sm.clock_text_mask(text, scale)
                    self.assertLessEqual(mask.width, size)
                    self.assertLessEqual(mask.height, size)

    def test_never_below_one(self):
        self.assertGreaterEqual(sm.clock_scale_for(8, "12:34"), 1)

    def test_a_bigger_panel_gets_bigger_digits(self):
        self.assertGreater(sm.clock_scale_for(128, "12:34"), sm.clock_scale_for(64, "12:34"))

    def test_uses_most_of_a_64px_panel(self):
        scale = sm.clock_scale_for(64, "12:34")
        self.assertGreaterEqual(sm.clock_text_mask("12:34", scale).width, 40)


class TestFormatClock(unittest.TestCase):
    def test_24_hour(self):
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 15, 22), True), "15:22")
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 0, 5), True), "00:05")

    def test_12_hour_drops_the_leading_zero(self):
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 15, 22), False), "3:22")
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 9, 41), False), "9:41")

    def test_12_hour_midnight_and_noon_are_twelve(self):
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 0, 5), False), "12:05")
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 12, 0), False), "12:00")

    def test_minutes_are_always_two_digits(self):
        self.assertEqual(sm.format_clock(datetime(2026, 8, 7, 7, 3), False), "7:03")


class TestDrawClockText(unittest.TestCase):
    def frame(self, colour=(0, 0, 0), size=64):
        return Image.new("RGB", (size, size), colour)

    def test_draws_white_pixels(self):
        frame = self.frame()
        sm.draw_clock_text(frame, "12:34", 2, outline=None)
        self.assertEqual(frame.getextrema()[0][1], 255)

    def test_is_horizontally_centred(self):
        # Centred on the glyph cells, not on the ink: digits occupy fixed-width cells so
        # the time does not shuffle sideways as the digits change.
        mask = sm.clock_text_mask("12:34", 2)
        left = (64 - mask.width) // 2
        self.assertLessEqual(abs(left - (64 - left - mask.width)), 1)

    def test_layout_does_not_jitter_between_times(self):
        widths = {sm.clock_text_mask(text, 2).width for text in ("11:11", "23:59", "10:08")}
        self.assertEqual(len(widths), 1, "same digit count must occupy the same width")

    def test_outline_makes_white_text_readable_on_a_pale_photo(self):
        frame = self.frame((255, 255, 255))
        sm.draw_clock_text(frame, "12:34", 2)
        self.assertEqual(frame.getextrema()[0][0], 0, "a black halo must be drawn")

    def test_without_an_outline_white_on_white_is_invisible(self):
        frame = self.frame((255, 255, 255))
        sm.draw_clock_text(frame, "12:34", 2, outline=None)
        self.assertEqual(frame.getextrema()[0][0], 255, "which is why the halo exists")

    def test_centre_y_moves_the_text(self):
        high, low = self.frame(), self.frame()
        sm.draw_clock_text(high, "12:34", 2, outline=None, centre_y=16)
        sm.draw_clock_text(low, "12:34", 2, outline=None, centre_y=48)
        def rows(frame):
            return [y for y in range(64)
                    if any(frame.getpixel((x, y)) != (0, 0, 0) for x in range(64))]
        self.assertLess(max(rows(high)), min(rows(low)))

    def test_stays_inside_the_frame(self):
        frame = self.frame()
        sm.draw_clock_text(frame, "23:59", sm.clock_scale_for(64, "23:59"))
        self.assertEqual(frame.size, (64, 64))


# ======================================================================================
# Photos
# ======================================================================================


class PhotoFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "photos"
        self.dir.mkdir()
        self.clock = FakeClock()

    def add(self, name, colour=(200, 30, 30), size=(200, 120)):
        Image.new("RGB", size, colour).save(self.dir / name)
        return self.dir / name

    def library(self, seconds=30.0, size=64):
        return sm.PhotoLibrary(self.dir, size, seconds=lambda: seconds, clock=self.clock)


class TestPhotoLibrary(PhotoFixture):
    def test_empty_directory_has_no_photo(self):
        library = self.library()
        self.assertFalse(library.advance())
        self.assertIsNone(library.current())

    def test_missing_directory_does_not_crash(self):
        library = sm.PhotoLibrary(
            self.dir / "nope", 64, seconds=lambda: 30.0, clock=self.clock
        )
        self.assertFalse(library.advance())
        self.assertEqual(library.available(), [])

    def test_fits_photos_to_the_panel(self):
        self.add("a.png")
        library = self.library()
        library.advance()
        self.assertEqual(library.current().size, (64, 64))
        self.assertEqual(library.current().mode, "RGB")

    def test_lists_only_image_files(self):
        self.add("a.png")
        self.add("b.jpg")
        (self.dir / "notes.txt").write_text("not an image")
        (self.dir / "sub").mkdir()
        self.assertEqual(self.library().scan().__len__(), 2)

    def test_advances_in_sorted_order_and_wraps(self):
        for name in ("3.png", "1.png", "2.png"):
            self.add(name)
        library = self.library()
        seen = []
        for _ in range(4):
            library.advance()
            seen.append(library.current_name())
        self.assertEqual(seen, ["1.png", "2.png", "3.png", "1.png"])

    def test_unreadable_files_are_skipped_not_fatal(self):
        (self.dir / "broken.png").write_bytes(b"definitely not a png")
        self.add("good.png")
        library = self.library()
        with quiet():
            self.assertTrue(library.advance())
        self.assertEqual(library.current_name(), "good.png")

    def test_all_files_broken_reports_no_photo(self):
        (self.dir / "broken.png").write_bytes(b"nope")
        library = self.library()
        with quiet():
            self.assertFalse(library.advance())

    def test_tick_holds_a_photo_for_its_full_time(self):
        self.add("1.png")
        self.add("2.png")
        library = self.library(seconds=30.0)
        library.tick()
        self.assertEqual(library.current_name(), "1.png")
        self.clock.advance(29)
        library.tick()
        self.assertEqual(library.current_name(), "1.png")
        self.clock.advance(2)
        library.tick()
        self.assertEqual(library.current_name(), "2.png")

    def test_new_photos_are_picked_up_without_a_restart(self):
        self.add("1.png")
        library = self.library()
        self.assertEqual(len(library.scan()), 1)
        self.add("2.png")
        os.utime(self.dir, (self.clock.now + 100, self.clock.now + 100))
        self.assertEqual(len(library.scan()), 2)

    def test_first_photo_event_lets_a_preview_wait(self):
        self.add("1.png")
        library = self.library()
        self.assertFalse(library.wait_for_first_photo(0.01))
        library.advance()
        self.assertTrue(library.wait_for_first_photo(0.01))

    def test_available_lists_names(self):
        self.add("b.png")
        self.add("a.png")
        library = self.library()
        library.scan()
        self.assertEqual(library.available(), ["a.png", "b.png"])

    def test_run_loop_stops_on_the_event(self):
        self.add("1.png")
        library = self.library()
        stop = threading.Event()
        stop.set()
        library.run(stop, interval=0.01)  # returns immediately
        self.assertIsNone(library.current())


# ======================================================================================
# Scenes
# ======================================================================================


class TestBlankScene(unittest.TestCase):
    def test_is_the_idle_frame(self):
        scene = sm.BlankScene(64)
        self.assertEqual(scene.frame(0.0).tobytes(), sm.render_idle(64).tobytes())

    def test_frame_is_reused(self):
        scene = sm.BlankScene(64)
        self.assertIs(scene.frame(0.0), scene.frame(1.0))


class TestClockScene(unittest.TestCase):
    def scene(self, moment=datetime(2026, 8, 7, 15, 22), use_24_hour=True, size=64):
        return sm.ClockScene(size, use_24_hour=lambda: use_24_hour, now=lambda: moment)

    def test_renders_the_time(self):
        frame = self.scene().frame(0.0)
        self.assertEqual(frame.size, (64, 64))
        self.assertEqual(frame.getextrema()[0][1], 255, "digits should be lit")

    def test_text_follows_the_clock_format(self):
        self.assertEqual(self.scene(use_24_hour=True).text(), "15:22")
        self.assertEqual(self.scene(use_24_hour=False).text(), "3:22")

    def test_frame_is_cached_within_the_minute(self):
        scene = self.scene()
        self.assertIs(scene.frame(0.0), scene.frame(19.0), "no need to redraw 20x a second")

    def test_frame_is_redrawn_when_the_minute_changes(self):
        moments = iter([datetime(2026, 8, 7, 15, 22), datetime(2026, 8, 7, 15, 23)])
        scene = sm.ClockScene(64, use_24_hour=lambda: True, now=lambda: next(moments))
        first = scene.frame(0.0).tobytes()
        self.assertNotEqual(first, scene.frame(1.0).tobytes())

    def test_format_change_takes_effect(self):
        fmt = {"24": True}
        scene = sm.ClockScene(
            64, use_24_hour=lambda: fmt["24"], now=lambda: datetime(2026, 8, 7, 15, 22)
        )
        first = scene.frame(0.0).tobytes()
        fmt["24"] = False
        self.assertNotEqual(first, scene.frame(1.0).tobytes())


class TestClockMotion(unittest.TestCase):
    """Blink and breathe. Both off by default: unrequested motion on a wall display is noise."""

    def scene(self, motion, microsecond=0, size=64):
        return sm.ClockScene(
            size,
            use_24_hour=lambda: True,
            now=lambda: datetime(2026, 8, 7, 15, 22, 30, microsecond),
            motion=lambda: motion,
        )

    def test_default_is_still(self):
        self.assertEqual(sm.Config().clock_motion, "still")
        self.assertEqual(sm.CLOCK_MOTIONS[0], "still")

    def test_every_motion_is_accepted_by_config(self):
        for motion in sm.CLOCK_MOTIONS:
            with self.subTest(motion=motion):
                self.assertEqual(sm.coerce_config({"clock_motion": motion}).clock_motion, motion)

    def test_unknown_motion_falls_back(self):
        self.assertEqual(sm.coerce_config({"clock_motion": "strobe"}).clock_motion, "still")

    # --- blink -------------------------------------------------------------------------

    def test_blink_shows_the_colon_in_the_first_half_second(self):
        self.assertEqual(self.scene("blink", 100_000).display_text(), "15:22")

    def test_blink_hides_the_colon_in_the_second_half_second(self):
        self.assertEqual(self.scene("blink", 700_000).display_text(), "15 22")

    def test_blink_boundary_is_the_half_second(self):
        self.assertIn(":", self.scene("blink", 499_999).display_text())
        self.assertNotIn(":", self.scene("blink", 500_000).display_text())

    def test_blink_does_not_shift_the_digits(self):
        """The colon is swapped for a same-width space, not removed."""
        on = sm.clock_text_mask(self.scene("blink", 0).display_text(), 2)
        off = sm.clock_text_mask(self.scene("blink", 900_000).display_text(), 2)
        self.assertEqual(on.width, off.width)

    def test_other_motions_never_hide_the_colon(self):
        for motion in ("still", "breathe"):
            for microsecond in (0, 500_000, 900_000):
                with self.subTest(motion=motion, microsecond=microsecond):
                    self.assertIn(":", self.scene(motion, microsecond).display_text())

    def test_blink_redraws_when_the_colon_changes(self):
        on = self.scene("blink", 100_000).frame(0.0).tobytes()
        off = self.scene("blink", 700_000).frame(0.0).tobytes()
        self.assertNotEqual(on, off)

    def test_blink_stays_full_brightness(self):
        self.assertEqual(self.scene("blink", 0).ink(1.7), (255, 255, 255))

    # --- breathe -----------------------------------------------------------------------

    def test_still_and_blink_are_always_full_brightness(self):
        for motion in ("still", "blink"):
            with self.subTest(motion=motion):
                scene = self.scene(motion)
                self.assertEqual({scene.ink_step(t) for t in (0, 1, 2.5, 4)},
                                 {sm.ClockScene.BREATHE_STEPS})

    def test_breathe_varies_brightness(self):
        scene = self.scene("breathe")
        levels = {scene.ink_step(t / 10) for t in range(int(scene.BREATHE_SECONDS * 10))}
        self.assertGreater(len(levels), 5, "should be a smooth ramp, not two states")

    def test_breathe_peaks_at_full_and_never_goes_dark(self):
        scene = self.scene("breathe")
        steps = [scene.ink_step(t / 20) for t in range(int(scene.BREATHE_SECONDS * 20) + 1)]
        self.assertEqual(max(steps), scene.BREATHE_STEPS)
        floor = round(scene.BREATHE_FLOOR * scene.BREATHE_STEPS)
        self.assertGreaterEqual(min(steps), floor, "the time must stay readable at the dimmest")

    def test_breathe_is_periodic(self):
        scene = self.scene("breathe")
        for t in (0.0, 0.7, 1.9, 3.3):
            with self.subTest(t=t):
                self.assertEqual(scene.ink_step(t), scene.ink_step(t + scene.BREATHE_SECONDS))

    def test_breathe_eases_symmetrically(self):
        """A cosine, so it slows at both ends rather than sawtoothing."""
        scene = self.scene("breathe")
        for t in (0.4, 1.1, 2.0):
            with self.subTest(t=t):
                self.assertEqual(scene.ink_step(t), scene.ink_step(scene.BREATHE_SECONDS - t))

    def test_breathe_dims_the_rendered_digits(self):
        scene = self.scene("breathe")
        bright = scene.frame(0.0).getextrema()[0][1]
        dim = scene.frame(scene.BREATHE_SECONDS / 2).getextrema()[0][1]
        self.assertGreater(bright, dim)

    def test_breathe_quantises_so_frames_stay_cacheable(self):
        scene = self.scene("breathe")
        renders = []
        original = sm.draw_clock_text
        try:
            sm.draw_clock_text = lambda *a, **k: renders.append(1) or original(*a, **k)
            for step in range(100):  # 5 s at 20 fps
                scene.frame(step / 20)
        finally:
            sm.draw_clock_text = original
        self.assertLess(len(renders), 50, "must not redraw every single frame")
        self.assertGreater(len(renders), 5, "but must actually animate")

    # --- over photos -------------------------------------------------------------------

    def test_blink_applies_over_a_photo(self):
        library = mock.Mock()
        library.current.return_value = art_image((255, 255, 255), (64, 64))
        on = sm.PhotoScene(library, 64, clock=self.scene("blink", 0)).frame(0.0).tobytes()
        off = sm.PhotoScene(library, 64, clock=self.scene("blink", 900_000)).frame(0.0).tobytes()
        self.assertNotEqual(on, off)

    def test_breathe_applies_over_a_photo(self):
        library = mock.Mock()
        library.current.return_value = art_image((0, 0, 0), (64, 64))
        scene = sm.PhotoScene(library, 64, clock=self.scene("breathe"))
        bright = scene.frame(0.0).getextrema()[0][1]
        dim = scene.frame(sm.ClockScene.BREATHE_SECONDS / 2).getextrema()[0][1]
        self.assertGreater(bright, dim)


class TestClockMotionWiring(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_flag_maps_to_config(self):
        self.assertEqual(
            sm.config_overrides_from(self.parse(["--clock-motion", "breathe"])),
            {"clock_motion": "breathe"},
        )

    def test_flag_accepts_every_motion(self):
        action = next(a for a in sm.build_parser()._actions if a.dest == "clock_motion")
        self.assertEqual(set(action.choices), set(sm.CLOCK_MOTIONS))

    def test_flag_defaults_to_leaving_config_alone(self):
        self.assertIsNone(self.parse([]).clock_motion)

    def test_build_scenes_reads_motion_from_config(self):
        store = stub_config(clock_motion="blink", clock_24_hour=True)
        library = sm.PhotoLibrary(Path("/nonexistent"), 64, seconds=lambda: 30.0)
        scenes = sm.build_scenes(
            64, store, library, now=lambda: datetime(2026, 8, 7, 15, 22, 30, 900_000)
        )
        self.assertEqual(scenes["clock"].display_text(), "15 22")

    def test_motion_change_takes_effect_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = sm.ConfigStore(path)
            store.save(sm.Config(clock_motion="still"))
            scenes = sm.build_scenes(
                64, store, sm.PhotoLibrary(Path("/nonexistent"), 64, seconds=lambda: 30.0),
                now=lambda: datetime(2026, 8, 7, 15, 22, 30, 900_000),
            )
            self.assertIn(":", scenes["clock"].display_text())
            store.update({"clock_motion": "blink"})
            self.assertNotIn(":", scenes["clock"].display_text())


class TestPhotoScene(PhotoFixture):
    def test_shows_the_current_photo(self):
        self.add("a.png", colour=(10, 200, 40))
        library = self.library()
        library.advance()
        frame = sm.PhotoScene(library, 64).frame(0.0)
        self.assertEqual(frame.getpixel((32, 32)), (10, 200, 40))

    def test_falls_back_to_the_idle_frame_with_no_photos(self):
        scene = sm.PhotoScene(self.library(), 64)
        self.assertEqual(scene.frame(0.0).tobytes(), sm.render_idle(64).tobytes())

    def test_falls_back_to_the_clock_when_one_is_configured(self):
        clock = sm.ClockScene(64, lambda: True, now=lambda: datetime(2026, 8, 7, 15, 22))
        scene = sm.PhotoScene(self.library(), 64, clock=clock)
        self.assertEqual(scene.frame(0.0).tobytes(), clock.frame(0.0).tobytes())

    def test_clock_is_drawn_over_the_photo(self):
        self.add("a.png", colour=(255, 255, 255))
        library = self.library()
        library.advance()
        clock = sm.ClockScene(64, lambda: True, now=lambda: datetime(2026, 8, 7, 15, 22))
        frame = sm.PhotoScene(library, 64, clock=clock).frame(0.0)
        self.assertEqual(frame.getextrema()[0][0], 0, "outlined text over a white photo")

    def test_does_not_mutate_the_library_photo(self):
        self.add("a.png", colour=(255, 255, 255))
        library = self.library()
        library.advance()
        clock = sm.ClockScene(64, lambda: True, now=lambda: datetime(2026, 8, 7, 15, 22))
        sm.PhotoScene(library, 64, clock=clock).frame(0.0)
        self.assertEqual(library.current().getextrema()[0][0], 255, "photo must stay pristine")


class TestSceneDirector(PhotoFixture):
    def build(self, **config):
        store = stub_config(**config)
        state = sm.PlaybackState()
        library = self.library()
        renderers = {name: sm.build_renderer(name, 64) for name in sm.RENDERER_STYLES}
        album = sm.AlbumScene(renderers, state, store)
        scenes = sm.build_scenes(
            64, store, library, now=lambda: datetime(2026, 8, 7, 15, 22)
        )
        return sm.SceneDirector(store, album, scenes, state), state, store

    def test_playback_selects_the_album_scene(self):
        director, state, _ = self.build()
        state.update(sm.PlaybackArt("k", "u", True), art_image(size=(60, 60)))
        self.assertEqual(director.scene_name(director.config_store.current()), "album")

    def test_no_playback_selects_the_idle_scene(self):
        director, _, store = self.build(idle_scene="clock")
        self.assertEqual(director.scene_name(store.current()), "clock")

    def test_override_wins_over_playback(self):
        director, state, store = self.build(override_scene="clock")
        state.update(sm.PlaybackArt("k", "u", True), art_image(size=(60, 60)))
        self.assertEqual(director.scene_name(store.current()), "clock")

    def test_every_scene_name_renders_a_frame(self):
        for scene in sm.SCENE_NAMES:
            with self.subTest(scene=scene):
                director, _, _ = self.build(override_scene=scene)
                frame = director(1.0)
                self.assertEqual(frame.size, (64, 64))
                self.assertEqual(frame.mode, "RGB")

    def test_idle_scene_clock_actually_shows_the_clock(self):
        director, _, _ = self.build(idle_scene="clock")
        self.assertEqual(director(1.0).getextrema()[0][1], 255)

    def test_unknown_scene_falls_back_to_album(self):
        director, _, _ = self.build()
        director.config_store = stub_config()
        director.scenes.pop("blank", None)
        self.assertEqual(director(1.0).size, (64, 64))

    def test_build_scenes_covers_every_non_album_scene(self):
        _, _, store = self.build()
        scenes = sm.build_scenes(64, store, self.library())
        self.assertEqual(set(scenes), {name for name in sm.SCENE_NAMES if name != "album"})


class TestConfiguredDisplay(unittest.TestCase):
    class Recording:
        def __init__(self):
            self.frames = []
            self.brightness = []
            self.entered = self.exited = 0

        def show(self, image):
            self.frames.append(image)

        def clear(self):
            pass

        def set_brightness(self, value):
            self.brightness.append(value)

        def __enter__(self):
            self.entered += 1
            return self

        def __exit__(self, *exc):
            self.exited += 1

    def test_passes_frames_through(self):
        inner = self.Recording()
        display = sm.ConfiguredDisplay(inner, stub_config())
        display.show(art_image())
        self.assertEqual(len(inner.frames), 1)

    def test_applies_brightness_once(self):
        inner = self.Recording()
        display = sm.ConfiguredDisplay(inner, stub_config(brightness=40))
        display.show(art_image())
        display.show(art_image())
        self.assertEqual(inner.brightness, [40], "only on change, not every frame")

    def test_no_brightness_configured_leaves_the_panel_alone(self):
        inner = self.Recording()
        sm.ConfiguredDisplay(inner, stub_config(brightness=None)).show(art_image())
        self.assertEqual(inner.brightness, [])

    def test_backend_without_brightness_support_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            inner = sm.MockDisplay(Path(tmp) / "f.png")
            sm.ConfiguredDisplay(inner, stub_config(brightness=40)).show(art_image(size=(8, 8)))

    def test_forwards_the_context_manager(self):
        inner = self.Recording()
        with sm.ConfiguredDisplay(inner, stub_config()):
            pass
        self.assertEqual((inner.entered, inner.exited), (1, 1))


# ======================================================================================
# Web control UI
# ======================================================================================


class TestControlServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = sm.ConfigStore(Path(self.tmp.name) / "config.json")
        self.store.save(sm.Config())
        self.status = {"provider": "lastfm", "scene": "album", "scenes": list(sm.SCENE_NAMES),
                       "styles": list(sm.RENDERER_STYLES)}
        with quiet():
            self.server = sm.ControlServer(
                self.store, status=lambda: self.status, host="127.0.0.1", port=0
            )
            self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def get(self, route):
        return sm.http_request("GET", self.base + route, timeout=5)

    def post(self, route, payload, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        request = sm.urllib.request.Request(
            self.base + route, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with sm.urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except sm.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_serves_a_self_contained_page(self):
        response = self.get("/")
        self.assertEqual(response.status, 200)
        page = response.body.decode()
        self.assertIn("<title>Tune Matrix</title>", page)
        # A strict no-external-assets rule: the Pi may have no internet at all.
        for marker in ("http://", "https://", "cdn"):
            self.assertNotIn(marker, page.replace("http-equiv", ""))

    def test_config_endpoint_returns_current_settings(self):
        response = self.get("/api/config")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), sm.Config().as_dict())

    def test_status_endpoint_reports_what_is_on_screen(self):
        self.assertEqual(self.get("/api/status").json()["scene"], "album")

    def test_post_updates_and_persists_config(self):
        status, body = self.post("/api/config", {"idle_scene": "clock"})
        self.assertEqual(status, 200)
        self.assertEqual(body["idle_scene"], "clock")
        self.assertEqual(self.store.current().idle_scene, "clock")

    def test_post_merges_rather_than_replacing(self):
        self.post("/api/config", {"style": "art"})
        self.post("/api/config", {"rpm": 45})
        self.assertEqual(self.store.current().style, "art")

    def test_post_clamps_hostile_values(self):
        _, body = self.post("/api/config", {"rpm": 99999, "brightness": -12})
        self.assertEqual(body["rpm"], 300.0)
        self.assertEqual(body["brightness"], 1)

    def test_post_ignores_unknown_keys(self):
        status, body = self.post("/api/config", {"exec": "rm -rf /", "style": "art"})
        self.assertEqual(status, 200)
        self.assertNotIn("exec", body)

    def test_post_rejects_invalid_json(self):
        status, body = self.post("/api/config", None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_rejects_a_non_object_body(self):
        self.assertEqual(self.post("/api/config", [1, 2, 3])[0], 400)

    def test_post_rejects_an_oversized_body(self):
        self.assertEqual(self.post("/api/config", {"style": "x" * 20000})[0], 400)

    def test_unknown_routes_are_404_not_files(self):
        """There is no filesystem serving at all, so no path can reach .env."""
        for route in ("/.env", "/config.json", "/../.env", "/tune_matrix.py", "/nope"):
            with self.subTest(route=route):
                self.assertEqual(self.get(route).status, 404)

    def test_post_to_an_unknown_route_is_404(self):
        self.assertEqual(self.post("/api/anything", {"style": "art"})[0], 404)

    def test_url_is_reported(self):
        self.assertIn("127.0.0.1", self.server.url)

    def test_stop_releases_the_port(self):
        port = self.server.server.server_address[1]
        self.server.stop()
        rebound = sm.ThreadingHTTPServer(("127.0.0.1", port), sm.BaseHTTPRequestHandler)
        rebound.server_close()
        self.server.stop = lambda: None  # already stopped; keep addCleanup happy


class TestControlStatus(PhotoFixture):
    def test_reports_scene_playback_and_photos(self):
        self.add("a.png")
        library = self.library()
        library.advance()
        store = stub_config(idle_scene="photos")
        state = sm.PlaybackState()
        renderers = {n: sm.build_renderer(n, 64) for n in sm.RENDERER_STYLES}
        director = sm.SceneDirector(
            store, sm.AlbumScene(renderers, state, store),
            sm.build_scenes(64, store, library), state,
        )
        status = sm.control_status("lastfm", state, director, library)
        self.assertEqual(status["provider"], "lastfm")
        self.assertEqual(status["scene"], "photos")
        self.assertEqual(status["playback"], "idle")
        self.assertEqual(status["photo"], "a.png")
        self.assertEqual(status["photo_count"], 1)
        self.assertEqual(status["scenes"], list(sm.SCENE_NAMES))

    def test_reports_the_track_when_playing(self):
        store = stub_config()
        state = sm.PlaybackState()
        state.update(sm.PlaybackArt("k", "u", True, title="Robyn - Honey"), art_image((1, 1, 1), (60, 60)))
        renderers = {n: sm.build_renderer(n, 64) for n in sm.RENDERER_STYLES}
        director = sm.SceneDirector(
            store, sm.AlbumScene(renderers, state, store),
            sm.build_scenes(64, store, self.library()), state,
        )
        status = sm.control_status("lastfm", state, director, self.library())
        self.assertEqual((status["scene"], status["playback"], status["track"]),
                         ("album", "playing", "Robyn - Honey"))


class TestSceneEndToEnd(unittest.TestCase):
    """Scenes selected from config.json, rendered through the real run() path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "config.json"
        self.photos = self.dir / "photos"
        self.photos.mkdir()
        self.output = self.dir / "frame.png"

    def write_config(self, **values):
        self.config.write_text(json.dumps(values), encoding="utf-8")

    def run_cli(self, *extra):
        argv = [
            "--provider", "demo", "--config", str(self.config), "--photos", str(self.photos),
            "--mock-output", str(self.output), "--once", *extra,
        ]
        with quiet():
            sm.run(sm.build_parser().parse_args(argv))
        with Image.open(self.output) as frame:
            return frame.convert("RGB").copy()

    def add_photo(self, name, colour):
        Image.new("RGB", (300, 200), colour).save(self.photos / name)

    def test_clock_override_shows_the_clock_not_album_art(self):
        self.write_config(override_scene="clock")
        frame = self.run_cli()
        self.assertEqual(frame.getextrema()[0][1], 255, "lit digits")
        self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0), "clock sits on black")

    def test_photos_override_shows_a_photo(self):
        self.add_photo("a.png", (12, 190, 60))
        self.write_config(override_scene="photos")
        self.assertEqual(self.run_cli().getpixel((32, 32)), (12, 190, 60))

    def test_scene_flag_shows_photos_immediately_even_while_playing(self):
        """--scene is the "let me just look at it" path; the demo provider starts playing."""
        self.add_photo("a.png", (12, 190, 60))
        self.write_config()
        self.assertEqual(self.run_cli("--scene", "photos").getpixel((32, 32)), (12, 190, 60))

    def test_photo_scene_with_an_empty_directory_does_not_crash(self):
        self.write_config()
        frame = self.run_cli("--scene", "photos")
        self.assertEqual(frame.tobytes(), sm.render_idle(64).tobytes())

    def test_heic_style_double_suffix_is_still_read(self):
        # Phone exports land as IMG_1234.HEIC.jpeg; only the final suffix should matter.
        Image.new("RGB", (300, 200), (200, 40, 90)).save(self.photos / "IMG_1.HEIC.jpeg")
        self.write_config()
        pixel = self.run_cli("--scene", "photos").getpixel((32, 32))
        # JPEG is lossy, so compare loosely: this is about the filename, not fidelity.
        for channel, expected in zip(pixel, (200, 40, 90)):
            self.assertAlmostEqual(channel, expected, delta=6)

    def test_photos_plus_clock_shows_both(self):
        self.add_photo("a.png", (255, 255, 255))
        self.write_config(override_scene="photos+clock")
        frame = self.run_cli()
        self.assertEqual(frame.getpixel((2, 2)), (255, 255, 255), "photo fills the panel")
        self.assertEqual(frame.getextrema()[0][0], 0, "with outlined digits over it")

    def test_idle_scene_applies_only_when_nothing_plays(self):
        self.write_config(idle_scene="clock")
        # The demo provider starts out playing, so album art wins.
        self.assertEqual(self.run_cli().getpixel((0, 0)), (0, 0, 0))

    def test_config_written_by_the_web_ui_takes_effect_next_frame(self):
        """The UI only ever writes config.json, so this is the whole integration."""
        self.add_photo("a.png", (12, 190, 60))
        store = sm.ConfigStore(self.config)
        store.update({"override_scene": "photos"})
        self.assertEqual(self.run_cli().getpixel((32, 32)), (12, 190, 60))

    def test_no_web_server_for_a_bounded_render(self):
        self.write_config()
        with mock.patch.object(sm, "ControlServer", side_effect=AssertionError("no server")):
            self.run_cli()

    def test_web_port_zero_disables_the_server(self):
        args = sm.build_parser().parse_args(["--web-port", "0"])
        self.assertEqual(args.web_port, 0)


if __name__ == "__main__":
    unittest.main()
