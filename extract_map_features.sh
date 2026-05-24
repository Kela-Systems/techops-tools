#!/usr/bin/env bash
# extract_map_features.sh
#
# Extract the map_features table from a Kela HUB server running in a
# Kubernetes cluster and save it locally as CSV.
#
# Usage:
#   ./extract_map_features.sh --context <ctx> [options]
#
# Examples:
#   ./extract_map_features.sh --context prod-cluster
#   ./extract_map_features.sh --context staging -o features.csv --where "classification = 'Alarm'"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage:
  ./extract_map_features.sh --context <ctx> [options]

Required:
  --context <ctx>         Kubernetes context for the hub server

Options:
  --pod <pod>             Pod name (default: postgresql-0)
  --user <user>           DB user (default: kela)
  --namespace <ns>        Namespace (default: kela)
  --table <name>          Table name (default: map_features)
  --where <clause>        Optional WHERE clause to filter rows
  --columns <cols>        Comma-separated column list (default: all)
  -o, --output <path>     Output CSV path (default: map_features_<context>_<timestamp>.csv)
  --with-header           Include CSV header row (default: true)
  --no-header             Omit CSV header row
  --count-only            Just print the row count and exit
  --list-contexts         List available kubectl contexts and exit
  -h, --help              Show this help

Examples:
  # Export all features from production
  ./extract_map_features.sh --context prod-cluster

  # Export only Alarm zones, no header
  ./extract_map_features.sh --context prod --where "classification = 'Alarm'" --no-header

  # Export specific columns
  ./extract_map_features.sh --context staging --columns "id,name,classification,color"

  # Just count rows
  ./extract_map_features.sh --context prod --count-only
USAGE
}

# ----------------------------------------------------------
# Defaults
# ----------------------------------------------------------
CTX=""
POD="postgresql-0"
DB_USER="kela"
NAMESPACE="kela"
TABLE_NAME="map_features"
WHERE_CLAUSE=""
COLUMNS=""
OUTPUT=""
WITH_HEADER="true"
COUNT_ONLY="false"
LIST_CONTEXTS="false"

# ----------------------------------------------------------
# Parse arguments
# ----------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)        CTX="${2:-}";          shift 2;;
    --pod)            POD="${2:-}";          shift 2;;
    --user)           DB_USER="${2:-}";      shift 2;;
    --namespace)      NAMESPACE="${2:-}";    shift 2;;
    --table)          TABLE_NAME="${2:-}";   shift 2;;
    --where)          WHERE_CLAUSE="${2:-}"; shift 2;;
    --columns)        COLUMNS="${2:-}";      shift 2;;
    -o|--output)      OUTPUT="${2:-}";       shift 2;;
    --with-header)    WITH_HEADER="true";    shift;;
    --no-header)      WITH_HEADER="false";   shift;;
    --count-only)     COUNT_ONLY="true";     shift;;
    --list-contexts)  LIST_CONTEXTS="true";  shift;;
    -h|--help)        usage; exit 0;;
    *)                die "Unknown argument: $1 (use --help)";;
  esac
done

# ----------------------------------------------------------
# List contexts mode
# ----------------------------------------------------------
if [[ "$LIST_CONTEXTS" == "true" ]]; then
  log "Available kubectl contexts:"
  kubectl config get-contexts -o name
  exit 0
fi

# ----------------------------------------------------------
# Validation
# ----------------------------------------------------------
command -v kubectl &>/dev/null || die "kubectl is not installed or not in PATH"
[[ -n "$CTX" ]] || die "--context is required"
[[ "$TABLE_NAME" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die "Invalid table name: $TABLE_NAME"

if ! kubectl config get-contexts "$CTX" &>/dev/null; then
  log "Context '${CTX}' not found."
  read -rp "Set it up with k3s_kubeconfig.sh? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    die "Context '${CTX}' not found. Use --list-contexts to see available contexts."
  fi
  read -rp "SSH user: " ssh_user
  read -rp "SSH host: " ssh_host
  "${SCRIPT_DIR}/k3s_kubeconfig.sh" --user "$ssh_user" --host "$ssh_host" --context-name "$CTX"
  kubectl config get-contexts "$CTX" &>/dev/null || die "Failed to create context '${CTX}'"
  log "Context '${CTX}' is now available"
fi

# ----------------------------------------------------------
# Helper: run psql via kubectl exec
# ----------------------------------------------------------
hub_psql() {
  kubectl exec -i \
    --context "$CTX" \
    -n "$NAMESPACE" \
    "$POD" -- \
    psql -U "$DB_USER" "$@"
}

# ----------------------------------------------------------
# Connectivity check
# ----------------------------------------------------------
log "Connecting: context=$CTX pod=$POD ns=$NAMESPACE user=$DB_USER"
hub_psql -Atc "SELECT 1;" &>/dev/null \
  || die "Cannot connect to database"
log "Connected"

# ----------------------------------------------------------
# Count-only mode
# ----------------------------------------------------------
if [[ "$COUNT_ONLY" == "true" ]]; then
  SQL="SELECT COUNT(*) FROM ${TABLE_NAME}"
  [[ -n "$WHERE_CLAUSE" ]] && SQL="${SQL} WHERE ${WHERE_CLAUSE}"
  COUNT=$(hub_psql -Atc "${SQL};")
  log "Row count: ${COUNT}"
  exit 0
fi

# ----------------------------------------------------------
# Build SELECT query
# ----------------------------------------------------------
SELECT_COLS="${COLUMNS:-*}"
QUERY="SELECT ${SELECT_COLS} FROM ${TABLE_NAME}"
[[ -n "$WHERE_CLAUSE" ]] && QUERY="${QUERY} WHERE ${WHERE_CLAUSE}"

# ----------------------------------------------------------
# Default output filename
# ----------------------------------------------------------
if [[ -z "$OUTPUT" ]]; then
  TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
  OUTPUT="map_features_${CTX}_${TIMESTAMP}.csv"
fi

# ----------------------------------------------------------
# Export
# ----------------------------------------------------------
HEADER_OPT="true"
[[ "$WITH_HEADER" == "false" ]] && HEADER_OPT="false"

log "Exporting from ${TABLE_NAME}..."
COPY_SQL="COPY (${QUERY}) TO STDOUT WITH (FORMAT csv, HEADER ${HEADER_OPT}, NULL '');"
echo "$COPY_SQL" | hub_psql -q > "$OUTPUT"

ROW_COUNT=$(wc -l < "$OUTPUT" | tr -d ' ')
[[ "$WITH_HEADER" == "true" ]] && ROW_COUNT=$((ROW_COUNT - 1))

log "Exported ${ROW_COUNT} rows to ${OUTPUT}"
log "File size: $(du -h "$OUTPUT" | cut -f1)"
