# Power button: shut down after the rip, or after the step

Date: 2026-10-10. Status: approved in conversation, awaiting spec review.

## Goal

A panel button on the enclosure turns the box off and, pressed again later, turns it back
on. Pressing it never throws away a rip:

- **One press** waits for the current rip to finish (in the library and verified), then
  shuts down.
- **Two presses** finish the current *step*, then shut down.

## What the hardware already does

Verified on the Banana Pro reference box (AXP209 PMIC, Armbian, kernel 6.18):

- The onboard button **SW1** is wired to the PMIC's **PWRON** pin (schematic, POWER-PMU
  page). SW2 is reset.
- The kernel exposes it as `axp20x-pek`, `/dev/input/by-path/platform-1c2ac00.i2c-platform-axp20x-pek-event`,
  tagged `power-switch`. systemd-logind watches it, and `HandlePowerKey` is the default,
  `poweroff`.
- PMIC timing (`/sys/bus/platform/devices/axp20x-pek/`): `startup` 1000 ms (hold 1 s to
  power on), `shutdown` 6000 ms (hold 6 s for a hard cut).

So power-on and the hard cut are the PMIC's job and need no software. A panel button wired
**in parallel with SW1** (PWRON and GND) behaves exactly like SW1. A button on a CON6
GPIO cannot power the board on, because nothing on the SoC is running to see it.

## Constraints found while designing

1. **The service account cannot take the power key.** `systemd-inhibit
   --what=handle-power-key --mode=block` as `riparr` returns `Access denied`. There is no
   polkit on the image, and as root it succeeds. The button therefore needs a root-side
   owner.
2. **Two jobs can be active at once.** In cache mode `_run_job` hands a ripped disc to
   `transferring` and returns. `_send_loop` uploads it while `_loop` rips the next disc.
3. **There are only two safe stopping points**, places where a reboot loses nothing.
   `rip.recover()` fails anything in `identifying`/`ripping`, and resets
   `transferring`/`verifying` to a full re-send.
   - **Ripped to the card, upload not started:** `transferring`, `bytes_sent=0`,
     `local_path` present. `_send_loop` picks it up on next boot.
   - **Final:** `done`.

   So a "step" is **rip** or **send+verify**, never finer. Stopping between upload and
   verify would waste the upload.
4. **Nothing can hold the rip engine today.** There is no pause or drain. `auto_rip`
   only gates auto-enqueue, and `_loop` still drains `queued`. The web Shut down does not
   check for an active job.

## Behaviour

| Input | Result |
|---|---|
| One or two presses, idle | Clean shutdown now (same path as the web Shut down) |
| One press, busy | Mode **after_rip**: nothing new starts. Every job already started when the button is pressed (identifying, ripping, transferring or verifying) is committed and runs through to `done` or `failed`/`cancelled`. Then shut down |
| Double press (second press within 0.6 s), busy | Mode **after_step**: nothing new starts. Each running job stops at its next safe point. Then shut down |
| Double press during after_rip | Switch to after_step |
| Single press during after_step | No change |
| Hold 6 s | PMIC hard cut (hardware, unchanged) |
| Hold 1 s while off | PMIC power-on (hardware, unchanged) |

"Busy" means at least one job in `INTERRUPTIBLE` (`identifying`, `ripping`,
`transferring`, `verifying`).

A single press commits the jobs that have started at that moment. A committed job runs
to a final state even if it passes through `queued` on the way, as a disc does when its
scan ends, and the shutdown waits for it; it stops counting at `needs_input`, because a
question nobody is answering must not hold the box up. Jobs that were already `queued`
at the press wait for next boot. A double press ignores the committed set.

`queued` and `needs_input` jobs do not block shutdown. They survive a reboot untouched
and resume afterwards.

**The safe point in after_step, per running job:**

| Job is | Stops after |
|---|---|
| `identifying` | Identification. The job becomes `queued` and waits for next boot |
| `ripping`, cache mode | The rip. The handoff to `transferring` happens and the upload waits for next boot |
| `ripping`, direct mode | The rip and its inline verify/finish. The file is already in the library, so this is one step |
| `ripping`, TV season | The rip. Same as cache mode |
| `transferring`/`verifying`, single file | The upload and verify, through to `done` |
| `transferring`, TV season | The current episode, uploaded and verified. Done episodes are skipped on next boot |

**Cancelling** is done from the web interface only. The button has no cancel gesture, so
that one press always means "shut down".

A pending shutdown is **not persisted**. A reboot by any other route clears it.

## Components

### 1. `packaging/powerkey.py` and `packaging/riparr-powerkey.service` (root, new)

```
ExecStart=/usr/bin/systemd-inhibit --what=handle-power-key --mode=block \
          --who=Riparr --why="Riparr decides when to shut down" \
          /usr/bin/python3 /usr/local/lib/riparr/powerkey.py
Restart=on-failure
```

`powerkey.py` is standard library only, matching the other root-side helpers.

- **Finds the device.** It scans `/sys/class/input/event*/device/capabilities/key` for the
  `KEY_POWER` bit (116). It does not hardcode `axp20x-pek`, so any board with a power key
  works. If nothing is found it exits 0, and `Restart=on-failure` leaves it stopped.
- **Reads events.** It reads `struct input_event` using the native layout
  (`struct.calcsize("llHHi")`; 16 bytes on armhf, 24 on arm64) and acts on `EV_KEY`,
  code 116, value 1 (press).
