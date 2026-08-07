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
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

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
    """Return a stub http_request that yields the given responses in order."""
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


def lastfm_payload(images, now_playing=True, mbid=None, name="Teardrop", artist="Massive Attack"):
    track: dict[str, Any] = {"name": name, "artist": {"#text": artist}, "image": images}
    if now_playing:
        track["@attr"] = {"nowplaying": "true"}
    else:
        track["date"] = {"uts": "1600000000"}
    if mbid:
        track["mbid"] = mbid
    return {"recenttracks": {"track": [track]}}


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
        images = [
            {"size": "extralarge", "#text": "xl.png"},
            {"size": "bogus", "#text": "junk.png"},
        ]
        self.assertEqual(sm.select_lastfm_image_url(images), "xl.png")

    def test_unknown_size_used_when_nothing_else_available(self):
        self.assertEqual(
            sm.select_lastfm_image_url([{"size": "bogus", "#text": "junk.png"}]),
            "junk.png",
        )

    def test_placeholder_is_ignored(self):
        images = [{"size": "extralarge", "#text": PLACEHOLDER_URL}]
        self.assertIsNone(sm.select_lastfm_image_url(images))

    def test_real_art_wins_over_placeholder_even_when_larger(self):
        images = [
            {"size": "mega", "#text": PLACEHOLDER_URL},
            {"size": "small", "#text": "real.png"},
        ]
        self.assertEqual(sm.select_lastfm_image_url(images), "real.png")

    def test_empty_list(self):
        self.assertIsNone(sm.select_lastfm_image_url([]))


