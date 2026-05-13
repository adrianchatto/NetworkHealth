#!/usr/bin/env python3
"""
UniFi Network Diagnostic Analyser
Queries the last 7 days of InfluxDB data and produces a plain-English
diagnosis of what is wrong with your network and exactly how to fix it.

Usage:
    python diagnose.py

Environment variables (same .env as docker-compose):
    INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
"""

import os
import sys
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient

# ── Config ────────────────────────────────────────────────────────────────────
INFLUX_URL    = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG    = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "unifi")
RANGE         = os.environ.get("ANALYSIS_RANGE", "-7d")

SEPARATOR = "─" * 72


def section(title: str):
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def bullet(text: str, indent: int = 2):
    print(" " * indent + "• " + text)


# ── InfluxDB helpers ──────────────────────────────────────────────────────────
def query_df(client: InfluxDBClient, flux: str) -> pd.DataFrame:
    try:
        result = client.query_api().query_data_frame(flux)
        if isinstance(result, list):
            result = pd.concat(result, ignore_index=True) if result else pd.DataFrame()
        return result
    except Exception as exc:
        print(f"  [WARN] Query failed: {exc}")
        return pd.DataFrame()


# ── Analysis functions ────────────────────────────────────────────────────────

def analyse_sonos(client: InfluxDBClient) -> dict:
    """Find Sonos devices, analyse their signal quality and dropout patterns."""
    df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "wifi_client"
               and r.is_sonos == "true"
               and (r._field == "rssi" or r._field == "snr" or r._field == "satisfaction"
                    or r._field == "tx_retries"))
          |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
          |> pivot(rowKey: ["_time", "hostname", "ap_mac", "radio", "channel"],
                   columnKey: ["_field"], valueColumn: "_value")
    """)

    if df.empty:
        return {"found": False}

    results = {"found": True, "devices": []}
    for hostname, grp in df.groupby("hostname"):
        d = {
            "hostname": hostname,
            "ap_mac": grp["ap_mac"].mode().iloc[0] if "ap_mac" in grp else "unknown",
            "radio": grp["radio"].mode().iloc[0] if "radio" in grp else "unknown",
            "channel": grp["channel"].mode().iloc[0] if "channel" in grp else "unknown",
        }
        if "rssi" in grp:
            d["rssi_mean"]  = float(grp["rssi"].mean())
            d["rssi_min"]   = float(grp["rssi"].min())
            d["rssi_p10"]   = float(grp["rssi"].quantile(0.10))
            d["poor_rssi_pct"] = float((grp["rssi"] < -75).mean() * 100)
        if "snr" in grp:
            d["snr_mean"]   = float(grp["snr"].mean())
            d["poor_snr_pct"] = float((grp["snr"] < 20).mean() * 100)
        if "satisfaction" in grp:
            d["satisfaction_mean"] = float(grp["satisfaction"].mean())
        if "tx_retries" in grp:
            d["tx_retries_mean"] = float(grp["tx_retries"].mean())
        results["devices"].append(d)

    return results


def analyse_dropout_correlation(client: InfluxDBClient) -> dict:
    """Correlate Sonos RSSI with total client count — the guest-party effect."""
    rssi_df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "wifi_client"
               and r.is_sonos == "true" and r._field == "rssi")
          |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
    """)

    clients_df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "network_summary"
               and r._field == "wifi_clients")
          |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
    """)

    if rssi_df.empty or clients_df.empty:
        return {"correlation": None}

    # Align on time
    rssi_df     = rssi_df.rename(columns={"_value": "rssi"}).set_index("_time")["rssi"]
    clients_df  = clients_df.rename(columns={"_value": "clients"}).set_index("_time")["clients"]
    merged      = pd.concat([rssi_df, clients_df], axis=1).dropna()

    if len(merged) < 10:
        return {"correlation": None}

    corr = float(merged.corr().loc["rssi", "clients"])

    # Bucket by client count and find average RSSI
    merged["client_bucket"] = pd.cut(merged["clients"], bins=[0, 10, 20, 30, 50, 100],
                                      labels=["1-10", "11-20", "21-30", "31-50", "50+"])
    rssi_by_load = merged.groupby("client_bucket")["rssi"].mean().to_dict()

    # Find peak load periods
    peak_load = float(merged["clients"].quantile(0.95))
    peak_rssi = float(merged[merged["clients"] >= peak_load * 0.9]["rssi"].mean()) if not merged.empty else None

    return {
        "correlation": corr,
        "rssi_by_client_load": {str(k): round(v, 1) for k, v in rssi_by_load.items()},
        "peak_client_count_p95": peak_load,
        "rssi_at_peak_load": peak_rssi,
    }


def analyse_channel_congestion(client: InfluxDBClient) -> dict:
    """Find which APs and channels are congested."""
    df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "ap_radio"
               and (r._field == "channel_utilization" or r._field == "tx_retries" or r._field == "num_sta"))
          |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
          |> pivot(rowKey: ["_time", "ap_name", "radio", "channel"],
                   columnKey: ["_field"], valueColumn: "_value")
    """)

    if df.empty:
        return {}

    aps = {}
    for (ap_name, radio, channel), grp in df.groupby(["ap_name", "radio", "channel"]):
        key = f"{ap_name} ({radio}, ch{channel})"
        d = {"ap_name": ap_name, "radio": radio, "channel": str(channel)}
        if "channel_utilization" in grp:
            d["util_mean"]  = float(grp["channel_utilization"].mean())
            d["util_p95"]   = float(grp["channel_utilization"].quantile(0.95))
            d["util_max"]   = float(grp["channel_utilization"].max())
            d["high_util_pct"] = float((grp["channel_utilization"] > 60).mean() * 100)
        if "tx_retries" in grp:
            d["retries_mean"] = float(grp["tx_retries"].mean())
        if "num_sta" in grp:
            d["clients_mean"] = float(grp["num_sta"].mean())
            d["clients_max"]  = float(grp["num_sta"].max())
        aps[key] = d

    return aps


