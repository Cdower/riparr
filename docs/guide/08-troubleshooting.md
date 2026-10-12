# 8. Troubleshooting

[← Settings reference](07-settings-reference.md) · [Guide index](README.md)

Organized by **what you actually observed**, since there's no screen to read.

---

## `riparr.local` doesn't load

**First, wait two full minutes after plugging in.** First boot is slower than later boots.

**Then work out which of three things it is:**

| What you see | Meaning | Do this |
|---|---|---|
| Drive tray won't open, nothing at all | No power | Try another USB-C cable and supply. Charge-only cables are the usual culprit, and a brick with no 15V/20V profile is the next one. |
| Drive spins up, box never appears | Booting, or it couldn't join Wi-Fi | Wait two minutes, then see below |
| Another device can reach it by IP but not by name | Name resolution, not the box | See below |

### Couldn't join WiFi (blinking amber)

**Write the card again in the Preparer with the credentials corrected.** The box has no
screen and no Ethernet, so there is no other way in yet — and nothing is lost, because a
box that never joined has no settings on it.

*(An AP-mode fallback — the box putting up its own `Riparr-Setup` network so you can fix
this from a phone — is designed and [core] on the backlog, and is not built.)*

Most common causes, in order:

1. **Wrong password** — the usual one
2. **A 5GHz network on a 2.4GHz-only board.** Most supported boards are dual-band and
   5GHz is fine — but the Raspberry Pi Zero 2 W and Pi 3 Model B have no 5GHz radio, so on
   those boards the network must be 2.4GHz. The Preparer greys out networks your board
   can't see.
3. **No WiFi country set** — WiFi won't start without it (the Preparer sets this for you)
4. **The network is captive-portal or enterprise** (802.1X) — not supported

### It's up, but the name doesn't resolve (solid green)

`riparr.local` uses mDNS. Works out of the box on macOS, iOS, and most Linux; Windows 10+
usually works.

- **Try `http://riparr.local:8080`** — the port may be needed
- **Find the IP on your router** and browse to it directly. Look for `riparr` in the DHCP
  client list.
- **Some corporate and guest networks block mDNS.** Use the IP address.
- **VLANs:** mDNS doesn't cross subnets. Your computer must be on the same network segment
  as the box.

## It isn't auto ripping

**Open `riparr.local` and read the checklist under the Auto Rip switch.** It lists every
prerequisite whether or not it's met, so the row that isn't green is your answer:

| Row | Means |
|---|---|
| **Riparr can read discs** | MakeMKV is installed |
| **The MakeMKV key is current** | There's a key and it hasn't lapsed. Amber means it lapses within a week — rips fail the day it does |
| **A drive to read them in** | An optical drive is on the USB bus. A working adapter shows up here even with no disc in the tray |
| **Somewhere to put the files** | A library share is configured *and* has been tested |
| **Room to work** | Amber only if the card is too full to rip safely. Rips go to your library by default, so this normally means the share is away and the card is being used as the fallback |

A red row disables the switch entirely and links to the page that fixes it. An amber row
leaves Auto Rip on but explains why a disc you just put in might not get ripped.

**All green and still nothing?** Check the switch is actually on — the checklist tells you
Riparr *could* rip, not that you've asked it to. Then read the next section.

## Nothing happens when I insert a disc

**Nothing happens at all:** the disc wasn't detected.

- Wait 30 seconds — spin-up and reading the table of contents takes a moment
- Try a different disc. A badly scratched or dirty one may not mount at all.
- **Disc upside down.** It happens.

**The drive works at it and gives up:** it saw the disc but couldn't read it. Clean the
disc.

**Nothing on any disc:** the drive may not be enumerating. Check the web page — the
dashboard reports whether a drive is present at all. If it isn't, that's a cable or power
problem inside the box.

## The box resets when I insert a disc

**Power problem, not software.** Optical drives pull a big surge when they spin up, and
it's browning out the Pi.

- **Check what your brick actually offers.** The trigger board can only ask; if the brick
  has no 15V or 20V profile you'll be running on 9V or 5V and browning out. 45W or more
  usually has them, small phone chargers usually don't.
