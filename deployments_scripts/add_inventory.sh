#!/bin/bash
# add_inventory.sh - Create an Ansible inventory file for a new on-prem customer
#
# Generates a YAML inventory in the kela repo, commits it on a new branch,
# pushes, and opens a GitHub PR.
#
# Usage:
#   ./add_inventory.sh --name <customer-name> --tags <tailscale-tags> [options]
#   ./add_inventory.sh   (interactive mode)
#
# Examples:
#   ./add_inventory.sh --name acme-corp --tags "tag:configuration-management,tag:acme"
#   ./add_inventory.sh --name acme-corp --tags "tag:configuration-management,tag:acme" --dry-run

set -euo pipefail

KELA_REPO="${KELA_REPO:-$HOME/dev/kela}"
INVENTORY_REL="${INVENTORY_REL:-deployment/ansible/inventory/onprem/prod}"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

CUSTOMER_NAME=""
TAILSCALE_ACL_TAGS=""
ANSIBLE_USER="kela"
DISABLED_SERVICES="synthetic-device"
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage:
  ./add_inventory.sh --name <customer-name> --tags <tailscale-tags> [options]
  ./add_inventory.sh   (interactive mode — prompts for all values)

Required:
  --name <name>                 Customer name (used as filename, hostname, etc.)
  --tags <tags>                 Tailscale ACL tags (comma-separated)

Options:
  --user <user>                 Ansible SSH user (default: kela)
  --disabled-services <s1,s2>   Comma-separated list of disabled services
  --dry-run                     Preview generated file and git commands without writing
  -h, --help                    Show this help
USAGE
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --name)           CUSTOMER_NAME="$2"; shift 2 ;;
      --tags)           TAILSCALE_ACL_TAGS="$2"; shift 2 ;;
      --user)           ANSIBLE_USER="$2"; shift 2 ;;
      --disabled-services) DISABLED_SERVICES="$2"; shift 2 ;;
      --dry-run)        DRY_RUN=true; shift ;;
      -h|--help)        usage ;;
      *)                die "Unknown option: $1" ;;
    esac
  done
}

prompt_if_empty() {
  local varname="$1" prompt="$2" default="${3:-}"
  local current_val="${!varname}"

  if [[ -z "$current_val" ]]; then
    if [[ -n "$default" ]]; then
      read -rp "$prompt [$default]: " current_val
      current_val="${current_val:-$default}"
    else
      read -rp "$prompt: " current_val
    fi
    printf -v "$varname" '%s' "$current_val"
  fi
}

interactive_prompts() {
  echo "=== Add On-Prem Inventory ==="
  echo ""
  prompt_if_empty CUSTOMER_NAME "Customer name (e.g. acme-corp)"
  prompt_if_empty TAILSCALE_ACL_TAGS "Tailscale ACL tags (e.g. tag:configuration-management,tag:acme)"
  prompt_if_empty ANSIBLE_USER "Ansible SSH user" "kela"

  if [[ -z "$DISABLED_SERVICES" ]]; then
    read -rp "Disabled services (comma-separated, or leave empty): " DISABLED_SERVICES
  else
    read -rp "Disabled services (comma-separated) [$DISABLED_SERVICES]: " input
    DISABLED_SERVICES="${input:-$DISABLED_SERVICES}"
  fi
}

validate() {
  [[ -n "$CUSTOMER_NAME" ]]      || die "customer_name is required"
  [[ -n "$TAILSCALE_ACL_TAGS" ]] || die "tailscale_acl_tags is required"
  [[ -d "$KELA_REPO/.git" ]]     || die "Kela repo not found at $KELA_REPO"
  command -v gh &>/dev/null       || die "'gh' CLI is required (https://cli.github.com)"
  command -v git &>/dev/null      || die "'git' is required"
}

build_yaml() {
  local yaml=""
  yaml+="---\n"
  yaml+="# ${CUSTOMER_NAME} - On-prem deployment\n"
  yaml+="\n"
  yaml+="all:\n"
  yaml+="  vars:\n"
  yaml+="    aws_credentials_secret_id: \"/iam-users/${CUSTOMER_NAME}\"\n"
  yaml+="\n"
  yaml+="    tailscale_acl_tags: \"${TAILSCALE_ACL_TAGS}\"\n"

  if [[ -n "$DISABLED_SERVICES" ]]; then
    yaml+="\n"
    yaml+="    disabled_services:\n"
    IFS=',' read -ra services <<< "$DISABLED_SERVICES"
    for svc in "${services[@]}"; do
      svc="${svc#"${svc%%[![:space:]]*}"}"
      svc="${svc%"${svc##*[![:space:]]}"}"
      yaml+="      - ${svc}\n"
    done
  fi

  yaml+="\n"
  yaml+="  hosts:\n"
  yaml+="    ${CUSTOMER_NAME}:\n"
  yaml+="      ansible_user: ${ANSIBLE_USER}\n"

  printf '%b' "$yaml"
}

main() {
  parse_args "$@"

  # If required fields are still empty, run interactive prompts
  if [[ -z "$CUSTOMER_NAME" || -z "$TAILSCALE_ACL_TAGS" ]]; then
    interactive_prompts
  fi

  validate

  local inventory_dir="${KELA_REPO}/${INVENTORY_REL}"
  local target_file="${inventory_dir}/${CUSTOMER_NAME}.yml"

  if [[ -f "$target_file" ]] && [[ "$DRY_RUN" == false ]]; then
    read -rp "File ${target_file} already exists. Overwrite? [y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || die "Aborted"
  fi

  local yaml
  yaml="$(build_yaml)"

  if [[ "$DRY_RUN" == true ]]; then
    echo ""
    log "[DRY RUN] Would write to: ${target_file}"
    echo "---"
    echo "$yaml"
    echo "---"
    log "[DRY RUN] Branch: "$(gh api user --jq '.login')"/techops-add-${CUSTOMER_NAME}"
    log "[DRY RUN] Commit: ${CUSTOMER_NAME}: add onprem inventory file"
    log "[DRY RUN] Would push and open PR"
    exit 0
  fi

  # --- Git: prepare branch ---
  log "Preparing git branch in ${KELA_REPO}..."
  cd "$KELA_REPO"

  git checkout main
  git pull origin main

  local gh_user
  gh_user="$(gh api user --jq '.login')"
  local branch_name="${gh_user}/techops-1-add-${CUSTOMER_NAME}"

  log "Creating branch: ${branch_name}"
  if git show-ref --verify --quiet "refs/heads/${branch_name}"; then
    git branch -D "$branch_name"
  fi
  git checkout -b "$branch_name"

  # --- Generate inventory file ---
  log "Writing inventory file: ${target_file}"
  echo "$yaml" > "$target_file"

  # --- Git: commit, push, PR ---
  local commit_msg="${CUSTOMER_NAME}: add onprem inventory file"
  git add "$target_file"
  git commit -m "$commit_msg"

  log "Pushing branch to origin..."
  git push -u --force-with-lease origin "$branch_name"

  log "Creating pull request..."
  local pr_url
  pr_url="$(gh pr create \
    --title "$commit_msg" \
    --body "Add on-prem inventory file for **${CUSTOMER_NAME}**.

### Inventory details
- **Customer**: \`${CUSTOMER_NAME}\`
- **Tailscale tags**: \`${TAILSCALE_ACL_TAGS}\`
- **Ansible user**: \`${ANSIBLE_USER}\`
- **File**: \`${INVENTORY_REL}/${CUSTOMER_NAME}.yml\`")"

  echo ""
  log "Done!"
  log "PR: ${pr_url}"
  log "File: ${target_file}"
}

main "$@"
