#!/usr/bin/env bash
# pg_query.sh
#
# Query a PostgreSQL database running in a Kubernetes cluster via kubectl exec.
# Targets the postgres-0 pod by default.
#
# Usage:
#   ./pg_query.sh --context <KUBE_CONTEXT> --db <DATABASE> [options] [-- <SQL>]
#
# Examples:
#   # Run a single query
#   ./pg_query.sh --context prod-cluster --db myapp -- "SELECT * FROM users LIMIT 5;"
#
#   # Run query from file
#   ./pg_query.sh --context prod-cluster --db myapp --file query.sql
#
#   # Interactive psql session
#   ./pg_query.sh --context prod-cluster --db myapp --interactive
#
#   # List available contexts
#   ./pg_query.sh --list-contexts

set -euo pipefail

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage:
  ./pg_query.sh --context <KUBE_CONTEXT> --db <DATABASE> [options] [-- <SQL>]

Required:
  --context <ctx>       Kubernetes context to use
  --db <database>       PostgreSQL database name

Options:
  --namespace <ns>      Kubernetes namespace (default: kela)
  --pod <pod>           Pod name (default: postgres-0)
  --user <user>         PostgreSQL user (default: admin)
  --file <file>         SQL file to execute
  --interactive, -i     Open interactive psql session
  --list-contexts       List available kubectl contexts and exit
  --dry-run             Print the kubectl command without executing
  -h, --help            Show this help

SQL Query:
  Provide SQL after -- separator:
    ./pg_query.sh --context my-ctx --db mydb -- "SELECT 1;"

Notes:
  - The script uses 'kubectl exec' to run psql inside the postgres pod.
  - For interactive sessions, your terminal must support TTY.
  - Ensure you have kubectl configured with the appropriate context.
USAGE
}

# -------------------------
# Args
# -------------------------
KUBE_CONTEXT=""
NAMESPACE="kela"
POD_NAME="postgres-0"
DB_NAME="c2-db"
DB_USER="admin"
SQL_QUERY=""
SQL_FILE=""
INTERACTIVE="false"
DRY_RUN="false"
LIST_CONTEXTS="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) KUBE_CONTEXT="${2:-}"; shift 2;;
    --namespace) NAMESPACE="${2:-}"; shift 2;;
    --pod) POD_NAME="${2:-}"; shift 2;;
    --db) DB_NAME="${2:-}"; shift 2;;
    --user) DB_USER="${2:-}"; shift 2;;
    --file) SQL_FILE="${2:-}"; shift 2;;
    --interactive|-i) INTERACTIVE="true"; shift 1;;
    --list-contexts) LIST_CONTEXTS="true"; shift 1;;
    --dry-run) DRY_RUN="true"; shift 1;;
    -h|--help) usage; exit 0;;
    --) shift; SQL_QUERY="$*"; break;;
    *) die "Unknown arg: $1 (use --help)";;
  esac
done

# -------------------------
# List contexts mode
# -------------------------
if [[ "$LIST_CONTEXTS" == "true" ]]; then
  log "Available kubectl contexts:"
  kubectl config get-contexts -o name
  exit 0
fi

# -------------------------
# Validation
# -------------------------
[[ -n "$KUBE_CONTEXT" ]] || die "--context is required"
[[ -n "$DB_NAME" ]] || die "--db is required"

# Verify context exists
if ! kubectl config get-contexts "$KUBE_CONTEXT" &>/dev/null; then
  die "Context '$KUBE_CONTEXT' not found. Use --list-contexts to see available contexts."
fi

# Determine query source
if [[ -n "$SQL_FILE" && -n "$SQL_QUERY" ]]; then
  die "Cannot specify both --file and inline SQL query"
fi

if [[ -n "$SQL_FILE" ]]; then
  [[ -f "$SQL_FILE" ]] || die "SQL file not found: $SQL_FILE"
  SQL_QUERY="$(cat "$SQL_FILE")"
fi

# -------------------------
# Build kubectl command
# -------------------------
build_kubectl_cmd() {
  local base_cmd=(
    kubectl exec
    --context "$KUBE_CONTEXT"
    --namespace "$NAMESPACE"
  )

  if [[ "$INTERACTIVE" == "true" ]]; then
    base_cmd+=(-it)
  fi

  base_cmd+=("$POD_NAME" --)

  if [[ "$INTERACTIVE" == "true" ]]; then
    # Interactive psql session
    base_cmd+=(psql -U "$DB_USER" -d "$DB_NAME")
  elif [[ -n "$SQL_QUERY" ]]; then
    # Execute query
    base_cmd+=(psql -U "$DB_USER" -d "$DB_NAME" -c "$SQL_QUERY")
  else
    die "No SQL query provided. Use --interactive, --file, or provide SQL after --"
  fi

  printf '%q ' "${base_cmd[@]}"
}

KUBECTL_CMD=$(build_kubectl_cmd)

# -------------------------
# Execute
# -------------------------
if [[ "$DRY_RUN" == "true" ]]; then
  log "Dry run - command would be:"
  echo "$KUBECTL_CMD"
  exit 0
fi

log "Executing on context '$KUBE_CONTEXT', pod '$POD_NAME', database '$DB_NAME'"

if [[ "$INTERACTIVE" == "true" ]]; then
  kubectl exec \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    -it "$POD_NAME" -- \
    psql -U "$DB_USER" -d "$DB_NAME"
else
  kubectl exec \
    --context "$KUBE_CONTEXT" \
    --namespace "$NAMESPACE" \
    "$POD_NAME" -- \
    psql -U "$DB_USER" -d "$DB_NAME" -c "$SQL_QUERY"
fi

