#!/usr/bin/env bash
# Docker entrypoint script – runs before the Python bot starts.
# It says what the container looks like, brings up the virtual screen (TurboVNC) and
# the web-based VNC viewer (noVNC), then hands over to the bot.

# Exit immediately if any command fails
set -eo pipefail


# Browser profile directory (can be customized via BROWSER_DIR env var)
BROWSER="${BROWSER_DIR:-/fgc/data/browser}"
# A relative setting is meant to be relative to the app folder, an absolute one is already final.
case "$BROWSER" in /*) ;; *) BROWSER="/fgc/$BROWSER" ;; esac

# Remove Chrome's profile lock files to prevent "profile in use" errors
# after the container was stopped ungracefully (e.g. power loss, docker kill)
rm -f "$BROWSER"/*/SingletonLock "$BROWSER"/SingletonLock

# Tell Chrome/nodriver which virtual display to use
export DISPLAY="${DISPLAY:-:1}"

# ── Say who we are ──
# On a NAS the app is often started as a user other than root, and that same user cannot
# write the browser profile or start the screen, which looks like two unrelated faults.
if [ -w /fgc/data ]; then
	data_state="writable"
else
	data_state="NOT writable, the bot cannot save sessions or screenshots"
fi
# The lookup fails for a user id the image does not know, which is normal on a NAS, not a reason to stop.
user_name=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f1) || user_name=""
echo "Running as ${user_name:-unnamed}($(id -u)), data folder is $data_state"

# ── Start the virtual screen and the web viewer ──
# Same script the bot calls if the screen dies later, so it comes back exactly as it started.
/fgc/start-vnc.sh
echo

# ── Hand off to the main application ──
# 'tini' is a lightweight init process that properly handles signals (like Ctrl+C)
# and reaps zombie processes. The "$@" passes through the CMD from the Dockerfile
# (which is "python3 main.py" by default).
exec tini -g -- "$@"
