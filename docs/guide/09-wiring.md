#  <#Wiring#>
author: cdower
copied from claude code output.

I've added the 74AHCT125 wiring to the troubleshooting guide on the power-button branch (commit d1f0ff5), under "Wiring a 74AHCT125 level shifter". It's the standard wiring for this chip, but I haven't tried it on your board.

Get the right chip. It has to be 74AHCT125 or 74HCT125. Plain HC or AHC parts need about 3.5 V on the input, which is the problem you already have.

Unplug the box first. The LED goes back on 5 V, so move its VCC wire off pin 1.

Pin 1 of the chip is next to the notch or dot. Pins 1–7 run down one side and 8–14 back up the other.

┌───────────┬────────────────────┬──────────────────────────────────────────┐
│ Chip pin  │        Name        │                Connect to                │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 14        │ VCC                │ CON6 pin 2 (5 V)                         │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 7         │ GND                │ CON6 pin 6 (ground)                      │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 1         │ 1OE                │ Ground. This switches buffer 1 on        │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 2         │ 1A, input          │ CON6 pin 19 (MOSI)                       │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 3         │ 1Y, output         │ A 330–470 Ω resistor, then the LED's DIN │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 4, 10, 13 │ Unused buffers' OE │ 5 V. This switches them off              │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 5, 9, 12  │ Unused inputs      │ Ground. Don't leave them floating        │
├───────────┼────────────────────┼──────────────────────────────────────────┤
│ 6, 8, 11  │ Unused outputs     │ Nothing                                  │
└───────────┴────────────────────┴──────────────────────────────────────────┘

The LED itself:

┌─────┬──────────────────────────────────┐
│ LED │            Connect to            │
├─────┼──────────────────────────────────┤
│ VCC │ CON6 pin 4 (5 V)                 │
├─────┼──────────────────────────────────┤
│ GND │ CON6 pin 20                      │
├─────┼──────────────────────────────────┤
│ DIN │ Chip pin 3, through the resistor │
└─────┴──────────────────────────────────┘

Also:
- Put a 0.1 µF capacitor across chip pins 14 and 7, as close to the chip as you can.
- Keep the one at the LED.
- All grounds must be shared: the board's, the chip's and the LED's.

Single-gate option: an SN74AHCT1G125 (5-pin) is wired 5 → 5 V, 3 → ground, 1 → ground, 2 → pin 19, and 4 → resistor → DIN.

Check: press Test the LED. The colours should be full brightness with a proper blue. If the LED goes back to garbage white, the chip is most likely the wrong family, or pin 1 isn't grounded.
