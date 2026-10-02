#!/usr/bin/env bash

# Read-only target check. This script never starts or stops containers.
set -u
status=0

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"
architecture="$(uname -m)"

printf 'EdSpeech N95 preflight\n'
printf '%s\n' '===================='
printf 'Project: %s\n' "$project_root"
printf 'Architecture: %s\n' "$architecture"
printf 'Kernel: %s\n' "$(uname -srm)"
case "$architecture" in
  x86_64|amd64) printf '%s\n' 'Target architecture: Intel/AMD 64-bit' ;;
  *) printf '%s\n' 'Target architecture: warning, this is not the N95 x86_64 target' >&2; status=1 ;;
esac

if command -v docker >/dev/null 2>&1; then
  printf 'Docker: '
  docker --version
  printf 'Compose: '
  if docker compose version; then
    :
  else
    printf '%s\n' 'Compose: Docker Compose v2 is unavailable' >&2
    status=1
  fi
  if docker info >/dev/null 2>&1; then
    printf '%s\n' 'Docker engine: reachable'
  else
    printf '%s\n' 'Docker engine: unavailable (start it before deployment)' >&2
    status=1
  fi
else
  printf '%s\n' 'Docker: missing (install Docker Engine and Compose v2)' >&2
  status=1
fi

if command -v free >/dev/null 2>&1; then
  printf '%s\n' 'Memory:'
  free -h
else
  printf '%s\n' 'Memory: free(1) unavailable; inspect the host manually' >&2
fi

printf '%s\n' 'Disk:'
df -h "$project_root"

if [ -f .cache/edspeech-transfer-sha256.txt ] && command -v sha256sum >/dev/null 2>&1; then
  printf '%s\n' 'Transfer checksums:'
  if ! sha256sum -c .cache/edspeech-transfer-sha256.txt; then
    status=1
  fi
elif [ -f .cache/edspeech-transfer-sha256.txt ]; then
  printf '%s\n' 'sha256sum is missing; verify the transfer manifest manually' >&2
  status=1
else
  printf '%s\n' 'Checksum manifest missing: .cache/edspeech-transfer-sha256.txt' >&2
  status=1
fi

printf '%s\n' 'Preflight does not start services. Use the Phase 14 checklist for deployment.'
exit "$status"
