#!/usr/bin/env bash
# migrate_map_features.sh
#
# Migrate the map_features table between old Platform and new Kela HUB environments running in
# Kubernetes clusters. Connects via kubectl exec using separate contexts for
# source and destination.
#
# Transforms the old Platform schema (nested JSON raw_feature column) into the
# flat Kela HUB schema with discrete columns for color, icon, dash_array, etc.
#
# Usage:
#   ./migrate_map_features.sh --source-ctx <ctx> --dest-ctx <ctx> [options]
#
# Examples:
#   ./migrate_map_features.sh --source-ctx prod --dest-ctx staging
#   ./migrate_map_features.sh --source-ctx prod --dest-ctx staging --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log(){ echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage:
  ./migrate_map_features.sh --source-ctx <ctx> --dest-ctx <ctx> [options]

Required:
  --source-ctx <ctx>      Kubernetes context for the source environment
  --dest-ctx <ctx>        Kubernetes context for the destination environment

Options:
  --source-pod <pod>      Source pod name (default: postgres-0)
  --source-user <user>    Source DB user (default: admin)
  --source-db <db>        Source database name (default: c2-db)
  --source-ns <ns>        Source namespace (default: kela)
  --dest-pod <pod>        Destination pod name (default: postgresql-0)
  --dest-user <user>      Destination DB user (default: kela)
  --dest-ns <ns>          Destination namespace (default: kela)
  --dest-hub-id <id>      Destination hub identity for origin_hub_id.
                          Auto-detected from the hub-server SITE_ID env if omitted.
  --magos-pod <pod>       Magos pod name for zone configs (default: magos-service-0)
  --magos-container <ctr> Magos container name (default: mass)
  --table <name>          Table name on both envs (default: map_features)
  --no-restart            Skip restarting hub-server pod after migration
  --dry-run               Export transformed data and display without inserting
  --list-contexts         List available kubectl contexts and exit
  -h, --help              Show this help

Examples:
  # Full migration with defaults
  ./migrate_map_features.sh --source-ctx prod-cluster --dest-ctx staging-cluster

  # Preview transformed data without writing
  ./migrate_map_features.sh --source-ctx prod-cluster --dest-ctx staging-cluster --dry-run

  # Override pod names and users
  ./migrate_map_features.sh --source-ctx prod --dest-ctx staging \
    --source-pod my-pg-0 --source-user myuser \
    --dest-pod pg-dest-0 --dest-user destuser
USAGE
}

# ----------------------------------------------------------
# Parse arguments
# ----------------------------------------------------------
SOURCE_CTX=""
DEST_CTX=""
SOURCE_POD="postgres-0"
SOURCE_USER="admin"
SOURCE_DB="c2-db"
SOURCE_NS="kela"
DEST_POD="postgresql-0"
DEST_USER="kela"
DEST_NS="kela"
DEST_HUB_ID=""
MAGOS_POD="magos-service-0"
MAGOS_CONTAINER="mass"
TABLE_NAME="map_features"
DRY_RUN="false"
NO_RESTART="false"
LIST_CONTEXTS="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-ctx)   SOURCE_CTX="${2:-}";  shift 2;;
    --dest-ctx)     DEST_CTX="${2:-}";    shift 2;;
    --source-pod)   SOURCE_POD="${2:-}";  shift 2;;
    --source-user)  SOURCE_USER="${2:-}"; shift 2;;
    --source-db)    SOURCE_DB="${2:-}";   shift 2;;
    --source-ns)    SOURCE_NS="${2:-}";   shift 2;;
    --dest-pod)     DEST_POD="${2:-}";    shift 2;;
    --dest-user)    DEST_USER="${2:-}";   shift 2;;
    --dest-ns)      DEST_NS="${2:-}";     shift 2;;
    --dest-hub-id)  DEST_HUB_ID="${2:-}"; shift 2;;
    --magos-pod)    MAGOS_POD="${2:-}";   shift 2;;
    --magos-container) MAGOS_CONTAINER="${2:-}"; shift 2;;
    --table)        TABLE_NAME="${2:-}";  shift 2;;
    --dry-run)      DRY_RUN="true";      shift;;
    --no-restart)   NO_RESTART="true";   shift;;
    --list-contexts) LIST_CONTEXTS="true"; shift;;
    -h|--help)      usage; exit 0;;
    *)              die "Unknown argument: $1 (use --help)";;
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
command -v python3 &>/dev/null || die "python3 is required for alarm zone migration"
python3 -c "import yaml" 2>/dev/null || die "PyYAML is required (pip3 install pyyaml)"

