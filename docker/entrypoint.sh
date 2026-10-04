#!/bin/sh
# Runs the app as PUID:PGID so files aren't root-owned on the host.
# As root: create/reuse the user, chown /data only (never media), drop privs, exec.
# As non-root (`docker run --user`, tests): UMASK still applies, command runs as-is.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"

# Fail fast on compose typos instead of halfway through useradd/chown.
case "$PUID" in ''|*[!0-9]*) echo "entrypoint: PUID must be numeric, got '$PUID'" >&2; exit 1;; esac
case "$PGID" in ''|*[!0-9]*) echo "entrypoint: PGID must be numeric, got '$PGID'" >&2; exit 1;; esac
case "$UMASK" in [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
  *) echo "entrypoint: UMASK must be an octal mode like 022, got '$UMASK'" >&2; exit 1;;
esac
umask "$UMASK"

if [ "$(id -u)" -ne 0 ]; then
  exec "$@"
fi

if [ "$PUID" -eq 0 ]; then
  echo "entrypoint: PUID=0 -- running as root on purpose, files will be root-owned" >&2
  exec "$@"
fi

# Reuse whatever name already owns the id; only create when the id is free.
if ! getent group "$PGID" >/dev/null 2>&1; then
  if getent group verifyarr >/dev/null 2>&1; then
    groupmod -g "$PGID" verifyarr
  else
    groupadd -g "$PGID" verifyarr
  fi
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
  if getent passwd verifyarr >/dev/null 2>&1; then
    usermod -u "$PUID" -g "$PGID" verifyarr
  else
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin verifyarr
  fi
fi
USERNAME="$(getent passwd "$PUID" | cut -d: -f1)"

mkdir -p /data
# A chown that is refused (NFS root_squash, rootless podman, ACL-only shares) must not stop the
# container: the app only needs /data to be writable by PUID.
chown -R "$PUID:$PGID" /data 2>/dev/null || echo "entrypoint: could not chown /data to $PUID:$PGID -- continuing" >&2

# No --clear-groups: a `group_add: render` GPU setup needs its supplementary group.
if command -v setpriv >/dev/null 2>&1; then
  exec setpriv --reuid="$PUID" --regid="$PGID" --keep-groups -- "$@"
elif command -v runuser >/dev/null 2>&1; then
  exec runuser -u "$USERNAME" -g "$PGID" -- "$@"
else
  echo "entrypoint: neither setpriv nor runuser found -- running as root" >&2
  exec "$@"
fi
