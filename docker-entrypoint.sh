#!/usr/bin/env bash
# Docker entrypoint script – runs before the Python bot starts.
# It sets up the virtual display (TurboVNC) and web-based VNC viewer (noVNC)
# so you can watch the browser remotely through your web browser.

# Exit immediately if any command fails
set -eo pipefail


# Browser profile directory (can be customized via BROWSER_DIR env var)
BROWSER="${BROWSER_DIR:-data/browser}"

# Remove Chrome's profile lock file to prevent "profile in use" errors
# after the container was stopped ungracefully (e.g. power loss, docker kill)
rm -f "/fgc/$BROWSER/SingletonLock"

# Remove stale X11 display lock files that remain when reusing containers
# (common with 'docker compose up' after a restart)
rm -f /tmp/.X1-lock /tmp/.tX1-lock /tmp/.X11-unix/X1

# Tell Chrome/nodriver which virtual display to use
export DISPLAY=:1

# ── VNC password setup ──
# If VNC_PASSWORD is set, require a password to connect. Otherwise, no password.
if [ -z "$VNC_PASSWORD" ]; then
	pw="-SecurityTypes None"
	pwt="no password!"
else
	pw="-rfbauth ~/.vnc/passwd"
	mkdir -p ~/.vnc/
	echo "$VNC_PASSWORD" | /opt/TurboVNC/bin/vncpasswd -f >~/.vnc/passwd
	pwt="with password"
fi

# ── Start the virtual display server (TurboVNC) ──
# This creates a virtual monitor at the specified resolution so Chrome
# can render pages even though there is no physical screen attached.
# shellcheck disable=SC2086
/opt/TurboVNC/bin/vncserver $DISPLAY \
    -geometry "${WIDTH}x${HEIGHT}" \
    -depth "${DEPTH}" \
    -rfbport "${VNC_PORT}" \
    $pw -vgl \
    -log /fgc/data/TurboVNC.log \
    -xstartup /usr/bin/ratpoison > /dev/null 2>&1

echo "TurboVNC is running on port $VNC_PORT ($pwt) with resolution ${WIDTH}x${HEIGHT}"

# ── Start noVNC (web-based VNC client) ──
# This acts as a bridge: users can connect via a web browser at http://localhost:7080
# instead of needing a dedicated VNC client application.
websockify -D --web "/usr/share/novnc/" "$NOVNC_PORT" "localhost:$VNC_PORT" 2>/dev/null 1>&2 &
echo "noVNC (VNC via browser) is running on http://localhost:$NOVNC_PORT/?autoconnect=true"
echo

# ── Hand off to the main application ──
# 'tini' is a lightweight init process that properly handles signals (like Ctrl+C)
# and reaps zombie processes. The "$@" passes through the CMD from the Dockerfile
# (which is "python3 main.py" by default).
exec tini -g -- "$@"