[[ -n "$SOURCE_CTX" ]] || die "--source-ctx is required"
[[ -n "$DEST_CTX" ]]   || die "--dest-ctx is required"
[[ "$TABLE_NAME" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die "Invalid table name: $TABLE_NAME"

ensure_context() {
  local ctx="$1" label="$2"
  if kubectl config get-contexts "$ctx" &>/dev/null; then
    return 0
  fi
  log "${label} context '${ctx}' not found."
  read -rp "Set it up with k3s_kubeconfig.sh? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    die "${label} context '${ctx}' not found. Use --list-contexts to see available contexts."
  fi
  read -rp "SSH user: " ssh_user
  read -rp "SSH host: " ssh_host
  "${SCRIPT_DIR}/k3s_kubeconfig.sh" --user "$ssh_user" --host "$ssh_host" --context-name "$ctx"
  if ! kubectl config get-contexts "$ctx" &>/dev/null; then
    die "Failed to create ${label} context '${ctx}'"
  fi
  log "${label} context '${ctx}' is now available"
}

ensure_context "$SOURCE_CTX" "Source"
ensure_context "$DEST_CTX" "Destination"

# ----------------------------------------------------------
# Helper: run psql on source / destination via kubectl exec
# ----------------------------------------------------------
source_psql() {
  kubectl exec -i \
    --context "$SOURCE_CTX" \
    -n "$SOURCE_NS" \
    "$SOURCE_POD" -- \
    psql -U "$SOURCE_USER" -d "$SOURCE_DB" "$@"
}

dest_psql() {
  kubectl exec -i \
    --context "$DEST_CTX" \
    -n "$DEST_NS" \
    "$DEST_POD" -- \
    psql -U "$DEST_USER" "$@"
}

# ----------------------------------------------------------
# Connectivity checks
# ----------------------------------------------------------
log "Testing source: context=$SOURCE_CTX pod=$SOURCE_POD ns=$SOURCE_NS db=$SOURCE_DB user=$SOURCE_USER"
source_psql -Atc "SELECT 1;" &>/dev/null \
  || die "Cannot connect to source database"

log "Testing destination: context=$DEST_CTX pod=$DEST_POD ns=$DEST_NS user=$DEST_USER"
dest_psql -Atc "SELECT 1;" &>/dev/null \
  || die "Cannot connect to destination database"

log "Both databases are reachable"

# ----------------------------------------------------------
# Resolve destination hub identity (origin_hub_id)
#
# Every map_features row carries origin_hub_id, the federation tag identifying
# which hub the feature originated from. The destination hub only treats rows
# whose origin_hub_id matches its own SITE_ID as "local" and loads them; rows
# tagged with any other value are persisted but filtered out. So migrated rows
# must be stamped with the destination hub's SITE_ID.
# ----------------------------------------------------------
if [[ -z "$DEST_HUB_ID" ]]; then
  log "Auto-detecting destination hub id from hub-server SITE_ID..."
  DEST_HUB_ID=$(kubectl get deploy \
    --context "$DEST_CTX" \
    -n "$DEST_NS" \
    hub-server \
    -o jsonpath='{range .spec.template.spec.containers[*].env[?(@.name=="SITE_ID")]}{.value}{"\n"}{end}' \
    2>/dev/null | head -1 | tr -d '[:space:]' || true)

  [[ -n "$DEST_HUB_ID" ]] || die "Could not auto-detect destination hub id (SITE_ID) from the hub-server deployment. Pass it explicitly with --dest-hub-id <id>."
  log "Detected destination hub id: ${DEST_HUB_ID}"
else
  log "Using destination hub id: ${DEST_HUB_ID}"
fi

# ----------------------------------------------------------
# Pre-flight: verify destination table has expected columns
# ----------------------------------------------------------
log "Verifying destination table schema..."
MISSING_COLS=$(dest_psql -Atc "
  SELECT string_agg(c, ', ')
  FROM unnest(ARRAY[
    'id','name','classification','description','shape',
    'altitude_hae_meters','altitude_agl_meters','color','creation_time',
    'fill','fill_opacity','show_label','dash_array','icon','icon_size',
    'active_daily_windows','origin_hub_id'
  ]) AS c
  WHERE c NOT IN (
    SELECT column_name FROM information_schema.columns
    WHERE table_name = '${TABLE_NAME}'
  );
")
[[ -z "$MISSING_COLS" ]] || die "Destination table '${TABLE_NAME}' is missing columns: ${MISSING_COLS}"

# ----------------------------------------------------------
# Build the transformation SQL
# ----------------------------------------------------------
# Column mapping overview:
#   source uid                            -> id
#   raw_feature.extra_properties.label    -> name
#   (all topics)                          -> classification = 'Annotation'
#   raw_feature.description               -> description
#   source geometry (WKB)                 -> shape (direct copy)
#   raw_feature.geometry.hae              -> altitude_hae_meters (0 treated as NULL)
#   (none)                                -> altitude_agl_meters = NULL
#   raw_feature.extra_properties.color    -> color (CSS var -> hex)
#   source time                           -> creation_time
#   (always)                              -> fill = true
#   raw_feature.extra_properties.fillOpacity -> fill_opacity
#   (derived from label presence)         -> show_label
#   dashArray + weight                    -> dash_array (e.g. solid_2, dashed_1)
#   raw_feature.extra_properties.icon     -> icon (default: map-pin)
#   raw_feature.extra_properties.iconSize -> icon_size (default: 32)
#   (always)                              -> active_daily_windows = '[]'
#   (destination hub SITE_ID)             -> origin_hub_id
#
# Mantine CSS variable -> hex color mapping (standard Mantine v7 palette).
# If your source app uses a custom theme, update the CASE values below.
# ----------------------------------------------------------

build_transform_sql() {
  cat <<SQL
WITH parsed AS (
  SELECT
    uid,
    time,
    topic,
    geometry,
    regexp_replace(TRIM(BOTH '"' FROM raw_feature::text), '\\\\(.)', '\\1', 'g')::jsonb AS rf
  FROM ${TABLE_NAME}
),
extracted AS (
  SELECT
    uid,
    time,
    topic,
    geometry,
    rf,
    rf->'extra_properties' AS ep,
    COALESCE(NULLIF(TRIM(rf->'extra_properties'->>'weight'), ''), '') AS weight_val,
    COALESCE(NULLIF(TRIM(rf->'extra_properties'->>'dashArray'), ''), '') AS dash_val
  FROM parsed
)
SELECT
  uid AS id,
  COALESCE(rf->'extra_properties'->>'label', '') AS name,
  CASE topic
    WHEN 'no-fly-zone' THEN 'NoFly'
    ELSE 'Annotation'
  END AS classification,
  COALESCE(rf->>'description', '') AS description,
  geometry AS shape,
  NULLIF((rf->'geometry'->>'hae')::numeric, 0) AS altitude_hae_meters,
  NULL::numeric AS altitude_agl_meters,

  -- Mantine CSS variable -> hex color
  CASE rf->'extra_properties'->>'color'
    WHEN 'var(--mantine-color-blue-text)'     THEN '#1864AB'
    WHEN 'var(--mantine-color-blue-filled)'   THEN '#228BE6'
    WHEN 'var(--mantine-color-red-text)'      THEN '#C92A2A'
    WHEN 'var(--mantine-color-red-filled)'    THEN '#FA5252'
    WHEN 'var(--mantine-color-yellow-text)'   THEN '#FCC419'
    WHEN 'var(--mantine-color-orange-text)'   THEN '#D9480F'
    WHEN 'var(--mantine-color-orange-filled)' THEN '#FD7E14'
    WHEN 'var(--mantine-color-lime-filled)'   THEN '#82C91E'
    WHEN 'var(--mantine-color-teal-text)'     THEN '#087F5B'
    WHEN 'var(--mantine-color-teal-filled)'   THEN '#20C997'
    WHEN 'var(--mantine-color-cyan-text)'     THEN '#0B7285'
    WHEN 'var(--mantine-color-gray-filled)'   THEN '#868E96'
    ELSE '#1864AB'
  END AS color,

  time AS creation_time,
  true AS fill,
  COALESCE((rf->'extra_properties'->>'fillOpacity')::numeric, 0.2) AS fill_opacity,

  CASE
    WHEN COALESCE(rf->'extra_properties'->>'label', '') != '' THEN true
    ELSE false
  END AS show_label,

  -- dash_array: combine dashArray pattern + weight from source
  CASE
    WHEN dash_val != ''
      THEN 'dashed_' || CASE WHEN weight_val != '' THEN weight_val ELSE '1' END
    WHEN weight_val != ''
      THEN 'solid_' || weight_val
    ELSE ''
  END AS dash_array,

  -- Icon name mapping (source app -> destination app)
  CASE COALESCE(rf->'extra_properties'->>'icon', 'map-pin')
    WHEN 'house'        THEN 'home'
    WHEN 'helipad'      THEN 'helicopter'
    WHEN 'location_off' THEN 'map-pin-off'
    WHEN 'person'       THEN 'user'
    WHEN 'default'      THEN 'map-pin'
    ELSE COALESCE(rf->'extra_properties'->>'icon', 'map-pin')
  END AS icon,
  CASE
    WHEN NULLIF(TRIM(rf->'extra_properties'->>'iconSize'), '') ~ '^\d+$'
      THEN (rf->'extra_properties'->>'iconSize')::integer
    ELSE 32
  END AS icon_size,
  '[]' AS active_daily_windows,
  '${DEST_HUB_ID}' AS origin_hub_id

FROM extracted
SQL
}

TRANSFORM_SQL=$(build_transform_sql)

DEST_COLUMNS="id, name, classification, description, shape, altitude_hae_meters, altitude_agl_meters, color, creation_time, fill, fill_opacity, show_label, dash_array, icon, icon_size, active_daily_windows, origin_hub_id"

# ----------------------------------------------------------
# Export transformed data from source to a temp CSV file
# ----------------------------------------------------------
TEMP_FILE=$(mktemp "/tmp/migrate_map_features_XXXXXXXX") || die "Failed to create temp file"
SQL_FILE=$(mktemp "/tmp/migrate_upsert_XXXXXXXX") || die "Failed to create temp file"
ZONES_YAML=$(mktemp "/tmp/migrate_zones_XXXXXXXX") || die "Failed to create temp file"
trap 'rm -f "$TEMP_FILE" "$SQL_FILE" "$ZONES_YAML"' EXIT

log "Exporting transformed data from source..."
echo "COPY (${TRANSFORM_SQL}) TO STDOUT WITH (FORMAT csv, HEADER false, NULL '\N');" \
  | source_psql -q > "$TEMP_FILE"

ANNOTATION_COUNT=$(wc -l < "$TEMP_FILE" | tr -d ' ')
log "Exported ${ANNOTATION_COUNT} annotation rows from source"

# ----------------------------------------------------------
# Export alarm zones (magos pod)
# ----------------------------------------------------------
log "Reading zones.yaml from ${MAGOS_POD} (container: ${MAGOS_CONTAINER})..."

kubectl exec \
  --context "$SOURCE_CTX" \
  -n "$SOURCE_NS" \
  -c "$MAGOS_CONTAINER" \
  "$MAGOS_POD" -- \
  cat /opt/mass/data/config/zones.yaml > "$ZONES_YAML" 2>/dev/null \
  || { log "WARNING: Could not read zones.yaml from ${MAGOS_POD}. Skipping alarm zone migration."; ZONES_YAML=""; }

ALARM_COUNT=0
if [[ -n "$ZONES_YAML" && -s "$ZONES_YAML" ]]; then
  log "Generating alarm zone rows..."
  ALARM_ROWS=$(python3 - "$ZONES_YAML" "$DEST_HUB_ID" <<'PYEOF'
import sys
import csv
import io
import struct
import uuid
from datetime import datetime, timezone

import yaml

NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

def coords_to_ewkb_hex(coords):
    pts = [{"lat": c["lat"], "lng": c["lng"]} for c in coords]
    if pts[0]["lat"] != pts[-1]["lat"] or pts[0]["lng"] != pts[-1]["lng"]:
        pts.append(pts[0])
    n = len(pts)
    buf = struct.pack('<B', 1)            # little-endian
    buf += struct.pack('<I', 0x20000003)  # polygon + SRID flag
    buf += struct.pack('<I', 4326)        # SRID
    buf += struct.pack('<I', 1)           # 1 ring
    buf += struct.pack('<I', n)           # point count
    for p in pts:
        buf += struct.pack('<d', p['lng'])
        buf += struct.pack('<d', p['lat'])
    return buf.hex().upper()

zones_path = sys.argv[1]
origin_hub_id = sys.argv[2]

with open(zones_path) as f:
    zones_data = yaml.safe_load(f)

zones = zones_data if isinstance(zones_data, list) else zones_data.get("data", zones_data.get("zones", zones_data.get("items", [])))

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")

out = io.StringIO()
writer = csv.writer(out, lineterminator='\n')

for z in zones:
    if z.get("type") != "alarmZone":
        continue
    coords = (z.get("geometry") or {}).get("coordinates", [])
    if len(coords) < 3:
        continue

    zone_uid = z.get("uid", "")
    det_id = str(uuid.uuid5(NAMESPACE, zone_uid))
    shape = coords_to_ewkb_hex(coords)
    name = z.get("name", "")
    show_label = "t" if name else "f"

    writer.writerow([
        det_id,           # id
        name,             # name
        "Alarm",          # classification
        "",               # description
        shape,            # shape (EWKB hex)
        r"\N",            # altitude_hae_meters (NULL)
        r"\N",            # altitude_agl_meters (NULL)
        "#FA5252",        # color (red)
        now,              # creation_time
        "t",              # fill
        "0.2",            # fill_opacity
        show_label,       # show_label
        "solid_2",        # dash_array
        "map-pin",        # icon
        "32",             # icon_size
        "[]",             # active_daily_windows
        origin_hub_id,    # origin_hub_id
    ])

print(out.getvalue(), end="")
PYEOF
  )

  if [[ -n "$ALARM_ROWS" ]]; then
    # Ensure existing content ends with a newline before appending
    if [[ -s "$TEMP_FILE" ]] && [[ "$(tail -c1 "$TEMP_FILE" | wc -l)" -eq 0 ]]; then
      echo >> "$TEMP_FILE"
    fi
    echo -n "$ALARM_ROWS" >> "$TEMP_FILE"
    ALARM_COUNT=$(echo -n "$ALARM_ROWS" | wc -l | tr -d ' ')
    [[ "${ALARM_ROWS: -1}" != $'\n' ]] && ALARM_COUNT=$((ALARM_COUNT + 1))
    log "Generated ${ALARM_COUNT} alarm zone rows"
  else
    log "No matching alarm zones found"
  fi
else
  log "Skipping alarm zone migration (YAML files not available)"
fi

# Ensure the file ends with a newline so the COPY terminator (\.} lands on its own line
if [[ -s "$TEMP_FILE" ]] && [[ "$(tail -c1 "$TEMP_FILE" | wc -l)" -eq 0 ]]; then
  echo >> "$TEMP_FILE"
fi

ROW_COUNT=$(wc -l < "$TEMP_FILE" | tr -d ' ')
log "Total rows to migrate: ${ROW_COUNT} (${ANNOTATION_COUNT} annotations + ${ALARM_COUNT} alarm zones)"

if [[ "$ROW_COUNT" -eq 0 ]]; then
  log "No rows to migrate. Exiting."
  exit 0
fi

# ----------------------------------------------------------
# Dry run: display preview and exit
# ----------------------------------------------------------
if [[ "$DRY_RUN" == "true" ]]; then
  log "=== DRY RUN MODE ==="
  echo ""
  log "Transformation SQL:"
  echo "----"
  echo "$TRANSFORM_SQL"
  echo "----"
  echo ""
  log "Preview of exported CSV (first 5 rows):"
  echo "----"
  head -5 "$TEMP_FILE"
  echo "----"
  echo ""
  log "Total rows that would be inserted: ${ROW_COUNT}"
  echo ""
  log "Checking for unmapped color values in source..."
  UNMAPPED_COLORS=$(cat <<UNMAPPED_SQL | source_psql -At
SELECT DISTINCT regexp_replace(TRIM(BOTH '"' FROM raw_feature::text), '\\\\(.)', '\\1', 'g')::jsonb->'extra_properties'->>'color' AS src_color
FROM ${TABLE_NAME}
WHERE regexp_replace(TRIM(BOTH '"' FROM raw_feature::text), '\\\\(.)', '\\1', 'g')::jsonb->'extra_properties'->>'color' IS NOT NULL
  AND regexp_replace(TRIM(BOTH '"' FROM raw_feature::text), '\\\\(.)', '\\1', 'g')::jsonb->'extra_properties'->>'color' NOT IN (
    'var(--mantine-color-blue-text)',
    'var(--mantine-color-blue-filled)',
    'var(--mantine-color-red-text)',
    'var(--mantine-color-red-filled)',
    'var(--mantine-color-yellow-text)',
    'var(--mantine-color-orange-text)',
    'var(--mantine-color-orange-filled)',
    'var(--mantine-color-lime-filled)',
    'var(--mantine-color-teal-text)',
    'var(--mantine-color-teal-filled)',
    'var(--mantine-color-cyan-text)',
    'var(--mantine-color-gray-filled)'
  )
UNMAPPED_SQL
  )
  if [[ -n "$UNMAPPED_COLORS" ]]; then
    log "WARNING: The following color values will fall back to #1864AB (blue):"
    echo "$UNMAPPED_COLORS"
  else
    log "All color values have known mappings"
  fi
  echo ""
  DRY_RUN_CSV="/tmp/migrate_map_features_dry_run.csv"
  cp "$TEMP_FILE" "$DRY_RUN_CSV"
  log "Exported CSV saved to: ${DRY_RUN_CSV}"
  exit 0
fi

# ----------------------------------------------------------
# Import into destination (upsert via staging table)
# ----------------------------------------------------------
log "Upserting ${ROW_COUNT} rows into destination ${TABLE_NAME}..."

{
  echo "BEGIN;"
  echo "CREATE TEMP TABLE _import_staging (LIKE ${TABLE_NAME}) ON COMMIT DROP;"
  echo "COPY _import_staging (${DEST_COLUMNS}) FROM STDIN WITH (FORMAT csv, NULL '\N');"
  cat "$TEMP_FILE"
  echo "\."
  cat <<UPSERT_SQL
INSERT INTO ${TABLE_NAME} (${DEST_COLUMNS})
SELECT ${DEST_COLUMNS} FROM _import_staging
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name,
  classification = EXCLUDED.classification,
  description = EXCLUDED.description,
  shape = EXCLUDED.shape,
  altitude_hae_meters = EXCLUDED.altitude_hae_meters,
  altitude_agl_meters = EXCLUDED.altitude_agl_meters,
  color = EXCLUDED.color,
  creation_time = EXCLUDED.creation_time,
  fill = EXCLUDED.fill,
  fill_opacity = EXCLUDED.fill_opacity,
  show_label = EXCLUDED.show_label,
  dash_array = EXCLUDED.dash_array,
  icon = EXCLUDED.icon,
  icon_size = EXCLUDED.icon_size,
  active_daily_windows = EXCLUDED.active_daily_windows;
UPSERT_SQL
  echo "COMMIT;"
} > "$SQL_FILE"

dest_psql -v ON_ERROR_STOP=1 -q < "$SQL_FILE" \
  || die "Upsert into ${TABLE_NAME} failed; transaction rolled back. No rows were migrated."

# Verify row count on destination
DEST_COUNT=$(dest_psql -Atc "SELECT COUNT(*) FROM ${TABLE_NAME};")
log "Migration complete. ${ROW_COUNT} rows inserted."
log "Destination ${TABLE_NAME} now has ${DEST_COUNT} total rows."

# ----------------------------------------------------------
# Restart hub-server pod to reload in-memory feature cache
# ----------------------------------------------------------
if [[ "$NO_RESTART" == "true" ]]; then
  log "Skipping hub-server restart (--no-restart). Remember to restart it manually to pick up new features."
else
  log "Restarting hub-server pod on destination to pick up new features..."
  HUB_POD=$(kubectl get pods \
    --context "$DEST_CTX" \
    -n "$DEST_NS" \
    -l app=hub-server \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

  if [[ -n "$HUB_POD" ]]; then
    kubectl delete pod \
      --context "$DEST_CTX" \
      -n "$DEST_NS" \
      "$HUB_POD"
    log "Pod ${HUB_POD} deleted. Waiting for new pod to be ready..."
    kubectl wait pod \
      --context "$DEST_CTX" \
      -n "$DEST_NS" \
      -l app=hub-server \
      --for=condition=Ready \
      --timeout=120s
    log "hub-server pod is ready."
  else
    log "WARNING: Could not find hub-server pod. You may need to restart it manually."
  fi
fi