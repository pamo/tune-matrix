# Parts List

Hardware BOM for the Tune Matrix display. Optimized for a **lo-fi look done well**: chunky visible pixels, no glare, no flicker, thin enough to sit on a shelf and not look like a science fair project.

Prices and stock were checked in **August 2026** and will drift. Everything links to a real product page.

## The picks

| Part | Pick | Price | Where |
| --- | --- | --- | --- |
| Computer | Raspberry Pi Zero 2 **WH** (pre-soldered header) | $19.80 | [Adafruit 6008](https://www.adafruit.com/product/6008) |
| Display | 64x64 RGB LED Matrix, 2.5mm pitch, 45° curb-cut | $54.95 | [Adafruit 5407](https://www.adafruit.com/product/5407) |
| Driver | Adafruit RGB Matrix Bonnet for Raspberry Pi | $14.95 | [Adafruit 3211](https://www.adafruit.com/product/3211) |
| Diffuser | Black LED diffusion acrylic, 12" × 12", 2.6mm | $9.95 | [Adafruit 4594](https://www.adafruit.com/product/4594) |
| Storage | 32GB **A1-rated** microSD (SanDisk Extreme, Samsung EVO Select) | ~$10 | anywhere |
| Mounting | M3 nylon standoff + screw assortment | ~$6 | [Adafruit](https://www.adafruit.com/?q=m3+standoff) |
| **Power** | USB-C, three parts — see [below](#powering-it-over-usb-c) | $37.90 | |

The panel and the bonnet both ship with the cables they need — the 16-pin IDC ribbon and the power pigtail with spade lugs come in the box. You don't buy those separately.

## What it costs

| | |
| --- | --- |
| Core parts (everything above except power) | $115.65 |
| USB-C power chain | $37.90 |
| **Total** | **$153.55** |

That assumes you already own a 30W-or-better USB-C charger and cable. If not, add ~$20 for a brick.

The regulator is $29.95 of that $37.90. A generic "DC-DC 5V 5A" module or an RC hobby UBEC does the same job for $5–10, which brings the build to about **$130** — you're trading published protection specs and efficiency curves for price.

Not counted, because they depend on what you already have:

- **Enclosure** — $0 if you 3D print it (a few dollars of filament), up to ~$30 for laser-cut acrylic or wood.
- **A soldering iron** — you need one for the two bonnet mods below, and one of them is mandatory. If you don't own one, ~$25–40.

**For comparison, [TuneShine sells for $199.99](https://store.tuneshine.rocks/products/tuneshine)** with the same 6.3" 64x64 panel, in a finished wood enclosure, with its own app and cloud service. So building it saves roughly **$50** — real, but not the reason to do it. You're doing it for the parts you get to choose: your own photos, your own idle scenes, no subscription, and a display you can change the behaviour of.

## Powering it over USB-C

One USB-C cable to the wall, and a flush USB-C port on the enclosure. There's a spec wall in the way, and getting around it is what the three power parts are for.

**USB-C Power Delivery caps its 5V profile at 3A.** 5V/4A is not a standard PD voltage, so no amount of shopping produces a USB-C charger that will hand you 4A at 5V. On top of that, a plain USB-C cable is only rated for 3A unless it's e-marked for 5A. Pushing 4A down a generic cable is out of spec regardless of what the charger claims.

So: negotiate a **higher** voltage over USB-C, then convert it to 5V locally. This fixes the cable problem as a side effect — at 9V you draw only ~2A for the same 18W, comfortably inside a standard cable's rating. All the high current exists on the 5V side, after the converter, on two inches of wire inside your enclosure.

| Part | What it does | Price | Where |
| --- | --- | --- | --- |
| USB-C PD sink breakout (HUSB238) | Negotiates 9V or 12V from the charger instead of 5V. Jumper-selectable. Out of stock at Adafruit when checked; the [switchable version](https://www.adafruit.com/product/5991) does the same job with physical switches. | $5.95 | [Adafruit 5807](https://www.adafruit.com/product/5807) |
| 5V 5A step-down regulator | Converts that down to a solid 5V with 5A of headroom. Input range 6–38V, 85–95% efficient, with reverse-voltage, over-current, short-circuit and thermal protection built in. | $29.95 | [Pololu D24V50F5](https://www.pololu.com/product/2851) |
| Male 2.1mm barrel pigtail | Screw terminals on one end, a plug for the bonnet's jack on the other. | $2.00 | [Adafruit 369](https://www.adafruit.com/product/369) |
| USB-C charger, 30W or better | Must actually offer 9V. A 30W phone charger or any USB-C laptop brick will. | — | you probably own one |

Wiring is a short chain: charger → USB-C cable → PD breakout (set to 9V) → regulator input → regulator 5V output → barrel pigtail → the bonnet's DC jack.

Three details worth getting right:

- **Set the PD breakout to 9V or 12V, not 5V.** The regulator needs at least 6V in. Feeding it 5V gives you nothing.
- **Keep the barrel jack in the chain** rather than wiring the regulator straight to the bonnet's screw terminals. The jack sits behind the bonnet's over-voltage protection; the terminal block doesn't.
- **Check the charger actually offers 9V.** Most 20W-and-up USB-C bricks do, but a cheap 5V-only charger will leave the PD breakout unable to negotiate and nothing will light up.

The outcome: one USB-C cable to the wall, a flush port on the enclosure, and enough headroom that brightness is a free choice rather than a budget you're managing.

### If you'd rather not

A plain **5V 4A barrel supply** ([Adafruit 1466](https://www.adafruit.com/product/1466), $14.95) replaces all three parts and is $23 cheaper. You lose the single-USB-C tidiness and gain a wall wart with a barrel plug. Everything else in this list is unchanged.

### What not to do

Don't buy a 5V/4A supply that happens to have a USB-C connector on it and call it solved. Those exist, but the connector is then just a barrel jack in a different shape — the cable is still carrying 4A, still out of spec for a non-e-marked cable, and you've gained nothing over the barrel supply except a nicer-looking plug.

### How TuneShine does it with one cable and a plain brick

Worth checking against the commercial product, since it's the same panel. [TuneShine](https://store.tuneshine.rocks/products/tuneshine) is also a 6.3" 64x64 display, and it ships with a USB-C cable and a **20W** power brick.

That 20W is the tell. PD's 5V profile stops at 3A, which is **15W** — so a 20W brick cannot be delivering 20W at 5V. 20W over USB-C means 9V at about 2.2A, the standard profile on every 20W phone charger. So TuneShine is negotiating a higher voltage and stepping it down inside the enclosure — exactly the chain above, just hidden in the box. Their 4cm depth has room for it.

The second half is the budget. A 64x64 panel at full white and full brightness would want something like 38W at 5V. TuneShine allots 20W total, so it is also designed around real album art at a sensible brightness rather than a worst case that never happens.

Two useful conclusions for this build:

- A single USB-C cable is not a trick, it's a converter. There's no charger that skips the conversion.
- **20W is a realistic target, not 40W.** A 30W charger through the regulator gives roughly 3.6–4A at 5V after conversion losses, comfortably more than TuneShine budgets for the same panel. So the chain above isn't over-built — it's the same call the product makes.

## One supply or two?

Adafruit's guide says the Pi "must be powered separately, from the Pi's microUSB port," then immediately says you can just use the one plug. Both are true, and one component explains why. From the [bonnet pinouts page](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/pinouts):

> The driving Raspberry Pi must be powered separately, from the Pi's microUSB port but we do have a **1A diode on board** that will automatically power the Pi if/when the voltage drops. So if you want, just plug in the 5V wall adapter into the bonnet and it will automagically power up the Pi too!

The barrel jack feeds the panel directly and feeds the Pi **through a 1A diode**. That diode is doing two jobs, and each explains half the advice:

- **It lets one supply run everything.** Plug in the barrel jack and the Pi comes up too.
- **It's also why one supply is second-best.** A diode drops a few tenths of a volt, so the Pi sees meaningfully less than the 5V at the jack — and the Pi's undervoltage warning trips just under 4.7V, so you start close to the line. The panel's draw then swings by amps as the image changes, sagging that rail further exactly when a bright frame appears.

There's no backfeed risk in powering both. The diode only conducts toward the Pi, which is precisely why plugging a supply into the Pi's own port as well is safe rather than two supplies fighting.

**What I'd actually do.** For this build — one 64x64 panel and a Pi Zero 2 W — one supply is fine, and it's what most single-panel Zero builds run. A Zero 2 W draws a couple of hundred milliamps, nowhere near the diode's 1A ceiling. Give it headroom and keep the cable short.

Go to two supplies if any of these apply:

- You see undervoltage. Check it rather than guess — `0x0` means clean, and a non-zero result with bit 16 set means it has browned out since boot:
  ```bash
  vcgencmd get_throttled
  ```
- You swap in a Pi 3 or 4 later. Their peaks exceed 1A, so the diode becomes a bottleneck instead of a convenience.
- The display is doing anything you'd be annoyed to lose to a corrupt SD card.

Two supplies means 5V 4A into the bonnet's barrel jack for the panel, plus any decent 5V 2.5A supply into the Pi's own **PWR IN** port.

Also worth knowing: there's a **green LED next to the DC jack**. If it isn't lit, the bonnet has no good 5V and nothing else you try will work.

## Ordering from DigiKey

DigiKey stocks most of this, which is worth it for one order and one shipping charge. Part numbers, prices and stock below were each checked on a live DigiKey product page in August 2026.

| Part | DigiKey PN | Price | Stock when checked |
| --- | --- | --- | --- |
| 64x64 P2.5 panel | [1528-5407-ND](https://www.digikey.com/en/products/detail/adafruit-industries-llc/5407/16499340) | $54.95 | **3** — thin, check before planning around it |
| RGB Matrix Bonnet | [1528-2557-ND](https://www.digikey.com/en/products/detail/adafruit-industries-llc/3211/8535237) | $14.95 | 827 |
| Black diffusion acrylic, 12" × 12" | [1528-4594-ND](https://www.digikey.com/en/products/detail/adafruit-industries-llc/4594/12822316) | $9.95 | 148 |
| Male 2.1mm barrel pigtail | [search "adafruit 369"](https://www.digikey.com/en/products/result?keywords=adafruit%20369) | $2.00 | didn't verify the PN |
| 5V 4A supply, 5.5/2.1mm barrel | [1528-1466AD-ND](https://www.digikey.com/en/products/detail/adafruit-industries-llc/1466/10670047) | $14.95 | **Not stocked** — 4-week lead |
| Pi Zero 2 W (**no header**) | [2648-SC1176-ND](https://www.digikey.com/en/products/detail/raspberry-pi/SC1176/15298147) | $15.00 | **0** |
| MicroSD card | [browse](https://www.digikey.com/en/products/result?keywords=microsd%20card) | ~$10 | plenty |
| M3 standoffs and screws | [browse](https://www.digikey.com/en/products/result?keywords=m3%20standoff%20nylon) | ~$6 | plenty |

### Two things DigiKey can't do for you

**The Pi.** DigiKey lists only the headerless Zero 2 W, and it was at zero stock when I checked. The parts list calls for the **WH** because the bonnet needs a full 40-pin header, and I couldn't find a WH listing at DigiKey at all. So either buy the WH elsewhere — [Adafruit 6008](https://www.adafruit.com/product/6008) or [PiShop SC0721](https://www.pishop.us/product/raspberry-pi-zero-2w-with-headers/) — or buy the headerless one from DigiKey and solder on a 2x20 header. That's 40 joints to save about $4, which is a bad trade unless you enjoy it. Check [rpilocator](https://rpilocator.com/?cat=PIZERO2) for whoever has stock today.

**The barrel supply.** Adafruit's 1466 is a special-order item at DigiKey with a 4-week lead time, which defeats the point of consolidating the order. DigiKey suggests [PSAD36BSF-10-B2](https://www.digikey.com/en/products/result?keywords=PSAD36BSF-10-B2) (TT Electronics, 5V 20W = 4A, ~$14.30, in stock). The wattage is right — **verify the barrel is 5.5mm OD / 2.1mm ID and centre-positive before ordering**, because that's the one spec that will silently not fit. Any 5V ≥4A supply with that barrel works; there's nothing special about Adafruit's. Moot if you go the USB-C route.

The USB-C parts aren't DigiKey-friendly either: the Pololu regulator is best bought direct from [Pololu](https://www.pololu.com/product/2851).

## Why 4A and not 3A

The original plan was a 5V 3A USB-C wall wart straight into the bonnet. Adafruit's own docs call for **5V 4A or larger** on a 64x64, and full white on this panel is a theoretical 7.68A. Album art won't hit that, but a bright cover at full brightness will sag a 3A rail — and since the Pi is powered through the bonnet off that same rail, a sag isn't a dim frame, it's an unclean shutdown and eventually a corrupt SD card.

That's the whole reason the power chain above exists rather than just plugging a charger in. Brightness is effectively the current knob, so 3A is survivable if you keep the panel dim — but then the brightness slider in the web UI becomes something you must remember not to touch, and the failure mode is a corrupt SD card rather than a dim panel. Buying the headroom removes a footgun, not just a limit.

## Add the diffuser

This is the single largest visual difference between "DIY LED sign" and something you'd leave out on a shelf. Bare, the panel is 4096 point sources with visible black grid lines and enough glare to be unpleasant at desk distance.

Adafruit's **black** diffusion acrylic is the right material — unlike frosted or smoke-tinted acrylic it *sharpens* the pixel edges while killing glare, and it makes the display read as a black slab when it's off. Buy the 12" × 12" sheet and cut it down to the panel's 160mm × 160mm; it scores and snaps, and laser cuts cleanly.

Mount it **flush** against the LED face, not with an air gap. A gap blurs pixels into each other, which is the opposite of what a pixel-art aesthetic wants.

If 4594 is out of stock, TAP Plastics and E-Street Plastics both sell black LED-diffusing acrylic cut to size. Avoid generic white translucent acrylic — it blooms.

### Or print it, if you're printing the enclosure anyway

Adafruit's [Blurry Analog Clock](https://learn.adafruit.com/blurry-analog-clock) skips acrylic entirely and 3D prints its diffuser in **grey translucent PLA**. If you're already printing an enclosure that's an appealing trade: one print instead of a print plus a cut-and-glue step, no scoring, and the diffuser can be a captive layer of the front bezel rather than a separate sheet you have to hold in place.

The tradeoffs are real in both directions:

- **PLA** is cheaper (a few grams of filament), integrates into the enclosure, and lets you tune diffusion by changing wall thickness — that project leans into it, hence "blurry".
- **Acrylic** is more predictable. 2.6mm cast black acrylic has a known optical result; printed PLA's depends on your layer height, wall count and how translucent that particular filament really is. It also gives a glassy front face, where PLA reads matte.

For a pixel-art display where you want the pixels *sharp*, I'd still start with the acrylic — the whole reason to pick black diffusion acrylic over frosted is that it sharpens pixel edges rather than blurring them, which is the opposite of what that guide wants. But if you're printing anyway, printing a test bezel first costs almost nothing and you can compare them directly.

You can preview the effect before any of this arrives:

```bash
python tune_matrix.py --scene photos --photos ~/Pictures/test \
  --mock-output /tmp/p.png --preview-scale 10 --preview-grid --once
```

`--preview-grid` draws the inter-pixel gutter, which approximates how the panel reads behind the diffuser.

## Two solder mods you should do

Both take five minutes and neither is optional if you care how this looks.

**1. Address-E jumper — mandatory.** 64x64 panels use 5-address (ABCDE) multiplexing. On the bottom of the bonnet, bridge the middle pad to **8** with a blob of solder. Skip this and you get half an image. ([Adafruit's matrix setup guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup))

**2. GPIO4 ↔ GPIO18 jumper — the "quality" mod.** A single wire between these two pads enables hardware PWM, which is the difference between a visibly flickering panel and a rock-solid one. The cost is onboard audio, which this project doesn't use.

That second one changes the run command:

```bash
--hardware-mapping adafruit-hat-pwm --gpio-slowdown 2
```

Without the mod you're stuck on the flicker-prone fallback:

```bash
--hardware-mapping adafruit-hat --no-hardware-pulse --gpio-slowdown 4
```

Also worth adding `isolcpus=3` to `/boot/cmdline.txt` — it dedicates a core to the matrix refresh and noticeably steadies the image on a Zero 2 W.

## On the Pi Zero 2 W

Installing the `rgbmatrix` bindings can crash a Zero — that's the compiler running out of RAM, not a fundamental limit. Bump the swapfile to 1GB (`/etc/dphys-swapfile`, set `CONF_SWAPSIZE=1024`, restart the service) and it builds fine.

Keeping the Zero 2 W is the right call for a **display**: it fits inside the panel's own depth, so the whole thing stays thin. A Pi 4 hangs off the back and doubles the enclosure thickness.

That said, the Zero 2 W is the slowest board hzeller's library supports. If you do the PWM mod and still can't get a clean refresh, the escape hatch is a **Pi 3A+** (similar slim footprint) or a **Pi 4 2GB** (no fuss, thicker build). Note a Pi 3/4 also pushes you to two power supplies, since their peaks exceed the bonnet diode's 1A.

Don't buy a Pi 5 — hzeller's Pi 5 support was still [experimental as of early 2026](https://github.com/hzeller/rpi-rgb-led-matrix/issues/1878) because the RP1 chip changed the GPIO architecture out from under the library.

## Enclosure and sizing

- The panel is **160mm × 160mm** (64 × 2.5mm pitch), roughly 6.3" square, plus ~14mm depth for the panel body and rear connectors.
- The 45° curb-cut on 5407 exists so panels can butt together into cubes. Harmless on a single flat panel, but the chamfered corners mean a square bezel won't sit perfectly flush — design the frame with a small corner radius or a rebate.
- The panel has M3 threaded studs on the back. That's what the standoffs are for: they hold the Pi + bonnet stack off the panel body so nothing shorts.
- Going the USB-C route adds two small boards to find room for. Both are under an inch square, and they sit between your panel-mount USB-C port and the bonnet's jack.
- Give the bonnet some airflow. It doesn't run hot, but the panel does at high brightness, and a fully sealed box traps it.

## Things you don't need

- **Heatsinks.** Not at this workload.
- **A real-time clock.** The Pi is on Wi-Fi and NTP handles it. Caveat: after a power cut the clock is wrong until Wi-Fi reassociates. If that bothers you, an I²C RTC fits — SCL and SDA are free on the bonnet.
- **A 5A e-marked USB-C cable.** Only if you ignore the advice above and try to pull 4A at 5V over the cable.

---

Sources: [RGB Matrix Bonnet setup guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup) · [Bonnet pinouts](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/pinouts) · [64x64 P2.5 curb-cut panel](https://www.adafruit.com/product/5407) · [HUSB238 PD breakout guide](https://learn.adafruit.com/adafruit-husb238-usb-type-c-power-delivery-breakout/overview) · [Pololu D24V50F5](https://www.pololu.com/product/2851) · [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
