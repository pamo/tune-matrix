# Tune Matrix

Shows current album art on a 64x64 RGB matrix, either as a spinning record or as static
full-bleed art (see [Display style](#display-style)). In the default record style the album
art is the record surface itself: cropped to a disk, spun while playback is active, and
left stopped at the current angle when paused.

You do not need the panel to work on this — see
[Previewing without a matrix](#previewing-without-a-matrix) for a live terminal preview, GIF
recording, and a credential-free demo mode.

Supported providers:

- `lastfm` (default): Last.fm "now scrobbling" — universal best-effort provider that works
  for **any** service you scrobble (Spotify, Apple Music, Tidal, Deezer, YouTube Music, ...).
  One free API key, no OAuth round-trip, which is why it is the default.
- `spotify`: Spotify Web API currently-playing endpoint (live and exact, but Spotify only)
- `youtube-music`: YouTube Music history via `ytmusicapi`
- `demo`: synthetic playback for previewing the display with no credentials and no network

> **Apple Music has no direct API support** (see [Music provider support](#music-provider-support)),
> but you can still display it: scrobble Apple Music to Last.fm and run `--provider lastfm`.

## Files

- `tune_matrix.py` - Pi runtime script.
- `test_tune_matrix.py` - unit tests (stdlib `unittest`, no extra packages).
- `.env` - local provider credentials, ignored by Git.
- `.env.example` - template for recreating local config.
- `requirements.txt` - Python dependencies, excluding the hardware-specific RGB matrix bindings.

## Tests

```bash
python -m unittest -v
```

No network, no real clock, no matrix hardware, and no dependencies beyond Pillow, so this
runs on the Pi as well as a laptop.

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
   - `LASTFM_USER` (your Last.fm username, exactly as it appears in `last.fm/user/<name>`)

4. Verify, and **read the account it names back**:

   ```bash
   python tune_matrix.py --provider lastfm --auth-only
   ```

   ```
   Last.fm verified: user pam-o, 257,480 scrobbles. Check that is your account.
   ```

   Last.fm only rejects usernames that do not exist, so a typo that happens to be someone
   else's account verifies happily and then quietly shows their listening instead of
   yours. The scrobble count is there so a wrong account is obvious at a glance.

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
sudo -E .venv/bin/python tune_matrix.py \
  --rows 64 \
  --cols 64 \
  --chain-length 1 \
  --parallel 1 \
  --gpio-slowdown 4 \
  --no-hardware-pulse \
  --hardware-mapping adafruit-hat
```

`--chain-length` and `--parallel` must both be 1. Each frame is rendered as a single
square panel and pasted at the origin, so a chained display would leave the rest of the
chain black. Passing anything else is rejected at startup rather than silently rendering
a fraction of the panel.

The provider defaults to `lastfm`, so no flag is needed for it. Pick another explicitly:

```bash
sudo -E .venv/bin/python tune_matrix.py                          # lastfm
sudo -E .venv/bin/python tune_matrix.py --provider spotify
sudo -E .venv/bin/python tune_matrix.py --provider youtube-music
sudo -E .venv/bin/python tune_matrix.py --provider demo          # no credentials
```

Useful hardware options:

```bash
sudo -E .venv/bin/python tune_matrix.py \
  --hardware-mapping regular \
  --gpio-slowdown 2 \
  --brightness 65
```

To bring up new panel wiring, show a moving colour test pattern. This needs no provider
credentials and makes no network calls, so it is the first thing to run on a fresh Pi:

```bash
sudo -E .venv/bin/python tune_matrix.py --test-pattern
```

## Display style

`--style` picks what the panel shows:

| Style | What you get |
| --- | --- |
| `record` (default) | Album art cut into a circular disc that spins while playback is active, with a centre label and spindle hole. |
| `art` | Album art static and full-bleed, edge to edge. No disc, no rotation. |

```bash
sudo -E .venv/bin/python tune_matrix.py --style art
```

`--style art` does no per-frame work at all, so `--fps 2` is plenty and saves CPU on a
Pi Zero. `--rpm` only affects `record`.

## Previewing without a matrix

You do not need the panel to see exactly what it will show. All of these work with the
real Spotify and Last.fm connections, so you can watch live playback drive the display.

### Live in the terminal

The most useful one. Each character cell holds two vertical pixels via truecolour
half-blocks, which also makes each pixel come out roughly square, so a 64x64 frame is 64
columns by 32 rows. No dependencies, and it works over SSH.

```bash
python tune_matrix.py --preview-terminal
```

It magnifies to fill the window, draws a status line underneath, and renders into the
alternate screen buffer so your scrollback survives. Ctrl-C restores the terminal.

```
lastfm · record · playing · Britney Spears - Toxic · 20.0 fps · ctrl-c to stop
```

That line is the reason this is a debugging tool and not just a toy: it shows which
provider answered, which style is active, whether playback is playing / paused / idle, the
track by name, and the frame rate you are really achieving. Seeing the wrong track name is
usually the fastest way to notice a misconfiguration.

Notes worth knowing:

- **Magnification is limited by terminal height**, because each character row carries two
  pixel rows. A 64x64 frame needs 64x34 at 1x, 128x66 at 2x and 256x130 at 4x, so you want
  a tall window before scaling does much. Most windows get 1x.
- `--preview-scale N` raises the ceiling but is **clamped to what fits**, with a note on
  stderr saying what size window that scale would need:

  ```
  Warning: --preview-scale 4 needs a 256x130 terminal, but this one is 119x57. Falling back to 1x.
  ```

  Clamping rather than obeying is deliberate: overflowing the width makes every row
  line-wrap, which desynchronises the in-place redraw and turns the picture into confetti.
  A smaller preview beats a broken one. Auto-fit also re-evaluates as you resize.
- `--preview-grid` works here too, drawing the inter-pixel gutter.
- A frame is roughly 25 KB of escape codes, so 20 fps is ~500 KB/s. Over a slow SSH link
  use `--fps 10` or lower.
- `--once` deliberately stays on the main screen, since leaving the alternate screen would
  erase the single frame you asked to see.

### Other preview outputs

**No credentials at all.** `--provider demo` invents playback locally and cycles
playing → paused → nothing playing, so every state the panel can be in shows up in one
run. Nothing is downloaded:

```bash
python tune_matrix.py --provider demo --preview-terminal
python tune_matrix.py --provider demo --style art --demo-cycle-seconds 3
```

**Record an animation.** Writes a looping GIF, which is the easiest way to judge whether
the spin speed looks right:

```bash
python tune_matrix.py --provider demo --record-gif /tmp/spin.gif --record-seconds 6
```

**A single frame, magnified.** `--preview-scale` magnifies with nearest-neighbour so the
pixels stay crisp, and `--preview-grid` draws the inter-pixel gutter, which approximates
how the panel reads behind a diffusion layer:

```bash
python tune_matrix.py --mock-output /tmp/frame.png --preview-scale 8 --preview-grid --once
```

**Static sample frames**, no provider and no network:

```bash
python tune_matrix.py --preview-frames /tmp/preview --preview-scale 6
python tune_matrix.py --preview-frames /tmp/preview --style art
```

`--mock-output`, `--preview-terminal` and `--record-gif` are mutually exclusive; without
one of them the script drives real matrix hardware. `--preview-scale` and `--preview-grid`
apply to every preview output, and `--preview-scale` defaults to 1x for files and to
fill-the-window for the terminal.

Two notes. A bounded render (`--once`, `--record-gif`) waits briefly for the first poll to
land, so previews show real album art rather than the idle frame; the live path does not
wait and fills in as soon as the provider answers. And GIF is limited to 256 colours per
frame, so album art is quantised in a recording but not on the panel.

## Behaviour when things go wrong

The script distinguishes failures it can recover from and failures it cannot, which
matters when it runs unattended from a wall.

**Fatal, exits with a message** — missing environment variables, a bad or suspended API
key, a revoked Spotify refresh token, a missing `ytmusicapi` auth file. Retrying these
would never succeed, so they fail loudly.

**Transient, keeps running** — no network yet, DNS failure, a 5xx from the provider, or a
rate limit. On startup the display shows the idle frame and the poll thread retries until
the provider recovers; this is the normal case for a Pi that powers on before Wi-Fi
associates. While running, a failed poll leaves the current album art on screen rather
than blanking it, and repeated identical failures are logged once rather than at the poll
rate.

`--auth-only` is the exception: it fails loudly on transient errors too, because its whole
job is to tell you whether credentials work right now.

## Live smoke test (without matrix hardware)

Run this once to complete OAuth and cache a refresh token:

```bash
python tune_matrix.py --provider spotify --auth-only --auth-timeout-seconds 180
```

Then run a one-frame live smoke test and write the rendered frame to disk:

```bash
python tune_matrix.py --provider spotify --mock-output /tmp/tune-matrix-smoke.png --once
```

For Last.fm, verify the API key/user and render one frame (start a track first so something
is scrobbling, otherwise the frame renders idle):

```bash
python tune_matrix.py --provider lastfm --auth-only
python tune_matrix.py --provider lastfm --mock-output /tmp/lastfm-smoke.png --once
```

For YouTube Music, verify the auth headers and render one frame:

```bash
python tune_matrix.py --provider youtube-music --auth-only
python tune_matrix.py --provider youtube-music --mock-output /tmp/youtube-music-smoke.png --once
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

Because history carries no "is it playing" flag, playback state is inferred from
**freshness**: when the top history entry changes, the record starts spinning and keeps
spinning until `--ytmusic-stale-seconds` (default 600) passes with no further change. Two
consequences worth knowing:

- A track already sitting at the top of history when the process starts counts as a change,
  so expect one stale spin window after a restart.
- A long track will stop spinning before it ends if it outlasts the window. Raise
  `--ytmusic-stale-seconds` to trade a longer stale spin for fewer false stops.

If you want an accurate playing/paused signal, use `--provider lastfm` instead — it has a
real live `nowplaying` flag.

### Provider summary

| Service       | Live cloud now-playing? | Free API credentials? | Status in this project |
| ------------- | ----------------------- | --------------------- | ---------------------- |
| Spotify       | Yes (exact, native)     | Yes                   | **Supported** |
| Last.fm       | "Now scrobbling" via `user.getRecentTracks` | Yes (free API key) | **Supported** (default) — universal best-effort |
| YouTube Music | History only (unofficial) | Browser auth        | **Supported**, best-effort |
| Apple Music   | No                      | No (paid program)     | Use Last.fm (scrobble) |
| Tidal         | No official public now-playing endpoint | Limited/approval | Use Last.fm (scrobble) |
| Deezer        | No live playback-state endpoint | Yes (app reg)  | Use Last.fm (scrobble) |

Anything that can scrobble to Last.fm is therefore covered on a best-effort basis today.

If you want to add a *direct* provider, the bar is simple: it must let a **headless device
read the currently-playing track from the cloud**. Implement a small client exposing
`get_playback_art() -> PlaybackArt | None` (see `SpotifyClient`, `LastFmClient`, or
`YouTubeMusicClient`) and wire it into `build_provider`.
