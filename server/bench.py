#!/usr/bin/env python3
"""A repeatable benchmark of the paths the browser polls, with CPU seconds and hot stacks.

The Riparr page asks for the queue, the status and the autorip state every second or so,
and a Pi Zero 2 spends real CPU answering. This starts the service in mock mode, signs
in, drives that same mix, and reports per-endpoint latency, how much CPU the process
burned idle and under load, and (from the profiler's local dump) which frames the time
went to. Tasks that make the service faster run it before and after, so the numbers
below are what they quote.

Run, from server/:

    uv run --locked python bench.py [--cycles 200] [--idle 15] [--json PATH] [--keep DIR]
    uv run --locked python bench.py --url http://riparr.local:9797 --user U --password P

Standard library only, so it runs anywhere the service does. The one import from the
service is `riparr.profiling.MODE`, to say in the output whether the profile counts CPU
time (Linux) or wall time (macOS, where every sleeping thread looks busy).
"""
import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TOP = 15
POLLED = ["/api/queue", "/api/status", "/api/autorip"]
# The power page is polled less often than the rest, so the mix reaches it every third
# cycle rather than every one.
POWER = "/api/system/power"
POWER_EVERY = 3
USER = "bench"
PASSWORD = "benchbench1"


class Client:
    """One keep-alive connection that carries the session cookie back, like a browser."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.conn = http.client.HTTPConnection(host, port, timeout=30)
        self.cookie = None

    def reconnect(self):
        """Uvicorn closes a connection that sat idle for five seconds, as it would for
        a browser, which just opens another. The idle phase is longer than that."""
        self.conn.close()
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=30)

    def request(self, method, path, body=None):
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        self.conn.request(method, path, data, headers)
        resp = self.conn.getresponse()
        payload = resp.read()
        for name, value in resp.getheaders():
            if name.lower() == "set-cookie":
                self.cookie = value.split(";", 1)[0]
        return resp.status, payload


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(tmp, port):
    """Launch the service on a throwaway database and wait until it answers."""
    env = dict(os.environ, RIPARR_DB=os.path.join(tmp, "bench.db"), RIPARR_APPLIANCE="0",
               RIPARR_MOCK_FAST="1", RIPARR_PROFILE_DIR=os.path.join(tmp, "prof"),
               PYTHONUNBUFFERED="1")
    # stderr goes to a file, not a pipe: a pipe nobody reads fills and stalls the
    # service, and a file lets a failed start show what the child said.
    errlog = open(os.path.join(tmp, "server.err"), "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "riparr.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=errlog)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if Client("127.0.0.1", port).request("GET", "/api/setup/state")[0] == 200:
                return proc
        except OSError:
            pass
        time.sleep(0.2)
    proc.kill()
    proc.wait()
    errlog.seek(0)
    sys.stderr.write("The service did not start within 30 s:\n" + errlog.read())
    sys.exit(2)


def login(client, user, password, first_run):
    """Sign in, creating the account and finishing setup first on a fresh database."""
    creds = {"username": user, "password": password}
    if first_run:
        status, _ = client.request("POST", "/api/setup/user", creds)
        if status != 200:
            sys.exit("Creating the account failed with HTTP %d" % status)
    if not client.cookie:
        status, _ = client.request("POST", "/api/auth/login", creds)
        if status != 200:
            sys.exit("Signing in failed with HTTP %d" % status)
    if first_run:
        status, _ = client.request("POST", "/api/setup/complete")
        if status != 200:
            sys.exit("Completing setup failed with HTTP %d" % status)


def cputime(pid):
    """CPU seconds the process has used, from ps, or None when ps cannot say.

    `ps` is used rather than /proc because /proc does not exist on the development Mac.
    The output is [[dd-]hh:]mm:ss on both platforms, with a fraction on macOS. Linux
    reports whole seconds, so a short idle phase there reads coarsely.
    """
    try:
        out = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        days = 0
        if "-" in out:
            d, out = out.split("-", 1)
            days = int(d)
        seconds = 0.0
        for part in out.split(":"):
            seconds = seconds * 60 + float(part)
        return days * 86400 + seconds
    except ValueError:
        return None


def percentile(sorted_values, p):
    """Nearest-rank percentile of an already sorted list."""
    rank = max(1, -(-len(sorted_values) * p // 100))
    return sorted_values[int(rank) - 1]


def cycle(client, n, latencies=None):
    """One turn of the browser's polling mix, timing each request when asked."""
    paths = POLLED + ([POWER] if n % POWER_EVERY == 0 else [])
    for path in paths:
        t0 = time.perf_counter()
        status, _ = client.request("GET", path)
        elapsed = (time.perf_counter() - t0) * 1000
        if status != 200:
            sys.exit("GET %s answered HTTP %d" % (path, status))
        if latencies is not None:
            latencies.setdefault(path, []).append(elapsed)


def idle(pid, seconds):
    """Make no requests and see what the process spends anyway."""
    before = cputime(pid)
    time.sleep(seconds)
    after = cputime(pid)
    if before is None or after is None:
        return {"seconds": seconds, "cpu_seconds": None, "percent_of_core": None}
    used = max(0.0, after - before)
    return {"seconds": seconds, "cpu_seconds": round(used, 3),
            "percent_of_core": round(used / seconds * 100, 2) if seconds else None}


