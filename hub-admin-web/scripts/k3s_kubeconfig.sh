#!/bin/bash

set -e

# Simple script to update kubectl context for k3s cluster
# No dependencies, just pure bash

usage() {
    echo "Usage: $0 --user <user> --host <host> [--key-file <key_file>] [--context-name <name>]"
    echo ""
    echo "  -h, --help              Show this help message"
    echo "  -u, --user <user>       SSH username (required)"
    echo "  -H, --host <host>       SSH hostname or IP (required)"
    echo "  -k, --key-file <file>   Path to SSH private key (optional)"
    echo "  -c, --context-name      Kubectl context name (optional, defaults to hostname)"
    echo ""
    echo "Example:"
    echo "  $0 --user kela --host 192.168.1.100"
    echo "  $0 --user kela --host my-server.example.com --key-file ~/.ssh/id_rsa"
}

USER=""
HOST=""
KEY_FILE=""
CONTEXT_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -u|--user)
            USER="$2"
            shift 2
            ;;
        -H|--host)
            HOST="$2"
            shift 2
            ;;
        -k|--key-file)
            KEY_FILE="$2"
            shift 2
            ;;
        -c|--context-name)
            CONTEXT_NAME="$2"
            shift 2
            ;;
        *)
            echo "Error: Unknown option $1"
            usage
            exit 1
            ;;
    esac
done

# Validate required parameters
if [ -z "$USER" ]; then
    echo "Error: --user is required"
    usage
    exit 1
fi

if [ -z "$HOST" ]; then
    echo "Error: --host is required"
    usage
    exit 1
fi

# Set context name to host if not provided
if [ -z "$CONTEXT_NAME" ]; then
    CONTEXT_NAME="$HOST"
fi

echo "=================================="
echo "Setting up k3s kubectl context"
echo "=================================="
echo "User: $USER"
echo "Host: $HOST"
echo "Context name: $CONTEXT_NAME"
echo ""

# Build SSH command
SSH_CMD="ssh"
if [ -n "$KEY_FILE" ]; then
    SSH_CMD="$SSH_CMD -i $KEY_FILE"
fi
SSH_CMD="$SSH_CMD -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes"
SSH_CMD="$SSH_CMD ${USER}@${HOST}"

echo "Testing SSH connection..."
if ! $SSH_CMD "echo 'SSH connection successful'" > /dev/null 2>&1; then
    echo "Error: Cannot connect to $HOST via SSH"
    exit 1
fi
echo "✓ SSH connection successful"

# Get the machine's actual hostname. It may differ from the SSH host
# (e.g. Tailscale hostname), and the k3s TLS certificate is only valid
# for the actual hostname.
REMOTE_HOSTNAME=$($SSH_CMD "hostname" 2>/dev/null | tr -d '[:space:]')
if [ -n "$REMOTE_HOSTNAME" ] && [ "$REMOTE_HOSTNAME" != "$HOST" ]; then
    echo "✓ Remote hostname: $REMOTE_HOSTNAME (differs from SSH host, will set tls-server-name)"
fi

# Check if k3s.yaml exists
echo ""
echo "Checking for k3s.yaml on remote host..."
if ! $SSH_CMD "sudo test -f /etc/rancher/k3s/k3s.yaml"; then
    echo "Error: /etc/rancher/k3s/k3s.yaml not found on remote host"
    echo "Is k3s installed?"
    exit 1
fi
echo "✓ k3s.yaml found"

# Get k3s.yaml content
echo ""
echo "Reading k3s configuration..."
TEMP_DIR=$(mktemp -d)
$SSH_CMD "sudo cat /etc/rancher/k3s/k3s.yaml" > "${TEMP_DIR}/k3s.yaml"
echo "✓ Configuration downloaded"

# Extract certificate data
echo ""
echo "Extracting certificates..."
CA_DATA=$(grep "certificate-authority-data:" "${TEMP_DIR}/k3s.yaml" | head -1 | awk '{print $2}')
CLIENT_CERT_DATA=$(grep "client-certificate-data:" "${TEMP_DIR}/k3s.yaml" | head -1 | awk '{print $2}')
CLIENT_KEY_DATA=$(grep "client-key-data:" "${TEMP_DIR}/k3s.yaml" | head -1 | awk '{print $2}')

