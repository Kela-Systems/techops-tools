"""Hub server operations — find and restart the pod."""

import subprocess


def restart_hub_server(context: str, namespace: str) -> tuple[bool, str]:
    """Find and restart the hub-server pod. Returns (success, message)."""
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods",
                "-n", namespace,
                "--context", context,
                "-o", "jsonpath={.items[*].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        pods = result.stdout.strip().split()
        hub_pod = next((p for p in pods if "hub-server" in p), None)
        if not hub_pod:
            return False, "Could not find hub-server pod"

        subprocess.run(
            [
                "kubectl", "delete", "pod", hub_pod,
                "-n", namespace,
                "--context", context,
            ],
            timeout=30,
        )
        return True, f"Pod {hub_pod} restarted successfully"
    except subprocess.TimeoutExpired:
        return False, "kubectl command timed out"
    except Exception as e:
        return False, f"Error restarting hub-server: {e}"