- Try a shorter, thicker cable
- If you built it yourself: the board and the drive need **separate buck outputs**, and
  the 5V rail needs bulk capacitance. Sharing one node is exactly what causes this.

## The box vanishes off the network for hours

**Almost always the radio, not the box.** This board's Wi-Fi can stop passing traffic
while still reporting itself connected — nothing is logged, because from the driver's
point of view nothing happened. The box carries on running perfectly; it just cannot be
reached.

Riparr watches for this itself: it pings your router once a minute and reconnects,
reloads the Wi-Fi driver, and finally restarts the box. Check
**Settings → Network → If the connection drops** is on.

**If it keeps happening**, shorten *Wait this long first*. The usual cause is an access
point on one of the 5 GHz channels shared with radar: it has to change channel when it
detects any, and this board's driver does not always follow.

**To confirm it was the radio and not a crash**, over SSH:

```
journalctl -b -1 | grep fake-hwclock-save | tail -20
```

An unbroken hourly line through the outage means the box never stopped running.

## The rip failed

The web page says how far it got and why.

- **Clean the disc.** Soft cloth, center outward, never circular.
- **Check for scratches** on the data side
- **Retry.** Riparr already retried internally, but a reseat sometimes helps.
- **Some discs are genuinely unreadable** by some drives. A different drive may work.

Nothing partial is written to your library — you won't get a broken file.

## It ejected immediately — three flashes, or the tray twice

Not an error, and not the disc. You have ripped this one before, and Riparr saved you
half an hour.

**With a browser open** you will already be looking at the answer: the page jumps to
**Discs** and highlights the film, with the date it went into your library.

**Without one**, that is what the signal was: three short flashes of the drive's own
light, three times — or two tray cycles, if you set it that way. Change it on
**Settings → Ripping → Already-ripped discs**,
where **Try the light** and **Try the tray** let you see each one on demand.

**To rip it anyway:** the **Re-rip** button on that film's tile in **Discs**. Leave the
disc on the open tray — Riparr pulls the tray back in itself.

**If it says this about a disc you have never ripped**, Riparr has matched it to
something else by mistake. **Forget** on the same tile clears its memory of it, and the
next insertion is treated as new.

## Blinking amber — paused

Riparr can't reach your library share. The rip is safe and paused mid-flight.

1. **Is the NAS awake?** Drive spin-down and sleep timers are the usual cause.
2. **Settings → Library → Test write.** This tells you exactly which part is failing.
3. **Did credentials change?** A NAS password rotation will do this.
4. **Is the share full?**

Fix it and Riparr resumes where it stopped. Nothing is lost.

## Everything is slow

**Probably not a fault.** A Blu-ray takes about three hours and a 4K disc most of a day,
because the board's WiFi moves roughly 4 MB/s and the movie has to travel over it.

Genuinely slower than that:

- **Weak WiFi.** Move the box closer to the router. 2.4GHz is congested in most homes.
- **Microwave ovens and old cordless phones** sit right on 2.4GHz
- **NAS is busy** with something else

No SD card and no drive will make this faster. There's no Ethernet option on this board.

## Profiles are not arriving in Grafana

This only applies if you turned on Performance profiling in Settings → General. The
quickest check is that section's **Save and send a test profile** button: it sends one
profile right now and tells you what came back.

The same failure is on the Events page, from the "Profiling" component, as a line like:

```
Profile upload failed: HTTP 401 ...
```

What the end of that line means:

- **HTTP 401.** Grafana did not accept the credentials. Either the token is wrong or
  expired, or the stack ID is not the one the token belongs to. Paste both again.
- **HTTP 404.** The URL is wrong. It should be the `https://profiles-prod-…grafana.net`
  address from your stack's Profiles page, and nothing after `.net`: no path, no
  `/ingest`.
- **A URLError, such as "Name or service not known" or "timed out".** The box cannot
  reach the internet. Check the WiFi and the date on the box, then try the test button
  again.

The profiler never stops the box doing its job. A failed upload drops the oldest
profile after six are waiting, and nothing is written to the card.

## Names are wrong

- **Check the disc's queue entry** — Riparr flags anything it was unsure about rather than
  guessing