def analyse_roaming_events(client: InfluxDBClient) -> dict:
    """Count roaming / disconnect events, identify problematic times."""
    df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "network_event"
               and r._field == "message")
          |> filter(fn: (r) => r.event_key =~ /disconnect|roam|EVT_WU|handover/)
    """)

    if df.empty:
        return {"total_events": 0}

    total = len(df)
    by_type = df["event_key"].value_counts().to_dict() if "event_key" in df else {}

    # Events by hour of day
    if "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
        by_hour = df.groupby(df["_time"].dt.hour).size().to_dict()
    else:
        by_hour = {}

    peak_hour = max(by_hour, key=by_hour.get) if by_hour else None

    return {
        "total_events": total,
        "by_event_type": {str(k): int(v) for k, v in list(by_type.items())[:10]},
        "by_hour": {int(k): int(v) for k, v in by_hour.items()},
        "peak_hour": int(peak_hour) if peak_hour is not None else None,
    }


def analyse_ap_client_distribution(client: InfluxDBClient) -> dict:
    """Check if clients are balanced across APs or piling onto one."""
    df = query_df(client, f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {RANGE})
          |> filter(fn: (r) => r._measurement == "ap_radio" and r._field == "num_sta")
          |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
    """)

    if df.empty:
        return {}

    result = {}
    if "ap_name" in df.columns:
        for ap, grp in df.groupby("ap_name"):
            result[str(ap)] = {
                "clients_mean": float(grp["_value"].mean()),
                "clients_max":  float(grp["_value"].max()),
            }

    return result


# ── Report generation ─────────────────────────────────────────────────────────

def severity(condition: bool, high_msg: str, ok_msg: str = "") -> str:
    return f"⚠️  {high_msg}" if condition else f"✅  {ok_msg}" if ok_msg else ""


