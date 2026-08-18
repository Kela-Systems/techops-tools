#!/usr/bin/env bash
# Print a SHA-512 crypt hash ($6$salt$hash) for use as KELA_PASSWORD_HASH.
#
# The usual `openssl passwd -6` recipe does not work on macOS: /usr/bin/openssl
# is LibreSSL, whose `passwd` offers only -crypt, -1 and -apr1. Worse, it fails
# in a way that yields an empty string, which used to fall through to the
# built-in default password. This finds an OpenSSL that can actually do it.
#
# Usage:
#   ./lib/mkpasswd.sh          prompt twice, print the hash
#   ./lib/mkpasswd.sh --stdin  read the password from stdin, print the hash

set -euo pipefail

find_openssl() {
  local candidates=() brewprefix="" candidate
  [ -n "${OPENSSL:-}" ] && candidates+=("$OPENSSL")
  candidates+=(
    openssl
    /opt/homebrew/opt/openssl@3/bin/openssl
    /usr/local/opt/openssl@3/bin/openssl
    /opt/homebrew/bin/openssl
    /usr/local/bin/openssl
  )
  if command -v brew >/dev/null 2>&1; then
    brewprefix=$(brew --prefix openssl@3 2>/dev/null || true)
    [ -n "$brewprefix" ] && candidates+=("$brewprefix/bin/openssl")
  fi
  for candidate in "${candidates[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    # LibreSSL rejects -6, so probe rather than trusting the name
    if printf 'probe\n' | "$candidate" passwd -6 -stdin >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

ssl=$(find_openssl) || {
  cat >&2 <<'EOF'
error: no openssl with SHA-512 crypt support found.

  macOS ships LibreSSL as /usr/bin/openssl and its `passwd` has no -6.
  Fix with:   brew install openssl@3
  Or point at one explicitly:   OPENSSL=/path/to/openssl ./lib/mkpasswd.sh
  Or generate the hash on any Linux box:   openssl passwd -6
EOF
  exit 1
}

pw=""
pw2=""
if [ "${1:-}" = --stdin ]; then
  IFS= read -r pw || true
else
  printf 'Password for the kela user: ' >&2; IFS= read -rs pw || true; echo >&2
  printf 'Again:                      ' >&2; IFS= read -rs pw2 || true; echo >&2
  [ "$pw" = "$pw2" ] || { echo "error: passwords do not match" >&2; exit 1; }
fi
[ -n "$pw" ] || { echo "error: empty password" >&2; exit 1; }

printf '%s\n' "$pw" | "$ssl" passwd -6 -stdin