- **TV discs:** correct the first disc of a season and the rest follow — Riparr carries the numbering on from where the last disc stopped
- **Corrections are remembered** per disc, forever. You'll never fix the same disc twice.
- **Consistently wrong across many discs:** check your naming template in
  [settings](07-settings-reference.md#naming)

## It says my card is full

Shouldn't be possible in normal use — Riparr streams rips out as they're made.

If it happens, something downstream is stuck:

1. **Check the library share** — if uploads have been failing, the buffer is holding
   everything
2. **Look for stuck rips** in the queue
3. **Settings → Storage** shows what's held and why

## I unplugged it mid-rip

Fine. Plug it back in.

Riparr detects the interruption on boot and either resumes or cleanly fails that job. Your
library won't have a partial file and your card won't be corrupt — the system is built
assuming this will happen.

## I forgot my password

**You don't need to re-flash.** Riparr accepts a reset from the card itself, which is
proof you own the box in the same way re-flashing it would be — and keeps your settings,
shares and disc history.

1. Shut the box down (account menu → **Shut down**), then unplug it and take the card out
2. Put the card in a computer. The small **boot** partition mounts on its own
3. Create an empty file there called **`riparr-reset`** — no extension needed, and the
   contents are ignored
4. Card back in the box, plug it in, wait a minute
5. `riparr.local` asks you to create an account again

Riparr deletes the file as it acts on it, so the card goes back to normal on its own.
Nothing else is touched: your share, your naming templates and every disc it remembers
are all still there.

## Dates and "days left" look wrong

The box has no battery-backed clock. It learns the time from the internet at every boot,
and until it does, it believes whatever the last thing written to the card said.

If **System → Status** says the clock can't be right, Riparr stops making claims that
depend on it — including how long your MakeMKV key has left — rather than stating a
confident wrong number. It sorts itself out within a minute of the box reaching the
internet. If it doesn't, the box can't get out to a time server: see
[`riparr.local` doesn't load](#riparrlocal-doesnt-load) for the network side.

## The LED stays dark — "No status LED detected at /dev/spidev1.0"

The status LED is **not one of the LEDs on the board**. It's a single WS2812 RGB pixel
that you wire to the header yourself (see [Wiring the LED](#wiring-the-led) below).
Riparr never touches the board's own LEDs. On a Banana Pro, that means the green LED1 and
the blue LED2.

The LED test says "No status LED detected" when the SPI device it writes to doesn't
exist. Two things on the box have to be true, and a stock image has neither:

1. **SPI has a device node.** Riparr defaults to `/dev/spidev1.0`, which is right for the
   Orange Pi Zero 2W. Other boards expose a different bus on their header.
2. **Riparr is allowed to open it.** The service runs as the `riparr` user, and Armbian
   creates the node as `root:root 0600`. There is no `spi` group until you make one.

Fixed on a **Banana Pro** (Allwinner A20, Armbian 6.18) on 2026-10-11. Over SSH as `root`:

```sh
# 1. Expose SPI0 as /dev/spidev0.0. Its header pins are PI12 MOSI (pin 19),
#    PI11 SCK (pin 23), PI10 CS0 (pin 24). Wire the LED's DIN to MOSI, pin 19.
#    If armbianEnv.txt already has an overlays= line, append spi-spidev to it instead.
cp -p /boot/armbianEnv.txt /boot/armbianEnv.txt.bak-led
echo "overlays=spi-spidev"      >> /boot/armbianEnv.txt
echo "param_spidev_spi_bus=0"   >> /boot/armbianEnv.txt

# 2. Tell Riparr where the LED is. The service reads this file at start. A card the
#    Preparer wrote for a Banana Pro already has this line (from boards.py), so this
#    step is only for a card written before it did -- check with `grep LED /boot/riparr.conf`.
echo "RIPARR_LED_SPI=/dev/spidev0.0" >> /boot/riparr.conf

# 3. Let the riparr user open it.
groupadd --system spi
echo 'SUBSYSTEM=="spidev", GROUP="spi", MODE="0660"' > /etc/udev/rules.d/90-spidev.rules
usermod -aG spi riparr

reboot
```

To check that it worked:

```sh
ls -l /dev/spidev0.0          # crw-rw---- 1 root spi 153, 0 ...
id riparr                     # ... 987(spi)
```

"Detected" only means that device node exists and Riparr can write to it. It can't tell
whether an LED is actually wired.

### Wiring the LED

Banana Pro, header **CON6**. Count pins by their **physical numbers (1–40)**. Many
pinout diagrams print a large "GPIO#" next to each pin, and that is WiringPi numbering,
not the pin's position. MOSI is "GPIO 12" in WiringPi but **physical pin 19**.

| WS2812 | CON6 physical pin | Diagram label |
|---|---|---|
| **DIN** | **19** | MOSI (SPI0), chip pin PI12 |
| **GND** | **20** (next to 19) or 6 | Ground |
| **VCC** | **1** (3.3 V), or 2 / 4 (5 V) with a level shifter | See below |

There are two ways to power the LED:

- **Simplest: VCC on pin 1 (3.3 V).** Verified to work with a NeoPixel. It is slightly
  under the WS2812B's rated 3.5 V, so the LED is dimmer and blue looks pale.
- **Full brightness: VCC on pin 2 or 4 (5 V), only with a level shifter.** Put a 74AHCT125
  or SN74AHCT1G125, powered from 5 V, between pin 19 and DIN. Without it, the 3.3 V data
  is below the LED's threshold and the first pixel just shows garbage white.

Leave pin 17 (also 3.3 V) unconnected. If you have a strip, cut it down to a single pixel.
Riparr drives only the first one, and on 3.3 V the others should not draw from the
board's 3.3 V rail.

**Orientation.** The header uses the Raspberry Pi B+ layout. Hold the board with CON6
along the top edge and the USB and Ethernet ports on the right:

- **Pin 1** is at the **left** end, the end away from the audio jack.
- The **inner row** (toward the middle of the board) carries the **odd** pins: 1, 3,
  5 … 39.
- The **edge row** carries the **even** pins: 2, 4, 6 … 40.

So pin 19 is the **10th pin from the left on the inner row**, with pin 20 directly
beside it on the edge row.

**Confirm that before you plug anything in.** Either check is enough:

- **Look at the underside.** Pin 1's solder pad is square. All the others are round.
- **Use a multimeter with the board powered**, measuring from a known ground such as the
  USB shield. Pins 2 and 4 read 5V, pin 1 reads 3.3V and pin 6 reads 0V.

**Two parts to add:**

- **A 300–470 Ω resistor in series with DIN.** It protects both the board's pin and the
  LED's input.
- **A 0.1 µF capacitor across VCC and GND, right at the LED.** Many WS2812 breakout boards
  already have one.

**Why 5 V needs a level shifter.** Measured on a Banana Pro: with a 5.3 V supply the
WS2812B needs about 3.7 V to read a logic 1, and the board's MOSI pin gives about 3.2 V,
so the LED read nothing. Powering the pixel from the 3.3 V rail made the data readable.

### Wiring a 74AHCT125 level shifter (full brightness on 5 V)

The 74AHCT125 is four separate buffers in one 14-pin chip. Powered from 5 V, its inputs
read anything above 2.0 V as a 1, and its outputs swing a full 5 V. One buffer turns the
board's 3.3 V data into a signal the LED reads reliably at 5 V.

**Get the right chip.** It must be **AHCT** or **HCT** (74AHCT125 or 74HCT125). A plain
74HC125 or 74AHC125 needs about 3.5 V on its input, which is the same problem you started
with.

**Unplug the box first.** Then move the LED's VCC wire off pin 1, because the LED goes back
on 5 V.

Pin 1 of the chip is marked by a notch or dot at one end. Pins count down that side 1–7,
then back up the other side 8–14.

| 74AHCT125 pin | Name | Connect to |
|---|---|---|
| **14** | VCC | CON6 **pin 2** (5 V) |
| **7** | GND | CON6 **pin 6** (ground) |
| **1** | 1OE | **Ground.** It enables buffer 1, and it is active low |
| **2** | 1A (input) | CON6 **pin 19** (MOSI) |
| **3** | 1Y (output) | A **330–470 Ω resistor**, then the LED's **DIN** |
| 4, 10, 13 | 2OE, 3OE, 4OE | **5 V.** This switches the three unused buffers off |
| 5, 9, 12 | 2A, 3A, 4A | **Ground.** Unused inputs must not float |
| 6, 8, 11 | 2Y, 3Y, 4Y | Nothing |

The LED itself:

| LED | Connect to |
|---|---|
| **VCC** | CON6 **pin 4** (5 V) |
| **GND** | CON6 **pin 20** (or any ground) |
| **DIN** | Through the resistor, from chip pin 3 |

Also:

- **Put a 0.1 µF capacitor across chip pins 14 and 7**, as close to the chip as you can.
- **Keep the 0.1 µF capacitor at the LED**, across its VCC and GND.
- **Every ground must be common:** the board's, the chip's and the LED's. A level shifter
  with its own separate ground does nothing.

**Single-buffer version.** An SN74AHCT1G125 (5-pin SOT-23) is the same thing in one gate:

| Pin | Connect to |
|---|---|
| 5 (VCC) | 5 V |
| 3 (GND) | Ground |
| 1 (OE) | Ground |
| 2 (A) | Pin 19 |
| 4 (Y) | The resistor, then DIN |

There are no unused inputs to tie off.

**Check it before closing the case.** Power on and press **Test the LED**. Red, green,
blue and white should now be full brightness, with a proper blue. If the LED shows garbage
white, the chip is probably an HC/AHC part, or OE (pin 1) isn't grounded.

Then press **Test the LED** in the **Status LED** section under **System**. That button
sends `POST /api/system/led/test`.
The LED should go red, green, blue, then white. If the colours come out in a different
order, the LED is RGB rather than GRB.

To find the right bus on another board, `ls /sys/class/spi_master/` lists the
controllers that are enabled. `/boot/dtb/overlay/README.*-overlays` names the header pins
for each bus. Set `param_spidev_spi_bus` to the bus your LED is wired to, and point
`RIPARR_LED_SPI` at `/dev/spidev<bus>.0`. Once it works, that line belongs in the board's
`conf` in `tools/preparer/boards.py`, so the Preparer writes it for everyone after you.

## Adding a power button

The Banana Pro already has one: **SW1**, wired to the power chip (AXP209) rather than to
the processor. That matters, because only the power chip can turn a box back *on* — a
button on a CON6 GPIO pin could shut it down but never wake it. So a button on your
enclosure goes **in parallel with SW1**, and then behaves exactly like it.

### Wiring

Use any momentary, normally-open push button and two thin wires.

1. **Unplug the box, and disconnect the battery if one is fitted.**
2. Set a multimeter to continuity, with one probe on a known ground (the USB socket's
   shield, or CON6 pin 6). Touch each of SW1's pads in turn. The pads that beep are
   **ground**. The ones that only beep while you hold SW1 down are **PWRON**.
3. Solder one wire to a ground pad and one to a PWRON pad, and run both to your button.
   Polarity doesn't matter.
4. Check before you close the case: with the box unplugged, pressing your button should
   make PWRON beep against ground, just like pressing SW1.

### What a press does

| You do | The box does |
|---|---|
| **Press once**, idle | Shuts down cleanly. |
| **Press once**, busy | Finishes everything it has started, including a disc it is still reading, all the way into your library and verified, then shuts down. Nothing new starts. Queued discs wait for next time. |
| **Press twice** quickly, busy | Finishes the step it's on, then shuts down. A rip finishes ripping, and its upload happens after you turn it back on. An upload finishes and verifies. A TV season stops after the current episode. |
| **Hold 1 s** while off | Turns on. |
| **Hold 6 s** | Cuts power immediately, without shutting down cleanly. Only for when nothing else works. |

While it waits, the status LED (if you've wired one) **blinks white** and every page shows a banner saying what
it's waiting on, with a **Cancel** button.

If Riparr itself isn't responding, a press still turns the box off within ten seconds.

## Starting over

**Settings → System → Export settings** first, if you can reach the box. Then re-flash the
card and import the file after setup. Takes about a minute.

## Reporting a bug

**Settings → System → Logs** downloads a bundle. Include:

- What the box appeared to be doing
- The disc (title, DVD/BD/UHD, studio)
- Your drive model
- Your card size

---

[← Settings reference](07-settings-reference.md) · [Guide index](README.md)