- **Classifies presses.** A press opens a 0.6 s window. A second press inside it means
  `double`; otherwise, when the window closes, it's `single`.
- **Hands off.** It writes `single` or `double` to `/run/riparr/powerkey.request`
  (atomic: temp file, then rename).
- **Falls back.**
  - If `riparr.service` is not active, it calls `systemctl poweroff` directly.
  - If the request file is still present 10 s after writing (Riparr did not consume it),
    it calls `systemctl poweroff`.
  - Either way the button is never dead.
- When the service stops, the inhibitor lock is released and logind's default
  (`poweroff`) applies again.

### 2. `server/riparr/power.py` (new)

```python
MODES = (None, "after_rip", "after_step")

def request(kind)        # "single" | "double"; applies the table above
def cancel()             # -> None
def pending()            # -> {"mode", "since", "waiting_on": [job ids]} or None
def holding()            # True while any mode is set: rip.py asks this before starting work
def at_step_boundary()   # True in after_step: rip.py asks this at each safe point
def tick()               # called once a second; consumes powerkey.request, shuts down when clear
```

- **`tick()` runs on the LED thread's one-second loop**, so there is no new thread. When
  `pending()` has no `waiting_on` jobs, it calls `P.power_action("poweroff")` once.
- **State is a module-level dict behind a lock.** The rip, send and LED threads all read
  it.
- **It logs** through `SY.component("Power")`: the request, its mode, what it is waiting
  on, and the shutdown.

### 3. `rip.py` gates

| Where | Check | Effect |
|---|---|---|
| `_loop`, before `db.next_queued_job()` | `power.holding()` | No new rip starts |
| `_watch_discs`, before `enqueue()` | `power.holding()` | No auto-rip |
| `enqueue()` (manual, from the API) | `power.holding()` | Refused: "Shutting down — cancel it to rip" |
| `_send_loop`, before `db.next_sending_job()` | `power.at_step_boundary()` | No new upload starts. Uploads already running continue |
| `_run_job` | `power.at_step_boundary()` after identify | Job goes to `queued` and returns |
| `_run_job`, cache-mode handoff | none | Already returns after the rip, so nothing changes |
| Season transfer loop | `power.at_step_boundary()` between episodes | Stops, leaving the job `transferring` so `recover()` resumes it |

In after_rip mode the send loop **keeps** draining, because "the rip is finished" means
it is in the library.

### 4. LED

- **New state** in `led.STATES`: `"shutting_down": (WHITE, "blink")`.
- **Priority.** `current_state()` returns it while `power.pending()` is set, ahead of the
  job-derived states. While shutting down, that is the most important thing the box can
  say.
- `app.js` `LED_WORDS` gets the matching entry.

### 5. Web

- **`GET /api/status`** gains `power_pending` (the `pending()` value).
- **`POST /api/system/power/cancel`** (`require_user`) calls `power.cancel()`.
- **The UI** shows a banner on every page while pending: "Shutting down after this
  rip" or "Shutting down after this step", what it is waiting on, and a **Cancel**
  button.
- **The web Shut down is unchanged.** It is immediate, as now. Routing it through the
  drain is a separate decision.

### 6. Install

`apply-system.sh`:

- installs `powerkey.py` to `/usr/local/lib/riparr/`
- adds `riparr-powerkey.service` to the unit list
- runs `enable --now` on it (it is a plain service, not a path door)

The status table in `platform.py` (around line 1184) gets a row for it.

### 7. Docs

- **`08-troubleshooting.md`**, or a new section in `01-what-you-need.md` (whichever
  already owns enclosure wiring): wiring a panel button in parallel with SW1, telling the
  two pads apart with a multimeter (one is continuous with GND), and the press table.
- `CHANGELOG.md` entry.

## Error handling

- **Riparr down or hung:** `powerkey.py` powers off after 10 s. The cost is that a running
  rip is lost, which is the same as today's behaviour.
- **`powerkey.py` down:** logind's default handles the key, and a press powers off
  immediately. This is also the same as today.
- **A job fails or is cancelled while draining:** it reaches a final state, and the drain
  proceeds.
- **`P.power_action` returns not-ok** (old install without the door): log it, clear the
  mode, and show the reason in the banner.
- **A job stuck in `needs_input`:** it does not block, as above.

## Testing

There is no server test suite, so testing is a self-check script plus the real box.

1. **`server/riparr/power_selftest.py`** (no hardware):
   - single/double classification from synthetic timestamps
   - the mode transitions in the behaviour table
   - `waiting_on` against a fake set of job rows covering every state
   - the 10 s fallback, with the clock injected
2. **On the Banana Pro**, asking before each shutdown:
   - idle single press → powers off; 1 s hold → boots
   - single press mid-rip → LED blinks white, the rip and upload complete, `done`, power
     off
   - double press mid-rip (cache mode) → the rip completes and the box powers off; on
     boot the job uploads
   - cancel from the web → the box keeps running and auto-rip resumes
   - `systemctl stop riparr` then a press → powers off within 10 s
   - `systemctl stop riparr-powerkey` then a press → logind powers off immediately

## Out of scope

- Routing the web Shut down through the drain.
- Persisting a pending shutdown across reboots.
- A cancel gesture on the button.
- Long-press handling below the PMIC's 6 s cut.
