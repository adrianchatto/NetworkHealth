#!/usr/bin/env python3
"""
UniFi Network Monitor
Polls your UniFi Dream Machine every POLL_INTERVAL_SECONDS and writes
time-series data to InfluxDB v2. Tracks all WiFi clients (with Sonos
devices highlighted), AP radio stats, and network events.

Config is entirely via environment variables — see .env.template.
"""

import os
import sys
import time
import logging
import requests
import urllib3
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("unifi-collector")

# ── Config from environment ───────────────────────────────────────────────────
UNIFI_HOST        = os.environ.get("UNIFI_HOST", "https://192.168.1.1").rstrip("/")
UNIFI_USER        = os.environ.get("UNIFI_USER", "")
UNIFI_PASS        = os.environ.get("UNIFI_PASS", "")
UNIFI_SITE        = os.environ.get("UNIFI_SITE", "default")
UNIFI_VERIFY_SSL  = os.environ.get("UNIFI_VERIFY_SSL", "false").lower() == "true"

INFLUX_URL        = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN      = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG        = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET     = os.environ.get("INFLUX_BUCKET", "unifi")

POLL_INTERVAL     = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))  # 5 min


# ── UniFi API client ──────────────────────────────────────────────────────────
class UniFiClient:
    """Thin wrapper around the UniFi OS / Network Application REST API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = UNIFI_VERIFY_SSL
        self._auth_ok = False

    def _auth(self):
        resp = self.session.post(
            f"{UNIFI_HOST}/api/auth/login",
            json={"username": UNIFI_USER, "password": UNIFI_PASS},
            timeout=10,
        )
        resp.raise_for_status()
        # Persist CSRF token for write-safe idempotent requests
        token = (
            resp.json().get("data", {}).get("csrfToken")
            or resp.headers.get("X-CSRF-Token")
        )
        if token:
            self.session.headers["X-CSRF-Token"] = token
        self._auth_ok = True
        log.info("Authenticated with UniFi controller at %s", UNIFI_HOST)

    def _get(self, path: str) -> list:
        if not self._auth_ok:
            self._auth()
        url = f"{UNIFI_HOST}/proxy/network/api/s/{UNIFI_SITE}/{path}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 401:
                log.warning("Session expired — re-authenticating")
                self._auth_ok = False
                self._auth()
                r = self.session.get(url, timeout=15)
            r.raise_for_status()
            body = r.json()
            return body.get("data", []) if isinstance(body, dict) else []
        except Exception as exc:
            log.error("GET %s failed: %s", path, exc)
            return []

    def clients(self)          -> list: return self._get("stat/sta")
    def devices(self)          -> list: return self._get("stat/device")
    def events(self, n=500)    -> list: return self._get(f"stat/event?_limit={n}&_sort=-time")
    def health(self)           -> list: return self._get("stat/health")


# ── InfluxDB write helpers ────────────────────────────────────────────────────
def _flt(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def write_clients(write_api, clients: list, now: int):
    points = []
    for c in clients:
        if c.get("is_wired"):
            continue
        mac      = c.get("mac", "unknown")
        hostname = c.get("hostname") or c.get("name") or mac
        ap_mac   = c.get("ap_mac", "unknown")
        essid    = c.get("essid", "unknown")
        channel  = str(c.get("channel", "?"))
        radio    = c.get("radio", "?")   # ng=2.4 GHz  na=5 GHz  6e=6 GHz
        # Flag Sonos devices for easy filtering in Grafana
        is_sonos = "sonos" in hostname.lower() or "sonos" in str(c.get("oui", "")).lower()

        p = (
            Point("wifi_client")
            .tag("mac", mac)
            .tag("hostname", hostname)
            .tag("ap_mac", ap_mac)
            .tag("essid", essid)
            .tag("channel", channel)
            .tag("radio", radio)
            .tag("is_sonos", str(is_sonos).lower())
            .time(now, WritePrecision.SECONDS)
        )

        for field, key in [
            ("rssi",         "rssi"),
            ("signal",       "signal"),
            ("noise",        "noise"),
            ("tx_rate",      "tx_rate"),
            ("rx_rate",      "rx_rate"),
            ("satisfaction", "satisfaction"),
            ("tx_retries",   "tx_retries"),
            ("tx_bytes",     "tx_bytes"),
            ("rx_bytes",     "rx_bytes"),
            ("uptime",       "uptime"),
        ]:
            v = _flt(c.get(key))
            if v is not None:
                p = p.field(field, v)

        # Derived SNR
        sig = _flt(c.get("signal"))
        nse = _flt(c.get("noise"))
        if sig is not None and nse is not None:
            p = p.field("snr", sig - nse)

        points.append(p)

    if points:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("  clients  → %d WiFi client points written", len(points))


def write_devices(write_api, devices: list, now: int):
    points = []
    for d in devices:
        if d.get("type") not in ("uap", "udm", "udm-pro", "usg", "usw"):
            continue
        mac   = d.get("mac", "unknown")
        name  = d.get("name", mac)
        model = d.get("model", "unknown")

        # Per-radio stats (channel utilisation, retries, client count)
        for rs in d.get("radio_table_stats", []):
            radio   = rs.get("name", "?")
            channel = str(rs.get("channel", "?"))
            p = (
                Point("ap_radio")
                .tag("ap_mac", mac)
                .tag("ap_name", name)
                .tag("model", model)
                .tag("radio", radio)
                .tag("channel", channel)
                .time(now, WritePrecision.SECONDS)
            )
            for field, key in [
                ("channel_utilization",       "cu_total"),
                ("channel_utilization_self",  "cu_self"),
                ("tx_retries",                "tx_retries"),
                ("num_sta",                   "num_sta"),
                ("guest_num_sta",             "guest_num_sta"),
                ("tx_bytes",                  "tx_bytes"),
                ("rx_bytes",                  "rx_bytes"),
            ]:
                v = _flt(rs.get(key))
                if v is not None:
                    p = p.field(field, v)
            points.append(p)

        # Device-level health
        p = (
            Point("ap_device")
            .tag("ap_mac", mac)
            .tag("ap_name", name)
            .tag("model", model)
            .time(now, WritePrecision.SECONDS)
        )
        for field, key in [
            ("satisfaction", "satisfaction"),
            ("uptime",       "uptime"),
            ("num_sta",      "num_sta"),
        ]:
            v = _flt(d.get(key))
            if v is not None:
                p = p.field(field, v)
        ss = d.get("sys_stats", {})
        if ss.get("cpu"):
            p = p.field("cpu_pct", _flt(ss["cpu"]))
        if ss.get("mem"):
            p = p.field("mem_pct", _flt(ss["mem"]))
        points.append(p)

    if points:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("  devices  → %d AP/device points written", len(points))


def write_events(write_api, events: list, last_ts: int) -> int:
    """Write events newer than last_ts; returns the latest event timestamp seen."""
    points = []
    newest = last_ts

    for e in events:
        raw_ts = e.get("datetime") or e.get("time")
        if raw_ts is None:
            continue
        if isinstance(raw_ts, (int, float)):
            ts = int(raw_ts)
        else:
            try:
                ts = int(
                    datetime.fromisoformat(
                        str(raw_ts).replace("Z", "+00:00")
                    ).timestamp()
                )
            except Exception:
                continue

        if ts <= last_ts:
            continue

        newest = max(newest, ts)
        p = (
            Point("network_event")
            .tag("event_key",    e.get("key", "unknown"))
            .tag("ap_mac",       e.get("ap", "unknown"))
            .tag("client_mac",   e.get("user", "unknown"))
            .tag("radio",        str(e.get("radio", "?")))
            .field("message",    str(e.get("msg", e.get("key", ""))))
            .field("channel",    float(e.get("channel", 0) or 0))
            .time(ts, WritePrecision.SECONDS)
        )
        points.append(p)

    if points:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("  events   → %d new network events written", len(points))

    return newest


def write_network_summary(write_api, clients: list, devices: list, now: int):
    """Write a per-poll summary point — total clients, APs online, etc."""
    wifi_clients  = [c for c in clients if not c.get("is_wired")]
    wired_clients = [c for c in clients if c.get("is_wired")]
    aps_online    = len([d for d in devices if d.get("type") == "uap" and d.get("state") == 1])

    p = (
        Point("network_summary")
        .field("wifi_clients",  float(len(wifi_clients)))
        .field("wired_clients", float(len(wired_clients)))
        .field("aps_online",    float(aps_online))
        .time(now, WritePrecision.SECONDS)
    )
    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p])


# ── Bucket bootstrap ──────────────────────────────────────────────────────────
def ensure_bucket(influx: InfluxDBClient):
    try:
        api = influx.buckets_api()
        existing = {b.name for b in api.find_buckets().buckets}
        if INFLUX_BUCKET not in existing:
            api.create_bucket(bucket_name=INFLUX_BUCKET, org=INFLUX_ORG)
            log.info("Created InfluxDB bucket '%s'", INFLUX_BUCKET)
        else:
            log.info("InfluxDB bucket '%s' already exists", INFLUX_BUCKET)
    except Exception as exc:
        log.warning("Could not verify/create bucket: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("═══════════════════════════════════════")
    log.info("  UniFi collector starting")
    log.info("  Host     : %s", UNIFI_HOST)
    log.info("  Site     : %s", UNIFI_SITE)
    log.info("  InfluxDB : %s  bucket=%s  org=%s", INFLUX_URL, INFLUX_BUCKET, INFLUX_ORG)
    log.info("  Interval : %ds", POLL_INTERVAL)
    log.info("═══════════════════════════════════════")

    if not UNIFI_USER or not UNIFI_PASS:
        log.error("UNIFI_USER and UNIFI_PASS must be set. Exiting.")
        sys.exit(1)
    if not INFLUX_TOKEN:
        log.error("INFLUX_TOKEN must be set. Exiting.")
        sys.exit(1)

    unifi  = UniFiClient()
    influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write  = influx.write_api(write_options=SYNCHRONOUS)

    ensure_bucket(influx)

    last_event_ts = int(time.time()) - POLL_INTERVAL

    while True:
        try:
            now = int(time.time())
            log.info("── Poll at %s ──", datetime.utcfromtimestamp(now).strftime("%H:%M:%S UTC"))

            clients  = unifi.clients()
            devices  = unifi.devices()
            events   = unifi.events(n=500)

            write_clients(write, clients, now)
            write_devices(write, devices, now)
            last_event_ts = write_events(write, events, last_event_ts)
            write_network_summary(write, clients, devices, now)

            log.info("  summary  → %d WiFi + %d wired clients, %d device records",
                     len([c for c in clients if not c.get("is_wired")]),
                     len([c for c in clients if c.get("is_wired")]),
                     len(devices))

        except KeyboardInterrupt:
            log.info("Shutdown requested.")
            break
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