class TestLastFmClient(unittest.TestCase):
    def setUp(self):
        self.client = sm.LastFmClient("key", "user")
        patcher = mock.patch.object(sm, "http_request")
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, payload, status=200, headers=None):
        self.http.return_value = http_response(payload, status, headers)

    def test_now_playing_track(self):
        self.respond(lastfm_payload([{"size": "extralarge", "#text": "xl.png"}], mbid="mb-1"))
        art = self.client.get_playback_art()
        self.assertEqual(art, sm.PlaybackArt(key="mb-1", image_url="xl.png", is_playing=True))

    def test_key_falls_back_to_artist_and_title(self):
        self.respond(lastfm_payload([{"size": "large", "#text": "l.png"}]))
        self.assertEqual(self.client.get_playback_art().key, "Massive Attack - Teardrop")

    def test_historical_scrobble_renders_idle(self):
        self.respond(lastfm_payload([{"size": "large", "#text": "l.png"}], now_playing=False))
        self.assertIsNone(self.client.get_playback_art())

    def test_single_track_object_instead_of_list(self):
        self.respond(
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
        art = self.client.get_playback_art()
        self.assertIsNotNone(art)
        self.assertEqual(art.image_url, "m.png")

    def test_empty_track_list(self):
        self.respond({"recenttracks": {"track": []}})
        self.assertIsNone(self.client.get_playback_art())

    def test_missing_recenttracks_key(self):
        self.respond({})
        self.assertIsNone(self.client.get_playback_art())

    def test_track_without_art(self):
        self.respond(lastfm_payload([]))
        self.assertIsNone(self.client.get_playback_art())

    def test_placeholder_art_renders_idle(self):
        self.respond(lastfm_payload([{"size": "extralarge", "#text": PLACEHOLDER_URL}]))
        self.assertIsNone(self.client.get_playback_art())

    def test_auth_error_codes_name_the_env_vars(self):
        for code in (4, 6, 10, 26):
            with self.subTest(code=code):
                self.respond({"error": code, "message": "nope"})
                with self.assertRaises(RuntimeError) as ctx:
                    self.client.get_playback_art()
                self.assertIn("LASTFM_API_KEY", str(ctx.exception))

    def test_other_error_code_raises_without_auth_advice(self):
        self.respond({"error": 8, "message": "operation failed"})
        with self.assertRaises(RuntimeError) as ctx:
            self.client.get_playback_art()
        self.assertIn("operation failed", str(ctx.exception))
        self.assertNotIn("LASTFM_API_KEY", str(ctx.exception))

    def test_rate_limit_uses_retry_after_header(self):
        self.respond({}, status=429, headers={"Retry-After": "30"})
        with self.assertRaises(sm.ProviderRateLimitError) as ctx:
            self.client.get_playback_art()
        self.assertEqual(ctx.exception.retry_after_seconds, 30)
        self.assertEqual(ctx.exception.provider_name, "lastfm")

    def test_rate_limit_defaults_when_header_missing_or_garbage(self):
        for headers in ({}, {"Retry-After": "soon"}):
            with self.subTest(headers=headers):
                self.respond({}, status=429, headers=headers)
                with self.assertRaises(sm.ProviderRateLimitError) as ctx:
                    self.client.get_playback_art()
                self.assertEqual(ctx.exception.retry_after_seconds, 5)

    def test_server_error_raises(self):
        self.respond(b"boom", status=503)
        with self.assertRaises(RuntimeError):
            self.client.get_playback_art()

    def test_authorize_validates_credentials(self):
        self.respond({"error": 10, "message": "Invalid API key"})
        with self.assertRaises(RuntimeError):
            self.client.authorize()


class SpotifyClientFixture(unittest.TestCase):
    """Builds a SpotifyClient backed by a temp token cache that never needs OAuth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache" / "spotify_token.json"
        self.cache.parent.mkdir(parents=True)
        self.write_token({
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_at": time.time() + 3600,
        })

    def write_token(self, token):
        self.cache.write_text(json.dumps(token), encoding="utf-8")

    def build(self):
        return sm.SpotifyClient(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://127.0.0.1:8888/callback",
            token_cache=self.cache,
            open_browser=False,
            callback_timeout_seconds=1.0,
        )


class TestSpotifyCurrentlyPlaying(SpotifyClientFixture):
    def test_204_means_nothing_playing(self):
        client = self.build()
        with mock.patch.object(sm, "http_request", fake_http(http_response(b"", status=204))):
            self.assertIsNone(client.get_currently_playing())

    def test_200_returns_payload(self):
        client = self.build()
        with mock.patch.object(sm, "http_request", fake_http(http_response({"is_playing": True}))):
            self.assertEqual(client.get_currently_playing(), {"is_playing": True})

    def test_401_refreshes_once_then_succeeds(self):
        client = self.build()
        refreshes = []
        with mock.patch.object(client, "_refresh_access_token", side_effect=lambda: refreshes.append(1)):
            stub = fake_http(http_response(b"nope", status=401), http_response({"ok": True}))
            with mock.patch.object(sm, "http_request", stub):
                self.assertEqual(client.get_currently_playing(), {"ok": True})
        self.assertEqual(len(refreshes), 1, "should refresh exactly once")

    def test_repeated_401_gives_up_instead_of_recursing(self):
        client = self.build()
        with mock.patch.object(client, "_refresh_access_token"):
            stub = fake_http(http_response(b"nope", status=401))
            with mock.patch.object(sm, "http_request", stub):
                with self.assertRaises(RuntimeError) as ctx:
                    client.get_currently_playing()
        self.assertIn("401", str(ctx.exception))

    def test_429_raises_rate_limit_with_header_value(self):
        client = self.build()
        stub = fake_http(http_response(b"", status=429, headers={"Retry-After": "12"}))
        with mock.patch.object(sm, "http_request", stub):
            with self.assertRaises(sm.ProviderRateLimitError) as ctx:
                client.get_currently_playing()
        self.assertEqual(ctx.exception.retry_after_seconds, 12)

    def test_429_garbage_header_falls_back_to_default(self):
        client = self.build()
        stub = fake_http(http_response(b"", status=429, headers={"Retry-After": "later"}))
        with mock.patch.object(sm, "http_request", stub):
            with self.assertRaises(sm.ProviderRateLimitError) as ctx:
                client.get_currently_playing()
        self.assertEqual(ctx.exception.retry_after_seconds, 5)

    def test_server_error_raises(self):
        client = self.build()
        with mock.patch.object(sm, "http_request", fake_http(http_response(b"oops", status=500))):
            with self.assertRaises(RuntimeError):
                client.get_currently_playing()

    def test_get_playback_art_maps_response(self):
        client = self.build()
        payload = {
            "is_playing": True,
            "item": {
                "type": "track",
                "id": "track-1",
                "album": {"images": [{"url": "big.png", "width": 640}, {"url": "small.png", "width": 64}]},
            },
        }
        with mock.patch.object(sm, "http_request", fake_http(http_response(payload))):
            art = client.get_playback_art()
        self.assertEqual(art, sm.PlaybackArt(key="track-1", image_url="big.png", is_playing=True))


class TestSpotifyTokenCache(SpotifyClientFixture):
    def test_corrupt_cache_is_moved_aside_and_ignored(self):
        self.cache.write_text("{not json", encoding="utf-8")
        with quiet():
            client = self.build()
        self.assertIsNone(client.token)
        self.assertFalse(self.cache.exists())
        self.assertTrue(self.cache.with_name(f"{self.cache.name}.corrupt").exists())

    def test_load_survives_chmod_failure(self):
        client_cls_cache = self.cache
        with mock.patch.object(sm.os, "chmod", side_effect=OSError("read-only fs")), quiet():
            client = self.build()
        self.assertIsNotNone(client.token)
        self.assertEqual(client.token["access_token"], "at-1")
        self.assertTrue(client_cls_cache.exists())

    def test_save_is_atomic_and_private(self):
        client = self.build()
        client._save_token({"access_token": "at-2", "expires_in": 3600})
        self.assertEqual(json.loads(self.cache.read_text())["access_token"], "at-2")
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o600)
        leftovers = [p.name for p in self.cache.parent.iterdir() if p.name != self.cache.name]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_save_sets_expires_at_from_expires_in(self):
        client = self.build()
        before = time.time()
        client._save_token({"access_token": "at-2", "expires_in": 3600})
        expires_at = json.loads(self.cache.read_text())["expires_at"]
        self.assertGreater(expires_at, before)
        self.assertLessEqual(expires_at, before + 3600)

    def test_save_preserves_existing_refresh_token(self):
        client = self.build()
        client._save_token({"access_token": "at-2", "expires_in": 3600})
        self.assertEqual(json.loads(self.cache.read_text())["refresh_token"], "rt-1")

    def test_save_does_not_clobber_a_new_refresh_token(self):
        client = self.build()
        client._save_token({"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})
        self.assertEqual(json.loads(self.cache.read_text())["refresh_token"], "rt-2")

    def test_failed_write_leaves_original_intact(self):
        client = self.build()
        with mock.patch.object(sm.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                client._save_token({"access_token": "at-2", "expires_in": 3600})
        self.assertEqual(json.loads(self.cache.read_text())["access_token"], "at-1")
        leftovers = [p.name for p in self.cache.parent.iterdir() if p.name != self.cache.name]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


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

    def test_key_falls_back_to_uri_then_url(self):
        art = sm.playback_art_from_response({
            "item": {"type": "track", "uri": "spotify:track:x", "album": {"images": [{"url": "a.png", "width": 1}]}},
        })
        self.assertEqual(art.key, "spotify:track:x")


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

    def test_missing_auth_file_raises_actionable_error(self):
        client = sm.YouTubeMusicClient(auth_headers_path=Path("/nonexistent/auth.json"))
        with self.assertRaises(RuntimeError) as ctx:
            client.authorize()
        self.assertIn("YTMUSIC_AUTH_HEADERS_PATH", str(ctx.exception))


class TestRenderCache(unittest.TestCase):
    SIZE = 64
    ART_PIXEL = (16, 32)  # inside the disc, clear of the centre label and spindle

    @staticmethod
    def art(colour):
        return Image.new("RGB", (300, 300), colour)

    def sample(self, art, cache, angle=0.0, size=None):
        frame = sm.render_record(art, angle, size or self.SIZE, cache)
        return frame.getpixel(self.ART_PIXEL)

    def test_repeated_frames_reuse_the_fitted_art(self):
        cache = sm.RenderCache()
        art = self.art((255, 0, 0))
        sm.render_record(art, 0.0, self.SIZE, cache)
        fitted, mask = cache.fitted_art, cache.disc_mask
        sm.render_record(art, 90.0, self.SIZE, cache)
        self.assertIs(cache.fitted_art, fitted)
        self.assertIs(cache.disc_mask, mask)

    def test_rotation_still_applies_on_cached_frames(self):
        cache = sm.RenderCache()
        art = Image.new("RGB", (300, 300), (0, 0, 0))
        for y in range(150):
            for x in range(300):
                art.putpixel((x, y), (255, 0, 0))
        first = sm.render_record(art, 0.0, self.SIZE, cache).tobytes()
        second = sm.render_record(art, 90.0, self.SIZE, cache).tobytes()
        self.assertNotEqual(first, second, "cached art must still rotate each frame")

    def test_new_album_invalidates_the_cache(self):
        cache = sm.RenderCache()
        red, blue = self.art((255, 0, 0)), self.art((0, 0, 255))
        self.assertEqual(self.sample(red, cache), (255, 0, 0))
        self.assertEqual(self.sample(blue, cache), (0, 0, 255))
        self.assertEqual(self.sample(red, cache), (255, 0, 0))

    def test_cache_holds_a_strong_reference_to_its_source(self):
        cache = sm.RenderCache()
        art = self.art((255, 0, 0))
        sm.render_record(art, 0.0, self.SIZE, cache)
        self.assertIs(cache.source_art, art)

    def test_size_change_invalidates_the_cache(self):
        cache = sm.RenderCache()
        art = self.art((255, 0, 0))
        sm.render_record(art, 0.0, 64, cache)
        self.assertEqual(cache.disc_size, 60)
        sm.render_record(art, 0.0, 32, cache)
        self.assertEqual(cache.disc_size, 28)

    def test_matches_uncached_output(self):
        art = self.art((12, 200, 90))
        cached = sm.render_record(art, 37.0, self.SIZE, sm.RenderCache()).tobytes()
        uncached = sm.render_record(art, 37.0, self.SIZE, None).tobytes()
        self.assertEqual(cached, uncached)

    def test_no_art_renders_a_blank_frame(self):
        frame = sm.render_record(None, 0.0, self.SIZE, sm.RenderCache())
        self.assertEqual(frame.size, (self.SIZE, self.SIZE))
        self.assertEqual(frame.getpixel(self.ART_PIXEL), (0, 0, 0))


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
    def run_poll(self, results, initial_state=None, download=None):
        state = initial_state or sm.SharedPlaybackState()
        stop_event = threading.Event()
        provider = ScriptedProvider(results, stop_event)
        downloader = download or (lambda url: Image.new("RGB", (10, 10), (1, 2, 3)))
        with mock.patch.object(sm, "download_image", side_effect=downloader) as dl:
            with mock.patch("builtins.print"):
                sm.poll_provider(provider, state, threading.Lock(), stop_event, 0.0)
        return state, provider, dl

    def test_art_updates_shared_state(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, _, dl = self.run_poll([art])
        self.assertEqual(state.art_key, "k1")
        self.assertEqual(state.image_url, "u1.png")
        self.assertTrue(state.is_playing)
        self.assertIsNotNone(state.image)
        dl.assert_called_once_with("u1.png")

    def test_no_art_clears_shared_state(self):
        state = sm.SharedPlaybackState(
            art_key="old", image_url="old.png", image=Image.new("RGB", (4, 4)), is_playing=True
        )
        state, _, _ = self.run_poll([None], initial_state=state)
        self.assertIsNone(state.art_key)
        self.assertIsNone(state.image_url)
        self.assertIsNone(state.image)
        self.assertFalse(state.is_playing)

    def test_unchanged_track_is_not_redownloaded(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, _, dl = self.run_poll([art, art])
        self.assertEqual(dl.call_count, 1, "same key and url should reuse the cached image")
        self.assertIsNotNone(state.image)

    def test_pause_updates_flag_without_redownloading(self):
        playing = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        paused = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=False)
        state, _, dl = self.run_poll([playing, paused])
        self.assertEqual(dl.call_count, 1)
        self.assertFalse(state.is_playing)
        self.assertIsNotNone(state.image, "paused art must stay on screen")

    def test_rate_limit_preserves_current_art(self):
        existing = Image.new("RGB", (4, 4), (9, 9, 9))
        state = sm.SharedPlaybackState(
            art_key="k1", image_url="u1.png", image=existing, is_playing=True
        )
        state, _, _ = self.run_poll(
            [sm.ProviderRateLimitError("scripted", 1), None], initial_state=state
        )
        # The rate-limited call must not blank the display; the following poll does.
        self.assertIsNone(state.art_key)

    def test_rate_limit_alone_does_not_blank_the_display(self):
        existing = Image.new("RGB", (4, 4), (9, 9, 9))
        state = sm.SharedPlaybackState(
            art_key="k1", image_url="u1.png", image=existing, is_playing=True
        )
        state, _, _ = self.run_poll([sm.ProviderRateLimitError("scripted", 1)], initial_state=state)
        self.assertEqual(state.art_key, "k1")
        self.assertIs(state.image, existing)
        self.assertTrue(state.is_playing)

    def test_transient_exception_does_not_kill_the_loop(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)
        state, provider, _ = self.run_poll([RuntimeError("network down"), art])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(state.art_key, "k1")

    def test_download_failure_does_not_kill_the_loop(self):
        art = sm.PlaybackArt(key="k1", image_url="u1.png", is_playing=True)

        def flaky(url):
            raise RuntimeError("404")

        state, provider, _ = self.run_poll([art, art], download=flaky)
        self.assertEqual(provider.calls, 2)


class TestProviderRateLimitError(unittest.TestCase):
    def test_retry_after_is_clamped_to_at_least_one_second(self):
        for given in (0, -5):
            with self.subTest(given=given):
                self.assertEqual(sm.ProviderRateLimitError("p", given).retry_after_seconds, 1)

    def test_message_mentions_provider_and_delay(self):
        message = str(sm.ProviderRateLimitError("lastfm", 30))
        self.assertIn("lastfm", message)
        self.assertIn("30", message)


class TestCli(unittest.TestCase):
    def parse(self, argv):
        return sm.build_parser().parse_args(argv)

    def test_provider_defaults_to_spotify(self):
        self.assertEqual(self.parse([]).provider, "spotify")

    def test_known_providers_accepted(self):
        for provider in ("spotify", "lastfm", "youtube-music"):
            with self.subTest(provider=provider):
                self.assertEqual(self.parse(["--provider", provider]).provider, provider)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(SystemExit), quiet():
            self.parse(["--provider", "tidal"])

    def test_stale_window_defaults_and_overrides(self):
        self.assertEqual(self.parse([]).ytmusic_stale_seconds, 600.0)
        self.assertEqual(self.parse(["--ytmusic-stale-seconds", "90"]).ytmusic_stale_seconds, 90.0)

    def test_non_positive_values_rejected(self):
        for flag in ("--ytmusic-stale-seconds", "--auth-timeout-seconds", "--fps", "--rpm"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit), quiet():
                    self.parse([flag, "0"])

    def test_build_provider_reports_all_missing_lastfm_vars(self):
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

    def test_build_provider_passes_stale_window_through(self):
        args = self.parse(["--provider", "youtube-music", "--ytmusic-stale-seconds", "42"])
        provider = sm.build_provider(args)
        self.assertIsInstance(provider, sm.YouTubeMusicClient)
        self.assertEqual(provider.stale_after_seconds, 42.0)

    def test_missing_env_vars_helper(self):
        self.assertEqual(sm.missing_env_vars({"A": "x", "B": None, "C": ""}), ["B", "C"])


class TestCallbackServerTimeout(unittest.TestCase):
    def test_wait_for_code_times_out(self):
        server = sm.LocalCallbackServer("127.0.0.1", 0, "/callback", "state-1")
        with self.assertRaises(RuntimeError) as ctx:
            server.wait_for_code(timeout_seconds=0.2)
        self.assertIn("Timed out", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
