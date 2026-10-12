# Riparr service

The appliance itself: FastAPI + SQLite in a single process, serving a static frontend
(D2). No separate database, no separate web server, no container runtime — the binding
constraint is 512 MB of RAM.

```sh
./run.sh              # http://localhost:8000
```

Off the Pi it runs in **mock mode**: `riparr/platform.py` reports simulated hardware, a
simulated drive with a disc in it, and discoverable network shares, so the whole interface
is exercisable on a laptop. `IS_APPLIANCE` is the only switch.

## Layout

| File | Purpose |
|---|---|
| `riparr/main.py` | The API and static serving |
| `riparr/platform.py` | **Everything that differs between the appliance and a Mac.** Nothing else shells out. |
| `riparr/db.py` | SQLite. Settings are typed key/value; WAL, because the cable gets yanked (D4) |
| `riparr/profiling.py` | The sampling profiler and its Pyroscope uploader |
| `riparr/shares.py` | SMB discovery, and the write test that proves a share works |
| `riparr/updater.py` | Update check and install against the official repository |
| `static/` | The web UI |
| `static/themes/` | theme.park palettes (MIT), plus `servarr.css` |

## The interface

Follows *arr-family conventions closely enough to be immediately familiar: sub-navigation
in the sidebar expanded under the active section, count badges, a search underline beside
the logo, an icon-over-label toolbar, and flat underlined section headings.

All colour resolves through **theme.park**'s custom-property contract, so a user already
theming their *arr stack themes Riparr for free. Eleven palettes ship, totalling 14 KB.

> **`servarr.css` carries Sonarr's exact palette and dimensions, and Sonarr is GPL-3.0.**
> The licensing consequence is unresolved and deliberately
> confined to that one file plus a handful of constants in `app.css`.

## Two things worth knowing

**A share is not saved until it is proven.** `POST /api/shares` writes a real file, reads
it back, compares it and deletes it before persisting anything. An SMB write returning
success is not proof of a good file (D6), and a wrong share path otherwise surfaces at 3am
on the first rip rather than during setup.

**Capacity is reported as a mode, not a number.** Under D11 a buffer too small for the
next disc means *stream mode*, not a refusal. `/api/status` returns
`{discs_free, mode, phrase}` and the UI renders the phrase, because "no room" would
contradict the design and push people toward larger cards for a benefit that doesn't
exist.

## Tests

Each `*.test.py` beside this file is a standalone script:

```sh
uv run python music-pipeline.test.py
```

`music-pipeline.test.py` drives the real audio tools rather than mocks, and fails at the
start if any of them is missing. On a Mac they come from Homebrew:

```sh
brew install libcdio libcdio-paranoia flac lame opus-tools vorbis-tools ffmpeg
```

| Tool | Formula |
|---|---|
| `cd-info` | `libcdio` |
| `cd-paranoia` | `libcdio-paranoia` |
| `flac`, `metaflac` | `flac` |
| `lame` | `lame` |
| `opusenc` | `opus-tools` (not `libopusenc`, which is only the library) |
| `oggenc` | `vorbis-tools` |
| `ffmpeg`, `ffprobe` | `ffmpeg` |
| `bsdtar` | ships with macOS |

The others need the service's own dependencies (`uv run` has them), and CI runs them
all:

- `profiling.test.py`: the sampler weights stacks by CPU time and uploads folded text.
- `profiling-settings.test.py`: the profiling settings save, mask the token and apply live.
- `status-cache.test.py`: each cache behind `/api/status` expires and invalidates.
- `progress-throttle.test.py`: MakeMKV progress is written at most twice a second, and three handlers run off the event loop.
- `bench.test.py`: the benchmark starts a real server and reports every figure it promises.

`bench.py` is not a test but lives beside them. It starts the service in mock mode on a
throwaway database, signs in, and drives the mix the browser polls (the queue, the status
and the autorip state each cycle, the power page every third). It reports each endpoint's
p50, p95 and worst latency, the process's CPU seconds idle and per cycle under load, and
the hottest frames from the profiler's local dump. On macOS that profile counts wall
time, so read its shape rather than its percentages. `--json PATH` saves the numbers and
`--keep DIR` keeps the `.folded` files, which load in speedscope.app as "collapsed
stacks". `--no-profile` runs the service with the profiler off and skips the profile
tables, so two runs, with and without it, show what the sampler itself costs. Run it
before and after a change that is meant to make the service cheaper:

```sh
uv run --locked python bench.py
uv run --locked python bench.py --url http://riparr.local:9797 --user U --password P
```

The second form skips the idle and profile parts and measures a real box over the network.

The data-CD case builds its ISO with `xorriso` when there is one and falls back to
macOS's own `hdiutil`, so `xorriso` is optional here. CI installs the Debian equivalents;
see the music step in `.github/workflows/ci.yml`.

## API

Self-documenting at `/api/docs`. The UI is only the first client of it — that is what
makes Homepage widgets and multi-unit setups nearly free later.

## Not built

**The rip engine.** Queue, history and disc history read real tables that nothing
populates yet. Disc detection, MakeMKV invocation, the follow-copy uploader and the
fingerprint cache are all still ahead.