if [ -z "$CA_DATA" ] || [ -z "$CLIENT_CERT_DATA" ] || [ -z "$CLIENT_KEY_DATA" ]; then
    echo "Error: Failed to extract certificate data from k3s.yaml"
    rm -rf "$TEMP_DIR"
    exit 1
fi
echo "✓ Certificates extracted"

# Clean up temp directory
rm -rf "$TEMP_DIR"

# Get server IP (use the host as-is, since k3s.yaml uses localhost)
SERVER_IP="$HOST"

# Create .kube directory if it doesn't exist
mkdir -p "$HOME/.kube"

# Backup existing config
KUBE_CONFIG="$HOME/.kube/config"
if [ -f "$KUBE_CONFIG" ]; then
    BACKUP_FILE="${KUBE_CONFIG}.backup-$(date +%Y%m%d-%H%M%S)"
    echo ""
    echo "Backing up existing config to: $BACKUP_FILE"
    cp "$KUBE_CONFIG" "$BACKUP_FILE"
fi

echo ""
echo "Configuring kubectl context..."

# Remove existing context/cluster/user if they exist
kubectl config delete-context "$CONTEXT_NAME" 2>/dev/null || true
kubectl config delete-cluster "$CONTEXT_NAME" 2>/dev/null || true
kubectl config delete-user "$CONTEXT_NAME" 2>/dev/null || true

# Set credentials using a workaround (kubectl doesn't support --client-certificate-data directly)
kubectl config set-credentials "$CONTEXT_NAME" \
  --client-certificate="/tmp/${CONTEXT_NAME}_TEMP_CERT" \
  --client-key="/tmp/${CONTEXT_NAME}_TEMP_KEY"

# Replace file paths with actual certificate data
sed -i.bak "s|client-certificate: /tmp/${CONTEXT_NAME}_TEMP_CERT|client-certificate-data: ${CLIENT_CERT_DATA}|" "$KUBE_CONFIG"
sed -i.bak "s|client-key: /tmp/${CONTEXT_NAME}_TEMP_KEY|client-key-data: ${CLIENT_KEY_DATA}|" "$KUBE_CONFIG"
rm -f "${KUBE_CONFIG}.bak"

# Set cluster with CA data.
# If the machine's actual hostname differs from the SSH host (e.g. Tailscale
# hostname), pass --tls-server-name so certificate verification uses the
# hostname the k3s certificate was issued for.
SET_CLUSTER_ARGS=(
  --server="https://${SERVER_IP}:6443"
  --certificate-authority="/tmp/${CONTEXT_NAME}_TEMP_CA"
)
if [ -n "$REMOTE_HOSTNAME" ] && [ "$REMOTE_HOSTNAME" != "$HOST" ]; then
    SET_CLUSTER_ARGS+=(--tls-server-name="$REMOTE_HOSTNAME")
fi
kubectl config set-cluster "$CONTEXT_NAME" "${SET_CLUSTER_ARGS[@]}"

# Replace CA file path with actual data
sed -i.bak "s|certificate-authority: /tmp/${CONTEXT_NAME}_TEMP_CA|certificate-authority-data: ${CA_DATA}|" "$KUBE_CONFIG"
rm -f "${KUBE_CONFIG}.bak"

# Create context
kubectl config set-context "$CONTEXT_NAME" \
  --cluster="$CONTEXT_NAME" \
  --user="$CONTEXT_NAME"

# Switch to the new context
kubectl config use-context "$CONTEXT_NAME"

echo "✓ Context configured"
echo ""
echo "Testing connection..."
if kubectl get nodes > /dev/null 2>&1; then
    echo "✓ Connection successful!"
    echo ""
    kubectl get nodes
else
    echo "⚠ Warning: Connection test failed"
    echo "This might be because:"
    echo "  - The k3s API server is not accessible from this machine"
    echo "  - Firewall rules are blocking port 6443"
    echo "  - The host IP is not correct"
    echo ""
    echo "Context has been configured, but you may need to:"
    echo "  - Set up port forwarding: ssh -L 6443:localhost:6443 ${USER}@${HOST}"
    echo "  - Or configure network access to the k3s API server"
fi

echo ""
echo "=================================="
echo "✓ Setup complete!"
echo "=================================="
echo "Context name: $CONTEXT_NAME"
echo "Current context: $(kubectl config current-context)"
echo ""
echo "To use this context:"
echo "  kubectl config use-context $CONTEXT_NAME"
echo ""

