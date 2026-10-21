#!/bin/bash
# Installs (or updates) P3, the Parakeet Podcast Processor, on an Apple
# Silicon Mac, then opens its setup wizard in the browser.
#
#   curl -fsSL https://raw.githubusercontent.com/MtlPhil/parakeet-podcast-processor/main/install.sh | bash
#
# Needs no Homebrew, Xcode or git: Python, ffmpeg and deno are downloaded
# into the install folder, and Node only temporarily to build the web app.
# Running it again updates P3 and keeps your library, accounts and settings.
#
# Options (environment variables):
#   P3_HOME     install folder                 (default ~/Parakeet)
#   P3_PORT     preferred port                 (default 8000; the next free
#                                               one is used if it's taken)
#   P3_REF      GitHub branch or tag           (default main)
#   P3_REPO     GitHub owner/repo              (default MtlPhil/parakeet-podcast-processor)
#   P3_SOURCE   a local folder to install from instead of GitHub
#   P3_LABEL    launchd service name           (default com.parakeet.p3)
#   P3_NO_OPEN  set to 1 to not open the browser at the end
set -euo pipefail

P3_HOME="${P3_HOME:-$HOME/Parakeet}"
P3_PORT="${P3_PORT:-8000}"
P3_REF="${P3_REF:-main}"
P3_REPO="${P3_REPO:-MtlPhil/parakeet-podcast-processor}"
P3_SOURCE="${P3_SOURCE:-}"
P3_LABEL="${P3_LABEL:-com.parakeet.p3}"
P3_NO_OPEN="${P3_NO_OPEN:-}"
PYTHON_VERSION="3.13"
MIN_FREE_GB=10

PLIST="$HOME/Library/LaunchAgents/$P3_LABEL.plist"
TOOLS="$P3_HOME/tools"
LOG="$P3_HOME/logs/install.log"
WORK="$(mktemp -d -t p3-install)"
CURRENT_STEP="starting"

