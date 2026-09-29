# bench/

Harness behind [BENCHMARKS.md](../BENCHMARKS.md).

```bash
bench/setup.sh               # one-time; idempotent
python3 bench/run.py all     # micro + mock
python3 bench/run.py live    # real YouTube (needs an IP YouTube doesn't bot-check)
python3 bench/run.py report  # re-render tables and charts from existing JSON
```

Options: `--rounds N` (default 3), `--iters N` (default 50), `--quick` (smoke test only),
`--videos url,url` and `--pause S` for `live`.

## Layout

| Path | Purpose |
|---|---|
| `setup.sh` | Builds this repo; installs pinned competitors into `.work/` (git-ignored) |
| `run.py` | Orchestrator: `micro`, `mock`, `live`, `all`, `report` |
| `charts/` | Rust crate (charton) that renders `results/chart-*.svg` from the JSON |
| `mcp-client/drive.js` | The one MCP-stdio client used for every server |
| `mock/server.py` | Mock Innertube + caption endpoints; `MOCK_DELAY_MS` adds latency |
| `fake-yt-dlp/yt-dlp` | yt-dlp stub for mock runs |
| `canned/` | Fixtures: Innertube JSON, 80 KiB caption XML, matching VTT |
| `rust-client/`, `ts-client/`, `python-jdepoix/` | In-process clients for `micro` |
| `results/` | Committed results: `<suite>.json` (raw samples + environment), `<suite>.md`, charts |

## Pinned competitors

Pinned in `setup.sh` and recorded in every results file:

| Implementation | Version | Mock-mode adjustment |
|---|---|---|
| `@anaisbetts/mcp-youtube` | 0.8.0 | yt-dlp stub on `PATH` |
| `youtube-video-summarizer-mcp` (nabid-pf) | 1.8.9 | Innertube host rewritten to the mock in a separate copy |
| spinalshock/youtube-transcript-mcp | `20ebd3f` | yt-dlp stub; also built with its rate-limit sleep set to 0 |
| `youtube-transcript-api` (jdepoix) | 1.2.4 | URL constants patched at import |
| `yt-dlp` | 2026.8.19 | used by `live` only |
