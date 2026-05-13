#!/usr/bin/env bash
# install_unifi_mcp.sh — installs unifi-network-mcp and wires it into Claude Desktop / Cowork
# Run once from your Mac terminal: bash scripts/install_unifi_mcp.sh
set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. Check Node ────────────────────────────────────────────────────────────
command -v node >/dev/null 2>&1 || die "Node.js not found. Install via: brew install node"
command -v npm  >/dev/null 2>&1 || die "npm not found. Install via: brew install node"
NODE_VER=$(node --version)
info "Node $NODE_VER found"

# ── 2. Install package globally ──────────────────────────────────────────────
info "Installing unifi-network-mcp globally..."
npm install -g unifi-network-mcp@latest 2>&1 | tail -3
MCP_BIN=$(npm root -g)/unifi-network-mcp/dist/mcp.js
[ -f "$MCP_BIN" ] || die "Could not find mcp.js after install at $MCP_BIN"
ok "Installed at $MCP_BIN"

# ── 3. Collect credentials ───────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  You need a UniFi Network Integration API key."
echo "  Steps:"
echo "    1. Open https://192.168.1.1/settings/integrations"
echo "    2. Click 'Add Application' or 'Create Integration'"
echo "    3. Give it any name (e.g. Claude-MCP)"
echo "    4. Copy the API key shown"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
read -rsp "Paste your UniFi API key (input hidden): " UNIFI_API_KEY
echo ""
[ -z "$UNIFI_API_KEY" ] && die "API key cannot be empty"

# ── 4. Update Claude Desktop config ─────────────────────────────────────────
CONFIG_FILE="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
BACKUP_FILE="${CONFIG_FILE}.backup.$(date +%Y%m%d_%H%M%S)"

if [ -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_FILE" "$BACKUP_FILE"
    info "Backed up existing config to $BACKUP_FILE"
    # Merge using Python
    python3 - <<PYEOF
import json, sys, os

config_path = os.path.expanduser("$CONFIG_FILE")
mcp_bin     = "$MCP_BIN"
api_key     = "$UNIFI_API_KEY"

with open(config_path) as f:
    cfg = json.load(f)

cfg.setdefault("mcpServers", {})
cfg["mcpServers"]["unifi-network"] = {
    "command": "node",
    "args": [mcp_bin],
    "env": {
        "UNIFI_TARGETS": json.dumps([{
            "id": "home",
            "base_url": "https://192.168.1.1",
            "controller_type": "unifi_os",
            "default_site": "default",
            "auth": {"apiKey": api_key, "headerName": "X-API-KEY"},
            "verify_ssl": False,
            "timeout_ms": 20000,
            "rate_limit_per_sec": 5
        }])
    }
}

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)

print("Config updated.")
PYEOF
else
    info "No existing config found — creating new one"
    python3 - <<PYEOF
import json, os

config_path = os.path.expanduser("$CONFIG_FILE")
os.makedirs(os.path.dirname(config_path), exist_ok=True)
api_key = "$UNIFI_API_KEY"
mcp_bin = "$MCP_BIN"

cfg = {
    "mcpServers": {
        "unifi-network": {
            "command": "node",
            "args": [mcp_bin],
            "env": {
                "UNIFI_TARGETS": json.dumps([{
                    "id": "home",
                    "base_url": "https://192.168.1.1",
                    "controller_type": "unifi_os",
                    "default_site": "default",
                    "auth": {"apiKey": api_key, "headerName": "X-API-KEY"},
                    "verify_ssl": False,
                    "timeout_ms": 20000,
                    "rate_limit_per_sec": 5
                }])
            }
        }
    }
}

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)

print("Config created.")
PYEOF
fi

ok "Claude Desktop config updated"

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}  UniFi MCP installed successfully.${NC}"
echo ""
echo "  Next step: RESTART the Claude / Cowork desktop app."
echo "  The 'unifi-network' MCP will appear automatically."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
