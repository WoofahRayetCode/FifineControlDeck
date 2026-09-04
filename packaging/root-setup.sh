#!/usr/bin/env bash
# Runs as root (via pkexec): installs the udev rule AND fixes permissions on
# the already-connected device so no replug is needed.
set -e
RULE_SRC="$(dirname "$0")/70-fifine-deck.rules"
install -m 0644 "$RULE_SRC" /etc/udev/rules.d/70-fifine-deck.rules

# Earlier versions installed this rule as 99-fifine-deck.rules, where its
# TAG+="uaccess" never fired (73-seat-late.rules dispatches uaccess at 73).
rm -f /etc/udev/rules.d/99-fifine-deck.rules
udevadm control --reload-rules || true
udevadm trigger || true

# Who invoked us (pkexec / sudo) — for an immediate ACL if uaccess is slow.
INVOKER="${SUDO_USER:-}"
if [ -z "$INVOKER" ] && [ -n "${PKEXEC_UID:-}" ]; then
    INVOKER="$(id -nu "$PKEXEC_UID" 2>/dev/null || true)"
fi

fix_node() {
    local node="$1"
    [ -e "$node" ] || return 0
    chmod 0660 "$node" 2>/dev/null || true
    # Prefer plugdev when it exists; otherwise input (common on Arch) so the
    # MODE=0660 grant is usable even before the seat ACL lands.
    if getent group plugdev >/dev/null 2>&1; then
        chgrp plugdev "$node" 2>/dev/null || true
    elif getent group input >/dev/null 2>&1; then
        chgrp input "$node" 2>/dev/null || true
    fi
    if [ -n "$INVOKER" ] && command -v setfacl >/dev/null 2>&1; then
        setfacl -m "u:${INVOKER}:rw" "$node" 2>/dev/null || true
    fi
}

# Immediate effect for the currently-plugged device (no replug required).
for h in /sys/class/hidraw/hidraw*; do
    if grep -qi "3142" "$h/device/uevent" 2>/dev/null; then
        fix_node "/dev/$(basename "$h")"
    fi
done
for p in /sys/bus/usb/devices/*; do
    if [ -f "$p/idVendor" ] && [ "$(cat "$p/idVendor" 2>/dev/null)" = "3142" ]; then
        b=$(cat "$p/busnum" 2>/dev/null); d=$(cat "$p/devnum" 2>/dev/null)
        if [ -n "$b" ] && [ -n "$d" ]; then
            fix_node "$(printf "/dev/bus/usb/%03d/%03d" "$b" "$d")"
        fi
    fi
done
echo "OK: udev rule installed and current device permissions fixed."