bold=$'\033[1m'; green=$'\033[32m'; red=$'\033[31m'; dim=$'\033[2m'; reset=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { CURRENT_STEP="$1"; printf '%s→%s %s\n' "$bold" "$reset" "$1"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$1"; }
die()  { printf '\n%s%s%s\n' "$red" "$1" "$reset" >&2; [[ -n "${2:-}" ]] && printf '%s\n' "$2" >&2; exit 1; }

# Ask a yes/no question on the terminal (works even when piped from curl).
confirm() {
  local answer=""
  if [[ -r /dev/tty ]]; then
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r answer < /dev/tty || answer=""
  fi
  [[ "$answer" == [yY]* ]]
}

on_error() {
  local code=$?
  printf '\n%sSomething went wrong while: %s%s\n' "$red" "$CURRENT_STEP" "$reset" >&2
  if [[ -f "$LOG" ]]; then
    printf '%sLast lines of the log (%s):%s\n' "$dim" "$LOG" "$reset" >&2
    tail -n 15 "$LOG" >&2 || true
  fi
  printf '\nYou can run the same command again; it picks up where it left off.\n' >&2
  exit "$code"
}
trap on_error ERR
trap 'rm -rf "$WORK"' EXIT

# Runs a command with its output in the log, not on screen.
quietly() { "$@" >> "$LOG" 2>&1; }

download() { curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1"; }

port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# ------------------------------------------------------------------ checks

say ""
say "${bold}P3 · Podcast Processor installer${reset}"
say ""

[[ "$(uname -s)" == "Darwin" ]] || die "P3 runs only on a Mac."
[[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]] || die \
  "P3 needs a Mac with Apple Silicon (M1 or newer)." \
  "Its speech-to-text engines use Apple's MLX framework, which Intel Macs don't have."

mkdir -p "$P3_HOME/logs"
printf '\n===== %s install/update =====\n' "$(date)" >> "$LOG"

UPDATING=""
[[ -x "$P3_HOME/.venv/bin/python" ]] && UPDATING=1

free_gb=$(( $(df -k "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if (( free_gb < MIN_FREE_GB )); then
  say "Only ${free_gb} GB of disk space is free. P3 and its speech models need about ${MIN_FREE_GB} GB."
  confirm "Continue anyway?" || die "Stopped. Free up some space and run the command again."
fi

if [[ -n "$UPDATING" && -f "$P3_HOME/config/server.env" ]]; then
  current_port=$(grep -E '^P3_PORT=' "$P3_HOME/config/server.env" | cut -d= -f2 || true)
  if [[ -n "$current_port" ]] && curl -fsS -m 5 "http://127.0.0.1:$current_port/api/auth/status" 2>/dev/null | grep -q '"busy":true'; then
    say "P3 is processing episodes right now. Updating restarts it and interrupts that work."
    confirm "Update anyway?" || die "Stopped. Run the command again when P3 is idle."
  fi
fi

say "Installing into ${bold}$P3_HOME${reset}${UPDATING:+ (updating the existing install)}."
say "${dim}Details go to $LOG${reset}"
say ""

# ------------------------------------------------------------------- code

step "Getting P3"
SRC="$WORK/src"
mkdir -p "$SRC"
if [[ -n "$P3_SOURCE" ]]; then
  quietly rsync -a --exclude .git --exclude node_modules --exclude .venv \
    --exclude /data --exclude /config/feeds.yaml --exclude '/config/*.txt' \
    --exclude /config/server.env --exclude /logs --exclude /tools \
    --exclude /frontend/dist --exclude __pycache__ "$P3_SOURCE/" "$SRC/"
else
  download "https://codeload.github.com/$P3_REPO/tar.gz/$P3_REF" "$WORK/src.tar.gz"
  quietly tar -xzf "$WORK/src.tar.gz" -C "$SRC" --strip-components 1
fi
[[ -f "$SRC/pyproject.toml" && -d "$SRC/p3" ]] || die "The download doesn't look like P3."
# Replace the code; keep the library, settings, tools and Python.
quietly rsync -a --delete \
  --exclude /data/ --exclude /config/ --exclude /exports/ --exclude /blog_posts/ \
  --exclude /linkedin_posts/ --exclude /logs/ --exclude /tools/ --exclude /.venv/ \
  "$SRC/" "$P3_HOME/"
mkdir -p "$P3_HOME/config"
cp "$SRC/config/feeds.yaml.example" "$P3_HOME/config/" 2>/dev/null || true
chmod +x "$P3_HOME/bin/"* 2>/dev/null || true
if [[ -n "$P3_SOURCE" ]]; then ok "P3 from $P3_SOURCE"; else ok "P3 from github.com/$P3_REPO ($P3_REF)"; fi

# ------------------------------------------------------------------ tools

mkdir -p "$TOOLS/bin"
export UV_CACHE_DIR="$TOOLS/uv-cache"
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_NO_PROGRESS=1

step "Getting the Python installer (uv)"
download "https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-apple-darwin.tar.gz" "$WORK/uv.tar.gz"
quietly tar -xzf "$WORK/uv.tar.gz" -C "$WORK"
cp "$WORK"/uv-aarch64-apple-darwin/uv "$WORK"/uv-aarch64-apple-darwin/uvx "$TOOLS/bin/"
UV="$TOOLS/bin/uv"
ok "$("$UV" --version)"

step "Setting up Python $PYTHON_VERSION"
if [[ ! -x "$P3_HOME/.venv/bin/python" ]]; then
  quietly "$UV" venv --python "$PYTHON_VERSION" "$P3_HOME/.venv"
fi
ok "$("$P3_HOME/.venv/bin/python" --version)"

step "Installing P3 and its speech-to-text engines (the longest step)"
( cd "$P3_HOME" && quietly "$UV" pip install --python .venv/bin/python -e ".[web]" imageio-ffmpeg ) || die \
  "Installing P3's Python packages failed." \
  "If your macOS is old, update it in System Settings › General › Software Update and try again."
ok "Python packages installed"

step "Installing ffmpeg (audio conversion)"
FFMPEG="$("$P3_HOME/.venv/bin/python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
ln -sf "$FFMPEG" "$TOOLS/bin/ffmpeg"
ok "$("$TOOLS/bin/ffmpeg" -hide_banner -version | head -1 | cut -d' ' -f1-3)"

step "Installing deno (needed for YouTube)"
download "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip" "$WORK/deno.zip"
quietly unzip -o -q "$WORK/deno.zip" -d "$TOOLS/bin"
chmod +x "$TOOLS/bin/deno"
ok "$("$TOOLS/bin/deno" --version | head -1)"

# ---------------------------------------------------------------- web app

step "Building the web interface"
NODE_VERSION="$(curl -fsSL https://nodejs.org/dist/index.json | "$P3_HOME/.venv/bin/python" -c '
import json, sys
releases = [r for r in json.load(sys.stdin) if r["lts"] and "osx-arm64-tar" in r["files"]]
print(releases[0]["version"])')"
download "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-darwin-arm64.tar.gz" "$WORK/node.tar.gz"
quietly tar -xzf "$WORK/node.tar.gz" -C "$WORK"
NODE_BIN="$WORK/node-$NODE_VERSION-darwin-arm64/bin"
(
  cd "$P3_HOME/frontend"
  export PATH="$NODE_BIN:$PATH"
  unset NODE_ENV  # a production NODE_ENV would skip the build tools
  quietly npm ci --include=dev --no-audit --no-fund
  quietly npm run build
  rm -rf node_modules
)
[[ -f "$P3_HOME/frontend/dist/index.html" ]] || die "Building the web interface failed."
ok "Web interface built (Node $NODE_VERSION, removed afterwards)"

# ----------------------------------------------------------------- config

step "Preparing settings"
mkdir -p "$P3_HOME/data" "$P3_HOME/exports" "$P3_HOME/blog_posts" "$P3_HOME/linkedin_posts"
if [[ ! -f "$P3_HOME/config/feeds.yaml" ]]; then
  cat > "$P3_HOME/config/feeds.yaml" <<'YAML'
# Podcasts are added in the web app. Settings are changed on its Settings page.
feeds: []
settings:
  max_episodes_per_feed: 3
  audio_format: wav
  parakeet_enabled: true
  parakeet_model: mlx-community/parakeet-tdt-0.6b-v3
  whisper_model: turbo
  llm_provider: gemini
  export_format: [markdown, json]
YAML
  ok "Default settings written"
else
  ok "Keeping your settings"
fi
if [[ ! -f "$P3_HOME/config/server.env" ]]; then
  port="$P3_PORT"
  while port_in_use "$port"; do port=$((port + 1)); done
  printf '# Read by bin/p3-serve. Changed from P3'"'"'s Settings or setup wizard.\nP3_HOST=127.0.0.1\nP3_PORT=%s\n' "$port" \
    > "$P3_HOME/config/server.env"
  [[ "$port" != "$P3_PORT" ]] && ok "Port $P3_PORT is taken; using $port"
fi
PORT="$(grep -E '^P3_PORT=' "$P3_HOME/config/server.env" | cut -d= -f2)"
printf '# Written by install.sh\nP3_LABEL=%s\n' "$P3_LABEL" > "$P3_HOME/config/install.env"
URL="http://localhost:$PORT"

# ---------------------------------------------------------------- service

step "Starting P3 (it will also start by itself when you log in)"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$P3_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$P3_HOME/bin/p3-serve</string>
  </array>
  <key>WorkingDirectory</key><string>$P3_HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$P3_HOME/logs/p3.log</string>
  <key>StandardErrorPath</key><string>$P3_HOME/logs/p3.log</string>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/$P3_LABEL" >> "$LOG" 2>&1 || true
sleep 1
quietly launchctl bootstrap "gui/$(id -u)" "$PLIST"

started=""
for _ in $(seq 1 90); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/auth/status" >/dev/null 2>&1; then
    started=1; break
  fi
  sleep 1
done
[[ -n "$started" ]] || die "P3 didn't start." "See $P3_HOME/logs/p3.log for the reason."
ok "P3 is running at $URL"

cat > "$P3_HOME/Open P3.webloc" <<WEBLOC
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>$URL</string></dict></plist>
WEBLOC

say ""
say "${green}${bold}Done!${reset} P3 is at ${bold}$URL${reset} — bookmark it."
say "To update P3 later, run the same command again. To remove it: $P3_HOME/bin/p3-uninstall"
if [[ -z "$P3_NO_OPEN" ]]; then
  say "Opening the setup in your browser..."
  open "$URL" || true
fi
