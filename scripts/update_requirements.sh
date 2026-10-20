#!/usr/bin/env bash
# Regenerate the pinned requirements files from uv.lock, for people who install with plain pip.
# pyproject.toml and uv.lock stay the source of truth; never edit the requirements files by hand.
#
#   scripts/update_requirements.sh          rewrite requirements.txt, requirements-voice.txt, requirements-dev.txt
#   scripts/update_requirements.sh --check  exit 1 when one of them is out of date (CI)
set -euo pipefail

cd "$(dirname "$0")/.."

check=false
if [[ "${1:-}" == "--check" ]]; then
  check=true
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

export_to() {  # $1 = target file, the rest = uv export flags
  local target=$1
  shift
  uv export --frozen --no-hashes --format requirements-txt --quiet "$@" -o "$target"
}

generate() {  # $1 = output directory
  export_to "$1/requirements.txt" --no-dev                   # core: the `jarvis` command and every text feature
  export_to "$1/requirements-voice.txt" --no-dev --extra voice  # core plus the voice extra (wake word, VAD, Whisper)
  export_to "$1/requirements-dev.txt" --only-dev --no-emit-project  # tests and linting, on top of one of the above
}

if $check; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  generate "$tmp"
  stale=0
  for name in requirements.txt requirements-voice.txt requirements-dev.txt; do
    # the header line names the output file; compare everything else
    if ! diff -q <(grep -v '^#' "$tmp/$name") <(grep -v '^#' "$name" 2>/dev/null) >/dev/null; then
      echo "$name is out of date: run scripts/update_requirements.sh" >&2
      stale=1
    fi
  done
  exit $stale
fi

generate .
echo "requirements.txt, requirements-voice.txt and requirements-dev.txt updated from uv.lock"
