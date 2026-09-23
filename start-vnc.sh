#!/usr/bin/env bash
# Starts the virtual screen (TurboVNC) and the web viewer (noVNC).
# The entrypoint runs this at boot, and the bot runs it again when the screen dies mid-run.

set -uo pipefail

# Work out which screen we are responsible for, ":1" unless something says otherwise.
DISPLAY_NUM="${DISPLAY:-:1}"
DISPLAY_NUM="${DISPLAY_NUM##*:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"
case "$DISPLAY_NUM" in ''|*[!0-9]*) DISPLAY_NUM=1 ;; esac
export DISPLAY=":${DISPLAY_NUM}"

VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-7080}"
VNC_LOG="/fgc/data/TurboVNC.log"

port_answers() {
	(echo >"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# ── Clear whatever the previous screen left behind ──
# Without this a crashed Xvnc keeps its lock and the new one refuses to take the display.
/opt/TurboVNC/bin/vncserver -kill "$DISPLAY" >/dev/null 2>&1
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.tX${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

# ── VNC password setup ──
# If VNC_PASSWORD is set, require a password to connect. Otherwise, no password.
if [ -z "${VNC_PASSWORD:-}" ]; then
	pw="-SecurityTypes None"
	pwt="no password!"
else
	pw="-rfbauth $HOME/.vnc/passwd"
	pwt="with password"
	mkdir -p "$HOME/.vnc/"
	if ! echo "$VNC_PASSWORD" | /opt/TurboVNC/bin/vncpasswd -f >"$HOME/.vnc/passwd"; then
		echo "Could not write the VNC password file in $HOME/.vnc, check who this container runs as."
		exit 1
	fi
fi

# ── Start the virtual screen (TurboVNC) ──
# This is a monitor that only exists in memory, so Chrome can draw pages without one attached.
# shellcheck disable=SC2086
if ! started=$(/opt/TurboVNC/bin/vncserver "$DISPLAY" \
    -geometry "${WIDTH:-1280}x${HEIGHT:-720}" \
    -depth "${DEPTH:-24}" \
    -rfbport "$VNC_PORT" \
    $pw -vgl \
    -log "$VNC_LOG" \
    -xstartup /usr/bin/ratpoison 2>&1); then
	echo "TurboVNC would not start on $DISPLAY:"
	if [ -n "$started" ]; then
		echo "$started" | tail -n 5
	elif [ -f "$VNC_LOG" ]; then
		# Only when TurboVNC said nothing itself, because this log keeps earlier starts too.
		tail -n 5 "$VNC_LOG"
	fi
	echo "The full screen log is in your data folder, as TurboVNC.log."
	exit 1
fi

# vncserver returns before the screen is ready to be drawn on, so wait for its socket.
for _ in $(seq 1 20); do
	[ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
	sleep 0.5
done
if [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
	echo "TurboVNC reported success but screen $DISPLAY never appeared, see TurboVNC.log in your data folder."
	exit 1
fi

echo "TurboVNC is running on port $VNC_PORT ($pwt) with resolution ${WIDTH:-1280}x${HEIGHT:-720}"

# ── Start noVNC (VNC in a web browser) ──
# A dead web viewer does not stop the bot from claiming, so this only warns.
if port_answers "$NOVNC_PORT"; then
	echo "noVNC (VNC via browser) is already running on http://localhost:$NOVNC_PORT/?autoconnect=true"
	exit 0
fi

websockify -D --web "/usr/share/novnc/" "$NOVNC_PORT" "localhost:$VNC_PORT" >/tmp/websockify.log 2>&1
for _ in $(seq 1 10); do
	port_answers "$NOVNC_PORT" && break
	sleep 0.5
done
if port_answers "$NOVNC_PORT"; then
	echo "noVNC (VNC via browser) is running on http://localhost:$NOVNC_PORT/?autoconnect=true"
else
	echo "noVNC did not come up on port $NOVNC_PORT, so watching the browser will not work:"
	tail -n 5 /tmp/websockify.log 2>/dev/null
fi
