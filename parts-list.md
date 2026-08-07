# Parts List

Hardware BOM for the spinning-record display. Optimized for a **lo-fi look done well**: chunky
visible pixels, no glare, no flicker, thin enough to sit on a shelf and not look like a science fair
project.

Prices and stock are as of **August 2026** and will drift. Everything links to a real product page.

## The picks

| Part | Pick | Price | Where |
| --- | --- | --- | --- |
| Computer | Raspberry Pi Zero 2 **WH** (pre-soldered header) | $19.80 | [Adafruit 6008](https://www.adafruit.com/product/6008) |
| Display | 64x64 RGB LED Matrix, 2.5mm pitch, 45° curb-cut | $54.95 | [Adafruit 5407](https://www.adafruit.com/product/5407) |
| Driver | Adafruit RGB Matrix Bonnet for Raspberry Pi | $14.95 | [Adafruit 3211](https://www.adafruit.com/product/3211) |
| Power | 5V **4A** switching supply, 5.5mm/2.1mm barrel | $14.95 | [Adafruit 1466](https://www.adafruit.com/product/1466) |
| Diffuser | Black LED diffusion acrylic, 12" × 12", 2.6mm | $9.95 | [Adafruit 4594](https://www.adafruit.com/product/4594) |
| Storage | 32GB **A1-rated** microSD (SanDisk Extreme, Samsung EVO Select) | ~$10 | [Adafruit](https://www.adafruit.com/?q=microsd) / anywhere |
| Mounting | M3 nylon standoff + screw assortment | ~$6 | [Adafruit](https://www.adafruit.com/?q=m3+standoff) |

**Total: ~$132.** Add an enclosure (below) if you're not 3D printing one.

The panel and the bonnet both ship with the cables they need — the 16-pin IDC ribbon and the power
pigtail with spade lugs come in the box. You don't buy those separately.

## Changes from the original list, and why

### Drop the USB-C power path entirely

Original list had a 5V **3A** USB-C wall wart + a USB-C female breakout + hookup wire, feeding the
bonnet's screw terminals. Three problems:

1. **3A is undersized.** Adafruit's own bonnet docs call for **5V 4A or larger** on a 64x64. Full
   white on this panel is a theoretical 7.68A. Album art won't hit that, but a bright white cover at
   full brightness will sag a 3A rail — and the Pi is powered *through* the bonnet off that same
   rail, so a sag isn't a dim frame, it's an unclean shutdown and eventually a corrupt SD card.
2. **You can't fix it by buying a bigger USB-C brick.** USB-C PD caps 5V at 3A. Getting 4A+ at 5V
   over USB-C requires a fixed-voltage non-PD supply that specifically advertises it, which is rare
   and not worth hunting for.
3. **The breakout back-feeds the wrong connector.** The bonnet's screw terminal block is the *output*
   to the panel; the barrel jack is the *input*, and it's the side with the reverse-polarity and
   over/under-voltage protection. Wiring into the terminal block works electrically but skips the
   protection circuit.

The barrel-jack supply is cheaper than the two parts it replaces, deletes a soldering step, and is
the configuration Adafruit actually tests. If you want a flush USB-C port on the enclosure purely for
looks, that's a legitimate reason to go back — just source a fixed 5V 4A+ supply and use a breakout
with 5.1kΩ CC pulldowns.

### Buy the Pi with headers, and don't pay $38

$38 for a Pi Zero 2 W is roughly 2× MSRP — that's a reseller markup, probably because Adafruit's is
frequently out of stock. Check [rpilocator](https://rpilocator.com/?cat=PIZERO2) for in-stock
listings at real prices; PiShop.us, CanaKit, and Micro Center all carry it.

Get the **WH** (pre-soldered 2x20 header). The bonnet needs a full header, and hand-soldering 40 pins
onto a Zero to save $4 is not a good trade.

### Add the diffuser

This is the single largest visual difference between "DIY LED sign" and something you'd leave out on
a shelf. Bare, the panel is 4096 point sources with visible black grid lines and enough glare to be
unpleasant at desk distance.

Adafruit's **black** diffusion acrylic is the right material — unlike frosted or smoke-tinted acrylic
it *sharpens* the pixel edges while killing glare, and it makes the display read as a black slab when
it's off. Buy the 12" × 12" sheet and cut it down to the panel's 160mm × 160mm; it scores and snaps,
and it laser cuts cleanly if you have access to one.

Mount it **flush** against the LED face, not with an air gap. A gap blurs pixels into each other,
which is the opposite of what a pixel-art aesthetic wants.

If 4594 is out of stock, TAP Plastics and E-Street Plastics both sell black LED-diffusing acrylic cut
to size. Avoid generic white translucent acrylic — it blooms.

## Two solder mods you should do

Both take five minutes and neither is optional if you care about how this looks.

**1. Address-E jumper — mandatory.** 64x64 panels use 5-address (ABCDE) multiplexing. On the bottom
of the bonnet, bridge the middle pad to **8** with a blob of solder. Skip this and you get half an
image. ([Adafruit's matrix setup guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup))

**2. GPIO4 ↔ GPIO18 jumper — the "quality" mod.** A single wire between these two pads enables
hardware PWM, which is the difference between a visibly flickering panel and a rock-solid one. The
cost is onboard audio (HDMI and the 1/8" jack), which this project does not use.

This one has a direct effect on the run command in the README. Today you're running:

```
--hardware-mapping adafruit-hat --no-hardware-pulse --gpio-slowdown 4
```

That's the flicker-prone fallback config. After the mod:

```
--hardware-mapping adafruit-hat-pwm --gpio-slowdown 2
```

Also worth adding `isolcpus=3` to `/boot/cmdline.txt` — it dedicates a core to the matrix refresh and
noticeably steadies the image on a Zero 2 W.

## On the Pi Zero 2 W

The README notes the `rgbmatrix` bindings install crashed the Zero and suggests a Pi with more
memory. That crash is the compiler running out of RAM, not a fundamental limit — bump the swapfile to
1GB (`/etc/dphys-swapfile`, set `CONF_SWAPSIZE=1024`, restart the service) and it builds fine.

Keeping the Zero 2 W is the right call here specifically because this is a **display**: it fits
inside the panel's own depth, so the whole thing stays thin. A Pi 4 hangs off the back and doubles
the enclosure thickness.

That said, the Zero 2 W is the slowest board hzeller's library supports, and a spinning record is
animation, not a static frame. If you do the PWM mod and still can't get a clean refresh, the escape
hatch is a **Pi 3A+** (same slim-ish footprint) or a **Pi 4 2GB** (no fuss, thicker build). Do not
buy a Pi 5 — hzeller's Pi 5 support was still
[experimental as of early 2026](https://github.com/hzeller/rpi-rgb-led-matrix/issues/1878) because
the RP1 chip changed the GPIO architecture out from under the library.

## Enclosure and sizing

- Panel is **160mm × 160mm** (64 × 2.5mm pitch), roughly 6.3" square, plus ~14mm depth for the panel
  body and connectors on the back.
- The 45° curb-cut on 5407 exists so panels can butt together edge-to-edge into cubes. Harmless on a
  single flat panel, but the chamfered corners mean a square bezel won't sit perfectly flush — design
  the frame with a small corner radius or a rebate.
- The panel has M3 threaded studs on the back. That's what the standoffs are for: they hold the Pi +
  bonnet stack off the panel body so nothing shorts.
- Give the bonnet some airflow. It doesn't run hot, but the panel does at high brightness, and a
  fully sealed box traps it.

## Things you do not need

- **Heatsinks.** Not at this workload.
- **A real-time clock.** The Pi is on Wi-Fi; NTP handles it.
- **A separate Pi power supply.** The bonnet feeds 5V back to the Pi over the header. One supply,
  one cable.

---

Sources: [RGB Matrix Bonnet setup guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup) ·
[Bonnet product page](https://www.adafruit.com/product/3211) ·
[64x64 P2.5 curb-cut panel](https://www.adafruit.com/product/5407) ·
[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
