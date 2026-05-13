# UniFi Network Monitor — Setup Guide

## What this does

Monitors your UniFi network continuously for one week, writes all data to InfluxDB, visualises it in Grafana, and then produces a plain-English diagnosis of exactly what is causing your Sonos dropouts and how to fix them.

## Project layout

```
Network health/
├── .env.template            ← Copy to .env and fill in your values
├── docker-compose.yml       ← InfluxDB + collector (run on your Docker server)
├── collector/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── unifi_collector.py   ← Polls UniFi every 5 min
├── grafana/
│   ├── provisioning/        ← Auto-provision data source + dashboards
│   └── dashboards/
│       ├── network-overview.json   ← Import into Grafana
│       └── sonos-focus.json        ← Import into Grafana
├── analysis/
│   └── diagnose.py          ← Run after 7 days to get your diagnosis
└── scripts/
    └── install_unifi_mcp.sh ← Wires UniFi MCP into Claude/Cowork
```

---

## Step 1 — Create your .env file

```bash
cp .env.template .env
```

Edit `.env` and fill in:

| Variable | What to put |
|---|---|
| `UNIFI_USER` | Your UniFi admin username |
| `UNIFI_PASS` | Your UniFi admin password |
| `INFLUXDB_PASSWORD` | Any strong password for InfluxDB admin |
| `INFLUXDB_TOKEN` | A long random string (used as the API token) |

Generate a random token if needed:
```bash
openssl rand -hex 32
```

---

## Step 2 — Deploy on your Docker server

Copy the project folder to your Docker server, then:

```bash
# From the project directory on your Docker server
docker compose up -d
```

This starts:
- **InfluxDB** on port 8086
- **unifi-collector** — begins polling your Dream Machine at 192.168.1.1 every 5 minutes

Verify it is running:
```bash
docker compose logs -f collector
```

You should see lines like:
```
── Poll at 10:30:00 UTC ──
  clients  → 14 WiFi client points written
  devices  → 6 AP/device points written
  summary  → 14 WiFi + 3 wired clients, 3 device records
```

---

## Step 3 — Add InfluxDB to your existing Grafana

In your existing Grafana instance:

1. Go to **Configuration → Data Sources → Add data source**
2. Select **InfluxDB**
3. Set:
   - Query language: **Flux**
   - URL: `http://<docker-server-ip>:8086`
   - Organisation: `home`
   - Default bucket: `unifi`
   - Token: the value of `INFLUXDB_TOKEN` from your `.env`
4. Click **Save & Test** — should show "datasource is working"

---

## Step 4 — Import the Grafana dashboards

1. Go to **Dashboards → Import**
2. Upload `grafana/dashboards/network-overview.json`
3. Select your InfluxDB data source when prompted
4. Repeat for `grafana/dashboards/sonos-focus.json`

The **Sonos & Kitchen WiFi** dashboard is the one to watch — it shows:
- Sonos RSSI and SNR over time
- Total client count overlaid (the guest-load correlation)
- AP channel utilisation
- Disconnect/roam events

---

## Step 5 — Install the UniFi MCP in Cowork (optional but recommended)

This lets you ask Claude questions like "which AP is the Sonos on right now?" without leaving Cowork.

Run from your Mac terminal:
```bash
bash scripts/install_unifi_mcp.sh
```

The script will:
1. Install `unifi-network-mcp` globally via npm
2. Ask for your UniFi API key (from Settings → Integrations on the Dream Machine)
3. Update your Claude Desktop config automatically
4. Prompt you to restart Cowork

---

## Step 6 — Wait 7 days

Leave the collector running. It will silently gather data in the background regardless of whether Cowork or Grafana is open.

Check in on the Grafana dashboards periodically — the Sonos Focus dashboard will start showing patterns within the first 24 hours.

---

## Step 7 — Run the diagnostic analyser

After 7 days (or sooner if you already see clear patterns):

```bash
# On your Docker server (or Mac with InfluxDB accessible)
cd analysis
pip install -r requirements.txt
export INFLUX_URL=http://<docker-server-ip>:8086
export INFLUX_TOKEN=<your token>
export INFLUX_ORG=home
export INFLUX_BUCKET=unifi
python diagnose.py
```

Or source your `.env` file directly:
```bash
export $(grep -v '^#' ../.env | xargs) && python diagnose.py
```

This produces a full written diagnosis covering:
1. Sonos signal quality (RSSI, SNR, satisfaction score)
2. Correlation between guest count and dropout frequency
3. Channel congestion by AP and radio band
4. Roaming and disconnect event patterns
5. AP load distribution
6. Prioritised fix list with exact UniFi settings to change

---

## Troubleshooting

**Collector fails to authenticate**
- Verify `UNIFI_USER` and `UNIFI_PASS` are correct
- The account must have admin access to the site
- Check `docker compose logs collector`

**Collector can't reach 192.168.1.1**
- The collector uses `network_mode: host` — ensure your Docker server is on the same network as the Dream Machine
- If Docker server is on a different VLAN, update `UNIFI_HOST` with the correct routed IP

**InfluxDB 'bucket does not exist' error**
- The collector auto-creates the bucket on first run
- If it fails: `docker compose exec influxdb influx bucket create -n unifi -o home`

**Grafana shows 'no data'**
- Allow 5–10 minutes for the first poll to complete
- Check the time range selector in Grafana — set to 'Last 1 hour' initially
- Verify the data source token matches `INFLUXDB_TOKEN`

**Sonos not identified (is_sonos = false)**
- The collector identifies Sonos devices by hostname containing "sonos" or OUI
- In UniFi, set a fixed alias for each Sonos speaker containing "Sonos" in the name
- Alternatively, check what hostname appears in the network-overview dashboard and filter manually in Grafana