def poll(client, pid, cycles):
    """Drive the polling mix back to back and report latency and CPU per cycle."""
    latencies = {}
    before = cputime(pid) if pid else None
    t0 = time.perf_counter()
    for n in range(1, cycles + 1):
        cycle(client, n, latencies)
    wall = time.perf_counter() - t0
    after = cputime(pid) if pid else None
    cpu = None if before is None or after is None else max(0.0, after - before)
    endpoints = {}
    for path, values in latencies.items():
        values.sort()
        endpoints[path] = {"count": len(values),
                           "p50_ms": round(percentile(values, 50), 3),
                           "p95_ms": round(percentile(values, 95), 3),
                           "max_ms": round(values[-1], 3)}
    return {"endpoints": endpoints, "wall_seconds": round(wall, 3),
            "cpu_seconds": None if cpu is None else round(cpu, 3),
            "cpu_ms_per_cycle": None if cpu is None else round(cpu / cycles * 1000, 2)}


def stop(proc):
    """SIGTERM so the shutdown hook flushes the profiler's last window, then wait."""
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def profile_report(prof_dir):
    """Top frames by self and inclusive samples from every closed window on disk."""
    self_counts, inclusive, total = {}, {}, 0
    for name in sorted(os.listdir(prof_dir)) if os.path.isdir(prof_dir) else []:
        if not name.endswith(".folded"):
            continue
        with open(os.path.join(prof_dir, name)) as f:
            for line in f:
                stack, _, count = line.rstrip("\n").rpartition(" ")
                if not stack or not count.isdigit():
                    continue
                count = int(count)
                frames = stack.split(";")
                total += count
                self_counts[frames[-1]] = self_counts.get(frames[-1], 0) + count
                # A recursive frame appears several times in one stack but is one
                # stack's worth of time, so each frame counts once per line.
                for frame in set(frames):
                    inclusive[frame] = inclusive.get(frame, 0) + count

    def top(counts):
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]
        return [{"frame": f, "samples": c,
                 "percent": round(c / total * 100, 2) if total else 0.0}
                for f, c in ranked]
    return {"samples": total, "self": top(self_counts), "inclusive": top(inclusive)}


def fmt(value):
    return "n/a" if value is None else str(value)


def markdown(result, mode, cycles):
    out = ["# Riparr benchmark", ""]
    idle_ = result.get("idle")
    if idle_:
        out += ["## Idle", "",
                "| Seconds | CPU seconds | % of one core |", "|---|---|---|",
                "| %s | %s | %s |" % (idle_["seconds"], fmt(idle_["cpu_seconds"]),
                                      fmt(idle_["percent_of_core"])), ""]
    p = result["poll"]
    out += ["## Poll (%d cycles)" % cycles, "",
            "| Endpoint | Count | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|"]
    for path, e in p["endpoints"].items():
        out.append("| %s | %d | %.2f | %.2f | %.2f |"
                   % (path, e["count"], e["p50_ms"], e["p95_ms"], e["max_ms"]))
    out += ["", "Wall %s s, CPU %s s, **%s CPU ms per cycle**."
            % (p["wall_seconds"], fmt(p["cpu_seconds"]), fmt(p["cpu_ms_per_cycle"])), ""]
    if "self" in result:
        for key in ("self", "inclusive"):
            out += ["## Hottest frames by %s samples (%s profile, %d samples)"
                    % (key, mode, result["samples"]), "",
                    "| Frame | Samples | % |", "|---|---|---|"]
            out += ["| `%s` | %d | %.2f |" % (r["frame"], r["samples"], r["percent"])
                    for r in result[key]]
            out.append("")
        if mode == "wall":
            out += ["This is a wall-clock profile (macOS has no per-thread CPU clock "
                    "here), so threads that are asleep count as busy. Read the shape, "
                    "not the percentages.", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cycles", type=int, default=200)
    ap.add_argument("--idle", type=float, default=15)
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--keep", metavar="DIR", help="copy the .folded files here")
    ap.add_argument("--url", help="benchmark a running box instead of starting one")
    ap.add_argument("--user", default=USER)
    ap.add_argument("--password", default=PASSWORD)
    args = ap.parse_args()

    result = {}
    mode = None
    if args.url:
        u = urllib.parse.urlsplit(args.url)
        client = Client(u.hostname, u.port or 80)
        login(client, args.user, args.password, first_run=False)
        cycle(client, 1)
        result["poll"] = poll(client, None, args.cycles)
    else:
        from riparr import profiling
        mode = profiling.MODE
        tmp = tempfile.mkdtemp(prefix="riparr-bench-")
        port = free_port()
        proc = start(tmp, port)
        try:
            client = Client("127.0.0.1", port)
            login(client, USER, PASSWORD, first_run=True)
            cycle(client, 1)                              # warm-up, not measured
            result["idle"] = idle(proc.pid, args.idle)
            client.reconnect()
            result["poll"] = poll(client, proc.pid, args.cycles)
        finally:
            stop(proc)
        prof_dir = os.path.join(tmp, "prof")
        result.update(profile_report(prof_dir))
        if args.keep:
            os.makedirs(args.keep, exist_ok=True)
            for name in os.listdir(prof_dir) if os.path.isdir(prof_dir) else []:
                if name.endswith(".folded"):
                    shutil.copy(os.path.join(prof_dir, name), args.keep)
        shutil.rmtree(tmp, ignore_errors=True)

    print(markdown(result, mode, args.cycles))
    if args.json:
        data = {"idle": result.get("idle"), "poll": result["poll"]["endpoints"],
                "cpu_ms_per_cycle": result["poll"]["cpu_ms_per_cycle"],
                "self": result.get("self", []), "inclusive": result.get("inclusive", [])}
        with open(args.json, "w") as f:
            json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