def generate_report(sonos, correlation, channels, events, ap_dist):
    ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    print(f"\n{'═' * 72}")
    print(f"  UniFi Network Diagnostic Report")
    print(f"  Generated: {ts}  |  Analysis window: 7 days")
    print(f"{'═' * 72}")

    # ── 1. Sonos signal quality ──────────────────────────────────────────────
    section("1. SONOS DEVICE SIGNAL QUALITY")
    if not sonos.get("found"):
        print("  No Sonos devices identified in the data yet.")
        print("  Allow the collector to run for a few polls, then re-run.")
    else:
        for d in sonos["devices"]:
            print(f"\n  Device : {d['hostname']}")
            print(f"  AP     : {d['ap_mac']}   Radio: {d['radio']}   Channel: {d['channel']}")

            rssi_mean = d.get("rssi_mean")
            rssi_min  = d.get("rssi_min")
            poor_pct  = d.get("poor_rssi_pct", 0)
            sat_mean  = d.get("satisfaction_mean")
            snr_mean  = d.get("snr_mean")

            if rssi_mean is not None:
                print(f"\n  RSSI   : mean {rssi_mean:.0f} dBm  |  min {rssi_min:.0f} dBm  |  "
                      f"{poor_pct:.0f}% of time below -75 dBm")
                if rssi_mean < -75:
                    bullet(f"RSSI is poor ({rssi_mean:.0f} dBm). This Sonos is too far from an AP "
                           f"or obstructed. Target: better than -65 dBm.", 4)
                elif rssi_mean < -65:
                    bullet(f"RSSI is marginal ({rssi_mean:.0f} dBm). Acceptable at rest "
                           f"but degrades under load.", 4)
                else:
                    bullet(f"RSSI is acceptable ({rssi_mean:.0f} dBm).", 4)

            if snr_mean is not None:
                print(f"  SNR    : mean {snr_mean:.0f} dB")
                if snr_mean < 20:
                    bullet("SNR below 20 dB — high noise floor. Interference likely from "
                           "neighbours, microwave, or 2.4 GHz congestion.", 4)

            if sat_mean is not None:
                print(f"  Score  : UniFi satisfaction {sat_mean:.0f}/100")
                if sat_mean < 60:
                    bullet("Satisfaction score is low. UniFi itself flags this client as having "
                           "a poor experience.", 4)

            if d.get("radio") == "ng":
                bullet("⚠️  Sonos is on 2.4 GHz (radio=ng). Sonos should prefer 5 GHz — "
                       "2.4 GHz is more congested and has lower throughput.", 4)

    # ── 2. Guest-load correlation ────────────────────────────────────────────
    section("2. GUEST ACTIVITY CORRELATION")
    corr = correlation.get("correlation")
    if corr is None:
        print("  Not enough data yet for correlation analysis.")
    else:
        print(f"  Correlation between total client count and Sonos RSSI: {corr:.2f}")
        if corr < -0.4:
            print(f"\n  {severity(True, 'Strong negative correlation confirmed.')}")
            bullet("When more guests connect, Sonos signal quality measurably degrades.", 4)
            bullet("Root cause: channel congestion from additional devices raises noise floor "
                   "and increases contention, reducing available airtime for Sonos.", 4)

            rssi_by_load = correlation.get("rssi_by_client_load", {})
            if rssi_by_load:
                print("\n  RSSI by network load:")
                for bucket_label, rssi_val in rssi_by_load.items():
                    bar = "█" * max(0, int((rssi_val + 90) / 2))
                    print(f"    {bucket_label:>8} clients  →  {rssi_val:.0f} dBm  {bar}")

            peak = correlation.get("peak_client_count_p95")
            peak_rssi = correlation.get("rssi_at_peak_load")
            if peak and peak_rssi:
                print(f"\n  At peak load ({peak:.0f} clients): Sonos RSSI drops to {peak_rssi:.0f} dBm")
        elif -0.4 <= corr <= -0.1:
            print(f"\n  Mild correlation. Guest load is a contributing factor but not the sole cause.")
        else:
            print(f"\n  Weak/no correlation. Guest load is probably not the primary cause.")
            bullet("Look at permanent RF obstructions or AP placement instead.", 4)

    # ── 3. Channel congestion ────────────────────────────────────────────────
    section("3. CHANNEL CONGESTION ANALYSIS")
    if not channels:
        print("  No AP data available yet.")
    else:
        congested_aps = {k: v for k, v in channels.items() if v.get("util_p95", 0) > 50}
        healthy_aps   = {k: v for k, v in channels.items() if v.get("util_p95", 0) <= 50}

        if congested_aps:
            print(f"  {len(congested_aps)} congested AP radio(s) found:\n")
            for label, ap in sorted(congested_aps.items(), key=lambda x: -x[1].get("util_p95", 0)):
                print(f"  {label}")
                print(f"    Utilisation: mean {ap.get('util_mean',0):.0f}%  "
                      f"p95 {ap.get('util_p95',0):.0f}%  "
                      f"max {ap.get('util_max',0):.0f}%  "
                      f"({ap.get('high_util_pct',0):.0f}% of time above 60%)")
                if ap.get("clients_max"):
                    print(f"    Clients: avg {ap.get('clients_mean',0):.1f}  "
                          f"peak {ap.get('clients_max',0):.0f}")

                if ap.get("util_p95", 0) > 70:
                    bullet("CRITICAL: >70% utilisation at p95. This AP is severely overloaded.", 6)
                    if ap.get("radio") == "ng":
                        bullet("2.4 GHz band is highly congested. Switch Sonos (and other IoT) "
                               "to 5 GHz or enable band steering aggressively.", 6)
                    else:
                        bullet("5 GHz congested — too many clients. Consider adding an AP or "
                               "enabling 6 GHz if your hardware supports it.", 6)
                else:
                    bullet("Moderate congestion. Likely worsens during peak guest hours.", 6)
        else:
            print("  No severely congested APs found at p95.")

        if healthy_aps:
            print(f"\n  {len(healthy_aps)} AP radio(s) operating normally.")

    # ── 4. Roaming / disconnect events ──────────────────────────────────────
    section("4. ROAMING & DISCONNECT EVENTS")
    total_ev = events.get("total_events", 0)
    peak_hr  = events.get("peak_hour")

    print(f"  Total disconnect/roam events in 7 days: {total_ev}")
    if total_ev > 200:
        bullet("High event count indicates persistent instability.", 4)
    elif total_ev > 50:
        bullet("Moderate event count — some roaming instability present.", 4)
    else:
        bullet("Event count is within normal range.", 4)

    if events.get("by_event_type"):
        print("\n  Event breakdown:")
        for ev_type, count in events["by_event_type"].items():
            print(f"    {ev_type:<40} {count:>5}")

    if peak_hr is not None:
        print(f"\n  Peak event hour: {peak_hr:02d}:00 — {peak_hr+1:02d}:00 local time")
        if 18 <= peak_hr <= 23:
            bullet("Peak in evening hours — consistent with guest activity.", 4)
        elif 8 <= peak_hr <= 10:
            bullet("Peak in morning — possible interference from neighbouring networks "
                   "becoming active.", 4)

    # ── 5. AP load balance ───────────────────────────────────────────────────
    section("5. AP LOAD DISTRIBUTION")
    if not ap_dist:
        print("  No per-AP client data available yet.")
    else:
        print(f"  {'AP':<30} {'Avg clients':>12}  {'Peak clients':>13}")
        print(f"  {'-'*30}  {'-'*12}  {'-'*13}")
        for ap_name, d in sorted(ap_dist.items(), key=lambda x: -x[1].get("clients_mean", 0)):
            print(f"  {ap_name:<30} {d['clients_mean']:>11.1f}  {d['clients_max']:>12.0f}")

        means = [v["clients_mean"] for v in ap_dist.values()]
        if len(means) > 1 and max(means) > 2 * min(means):
            bullet("Load imbalance detected. One AP is handling significantly more clients. "
                   "Check band steering and minimum RSSI thresholds.", 4)
        else:
            bullet("Client load is reasonably balanced across APs.", 4)

    # ── 6. Recommended fixes ─────────────────────────────────────────────────
    section("6. RECOMMENDED FIXES (PRIORITY ORDER)")

    fixes = []

    # Sonos on wrong band
    for d in sonos.get("devices", []):
        if d.get("radio") == "ng":
            fixes.append((1, "CRITICAL",
                "Sonos is on 2.4 GHz.",
                "In UniFi Network > WiFi, enable band steering or create a separate 5 GHz SSID "
                "and force Sonos to connect to it. 5 GHz has far less interference and higher "
                "throughput. This alone may resolve the dropouts."))

    # Poor RSSI
    for d in sonos.get("devices", []):
        if d.get("rssi_mean", 0) < -70:
            fixes.append((2, "HIGH",
                f"Sonos RSSI is {d.get('rssi_mean', 'unknown'):.0f} dBm (target: better than -65 dBm).",
                "Move the nearest AP closer to the kitchen, or add a new AP. In UniFi, check "
                "'Clients' view to see which AP the Sonos is associating with and its exact "
                "distance/obstacle path."))

    # Channel congestion
    for label, ap in channels.items():
        if ap.get("util_p95", 0) > 65:
            radio = ap.get("radio", "?")
            ch    = ap.get("channel", "?")
            fixes.append((3, "HIGH",
                f"AP '{ap['ap_name']}' on {radio}/ch{ch} has {ap.get('util_p95',0):.0f}% channel "
                f"utilisation at p95.",
                "In UniFi > WiFi > Edit AP, set minimum TX power to high and enable "
                "'Optimize Network Traffic'. If on 2.4 GHz, reduce max client count or "
                "migrate devices to 5 GHz. Consider adding a dedicated 6 GHz AP if your UDM "
                "supports UniFi U7 series APs."))

    # Guest load correlation
    if correlation.get("correlation", 0) < -0.4:
        fixes.append((4, "MEDIUM",
            "Guest traffic is the primary trigger for Sonos dropouts.",
            "Create a separate IoT/Sonos VLAN with its own SSID on 5 GHz band only. This "
            "isolates Sonos from guest device contention. In UniFi, go to Settings > Networks, "
            "add a new VLAN, then create a dedicated WiFi SSID assigned to that network. "
            "Force all Sonos speakers to connect to this SSID only."))

    # High event count
    if events.get("total_events", 0) > 100:
        fixes.append((5, "MEDIUM",
            "High roaming/disconnect event count.",
            "Lower the 'Minimum RSSI' threshold in UniFi WiFi settings (try -72 dBm). This "
            "forces clients to disconnect from a weak AP sooner and roam to a stronger one, "
            "reducing the sticky-client problem where Sonos clings to a distant AP."))

    # AP load imbalance
    means = [v["clients_mean"] for v in ap_dist.values()]
    if len(means) > 1 and max(means) > 2 * min(means):
        fixes.append((6, "LOW",
            "AP load imbalance.",
            "Enable 'Load Balancing' in UniFi WiFi settings. Also set minimum RSSI per AP so "
            "clients disconnect and reassociate with the better AP."))

    if not fixes:
        print("\n  No critical issues found. Network appears healthy.")
    else:
        for i, (_, sev, problem, fix) in enumerate(sorted(fixes, key=lambda x: x[0]), 1):
            print(f"\n  [{i}] [{sev}] {problem}")
            # Word-wrap the fix text
            words = fix.split()
            line = "      "
            for word in words:
                if len(line) + len(word) + 1 > 72:
                    print(line)
                    line = "      " + word
                else:
                    line += (" " if line.strip() else "") + word
            if line.strip():
                print(line)

    # ── Summary ──────────────────────────────────────────────────────────────
    section("SUMMARY")
    critical = [f for f in fixes if f[1] == "CRITICAL"]
    high     = [f for f in fixes if f[1] == "HIGH"]
    medium   = [f for f in fixes if f[1] == "MEDIUM"]

    if critical:
        print(f"  {len(critical)} CRITICAL issue(s) — address these first.")
    if high:
        print(f"  {len(high)} HIGH priority issue(s).")
    if medium:
        print(f"  {len(medium)} MEDIUM priority issue(s).")
    if not fixes:
        print("  Network health looks good. Continue monitoring.")

    print(f"\n  Most likely root cause of Sonos dropouts:")
    if not sonos.get("found"):
        print("  → Cannot determine yet (no data collected).")
    elif correlation.get("correlation", 0) < -0.35 and any(
        ap.get("util_p95", 0) > 55 for ap in channels.values()
    ):
        print("  → Guest-driven channel congestion. When guests connect, the 2.4/5 GHz band")
        print("    becomes saturated, raising the noise floor for Sonos.")
        print("    Fix: Dedicated Sonos SSID on 5 GHz only (Fix #4) + band steering (Fix #1).")
    elif any(d.get("rssi_mean", 0) < -70 for d in sonos.get("devices", [])):
        print("  → Poor AP coverage in the kitchen. Sonos RSSI is consistently weak,")
        print("    meaning it is at the edge of reliable coverage.")
        print("    Fix: Move or add an AP closer to the kitchen.")
    else:
        print("  → Review the findings above — multiple contributing factors.")

    print(f"\n{'═' * 72}\n")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if not INFLUX_TOKEN:
        print("ERROR: INFLUX_TOKEN not set. Source your .env file first:")
        print("  export $(grep -v '^#' .env | xargs) && python analysis/diagnose.py")
        sys.exit(1)

    print("Connecting to InfluxDB...")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)

    print(f"Querying {RANGE} of data from bucket '{INFLUX_BUCKET}'...")
    print("(This may take 10–30 seconds for a full week of data)\n")

    sonos       = analyse_sonos(client)
    correlation = analyse_dropout_correlation(client)
    channels    = analyse_channel_congestion(client)
    events      = analyse_roaming_events(client)
    ap_dist     = analyse_ap_client_distribution(client)

    generate_report(sonos, correlation, channels, events, ap_dist)

    client.close()


if __name__ == "__main__":
    main()
