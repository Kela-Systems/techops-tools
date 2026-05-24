#/usr/local/bin/fs_cleanup.sh

#!/usr/bin/env bash
set -euo pipefail

#
# Cleanup action invoked by Monit (or manually).
# Iterates over known folders and deletes all files except the most recent N.
#
# Usage:
#   fs_cleanup.sh                          # uses default config
#   fs_cleanup.sh -c /path/to/folders.conf # custom config
#   fs_cleanup.sh -d /single/folder -k 1   # one-off single folder
#

CONF_FILE="/etc/fs_monitor/folders.conf"
LOG_FILE="/var/log/fs_monitor.log"
DEFAULT_KEEP=1
SINGLE_DIR=""
SINGLE_KEEP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--config) CONF_FILE="$2"; shift 2 ;;
        -d|--dir)    SINGLE_DIR="$2"; shift 2 ;;
        -k|--keep)   SINGLE_KEEP="$2"; shift 2 ;;
        -l|--log)    LOG_FILE="$2"; shift 2 ;;
        *)           echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    local level="$1"; shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" | tee -a "$LOG_FILE"
}

clean_folder() {
    local dir="$1"
    local keep="$2"

    if [[ ! -d "$dir" ]]; then
        log "ERROR" "Directory does not exist: $dir"
        return 1
    fi

    local file_count
    file_count=$(find "$dir" -maxdepth 1 -type f | wc -l)

    if [[ "$file_count" -le "$keep" ]]; then
        log "INFO" "[$dir] $file_count file(s), nothing to clean (keep=$keep)"
        return 0
    fi

    log "WARN" "[$dir] Cleanup: $file_count files found, keeping $keep most recent"

    local deleted=0
    local freed_kb=0

    while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        local size_kb
        size_kb=$(du -k "$file" 2>/dev/null | awk '{print $1}')
        if rm -f "$file"; then
            log "INFO" "[$dir] Deleted: $(basename "$file") (${size_kb} KB)"
            ((deleted++)) || true
            ((freed_kb += size_kb)) || true
        else
            log "ERROR" "[$dir] Failed to delete: $file"
        fi
    done < <(
        find "$dir" -maxdepth 1 -type f -printf '%T@ %p\n' \
            | sort -n \
            | head -n -"$keep" \
            | awk '{print $2}'
    )

    log "INFO" "[$dir] Done: removed $deleted file(s), freed ~$((freed_kb / 1024)) MB"
}

# Single-folder mode (backward compatible, useful for testing)
if [[ -n "$SINGLE_DIR" ]]; then
    clean_folder "$SINGLE_DIR" "${SINGLE_KEEP:-$DEFAULT_KEEP}"
    exit $?
fi

# Multi-folder mode: read from config
if [[ ! -f "$CONF_FILE" ]]; then
    log "ERROR" "Config file not found: $CONF_FILE"
    exit 1
fi

log "INFO" "=== Cleanup run started ==="

total_folders=0
while IFS= read -r line; do
    # skip blank lines and comments
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    dir=$(echo "$line" | awk '{print $1}')
    keep=$(echo "$line" | awk '{print $2}')
    keep="${keep:-$DEFAULT_KEEP}"

    clean_folder "$dir" "$keep"
    ((total_folders++)) || true
done < "$CONF_FILE"

log "INFO" "=== Cleanup run finished ($total_folders folder(s) processed) ==="
