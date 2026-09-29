#!/usr/bin/env bash
# Install every benchmark participant at a pinned version into bench/.work/.
#
# Idempotent: re-running skips anything already installed. Delete bench/.work/
# to start clean. Nothing is installed globally; nothing outside bench/.work/
# (and cargo's target dirs) is written.
#
# Requirements: cargo, node >= 18 + npm, python3 >= 3.9 (with venv), go >= 1.21, git.
set -euo pipefail

# ── Pinned versions ─────────────────────────────────────────────────────────
# Bump deliberately; every results file records the versions it ran against.
ANAISBETTS_VERSION="0.8.0"                                   # npm @anaisbetts/mcp-youtube
NABID_VERSION="1.8.9"                                        # npm youtube-video-summarizer-mcp
SPINALSHOCK_REPO="https://github.com/spinalshock/youtube-transcript-mcp"
SPINALSHOCK_COMMIT="20ebd3f571ef105302181a6fc9cafd3361181a9b"
JDEPOIX_VERSION="1.2.4"                                      # PyPI youtube-transcript-api
YTDLP_VERSION="2026.8.19"                                    # PyPI yt-dlp (live mode only)

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$BENCH")"
WORK="$BENCH/.work"
mkdir -p "$WORK"

step() { printf '\n==> %s\n' "$*"; }

step "this repo: release binary (crates/native)"
( cd "$REPO" && cargo build --release -p youtube-transcript-mcp )

step "this repo: microbench client (bench/rust-client)"
( cd "$BENCH/rust-client" && cargo build --release )

step "TS port (bench/ts-client)"
( cd "$BENCH/ts-client" && npm ci --no-audit --no-fund && npx tsc -p . )

step "Python venv: youtube-transcript-api==$JDEPOIX_VERSION, yt-dlp==$YTDLP_VERSION"
if [ ! -x "$WORK/venv/bin/python" ]; then python3 -m venv "$WORK/venv"; fi
"$WORK/venv/bin/pip" install -q --disable-pip-version-check \
  "youtube-transcript-api==$JDEPOIX_VERSION" "yt-dlp==$YTDLP_VERSION"

install_npm_pkg() { # <dir> <package@version>
  local dir="$1" spec="$2"
  if [ -f "$dir/package/.installed" ]; then return; fi
  rm -rf "$dir" && mkdir -p "$dir"
  ( cd "$dir" && npm pack --silent "$spec" >/dev/null && tar -xzf ./*.tgz )
  ( cd "$dir/package" && npm install --omit=dev --no-audit --no-fund --ignore-scripts )
  touch "$dir/package/.installed"
}

step "anaisbetts/mcp-youtube@$ANAISBETTS_VERSION"
install_npm_pkg "$WORK/anaisbetts" "@anaisbetts/mcp-youtube@$ANAISBETTS_VERSION"

step "youtube-video-summarizer-mcp@$NABID_VERSION (nabid-pf)"
install_npm_pkg "$WORK/nabid" "youtube-video-summarizer-mcp@$NABID_VERSION"
# Mock-mode copy: youtube-caption-extractor hardcodes the Innertube host, so
# point a *separate* copy at the local mock. The unpatched copy is used live.
if [ ! -d "$WORK/nabid-mock" ]; then
  cp -r "$WORK/nabid" "$WORK/nabid-mock"
  sed -i "s|https://www.youtube.com/youtubei/v1|http://127.0.0.1:18080/youtubei/v1|g" \
    "$WORK/nabid-mock/package/node_modules/youtube-caption-extractor/dist/index.js"
  grep -q "127.0.0.1:18080" \
    "$WORK/nabid-mock/package/node_modules/youtube-caption-extractor/dist/index.js" \
    || { echo "nabid patch did not apply" >&2; exit 1; }
fi

step "spinalshock/youtube-transcript-mcp@${SPINALSHOCK_COMMIT:0:7}"
if [ ! -d "$WORK/spinalshock/.git" ]; then
  git clone -q "$SPINALSHOCK_REPO" "$WORK/spinalshock"
fi
( cd "$WORK/spinalshock" && git checkout -q "$SPINALSHOCK_COMMIT" \
    && go build -o spinalshock-mcp . )
# Variant with the built-in 1.5–3 s rate-limit sleep removed, to isolate the
# cost of the yt-dlp architecture from the deliberate throttle.
if [ ! -x "$WORK/spinalshock/spinalshock-mcp-norl" ]; then
  ( cd "$WORK/spinalshock" \
    && sed -i -E 's/(MinRateLimitMs *= *)[0-9]+/\10/; s/(MaxRateLimitMs *= *)[0-9]+/\10/' internal/config/config.go \
    && go build -o spinalshock-mcp-norl . \
    && git checkout -q -- internal/config/config.go )
fi

step "record versions"
{
  echo "{"
  echo "  \"anaisbetts\": \"$ANAISBETTS_VERSION\","
  echo "  \"nabid\": \"$NABID_VERSION\","
  echo "  \"spinalshock\": \"$SPINALSHOCK_COMMIT\","
  echo "  \"youtube-transcript-api\": \"$JDEPOIX_VERSION\","
  echo "  \"yt-dlp\": \"$YTDLP_VERSION\""
  echo "}"
} > "$WORK/versions.json"

echo
echo "Setup complete. Next: python3 bench/run.py mock   (or: all, micro, live)"
