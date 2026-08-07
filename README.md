# Now Playing Matrix

Shows current album art on a 64x64 RGB matrix as a circular record. The album art is the record surface itself: it is cropped to a disk, spun while playback is active, and left stopped at the current angle when paused.

Supported providers:

- `spotify` (default): Spotify Web API currently-playing endpoint (live, exact)
- `lastfm`: Last.fm "now scrobbling" — universal best-effort provider that works for **any**
  service you scrobble (Spotify, Apple Music, Tidal, Deezer, YouTube Music, ...)
- `youtube-music`: YouTube Music history via `ytmusicapi`

> **Apple Music has no direct API support** (see [Music provider support](#music-provider-support)),
> but you can still display it: scrobble Apple Music to Last.fm and run `--provider lastfm`.

## Files

- `spotify_matrix.py` - Pi runtime script.
- `.env` - local provider credentials, ignored by Git.
- `.env.example` - template for recreating local config.
- `requirements.txt` - Python dependencies, excluding the hardware-specific RGB matrix bindings.

## Raspberry Pi setup

Install the RGB matrix Python bindings from the `hzeller/rpi-rgb-led-matrix` project for your HAT/wiring, then install this project's dependencies:

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```

The `--system-site-packages` flag is useful if the `rgbmatrix` bindings were installed system-wide.

This install sometimes crashes the raspberry pi zero, I had to do some fancy workarounds. Might be easier to use a pi with more memory!

## Spotify setup

In the Spotify developer dashboard, make sure this redirect URI is allowlisted exactly:

```text
http://127.0.0.1:8888/callback
```

For a headless Pi, forward the callback port from your computer:

```bash
ssh -L 8888:127.0.0.1:8888 pi@raspberrypi.local
```

Then run the script on the Pi and open the printed authorization URL in your local browser.

## Last.fm setup (recommended for Apple Music and everything else)

Last.fm shows whatever you are currently scrobbling, no matter which app plays it, so a
single setup covers Spotify, Apple Music, Tidal, Deezer, YouTube Music, and more. It needs
no paid account and polls cleanly from a headless Pi.

1. Create an API account at https://www.last.fm/api/account/create and copy the **API key**.
2. Make sure your player scrobbles to Last.fm:
   - Spotify: connect Last.fm in your Spotify account settings.
   - Apple Music: use a scrobbler (e.g. the official Last.fm mobile app, or a desktop
     scrobbler) so plays land on your Last.fm profile.
3. Set these in `.env`:

   - `LASTFM_API_KEY`
   - `LASTFM_USER` (your Last.fm username)

Then run with `--provider lastfm`.

> Best-effort by nature: the display follows your Last.fm "now scrobbling" track. It updates
> within a poll cycle of the scrobble and shows idle whenever nothing is actively scrobbling.

## YouTube Music setup

`ytmusicapi` is an optional dependency — it is commented out in `requirements.txt` so the
default install stays small on a Pi Zero. Install it explicitly for this provider:

```bash
pip install "ytmusicapi>=1.8"
```

Then create auth headers with `ytmusicapi` and save the JSON file (default path
`.cache/ytmusic_auth.json`, or set `YTMUSIC_AUTH_HEADERS_PATH`), and run with
`--provider youtube-music`.

## Run

This is the working command to run the script on your raspberry pi:

```bash
sudo -E .venv/bin/python spotify_matrix.py \
  --rows 64 \
  --cols 64 \
  --chain-length 1 \
  --parallel 1 \
  --gpio-slowdown 4 \
  --no-hardware-pulse \
  --hardware-mapping adafruit-hat
```

Use a specific provider:

```bash
sudo -E .venv/bin/python spotify_matrix.py --provider spotify
sudo -E .venv/bin/python spotify_matrix.py --provider lastfm
sudo -E .venv/bin/python spotify_matrix.py --provider youtube-music
```

Useful hardware options:

```bash
sudo -E .venv/bin/python spotify_matrix.py \
  --hardware-mapping regular \
  --gpio-slowdown 2 \
  --brightness 65
```

To bring up new panel wiring, show a moving colour test pattern. This needs no provider
credentials and makes no network calls, so it is the first thing to run on a fresh Pi:

```bash
sudo -E .venv/bin/python spotify_matrix.py --test-pattern
```

For a non-Pi test that writes one PNG frame instead of using matrix hardware:

```bash
python spotify_matrix.py --mock-output /tmp/spotify-matrix-frame.png --once
```

To verify the album art is what spins on the disk, render four local preview frames:

```bash
python spotify_matrix.py --preview-frames /tmp/spotify-matrix-preview
```

## Live smoke test (without matrix hardware)

Run this once to complete OAuth and cache a refresh token:

```bash
python spotify_matrix.py --provider spotify --auth-only --auth-timeout-seconds 180
```

Then run a one-frame live smoke test and write the rendered frame to disk:

```bash
python spotify_matrix.py --provider spotify --mock-output /tmp/spotify-matrix-smoke.png --once
```

For Last.fm, verify the API key/user and render one frame (start a track first so something
is scrobbling, otherwise the frame renders idle):

```bash
python spotify_matrix.py --provider lastfm --auth-only
python spotify_matrix.py --provider lastfm --mock-output /tmp/lastfm-smoke.png --once
```

For YouTube Music, verify the auth headers and render one frame:

```bash
python spotify_matrix.py --provider youtube-music --auth-only
python spotify_matrix.py --provider youtube-music --mock-output /tmp/youtube-music-smoke.png --once
```

## Music provider support

This project is meant to run **hands-off**: a headless Raspberry Pi on a wall that
polls "what is playing right now" from anywhere and spins the album art. That single
requirement — a remote device reading *live* now-playing state from the cloud — is what
makes or breaks each music service.

### Spotify — fully supported

Spotify's Web API exposes [`/me/player/currently-playing`](https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track).
A headless device can poll real-time playback for the account regardless of which device
is actually playing. The developer app is free to register (no paid account). This is the
model the whole project is built around.

### Last.fm — universal best-effort provider

`--provider lastfm` reads your Last.fm "now scrobbling" track via
[`user.getRecentTracks`](https://www.last.fm/api/show/user.getRecentTracks) (the live track
carries a `nowplaying` flag). Because nearly every player can scrobble to Last.fm, this one
provider covers **any** service — Spotify, Apple Music, Tidal, Deezer, YouTube Music — from a
single free, headless, cloud-pollable endpoint. It is the recommended way to display services
that expose no live now-playing API of their own.

Trade-offs: it reflects whatever the player reports to Last.fm, so it depends on a working
scrobbler, updates within a poll cycle of the scrobble, and shows idle when nothing is
actively scrobbling. Album art comes from Last.fm's metadata and is occasionally a placeholder
for obscure releases.

### Apple Music — no direct API, use Last.fm instead

A direct Apple Music provider is **not feasible** for this project. Two separate blockers,
either of which alone is fatal:

1. **No live now-playing in the cloud API.** The Apple Music API exposes catalog, library,
   playlists, and *recently played* — but **no "currently playing" endpoint**. Real-time
   now-playing state only exists inside **MusicKit running on the device that is actually
   playing** (a browser tab, a Mac, an iPhone). A separate headless Pi cannot ask Apple's
   servers what the account is playing right now. The best the cloud offers is
   recently-played, which lags and is not live — not good enough for a now-playing display.
2. **Paid membership required for credentials.** Even the recently-played path needs a
   developer token signed with a MusicKit `.p8` private key, and that key can only be
   created with a paid **Apple Developer Program** membership (~$99/€99 per year). There is
   no free tier for the API credentials.

The only ways to get live Apple Music now-playing onto a headless display would require a
**companion app on the actual playback device** to push state to the Pi (e.g. a MusicKit JS
instance, a native helper, or an iOS Shortcuts automation). These are not hands-off, are
platform-bound (macOS/iOS only), and the Shortcuts route in particular is unreliable — so a
native Apple Music provider is out of scope.

**The practical answer: scrobble Apple Music to Last.fm and use `--provider lastfm`.** That
gives a hands-off, headless Apple Music display without a paid developer account.

> Local-only alternative (not implemented): on macOS the Music app is fully scriptable via
> AppleScript/JXA with no developer account, but that only works on the Mac that is playing,
> not on a remote Pi, so it does not fit a wall display.

### YouTube Music — supported, with caveats

Uses [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) to read playback **history**
(an unofficial API backed by browser auth headers). It works headless, but it reads the most
recent history entry rather than a true live "now playing" signal, and unofficial endpoints
can break without notice.

### Provider summary

| Service       | Live cloud now-playing? | Free API credentials? | Status in this project |
| ------------- | ----------------------- | --------------------- | ---------------------- |
| Spotify       | Yes (exact, native)     | Yes                   | **Supported** (default) |
| Last.fm       | "Now scrobbling" via `user.getRecentTracks` | Yes (free API key) | **Supported** — universal best-effort |
| YouTube Music | History only (unofficial) | Browser auth        | **Supported**, best-effort |
| Apple Music   | No                      | No (paid program)     | Use Last.fm (scrobble) |
| Tidal         | No official public now-playing endpoint | Limited/approval | Use Last.fm (scrobble) |
| Deezer        | No live playback-state endpoint | Yes (app reg)  | Use Last.fm (scrobble) |

Anything that can scrobble to Last.fm is therefore covered on a best-effort basis today.

If you want to add a *direct* provider, the bar is simple: it must let a **headless device
read the currently-playing track from the cloud**. Implement a small client exposing
`get_playback_art() -> PlaybackArt | None` (see `SpotifyClient`, `LastFmClient`, or
`YouTubeMusicClient`) and wire it into `build_provider`.
