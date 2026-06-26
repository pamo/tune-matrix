# Spotify Matrix

Shows the current Spotify album art on a 64x64 RGB matrix as a circular record. The album art is the record surface itself: it is cropped to a disk, spun while Spotify reports playback as active, and left stopped at the current angle when paused.

This uses Spotify's Web API `currently-playing` endpoint, not the browser-only Web Playback SDK. The first run opens Spotify OAuth, then the script stores a refresh token in `.cache/spotify_token.json`.

## Files

- `spotify_matrix.py` - Pi runtime script.
- `.env` - local Spotify credentials, ignored by Git.
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

Useful hardware options:

```bash
sudo -E .venv/bin/python spotify_matrix.py \
  --hardware-mapping regular \
  --gpio-slowdown 2 \
  --brightness 65
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
python spotify_matrix.py --auth-only --auth-timeout-seconds 180
```

Then run a one-frame live smoke test against Spotify's currently-playing API and write the rendered frame to disk:

```bash
python spotify_matrix.py --mock-output /tmp/spotify-matrix-smoke.png --once
```
