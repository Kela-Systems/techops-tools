#!/bin/bash
# /usr/local/bin/alert.sh
#
# Posts a disk-usage alert to Slack. Requires SLACK_WEBHOOK_URL in the
# environment (set it via Monit's `env` block or /etc/default/fs_monitor).

set -euo pipefail

: "${SLACK_WEBHOOK_URL:?SLACK_WEBHOOK_URL must be set in the environment}"

HOST_NAME="${HOST_NAME:-$(hostname)}"
THRESHOLD="${THRESHOLD:-75%}"

curl -X POST -H 'Content-type: application/json' \
  --data "{\"text\":\"${HOST_NAME} disk usage exceeded ${THRESHOLD}\"}" \
  "${SLACK_WEBHOOK_URL}"
