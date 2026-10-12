#!/usr/bin/env python3
"""The benchmark runs end to end and reports what it promises.

The bench is what Tasks 4 and 5 quote before and after, so a bench that quietly prints
nothing useful would make every later claim worthless. This runs it small (six cycles,
one second idle) against a throwaway mock-mode service and checks the shape of what
comes out, not the numbers themselves, which belong to the machine. A second, smaller run
with `--no-profile` checks that the baseline for the profiler's own cost still runs and
says the profiler was off.

Run: uv run --locked python bench.test.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


def bench(*args):
    # Its own session, so a timeout can take the bench and the server it started down
    # together. Killing only the bench with SIGKILL would skip its cleanup and leave
    # uvicorn running.
    child = subprocess.Popen(
        [sys.executable, "bench.py", *args],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        raise
    return subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)


with tempfile.TemporaryDirectory() as tmp:
    out = os.path.join(tmp, "out.json")
    keep = os.path.join(tmp, "kept")
    run = bench("--cycles", "6", "--idle", "1", "--json", out, "--keep", keep)
    if run.returncode:
        print(run.stdout, run.stderr)
    check("exit status", run.returncode, 0)
    check("status row in the table", "| /api/status |" in run.stdout, True)
    check("idle heading", "Idle" in run.stdout, True)
    check("self heading", "self" in run.stdout, True)
    check("inclusive heading", "inclusive" in run.stdout, True)

    data = json.load(open(out)) if os.path.exists(out) else {}
    check("p50 is positive", data.get("poll", {}).get("/api/status", {}).get("p50_ms", 0) > 0, True)
    check("idle cpu is reported", data.get("idle", {}).get("cpu_seconds", -1) >= 0, True)
    check("self list is not empty", bool(data.get("self")), True)
    check("folded files kept",
          bool(os.path.isdir(keep) and [f for f in os.listdir(keep) if f.endswith(".folded")]),
          True)

    run = bench("--no-profile", "--cycles", "3", "--idle", "1")
    if run.returncode:
        print(run.stdout, run.stderr)
    check("--no-profile exits 0", run.returncode, 0)
    check("--no-profile says the profiler was off",
          "The profiler was off for this run" in run.stdout, True)
    check("--no-profile still measures the poll", "| /api/status |" in run.stdout, True)
    check("--no-profile prints no profile tables", "Hottest frames" in run.stdout, False)

if failures:
    print(f"\n{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("\nall passed")
