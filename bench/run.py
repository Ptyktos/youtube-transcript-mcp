#!/usr/bin/env python3
"""Benchmark orchestrator for youtube-transcript-mcp vs. other implementations.

    python3 bench/run.py micro          # in-process: Rust core vs TS port vs jdepoix
    python3 bench/run.py mock           # MCP-stdio servers vs local mock (LAN + PROD-sim)
    python3 bench/run.py live           # MCP-stdio servers + yt-dlp vs real YouTube
    python3 bench/run.py all            # micro + mock
    python3 bench/run.py report         # re-render results/*.md and chart from JSON

Run bench/setup.sh first. Each suite writes bench/results/<suite>.json (raw
samples + environment) and bench/results/<suite>.md (tables). `mock` also
renders bench/results/youtube-latency-memory.svg.

Options: --rounds N (fresh process per round, default 3), --iters N (calls per
round, default 50), --port N (mock port, default 18080), --quick (1 round,
10 iters; for smoke-testing the harness only — don't publish these).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import statistics as st
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
WORK = BENCH / ".work"
RESULTS = BENCH / "results"

OUR_BIN = REPO / "target" / "release" / "youtube-transcript-mcp"
RUST_CLIENT = BENCH / "rust-client" / "target" / "release" / "rust-bench-client"
TS_CLIENT = BENCH / "ts-client" / "dist" / "main.js"
PY = WORK / "venv" / "bin" / "python"
JDEPOIX = BENCH / "python-jdepoix" / "main.py"
DRIVE = BENCH / "mcp-client" / "drive.js"
MOCK = BENCH / "mock" / "server.py"
FAKE_YTDLP_DIR = BENCH / "fake-yt-dlp"
ANAIS = WORK / "anaisbetts" / "package" / "dist" / "index.js"
NABID = WORK / "nabid" / "package" / "dist" / "index.js"
NABID_MOCK = WORK / "nabid-mock" / "package" / "dist" / "index.js"
SPINAL = WORK / "spinalshock" / "spinalshock-mcp"
SPINAL_NORL = WORK / "spinalshock" / "spinalshock-mcp-norl"

MOCK_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# Live-mode videos: public, captioned, a spread of lengths. Override with --videos.
LIVE_VIDEOS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # 3.5 min music video
    "https://www.youtube.com/watch?v=aircAruvnKk",  # 19 min lecture (3Blue1Brown)
    "https://www.youtube.com/watch?v=zjkBMFhNj_g",  # 60 min talk (Karpathy, Intro to LLMs)
    "https://www.youtube.com/watch?v=kCc8FmEb1nY",  # 116 min lecture (Karpathy, GPT from scratch)
]


# ── helpers ──────────────────────────────────────────────────────────────────

def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q / 100
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def environment() -> dict:
    cpu = ""
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.startswith("model name"):
                cpu = ln.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = platform.processor()
    versions = {}
    if (WORK / "versions.json").exists():
        versions = json.loads((WORK / "versions.json").read_text())
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_sha": sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"]),
        "git_dirty": bool(sh(["git", "-C", str(REPO), "status", "--porcelain", "--", "crates"])),
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": cpu,
        "cores": os.cpu_count(),
        "rustc": sh(["rustc", "--version"]),
        "node": sh(["node", "--version"]),
        "python": platform.python_version(),
        "go": sh(["go", "version"]),
        "binary_bytes": OUR_BIN.stat().st_size if OUR_BIN.exists() else None,
        "third_party": versions,
    }


def run_json(cmd: list[str], env: dict | None = None, timeout: int = 900) -> dict:
    p = subprocess.run(cmd, capture_output=True, text=True, env=env or os.environ, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd[:3]))}… failed: {(p.stderr or p.stdout).strip()[:400]}")
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("{")), None)
    if line is None:
        raise RuntimeError(f"no JSON from {cmd[:3]}: {p.stdout[:200]}")
    return json.loads(line)


class Mock:
    def __init__(self, port: int, delay_ms: int):
        self.port, self.delay_ms = port, delay_ms

    def __enter__(self):
        env = {**os.environ, "MOCK_DELAY_MS": str(self.delay_ms)}
        self.p = subprocess.Popen([sys.executable, str(MOCK), str(self.port)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as r:
                    if r.read() == b"ok":
                        return self
            except Exception:
                time.sleep(0.05)
        self.p.kill()
        raise RuntimeError(f"mock did not start on port {self.port} (already in use?)")

    def __exit__(self, *exc):
        self.p.terminate()
        try:
            self.p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.p.kill()


def require(*paths: Path):
    missing = [str(p.relative_to(REPO)) for p in paths if not p.exists()]
    if missing:
        raise SystemExit("missing (run bench/setup.sh first): " + ", ".join(missing))


# ── MCP-stdio driving ────────────────────────────────────────────────────────

def drive(mode: str, iters: int, impl: dict, urls: list[str], env: dict, timeout_ms: int = 120000) -> dict:
    cmd = ["node", str(DRIVE), mode, str(iters), impl["tool"], ",".join(urls),
           "--arg-name", impl.get("arg", "url"), "--timeout-ms", str(timeout_ms)]
    if impl.get("extra"):
        cmd += ["--extra", json.dumps(impl["extra"])]
    cmd += ["--", *impl["argv"]]
    return run_json(cmd, env=env, timeout=timeout_ms * max(iters, 1) // 1000 + 60)


def bench_impl(impl: dict, urls: list[str], env: dict, rounds: int, iters: int, colds: int,
               pause_s: float = 0.0) -> dict:
    """`colds` fresh-process cold starts, then `rounds` fresh processes × `iters` calls."""
    cold_ms, cold_ok = [], []
    for _ in range(colds):
        r = drive("cold", 1, impl, urls, env)
        cold_ms.append(r["ms"])
        cold_ok.append(r["ok"])
        time.sleep(pause_s)
    samples, ok, payload, rss = [], [], [], []
    for _ in range(rounds):
        r = drive("e2e", iters, impl, urls, env)
        samples += r["samples"]
        ok += r["ok"]
        payload += r["bytes"]
        rss.append(r["rssKib"])
        time.sleep(pause_s)
    good = [s for s, k in zip(samples, ok) if k]
    return {
        "key": impl["key"], "label": impl["label"], "kind": impl["kind"],
        "cold_ms": cold_ms, "cold_ok": cold_ok, "samples_ms": samples, "ok": ok,
        "payload_bytes": payload, "rss_kib": rss,
        "summary": {
            "cold_ms_median": st.median(cold_ms) if cold_ms else None,
            "p50_ms": pct(good, 50), "p95_ms": pct(good, 95), "p99_ms": pct(good, 99),
            "throughput_rps": len(good) * 1000 / sum(good) if good else 0.0,
            "success_rate": sum(ok) / len(ok) if ok else 0.0,
            "rss_mib_max": max(rss) / 1024 if rss else None,
            "payload_bytes_median": st.median([b for b, k in zip(payload, ok) if k]) if any(ok) else None,
        },
    }


def mcp_impls(mode: str) -> list[dict]:
    """Every MCP server under test. mode = 'mock' or 'live'."""
    nabid = NABID_MOCK if mode == "mock" else NABID
    return [
        {"key": "this-repo", "label": "this repo (Rust, rmcp stdio)", "kind": "direct-innertube",
         "tool": "get_transcript", "argv": [str(OUR_BIN), "--stdio"]},
        {"key": "nabid", "label": "nabid-pf youtube-video-summarizer-mcp (Node)", "kind": "direct-innertube",
         "tool": "get-video-info-for-summary-from-url", "arg": "videoUrl", "argv": ["node", str(nabid)]},
        {"key": "anaisbetts", "label": "anaisbetts/mcp-youtube (Node + yt-dlp)", "kind": "yt-dlp",
         "tool": "download_youtube_url", "argv": ["node", str(ANAIS)]},
        {"key": "spinalshock-norl", "label": "spinalshock (Go + yt-dlp), rate-limit sleep removed", "kind": "yt-dlp",
         "tool": "get_transcript", "argv": [str(SPINAL_NORL)]},
        {"key": "spinalshock", "label": "spinalshock (Go + yt-dlp), as shipped", "kind": "yt-dlp",
         "tool": "get_transcript", "argv": [str(SPINAL)], "max_iters": 5},
    ]


# ── suites ───────────────────────────────────────────────────────────────────

def suite_micro(a) -> dict:
    require(RUST_CLIENT, TS_CLIENT, PY)
    iters = 5000
    clients = {
        "rust": [str(RUST_CLIENT)],
        "ts": ["node", str(TS_CLIENT)],
        "python": [str(PY), str(JDEPOIX)],
    }
    out = {"suite": "micro", "env": environment(), "params": {"parse_iters": iters, "e2e_iters": 100,
                                                              "cold_runs": 5, "mock_delay_ms": 0}, "rows": {}}
    env = {**os.environ, "MOCK_URL": f"http://127.0.0.1:{a.port}", "NODE_NO_WARNINGS": "1"}
    with Mock(a.port, 0):
        for name, argv in clients.items():
            print(f"  micro: {name}", flush=True)
            row = {}
            row["parse_xml"] = run_json(argv + ["parse", str(iters)], env)
            row["parse_json"] = run_json(argv + ["parse-json", str(iters)], env)
            row["cold_ms"] = [run_json(argv + ["cold"], env)["ms"] for _ in range(5)]
            row["e2e"] = run_json(argv + ["e2e", "100"], env)
            row["memory"] = run_json(argv + ["memory", "100"], env)
            out["rows"][name] = row
    return out


def suite_mock(a) -> dict:
    require(OUR_BIN, DRIVE, ANAIS, NABID_MOCK, SPINAL, SPINAL_NORL)
    rounds, iters = (1, 10) if a.quick else (a.rounds, a.iters)
    out = {"suite": "mock", "env": environment(),
           "params": {"rounds": rounds, "iters": iters, "cold_runs": 5, "port": a.port,
                      "fixture": "bench/canned/caption.xml (80 KiB, 1000 cues)", "video": MOCK_VIDEO},
           "profiles": []}
    for prof, delay in (("LAN", 0), ("PROD-sim", 80)):
        base = {**os.environ, "MOCK_DELAY_MS": str(delay),
                "YTMCP_INNERTUBE_URL": f"http://127.0.0.1:{a.port}/youtubei/v1/player",
                "PATH": f"{FAKE_YTDLP_DIR}:{os.environ.get('PATH', '')}"}
        rows = []
        with Mock(a.port, delay):
            for impl in mcp_impls("mock"):
                n = min(iters, impl.get("max_iters", iters))
                c = 3 if impl.get("max_iters") else 5
                print(f"  mock/{prof}: {impl['label']}", flush=True)
                try:
                    rows.append(bench_impl(impl, [MOCK_VIDEO], base, 1 if impl.get("max_iters") else rounds, n, c))
                    s = rows[-1]["summary"]
                    print(f"    p50={s['p50_ms']:.2f}ms cold={s['cold_ms_median']:.1f}ms "
                          f"rss={s['rss_mib_max']:.1f}MiB ok={s['success_rate']:.0%} bytes={s['payload_bytes_median']}")
                except Exception as e:  # keep going; record the failure
                    print(f"    ERROR {e}")
                    rows.append({"key": impl["key"], "label": impl["label"], "error": str(e)})
            # Context size of this repo's output formats, same fixture.
            fmt = {}
            for f in ("text", "markdown", "json", "srt", "vtt"):
                p = subprocess.run([str(OUR_BIN), "--url", MOCK_VIDEO, "--format", f], env=base,
                                   capture_output=True, timeout=60)
                fmt[f] = len(p.stdout) if p.returncode == 0 else None
        out["profiles"].append({"profile": prof, "delay_ms": delay, "rows": rows, "format_bytes": fmt})
    return out


def suite_live(a) -> dict:
    require(OUR_BIN, DRIVE, ANAIS, NABID, SPINAL_NORL, PY)
    videos = a.videos.split(",") if a.videos else LIVE_VIDEOS
    reps = a.rounds
    ytdlp_bin = str(WORK / "venv" / "bin")
    env = {**os.environ, "PATH": f"{ytdlp_bin}:{os.environ.get('PATH', '')}"}
    env.pop("YTMCP_INNERTUBE_URL", None)
    out = {"suite": "live", "env": environment(),
           "params": {"videos": videos, "reps": reps, "pause_s": a.pause}, "rows": []}
    # Only the as-shipped spinalshock would add 1.5–3 s of deliberate sleep per
    # call; the no-rate-limit build isolates the yt-dlp cost instead.
    impls = [i for i in mcp_impls("live") if i["key"] != "spinalshock"]
    for impl in impls:
        print(f"  live: {impl['label']}", flush=True)
        try:
            r = bench_impl(impl, videos, env, rounds=reps, iters=len(videos), colds=1, pause_s=a.pause)
            out["rows"].append(r)
            s = r["summary"]
            print(f"    p50={s['p50_ms']:.0f}ms ok={s['success_rate']:.0%} rss={s['rss_mib_max']:.1f}MiB")
        except Exception as e:
            print(f"    ERROR {e}")
            out["rows"].append({"key": impl["key"], "label": impl["label"], "error": str(e)})
    # Library / CLI baselines (not MCP servers): one fresh process per video.
    baselines = {
        "yt-dlp-cli": lambda v, d: [str(WORK / "venv" / "bin" / "yt-dlp"), "--skip-download", "--write-subs",
                                    "--write-auto-subs", "--sub-langs", "en", "--sub-format", "vtt",
                                    "-o", f"{d}/%(id)s", "--no-warnings", "--quiet", v],
        "jdepoix-lib": lambda v, d: [str(PY), "-c",
                                     "import sys;from youtube_transcript_api import YouTubeTranscriptApi as A;"
                                     "t=A().fetch(sys.argv[1].split('v=')[1],languages=('en',));"
                                     "print(' '.join(s.text for s in t))", v],
    }
    tmp = WORK / "live-tmp"
    for key, mk in baselines.items():
        print(f"  live: {key}", flush=True)
        samples, ok, payload = [], [], []
        for _ in range(reps):
            for v in videos:
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                t0 = time.perf_counter()
                p = subprocess.run(mk(v, tmp), capture_output=True, env=env, timeout=300)
                ms = (time.perf_counter() - t0) * 1000
                nbytes = len(p.stdout) + sum(f.stat().st_size for f in tmp.glob("*"))
                samples.append(ms)
                ok.append(p.returncode == 0 and nbytes > 0)
                payload.append(nbytes)
                time.sleep(a.pause)
        good = [s for s, k in zip(samples, ok) if k]
        out["rows"].append({"key": key, "label": key, "kind": "baseline", "samples_ms": samples, "ok": ok,
                            "payload_bytes": payload,
                            "summary": {"p50_ms": pct(good, 50), "p95_ms": pct(good, 95),
                                        "success_rate": sum(ok) / len(ok),
                                        "payload_bytes_median": st.median([b for b, k in zip(payload, ok) if k])
                                        if any(ok) else None}})
        print(f"    p50={pct(good, 50):.0f}ms ok={sum(ok)}/{len(ok)}")
    shutil.rmtree(tmp, ignore_errors=True)
    # What a generic "web fetch" of the watch page hands the model instead.
    watch = []
    for v in videos:
        try:
            req = urllib.request.Request(v, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                watch.append(len(r.read()))
        except Exception:
            watch.append(None)
    out["watch_page_bytes"] = watch
    return out


# ── reporting ────────────────────────────────────────────────────────────────

def f(x, spec=".2f", unit=""):
    return "—" if x is None or x != x else f"{x:{spec}}{unit}"


def env_block(e: dict) -> list[str]:
    tp = ", ".join(f"{k} {v[:7] if len(v) == 40 else v}" for k, v in e.get("third_party", {}).items())
    return [
        f"- **When:** {e['timestamp']} · commit `{e['git_sha']}`{' (dirty)' if e['git_dirty'] else ''}",
        f"- **Machine:** {e['cpu']} · {e['cores']} cores · {e['os']}",
        f"- **Toolchains:** {e['rustc']} · Node {e['node']} · Python {e['python']} · {e['go']}",
        f"- **Third-party versions:** {tp or 'n/a'}",
        f"- **This repo's binary:** {e['binary_bytes'] / 2**20:.1f} MiB (release, stripped)" if e.get("binary_bytes") else "",
    ]


def report_mock(d: dict) -> str:
    L = ["# Mock benchmark — MCP servers over stdio", "",
         "Generated by `python3 bench/run.py mock`. Do not edit by hand.", "", *env_block(d["env"]), "",
         f"Each server is spawned by the same Node MCP client (`bench/mcp-client/drive.js`). "
         f"Cold = spawn → initialize → first `tools/call` result, median of {d['params']['cold_runs']} fresh processes. "
         f"Latency = {d['params']['rounds']} fresh processes × {d['params']['iters']} sequential calls "
         f"(spinalshock as shipped: 1 × 5). Peak RSS = max VmHWM across rounds. "
         f"Payload = bytes of text returned to the agent. Fixture: {d['params']['fixture']}.", ""]
    for p in d["profiles"]:
        L += [f"## {p['profile']} (`MOCK_DELAY_MS={p['delay_ms']}`)", "",
              "| Implementation | Fetch path | Cold (ms) | p50 (ms) | p95 (ms) | p99 (ms) | req/s | Peak RSS (MiB) | Payload (bytes) | OK |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in p["rows"]:
            if "error" in r:
                L.append(f"| {r['label']} | — | error: {r['error'][:60]} | | | | | | | |")
                continue
            s = r["summary"]
            L.append(f"| {r['label']} | {r['kind']} | {f(s['cold_ms_median'], '.1f')} | {f(s['p50_ms'])} | "
                     f"{f(s['p95_ms'])} | {f(s['p99_ms'])} | {f(s['throughput_rps'], '.1f')} | "
                     f"{f(s['rss_mib_max'], '.1f')} | {f(s['payload_bytes_median'], '.0f')} | {s['success_rate']:.0%} |")
        L.append("")
    fb = d["profiles"][0]["format_bytes"]
    L += ["## This repo's output formats (same fixture)", "",
          "| `format` | Bytes |", "|---|---:|"] + [f"| `{k}` | {f(v, '.0f')} |" for k, v in fb.items()] + [""]
    return "\n".join(L)


def report_micro(d: dict) -> str:
    rows, it = d["rows"], d["params"]["parse_iters"]
    names = {"rust": "Rust core (this repo)", "ts": "TS port (fast-xml-parser)", "python": "jdepoix (Python)"}
    L = ["# Micro benchmark — in-process clients", "", "Generated by `python3 bench/run.py micro`.", "",
         *env_block(d["env"]), "",
         "These are library-level clients (no MCP framing) hitting the local mock with no added delay. "
         "The TS port is an algorithm-equivalent reference written for this benchmark, not a published server.", "",
         "| Metric | " + " | ".join(names[k] for k in rows) + " |", "|---|" + "---:|" * len(rows)]
    xml_bytes = rows["rust"]["parse_xml"]["bytesIn"]
    L.append("| XML parse, µs/iter (80 KiB) | " + " | ".join(f(r["parse_xml"]["ms"] * 1000 / it, ".0f") for r in rows.values()) + " |")
    L.append("| XML parse, MiB/s | " + " | ".join(f(xml_bytes * it / (r["parse_xml"]["ms"] / 1000) / 2**20, ".0f") for r in rows.values()) + " |")
    L.append("| Innertube JSON parse, µs/iter | " + " | ".join(f(r["parse_json"]["ms"] * 1000 / it, ".2f") for r in rows.values()) + " |")
    L.append("| First call in a fresh process, median ms (timer starts after runtime start-up) | " + " | ".join(f(st.median(r["cold_ms"]), ".1f") for r in rows.values()) + " |")
    L.append("| e2e p50, ms (100 calls) | " + " | ".join(f(pct(r["e2e"]["samples"], 50)) for r in rows.values()) + " |")
    L.append("| e2e p95, ms | " + " | ".join(f(pct(r["e2e"]["samples"], 95)) for r in rows.values()) + " |")
    L.append("| Peak RSS after 100 calls, MiB | " + " | ".join(f(r["memory"]["rssKib"] / 1024, ".1f") for r in rows.values()) + " |")
    return "\n".join(L) + "\n"


def report_live(d: dict) -> str:
    L = ["# Live benchmark — real YouTube", "", "Generated by `python3 bench/run.py live`.", "",
         *env_block(d["env"]), "",
         f"Videos: {', '.join(d['params']['videos'])} · {d['params']['reps']} rep(s) · "
         f"{d['params']['pause_s']} s pause between calls (not timed).", "",
         "Live numbers depend on your network path to YouTube and on whether YouTube is bot-checking "
         "your IP (cloud IPs usually are). Latency percentiles only include successful calls; check the OK column.", "",
         "| Implementation | Kind | p50 (ms) | p95 (ms) | Peak RSS (MiB) | Payload p50 (bytes) | OK |",
         "|---|---|---:|---:|---:|---:|---:|"]
    for r in d["rows"]:
        if "error" in r:
            L.append(f"| {r['label']} | — | error: {r['error'][:60]} | | | | |")
            continue
        s = r["summary"]
        L.append(f"| {r['label']} | {r['kind']} | {f(s['p50_ms'], '.0f')} | {f(s['p95_ms'], '.0f')} | "
                 f"{f(s.get('rss_mib_max'), '.1f')} | {f(s['payload_bytes_median'], '.0f')} | {s['success_rate']:.0%} |")
    wb = [b for b in d.get("watch_page_bytes", []) if b]
    if wb:
        L += ["", f"Raw watch-page HTML (what a generic web-fetch tool downloads, before any extraction): "
                  f"median {st.median(wb) / 1024:.0f} KiB per video."]
    return "\n".join(L) + "\n"


def write(suite: str, data: dict):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{suite}.json").write_text(json.dumps(data, indent=1) + "\n")
    render(suite)


def render(suite: str):
    p = RESULTS / f"{suite}.json"
    if not p.exists():
        return
    d = json.loads(p.read_text())
    md = {"mock": report_mock, "micro": report_micro, "live": report_live}[suite](d)
    (RESULTS / f"{suite}.md").write_text(md)
    print(f"wrote {p.relative_to(REPO)} and {suite}.md")
    if suite in ("mock", "micro"):
        subprocess.run(["cargo", "run", "--release", "-q", "--manifest-path",
                        str(BENCH / "charts" / "Cargo.toml")], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suite", choices=["micro", "mock", "live", "all", "report"])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--videos", help="live: comma-separated URLs")
    ap.add_argument("--pause", type=float, default=2.0, help="live: seconds between calls")
    a = ap.parse_args()
    if a.port != 18080 and a.suite in ("mock", "all"):
        raise SystemExit("--port must stay 18080 for `mock`: the nabid-pf copy is patched to that port")
    if a.suite == "report":
        for s in ("micro", "mock", "live"):
            render(s)
        return
    for s in (["micro", "mock"] if a.suite == "all" else [a.suite]):
        print(f"== {s}", flush=True)
        write(s, {"micro": suite_micro, "mock": suite_mock, "live": suite_live}[s](a))


if __name__ == "__main__":
    main()
