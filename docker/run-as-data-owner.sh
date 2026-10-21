#!/usr/bin/env sh
# exec wrapper: run "$@" without root privileges, as the user that can own /app/data.
# Used by both compose services (dashboard via entrypoint.sh, api via its command).
#
# Started as root (the image default), pick the uid/gid:
#  1. /app/data owned by a regular user (Linux bind mount of the repo's ./data, any mode
#     incl. 0700)                                   -> run as that owner;
#  2. root-owned but writable by `app` (10001)      -> run as app (macOS/colima virtiofs);
#  3. root-owned, not writable, inside a user namespace (rootless Docker/Podman: container
#     root *is* the host user)                      -> stay container root;
#  4. root-owned, not writable, empty (fresh volume) -> chown the dir itself (never
#     recursively) to app and run as app;
#  5. anything else                                 -> refuse with a fix-it message.
# A bind mount is never chown'ed: that would take repo files away from their owner.
# Not root already (e.g. `docker run --user`)       -> just exec.
set -e

APP_UID=10001
APP_GID=10001
data_root="${DATA_ROOT:-/app/data}"

if [ "$(id -u)" != 0 ]; then
  exec "$@"
fi

drop() {  # $1 uid, $2 gid, then the command
  uid="$1"; gid="$2"; shift 2
  # arbitrary host uids have no passwd entry / home -> give them a writable HOME
  if [ "$uid" = "$APP_UID" ]; then HOME=/home/app; else HOME=/tmp; fi
  export HOME
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups \
       --no-new-privs --inh-caps=-all --bounding-set=-all -- "$@"
}

writable_by() {  # $1 uid, $2 gid
  setpriv --reuid="$1" --regid="$2" --clear-groups -- test -w "$data_root" 2>/dev/null
}

in_user_namespace() {  # the initial namespace maps the full range: "0 0 4294967295"
  [ -r /proc/self/uid_map ] || return 1
  # shellcheck disable=SC2046
  set -- $(cat /proc/self/uid_map)
  [ "$#" -ne 3 ] || [ "$1" != 0 ] || [ "$2" != 0 ] || [ "$3" != 4294967295 ]
}

owner_uid="$(stat -c %u "$data_root")"
owner_gid="$(stat -c %g "$data_root")"

if [ "$owner_uid" != 0 ]; then
  # leftovers from an older image that ran as root: the dir is the user's, its contents not
  for f in "$data_root"/output "$data_root"/analytics.duckdb; do
    if [ -e "$f" ] && [ "$(stat -c %u "$f")" = 0 ] && ! (
        setpriv --reuid="$owner_uid" --regid="$owner_gid" --clear-groups -- test -w "$f"); then
      echo "ERROR: $f is owned by root (written by an older image that ran as root)." >&2
      echo "  Fix once on the host:  sudo chown -R \"\$USER\" data/" >&2
      exit 1
    fi
  done
  drop "$owner_uid" "$owner_gid" "$@"
fi
if writable_by "$APP_UID" "$APP_GID"; then
  drop "$APP_UID" "$APP_GID" "$@"
fi
if in_user_namespace; then
  echo "note: $data_root is root-owned inside a user namespace (rootless Docker/Podman);" \
       "running as container root, which maps to the unprivileged host user." >&2
  exec setpriv --no-new-privs -- "$@"
fi
if [ -z "$(ls -A "$data_root")" ]; then
  chown "$APP_UID:$APP_GID" "$data_root"  # empty dir only, non-recursive
  drop "$APP_UID" "$APP_GID" "$@"
fi
echo "ERROR: $data_root is owned by root and not writable by the app user (uid $APP_UID)." >&2
echo "  If it is the repo's ./data (e.g. left over from an older image that ran as root)," \
     "fix it on the host:  sudo chown -R \"\$USER\" data/" >&2
exit 1
