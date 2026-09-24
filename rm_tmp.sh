#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
removed=0

remove_path() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    rm -rf -- "$path"
    printf 'removed: %s\n' "${path#"$root"/}"
    removed=$((removed + 1))
  fi
}

for name in build dist install log; do
  remove_path "$root/$name"
done

while IFS= read -r -d '' path; do
  remove_path "$path"
done < <(
  find "$root" -depth \
    \( -type d \( -name __pycache__ -o -name '*.egg-info' -o \
                    -name .pytest_cache -o -name .mypy_cache -o \
                    -name .ruff_cache \) \
       -o -type f \( -name '*.py[cod]' -o -name '.coverage*' -o \
                      -name .DS_Store -o -name '*~' -o \
                      -name '*.swp' -o -name '*.swo' \) \) \
    -print0
)

printf 'done: %d paths removed\n' "$removed"
