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
# Best effort: with cap_drop root may not delete the user's files, and the bot clears these locks itself.
rm -f "$BROWSER"/*/SingletonLock "$BROWSER"/SingletonLock 2>/dev/null || true

# Tell Chrome/nodriver which virtual display to use
export DISPLAY="${DISPLAY:-:1}"

# ── Run as your own user (PUID / PGID) ──
# Files in a mounted data folder then belong to you on the host instead of to root (issue #58).

# cap_drop: ALL takes away what the switch needs, so it is tried on a scratch file before anything changes.
can_switch_to() {
	local probe ok=1
	probe=$(mktemp) || return 1
	chown "$1:$2" "$probe" 2>/dev/null && setpriv --reuid="$1" --regid="$2" --clear-groups true 2>/dev/null && ok=0
	rm -f "$probe"
	return "$ok"
}

run_as=()
run_uid="$(id -u)"
if [ -n "${PUID:-}" ]; then
	PGID="${PGID:-$PUID}"
	if [ "$run_uid" != "0" ]; then
		echo "PUID is set, but the container already runs as user $run_uid, so PUID is ignored."
	elif ! [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]]; then
		echo "PUID and PGID have to be numbers, so the container keeps running as root."
	elif ! can_switch_to "$PUID" "$PGID"; then
		echo "PUID needs the CHOWN, SETUID and SETGID capabilities, which this container was started without" \
			"(cap_drop). Add them back with cap_add, until then the container keeps running as root."
	else
		# read_only: true leaves /etc unwritable, and the numbers alone are enough to switch.
		if [ -w /etc/passwd ] && [ -w /etc/group ]; then
			getent group "$PGID" >/dev/null || groupadd -o -g "$PGID" fgc
			getent passwd "$PUID" >/dev/null || useradd -o -u "$PUID" -g "$PGID" -d /fgc/home -M -s /usr/sbin/nologin fgc
		fi
		# Same for /fgc, so the home folder goes to /tmp, the one place a read-only container can write.
		if mkdir -p /fgc/home 2>/dev/null; then
			export HOME=/fgc/home
		else
			export HOME=/tmp/fgc-home
		fi
		mkdir -p "$HOME" /fgc/data /tmp/.X11-unix
		chmod 1777 /tmp/.X11-unix
		chown "$PUID:$PGID" "$HOME"
		# A screen lock left by an earlier root start would keep this user's screen from starting.
		rm -f /tmp/.X*-lock /tmp/.tX*-lock /tmp/.X11-unix/X* 2>/dev/null || true
		# Only when something still belongs to someone else: browser profiles are thousands of files.
		for dir in /fgc/data "$BROWSER"; do
			if [ -e "$dir" ] && [ -n "$(find "$dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit 2>/dev/null)" ]; then
				echo "Handing $dir over to $PUID:$PGID, a one-time step that can take a moment."
				chown -R "$PUID:$PGID" "$dir"
			fi
		done
		# --init-groups looks the user up by name, which a user with no passwd entry does not have.
		if getent passwd "$PUID" >/dev/null; then groups=--init-groups; else groups=--clear-groups; fi
		run_as=(setpriv --reuid="$PUID" --regid="$PGID" "$groups")
		run_uid="$PUID"
	fi
fi

# A container a template starts as an unknown user gets "/" as its home, where VNC cannot write.
if ! "${run_as[@]}" test -w "${HOME:-/}"; then
	export HOME=/tmp/fgc-home
	"${run_as[@]}" mkdir -p "$HOME"
fi

# ── Say who we are ──
# On a NAS the app is often started as a user other than root, and that same user cannot
# write the browser profile or start the screen, which looks like two unrelated faults.
if "${run_as[@]}" test -w /fgc/data; then
	data_state="writable"
else
	data_state="NOT writable, the bot cannot save sessions or screenshots"
fi
# The lookup fails for a user id the image does not know, which is normal on a NAS, not a reason to stop.
user_name=$(getent passwd "$run_uid" 2>/dev/null | cut -d: -f1) || user_name=""
echo "Running as ${user_name:-unnamed}($run_uid), data folder is $data_state"

# ── Start the virtual screen and the web viewer ──
# Same script the bot calls if the screen dies later, so it comes back exactly as it started.
"${run_as[@]}" /fgc/start-vnc.sh
echo

# ── Hand off to the main application ──
# 'tini' is a lightweight init process that properly handles signals (like Ctrl+C)
# and reaps zombie processes. The "$@" passes through the CMD from the Dockerfile
# (which is "python3 main.py" by default).
exec "${run_as[@]}" tini -g -- "$@"
