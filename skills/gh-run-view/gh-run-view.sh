#!/usr/bin/env bash
# View GitHub Actions logs with terminal colors
#
# Usage:
#   gh-run-view <github-actions-url>         # Parse run/job from GitHub URL
#   gh-run-view <job-id> <run-id>            # Specific job in a run
#
# Pipe to `less -R` for pagination with colors

set -euo pipefail

ESC=$'\e'
BOLD="${ESC}[1m"
RESET="${ESC}[0m"
BLUE="${ESC}[1;34m"
RED="${ESC}[1;31m"
YELLOW="${ESC}[1;33m"
CYAN="${ESC}[1;36m"
DIM="${ESC}[0;90m"

colorize_log() {
  # Strip "job_name\tstep_name\t" prefix from gh output
  sed "s/^[^\t]*\t[^\t]*\t//" | sed "
    # Convert GitHub Actions workflow commands to ANSI colors
    s/##\[group\]/${BLUE}▶ /g
    s/##\[endgroup\]/${RESET}/g
    s/##\[error\]/${RED}✗ ERROR: /g
    s/##\[warning\]/${YELLOW}⚠ WARNING: /g
    s/##\[notice\]/${CYAN}ℹ NOTICE: /g
    s/##\[debug\]/${DIM}DEBUG: /g
  "
}

show_help() {
  cat <<EOF
${BOLD}gh-run-view${RESET} - View GitHub Actions logs with terminal colors

${BOLD}USAGE${RESET}
  gh-run-view <github-actions-url>         Parse run/job from GitHub URL
  gh-run-view <job-id> <run-id>            Specific job in a run

${BOLD}EXAMPLES${RESET}
  gh-run-view https://github.com/owner/repo/actions/runs/12345/job/67890
  gh-run-view 67890 12345
  gh-run-view <url-or-args> | less -R

EOF
}

# Parse GitHub Actions URL to extract run_id and job_id
# Formats:
#   https://github.com/owner/repo/actions/runs/12345678/job/99999999
#   https://github.com/owner/repo/actions/runs/12345678/jobs/99999999
parse_github_url() {
  local url="$1"
  local run_id=""
  local job_id=""
  
  # Extract run ID
  if [[ "$url" =~ /actions/runs/([0-9]+) ]]; then
    run_id="${BASH_REMATCH[1]}"
  fi
  
  # Extract job ID (supports both /job/ and /jobs/)
  if [[ "$url" =~ /jobs?/([0-9]+) ]]; then
    job_id="${BASH_REMATCH[1]}"
  fi
  
  echo "$run_id $job_id"
}

is_github_url() {
  [[ "$1" =~ ^https?://github\.com/.*/actions/runs/[0-9]+ ]]
}

fetch_log() {
  local run_id="$1"
  local job_id="$2"
  
  gh run view "$run_id" --job "$job_id" --log
}

main() {
  local run_id=""
  local job_id=""
  
  if [[ $# -eq 0 ]]; then
    show_help
    exit 1
  fi
  
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      if is_github_url "$1"; then
        read -r run_id job_id <<< "$(parse_github_url "$1")"
        if [[ -z "$job_id" ]]; then
          echo "Error: URL must include job ID (e.g., /job/12345)" >&2
          exit 1
        fi
      elif [[ $# -eq 2 ]]; then
        job_id="$1"
        run_id="$2"
      else
        echo "Error: Expected <url> or <job-id> <run-id>" >&2
        show_help
        exit 1
      fi
      ;;
  esac
  
  fetch_log "$run_id" "$job_id" | colorize_log
}

main "$@"
