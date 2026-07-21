#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build an interactive HTML dashboard from a Volkswagen EU Data Act export.

Get your data: register at https://eu-data-act.drivesomethinggreater.com/de/en
(Volkswagen's EU Data Act portal), request the historical data package for your
vehicle and download the zip when notified — the link expires after a few days.
See README.md for the full walkthrough.

Usage:
    python3 build_dashboard.py [export.json ...] [-o dashboard.html]
                               [--language {en,de,nl,lt}]

Reads the vehicle data export (a flat list of {key, dataFieldName, value,
timestampUtc} records), cleans known sensor error values, aggregates the
high-frequency diagnostic channels into chart-ready series and writes a single
self-contained dashboard.html (no network access needed to view it).

Field descriptions from VW's Data Dictionary are bundled into the script —
the export JSON is the only input needed.
"""

import argparse
import bisect
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

# All displayed times are shifted from UTC by this offset. Match it to your
# vehicle's clock with --utc-offset (the export itself is UTC; the value only
# affects display and day boundaries).
LOCAL_UTC_OFFSET_H = 3.0
LOCAL_TZ_LABEL = "UTC+3"

# Usable battery capacity assumed for energy estimates (ID.7 Pro: 77 kWh
# usable; Pro S: 86). Only affects kWh figures, not the SoC data itself.
# Override with --pack-kwh.
PACK_KWH_USABLE = 77.0


def set_timezone(offset_h):
    global LOCAL_UTC_OFFSET_H, LOCAL_TZ_LABEL
    LOCAL_UTC_OFFSET_H = offset_h
    LOCAL_TZ_LABEL = "UTC%+g" % offset_h


# All MEB packs run 96 cells in series; pack voltage = mean cell voltage x 96
MEB_SERIES_CELLS = 96

# VIN positions 7-8 identify the model on VW EU VINs (WVWZZZ<code>...), and
# each MEB model shipped with a known set of usable pack sizes (kWh). The
# measured capacity is matched against these to pick the nominal for
# state-of-health. Sizes are usable (net) figures as commonly published.
VIN_MODELS = {
    "E1": ("Volkswagen ID.3", [45, 52, 58, 77, 79]),
    "E2": ("Volkswagen ID.4 / ID.5", [52, 77, 79]),
    "ED": ("Volkswagen ID.7", [77, 86]),
    "EB": ("Volkswagen ID. Buzz", [77, 86]),
    "NY": ("Škoda Enyaq", [58, 77]),
}
# The portal serves all participating VW Group brands; recognise them from the
# VIN's world manufacturer identifier so the dashboard is titled sensibly even
# where the per-model code table above doesn't apply.
VIN_BRANDS = {
    "WVW": "Volkswagen", "WVG": "Volkswagen",
    "WV1": "Volkswagen Commercial Vehicles", "WV2": "Volkswagen Commercial Vehicles",
    "WV3": "Volkswagen Commercial Vehicles",
    "WAU": "Audi", "WA1": "Audi", "WUA": "Audi",
    "TMB": "Škoda", "VSS": "SEAT / Cupra", "SCB": "Bentley",
}
# fallback across the MEB family when the model is unknown
KNOWN_USABLE_KWH = [45, 52, 58, 77, 79, 86]


def detect_vehicle_title(vin):
    if vin and len(vin) >= 8:
        entry = VIN_MODELS.get(vin[6:8].upper())
        if entry:
            return entry[0]
    if vin and len(vin) >= 3:
        brand = VIN_BRANDS.get(vin[:3].upper())
        if brand:
            return brand
    return "Volkswagen Group EV"


def pack_options_for_vin(vin):
    if vin and len(vin) >= 8:
        entry = VIN_MODELS.get(vin[6:8].upper())
        if entry:
            return entry[1]
    return KNOWN_USABLE_KWH


def pick_nominal_pack(measured_kwh, options):
    """Packs only lose capacity, so the nominal must be >= the measured value
    (minus ~3% measurement tolerance). Pick the smallest option that fits —
    a pack measuring 80 kWh can only be a degraded 86, never a healthy 77."""
    fits = [c for c in options if c >= measured_kwh * 0.97]
    return min(fits) if fits else max(options)


def detect_utc_offset(recs):
    """The vehicle's own clock (instrumentClusterTime fields) carries its
    local UTC offset, e.g. '2026-07-20T10:42:49.000+03:00'."""
    rx = re.compile(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?([+-])(\d{2}):(\d{2})$")
    for r in recs:
        if "instrumentClusterTime" in r["dataFieldName"]:
            m = rx.search(r["value"].strip())
            if m:
                sign = -1 if m.group(1) == "-" else 1
                return sign * (int(m.group(2)) + int(m.group(3)) / 60)
    return None


def estimate_pack_capacity(charge_runs, current, cell_max, cell_min):
    """Measure usable capacity: integrate battery current x pack voltage over
    charging windows with a known SoC gain (>= 30% for a stable estimate,
    >= 70% current-sample coverage). Pack voltage = mean cell voltage x 96
    (all MEB packs are 96s). Median across charges is the measured capacity."""
    cmin = dict(cell_min)
    volt = {t: (v + cmin[t]) / 2 / 1000 * MEB_SERIES_CELLS
            for t, v in cell_max if t in cmin}
    volt_ts = sorted(volt)
    estimates = []
    for a, va, b, vb in charge_runs:
        dsoc = vb - va
        if dsoc < 30 or b <= a:
            continue
        win = [(t, i) for t, i in current if a <= t <= b]
        if len(win) < 20:
            continue
        e_wh = covered = 0.0
        for (t0, i0), (t1, i1) in zip(win, win[1:]):
            gap = t1 - t0
            if gap > 300:
                continue
            mid = (t0 + t1) / 2
            k = bisect.bisect_left(volt_ts, mid)
            cands = [volt_ts[j] for j in (k - 1, k) if 0 <= j < len(volt_ts)]
            if not cands:
                continue
            tv = min(cands, key=lambda x: abs(x - mid))
            if abs(tv - mid) > 900:
                continue
            e_wh += (i0 + i1) / 2 * volt[tv] * gap / 3600
            covered += gap
        frac = covered / (b - a)
        if frac < 0.7:
            continue
        cap = e_wh / 1000 / frac / dsoc * 100
        if 20 <= cap <= 130:   # sanity band; outside = bad sign convention or data
            estimates.append({"start": a, "dsoc": round(dsoc),
                              "socFrom": round(va), "socTo": round(vb),
                              "hours": round((b - a) / 3600, 1),
                              "coveragePct": round(frac * 100),
                              "samples": len(win),
                              "kwhIn": round(e_wh / 1000 / frac, 1),
                              "capKwh": round(cap, 1)})
    return estimates


def assess_battery_health(measured_kwh, nominal_kwh, spread_stats, capacity_proxy=None):
    """Plain-language verdict from measured capacity and cell imbalance.
    Deliberately conservative: 'attention' is a prompt to investigate, not a
    diagnosis. Returns dict for the dashboard's health card."""
    reasons = []
    levels = []   # 0 good, 1 fair, 2 attention

    if spread_stats:
        med, mx = spread_stats["median"], spread_stats["max"]
        if med <= 10 and mx <= 40:
            levels.append(0)
            reasons.append(("good", f"Cells are well balanced ({med:g} mV median spread, {mx:g} mV worst)"))
        elif med <= 20 and mx <= 60:
            levels.append(1)
            reasons.append(("fair", f"Cell imbalance is elevated but acceptable ({med:g} mV median, {mx:g} mV worst)"))
        else:
            levels.append(2)
            reasons.append(("attention", f"Cell imbalance is high ({med:g} mV median, {mx:g} mV worst) — worth a service check"))

    soh = None
    if measured_kwh and nominal_kwh:
        soh = min(100, round(measured_kwh / nominal_kwh * 100))
        if soh >= 90:
            levels.append(0)
            reasons.append(("good", f"Measured usable capacity ≈ {measured_kwh:g} kWh — about {soh}% of the {nominal_kwh:g} kWh nominal pack"))
        elif soh >= 78:
            levels.append(1)
            reasons.append(("fair", f"Measured usable capacity ≈ {measured_kwh:g} kWh (~{soh}% of nominal) — normal aging"))
        else:
            levels.append(2)
            reasons.append(("attention", f"Measured usable capacity ≈ {measured_kwh:g} kWh (~{soh}% of nominal) — approaching the 70% warranty threshold"))
    elif capacity_proxy:
        by_type = capacity_proxy.get("byType", [])
        type_text = ""
        if len(by_type) >= 2:
            type_text = " (" + ", ".join(
                f"{row['type']} median {row['medianKwh']:g} kWh" for row in by_type) + ")"
        reasons.append((
            "info",
            f"Reported charging energy / SoC gained has a {capacity_proxy['medianKwh']:g} kWh "
            f"median{type_text}, but the export does not document where energy is metered; "
            "charging losses, auxiliaries and 1% SoC rounding mean this ratio cannot support a battery-capacity or SoH conclusion."
        ))
    elif not measured_kwh:
        reasons.append(("info", "Capacity could not be measured (needs a ≥30% charge while the car reports current) — verdict based on cell balance only"))

    if not levels:
        verdict = "unknown"
    else:
        verdict = ["good", "fair", "attention"][max(levels)]
    return {"verdict": verdict, "sohPct": soh, "reasons": reasons}
CHARGE_SAMPLE_GAP_S = 30 * 60
TRIP_SAMPLE_GAP_S = 30 * 60

THERMAL_CHANNELS = {
    "543977": "Sensor A",
    "546303": "Sensor B",
    "545648": "Sensor C",
    "546464": "Sensor D",
    "546126": "Sensor E",
    "544652": "Sensor F",
    "546445": "Sensor G",
}

# Diagnostic measurement channels are not documented in VW's Data Dictionary;
# these labels are inferred from units, value ranges and cross-checks against
# the snapshot fields (odometer/SoC match mileage_info/hvsoc_info exactly).
DIAG_LABELS = {
    "180876": ("Odometer", "km", "matches mileage_info; error value 1048574 filtered"),
    "180886": ("HV battery state of charge", "%", "matches hvsoc_info"),
    "180806": ("Ambient temperature", "K", "Kelvin; 0 K error values filtered"),
    "180957": ("Binary status flag", "", "meaning unknown"),
    "545620": ("Vehicle speed", "km/h", ""),
    "543765": ("Highest cell voltage", "mV", "always >= channel 545776"),
    "545776": ("Lowest cell voltage", "mV", "always <= channel 543765"),
    "546774": ("HV battery current", "A", "negative = discharge peaks"),
    "543919": ("Thermal management operating mode", "", "German enum labels"),
    "545844": ("Valve motor voltage", "V", ""),
    "543814": ("Coolant valve state", "", "Ventil_(nicht_)angesteuert"),
    "544790": ("Coolant valve state", "", "Ventil_(nicht_)angesteuert"),
    "546697": ("Coolant flow", "L/min", ""),
}
for _t in ("543977", "546303", "545648", "546464", "546126", "544652", "546445"):
    DIAG_LABELS[_t] = ("Thermal system temperature sensor", "°C", "exact location unknown")
for _e in ("546573", "546114", "545548", "546682", "543975", "543879", "544829", "546538"):
    DIAG_LABELS[_e] = ("Thermal system state", "enum", "meaning unknown")

THERMAL_MODE_LABELS = {
    "Lueften": "Ventilation",
    "Kuehlen_Innenraum": "Cabin cooling",
    "Kuehlen_Innenraum_mit_HGK": "Cabin cooling + HGK",
    "Heizen_kombiniert_WP": "Combined heating (heat pump)",
    "Heizen_Luft_WP": "Air heating (heat pump)",
    "Shutdown": "Shutdown",
    "Init": "Init",
}


# ---------------------------------------------------------------------------
# Bundled Data Dictionary content — extracted once from VW's
# "251022_01_SVK_DataDictionary_V4.0 - Historical Data" document (11.09.2025).
# The dictionary changes rarely; if VW publishes a new version, regenerate
# this block (see the repository notes) rather than parsing at runtime.
BUNDLED_DICTIONARY_INFO = {
    "keys": 5140,
    "uuidOccurrences": 5150,
    "dataPointNames": 4431,
    "describedNames": 4429,
    "pdfDescribedNames": 4423,
    "ambiguousDataPointNames": 27,
    "version": "V4.0 (11.09.2025), bundled",
    "terms": {
        "warninglightdata.messageId": True,
        "warninglightdata.priority": True,
        "warninglightdata.serviceLead": True,
        "payloadDecoded_warnings_id": True,
        "payloadDecoded_warnings_fields": True,
        "ilfdia.status": True,
    },
}

BUNDLED_FIELD_DESCRIPTIONS = {
    "*].debouncePostTime": "seconds until the vehicle sends a Geofencing Violation notification to the Business Service",
    "*].endingReason": "Spielt beim steuern des Wiederholjobs eine Rolle falls ein MK vom System automatisch entzogen werden soll",
    ".recurring.departureTime": "envelo recurring timer for for when the climatization should be completed on a specific day.",
    "01_msp_elli.msp_elli_additional_data.free_text_1": "raw data 1",
    "01_msp_elli.msp_elli_additional_data.free_text_2": "raw data 2",
    "01_msp_elli.msp_elli_additional_data.free_text_3": "raw data 3",
    "01_msp_elli.msp_elli_additional_data.free_text_4": "raw data 4",
    "01_msp_elli.msp_elli_additional_data.free_text_5": "raw data 5",
    "01_msp_elli.msp_elli_additional_data.free_text_6": "raw data 6",
    "01_msp_elli.msp_elli_additional_data.free_text_7": "raw data 7",
    "01_msp_elli.msp_elli_additional_data.free_text_8": "raw data 8",
    "01_msp_elli.msp_elli_additional_data.id": "The id of the cdr. Unique within a cpo.",
    "01_msp_elli.msp_elli_additional_data.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_app_additional_data.info": "information description",
    "01_msp_elli.msp_elli_app_additional_data.info_category": "category of information",
    "01_msp_elli.msp_elli_app_additional_data.timestamp": "time when the data was recorded",
    "01_msp_elli.msp_elli_app_crashlytics.app_orientation": "App's UI orientation at crash time",
    "01_msp_elli.msp_elli_app_crashlytics.application": "App version details",
    "01_msp_elli.msp_elli_app_crashlytics.blame_frame": "Stack frame suspected to have caused crash",
    "01_msp_elli.msp_elli_app_crashlytics.breadcrumbs": "App events before crash for debugging",
    "01_msp_elli.msp_elli_app_crashlytics.bundle_identifier": "App's bundle identifier or package name",
    "01_msp_elli.msp_elli_app_crashlytics.crashlytics_sdk_version": "Version of Crashlytics SDK used",
    "01_msp_elli.msp_elli_app_crashlytics.custom_keys": "Custom app-specific debug values",
    "01_msp_elli.msp_elli_app_crashlytics.device": "Information about the user's device",
    "01_msp_elli.msp_elli_app_crashlytics.device_orientation": "Physical orientation of the device",
    "01_msp_elli.msp_elli_app_crashlytics.error_type": "High-level category of error",
    "01_msp_elli.msp_elli_app_crashlytics.errors": "Non-fatal errors that occurred",
    "01_msp_elli.msp_elli_app_crashlytics.event_id": "Unique identifier for the crash or event",
    "01_msp_elli.msp_elli_app_crashlytics.event_timestamp": "Time when the crash/event occurred",
    "01_msp_elli.msp_elli_app_crashlytics.exceptions": "Exception stack traces",
    "01_msp_elli.msp_elli_app_crashlytics.iam_id": "User metadata at time of crash",
    "01_msp_elli.msp_elli_app_crashlytics.installation_uuid": "Unique app installation ID",
    "01_msp_elli.msp_elli_app_crashlytics.issue_id": "Unique identifier for the issue in Crashlytics",
    "01_msp_elli.msp_elli_app_crashlytics.issue_subtitle": "Short summary or context of the issue",
    "01_msp_elli.msp_elli_app_crashlytics.issue_title": "Title describing the issue",
    "01_msp_elli.msp_elli_app_crashlytics.logs": "App logs recorded near time of crash",
    "01_msp_elli.msp_elli_app_crashlytics.memory": "Memory usage details at the time of crash",
    "01_msp_elli.msp_elli_app_crashlytics.native_crash_info": "Info for native-level crashes",
    "01_msp_elli.msp_elli_app_crashlytics.operating_system": "Details of the OS running the app",
    "01_msp_elli.msp_elli_app_crashlytics.platform": "Platform on which the app is running (e.g. iOS or Android)",
    "01_msp_elli.msp_elli_app_crashlytics.process_state": "State of the app process at crash time",
    "01_msp_elli.msp_elli_app_crashlytics.received_timestamp": "Time when the event was received by backend",
    "01_msp_elli.msp_elli_app_crashlytics.remote_config_feature_rollouts": "Remote Config rollout data",
    "01_msp_elli.msp_elli_app_crashlytics.storage": "Storage usage at the time of crash",
    "01_msp_elli.msp_elli_app_crashlytics.threads": "Thread information at crash time",
    "01_msp_elli.msp_elli_app_crashlytics.unity_metadata": "Unity-specific device and runtime details",
    "01_msp_elli.msp_elli_app_crashlytics.variant_id": "Experiment or rollout variant ID",
    "01_msp_elli.msp_elli_app_feedback.categories": "Feedback categories (can be multiple)",
    "01_msp_elli.msp_elli_app_feedback.content": "The actual feedback text",
    "01_msp_elli.msp_elli_app_feedback.created_at": "Record creation timestamp",
    "01_msp_elli.msp_elli_app_feedback.id": "Unique feedback ID",
    "01_msp_elli.msp_elli_app_feedback.updated_at": "Last update timestamp",
    "01_msp_elli.msp_elli_app_ratings.created_at": "Record creation timestamp.",
    "01_msp_elli.msp_elli_app_ratings.id": "Unique rating ID",
    "01_msp_elli.msp_elli_app_ratings.rating": "Numerical rating (e.g., 1 -5)",
    "01_msp_elli.msp_elli_app_ratings.updated_at": "Last update timestamp",
    "01_msp_elli.msp_elli_app_satisfaction_feedback.created_at": "Record creation timestamp",
    "01_msp_elli.msp_elli_app_satisfaction_feedback.id": "Unique record ID",
    "01_msp_elli.msp_elli_app_satisfaction_feedback.satisfied": "Whether the user is satisfied",
    "01_msp_elli.msp_elli_app_satisfaction_feedback.updated_at": "Last update timestamp",
    "01_msp_elli.msp_elli_ocpi_cdr.auth_method": "Method used for authentication.",
    "01_msp_elli.msp_elli_ocpi_cdr.authorization_reference": "Reference to the authorization given by the eMSP.",
    "01_msp_elli.msp_elli_ocpi_cdr.charging_periods": "List of Charging Periods that make up this charging session.",
    "01_msp_elli.msp_elli_ocpi_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "01_msp_elli.msp_elli_ocpi_cdr.credit": "When set to true, this is a Credit CDR.",
    "01_msp_elli.msp_elli_ocpi_cdr.credit_reference_id": "Is required to be set for a Credit CDR. This SHALL contain the id of the CDR for which this is a Credit CDR.",
    "01_msp_elli.msp_elli_ocpi_cdr.currency": "Currency of the CDR in ISO 4217 Code.",
    "01_msp_elli.msp_elli_ocpi_cdr.end_date_time": "The timestamp when the session was completed/finished, charging might have finished before the session ends, for example: EV is full, but parking cost also has to be paid.",
    "01_msp_elli.msp_elli_ocpi_cdr.id": "The id of the cdr. Unique within a cpo.",
    "01_msp_elli.msp_elli_ocpi_cdr.last_updated": "Timestamp when this CDR was last updated (or created).",
    "01_msp_elli.msp_elli_ocpi_cdr.meter_id": "Identification of the Meter inside the Charge Point.",
    "01_msp_elli.msp_elli_ocpi_cdr.party_id": "ID of the CPO that owns this CDR (following the ISO-15118 standard).",
    "01_msp_elli.msp_elli_ocpi_cdr.peer_country_code": "ISO-3166 alpha-2 country code of the peer that owns this CDR.",
    "01_msp_elli.msp_elli_ocpi_cdr.peer_party_id": "ID of the peer that owns this CDR (following the ISO-15118 standard).",
    "01_msp_elli.msp_elli_ocpi_cdr.remark": "Optional remark, can be used to provide additional human readable information to the CDR.",
    "01_msp_elli.msp_elli_ocpi_cdr.session_id": "Unique ID of the Session for which this CDR is sent.",
    "01_msp_elli.msp_elli_ocpi_cdr.signed_data": "Signed data that belongs to this charging Session.",
    "01_msp_elli.msp_elli_ocpi_cdr.start_date_time": "Start timestamp of the charging session, or in-case of a reservation (before the start of a session) the start of the reservation.",
    "01_msp_elli.msp_elli_ocpi_cdr.status": "Indicates the current status of the CDR.",
    "01_msp_elli.msp_elli_ocpi_cdr.tariffs": "Tariffs.",
    "01_msp_elli.msp_elli_ocpi_cdr.total_energy": "Total energy charged, in kWh.",
    "01_msp_elli.msp_elli_ocpi_cdr.total_time": "Total duration of the charging session (including the duration of charging and not charging), in hours.",
    "01_msp_elli.msp_elli_ocpi_cdr.updated_at": "Timestamp when the CDR was last updated in Elli's backend",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.auth_method": "Method used for authentication.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.charging_periods_dimension_type": "Charging period dimension type.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.charging_periods_dimensions_volume": "Charging period dimension volume.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.charging_periods_start_time": "Charging period start date time.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.country_code": "ISO-3166 alpha-2 country code of the CPO that owns this CDR.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.created_at": "Timestamp when the cdr first arrived in Elli's backend and created an error.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.error": "Error type",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.error_detail": "Error details",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.id": "The id of the cdr. Unique within a cpo.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.last_updated": "Timestamp when this CDR was last updated (or created).",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.party_id": "ID of the CPO that owns this CDR (following the ISO-15118 standard).",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.peer_country_code": "ISO-3166 alpha-2 country code of the peer that owns this CDR.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.peer_party_id": "ID of the peer that owns this CDR (following the ISO-15118 standard).",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.remark": "Optional remark, can be used to provide additional human readable information to the CDR.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.start_date_time": "Date and time when charging session has started.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.stop_date_time": "Date and time when charging session has stopped.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.total_energy": "Total amount of energy consumed during charging session.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.total_parking_time": "Total duration of the charging session where the EV was not charging, in hours.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.total_time": "Total duration of charging session.",
    "01_msp_elli.msp_elli_ocpi_plausibility_monitor_cdr.updated_at": "Timestamp when the cdr error was last updated in Elli's backend",
    "01_msp_elli.msp_elli_oicp_cdr.calibration_law_verification_info": "This field provides additional information which could help directly or indirectly to verify the signed metering value by using respective Transparency Software.",
    "01_msp_elli.msp_elli_oicp_cdr.charging_end": "The date and time at which the charging process stopped.",
    "01_msp_elli.msp_elli_oicp_cdr.charging_start": "The date and time at which the charging process started.",
    "01_msp_elli.msp_elli_oicp_cdr.consumed_energy": "The difference between MeterValueEnd and MeterValueStart in kWh.",
    "01_msp_elli.msp_elli_oicp_cdr.cpo_partner_session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_oicp_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "01_msp_elli.msp_elli_oicp_cdr.evse_id": "The ID that identifies the charging spot.",
    "01_msp_elli.msp_elli_oicp_cdr.hub_operator_id": "Hub operator.",
    "01_msp_elli.msp_elli_oicp_cdr.hub_provider_id": "Hub provider.",
    "01_msp_elli.msp_elli_oicp_cdr.id": "The id of the cdr. Unique within a cpo.",
    "01_msp_elli.msp_elli_oicp_cdr.meter_value_end": "The ending meter value in kWh.",
    "01_msp_elli.msp_elli_oicp_cdr.meter_value_in_between": "List of meter values that may have been taken in between (kWh).",
    "01_msp_elli.msp_elli_oicp_cdr.meter_value_start": "The starting meter value in kWh.",
    "01_msp_elli.msp_elli_oicp_cdr.partner_product_id": "A pricing product name (for identifying a tariff) that must be unique.",
    "01_msp_elli.msp_elli_oicp_cdr.session_end": "The date and time at which the session ended. E. g. Swipe of RFID or Cable disconnected.",
    "01_msp_elli.msp_elli_oicp_cdr.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_oicp_cdr.session_start": "The date and time at which the session started, e.g. swipe of RFID or cable connected.",
    "01_msp_elli.msp_elli_oicp_cdr.signed_metering_values": "Metering Signature basically contains all metering signature values (these values should be in Transparency software format) for different status of charging session for eg start, end or progress. In total you can provide maximum 10 metering signature values.",
    "01_msp_elli.msp_elli_oicp_cdr.tenant_id": "Id of tenant this subscriber belongs to. Filter for the Elli tenant only.",
    "01_msp_elli.msp_elli_oicp_cdr.updated_at": "Timestamp when this CDR was updated in Elli's backend",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.calibration_law_verification_info": "This field provides additional information which could help directly or indirectly to verify the signed metering value by using respective Transparency Software.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.charging_end": "The date and time at which the charging process stopped.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.charging_start": "The date and time at which the charging process started.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.consumed_energy": "The difference between MeterValueEnd and MeterValueStart in kWh.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.cpo_partner_session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.emp_partner_session_id": "Field containing the session id assigned by an EMP to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.error_type": "CDR error type",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.evse_id": "The ID that identifies the charging spot.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.hub_operator_id": "Hub operator.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.hub_provider_id": "Hub provider.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.id": "The id of the cdr. Unique within a cpo.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.meter_value_end": "The ending meter value in kWh.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.meter_value_in_between": "List of meter values that may have been taken in between (kWh).",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.meter_value_start": "The starting meter value in kWh.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.partner_product_id": "A pricing product name (for identifying a tariff) that must be unique.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.session_start": "The date and time at which the session started, e.g. swipe of RFID or cable connected.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.signed_metering_values": "Metering Signature basically contains all metering signature values (these values should be in Transparency software format) for different status of charging session for eg start, end or progress. In total you can provide maximum 10 metering signature values.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.tenant_id": "Id of tenant this subscriber belongs to. Filter for the Elli tenant only.",
    "01_msp_elli.msp_elli_oicp_plausibility_monitor_cdr.updated_at": "Timestamp when this CDR was updated in Elli's backend.",
    "02_msp_oem.msp_oem_additional_data.free_text_1": "raw data 1",
    "02_msp_oem.msp_oem_additional_data.free_text_2": "raw data 2",
    "02_msp_oem.msp_oem_additional_data.free_text_3": "raw data 3",
    "02_msp_oem.msp_oem_additional_data.free_text_4": "raw data 4",
    "02_msp_oem.msp_oem_additional_data.free_text_5": "raw data 5",
    "02_msp_oem.msp_oem_additional_data.free_text_6": "raw data 6",
    "02_msp_oem.msp_oem_additional_data.free_text_7": "raw data 7",
    "02_msp_oem.msp_oem_additional_data.free_text_8": "raw data 8",
    "02_msp_oem.msp_oem_additional_data.id": "The id of the cdr. Unique within a cpo.",
    "02_msp_oem.msp_oem_additional_data.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "02_msp_oem.msp_oem_ocpi_cdr.auth_method": "Method used for authentication.",
    "02_msp_oem.msp_oem_ocpi_cdr.authorization_reference": "Reference to the authorization given by the eMSP.",
    "02_msp_oem.msp_oem_ocpi_cdr.charging_periods": "List of Charging Periods that make up this charging session.",
    "02_msp_oem.msp_oem_ocpi_cdr.country_code": "ISO-3166 alpha-2 country code of the CPO that owns this CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "02_msp_oem.msp_oem_ocpi_cdr.credit": "When set to true, this is a Credit CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.credit_reference_id": "Is required to be set for a Credit CDR. This SHALL contain the id of the CDR for which this is a Credit CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.currency": "Currency of the CDR in ISO 4217 Code.",
    "02_msp_oem.msp_oem_ocpi_cdr.end_date_time": "The timestamp when the session was completed/finished, charging might have finished before the session ends, for example: EV is full, but parking cost also has to be paid.",
    "02_msp_oem.msp_oem_ocpi_cdr.id": "The id of the cdr. Unique within a cpo.",
    "02_msp_oem.msp_oem_ocpi_cdr.last_updated": "Timestamp when this CDR was last updated (or created).",
    "02_msp_oem.msp_oem_ocpi_cdr.meter_id": "Identification of the Meter inside the Charge Point.",
    "02_msp_oem.msp_oem_ocpi_cdr.party_id": "ID of the CPO that owns this CDR (following the ISO-15118 standard).",
    "02_msp_oem.msp_oem_ocpi_cdr.peer_country_code": "ISO-3166 alpha-2 country code of the peer that owns this CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.peer_party_id": "ID of the peer that owns this CDR (following the ISO-15118 standard).",
    "02_msp_oem.msp_oem_ocpi_cdr.remark": "Optional remark, can be used to provide additional human readable information to the CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.session_id": "Unique ID of the Session for which this CDR is sent.",
    "02_msp_oem.msp_oem_ocpi_cdr.signed_data": "Signed data that belongs to this charging Session.",
    "02_msp_oem.msp_oem_ocpi_cdr.start_date_time": "Start timestamp of the charging session, or in-case of a reservation (before the start of a session) the start of the reservation.",
    "02_msp_oem.msp_oem_ocpi_cdr.status": "Indicates the current status of the CDR.",
    "02_msp_oem.msp_oem_ocpi_cdr.total_energy": "Total energy charged, in kWh.",
    "02_msp_oem.msp_oem_ocpi_cdr.total_parking_time": "Total duration of the charging session where the EV was not charging, in hours.",
    "02_msp_oem.msp_oem_ocpi_cdr.total_time": "Total duration of the charging session (including the duration of charging and not charging), in hours.",
    "02_msp_oem.msp_oem_ocpi_cdr.updated_at": "Timestamp when the CDR was last updated in Elli's backend",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.auth_id": "Authentication ID",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.auth_method": "Method used for authentication",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.charging_periods_dimension_type": "Charging period dimension type",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.charging_periods_dimensions_volume": "Charging period dimension volume",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.charging_periods_start_time": "Charging period start date time",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.country_code": "ISO-3166 alpha-2 country code of the CPO that owns this CDR",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.created_at": "Timestamp when the cdr first arrived in Elli's backend and created an error",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.error": "Error type",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.error_detail": "Error details",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.id": "The id of the cdr. Unique within a cpo",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.last_updated": "Timestamp when this CDR was last updated (or created)",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.party_id": "ID of the CPO that owns this CDR (following the ISO-15118 standard)",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.peer_country_code": "ISO-3166 alpha-2 country code of the peer that owns this CDR",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.peer_party_id": "ID of the peer that owns this CDR (following the ISO-15118 standard)",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.remark": "Optional remark, can be used to provide additional human readable information to the CDR",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.start_date_time": "Date and time when charging session has started",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.stop_date_time": "Date and time when charging session has stopped",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.total_energy": "Total amount of energy consumed during charging session",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.total_parking_time": "Total duration of the charging session where the EV was not charging, in hours",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.total_time": "Total duration of charging session",
    "02_msp_oem.msp_oem_ocpi_plausibility_monitor_cdr.updated_at": "Timestamp when the cdr error was last updated in Elli's backend",
    "02_msp_oem.msp_oem_oicp_cdr.calibration_law_verification_info": "This field provides additional information which could help directly or indirectly to verify the signed metering value by using respective Transparency Software.",
    "02_msp_oem.msp_oem_oicp_cdr.charging_end": "The date and time at which the charging process stopped.",
    "02_msp_oem.msp_oem_oicp_cdr.charging_start": "The date and time at which the charging process started.",
    "02_msp_oem.msp_oem_oicp_cdr.consumed_energy": "The difference between MeterValueEnd and MeterValueStart in kWh.",
    "02_msp_oem.msp_oem_oicp_cdr.cpo_partner_session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "02_msp_oem.msp_oem_oicp_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "02_msp_oem.msp_oem_oicp_cdr.evse_id": "The ID that identifies the charging spot.",
    "02_msp_oem.msp_oem_oicp_cdr.hub_operator_id": "Hub operator.",
    "02_msp_oem.msp_oem_oicp_cdr.hub_provider_id": "Hub provider.",
    "02_msp_oem.msp_oem_oicp_cdr.id": "The id of the cdr. Unique within a cpo.",
    "02_msp_oem.msp_oem_oicp_cdr.meter_value_end": "The ending meter value in kWh.",
    "02_msp_oem.msp_oem_oicp_cdr.meter_value_in_between": "List of meter values that may have been taken in between (kWh).",
    "02_msp_oem.msp_oem_oicp_cdr.meter_value_start": "The starting meter value in kWh.",
    "02_msp_oem.msp_oem_oicp_cdr.partner_product_id": "A pricing product name (for identifying a tariff) that must be unique.",
    "02_msp_oem.msp_oem_oicp_cdr.session_end": "The date and time at which the session ended. E. g. Swipe of RFID or Cable disconnected.",
    "02_msp_oem.msp_oem_oicp_cdr.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "02_msp_oem.msp_oem_oicp_cdr.signed_metering_values": "Metering Signature basically contains all metering signature values (these values should be in Transparency software format) for different status of charging session for eg start, end or progress. In total you can provide maximum 10 metering signature values.",
    "02_msp_oem.msp_oem_oicp_cdr.updated_at": "Timestamp when this CDR was updated in Elli's backend",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.calibration_law_verification_info": "This field provides additional information which could help directly or indirectly to verify the signed metering value by using respective Transparency Software.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.charging_end": "The date and time at which the charging process stopped.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.charging_start": "The date and time at which the charging process started.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.consumed_energy": "The difference between MeterValueEnd and MeterValueStart in kWh.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.cpo_partner_session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.created_at": "Timestamp when this CDR was persisted in Elli's backend.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.error_type": "CDR error type",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.evse_id": "The ID that identifies the charging spot.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.hub_operator_id": "Hub operator.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.hub_provider_id": "Hub provider.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.id": "The id of the cdr. Unique within a cpo.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.meter_value_end": "The ending meter value in kWh.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.meter_value_in_between": "List of meter values that may have been taken in between (kWh).",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.meter_value_start": "The starting meter value in kWh.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.partner_product_id": "A pricing product name (for identifying a tariff) that must be unique.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.session_end": "The date and time at which the session ended. E. g. Swipe of RFID or Cable disconnected.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.session_id": "Field containing the session id assigned by the CPO to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.session_start": "The date and time at which the session started, e.g. swipe of RFID or cable connected.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.signed_metering_values": "Metering Signature basically contains all metering signature values (these values should be in Transparency software format) for different status of charging session for eg start, end or progress. In total you can provide maximum 10 metering signature values.",
    "02_msp_oem.msp_oem_oicp_plausibility_monitor_cdr.updated_at": "Timestamp when this CDR was updated in Elli's backend.",
    "05_fleet.fleet_additional_data.fleet_id": "unique identifier of the charging station",
    "05_fleet.fleet_additional_data.info": "information description",
    "05_fleet.fleet_additional_data.info_category": "category of information",
    "05_fleet.fleet_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_additional_data.timestamp": "time when the data was recorded",
    "05_fleet.fleet_b2bwallet_scan_session.created_at": "Timestamp indicating when the scan session was created.",
    "05_fleet.fleet_b2bwallet_scan_session.iam_id": "Reference to the user who initiated the scan session.",
    "05_fleet.fleet_b2bwallet_scan_session.id": "Unique identifier for the scan session.",
    "05_fleet.fleet_b2bwallet_scan_session.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_b2bwallet_scan_session.status": "Current status of the scan session (e.g., 'started', 'waiting_for_login', 'done').",
    "05_fleet.fleet_b2bwallet_scan_session.updated_at": "Timestamp of the last update to the scan session.",
    "05_fleet.fleet_general_charging_record.auth_method": "Authentication method used (e.g., RFID, EMAID).",
    "05_fleet.fleet_general_charging_record.branch_history_id": "Reference to historical branch assignment.",
    "05_fleet.fleet_general_charging_record.correction_reference_id": "Reference to a previous corrected session.",
    "05_fleet.fleet_general_charging_record.created_at": "Timestamp when the record was created.",
    "05_fleet.fleet_general_charging_record.currency_code": "Currency in which the session cost is calculated (e.g., EUR, USD).",
    "05_fleet.fleet_general_charging_record.fleet_driver_id": "ID of the fleet driver.",
    "05_fleet.fleet_general_charging_record.fleet_id": "ID of the fleet to which this session belongs.",
    "05_fleet.fleet_general_charging_record.iam_tenant_id": "Tenant ID in IAM system.",
    "05_fleet.fleet_general_charging_record.id": "Primary key for the charging record.",
    "05_fleet.fleet_general_charging_record.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_general_charging_record.record_type": "Type of record (e.g., public, home, third_party).",
    "05_fleet.fleet_general_charging_record.start_date_time": "Start timestamp of the charging session.",
    "05_fleet.fleet_general_charging_record.stop_date_time": "Stop timestamp of the charging session.",
    "05_fleet.fleet_general_charging_record.total_energy_wh": "Total energy consumed in watt-hours.",
    "05_fleet.fleet_general_charging_record.updated_at": "Timestamp when the record was last updated.",
    "05_fleet.fleet_general_charging_record.validated_at": "Timestamp when the session was validated.",
    "05_fleet.fleet_home_charging_record_details.charging_record_id": "Primary key and foreign key linking to the general charging record.",
    "05_fleet.fleet_home_charging_record_details.created_at": "Timestamp when the record was created.",
    "05_fleet.fleet_home_charging_record_details.csms_station_id": "Unique identifier of the charging station in the CSMS system.",
    "05_fleet.fleet_home_charging_record_details.electricity_contract_id": "Reference to the electricity contract under which the session was billed.",
    "05_fleet.fleet_home_charging_record_details.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_home_charging_record_details.updated_at": "Timestamp when the record was last updated.",
    "05_fleet.fleet_public_charging_record_details.charging_record_id": "Primary key and foreign key to the general charging record.",
    "05_fleet.fleet_public_charging_record_details.created_at": "Timestamp when the record was created.",
    "05_fleet.fleet_public_charging_record_details.evse_id": "Identifier of the EVSE (Electric Vehicle Supply Equipment) used.",
    "05_fleet.fleet_public_charging_record_details.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_public_charging_record_details.power_type": "Type of charging power used (e.g., AC, DC).",
    "05_fleet.fleet_public_charging_record_details.roaming_partner": "Name of the roaming partner.",
    "05_fleet.fleet_public_charging_record_details.tariff_id": "Reference to the tariff applied to this charging session.",
    "05_fleet.fleet_public_charging_record_details.updated_at": "Timestamp when the record was last updated.",
    "05_fleet.fleet_third_party_charging_record_details.charging_record_id": "Primary key and reference to the general charging record.",
    "05_fleet.fleet_third_party_charging_record_details.created_at": "Timestamp when the record was created.",
    "05_fleet.fleet_third_party_charging_record_details.electricity_contract_id": "Reference to the electricity contract governing the session.",
    "05_fleet.fleet_third_party_charging_record_details.meter_final_reading": "Final energy reading (wh).",
    "05_fleet.fleet_third_party_charging_record_details.meter_initial_reading": "Initial energy reading (wh).",
    "05_fleet.fleet_third_party_charging_record_details.organization_iam_id": "IAM ID of the owning organization.",
    "05_fleet.fleet_third_party_charging_record_details.report_type": "Type of report associated with the session (e.g., meter_reading, wh_value).",
    "05_fleet.fleet_third_party_charging_record_details.session_type": "Type of session (e.g., single, multiple).",
    "05_fleet.fleet_third_party_charging_record_details.third_party_station_id": "ID of the third-party station where the charging took place.",
    "05_fleet.fleet_third_party_charging_record_details.updated_at": "Timestamp when the record was last updated.",
    "07_wallbox_elli.wallbox_elli_app_additional_data.iam_id": "ID of the user",
    "07_wallbox_elli.wallbox_elli_app_additional_data.info": "information description",
    "07_wallbox_elli.wallbox_elli_app_additional_data.info_category": "category of information",
    "07_wallbox_elli.wallbox_elli_app_additional_data.timestamp": "time when the data was recorded",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.app_orientation": "App's UI orientation at crash time",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.application": "App version details",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.blame_frame": "Stack frame suspected to have caused crash",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.breadcrumbs": "App events before crash for debugging",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.bundle_identifier": "App's bundle identifier or package name",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.crashlytics_sdk_version": "Version of Crashlytics SDK used",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.custom_keys": "Custom app-specific debug values",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.device": "Information about the user's device",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.device_orientation": "Physical orientation of the device",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.error_type": "High-level category of error",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.errors": "Non-fatal errors that occurred",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.event_id": "Unique identifier for the crash or event",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.event_timestamp": "Time when the crash/event occurred",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.exceptions": "Exception stack traces",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.iam_id": "User metadata at time of crash",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.installation_uuid": "Unique app installation ID",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.issue_id": "Unique identifier for the issue in Crashlytics",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.issue_subtitle": "Short summary or context of the issue",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.issue_title": "Title describing the issue",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.logs": "App logs recorded near time of crash",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.memory": "Memory usage details at the time of crash",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.native_crash_info": "Info for native-level crashes",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.operating_system": "Details of the OS running the app",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.platform": "Platform on which the app is running (e.g. iOS or Android)",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.process_state": "State of the app process at crash time",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.received_timestamp": "Time when the event was received by backend",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.remote_config_feature_rollouts": "Remote Config rollout data",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.storage": "Storage usage at the time of crash",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.threads": "Thread information at crash time",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.unity_metadata": "Unity-specific device and runtime details",
    "07_wallbox_elli.wallbox_elli_app_crashlytics.variant_id": "Experiment or rollout variant ID",
    "07_wallbox_elli.wallbox_elli_app_feedback.categories": "Feedback categories (can be multiple)",
    "07_wallbox_elli.wallbox_elli_app_feedback.content": "The actual feedback text",
    "07_wallbox_elli.wallbox_elli_app_feedback.created_at": "Record creation timestamp",
    "07_wallbox_elli.wallbox_elli_app_feedback.iam_id": "ID of the user giving feedback",
    "07_wallbox_elli.wallbox_elli_app_feedback.id": "Unique feedback ID",
    "07_wallbox_elli.wallbox_elli_app_feedback.updated_at": "Last update timestamp",
    "07_wallbox_elli.wallbox_elli_app_ratings.iam_id": "ID of the user giving the rating",
    "07_wallbox_elli.wallbox_elli_app_ratings.id": "Unique rating ID",
    "07_wallbox_elli.wallbox_elli_app_ratings.rating": "Numerical rating (e.g., 1 -5)",
    "07_wallbox_elli.wallbox_elli_app_ratings.updated_at": "Last update timestamp",
    "07_wallbox_elli.wallbox_elli_app_satisfaction_feedback.created_at": "Record creation timestamp",
    "07_wallbox_elli.wallbox_elli_app_satisfaction_feedback.iam_id": "ID of the user",
    "07_wallbox_elli.wallbox_elli_app_satisfaction_feedback.id": "Unique record ID",
    "07_wallbox_elli.wallbox_elli_app_satisfaction_feedback.satisfied": "Whether the user is satisfied",
    "07_wallbox_elli.wallbox_elli_app_satisfaction_feedback.updated_at": "Last update timestamp",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.info": "information description",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.info_category": "category of information",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.station_id": "The id of the Station wallbox.",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_additional_data.timestamp": "Timestamp of creation of this record",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.authentication_method": "private_card_owned, private_remote or fleet_card",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.authorization_provider_id": "id of the services that authorized the session",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.charging_session_id": "Id of the charging session",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.created_at": "The date when this database record was created in this database",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.fault_cause": "reason why the session faulted",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.iam_id": "id of the database record of the user",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.id": "Id of this database record",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.rfid_card_id": "id of the RFID card used for authorization",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.rfid_card_label": "label of the RFID card used for authorization",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.start_date_time": "session start time",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.station_id": "Id of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.stop_date_time": "session end time",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.total_energy_wh": "total charged energy",
    "07_wallbox_elli.wallbox_elli_hss_charging_records.updated_at": "The date when this database record was last updated.",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.info": "information description",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.info_category": "category of information",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.station_id": "The id of the Station wallbox.",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_additional_data.timestamp": "Timestamp of creation of this record",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.charging_profile_id": "Charging profile as configured on the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.message_id": "The unique ID of the PubSub message",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.publish_time": "Timestamp when the PubSub message was published",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_cleared_events.timestamp": "Timestamp when charging profile was cleared",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.charging_profile_charging_schedule": "List of charging periods",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.charging_profile_id": "Charging profile as configured on the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.charging_profile_kind": "Indicates the kind of schedule",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.charging_profile_purpose": "Defines the purpose of the schedule transferred by this profile",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.charging_profile_stack_level": "Value determining level in hierarchy stack of profiles",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.message_id": "The unique ID of the PubSub message",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.ocpp_evse_id": "Station evse id",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.publish_time": "Timestamp when the PubSub message was published",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profile_set_events.timestamp": "Timestamp when charging profile was set",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.message_id": "The unique ID of the PubSub message",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.publish_time": "Timestamp when the PubSub message was published",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_profiles_reset_events.timestamp": "Timestamp when charging profile was cleared",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authentication_method": "Method how the charging session was authorized (unstable, will be removed at some point)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authorization_id_token_type": "Type of the ID token used to authorize the charging session, one of \"Central\", \"eMAID\", \"ISO14443\", \"ISO15693\", \"KeyCode\", \"Local\", \"MacAddress\", \"NoAuthorization\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authorization_mode": "Authorization mode of the charging station at the time the session was authorized, one of \"no_authorization_cs\", \"no_authorization_csms\", \"authorization_csms\", \"authorization_e_roaming \" (unstable, will be removed at some point)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authorization_provider_id": "ID of the authorization provider that authorized the charging session",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authorization_provider_name": "Name of the authorization provider that authorized the charging session",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.authorization_provider_reference": "Reference given by of the authorization provider that authorized the charging session",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.cache_expiry_date": "Timestamp when the authorization cache expires in the charging station, only relevant for OCPP 1.6 (operational data, not relevant for analytics)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.charging_state": "Charging state of the session as reported by the station, one of \"Charging\", \"EVDetected\", \"SuspendedEV\", \"SuspendedEVSE\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.created_at": "Timestamp when the session was created in the CSMS",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.e_roaming_authorization_reference": "Authorization reference provided by MSP (deprecated, use authorization_provider_re ference instead)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.e_roaming_token_contract_id": "Contract ID provided by MSP (deprecated)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.e_roaming_token_type": "Type of the ID token provided by MSP, one of \"RFID\", \"APP_USER\" (deprecated, use authorization_id_token_ty pe instead)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.expiry_date": "Timestamp when the authorization of the charging session will expire (operational data, not relevant for analytics)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.fault_cause": "Reason why charging session has lifecycle state \"Faulted\", one of \"negative_duration\", \"negative_energy_consu mption\", \"closed_by_new_session\" , \"missing_country_for_cpo _price\", \"missing_country_for_tot al_compensation\", \"terminated_by_agent\", \"terminated_by_system\", \"missing_meter_value\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.id": "Unique ID of the charging session in the CSMS",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.last_meter_value_timestamp": "Timestamp when last meter value was received",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.last_meter_value_wh": "Latest meter value between start and end of the session in Watt hours (Wh)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.lifecycle": "Lifecycle of the charging session as modelled in the CSMS, one of \"CablePluggedIn\", \"Active\", \"Closed\", \"Faulted\", \"AuthorizationHandled\", \"Aborted\", \"PreAuthorizationHandle d\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.ocmf_data": "Signed ocmf data provided by some stations (operational data, not relevant for analytics)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.ocpp_connector_id": "ID of the connector related to the charging session as defined by OCPP",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.ocpp_evse_id": "ID of the EVSE related to the charging session as defined by OCPP",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.ocpp_transaction_id": "ID of the charging session used by the charging station as defined by OCPP",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.offline": "EXPERIMENTAL, might be removed again in the future if it turns out to be unreliable! This flag indicates whether a session was marked as offline by the charging station. It defaults to false for OCPP 2.0.x (except for authorization-handled sessions in some cases) and will always be undefined for OCPP 1.6.",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.rfid_card_id": "ID of the RFID card that was used to authorize the charging session (deprecated)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.rfid_card_label": "Label of the RFID card that was used to authorize the charging session (deprecated)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.start_date_time": "Timestamp when the session started at the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.start_meter_value_wh": "Meter value at the start of the session in Watt hours (Wh)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.start_reason": "Start (trigger) reason provided by station on session start",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.station_id": "ID of the charging station related to the session",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.station_serial_number": "Serial number of the charging station as defined by the vendor",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.stop_date_time": "Timestamp when the session stopped at the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.stop_meter_value_wh": "Meter value at the end of the session in Watt hours (Wh)",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.stop_reason": "Stop reason provided by station on session end",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.trigger_reason": "Trigger reason provided by station on session end",
    "07_wallbox_elli.wallbox_elli_hss_csms_charging_session.updated_at": "Timestamp when the session was last updated",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.connection_state": "Indicates the event_type, one of \"connected\", \"disconnected\" or \"connected_in_quarantin e_mode\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.message_id": "The unique ID of the PubSub message",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.model": "Model of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_connection_state_changed_events.timestamp": "Station disconnection timestamp",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.error_code": "OCPP error codes in case of failed connector status changes",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.error_info": "Additional free format information about the error",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.firmware_version": "Version of firmware installed on charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.message_id": "The unique ID of the PubSub message",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.model": "Model of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.ocpp_connector_id": "Station connector id",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.ocpp_evse_id": "Station evse id",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.reported_timestamp": "Timestamp reported by the station about the status change",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.status": "Status value of station connector",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.timestamp": "Station connector state change timestamp. Time of receipt of the OCPP message from CSMS",
    "07_wallbox_elli.wallbox_elli_hss_csms_connector_status_changed_events.vendor_error_code": "A vendor-specific error code",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.action": "OCPP action that was performed",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.device_twin_id": "The unique id of the device twin of the station (unclear prospect, not exposing for analytics)",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.error_code": "Error code returned by the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.model_name": "Name of the charging station model",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.ocpp_protocol": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.response_type": "Response type returned by the station",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.station_id": "ID of the charging station which sends the status notification",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_ocpp_errors.timestamp": "Timestamp when the error event was logged",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.http_status_code": "The status code of the HTTP request in SCS",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.scs_endpoint": "The timestamp of the contact data point",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.station_id": "ID of the charging station related to the client certificate info",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_scs_station_contact.timestamp": "The timestamp of the contact data point",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.model_name": "Name of the charging station model",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.ocpp_version": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.serial_number": "The serial number of the charging station as defined by the vendor",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.station_id": "ID of the charging station which sends the status notification",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_csms_status_notifications.timestamp": "Timestamp when the status notification was reported by the station",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.info": "information description",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.info_category": "category of information",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.station_id": "The id of the Station wallbox.",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_fws_additional_data.timestamp": "Timestamp of creation of this record",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.allow_auto_update": "Boolean flag indicating if auto update is allowed.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.allow_auto_update_changed_by": "Identifier for who changed the auto-update flag.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.allow_auto_update_last_changed": "Timestamp when the auto-update flag was last changed.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.channel": "Channel identifier stored as a STRING (UUID).",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.connected": "Boolean flag indicating if the station is connected.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.connection_changed_at": "Timestamp of the last connection change.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.current_firmware_version": "Reference to the current firmware version, stored as text.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.external_station_uuid": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.goto_firmware_version": "Reference to the firmware version to go to, stored as text.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.model": "Model identifier stored as a STRING (UUID).",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.occ_version": "Occurrence version, default is 0.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.product": "Reference to the product id.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.product_updated_at": "Timestamp when the product was last updated.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.serial_number": "Serial number of the station.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.station_id": "station id for which the firmware update was attempted",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.version_string": "Version string of the station.",
    "07_wallbox_elli.wallbox_elli_hss_fws_stations.version_string_changed_at": "Timestamp when the version string was changed.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.failed_reason": "Reason for failure, if any.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.finished_at": "Timestamp when the update attempt finished.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.id": "Primary key stored as STRING (UUID).",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.occ_version": "Occurrence version number, default is 0.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.request_id": "Unique request id (SERIAL in source, numeric).",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.started_at": "Timestamp when the update attempt started.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.success": "Boolean flag indicating if the update was successful.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.update_process": "Reference to update_process id.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_attempts.valid": "Boolean flag indicating if the row is valid.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.created_by": "Identifier of the user who created the update process.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.finished_at": "Timestamp when the update process finished.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.goto_firmware": "Reference to the firmware id.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.id": "Primary key",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.next_check": "Timestamp for the next check of the update process.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.occ_version": "Occurrence version number, default is 0.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.success": "Boolean flag indicating if the update process succeeded.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.terminated_by": "Identifier of who terminated the update process.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.termination_reason": "Reason for termination of the update process.",
    "07_wallbox_elli.wallbox_elli_hss_fws_update_process.triggered_at": "Timestamp when the update process was triggered.",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.info": "information description",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.info_category": "category of information",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.station_id": "The id of the Station wallbox.",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_scs_additional_data.timestamp": "Timestamp of creation of this record",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.attribute_status": "Attribute Status stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.component_instance": "Component Instance stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.component_name": "Component Name stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.fetched_at": "Timestamp when the configurations was fetchedAt",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.id": "Primary key",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.ocpp_type": "Ocpp Version stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.read_only": "Mark configuration as readOnly",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.revision": "An integer value representing the revision number.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.value": "Value stored as String",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.variable_instance": "Variable Instance stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_configurations.variable_name": "Variable Name stored as String.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.channel_id": "A UUID representing the Channel Id, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.channel_id_changed_at": "Timestamp when the channel Id was changed.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.channel_id_data_source_owner_revision": "An integer value representing the channelId Data Source Owner Revision number.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.connected": "Boolean flag indicating if the station is connected.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.connection_changed_at": "Timestamp when the connection was changed.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.connection_source": "Connection Source, stored as text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.last_booted_at": "Timestamp when the Station was last booted.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.model_id": "A UUID representing the station Model Id, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.reported_firmware_version": "Reported firmware version, stored as text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.reported_firmware_version_changed_at": "Timestamp when the reported Firmware Version was changed.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.revision": "An integer value representing the revision number.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.serial_number": "Station serial number",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.station_created_at": "Timestamp when the Station was created.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.station_deleted_at": "Timestamp when the Station was deleted.",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_scs_station_information.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.finished_at": "Timestamp when the update process finished.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.id": "Primary key",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.result": "A Json representing the update attemp result, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.revision": "An integer value representing the revision number.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.state": "Update attempt state, stored as text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.station_id": "A UUID representing the station, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.triggered_at": "Timestamp when the update attempt was triggered.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_attempts.update_process_id": "A UUID representing the station, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.configuration_to_rollout": "A Json representing the configuration To Rollout, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.finished_at": "Timestamp when the update process finished.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.id": "Primary key",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.station_id": "station id for which the firmware update was attempted",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.success": "Boolean flag indicating if the process finished successfully.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.target_configuration_id": "A UUID representing the station Target Configuration Id, converted to Text.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_scs_update_process.triggered_at": "Timestamp when the update process was triggered.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.dtc_code": "Diagnostic trouble code as text.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.id": "Primary key",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.resolved_at": "Timestamp when the event was resolved. This field is nullable.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.severity": "Severity level (converted from enum to STRING).",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.started_at": "Timestamp when the event started.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_error_histories.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.iam_id": "Id of the user of the wallbox",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.last_update_from_station": "Timestamp indicating when the station was last updated.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.revision": "An integer value representing the revision number.",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.severity": "Severity level (converted from enum to STRING).",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.station_id": "Unique ID of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_sds_station_information.tenant_id": "The tenant id of the user.",
    "07_wallbox_elli.wallbox_elli_hss_station_events.created_at": "The date when this database record was created in this database",
    "07_wallbox_elli.wallbox_elli_hss_station_events.event_type": "claim or unclaim",
    "07_wallbox_elli.wallbox_elli_hss_station_events.finished_at": "time when the claim or unclaim process finished",
    "07_wallbox_elli.wallbox_elli_hss_station_events.iam_id": "id of the database record of the user",
    "07_wallbox_elli.wallbox_elli_hss_station_events.id": "id of this database record",
    "07_wallbox_elli.wallbox_elli_hss_station_events.started_at": "time when the user made the request to claim or unlcaim",
    "07_wallbox_elli.wallbox_elli_hss_station_events.station_id": "Id of the charging station",
    "07_wallbox_elli.wallbox_elli_hss_station_events.tenant_id": "The tenant id of the user",
    "07_wallbox_elli.wallbox_elli_hss_station_events.updated_at": "The date when this database record was last updated.",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.data": "Raw data of actual event",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.document_id": "Id",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.document_name": "Full name",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.event_id": "Id of the last update",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.iam_id": "unique id of the database record of the user",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.operation": "One of CREATE, UPDATE, IMPORT.",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.station_id": "ID of the charging station",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.tenant_id": "The tenant id of the user",
    "07_wallbox_elli.wallbox_elli_scp_scheduler_events.timestamp": "Timestamp of the last update",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.info": "information description",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.info_category": "category of information",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.station_id": "The id of the Station wallbox.",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.tenant_id": "The tenant id of the user",
    "08_wallbox_oem.wallbox_oem_hss_additional_data.timestamp": "Timestamp of creation of this record",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.authentication_method": "private_card_owned, private_remote or fleet_card",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.authorization_provider_id": "id of the services that authorized the session",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.charging_session_id": "Id of the charging session",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.created_at": "The date when this database record was created in this database",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.fault_cause": "reason why the session faulted",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.fleet_organization_iam_id": "id of the corresponding fleet",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.iam_id": "id of the database record of the user",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.id": "Id of this database record",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.rfid_card_id": "id of the RFID card used for authorization",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.rfid_card_label": "label of the RFID card used for authorization",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.start_date_time": "session start time",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.station_id": "Id of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.stop_date_time": "session end time",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.total_energy_wh": "total charged energy",
    "08_wallbox_oem.wallbox_oem_hss_charging_records.updated_at": "The date when this database record was last updated.",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.info": "information description",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.info_category": "category of information",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.station_id": "The id of the Station wallbox.",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_additional_data.timestamp": "Timestamp of creation of this record",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.charging_profile_id": "Charging profile as configured on the station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.message_id": "The unique ID of the PubSub message",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.publish_time": "Timestamp when the PubSub message was published",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_cleared_events.timestamp": "Timestamp when charging profile was cleared",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.charging_profile_charging_schedule": "List of charging periods",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.charging_profile_id": "Charging profile as configured on the station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.charging_profile_kind": "Indicates the kind of schedule",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.charging_profile_purpose": "Defines the purpose of the schedule transferred by this profile",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.charging_profile_stack_level": "Value determining level in hierarchy stack of profiles",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.message_id": "The unique ID of the PubSub message",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.ocpp_evse_id": "Station evse id",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.publish_time": "Timestamp when the PubSub message was published",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profile_set_events.timestamp": "Timestamp when charging profile was set",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.message_id": "The unique ID of the PubSub message",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.publish_time": "Timestamp when the PubSub message was published",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_profiles_reset_events.timestamp": "Timestamp when charging profile was cleared",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authentication_method": "Method how the charging session was authorized (unstable, will be removed at some point)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_id_token_type": "Type of the ID token used to authorize the charging session, one of \"Central\", \"eMAID\", \"ISO14443\", \"ISO15693\", \"KeyCode\", \"Local\", \"MacAddress\", \"NoAuthorization\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_mode": "Authorization mode of the charging station at the time the session was authorized, one of \"no_authorization_cs\", \"no_authorization_csms\", \"authorization_csms\", \"authorization_e_roaming \" (unstable, will be removed at some point)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_provider_id": "ID of the authorization provider that authorized the charging session",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_provider_name": "Name of the authorization provider that authorized the charging session",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_provider_reference": "Reference given by of the authorization provider that authorized the charging session",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.authorization_result": "Authorization result for the charging session, one of \"Accepted\", \"Invalid\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.cache_expiry_date": "Timestamp when the authorization cache expires in the charging station, only relevant for OCPP 1.6 (operational data, not relevant for analytics)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.charging_state": "Charging state of the session as reported by the station, one of \"Charging\", \"EVDetected\", \"SuspendedEV\", \"SuspendedEVSE\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.created_at": "Timestamp when the session was created in the CSMS",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.e_roaming_authorization_reference": "Authorization reference provided by MSP (deprecated, use authorization_provider_re ference instead)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.e_roaming_token_contract_id": "Contract ID provided by MSP (deprecated)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.e_roaming_token_type": "Type of the ID token provided by MSP, one of \"RFID\", \"APP_USER\" (deprecated, use authorization_id_token_ty pe instead)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.expiry_date": "Timestamp when the authorization of the charging session will expire (operational data, not relevant for analytics)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.fault_cause": "Reason why charging session has lifecycle state \"Faulted\", one of \"negative_duration\", \"negative_energy_consu mption\", \"closed_by_new_session\" , \"missing_country_for_cpo _price\", \"missing_country_for_tot al_compensation\", \"terminated_by_agent\", \"terminated_by_system\", \"missing_meter_value\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.id": "Unique ID of the charging session in the CSMS",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.last_meter_value_timestamp": "Timestamp when last meter value was received",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.last_meter_value_wh": "Latest meter value between start and end of the session in Watt hours (Wh)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.lifecycle": "Lifecycle of the charging session as modelled in the CSMS, one of \"CablePluggedIn\", \"Active\", \"Closed\", \"Faulted\", \"AuthorizationHandled\", \"Aborted\", \"PreAuthorizationHandle d\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.ocmf_data": "Signed ocmf data provided by some stations (operational data, not relevant for analytics)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.ocpp_connector_id": "ID of the connector related to the charging session as defined by OCPP",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.ocpp_evse_id": "ID of the EVSE related to the charging session as defined by OCPP",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.ocpp_transaction_id": "ID of the charging session used by the charging station as defined by OCPP",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.offline": "EXPERIMENTAL, might be removed again in the future if it turns out to be unreliable! This flag indicates whether a session was marked as offline by the charging station. It defaults to false for OCPP 2.0.x (except for authorization-handled sessions in some cases) and will always be undefined for OCPP 1.6.",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.rfid_card_id": "ID of the RFID card that was used to authorize the charging session (deprecated)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.rfid_card_label": "Label of the RFID card that was used to authorize the charging session (deprecated)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.start_meter_value_wh": "Meter value at the start of the session in Watt hours (Wh)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.start_reason": "Start (trigger) reason provided by station on session start",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.station_id": "ID of the charging station related to the session",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.station_serial_number": "Serial number of the charging station as defined by the vendor",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.stop_date_time": "Timestamp when the session stopped at the station",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.stop_meter_value_wh": "Meter value at the end of the session in Watt hours (Wh)",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.stop_reason": "Stop reason provided by station on session end",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.trigger_reason": "Trigger reason provided by station on session end",
    "08_wallbox_oem.wallbox_oem_hss_csms_charging_session.updated_at": "Timestamp when the session was last updated",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.connection_state": "Indicates the event_type, one of \"connected\", \"disconnected\" or \"connected_in_quarantin e_mode\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.message_id": "The unique ID of the PubSub message",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.model": "Model of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_connection_state_changed_events.timestamp": "Station disconnection timestamp",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.error_code": "OCPP error codes in case of failed connector status changes",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.error_info": "Additional free format information about the error",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.firmware_version": "Version of firmware installed on charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.message_id": "The unique ID of the PubSub message",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.model": "Model of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.ocpp_connector_id": "Station connector id",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.ocpp_evse_id": "Station evse id",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.reported_timestamp": "Timestamp reported by the station about the status change",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.status": "Status value of station connector",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.timestamp": "Station connector state change timestamp. Time of receipt of the OCPP message from CSMS",
    "08_wallbox_oem.wallbox_oem_hss_csms_connector_status_changed_events.vendor_error_code": "A vendor-specific error code",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.action": "OCPP action that was performed",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.device_twin_id": "The unique id of the device twin of the station (unclear prospect, not exposing for analytics)",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.error_code": "Error code returned by the station",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.model_name": "Name of the charging station model",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.ocpp_protocol": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.response_type": "Response type returned by the station",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.station_id": "ID of the charging station which sends the status notification",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_ocpp_errors.timestamp": "Timestamp when the error event was logged",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.http_status_code": "The status code of the HTTP request in SCS",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.scs_endpoint": "The timestamp of the contact data point",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.station_id": "ID of the charging station related to the client certificate info",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_scs_station_contact.timestamp": "The timestamp of the contact data point",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.model_name": "Name of the charging station model",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.ocpp_version": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.serial_number": "The serial number of the charging station as defined by the vendor",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.station_id": "ID of the charging station which sends the status notification",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_csms_status_notifications.timestamp": "Timestamp when the status notification was reported by the station",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.info": "information description",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.info_category": "category of information",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.station_id": "The id of the Station wallbox.",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.tenant_id": "The tenant id of the user",
    "08_wallbox_oem.wallbox_oem_hss_fws_additional_data.timestamp": "Timestamp of creation of this record",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.allow_auto_update": "Boolean flag indicating if auto update is allowed.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.allow_auto_update_changed_by": "Identifier for who changed the auto-update flag.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.allow_auto_update_last_changed": "Timestamp when the auto-update flag was last changed.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.channel": "Channel identifier stored as a STRING (UUID).",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.connected": "Boolean flag indicating if the station is connected.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.connection_changed_at": "Timestamp of the last connection change.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.current_firmware_version": "Reference to the current firmware version, stored as text.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.external_station_uuid": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.goto_firmware_version": "Reference to the firmware version to go to, stored as text.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.model": "Model identifier stored as a STRING (UUID).",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.occ_version": "Occurrence version, default is 0.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.product": "Reference to the product id.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.product_updated_at": "Timestamp when the product was last updated.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.serial_number": "Serial number of the station.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.station_id": "station id for which the firmware update was attempted",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.version_string": "Version string of the station.",
    "08_wallbox_oem.wallbox_oem_hss_fws_stations.version_string_changed_at": "Timestamp when the version string was changed.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.failed_reason": "Reason for failure, if any.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.finished_at": "Timestamp when the update attempt finished.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.id": "Primary key stored as STRING (UUID).",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.occ_version": "Occurrence version number, default is 0.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.request_id": "Unique request id (SERIAL in source, numeric).",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.started_at": "Timestamp when the update attempt started.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.success": "Boolean flag indicating if the update was successful.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.update_process": "Reference to update_process id.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_attempts.valid": "Boolean flag indicating if the row is valid.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.created_by": "Identifier of the user who created the update process.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.finished_at": "Timestamp when the update process finished.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.goto_firmware": "Reference to the firmware id.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.id": "Primary key",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.next_check": "Timestamp for the next check of the update process.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.occ_version": "Occurrence version number, default is 0.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.success": "Boolean flag indicating if the update process succeeded.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.terminated_by": "Identifier of who terminated the update process.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.termination_reason": "Reason for termination of the update process.",
    "08_wallbox_oem.wallbox_oem_hss_fws_update_process.triggered_at": "Timestamp when the update process was triggered.",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.info": "information description",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.info_category": "category of information",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.station_id": "The id of the Station wallbox.",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.tenant_id": "The tenant id of the user",
    "08_wallbox_oem.wallbox_oem_hss_scs_additional_data.timestamp": "Timestamp of creation of this record",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.attribute_status": "Attribute Status stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.component_instance": "Component Instance stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.component_name": "Component Name stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.fetched_at": "Timestamp when the configurations was fetchedAt",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.id": "Primary key",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.ocpp_type": "Ocpp Version stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.read_only": "Mark configuration as readOnly",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.revision": "An integer value representing the revision number.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.station_id": "A UUID representing the station",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.value": "Value stored as String",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.variable_instance": "Variable Instance stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_configurations.variable_name": "Variable Name stored as String.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.channel_id": "A UUID representing the Channel Id, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.channel_id_changed_at": "Timestamp when the channel Id was changed.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.channel_id_data_source_owner_revision": "An integer value representing the channelId Data Source Owner Revision number.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.connected": "Boolean flag indicating if the station is connected.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.connection_changed_at": "Timestamp when the connection was changed.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.connection_source": "Connection Source, stored as text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.last_booted_at": "Timestamp when the Station was last booted.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.model_id": "A UUID representing the station Model Id, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.reported_firmware_version": "Reported firmware version, stored as text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.reported_firmware_version_changed_at": "Timestamp when the reported Firmware Version was changed.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.revision": "An integer value representing the revision number.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.serial_number": "Station serial number",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.station_created_at": "Timestamp when the Station was created.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.station_deleted_at": "Timestamp when the Station was deleted.",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_scs_station_information.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.finished_at": "Timestamp when the update process finished.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.id": "Primary key",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.result": "A Json representing the update attemp result, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.revision": "An integer value representing the revision number.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.state": "Update attempt state, stored as text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.triggered_at": "Timestamp when the update attempt was triggered.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_attempts.update_process_id": "A UUID representing the station, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.configuration_to_rollout": "A Json representing the configuration To Rollout, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.finished_at": "Timestamp when the update process finished.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.id": "Primary key",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.success": "Boolean flag indicating if the process finished successfully.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.target_configuration_id": "A UUID representing the station Target Configuration Id, converted to Text.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_scs_update_process.triggered_at": "Timestamp when the update process was triggered.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.dtc_code": "Diagnostic trouble code as text.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.id": "Primary key",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.resolved_at": "Timestamp when the event was resolved. This field is nullable.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.severity": "Severity level (converted from enum to STRING).",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.started_at": "Timestamp when the event started.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_error_histories.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.iam_id": "Id of the user of the wallbox",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.last_update_from_station": "Timestamp indicating when the station was last updated.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.revision": "An integer value representing the revision number.",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.severity": "Severity level (converted from enum to STRING).",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.station_id": "Unique ID of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_sds_station_information.tenant_id": "The tenant id of the user.",
    "08_wallbox_oem.wallbox_oem_hss_station_events.created_at": "The date when this database record was created in this database",
    "08_wallbox_oem.wallbox_oem_hss_station_events.event_type": "claim or unclaim",
    "08_wallbox_oem.wallbox_oem_hss_station_events.finished_at": "time when the claim or unclaim process finished",
    "08_wallbox_oem.wallbox_oem_hss_station_events.iam_id": "id of the database record of the user",
    "08_wallbox_oem.wallbox_oem_hss_station_events.id": "id of this database record",
    "08_wallbox_oem.wallbox_oem_hss_station_events.started_at": "time when the user made the request to claim or unlcaim",
    "08_wallbox_oem.wallbox_oem_hss_station_events.station_id": "Id of the charging station",
    "08_wallbox_oem.wallbox_oem_hss_station_events.tenant_id": "The tenant id of the user",
    "08_wallbox_oem.wallbox_oem_hss_station_events.updated_at": "The date when this database record was last updated.",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.data": "Raw data of actual event",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.document_id": "Id",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.document_name": "Full name",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.event_id": "Id of the last update",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.iam_id": "unique id of the database record of the user",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.operation": "One of CREATE, UPDATE, IMPORT.",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.station_id": "ID of the charging station",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.tenant_id": "The tenant id of the user",
    "08_wallbox_oem.wallbox_oem_scp_scheduler_events.timestamp": "Timestamp of the last update",
    "09_11_csm.csm_additional_data.info": "information description",
    "09_11_csm.csm_additional_data.info_category": "category of information",
    "09_11_csm.csm_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_additional_data.station_id": "unique identifier of the charging station",
    "09_11_csm.csm_additional_data.timestamp": "time when the data was recorded",
    "09_11_csm.csm_charging_records.accessibility_type": "Accessibility type of the EVSE according to OICP 2.3, i.e. FREE_PUBLICLY_ACCESS IBLE, RESTRICTED_ACCESS, PAYING_PUBLICLY_ACCE SSIBLE, TEST_STATION.",
    "09_11_csm.csm_charging_records.authentication_method": "Authentication method used to authenticate the charging session.",
    "09_11_csm.csm_charging_records.authorization_mode": "Station authorization mode at the time this resource was created.",
    "09_11_csm.csm_charging_records.authorized_by_iam_id": "ID of organization owning the card used to authorize a session. Only set if authorized with a paired Elli card.",
    "09_11_csm.csm_charging_records.authorized_by_iam_type": "\"Organization\" or null, only set if authorized with a paired Elli card.",
    "09_11_csm.csm_charging_records.charging_connector": "Charging connector used during the charging session.",
    "09_11_csm.csm_charging_records.charging_record_id": "The primary identifier of a charging record.",
    "09_11_csm.csm_charging_records.charging_session_id": "The primary identifier of a charging session.",
    "09_11_csm.csm_charging_records.charging_spot": "Charging spot, ID of the EVSE in relation to the station (typically 1 and 2).",
    "09_11_csm.csm_charging_records.compensation_base_rate": "Compensation rate used to compute total compensation amount in Eurocent (minor unit) per kWh.",
    "09_11_csm.csm_charging_records.compensation_currency": "Currency denoting the compensation amount.",
    "09_11_csm.csm_charging_records.compensation_minor_unit": "Total site-operator compensation amount in Eurocent (minor unit).",
    "09_11_csm.csm_charging_records.contract_type": "CSM contract type of owning organization.",
    "09_11_csm.csm_charging_records.created_at": "Creation timestamp of this entity.",
    "09_11_csm.csm_charging_records.credit_memo_id": "Credit Memo ID in ESS a charging record was compensated in.",
    "09_11_csm.csm_charging_records.duration_seconds": "Session duration in seconds.",
    "09_11_csm.csm_charging_records.e_roaming_authorization_reference": "Reference to the authorization event for the underlying charging session in E Roaming.",
    "09_11_csm.csm_charging_records.e_roaming_token_type": "\"APP_USER\" if remote start, \"RIFD\" if authorized with an RFID card.",
    "09_11_csm.csm_charging_records.employee_token": "Token used for Employee Charging.",
    "09_11_csm.csm_charging_records.energy_wh": "Energy consumed during the charging session.",
    "09_11_csm.csm_charging_records.fault_cause": "Fault cause why the underlying charging session faulted.",
    "09_11_csm.csm_charging_records.organization_branch_id": "Organization branch ID this resource is assigned to.",
    "09_11_csm.csm_charging_records.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_charging_records.organization_type": "Organization type of the the owning organization.",
    "09_11_csm.csm_charging_records.power_type": "Power type of the charging point (AC or DC).",
    "09_11_csm.csm_charging_records.public_evse_id": "Public EVSE ID of the charging point. (Sometimes referred to as OCPI EVSE ID in other systems)",
    "09_11_csm.csm_charging_records.session_faulted": "Boolean flag whether the underlying charging session has faulted.",
    "09_11_csm.csm_charging_records.session_start_date": "Start timestamp of the underlying charging session.",
    "09_11_csm.csm_charging_records.session_stop_date": "Stop timestamp of the underlying charging session.",
    "09_11_csm.csm_charging_records.signed_data": "Signed charging session data for Eichrecht according to SAFE OCMF.",
    "09_11_csm.csm_charging_records.signed_data_lem": "Signed charging session data for Eichrecht specific to LEM meters.",
    "09_11_csm.csm_charging_records.station_id": "The primary identifier of a charging station.",
    "09_11_csm.csm_charging_records.station_model": "Internal/technical station model name.",
    "09_11_csm.csm_charging_records.station_name": "Station name of the charging station in the ESS.",
    "09_11_csm.csm_charging_records.station_serial_number": "Serial number of the charging station a charging session has happened.",
    "09_11_csm.csm_charging_records.updated_at": "Last update timestamp of this entity.",
    "09_11_csm.csm_charging_session_authorizations.authentication_method": "Authentication method used to authenticate the charging session.",
    "09_11_csm.csm_charging_session_authorizations.authorization_cache_expiry_date": "Timestamp until when the station may cache the accepted authorization.",
    "09_11_csm.csm_charging_session_authorizations.authorization_expiry_date": "Timestamp when the authorization of the session expires (OCPP 1.6 only).",
    "09_11_csm.csm_charging_session_authorizations.authorization_id_token_type": "The type of the authorization ID token.",
    "09_11_csm.csm_charging_session_authorizations.authorization_provider_id": "Unique identifier of the authorization provider.",
    "09_11_csm.csm_charging_session_authorizations.authorization_provider_name": "Human-readable name of the authorization provider.",
    "09_11_csm.csm_charging_session_authorizations.authorization_result": "Whether the authorization was accepted or invalid.",
    "09_11_csm.csm_charging_session_authorizations.charging_session_authorization_id": "Unique identifier of the charging session authorization.",
    "09_11_csm.csm_charging_session_authorizations.created_at": "Creation timestamp of this entity.",
    "09_11_csm.csm_charging_session_authorizations.e_roaming_authorization_reference": "Reference to the authorization event for the underlying charging session in E Roaming.",
    "09_11_csm.csm_charging_session_authorizations.e_roaming_token_contract_id": "The contract id associated to the eRoaming token used to authorize the charging session.",
    "09_11_csm.csm_charging_session_authorizations.e_roaming_token_type": "\"APP_USER\" if remote start, \"RIFD\" if authorized with an RFID card.",
    "09_11_csm.csm_charging_session_authorizations.employee_token": "Token used for Employee Charging.",
    "09_11_csm.csm_charging_session_authorizations.idempotency_key": "Key to support idempotent retries of the authorization for the same session (OCPP 2.0 only).",
    "09_11_csm.csm_charging_session_authorizations.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_charging_session_authorizations.updated_at": "Last update timestamp of this entity.",
    "09_11_csm.csm_charging_sessions.authorization_details_id": "The primary identifier of the charging session authorization.",
    "09_11_csm.csm_charging_sessions.authorization_mode": "Station authorization mode at the time this resource was created.",
    "09_11_csm.csm_charging_sessions.charging_connector": "Charging connector used during the charging session.",
    "09_11_csm.csm_charging_sessions.charging_session_id": "The primary identifier of a charging session.",
    "09_11_csm.csm_charging_sessions.charging_spot": "Charging spot, ID of the EVSE in relation to the station (typically 1 and 2)",
    "09_11_csm.csm_charging_sessions.charging_state": "Last charging state of the session.",
    "09_11_csm.csm_charging_sessions.contract_type": "CSM contract type of owning organization.",
    "09_11_csm.csm_charging_sessions.created_at": "Creation timestamp of this entity.",
    "09_11_csm.csm_charging_sessions.energy_wh": "Energy consumed during the charging session.",
    "09_11_csm.csm_charging_sessions.fault_cause": "Fault cause why the underlying charging session faulted.",
    "09_11_csm.csm_charging_sessions.last_meter_value_wh": "Last received meter value in Wh of the charging session.",
    "09_11_csm.csm_charging_sessions.organization_branch_id": "Organization branch ID this resource is assigned to.",
    "09_11_csm.csm_charging_sessions.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_charging_sessions.session_lifecycle": "Last lifecycle of the session",
    "09_11_csm.csm_charging_sessions.start_date_time": "Start timestamp of the underlying charging session.",
    "09_11_csm.csm_charging_sessions.start_meter_value_wh": "The meter value in Wh when the charging session started.",
    "09_11_csm.csm_charging_sessions.station_id": "The primary identifier of a charging station.",
    "09_11_csm.csm_charging_sessions.station_name": "Station name of the charging station in the ESS.",
    "09_11_csm.csm_charging_sessions.station_serial_number": "Serial number of the charging station a charging session has happened.",
    "09_11_csm.csm_charging_sessions.stop_date_time": "Stop timestamp of the underlying charging session.",
    "09_11_csm.csm_charging_sessions.updated_at": "Last update timestamp of this entity.",
    "09_11_csm.csm_csms_additional_data.info": "information description",
    "09_11_csm.csm_csms_additional_data.info_category": "category of information",
    "09_11_csm.csm_csms_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_additional_data.station_id": "unique identifier of the charging station",
    "09_11_csm.csm_csms_additional_data.timestamp": "Timestamp of creation of this record",
    "09_11_csm.csm_csms_charging_sessions.authorization_id_token_type": "Type of the ID token used to authorize the charging session, one of \"Central\", \"eMAID\", \"ISO14443\", \"ISO15693\", \"KeyCode\", \"Local\", \"MacAddress\", \"NoAuthorization\"",
    "09_11_csm.csm_csms_charging_sessions.authorization_mode": "Authorization mode of the charging station at the time the session was authorized, one of \"no_authorization_cs\", \"no_authorization_csms\", \"authorization_csms\", \"authorization_e_roaming \" (unstable, will be removed at some point)",
    "09_11_csm.csm_csms_charging_sessions.authorization_provider_id": "ID of the authorization provider that authorized the charging session",
    "09_11_csm.csm_csms_charging_sessions.authorization_provider_name": "Name of the authorization provider that authorized the charging session",
    "09_11_csm.csm_csms_charging_sessions.authorization_provider_reference": "Reference given by of the authorization provider that authorized the charging session",
    "09_11_csm.csm_csms_charging_sessions.authorization_result": "Authorization result for the charging session, one of \"Accepted\", \"Invalid\"",
    "09_11_csm.csm_csms_charging_sessions.cache_expiry_date": "Timestamp when the authorization cache expires in the charging station, only relevant for OCPP 1.6 (operational data, not relevant for analytics)",
    "09_11_csm.csm_csms_charging_sessions.charging_state": "Charging state of the session as reported by the station, one of \"Charging\", \"EVDetected\", \"SuspendedEV\", \"SuspendedEVSE\"",
    "09_11_csm.csm_csms_charging_sessions.created_at": "Timestamp when the session was created in the CSMS",
    "09_11_csm.csm_csms_charging_sessions.e_roaming_authorization_reference": "Authorization reference provided by MSP (deprecated, use authorization_provider_re ference instead)",
    "09_11_csm.csm_csms_charging_sessions.e_roaming_token_contract_id": "Contract ID provided by MSP (deprecated)",
    "09_11_csm.csm_csms_charging_sessions.e_roaming_token_type": "Type of the ID token provided by MSP, one of \"RFID\", \"APP_USER\" (deprecated, use authorization_id_token_ty pe instead)",
    "09_11_csm.csm_csms_charging_sessions.expiry_date": "Timestamp when the authorization of the charging session will expire (operational data, not relevant for analytics)",
    "09_11_csm.csm_csms_charging_sessions.fault_cause": "Reason why charging session has lifecycle state \"Faulted\", one of \"negative_duration\", \"negative_energy_consu mption\", \"closed_by_new_session\" , \"missing_country_for_cpo _price\", \"missing_country_for_tot al_compensation\", \"terminated_by_agent\", \"terminated_by_system\", \"missing_meter_value\"",
    "09_11_csm.csm_csms_charging_sessions.id": "Unique ID of the charging session in the CSMS",
    "09_11_csm.csm_csms_charging_sessions.last_meter_value_timestamp": "Timestamp when last meter value was received",
    "09_11_csm.csm_csms_charging_sessions.last_meter_value_wh": "Latest meter value between start and end of the session in Watt hours (Wh)",
    "09_11_csm.csm_csms_charging_sessions.lifecycle": "Lifecycle of the charging session as modelled in the CSMS, one of \"CablePluggedIn\", \"Active\", \"Closed\", \"Faulted\", \"AuthorizationHandled\", \"Aborted\", \"PreAuthorizationHandle d\"",
    "09_11_csm.csm_csms_charging_sessions.ocmf_data": "Signed ocmf data provided by some stations (operational data, not relevant for analytics)",
    "09_11_csm.csm_csms_charging_sessions.ocpp_connector_id": "ID of the connector related to the charging session as defined by OCPP",
    "09_11_csm.csm_csms_charging_sessions.ocpp_evse_id": "ID of the EVSE related to the charging session as defined by OCPP",
    "09_11_csm.csm_csms_charging_sessions.offline": "EXPERIMENTAL, might be removed again in the future if it turns out to be unreliable! This flag indicates whether a session was marked as offline by the charging station. It defaults to false for OCPP 2.0.x (except for authorization-handled sessions in some cases) and will always be undefined for OCPP 1.6.",
    "09_11_csm.csm_csms_charging_sessions.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_charging_sessions.start_date_time": "Timestamp when the session started at the station",
    "09_11_csm.csm_csms_charging_sessions.start_meter_value_wh": "Meter value at the start of the session in Watt hours (Wh)",
    "09_11_csm.csm_csms_charging_sessions.start_reason": "Start (trigger) reason provided by station on session start",
    "09_11_csm.csm_csms_charging_sessions.station_id": "ID of the charging station related to the session",
    "09_11_csm.csm_csms_charging_sessions.station_serial_number": "Serial number of the charging station as defined by the vendor",
    "09_11_csm.csm_csms_charging_sessions.stop_date_time": "Timestamp when the session stopped at the station",
    "09_11_csm.csm_csms_charging_sessions.stop_meter_value_wh": "Meter value at the end of the session in Watt hours (Wh)",
    "09_11_csm.csm_csms_charging_sessions.stop_reason": "Stop reason provided by station on session end",
    "09_11_csm.csm_csms_charging_sessions.trigger_reason": "Trigger reason provided by station on session end",
    "09_11_csm.csm_csms_charging_sessions.updated_at": "Timestamp when the session was last updated",
    "09_11_csm.csm_csms_ocpp_errors.action": "OCPP action that was performed",
    "09_11_csm.csm_csms_ocpp_errors.device_twin_id": "The unique id of the device twin of the station (unclear prospect, not exposing for analytics)",
    "09_11_csm.csm_csms_ocpp_errors.error_code": "Error code returned by the station",
    "09_11_csm.csm_csms_ocpp_errors.model_name": "Name of the charging station model",
    "09_11_csm.csm_csms_ocpp_errors.ocpp_protocol": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "09_11_csm.csm_csms_ocpp_errors.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_ocpp_errors.response_type": "Response type returned by the station",
    "09_11_csm.csm_csms_ocpp_errors.station_id": "ID of the charging station related to the client certificate info",
    "09_11_csm.csm_csms_ocpp_errors.timestamp": "Timestamp when the error event was logged",
    "09_11_csm.csm_csms_scs_station_contact.http_status_code": "The status code of the HTTP request in SCS",
    "09_11_csm.csm_csms_scs_station_contact.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_scs_station_contact.scs_endpoint": "The timestamp of the contact data point",
    "09_11_csm.csm_csms_scs_station_contact.station_id": "ID of the charging station related to the client certificate info",
    "09_11_csm.csm_csms_scs_station_contact.timestamp": "The timestamp of the contact data point",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.charging_profile_id": "Charging profile as configured on the station",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.message_id": "The unique ID of the PubSub message",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.publish_time": "Timestamp when the PubSub message was published",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.station_id": "ID of the charging station",
    "09_11_csm.csm_csms_station_charging_profile_cleared_events.timestamp": "Timestamp when charging profile was cleared",
    "09_11_csm.csm_csms_station_charging_profile_set_events.charging_profile_charging_schedule": "List of charging periods",
    "09_11_csm.csm_csms_station_charging_profile_set_events.charging_profile_id": "Charging profile as configured on the station",
    "09_11_csm.csm_csms_station_charging_profile_set_events.charging_profile_kind": "Indicates the kind of schedule",
    "09_11_csm.csm_csms_station_charging_profile_set_events.charging_profile_purpose": "Defines the purpose of the schedule transferred by this profile",
    "09_11_csm.csm_csms_station_charging_profile_set_events.charging_profile_stack_level": "Value determining level in hierarchy stack of profiles",
    "09_11_csm.csm_csms_station_charging_profile_set_events.message_id": "The unique ID of the PubSub message",
    "09_11_csm.csm_csms_station_charging_profile_set_events.ocpp_evse_id": "Station evse id",
    "09_11_csm.csm_csms_station_charging_profile_set_events.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_station_charging_profile_set_events.publish_time": "Timestamp when the PubSub message was published",
    "09_11_csm.csm_csms_station_charging_profile_set_events.station_id": "ID of the charging station",
    "09_11_csm.csm_csms_station_charging_profile_set_events.timestamp": "Timestamp when charging profile was set",
    "09_11_csm.csm_csms_station_charging_profiles_reset_events.message_id": "The unique ID of the PubSub message",
    "09_11_csm.csm_csms_station_charging_profiles_reset_events.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_station_charging_profiles_reset_events.publish_time": "Timestamp when the PubSub message was published",
    "09_11_csm.csm_csms_station_charging_profiles_reset_events.station_id": "ID of the charging station",
    "09_11_csm.csm_csms_station_charging_profiles_reset_events.timestamp": "Timestamp when charging profile was cleared",
    "09_11_csm.csm_csms_station_connection_state_changed_events.connection_state": "Indicates the event_type, one of \"connected\", \"disconnected\" or \"connected_in_quarantin e_mode\"",
    "09_11_csm.csm_csms_station_connection_state_changed_events.message_id": "The unique ID of the PubSub message",
    "09_11_csm.csm_csms_station_connection_state_changed_events.model": "Model of the charging station",
    "09_11_csm.csm_csms_station_connection_state_changed_events.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_station_connection_state_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "09_11_csm.csm_csms_station_connection_state_changed_events.station_id": "ID of the charging station",
    "09_11_csm.csm_csms_station_connection_state_changed_events.timestamp": "Station disconnection timestamp",
    "09_11_csm.csm_csms_station_connector_status_changed_events.error_code": "OCPP error codes in case of failed connector status changes",
    "09_11_csm.csm_csms_station_connector_status_changed_events.error_info": "Additional free format information about the error",
    "09_11_csm.csm_csms_station_connector_status_changed_events.firmware_version": "Version of firmware installed on charging station",
    "09_11_csm.csm_csms_station_connector_status_changed_events.message_id": "The unique ID of the PubSub message",
    "09_11_csm.csm_csms_station_connector_status_changed_events.model": "Model of the charging station",
    "09_11_csm.csm_csms_station_connector_status_changed_events.ocpp_connector_id": "Station connector id",
    "09_11_csm.csm_csms_station_connector_status_changed_events.ocpp_evse_id": "Station evse id",
    "09_11_csm.csm_csms_station_connector_status_changed_events.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_station_connector_status_changed_events.publish_time": "Timestamp when the PubSub message was published",
    "09_11_csm.csm_csms_station_connector_status_changed_events.reported_timestamp": "Timestamp reported by the station about the status change",
    "09_11_csm.csm_csms_station_connector_status_changed_events.station_id": "ID of the charging station",
    "09_11_csm.csm_csms_station_connector_status_changed_events.status": "Status value of station connector",
    "09_11_csm.csm_csms_station_connector_status_changed_events.timestamp": "Station connector state change timestamp. Time of receipt of the OCPP message from CSMS",
    "09_11_csm.csm_csms_station_connector_status_changed_events.vendor_error_code": "A vendor-specific error code",
    "09_11_csm.csm_csms_status_notifications.model_name": "Name of the charging station model",
    "09_11_csm.csm_csms_status_notifications.ocpp_version": "Version of the OCPP protocol the station uses for communication with the CSMS, one of \"OCPP_1.6\", \"OCPP_2.0.0\", \"OCPP_2.0.1\"",
    "09_11_csm.csm_csms_status_notifications.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_csms_status_notifications.serial_number": "The serial number of the charging station as defined by the vendor",
    "09_11_csm.csm_csms_status_notifications.station_id": "ID of the charging station which sends the status notification",
    "09_11_csm.csm_csms_status_notifications.timestamp": "Timestamp when the status notification was reported by the station",
    "09_11_csm.csm_flexpole_daphne_additional_data.chargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_additional_data.info": "information description",
    "09_11_csm.csm_flexpole_daphne_additional_data.info_category": "category of information",
    "09_11_csm.csm_flexpole_daphne_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_additional_data.timestamp": "time when the data was recorded",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.ChargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.description": "description of the error",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.dtc": "diagnostic trouble code",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.dtc_can_message": "name of the CAN bus message",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.dtc_id": "unique identifier of the error",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.ecu": "electronic control unit that sent the message",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.faultLevel": "severity level of the error",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.ingestion_timestamp": "999909",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.signalsToString": "signals that triggered the error or are related",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.start_timestamp": "time when the error started",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.stop_timestamp": "time when the error was resolved",
    "09_11_csm.csm_flexpole_daphne_dtc_messages.timestamp": "time when the data was recorded",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.chargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.ecu": "name of the electronic control unit",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.ingestion_timestamp": "time when the data was received",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.timestamp": "time when the data was recorded",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.version": "current version installed on the ecu",
    "09_11_csm.csm_flexpole_daphne_ecu_messages.version_type": "whether the version is concerning hardware or software of the ecu",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.ChargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_ICCID": "SIM card identifier for TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_IMEI": "International Mobile Equipment Identity for TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_IMSI": "International Mobile Subscriber Identity for TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_RSRQ": "signal quality reported by TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_RSSI": "signal strength reported by TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_network_type": "network connection type used by TBox1",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_ICCID": "SIM card identifier for TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_IMEI": "International Mobile Equipment Identity for TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_IMSI": "International Mobile Subscriber Identity for TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_RSRQ": "signal quality reported by TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_RSSI": "signal strength reported by TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_network_type": "network connection type used by TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_real": "time TBox2 has been running",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_suspend": "time TBox2 has been in suspended mode",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_total": "total uptime of TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.compatibilityStatus": "info whether all ecus have compatible hardware and software versions",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.compatibilityStatusInfo": "additonal info about ecu versions",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.cpuloadAverage": "average cpu usage of the device",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.gun_plug_cycles_sideA": "number of plug cycles on side A",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.gun_plug_cycles_sideB": "number of plug cycles on side B",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.incorrectVersion": "list of incorrect versions",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.ingestion_timestamp": "time when the data was received",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.last_contact_to_operator_backend": "time of the last successful connection to the customer's operating backend",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.memoryUsage": "memory usage of the device",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.operator_backend_status": "connection status of the customer's backend",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.software_version": "firmware version of TBox2",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.status_general": "overall status of the station",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.status_sideA": "status of the connector on side A",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.status_sideB": "status of the connector on side B",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.system_SW_version": "firmware version of overall system",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.timestamp": "time when the data was recorded",
    "09_11_csm.csm_flexpole_daphne_heartbeat_messages.unknownVersion": "list of unknown versions",
    "09_11_csm.csm_flexpole_daphne_signal_messages.ChargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_signal_messages.canBusId": "identifier of the CAN bus sending the message",
    "09_11_csm.csm_flexpole_daphne_signal_messages.ecu": "electronic control unit that sent the message",
    "09_11_csm.csm_flexpole_daphne_signal_messages.messageName": "name of signal group",
    "09_11_csm.csm_flexpole_daphne_signal_messages.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_signal_messages.rawMessage": "unknown",
    "09_11_csm.csm_flexpole_daphne_signal_messages.signal": "name of the signal",
    "09_11_csm.csm_flexpole_daphne_signal_messages.signalId": "identifier of the signal",
    "09_11_csm.csm_flexpole_daphne_signal_messages.signalsetFrequency": "frequency of reported signals",
    "09_11_csm.csm_flexpole_daphne_signal_messages.timestamp": "time when the data was recorded",
    "09_11_csm.csm_flexpole_daphne_signal_messages.value": "reported value of the signal",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.chargingStationID": "unique identifier of the Flexpole",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.destinationDevice": "component that received the message",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.log": "content of the message",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.loggingDevice": "component that stores the log",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.sourceDevice": "component that sent the message",
    "09_11_csm.csm_flexpole_daphne_textlog_messages.timestamp": "time when the data was recorded",
    "09_11_csm.csm_lm_additional_data.info": "information description",
    "09_11_csm.csm_lm_additional_data.info_category": "category of information",
    "09_11_csm.csm_lm_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_additional_data.station_id": "The id of the Station wallbox.",
    "09_11_csm.csm_lm_additional_data.timestamp": "Timestamp of creation of this record",
    "09_11_csm.csm_lm_charge_point_change_event.ephemeral_backup_active_after": "The timestamp after when the ephemeral profile is active. It is null if ephemeral profiles are not used with the station.",
    "09_11_csm.csm_lm_charge_point_change_event.ephemeral_backup_watt": "The maximum power which the station can charge with when the ephemeral profile is active. It is null if ephemeral profiles are not used with the station.",
    "09_11_csm.csm_lm_charge_point_change_event.load_group_id": "The id of the load group.",
    "09_11_csm.csm_lm_charge_point_change_event.max_power_watt": "The maximum power which the station can charge with.",
    "09_11_csm.csm_lm_charge_point_change_event.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_charge_point_change_event.station_id": "The primary identifier of a charging station.",
    "09_11_csm.csm_lm_charge_point_change_event.status": "The status of a charge point.",
    "09_11_csm.csm_lm_charge_point_change_event.timestamp": "Creation timestamp of this row.",
    "09_11_csm.csm_lm_load_group_audit_log.details": "The event details in json format. Depending on the event type, the json structure can differ.",
    "09_11_csm.csm_lm_load_group_audit_log.event": "The type of the event that was received for the load group (ex: CREATE, DELETE, ADD_CHARGE_POINT, ...).",
    "09_11_csm.csm_lm_load_group_audit_log.initiator": "The initiator that triggere the load group update.",
    "09_11_csm.csm_lm_load_group_audit_log.load_group_id": "The id of the load group.",
    "09_11_csm.csm_lm_load_group_audit_log.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_load_group_audit_log.timestamp": "Creation timestamp of this row.",
    "09_11_csm.csm_lm_load_group_change.add_charge_point": "If a station was added in the load group, then the id of that station, otherwise null.",
    "09_11_csm.csm_lm_load_group_change.change_id": "The id of the load group change.",
    "09_11_csm.csm_lm_load_group_change.initiator": "The initiator that triggered the load group update.",
    "09_11_csm.csm_lm_load_group_change.load_group_id": "The id of the load group.",
    "09_11_csm.csm_lm_load_group_change.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_load_group_change.origin_trace_id": "The trace id of the load group change.",
    "09_11_csm.csm_lm_load_group_change.remove_charge_point": "If a station was removed from the load group, then the id of that station, otherwise null.",
    "09_11_csm.csm_lm_load_group_change.status": "The status of the load group change showing if it was successfully processed or not.",
    "09_11_csm.csm_lm_load_group_change.timestamp": "Creation timestamp of this row.",
    "09_11_csm.csm_lm_schedule.csm_lm_schedule_is_enabled": "True if the schedule is enabled, otherwise false.",
    "09_11_csm.csm_lm_schedule.csm_lm_schedule_load_group_id": "The id of the load group.",
    "09_11_csm.csm_lm_schedule.csm_lm_schedule_organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_schedule.csm_lm_schedule_setting_id": "The id of the setting for the load group.",
    "09_11_csm.csm_lm_schedule.csm_lm_schedule_shift": "The load group's schedule in json format containing day of week, hours, maximum power and if it is enabled.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_pubsub_message_id": "The id of the pubsub message.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_reported_timestamp": "The reported timestamp of the session's energy consumption.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_session_energy_consumption_wh": "Energy consumed during the charging session.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_session_id": "The primary identifier of a charging session.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_station_evse_id": "Unique identifier of an EVSE in the station context, e.g. 1 and 2.",
    "09_11_csm.csm_lm_session_energy_consumption.csm_lm_session_energy_consumption_station_id": "The primary identifier of a charging station.",
    "09_11_csm.csm_wallbox_additional_data.info": "information description",
    "09_11_csm.csm_wallbox_additional_data.info_category": "category of information",
    "09_11_csm.csm_wallbox_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_additional_data.station_id": "The id of the Station wallbox.",
    "09_11_csm.csm_wallbox_additional_data.timestamp": "Timestamp of creation of this record",
    "09_11_csm.csm_wallbox_fws_stations.allow_auto_update": "Boolean flag indicating if auto update is allowed.",
    "09_11_csm.csm_wallbox_fws_stations.allow_auto_update_changed_by": "Identifier for who changed the auto-update flag.",
    "09_11_csm.csm_wallbox_fws_stations.allow_auto_update_last_changed": "Timestamp when the auto-update flag was last changed.",
    "09_11_csm.csm_wallbox_fws_stations.channel": "Channel identifier stored as a STRING (UUID).",
    "09_11_csm.csm_wallbox_fws_stations.connected": "Boolean flag indicating if the station is connected.",
    "09_11_csm.csm_wallbox_fws_stations.connection_changed_at": "Timestamp of the last connection change.",
    "09_11_csm.csm_wallbox_fws_stations.current_firmware_version": "Reference to the current firmware version, stored as text.",
    "09_11_csm.csm_wallbox_fws_stations.external_station_uuid": "Primary key",
    "09_11_csm.csm_wallbox_fws_stations.goto_firmware_version": "Reference to the firmware version to go to, stored as text.",
    "09_11_csm.csm_wallbox_fws_stations.model": "Model identifier stored as a STRING (UUID).",
    "09_11_csm.csm_wallbox_fws_stations.occ_version": "Occurrence version, default is 0.",
    "09_11_csm.csm_wallbox_fws_stations.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_fws_stations.product": "Reference to the product id.",
    "09_11_csm.csm_wallbox_fws_stations.product_updated_at": "Timestamp when the product was last updated.",
    "09_11_csm.csm_wallbox_fws_stations.serial_number": "Serial number of the station.",
    "09_11_csm.csm_wallbox_fws_stations.version_string": "Version string of the station.",
    "09_11_csm.csm_wallbox_fws_stations.version_string_changed_at": "Timestamp when the version string was changed.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.failed_reason": "Reason for failure, if any",
    "09_11_csm.csm_wallbox_fws_update_attamepts.finished_at": "Timestamp when the update attempt finished.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.id": "Primary key stored as STRING (UUID).",
    "09_11_csm.csm_wallbox_fws_update_attamepts.occ_version": "Occurrence version number, default is 0.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.request_id": "Unique request id (SERIAL in source, numeric).",
    "09_11_csm.csm_wallbox_fws_update_attamepts.started_at": "Timestamp when the update attempt started.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.success": "Boolean flag indicating if the update was successful.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.update_process": "Reference to update_process id.",
    "09_11_csm.csm_wallbox_fws_update_attamepts.valid": "Boolean flag indicating if the row is valid.",
    "09_11_csm.csm_wallbox_fws_update_processes.created_by": "Identifier of the user who created the update process.",
    "09_11_csm.csm_wallbox_fws_update_processes.finished_at": "Timestamp when the update process finished.",
    "09_11_csm.csm_wallbox_fws_update_processes.goto_firmware": "Reference to the firmware id.",
    "09_11_csm.csm_wallbox_fws_update_processes.id": "Primary key",
    "09_11_csm.csm_wallbox_fws_update_processes.next_check": "Timestamp for the next check of the update process.",
    "09_11_csm.csm_wallbox_fws_update_processes.occ_version": "Occurrence version number, default is 0.",
    "09_11_csm.csm_wallbox_fws_update_processes.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_fws_update_processes.station_id": "Reference to the station id.",
    "09_11_csm.csm_wallbox_fws_update_processes.success": "Boolean flag indicating if the update process succeeded.",
    "09_11_csm.csm_wallbox_fws_update_processes.terminated_by": "Identifier of who terminated the update process.",
    "09_11_csm.csm_wallbox_fws_update_processes.termination_reason": "Reason for termination of the update process.",
    "09_11_csm.csm_wallbox_fws_update_processes.triggered_at": "Timestamp when the update process was triggered.",
    "09_11_csm.csm_wallbox_scs_configurations.attribute_status": "Attribute Status stored as String.",
    "09_11_csm.csm_wallbox_scs_configurations.component_instance": "Component Instance stored as String.",
    "09_11_csm.csm_wallbox_scs_configurations.component_name": "Component Name stored as String.",
    "09_11_csm.csm_wallbox_scs_configurations.fetched_at": "Timestamp when the configurations was fetchedAt",
    "09_11_csm.csm_wallbox_scs_configurations.id": "Primary key",
    "09_11_csm.csm_wallbox_scs_configurations.ocpp_type": "Ocpp Version stored as String.",
    "09_11_csm.csm_wallbox_scs_configurations.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_scs_configurations.read_only": "Mark configuration as readOnly",
    "09_11_csm.csm_wallbox_scs_configurations.revision": "An integer value representing the revision number.",
    "09_11_csm.csm_wallbox_scs_configurations.station_id": "A UUID representing the station, converted to Text",
    "09_11_csm.csm_wallbox_scs_configurations.value": "Value stored as String",
    "09_11_csm.csm_wallbox_scs_configurations.variable_instance": "Variable Instance stored as String.",
    "09_11_csm.csm_wallbox_scs_configurations.variable_name": "Variable Name stored as String.",
    "09_11_csm.csm_wallbox_scs_station_information.channel_id": "A UUID representing the Channel Id, converted to Text.",
    "09_11_csm.csm_wallbox_scs_station_information.channel_id_changed_at": "Timestamp when the channel Id was changed.",
    "09_11_csm.csm_wallbox_scs_station_information.channel_id_data_source_owner_revision": "An integer value representing the channelId Data Source Owner Revision number.",
    "09_11_csm.csm_wallbox_scs_station_information.connected": "Boolean flag indicating if the station is connected.",
    "09_11_csm.csm_wallbox_scs_station_information.connection_changed_at": "Timestamp when the connection was changed.",
    "09_11_csm.csm_wallbox_scs_station_information.connection_source": "Connection Source, stored as text.",
    "09_11_csm.csm_wallbox_scs_station_information.last_booted_at": "Timestamp when the Station was last booted.",
    "09_11_csm.csm_wallbox_scs_station_information.model_id": "A UUID representing the station Model Id, converted to Text.",
    "09_11_csm.csm_wallbox_scs_station_information.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_scs_station_information.reported_firmware_version": "Reported firmware version, stored as text.",
    "09_11_csm.csm_wallbox_scs_station_information.reported_firmware_version_changed_at": "Timestamp when the reported Firmware Version was changed.",
    "09_11_csm.csm_wallbox_scs_station_information.revision": "An integer value representing the revision number.",
    "09_11_csm.csm_wallbox_scs_station_information.serial_number": "Station serial number",
    "09_11_csm.csm_wallbox_scs_station_information.station_created_at": "Timestamp when the Station was created.",
    "09_11_csm.csm_wallbox_scs_station_information.station_deleted_at": "Timestamp when the Station was deleted.",
    "09_11_csm.csm_wallbox_scs_station_information.station_id": "Primary key",
    "09_11_csm.csm_wallbox_scs_update_attempts.finished_at": "Timestamp when the update process finished.",
    "09_11_csm.csm_wallbox_scs_update_attempts.id": "Primary key",
    "09_11_csm.csm_wallbox_scs_update_attempts.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_scs_update_attempts.result": "A Json representing the update attemp result, converted to Text.",
    "09_11_csm.csm_wallbox_scs_update_attempts.revision": "An integer value representing the revision number.",
    "09_11_csm.csm_wallbox_scs_update_attempts.state": "Update attempt state, stored as text.",
    "09_11_csm.csm_wallbox_scs_update_attempts.triggered_at": "Timestamp when the update attempt was triggered.",
    "09_11_csm.csm_wallbox_scs_update_attempts.update_process_id": "A UUID representing the station, converted to Text.",
    "09_11_csm.csm_wallbox_scs_update_processes.configuration_to_rollout": "A Json representing the configuration To Rollout, converted to Text.",
    "09_11_csm.csm_wallbox_scs_update_processes.finished_at": "Timestamp when the update process finished.",
    "09_11_csm.csm_wallbox_scs_update_processes.id": "Primary key",
    "09_11_csm.csm_wallbox_scs_update_processes.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_scs_update_processes.station_id": "A UUID representing the station, converted to Text.",
    "09_11_csm.csm_wallbox_scs_update_processes.success": "Boolean flag indicating if the process finished successfully.",
    "09_11_csm.csm_wallbox_scs_update_processes.target_configuration_id": "A UUID representing the station Target Configuration Id, converted to Text.",
    "09_11_csm.csm_wallbox_scs_update_processes.triggered_at": "Timestamp when the update process was triggered.",
    "09_11_csm.csm_wallbox_sds_station_error_histories.dtc_code": "Diagnostic trouble code as text.",
    "09_11_csm.csm_wallbox_sds_station_error_histories.id": "Primary key",
    "09_11_csm.csm_wallbox_sds_station_error_histories.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_sds_station_error_histories.resolved_at": "Timestamp when the event was resolved. This field is nullable.",
    "09_11_csm.csm_wallbox_sds_station_error_histories.severity": "Severity level (converted from enum to STRING).",
    "09_11_csm.csm_wallbox_sds_station_error_histories.started_at": "Timestamp when the event started.",
    "09_11_csm.csm_wallbox_sds_station_error_histories.station_id": "A UUID representing the station, converted to text.",
    "09_11_csm.csm_wallbox_sds_station_information.last_update_from_station": "Timestamp indicating when the station was last updated.",
    "09_11_csm.csm_wallbox_sds_station_information.organization_iam_id": "IAM ID of the owning organization.",
    "09_11_csm.csm_wallbox_sds_station_information.revision": "An integer value representing the revision number.",
    "09_11_csm.csm_wallbox_sds_station_information.severity": "Severity level (converted from enum to STRING).",
    "09_11_csm.csm_wallbox_sds_station_information.station_id": "A UUID representing the station, converted to text.",
    "1023421177-0-96": "Data set that contains information about the unit of distance measurement used in the vehicle. 0x0 = km 0x1 = miles",
    "1023421177-10-96": "Data set that contains information about the unit of distance measurement used in the vehicle. 0x0 = km 0x1 = miles",
    "1023421177-255-414": "Data set that contains information about the unit of distance measurement. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421179-0-98": "Data set that contains information about the temperature unit settings. 0x0 = Celsius 0x1 = Fahrenheit",
    "1023421179-10-98": "Data set that contains information about the temperature unit settings. 0x0 = Celsius 0x1 = Fahrenheit",
    "1023421179-255-414": "Data set that contains information about the temperature unit settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421180-0-99": "Data set that specifies the volume unit. 0x0 = Liter 0x1 = Gallon (UK) 0x2 = Gallon (US)",
    "1023421180-10-99": "Data set that specifies the volume unit. 0x0 = Liter 0x1 = Gallon (UK) 0x2 = Gallon (US)",
    "1023421180-255-414": "Data set that specifies the volume unit. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421181-0-100": "Data set that contains information about the unit of measurement used for fuel consumption. 0x0 = mpg_UK 0x1 = mpg_US 0x2 = l_per_100km 0x3 = km_per_l",
    "1023421181-10-100": "Data set that contains information about the unit of measurement used for fuel consumption. 0x0 = mpg_UK 0x1 = mpg_US 0x2 = l_per_100km 0x3 = km_per_l",
    "1023421181-255-414": "Data set that contains information about the unit of measurement used for fuel consumption. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421182-0-101": "Data set that contains information about the pressure unit settings. 0x0 = bar 0x1 = PSI 0x2 = kPa",
    "1023421182-10-101": "Data set that contains information about the pressure unit settings. 0x0 = bar 0x1 = PSI 0x2 = kPa",
    "1023421182-255-414": "Data set that contains information about the pressure unit settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421183-0-102": "Data set that specifies the unit of measurement for gas consumption. 0x0 = kg_per_100km 0x1 = km_per_kg 0x2 = m3_per_100km 0x3 = km_per_m3 0x4 = miles_per_lbs 0x5 = miles_per_yard3 0x6 = miles_per_kg (DF3.5) 0x7 = miles_per_m3 (DF3.5) 0x8 = miles_per_gallon_equival ent_US (mpge_US, DF3.5)",
    "1023421183-10-102": "Data set that specifies the unit of measurement for gas consumption in a vehicle. 0x0 = kg_per_100km 0x1 = km_per_kg 0x2 = m3_per_100km 0x3 = km_per_m3 0x4 = miles_per_lbs 0x5 = miles_per_yard3 0x6 = miles_per_kg (DF3.5) 0x7 = miles_per_m3 (DF3.5) 0x8 = miles_per_gallon_equival ent_US (mpge_US, DF3.5)",
    "1023421183-255-414": "Data set that contains information about the unit of gas consumption. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421184-0-103": "Data set that contains information about the unit of mass used in the vehicle, represented as an integer value. 0x0 = kg 0x1 = lbs",
    "1023421184-10-103": "Data set that contains information about the unit of mass used in the vehicle. 0x0 = kg 0x1 = lbs",
    "1023421184-255-414": "Data set that contains information about the unit of mass. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421185-0-104": "Data set that contains information about the date display format settings in the vehicle. 0x0 = day / month / year 0x1 = month / day / year 0x2 = year / month / day",
    "1023421185-10-104": "Data set that specifies the format in which the date is displayed in the vehicle. 0x0 = day / month / year 0x1 = month / day / year 0x2 = year / month / day",
    "1023421185-255-414": "Data set that contains information about the display format settings for the date. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421186-0-105": "Data set that contains information about the time display format settings in the vehicle. 0x0 = 24h 0x1 = 12h AM/PM",
    "1023421186-10-105": "Data set that contains information about the time display format settings in the vehicle. 0x0 = 24h 0x1 = 12h AM/PM",
    "1023421186-255-414": "Data set that contains information about the time display format settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421187-0-106": "Data set that provides information about the unit of measurement used for electric energy consumption or efficiency in a vehicle. 0x0 = kWh_per_100km 0x1 = km_per_kWh 0x2 = kWh_per_100miles 0x3 = miles_per_kWh 0x4 = miles_per_gallon_equival ent_US",
    "1023421187-10-106": "Data set that specifies the unit of measurement for electric energy consumption or efficiency in a vehicle. 0x0 = kWh_per_100km 0x1 = km_per_kWh 0x2 = kWh_per_100miles 0x3 = miles_per_kWh 0x4 = miles_per_gallon_equival ent_US",
    "1023421187-255-414": "Data set that specifies the unit of measurement for electric energy consumption or efficiency in a vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421188-0-107": "Data set that contains information about the active status of oil temperature visualisation. On/Off",
    "1023421189-0-107": "Data set that contains information about active status VZA. On/Off",
    "1023421190-0-107": "Data set that contains information about active status VZA as MFA user. On/Off",
    "1023421193-0-107": "Data set that contains information about active status of digital velocity. On/Off",
    "1023421194-0-107": "Data set that contains information about active status of average consumption. On/Off",
    "1023421195-0-107": "Data set that contains information about active status of average speed. On/Off",
    "1023421196-0-107": "Data set that contains information about active status of driving distance. On/Off",
    "1023421197-0-107": "Data set that contains information about active status of driving time. On/Off",
    "1023421199-0-107": "Data set that contains information about active status of range. On/Off",
    "1023421202-0-107": "Data set that provides information about the activation statis of the speed alert feature. On/Off",
    "1023421202-10-107": "Data set that provides information about the activation statis of the speed alert feature. On/Off",
    "1023421202-255-414": "Data set that provides information about the activation statis of the speed alert feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421203-0-108": "Data set that provides information about the activation statis of the speed limit feature. n/a",
    "1023421203-10-108": "Data set that provides information about the activation statis of the speed limit feature. n/a",
    "1023421203-255-414": "Data set that provides information about the activation statis of the speed threshold feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421205-0-107": "Data set that contains information about active status of secondary speed. On/Off",
    "1023421206-0-107": "Data set that contains information about active status of reset time. On/Off",
    "1023421207-0-107": "Data set that provides information about the activation status of the zero-emission distance feature. On/Off",
    "1023421209-0-107": "Data set that contains information about active status of zero emission time. On/Off",
    "1023421210-0-107": "Data set that contains information about active status of lap timer display. On/Off",
    "1023421214-0-112": "Data set that contains information about active status of last mode. 12/17: see module parameter definition PARAM_699",
    "1023421215-0-462": "Data set that provides information about the additional display settings for the digital clock. active (0) / inactive (1)",
    "1023421215-0-542": "Data set that provides information about the additional digital clock. n/a",
    "1023421215-10-462": "Data set that provides information about the additional display settings for the digital clock. active (0) / inactive (1)",
    "1023421215-255-414": "Data set that provides information about the additional display settings for the digital clock. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421216-0-113": "Data set that contains information about last mode customer unit. 12/17: see module parameter definition PARAM_701",
    "1023421220-0-117": "Data set that contains information about DC left tube. 12/17: see BAP DisplayConfig",
    "1023421221-0-117": "Data set that contains information about DC right tube. 12/17: see BAP DisplayConfig",
    "1023421222-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421223-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421224-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421225-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421226-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421227-0-118": "Data set that contains information about DC left tube space. 12/17: see BAP DisplayConfig",
    "1023421228-0-117": "Data set that contains information about display config. 12/17: see BAP DisplayConfig",
    "1023421229-0-117": "Data set that contains information about DC active space. 12/17: see BAP DisplayConfig",
    "1023421232-0-121": "Data set that contains information about active design. 0 = Classic design 1 = Sport design",
    "1023421240-0-125": "Data set that provides information about the human interface space. n/a",
    "1023421240-0-372": "Data set that contains information about the human interface active stage. 48,120,48,32,86,105,101 ,119,45,49,10,48,120,49, 32,86,105,101,119,45,50",
    "1023421240-10-125": "Data set that provides information about the human interface space. n/a",
    "1023421240-10-372": "Data set that contains information about the human interface active stage. 48,120,48,32,86,105,101 ,119,45,49,10,48,120,49, 32,86,105,101,119,45,50",
    "1023421240-255-368": "Data set that provides information about the active stage settings in the human-machine interface (HMI). 91,123,34,100,97,116,97 ,116,121,112,101,34,58, 34,98,111,111,108,34,44 ,34,100,101,115,99,114, 105,112,116,105,111,11 0,34,58,34,34,44,34,109, 105,110,34,58,34,48,34, 44,34,109,97,120,34,58, 34,49,34,44,34,115,116, 101,112,115,105,122,10 1,34,58,34,49,34,44,34,1 00,101,102,97,117,108,1 16,34,58,34,49,34,44,34, 100,97,116,97,108,101,1 10,103,116,104,34,58,34 ,49,34,125,44,123,34,10 0,97,116,97,116,121,112 ,101,34,58,34,117,105,1 10,116,56,34,44,34,100, 101,115,99,114,105,112, 116,105,111,110,34,58,3 4,34,44,34,109,105,110,",
    "1023421240-255-414": "Data set that provides information about the human interface space. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421241-0-126": "Data set that contains information about the display track change popup. Kombi_Datenmanager: SIGNAL_8382",
    "1023421242-0-127": "Data set that contains information about the side display left. Kombi_Datenmanager: SIGNAL_8259",
    "1023421243-0-128": "Data set that contains information about the side display right. Kombi_Datenmanager: SIGNAL_8260",
    "1023421244-0-129": "Data set that contains information about the active context display. Kombi_Datenmanager: SIGNAL_8592",
    "1023421254-0-134": "Data set that contains information about the language settings of the vehicle. 0x00 Deutsch 0x01 UK Englisch etc.",
    "1023421254-10-134": "Data set that contains information about the language settings of the vehicle. 0x00 Deutsch 0x01 UK Englisch etc.",
    "1023421254-255-414": "Data set that contains information about the language settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421255-0-135": "Data set that provides information about the display status of road sign notifications. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421255-10-135": "Data set that provides information about the display status of road sign notifications. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421255-255-414": "Data set that provides information about road sign notifications. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421256-0-135": "Data set that provides information about adaptive cruise control. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421256-10-135": "Data set that provides information about adaptive cruise control status. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421256-255-414": "Data set that provides information about adaptive cruise control settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421258-0-135": "Data set that provides information about whether the Sport Chrono feature is displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421258-10-135": "Data set that provides information about whether the Sport Chrono feature is displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421258-255-414": "Data set that contains information about the configuration and status of the Sport Chrono feature in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421262-0-135": "Data set that provides information about whether the map display is active or inactive. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421262-10-135": "Data set that provides information about whether the map display is active or inactive. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421262-255-414": "Data set that contains structured information related to mapping functionality, represented by a combination of boolean and unsigned 8-bit integer data types. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421264-0-135": "Data set that provides information about display status of Audi / Media. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421264-10-135": "Data set that provides information about the display status of the Audio/Media feature, indicating whether it is currently shown or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421264-255-414": "Data set that contains information related to audio and media settings in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421265-0-135": "Data set that provides information about the activation status of the active roll stabilization system. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421265-10-135": "Data set that provides information about the activation status of the active roll stabilization system. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421265-255-414": "Data set that provides information about the active roll stabilization system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421266-0-136": "Online 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421266-10-136": "Online 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421266-255-414": "Data set that contains information about the online status of a specific feature or system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421267-0-135": "Data set that provides information about the display status of the drive mode. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421267-10-135": "Data set that provides information about the display status of the drive mode. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421267-255-414": "Data set that provides information about the drive mode settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421268-0-138": "Data set that provides information about additional instrument status. 0x00 TRUE= instrument on, 0x01 FALSE = instrument off",
    "1023421268-10-138": "Data set that provides information about additional instrument status. 0x00 TRUE= instrument on, 0x01 FALSE = instrument off",
    "1023421268-255-414": "Data set that contains information about the status and configuration of an auxiliary instrument. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421269-0-139": "Data set that provides information about the lighting status of an auxiliary instrument. 0x00 TRUE = light on, 0x01 FALSE = light off",
    "1023421269-10-139": "Data set that provides information about the lighting status of an auxiliary instrument. 0x00 TRUE = light on, 0x01 FALSE = light off",
    "1023421269-255-414": "Data set that contains information about the lighting settings of an auxiliary instrument. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421270-0-140": "Data set that contains information about the time displayed on an auxiliary instrument. 0x00 TRUE = time on, 0x01 FALSE = time off",
    "1023421270-10-140": "Data set that contains information about the time displayed on an auxiliary instrument. 0x00 TRUE = time on, 0x01 FALSE = time off",
    "1023421270-255-414": "Data set that contains information about the time displayed on an auxiliary instrument. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421271-0-142": "Data set that contains information about the brightness level of an auxiliary instrument, expressed as a percentage. Type: Number Unit: percent",
    "1023421271-10-142": "Data set that contains information about the brightness level of an auxiliary instrument, expressed as a percentage. Type: Number Unit: percent",
    "1023421271-255-414": "Data set that contains information about the brightness level of an auxiliary instrument. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421273-0-145": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421273-10-145": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421273-255-414": "Data set that provides information about user X indivual options. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421274-0-146": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421274-10-146": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421274-255-414": "Data set that provides information about user X indivual options. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421275-0-147": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421275-10-147": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421275-255-414": "Data set that provides information about user X indivual options. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421276-0-148": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421276-10-148": "Data set that provides information about user X indivual options. see BAP DisplayConfig",
    "1023421276-255-414": "Data set that provides information about user X indivual options. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421295-0-461": "Analog Clock Additional Display active (0) / inactive (1)",
    "1023421295-0-543": "Data set that provides information about the activation status of the analog clock. active (0) / inactive (1)",
    "1023421295-10-461": "Analog Clock Additional Display active (0) / inactive (1)",
    "1023421295-255-414": "Data set that provides information about the additional display of an analog clock. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421296-0-543": "Data set that provides information about the activation status of the additional compass display. active (0) / inactive (1)",
    "1023421296-10-461": "Data set that provides information about the activation status of the additional compass display. active (0) / inactive (1)",
    "1023421296-255-414": "Data set that contains information about the additional compass display settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421298-0-473": "Data set that contains information about whether the tire pressure is displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421298-10-473": "Data set that contains information about whether the tire pressure is displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421298-255-414": "Data set that contains information about the tire pressure. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421299-0-473": "Data set that provides information about the display status of the shift assistant. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421299-10-473": "Data set that provides information about the display status of the shift assistant feature. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421299-255-414": "Data set that provides information about the gear shift assistant settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421300-0-475": "Performance 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421300-10-475": "Performance 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421300-255-414": "Data set that contains structured information related to performance settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421310-0-493": "Data set that provides information about the activation status of the information dimmer functionality. 0 = Infodimmer inactive 1 = Infodimmer active",
    "1023421310-10-493": "Info Dimmer 0 = Infodimmer inactive 1 = Infodimmer active",
    "1023421310-255-414": "Data set that contains information about the dimming settings of the vehicle's information display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421311-0-508": "Data set that provides information about the type of additional display configured in the vehicle, such as a digital clock, an analog clock, or a compass. 0 - digital clock 1 - analog clock 2 - compass",
    "1023421316-0-458": "Battery Status 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421316-10-458": "Battery Status 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421316-255-414": "Data set that provides information about the battery, structured to include multiple data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421317-0-381": "Display Large Stage 48,58,32,73,70,65,47,76, 97,100,101,110,47,68,10 1,107,111,114,97,116,11 1,114,44,32,49,58,32,78, 97,118,105,103,97,116,1 05,111,110",
    "1023421317-10-381": "Display Large Stage 48,58,32,73,70,65,47,76, 97,100,101,110,47,68,10 1,107,111,114,97,116,11 1,114,44,32,49,58,32,78, 97,118,105,103,97,116,1 05,111,110",
    "1023421317-255-368": "Data set that provides information about the display status of a specific feature, including its configuration parameters and associated data type. 91,123,34,100,97,116,97 ,116,121,112,101,34,58, 34,98,111,111,108,34,44 ,34,100,101,115,99,114, 105,112,116,105,111,11 0,34,58,34,34,44,34,109, 105,110,34,58,34,48,34, 44,34,109,97,120,34,58, 34,49,34,44,34,115,116, 101,112,115,105,122,10 1,34,58,34,49,34,44,34,1 00,101,102,97,117,108,1 16,34,58,34,49,34,44,34, 100,97,116,97,108,101,1 10,103,116,104,34,58,34 ,49,34,125,44,123,34,10 0,97,116,97,116,121,112 ,101,34,58,34,117,105,1 10,116,56,34,44,34,100, 101,115,99,114,105,112,",
    "1023421318-0-510": "Data set that provides information about the last mode.",
    "1023421320-10-345": "Data set that provides information about the context in which specific vehicle-related data is being viewed, distinguishing between different operational scenarios. 0: IFA context, 1: Trip context",
    "1023421321-10-323": "Data set that provides information about the selected center tab in the vehicle's interface. 0: CAR Tab, 1: Navigation, 2: Media",
    "1023421322-10-336": "Trackscreen BC Seite Area B 1 = dummy_value, 2 = G-force, 3 = Tire information, 4 = dummy_value",
    "1023421323-10-341": "Data set that provides information about the activation status of specific special screens in the vehicle, indicating whether no special screen is active, or if a specific mode such as \"Track\" or \"Offroad\" is active. 0x00 = no Sonderscreen active, 0x01 = Sonderscreen Track active, 0x02 = Sonderscreen Offroad active",
    "1023421324-10-342": "Trackscreen Area D 1 = dummy_value, 2 = dummy_value, 3 = Sport Chrono, 4 = dummy_value, 5 = MyInfo",
    "1023421325-10-343": "Data set that provides information about whether the speedometer is displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421326-10-155": "Data set that provides information about whether the power meter display is active or inactive. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421328-10-155": "Pure Display Status 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421329-10-155": "Data set that provides information about the recognition status of traffic signs, indicating whether they are displayed or not. 0x01 TRUE =displayed, 0x00 FALSE=not displayed",
    "1023421378-0-457": "Data set that provides information about the active map display mode in the vehicle's human-machine interface (HMI), indicating whether a large or small map is currently active. TRUE == large map; FALSE == small map",
    "1023421378-10-457": "HMI Active Map TRUE == large map; FALSE == small map",
    "1023421378-255-414": "Data set that contains information about the active map status in the human-machine interface (HMI). [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421379-0-458": "Efficiency 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421379-10-458": "Efficiency 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421379-255-414": "Data set that provides information about efficiency settings, structured to include multiple data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421380-0-458": "Boost 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421380-10-458": "Boost 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421380-255-414": "Data set that contains information about the boost settings, represented as a structured data type. This data set includes multiple fields, such as a boolean field and an 8-bit unsigned integer field, each with specific attributes like minimum and maximum values, step size, default value, and data length. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421382-0-458": "Data set that provides information about whether the electric consumption display is active or inactive. 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421382-10-458": "E Consumption 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421382-255-414": "Data set that provides information related to electric consumption, structured to include multiple data points with specific attributes such as data type, range, step size, and default values. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421383-0-458": "Aero Display Status 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421383-10-458": "Aero Display Status 0x01 TRUE =displayed 0x00 FALSE=not displayed",
    "1023421383-255-414": "Data set that contains structured information related to aerodynamic settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421384-0-463": "Data set that contains information about the manual zoom value of the map, represented as an integer. Zoom value of Map",
    "1023421384-10-463": "Data set that contains information about the manual zoom value of the map. Zoom value of Map",
    "1023421384-255-414": "Data set that contains information about the manual zoom value, structured to include multiple data types and parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421385-0-462": "Data set that contains information about the activation status of the autozoom feature, represented as an integer value. active (1) / inactive (0)",
    "1023421385-10-462": "Autozoom Settings active (1) / inactive (0)",
    "1023421385-255-414": "Data set that contains information about the autozoom functionality, including its activation status and associated parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421393-0-464": "Active Skin Settings 1-Classic 2-Reduced 3-Track 4-Heritage",
    "1023421393-10-464": "Active Skin Settings 1-Classic 2-Reduced 3-Track 4-Heritage",
    "1023421393-255-414": "Data set that contains information about the activation status of a specific feature or functionality, represented by a structured data type. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1023421394-0-467": "Volume Persistence 0 = quiet 1 = medium 2 = loud",
    "1023421394-10-467": "Volume Persistence 0 = quiet 1 = medium 2 = loud",
    "1023421394-255-414": "Data set that contains information about the persistence of volume settings, structured to include multiple data types and parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1049685-0-344": "Niko Test test",
    "1056976616-1-413": "Data set that provides information about the activation status and variant configuration of the Lane Assist system. 0 = Off; 1 = On; 2–255 Variant",
    "1056976622-0-469": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976622-1-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976622-2-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976622-255-552": "Data set that contains information about the configuration of an individual preset for a specific content area. [{datatype:\", description\":\"struct[defau lt][E3][G2PA]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976623-0-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976623-1-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976623-2-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976623-255-552": "Data set that contains information about the configuration of an individual preset for a specific content area. [{datatype:\", description\":\"struct[defau lt][E3][G2PA]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976624-0-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976624-1-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976624-2-160": "Data set that contains information about the configuration of an individual preset for a specific content area. See BAP catalog",
    "1056976624-255-552": "Data set that contains information about the configuration of an individual preset for a specific content area. [{datatype:\", description\":\"struct[defau lt][E3][G2PA]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976626-0-496": "Data set that contains information about the activation status of the compass feature.",
    "1056976626-10-496": "Data set that contains information about the activation status of the compass feature.",
    "1056976626-255-414": "Data set that contains information about the activation status of the compass feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976627-0-496": "Data set that contains information about the activation status of the Sport Chrono feature, represented as a boolean value.",
    "1056976627-10-496": "Data set that contains information about the activation status of the Sport Chrono feature, represented as a boolean value.",
    "1056976627-255-414": "Data set that contains information about the activation status of the Sport Chrono feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976630-0-496": "Data set that contains information about the activation status of the cross-traffic warning system.",
    "1056976630-10-496": "Data set that contains information about the activation status of the cross-traffic warning system.",
    "1056976630-255-414": "Data set that contains information about the activation status of the cross-traffic warning system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976634-0-496": "Data set that indicates whether the vehicle's demo mode is activated or deactivated.",
    "1056976634-10-496": "Data set that indicates whether the vehicle is in demo mode.",
    "1056976634-255-414": "Data set that contains information about the activation status and configuration of a demo mode feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976636-0-496": "Data set that provides information about the visualization of the vehicle's width.",
    "1056976636-10-496": "Data set that provides information about the visualization of the vehicle's width.",
    "1056976636-255-414": "Data set that provides information for visualizing the width of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976640-0-498": "Data set that provides information about the status of driving safety features. [off=0,on=1,reseved1=2]",
    "1056976640-10-498": "Data set that provides information about the status of driving safety features. [off=0,on=1,reseved1=2]",
    "1056976640-255-414": "Data set that provides information related to driving safety, structured to include multiple data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976641-0-498": "Data set that provides information about the status of the assisted driving feature. [off=0,on=1,reseved1=2]",
    "1056976641-10-498": "Data set that provides information about the status of the assisted driving feature, indicating whether it is off, on, or in a reserved state. [off=0,on=1,reseved1=2]",
    "1056976641-255-414": "Data set that provides information about the assisted driving feature, including its activation status and associated parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976642-0-504": "Tag Night Mode [DAY=0, NIGHT=1, AUTO=2]",
    "1056976642-10-504": "Data set that provides information about the day, night, or automatic mode setting. [DAY=0, NIGHT=1, AUTO=2]",
    "1056976642-255-414": "Tag Night [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976645-0-261": "Data set that provides information about the activation status of the Travel Assist Augmented Reality (AR) feature. On/Off",
    "1056976645-10-261": "Data set that provides information about the activation status of the Travel Assist Augmented Reality (AR) feature. On/Off",
    "1056976654-1-672": "Data set that indicates whether the vehicle's demo mode is activated or deactivated. [off=0,on=1,reseved1=2]",
    "1056976654-10-672": "Data set that indicates whether the vehicle is operating in demo mode. [off=0,on=1,reseved1=2]",
    "1056976654-11-672": "Data set that indicates whether the vehicle is operating in demo mode. [off=0,on=1,reseved1=2]",
    "1056976654-255-414": "Data set that contains information about the activation status and configuration of a demo mode feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976656-1-672": "Additional Navigation Information [off=0,on=1,reseved1=2]",
    "1056976656-10-672": "Additional Navigation Information [off=0,on=1,reseved1=2]",
    "1056976656-11-672": "Data set that contains additional navigation information. [off=0,on=1,reseved1=2]",
    "1056976656-255-414": "Data set that contains additional navigation information. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976657-255-414": "Data set that provides information about local hazard notifications. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976659-0-672": "Data set that contains information about the activation status of the efficiency card feature. [off=0,on=1,reseved1=2]",
    "1056976659-1-672": "Data set that contains information about the activation status of the efficiency card feature. [off=0,on=1,reseved1=2]",
    "1056976659-10-672": "Data set that contains information about the activation status of the efficiency card feature. [off=0,on=1,reseved1=2]",
    "1056976659-11-672": "Data set that contains information about the activation status of the efficiency card feature. [off=0,on=1,reseved1=2]",
    "1056976659-255-414": "Data set that contains information about the activation status of the efficiency card feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976660-0-672": "Data set that contains information about the activation status of the S-Card functionality. [off=0,on=1,reseved1=2]",
    "1056976660-1-672": "Data set that contains information about the activation status of the S-Card feature. [off=0,on=1,reseved1=2]",
    "1056976660-10-672": "Data set that contains information about the activation status of the S-Card feature. [off=0,on=1,reseved1=2]",
    "1056976660-11-672": "Data set that provides information about the activation status of the S-Card feature. [off=0,on=1,reseved1=2]",
    "1056976660-255-414": "Data set that contains information about the activation status of the S-Card feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976661-0-672": "Data set that contains information about the activation status of the RS-Card feature. [off=0,on=1,reseved1=2]",
    "1056976661-1-672": "Data set that contains information about the activation status of the RS-Card feature. [off=0,on=1,reseved1=2]",
    "1056976661-10-672": "Data set that contains information about the activation status of the RS-Card feature. [off=0,on=1,reseved1=2]",
    "1056976661-11-672": "Data set that contains information about the activation status of the RS-Card functionality. [off=0,on=1,reseved1=2]",
    "1056976661-255-414": "Data set that contains information about the activation status of the RS-Card feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976663-1-671": "Data set that contains information about the activation status of the Head-Up Display (HUD). HUD power state, [off=0, on=1, reserved1=2]",
    "1056976663-10-671": "Data set that contains information about the activation status of the Head-Up Display (HUD). HUD power state, [off=0, on=1, reserved1=2]",
    "1056976663-11-671": "Data set that contains information about the activation status of the Head-Up Display (HUD). HUD power state, [off=0, on=1, reserved1=2]",
    "1056976663-255-414": "Data set that contains information about the activation status of the Head-Up Display (HUD). [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976665-1-669": "Hud Image Rotation -100% = left image rotation; +100% = right image rotation",
    "1056976665-10-669": "Data set that contains information about the rotation settings of the Head-Up Display (HUD), where the rotation can be adjusted to the left or right within a defined range. -100% = left image rotation; +100% = right image rotation",
    "1056976665-11-669": "Data set that contains information about the rotation angle of the Head-Up Display (HUD) image, indicating adjustments for left or right rotation. -100% = left image rotation; +100% = right image rotation",
    "1056976665-255-414": "Data set that contains information about the configuration of the head-up display (HUD) image rotation. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976669-0-714": "Content Mode View [ AR=0, TILT_ANGLE=1, rs=2, reserved1=3 , reserved1=4 ]",
    "1056976669-10-714": "Content Mode View [ AR=0, TILT_ANGLE=1, rs=2, reserved1=3 , reserved1=4 ]",
    "1056976669-255-414": "Data set that provides information about the content mode or view settings, structured to include multiple data types and parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976901-0-157": "Hud Image Rotation 0% - 100%",
    "1056976901-10-157": "Data set that contains information about the rotation angle of the Head-Up Display (HUD) in percentage values. 0% - 100%",
    "1056976901-255-414": "Data set that contains information about the rotation settings of the Head-Up Display (HUD). [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976903-0-9": "Data set that contains information about the activation status of the alternative color design feature. On/Off",
    "1056976903-10-9": "Data set that contains information about the activation status of the alternative color design feature. On/Off",
    "1056976904-0-9": "Data set that contains information about the activation status of the color adjustment for day/night dimming. On/Off",
    "1056976904-10-9": "Data set that contains information about the activation status of the color adjustment for day/night dimming. On/Off",
    "1056976905-0-9": "Data set that contains information about the activation status of the Head-Up Display (HUD). On/Off",
    "1056976905-10-9": "Data set that contains information about the activation status of the Head-Up Display (HUD). On/Off",
    "1056976905-255-414": "Data set that contains information about the activation status of the Head-Up Display (HUD). [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976909-0-9": "Data set that provides information about the activation status of warning messages. On/Off",
    "1056976909-10-9": "Data set that provides information about the activation status of warning messages. On/Off",
    "1056976909-255-414": "Data set that provides information about warning messages, structured to include multiple data points with specific attributes such as data type, range, and default values. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976911-0-9": "Data set that contains information about the activation status of the traffic sign recognition feature. On/Off",
    "1056976911-10-9": "Data set that contains information about the activation status of the traffic sign recognition feature. On/Off",
    "1056976913-0-9": "Eco Status On/Off",
    "1056976913-10-9": "Eco Status On/Off",
    "1056976913-255-414": "Eco Settings [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1056976915-0-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-1-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-10-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-11-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-12-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-2-160": "Individual Preset Content Area See BAP catalog",
    "1056976915-255-550": "Data set that contains information about the configuration of an individual preset for a specific content area within the vehicle. [{datatype:\", description\":\"struct[defau lt][G2PA][E3]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976916-0-161": "Layout Enum value: 0-4",
    "1056976916-1-503": "Layout",
    "1056976916-10-161": "Layout Enum value: 0-4",
    "1056976916-11-503": "Data set that contains information about the layout configuration, represented as an integer value.",
    "1056976916-255-551": "Data set that contains information about the layout configuration, structured as a data type. [{datatype:\", description\":\"struct[defau lt][default PPE]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976917-0-162": "Status Bar On/Off",
    "1056976919-0-162": "Angle Analog Display On/Off",
    "1056976921-0-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-1-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-10-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-11-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-12-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-2-160": "Individual Preset Content Area See BAP catalog",
    "1056976921-255-550": "Data set that contains information about the configuration of an individual preset for a specific content area. [{datatype:\", description\":\"struct[defau lt][G2PA][E3]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976922-0-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-1-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-10-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-11-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-12-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-2-160": "Individual Preset Content Area See BAP catalog",
    "1056976922-255-550": "Data set that contains information about the configuration of an individual preset for a specific content area within the vehicle. [{datatype:\", description\":\"struct[defau lt][G2PA][E3]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1056976923-0-365": "Temporary Content",
    "1056976924-0-367": "Data set that provides information about the visualization settings for the phone list, specifying whether and where the phone list is displayed, such as in the head-up display (HUD), the virtual cockpit, or both. 0x0 = no_visualisation, 0x1 = visualisation_in_HUD, 0x2 = visualisation_in_virtualco ckpit, 0x3 = visualisation_in_HUD_an d_cockpit",
    "1056976925-0-367": "Data set that provides information about the visualization settings for call notifications, specifying whether and where the notifications are displayed, such as in the head-up display (HUD), the virtual cockpit, or both. 0x0 = no_visualisation, 0x1 = visualisation_in_HUD, 0x2 = visualisation_in_virtualco ckpit, 0x3 = visualisation_in_HUD_an d_cockpit",
    "1056976925-10-367": "Data set that provides information about the visualization settings for call notifications, specifying whether and where the notifications are displayed, such as in the head-up display (HUD), the virtual cockpit, or both. 0x0 = no_visualisation, 0x1 = visualisation_in_HUD, 0x2 = visualisation_in_virtualco ckpit, 0x3 = visualisation_in_HUD_an d_cockpit",
    "1056976925-255-414": "Data set that provides information about the configuration and status of a call-related pop-up feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1157640905-255-12": "Data set that contains structured information about preset configurations for the human-machine interface (HMI). struct { Variante 0, Variante 1}",
    "1157640906-0-163": "Data set that contains information about satellite digital audio radio service (SDARS) presets, including details such as channel identifiers, channel numbers, and channel names. struct { SDARS.ChannelSID: int32; SDARS.ChannelNumber: int64; SDARS.ChannelName[16 ]: utf8; }[36]",
    "117444545-255-5": "Data set that contains information about the longitudinal adjustment position of the front driver's seat. 1 ‰ / 0-1000 ‰",
    "117444546-255-5": "Data set that contains information about the height adjustment of the front driver's seat. 1 ‰ / 0-1000 ‰",
    "117444548-0-5": "Driver Seat Cushion Depth Adjustment 1 ‰ / 0-1000 ‰",
    "117444549-0-369": "Data set that provides information about the front seat adjustment reserve. n/a",
    "117444550-0-369": "Data set that provides information about the front seat adjustment reserve. n/a",
    "117444555-0-6": "Data set that contains information about the lumbar support width adjustment position for the front driver's seat. 1 % / 0-100 %",
    "117444556-0-370": "Data set that provides information about lumbar support adjustment reserve. n/a",
    "117444557-0-370": "Data set that provides information about lumbar support adjustment reserve. n/a",
    "117444558-0-370": "Data set that provides information about lumbar support adjustment reserve. n/a",
    "117444559-0-370": "Data set that provides information about lumbar support adjustment reserve. n/a",
    "117444560-0-370": "Data set that provides information about lumbar support adjustment reserve. n/a",
    "117444565-0-371": "Data set that provides information about the horizontal headrest adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444565-0-5": "Driver Seat Headrest Horizontal Adjustment Reserve 1 ‰ / 0-1000 ‰",
    "117444566-0-371": "Data set that provides information about the headrest adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444567-0-371": "Data set that provides information about the headrest tilt adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444568-0-371": "Data set that provides information about the seat back adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444569-0-371": "Data set that provides information about the seat back adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444570-0-371": "Data set that provides information about the seat back adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444571-0-371": "Data set that provides information about the seat back adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444572-0-369": "Data set that provides information about adjustment of seatbelt height. n/a",
    "117444572-0-5": "Data set that contains information about the height adjustment of the front driver's seatbelt. 1 ‰ / 0-1000 ‰",
    "117444573-0-371": "Data set that provides information about the front seat adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444574-0-371": "Data set that provides information about the front seat adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444575-0-371": "Data set that provides information about the front seat adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444576-0-371": "Data set that provides information about the front seat adjustment reserve. 0x0 0km/h 0x1 1km/h … 0x145 325km/h",
    "117444585-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444586-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444587-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444588-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444589-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444590-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444591-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444592-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444593-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444594-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444595-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444596-0-374": "Data set that provides information about seat back massage adjustment reserve. n/a",
    "117444627-0-379": "Data set that provides information about the front seat massageintensity adjustment reserve. 0x0 0% 0 (dec) = 0% 1 (dec) = 1% ... 100 (dec) = 100%",
    "117444641-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444642-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444643-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444644-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444645-0-8": "Data set that provides information about the activation status of the child seat safety function in the vehicle. 0 = off; 1 = on",
    "117444646-0-29": "Data set that provides information about the activation status of a seat function in the vehicle. 0 = off; 1 = on",
    "117444646-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444647-0-29": "Data set that provides information about the activation status of a specific seat function in the vehicle. 0 = off; 1 = on",
    "117444647-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444648-0-29": "Data set that provides information about the activation status of a seat function in the vehicle. 0 = off; 1 = on",
    "117444648-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117444649-0-29": "Data set that contains information about the activation status of a seat function in the vehicle. 0 = off; 1 = on",
    "117444649-0-373": "Data set that provides information about seat adjustment reserve. n/a",
    "117445413-0-54": "Position X 1 % / 0-100 %",
    "117445413-1-54": "Position X 1 % / 0-100 %",
    "117445414-0-54": "Position Z 1 % / 0-100 %",
    "117445414-1-54": "Position Z 1 % / 0-100 %",
    "12-volt-li-battery-soh.modelDataEntries.BEM_Batteriediagnose.doubleValue": "12V battery warning",
    "12-volt-li-battery-soh.modelDataEntries.BZE_SOH_P.doubleValue": "battery_aging_power: Po wer-related health of the battery",
    "12-volt-li-battery-soh.modelDataEntries.BZE_SOH_Q.doubleValue": "battery_aging_capacity: Capacity-related health of the battery",
    "12-volt-li-battery-soh.modelDataEntries.NVEM_Batterie_Service.doubleValue": "12V battery warning",
    "12-volt-li-battery-soh.modelDataEntries.NVEM_Bordnetzdiagnose.doubleValue": "12V battery warning",
    "12-volt-li-battery-soh.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "1224751769-0-166": "Data set that contains information about the selected preset bank. 0=… ;",
    "1224751772-0-170": "Online Linking Preference 2: off; 1: manual; 0: automatic",
    "1224751772-1-557": "Online Linking Preference Android",
    "1224751772-10-170": "Data set that contains information about the user's preference for online linking settings, which can be configured as off, manual, or automatic. 2: off; 1: manual; 0: automatic",
    "1224751772-11-557": "Online Linking Preference Android",
    "1224751772-255-414": "Data set that contains information about the online linking preference settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751773-0-171": "Online Audio Quality 1: low; 0: high",
    "1224751773-1-556": "Online Audio Quality Android",
    "1224751773-10-171": "Online Audio Quality 1: low; 0: high",
    "1224751773-11-556": "Online Audio Quality Android",
    "1224751773-255-414": "Data set that contains information about the configuration of online audio quality settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751775-0-173": "Data set that contains information about the sorting preferences for FM station lists. 0: Alphabetic; 1: Grouped; 2: Frequency; 3: Genre; 4: HdRadioFirst",
    "1224751776-0-29": "Data set that contains information about the activation status of the FM HD Radio feature. 0 = off; 1 = on",
    "1224751776-1-555": "Fm Hd Radio Activation Android",
    "1224751776-10-29": "Data set that contains information about the activation status of the FM HD Radio feature. 0 = off; 1 = on",
    "1224751776-11-555": "Fm Hd Radio Activation Android",
    "1224751776-255-414": "Data set that contains information about the activation status of the FM HD Radio feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751777-0-29": "Data set that contains information about the activation status of the HD radio feature in the vehicle. 0 = off; 1 = on",
    "1224751777-1-555": "Setting Am Hd Radio Activation Android",
    "1224751777-10-29": "Data set that contains information about the activation status of the AM HD Radio feature. 0 = off; 1 = on",
    "1224751777-11-555": "Setting Am Hd Radio Activation Android",
    "1224751777-255-414": "Data set that contains information about the activation status of the AM HD Radio setting. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751780-0-176": "Data set that contains information about the FM alternative frequency setting, indicating whether it is enabled or disabled. 0 = off; 1 = on",
    "1224751781-0-29": "Additional Online Metadata 0 = off; 1 = on",
    "1224751781-1-555": "Additional Online Metadata Android",
    "1224751781-10-29": "Additional Online Metadata 0 = off; 1 = on",
    "1224751781-11-555": "Additional Online Metadata Android",
    "1224751781-255-414": "Data set that contains additional online metadata settings, structured as a composite data type. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751783-0-178": "Sxm Sports Seek Notifications 0 = off; 1 = on",
    "1224751784-0-179": "Rds Regional Setting 1: Fixed; 2: Automatic",
    "1224751785-0-180": "Data set that contains information about the configuration of arrow key functionality, specifying whether it is set to navigate through a station list or a preset list. 1: Station List; 0: Preset List",
    "1224751786-0-8": "Data set that contains information about the activation status of the traffic program setting. 0 = off; 1 = on",
    "1224751786-1-559": "Traffic Program Settings Android",
    "1224751786-10-8": "Traffic Program Settings 0 = off; 1 = on",
    "1224751786-11-559": "Traffic Program Settings Android",
    "1224751786-255-414": "Data set that contains information about the settings related to the traffic program. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751787-0-29": "Data set that contains information about the activation status of the radio text feature. 0 = off; 1 = on",
    "1224751789-0-29": "Data set that contains information about the activation status of other announcements, represented as a boolean value. 0 = off; 1 = on",
    "1224751789-1-555": "Other Announcements Settings Android",
    "1224751789-10-29": "Other Announcements 0 = off; 1 = on",
    "1224751789-11-555": "Setting Other Announcements Android",
    "1224751789-255-414": "Data set that contains configuration settings related to other announcements. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751791-0-182": "Data set that contains information about the activation status of the Radio Data System (RDS) feature. 0 = off; 1 = on",
    "1224751792-0-29": "Data set that contains information about the automatic selection of station logos, indicating whether this feature is enabled or disabled. 0 = off; 1 = on",
    "1224751793-0-183": "Logo Region Setting -1 = no user selection; 1= automatic; 2-255: specific country or region according to the data base mapping",
    "1224751796-0-29": "Data set that contains information about the activation status of emergency notifications in the vehicle. 0 = off; 1 = on",
    "1224751797-0-185": "Show Stations Settings Data type: byte, '1: FM, 0: FM/DAB",
    "1224751798-0-29": "Setting DAB Soft Linking 0 = off; 1 = on",
    "1224751798-1-555": "Setting DAB Soft Linking Android",
    "1224751798-10-29": "Data set that contains information about the activation status of the DAB (Digital Audio Broadcasting) soft linking feature. 0 = off; 1 = on",
    "1224751798-11-555": "Setting DAB Soft Linking Android",
    "1224751798-255-414": "Data set that contains information about the configuration of the Digital Audio Broadcasting (DAB) soft linking feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751799-0-186": "Data set that specifies the sorting preference for a DAB (Digital Audio Broadcasting) station list, indicating whether the stations are sorted alphabetically or by genre. false: alphabet; true: genre",
    "1224751800-0-29": "Data set that contains information about the activation status of the service linking feature between DAB (Digital Audio Broadcasting) and FM (Frequency Modulation) radio. 0 = off; 1 = on",
    "1224751801-0-29": "Data set that contains information about the activation status of a service related to following DAB (Digital Audio Broadcasting) signals. 0 = off; 1 = on",
    "1224751803-0-188": "Data set that contains information about the last situation mode (LSM) for Digital Audio Broadcasting (DAB). This includes details such as ensemble identifiers, service identifiers, slideshow availability, program type codes, and both full and short names of the service. struct { DABLSM.ECC: int16;, DABLSM.sid: int32;, DABLSM.serviceId: int8;, DABLSM.EnsID: int32;, DABLSM.slideShow: boolean;, DABLSM.name[16]: utf8;, DABLSM.shortName[16]: utf8;, DABLSM.ptyCode: int32;, }",
    "1224751804-0-16": "Data set that contains information about the last situation mode related to web-based media playback, including details such as the name of the media, station identification, whether the media is a podcast, the episode key, and the current playback position of the episode. struct { WEBLSM.name[16]: utf8; WEBLSM.stationId: int64; WEBLSM.WebIsPodcast: boolean; WEBLSM.WebEpisodeKey : int32; WEBLSM.WebEpisodeCur rentPlayPosition: int64; }",
    "1224751804-1-17": "Data set that contains information about the last situation mode (LSM) related to web-based media. This includes details such as the name of the media, station identification, podcast status, episode key, current play position, search path, country, language, genre, and whether the data pertains to the last situation mode. struct { WEBLSM.name[128]: utf8; WEBLSM.stationId: int64; WEBLSM.WebIsPodcast: boolean; WEBLSM.WebEpisodeKey : int32; WEBLSM.WebEpisodeCur rentPlayPosition: int64; WEBLSM.SearchPath[512 ]: utf8;",
    "1224751804-255-18": "Data set that contains information about the last situation mode, represented as a structured data type with predefined variants. struct { Variante 0, Variante 1}",
    "1224751805-0-189": "Data set that contains information about the last situation mode (LSM) for the Satellite Digital Audio Radio Service (SDARS), including details such as channel identifier, channel number, channel name, and associated weblink. struct { SDARSLSM.ChannelSID: uint16;, SDARSLSM.ChannelNum ber: uint32;, SDARSLSM.ChannelNam e[16]: utf8;, SDARSLSM.Weblink[255]: utf8;, }",
    "1224751806-0-190": "Data set that contains structured information, including a frequency value and a name field, related to AM_TI LSM. struct { AM_TILSM.freq: int64;, AM_TILSM.name[16]: utf8;, }",
    "1224751807-0-191": "Data set that contains information about the preferred view settings for a display, represented as an integer value. 0= cover art; 1= slideshow; 2= station logo",
    "1224751807-1-560": "Preferred View Settings Android",
    "1224751807-10-191": "Data set that contains information about the preferred view settings for a display, where the configuration options are represented by numerical values. 0= cover art; 1= slideshow; 2= station logo",
    "1224751807-11-560": "Preferred View Settings Android",
    "1224751807-255-414": "Data set that contains information about the preferred view settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751808-0-29": "Data set that contains information about the activation status of the SiriusXM seek notification feature. 0 = off; 1 = on",
    "1224751809-0-29": "Data set that contains information about the activation status of the \"SxmTuneStart\" setting, represented as a boolean value. 0 = off; 1 = on",
    "1224751813-0-397": "Data set that contains information about the last situation mode (LSM) for SDARS (Satellite Digital Audio Radio Service). This data set includes details such as the channel SID (Service Identifier), channel number, channel name, associated weblink, station description, and logo path. The data is structured and utilizes specific data types, including unsigned integers and UTF-8 encoded strings, to represent the respective fields. struct { SDARSLSM.ChannelSID: uint16;, SDARSLSM.ChannelNum ber: uint32;, SDARSLSM.ChannelNam e[16]: utf8;,",
    "1224751815-0-399": "Setting Genres 0 = off; 1 = on",
    "1224751815-1-555": "Setting Genres Android",
    "1224751815-10-399": "Setting Genres 0 = off; 1 = on",
    "1224751815-11-555": "Setting Genres Android",
    "1224751815-255-414": "Data set that contains structured information about configurable settings, including a boolean value and an unsigned 8-bit integer. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751816-0-400": "Data set that contains information about the activation status of an alarm setting. 0 = off; 1 = on",
    "1224751816-1-559": "Setting Alarm Android",
    "1224751816-10-400": "Setting Alarm 0 = off; 1 = on",
    "1224751816-11-559": "Setting Alarm Android",
    "1224751816-255-414": "Data set that contains information about the alarm settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751820-1-595": "Sirius Xm 360l ASCII",
    "1224751820-11-595": "Sirius Xm 360l ASCII",
    "1224751820-255-414": "Data set that provides information about the Sirius XM 360L feature, including its activation status and associated configuration parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1224751822-0-399": "Data set that contains information about the activation status of the DAB slideshow feature. 0 = off; 1 = on",
    "1224751823-0-732": "Tagging Provider 0=none; 1=spotify, 2=amazonMusic, 3=appleMusic, 4=deezer",
    "1224751823-10-732": "Tagging Provider Settings 0=none; 1=spotify, 2=amazonMusic, 3=appleMusic, 4=deezer",
    "1258307203-0-22": "Data set that contains information about whether roads restricted by time or season should be avoided. On/Off",
    "1258307203-1-23": "Data set that contains information about the preference for avoiding roads that are restricted based on time or season. 0=avoid 1=allow 2=automatic",
    "1258307203-10-22": "Data set that indicates whether roads restricted by time or season should be avoided. On/Off",
    "1258307203-11-23": "Data set that contains information about the preference for avoiding roads that are restricted based on time or season. 0=avoid 1=allow 2=automatic",
    "1258307203-255-24": "Data set that contains information about roads to be avoided based on time and seasonal restrictions. Struct[VW][AUDI]",
    "1258307211-0-196": "Data set that provides information about the activation status of the fuel tank warning. On/Off",
    "1258307211-10-196": "Data set that provides information about the activation status of the fuel tank warning indicator. On/Off",
    "1258307211-255-414": "Data set that provides information about the fuel tank warning status. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307216-0-198": "Data set that contains information about the start time and arrival time. Start time; Arrival time",
    "1258307216-10-198": "Time Display Start time; Arrival time",
    "1258307216-255-414": "Data set that provides information about the time display settings, including its activation status and additional configuration parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307217-0-230": "Data set that provides information about the activation status of the Porsche Charging Planner feature. 0=off 1=on",
    "1258307217-10-230": "Data set that provides information about the activation status of the Porsche Charging Planner feature. 0=off 1=on",
    "1258307217-255-414": "Data set that provides information about the Porsche Charging Planner, including its configuration and operational parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307218-0-235": "Data set that contains information about whether the prioritization of the Porsche Charging Service is enabled or disabled. 0=off 1=on",
    "1258307218-10-235": "Data set that contains information about whether the prioritization of the Porsche Charging Service is enabled or disabled. 0=off 1=on",
    "1258307218-255-414": "Data set that contains information about the prioritization of the Porsche Charging Service. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307220-0-230": "Data set that contains information about the automatic state of charge (SOC) selection upon arrival, indicating whether this feature is enabled or disabled. 0=off 1=on",
    "1258307220-10-230": "Data set that contains information about the automatic state of charge (SOC) selection upon arrival, indicating whether this feature is enabled or disabled. 0=off 1=on",
    "1258307220-255-414": "Data set that provides information about the automatic state of charge (SOC) selection upon arrival. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307224-0-196": "Kanban Display Signs On/Off",
    "1258307225-0-196": "Data set that contains information about the activation status of traffic sign display functionality for specific regions. On/Off",
    "1258307226-0-202": "Traffic Signs Display 0=visualsound; 1=visual; 2=off",
    "1258307227-0-203": "Data set that contains information about traffic signs in Japan, represented in a format that specifies the mode of representation, such as visual and/or sound. 0=visualsound; 1=visual; 2=off",
    "1258307228-0-204": "Traffic Signs Korea 0;1;2..99, xx;yyy;zzz…",
    "1258307229-0-205": "Data set that provides information about the input mode for destination entry in the human-machine interface (HMI). 0=SingleLine; 1=StepbyStep",
    "1258307233-0-196": "Etc Upload On/Off",
    "1258307233-10-196": "Etc Upload On/Off",
    "1258307233-255-414": "Data set that contains information related to the ETC 2.0 upload functionality. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307235-0-196": "Data set that provides information about the activation status of the ETC expiration warning. On/Off",
    "1258307236-0-207": "Data set that provides information about the toll fee notification status, indicating its current state. 0;1;2 (3 states: - on, visual, off)",
    "1258307236-10-207": "Data set that provides information about the toll fee notification status, indicating its current state. 0;1;2 (3 states: - on, visual, off)",
    "1258307236-255-414": "Data set that provides information about toll fee notifications. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307237-0-207": "Data set that provides information about the reminder status for an ETC card not being inserted, including its operational states. 0;1;2 (3 states: - on, visual, off)",
    "1258307237-10-207": "Data set that provides information about the reminder status for an ETC card not being inserted, including its operational states. 0;1;2 (3 states: - on, visual, off)",
    "1258307237-255-414": "Data set that provides information about the reminder status for an ETC (Electronic Toll Collection) card not being inserted. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307238-0-207": "Data set that provides information about the reminder status for an ETC card that remains inserted in the vehicle. 0;1;2 (3 states: - on, visual, off)",
    "1258307238-10-207": "Data set that provides information about the reminder status for an ETC card that remains inserted in the vehicle. 0;1;2 (3 states: - on, visual, off)",
    "1258307238-255-414": "Data set that provides a reminder indicating whether an ETC (Electronic Toll Collection) card is still inserted in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307239-0-196": "Data set that provides information about the activation status of a notification related to crossing a country border. On/Off",
    "1258307239-10-196": "Data set that provides information about the activation status of a notification related to crossing a country border. On/Off",
    "1258307239-255-414": "Data set that provides information about the indication of a country border. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307240-0-208": "Data set that contains information about the map configuration, offering three different styles for regions such as Japan and Korea. Map configuration (three different styles)",
    "1258307241-0-209": "Data set that contains information about the font size settings for map text, specifically for regions such as Japan and Korea. This data set allows for adjustments in text size, including default, smaller, and larger options. font size for map texts (default, smaller, larger)",
    "1258307242-0-210": "Data set that contains information about the route color settings for navigation maps, specifically for regions such as Japan and Korea. Route color (default, color 2, color 3)",
    "1258307243-0-196": "Data set that provides information about the activation status of the map traffic layer. On/Off",
    "1258307244-0-158": "Resumable Tour Targets â  Da 42",
    "1258307245-0-137": "Volatile Import Targets â  DataF 42",
    "1258307246-0-196": "Data set that provides information about the activation status of VICS traffic event notifications within the map settings. On/Off",
    "1258307246-10-196": "Data set that contains information about the activation status of VICS traffic event notifications in the map settings. On/Off",
    "1258307246-255-414": "Data set that contains information about VICS (Vehicle Information and Communication System) traffic event notifications in the map settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307248-0-211": "Data set that provides information about the active single map renderer, indicating whether the main unit or the instrument cluster is currently in use. 0:Main unit, 1:Kombi",
    "1258307253-0-230": "Data set that provides information about whether the display of country information is enabled or disabled. 0=off 1=on",
    "1258307253-10-230": "Data set that provides information about whether the display of country information is enabled or disabled. 0=off 1=on",
    "1258307253-255-414": "Data set that provides information about the display of country-specific settings or information. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307254-0-230": "Data set that contains information about the activation status of the speed limit display functionality. 0=off 1=on",
    "1258307254-10-230": "Data set that contains information about whether the speed limit display feature is activated or deactivated. 0=off 1=on",
    "1258307254-255-414": "Data set that provides information about the visibility status of the speed limit display, which is part of the traffic sign recognition system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307255-0-588": "Hi Pass 0=off; 1=on",
    "1258307255-10-588": "Data set that contains information about the activation status of the HI Pass feature, represented as a boolean value. 0=off; 1=on",
    "1258307255-255-414": "Data set that contains information about the configuration of the \"HI Pass\" feature, structured with multiple data fields. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307257-0-587": "Data set that provides information about the activation status of the emergency warning system. 0=off; 1=on",
    "1258307257-10-587": "Data set that provides information about the activation status of the emergency warning system. 0=off; 1=on",
    "1258307258-0-589": "Data set that provides information about the activation status of the traffic minimap popup feature. 0=off; 1=on",
    "1258307258-10-589": "Data set that provides information about the activation status of the traffic minimap popup feature. 0=off; 1=on",
    "1258307258-255-414": "Data set that provides information about the configuration of a traffic minimap popup feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307259-0-586": "Data set that provides information about the maximum travel speed setting for an electric vehicle. Range of Values 0..254;step: 1 unit:km/h; 255 reserved OFF",
    "1258307259-10-586": "Data set that provides information about the maximum travel speed of an electric vehicle (EV). Range of Values 0..254;step: 1 unit:km/h; 255 reserved OFF",
    "1258307259-255-414": "Data set that provides information about the maximum travel velocity for an electric vehicle (EV). [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307264-0-650": "Traffic Overview 0-Traffic small map 1-Traffic status bar",
    "1258307264-10-650": "Data set that provides information about traffic conditions, including a small traffic map and a traffic status bar. 0-Traffic small map 1-Traffic status bar",
    "1258307264-255-414": "Data set that provides an overview of traffic-related information. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307266-0-651": "Data set that contains information about the map color settings, which determine the display mode based on predefined options such as day, night, or automatic mode. 0-Day; 1-Night; 2-Auto",
    "1258307267-0-652": "Data set that provides information about the activation status of traffic display on the map. On/Off",
    "1258307267-10-652": "Data set that provides information about the activation status of traffic display on the map. On/Off",
    "1258307267-255-414": "Data set that provides information about the traffic displayed on the map. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307269-0-652": "Data set that contains information about the activation status of the autozoom feature, represented as a boolean value indicating whether the feature is turned on or off. On/Off",
    "1258307270-0-652": "Data set that contains information about the activation status of the online map functionality. On/Off",
    "1258307270-10-652": "Data set that contains information about the activation status of the online map feature. On/Off",
    "1258307270-255-414": "Data set that contains information about the online map settings, structured to include multiple data fields with specific attributes such as data type, range, step size, default value, and data length. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307278-0-661": "Data set that provides information about traffic conditions, including a small map representation and a traffic status bar. 0-Traffic small map 1-Traffic status bar",
    "1258307281-0-654": "Data set that contains information about the activation status of the online map feature. On/Off",
    "1258307285-0-667": "Data Collection Frontend On/Off",
    "1258307285-10-667": "Data Collection Frontend On/Off",
    "1258307285-255-414": "Data Collection Frontend [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307287-0-667": "Data Collection Backend On/Off",
    "1258307287-10-667": "Data Collection Backend On/Off",
    "1258307287-255-414": "Data Collection Backend [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307293-0-661": "Traffic Overview PID 0-Traffic small map 1-Traffic status bar",
    "1258307293-10-661": "Data set that provides information about the traffic overview, including details such as the display of a small traffic map or a traffic status bar. 0-Traffic small map 1-Traffic status bar",
    "1258307293-255-414": "Data set that provides an overview of traffic-related information, structured to include multiple data points with specific attributes such as data type, range, and default values. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307295-0-679": "Data set that contains information about the map color settings, which determine the display mode based on predefined conditions such as day, night, or automatic adjustment. 0-Day; 1-Night; 2-Auto",
    "1258307295-10-679": "Data set that contains information about the map color settings, which can be configured for different modes such as day, night, or automatic adjustment. 0-Day; 1-Night; 2-Auto",
    "1258307295-255-414": "Data set that contains information about the configuration of map color settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307296-0-667": "Data set that contains information about the activation status of traffic display on the map. On/Off",
    "1258307296-10-667": "Data set that provides information about the activation status of traffic display on the map. On/Off",
    "1258307296-255-414": "Data set that provides information about the traffic display settings on the map, including parameters for enabling or disabling the feature and additional configuration details. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307298-0-667": "Data set that contains information about the activation status of the autozoom feature, represented as a boolean value indicating whether the feature is turned on or off. On/Off",
    "1258307298-10-667": "Data set that contains information about the activation status of the autozoom feature, represented as a boolean value indicating whether the feature is turned on or off. On/Off",
    "1258307298-255-414": "Data set that contains information about the autozoom functionality, including its activation status and associated parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307299-0-667": "Data set that contains information about the activation status of the online map functionality. On/Off",
    "1258307299-10-667": "Data set that contains information about the activation status of the online map functionality. On/Off",
    "1258307299-255-414": "Data set that contains information about the online map PID, structured as a combination of data fields with specific attributes such as data type, range, step size, and default values. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307301-0-8": "Satellite Maps Main 0 = off; 1 = on",
    "1258307301-10-8": "Satellite Maps Main 0 = off; 1 = on",
    "1258307301-255-414": "Data set that provides information about the main settings for satellite maps. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307302-0-8": "Satellite Maps Cluster 0 = off; 1 = on",
    "1258307302-10-8": "Satellite Maps Cluster 0 = off; 1 = on",
    "1258307302-255-414": "Data set that contains information related to satellite map settings, structured to include multiple data fields. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307305-0-29": "Data set that provides information about the activation status of traffic incident notifications. 0 = off; 1 = on",
    "1258307305-10-29": "Data set that provides information about the main status of traffic incidents, indicating whether it is active or inactive. 0 = off; 1 = on",
    "1258307305-255-414": "Data set that provides information about traffic incidents, structured to include multiple data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307306-0-29": "Data set that provides information about the activation status of the traffic incidents cluster. 0 = off; 1 = on",
    "1258307306-10-29": "Data set that provides information about the activation status of the traffic incidents cluster. 0 = off; 1 = on",
    "1258307306-255-414": "Data set that provides information about traffic incidents, structured as a cluster containing multiple data fields. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307307-0-29": "Data set that provides information about the main traffic information setting, indicating whether it is enabled or disabled. 0 = off; 1 = on",
    "1258307307-10-29": "Data set that contains information about the main traffic information setting, indicating whether it is enabled or disabled. 0 = off; 1 = on",
    "1258307307-255-414": "Data set that provides information about the main traffic data, structured to include multiple data points with specific attributes such as data type, range, and default values. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307308-0-29": "Data set that provides information about the activation status of traffic information in the instrument cluster. 0 = off; 1 = on",
    "1258307308-10-29": "Data set that provides information about the activation status of the traffic information cluster. 0 = off; 1 = on",
    "1258307308-255-414": "Data set that provides information about traffic-related data displayed in the instrument cluster. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307309-0-213": "Data set that provides information about the current mode of operation for day or night settings, including automatic, day, or night modes. 0=automatic 1=day 2=night",
    "1258307309-10-213": "Data set that provides information about the current mode of the vehicle's display or lighting system, indicating whether it is set to automatic, day, or night mode. 0=automatic 1=day 2=night",
    "1258307309-255-414": "Data set that provides information about the day/night mode settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307310-0-214": "Data set that contains information about the map orientation settings in the vehicle's navigation system. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307310-10-214": "Data set that contains information about the map orientation settings in the vehicle's navigation system. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307310-255-414": "Data set that provides information about the main map orientation settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307311-0-214": "Data set that contains information about the map orientation settings in the vehicle's instrument cluster. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307311-10-214": "Data set that contains information about the map orientation settings displayed in the vehicle's instrument cluster. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307311-255-414": "Data set that provides information about the map orientation settings in the vehicle's instrument cluster. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307312-0-215": "Data set that provides information about the automatic zoom settings, including modes such as on, intersection, and off. 0=on; 1=intersection, 2=off",
    "1258307312-10-215": "Data set that provides information about the automatic zoom settings, including modes such as on, intersection, and off. 0=on; 1=intersection, 2=off",
    "1258307312-255-414": "Data set that contains information about the automatic zoom functionality, including its activation status and associated parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307313-0-215": "Data set that provides information about the automatic zoom settings for the cluster display. 0=on; 1=intersection, 2=off",
    "1258307313-10-215": "Data set that provides information about the automatic zoom settings for the cluster display. 0=on; 1=intersection, 2=off",
    "1258307313-255-414": "Data set that contains information about the automatic zoom functionality in the vehicle's instrument cluster. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307314-0-29": "Data set that contains information about the activation status of the 3D sight main feature in map content. 0 = off; 1 = on",
    "1258307314-10-29": "Data set that contains information about the activation status of the 3D sight main feature in the map content. 0 = off; 1 = on",
    "1258307314-255-414": "Data set that contains information about the 3D sight main settings for map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307315-0-29": "Data set that contains information about the activation status of the 3D sight cluster in map content. 0 = off; 1 = on",
    "1258307315-10-29": "Data set that contains information about the activation status of the 3D sight cluster in map content. 0 = off; 1 = on",
    "1258307315-255-414": "Data set that contains information about the 3D sight cluster within map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307316-0-29": "Data set that contains information about the activation status of 3D city models in map content. 0 = off; 1 = on",
    "1258307316-10-29": "Data set that contains information about the activation status of 3D city models in map content. 0 = off; 1 = on",
    "1258307316-255-414": "Data set that contains information about the availability and configuration of 3D city models for map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307317-0-29": "Data set that contains information about the activation status of 3D city models in map content. 0 = off; 1 = on",
    "1258307317-10-29": "Data set that contains information about the activation status of 3D city models in map content. 0 = off; 1 = on",
    "1258307317-255-414": "Data set that contains information about the availability and configuration of 3D city models within the map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307318-0-29": "Data set that contains information about the activation status of favorite destinations in the map content. 0 = off; 1 = on",
    "1258307318-10-29": "Data set that contains information about the activation status of the main favorite destinations in the map content. 0 = off; 1 = on",
    "1258307318-255-414": "Data set that contains information about favorite destinations stored in the main map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307319-0-29": "Data set that contains information about the activation status of the favorite destinations cluster in the map content. 0 = off; 1 = on",
    "1258307319-10-29": "Data set that contains information about the activation status of the favorite destinations cluster within the map content. 0 = off; 1 = on",
    "1258307319-255-414": "Data set that contains information about favorite destinations within a map content cluster. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307320-0-29": "Data set that contains information about the activation status of the main weather content on the map. 0 = off; 1 = on",
    "1258307320-10-29": "Data set that contains information about whether the weather overlay on the map content is enabled or disabled. 0 = off; 1 = on",
    "1258307320-255-414": "Data set that provides information about the primary weather content displayed on the map. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307321-0-29": "Data set that contains information about the activation status of weather-related map content, represented as a boolean value. 0 = off; 1 = on",
    "1258307321-10-29": "Data set that contains information about the activation status of weather cluster map content. 0 = off; 1 = on",
    "1258307321-255-414": "Data set that contains information about weather-related map content, structured to include multiple data points for detailed representation. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307322-0-29": "Data set that contains information about the activation status of the main Points of Interest (POI) map content. 0 = off; 1 = on",
    "1258307322-10-29": "Data set that contains information about the activation status of the main Points of Interest (POI) map content. 0 = off; 1 = on",
    "1258307322-255-414": "Data set that contains information about map content, specifically related to Points of Interest (POI) main settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307323-0-29": "Data set that contains information about the activation status of point-of-interest (POI) clustering on the map. 0 = off; 1 = on",
    "1258307323-10-29": "Data set that contains information about the activation status of point-of-interest (POI) clustering on the map. 0 = off; 1 = on",
    "1258307323-255-414": "Data set that contains information about the clustering of points of interest (POI) on a map. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307325-0-217": "Data set that provides information about the traffic detour setting, indicating whether it is configured to operate automatically or manually. 0=automatic 1=manual",
    "1258307325-10-217": "Data set that provides information about the traffic detour setting, indicating whether it is configured to operate automatically or manually. 0=automatic 1=manual",
    "1258307325-255-414": "Data set that provides information about traffic detour settings, including specific parameters and configurations. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307330-0-29": "Data set that provides information about the traffic status along a route. 0 = off; 1 = on",
    "1258307330-10-29": "Data set that provides information about the traffic status along a route, indicating whether it is enabled or disabled. 0 = off; 1 = on",
    "1258307330-255-414": "Data set that provides information about traffic conditions along a route. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307332-0-8": "Notification In Companion 0 = off; 1 = on",
    "1258307332-10-8": "Notification In Companion 0 = off; 1 = on",
    "1258307332-255-414": "Data set that provides information about notifications in a companion system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307336-0-219": "Data set that provides information about the open or closed status of the right tower in the map. 0=closed 1=opened",
    "1258307336-10-219": "Data set that provides information about the open or closed status of the right tower in the map. 0=closed 1=opened",
    "1258307336-255-414": "Data set that provides information about the right tower in a map, structured with multiple data fields. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307338-0-653": "Data set that contains information about the state of charge (SOC) at the destination, expressed as a percentage. In percentage",
    "1258307338-10-653": "Data set that contains information about the state of charge (SOC) at the destination, expressed as a percentage. In percentage",
    "1258307338-255-414": "Data set that provides information about the state of charge (SOC) at the destination. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307339-0-653": "Data set that contains information about the state of charge (SOC) of the vehicle's battery at a charging station, expressed as a percentage. In percentage",
    "1258307339-10-653": "Data set that contains information about the state of charge (SOC) of the vehicle's battery at a charging station, expressed as a percentage. In percentage",
    "1258307339-255-414": "Data set that provides information about the state of charge (SOC) at a charging station. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307340-0-667": "Data set that provides information about the activation status of the range assurance feature, represented as a boolean value indicating whether the feature is turned on or off. On/Off",
    "1258307340-10-667": "Data set that provides information about the activation status of the range assurance feature, represented as a boolean value indicating whether the feature is turned on or off. On/Off",
    "1258307340-255-414": "Data set that provides information related to range assurance settings, structured to include multiple data fields with specific attributes such as data type, minimum and maximum values, step size, default values, and data length. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307343-0-680": "Data set that contains information about the activation status of data collection for product improvement purposes. On/Off",
    "1258307343-10-680": "Data set that contains a boolean value indicating whether data collection for product improvement is enabled or disabled. On/Off",
    "1258307343-255-414": "Data set that contains information related to the configuration of data collection for product improvement purposes. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307401-0-220": "Data set that contains information about the selected prefecture for VICS (Vehicle Information and Communication System). 0;1;2;3;N prefecture1; prefecture2;… prefectureN",
    "1258307402-0-221": "Data set that provides information about the traffic flow settings, specifying the type of roads or automatic mode for traffic data processing. 0;1;2;3;4 Highway; Normal roads; All roads; Automatic",
    "1258307403-0-222": "Data set that contains information about traffic map display options, including settings for showing free-flow traffic, traffic congestions, and traffic report icons on the map. 0;1;2 Show free-flow; Show Traffic congestions; Show Traffic report Icons on map",
    "1258307404-0-223": "Popup Duration Time 0:1:2 5, 10, 15 sec.)",
    "1258307405-0-224": "Data set that contains information about the status of VICS (Vehicle Information and Communication System) beacon messages, indicating whether they are active or inactive. true/false",
    "1258307406-0-224": "Vics Beacon Graphics true/false",
    "1258307407-0-224": "Data set that provides information about whether the vehicle is approaching a traffic event. true/false",
    "1258307408-0-225": "Data set that provides information about the station selection mode, indicating whether it is set to manual or automatic. 0;1 Manual, Automatic",
    "1258307410-0-226": "Data set that contains information about the preferred charging type selected by the user, indicating whether fast or normal charging is preferred. 0 = fast; 1 = normal",
    "1258307411-0-227": "Data set that contains information about the preferred charging payment method selected by the user. 0=No preference; 1=conventional; 2=VW",
    "1258307412-0-228": "Data set that contains information about the activation status of a specific payment method for Volkswagen-related services. 0=off 1=on",
    "1258307413-0-228": "Payment Provider Specific Method 0=off 1=on",
    "1258307415-0-229": "Data set that provides information about the status of charging stations, including their availability, operational condition, or reservation status. enum list elements: 0x01 status only (known) = free, 0x02 all except status (known) = defective or reserved, 0x03 all",
    "1258307416-0-230": "Include Offline Stations 0=off 1=on",
    "1258307419-0-228": "Data set that provides information about whether a range warning is activated or deactivated. 0=off 1=on",
    "1258307421-0-215": "Data set that contains information about the automatic zoom settings for the side view, indicating different operational modes. 0=on; 1=intersection, 2=off",
    "1258307421-0-231": "Data set that provides information about the automatic zoom feature. 0=on; 1=intersection, 2=off",
    "1258307421-10-231": "Data set that contains information about the automatic zoom settings for the side view, indicating different operational modes. 0=on; 1=intersection, 2=off",
    "1258307421-255-414": "Data set that contains information about the automatic zoom functionality for the side view, including its activation status and associated parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307422-0-232": "Data set that contains a string representing a universally unique identifier (UUID) related to navigation. string with UUID",
    "1258307423-0-449": "Data set that contains information about the zoom scale, represented as a zoom level in centimeters, ranging from 30 meters to 2500 kilometers. Zoom-Level in cm: 30m-2500km =&gt; [3000 — 2500 000 00]",
    "1258307423-10-449": "Data set that contains information about the zoom scale, represented as a zoom level in centimeters, ranging from 30 meters to 2500 kilometers. Zoom-Level in cm: 30m-2500km =&gt; [3000 — 2500 000 00]",
    "1258307423-255-414": "Data set that contains information about the zoom scale settings, including its configuration parameters and data structure. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307424-0-233": "Data set that contains information about whether the range map functionality is activated or deactivated. 0 = off; 1 = on",
    "1258307424-10-233": "Data set that contains information about whether the range map feature is activated or deactivated. 0 = off; 1 = on",
    "1258307424-255-414": "Data set that provides information about the range map, structured as a combination of data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307425-0-234": "Data set that provides information about the state of the range layer, indicating whether it is active or inactive. 0=active, 1=inactive",
    "1258307501-0-235": "Data set that contains information about the activation status of satellite maps on the second display. 0=off 1=on",
    "1258307501-10-235": "Data set that contains information about the activation status of satellite maps on the second display. 0=off 1=on",
    "1258307501-255-414": "Data set that provides information about the configuration and status of satellite maps displayed on a secondary screen. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307503-0-230": "Data set that indicates whether the second display for traffic incidents is activated or deactivated. 0=off 1=on",
    "1258307503-10-230": "Data set that indicates whether the second display for traffic incidents is activated or deactivated. 0=off 1=on",
    "1258307503-255-414": "Data set that provides information about traffic incidents displayed on a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307504-0-230": "Data set that contains information about the activation status of the traffic information on the second display. 0=off 1=on",
    "1258307504-10-230": "Data set that contains information about the activation status of the traffic information display on a secondary screen. 0=off 1=on",
    "1258307504-255-414": "Data set that provides information about the traffic information displayed on a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307505-0-213": "Data set that contains information about the mode settings for the second display, indicating whether it is set to automatic, day, or night mode. 0=automatic 1=day 2=night",
    "1258307505-10-213": "Data set that contains information about the mode settings for the second display, indicating whether it is set to automatic, day, or night mode. 0=automatic 1=day 2=night",
    "1258307505-255-414": "Data set that provides information about the day/night mode status for a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307506-0-214": "Data set that contains information about the map orientation settings for a secondary display. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307506-10-214": "Data set that contains information about the map orientation settings for a secondary display. 0=2D north; 1=2D drive; 2=3D drive; 3=Overview",
    "1258307506-255-414": "Data set that contains information about the map orientation settings for a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307507-0-236": "Data set that contains information about the automatic zoom settings for the second display. 0=off 1=on 2=intersection",
    "1258307507-10-236": "Data set that contains information about the automatic zoom settings for the second display, indicating whether the feature is off, on, or set to adjust at intersections. 0=off 1=on 2=intersection",
    "1258307507-255-414": "Data set that contains information about the automatic zoom functionality for a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307508-0-230": "Data set that contains information about the activation status of the 3D map content on the second display. 0=off 1=on",
    "1258307508-10-230": "Data set that contains information about the activation status of the 3D map content on the second display. 0=off 1=on",
    "1258307508-255-414": "Data set that provides information about the availability and configuration of 3D map content for a secondary display in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307509-0-230": "Data set that contains information about the activation status of 3D city models displayed on a secondary screen. 0=off 1=on",
    "1258307509-10-230": "Data set that contains information about the activation status of 3D city models displayed on a secondary screen in the vehicle's navigation system. 0=off 1=on",
    "1258307509-255-414": "Data set that provides information about the availability and configuration of 3D city models for a secondary display in the vehicle's navigation system. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307510-0-230": "Data set that indicates whether the display of favorite destinations on the second map display is activated or deactivated. 0=off 1=on",
    "1258307510-10-230": "Data set that contains information about the activation status of the favorite destinations feature for the second display in the map content. 0=off 1=on",
    "1258307510-255-414": "Data set that contains information about favorite destinations displayed on the second display of the map content. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307511-0-230": "Data set that contains information about the activation status of the weather display on the secondary map interface. 0=off 1=on",
    "1258307511-10-230": "Data set that contains information about the activation status of the weather display on the secondary map interface. 0=off 1=on",
    "1258307511-255-414": "Data set that provides information about the weather display settings on the secondary map interface. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1258307512-0-230": "Data set that contains information about the activation status of the map content displayed on the second display. 0=off 1=on",
    "1258307512-10-230": "Data set that contains information about the activation status of map content on the second display. 0=off 1=on",
    "1258307512-255-414": "Data set that provides information about the map content displayed on a secondary display. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1325418065-0-400": "Data set that contains information about the shuffle playback setting, indicating whether the shuffle mode is enabled or disabled. 0 = off; 1 = on",
    "1325418066-0-479": "Data set that specifies the repeat mode setting for media playback. 0: Off 1: Folder 2: File 3 Playlist 4: Disc",
    "1325418066-0-480": "Data set that specifies the repeat mode setting for media playback. 0: Off 1: Folder 2: File 3 Playlist 4: Disc",
    "1325418068-0-482": "Data set that contains information about the time position within the currently playing file. time position",
    "1325418075-0-237": "All Favorits MHD See MIB3_PSO_favorites-and-global-playlist-parameters.pdf",
    "1325418865-0-382": "Data set that contains information about the video display format settings, specifying the aspect ratio or scaling mode for video playback. 0 - Fit to screen / AUTO, 1 -16:9 (Widescreen), 2 - 4:3 (Standard), 3 - 14:9 (Zoom), 4 - Original",
    "1325418866-0-383": "Data set that contains information about the video format settings for DVD playback, specifying the aspect ratio or display mode to be used. 0 - Fit to screen / AUTO, 1 -16:9 (Widescreen), 2 - 4:3 (Standard), 3 - 14:9 (Zoom), 4 - 47:20 (Cinemascope)",
    "1325418867-0-506": "Data set that contains information about the device ID and media ID. Device ID and Media ID",
    "1325418873-0-386": "Data set that contains information about shortcuts, including their storage capacity and persistence in the Human-Machine Interface (HMI). Shortcuts, max. bytes in the HMI persistence: 1000 bytes per preset (1000*8 shortcuts),",
    "1325418876-0-389": "Tv Ews 0 = off; 1 = on",
    "1325418876-10-389": "Tv Ews 0 = off; 1 = on",
    "1325418876-255-414": "Data set that contains structured information related to the \"TV EWS\" feature. This data set includes multiple data fields with specific attributes, such as data type, minimum and maximum values, step size, default values, and data length. The data fields are defined as a structure, combining a boolean data type and an 8-bit unsigned integer. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1325418877-0-390": "Data set that contains information about the child lock settings for the television feature in the vehicle, specifying whether there are no restrictions or an age level restriction is applied. 0 = no restriction; otherwise age level",
    "1325418877-10-390": "Data set that contains information about the child lock settings for the television feature in the vehicle. 0 = no restriction; otherwise age level",
    "1325418877-255-414": "Data set that contains information about the child lock settings for the television functionality in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1325418883-0-394": "Tv Class Filter 16 integer entries of 4 bytes each",
    "1325418883-10-394": "Tv Class Filter 16 integer entries of 4 bytes each",
    "1325418883-255-414": "Data set that contains information about the TV Class filter settings, structured to include multiple data types and parameters. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973499-0-251": "Data set that contains information about the configuration of tiles on the home screen, supporting up to eight entries with each entry structured to include a string, an integer, and a long data type. struct{ string[30] int long }[dynSize=8]",
    "1358973500-0-252": "Data set that contains information about the configuration of the home screen tiles, including their position and widget type, specific to Å koda vehicles. struct{ int8 int8 int8 int64 }[dynSize=60]",
    "1358973503-0-29": "Data set that contains information about the activation status of the wake-up phrase functionality. 0 = off; 1 = on",
    "1358973504-0-253": "Data set that contains information about the configuration of coachmarks, including their identification and visibility status. struct{ int:8 -&gt; coachmark id bool -&gt; true if coachmark has to be show }[dynSize=20]",
    "1358973506-0-254": "Data set that contains information about the type of voice setting configured in the vehicle.",
    "1358973508-0-255": "Data set that contains information about the human-machine interface (HMI) color settings. int:32[8]",
    "1358973509-0-256": "Data set that contains information about the automatic color setting status of the Human-Machine Interface (HMI). 0 = off; 1 = on",
    "1358973510-0-255": "Data set that contains information about the type of clock display in the vehicle, specifying whether it is analog or digital. int:32[8]",
    "1358973511-0-256": "Time Display In 10s Off Mode 0 = off; 1 = on",
    "1358973512-0-29": "Data set that contains information about the activation status of haptic acoustic feedback. 0 = off; 1 = on",
    "1358973512-10-29": "Data set that contains information about the activation status of haptic acoustic feedback. 0 = off; 1 = on",
    "1358973512-255-414": "Data set that provides information about the haptic and acoustic feedback settings of the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973513-0-257": "Data set that contains configuration information for the main menu, specifically the arrangement of all tiles. struct{long:64[64]}",
    "1358973514-0-258": "Data set that contains structured information represented as an array of eight elements, each consisting of a 32-bit integer and a long integer, intended for use in a control center interface. struct{ int32 long }[8]",
    "1358973516-0-256": "Data set that contains information about whether the time display is enabled or disabled in standby mode. 0 = off; 1 = on",
    "1358973516-10-256": "Data set that indicates whether the time display is enabled or disabled in standby mode. 0 = off; 1 = on",
    "1358973516-255-414": "Data set that provides information about the display of time in standby mode. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973519-0-261": "Acoustic Sensor Button Feedback On/Off",
    "1358973520-0-262": "Offclock Layout OffclockLayout",
    "1358973521-0-263": "Homscreen Page ID Note: value range added automatically",
    "1358973522-0-264": "Additional Keyboard Languages long:64; Note: value range automatically added",
    "1358973522-10-264": "Additional Keyboard Languages long:64; Note: value range automatically added",
    "1358973522-255-414": "Data set that contains information about additional keyboard languages configured in the vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973523-0-474": "Data set that indicates whether the activation of the speech dialog system (SDS) via a wake-up word is enabled or disabled. 0 = false 1 = true",
    "1358973523-10-474": "Data set that indicates whether the activation of the speech dialog system (SDS) is enabled via a wake-up word. 0 = false 1 = true",
    "1358973523-255-414": "Data set that provides information about the activation of a specific system via a wake-up word. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973524-0-29": "Data set that contains information about the activation status of the end tone in the voice dialogue system. 0 = off; 1 = on",
    "1358973525-0-29": "Example Commands Infotainment System 0 = off; 1 = on",
    "1358973526-0-29": "Example Commands Instrument Cluster 0 = off; 1 = on",
    "1358973529-0-29": "Data set that contains information about the activation status of the voice control system. 0 = off; 1 = on",
    "1358973530-0-29": "Data set that contains information about the activation status of the end tone for voice control. 0 = off; 1 = on",
    "1358973531-0-29": "Data set that contains information about the input tone status in the voice dialogue system. 0 = off; 1 = on",
    "1358973533-0-266": "Data set that contains information about the configuration of shortcut buttons 1 to 6 on the vehicle's home screen. struct{ int long }[6]",
    "1358973535-0-268": "Background Image ID ImageID",
    "1358973536-0-252": "Data set that contains information about the configuration of the Homescreen 2.0 tiles, including their position and widget type. struct{ int8 int8 int8 int64 }[dynSize=60]",
    "1358973540-0-271": "Data set that indicates whether the SDS Expert Mode is enabled or disabled. 0 = false; 1 = true",
    "1358973540-10-271": "Sds Expert Mode 0 = false; 1 = true",
    "1358973540-255-414": "SDS Expert Mode [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973544-0-502": "Data set that contains information about the activation status of the text input swipe functionality. 1=on; 0=off",
    "1358973544-10-502": "Data set that indicates whether the text input functionality using swipe is enabled or disabled. 1=on; 0=off",
    "1358973544-255-414": "Data set that contains structured information regarding the configuration of swipe functionality, including parameters for text input usage. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973545-0-502": "Data set that contains information about the activation status of handwriting recognition (HWR) gestures for text input. 1=on; 0=off",
    "1358973545-10-502": "Data set that contains information about the activation status of text input using handwriting recognition gestures. 1=on; 0=off",
    "1358973545-255-414": "Data set that contains information about the configuration of text input using handwriting recognition (HWR) gestures. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973546-0-502": "Data set that contains information about whether text input proposals from contacts are enabled or disabled. 1=on; 0=off",
    "1358973546-10-502": "Data set that contains information about the activation status of text input proposals derived from contact data. 1=on; 0=off",
    "1358973546-255-414": "Data set that provides information about text input proposals derived from contact data. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973549-0-447": "Data set that contains information about the selected wallpaper screensaver setting, represented as an integer value. 0-13 (0 = Picture black, 1-13 = Screensaverpicture)",
    "1358973549-10-447": "Data set that contains information about the selected wallpaper screensaver setting, represented as an integer value. 0-13 (0 = Picture black, 1-13 = Screensaverpicture)",
    "1358973549-255-414": "Data set that contains information about the configuration of the wallpaper screensaver settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973550-0-448": "Data set that contains information about the activation status of the \"Push-Button\" functionality, which turns off the display. (Push Button, Display turns off) 0 and 1",
    "1358973550-10-448": "Data set that contains information about the status of a push-button feature that turns off the display, represented as a boolean value. (Push Button, Display turns off) 0 and 1",
    "1358973550-255-414": "Data set that provides information about the configuration of the \"Push-Button\" display wallpaper, including its activation or deactivation status and associated settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973553-0-569": "Data set that contains information about the configuration of the upper status bar display, including elements such as time, temperature, and air quality.",
    "1358973553-10-569": "Data set that contains information about the configuration of the upper status bar display, including elements such as time, temperature, and air quality, for the central information display (CID).",
    "1358973553-255-414": "Data set that contains configuration settings for the upper status bar display in the vehicle's central information display (CID). This includes options for displaying values such as time, temperature, and air quality. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973554-0-569": "Data set that contains information about the configuration of the upper status bar, specifically related to displaying values such as time, temperature, or air quality.",
    "1358973554-10-569": "Data set that contains information about the configuration of the upper status bar, specifically related to displaying values such as time, temperature, or air quality.",
    "1358973554-255-414": "Data set that contains configuration information for the upper status bar display, including settings for elements such as time, temperature, and air quality. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973563-0-569": "Data set that contains information about the configuration of the upper status bar, including elements such as time, temperature, and air quality.",
    "1358973564-0-569": "Data set that contains information about the configuration of the upper status bar, including elements such as time, temperature, and air quality, as displayed on the central information display (CID).",
    "1358973568-0-453": "Data set that contains information about the configuration of top bar favorites in the vehicle's user interface. [{datatype:\", description\":\"struct{int32 long}[8]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1358973570-0-453": "Data set that contains information about the configuration of up to eight favorite settings in the vehicle's control center. [{datatype:\", description\":\"struct{int32 long}[8]\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1358973572-0-640": "Data set that contains information about a customizable wake-up phrase, represented as a string data type.",
    "1358973574-0-636": "Second Static Wakeup Phrase 0 = off; 1 = on",
    "1358973625-0-646": "Gridmenu Vehicle And App More [{datatype:\", description\":\"struct{long: 64[64]}\", min:\", max\":\"\", stepsize:\", default\":\"\", datalength:\"}]",
    "1358973627-0-657": "Gridmenu Vehicle And App More",
    "1358973629-0-8": "Data set that contains information about the activation status of a custom wake-up phrase. 0 = off; 1 = on",
    "1358973631-1-677": "Data set that contains information about the theme name, represented as a string data type.",
    "1358973631-10-677": "Data set that contains information about the theme name, represented as a string data type.",
    "1358973631-11-677": "Data set that contains information about the theme name, represented as a string data type.",
    "1358973631-255-414": "Data set that contains information about the theme configuration, including its name and associated settings. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973632-1-676": "Data set that contains information about the version of a theme, represented as an unsigned integer.",
    "1358973632-10-676": "Data set that contains information about the version of a theme, represented as an unsigned integer.",
    "1358973632-11-676": "Data set that contains information about the version of the theme, represented as an unsigned integer.",
    "1358973632-255-414": "Data set that provides information about the theme version, structured as a combination of data types. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973633-1-676": "Data set that contains information about theme-related features in the vehicle.",
    "1358973633-10-676": "Data set that contains information about theme-related features in the vehicle.",
    "1358973633-255-414": "Data set that contains information about theme features, structured to include multiple data points with specific attributes such as data type, range, step size, default values, and data length. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973637-0-273": "Data set that contains serialized JSON objects in a structured format, represented as a byte array, which are used to define the order of tiles in an application grid. serialized JSON object, byte[]",
    "1358973637-10-273": "Data set that contains serialized JSON objects in a structured format, represented as a byte array. serialized JSON object, byte[]",
    "1358973637-255-414": "App Grid Tile Order [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973641-0-509": "Data set that contains information about the activation status of the wake-up word functionality.",
    "1358973642-0-699": "Wakeup Word 0 — O LIMITED (= 365 days) 3 -UNLIMITED",
    "1358973897-0-273": "Data set that contains serialized configuration information for the main menu layout, including the arrangement of all tiles. serialized JSON object, byte[]",
    "1358973897-10-273": "Data set that contains serialized configuration information for the main menu layout, including the arrangement of all tiles. serialized JSON object, byte[]",
    "1358973897-255-414": "Main Menu Configuration [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973898-0-274": "Direct Access Bar Tile One serialized JSON object, byte[]",
    "1358973898-10-274": "Direct Access Bar Tile 1 serialized JSON object, byte[]",
    "1358973898-255-414": "Data set that contains information about the configuration and state of the first tile in the direct access bar. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973899-0-274": "Direct Access Bar Tile Two serialized JSON object, byte[]",
    "1358973900-0-274": "Direct Access Bar Tile 3 serialized JSON object, byte[]",
    "1358973901-0-274": "Direct Access Bar Tile Four serialized JSON object, byte[]",
    "1358973936-0-276": "Setup Assistant serialized JSON object, byte[]",
    "1358973937-0-277": "Data set that indicates whether the notification for a new text message is enabled or disabled. 1= on; 0=off",
    "1358973938-0-277": "Data set that indicates whether the notification for new email is enabled or disabled. 1= on; 0=off",
    "1358973939-0-277": "Data set that indicates whether notifications for missed calls are enabled or disabled. 1= on; 0=off",
    "1358973940-0-277": "Data set that provides information about the notification status for charging in an electric vehicle. 1= on; 0=off",
    "1358973940-10-277": "Data set that provides information about the notification status for charging in an e-tron vehicle. 1= on; 0=off",
    "1358973940-255-414": "Data set that provides information about notifications related to the charging process of an electric vehicle. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973941-0-277": "Data set that provides information about the activation status of a notification related to refueling. 1= on; 0=off",
    "1358973942-0-277": "Data set that provides information about the activation status of parking notifications. 1= on; 0=off",
    "1358973943-0-277": "Data set that provides information about the activation status of notifications for border crossings. 1= on; 0=off",
    "1358973944-0-277": "Data set that provides information about the activation status of notifications for PNAV. 1= on; 0=off",
    "1358973945-0-277": "Data set that provides information about the activation status of a regulation warning notification specific to the Asia region. 1= on; 0=off",
    "1358973946-0-277": "Data set that provides information about the activation status of notifications for route briefing. 1= on; 0=off",
    "1358973947-0-277": "Data set that provides information about the activation status of notifications for SiriusXM alerts. 1= on; 0=off",
    "1358973948-0-277": "Data set that provides information about the activation status of the notification for wireless phone charging. 1= on; 0=off",
    "1358973949-0-277": "Data set that provides information about the activation status of a time notification feature. 1= on; 0=off",
    "1358973950-0-277": "Data set that provides information about the activation status of calendar notifications. 1= on; 0=off",
    "1358973951-0-277": "Data set that provides information about the notification status for ETC (Electronic Toll Collection) in Japan. 1= on; 0=off",
    "1358973952-0-277": "Data set that indicates whether the notification for the first-time wizard is enabled or disabled. 1= on; 0=off",
    "1358973953-0-277": "Data set that provides information about the activation status of notifications for data plans. 1= on; 0=off",
    "1358973954-0-277": "Data set that provides information about the activation status of notifications related to myAudi login. 1= on; 0=off",
    "1358973955-0-277": "Data set that provides information about the activation status of notifications related to connect licenses. 1= on; 0=off",
    "1358973956-0-277": "Data set that indicates whether the notification for Audi connect installation is enabled or disabled. 1= on; 0=off",
    "1358973963-0-431": "Data set that contains a sorted, comma-separated list of unique shortcut identifiers. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973963-10-431": "Data set that contains a sorted, comma-separated list of unique shortcut identifiers. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973963-255-414": "Data set that contains information about shortcut settings, structured as a combination of data points. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973964-0-432": "Data set that contains a sorted, comma-separated list of unique identifiers representing the arrangement of favorite tiles in the favorites view. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973964-10-432": "Data set that contains a sorted, comma-separated list of unique identifiers representing the arrangement of favorite tiles in the favorites view. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973964-255-414": "Data set that contains information about the customized arrangement of favorite tiles in the favorites view. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973965-0-433": "Data set that contains information about the type of clock display in the vehicle, indicating whether it is analog or digital. 0 = analog; 1 = digital",
    "1358973966-0-434": "Data set that contains a sorted, comma-separated list of unique identifiers for dashboard tiles. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973966-10-434": "Data set that contains a sorted, comma-separated list of unique identifiers for dashboard tiles. Sorted comma-separated list of unique dashboard tile identifiers",
    "1358973966-255-414": "Dashboard Tile One [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1358973968-0-436": "Screen Layer Show Basic An integer without sign. So unsigned byte or unsigned int or just int, or in the JavaScript world you would say —n",
    "1358973969-0-436": "Screen Layer Show Expert An integer without sign. So unsigned byte or unsigned int or just int, or in the JavaScript world you would say —n",
    "1392529930-0-781": "Data set that contains configuration information for scenario 1, represented as a structured data type. This data set includes six bitmap values, each of type uint32, which collectively define the configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529930-10-781": "Data set that contains configuration information for scenario 1, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration details of the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529931-0-781": "Data set that contains configuration information for scenario 2, represented as a structured data type. This data set includes six bitmap values, each of type uint32, which are used to define specific configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529931-10-781": "Data set that contains configuration information for scenario 2, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration details for the specified scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529932-0-791": "Data set that contains the configuration details for scenario 3, structured in a specific format.",
    "1392529932-10-791": "Data set that contains the configuration details for scenario 3, structured in a format that organizes related data fields systematically.",
    "1392529933-0-781": "Data set that contains structured information represented as six 32-bit bitmap values, used for the configuration of scenario 4. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529933-10-781": "Data set that contains configuration information for scenario 4, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration parameters for the specified scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529934-0-791": "Data set that contains the configuration details for scenario 5, structured in a specific format.",
    "1392529934-10-791": "Data set that contains the configuration details for scenario 5, structured in a format that organizes related data fields systematically.",
    "1392529935-0-781": "Data set that contains configuration information for scenario 6, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration details for the specified scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529935-10-781": "Data set that contains configuration information for scenario 6, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529936-0-781": "Data set that contains structured information represented as six 32-bit unsigned integer bitmaps, used for the configuration of a specific scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529936-10-781": "Data set that contains configuration information for Scenario 7, represented as a structured data type. This data set includes six bitmap values, each of type uint32, which are used to define specific configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529937-0-781": "Data set that contains configuration information for Scenario 8, represented as a structured data type. This data set includes six bitmap values, each of type uint32, which are used to define specific configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529937-10-781": "Data set that contains configuration information for Scenario 8, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration parameters for the scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529938-0-781": "Data set that contains configuration information for scenario 9, represented as a structured data type. The configuration is described using six 32-bit unsigned integer bitmaps. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529938-10-781": "Data set that contains configuration information for scenario 9, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, which collectively describe the configuration details for the specified scenario. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529939-0-781": "Data set that contains structured information represented as bitmaps for the configuration of scenario 10. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "1392529939-10-781": "Data set that contains configuration information for scenario 10, represented as a structured data type. This data set includes six bitmap values, each defined as an unsigned 32-bit integer, to specify the configuration details. [uint32,...,uint32](length: 6) bitmaps for configuration of scenario",
    "13_flexpole_3p_csm.csm_flexpole_daphne_additional_data.chargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_additional_data.info": "information description",
    "13_flexpole_3p_csm.csm_flexpole_daphne_additional_data.info_category": "category of information",
    "13_flexpole_3p_csm.csm_flexpole_daphne_additional_data.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_additional_data.timestamp": "time when the data was recorded",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.ChargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.description": "description of the error",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.dtc": "diagnostic trouble code",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.dtc_can_message": "name of the CAN bus message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.dtc_id": "unique identifier of the error",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.ecu": "electronic control unit that sent the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.faultLevel": "severity level of the error",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.ingestion_timestamp": "999909",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.signalsToString": "signals that triggered the error or are related",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.start_timestamp": "time when the error started",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.stop_timestamp": "time when the error was resolved",
    "13_flexpole_3p_csm.csm_flexpole_daphne_dtc_messages.timestamp": "time when the data was recorded",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.chargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.ecu": "name of the electronic control unit",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.ingestion_timestamp": "time when the data was received",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.timestamp": "time when the data was recorded",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.version": "current version installed on the ecu",
    "13_flexpole_3p_csm.csm_flexpole_daphne_ecu_messages.version_type": "whether the version is concerning hardware or software of the ecu",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.ChargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_ICCID": "SIM card identifier for TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_IMEI": "International Mobile Equipment Identity for TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_IMSI": "International Mobile Subscriber Identity for TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_RSRQ": "signal quality reported by TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_RSSI": "signal strength reported by TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox1_network_type": "network connection type used by TBox1",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_ICCID": "SIM card identifier for TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_IMEI": "International Mobile Equipment Identity for TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_IMSI": "International Mobile Subscriber Identity for TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_RSRQ": "signal quality reported by TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_RSSI": "signal strength reported by TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_network_type": "network connection type used by TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_real": "time TBox2 has been running",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_suspend": "time TBox2 has been in suspended mode",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.TBox2_uptime_total": "total uptime of TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.compatibilityStatus": "info whether all ecus have compatible hardware and software versions",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.compatibilityStatusInfo": "additonal info about ecu versions",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.cpuloadAverage": "average cpu usage of the device",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.gun_plug_cycles_sideA": "number of plug cycles on side A",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.gun_plug_cycles_sideB": "number of plug cycles on side B",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.incorrectVersion": "list of incorrect versions",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.ingestion_timestamp": "time when the data was received",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.last_contact_to_operator_backend": "time of the last successful connection to the customer's operating backend",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.memoryUsage": "memory usage of the device",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.operator_backend_status": "connection status of the customer's backend",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.software_version": "firmware version of TBox2",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.status_general": "overall status of the station",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.status_sideA": "status of the connector on side A",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.status_sideB": "status of the connector on side B",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.system_SW_version": "firmware version of overall system",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.timestamp": "time when the data was recorded",
    "13_flexpole_3p_csm.csm_flexpole_daphne_heartbeat_messages.unknownVersion": "list of unknown versions",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.ChargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.canBusId": "identifier of the CAN bus sending the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.ecu": "electronic control unit that sent the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.messageName": "name of signal group",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.rawMessage": "unknown",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.signal": "name of the signal",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.signalId": "identifier of the signal",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.signalsetFrequency": "frequency of reported signals",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.timestamp": "time when the data was recorded",
    "13_flexpole_3p_csm.csm_flexpole_daphne_signal_messages.value": "reported value of the signal",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.chargingStationID": "unique identifier of the Flexpole",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.destinationDevice": "component that received the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.log": "content of the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.loggingDevice": "component that stores the log",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.organization_iam_id": "IAM ID of the owning organization.",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.sourceDevice": "component that sent the message",
    "13_flexpole_3p_csm.csm_flexpole_daphne_textlog_messages.timestamp": "time when the data was recorded",
    "1426085361-0-288": "Data set that contains information about the ringtone settings associated with Bluetooth devices connected to the vehicle. This includes the Bluetooth device addresses and their corresponding ringtone indices. struct ringtone {, Bluetooth Device Address 1: uint8[6], ringtoneIndex 1: uint8, Bluetooth Device Address 2: uint8[6], ringtoneIndex 2: uint8, }, --------------------------------------------------------, Bluetooth Device Address 1; [0,15] per uint8, ringtoneIndex 1; 0=ringtone0, 1=ringtone1, ..., Bluetooth Device Address 2; [0,15] per uint8, ringtoneIndex 1; 0=ringtone0, 1=ringtone1, …",
    "1426085362-0-289": "Data set that provides information about the \"Don't forget phone reminder\" feature. This data set includes a structured configuration containing Bluetooth device addresses and their corresponding activation statuses. It allows the system to manage reminders for up to two Bluetooth devices, specifying whether each device is enabled or disabled. struct phoneReminder {, Bluetooth Device Address 1: uint8[6], isEnabled 1: uint8, Bluetooth Device Address 2: uint8[6], isEnabled 2: uint8, }, ------------------------------------------------------, Bluetooth Device Address 1; [0,15] per uint8, isEnabled 1; 0=false, 1=true,",
    "1426085379-0-296": "Data set that contains information about the conversational view settings, including Bluetooth device addresses and their respective enablement statuses. struct conversationalView {, Bluetooth Device Address 1: uint8[6], isEnabled 1: uint8, Bluetooth Device Address 2: uint8[6], isEnabled 2: uint8, }, --------------------------------------------------, Bluetooth Device Address 1; [0,15] per uint8, isEnabled 1; 0=false, 1=true, Bluetooth Device Address 2; [0,15] per uint8, isEnabled 2; 0=false, 1=true",
    "1426085383-4-230": "Data set that contains information about the WLAN activation status, indicating whether the WLAN functionality is turned on or off. 0=off 1=on",
    "1426085387-8-230": "Data set that provides information about the activation status of Voice over LTE (VoLTE) functionality. 0=off 1=on",
    "1426085388-9-230": "Data set that contains information about the activation status of the incoming tone for noise cancellation. 0=off 1=on",
    "1426085389-9-298": "Data set that contains configuration options for the notification center, allowing the customization of various settings through true/false values. Options {, option1: true/false;, option2: true/false;, option3 true/false;, option4: true/false;, option5 true/false;, option6: true/false;, option7 true/false;, option8: true/false;, option9: true/false;, option10: true/false;, option11: true/false;, option12: true/false;, option13: true/false;, option14: true/false;, option15: true/false;, ..., }, , ,",
    "1459640793-0-8": "Data set that contains information about the activation status of the touchscreen tone, indicating whether the tone is enabled or disabled. 0 = off; 1 = on",
    "1459640793-10-8": "Data set that contains information about the activation status of the touchscreen tone, indicating whether the tone is enabled or disabled. 0 = off; 1 = on",
    "1459640793-255-414": "Data set that contains information about the activation status of the touchscreen tone feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1459640794-0-29": "Data set that contains information about the activation status of the welcome sound feature. 0 = off; 1 = on",
    "1459640796-0-8": "Data set that contains information about the activation status of the sensor key tone, indicating whether the tone is enabled or disabled. 0 = off; 1 = on",
    "1459640796-10-8": "Data set that contains information about the activation status of the sensor key tone, indicating whether the tone is enabled or disabled. 0 = off; 1 = on",
    "1459640796-255-414": "Data set that contains information about the activation status of the sensor key tone. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1459640797-0-8": "Data set that contains information about the activation status of the leaving sound feature. 0 = off; 1 = on",
    "1459640797-10-8": "Data set that contains information about the activation status of the leaving sound feature. 0 = off; 1 = on",
    "1459640797-255-414": "Data set that contains information about the activation status of the leaving sound feature. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "1459640799-0-300": "Data set that contains information about the volume level controlled via the vehicle's touchscreen interface. Volume",
    "1459640799-1-500": "Data set that contains information about the volume level controlled via the touchscreen interface. Volume",
    "1459640799-10-300": "Data set that contains information about the volume level controlled via the vehicle's touchscreen interface. Volume",
    "1459640799-11-500": "Data set that contains information about the volume level controlled via the touchscreen interface. Volume",
    "1459640799-255-414": "Data set that contains information about the volume settings adjusted via the vehicle's touchscreen interface. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "150999945-0-36": "Off Operation 0 = off; 1 = on",
    "150999945-1-733": "Off Operation 0 = on; 16 = off (16 = no OFF operation; 0 = OFF operation)",
    "150999946-0-36": "Data set that provides information about the operational status of the rear system, indicating whether it is turned off or on. 0 = off; 1 = on",
    "150999946-1-733": "Rear Off Operation 0 = on; 16 = off (16 = no OFF operation; 0 = OFF operation)",
    "150999947-0-36": "Data set that contains information about the status of the rear lock. 0 = off; 1 = on",
    "150999947-1-734": "Data set that provides information about the locking status of the rear lock, indicating whether it is locked or not. 0 = on; 1 = off (1 = not locked; 0 = locked)",
    "150999948-0-36": "Data set that contains information about the activation status of the maximum defrost function in the vehicle. 0 = off; 1 = on",
    "150999949-0-36": "Auto Zone Front Driver 0 = off; 1 = on",
    "150999950-0-36": "Auto Zone Front Passenger 0 = off; 1 = on",
    "150999953-0-320": "Data set that provides information about the target temperature value for driver zone. n/a",
    "150999953-0-55": "Data set that contains the target temperature values for the driver zone. 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "150999953-1-736": "Data set that contains the target temperature values for the driver zone, represented in a range of 10.0°C to 35.5°C with increments of 0.1°C. Bus value 0..255 = 10..35.5 °C, in 0.1°C steps. Bus value 0 = Low/LO, Bus value 255 = High/HI.",
    "150999954-0-320": "Data set that provides information about the target temperature value for passenger zone. n/a",
    "150999954-0-55": "Data set that contains the target temperature values for the passenger zone. 10 â Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "150999954-1-736": "Data set that contains the target temperature settings for the passenger zone in the vehicle. Bus value 0..255 = 10..35.5 °C, in 0.1°C steps. Bus value 0 = Low/LO, Bus value 255 = High/HI.",
    "150999955-0-55": "Data set that provides information about the target temperature value for Zone 3, 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "150999955-0-737": "Temperature Setpoint Zone Three 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "150999955-1-736": "Data set that contains the target temperature settings for Zone 3 in the vehicle's climate control system. Bus value 0..255 = 10..35.5 °C, in 0.1°C steps. Bus value 0 = Low/LO, Bus value 255 = High/HI.",
    "150999956-0-322": "Temperature Setpoint Zone Four 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "150999956-1-736": "Data set that contains the target temperature settings for Zone 4 in the vehicle's climate control system. Bus value 0..255 = 10..35.5 °C, in 0.1°C steps. Bus value 0 = Low/LO, Bus value 255 = High/HI.",
    "150999968-0-36": "Sync Status 0 = off; 1 = on",
    "150999974-0-36": "Data set that contains information about the automatic recirculation setting in the vehicle's climate control system. 0 = off; 1 = on",
    "150999976-0-36": "Auto Zone Z3 0 = off; 1 = on",
    "150999977-0-36": "Auto Zone Z4 0 = off; 1 = on",
    "150999979-0-36": "Data set that contains information about the activation status of seat-dependent climate control in ECO mode. 0 = off; 1 = on",
    "150999993-0-708": "Data set that contains information about the status of fragrance cartridges used for air freshening in the vehicle. 0x0 = no_cartridge; 0x1 = cartridge_1; 0x2 = cartridge_2; 0x3 = cartridge_3; 0x4 = cartridge_4",
    "151000001-0-61": "Data set that contains information about the footwell temperature offset for the driver. 0=-2; 1=-1; 2=0; 3=1; 4=2",
    "151000001-1-740": "Footwell Temperature Offset Driver 254 = -2; -1 = 255; 0 = 0; 1 = 1; 2 = 2",
    "151000002-0-61": "Data set that contains information about the footwell temperature offset for the front passenger. 0=-2; 1=-1; 2=0; 3=1; 4=2",
    "151000002-1-740": "Passenger Footwell Temperature Offset 254 = -2; -1 = 255; 0 = 0; 1 = 1; 2 = 2",
    "151000004-0-62": "Data set that contains information about the vertical positioning of the air nozzle on the driver's side within the climate control system. ClimateZone1.NozzlePosi tioning.Horizontal/Vertica l, 0=-100; 200 = +100",
    "151000005-0-63": "Data set that contains raw values for the style configuration of the driver's side air vent positioning within the climate control system, as defined in the corresponding BAP catalog. ClimateZone1.NozzlePosi tioning.Style Rohwerte entsprechend BAP-Katalog",
    "151000008-0-62": "Data set that contains information about the vertical positioning of the air nozzle on the passenger side, represented as a value within a defined range. ClimateZone1.NozzlePosi tioning.Horizontal/Vertica l, 0=-100; 200 = +100",
    "151000009-0-63": "Data set that contains raw values for the style configuration of the passenger-side air vent, based on the ClimateZone1 nozzle positioning, as defined in the BAP catalog. ClimateZone1.NozzlePosi tioning.Style Rohwerte entsprechend BAP-Katalog",
    "151000012-0-62": "Data set that contains information about the vertical positioning of the central air nozzle for the driver's side. ClimateZone1.NozzlePosi tioning.Horizontal/Vertica l, 0=-100; 200 = +100",
    "151000013-0-63": "Data set that contains raw values for the style configuration of the central air vent on the driver's side, based on the BAP catalog. ClimateZone1.NozzlePosi tioning.Style Rohwerte entsprechend BAP-Katalog",
    "151000016-0-62": "Data set that contains information about the vertical positioning of the middle air nozzle on the passenger side, specifically within the climate control system. ClimateZone1.NozzlePosi tioning.Horizontal/Vertica l, 0=-100; 200 = +100",
    "151000017-0-63": "Data set that contains raw values for the style configuration of the middle air vent on the passenger side, based on the BAP catalog. ClimateZone1.NozzlePosi tioning.Style Rohwerte entsprechend BAP-Katalog",
    "151000025-0-36": "Off Operation Convertible 0 = off; 1 = on",
    "151000026-0-689": "Data set that contains information about the activation status of seat-dependent climate control in convertible vehicles. 0 = off; 1 = on",
    "151000027-0-688": "Data set that contains information about the manually adjustable fan level for the front passenger side. 0 ... 12 (maximum level)",
    "151000035-0-36": "Auto Zone Front Passenger Convertible 0 = off; 1 = on",
    "151000036-0-702": "Data set that contains the target temperature value for the fifth climate control zone in the vehicle. 10 â entered; 0 = 10°C, 69 = 35.5°C)",
    "151000036-1-736": "Data set that contains the target temperature value for the fifth climate control zone in the vehicle. Bus value 0..255 = 10..35.5 °C, in 0.1°C steps. Bus value 0 = Low/LO, Bus value 255 = High/HI.",
    "151000043-0-36": "Auto Zone Z5 0 = off; 1 = on",
    "151000044-0-321": "Off Operation Convertible 0 = off; 1 = on",
    "151000045-0-321": "Rear Off Operation Cabrio 0 = off; 1 = on",
    "151000046-0-321": "Data set that provides information about the status of the rear lock mechanism for convertible vehicles. 0 = off; 1 = on",
    "151000047-0-321": "Data set that contains information about the activation status of the maximum defrost function for convertible vehicles. 0 = off; 1 = on",
    "151000048-0-321": "Data set that contains information about the activation status of the front driver's AUTO zone in a convertible vehicle. 0 = off; 1 = on",
    "151000049-0-322": "Temperature Setpoint Driver Zone Cabrio 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "151000050-0-322": "Data set that contains the target temperature values for the passenger zone in convertible vehicles. 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "151000051-0-322": "Data set that contains the target temperature values for Zone 3 in convertible vehicles. 10 … 35.5 °C (columns Y - AA actual physical values, not raw values? conversion raw value -&gt; physical value?)",
    "151000062-0-321": "Sync Cabrio 0 = off; 1 = on",
    "151000066-0-321": "Data set that contains information about the automatic recirculation mode status for convertible vehicles. 0 = off; 1 = on",
    "151000098-0-36": "Off Operation Z5 0 = off; 1 = on",
    "151000098-1-797": "Off Operation Z5 0 = OFF active, 16 = OFF not active",
    "151000102-0-348": "Pure Air 0 = off; 1 = on",
    "151000104-0-859": "Data set that provides information about the activation status of the rear lock, with the status represented as either active or inactive. 0 = Inactive 1 = Active",
    "151000109-0-719": "Data set that indicates whether the premium air quality feature is enabled or disabled.",
    "1526730784-0-8": "Data set that contains information about the activation status of the driver's seat entry and exit assistance feature. 0 = off; 1 = on",
    "1526730784-1-8": "Data set that contains information about the activation status of the entry and exit assistance feature for the front driver's seat. 0 = off; 1 = on",
    "1526730784-255-8": "Data set that contains information about the activation status of the entry and exit assistance feature for the front driver's seat. 0 = off; 1 = on",
    "1560285213-0-53": "Data set that provides information about the active massage program of the driver seat. 0x00 none 0x01 Program1 0x02 Program2 0x03 Program3 0x04 Program4 0x05 Program5 0x06 Program6 0x07 Program7 0x08 Program8 0x09 Program9 0x0A Program10 0x0B Program11 0x0C Program12 0x0D..0xFF reserved",
    "1593860523-0-509": "Data set that provides information about additional details displayed in the cockpit.",
    "1593860524-0-510": "Standard App Navigation",
    "1593860525-0-510": "Standard App Media",
    "1593860526-0-510": "Standardapp Podcast",
    "1593860527-0-510": "Standardapp Radio",
    "1593860528-0-510": "Standardapp Messaging",
    "1593860529-0-510": "Standard App Calendar",
    "1593860530-0-509": "Data set that contains information about the general privacy settings of the vehicle.",
    "1593860531-0-509": "Data set that contains information about the privacy settings related to online services.",
    "1593860532-0-509": "Data set that contains information about the privacy setting for call recommendations.",
    "1593860533-0-509": "Data set that contains information about the privacy setting for smart recommendations.",
    "1593860534-0-509": "Data set that contains information about the privacy settings related to smart routines.",
    "1593860535-0-509": "Data set that indicates whether data collection for privacy purposes is enabled or disabled.",
    "1593860536-0-509": "No description possible.",
    "1593860537-0-509": "Short Answer",
    "1593860538-0-509": "Data set that contains information about the activation status of earcons, which are auditory signals used to provide feedback or alerts in the vehicle.",
    "1593860539-0-509": "Data set that contains a general recommendation status, represented as a boolean value.",
    "1593860540-0-514": "Data set that provides information about the acoustic recommendation settings, indicating the type of acoustic feedback configured. 0 = off; 1 = sound only; 2 = sound &amp; voice",
    "1593860541-0-509": "Data set that provides information about the recommendation for calls.",
    "1593860544-0-509": "Data set that contains information about the activation status of routines for mirror tilting.",
    "1593860547-0-509": "Routine IPA Presettings",
    "16778226-1-33": "Data set that provides information about the test of ambient illuminationactive profile. n/a",
    "16778227-1-34": "Data set that contains information about the red color component (R) of the ambient lighting, represented as an RGB (Red, Green, Blue) color value. RGB color component",
    "16778228-1-34": "Data set that contains information about the RGB color component of the ambient lighting. RGB color component",
    "16778229-1-34": "Data set that contains information about the RGB color components of the ambient lighting. RGB color component",
    "16778230-1-34": "Data set that contains information about the red color component (R) of the contour lighting, represented as an RGB color proportion. RGB color component",
    "16778231-1-34": "Data set that contains information about the green color component (G) of the contour lighting, represented as an RGB color component. RGB color component",
    "16778232-1-34": "Data set that contains information about the RGB color component of contour lighting. RGB color component",
    "16778241-1-36": "Data set that contains information about the status of the daytime running lights. 0 = off; 1 = on",
    "16778242-1-37": "Data set that contains information about the duration of the \"Coming Home\" lighting feature. Illumination duration",
    "16778243-1-36": "Data set that contains information about the status of the \"Coming Home\" feature, indicating whether it is activated or deactivated. 0 = off; 1 = on",
    "16778244-1-37": "Data set that contains information about the duration of the \"Leaving Home\" lighting feature. Illumination duration",
    "16778245-1-36": "Data set that contains information about the activation status of the \"Leaving Home\" feature. 0 = off; 1 = on",
    "16778247-1-36": "Data set that provides information about the activation status of the dynamic cornering light system. 0 = off; 1 = on",
    "16778250-1-36": "Data set that contains information about the activation status of the Dynamic Light Assist feature, specifically for Audi Matrix-Beam headlights. 0 = off; 1 = on",
    "16778251-1-38": "Data set that provides information about the winter position setting of the front windshield wiper. bit field, 0 = front wiper is not set to winter position; 1 = front wiper is set to winter position",
    "16778252-1-39": "Data set that provides information about the activation status of the rear wiper when the vehicle is in reverse gear. bit field: 0 = rear wiper is not activated parallel to front wiper in reverse gear; 1 = rear wiper is activated parallel to front wiper in reverse gear",
    "16778253-1-40": "Data set that provides information about the activation status of the TearsWiping function. bit field: 0 = tears wiping off; 1 = tears wiping on",
    "16778254-1-41": "Data set that contains information about the activation status of automatic windshield wiping when rain is detected. bit field: 0 = automatic wiping off; 1 = automatic wiping on when rain is detected",
    "16778257-1-43": "Data set that contains information about the sensitivity settings of the light sensor, indicating the activation time based on predefined sensitivity levels. 0=sensitive; 1=normal; 2=intensitive",
    "16778259-1-36": "Data set that contains information about the exterior ambient lighting status related to the keyless entry system. 0 = off; 1 = on",
    "16778260-1-36": "Data set that contains information about the activation status of an additional exterior ambient light. 0 = off; 1 = on",
    "16778261-1-44": "Data set that allows configuration of the number of blinking cycles for a specific vehicle function. 2=2 times blinking; 3=3 times blinking; 4=4 times blinking; 5=5 times blinking",
    "16778263-1-45": "Data set that contains information about the status of the ComingHome feature, indicating whether it is set to a classic mode or a staged mode. 0 = classic ComingHome; 1 = staged ComingHome",
    "16778264-1-46": "Data set that provides information about the status of the \"LeavingHome\" functionality, indicating whether it is in a classic mode or a staged mode. 0 = classic LeavingHome; 1 = staged LeavingHome",
    "16778269-0-48": "Data set that contains information about the signature configuration of the rear lights. 0 = default (no signature), 1 = signature 1, 2 = signature 2, 3 = signature 3, 4 = signature 4, 5 = signature 5, …, 15 = signature 15",
    "16778269-1-724": "Signature Rear Lights Data type for eQ5, otherwise globally valid",
    "16778269-2-728": "Signature Rear Lights Data type for E6, otherwise globally valid",
    "16778269-255-731": "Data set that contains structured information encompassing all variants of the rear light signature. struct from all variants",
    "16778269-3-730": "Signature Rear Lights Data type for B10, otherwise globally valid",
    "16778269-4-725": "Signature Rear Lights Data type for Q5NF, otherwise globally valid",
    "16778269-5-729": "Signature Rear Lights Data type for C9, otherwise globally valid",
    "16778269-6-726": "Signature Rear Lights Data type for Q7NF, otherwise globally valid",
    "16778269-7-722": "Data set that contains information about the signature of the rear lights, defined by a specific data type applicable for Q9 and globally valid otherwise. Data type for Q9, otherwise globally valid",
    "16778269-8-723": "Data set that contains information about the signature of the rear lights. Data type for Q8 e-tron, otherwise globally valid",
    "16778269-9-727": "Data set that contains information about the signature of the rear lights, applicable globally except for specific configurations related to a particular vehicle type. Data type for Landjet, otherwise globally valid",
    "16778275-0-48": "Data set that contains information about the headlight signature configuration of the vehicle. 0 = default (no signature), 1 = signature 1, 2 = signature 2, 3 = signature 3, 4 = signature 4, 5 = signature 5, …, 15 = signature 15",
    "16778275-1-724": "Signature Headlights Data type for eQ5, otherwise globally valid",
    "16778275-2-728": "Signature Headlights Data type for E6, otherwise globally valid",
    "16778275-255-731": "Data set that contains structured information encompassing all variants of the headlight signature. struct from all variants",
    "16778275-3-730": "Signature Headlights Data type for B10, otherwise globally valid",
    "16778275-4-725": "Data set that contains information about the signature of the headlights. Data type for Q5NF, otherwise globally valid",
    "16778275-5-729": "Signature Headlights Data type for C9, otherwise globally valid",
    "16778275-6-726": "Signature Headlights Data type for Q7NF, otherwise globally valid",
    "16778275-7-722": "Signature Headlights Data type for Q9, otherwise globally valid",
    "16778275-8-723": "Data set that contains information about the signature of the headlights. Data type for Q8 e-tron, otherwise globally valid",
    "16778275-9-727": "Signature Headlights Data type for Landjet, otherwise globally valid",
    "16778278--": "Data set that provides information about the activation status of the communication light.",
    "16778278-0-515": "Data set that indicates whether the communication light is active or inactive. 0 = Off 1 = On",
    "16778278-1-515": "Data set that indicates whether the communication light is active or inactive. 0 = Off 1 = On",
    "16778281-1-562": "Data set that contains version information and UV values for two colors related to ambient light color settings. version information, U'V' values for two colors",
    "184555379-0-65": "Data set that contains information about the level of the steering wheel heating, indicating its intensity or whether it is turned off. Level 0 â",
    "184555399-0-66": "Data set that provides information about the seat heating level of the driver seat. Level 0 … 3 (0 = Off)",
    "184555401-0-332": "Data set that provides information about the seat heating balance. n/a",
    "184555402-0-332": "Data set that provides information about the seat vent balance. n/a",
    "184555404-0-334": "Data set that provides information about the driver seat vent level. n/a",
    "184555405-0-335": "Data set that provides information about the precondition driver seat heating. n/a",
    "184555406-0-335": "Data set that provides information about the precondition driver seat vent. n/a",
    "184555415-0-337": "Data set that provides information about the in car vent driver seat vent coupling. n/a",
    "251666249-0-77": "Data set that provides information about the status and configuration of the speed limit assistant, including whether the adoption feature is enabled or disabled. Bit 0 False adoption OFF, True adoption ON, Bit 1..7 False reserved, True reserved, Tempolimitassistent, Defaultwert: Aus",
    "251666250-0-78": "Data set that contains information about deviations from the standard speed limit settings, represented as predefined offset levels. 0x00 speedlimit_offset_OFF, 0x01 small_speedlimit_offset, 0x02 medium_speedlimit_offse t, 0x03 large_speedlimit_offset, 0x04..0xFF reserved",
    "251666255-0-337": "Data set that provides information about predictive ESC. n/a",
    "251666255-0-68": "Data set that contains information about the activation status of the predictive electronic stability control (ESC) system. On/Off",
    "251666256-0-363": "Trigger Threshold",
    "251666261-0-80": "Data set that provides information about the activation status of the traffic jam end assistant system. Bit 0 False System OFF True System ON Bit 1..7 False reserved True reserved",
    "251666266-0-822": "Data set that provides information about the activation status of the Traffic Light Assist feature. 0x00: TrafficLightAssist OFF, 0x01: TrafficLightAssist ON, 0x02-0xFF: reserved",
    "251666267-0-9": "Data set that contains information about the activation status of a pre-warning system. On/Off",
    "251666268-0-81": "Prewarning Time Gap Bit 0..7 0x00 Off (if pre-warning last mode coded) 0x01 late 0x02 medium 0x03 early 0x04..0xFF reserved",
    "251666272-0-824": "Data set that provides information about the activation status of the Stop Sign Assist feature. 0x00: Stop Sign Assist OFF, 0x01: Stop Sign Assist ON, 0x02-0xFF: reserved",
    "251666273-0-825": "Data set that provides information about the activation status of the swarm speed assist feature. 0x00: Swarm Speed Assist OFF, 0x01: Swarm Speed Assist ON, 0x02-0xFF: reserved",
    "251666274-0-823": "Data set that provides information about the status of the predictive speed control system. 0x00: predictive speed control OFF, 0x01: predictive speed control reduced, 0x02: predictive speed control ON, 0x03-0xFF: reserved",
    "251666276-0-827": "Driver Offset 0x00: pAccOffset OFF, 0x01: pAccOffset ON, 0x02-0xFF: reserved",
    "251666277-0-828": "Driver Offset Lower 0x0F: init/unknown",
    "251666278-0-828": "Driver Offset Top 0x0F: init/unknown",
    "251666279-0-829": "Driver Offset Unit 0x00: pAccOffsetUnit : kmh, 0x01: pAccOffsetUnit : mph, 0x02-0xFF: reserved",
    "251666280-0-82": "Data set that contains information about the activation status of the Side Assist system, also referred to as Blind Spot Detection (BSD). On/Off",
    "251666282-0-830": "Driver Offset Country Code 0x00: no information available (init state) 0xFE: no country info available (e.g. no map data in navigation)",
    "251666301-0-84": "Data set that contains information about the vibration status of a system, indicating whether it is enabled or disabled. 0 = on; 1 = off",
    "251666302-0-84": "Data set that contains information about the activation status of the Emergency Assist feature. 0 = on; 1 = off",
    "251666303-0-85": "Data set that contains information about the intensity level of steering intervention, which can be categorized into predefined levels such as weak, medium, or strong. 0x00 weak, 0x01 medium, 0x02 strong",
    "251666340-0-36": "Instrument Display Settings 0 = off; 1 = on",
    "251666341-0-36": "Data set that provides information about the activation status of the speed warning system associated with traffic sign recognition. 0 = off; 1 = on",
    "251666342-0-86": "Data set that provides information about the configurable speed warning offset settings in the vehicle. km/h: 0; 5; 10; 15; 20 mph: 0; 3; 6; 9; 12",
    "251666346-0-88": "Data set that contains information about the unit of measurement for the speed warning system. 0 = km/h; 1 = mph",
    "251666347-0-88": "Data set that contains information about the unit of measurement for the maximum speed limit applicable to trailers. 0 = km/h; 1 = mph",
    "251666349-0-84": "Corner Cut 0 = on; 1 = off",
    "251666350-0-89": "Data set that contains information about the warning threshold settings for the distance warning system. 0 = off; 1 = 1s; 2 = 2s; 3 = 3s",
    "251666390-0-361": "Data set that provides information about eco driving tips. n/a",
    "251666390-0-9": "Data set that contains information about the activation status of eco driving recommendations. On/Off",
    "251666391-0-362": "Data set that contains information about the feedback status of the accelerator pedal.",
    "2600000001-1-1001": "General Test Parameter n/a",
    "2600000001-2-1101": "General Test Parameter n/a",
    "2600000001-3-1201": "General Test Parameter n/a",
    "2600000002-1-1002": "General Test Parameter n/a",
    "2600000002-2-1202": "General Test Parameter n/a",
    "2600000003-1-1003": "General Test Parameter n/a",
    "2600000003-2-1103": "General Test Parameter n/a",
    "2600000003-3-1203": "General Test Parameter n/a",
    "2700000001-1-1001": "General Test Parameter n/a",
    "2700000002-1-1002": "General Test Parameter n/a",
    "2700000002-2-1102": "General Test Parameter n/a",
    "2700000002-3-1302": "General Test Parameter n/a",
    "2700000003-2-1103": "General Test Parameter n/a",
    "2700000003-3-1303": "General Test Parameter n/a",
    "2800000001-1-1001": "General Test Parameter n/a",
    "2800000001-2-1201": "General Test Parameter n/a",
    "2800000001-3-1301": "General Test Parameter n/a",
    "2800000002-1-1002": "General Test Parameter n/a",
    "2800000002-2-1202": "General Test Parameter n/a",
    "2800000002-3-1302": "General Test Parameter n/a",
    "2800000003-2-1203": "General Test Parameter n/a",
    "2800000003-3-1303": "General Test Parameter n/a",
    "285221674-0-68": "Data set that provides information about the activation status of the maneuvering brake function. On/Off",
    "285221676-0-82": "Data set that contains information about the activation status of the Rear Cross Traffic Alert (RCTA) system. On/Off",
    "285221677-0-68": "Rcta Brake Intervention On/Off",
    "285221679-0-665": "Data set that contains information about the volume level of audio output. 1 = quiet, 5 = medium; 9 = loud",
    "285221681-0-766": "Data set that provides information about the dynamically set target distance to the car ahead. 0=NEAR; 1=MEDIUM; 2=FAR;",
    "285221773-0-90": "Data set that contains information about the pitch frequency of the front sound emitter. 1=500Hz,… 9=2000Hz; 10=988Hz",
    "285221774-0-91": "Volume Front Sound Emitter 1 = quiet, 9 = loud",
    "285221775-0-92": "Data set that contains information about the pitch frequency settings of the rear sound emitter. 1=500Hz,… 9=2000Hz; 10=988Hz",
    "285221776-0-91": "Data set that contains information about the volume level of the rear sound emitter. 1 = quiet, 9 = loud",
    "285221874-0-511": "Data set that contains information about the status of an audible notification when a parking space is detected. 0 = off; 1 = on",
    "285221975-0-513": "Data set that contains information about the last selected front view mode of the vehicle. 0 = FrontView parking box; 1 = FrontView panorama",
    "352341537-0-36": "Data set that provides information about the activation status of vehicle-to-everything (V2X) communication. 0 = off; 1 = on",
    "352341537-10-36": "Data set that provides information about the activation status of vehicle-to-everything (V2X) communication. 0 = off; 1 = on",
    "352341537-255-414": "Data set that provides information about the activation status of C2X communication. [{datatype:bool, description\":\"\", min:0, max\":\"1\", stepsize:1, default\":\"1\", datalength:1},{datatype:u int8, description\":\"\", min:0, max\":\"255\", stepsize:1, default\":\"1\", datalength:1}]",
    "352341541-0-36": "Data set that provides information about the activation status of a safety system intervention. 0 = off; 1 = on",
    "352341541-0-368": "Data set that provides information about a security intrusion. [{\"datatype\":\"bool\",\"descr iption\":\"\",\"min\":\"0\",\"max\" :\"1\",\"stepsize\":\"1\",\"defaul t\":\"1\",\"datalength\":\"1\"},{\" datatype\":\"uint8\",\"descri ption\":\"\",\"min\":\"0\",\"max\": \"255\",\"stepsize\":\"1\",\"defa ult\":\"1\",\"datalength\":\"1\"}]",
    "352341544-0-36": "Traffic Jam End Status 0 = off; 1 = on",
    "352341544-0-368": "Data set that provides information about the end of a traffic jam. [{\"datatype\":\"bool\",\"descr iption\":\"\",\"min\":\"0\",\"max\" :\"1\",\"stepsize\":\"1\",\"defaul t\":\"1\",\"datalength\":\"1\"},{\" datatype\":\"uint8\",\"descri ption\":\"\",\"min\":\"0\",\"max\": \"255\",\"stepsize\":\"1\",\"defa ult\":\"1\",\"datalength\":\"1\"}]",
    "352341547-0-36": "Visibility Obstruction 0 = off; 1 = on",
    "352341547-0-368": "Data set that provides information about limited visibility. [{\"datatype\":\"bool\",\"descr iption\":\"\",\"min\":\"0\",\"max\" :\"1\",\"stepsize\":\"1\",\"defaul t\":\"1\",\"datalength\":\"1\"},{\" datatype\":\"uint8\",\"descri ption\":\"\",\"min\":\"0\",\"max\": \"255\",\"stepsize\":\"1\",\"defa ult\":\"1\",\"datalength\":\"1\"}]",
    "352341548-0-36": "Data set that provides information about the activation status of a special-purpose vehicle feature. 0 = off; 1 = on",
    "352341548-0-368": "Data set that provides information about special operations vehicle. [{\"datatype\":\"bool\",\"descr iption\":\"\",\"min\":\"0\",\"max\" :\"1\",\"stepsize\":\"1\",\"defaul t\":\"1\",\"datalength\":\"1\"},{\" datatype\":\"uint8\",\"descri ption\":\"\",\"min\":\"0\",\"max\": \"255\",\"stepsize\":\"1\",\"defa ult\":\"1\",\"datalength\":\"1\"}]",
    "352341549-0-36": "Data set that contains information about the activation status of special-purpose vehicles. 0 = off; 1 = on",
    "352341549-0-368": "Data set that provides information about special operations vehicles. [{\"datatype\":\"bool\",\"descr iption\":\"\",\"min\":\"0\",\"max\" :\"1\",\"stepsize\":\"1\",\"defaul t\":\"1\",\"datalength\":\"1\"},{\" datatype\":\"uint8\",\"descri ption\":\"\",\"min\":\"0\",\"max\": \"255\",\"stepsize\":\"1\",\"defa ult\":\"1\",\"datalength\":\"1\"}]",
    "385899971-0-315": "Data set that contains a semicolon-separated list of key-value pairs, represented as a string. semicolon separated list of key-value-pairs",
    "50333649-1-49": "Data set that contains information about the door unlocking configuration of the vehicle. 0 = single door unlocking; 1 = side-selective unlocking; 2 = full vehicle unlocking",
    "50333651-1-36": "Acknowledgment Tone 0 = off; 1 = on",
    "50333653-1-36": "Data set that provides information about the status of the electric cargo area cover. 0 = off; 1 = on",
    "50333654-0-51": "Deactivation IRU And NGS 0 = IRÃ and NGS off; 1 = IRÃ on; 2 = NGS on; 3 = IRÃ and NGS on",
    "83889081-1-36": "Data set that contains information about the activation status of the passenger-side mirror tilt function when the vehicle is in reverse gear. 0 = off; 1 = on",
    "83889082-1-36": "Data set that contains information about the synchronization setting for mirror adjustments. 0 = off; 1 = on",
    "83889083-1-36": "Data set that contains information about the configuration setting for folding mirrors during parking. 0 = off; 1 = on",
    "83889084-0-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the X-axis. Mirror position",
    "83889084-1-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the X-axis. Mirror position",
    "83889084-255-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the X-axis. Mirror position",
    "83889085-0-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the Y-axis. Mirror position",
    "83889085-1-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the Y-axis. Mirror position",
    "83889085-255-4": "Data set that contains information about the position of the exterior mirror on the driver's side along the Y-axis. Mirror position",
    "83889086-0-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis. Mirror position",
    "83889086-1-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis. Mirror position",
    "83889086-255-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis. Mirror position",
    "83889087-0-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the Y-axis. Mirror position",
    "83889087-1-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the Y-axis. Mirror position",
    "83889087-255-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the Y-axis. Mirror position",
    "83889088-0-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis when the mirror is tilted. Mirror position",
    "83889088-1-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis when the mirror is tilted downward. Mirror position",
    "83889088-255-4": "Data set that contains information about the position of the exterior mirror on the passenger side along the X-axis when the mirror is tilted downward. Mirror position",
    "83889089-0-4": "Data set that contains information about the vertical position of the passenger-side exterior mirror when adjusted or lowered. Mirror position",
    "83889089-1-4": "Data set that contains information about the vertical position adjustment of the passenger-side exterior mirror when lowering the mirror. Mirror position",
    "83889089-255-4": "Data set that contains information about the vertical position adjustment of the passenger-side exterior mirror when lowering. Mirror position",
    "83889096-0-505": "Data set that provides information about lowering driverside mirror position.",
    "83889097--": "Data set that contains information about the vertical position adjustment of the exterior mirror on the driver's side when the mirror is tilted downward.",
    "83889097-0-505": "Data set that contains information about the vertical position adjustment of the exterior mirror on the driver's side when tilting the mirror downward.",
    "83889097-1-505": "Data set that contains information about the vertical position adjustment of the exterior mirror on the driver's side when tilting the mirror downward.",
    "83889102-0-575": "View Main Section Y Position Left Unit_None",
    "83889103-0-576": "View Main Section Position Left Unit_None",
    "83889104-0-577": "View Main Section Y Position Right Unit_None",
    "83889105-0-576": "View Main Section X Position Right Unit_None",
    "83889106-0-578": "Park Section Y Position Left Unit_None",
    "83889107-0-576": "Park Section X Position Left Unit_None",
    "83889108-0-578": "Data set that contains information about the vertical position of the right park section view in a vehicle. Unit_None",
    "83889109-0-576": "Data set that contains information about the horizontal position of a specific park section on the right side, represented as an integer value. Unit_None",
    "83889110-0-577": "View Highway Y Position Left Unit_None",
    "83889111-0-579": "Highway X Position Left Unit_None",
    "83889112-0-577": "Highway Y Position Right Unit_None",
    "83889113-0-579": "View Highway X Position Right Unit_None",
    "83889114-0-582": "Data set that contains information about the brightness status of a vehicle system, represented as a percentage. %",
    "83889115-0-583": "Data set that provides information about the activation status of the highway view feature. 0 = off; 1 = on",
    "83889116-0-583": "Data set that provides information about the activation status of the turn view functionality. 0 = off; 1 = on",
    "83889117-0-583": "Data set that provides information about the activation status of the Park View system. 0 = off; 1 = on",
    "AccessStatus.[*].AccessStatusDoorInfo.[*].name": "UC Vehicle State Reminder: Status der Klappen des Fahrzeugs",
    "AccessStatus.[*].AccessStatusDoorInfo.[*].status.[*]": "UC Vehicle State Reminder: Status der Klappen des Fahrzeugs",
    "AccessStatus.[*].AccessStatusWindowInfo.[*].name": "UC Vehicle State Reminder: Status der Klappen des Fahrzeugs",
    "AccessStatus.[*].AccessStatusWindowInfo.[*].status.[*]": "UC Vehicle State Reminder: Status der Klappen des Fahrzeugs",
    "ActivationState": "Activation status of the Plug &amp; Charge Authorization",
    "Actual Charge Rate": "Indicates actual charge rate",
    "Adapter": "adapter for NACS charging station",
    "Adblue/Scr Range Unit": "Specifies the unit of which the value was sent in",
    "Adblue/Scr Range Value": "Specifies the distance until adblue needs to be refilled",
    "Adblue/Scr RangeTimestamp": "Specifies the time at which this value was sent from the vehicle.",
    "BCM1_Aussen_Temp_ungef_XIX_Klima_Sensor_02_MQB_XIX_E3V_VLAN_Connect": "Unfiltered outside-air temperature reading from BCM1.",
    "BEM Level": "BEM (Battery Energy Management) Level for auxiliary battery",
    "BLEIDENT.bledataDdas.[*].dataRevisionNr": "Counter. Incremented when the vehicle transmits new changed data.",
    "BLEIDENT.bledataDdas.[*].publicKeyEncoded": "Public key of the DDA (base64-encoded)",
    "BLEIDENT.bledataDdas.[*].stsEncoded": "Symmetric Transceiver Secret (base64-encoded)",
    "BLEIDENT.bledataDdas.[*].updateDatetime": "Time at which the record was created or changed",
    "BLEIDENT.swapagreeVins.[*].updateDatetime": "Time at which the record was created or changed",
    "BLEIDENT.swapagrees.[*].bledataDevice.accessRight": "Access Right for Holoride.",
    "BLEIDENT.swapagrees.[*].bledataDevice.lastusage": "Time when the device was last used.",
    "BLEIDENT.swapagrees.[*].bledataDevice.publicKeyEncoded": "Public key of the device (base64-encoded)",
    "BLEIDENT.swapagrees.[*].bledataDevice.updateDatetime": "Time at which the record was created or changed",
    "BLEIDENT.swapagrees.[*].updateDatetime": "Time at which the record was created or changed",
    "BLEIDENT.swapdatas.[*].updateDatetime": "Time at which the record was created or changed",
    "BMC_IWU_Wert_neg_XIX_BMC_HV_02_XIX_HCP5_CANFD01": "Insulation resistance value between HV— chassis as measured by the BMC.",
    "BMC_IWU_Wert_pos_XIX_BMC_HV_02_XIX_HCP5_CANFD01": "Insulation resistance value between HV+ and chassis as measured by the BMC.",
    "BMC_Leerlaufspannung_XIX_BMC_HV_02_XIX_HCP5_CANFD01": "No-load voltage measured at the HV battery by the BMC.",
    "BMC_Spannung_DC_Ladesaeule_XIX_BMC_HV_02_XIX_HCP5_CANFD01": "Voltage at the DC charging station measured between the DC HV lines.",
    "BMC_Spannung_XIX_BMC_HV_01_XIX_HCP5_CANFD01": "Instantaneous battery voltage measured by the BMC.",
    "BMC_Spannung_ZwischenKreis_2_XIX_BMC_HV_01_XIX_HCP5_CANFD01": "Instantaneous value: Second DC link voltage for batteries with two HV outputs (two HV circuits)",
    "BMC_Spannung_ZwischenKreis_XIX_BMC_HV_01_XIX_HCP5_CANFD01": "Instantaneous DC link voltage of the battery measured by the BMC.",
    "BMC_Strom_02_XIX_BMC_HV_01_XIX_HCP5_CANFD02": "Second channel instantaneous current measurement from the BMC.",
    "BMC_Strom_XIX_BMC_HV_01_XIX_HCP5_CANFD01": "Instantaneous battery current measured by the BMC; positive = charge, negative = discharge.",
    "BMS_CMC_Temperatur_001_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #001, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_002_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #002, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_003_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #003, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_004_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #004, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_005_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #005, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_006_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #006, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_007_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #007, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_008_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #008, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_009_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #009, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_010_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #010, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_011_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #011, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_012_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #012, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_013_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #013, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_014_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #014, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_015_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #015, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_016_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #016, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_017_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #017, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_018_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #018, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_019_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #019, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_020_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #020, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_021_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #021, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_022_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #022, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_023_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #023, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_024_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #024, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_025_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #025, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_026_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #026, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_027_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #027, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_028_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #028, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_029_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #029, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_030_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #030, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_031_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #031, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_032_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #032, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_033_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #033, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_034_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #034, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_035_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #035, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_036_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #036, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_037_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #037, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_038_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #038, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_039_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #039, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_040_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #040, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_041_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #041, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_042_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #042, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_043_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #043, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_044_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #044, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_045_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #045, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_046_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #046, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_047_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #047, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_048_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #048, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_049_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #049, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_050_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #050, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_051_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #051, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_052_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #052, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_053_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #053, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_054_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #054, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_055_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #055, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Temperatur_056_XIX_BMS_CMC_04_Mx00_XIX_E3V_VLAN_Connect": "Temperature measurement for cell-module #056, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_001_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #001, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_002_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #002, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_003_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #003, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_004_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #004, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_005_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #005, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_006_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #006, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_007_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #007, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_008_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #008, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_009_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #009, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_010_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #010, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_011_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #011, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_012_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #012, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_013_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #013, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_014_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #014, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_015_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #015, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_016_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #016, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_017_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #017, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_018_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #018, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_019_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #019, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_020_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #020, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_021_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #021, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_022_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #022, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_023_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #023, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_024_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #024, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_025_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #025, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_026_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #026, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_027_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #027, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_028_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #028, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_029_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #029, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_030_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #030, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_031_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #031, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_032_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #032, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_033_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #033, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_034_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #034, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_035_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #035, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_036_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #036, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_037_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #037, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_038_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #038, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_039_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #039, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_040_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #040, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_041_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #041, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_042_XIX_BMS_CMC_04_Mx01_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #042, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_043_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #043, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_044_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #044, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_045_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #045, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_046_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #046, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_047_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #047, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_048_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #048, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_049_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #049, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_050_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #050, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_051_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #051, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_052_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #052, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_053_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #053, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_054_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #054, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_055_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #055, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_056_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #056, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_057_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #057, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_058_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #058, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_059_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #059, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_060_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #060, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_061_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #061, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_062_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #062, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_063_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #063, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_064_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #064, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_065_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #065, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_066_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #066, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_067_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #067, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_068_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #068, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_069_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #069, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_070_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #070, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_071_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #071, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_072_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #072, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_073_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #073, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_074_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #074, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_075_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #075, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_076_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #076, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_077_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #077, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_078_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #078, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_079_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #079, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_080_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #080, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_081_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #081, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_082_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #082, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_083_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #083, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_084_XIX_BMS_CMC_04_Mx02_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #084, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_085_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #085, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_086_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #086, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_087_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #087, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_088_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #088, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_089_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #089, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_090_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #090, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_091_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #091, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_092_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #092, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_093_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #093, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_094_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #094, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_095_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #095, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_096_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #096, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_097_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #097, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_098_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #098, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_099_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #099, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_100_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #100, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_101_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #101, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_102_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #102, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_103_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #103, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_104_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #104, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_105_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #105, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_106_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #106, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_107_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #107, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_108_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #108, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_109_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #109, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_110_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #110, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_111_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #111, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_112_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #112, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_113_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #113, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_114_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #114, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_115_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #115, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_116_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #116, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_117_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #117, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_118_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #118, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_119_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #119, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_120_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #120, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_121_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #121, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_122_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #122, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_123_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #123, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_124_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #124, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_125_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #125, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_126_XIX_BMS_CMC_04_Mx03_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #126, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_127_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #127, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_128_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #128, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_129_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #129, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_130_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #130, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_131_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #131, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_132_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #132, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_133_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #133, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_134_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #134, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_135_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #135, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_136_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #136, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_137_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #137, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_138_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #138, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_139_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #139, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_140_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #140, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_141_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #141, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_142_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #142, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_143_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #143, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_144_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #144, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_145_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #145, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_146_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #146, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_147_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #147, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_148_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #148, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_149_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #149, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_150_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #150, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_151_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #151, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_152_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #152, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_153_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #153, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_154_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #154, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_155_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #155, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_156_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #156, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_157_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #157, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_158_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #158, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_159_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #159, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_CMC_Zellspannung_160_XIX_BMS_CMC_04_Mx04_XIX_E3V_VLAN_Connect": "Individual cell voltage for cell #160, transmitted via the Cell Monitoring Controller (CMC) over the RTM interface.",
    "BMS_Pack_SN_14_XIX_BMS_28_XIX_E3V_VLAN_Connect": "ASCII-encoded byte 14 of the high-voltage battery pack serial number.",
    "BMS_RIso_Ext_XIX_BMS_24_XIX_E3V_VLAN_Connect": "Minimum insulation resistance between the battery housing and HV+/-when contactors are closed (external measurement).",
    "BMS_RuecklaufTemperatur_XIX_BMS_25_XIX_E3V_VLAN_Connect": "Measured temperature of the cooling medium at the outlet of the battery cooling circuit.",
    "BMS_Spannung_XIX_BMS_20_XIX_E3V_VLAN_Connect": "The instantaneous voltage of the battery measured at cell modules, before contactors and fuses.",
    "BMS_Spannung_Zwischenkreis_XIX_BMS_20_XIX_E3V_VLAN_Connect": "Voltage at the battery output to the DC link / power electronics / HV network.",
    "BMS_Strom_XIX_BMS_20_XIX_E3V_VLAN_Connect": "The instantaneous current of the battery. Positive = charging current, negative = discharging current.",
    "BMS_Temperatur_XIX_BMS_25_XIX_E3V_VLAN_Connect": "The current temperature of the traction battery, measured internally at the cell-module level.",
    "BMS_VorlaufTemperatur_XIX_BMS_25_XIX_E3V_VLAN_Connect": "Measured temperature of the cooling medium at the inlet of the battery cooling circuit.",
    "Battery charging status -SOC": "Indicates the current charging status for the battery",
    "BatteryTemperature.[*].temperatureHvBatteryMaxKelvin": "UC Battery Cold Warning: Battery temperature to evalute critical situations",
    "BatteryTemperature.[*].temperatureHvBatteryMinKelvin": "UC Battery Cold Warning: Battery temperature to evalute critical situations",
    "Blocking": "The Blocking of the ContractStatusView structure",
    "CF.tCfCalls.[*].callbackStatus": "Enum -CALLBACK_WAIT,CALLBA CK_RECEIVED,TIMEOUT_ PROCESSED,ERROR_PR OCESSED",
    "CF.tCfCalls.[*].expirationTime": "TCFCalls expiration time stamp",
    "CF.tCfCalls.[*].tVehicleData.direction": "Value representing the bearing or heading in degrees.",
    "CURRENT_GEAR": "Reflects the actual gear that the vehicle's transmission is currently in. This may not match the driver's selection (from GEAR_SELECTION) due to conditions like clutch delay, safety checks, or automatic behavior in CVTs or automatics.",
    "CarCapturedTime": "The CarCapturedTime of the AggregatedVehicleStateBl ocksWithHeaderJsonOdp 15 structure",
    "CertValidityEnd": "End date of validity for the technical Plug &amp; Charge certificate",
    "CertValidityStart": "Start date of validity for the technical Plug &amp; Charge certificate",
    "CertificateInstallationRes": "The CertificateInstallationRes of the ProvideSignedContractDa taResponse structure",
    "Certificates": "Container holding provisioning certificate to install on the vehicle",
    "Charge Target Time": "Target time for charging in Day,hour,minute,month,o ffset,year with # as delimeter",
    "Charging Mode": "Mode of charging",
    "Charging Power": "Power of charging",
    "Charging Reason (Trigger)": "Trigger for charging",
    "Charging State": "Indicates the state of charging with invalid, unsupported, off, charching, completed, error, conservationCharging",
    "ChargingCareSettings.[*].batteryCareMode": "UC Battery Protection: Activation state of battery care mode to prevent notifications if already active",
    "ChargingEvent.[*].BatteryStatus.[*].cruisingRangeElectricKm": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].BatteryStatus.[*].currentSocPct": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].BatteryStatus.[*].navigationTargetSocPct": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargeMode": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargePowerKW": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargeRateKmph": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargeTargetTime": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargeType": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargingScenario": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargingSettings": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].chargingState": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].estimatedFinishTimeLocal": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].remainingChargingTimeNavigationMin": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].ChargingStatus.[*].remainingChargingTimeToCompleteMin": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].externalPower": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].flapOpenState": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].ledColor": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].plugConnectionState": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].plugLockState": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingEvent.[*].PlugStatus.[*].plugPosition": "UC Battery Protection / Battery Cold Warning: Charging event information for use case logic",
    "ChargingProfileStatus.[*].ChargingProfile.[*].ChargingProfileOption.[*].autoUnlockPlugWhenCharged": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].ChargingProfileOption.[*].usePrivateCurrentEnabled": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].PreferredChargingTime.[*].enabled": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].PreferredChargingTime.[*].endTime": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].PreferredChargingTime.[*].id": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].PreferredChargingTime.[*].startTime": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].id": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].maxChargingCurrent": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].minSOCPct": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].name": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].profileType": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].relevantAtCurrentLocation": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].targetSOCPct": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].ChargingProfile.[*].uuid": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].NextChargingTimer.[*].id": "Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].NextChargingTimer.[*].targetSOCReachable": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].timeInCar": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ChargingProfileStatus.[*].vehiclePositionedInProfileID": "UC Predictive Wakeup: Charging Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].fridays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].mondays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].saturdays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].sundays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].thursdays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].tuesdays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].RecurringOn.[*].wednesdays": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].startTime": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].RecurringTimer.[*].startTimeLocal": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].SingleTimer.[*].startDateTime": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].SingleTimer.[*].startDateTimeLocal": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].enabled": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].ClimatisationTimer.[*].id": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].ClimatisationTimerStatus.[*].timeInCar": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].climatisationStatus.[*].climatisationState": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].climatisationStatus.[*].climatisationTrigger": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ClimatisationEvent.[*].climatisationStatus.[*].minRemainingClimatisationTime": "UC Predictive Wakeup: Climatisation Timer information for vehicle wakeup trigger",
    "ContractContainer": "Container holding Contract-Certificate to install on the vehicle",
    "ContractId": "ID (emaid) of the contract certificate",
    "ContractMemoryIndex": "Memory index of the contract certificate",
    "ContractState": "Installation status of the contract certificate",
    "ContractStatus": "The ContractStatus of the MessageBody structure",
    "CurrencyUnit": "The CurrencyUnit of the PncContractInfoType structure",
    "DWA.historyEntries.[*].vehicleUtcTimestamp": "Timestamp sent by the vehicle.",
    "DW_Kilometerstand": "The total distance that a vehicle has traveled, as recorded by the odometer. physical value range: [0; 1048573] raw value range: [0; 1048573] scale: 1 offset: 0 Init value (raw): 1048574 Error value (raw): 1048575",
    "Delete": "or record is flagged for deletion.",
    "EM1_Strom_HV_XIX_EM1_01_XIX_E3V_VLAN_Connect": "Instantaneous high-voltage DC current of powertrain motor 1.",
    "EM2_Strom_HV_XIX_EM2_01_XIX_E3V_VLAN_Connect": "Instantaneous high-voltage DC current of powertrain motor 2.",
    "ElectricDrive__ElectricDrive_electricDrives_1__currentDCCurrent": "Instantaneous high-voltage DC current of powertrain motor 1.",
    "ElectricDrive__ElectricDrive_electricDrives_2__currentDCCurrent": "Instantaneous high-voltage DC current of powertrain motor 2.",
    "Emaid": "Identifier for the contract certificate",
    "Energy Flow": "Flow of energy",
    "EnvironmentalSensorData__EnvironmentalSensorData_sensorData_1__outdoorRawTemperature": "Unfiltered outside-air temperature reading from BCM1.",
    "External Power SupplyState": "State of external power supply",
    "FinishTimeDate": "time/date of current charging process to reach a target SOC as defined in the charging settings or in an active charging profile.",
    "Fuel Level State": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "Fuel Level Timestamp": "Specifies the time at which this value was sent from the vehicle.",
    "Fuel Level Trigger Type": "Specifies what triggered the stored data to be sent from the vehicle",
    "Fuel Level Value": "Specifies the amount of fuel left in the tank",
    "GEOFEN.definitionLists.[*].callerInformation.channel": "The channel used to send the Definition list was used",
    "GEOFEN.definitionLists.[*].callerInformation.traceId": "traceId",
    "GEOFEN.definitionLists.[*].currentResyncTries": "Number of resync attempts",
    "GEOFEN.definitionLists.[*].definitions.[*].alertSchedule.startTime": "General start of the Validity period of the schedule",
    "GEOFEN.definitionLists.[*].definitions.[*].definitionIndex": "Definition Index in the vehicle",
    "GEOFEN.definitionLists.[*].definitions.[*].definitionListId": "Reference to the corresponding List",
    "GEOFEN.definitionLists.[*].definitions.[*].definitionName": "The name of the definition",
    "GEOFEN.definitionLists.[*].definitions.[*].geofencingArea.rotationAngle": "The rotation angle of the Area in degrees.",
    "GEOFEN.definitionLists.[*].definitions.[*].geofencingArea.zoneType": "Type of zone",
    "GEOFEN.definitionLists.[*].status": "Status of the definition list",
    "GEOFEN.definitionLists.requestType": "Type of list that sent to vehicle",
    "GEOFEN.geofencingAlerts.[*].definitionId": "Reference to definition Id.",
    "GEOFEN.geofencingAlerts.[*].definitionIndex": "Definition Index in the vehicle",
    "GEOFEN.geofencingAlerts.[*].definitionName": "The name of the definition",
    "GEOFEN.geofencingAlerts.[*].occurrencePosition.trueness": "Reliability of the GPS signal.",
    "GEOFEN.geofencingJobs.[*].debouncePostTime": "Value for time advance in seconds until the vehicle sends a Geofencing Violation notification to the Business Service",
    "GEOFEN.geofencingJobs.[*].debouncePreTime": "Value for time lag in seconds until the vehicle sends a Geofencing Violation notification to the Business Service",
    "GEOFEN.geofencingJobs.[*].definitionListId": "The ID of the associated list",
    "GEOFEN.geofencingJobs.[*].service": "Service name",
    "GEOFEN.geofencingJobs.[*].spatialTolerance": "Value for spatial tolerance, up to the vehicle sends a geofencing violation to the business service.",
    "GEOFEN.geofencingJobs.[*].status": "Status of the job",
    "GEOFEN.requestHistoryEntries.[*].definitionListId": "Reference to the corresponding DefinitionList.",
    "GEOFEN.requestHistoryEntries.[*].requestResult": "Result of the request",
    "GEOFEN.requestHistoryEntries.[*].requestTimestamp": "The time stamp of the Updating the entry in the database.",
    "GEOFEN.requestHistoryEntries.[*].requestType": "Type of request",
    "HV-SOC": "Indicates the state of charge for the high voltage battery",
    "HVLE_OBG_DC_IstStrom_XIX_OBG_01_XIX_HCP5_CANFD01": "Actual DC charging current measured by the charger OBG module.",
    "HVLE_Temperatur_XIX_IPB_03_XIX_E3V_VLAN_Connect": "Temperature of the high-voltage electronics control unit (SAC).",
    "HVLE_Temperatur_XIX_IPB_03_XIX_HCP5_CANFD01": "Temperature of the high-voltage electronics control unit (SAC).",
    "HVLX_Isond_IstStromLadesaeule_XIX_ISOND_01_XIX_HCP5_CANFD01": "Instantaneous charging-station current measurement (EVSEPresentCurrent) per DIN70121 RC_2.",
    "Hvsoc State": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "Hvsoc Timestamp": "Last reported high voltage battery percentage",
    "Hvsoc Trigger Type": "Specifies what triggered the stored data to be sent from the vehicle",
    "Hvsoc Value": "Last reported high voltage battery percentage",
    "IDK_consent_document_version": "The version of the consent document for which the decision is stored",
    "INFO_MAKE": "Manufacturer of vehicle",
    "INFO_MODEL": "Model of vehicle",
    "INFO_MODEL_YEAR": "Model year of vehicle",
    "INFO_VIN": "VIN of vehicle",
    "IPB_Wasser_TEMP_AUS_XIX_IPB_03_XIX_HCP5_CANFD01": "Water temperature at the outlet of the IPB heat exchanger.",
    "IPB_Wasser_TEMP_EIN_XIX_IPB_03_XIX_HCP5_CANFD01": "Water temperature at the inlet of the IPB heat exchanger.",
    "Ignition.[*].ignitionOn": "UC Battery Protection / Battery Cold Warning: Ignition event as use case trigger and basis for trip calculation",
    "ImplementationStatus": "Implementation status of the Plug &amp; Charge feature",
    "KBI_Aussen_Temp_gef_XIX_Temperaturen_01_XIX_E3V_VLAN_Connect": "Filtered outside-air temperature measurement.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_Isolation_Resistance_Battery_Minus_0x1E1A": "Insulation resistance measured between the battery negative terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_Isolation_Resistance_Battery_Plus_0x1E18": "Insulation resistance measured between the battery positive terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_Isolation_Resistance_System_Minus_0x1E19": "Insulation resistance measured between the system negative and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_Isolation_Resistance_System_Plus_0x1E17": "Insulation resistance measured between the system positive and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_Temperature_of_battery_junction_box_0x58E2": "Temperature measured at the battery junction box.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_insulation_resistance_battery_minus_0x1E1A": "Insulation resistance measured between battery negative terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_insulation_resistance_battery_plus_0x1E18": "Insulation resistance measured between battery positive terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_insulation_resistance_system_minus_0x1E19": "Insulation resistance measured between system negative and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_insulation_resistance_system_plus_0x1E17": "Insulation resistance measured between system positive and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_isolation_resistance_battery_minus_0x1E1A_1": "Insulation resistance measured between the battery negative terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_isolation_resistance_battery_plus_0x1E18_1": "Insulation resistance measured between the battery positive terminal and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_isolation_resistance_system_minus_0x1E19_1": "Insulation resistance measured between the system negative and chassis.",
    "LL_BatteEnergContrModulUDS_ReadDataByIdentMeasuValue_isolation_resistance_system_plus_0x1E17_1": "Insulation resistance measured between the system positive and chassis.",
    "LL_HighVoltaCentrUnitUDS_ReadDataByIdentMeasuValue_Sac_temperature_actual_value_0x375": "Actual temperature of the high-voltage electronics control unit (SAC).",
    "LL_SmartActuaChargInterDevic1UDS_ReadDataByIdentMeasuValue_HVLS_temperature_0x302": "Temperature reading from -the first HV line’s sensor.",
    "LL_SmartActuaChargInterDevic2UDS_ReadDataByIdentMeasuValue_HVLS_temperature_0x302": "Temperature reading from -the second HV line—system sensor.",
    "LSS Record Time To Live": "Epoch seconds at which the row should be deleted. Currently not in use.",
    "Last Battery ChargerUpdate Trigger": "Contains trigger info about last battery charger update",
    "Lock State": "Describes the lock state",
    "Long Term Data: AverageAux ConsumerConsumption": "Average Auxiliary Consumer Consumption during long term trip",
    "Long Term Data: AverageElectr EngineConsumption": "Average Electric Engine Consumption during long term trip",
    "Long Term Data: AverageFuel Consumption": "Average Fuel Consumption during long term trip",
    "Long Term Data: AverageGas Consumption": "Average gas Consumption during long term trip",
    "Long Term Data: AverageRecuperation": "Average recuperation during long term trip",
    "Long Term Data: AverageSpeed": "Average speed",
    "Long Term Data: Mileage": "Overall Mileage for long term trips",
    "Long Term Data: RangeGain Distance": "Gained range distance in [0.1 km] during long term trip",
    "Long Term Data: StartMileage": "Mileage when trip started",
    "Long Term Data: TravelTime": "Time of trip with vehicle ready for driving",
    "Long Term Data: ZeroEmission Distance": "Distance driven without emission in [0,1km] during long term trip",
    "Lvsoc State": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "Lvsoc Timestamp": "Specifies the time at which this value was sent from the vehicle.",
    "Lvsoc Trigger Type": "Specifies what triggered the stored data to be sent from the vehicle",
    "Lvsoc Value": "Last reported low voltage battery percentage",
    "MOBKEY.keyVoucherList.[*].partNo": "Artikel aus dem Artikelkatalog vom Webshop für den Kauf der Vouchers",
    "MOBKEY.mobileKeyList.[*].installationState": "Status der MK-Vergabe",
    "MOBKEY.permissionList.[*].endingReason": "Entzugsursache von MK. Spielt beim steuern des Wiederholjobs eine Rolle falls ein MK vom System automatisch entzogen werden soll",
    "MOBKEY.permissionList.[*].removalState": "Placeholder for MOBKEY.permissionList.r emovalState",
    "MOBKEY.permissionList.[*].state": "permissionList state",
    "MOBKEY.serviceStatusList.[*].smartCardActivated": "Flag der Smartcard-Aktivierung",
    "Maintenance.[*].inspectionDueDays": "UC MOD4 data export: A Time until next inspection",
    "Maintenance.[*].inspectionDueKm": "UC MOD4 data export: A Time until next inspection",
    "Maintenance.[*].oilServiceDueKm": "UC MOD4 data export: A Time until next inspection",
    "MaxPriceMultiplier": "The MaxPriceMultiplier of the PncContractInfoType structure",
    "MaxPriceValue": "The MaxPriceValue of the PncContractInfoType structure",
    "MemoryIndex": "Save slot for contract certificate",
    "Message Data Message Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Message Data Send Count": "The number of messages sent towards the vehicle.",
    "Message Data SentTimestamp": "The time at which the latest message towards the vehicle was sent.",
    "Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "MoseCsr": "Certificate-Signing-Request in Mose-Format to request the provisioning certificate",
    "NextChargingTimer.[*].id": "Charging Timer information for vehicle wakeup trigger",
    "Number": "request is a response to a short message after upgrading to IP communication",
    "NumberSupportedContractChains": "Number of supported Contract Chains",
    "NumberSupportedPeRoots": "Number of supported Private Environment certificates",
    "NumberSupportedV2GRoots": "Number of supported Vehicle to Grid certificates",
    "OPR.calls.[*].auditTraceId": "Audit Trace ID,z.B. OPR-ivwb1035-3819312000000-ADE",
    "OPR.calls.[*].cause": "Reason for the breakdown call:Values:\"I\" = Incident/Accident —B â Breakdown/Panne",
    "OPR.calls.[*].dtcDataTime": "Represents the date and time associated with a (DTC).",
    "OPR.calls.[*].expirationDate": "Time from which the call can be deleted.",
    "OPR.calls.[*].gotSendGDCData": "Whether global diagnostic data or configuration has been received.",
    "OPR.calls.[*].gotSendVehicleData": "Whether specific vehicle data or a service has been received.",
    "OPR.calls.[*].instrumentClusterTime": "Date/time from the instrument cluster (no time zone)",
    "OPR.calls.[*].isDTCAvailable": "Indicates whether a DTC is present or accessible.",
    "OPR.calls.[*].isMarkedForDelete": "Indicates whether an item or record is flagged for deletion.",
    "OPR.calls.[*].isNotify": "Indicates whether a notification or alert is enabled or triggered.",
    "OPR.calls.[*].mileage": "Mileage of the vehicle",
    "OPR.calls.[*].obdcData.[*].dataValue": "Vehicle Data -Wert",
    "OPR.calls.[*].obdcData.[*].id.dataId": "ID of the data group of the Vehicle Data value (provided by the TSS)",
    "OPR.calls.[*].obdcData.[*].id.fieldId": "ID of the Vehicle Data value (supplied by the TSS)",
    "OPR.calls.[*].obdcData.[*].milCarCaptured": "Mileage of the vehicle when the value is recorded (in the vehicle)",
    "OPR.calls.[*].obdcData.[*].milCarSent": "Mileage of the vehicle when the value is transmitted to the backend",
    "OPR.calls.[*].obdcData.[*].picId": "Pictogram ID",
    "OPR.calls.[*].obdcData.[*].text": "Data value text",
    "OPR.calls.[*].obdcData.[*].textId": "Text ID transmitted by TSS",
    "OPR.calls.[*].obdcData.[*].tsCarCaptured": "Time of recording of the value in the vehicle (without time zone information)",
    "OPR.calls.[*].obdcData.[*].tsCarSent": "Time of transmission of the value from the vehicle (without time zone information)",
    "OPR.calls.[*].obdcData.[*].tsCarSentUtc": "Time of transmission of the value of the vehicle (in UTC+0000)",
    "OPR.calls.[*].obdcData.[*].tsTssReceivedUtc": "Time of receipt of the value by the TSS (in UTC+0000)",
    "OPR.calls.[*].obdcData.[*].unit": "Unit of value (e.g. km)",
    "OPR.calls.[*].remainingRange": "Remaining range of the vehicle when the function is triggered",
    "OPR.calls.[*].timeStampExecuted": "Stores the timestamp of when an operation was executed.",
    "OPR.calls.[*].timestampReceived": "Time the vehicle record was received by the OS in UTC+0000",
    "OPR.remoteCalls.[*].expirationDate": "Time from which the call can be deleted.",
    "OPR.remoteCalls.[*].requestCarDataFlag": "It is for requesting data from car in case of breakdown",
    "OPR.remoteCalls.[*].timestampReceived": "Time the vehicle record was received by the OS in UTC+0000",
    "OTV.customerContacts.[*].activationTimestamp": "Time at which it activated.",
    "OTV.customerContacts.[*].activator": "Information about trigger for the entry.Possible values: · CUSTOMER: Entry was made by customers via frontend clients (default) · AUTO: Entry was made via auto-activation. · OLD: Entry was made before the introduction of PCR_347",
    "OTV.customerContacts.[*].nextHistNotification": "Time of the next history notification. (is automatically determined by the multi-tent configuration)",
    "OTV.customerContacts.[*].notifyFlag": "The customer wants to be notified by email (via system WEGA) as soon as a new appointment is set in the OTV-BS. (Values: 1 = true, 0 = false)",
    "OTV.customerContacts.[*].otvCalls.[*].appointment.appointmentDate": "Scheduled time of the appointment (date/time)",
    "OTV.customerContacts.[*].otvCalls.[*].appointment.expirationDate": "Time from which the appointment can be deleted.",
    "OTV.customerContacts.[*].otvCalls.[*].appointment.notifyCustomer": "The customer should be notified by email (via system WEGA) about this date (values: 1 = true, 0 = false)",
    "OTV.customerContacts.[*].otvCalls.[*].appointment.requestId": "Unique ID of the request",
    "OTV.customerContacts.[*].otvCalls.[*].appointment.tzOffset": "Difference of time zone against UTC in minutes",
    "OTV.customerContacts.[*].otvCalls.[*].auditTraceId": "Audit Trace ID,e.g. OTV-ivwb1035-38193120000000-ADE",
    "OTV.customerContacts.[*].otvCalls.[*].expirationDate": "Time from which the call can be deleted.",
    "OTV.customerContacts.[*].otvCalls.[*].instrumentClusterTime": "Date/time from the instrument cluster (no time zone)",
    "OTV.customerContacts.[*].otvCalls.[*].mileage": "Mileage of the vehicle",
    "OTV.customerContacts.[*].otvCalls.[*].requestId": "Unique ID of the request (UUID). Generated by the OS when the request is received.",
    "OTV.customerContacts.[*].otvCalls.[*].timestampCarSent": "Time (date/time) when the vehicle record was sent by the vehicle in UTC.max. Accuracy: msec",
    "OTV.customerContacts.[*].otvCalls.[*].timestampReceived": "Time at which the vehicle data record was received by the OS in UTC",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleConfdata.requestId": "Unique ID of the request (UUID)",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].days": "Number of days",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].distance": "Distance covered by vehicle",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].eventType": "Type of event",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].id.eventIdx": "Event ID",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].id.requestId": "Unique ID of the request (UUID)",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].status": "Status of event",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleMaintEvents.[*].unit": "Unit of value (e.g. km)",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleStatus.requestId": "Unique ID of the request",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleStatus.vehicleStatusUid": "ID of vehicle condition in zFDI (supplied by TSS)",
    "OTV.customerContacts.[*].otvCalls.[*].vehicleStatus.warningLightPriority": "Warning light priority (\"Red\", \"Yellow\", \"White\")",
    "OTV.customerContacts.[*].postHistoryFlag": "The customer wants to be notified regularly if a request history is available. (Values: 1 = true, 0 = false)",
    "OTV.customerContacts.[*].remoteCalls.[*].auditTraceId": "Audit Trace ID,e.g. OTV-ivwb1035-38193120000000-ADE",
    "OTV.customerContacts.[*].remoteCalls.[*].blockUntil": "Time until which further remote trigger calls for the vehicle are rejected.",
    "OTV.customerContacts.[*].remoteCalls.[*].category": "Category of a \"remote\" request",
    "OTV.customerContacts.[*].remoteCalls.[*].correlationId": "Unique ID of the request (UUID) transmitted by the calling system",
    "OTV.customerContacts.[*].remoteCalls.[*].reason": "Reason for a \"remote\" request",
    "OTV.customerContacts.[*].remoteCalls.[*].timeoutDate": "Time at which the timeout expires.",
    "OTV.customerContacts.[*].remoteCalls.[*].timestampReceived": "Time at which the vehicle data record was received by the OS in UTC",
    "OTV.customerContacts.[*].servicepartner.insertDatetime": "Interl attribute: Interl timestamp of the insert",
    "OTV.customerContacts.[*].servicepartner.markedForDelete": "Interl attribute: Status fla (1= entry can be deleted, 0= entry cannot be deleted)",
    "OTV.customerContacts.[*].servicepartner.otvFlag": "Flag: SP participates in OTV (\"Y\" or \"N\")",
    "Odometer.[*].odometerKm": "UC MOD4 data export: Odometer information",
    "Oil level - Total/max": "Amount of motoroil level in liter",
    "Oil level - actual level": "Amount of current motoroil level in percentage",
    "OutsideTemperature.[*].temperatureOutsideKelvin": "UC MOD4 data export: Outside temperature data",
    "PERF_ODOMETER": "Represents the total accumulated distance traveled by the vehicle — essentially the odometer reading you see on the dashboard",
    "PSO.revisions.[*].changedClientTimestamp": "Timestamp of change without timezone",
    "PSO.revisions.[*].revisionNr": "Version number",
    "PSO.revisions.[*].syncGroup": "Technical id",
    "PSO.settings.[*].changedBy": "Data modified by",
    "PSO.settings.[*].changedClientTimestamp": "Timestamp of change without timezone",
    "PSO.settings.[*].format": "Technical Description for neutral format or hardware specific value",
    "PSO.settings.[*].id": "unique id for a entry in the table",
    "PSO.settings.[*].isFactorySetting": "Boolean flag to identify factory setting",
    "PSO.settings.[*].settingDefinition": "Technical id",
    "PSO.settings.[*].value": "Value of format",
    "PSO.subscriptions.[*].creationTimestamp": "Record creation timestamp",
    "PSO.syncData.[*].syncAttemptBackendTimestamp": "Timestamp",
    "PSO.syncData.[*].syncCli": "Timestamp",
    "PSO.syncData.[*].syncGroup": "Technical Id",
    "PSO.syncData.[*].syncRevisionNr": "Technical number of the sync",
    "PSO.syncData.[*].syncSuccessBackendTimestamp": "Timestamp",
    "PSO.syncGroup.[*].expirationTimestamp": "Timestamp",
    "PSO.syncGroup.[*].syncGroupElements.[*].format": "The id in SyncGroupElement is a system-generated unique identifier for each record in the SYNCGROUPELEMENT table, like a serial number that ensures each element can be individually tracked and managed.",
    "PSO.syncGroup.[*].syncGroupElements.[*].settingDefinitionId": "syncgroupelement.fk_set tingdefinition refers to a field named fk_settingdefinition in the syncgroupelement table or entity. The fk_ prefix indicates it is a foreign key, so this field likely references the primary key of the settingdefinition table/entity. This establishes a relationship where each syncgroupelement is associated with a specific settingdefinition.",
    "PSO.syncGroup.[*].syncGroupElements.[*].synGroupId": "refers to a field named fk_syncgroup in the syncgroupelement table or entity. The fk_ prefix suggests it is a foreign key, which means this field holds a reference to the primary key of the syncgroup table/entity. This establishes a relationship where each syncgroupelement is associated with a specific syncgroup.",
    "PSO.syncGroup.[*].syncGroupNumber": "Group of SyncGroupElements that are handled as a \"block\" with regard to data exchange",
    "PSO.vinState.[*].nextSyncGroupNr": "The nextSyncGroupNr field tracks the next available synchronization group number for a vehicle’s",
    "PSO.vinUserStateData.[*].syncAttemptBackendTimestamp": "Timestamp",
    "PSO.vinUserStateData.[*].syncSuccessBackendTimestamp": "Timestamp",
    "Parking brake": "Indicates if the parking brake is inactive (0) or active (1)",
    "ParkingPosition.[*].lat": "UC Predictive Wakeup: Last parking position to identify start up routines",
    "ParkingPosition.[*].lon": "UC Predictive Wakeup: Last parking position to identify start up routines",
    "Plug Connection State": "Indicates the state of plug connection",
    "Priority": "The Priority of the PncContractInfoType structure",
    "RBC.backendSettings.[*].nSoc": "State of Charge",
    "RBC.chargeOffsetTimestamps.[*].offset": "Offset",
    "RBC.chargerActions.[*].actionId": "Action id",
    "RBC.chargerActions.[*].actionState": "Processing state of the action",
    "RBC.chargerActions.[*].actionType": "Type of action",
    "RBC.chargerActions.[*].errorCode": "Error Code",
    "RBC.chargerActions.[*].fetchTime": "Time when the action was fetched",
    "RBC.chargerActions.[*].notificationChannelData.address": "Placeholder for RBC",
    "RBC.chargerActions.[*].notificationChannelData.channel": "Channel Info",
    "RBC.chargerSettings.[*].chargeModeSelection.availableModes": "Charging modes available",
    "RBC.chargerSettings.[*].chargeModeSelection.modificationReason": "Reason for change in charge mode",
    "RBC.chargerSettings.[*].chargeModeSelection.modificationReasonTimestamp": "Timestamp when reason was set",
    "RBC.chargerSettings.[*].chargeModeSelection.value": "Charging Mode setting",
    "RBC.chargerSettings.[*].chargeModeSelection.valueTimestamp": "Timestamp when charging mode was set",
    "RBC.chargerSettings.[*].globalAutoUnlockAC": "globalAutoUnlockACType (enum string) It Specifies the state of the global auto unlock AC at the end of charging. Can be one of the following are invalid, off, once, permanent, unavailable, unsupported",
    "RBC.chargerSettings.[*].globalItPeRange": "Boolean flag",
    "RBC.chargerSettings.[*].globalTargetSOC": "Global target state of charge",
    "RBC.chargerSettings.[*].globalTargetSOCModification": "Reason for global target modification",
    "RBC.chargerSettings.[*].maxChargeCurrentAmpere": "Maximum allowed amperage",
    "RBC.chargerSettings.[*].maxChargeCurrentTimestamp": "Timestamp when max amperage was set",
    "RBC.chargerSettings.[*].navTargetSoc": "Target State of Charge",
    "RBC.chargerSettings.[*].plugAutoUnlockSettings.allowACOnce": "allowAConce flag",
    "RBC.chargerSettings.[*].plugAutoUnlockSettings.allowACPermanent": "Automatically unlocks the charging plug after a AC charging process. This setting is permanent",
    "RBC.chargerSettings.[*].plugAutoUnlockSettings.allowDCOnce": "allowDConce flag",
    "RBC.chargerSettings.[*].plugAutoUnlockSettings.allowDCPermanent": "Automatically unlocks the charging plug after a CC charging process. This setting is permanent, i.e., it is not reset after a KL15 off.",
    "RBC.chargerSettings.[*].plugAutoUnlockSettingsTimestamp": "Timestamp when bitmap was set",
    "RBC.chargerSettings.[*].wirelessChargingSettings.systemState.modificationReason": "Wireless Charging error reason",
    "RBC.chargerSettings.[*].wirelessChargingSettings.systemState.modificationReasonTimestamp": "Timestamp when reason was set",
    "RBC.chargerSettings.[*].wirelessChargingSettings.systemState.value": "Wireless charging activation flag",
    "RBC.chargerSettings.[*].wirelessChargingSettings.systemState.valueTimestamp": "Timestamp when the flag was (de)activated.",
    "RBC.chargerSettings.[*].wirelessChargingSettings.temporaryDeactivationSetting.modificationReason": "Reason for temp deactivation of wireless charging",
    "RBC.chargerSettings.[*].wirelessChargingSettings.temporaryDeactivationSetting.modificationReasonTimestamp": "Timestamp when reason was set",
    "RBC.chargerSettings.[*].wirelessChargingSettings.temporaryDeactivationSetting.value": "Wireless charging temp deactivation",
    "RBC.chargerSettings.[*].wirelessChargingSettings.temporaryDeactivationSetting.valueTimestamp": "Timestamp when wireless charging temp deactivation was set",
    "RBC.notificationSettings.[*].plugReminderNotificationSettings.notificationsEnabled": "Flag",
    "RBC.notificationSettings.[*].plugReminderNotificationSettings.thresholdSoc": "Threshold state of charge",
    "RBC.plugReminders.[*].notificationTimeout": "This parameter is associated with sending plug reminder notification. This is calculated on the basis of SOC Timestamp Clamp 15 timeout Logic Notification timeout = SOC Timestamp + Clamp15 Timeout (In Milliseconds)",
    "RBC.vehicleStates.[*].chargedCycle": "Boolean Flag",
    "RBC.vehicleStates.[*].charging": "Flag",
    "RBC.vehicleStates.[*].chargingTimestamp": "Timestamp when flag was set",
    "RBC.vehicleStates.[*].pluginNotificationStatus": "Flag for plugin notification status",
    "RBC.vehicleStates.[*].soc": "State of charge",
    "RBC.vehicleStates.[*].socTimestamp": "Timestamp when soc was set",
    "RDT.timerActions.[*].actionId": "Action id",
    "RDT.timerActions.[*].actionState": "Processing state of the action",
    "RDT.timerActions.[*].actionType": "Type of action",
    "RDT.timerActions.[*].errorCode": "Error Code",
    "RDT.timerActions.[*].fetchTime": "Time when the action was fetched",
    "RDT.timerActions.[*].notificationChannelData.address": "address Info",
    "RDT.timerActions.[*].notificationChannelData.channel": "Channel Info",
    "RDT.timerBackendSettings.[*].connectPlugTimerNotifyMinutes": "Time span until departure to notify if vehicle is connected",
    "RDT.timerBackendSettings.[*].departReminderTimerNotifyMinutes": "Time span for notification of scheduled departure time",
    "RDT.timerBasicSettings.[*].chargeMinLimit": "Minimum charge level",
    "RDT.timerBasicSettings.[*].chargeMinLimitTimestamp": "Timestamp when charge_min_limit was set",
    "RDT.timerBasicSettings.[*].climatisationWithoutHVPower": "Flag",
    "RDT.timerBasicSettings.[*].targetTemperature": "Target temperature",
    "RDT.timerBasicSettings.[*].targetTemperatureTimestamp": "Timestamp when target_temperature was set",
    "RDT.timerElements.[*].profileID": "Id of the associated profile",
    "RDT.timerElements.[*].profileIDTimestamp": "Timestamp when profile was created",
    "RDT.timerElements.[*].timedCheckTime": "Timestamp when next check of backend settings should take place",
    "RDT.timerElements.[*].timerFequency": "Frequency of the timer",
    "RDT.timerElements.[*].timerFequencyTimestamp": "Timestamp when frequency was set",
    "RDT.timerElements.[*].timerID": "Timer Id",
    "RDT.timerElements.[*].timerProgrammedStatus": "Status of programmed timers",
    "RDT.timerProfileElements.[*].chargeMaxCurrent": "Max allowed amperage of the power source to be drawn",
    "RDT.timerProfileElements.[*].chargeMaxCurrentTimestamp": "Timestamp when charge_max_current was set",
    "RDT.timerProfileElements.[*].nightRateActive": "Flag whether night time power should be used",
    "RDT.timerProfileElements.[*].profileID": "Profile Id",
    "RDT.timerProfileElements.[*].profileName": "Name of profile",
    "RDT.timerProfileElements.[*].profileNameTimestamp": "Timestamp when profile_name was set",
    "RDT.timerProfileElements.[*].targetChargeLevel": "Target level of charge",
    "RDT.timerProfileElements.[*].targetChargeLevelTimestamp": "Timestamp when target_charge_level was set",
    "RDT.timerStatusData.[*].instrumentClusterTime": "Current time displayed in the vehicle",
    "RDT.timerStatusData.[*].instrumentClusterTimeTimestamp": "Timestamp when instrument_cluster_time was set",
    "RDT.timerStatusData.[*].timerChargeScheduleStatus": "Scheduling of charge states",
    "RDT.timerStatusData.[*].timerChargeScheduleStatusTimestamp": "Timestamp when charge_schedule_stat was set",
    "RDT.timerStatusData.[*].timerClimateScheduleStatus": "Scheduling of air conditioning levels",
    "RDT.timerStatusData.[*].timerClimateScheduleStatusTimestamp": "Timestamp when climate_schedule_stat was set",
    "RDT.timerStatusData.[*].timerExpiredStatus": "Expired means a timer has started its actions",
    "RDT.timerStatusData.[*].timerExpiredStatusTimestamp": "Timestamp when expired_stat was set",
    "RDT.timerStatusData.[*].timerID": "Timer Id",
    "RHF.rhfJobs.[*].status": "Status des Jobs. • PENDING: Empfang wurde vom Fahrzeug noch nicht bestätigt. • ACK: Der Empfang wurde vom Fahrzeug bestätigt. • ERROR: Es ist ein Fehler aufgetreten.\" is: \"Status of the job. • PENDING: The receipt has not yet been confirmed by the vehicle. • ACK: The receipt has been confirmed by the vehicle. • ERROR: An error has occurred.",
    "RHF.rhfJobs.businessEntityRef": "BusinessEntityRef",
    "RHF.rhfJobs.definitionListId": "Reference to the corresponding Honk&amp;Flash request.",
    "RHF.rhfJobs.obdJobId": "Job ID assigned by the Outbound Dispatcher. Note: When the job is initially created in the database, the Job ID is NULL and is only populated after being sent via the OBD (Outbound Dispatcher)",
    "RHF.rhfJobs.service": "Name of the service",
    "RHF.rhfRequestHistories.[*].requestId": "RhfRequestHistorie's requestId",
    "RHF.rhfRequestHistories.[*].requestStatusCode": "Request status code",
    "RHF.rhfRequestHistories.[*].requestStatusReason": "Request status code reason",
    "RHF.rhfRequestHistories.[*].requestTimestamp": "Request date",
    "RHF.rhfRequestHistories.[*].serviceOperationCodes": "RhfRequestHistories serviceOperationCodes",
    "RHF.rhfRequestHistories.brand": "brand",
    "RHF.rhfRequestHistories.requestStatus.statusCode": "Request status code",
    "RHF.rhfRequestHistories.requestStatus.statusReason": "Request status reason",
    "RHF.rhfRequests.[*].channel": "Request transmitted channel",
    "RHF.rhfRequests.[*].status.statusCode": "Request status code",
    "RHF.rhfRequests.appVersion": "App Version",
    "RHF.rhfRequests.brand": "brand",
    "RHF.rhfRequests.rhfJob.businessEntityRef": "BusinessEntityRef",
    "RHF.rhfRequests.rhfJob.definitionListId": "Reference to the corresponding Honk&amp;Flash request.",
    "RHF.rhfRequests.rhfJob.service": "Name of the service",
    "RHF.rhfRequests.rhfJob.status": "Status of the job",
    "RHF.rhfRequests.serviceDuration": "Requeest serviceDuration",
    "RHF.rhfRequests.status.statusReason": "Request status code reason",
    "RHF.rhfRequests.type": "Type of the request",
    "RLU.rluActions.[*].channel": "Channel used to call the lock and unlock operations.",
    "RLU.rluActions.[*].errorCode": "Error code from OS",
    "RLU.rluActions.[*].expirationTime": "Expiration time of the data record",
    "RLU.rluActions.[*].lockStatus": "Locking status of the vehicle after job execution, delivered by the vehicle",
    "RLU.rluActions.[*].pkId": "Primary Key",
    "RLU.rluActions.[*].rluResult": "Result value of the vehicle after job execution (see interface concept)",
    "RLU.rluActions.[*].sessionId": "Ensures that exactly one result is recorded for an executed action (session).",
    "RLU.rluSessions.[*].challenge": "Challenge of the backend",
    "RLU.rluSessions.[*].channel": "Channel through which the operation was called (values according to the interface concept)",
    "RLU.rluSessions.[*].expirationTime": "Expiration time of the RLU session",
    "RLU.rluSessions.[*].opStatus": "Indication that the operation is active, terminated, or timed out.",
    "RLU.rluSessions.[*].pkId": "Primary Key",
    "RLU.rluSessions.[*].relatedJobId": "JobId from the OBD, to identify the current session data in the asynchronous calls from the vehicle,",
    "RLU.rluSessions.[*].traceId": "TraceId generated in the OS for traceability of the workflow within the OS.",
    "RPC.climaterActions.[*].actionId": "Action id",
    "RPC.climaterActions.[*].actionState": "Processing state of the action",
    "RPC.climaterActions.[*].actionType": "Type of action",
    "RPC.climaterActions.[*].errorCode": "Error Code",
    "RPC.climaterActions.[*].fetchTime": "Time when the action was fetched",
    "RPC.climaterActions.[*].notificationChannelData.address": "Placeholder for RPC.climaterActions.noti ficationChannelData.add ress",
    "RPC.climaterActions.[*].notificationChannelData.channel": "Channel Info",
    "RPC.climaterSettings.[*].climatisationWithoutHVPower": "Flag whether regulating the temperature is allowed without a power source.",
    "RPC.climaterSettings.[*].targetTemperature": "Target temperature in vehicle",
    "RPC.climaterSettings.[*].targetTemperatureMeasurementState": "Target temperature MS provides Measurement State in String format with Enum Values as mentioned below:\\ntargetTemperatu re: type: integer format: int32 targetTemperatureMeasu rementState: type: string Enum: - INVALID -UNSUPPORTED - VALID",
    "RPC.climaterSettings.[*].targetTemperatureTimestamp": "Timestamp when target_temp was set",
    "RPT.backendSettings.[*].connectPlugTimerNotify": "Time span until departure to notify if vehicle is connected",
    "RPT.profileLists.[*].carCapturedUTCTimestamp": "Car captured UTC timestamp",
    "RPT.profileLists.[*].estimatedFinishTimeDate": "Estimated finish time of charging",
    "RPT.profileLists.[*].instrumentClusterTime": "Instrument Cluster Time",
    "RPT.profileLists.[*].privateCurrentState": "privateCurrentState of profile",
    "RPT.profileLists.[*].profileActivationState": "Profile Activation State",
    "RPT.profileLists.[*].profiles.[*].postalAddress": "postal Address of profile",
    "RPT.profileLists.[*].profiles.[*].powerLimitation.power": "powerLimitation Power Value",
    "RPT.profileLists.[*].profiles.[*].powerLimitation.startHour": "powerLimitation startHour",
    "RPT.profileLists.[*].profiles.[*].powerLimitation.startMinute": "powerLimitation startMinute",
    "RPT.profileLists.[*].profiles.[*].preferredChargingTime.startHour": "Preferred Charging StartHour",
    "RPT.profileLists.[*].profiles.[*].preferredChargingTime.startMinute": "Preferred Charging StartMinute",
    "RPT.profileLists.[*].profiles.[*].preferredChargingTimes.[*].preferredChargingTimeID": "Preferred Charging ID",
    "RPT.profileLists.[*].profiles.[*].preferredChargingTimes.[*].startHour": "Preferred Charging StartHour",
    "RPT.profileLists.[*].profiles.[*].preferredChargingTimes.[*].startMinute": "Preferred Charging StartMinute",
    "RPT.profileLists.[*].profiles.[*].profileActivation": "profileActivation State",
    "RPT.profileLists.[*].profiles.[*].profileId": "ID of the profile",
    "RPT.profileLists.[*].profiles.[*].profileName": "Name of the profile",
    "RPT.profileLists.[*].profiles.[*].profileOptions.autoPlugUnlockEnabled": "Profile Options autoPlugUnlock State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.energyCostOptimisationEnabled": "Profile Options energyCostOptimisation State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.energyMixOptimisationEnabled": "Profile Options energyMixOptimisation State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.powerLimitationEnabled": "Profile Options powerLimitation State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.preferredChargingEnabled": "Profile Options preferredCharging State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.smartChargingEnabled": "Profile Options smartCharging State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.timeBasedEnabled": "Profile Options timeBased State",
    "RPT.profileLists.[*].profiles.[*].profileOptions.usePrivateCurrentEnabled": "Profile Options PrivateCurrent State",
    "RPT.profileLists.[*].profiles.[*].profilePosition.radius": "profilePosition radius Value",
    "RPT.profileLists.[*].profiles.[*].profilePosition.radiusUnit": "profilePosition radiusUnit Value",
    "RPT.profileLists.[*].profiles.[*].profileTargetSoc": "profileTargetSoc value of profile",
    "RPT.profileLists.[*].smartChargingState": "smartChargingState of profile",
    "RPT.profileLists.[*].targetReachable": "Target reachable",
    "RPT.profileTimerActions.[*].actionId": "Action id",
    "RPT.profileTimerActions.[*].actionState": "Processing state of the action",
    "RPT.profileTimerActions.[*].errorCode": "Placeholder for RPT",
    "RPT.profileTimerActions.[*].fetchTime": "Time when the action was fetched",
    "RPT.profileTimerActions.[*].notificationChannelData.address": "Placeholder for RPT",
    "RPT.profileTimerActions.[*].notificationChannelData.channel": "Channel Info",
    "RPT.profileTimerActions.[*].type": "Type of action",
    "RPT.profiles.[*].postalAddress": "postal Address of profile",
    "RPT.profiles.[*].powerLimitation.power": "powerLimitation Power Value",
    "RPT.profiles.[*].powerLimitation.startHour": "powerLimitation startHour",
    "RPT.profiles.[*].powerLimitation.startMinute": "powerLimitation startMinute",
    "RPT.profiles.[*].preferredChargingTime.startHour": "Preferred Charging StartHour",
    "RPT.profiles.[*].preferredChargingTime.startMinute": "Preferred Charging StartMinute",
    "RPT.profiles.[*].preferredChargingTimes.[*].preferredChargingTimeID": "Preferred Charging ID",
    "RPT.profiles.[*].preferredChargingTimes.[*].startHour": "Preferred Charging StartHour",
    "RPT.profiles.[*].preferredChargingTimes.[*].startMinute": "Preferred Charging StartMinute",
    "RPT.profiles.[*].profileActivation": "profileActivation State",
    "RPT.profiles.[*].profileId": "ID of the profile",
    "RPT.profiles.[*].profileName": "Name of the profile",
    "RPT.profiles.[*].profileOptions.autoPlugUnlockEnabled": "Profile Options autoPlugUnlock State",
    "RPT.profiles.[*].profileOptions.energyCostOptimisationEnabled": "Profile Options energyCostOptimisation State",
    "RPT.profiles.[*].profileOptions.energyMixOptimisationEnabled": "Profile Options energyMixOptimisation State",
    "RPT.profiles.[*].profileOptions.powerLimitationEnabled": "Profile Options powerLimitation State",
    "RPT.profiles.[*].profileOptions.preferredChargingEnabled": "Profile Options preferredCharging State",
    "RPT.profiles.[*].profileOptions.smartChargingEnabled": "Profile Options smartCharging State",
    "RPT.profiles.[*].profileOptions.timeBasedEnabled": "Profile Options timeBased State",
    "RPT.profiles.[*].profileOptions.useExternalServiceEnabled": "Profile Options useExternalService State",
    "RPT.profiles.[*].profileOptions.usePrivateCurrentEnabled": "Profile Options PrivateCurrent State",
    "RPT.profiles.[*].profilePosition.radius": "profilePosition radius Value",
    "RPT.profiles.[*].profilePosition.radiusUnit": "profilePosition radiusUnit Value",
    "RPT.profiles.[*].profileTargetSoc": "profileTargetSoc value of profile",
    "RPT.timerLists.[*].carCapturedUTCTimestamp": "Car captured UTC timestamp",
    "RPT.timerLists.[*].instrumentClusterTime": "Instrument Cluster time",
    "RPT.timerLists.[*].timers.[*].targetSoc": "targetSoc of the timer",
    "RPT.timerLists.[*].timers.[*].timedCheckTime": "timedCheckTime of the timer",
    "RPT.timerLists.[*].timers.[*].timerActivationState": "timerActivationState of the timer",
    "RPT.timerLists.[*].timers.[*].timerChargeOption": "timerChargeOption of the timer",
    "RPT.timerLists.[*].timers.[*].timerClimaOption": "timerClimaOption of the timer",
    "RPT.timerLists.[*].timers.[*].timerId": "Timer ID",
    "RPT.timerLists.[*].timers.[*].timerSettings.date.day": "Timersettings Day Value",
    "RPT.timerLists.[*].timers.[*].timerSettings.date.year": "Timersettings Year Value",
    "RPT.timers.[*].targetSoc": "targetSoc of the timer",
    "RPT.timers.[*].timedCheckTime": "timedCheckTime of the timer",
    "RPT.timers.[*].timerActivationState": "timerActivationState of the timer",
    "RPT.timers.[*].timerChargeOption": "timerChargeOption of the timer",
    "RPT.timers.[*].timerClimaOption": "timerClimaOption of the timer",
    "RPT.timers.[*].timerId": "Timer ID",
    "RPT.timers.[*].timerSettings.date.day": "timerSettings Day Value",
    "RPT.timers.[*].timerSettings.date.year": "timerSettings Year Value",
    "RS.actionSessions.[*].actionTimestamp": "Time stamp of the session record attachment.",
    "RS.actionSessions.[*].appName": "App name of the calling smartphone app",
    "RS.actionSessions.[*].appVersion": "App version of the calling smartphone app",
    "RS.actionSessions.[*].channel": "Channel through which the operation was called (values according to the interface concept).",
    "RS.actionSessions.[*].expirationTime": "Timestamp of the expiration time of the session.",
    "RS.actionSessions.[*].id": "Primary Key.",
    "RS.actionSessions.[*].opStatus": "Internal status of processing",
    "RS.actionSessions.[*].quickstartActionSessions.[*].actionSession": "FK to the ACTION_SESSION_DATA table.",
    "RS.actionSessions.[*].quickstartActionSessions.[*].active": "true for quickStart and false for quickStop",
    "RS.actionSessions.[*].quickstartActionSessions.[*].climatisationDuration": "Heating time in minutes after the quickStart",
    "RS.actionSessions.[*].quickstartActionSessions.[*].id": "Primary Key.",
    "RS.actionSessions.[*].quickstartActionSessions.[*].startMode": "Enum: heating, ventilation",
    "RS.actionSessions.[*].quickstartActionSessions.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.actionSessions.[*].relatedJobId": "Job-Id (X-RelatedJobId) for later assignment of the incoming calls to session data record) from the OBD and vehicle.",
    "RS.actionSessions.[*].settingsActionSession.actionSession": "FK to the ACTION_SESSION_DATA table.",
    "RS.actionSessions.[*].settingsActionSession.climatisationDuration": "Heating time in minutes after the quickStart",
    "RS.actionSessions.[*].settingsActionSession.id": "Primary Key.",
    "RS.actionSessions.[*].settingsActionSession.startMode": "Start mode: heating, ventilation",
    "RS.actionSessions.[*].settingsActionSession.targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.actionSessions.[*].timerActionSessions.[*].actionSession": "FK to the ACTION_SESSION_DATA table.",
    "RS.actionSessions.[*].timerActionSessions.[*].id": "Primary Key.",
    "RS.actionSessions.[*].timerActionSessions.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.actionSessions.[*].timerActionSessions.[*].timerId": "The TimerID. Currently, there are a maximum of two timers per vehicle.",
    "RS.actionSessions.[*].timerActionSessions.[*].timerProgrammedStatus": "Programming stalls: programmed or not_programmed",
    "RS.actionSessions.[*].traceId": "The TraceId to identify the workflow (see also 3.3 Tracing of events).",
    "RS.frontendData.[*].appName": "Name of the calling smartphone app (see Interface Concept).",
    "RS.frontendData.[*].appVersion": "Version of the calling smartphone app (see Interface Concept) .",
    "RS.frontendData.[*].channel": "Channel through which the operation was called (values according to the interface concept).",
    "RS.frontendData.[*].frontendQuickstartActions.[*].active": "These parameters distinguish whether the action quickStart or quickStop is executed Y = quickStart N = quickStop",
    "RS.frontendData.[*].frontendQuickstartActions.[*].climatisationDuration": "Heating duration of the current quickStart programming.",
    "RS.frontendData.[*].frontendQuickstartActions.[*].frontendData": "FK to the FRONTEND_DATA table.",
    "RS.frontendData.[*].frontendQuickstartActions.[*].id": "Primary Key.",
    "RS.frontendData.[*].frontendQuickstartActions.[*].startMode": "Start mode: heating, ventilation",
    "RS.frontendData.[*].frontendQuickstartActions.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].frontendSettingsAction.climatisationDuration": "Heating time in minutes after the quickStart",
    "RS.frontendData.[*].frontendSettingsAction.frontendData": "FK to the FRONTEND_DATA table.",
    "RS.frontendData.[*].frontendSettingsAction.id": "Primary Key.",
    "RS.frontendData.[*].frontendSettingsAction.startMode": "Start mode: heating, ventilation",
    "RS.frontendData.[*].frontendSettingsAction.targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].frontendTimerActions.[*].frontendDataID": "FK to the FRONTEND_DATA table.",
    "RS.frontendData.[*].frontendTimerActions.[*].id": "Primary Key.",
    "RS.frontendData.[*].frontendTimerActions.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].frontendTimerActions.[*].timerId": "The TimerID. Currently, there are a maximum of two timers per vehicle.",
    "RS.frontendData.[*].frontendTimerActions.[*].timerProgrammedStatus": "Programming stalls: programmed or not_programmed",
    "RS.frontendData.[*].id": "Primary Key.",
    "RS.frontendData.[*].requestId": "ID of the frontend request.",
    "RS.frontendData.[*].rsActions.[*].actionTimestamp": "Timestamp of the action.",
    "RS.frontendData.[*].rsActions.[*].channel": "Channel through which the operation was called (values according to the interface concept).",
    "RS.frontendData.[*].rsActions.[*].expirationTime": "Expiration time of the record",
    "RS.frontendData.[*].rsActions.[*].frontendData": "FK to FRONTEND_DATA table",
    "RS.frontendData.[*].rsActions.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].mbbErrorCode": "The error code in case of failure. For example, a timeout in the MBB.",
    "RS.frontendData.[*].rsActions.[*].relatedJobId": "The job id for later identification (session). If this value is not filled, then this action was not triggered by the frontend.",
    "RS.frontendData.[*].rsActions.[*].traceId": "ID to identify the technical log output.",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].climateStatusCode": "Status code of the heater, which is supplied by the vehicle, e.g. via climatisationStateReport. climateErrorCode",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].climatisationDuration": "Air conditioning duration in minutes",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].remainingClimateTime": "Remaining heating time in minutes (remaining time)",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].rsAction": "FK to the RS_ACTION table.",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].startMode": "Start mode: heating, ventilation",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].rsActions.[*].vehicleClimaReports.[*].targetTemperatureState": "MeasurementState VALID,UNSUPPORTED,IN VALID",
    "RS.frontendData.[*].rsActions.[*].vehicleErrorReports.[*].actionContext": "Action context in which the error was reported (ACTION_CLIMA ACTION_TIMER SETTINGS_CLIMA SETTINGS_TIMER",
    "RS.frontendData.[*].rsActions.[*].vehicleErrorReports.[*].errorCode": "Error code provided by the vehicle.",
    "RS.frontendData.[*].rsActions.[*].vehicleErrorReports.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].vehicleErrorReports.[*].rsAction": "FK to the RS_ACTION table.",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].carCapturedTstmp": "from Vehicle in UTC (OSAM request) (Time of tapping the data in the vehicle)",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].climatisationDuration": "Heating time in minutes after the quickStart",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].rsAction": "FK to the RS_ACTION table.",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].startMode": "Start mode: heating, ventilation",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].rsActions.[*].vehicleSettingsReports.[*].targetTemperatureState": "MeasurementState VALID,UNSUPPORTED,IN VALID",
    "RS.frontendData.[*].rsActions.[*].vehicleTemperatureReports.[*].carCapturedTstmp": "from Vehicle in UTC (OSAM request) (Time of tapping the data in the vehicle)",
    "RS.frontendData.[*].rsActions.[*].vehicleTemperatureReports.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].vehicleTemperatureReports.[*].outdoorTemp": "Temperatures in deci-Kelvin (dK)",
    "RS.frontendData.[*].rsActions.[*].vehicleTemperatureReports.[*].outdoorTempStatus": "Enum: valid, invalid (temperature sensor defective), unsupported (temperature sensor does not exist)",
    "RS.frontendData.[*].rsActions.[*].vehicleTemperatureReports.[*].rsAction": "FK to the RS_ACTION table.",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].id": "Primary Key.",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].rsAction": "FK to the RS_ACTION table.",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].targetTemperature": "SITemperature in deci-Kelvin (dk)",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].targetTemperatureState": "MeasurementState VALID,UNSUPPORTED,IN VALID",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].timerId": "The TimerID. Currently, there are a maximum of two timers per vehicle.",
    "RS.frontendData.[*].rsActions.[*].vehicleTimerReports.[*].timerProgrammedStatus": "Programming stalls: programmed or not_programmed",
    "RS.rsSettings.[*].carCapturedTstmp": "from Vehicle in UTC (OSAM request) (Time of tapping the data in the vehicle)",
    "RS.rsSettings.[*].id.skey": "Setting key (Primary Key) e.g. 1: heaterMode e.g. 2: air conditioningDuration",
    "RS.rsSettings.[*].source": "Enum: Frontend, Vehicle",
    "RS.rsSettings.[*].svalue": "Setting value e.g.1: comfort e.g.2: 30",
    "RS.rsStatus.[*].climaCarCapturedTstmp": "Time in UTC at the time the vehicle data is retrieved, which is reported via Clima-Status-Report.",
    "RS.rsStatus.[*].climaClusterTime": "Vehicle time which is reported via Clima-Status-Report.",
    "RS.rsStatus.[*].climaClusterTimeSet": "Time of receipt of a Clima status report in the OS",
    "RS.rsStatus.[*].climaControl": "Heating in the vehicle is in the preheating state (flag)",
    "RS.rsStatus.[*].climateStatusCode": "Status code of the heater, which is supplied by the vehicle, e.g. via climatisationStateReport. climateErrorCode.",
    "RS.rsStatus.[*].climatisationDuration": "Climatization time in minutes",
    "RS.rsStatus.[*].id": "Primary Key.",
    "RS.rsStatus.[*].remainingClimateTime": "Remaining heating time in minutes (remaining time)",
    "RS.rsStatus.[*].serviceIsInitialized": "Shows if the service is initialized for a specific VIN",
    "RS.rsStatus.[*].statusTimers.[*].id": "Primary Key.",
    "RS.rsStatus.[*].statusTimers.[*].rsStatus": "FK to the RS_STATUS table.",
    "RS.rsStatus.[*].statusTimers.[*].timerId": "The TimerID. Currently, there are a maximum of two timers per vehicle.",
    "RS.rsStatus.[*].statusTimers.[*].timerProgrammedStatus": "Programming stalls: programmed or not_programmed",
    "RS.rsStatus.[*].timerCarCapturedTstmp": "Time in UTC at the time of tapping the vehicle data, which is reported via timer status report.",
    "RS.rsStatus.[*].timerClusterTime": "Vehicle time which is reported via timer status report.",
    "RS.rsStatus.[*].timerClusterTimeSet": "Time of receipt in the OS of a timer status report",
    "RS.rsStatus.[*].triggerType": "Stores the trigger information. Possible values: 1. immediately 2. invalid 3. push-button 4. timer1 5. timer2 6. timer3 7. timer4 8. unsupported",
    "RS.rsTemperatures.[*].carCapturedTstmp": "from Vehicle in UTC (OSAM request) (Time of tapping the data in the vehicle)",
    "RS.rsTemperatures.[*].currTimestamp": "Current temperatures timestamp for remote auxiliary heating.",
    "RS.rsTemperatures.[*].outdoorTemp": "Temperatures in deci-Kelvin (dK) (Conversation rule dkó Celsius, see 20141127_RemotePreTri pClima.pdf MOD_RPC_1541 )",
    "RS.rsTemperatures.[*].outdoorTempStatus": "Enum: valid, invalid, unsupported",
    "RTS.deleteHistories.[*].id": "Trip id",
    "RTS.deleteHistories.[*].parameters": "Other parameters that can be null",
    "RTS.deleteHistories.[*].source": "Origin of delete request",
    "RTS.deleteHistories.[*].timestampField": "Time of deletion",
    "RTS.deleteHistories.[*].usecase": "Reason for deletion",
    "RTS.tripData.[*].mileage": "Mileage",
    "RVT Message Data SendCount": "The number of messages sent towards the vehicle.",
    "RVT Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "RVT Message DataMessage Id": "VIL messageId for the latest message sent towards the vehicle.",
    "RVT Pending Rule ChangeMessage I": "Used to correlate if a (VIL) messageId belongs to an RVT change or not.",
    "RVT Pending Rule Order Id": "Identifier corresponding to the latest request to activate or inactivate the RVT state. This value stays the same during retries.",
    "RVT Product ActivationState Is Active": "Indicates whether product is active or not for the vehicle.",
    "RVT Product ActivationState Last Modified": "Time at which the isActive value was last modified.",
    "RVT Rule Active Until": "Epoch seconds at which the RVT activation should be inactivated.",
    "RVT Rule PendingActivation State": "Indicates if there is a pending activation change. Can be either RVT_PENDING_ACTIVE, RVT_PENDING_INACTIVE or empty (no pending changes).",
    "RVT Service ActivationState Is Active": "Indicates whether service is active or not for the vehicle.",
    "RVT Service ActivationState Last Modified": "Time at which the isActive value was last modified.",
    "RVT User ProvidedSettings Array Size": "The size of data array when being sent from the vehicle.",
    "RVT User ProvidedSettings Frequency": "The time in seconds between data points being collected in the vehicle.",
    "RVT User ProvidedSettings Precision": "The distance in meters between data points being collected in the vehicle.",
    "RVT product enabled": "True if the RVT product is enabled.",
    "RVT.rvtDefinitionLists.[*].currentResyncTries": "Number of resync attempts",
    "RVT.rvtDefinitionLists.[*].definitions.sendTrackingStatus": "Tracking Status",
    "RVT.rvtDefinitionLists.[*].definitions.trackingMode": "The tracking mode period to retrieved position information",
    "RVT.rvtDefinitionLists.[*].definitions.triggerMode": "The trigger mode to trigger tracking request",
    "RVT.rvtDefinitionLists.[*].definitions.triggerTime": "The time that leads to a Position notification from the vehicle",
    "RVT.rvtDefinitionLists.[*].requestType": "The type of job.",
    "RVT.rvtDefinitionLists.[*].trackingMode": "The tracking mode.",
    "Remote Speed Alertproduct enabled": "True if the Remote Speed Alert product is enabled.",
    "ResponseCode": "The ResponseCode of the PncCreateVehicleCertJob Type structure",
    "Result": "The Result of the ContractJobNotification structure",
    "RootCertContainerId": "The RootCertContainerId of the PncCreateVehicleCertRe qType structure",
    "SCR - Number of enginestarts": "Number of engine starts for the vehicle &lt;=13 number of restarts, ==14 driveability, ==15 no_driveability",
    "SMSFallbackByProfile": "supportsSMSFallbackByP rofile",
    "SPEEDA.alertSchedules.[*].startTime": "Date from which the schedule is active",
    "SPEEDA.definitionLists.[*].callerInformation.channel": "The channel used to send the Definition list was used",
    "SPEEDA.definitionLists.[*].callerInformation.traceId": "traceId",
    "SPEEDA.definitionLists.[*].definitions.[*].definitionIndex": "Definition Index in the vehicle",
    "SPEEDA.definitionLists.[*].definitions.[*].definitionListId": "Reference to the corresponding List",
    "SPEEDA.definitionLists.[*].definitions.[*].definitionName": "The name of the definition",
    "SPEEDA.definitionLists.[*].definitions.[*].listIndex": "Index for sorting the Definitions within the definition list",
    "SPEEDA.definitionLists.[*].requestType": "Type of list that sent to vehicle",
    "SPEEDA.requestHistoryEntries.[*].definitionListId": "Reference to the corresponding DefinitionList.",
    "SPEEDA.requestHistoryEntries.[*].requestResult": "Result of the request",
    "SPEEDA.requestHistoryEntries.[*].requestTimestamp": "The time stamp of the Updating the entry in the database.",
    "SPEEDA.requestHistoryEntries.[*].requestType": "Type of request",
    "SPEEDA.speedalertAlerts.[*].businessId": "Business ID of the signal.",
    "SPEEDA.speedalertAlerts.[*].definitionId": "Reference to definition Id.",
    "SPEEDA.speedalertAlerts.[*].definitionIndex": "Definition Index in the vehicle",
    "SPEEDA.speedalertAlerts.[*].definitionName": "The name of the definition",
    "SPEEDA.speedalertAlerts.[*].definitionSpeedLimit": "Speed limit of definition",
    "SPEEDA.speedalertAlerts.[*].occurrencePosition.trueness": "Reliability of the GPS signal.",
    "SPEEDA.speedalertJobs.[*].debouncePostTime": "Value for time advance in seconds until the vehicle sends a speed signal notification to the Business Service",
    "SPEEDA.speedalertJobs.[*].debouncePreTime": "Value for time lag in seconds until the vehicle sends a speed signal notification to the Business Service",
    "SPEEDA.speedalertJobs.[*].status": "Status of the job",
    "Service Interval TriggerType": "Specifies what triggered the stored data to be sent from the vehicle",
    "Service IntervalTimestamp": "Specifies the time at which this value was sent from the vehicle.",
    "ServiceInterval.[*].DueInDistance": "Last reported value of due distance for vehicle maintenance",
    "ServiceInterval.[*].DueInDistanceState": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "ServiceInterval.[*].DueInDistanceTimestamp": "Specifies the time at which this value was sent from the vehicle.",
    "ServiceInterval.[*].DueInDistanceUnit": "Specifies the unit of which the value was sent in.",
    "ServiceInterval.[*].DueInTimeState": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "ServiceInterval.[*].DueInTimeTimestamp": "Specifies the time at which this value was sent from the vehicle.",
    "SoftwareVersionMajor": "Main version number of the Plug &amp; Charge feature",
    "SoftwareVersionMinor": "Extended version number of the Plug &amp; Charge feature",
    "Speed Alert Index 1 AlertType": "Can be CURFEW_ALERT or SPEED_ALERT",
    "Speed Alert Index 1 Id": "Index of the alert, also known as index, i.e. the alert \"slot\" in the vehicle.",
    "Speed Alert Index 1 IsActive": "Indicates whether a particular alert is active or not, i.e. enabled or disabled.",
    "Speed Alert Index 1 IsLocation Needed": "Indicates whether the vehicle should include its location in the alert trigger message.",
    "Speed Alert Index 1 LastModified": "Time at which the alert was last modified.",
    "Speed Alert Index 1 PreDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 1Message Data Message Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Speed Alert Index 1Message Data Send Count": "The number of messages sent towards the vehicle.",
    "Speed Alert Index 1Message Data SentTimestamp": "The time at which the latest message towards the vehicle was sent.",
    "Speed Alert Index 1Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "Speed Alert Index 1Name": "User defined name of the alert.",
    "Speed Alert Index 1Order Id": "Identifier corresponding to the latest request from the user to activate or inactivate the alert. This value stays the same during retries.",
    "Speed Alert Index 1Post Debounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 1Schedule End Date": "The date at which the alert stops being active.",
    "Speed Alert Index 1Schedule End Time": "Hour and minutes at which the alert stops being active.",
    "Speed Alert Index 1Schedule Is Recurring": "True if the schedule is recurring on a set of weekdays as described by recurringOn.",
    "Speed Alert Index 1Schedule Recurring OnFridays": "True if this alert is active on Fridays.",
    "Speed Alert Index 1Schedule Recurring OnMondays": "True if this alert is active on Mondays.",
    "Speed Alert Index 1Schedule Recurring OnSaturdays": "True if this alert is active on Saturdays.",
    "Speed Alert Index 1Schedule Recurring OnSundays": "True if this alert is active on Sundays.",
    "Speed Alert Index 1Schedule Recurring OnThursdays": "True if this alert is active on Thursdays.",
    "Speed Alert Index 1Schedule Recurring OnTuesdays": "True if this alert is active on Tuesdays.",
    "Speed Alert Index 1Schedule Recurring OnWednesdays": "True if this alert is active on Wednesdays.",
    "Speed Alert Index 1Schedule Start Date": "The date at which the alert starts being active.",
    "Speed Alert Index 1Schedule Start Time": "Hour and minutes at which the alert starts being active.",
    "Speed Alert Index 1Threshold In KM Per Hour": "Threshold value for triggering the alert.",
    "Speed Alert Index 2 AlertId": "Index of the alert, also known as index, i.e. the alert \"slot\" in the vehicle.",
    "Speed Alert Index 2 AlertType": "Can be CURFEW_ALERT or SPEED_ALERT",
    "Speed Alert Index 2 IsActive": "Indicates whether a particular alert is active or not, i.e. enabled or disabled.",
    "Speed Alert Index 2 IsLocation Needed": "Indicates whether the vehicle should include its location in the alert trigger message.",
    "Speed Alert Index 2 LastModified": "Time at which the alert was last modified.",
    "Speed Alert Index 2 Name": "User defined name of the alert.",
    "Speed Alert Index 2 PostDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 2 PreDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 2Message Data Message Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Speed Alert Index 2Message Data Send Count": "The number of messages sent towards the vehicle.",
    "Speed Alert Index 2Message Data SentTimestamp": "The time at which the latest message towards the vehicle was sent.",
    "Speed Alert Index 2Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "Speed Alert Index 2Order Id": "Identifier corresponding to the latest request from the user to activate or inactivate the alert. This value stays the same during retries.",
    "Speed Alert Index 2Schedule End Date": "The date at which the alert stops being active.",
    "Speed Alert Index 2Schedule End Time": "Hour and minutes at which the alert stops being active.",
    "Speed Alert Index 2Schedule Is Recurring": "True if the schedule is recurring on a set of weekdays as described by recurringOn.",
    "Speed Alert Index 2Schedule Recurring OnMondays": "True if this alert is active on Mondays.",
    "Speed Alert Index 2Schedule Recurring OnSaturdays": "True if this alert is active on Saturdays.",
    "Speed Alert Index 2Schedule Recurring OnSundays": "True if this alert is active on Sundays.",
    "Speed Alert Index 2Schedule Recurring OnThursdays": "True if this alert is active on Thursdays.",
    "Speed Alert Index 2Schedule Recurring OnTuesdays": "True if this alert is active on Tuesdays.",
    "Speed Alert Index 2Schedule Recurring OnWednesdays": "True if this alert is active on Wednesdays.",
    "Speed Alert Index 2Schedule Start Date": "The date at which the alert starts being active.",
    "Speed Alert Index 2Schedule Start Time": "Hour and minutes at which the alert starts being active.",
    "Speed Alert Index 2Threshold In KM Per Hour": "Threshold value for triggering the alert.",
    "Speed Alert Index 3 AlertId": "Index of the alert, also known as index, i.e. the alert \"slot\" in the vehicle.",
    "Speed Alert Index 3 AlertIs Active": "Indicates whether a particular alert is active or not, i.e. enabled or disabled.",
    "Speed Alert Index 3 AlertIs Location Needed": "Indicates whether the vehicle should include its location in the alert trigger message.",
    "Speed Alert Index 3 AlertLast Modified": "Time at which the alert was last modified.",
    "Speed Alert Index 3 AlertOrder Id": "Identifier corresponding to the latest request from the user to activate or inactivate the alert. This value stays the same during retries.",
    "Speed Alert Index 3 AlertPost Debounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 3 AlertPre Debounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 3 AlertSchedule End Date": "The date at which the alert stops being active.",
    "Speed Alert Index 3 AlertSchedule End Time": "Hour and minutes at which the alert stops being active.",
    "Speed Alert Index 3 AlertSchedule Is Recurring": "True if the schedule is recurring on a set of weekdays as described by recurringOn.",
    "Speed Alert Index 3 AlertSchedule Recurring OnFridays": "True if this alert is active on Fridays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnMondays": "True if this alert is active on Mondays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnSaturdays": "True if this alert is active on Saturdays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnSundays": "True if this alert is active on Sundays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnThursdays": "True if this alert is active on Thursdays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnTuesdays": "True if this alert is active on Tuesdays.",
    "Speed Alert Index 3 AlertSchedule Recurring OnWednesdays": "True if this alert is active on Wednesdays.",
    "Speed Alert Index 3 AlertSchedule Start Date": "The date at which the alert starts being active.",
    "Speed Alert Index 3 AlertSchedule Start Time": "Hour and minutes at which the alert starts being active.",
    "Speed Alert Index 3 AlertSpeed Threshold In KMPer Hour": "Threshold value for triggering the alert.",
    "Speed Alert Index 3 Name": "User defined name of the alert.",
    "Speed Alert Index 3 Type": "Can be CURFEW_ALERT or SPEED_ALERT",
    "Speed Alert Index 3Message Data Message Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Speed Alert Index 3Message Data Send Count": "The number of messages sent towards the vehicle.",
    "Speed Alert Index 3Message Data SentTimestamp": "The time at which the latest message towards the vehicle was sent.",
    "Speed Alert Index 3Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "Speed Alert Index 4 Id": "Index of the alert, also known as index, i.e. the alert \"slot\" in the vehicle.",
    "Speed Alert Index 4 IsActive": "Indicates whether a particular alert is active or not, i.e. enabled or disabled.",
    "Speed Alert Index 4 IsLocation Needed": "Indicates whether the vehicle should include its location in the alert trigger message.",
    "Speed Alert Index 4 LastModified": "Time at which the alert was last modified.",
    "Speed Alert Index 4 Name": "User defined name of the alert.",
    "Speed Alert Index 4 OrderId": "Identifier corresponding to the latest request from the user to activate or inactivate the alert. This value stays the same during retries.",
    "Speed Alert Index 4 PostDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 4 PreDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Speed Alert Index 4 Type": "Can be CURFEW_ALERT or SPEED_ALERT",
    "Speed Alert Index 4Message Data Message Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Speed Alert Index 4Message Data Send Count": "The number of messages sent towards the vehicle.",
    "Speed Alert Index 4Message Data SentTimestamp": "The time at which the latest message towards the vehicle was sent.",
    "Speed Alert Index 4Message DataAcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "Speed Alert Index 4Schedule End Date": "The date at which the alert stops being active.",
    "Speed Alert Index 4Schedule End Time": "Hour and minutes at which the alert stops being active.",
    "Speed Alert Index 4Schedule Is Recurring": "True if the schedule is recurring on a set of weekdays as described by recurringOn.",
    "Speed Alert Index 4Schedule Recurring OnFridays": "True if this alert is active on Fridays.",
    "Speed Alert Index 4Schedule Recurring OnMondays": "True if this alert is active on Mondays.",
    "Speed Alert Index 4Schedule Recurring OnSaturdays": "True if this alert is active on Saturdays.",
    "Speed Alert Index 4Schedule Recurring OnSundays": "True if this alert is active on Sundays.",
    "Speed Alert Index 4Schedule Recurring OnThursdays": "True if this alert is active on Thursdays.",
    "Speed Alert Index 4Schedule Recurring OnTuesdays": "True if this alert is active on Tuesdays.",
    "Speed Alert Index 4Schedule Recurring OnWednesdays": "True if this alert is active on Wednesdays.",
    "Speed Alert Index 4Schedule Start Date": "The date at which the alert starts being active.",
    "Speed Alert Index 4Schedule Start Time": "Hour and minutes at which the alert starts being active.",
    "Speed Alert Index2Schedule Recurring OnFridays": "True if this alert is active on Fridays.",
    "Speed Alert Index4Threshold In KM PerHour": "Threshold value for triggering the alert.",
    "Speed Alert NotificationBreach Type": "Can be SPEED_ALERT_DEFAULT, SPEED_ALERT_INITIAL or SPEED_ALERT_CONCLUS IVE",
    "State of charge": "State of charge for vehicles with an electric battery in percentage",
    "Status": "Installation status of the provisioning certificate",
    "StatusCode": "The StatusCode of the ContractChangeRequest Odp15 structure",
    "SupportedContracts": "Number of supported Contract Chains",
    "SupportedPeRoots": "Number of supported Private Environment certificates",
    "SupportedV2GRoots": "Number of supported Vehicle to Grid certificates",
    "Timestamp": "latest message towards the vehicle was sent.",
    "Trailer Found State": "Specifies if the stored data is valid, invalid, unsupported or if it contains an error",
    "Trailer Found Timestamp": "Specifies the time at which this value was sent from the vehicle.",
    "Tyre pressure actual frontright": "Indicates if the actual front right tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "Tyre pressure actual sparetyre": "Indicates if the actual spare tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "Tyre pressure differentialrear left": "Indicates if the differential rear left tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "Tyre pressure requiredfront left": "Indicates if the required front left tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "Tyre pressure requiredrear right": "Indicates if the required rear left tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "Tyre pressure requiredspare tyre": "Indicates if the required spare tyre pressure is unsupported (0) or invalid (1) or valid (int)",
    "USM Data Trigger Type": "Specifies what triggered the stored data to be sent from the vehicle",
    "USM Data USMTimestamp Seconds &USM Data USMTimestamp Nanos": "Specifies the time at which this value was sent from the vehicle.",
    "UnknownStatus": "Status of the reporting of unknown certificates",
    "UserID": "Unique userID used for customer identification",
    "VALETA.definitionLists.[*].businessId": "Technical ID that the Represents the original ID of the definition list.",
    "VALETA.definitionLists.[*].callerInformation.channel": "The channel used to send the Definition list was used",
    "VALETA.definitionLists.[*].callerInformation.traceId": "traceId",
    "VALETA.definitionLists.[*].currentResyncTries": "Number of resync attempts",
    "VALETA.definitionLists.[*].definitions.[*].businessId": "Technical ID that is the ID of the definition",
    "VALETA.definitionLists.[*].definitions.[*].definitionIndex": "Definition Index in the vehicle",
    "VALETA.definitionLists.[*].definitions.[*].definitionListId": "Reference to the corresponding List",
    "VALETA.definitionLists.[*].definitions.[*].listIndex": "Index for sorting the Definitions within the definition list",
    "VALETA.definitionLists.[*].definitions.[*].valetalertArea.radius": "Radius 1 of the ellipse in meters.",
    "VALETA.definitionLists.[*].definitions.[*].valetalertArea.valetalertDefinitionId": "Technical ID of the definition, to which the area refers",
    "VALETA.definitionLists.[*].requestType": "Type of list that sent to vehicle",
    "VALETA.definitionLists.[*].resync": "Job resynchronization between business service and vehicle",
    "VALETA.definitionLists.[*].status": "Status of the definition list",
    "VALETA.requestHistoryEntries.[*].businessId": "Business ID of the entry",
    "VALETA.requestHistoryEntries.[*].definitionListId": "Reference to the corresponding DefinitionList.",
    "VALETA.requestHistoryEntries.[*].requestResult": "Result of the request",
    "VALETA.requestHistoryEntries.[*].requestTimestamp": "The time stamp of the Updating the entry in the database.",
    "VALETA.requestHistoryEntries.[*].requestType": "Type of request",
    "VALETA.valetalertAlerts.[*].businessId": "Business ID of the signal",
    "VALETA.valetalertAlerts.[*].definitionId": "Reference to definition Id.",
    "VALETA.valetalertAlerts.[*].definitionIndex": "Definition Index in the vehicle",
    "VALETA.valetalertAlerts.[*].definitionListId": "Reference to definition Id.",
    "VALETA.valetalertAlerts.[*].definitionName": "The name of the definition",
    "VALETA.valetalertAlerts.[*].definitionSpeedLimit": "The speed limit applicable to the definition that refers to the warning",
    "VALETA.valetalertAlerts.[*].occurrencePosition.trueness": "Reliability of the GPS signal.",
    "VALETA.valetalertJobs.[*].debouncePostTime": "Value for time lag in seconds until the vehicle sends a violation notification to the Business Service sends.",
    "VALETA.valetalertJobs.[*].debouncePreTime": "Value for time advance in seconds until the vehicle sends a violation notification to the business service.",
    "VALETA.valetalertJobs.[*].definitionListId": "The ID of the associated list",
    "VALETA.valetalertJobs.[*].service": "Service name",
    "VALETA.valetalertJobs.[*].spatialTolerance": "Value for spatial tolerance, up to the vehicle sends a valetalert violation to the business service.",
    "VALETA.valetalertJobs.[*].status": "Status of the job",
    "VHR.vehicleHealthReportConfigurationList.[*].distanceBased.distance": "DISTANCEBASED_DISTA NCE",
    "VHR.vehicleHealthReportConfigurationList.[*].distanceBased.startMileage": "DISTANCEBASED_START MILEAGE",
    "VHR.vehicleHealthReportConfigurationList.[*].maintenanceBased.distance": "MAINTENANCEBASED_DI STANCE",
    "VHR.vehicleHealthReports.[*].vehicleHealthReport.[*].mileage": "MILEAGE",
    "VSR.tssVehicleDataList.[*].carCaptured": "Time when the vehicle collected the data",
    "VSR.tssVehicleDataList.[*].carSent": "Time when the vehicle sent the data",
    "VSR.tssVehicleDataList.[*].carSentUtc": "Time when the vehicle sent the data (in utc)",
    "VSR.tssVehicleDataList.[*].dataValue": "Value of TSS data field",
    "VSR.tssVehicleDataList.[*].milCarCaptured": "Mileage of the vehicle when the value was recorded (in the vehicle)",
    "VSR.tssVehicleDataList.[*].milCarSent": "Mileage of the vehicle when the value was transmitted",
    "VSR.tssVehicleDataList.[*].unit": "Unit of TSS data field",
    "VTS.keys.[*].id": "Application Data Technical ID",
    "VTS.keys.[*].keyEncrypted": "Encypted Key",
    "VTS.keys.[*].keySequenceNumber": "Sequence Number of the key. Will be used during key exchange handshake to prevent race conditions",
    "VTS.keys.[*].maxUsage": "The maximum number sms fallback key can be used",
    "VTS.keys.[*].usageCount": "Number of time sms fallback key has been used",
    "VTS.keys.[*].validUntil": "Validity of key",
    "VTS.keys.[*].version": "Version of JPA optimistic locking",
    "VTS.retryQueueElements.[*].backendSystemType": "dentifies the type of system which has been called. Either MBB Core, Fazit or VTS Backend",
    "VTS.retryQueueElements.[*].error": "Error raised during the call",
    "VTS.retryQueueElements.[*].firstCall": "Timestamp of the first call; resp. Timestamp of creating or updating the queue entry.",
    "VTS.retryQueueElements.[*].httpMethod": "HTTP method of the call; May be empty for Fazit calls",
    "VTS.retryQueueElements.[*].id": "Foreign key",
    "VTS.retryQueueElements.[*].nextCall": "Timestamp of the next retry call. Should be initially filled with actual time + VTS Business Service Parameter RetryDuration",
    "VTS.retryQueueElements.[*].numberOfCalls": "Number of tries",
    "VTS.retryQueueElements.[*].version": "Version of JPA optimistic locking",
    "VTS.retryQueueElements.[*].vtsSystemState": "Optional; Contains the VTSSystemState of the call, if the call was a vtsState call, not being a command response",
    "VTS.vehicleCryptoElements.[*].communicationScheme": "GLCS defines two communication schemes -Request/Response and Job. (See [GLCSJOB], [GLCSIFP], [GLCSIFP] and [GLCSIFP].",
    "VTS.vehicleCryptoElements.[*].encryptionAlgorithm": "16-bit value identifying the used encryption algorithm. Values are defined by IKEv2 Encryption Algorithm IDs",
    "VTS.vehicleCryptoElements.[*].keyExchangeProtocol": "Its the algorithm used to exchange keys. It can any of the following values plain, DH (Diffie Hellmann), MTI/A0,ECDH (Elliptic curve Diffie Hellman), ECDHC (lliptic Curve Diffie Hellmann with cofact or multiplication).",
    "VTS.vehicleCryptoElements.[*].signatureAlgorithm": "Its the used signature method. XML signature syntax and processing, W3C Recommendation",
    "VTS.vehicleCryptoElements.[*].version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].commands.[*].changedAt": "Command changed or update time",
    "VTS.vehicles.[*].commands.[*].commandSpecialModes.[*].command.id": "Foreign key",
    "VTS.vehicles.[*].commands.[*].commandSpecialModes.[*].duration": "Durations (in minutes)",
    "VTS.vehicles.[*].commands.[*].commandSpecialModes.[*].lastActivity": "Last activity performed",
    "VTS.vehicles.[*].commands.[*].commandSpecialModes.[*].specialMode": "Special Mode e.g -garage, transport etc",
    "VTS.vehicles.[*].commands.[*].commandSpecialModes.[*].version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].commands.[*].crankInhibition": "If set true, crank inhibition is supported.",
    "VTS.vehicles.[*].commands.[*].id": "Application Data Technical ID",
    "VTS.vehicles.[*].commands.[*].newVehicleState": "Enum -SUSPEND,FIM,ATTENDA NCE,ALARM,THEFT",
    "VTS.vehicles.[*].commands.[*].notificationChannel": "FNS Channel to use for notifications",
    "VTS.vehicles.[*].commands.[*].originalCaller": "Enum -FRONTEND,VEHICLE,MB B,BE",
    "VTS.vehicles.[*].commands.[*].requestStatus": "The requested command state",
    "VTS.vehicles.[*].commands.[*].shortMessage.command.id": "Short Messages PKID",
    "VTS.vehicles.[*].commands.[*].shortMessage.id": "Application Data Technical ID",
    "VTS.vehicles.[*].commands.[*].shortMessage.messageNumber": "Must be present, if the request is a response to a short message after upgrading to IP communication",
    "VTS.vehicles.[*].commands.[*].shortMessage.timeOut": "Time out of short message",
    "VTS.vehicles.[*].commands.[*].shortMessage.version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].commands.[*].theftMark": "Theft marked",
    "VTS.vehicles.[*].commands.[*].vtsCommand": "VTS business service generated command id",
    "VTS.vehicles.[*].commands.[*].vtsError": "Error raised during the call",
    "VTS.vehicles.[*].commands.[*].vtsStatus": "The requested command state",
    "VTS.vehicles.[*].communicationStatus.[*].calledDirection": "From which side service is called",
    "VTS.vehicles.[*].communicationStatus.[*].commandType": "Type of command",
    "VTS.vehicles.[*].communicationStatus.[*].method": "HTTP method of the call",
    "VTS.vehicles.[*].communicationStatus.[*].obdCallbackStatus": "Outbound Dispatcher state e.g Delivered,expired,confir med",
    "VTS.vehicles.[*].communicationStatus.[*].response": "Response",
    "VTS.vehicles.[*].communicationStatus.[*].version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].crankInhibitionState": "Reports the state of crankInhibition. Either activated, off, activation or deactivating",
    "VTS.vehicles.[*].deviceInfo.ecuGeneration": "This value can be used to distinguish between different device generations. If the vehicle reports a structured combined device installed base, the business service will fill in the ECUGeneration of the sub-device reporting to support VTS23.",
    "VTS.vehicles.[*].deviceInfo.euiccid": "A unique global identifier for eUICC SIMs",
    "VTS.vehicles.[*].deviceInfo.hwVersion": "Hardware-Version; max length 255 characters",
    "VTS.vehicles.[*].deviceInfo.imei": "IMEI of the Modem",
    "VTS.vehicles.[*].deviceInfo.imsi": "International Mobile Subscriber Identity of the (embedded) SIM to identify the vehicle",
    "VTS.vehicles.[*].deviceInfo.productionDate": "Date of device production",
    "VTS.vehicles.[*].deviceInfo.supportsBackupBattery": "supports Backup Battery (Y/N)",
    "VTS.vehicles.[*].deviceInfo.supportsDWA": "supports DWA (Y/N)",
    "VTS.vehicles.[*].deviceInfo.supportsDoorLock": "supports Door Lock (Y/N)",
    "VTS.vehicles.[*].deviceInfo.supportsShockSensor": "True if device is equipped with a shock sensor and VTS function can determine and report the state of it",
    "VTS.vehicles.[*].deviceInfo.supportsSpeedDegradation": "True if VTS function can control a speed degradation feature of the vehicle",
    "VTS.vehicles.[*].deviceInfo.swVersion": "Software-Version of the device",
    "VTS.vehicles.[*].deviceInfo.version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].deviceInfo.vtsVersion": "Software-Version of the VTS function",
    "VTS.vehicles.[*].engineLockTimeToWait": "Optional attribute how long to wait in seconds with inhibition when ignition has turned off. Only suitable for enabling crank inhibition. If not set, value from VTS Profile will be used",
    "VTS.vehicles.[*].id": "Application Data Technical ID",
    "VTS.vehicles.[*].isMarkedForDelete": "True if marked for delete",
    "VTS.vehicles.[*].isStateForced": "JobId related to force attendance",
    "VTS.vehicles.[*].keyExchangeJobId": "Whenever sending a status was triggered from the backend jobID is set for all request from device regarding this job",
    "VTS.vehicles.[*].keyExchangeJobStartAt": "Timstamp when key exchnage job started",
    "VTS.vehicles.[*].licenseDuration": "Duration of license as ISO 8601 duration",
    "VTS.vehicles.[*].licenseExpiration": "Timestamp when the license will expire. Usually this is this defined by the actual active license. Near expiration date of the actual active license there might already exist a prolongation license, defining the expirationDate",
    "VTS.vehicles.[*].licenseProducttype": "License Product type. e. g VTSduringTheft,VTSBasic, VTSplus",
    "VTS.vehicles.[*].licenseStartDate": "Timestamp of the activation of the actual cumulated license. Not necessarily defined by the actual license",
    "VTS.vehicles.[*].licenseStatus": "Status of license",
    "VTS.vehicles.[*].licenseWarned": "True, if a warning notice about license notification has been sent.",
    "VTS.vehicles.[*].lowBatteryEventTimestamp": "Timestamp whe low battery event is received",
    "VTS.vehicles.[*].messageNumber": "Must be present, if the request is a response to a short message after upgrading to IP communication",
    "VTS.vehicles.[*].offlineWithSMSConnection": "True for offline With SMS Connection",
    "VTS.vehicles.[*].registrationCountry": "Country where the vehicle is registered / will be registered",
    "VTS.vehicles.[*].sendDiagnosisJobId": "JobId relate to send diagnosis",
    "VTS.vehicles.[*].shortMessages.[*].messageNumber": "Must be present, if the request is a response to a short message after upgrading to IP communication",
    "VTS.vehicles.[*].shortMessages.[*].timeOut": "Time out of short message",
    "VTS.vehicles.[*].shortMessages.[*].version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].state": "State of vehicle -SUSPEND,ALARM,ATTEN DANCE",
    "VTS.vehicles.[*].supportsSMSFallbackByInstallBase": "True if supportsSMSFallbackByI nstallBase",
    "VTS.vehicles.[*].supportsSMSFallbackByProfile": "True if supportsSMSFallbackByP rofile",
    "VTS.vehicles.[*].tsLeavingAttendance": "Contains a timestamp (UTC), when VTSSystemState Attendance was left",
    "VTS.vehicles.[*].tsStartedAttendance": "Contains a timestamp (UTC), when VTSSystemStae Attendance was reached",
    "VTS.vehicles.[*].usesSMSFallback": "True if uses SMS Fallback",
    "VTS.vehicles.[*].vehicleSpecialModes.[*].expiration": "Expiration time for special mode",
    "VTS.vehicles.[*].vehicleSpecialModes.[*].isSpecialModeExpiryNotified": "Ture if Special Mode Expiry Notified",
    "VTS.vehicles.[*].vehicleSpecialModes.[*].specialMode": "Special Mode e.g -garage, transport etc",
    "VTS.vehicles.[*].vehicleSpecialModes.[*].version": "Version of JPA optimistic locking",
    "VTS.vehicles.[*].version": "Version of JPA optimistic locking",
    "Valet Alert Id": "Index of the alert, also known as index, i.e. the alert \"slot\" in the vehicle.",
    "Valet Alert Is Active": "Indicates whether a particular alert is active or not, i.e. enabled or disabled.",
    "Valet Alert Is LocationNeeded": "Indicates whether the vehicle should include its location in the alert trigger message.",
    "Valet Alert Last Modified": "Time at which the alert was last modified.",
    "Valet Alert Message DataMessage Id": "VIL messageId for the latest message sent towards the vehicle.",
    "Valet Alert MessageData AcknowledgedTimestamp": "The time at which LSS received an acknowledgement from the vehicle messageId.",
    "Valet Alert MessageData Send Count": "The number of messages sent towards the vehicle.",
    "Valet Alert MessageData Sent Timestamp": "The time at which the latest message towards the vehicle was sent.",
    "Valet Alert Name": "User defined name of the alert.",
    "Valet Alert Order Id": "Identifier corresponding to the latest request from the user to activate or inactivate the alert. This value stays the same during retries.",
    "Valet Alert PostDebounce Time": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Valet Alert Pre DebounceTime": "Prevents the vehicle to send frequent messages when travelling around the threshold value.",
    "Valet Alert Schedule EndDate": "The date at which the alert stops being active.",
    "Valet Alert Schedule EndTime": "Hour and minutes at which the alert stops being active.",
    "Valet Alert Schedule IsRecurring": "True if the schedule is recurring on a set of weekdays as described by recurringOn.",
    "Valet Alert Schedule StartDate": "The date at which the alert starts being active.",
    "Valet Alert Schedule StartTime": "Hour and minutes at which the alert starts being active.",
    "Valet Alert ScheduleRecurring On Fridays": "True if this alert is active on Fridays.",
    "Valet Alert ScheduleRecurring On Mondays": "True if this alert is active on Mondays.",
    "Valet Alert ScheduleRecurring On Saturdays": "True if this alert is active on Saturdays.",
    "Valet Alert ScheduleRecurring On Sundays": "True if this alert is active on Sundays.",
    "Valet Alert ScheduleRecurring On Thursdays": "True if this alert is active on Thursdays.",
    "Valet Alert ScheduleRecurring On Tuesdays": "True if this alert is active on Tuesdays.",
    "Valet Alert ScheduleRecurring OnWednesdays": "True if this alert is active on Wednesdays.",
    "Valet Alert SpeedThreshold In KM Per Hour": "Threshold value for triggering the alert.",
    "Valet Alert Type": "Can be CURFEW_ALERT or SPEED_ALERT",
    "Valet Alert productenabled": "True if the Valet Alert product is enabled.",
    "ValidityEnd": "Validity enddate of the provisioning certificate",
    "ValidityStart": "Validity startdate of the provisioning certificate",
    "Vehicle Data TimestampSeconds & Vehicle DataTimestamp Nanos": "Specifies the time at which this value was sent from the vehicle.",
    "Vehicle Data Trigger Type": "Specifies what triggered the stored data to be sent from the vehicle",
    "Vehicle Data Trigger TypeTimestamp": "Specifies the time at which this value was sent from the vehicle.",
    "VehicleCertContainer": "Container holding provisioning certificate to install on the vehicle",
    "VehicleCertSigningReq": "The VehicleCertSigningReq of the PncCreateVehicleCertRe qType structure",
    "VehicleCsrMoseContainer": "The VehicleCsrMoseContaine r of the PncCreateVehicleCertRe qType structure",
    "WarningLights.[*].WarningLight.[*].category": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].customerRelevance": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].fieldActionCode": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].fieldActionCriteria": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].icon": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].iconColor": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].iconName": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].messageId": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].notificationId": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].oruRelevant": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].oruStatus": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].priority": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].serviceLead": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].subCategory": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].text": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].timeOfOccurrence": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].WarningLight.[*].type": "UC MOD4 data export: Warning Lights status",
    "WarningLights.[*].mileageKm": "UC MOD4 data export: Warning Lights status",
    "Wischer1_Wischgeschwindigkeit": "Current wiper velocity measured by sensors. physical value range: [10; 70] raw value range: [1; 61] scale: 1 offset: 9 Init value (raw): 62 Error value (raw): 63",
    "[*]_alarmSource": "Alarm category from where the alarm was triggered",
    "[*]_sourceIndex": "A numeric identifier that identifies the source of the alarm",
    "].id.fieldId": "value (supplied by the TSS)",
    "].otvCalls.[*].appointment.notifyCustomer": "notified by email (via system WEGA) about this date (values: 1 = true, 0 = false)",
    "].otvCalls.[*].timestampCarSent": "the vehicle record was sent by the vehicle in UTC.max. Accuracy: msec",
    "].postHistoryFlag": "notified regularly if a request history is available. (Values: 1 = true, 0 = false)",
    "acVoltageL1": "Phase 1 voltage",
    "acVoltageL2": "Phase 2 voltage",
    "acVoltageL3": "Phase 3 voltage",
    "activeDomains": "Last car readiness had active domains set to true of false",
    "actual_soc": "Actual state of charge",
    "air-filter.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "air-filter.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "air-filter.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "air_spring_front_left E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "air_spring_front_right E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "air_spring_rear_left E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "air_spring_rear_right E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "alternativeAuxiliaryPowerInKwh": "The consumption for all residual (non-comfort-related) additional consumers. For example the consumption when customer turns on the air condition.",
    "atus.value": "trunk lid of the vehicle with subcategory trunk lid status with subcategory value",
    "autoUnlockPlugWhenCharged": "The value indicating if the charge plug is to be automatically unlocked (or not) once the charging is completed.",
    "auxiliaryPowerInKwh": "The consumption for all comfort-related additional consumers. For example the consumption when customer turns on the air condition.",
    "backendError.backendCapturedTimestamp": "The UTC timestamp specifying when the error occurred in the Device Platform.",
    "backendError.errorDescription": "The description of the error code in the Device Platform.",
    "backendError.errorNumber": "The number that represents an error code in the Device Platform.",
    "batteryClimatizationConsumption": "normalized value of energy consumption for battery climtization for standard mode (non-comfort-related)",
    "batteryStatus.chargeEnergy_kWh": "The Energy in kWh, that the battery can deliver based on the current SOC. The range is between 0 and 1000kWh.",
    "batteryStatus.cruisingRange.engineType": "The type of engine / power / fuel, based on energy source.",
    "batteryStatus.cruisingRange.range": "The range of the corresponding engine.",
    "batteryStatus.cruisingRange.unitBeforeConversion": "The cruising range unit reported by the vehicle.",
    "batteryStatus.currentSOC_pct": "The current SOC of HV-Battery between 0 and 100% SOC with a resolution of 1%.",
    "batteryStatus.hvBatteryTemperature.temperatureUnit": "Unit of hv battery temperature.",
    "batteryStatus.hvBatteryTemperature.temperatureValue": "Current temperature value of hv battery.",
    "batteryStatus.routeBasedRange.range": "The route based range of the corresponding engine when the route guidance active.",
    "batteryStatus.routeBasedRange.unitBeforeConversion": "The route based range unit reported by the vehicle.",
    "battery_aging": "Battery aging indicator",
    "battery_energy": "Amount of energy charged in battery",
    "battery_temperature": "The temperature of the battery",
    "batterystatus.carCapturedTimestamp": "Car Captured Timestamp",
    "batterystatus.cruisingRangeElectricInKm": "Estimated cruising range in kilometer",
    "batterystatus.currentSOCInPct": "State of charge of the HV-battery",
    "batterytemperaturedata.carCapturedTimestamp": "Car Captured Timestamp",
    "batterytemperaturedata.temperatureHvBatteryMax": "Maximum of the HV-battery temperature",
    "batterytemperaturedata.temperatureHvBatteryMin": "Minimum of the HV-battery temperature",
    "bidirectionalChargingMode.bidiMaxSoc": "The value defines the upper threshold to which charging of the vehicle battery with energy from the Home Energy Management System (HEMS) will take place.",
    "bidirectionalChargingMode.bidiMinSoc": "The value defines the lower threshold to which discharging of the vehicle battery will take place. The energy from the vehicles battery is provided to the Home Energy Management System (HEMS) until bidi_minSOC has been reached.",
    "bidirectionalChargingMode.chargingMode": "The mode of an ongoing charging process.",
    "bidirectionalChargingMode.modeInformation": "Information about the bidirectional charging mode.",
    "bitionState": "crankInhibition. Either activated, off, activation or deactivating",
    "brake-fluid-age.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "brake-fluid-age.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "brake-fluid-age.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "brake-pad-change.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "brake-pad-change.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "brake-pad-change.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "budgetStartBatteryLevel": "Pre ID.S3 available budget at start of 24hr period",
    "budgetStartTime": "Pre ID.S3 start time for 24hr measurement period",
    "calc_source": "Source of calculation",
    "calibrationStatus.calibrationActivation": "Possible switch values off, permanent, once.",
    "calibrationStatus.calibrationFailure": "Notification: if calibration failed.",
    "calibrationStatus.calibrationNeedDetected": "Notification: First hint for battery calibration.",
    "calibrationStatus.calibrationRequests.calibrationRequestEscalationOne": "calibrationRequestEscala tionOne value for calibrationRequests.",
    "calibrationStatus.calibrationRequests.calibrationRequestEscalationTwo": "calibrationRequestEscala tionTwo value for calibrationRequests.",
    "calibrationStatus.calibrationRequests.calibrationRequestInitial": "calibrationRequestInitial value for calibrationRequests.",
    "calibrationStatus.calibrationState": "Describes the current state of battery calibration.",
    "car.brd": "Brand name of the car",
    "car.mod": "Model code of the car (not real name)",
    "car.vin": "Unique identifier of the car",
    "car.yer": "Year the car was manufactured",
    "carCapturedTimeStamp.nanos": "The nano seconds of the UTC timestamp specifying the time at which the vehicle sent this report.",
    "carCapturedTimeStamp.seconds": "The seconds of the UTC timestamp specifying the time at which the vehicle sent this report.",
    "carCapturedTimestamp": "as UTC",
    "carCapturedUTCTimestamp": "Car captured UTC timestamp",
    "careMode": "The value indicates if the Battery Charging Care Mode functionality is on or off.",
    "cationStatus.[*].method": "VTS.",
    "causedBy": "The reason the report was sent by the vehicle.",
    "cenario": "vehicle is charging or waiting to charge.",
    "chargeModeSelection": "The value indicating if the vehicle shall start charging immediately once preconditions are met or if the vehicle shall start charging whenever a timer is active.",
    "chargingConnectors.[*].baseLoadInkW": "Base load caused by signal transfer",
    "chargingConnectors.[*].currentType": "Definition of the current type used for charging",
    "chargingConnectors.[*].efficiency": "Charging efficiency parameter",
    "chargingConnectors.[*].maxPowerInkW": "Maximum charging power",
    "chargingConnectors.[*].maxVoltageInV": "Maximum charging voltage",
    "chargingConnectors.[*].plugTypes.[*]": "Plug type supported by a charging station",
    "chargingConnectors.[*].voltageRange.maxVoltageInV": "maximum charging voltage",
    "chargingConnectors.[*].voltageRange.minVoltageInV": "minimum charging voltage",
    "chargingState": "Electric vehicle charging state",
    "chargingStatus.actionState": "The state describes if the vehicle is charging immediately without a certain goal, or based on a timer/profile.",
    "chargingStatus.chargeMode": "The mode of an ongoing charging process.",
    "chargingStatus.chargePower_kW": "The actual charge power to the HV battery in kW.",
    "chargingStatus.chargeType": "The type of current the connected power supply provides and is used for charging.",
    "chargingStatus.chargingErrorCode": "The number that represents a charging error code in the vehicle.",
    "chargingStatus.chargingScenario": "The scenario of why the vehicle is charging or waiting to charge.",
    "chargingStatus.currentChargeState": "The State of Charging process.",
    "chargingStatus.maximumChargePowerCurrent_kW": "Current maximum charge power current capacity of the battery.",
    "chargingStatus.profileChargeReason": "The specific reason why the charging process is currently running when a profile is active.",
    "chargingStatus.remainingChargingTimeToComplete_min.nanos": "The nano seconds of the estimated remaining charging time until the SOC is reached which was explicitly specified with a target SOC parameter of a 'Start charging' request or a charging profile (remaining time will be provided only when chargeMode is \"immediately\" or \"immediately profile\") Range is between 0 an 3000 minutes with a resolution of 5 minutes.",
    "chargingStatus.updateReason": "The reason for the report being sent from the vehicle.",
    "charging_history_id": "Cars charging session counter",
    "charging_power": "Total charging power",
    "charging_reason": "Reason why charging power is at its actual value",
    "charging_record_details.status": "charging record (e.g., submitted, reimbursed, rejected).",
    "charging_type": "TYPE_CHARGING_NOT_A CTIVE TYPE_AC_CHARGING_AC TIVE TYPE_DC_CHARGING_AC TIVE TYPE_AWC &lt;==&gt; 3(awc) TYPE_DC_DC &lt;==&gt; 4(dc_dc) TYPE_HPC &lt;==&gt; 5(hpc) TYPE_ERROR",
    "chargingcaresettingsdata.batteryCareMode": "Status of the battery care mode settings",
    "chargingcaresettingsdata.carCapturedTimestamp": "Car Captured Timestamp",
    "chargingstatus.carCapturedTimestamp": "Car Captured Timestamp",
    "chargingstatus.chargeMode": "Charge mode (manual, timer based)",
    "chargingstatus.chargePowerInKW": "Current charging power in KW",
    "chargingstatus.chargeRateInInKMPH": "Current charging rate in kilometer per hour",
    "chargingstatus.chargeType": "Charge type (AC/DC)",
    "chargingstatus.chargeenergykwh": "Charged energy in KWH",
    "chargingstatus.chargingSettings": "Charge Setting (default, profile based)",
    "chargingstatus.chargingState": "State of the charging process",
    "chargingstatus.remainingChargingTimeToCompleteInMin": "Time to complete the charging (to reach target soc)",
    "checksum": "the checksum of the file. This is received from the ETag information on the bucket.",
    "ckTimeToWait": "long to wait in seconds with inhibition when ignition has turned off. Only suitable for enabling crank inhibition. If not set, value from VTS Profile will be used",
    "climatisationstatuswrapperdata.carCapturedTimestamp": "Car Captured Timestamp",
    "climatisationstatuswrapperdata.climatisationState": "Status of the pre climatization",
    "climatisationstatuswrapperdata.climatisationTrigger": "Trigger of the pre climatization",
    "climatisationstatuswrapperdata.remainingClimatisationTimeInMin": "Remaining time until pre climatization is stopped",
    "code": "envelope.[*].report. that something went wrong in the vehicle regarding the temperature.",
    "connMgrStatus": "State of the network connection manager",
    "connected_devices": "connected devices",
    "connectionTimestamp": "Used for managing 24h budget cycle in Pre ME3 vehicles",
    "consent_date": "Date of when the consent decision was made",
    "consent_legal_entity": "The legal entity the consent decision was made about",
    "costs": "Total charging energy costs",
    "cpHigh": "Command pilot voltage high level",
    "cpLow": "Command pilot voltage low level",
    "cpPlcModemStatus": "Status of the command pilot plc modem",
    "cpPwm": "PWM duty cycle to vehicle",
    "cpuLoad": "Current cpu load percentage",
    "crt.authorizationEndpoint.AUDI": "ID-Token endpoint of AUDI IDP",
    "crt.authorizationEndpoint.PORSCHE": "ID-Token endpoint of PORSCHE IDP",
    "crt.authorizationGrantType.AUDI": "grant_type ID-Token request",
    "crt.authorizationGrantType.PORSCHE": "PORSCHE: grant_type ID-Token request",
    "crt.containerEndpoint.AUDI": "AUDI: Container Endpoint",
    "crt.containerEndpoint.PORSCHE": "PORSCHE: Container endpoint",
    "crt.csrEndpoint.AUDI": "AUDI: CSR endpoint",
    "crt.csrEndpoint.PORSCHE": "PORSCHE: CSR endpoint",
    "crt.deviceAuthorizationEndpoint.PORSCHE": "PORSCHE: Device-Code Endpoint",
    "crt.idTokenConverterEndpoint.PORSCHE": "PORSCHE: MBB-Compatible IDT converter",
    "crt.ilfBackendHost.AUDI": "AUDI: ILF-Backend-Host",
    "crt.ilfBackendHost.PORSCHE": "PORSCHE: ILF-Backend-Host",
    "crt.mqttBrokerEndpoint": "MQTT Broker endpoint",
    "crt.quarantineEndpoint.AUDI": "AUDI: Quarantine endpoint",
    "crt.quarantineEndpoint.PORSCHE": "PORSCHE: Quarantine endpoint",
    "crt.tokenEndpoint": "Token endpoint of the MBB Authorization Service. The devices can retrieve the below listed endpoints for several systems without the need for authentication or authorization. This helps the devices to get the necessary config. info soon after being manufactured or going through software update.",
    "crt.userinfoEndpoint.AUDI": "AUDI: Userinfo endpoint",
    "crt.userinfoEndpoint.PORSCHE": "PORSCHE: Userinfo endpoint",
    "cruise_range_primary_info.unit": "Information regarding the cruise range primary of the vehicle with subcategory unit",
    "cruise_range_primary_info.value": "Information regarding the cruise range primary of the vehicle with subcategory value",
    "cso_v1_drivingenvironment_outdoorTemperature_subscribefield: value": "A stateful float containing the temperature in Kelvin",
    "cso_v1_drivingenvironment_roadattributes_streetClass_subscribefield: value": "The attribute _street class_ is related to the legal street classes defined in the area the vehicle is currently driving in. As general 'rule of thumb' the lower the number, the more important is the referenced road, the more traffic volume can be handled and the faster vehicles can travel.",
    "cso_v1_drivingenvironment_weathercondition_brightness_subscribe": "brightness from photosensor from 0 to 65535 with resolution 1. sunintensitiyleft from 0 to 1200 with resolution 5 in Watt/m2. sunintensitiyright from 0 to 1200 with resolution 5 in Watt/m2",
    "cso_v1_drivingenvironment_weathercondition_brightnessforward_subscribe": "fieldbrightness from 0 to 101200 with resolution 400 in LUX forwardbrightness from 0 to 6126 with resolution 6 in LUX",
    "cso_v1_drivingenvironment_weathercondition_dewpoint_subscribe": "dewPoint from -40 to 60 degree celsius with 0.1 resolution",
    "cso_v1_drivingenvironment_weathercondition_humidity_subscribe": "humidity in range from 0 to 126 in percent (range &gt; 100% means relative humidity = 100%) with 0.5 resolution and temperature in range from -40 to 90 degree celsius with 0.1 resolution",
    "cso_v1_vehicle_body_exterior_lights_highbeam_status_subscribefield: value": "Message indicating if the given light is requested to be switched on or off. 'true' if on, 'false' if off",
    "cso_v1_vehicle_body_exterior_lights_lowbeam_status_subscribefield: value": "Message indicating if the given light is requested to be switched on or off. 'true' if on, 'false' if off",
    "currency": "Currency",
    "current_energy_cost": "Current Energy cost until this time",
    "current_version": "the current installed software version",
    "custom_costs": "Cost entered by the customer",
    "damper-state.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "damper_front_left E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "damper_front_right E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "damper_rear_left E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "damper_rear_right E31.2.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "debouncePostTime": "seconds until the vehicle sends a violation notification to the Business Service sends.",
    "definitions.[*].listIndex": "Definitions within the definition list",
    "definitions.triggerMileage": "position notification from the vehicle.",
    "door_info.front_left.door_lock_status.value": "Information regarding the door of the vehicle with subcategory front left with subcategory door lock status with subcategory value",
    "door_info.front_left.door_status.value": "Information regarding the door of the vehicle with subcategory front left with subcategory door status with subcategory value",
    "door_info.front_right.door_lock_status.value": "Information regarding the door of the vehicle with subcategory front right with subcategory door lock status with subcategory value",
    "door_info.front_right.door_status.value": "Information regarding the door of the vehicle with subcategory front right with subcategory door status with subcategory value",
    "door_info.rear_left.door_lock_status.value": "Information regarding the door of the vehicle with subcategory rear left with subcategory door lock status with subcategory value",
    "door_info.rear_left.door_status.value": "Information regarding the door of the vehicle with subcategory rear left with subcategory door status with subcategory value",
    "door_info.rear_right.door_lock_status.value": "Information regarding the door of the vehicle with subcategory rear right with subcategory door lock status with subcategory value",
    "door_info.rear_right.door_status.value": "Information regarding the door of the vehicle with subcategory rear right with subcategory door status with subcategory value",
    "drive-belt.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "drive-belt.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "drive-belt.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "ds.[*].newVehicleState": "SUSPEND,FIM,ATTENDA NCE,ALARM,THEFT",
    "ds.[*].originalCaller": "FRONTEND,VEHICLE,MB B,BE",
    "eDueDays": "Time until next inspection",
    "e_kmph": "time unit during ongoing charging process.",
    "end_soc": "Customer State of Charge at the end of the charging session",
    "energyContentOverSOC.[*].stateOfChargeInPercent": "Energy State of charge",
    "energyContentOverSOC.[*].stateOfChargeInkWh": "State of Energy",
    "energy_demand": "value of energyDemand",
    "energy_sum": "Total amount of car's energy consumption",
    "entTimestamp150999969-0-36": "Sync Fond 0 = off; 1 = on",
    "envelope.[*].context.backendCapturedTimestamp.nanos": "Fractions of a second at nanosecond resolution, complementing the seconds field.",
    "envelope.[*].context.backendCapturedTimestamp.seconds": "Specifies the seconds, of UTC time since Unix epoch 1970-01-01T00:00:00Z, at which this report was saved in the backend.",
    "envelope.[*].context.carCapturedTimeStamp.nanos": "Fractions of a second at nanosecond resolution, complementing the seconds field.",
    "envelope.[*].context.carCapturedTimeStamp.seconds": "Indicates the seconds, of UTC time since Unix epoch 1970-01-01T00:00:00Z, at which the vehicle sent this report to the backend.",
    "envelope.[*].context.causedBy": "If the report contains an error then this field describes what cause the error, for instance EDIT_CLIMA_TIMERS.",
    "envelope.[*].context.errorContext.errorType": "Provides information about the error type.",
    "envelope.[*].context.messageId": "A generated string that uniquely identifies the vehicle message.",
    "envelope.[*].context.payloadType": "Specified the type of the report, which this context is part of, for instance CLIMA_SETTINGS_REPOR T.",
    "envelope.[*].context.spanId": "Deprecated field, being phased out by newer reports. Unique identifier for a span, which represents a single operation or computation within a trace.",
    "envelope.[*].context.traceId": "Unique string to be used for tracking purposes like tagging logs. This can make it possible to follow a flow over service boundaries.",
    "envelope.[*].context.traceState": "Deprecated field, being phased out by newer reports. Provides additional vendor-specific trace identification information across different distributed tracing systems. It also conveys information about the request’s position in multiple distributed tracing graphs.",
    "envelope.[*].context.trackingIdentifier": "String to be used for tracking purposes.",
    "envelope.[*].report.backendError.errorDescription": "A description describing the error state of the backend.",
    "envelope.[*].report.backendError.errorNumber": "A number that represents a error code in the backend.",
    "envelope.[*].report.climatizationElementSettings.isClimatizationAtUnlock": "A settings value that describes if the climatization should start after opening the doors with the car key.",
    "envelope.[*].report.climatizationElementSettings.zoneFrontLeftEnabled": "A settings value that describes if front left zone (seat) should be acclimatized.",
    "envelope.[*].report.climatizationElementSettings.zoneFrontRightEnabled": "A settings value that describes if front right zone (seat) should be acclimatized.",
    "envelope.[*].report.climatizationElementSettings.zoneRearLeftEnabled": "A settings value that describes if rear left zone (seat) should be acclimatized.",
    "envelope.[*].report.climatizationElementSettings.zoneRearRightEnabled": "A settings value that describes if rear right zone (seat) should be acclimatized.",
    "envelope.[*].report.climatizationMode": "Describes climatization mode. If \\\"UNDEFINED\\\" then not applicable for that vehicle brand and or model.",
    "envelope.[*].report.climatizationWithoutExternalPower": "A settings value that determines if the infrastructure is inactive or available. If the battery is low (less than 20%), climatization will not be started.",
    "envelope.[*].report.duration": "A settings value indicating how long the climatization will run for.",
    "envelope.[*].report.errorCode": "Describes an aborted situation, which indicates that the climatization could not start.",
    "envelope.[*].report.inCabinTemperature.measurementState": "Describes the measurement state, can be either invalid, unsupported or valid.",
    "envelope.[*].report.inCabinTemperature.temperature.temperature": "A temperature value that describes the latest in-car temperature.",
    "envelope.[*].report.inCabinTemperature.temperature.unit": "The temperature unit used, can be either celsius or fahrenheit.",
    "envelope.[*].report.instrumentClusterTime": "Describes the time with time zone as set by the user inside the car.",
    "envelope.[*].report.messageId": "A value that is used to identify a message.",
    "envelope.[*].report.outdoorTemperature.measurementState": "Describes the measurement state, can be either invalid, unsupported or valid.",
    "envelope.[*].report.outdoorTemperature.temperature.temperature": "A temperature value that describes the latest outdoor (outside-car) temperature.",
    "envelope.[*].report.outdoorTemperature.temperature.unit": "The temperature unit used, can be either celsius or fahrenheit.",
    "envelope.[*].report.remainingClimatizationTime_min.nanos": "Fractions of a second at nanosecond resolution, complementing the seconds field.",
    "envelope.[*].report.remainingClimatizationTime_min.seconds": "Describes how long time (in seconds) it is left until the climatization has (approximately) reached the climatization goal.",
    "envelope.[*].report.status": "Describes what the vehicle is doing to reach the wanted temperature, e.g. cooling, heating or ventilating.",
    "envelope.[*].report.targetTemperature.temperature": "A settings value that describes what temperature the vehicle shall reach while climatization is active.",
    "envelope.[*].report.targetTemperature.unit": "The temperature unit used, can be either celsius or fahrenheit.",
    "envelope.[*].report.timers.id": "A number that is used to identify the different timers.",
    "envelope.[*].report.timers.isEnabled": "A value that describes if the timer is enabled or not.",
    "envelope.[*].report.timers.recurring.recurringOn.fridays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.mondays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.saturdays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.sundays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.thursdays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.tuesdays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.timers.recurring.recurringOn.wednesdays": "A recurring climatization timer setting that describes if the recurring timer should run on this specific day.",
    "envelope.[*].report.trigger": "Describes why the climatization has started, e.g. climatization timer, charging profile timer or immediately by user.",
    "envelope.[*].report.triggerTimerId": "The Id of the climatization timer that triggered the climatization to start. This parameter is only present when \\\"trigger\\\" is set to \\\"timer\\\".",
    "envelope.[*].report.vehicleError.errorDescription": "A description describing the error state of the vehicle.",
    "envelope.[*].report.vehicleError.errorNumber": "A number that represents an error code in the vehicle.",
    "envelope.[*].report.windowHeatingState": "Describes the window heating status, e.g. invalid, off or on.",
    "errorCode": "Error Code in Phone Number List status message",
    "errorContext.errorType": "The type of error.",
    "ers.[*].timerId": "there are a maximum of two timers per vehicle.",
    "etag": "AWS ETAG(Entity TAG)",
    "events.appVersion": "application version of produced event",
    "events.browserResolutionHeight": "browser Resolution Height",
    "events.browserResolutionWidth": "browser Resolution Width",
    "events.entryPointID": "Id of entrypoint where user entered the webapp",
    "events.entryPointType": "type of entrypoint where user entered the webapp",
    "events.event": "event",
    "events.eventAction": "action of the event",
    "events.eventCategory": "category of event",
    "events.eventValue": "value of event",
    "events.id": "id of event",
    "events.name": "name of the event",
    "events.timestamp": "timestamp of event",
    "events.uri": "uri of event",
    "first-aid-kit.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "freezeframe": "Snapshot of system state at DTC occurrence",
    "front.modelDataEntries.odometerValue.longValue": "rdk-battery-left-Aktueller Kilometerstand",
    "fuel_level_info.value": "Information regarding the fuel level of the vehicle with subcategory unit",
    "general-inspection.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "gridPlcModemStatus": "Status of the grid plc modem",
    "harging_sessions.authentication_method": "session was authorized (unstable, will be removed at some point)",
    "harging_sessions.ocpp_transaction_id": "used by the charging station as defined by OCPP",
    "hasWarnedDailyPowerBudget": "Pre ID.S3 indicates daily energy budget is almost used up",
    "hasWarnedPowerLevel": "Indicates energy budget is almost used up",
    "hems_data": "value hems_data",
    "history.[*].clock_src": "the value of clock_src",
    "history.[*].costs": "the value of costs",
    "history.[*].duration": "the value of the charging duration",
    "history.[*].end_soc": "the value of end_soc",
    "history.[*].energy_sum_kwh": "the value of energy_sum per kw/h",
    "history.[*].history_id": "primary key of the record",
    "history.[*].self_energy": "the value of self_energy",
    "history.[*].session_id": "the id of the charging session",
    "history.[*].start_soc": "the value of start_soc",
    "history.[*].start_time": "The value of the charging start time",
    "history_id": "Cars charging counter",
    "home_storage_charging": "The option to start bi-directional DC charging where the vehicle offers to either provide energy to the home storage or store energy surplus is currently available.",
    "hood_info.hood_lock_status.value": "Information regarding the hood of the vehicle with subcategory hood lock status with subcategory value",
    "hood_info.hood_status.value": "Information regarding the hood of the vehicle with subcategory hood status with subcategory value",
    "hvbatterytemperature_info.max_temperature.unit": "Information regarding the hvbatterytemperature of the vehicle with subcategory max temperature with subcategory unit",
    "hvbatterytemperature_info.max_temperature.value": "Information regarding the hvbatterytemperature of the vehicle with subcategory max temperature with subcategory value",
    "hvbatterytemperature_info.min_temperature.unit": "Information regarding the hvbatterytemperature of the vehicle with subcategory min temperature with subcategory unit",
    "hvbatterytemperature_info.min_temperature.value": "Information regarding the hvbatterytemperature of the vehicle with subcategory min temperature with subcategory value",
    "hvsoc_info.value": "Information regarding the hvsoc of the vehicle with subcategory value",
    "i_cdr.country_code": "code of the CPO that owns this CDR.",
    "i_cdr.invoice_reference_id": "reference an invoice, that will later be send for this CDR.",
    "i_cdr.total_parking_time": "charging session where the EV was not charging, in hours.",
    "ident": "Unique identifier or serial number of the equipment.",
    "ignitioneventdata.carCapturedTimestamp": "Car Captured Timestamp",
    "ignitioneventdata.type": "Ignition status (on/off)",
    "ilfdia.FAZIT-String": "Identifier string from the header section.",
    "ilfdia.status": "DTC Status",
    "ilfinv.fazit_string": "a unique ID that identify a device",
    "ilftrf.public.tariff_slot_entity.[*].day_of_week": "the days on which the slot is active in a week.",
    "ilftrf.public.tariff_slot_entity.[*].months": "the months on which the slot is active per year.",
    "ilftrf.public.tariff_slot_entity.[*].price": "the tariff price",
    "ilftrf.public.tariff_slot_entity.[*].start": "The ISO 8601 time (without date)",
    "ilftrf.public.tariff_slot_entity.[*].tariff_slot_id": "unique primary key",
    "ime": "time for vehicle maintenance",
    "immediate_charging": "The option to start charging immediately is currently available.",
    "immediate_discharging": "The option to start bi-directional DC charging to discharge the vehicle to provide power to the home storage is currently available.",
    "inserted_at": "timestamp of the entry in DB",
    "instrumentClusterTime": "The time that is adjusted inside the vehicle.",
    "interior-air-filter.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "interior-air-filter.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "interior-air-filter.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "interiorClimatizationConsumption": "value of interior climatization consumption",
    "inventoryData.[*].odometer": "The odometer reading that is stored in this ECU",
    "inventoryData.[*].odometerUnit": "The unit of the odometer reading, km/miles",
    "inventoryData.[*].swVersions.[*]": "This data represents the current SW version of the vehicle",
    "inventory_data_version": "Version of the produc-tion payload schema",
    "inventory_id": "the primary key of the record",
    "isConnected": "vehicle is considered connected",
    "izationWithoutExternalPower": "determines if the infrastructure is inactive or available. If the battery is low (less than 20%), climatization will not be started.",
    "linuxRamFree": "Free RAM memory in Mbytes",
    "lli_app_ratings.created_at": "timestamp",
    "lli_hss_charging_records.fleet_organization_iam_id": "fleet",
    "lli_hss_csms_charging_session.authorization_result": "the charging session, one of \"Accepted\", \"Invalid\"",
    "logbook.[*].endOdometer": "End odometer value of a specific trip managed in the User Chooser Online Logbook",
    "logbook.[*].odometer_unit": "Unit of the odometer of a customers' vehicle",
    "logbook.[*].reason": "Reason for a specific trip managed in the User Chooser Online Logbook",
    "logbook.[*].startAddress": "Start adress of a specific trip managed in the User Chooser Online Logbook",
    "logbook.[*].visitedBusinessPartner": "Name of the visited business partner of a specific trip managed in Logbookthe User Chooser Online",
    "lugAutoUnlockSettings.allowDCPermanent": "charging plug after a CC charging process. This setting is permanent, i.e., it is not reset after a KL15 off.",
    "m4coreStatus": "Status of m4 core",
    "mac": "6 integer bytes of mac address",
    "maintenancestatusdata.carCapturedTimestamp": "Car Captured Timestamp",
    "maintenancestatusdata.inspectionDueDays": "Days left unitl inspection",
    "maxChargingCurrent": "The value indicating if the vehicle shall use max or a reduced amount of current while charging.",
    "mdkActivated": "activation status of the local MDK system of the vehicle",
    "milage_km": "Kilometerstand",
    "mileage_info.unit": "Information regarding the mileage of the vehicle with subcategory unit",
    "mileage_info.value": "Information regarding the mileage of the vehicle with subcategory value",
    "mileage_km": "Current mileage state",
    "mileagestatusdata.carCapturedTimestamp": "Car Captured Timestamp",
    "mileagestatusdata.mileage": "Total mileage of the car",
    "motor-oil.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "motor-oil.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "motor-oil.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "motor-oil.modelDataEntries.remainingLifetimeDays.doubleValue": "Zeit bis zum nächsten Ãlservice",
    "motor-oil.modelDataEntries.remainingLifetimeKm.doubleValue": "Distanz bis zum nächsten Ãlservice",
    "nextChargingTimer.estimatedChargingFinishedAtUTC.nanos": "The nano seconds of the date and time the charging is estimated to be completed by.",
    "nextChargingTimer.estimatedChargingFinishedAtUTC.seconds": "The seconds of the date and time the charging is estimated to be completed by.",
    "nextChargingTimer.estimatedChargingStartedAtUTC.nanos": "The nano seconds of date and time the charging is estimated to start at.",
    "nextChargingTimer.estimatedChargingStartedAtUTC.seconds": "The seconds of the date and time the charging is estimated to start at.",
    "nextChargingTimer.targetChargePercentageReachable": "Indicates if the next timer will reach charging goals.",
    "o.value": "cruise range primary of the vehicle with subcategory value",
    "ocpi_cdr.invoice_reference_id": "reference an invoice, that will later be send for this CDR.",
    "oducttype": "VTSduringTheft,VTSBasic, VTSplus",
    "oem_hss_csms_charging_session.start_date_time": "session started at the station",
    "oicp_cdr.emp_partner_session_id": "session id assigned by an EMP to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "oicp_cdr.session_start": "which the session started, e.g. swipe of RFID or cable connected.",
    "oicp_plausibility_monitor_cdr.emp_partner_session_id": "session id assigned by an EMP to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "oilServiceDue_days": "Time to next oil service",
    "oilServiceDue_km": "Distance to next oil service",
    "onFailureReason": "calibration was not successful.",
    "on_energy_consumption.csm_lm_session_energy_consumption_created_at": "this entity.",
    "only_own_current": "The option to start charging with Home Energy Management System (HEMS) is currently available.",
    "operationTime": "Active charging hours dtc occured at",
    "origin": "portal origin derived from idp claim (e.g. myAudi)",
    "oruActiveCampaignVehicleStatus.[*].at": "This data represents the time of each progress of OTA update during the whole process",
    "oruActiveCampaignVehicleStatus.[*].campaignVehicleStatus": "This data represents the progress of the OTA update",
    "oruActiveCampaignVehicleStatus.[*].comment": "This data represents the result of the OTA update",
    "oruActiveCampaignVehicleStatus.[*].errorCode": "This data represents code for failure",
    "oruActiveCampaignVehicleStatus.[*].errorMessage": "This data represents details for failure",
    "osShutdown": "Communications unit is shutting down",
    "otificationTimeout": "associated with sending plug reminder notification. This is calculated on the basis of SOC Timestamp Clamp 15 timeout Logic Notification timeout = SOC Timestamp + Clamp15 Timeout (In Milliseconds)",
    "outdoortemperature_info.unit": "Information regarding the outdoortemperature of the vehicle with subcategory unit",
    "outdoortemperature_info.value": "Information regarding the outdoortemperature of the vehicle with subcategory value",
    "outsidetemperaturedata.carCapturedTimestamp": "Car Captured Timestamp",
    "outsidetemperaturedata.temperatureOutsideVehicle": "Outdoor temperature in degrees Kelvin",
    "p_cdr.emp_partner_session_id": "session id assigned by an EMP to the related operation. Partner systems can use this field to link their own session handling to HBS processes.",
    "p_plausibility_monitor_cdr.session_end": "which the session ended. E. g. Swipe of RFID or Cable disconnected.",
    "pairingStatus": "App Status for the customer and to control the app, derived from MDK-BS internal vehicle status",
    "parking_brake_info.value": "Information regarding the parking brake of the vehicle with subcategory value",
    "parking_lights_info.left_status.value": "Information regarding the parking lights of the vehicle with subcategory left status with subcategory value",
    "parking_lights_info.right_status.value": "Information regarding the parking lights of the vehicle with subcategory right status with subcategory value",
    "partition_dt": "Timestamp when TSS data is received in MAN connectivity layer",
    "payloadDecoded_brand": "Vehicle brand",
    "payloadDecoded_country": "Country of vehicle registration",
    "payloadDecoded_fuelLevel": "Full level in %",
    "payloadDecoded_ican_components_cid": "Unique identifier of the component within the in-vehicle CAN network. id of component, 1= ENGINE_OIL, 50=MVS",
    "payloadDecoded_ican_components_data": "due date or remaining KM, depending on component",
    "payloadDecoded_ican_components_optMode": "Optimization modes of compoments ZWS: Time-based maintenance system FWS: Flexible maintenance system",
    "payloadDecoded_ican_components_unit": "Measurement unit in-vehicle CAN bus",
    "payloadDecoded_mileage": "Odomoter value in km",
    "payloadDecoded_position_bearing": "Heading of vehicle in decial degree, interval (-180*;+180*)",
    "payloadDecoded_position_timestamp": "UTC timestamp when dispatched",
    "payloadDecoded_position_trueness": "fair, good, none, weak",
    "payloadDecoded_timestamp": "Timestamp when data is received in MBBconnector",
    "payloadDecoded_vehicleState_ambientAirTemperature": "Outside temperature in dK",
    "payloadDecoded_vehicleState_catalystTankInfo_level": "AdBlue level in %",
    "payloadDecoded_vehicleState_catalystTankInfo_remainingRange": "Left AdBlue range (SCR distance)",
    "payloadDecoded_vehicleState_catalystTankInfo_totalRange": "Total AdBlue range (SCR total distance)",
    "payloadDecoded_vehicleState_dashboard_engineOilLevel_indicatorLamp": "Engine oil warning lamp",
    "payloadDecoded_vehicleState_dashboard_engineOilLevel_unit": "Measurement unit of Oil Level",
    "payloadDecoded_vehicleState_dashboard_engineOilLevel_value": "Engine oil level in %",
    "payloadDecoded_vehicleState_dashboard_fuelLevel_indicatorLamp": "Odomoter value in km",
    "payloadDecoded_vehicleState_dashboard_fuelLevel_unit": "Measurement unit of Fuel Level",
    "payloadDecoded_vehicleState_dashboard_fuelLevel_value": "Fuel level in %",
    "payloadDecoded_vehicleState_tyreInfo_pressure": "List of Tire Pressures",
    "payloadDecoded_warnings_fields": "Information about the warning lights being actived in the instrument cluster filtered (FFF)",
    "payloadDecoded_warnings_fields_TsTssReceivedUtc": "UTC time when received by TSS",
    "payloadDecoded_warnings_fields_id": "Unique identifier of the warning message",
    "payloadDecoded_warnings_fields_milCarCaptured": "Mileage at the time of capture",
    "payloadDecoded_warnings_fields_milCarSent": "Mileage at the time of sending",
    "payloadDecoded_warnings_fields_picId": "ID of the associated picture",
    "payloadDecoded_warnings_fields_text": "Text associated with the field",
    "payloadDecoded_warnings_fields_textId": "ID for the associated text",
    "payloadDecoded_warnings_fields_tsCarCaptured": "Car local time when captured",
    "payloadDecoded_warnings_fields_tsCarSent": "Car local time when sent",
    "payloadDecoded_warnings_fields_tsCarSentUtc": "UTC time when sent",
    "payloadDecoded_warnings_fields_value": "The value of the field",
    "payloadDecoded_warnings_id": "Summary of all collected warnings",
    "payloadType": "The type of report sent by the vehicle.",
    "pendingUseHV": "used for managing the setting of the HV value in the vehicle",
    "piration": "license will expire. Usually this is this defined by the actual active license. Near expiration date of the actual active license there might already exist a prolongation license, defining the expirationDate",
    "planned_power": "Planned power in KW for the slot",
    "plugStatusItem.chargingPlugType": "The type of plug.",
    "plugStatusItem.flapLockState": "The flap lock state.",
    "plugStatusItem.flap_open_state": "The flap open state.",
    "plugStatusItem.infrastructureState": "The current state of infrastructure.",
    "plugStatusItem.plugConnectionState": "The current plug connection state.",
    "plugStatusItem.plugLockState": "The current lock state of the plug.",
    "plugStatusItem.plugPosition": "The position of the plug.",
    "plugstatus.carCapturedTimestamp": "Car Captured Timestamp",
    "plugstatus.externalPower": "Indikator if the source (e.g. Wallbox) provide power",
    "plugstatus.plugConnectionState": "Indicator if the plug is connected",
    "plugstatus.plugLockState": "Indicator if the plug is looked",
    "po_user_id": "primary key of the user record",
    "power_curve.[*].power_curve_slot.[*].available_self_generated_power": "value for availa-ble_self_generated_powe r",
    "power_curve.[*].power_curve_slot.[*].current_energy_cost": "value for current_energy_cost",
    "power_curve.[*].power_curve_slot.[*].power_curve_id": "this is the foreign key of the pow- er_curve_data record. Must not be null",
    "power_curve.[*].power_curve_slot.[*].power_l1": "value for power_l1",
    "power_curve.[*].power_curve_slot.[*].power_l2": "value for power_l2",
    "power_curve.[*].power_curve_slot.[*].power_l3": "value for power_l3",
    "power_curve.[*].power_curve_slot.[*].power_limit_l1": "value for power_limit_l1",
    "power_curve.[*].power_curve_slot.[*].power_limit_l2": "value for power_limit_l2",
    "power_curve.[*].power_curve_slot.[*].power_limit_l3": "value for power_limit_l3",
    "power_curve.[*].power_curve_slot.[*].power_limit_reason_l1": "value for power_limit_reason_l1",
    "power_curve.[*].power_curve_slot.[*].power_limit_reason_l2": "value for power_limit_reason_l2",
    "power_curve.[*].power_curve_slot.[*].power_limit_reason_l3": "value for power_limit_reason_l3",
    "power_curve.[*].session_id": "the id of the charging session",
    "power_curve.[*].start_time": "The value of the charging start time",
    "power_curve_id": "this is the foreign key of the pow- er_curve_data record. Must not be null",
    "power_limit_infrastructure": "Power limit of charging infrastructure",
    "power_plan.[*].plan_id": "the primary key of the record",
    "power_plan.[*].power_plan_slot.[*].offset_val": "the offset of the defined time",
    "power_plan.[*].power_plan_slot.[*].plan_id": "the id of the power_plan record that is linked with that record",
    "power_plan.[*].power_plan_slot.[*].power_plan_w": "the planned charging power",
    "power_plan.[*].power_plan_slot.[*].slot_id": "the primary key of the record",
    "power_plan.[*].session_id": "the id of the charging session",
    "power_plan_id": "The unique identifier of a resource element instance",
    "preferred_charging_times": "The option to start charging using preferred charging times is currently available.",
    "production_data_for_after_sales": "Production data for after sales as JSON object specified by the OEM",
    "production_data_for_customer": "Production data for cus-tomer as JSON object specified by the OEM",
    "production_record": "Production record as JSON object specified by the OEM",
    "profile_name": "Profile name (eg. At home)",
    "profiles.maxChargingCurrent": "Max/reduced current while charging.",
    "profiles.options.autoUnlockPlugWhenCharged": "Whether the plug unlocks automatically after charging.",
    "profiles.options.smartChargingEnabled": "Smart charging enabled.",
    "profiles.options.useExternalService": "Use PV system or external supply.",
    "profiles.options.usePreferredChargingTimes": "Use preferred charging times.",
    "profiles.options.usePrivateCurrentEnabled": "Use private current.",
    "profiles.profileType": "Profile type.",
    "profiles.targetSOCPercentage": "Target SOC.",
    "profiles.timers.charging": "Charging flag.",
    "profiles.timers.climatization": "Climatisation flag.",
    "profiles.timers.recurring.recurringOn.saturdays": "Scheduled Saturday.",
    "profiles.timers.recurring.recurringOn.sundays": "Scheduled Sunday.",
    "profiles.timers.recurring.recurringOn.thursdays": "Scheduled Thursday.",
    "profiles.timers.recurring.recurringOn.tuesdays": "Scheduled Tuesday.",
    "profiles.timers.recurring.recurringOn.wednesdays": "Scheduled Wednesday.",
    "profiles.timers.single.departure.nanos": "The nano seconds of the time by when the charging should be completed. This time is a one-time occurrence.",
    "profiles.timers.uuid": "Timer UUID.",
    "profiles.uuid": "Profile UUID.",
    "rdk-battery-left-front.modelDataEntries.TABROW_LeftFrontTirePressSensoIdent_Param_SensoIdentNumbe.longValue": "identification number of tire pressure control battery unit left front",
    "rdk-battery-left-front.modelDataEntries.TABROW_LeftFrontTirePressSensoRemaiOperaTime_Restlebensdauer_1.longValue": "the remaining lifetime of tire pressure control battery left front",
    "rdk-battery-left-rear.modelDataEntries.TABROW_LeftRearTirePressSensoIdent_Param_SensoIdentNumbe.longValue": "identification number of tire pressure control battery unit left rear",
    "rdk-battery-left-rear.modelDataEntries.TABROW_LeftRearTirePressSensoRemaiOperaTime_Restlebensdauer_1.longValue": "the remaining lifetime of tire pressure control battery left rear",
    "rdk-battery-left-rear.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "rdk-battery-right-front.modelDataEntries.TABROW_RightFrontTirePressSensoIdent_Param_SensoIdentNumbe.longValue": "identification number of tire pressure control battery unit right front",
    "rdk-battery-right-front.modelDataEntries.TABROW_RightFrontTirePressSensoRemaiOperaTime_Restlebensdauer_1.longValue": "the remaining lifetime of tire pressure control battery right front",
    "rdk-battery-right-front.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "rdk-battery-right-rear.modelDataEntries.TABROW_RightRearTirePressSensoIdent_Param_SensoIdentNumbe.longValue": "identification number of tire pressure control battery unit right rear",
    "rdk-battery-right-rear.modelDataEntries.TABROW_RightRearTirePressSensoRemaiOperaTime_Restlebensdauer_1.longValue": "the remaining lifetime of tire pressure control battery right rear",
    "rdk-battery-right-rear.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "resetUseHV": "Indicates a factory reset has occurred and the useHV option should be reset when the vehicle is back online",
    "residualConsumption": "value of energy consumption of residual car network components",
    "rootPart1FreeMem": "Free ROM memory of root partition 1",
    "rootPart2FreeMem": "Free ROM memory of root partition 2",
    "rts.[*].carCapturedTimestamp": "Timestamp in which the relevant trip was captured in the car",
    "rts.[*].messageReceivedTimestamp": "Timestamp when the vehicle java service received the message.",
    "rts.[*].overallMileage": "Mileage of the vehicle",
    "rts.[*].reason": "Specifies what triggered the stored data to be sent from the vehicle. (DEFAULT, CLAMP15_OFF or USER_RESET)",
    "rts.[*].updatedTimestamp": "whenever a trip update is sent from the vehicle, the 'updated timestamp' will be updated",
    "scr_range_info.unit": "Information regarding scr range of the vehicle with subcategory unit",
    "scr_range_info.value": "Information regarding the scr range of the vehicle with subcategory value",
    "seasonal-tire-change.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "seconds_elapsed": "Time in seconds since last DTC status change.",
    "self_energy": "Total amount of car's self-energy consumption (PV energy consumption)",
    "self_power": "Self produced charging power",
    "service-interval.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "service-interval.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "service-interval.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "service-interval.modelDataEntries.remainingLifetimeDays.doubleValue": "service history date",
    "service-interval.modelDataEntries.remainingLifetimeKm.doubleValue": "service history mileage",
    "serviceCardActivated": "Activation status of the service card",
    "service_maintenance_info.due_in_distance.unit": "Service maintenancen info due in instance",
    "service_maintenance_info.due_in_distance.value": "Information regarding the service maintenance of the vehicle with subcategory due in distance with subcategory value",
    "service_maintenance_info.due_in_time.value": "Information regarding the service maintenance of the vehicle with subcategory due in time with subcategory value",
    "service_maintenance_info.service_type": "Information regarding the service maintenance of the vehicle with subcategory service type",
    "session_duration": "Duration of the session in seconds. (max 48h)",
    "session_id_infrastructure": "Session Id assigned by the charging infrastructure",
    "signed_inventory_data": "Signed production spe-cific device data as JWT string",
    "slot_id": "The unique identifier of a resource element instance",
    "soc": "State of charge",
    "spark-plugs.modelDataEntries.FIX_Maximum_distance_to_next_mileage_based_service_event_zdc.doubleValue": "service history mileage",
    "spark-plugs.modelDataEntries.FIX_Maximum_time_to_next_time_based_service_event_zdc.doubleValue": "service history date",
    "spark-plugs.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "spatialTolerance": "tolerance, up to the vehicle sends a valetalert violation to the business service.",
    "start_session": "Start time of charging session - ISO 8601 time stamp with timezone \"2017-10-26T10:59:46.227+02:00\" time when charging communication has been established (=new session ID from charging infrastructure) cars local time, which customer sees in the car",
    "start_soc": "Customer State of Charge at the beginning of the charging session",
    "start_time": "timestamp with time zone",
    "state.notification": "The current battery charging care mode notification state.",
    "state.threshold": "The maximum charge limit of SOC enforced while battery care mode is active.",
    "state_connection_car": "value of state_connection_car",
    "state_connection_home": "value of state_connection_home",
    "state_feature_hems": "value of state_connection_hems",
    "status_id": "the primary key of the record",
    "sun_roof_info.sunroof_percentage_open.value": "Information regarding the sun roof of the vehicle with subcategory sun roof percentage open with subcategory value",
    "sun_roof_info.sunroof_status.value": "Information regarding the sun roof of the vehicle with subcategory sun roof status with subcategory value",
    "t_speeda_alert.definition_id": "Reference to the definition ID",
    "t_speeda_alert.definition_index": "Index of the associated job in vehicle",
    "t_speeda_alert.definition_name": "User assign name for definition",
    "t_speeda_definition.definition_index": "Index of the associated job in the vehicle",
    "t_speeda_definition.definition_name": "The name of the definition — set by the customer: It is the basis for the alarm",
    "t_speeda_definition.list_index": "Index for sorting the definitions within the definition list",
    "t_speeda_definition.ref_list_id": "Technical ID representing the ID of the definition list to which the definition belongs",
    "t_speeda_definition_list.channel": "Channel used to send the request",
    "t_speeda_definition_list.list_type": "List type ACTIVATE /DEACTIVATE",
    "t_speeda_definition_list.status": "Status of the definition list • PENDING: Receipt has not yet been confirmed by the vehicle • ACK: Receipt has been confirmed by the vehicle • ERROR: An error has occurred • RESYNC: The definition list is in resynchronization with the vehicle.\"",
    "t_speeda_definition_list.traceid": "The ID sent by the application, which is used only for tracking the request. (technical ID",
    "t_speeda_job.business_id": "Business ID of the signal. Default value is the same as the ID",
    "t_speeda_job.debounce_post_time": "Value for the time lead in seconds until the vehicle sends a speed signal notification to the business service.",
    "t_speeda_job.debounce_pre_time": "Value for the time delay in seconds until the vehicle sends a speed signal notification to the business service",
    "t_speeda_job.obd_callback_timestamp": "Ond callback timestamp",
    "t_speeda_job.obd_job_id": "Job ID assigned by the outbound dispatcher Note: When the job is first created in the database, the JobId is NULL and is only filled after being sent via the OBD.",
    "t_speeda_job.ref_request_id": "Ref request id",
    "t_speeda_req_histentry.request_result": "Result of the request. Can be SUCCESS if the request was successful, PENDING if the request is being processed, RESYNC if the information was internally resynchronized, or ERROR if an error occurred during the request processing.",
    "t_speeda_req_histentry.request_timestamp": "Timestamp when the request was made.",
    "t_speeda_req_histentry.request_type": "\"Type of the request • ACTIVATE: An ActivateJob was sent • DEACTIVATE: A DeactivateJob was sent • UPDATE: Only data was updated in the backend service\"",
    "t_speeda_req_histentrylist_id": "Reference to the associated definition list.",
    "t_speeda_schedule.ref_definition_id": "Technical ID that represents the ID schedule is linked.",
    "t_speeda_schedule.start_time": "Start time of schedule",
    "t_speeda_schedule.start_time_of_day": "Start time of schedule of day",
    "targetSoc_pct": "The maximum charge level the battery should be charged as specified by the user. The allowed range is between 25% and 100%.",
    "target_region": "Target region of the de-vice given in JSON ob-ject productionDetails",
    "target_soc": "Target State of Charge for a charging session",
    "tariff_data_source": "value of tariff_data_source",
    "tariff_id": "The unique identifier of a the used Tariff, if available. For Example Charging Infrastructure or Backend",
    "tariff_source": "Qualifies the Tariff Source",
    "tdoorTemp": "Kelvin (dK) (Conversation rule dkó Celsius, see 20141127_RemotePreTri pClima.pdf MOD_RPC_1541 )",
    "tempCable": "Temperature value of sensor on grid cable side",
    "tempLcd": "Temperature value of sensor near display",
    "tempRelay1": "Temperature value of sensor near relay 1",
    "tempRelay2": "Temperature value of sensor near relay 2",
    "tempUC": "Temperature value of sensor in A9 core",
    "timeInCar": "Event timestamp (as local time time in the car)",
    "timeOfOccurrenceOnCar": "Timestamp when the event is generated",
    "timeOfReceipt": "Timestamp when the event is received in the backend",
    "timeToLive": "The time to live for the item in the database.",
    "time_of_inventory_upload": "Date and time of the inventory upload",
    "timer_charging": "The option to start charging with a timer is currently available.",
    "timer_charging_climatization": "The option to start charging and start climatization with a timer is currently available.",
    "timestamp": "Timestamp of when the environment snapshot was taken.",
    "timestamp(vehicle)": "Timestamp in Phone Number List status message",
    "tire-fit-kit.modelDataEntries.odometerValue.longValue": "Aktueller Kilometerstand",
    "tire_info.front_left.actual_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory front left with subcategory actual pressure status with subcategory unit",
    "tire_info.front_left.actual_pressure_status.value": "Information regarding the tires of the vehicle with subcategory front left with subcategory actual pressure status with subcategory value",
    "tire_info.front_left.required_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory front left with subcategory required pressure status with subcategory unit",
    "tire_info.front_left.required_pressure_status.value": "Information regarding the tires of the vehicle with subcategory front left with subcategory required pressure status with subcategory value",
    "tire_info.front_right.actual_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory front right with subcategory actual pressure status with subcategory unit",
    "tire_info.front_right.actual_pressure_status.value": "Information regarding the tires of the vehicle with subcategory front right with subcategory actual pressure status with subcategory value",
    "tire_info.front_right.required_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory front right with subcategory required pressure status with subcategory unit",
    "tire_info.front_right.required_pressure_status.value": "Information regarding the tires of the vehicle with subcategory front right with subcategory required pressure status with subcategory value",
    "tire_info.rear_left.actual_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory rear left with subcategory actual pressure status with subcategory unit",
    "tire_info.rear_left.actual_pressure_status.value": "Information regarding the tires of the vehicle with subcategory rear left with subcategory actual pressure status with subcategory value",
    "tire_info.rear_left.required_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory rear left with subcategory required pressure status with subcategory unit",
    "tire_info.rear_left.required_pressure_status.value": "Information regarding the tires of the vehicle with subcategory rear left with subcategory required pressure status with subcategory value",
    "tire_info.rear_right.actual_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory rear right with subcategory actual pressure status with subcategory unit",
    "tire_info.rear_right.actual_pressure_status.value": "Information regarding the tires of the vehicle with subcategory rear right with subcategory actual pressure status with subcategory value",
    "tire_info.rear_right.required_pressure_status.unit": "Information regarding the tires of the vehicle with subcategory rear right with subcategory required pressure status with subcategory unit",
    "tire_info.rear_right.required_pressure_status.value": "Information regarding the tires of the vehicle with subcategory rear right with subcategory required pressure status with subcategory value",
    "totalDistance": "Absolut mileage of a vehicle",
    "totalDistanceChargeDepletingEngineOff": "Absolut mileage electric of a vehicle",
    "trailerState": "Indicate if vehicle has trailer or not",
    "training_data_id": "Unique identifier for training data collected",
    "trp.idlSec": "Time the vehicle was idle during the trip",
    "trp.odoE": "Odometer reading at end",
    "trp.odoS": "Odometer reading at start",
    "trp.staSec": "Time the vehicle was standing still",
    "trp.tzn": "IANA time zone for the trip",
    "trunk_lid_info.trunk_lid_lock_status.value": "Information regarding the trunk lid of the vehicle with subcategory trunk lid lock status with subcategory value",
    "trunk_lid_info.trunk_lid_status.value": "Information regarding the trunk lid of the vehicle with subcategory trunk lid status with subcategory value",
    "unlock_all": "Remote Unlock All Doors",
    "useHVMessageId": "used for managing acknowledgement of HV setting",
    "userdataPart1FreeMem": "Free ROM memory of user partition 1",
    "userdataPart2FreeMem": "Free ROM memory of user partition 2",
    "usr.ana[].cnt": "Count or metric of the analytics event",
    "usr.ana[].nam": "Name of the screen or analytics event",
    "usr.cfg": "Remote configuration version used by the app",
    "vehicleError.backendCapturedTimestamp": "The UTC timestamp specifying when the error report was received in the Device Platform backend.",
    "vehicleError.carCapturedTimestamp": "The UTC timestamp specifying when the error report was sent from the car.",
    "vehicleError.errorDescription": "The description of the error code from the car.",
    "vehicleError.errorNumber": "The number that represents the error code from the car.",
    "vehicleIdentifier": "The unique identifier of the vehicle used in the Device Platform backend.",
    "vehicleMaxSpeed": "Active vehicle speed limitation of the drive mode",
    "vehiclePairingStatus": "App Status for the customer and to control the app, derived from MDK-BS internal vehicle status",
    "vehiclePlatform": "The type of vehicle platform.",
    "velocityAdaptation.velocityOffsetFRC.[*]": "velocity offset on different road",
    "velocityAdaptation.velocityOffsetUnlimitedHighway": "Adapted offset of driven speed to AvrgSpeed for RangeOnRoute calculation on unlimited highways",
    "velocityAdaptation.zeroSpeedAdaptationOffsetRatio": "Current offset of zero energyContent (currentEnergyContent) to zero state of charge due to cold battery",
    "warninglightdata.carCapturedTimestamp": "Car Captured Timestamp",
    "warninglightdata.category": "Category of the warning (eg. ASSISTANCE, TIRE, ENGINE)",
    "warninglightdata.customerRelevance": "Customer relevance of the warning",
    "warninglightdata.iconColor": "Color of the warning icon (white, yellow, red)",
    "warninglightdata.messageId": "ID of the warning message",
    "warninglightdata.notificationId": "ID of the warning notification",
    "warninglightdata.priority": "Priority of the warning as number",
    "warninglightdata.serviceLead": "Service lead relevance of the warning",
    "warninglightdata.text": "Warning text shown in the driver display",
    "whitelistTimestamp": "vehicle was confirmed white listed last at this timestamp",
    "wifiModemStatus": "Status of the wifi modem",
    "window_info.front_left.window_percentage_open.value": "Information regarding the window of the vehicle with subcategory front left with subcategory window percentage open with subcategory value",
    "window_info.front_left.window_status.value": "Information regarding the window of the vehicle with subcategory front left with subcategory window status with subcategory value",
    "window_info.front_right.window_percentage_open.value": "Information regarding the window of the vehicle with subcategory front right with subcategory window percentage open with subcategory value",
    "window_info.front_right.window_status.value": "Information regarding the window of the vehicle with subcategory front right with subcategory window status with subcategory value",
    "window_info.rear_left.window_percentage_open.value": "Information regarding the window of the vehicle with subcategory rear left with subcategory window percentage open with subcategory value",
    "window_info.rear_left.window_status.value": "Information regarding the window of the vehicle with subcategory rear left with subcategory window status with subcategory value",
    "window_info.rear_right.window_percentage_open.value": "Information regarding the window of the vehicle with subcategory rear right with subcategory window percentage open with subcategory value",
    "window_info.rear_right.window_status.value": "Information regarding the window of the vehicle with subcategory rear right with subcategory window status with subcategory value",
    "zigbeeModemStatus": "Status of the zigBee modem",
}


def parse_ts(s):
    """Both '2026-05-15 07:23:51' and '2026-06-29T11:21:22.432Z' occur."""
    if not s or s == "N/A":
        return None
    s = s.replace("T", " ").rstrip("Z").split(".")[0]
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return int(t.replace(tzinfo=dt.timezone.utc).timestamp())


def to_local(epoch):
    return dt.datetime.fromtimestamp(epoch + LOCAL_UTC_OFFSET_H * 3600, dt.timezone.utc)


def num(value):
    try:
        return float(value.split()[0])
    except (ValueError, IndexError):
        return None


def series(recs, field, lo=None, hi=None):
    """Sorted [(epoch, value)] for a numeric channel, range-filtered."""
    out = []
    for r in recs:
        if r["dataFieldName"] != field:
            continue
        t = parse_ts(r.get("timestampUtc"))
        v = num(r["value"])
        if t is None or v is None:
            continue
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        out.append((t, v))
    out.sort()
    return out


def despike(pts, jump):
    """Drop single-sample spikes: values that leap > jump away from BOTH
    temporal neighbors (in the same direction). The SoC channel emits isolated
    0.0 readings between otherwise-normal values; a genuine extreme would be
    approached gradually and survives this filter."""
    if len(pts) < 3:
        return pts
    out = [pts[0]] if abs(pts[0][1] - pts[1][1]) <= jump else []
    for prev, cur, nxt in zip(pts, pts[1:], pts[2:]):
        d1, d2 = cur[1] - prev[1], cur[1] - nxt[1]
        if abs(d1) > jump and abs(d2) > jump and d1 * d2 > 0:
            continue
        out.append(cur)
    if abs(pts[-1][1] - pts[-2][1]) <= jump:
        out.append(pts[-1])
    return out


def bucket_mean(pts, seconds):
    out = collections.defaultdict(list)
    for t, v in pts:
        out[t // seconds * seconds].append(v)
    return sorted((t + seconds // 2, sum(vs) / len(vs)) for t, vs in out.items())


def round2(pts):
    return [[t, round(v, 2)] for t, v in pts]


def median(values):
    values = sorted(values)
    if not values:
        return None
    i = len(values) // 2
    return values[i] if len(values) % 2 else (values[i - 1] + values[i]) / 2


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    return values[round((len(values) - 1) * p)]


def collapse_median(pts):
    """One robust value per timestamp; the export sometimes duplicates a
    timestamp with conflicting samples."""
    grouped = collections.defaultdict(list)
    for t, v in pts:
        grouped[t].append(v)
    return sorted((t, median(vs)) for t, vs in grouped.items())


def nearest_value(pts, t, max_gap_s=20 * 60):
    if not pts:
        return None
    times = [x for x, _ in pts]
    i = bisect.bisect_left(times, t)
    candidates = []
    if i < len(pts):
        candidates.append(pts[i])
    if i:
        candidates.append(pts[i - 1])
    best = min(candidates, key=lambda p: abs(p[0] - t))
    return best[1] if abs(best[0] - t) <= max_gap_s else None


def nearest_with_age(pts, t, max_gap_s=20 * 60):
    """Like nearest_value but also returns how stale the sample is (seconds)."""
    if not pts:
        return None, None
    times = [x for x, _ in pts]
    i = bisect.bisect_left(times, t)
    cands = [pts[j] for j in (i - 1, i) if 0 <= j < len(pts)]
    best = min(cands, key=lambda p: abs(p[0] - t))
    age = abs(best[0] - t)
    return (best[1], age) if age <= max_gap_s else (None, None)


def mask_identifier(value):
    if not value:
        return value
    value = str(value)
    return value[:3] + "…" + value[-4:] if len(value) > 8 else "[redacted]"


def is_sensitive_field(field):
    f = field.lower()
    return (f in {"userid", "vehicleidentifier", "usehvmessageid"}
            or "trackingidentifier" in f)


def decode_config_value(value, description):
    """Decode only unambiguous settings; retain the raw value alongside it."""
    value = value.strip()
    if value == "factory setting is used":
        return "Factory default"
    desc = description.lower()
    if re.search(r"\b0\s*=\s*off\s*;?\s*1\s*=\s*on\b", desc):
        try:
            raw = bytes.fromhex(value).decode("ascii")
            n = int(raw) if re.fullmatch(r"[01]", raw) else int(value, 16)
            return "On" if n == 1 else "Off" if n == 0 else value
        except (ValueError, UnicodeDecodeError):
            return value
    if "level 0" in desc and "3" in desc:
        try:
            return "Level " + str(int(value, 16))
        except ValueError:
            return value
    return value


def detect_sessions(activity_ts, odo, soc, speed, gap_s=45 * 60):
    """Cluster activity timestamps into sessions; classify drive vs charge."""
    def within(pts, a, b):
        return [(t, v) for t, v in pts if a <= t <= b]

    sessions = []
    if not activity_ts:
        return sessions
    start = prev = activity_ts[0]
    clusters = []
    for t in activity_ts[1:]:
        if t - prev > gap_s:
            clusters.append((start, prev))
            start = t
        prev = t
    clusters.append((start, prev))

    for a, b in clusters:
        o = within(odo, a, b)
        s = within(soc, a, b)
        v = within(speed, a, b)
        dist = round(max(p for _, p in o) - min(p for _, p in o)) if o else 0
        vmax = round(max(p for _, p in v)) if v else None
        soc_from = round(s[0][1]) if s else None
        soc_to = round(s[-1][1]) if s else None
        if dist >= 1:
            kind = "drive"
        elif soc_from is not None and soc_to is not None and soc_to - soc_from >= 2:
            kind = "charge"
        else:
            kind = "other"
        if b - a < 120 and dist < 1:
            continue
        sessions.append({
            "start": a, "end": b, "dist": dist, "vmax": vmax,
            "socFrom": soc_from, "socTo": soc_to, "kind": kind,
        })
    return sessions


def detect_charge_runs(soc, gap_s=CHARGE_SAMPLE_GAP_S, min_gain=3):
    """Return sustained nondecreasing SoC runs that represent charging.

    Keeping this in one helper ensures trip boundaries and the charging ledger
    use the same evidence. In particular, a short charging stop must not be
    hidden inside a single odometer-derived trip.
    """
    charges = []
    run = None

    def finish(candidate):
        if candidate and candidate[3] - candidate[1] >= min_gain:
            charges.append(candidate)

    for (t0, v0), (t1, v1) in zip(soc, soc[1:]):
        if t1 - t0 <= gap_s and v1 >= v0:
            if v1 > v0:
                run = [t0, v0, t1, v1] if run is None else [run[0], run[1], t1, v1]
            elif run:
                run[2] = t1
                run[3] = v1
        else:
            finish(run)
            run = None
    finish(run)
    return charges


def detect_trips(odo, soc, speed, current, ambient, sample_gap_s=TRIP_SAMPLE_GAP_S):
    """Build an evidence-led trip ledger from positive odometer edges.

    Edges separated by no more than 30 minutes are observed movement and are
    clustered into trips, except where a sustained charging run proves that a
    stationary charge stop occurred between them. Longer edges prove that
    distance was added, but not when within the gap; those remain explicit
    unresolved movement intervals. Together the two sets reconcile exactly to
    the odometer delta.
    """
    odo = collapse_median(odo)
    edges = []
    for (t0, v0), (t1, v1) in zip(odo, odo[1:]):
        delta = v1 - v0
        if 0 < delta <= 500:
            edges.append((t0, t1, delta))

    observed = [e for e in edges if e[1] - e[0] <= sample_gap_s]
    gaps = [e for e in edges if e[1] - e[0] > sample_gap_s]
    charge_runs = detect_charge_runs(soc)

    def charge_between(left, right):
        """A charge substantially contained in a no-movement interval.

        Sensor timestamps can lead or lag odometer edges slightly, hence the
        five-minute tolerance at either boundary.
        """
        tolerance = 5 * 60
        return any(ca >= left - tolerance and cb <= right + tolerance
                   and cb > left and ca < right
                   for ca, _va, cb, _vb in charge_runs)

    clusters = []
    for a, b, km in observed:
        if (not clusters or a - clusters[-1][1] > sample_gap_s
                or charge_between(clusters[-1][1], a)):
            clusters.append([a, b, km])
        else:
            clusters[-1][1] = b
            clusters[-1][2] += km

    trips = []
    for a, b, km in clusters:
        pad = 5 * 60
        speeds = [v for t, v in speed if a - pad <= t <= b + pad]
        amps = [v for t, v in current if a - pad <= t <= b + pad]
        ambs = [v for t, v in ambient if a - pad <= t <= b + pad]
        moving = [v for v in speeds if v > 1]
        dur_min = (b - a) / 60
        moving_min = round(dur_min * len(moving) / len(speeds)) if speeds else None
        sf, sf_age = nearest_with_age(soc, a)
        st, st_age = nearest_with_age(soc, b)
        soc_used = max(0, sf - st) if sf is not None and st is not None else None
        kwh100 = (soc_used / 100 * PACK_KWH_USABLE / km * 100
                  if soc_used is not None and soc_used >= 1 and km >= 5 else None)
        cons_conf = None
        if kwh100 is not None:
            cons_conf = "good" if max(sf_age, st_age) <= 10 * 60 else "fair"
        trips.append({
            "start": a, "end": b, "dist": round(km, 1),
            "vmax": round(max(speeds)) if speeds else None,
            "avgMoving": round(sum(moving) / len(moving)) if moving else None,
            "movingMin": moving_min,
            "socFrom": round(sf, 1) if sf is not None else None,
            "socTo": round(st, 1) if st is not None else None,
            "socUsed": round(soc_used, 1) if soc_used is not None else None,
            "kwh100": round(kwh100, 1) if kwh100 is not None else None,
            "consConf": cons_conf,
            "peakDischargeA": round(abs(min(amps)), 1) if amps and min(amps) < 0 else None,
            "peakRegenA": round(max(amps), 1) if amps and max(amps) > 0 else None,
            "ambientC": round(sum(ambs) / len(ambs), 1) if ambs else None,
            "confidence": "observed",
        })

    # Sampling gaps still deserve honesty about what little telemetry they do
    # contain: sparse speed or battery-discharge samples prove the car moved
    # at a knowable time, even though most of the gap's distance stays
    # unsampled. Surface that as evidence, never as reconstructed trips —
    # one gap can hide several drives.
    movement_gaps = []
    for a, b, km in gaps:
        moving = [(t, v) for t, v in speed if a < t < b and v > 1]
        load = [(t, v) for t, v in current if a < t < b and v < -5]
        times = [t for t, _ in moving] + [t for t, _ in load]
        movement_gaps.append({
            "start": a, "end": b, "dist": round(km, 1),
            "hours": round((b - a) / 3600, 1), "confidence": "sampling gap",
            "movingSamples": len(moving),
            "vmaxInGap": round(max(v for _, v in moving)) if moving else None,
            "loadSamples": len(load),
            "evidenceFrom": min(times) if times else None,
            "evidenceTo": max(times) if times else None,
            "timing": "partial" if times else "none",
        })
    return trips, movement_gaps, round(sum(e[2] for e in edges), 1)


def build(export_paths, out_path, price_kwh=None, currency="€", csv_dir=None,
          include_identifiers=False, vehicle_title=None, pack_kwh=None, utc_offset=None,
          language="en"):
    language = str(language).lower()
    if language not in {"en", "de", "nl", "lt"}:
        raise ValueError("language must be one of: en, de, nl, lt")
    # Merge any number of exports (VW purges history, so feeding every export
    # you ever request builds an archive deeper than any single package).
    recs, seen, vin = [], set(), None
    export_paths = sorted(export_paths, key=os.path.getmtime)
    for source_i, path in enumerate(export_paths):
        with open(path) as f:
            export = json.load(f)
        vin = export.get("vin", vin)
        fresh = 0
        # Normalise records once at ingestion: real-world exports (e.g. Škoda)
        # can omit "value" or "key" on individual records, and every consumer
        # downstream assumes all four fields exist as strings.
        data = export.get("Data") or []
        for r in data:
            field = r.get("dataFieldName")
            if not isinstance(field, str):
                continue
            value = r.get("value")
            value = "" if value is None else str(value)
            # Indexed structured arrays restart at [0]/[1] in every package.
            # Keep those records source-scoped until their stable session/date
            # identity can be reconstructed; otherwise merging exports lets a
            # later array silently overwrite an earlier one at the same index.
            indexed_structured = field.startswith((
                "chargingSession.[", "powerCurve.[",
                "aggregation.day.[", "aggregation.month.["))
            k = (field, value, r.get("timestampUtc"), r.get("key"),
                 source_i if indexed_structured else None)
            if k not in seen:
                seen.add(k)
                recs.append({"dataFieldName": field, "value": value,
                             "timestampUtc": r.get("timestampUtc"), "key": r.get("key"),
                             "_source": source_i})
                fresh += 1
        print(f"loaded {os.path.basename(path)}: {len(data):,} records, {fresh:,} new")
    export_path = export_paths[-1]
    print(f"merged total: {len(recs):,} records for VIN {vin}")

    # ---- auto-detection: model from VIN, timezone from the car's clock ----
    if not vehicle_title:
        vehicle_title = detect_vehicle_title(vin)
        print(f"vehicle: {vehicle_title} (from VIN — override with --vehicle-title)")
    if utc_offset is None:
        utc_offset = detect_utc_offset(recs)
        if utc_offset is None:
            print("note: vehicle clock offset not found in export — using UTC (override with --utc-offset)")
            utc_offset = 0.0
        else:
            print(f"vehicle clock: UTC{utc_offset:+g} (auto-detected)")
    set_timezone(utc_offset)

    # ---- structured charging history (newer export schema) ----------------
    # Some exports (first seen on a Škoda Enyaq) carry no numeric diagnostic
    # channels at all; instead they deliver documented, indexed structures:
    # chargingSession.[i].*, both powerCurve representations and daily/monthly
    # aggregates, plus high-volume value-less event records. Parse those here
    # — a no-op for diagnostic-format exports.
    sess_re = re.compile(r"^chargingSession\.\[(\d+)\]\.(.+)$")
    curve_re = re.compile(r"^powerCurve\.\[(\d+)\]\.timeCurve\.\[(\d+)\]\.(\w+)$")
    soc_curve_re = re.compile(r"^powerCurve\.\[(\d+)\]\.socCurve\.\[(\d+)\]\.(\w+)$")
    curve_id_re = re.compile(r"^powerCurve\.\[(\d+)\]\.sessionId$")
    aggday_re = re.compile(r"^aggregation\.day\.\[(\d+)\]\.(\w+)$")
    aggmonth_re = re.compile(r"^aggregation\.month\.\[(\d+)\]\.(\w+)$")
    sessions_raw = collections.defaultdict(dict)
    curves_raw = collections.defaultdict(dict)
    soc_curves_raw = collections.defaultdict(dict)
    curve_session_ids = {}
    aggday_raw = collections.defaultdict(dict)
    aggmonth_raw = collections.defaultdict(dict)
    activity_by_field = collections.defaultdict(list)
    for r in recs:
        f = r["dataFieldName"]
        m = sess_re.match(f)
        if m:
            sessions_raw[(r["_source"], int(m.group(1)))][m.group(2)] = r["value"]
            continue
        m = curve_re.match(f)
        if m:
            curves_raw[(r["_source"], int(m.group(1)))].setdefault(
                int(m.group(2)), {})[m.group(3)] = r["value"]
            continue
        m = soc_curve_re.match(f)
        if m:
            soc_curves_raw[(r["_source"], int(m.group(1)))].setdefault(
                int(m.group(2)), {})[m.group(3)] = r["value"]
            continue
        m = curve_id_re.match(f)
        if m:
            curve_session_ids[(r["_source"], int(m.group(1)))] = r["value"]
            continue
        m = aggday_re.match(f)
        if m:
            aggday_raw[(r["_source"], int(m.group(1)))][m.group(2)] = r["value"]
            continue
        m = aggmonth_re.match(f)
        if m:
            aggmonth_raw[(r["_source"], int(m.group(1)))][m.group(2)] = r["value"]
            continue
        if f in {"speed", "longTermAverageConsumption", "ignition"} and r["value"] == "":
            t = parse_ts(r.get("timestampUtc"))
            if t is not None:
                activity_by_field[f].append(t)

    # Prefer the dense speed-event stream. The two fallbacks establish that
    # the vehicle was reporting, but their missing values still reveal no
    # speed, consumption or ignition state.
    activity_source = next((f for f in ("speed", "longTermAverageConsumption", "ignition")
                            if activity_by_field[f]), None)
    activity_ts = sorted(set(activity_by_field.get(activity_source, [])))

    # The two arrays currently use matching indices, but sessionId is the
    # documented join key and remains correct if either array is reordered or
    # truncated in a future export.
    session_idx_by_id = {(i[0], s.get("sessionId")): i for i, s in sessions_raw.items()
                         if s.get("sessionId")}
    curves_by_session = collections.defaultdict(dict)
    soc_curves_by_session = collections.defaultdict(dict)
    for curve_i, points in curves_raw.items():
        session_i = session_idx_by_id.get(
            (curve_i[0], curve_session_ids.get(curve_i)), curve_i)
        curves_by_session[session_i] = points
    for curve_i, points in soc_curves_raw.items():
        session_i = session_idx_by_id.get(
            (curve_i[0], curve_session_ids.get(curve_i)), curve_i)
        soc_curves_by_session[session_i] = points

    # The same charging session commonly reappears in overlapping packages,
    # sometimes at a different array index. Merge by the documented sessionId
    # (or, when absent, its observed time/SoC window), preferring the richer
    # curve representation. Re-key to simple integers for the analysis below.
    canonical_sessions = {}
    for group_i in sorted(sessions_raw):
        sess = sessions_raw[group_i]
        identity = (("id", sess.get("sessionId")) if sess.get("sessionId") else
                    ("window", sess.get("startChargingTimestamp"),
                     sess.get("stopChargingTimestamp"), sess.get("startSoc"),
                     sess.get("endSoc")))
        item = canonical_sessions.setdefault(identity, {
            "session": {}, "curve": {}, "socCurve": {},
            "curveScore": -1, "socCurveScore": -1,
        })
        item["session"].update({k: v for k, v in sess.items() if v != ""})
        curve = curves_by_session.get(group_i, {})
        curve_score = sum(len(p) for p in curve.values())
        if curve_score >= item["curveScore"]:
            item["curve"], item["curveScore"] = curve, curve_score
        soc_curve = soc_curves_by_session.get(group_i, {})
        soc_curve_score = sum(len(p) for p in soc_curve.values())
        if soc_curve_score >= item["socCurveScore"]:
            item["socCurve"], item["socCurveScore"] = soc_curve, soc_curve_score

    sessions_raw = {}
    curves_by_session = collections.defaultdict(dict)
    soc_curves_by_session = collections.defaultdict(dict)
    for i, item in enumerate(canonical_sessions.values()):
        sessions_raw[i] = item["session"]
        curves_by_session[i] = item["curve"]
        soc_curves_by_session[i] = item["socCurve"]
    curves_raw = {i: points for i, points in curves_by_session.items() if points}
    soc_curves_raw = {i: points for i, points in soc_curves_by_session.items() if points}

    def dedupe_aggregates(raw, chars):
        by_date = {}
        for group_i in sorted(raw):
            entry = raw[group_i]
            date = (entry.get("date") or "")[:chars]
            if date:
                by_date[date] = entry
        return {i: entry for i, entry in enumerate(by_date.values())}

    aggday_raw = dedupe_aggregates(aggday_raw, 10)
    aggmonth_raw = dedupe_aggregates(aggmonth_raw, 7)

    structured_soc = []
    for tc in curves_by_session.values():
        for j, p in tc.items():
            t = parse_ts(p.get("timestamp"))
            v = num(p.get("soc", ""))
            if t is not None and v is not None and 0 <= v <= 100:
                structured_soc.append((t, v))
    for sess in sessions_raw.values():
        for tk, sk in (("startChargingTimestamp", "startSoc"),
                       ("stopChargingTimestamp", "endSoc")):
            t = parse_ts(sess.get(tk))
            v = num(sess.get(sk, ""))
            if t is not None and v is not None and 0 <= v <= 100:
                structured_soc.append((t, v))
    structured_export = bool(sessions_raw or curves_raw or aggday_raw or aggmonth_raw)
    if structured_export or activity_ts:
        print(f"structured export schema: {len(sessions_raw)} reported charging sessions, "
              f"{len(curves_raw)} power curves, {len(aggday_raw)} daily and "
              f"{len(aggmonth_raw)} monthly charge aggregates, "
              f"{len(activity_ts):,} value-less {activity_source or 'activity'} events")

    # ---- core diagnostic series (cleaned) ---------------------------------
    odo = series(recs, "180876", lo=1000, hi=500000)
    med = sorted(v for _, v in odo)[len(odo) // 2] if odo else 0
    odo = collapse_median([(t, v) for t, v in odo if abs(v - med) <= 20000])
    soc_raw = series(recs, "180886", lo=0, hi=100)
    # 0 is this channel's error value (isolated 0-readings between normal ones);
    # drop those first so a real value next to a glitch isn't despiked away.
    soc = [p for i, p in enumerate(soc_raw)
           if not (p[1] == 0
                   and (i == 0 or soc_raw[i - 1][1] > 5)
                   and (i == len(soc_raw) - 1 or soc_raw[i + 1][1] > 5))]
    soc = collapse_median(despike(soc, 25))
    if len(soc) < len(soc_raw):
        print(f"dropped {len(soc_raw) - len(soc)} SoC glitch readings")
    if structured_soc:
        soc = collapse_median(sorted(soc + structured_soc))
    ambient = [(t, v - 273.15) for t, v in series(recs, "180806", lo=233, hi=328)]
    speed = collapse_median(series(recs, "545620", lo=0, hi=250))
    cell_max = collapse_median(series(recs, "543765", lo=2500, hi=4500))
    cell_min = collapse_median(series(recs, "545776", lo=2500, hi=4500))
    current = collapse_median(series(recs, "546774", lo=-800, hi=400))
    coolant_flow = collapse_median(series(recs, "546697", lo=0, hi=20))
    thermal = {channel: collapse_median(series(recs, channel, lo=-40, hi=100))
               for channel in THERMAL_CHANNELS}

    all_ts = [t for t, _ in odo] + [t for t, _ in soc] + [t for t, _ in speed]
    all_ts += [t for t, _ in cell_max]
    all_ts += activity_ts
    all_ts = sorted(set(all_ts))
    if not recs:
        sys.exit("no records found in the export — is this a Data Act portal JSON?")
    # A package with records but zero time-series content is the portal's
    # known incomplete-delivery failure. Don't hard-fail: build a
    # snapshot-only dashboard and tell the user to re-request and complain.
    package_incomplete = not all_ts
    if package_incomplete:
        raw_times = sorted(t for t in (parse_ts(r.get("timestampUtc")) for r in recs) if t)
        t_min = raw_times[0] if raw_times else 0
        t_max = raw_times[-1] if raw_times else 0
        print("WARNING: this package contains no diagnostic or charging history at all — "
              "a known incomplete delivery by VW's portal, not a problem with the car. "
              "Building a snapshot-only dashboard; request the export again and consider "
              "complaining through the portal's contact form.")
    else:
        t_min, t_max = all_ts[0], all_ts[-1]

    # ---- pack capacity: measured from charging sessions when possible -----
    charge_runs = detect_charge_runs(soc)
    cap_estimates = estimate_pack_capacity(charge_runs, current, cell_max, cell_min)
    capacity_method = "integrated" if cap_estimates else None
    if not cap_estimates and sessions_raw:
        # No battery-current channel to integrate. Keep reported session energy
        # / SoC gained as a descriptive proxy, but never promote it to usable
        # capacity or SoH: the export does not document the energy measurement
        # point and AC/DC sessions show materially different ratios.
        for i in sorted(sessions_raw):
            sess = sessions_raw[i]
            a = parse_ts(sess.get("startChargingTimestamp"))
            b = parse_ts(sess.get("stopChargingTimestamp"))
            dsoc = num(sess.get("deltaSoc", ""))
            kwh = num(sess.get("totalEnergyCharged", ""))
            sf = num(sess.get("startSoc", ""))
            st = num(sess.get("endSoc", ""))
            if a is None or b is None or dsoc is None or kwh is None:
                continue
            if dsoc < 30 or kwh <= 0:
                continue
            cap = kwh / dsoc * 100
            if 20 <= cap <= 130:
                ctype = (sess.get("chargeType") or "").strip()
                cap_estimates.append({
                    "start": a, "dsoc": round(dsoc),
                    "socFrom": round(sf) if sf is not None else None,
                    "socTo": round(st) if st is not None else None,
                    "hours": round((b - a) / 3600, 1),
                    "coveragePct": None,
                    "samples": len(curves_by_session.get(i, {})) or None,
                    "kwhIn": round(kwh, 1), "capKwh": round(cap, 1),
                    "type": {"DC": "DC fast", "AC": "AC"}.get(ctype, ctype or "?")})
        if cap_estimates:
            capacity_method = "reported_proxy"

    # Physical plausibility: usable capacity can never exceed the largest pack
    # this model shipped with. For direct battery-terminal integration, reject
    # estimates above it plus 5% tolerance. For reported-energy proxies, merely
    # flag them: one-sided trimming would bias the already-uncertain median.
    pack_opts = pack_options_for_vin(vin)
    pack_max = max(pack_opts)
    for e in cap_estimates:
        e["abovePackMax"] = e["capKwh"] > pack_max
        e["plausible"] = e["capKwh"] <= pack_max * 1.05
        e["usedForMedian"] = e["plausible"] if capacity_method == "integrated" else True
    used_caps = [e["capKwh"] for e in cap_estimates if e["usedForMedian"]]
    above_pack = sum(e["abovePackMax"] for e in cap_estimates)
    excluded_caps = sum(not e["usedForMedian"] for e in cap_estimates)
    capacity_proxy = None
    if capacity_method == "reported_proxy":
        by_type = []
        for ctype in sorted({e["type"] for e in cap_estimates}):
            values = [e["capKwh"] for e in cap_estimates if e["type"] == ctype]
            by_type.append({"type": ctype, "sessions": len(values),
                            "medianKwh": round(median(values), 1)})
        capacity_proxy = {
            "medianKwh": round(median(used_caps), 1), "sessions": len(used_caps),
            "abovePackMax": above_pack, "byType": by_type,
        }
        measured_kwh = None
        print(f"charging-energy/SoC proxy: {capacity_proxy['medianKwh']:g} kWh median "
              f"across {len(used_caps)} sessions; not used as battery capacity or SoH"
              + (f" ({above_pack} session(s) exceed the {pack_max:g} kWh pack maximum)"
                 if above_pack else ""))
    else:
        if excluded_caps:
            print(f"excluded {excluded_caps} capacity estimate(s) above the "
                  f"{pack_max:g} kWh pack maximum plus 5% measurement tolerance")
        measured_kwh = round(median(used_caps), 1) if used_caps else None

    global PACK_KWH_USABLE
    if pack_kwh is not None:
        nominal_kwh = pack_kwh
    elif measured_kwh:
        nominal_kwh = pick_nominal_pack(measured_kwh, pack_opts)
    elif capacity_proxy:
        nominal_kwh = pick_nominal_pack(capacity_proxy["medianKwh"], pack_opts)
    else:
        nominal_kwh = PACK_KWH_USABLE

    if pack_kwh is not None:
        PACK_KWH_USABLE = pack_kwh
        pack_source = "set with --pack-kwh"
    elif measured_kwh:
        PACK_KWH_USABLE = measured_kwh
        pack_source = f"measured from {len(used_caps)} battery-terminal charging session(s)"
    elif capacity_proxy:
        PACK_KWH_USABLE = nominal_kwh
        pack_source = "nominal pack inferred from model and charging-energy proxy"
    else:
        pack_source = "default assumption — pass --pack-kwh to correct"

    pack_note = None
    if measured_kwh and pack_kwh is None:
        pack_note = (f"{vehicle_title} shipped with {' / '.join(str(p) for p in pack_opts)} kWh "
                     f"usable packs — the measured capacity matches the {nominal_kwh:g} kWh pack")
    elif capacity_proxy and pack_kwh is None:
        pack_note = (f"{vehicle_title} shipped with {' / '.join(str(p) for p in pack_opts)} kWh "
                     f"usable packs — the charging-energy proxy is consistent with the "
                     f"{nominal_kwh:g} kWh variant, but cannot measure remaining capacity")
    print(f"usable pack capacity: {PACK_KWH_USABLE:g} kWh ({pack_source})"
          + (f"; nominal {nominal_kwh:g} kWh of options {pack_opts}"
             if measured_kwh or capacity_proxy else ""))

    # ---- distance and trip coverage ---------------------------------------
    by_day = collections.defaultdict(list)
    for t, v in odo:
        by_day[to_local(t).date()].append(v)
    daily_parts = collections.defaultdict(lambda: {"km": 0.0, "gapKm": 0.0})
    for (t0, v0), (t1, v1) in zip(odo, odo[1:]):
        delta = v1 - v0
        if not 0 < delta <= 500:
            continue
        day = to_local(t1).date()
        daily_parts[day]["km"] += delta
        if t1 - t0 > TRIP_SAMPLE_GAP_S:
            daily_parts[day]["gapKm"] += delta
    daily = [{
        "d": d.isoformat(), "km": round(daily_parts[d]["km"], 1),
        "gapKm": round(daily_parts[d]["gapKm"], 1),
    } for d in sorted(by_day)]
    trips, movement_gaps, distance_total = detect_trips(odo, soc, speed, current, ambient)
    distance_observed = round(sum(t["dist"] for t in trips), 1)
    print(f"detected {len(trips)} observed trips covering {distance_observed:g} km; "
          f"{sum(g['dist'] for g in movement_gaps):g} km falls inside sampling gaps")

    # ---- charge events (runs of rising SoC) -------------------------------
    charges = charge_runs
    charge_rows = []
    for a, va, b, vb in charges:
        dur_h = (b - a) / 3600
        kw = (vb - va) / 100 * PACK_KWH_USABLE / dur_h if dur_h > 0 else 0
        charge_rows.append({
            "start": a, "end": b, "socFrom": round(va), "socTo": round(vb),
            "kwh": round((vb - va) / 100 * PACK_KWH_USABLE, 1),
            "kw": round(kw, 1),
            "confidence": "derived",
            # sparse sampling underestimates short DC stops, so the DC bar is low
            "type": "DC fast" if kw >= 18 else ("AC" if kw >= 2.5 else "Slow / scheduled"),
        })

    # ---- per-session charging power curves --------------------------------
    # power at the battery = charge current x pack voltage, 5-min buckets;
    # shows taper and pauses that start/end averages hide
    cmin_d = dict(cell_min)
    volt_map = {t: (v + cmin_d[t]) / 2 / 1000 * MEB_SERIES_CELLS
                for t, v in cell_max if t in cmin_d}
    volt_ts_sorted = sorted(volt_map)

    def vpack_at(t):
        i = bisect.bisect_left(volt_ts_sorted, t)
        cands = [volt_ts_sorted[j] for j in (i - 1, i) if 0 <= j < len(volt_ts_sorted)]
        if not cands:
            return None
        tv = min(cands, key=lambda x: abs(x - t))
        return volt_map[tv] if abs(tv - t) <= 900 else None

    for row, (a, _va, b, _vb) in zip(charge_rows, charges):
        pw = []
        for t, amp in current:
            if a <= t <= b and amp > 0:
                v = vpack_at(t)
                if v:
                    pw.append((t, amp * v / 1000))
        row["powerCurve"] = round2(bucket_mean(pw, 300))
        row["peakKw"] = round(max(v for _, v in pw), 1) if pw else None

    # ---- charge type from the power-curve shape ---------------------------
    # Average power misreads tapered DC stops and boosted AC; when a curve
    # exists, classify from peak and median plateau instead. MEB AC tops out
    # at ~11 kW at the battery, so a 20 kW peak is a safe DC discriminator.
    for row in charge_rows:
        if row.get("typeBasis") == "reported":
            continue
        curve = row["powerCurve"]
        if len(curve) >= 3 and row["peakKw"] is not None:
            plateau = median([p for _, p in curve])
            row["type"] = ("DC fast" if row["peakKw"] >= 20
                           else "AC" if plateau >= 2.5 else "Slow / scheduled")
            row["typeBasis"] = "power curve"
        else:
            row["typeBasis"] = "average power"

    # structured exports: the vehicle's own session records replace inference
    structured_sessions = []
    for i in sorted(sessions_raw):
        sess = sessions_raw[i]
        a = parse_ts(sess.get("startChargingTimestamp"))
        b = parse_ts(sess.get("stopChargingTimestamp"))
        if a is None or b is None or b < a:
            continue
        sf = num(sess.get("startSoc", ""))
        st = num(sess.get("endSoc", ""))
        kwh = num(sess.get("totalEnergyCharged", ""))
        avg_kw = num(sess.get("averageChargePower", ""))
        peak_kw = num(sess.get("peakChargePower", ""))
        active_s = num(sess.get("activeChargingTime", ""))
        connected_at = parse_ts(sess.get("connectionTimestamp"))
        disconnected_at = parse_ts(sess.get("disconnectionTimestamp"))
        ctype = (sess.get("chargeType") or "").strip()
        charge_modes = []
        for key in sorted(sess):
            if re.match(r"^chargeMode\.\[\d+\]$", key) and sess[key] not in charge_modes:
                charge_modes.append(sess[key])
        pcurve = []
        for j in sorted(curves_by_session.get(i, {})):
            p = curves_by_session[i][j]
            t = parse_ts(p.get("timestamp"))
            kw = num(p.get("chargePower", ""))
            if t is not None and kw is not None:
                pcurve.append((t, kw))
        soc_curve = []
        for j in sorted(soc_curves_by_session.get(i, {})):
            p = soc_curves_by_session[i][j]
            sv = num(p.get("soc", ""))
            kw = num(p.get("chargePower", ""))
            if sv is not None and kw is not None and 0 <= sv <= 100:
                soc_curve.append((sv, kw))
        structured_sessions.append({
            "start": a, "end": b,
            "connection": connected_at, "disconnection": disconnected_at,
            "elapsedMin": round((b - a) / 60, 1),
            "activeMin": round(active_s / 60, 1) if active_s is not None else None,
            "connectedMin": (round((disconnected_at - connected_at) / 60, 1)
                             if connected_at is not None and disconnected_at is not None
                             and disconnected_at >= connected_at else None),
            "socFrom": round(sf) if sf is not None else None,
            "socTo": round(st) if st is not None else None,
            "kwh": round(kwh, 1) if kwh is not None else None,
            "kw": round(avg_kw, 1) if avg_kw is not None else None,
            "peakKw": round(peak_kw, 1) if peak_kw is not None else None,
            "confidence": "observed",
            "type": {"DC": "DC fast", "AC": "AC"}.get(ctype, ctype or "?"),
            "typeBasis": "reported",
            "chargeModes": charge_modes,
            "powerCurve": round2(bucket_mean(pcurve, 300)) if pcurve else [],
            "socPowerCurve": round2(soc_curve),
        })
    if structured_sessions:
        structured_sessions.sort(key=lambda x: x["start"])
        charge_rows = structured_sessions

    # tag each capacity estimate with its charging session's (final) type
    type_by_start = {r["start"]: r["type"] for r in charge_rows}
    for e in cap_estimates:
        e["type"] = type_by_start.get(e["start"], e.get("type"))

    # ---- trip energy cross-check: integrate battery current x pack voltage --
    # Second, independent estimator next to the SoC-delta method; also splits
    # traction from regenerated energy. Scaled by current-sample coverage,
    # reported only when coverage is solid.
    def window_energy(a, b):
        win = [(t, i) for t, i in current if a <= t <= b]
        if len(win) < 5 or b <= a:
            return None
        e_wh = traction_wh = regen_wh = covered = 0.0
        for (t0, i0), (t1, i1) in zip(win, win[1:]):
            gap = t1 - t0
            if gap > 300:
                continue
            v = vpack_at((t0 + t1) / 2)
            if v is None:
                continue
            p = (i0 + i1) / 2 * v          # W, signed: negative = discharge
            e_wh += p * gap / 3600
            if p < 0:
                traction_wh += -p * gap / 3600
            else:
                regen_wh += p * gap / 3600
            covered += gap
        frac = covered / (b - a)
        out = {"coverage": frac}
        if frac >= 0.7:
            out["net"] = -e_wh / 1000 / frac
            out["traction"] = traction_wh / 1000 / frac
            out["regen"] = regen_wh / 1000 / frac
        return out

    for t_row in trips:
        res = window_energy(t_row["start"], t_row["end"])
        t_row["ivCoveragePct"] = round(res["coverage"] * 100) if res else None
        if res and "net" in res:
            t_row["ivKwh"] = round(res["net"], 2)
            t_row["tractionKwh"] = round(res["traction"], 2)
            t_row["regenKwh"] = round(res["regen"], 2)
            t_row["ivKwh100"] = (round(res["net"] / t_row["dist"] * 100, 1)
                                 if t_row["dist"] >= 5 and res["net"] > 0 else None)
        else:
            t_row["ivKwh"] = t_row["tractionKwh"] = t_row["regenKwh"] = None
            t_row["ivKwh100"] = None

    # pack voltage as its own derived series (mean cell x 96)
    volt_pts = sorted(volt_map.items())

    # ---- daily energy consumption while driving ---------------------------
    # sum SoC decreases between samples < 30 min apart (car awake = driving);
    # only days with enough distance and drop for a meaningful estimate
    drop_day = collections.defaultdict(float)
    for (t0, v0), (t1, v1) in zip(soc, soc[1:]):
        if v0 > v1 and t1 - t0 < 1800:
            drop_day[to_local(t1).date()] += v0 - v1
    amb_day = collections.defaultdict(list)
    for t, v in ambient:
        amb_day[to_local(t).date()].append(v)
    consumption = []
    for d, vs in sorted(by_day.items()):
        km = max(vs) - min(vs)
        drop = drop_day.get(d, 0)
        if km >= 20 and drop >= 3:
            ambs = amb_day.get(d)
            consumption.append({
                "d": d.isoformat(), "km": round(km), "socUsed": round(drop, 1),
                "kwh100": round(drop / 100 * PACK_KWH_USABLE / km * 100, 1),
                "ambientC": round(sum(ambs) / len(ambs), 1) if ambs else None})

    # ---- idle (phantom) drain: SoC lost while parked >= 8 h ---------------
    odo_ts = [t for t, _ in odo]
    odo_v = [v for _, v in odo]

    def odo_at(t):
        i = bisect.bisect_right(odo_ts, t) - 1
        return odo_v[i] if i >= 0 else None

    drain_pairs = []
    for (t0, v0), (t1, v1) in zip(soc, soc[1:]):
        if t1 - t0 < 8 * 3600 or v1 > v0:
            continue
        o0, o1 = odo_at(t0), odo_at(t1)
        if o0 is not None and o1 is not None and o1 - o0 < 2:
            drain_pairs.append([t0, t1, round(v0 - v1, 1)])

    # ---- cell voltage spread (imbalance), daily median --------------------
    cmax_by_t = dict(cell_max)
    spread_raw = []
    spread_day = collections.defaultdict(list)
    for t, v in cell_min:
        if t in cmax_by_t:
            delta = cmax_by_t[t] - v
            if 0 <= delta <= 100:
                spread_raw.append((t, delta))
                spread_day[to_local(t).date()].append(delta)
    spread = [[int(dt.datetime.combine(d, dt.time(12), dt.timezone.utc).timestamp()) - LOCAL_UTC_OFFSET_H * 3600,
               median(vs)]
              for d, vs in sorted(spread_day.items())]
    spread_values = [v for _, v in spread_raw]
    spread_stats = {
        "samples": len(spread_values), "median": round(median(spread_values), 1),
        "p95": round(percentile(spread_values, .95), 1),
        "p99": round(percentile(spread_values, .99), 1),
        "max": round(max(spread_values), 1),
    } if spread_values else {}

    # ---- battery health verdict -------------------------------------------
    health = assess_battery_health(measured_kwh, nominal_kwh, spread_stats,
                                   capacity_proxy=capacity_proxy)
    verdict_txt = {"good": "looks healthy", "fair": "shows normal wear",
                   "attention": "worth checking", "unknown": "not enough data"}[health["verdict"]]
    print(f"battery health: {verdict_txt}"
          + (f" (SoH ~{health['sohPct']}%)" if health.get("sohPct") else ""))

    # pair each spread sample with the concurrent battery current (<=10 min)
    # so the dashboard can show imbalance under load / regen / charge / idle
    cur_ts_sorted = [t for t, _ in current]
    cur_vals = [v for _, v in current]
    spread_cur_raw = []
    for t, d in spread_raw:
        i = bisect.bisect_left(cur_ts_sorted, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(cur_ts_sorted)]
        if not cands:
            continue
        j = min(cands, key=lambda j: abs(cur_ts_sorted[j] - t))
        if abs(cur_ts_sorted[j] - t) <= 600:
            spread_cur_raw.append([t, round(d, 1), round(cur_vals[j], 1)])

    spread_soc = collections.defaultdict(list)
    spread_soc_raw = []
    for t, value in spread_raw:
        sv = nearest_value(soc, t)
        if sv is not None:
            band = min(80, int(sv // 20) * 20)
            spread_soc[band].append(value)
            spread_soc_raw.append([t, round(value, 1), round(sv, 1)])
    spread_by_soc = [{
        "band": f"{band}–{band + 20}%", "samples": len(values),
        "median": round(median(values), 1),
        "p95": round(percentile(values, .95), 1),
    } for band, values in sorted(spread_soc.items())]

    # ---- battery current and thermal telemetry ----------------------------
    current_values = [v for _, v in current]
    current_stats = {
        "samples": len(current_values), "min": round(min(current_values), 1),
        "p05": round(percentile(current_values, .05), 1),
        "median": round(median(current_values), 1),
        "p95": round(percentile(current_values, .95), 1),
        "max": round(max(current_values), 1),
    } if current_values else {}
    thermal_summary = []
    for channel, label in THERMAL_CHANNELS.items():
        values = [v for _, v in thermal[channel]]
        if values:
            thermal_summary.append({
                "channel": channel, "label": label, "samples": len(values),
                "min": round(min(values), 1), "median": round(median(values), 1),
                "p95": round(percentile(values, .95), 1), "max": round(max(values), 1),
            })

    # ---- thermal mode samples (share computed client-side per range) ------
    mode_names = []
    mode_idx = {}
    mode_pts = []
    for r in recs:
        if r["dataFieldName"] != "543919":
            continue
        m = r["value"].strip()
        t = parse_ts(r.get("timestampUtc"))
        if t is None or m == "Init":
            continue
        if m not in mode_idx:
            mode_idx[m] = len(mode_names)
            mode_names.append({"raw": m, "label": THERMAL_MODE_LABELS.get(m, m)})
        mode_pts.append((t, mode_idx[m]))
    mode_pts.sort()

    # ---- coolant valve actuation (inferred enum channels) -----------------
    # Ventil_(nicht_)angesteuert edges; reduced to state transitions so the
    # payload stays small even with dense sampling.
    valves = []
    for valve_ch, valve_label in (("543814", "Coolant valve 543814"),
                                  ("544790", "Coolant valve 544790")):
        vpts = []
        for r in recs:
            if r["dataFieldName"] != valve_ch:
                continue
            t = parse_ts(r.get("timestampUtc"))
            if t is None:
                continue
            raw = r["value"].strip()
            state = 1 if raw == "Ventil_angesteuert" else (
                0 if raw == "Ventil_nicht_angesteuert" else None)
            if state is None:
                continue
            vpts.append((t, state))
        vpts.sort()
        transitions = []
        prev_state = None
        for t, stv in vpts:
            if prev_state is None or stv != prev_state:
                transitions.append([t, stv])
                prev_state = stv
        on = sum(1 for _, v2 in vpts if v2 == 1)
        valves.append({
            "channel": valve_ch, "label": valve_label, "samples": len(vpts),
            "onPct": round(on / len(vpts) * 100, 1) if vpts else None,
            "lastT": vpts[-1][0] if vpts else None,
            "transitions": transitions,
        })

    # ---- legacy activity sessions (kept for diagnostic-format CSVs) -------
    # Value-less structured events are reporting evidence, not trip or charge
    # state. Do not turn them into authoritative-looking pseudo-sessions.
    sessions = ([] if structured_export and not odo and not speed
                else detect_sessions(all_ts, odo, soc, speed))

    # ---- latest snapshot values -------------------------------------------
    counts = collections.Counter(r["dataFieldName"] for r in recs)
    # Documented snapshot/configuration fields are non-numeric. Do not cap
    # them by record count: a long archive assembled from many exports can
    # legitimately contain more than nine snapshots of the same field.
    snap_recs = [r for r in recs if not r["dataFieldName"].isdigit()]

    def snap(pattern):
        rx = re.compile(pattern)
        best = None
        for r in snap_recs:
            if rx.search(r["dataFieldName"]):
                t = parse_ts(r.get("timestampUtc")) or 0
                if best is None or t >= best[0]:
                    best = (t, r["value"].strip())
        return best

    def snap_val(pattern):
        b = snap(pattern)
        return b[1] if b else None

    def snap_item(pattern):
        """Snapshot value plus its own capture time.

        Snapshot groups are assembled from several backend reports that can be
        minutes or weeks apart, so a single card-wide timestamp is not enough.
        """
        b = snap(pattern)
        return {"value": b[1], "time": b[0] or None} if b else None

    def item_value(item):
        return item["value"] if item else None

    def latest_item_time(*items):
        times = [item["time"] for item in items if item and item.get("time")]
        return max(times) if times else None

    status = {
        "odometer": snap_val(r"^mileage_info\.value$"),
        "soc": snap_val(r"^batteryStatus\.currentSOC_pct$") or snap_val(r"^hvsoc_info\.value$"),
        "range": snap_val(r"cruisingRange\.range$") or snap_val(r"^cruise_range_primary_info\.value$"),
        "targetSoc": snap_val(r"^targetSoc_pct$"),
        "careMode": snap_val(r"^careMode$"),
        "battTempMin": snap_val(r"hvbatterytemperature_info\.min_temperature\.value$"),
        "battTempMax": snap_val(r"hvbatterytemperature_info\.max_temperature\.value$"),
        "outdoorTemp": snap_val(r"^outdoortemperature_info\.value$"),
        "chargeState": snap_val(r"^chargingStatus\.currentChargeState$"),
        "chargePower": snap_val(r"^chargingStatus\.chargePower_kW$"),
        "chargeModeSel": snap_val(r"^chargeModeSelection$"),
        "plugConn": snap_val(r"plugStatusItem\.plugConnectionState$"),
        "plugLock": snap_val(r"plugStatusItem\.plugLockState$"),
        "climaTarget": snap_val(r"targetTemperature\.temperature$"),
        "windowHeating": snap_val(r"windowHeatingState$"),
        "parkingBrake": snap_val(r"^parking_brake_info\.value$"),
        "serviceDueDays": snap_val(r"service_maintenance_info\.due_in_time\.value$"),
        "serviceType": snap_val(r"service_maintenance_info\.service_type$"),
        "doors": {},
        "closures": [],
        "parkingLights": {
            "left": snap_item(r"^parking_lights_info\.left_status\.value$"),
            "right": snap_item(r"^parking_lights_info\.right_status\.value$"),
        },
        "chargingPolicy": {
            "maxCurrent": snap_item(r"^maxChargingCurrent$"),
            "autoUnlock": snap_item(r"^autoUnlockPlugWhenCharged$"),
            "chargeMode": snap_item(r"^chargingStatus\.chargeMode$"),
            "chargeType": snap_item(r"^chargingStatus\.chargeType$"),
            "actionState": snap_item(r"^chargingStatus\.actionState$"),
            "infrastructure": snap_item(r"plugStatusItem\.infrastructureState$"),
            "plugType": snap_item(r"plugStatusItem\.chargingPlugType$"),
            "v2hDischarge": snap_item(r"^immediate_discharging$"),
            "v2hHomeStorage": snap_item(r"^home_storage_charging$"),
        },
        "platform": snap_val(r"^vehiclePlatform$"),
        "careThreshold": snap_val(r"^state\.threshold$"),
        "careNotification": snap_val(r"^state\.notification$"),
        "aux": {
            "residual": snap_item(r"^residualConsumption$"),
            "interiorClima": snap_item(r"^interiorClimatizationConsumption$"),
            "batteryClima": snap_item(r"^batteryClimatizationConsumption$"),
            "budgetStartLevel": snap_item(r"^budgetStartBatteryLevel$"),
            "warnedPower": snap_item(r"^hasWarnedPowerLevel$"),
            "warnedBudget": snap_item(r"^hasWarnedDailyPowerBudget$"),
        },
        "climate": {
            "target": snap_item(r"targetTemperature\.temperature$"),
            "windowHeating": snap_item(r"windowHeatingState$"),
            "state": snap_item(r"^envelope\.\[\d+\]\.report\.status$"),
            "trigger": snap_item(r"^envelope\.\[\d+\]\.report\.trigger$"),
            "withoutExternalPower": snap_item(r"climatizationWithoutExternalPower$"),
            "atUnlock": snap_item(r"climatizationElementSettings\.isClimatizationAtUnlock$"),
            "zoneFrontLeft": snap_item(r"climatizationElementSettings\.zoneFrontLeftEnabled$"),
            "zoneFrontRight": snap_item(r"climatizationElementSettings\.zoneFrontRightEnabled$"),
            "zoneRearLeft": snap_item(r"climatizationElementSettings\.zoneRearLeftEnabled$"),
            "zoneRearRight": snap_item(r"climatizationElementSettings\.zoneRearRightEnabled$"),
            "timerEnabled": snap_item(r"report\.timers\.isEnabled$"),
            "chargeTimerOption": snap_item(r"^timer_charging$"),
            "chargeClimateTimerOption": snap_item(r"^timer_charging_climatization$"),
            "timerIds": [],
            "timerIdsCapturedAt": None,
        },
        "connectivity": {
            "vehicleConnected": snap_item(r"^isConnected$"),
            "activeDomains": snap_item(r"^activeDomains$"),
            "osShutdown": snap_item(r"^osShutdown$"),
            "v2x": snap_item(r"^352341537-0-36$"),
            "lastConnection": None,
        },
        "capturedAt": None,
    }

    # V2X is a documented hex-encoded on/off setting. Decode it with the same
    # conservative logic used by the full configuration table.
    if status["connectivity"]["v2x"]:
        status["connectivity"]["v2x"]["value"] = decode_config_value(
            status["connectivity"]["v2x"]["value"],
            BUNDLED_FIELD_DESCRIPTIONS.get("352341537-0-36", ""))

    # These fields have no timestampUtc; connectionTimestamp is itself an
    # epoch timestamp in milliseconds and is the only defensible time to show.
    connection_raw = snap_val(r"^connectionTimestamp$")
    if connection_raw:
        try:
            connection_epoch = int(float(connection_raw))
            if connection_epoch > 10 ** 11:
                connection_epoch //= 1000
            status["connectivity"]["lastConnection"] = connection_epoch
        except ValueError:
            pass

    # The flattened export can deliver several timer IDs but only one
    # unindexed isEnabled value. Preserve every ID without claiming which one
    # the state belongs to.
    timer_ids = []
    timer_times = []
    timer_rx = re.compile(r"^envelope\.\[\d+\]\.report\.timers\.id$")
    for r in snap_recs:
        if not timer_rx.search(r["dataFieldName"]):
            continue
        value = r["value"].strip()
        if value not in timer_ids:
            timer_ids.append(value)
        t = parse_ts(r.get("timestampUtc"))
        if t:
            timer_times.append(t)
    timer_ids.sort(key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    status["climate"]["timerIds"] = timer_ids
    status["climate"]["timerIdsCapturedAt"] = max(timer_times) if timer_times else None

    for pos in ("front_left", "front_right", "rear_left", "rear_right"):
        label = pos.replace("_", " ")
        door_state = snap_item(rf"^door_info\.{pos}\.door_status\.value$")
        door_lock = snap_item(rf"^door_info\.{pos}\.door_lock_status\.value$")
        status["doors"]["door " + label] = item_value(door_state)
        if door_state or door_lock:
            status["closures"].append({
                "label": label + " door", "state": item_value(door_state),
                "lock": item_value(door_lock), "openPct": None,
                "time": latest_item_time(door_state, door_lock),
            })

        window_state = snap_item(rf"^window_info\.{pos}\.window_status\.value$")
        window_pct = snap_item(rf"^window_info\.{pos}\.window_percentage_open\.value$")
        if window_state:
            status["doors"]["window " + label] = item_value(window_state)
        if window_state or window_pct:
            status["closures"].append({
                "label": label + " window", "state": item_value(window_state),
                "lock": None, "openPct": item_value(window_pct),
                "time": latest_item_time(window_state, window_pct),
            })

    for label, state_pattern, lock_pattern in (
            ("hood", r"^hood_info\.hood_status\.value$", r"^hood_info\.hood_lock_status\.value$"),
            ("trunk", r"^trunk_lid_info\.trunk_lid_status\.value$",
             r"^trunk_lid_info\.trunk_lid_lock_status\.value$")):
        state_item = snap_item(state_pattern)
        lock_item = snap_item(lock_pattern)
        status["doors"][label] = item_value(state_item)
        if state_item or lock_item:
            status["closures"].append({
                "label": label, "state": item_value(state_item),
                "lock": item_value(lock_item), "openPct": None,
                "time": latest_item_time(state_item, lock_item),
            })
    cap = snap(r"^mileage_info\.value$")
    status["capturedAt"] = cap[0] if cap else None

    # ---- data inventory ----------------------------------------------------
    field_key = {}
    key_to_field = {}
    first_last = {}
    sample = {}
    latest_record = {}
    for r in recs:
        f = r["dataFieldName"]
        field_key.setdefault(f, r["key"])
        key_to_field.setdefault(r["key"], f)
        shown = r["value"].strip()[:48]
        if not include_identifiers and is_sensitive_field(f):
            shown = "[redacted]"
        sample.setdefault(f, shown)
        t = parse_ts(r.get("timestampUtc"))
        if f not in latest_record or (t or 0) >= latest_record[f][0]:
            latest_record[f] = (t or 0, r)
        if t:
            a, b = first_last.get(f, (t, t))
            first_last[f] = (min(a, t), max(b, t))

    dictionary_info = dict(BUNDLED_DICTIONARY_INFO)

    def normalize_field_name(f):
        return re.sub(r"\.\[\d+\]", ".[*]", f)

    def field_description(f):
        return BUNDLED_FIELD_DESCRIPTIONS.get(normalize_field_name(f), "")

    raw_inventory = []
    for f, n in counts.most_common():
        if f in DIAG_LABELS:
            label, unit, note = DIAG_LABELS[f]
            d = label + (f" [{unit}]" if unit else "")
            d += " — inferred, not in Data Act dictionary" + (f"; {note}" if note else "")
        else:
            d = field_description(f)
        fl = first_last.get(f)
        raw_inventory.append({
            "field": f, "n": n, "desc": d[:220], "sample": sample[f],
            "first": fl[0] if fl else None, "last": fl[1] if fl else None,
        })

    # ---- settings and configuration ---------------------------------------
    config_re = re.compile(r"^\d+-\d+-\d+$")
    setting_terms = (
        "activation status", "user preference", "settings", "configured",
        "specified by the user", "target temperature", "option to start",
        "timer is enabled", "functionality is on or off",
    )
    configuration = []
    for item in raw_inventory:
        f, desc = item["field"], item["desc"]
        if is_sensitive_field(f):
            continue
        if not (config_re.match(f) or any(term in desc.lower() for term in setting_terms)):
            continue
        t, record = latest_record[f]
        raw = record["value"].strip()
        configuration.append({
            "field": f, "time": t or None, "value": decode_config_value(raw, desc),
            "raw": raw[:64], "description": desc[:220],
            "source": "factory default" if raw == "factory setting is used" else "explicit",
        })
    configuration.sort(key=lambda x: ((x["time"] or 0), x["field"]), reverse=True)

    # Array indices turn a compact structured schema into thousands of
    # apparent fields. Group those paths while retaining record totals, raw
    # field counts and one concrete example for discoverability.
    inventory_groups = {}
    for item in raw_inventory:
        normalized = normalize_field_name(item["field"])
        group = inventory_groups.setdefault(normalized, {
            "field": normalized, "n": 0, "variants": 0,
            "desc": item["desc"], "sample": item["sample"],
            "example": item["field"], "first": None, "last": None,
        })
        group["n"] += item["n"]
        group["variants"] += 1
        if not group["sample"] and item["sample"]:
            group["sample"] = item["sample"]
        if item["first"] is not None:
            group["first"] = (item["first"] if group["first"] is None
                              else min(group["first"], item["first"]))
        if item["last"] is not None:
            group["last"] = (item["last"] if group["last"] is None
                             else max(group["last"], item["last"]))
    inventory = sorted(inventory_groups.values(), key=lambda item: (-item["n"], item["field"]))

    # ---- remote actions, reports and errors -------------------------------
    cause_at = collections.defaultdict(set)   # several causes can share a timestamp
    errors_at = collections.defaultdict(list)
    env_backend_ts = {}                       # envelope index -> backendCapturedTimestamp
    for r in recs:
        t = parse_ts(r.get("timestampUtc"))
        f, value = r["dataFieldName"], r["value"].strip()
        if f == "causedBy" and t:
            cause_at[t].add(value)
        if ("errorDescription" in f or f == "vehicleError.errorNumber") and t:
            errors_at[t].append(value)
        m = re.match(r"envelope\.\[(\d+)\]\.context\.backendCapturedTimestamp\.seconds$", f)
        if m:
            try:
                env_backend_ts[m.group(1)] = int(float(value))
            except ValueError:
                pass
    events = []
    for r in recs:
        f, value = r["dataFieldName"], r["value"].strip()
        t = parse_ts(r.get("timestampUtc"))
        if f == "unlock_all":
            events.append({"time": t, "kind": "remote action", "event": "Unlock all doors",
                           "detail": value, "confidence": "observed"})
        elif f == "payloadType":
            detail = " · ".join(sorted(cause_at.get(t, []))) if t else ""
            if errors_at.get(t):
                detail = " · ".join(filter(None, [detail, *errors_at[t]]))
            events.append({
                "time": t, "kind": "error" if "ERROR" in value else "vehicle report",
                "event": value.replace("_", " ").title(), "detail": detail,
                "confidence": "observed",
            })
    for r in recs:
        if r["dataFieldName"].endswith("backendError.errorDescription"):
            t = parse_ts(r.get("timestampUtc"))
            if t is None:
                # undated envelope entries carry a recoverable backend timestamp
                m = re.match(r"envelope\.\[(\d+)\]\.", r["dataFieldName"])
                if m:
                    t = env_backend_ts.get(m.group(1))
            events.append({"time": t, "kind": "error",
                           "event": "Climatization backend error",
                           "detail": r["value"].strip()[:180], "confidence": "observed"})
    events.sort(key=lambda x: x["time"] or 0, reverse=True)

    # ---- completeness evidence --------------------------------------------
    record_times = [parse_ts(r.get("timestampUtc")) for r in recs]
    record_times = [t for t in record_times if t]
    export_keys = {r["key"] for r in recs}
    export_fields = set(counts)
    numeric_fields = {f for f in export_fields if f.isdigit()}
    numeric_records = sum(counts[f] for f in numeric_fields)

    def coverage_row(label, predicate):
        fields = [f for f in export_fields if predicate(f)]
        times = [t for f in fields for t in first_last.get(f, ())]
        return {"label": label, "fields": len(fields),
                "records": sum(counts[f] for f in fields),
                "first": min(times) if times else None, "last": max(times) if times else None}

    coverage = [
        coverage_row("Numeric diagnostics", lambda f: f.isdigit()),
        coverage_row("Configuration", lambda f: bool(config_re.match(f))),
        coverage_row("Remote actions & reports", lambda f: f in {"unlock_all", "payloadType", "causedBy"}
                     or "vehicleError" in f or "backendError" in f),
        coverage_row("Service & maintenance", lambda f: f.startswith("service_maintenance_info")),
        coverage_row("Warnings & DTCs", lambda f: any(x in f.lower() for x in ("warning", "ilfdia", "dtc"))),
    ]
    expected_fields = [{
        "field": term,
        "dictionary": bool(dictionary_info.get("terms", {}).get(term)),
        "export": term in export_fields,
    } for term in dictionary_info.get("terms", {})]
    lower_fields = [f.lower() for f in export_fields]
    not_found = []
    if not any(any(term in f for term in ("latitude", "longitude", "gps", "geolocation"))
               for f in lower_fields):
        not_found.append("GPS or route coordinates")
    if not any("tire" in f or "tyre" in f or "tirepressure" in f for f in lower_fields):
        not_found.append("tyre-pressure values")
    if not any("stateofhealth" in f or f.endswith(".soh") or "batterycapacity" in f
               for f in lower_fields):
        not_found.append("direct battery SOH or capacity")
    if not any(any(term in f for term in ("warning", "ilfdia", "dtc")) for f in lower_fields):
        not_found.append("warning or DTC records")
    if not any("repair" in f or "service_history" in f or "maintenance_history" in f
               for f in lower_fields):
        not_found.append("repair history")
    export_time = parse_ts(re.sub(
        r".*_(\d{8})(\d{6}).*",
        lambda m: f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} "
                  f"{m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}",
        os.path.basename(export_path)))
    completeness = {
        "dictionaryKeys": dictionary_info.get("keys"), "exportKeys": len(export_keys),
        "matchedKeys": sum(1 for f in field_key if field_description(f)),
        "exportFields": len(export_fields),
        "numericFields": len(numeric_fields), "numericRecords": numeric_records,
        "numericPct": round(numeric_records / len(recs) * 100, 3),
        "rawMin": min(record_times) if record_times else None,
        "rawMax": max(record_times) if record_times else None,
        "diagnosticMin": t_min, "diagnosticMax": t_max,
        "diagnosticLagDays": round((export_time - t_max) / 86400, 1) if export_time else None,
        "coverage": coverage, "expectedFields": expected_fields, "notFound": not_found,
    }

    # ---- payload -----------------------------------------------------------
    display_export = os.path.basename(export_path)
    if not include_identifiers and vin:
        display_export = display_export.replace(vin, mask_identifier(vin))
    soc_source = ("mixed" if soc_raw and structured_soc else
                  "structured_charging" if structured_soc else "diagnostic")
    payload = {
        "vin": vin if include_identifiers else mask_identifier(vin),
        "identifiersRedacted": not include_identifiers,
        "packageIncomplete": package_incomplete,
        "structuredExport": structured_export,
        "exportFile": display_export + (
            f" (+{len(export_paths) - 1} more merged)" if len(export_paths) > 1 else ""),
        "exportTime": export_time,
        "tzOffsetH": LOCAL_UTC_OFFSET_H,
        "tzLabel": LOCAL_TZ_LABEL,
        "tMin": t_min, "tMax": t_max,
        "nRecords": len(recs), "nFields": len(counts), "nFieldPatterns": len(inventory),
        "daily": daily,
        "soc": round2(soc),
        "socSource": soc_source,
        "ambient": round2(bucket_mean(ambient, 1800)),
        "cellMax": round2(bucket_mean(cell_max, 1200)),
        "cellMin": round2(bucket_mean(cell_min, 1200)),
        "current": round2(bucket_mean(current, 120)),
        "currentStats": current_stats,
        "thermal": [{"channel": channel, "label": THERMAL_CHANNELS[channel],
                     "pts": round2(bucket_mean(points, 1200))}
                    for channel, points in thermal.items()],
        "thermalSummary": thermal_summary,
        "coolantFlow": round2(bucket_mean(coolant_flow, 600)),
        "packVoltage": round2(bucket_mean(volt_pts, 1200)),
        "valves": valves,
        "odoDaily": [{"d": d.isoformat(), "km": round(max(vs))}
                     for d, vs in sorted(by_day.items())],
        "chargedDaily": sorted(({"d": (e.get("date") or "").split("T")[0],
                                 "kwh": round(num(e.get("chargedEnergy", "")), 1)}
                                for e in aggday_raw.values()
                                if (e.get("date") or "").split("T")[0]
                                and num(e.get("chargedEnergy", "")) is not None),
                               key=lambda x: x["d"]),
        "chargedMonthly": sorted(({"d": (e.get("date") or "")[:7],
                                    "kwh": round(num(e.get("chargedEnergy", "")), 1)}
                                   for e in aggmonth_raw.values()
                                   if len(e.get("date") or "") >= 7
                                   and num(e.get("chargedEnergy", "")) is not None),
                                  key=lambda x: x["d"]),
        "activity": [[t] for t in activity_ts],
        "activitySource": activity_source,
        "activityEventCounts": {f: len(activity_by_field.get(f, []))
                                for f in ("speed", "longTermAverageConsumption", "ignition")},
        "speedRaw": [[t, round(v, 1)] for t, v in speed],
        "modeNames": mode_names,
        "modeRaw": [[t, i] for t, i in mode_pts],
        "charges": charge_rows,
        "consumption": consumption,
        "spread": spread,
        "spreadRaw": round2(spread_raw),
        "spreadStats": spread_stats,
        "spreadBySoc": spread_by_soc,
        "spreadSocRaw": spread_soc_raw,
        "spreadCurRaw": spread_cur_raw,
        "drainPairs": drain_pairs,
        "packKwh": PACK_KWH_USABLE,
        "packSource": pack_source,
        "capacityMethod": capacity_method,
        "capacityProxy": capacity_proxy,
        "measuredKwh": measured_kwh,
        "nominalKwh": nominal_kwh,
        "capEstimates": cap_estimates,
        "packNote": pack_note,
        "health": health,
        "priceKwh": price_kwh,
        "currency": currency,
        "sessions": sessions,
        "trips": trips,
        "movementGaps": movement_gaps,
        "distanceTotal": distance_total,
        "distanceObserved": distance_observed,
        "status": status,
        "events": events,
        "configuration": configuration,
        "completeness": completeness,
        "inventory": inventory,
    }

    data_json = json.dumps(payload, separators=(",", ":"))
    # Safe inside an HTML <script>; escapes resolve when JavaScript parses the literal.
    data_json = data_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    safe_title = vehicle_title.replace("&", "&amp;").replace("<", "&lt;")
    html = TEMPLATE.replace("/*__DATA__*/null", data_json) \
                   .replace("__VEHICLE_TITLE__", safe_title) \
                   .replace("__REPORT_LANGUAGE__", language)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")

    if csv_dir:
        import csv
        os.makedirs(csv_dir, exist_ok=True)

        def wcsv(name, header, rows):
            with open(os.path.join(csv_dir, name), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)

        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
        wcsv("soc.csv", ["timestamp_utc", "soc_pct", "source"],
             [(iso(t), v, soc_source) for t, v in soc])
        wcsv("odometer.csv", ["timestamp_utc", "km"], [(iso(t), v) for t, v in odo])
        wcsv("ambient_temp.csv", ["timestamp_utc", "deg_c"],
             [(iso(t), round(v, 2)) for t, v in ambient])
        wcsv("speed.csv", ["timestamp_utc", "kmh"], [(iso(t), v) for t, v in speed])
        wcsv("cell_voltage.csv", ["timestamp_utc", "max_mv", "min_mv"],
             [(iso(t), v, dict(cell_min).get(t, "")) for t, v in cell_max])
        wcsv("daily_distance.csv", ["date", "km", "km_across_sampling_gaps"],
             [(d["d"], d["km"], d["gapKm"]) for d in daily])
        wcsv("trips.csv", ["start_utc", "end_utc", "km", "max_kmh", "soc_from", "soc_to",
                            "est_kwh_per_100km", "peak_discharge_a", "peak_regen_a", "confidence",
                            "ambient_c", "iv_kwh", "iv_kwh_per_100km", "iv_coverage_pct",
                            "traction_kwh", "regen_kwh"],
             [(iso(t["start"]), iso(t["end"]), t["dist"], t["vmax"], t["socFrom"], t["socTo"],
               t["kwh100"], t["peakDischargeA"], t["peakRegenA"], t["confidence"],
               t["ambientC"], t["ivKwh"], t["ivKwh100"], t["ivCoveragePct"],
               t["tractionKwh"], t["regenKwh"]) for t in trips])
        wcsv("movement_gaps.csv", ["start_utc", "end_utc", "km", "gap_hours", "confidence",
                                   "timing_evidence", "moving_speed_samples", "max_kmh_in_gap",
                                   "discharge_samples", "evidence_from_utc", "evidence_to_utc"],
             [(iso(g["start"]), iso(g["end"]), g["dist"], g["hours"], g["confidence"],
               g["timing"], g["movingSamples"], g["vmaxInGap"], g["loadSamples"],
               iso(g["evidenceFrom"]) if g["evidenceFrom"] else "",
               iso(g["evidenceTo"]) if g["evidenceTo"] else "")
              for g in movement_gaps])
        wcsv("sessions.csv", ["start_utc", "end_utc", "type", "km", "max_kmh", "soc_from", "soc_to"],
             [(iso(s["start"]), iso(s["end"]), s["kind"], s["dist"], s["vmax"],
               s["socFrom"], s["socTo"]) for s in sessions])
        wcsv("charges.csv", ["start_utc", "end_utc", "soc_from", "soc_to", "est_kwh", "est_kw", "type",
                             "type_basis", "connection_utc", "disconnection_utc",
                             "elapsed_minutes", "active_minutes", "connected_minutes", "charge_modes",
                             "peak_kw"],
             [(iso(c["start"]), iso(c["end"]), c["socFrom"], c["socTo"], c["kwh"],
               c["kw"], c["type"], c["typeBasis"],
               iso(c["connection"]) if c.get("connection") is not None else "",
               iso(c["disconnection"]) if c.get("disconnection") is not None else "",
               c.get("elapsedMin", ""), c.get("activeMin", ""), c.get("connectedMin", ""),
               " | ".join(c.get("chargeModes", [])), c.get("peakKw", "")) for c in charge_rows])
        wcsv("charge_power_over_time.csv",
             ["session_start_utc", "type", "timestamp_utc", "charge_kw"],
             [(iso(c["start"]), c["type"], iso(t), kw)
              for c in charge_rows for t, kw in c.get("powerCurve", [])])
        wcsv("charge_power_by_soc.csv", ["session_start_utc", "type", "soc_pct", "charge_kw"],
             [(iso(c["start"]), c["type"], soc_value, kw)
              for c in charge_rows for soc_value, kw in c.get("socPowerCurve", [])])
        wcsv("consumption.csv", ["date", "km", "soc_used_pct", "kwh_per_100km", "ambient_c"],
             [(c["d"], c["km"], c["socUsed"], c["kwh100"], c["ambientC"]) for c in consumption])
        wcsv("battery_current.csv", ["timestamp_utc", "ampere"],
             [(iso(t), v) for t, v in current])
        wcsv("coolant_flow.csv", ["timestamp_utc", "litres_per_min"],
             [(iso(t), v) for t, v in coolant_flow])
        wcsv("thermal_sensors.csv", ["timestamp_utc", "sensor", "channel", "deg_c"],
             [(iso(t), THERMAL_CHANNELS[channel], channel, v)
              for channel, points in thermal.items() for t, v in points])
        wcsv("activity_events.csv", ["timestamp_utc", "kind", "event", "detail", "confidence"],
             [(iso(e["time"]) if e["time"] else "", e["kind"], e["event"], e["detail"], e["confidence"])
              for e in events])
        wcsv("reporting_activity.csv", ["timestamp_utc", "source"],
             [(iso(t), activity_source or "") for t in activity_ts])
        wcsv("configuration.csv", ["timestamp_utc", "field", "value", "raw", "source", "description"],
             [(iso(c["time"]) if c["time"] else "", c["field"], c["value"], c["raw"],
               c["source"], c["description"]) for c in configuration])
        wcsv("pack_voltage.csv", ["timestamp_utc", "volts"],
             [(iso(t), round(v, 2)) for t, v in volt_pts])
        wcsv("charged_daily.csv", ["date", "charged_kwh"],
             [(e["d"], e["kwh"]) for e in payload["chargedDaily"]])
        wcsv("charged_monthly.csv", ["month", "charged_kwh"],
             [(e["d"], e["kwh"]) for e in payload["chargedMonthly"]])
        wcsv("capacity_estimates.csv",
             ["charge_start_utc", "type", "method", "soc_from", "soc_to",
              "delta_soc_pct", "duration_hours", "energy_in_kwh", "current_coverage_pct",
              "samples", "energy_per_soc_gained_kwh", "above_pack_max", "plausible",
              "used_for_median"],
             [(iso(e["start"]), e.get("type", ""), capacity_method or "",
               e.get("socFrom", ""), e.get("socTo", ""), e.get("dsoc", ""),
               e.get("hours", ""), e.get("kwhIn", ""), e.get("coveragePct", ""),
               e.get("samples", ""), e.get("capKwh", ""), e.get("abovePackMax", ""),
               e.get("plausible", ""), e.get("usedForMedian", "")) for e in cap_estimates])
        print(f"wrote cleaned CSVs to {csv_dir}/")
    print(f"data window: {to_local(t_min):%Y-%m-%d} .. {to_local(t_max):%Y-%m-%d} "
          f"({LOCAL_TZ_LABEL}), open the file in any browser")


TEMPLATE = r"""<!doctype html>
<html lang="__REPORT_LANGUAGE__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__VEHICLE_TITLE__ — vehicle data</title>
<style>
:root {
  color-scheme: light;
  --page:#f9f9f7; --surface-1:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7;
  --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#008300; --s3:#a34fba; --s4:#d26400;
  --s5:#007b83; --s6:#6d6f1d; --s7:#9b4054;
  --warn:#fab219; --good:#0ca30c; --crit:#d03b3b;
  --track:#cde2fb; --observed:#12733b; --derived:#2a78d6; --inferred:#9a6700;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:#0d0d0d; --surface-1:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --baseline:#383835;
  --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#19a85b; --s3:#c277d3; --s4:#ec8a36;
  --s5:#34adb5; --s6:#b0b34f; --s7:#d47a8d;
  --warn:#fab219; --good:#0ca30c; --crit:#d03b3b;
  --track:#0d366b; --observed:#43b876; --derived:#6aa7ee; --inferred:#e7b44a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:#0d0d0d; --surface-1:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --baseline:#383835;
    --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#19a85b; --s3:#c277d3; --s4:#ec8a36;
    --s5:#34adb5; --s6:#b0b34f; --s7:#d47a8d;
    --warn:#fab219; --good:#0ca30c; --crit:#d03b3b;
    --track:#0d366b; --observed:#43b876; --derived:#6aa7ee; --inferred:#e7b44a;
  }
}
* { box-sizing:border-box; margin:0; }
body {
  background:var(--page); color:var(--ink);
  font:14px/1.45 "Avenir Next","Segoe UI",system-ui,sans-serif;
  padding:20px clamp(12px,3vw,36px) 60px; max-width:1720px; margin:0 auto;
}
a { color:var(--s1); }
header.page { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px; margin-bottom:18px; }
header.page h1 { font:700 23px/1 "DIN Alternate","Avenir Next Condensed","Avenir Next",sans-serif; letter-spacing:.02em; }
header.page .sub { color:var(--ink-2); font-size:13px; }
#themeBtn {
  margin-left:auto; border:1px solid var(--border); background:var(--surface-1);
  color:var(--ink-2); border-radius:8px; padding:5px 12px; cursor:pointer; font:inherit; font-size:13px;
}
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); margin-bottom:14px; }
.grid.charts { grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
@media (max-width:500px){ .grid.charts { grid-template-columns:1fr; } }
.sectionHead { display:grid; grid-template-columns:minmax(180px,.45fr) minmax(280px,1fr); gap:24px;
  align-items:end; padding-top:30px; margin:12px 0 12px; border-top:1px solid var(--border); }
.sectionHead .eyebrow { color:var(--s1); font:700 11px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;
  letter-spacing:.12em; text-transform:uppercase; }
.sectionHead h2 { font:700 25px/1.05 "DIN Alternate","Avenir Next Condensed","Avenir Next",sans-serif; margin-top:5px; }
.sectionHead p { color:var(--ink-2); max-width:760px; }
.evidenceRail { display:grid; grid-template-columns:repeat(3,1fr); border:1px solid var(--border);
  border-radius:12px; overflow:hidden; background:var(--surface-1); margin-bottom:18px; }
.evidenceRail > div { padding:11px 14px; border-left:4px solid; }
.evidenceRail > div + div { border-top:0; border-left-width:1px; }
.evidenceRail .observed { border-color:var(--observed); }
.evidenceRail .derived { border-color:var(--derived); }
.evidenceRail .inferred { border-color:var(--inferred); }
.evidenceRail strong { display:block; font-size:12px; }
.evidenceRail span { color:var(--muted); font-size:11px; }
@media (max-width:720px){ .sectionHead { grid-template-columns:1fr; gap:5px; }
  .evidenceRail { grid-template-columns:1fr; } .evidenceRail > div + div { border-left-width:4px; border-top:1px solid var(--border); } }
.card {
  background:var(--surface-1); border:1px solid var(--border); border-radius:12px;
  padding:14px 16px; min-width:0;
}
.card h2 { font-size:14px; font-weight:600; }
.card .sub { color:var(--muted); font-size:12px; margin-top:1px; }
.card header { display:flex; align-items:flex-start; gap:10px; margin-bottom:8px; }
.card header .grow { flex:1; min-width:0; }
.prov { display:inline-flex; align-items:center; border-radius:999px; padding:2px 7px;
  font:650 10px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace; text-transform:uppercase;
  letter-spacing:.06em; border:1px solid currentColor; white-space:nowrap; }
.prov.observed { color:var(--observed); } .prov.derived { color:var(--derived); }
.prov.inferred { color:var(--inferred); }
.tblBtn {
  border:1px solid var(--border); background:none; color:var(--muted); font:inherit;
  font-size:12px; border-radius:6px; padding:2px 9px; cursor:pointer; flex:none;
}
.tblBtn[aria-pressed="true"] { color:var(--ink); border-color:var(--baseline); }
.viz { position:relative; }
.viz svg { display:block; width:100%; }
.viz .tt {
  position:absolute; pointer-events:none; background:var(--surface-1); color:var(--ink);
  border:1px solid var(--border); border-radius:8px; box-shadow:0 4px 14px rgba(0,0,0,.18);
  padding:7px 10px; font-size:12px; display:none; z-index:5; max-width:230px;
}
.tt .when { color:var(--muted); margin-bottom:3px; }
.tt .row { display:flex; align-items:center; gap:6px; }
.tt .key { width:12px; height:0; border-top:2.5px solid; flex:none; }
.tt .v { font-weight:650; }
.tt .n { color:var(--ink-2); }
.legend { display:flex; gap:14px; font-size:12px; color:var(--ink-2); margin-top:6px; flex-wrap:wrap; }
.legend button, .legend > span { display:flex; align-items:center; gap:6px; background:none; border:none; color:inherit; font:inherit; padding:0; }
.legend button { cursor:pointer; }
.legend button[aria-pressed="false"] { opacity:.35; }
.legend .key { width:14px; border-top:2.5px solid; }
.filters { display:flex; gap:8px; align-items:center; margin:20px 0 14px; flex-wrap:wrap; }
.filters .lbl { color:var(--muted); font-size:12px; margin-right:2px; }
.filters button {
  border:1px solid var(--border); background:var(--surface-1); color:var(--ink-2);
  font:inherit; font-size:13px; border-radius:8px; padding:5px 13px; cursor:pointer;
}
.filters button[aria-pressed="true"] { color:var(--ink); border-color:var(--s1); box-shadow:inset 0 0 0 1px var(--s1); }
.kpi .label { color:var(--ink-2); font-size:13px; }
.kpi .value { font-size:30px; font-weight:600; margin-top:2px; }
.kpi .value small { font-size:15px; font-weight:500; color:var(--ink-2); }
.kpi .ctx { color:var(--muted); font-size:12px; margin-top:2px; }
.kpi svg { margin-top:6px; }
.rows { display:grid; gap:6px; font-size:13px; }
.rows .r { display:flex; justify-content:space-between; gap:12px; }
.rows .r .k { color:var(--ink-2); }
.rows .r .v { font-weight:550; text-align:right; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }
.chip {
  display:inline-flex; align-items:center; gap:5px; border:1px solid var(--border);
  border-radius:999px; padding:2px 10px 2px 8px; font-size:12px; color:var(--ink-2);
}
.chip .dot { width:7px; height:7px; border-radius:50%; background:var(--baseline); }
.chip.warn { border-color:var(--warn); color:var(--ink); }
.chip.warn .dot { background:var(--warn); }
.meter { margin:8px 0 4px; }
.meter .track { position:relative; height:10px; border-radius:5px; background:var(--track); overflow:visible; }
.meter .fill { position:absolute; inset:0 auto 0 0; border-radius:5px; background:var(--s1); }
.meter .tick { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--ink-2); }
.meter .lbls { display:flex; justify-content:space-between; color:var(--muted); font-size:11px; margin-top:4px; }
.foot { color:var(--muted); font-size:11px; margin-top:10px; }
table.dv { border-collapse:collapse; width:100%; font-size:12.5px; }
table.dv th { text-align:left; color:var(--muted); font-weight:500; border-bottom:1px solid var(--grid); padding:4px 10px 4px 0; }
table.dv td { border-bottom:1px solid var(--grid); padding:4px 10px 4px 0; font-variant-numeric:tabular-nums; }
table.dv td.num, table.dv th.num { text-align:right; }
.scroll { overflow-x:auto; }
.tableWrap { max-height:340px; overflow:auto; }
table.dv td:first-child { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11.5px; }
table.dv.configTable { table-layout:fixed; min-width:1040px; }
table.dv.configTable th:nth-child(1) { width:148px; }
table.dv.configTable th:nth-child(2) { width:168px; }
table.dv.configTable th:nth-child(3),
table.dv.configTable th:nth-child(4) { width:210px; }
table.dv.configTable th:nth-child(5) { width:82px; }
table.dv.configTable td { vertical-align:top; }
table.dv.configTable td:nth-child(2),
table.dv.configTable td:nth-child(3),
table.dv.configTable td:nth-child(4),
table.dv.configTable td:nth-child(6) { overflow-wrap:anywhere; word-break:break-word; }
table.dv.configTable td:nth-child(2),
table.dv.configTable td:nth-child(4) {
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11.5px;
}
.tag { display:inline-block; padding:1px 7px; border-radius:999px; border:1px solid var(--border); font-size:11px; }
.metricStrip { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
  background:var(--border); border:1px solid var(--border); border-radius:10px; overflow:hidden; margin-top:10px; }
.metricStrip > div { background:var(--surface-1); padding:10px 12px; }
.metricStrip .n { font-size:21px; font-weight:650; }
.metricStrip .l { color:var(--muted); font-size:11px; }
.note { border-left:3px solid var(--inferred); padding:8px 10px; color:var(--ink-2); background:color-mix(in srgb,var(--warn) 8%,transparent); font-size:12px; margin-top:10px; }
.note.crit { border-left-color:var(--crit); background:color-mix(in srgb,var(--crit) 8%,transparent); color:var(--ink); }
button:focus-visible, summary:focus-visible, [tabindex="0"]:focus-visible { outline:2px solid var(--s1); outline-offset:2px; }
svg text { fill:var(--muted); font:11px "Avenir Next","Segoe UI",system-ui,sans-serif; }
svg .grid line { stroke:var(--grid); stroke-width:1; shape-rendering:crispEdges; }
svg .base { stroke:var(--baseline); stroke-width:1; shape-rendering:crispEdges; }
svg .l1 { stroke:var(--s1); fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
svg .l2 { stroke:var(--s2); fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
svg .bridge { stroke-width:1.25; stroke-dasharray:2 5; opacity:.5; }
svg .f1 { fill:var(--s1); }
svg .f2 { fill:var(--s2); }
svg .wash1 { fill:var(--s1); opacity:.1; }
svg .ring { fill:var(--surface-1); }
svg .xhair { stroke:var(--baseline); stroke-width:1; shape-rendering:crispEdges; }
svg .endlbl { fill:var(--ink-2); font-weight:600; }
svg .barlbl { fill:var(--ink-2); font-weight:600; }
svg .hcell { stroke:var(--surface-1); stroke-width:2; }
svg .hempty { fill:none; stroke:var(--grid); stroke-width:1; }
details.inv summary { cursor:pointer; font-weight:600; font-size:14px; padding:4px 0; }
footer.page { color:var(--muted); font-size:12px; margin-top:26px; }
.topbar { position:sticky; top:0; z-index:30; background:var(--page);
  margin:0 calc(-1 * clamp(12px,3vw,36px)); padding:4px clamp(12px,3vw,36px) 0;
  border-bottom:1px solid var(--grid); }
.tabs { display:flex; gap:2px; overflow-x:auto; scrollbar-width:none; }
.tabs::-webkit-scrollbar { display:none; }
.tabs button { appearance:none; border:none; background:none; color:var(--ink-2); font:inherit;
  font-size:13.5px; padding:9px 13px 12px; cursor:pointer; white-space:nowrap; position:relative;
  border-radius:8px 8px 0 0; }
.tabs button:hover { color:var(--ink); background:var(--surface-1); }
.tabs button[aria-selected="true"] { color:var(--ink); font-weight:600; }
.tabs button[aria-selected="true"]::after { content:""; position:absolute; left:11px; right:11px;
  bottom:0; height:3px; border-radius:2px 2px 0 0; background:var(--s1); }
.topbar .filters { margin:0; padding:9px 0 10px; border-top:1px solid var(--grid); }
table.dv td:first-child { white-space:nowrap; }
.healthBadge { display:inline-flex; align-items:center; gap:10px; font-size:17px; font-weight:650; margin-top:6px; }
.healthBadge .hdot, .kpi .value .hdot { display:inline-block; width:12px; height:12px; border-radius:50%; flex:none; }
.kpi .value .hdot { width:11px; height:11px; margin-right:9px; vertical-align:3px; }
.hdot.good { background:var(--good); }
.hdot.fair { background:var(--warn); }
.hdot.attention { background:var(--crit); }
.hdot.unknown { background:var(--muted); }
.hreason { font-size:13px; color:var(--ink-2); padding-left:14px; position:relative; }
.hreason::before { content:"•"; position:absolute; left:2px; color:var(--muted); }
.facets { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:2px 12px; }
.facet .flabel { font-size:12px; font-weight:600; color:var(--ink-2); margin:6px 0 0 44px; }
.tab-panel { display:none; }
.tab-panel.active { display:block; animation:panelIn .16s ease; }
@keyframes panelIn { from { opacity:.35; transform:translateY(4px); } to { opacity:1; transform:none; } }
@media (prefers-reduced-motion: reduce){ .tab-panel.active { animation:none; } }
</style>
</head>
<body>
<header class="page">
  <h1>__VEHICLE_TITLE__</h1>
  <span class="sub" id="headSub"></span>
  <button id="themeBtn" type="button">Theme: auto</button>
</header>

<div class="topbar">
  <nav class="tabs" id="tabs" role="tablist" aria-label="Dashboard sections"></nav>
  <div class="filters" id="filters" role="group" aria-label="Date range">
    <span class="lbl">Diagnostic range</span>
  </div>
</div>

<section class="tab-panel" id="panel-overview" role="tabpanel" aria-labelledby="tab-overview">
<div class="sectionHead"><div><div class="eyebrow">Current state</div><h2>Vehicle snapshot</h2></div>
  <p>The most recent status records in the package plus headline figures for the selected range. Snapshot cards are single observations, not a continuous history.</p></div>
<div id="ovNotes"></div>
<section class="grid" id="statusCards"></section>
<section class="grid" id="kpis"></section>
</section>

<section class="tab-panel" id="panel-driving" role="tabpanel" aria-labelledby="tab-driving">
<div class="sectionHead"><div><div class="eyebrow">Movement & energy</div><h2>Driving and charging</h2></div>
  <p>Distance reconciles to the odometer. Trips show observed movement and split at sustained charging stops; kilometres hidden inside long sampling gaps stay explicitly unassigned.</p></div>
<section class="grid charts" id="driveCharts"></section>
<section class="grid" style="grid-template-columns:1fr" id="driveTables"></section>
</section>

<section class="tab-panel" id="panel-battery" role="tabpanel" aria-labelledby="tab-battery">
<div class="sectionHead"><div><div class="eyebrow">High-voltage system</div><h2>Battery diagnostics</h2></div>
  <p>Cell balance and current are useful diagnostic proxies. They do not constitute an official state-of-health or usable-capacity measurement.</p></div>
<section class="grid" id="healthCards" style="grid-template-columns:1fr"></section>
<section class="grid charts" id="batteryCharts"></section>
</section>

<section class="tab-panel" id="panel-thermal" role="tabpanel" aria-labelledby="tab-thermal">
<div class="sectionHead"><div><div class="eyebrow">Heat movement</div><h2>Thermal system</h2></div>
  <p>Seven undocumented temperature channels are retained as Sensors A–G, alongside coolant flow and the vehicle’s own operating-mode labels.</p></div>
<section class="grid charts" id="thermalCharts"></section>
</section>

<section class="tab-panel" id="panel-backend" role="tabpanel" aria-labelledby="tab-backend">
<div class="sectionHead"><div><div class="eyebrow">Backend trail</div><h2>Activity and configuration</h2></div>
  <p>Remote actions, backend errors, vehicle reports, and settings found anywhere in the package, including records outside the diagnostic window.</p></div>
<section class="grid" style="grid-template-columns:1fr" id="activityTables"></section>
</section>

<section class="tab-panel" id="panel-audit" role="tabpanel" aria-labelledby="tab-audit">
<div class="sectionHead"><div><div class="eyebrow">Package audit</div><h2>Completeness evidence</h2></div>
  <p>What the dictionary says exists, what this export actually contains, and how deep each delivered category goes.</p></div>
<div class="evidenceRail" aria-label="Data provenance legend">
  <div class="observed"><strong>Observed</strong><span>Directly present in the export</span></div>
  <div class="derived"><strong>Derived</strong><span>Calculated from observed samples</span></div>
  <div class="inferred"><strong>Inferred</strong><span>Undocumented channel meaning or assumption</span></div>
</div>
<section class="grid" id="evidenceCards"></section>
<section class="grid" style="grid-template-columns:1fr" id="evidenceTables"></section>

<details class="inv card" id="invWrap">
  <summary>Data inventory — every field in the export</summary>
  <div class="tableWrap" style="margin-top:8px"></div>
</details>
</section>

<footer class="page" id="pageFoot"></footer>

<script>
"use strict";
const DATA = /*__DATA__*/null;
const OFF = DATA.tzOffsetH * 3600;
const PACK_SHORT = (DATA.packSource || "").startsWith("measured") ? "measured" : "assumed";
const DASHBOARD_LANGUAGE_TAGS = { en:"en-GB", de:"de-DE", nl:"nl-NL", lt:"lt-LT" };
let DASHBOARD_LANGUAGE = "en";

/* English remains the source text in the renderer. Each entry is
 * [German, Dutch, Lithuanian]. Official VW dictionary descriptions are
 * deliberately excluded by localizeDashboard(). */
const DASHBOARD_TEXT = {
  "Table":["Tabelle","Tabel","Lentelė"],
  "km/h":["km/h","km/u","km/h"],
  "kWh/100km":["kWh/100 km","kWh/100 km","kWh/100 km"],
  "Theme: auto":["Design: automatisch","Thema: automatisch","Tema: automatinė"],
  "Theme: light":["Design: hell","Thema: licht","Tema: šviesi"],
  "Theme: dark":["Design: dunkel","Thema: donker","Tema: tamsi"],
  "Vehicle data":["Fahrzeugdaten","Voertuiggegevens","Automobilio duomenys"],
  "vehicle data":["Fahrzeugdaten","voertuiggegevens","automobilio duomenys"],
  "Vehicle":["Fahrzeug","Voertuig","Automobilis"],
  "Generated locally by ":["Lokal erstellt mit ","Lokaal gegenereerd door ","Vietoje sugeneravo "],
  " — free in-browser analysis of Volkswagen Group EU Data Act exports. Nothing was uploaded.":[" — kostenlose Browser-Analyse von EU-Data-Act-Exporten des Volkswagen-Konzerns. Es wurde nichts hochgeladen."," — gratis browseranalyse van EU Data Act-exports van de Volkswagen Groep. Er is niets geüpload."," — nemokama „Volkswagen Group“ ES Duomenų akto eksportų analizė naršyklėje. Niekas nebuvo įkelta."],
  "Dashboard sections":["Dashboard-Bereiche","Dashboardonderdelen","Skydelio skyriai"],
  "Date range":["Zeitraum","Datumbereik","Datos intervalas"],
  "Diagnostic range":["Diagnosezeitraum","Diagnostisch bereik","Diagnostikos laikotarpis"],
  "Current state":["Aktueller Zustand","Huidige toestand","Dabartinė būsena"],
  "Vehicle snapshot":["Fahrzeugübersicht","Voertuigmomentopname","Automobilio momentinė būsena"],
  "The most recent status records in the package plus headline figures for the selected range. Snapshot cards are single observations, not a continuous history.":["Die neuesten Statusdaten im Paket sowie Kennzahlen für den gewählten Zeitraum. Momentaufnahmen sind Einzelbeobachtungen, kein lückenloser Verlauf.","De meest recente statusgegevens in het pakket plus kerncijfers voor de gekozen periode. Momentopnamen zijn losse waarnemingen, geen doorlopende historie.","Naujausi paketo būsenos įrašai ir pasirinkto laikotarpio pagrindiniai rodikliai. Momentinės kortelės yra pavieniai stebėjimai, o ne nenutrūkstama istorija."],
  "Movement & energy":["Bewegung & Energie","Ritten & energie","Judėjimas ir energija"],
  "Driving and charging":["Fahren und Laden","Rijden en laden","Važiavimas ir įkrovimas"],
  "Distance reconciles to the odometer. Trips show observed movement and split at sustained charging stops; kilometres hidden inside long sampling gaps stay explicitly unassigned.":["Die Strecke wird mit dem Kilometerzähler abgeglichen. Fahrten zeigen beobachtete Bewegung und werden bei längeren Ladestopps geteilt; Kilometer in großen Messlücken bleiben ausdrücklich nicht zugeordnet.","Afstand wordt afgestemd op de kilometerstand. Ritten tonen waargenomen beweging en worden gesplitst bij langdurige laadstops; kilometers in lange meetgaten blijven expliciet niet toegewezen.","Atstumas suderinamas su odometru. Kelionės rodo stebėtą judėjimą ir atskiriamos per ilgesnius įkrovimo sustojimus; kilometrai ilgose matavimo spragose aiškiai paliekami nepriskirti."],
  "High-voltage system":["Hochvoltsystem","Hoogspanningssysteem","Aukštos įtampos sistema"],
  "Battery diagnostics":["Batteriediagnose","Accudiagnose","Baterijos diagnostika"],
  "Cell balance and current are useful diagnostic proxies. They do not constitute an official state-of-health or usable-capacity measurement.":["Zellbalance und Strom sind nützliche Diagnoseindikatoren. Sie sind keine offizielle Messung von Batteriezustand oder nutzbarer Kapazität.","Celbalans en stroom zijn nuttige diagnostische indicatoren. Ze vormen geen officiële meting van de accugezondheid of bruikbare capaciteit.","Celių balansas ir srovė yra naudingi diagnostiniai rodikliai. Tai nėra oficialus baterijos būklės ar naudingosios talpos matavimas."],
  "Heat movement":["Wärmeströme","Warmtestromen","Šilumos judėjimas"],
  "Thermal system":["Thermosystem","Thermisch systeem","Šiluminė sistema"],
  "Seven undocumented temperature channels are retained as Sensors A–G, alongside coolant flow and the vehicle’s own operating-mode labels.":["Sieben undokumentierte Temperaturkanäle werden als Sensoren A–G zusammen mit Kühlmittelfluss und den fahrzeugeigenen Betriebsmodus-Bezeichnungen dargestellt.","Zeven ongedocumenteerde temperatuurkanalen blijven zichtbaar als sensoren A–G, naast koelmiddelstroom en de eigen bedrijfsmoduslabels van het voertuig.","Septyni nedokumentuoti temperatūros kanalai rodomi kaip jutikliai A–G kartu su aušinimo skysčio srautu ir automobilio veikimo režimų pavadinimais."],
  "Backend trail":["Backend-Verlauf","Backendspoor","Sistemų žurnalas"],
  "Activity and configuration":["Aktivität und Konfiguration","Activiteit en configuratie","Veikla ir konfigūracija"],
  "Remote actions, backend errors, vehicle reports, and settings found anywhere in the package, including records outside the diagnostic window.":["Fernaktionen, Backend-Fehler, Fahrzeugberichte und Einstellungen aus dem gesamten Paket, einschließlich Einträgen außerhalb des Diagnosezeitraums.","Acties op afstand, backendfouten, voertuigrapporten en instellingen uit het hele pakket, ook buiten het diagnostische tijdvenster.","Nuotoliniai veiksmai, sistemų klaidos, automobilio ataskaitos ir nustatymai iš viso paketo, įskaitant įrašus už diagnostikos laikotarpio ribų."],
  "Package audit":["Paketprüfung","Pakketaudit","Paketo auditas"],
  "Completeness evidence":["Vollständigkeitsnachweis","Bewijs van volledigheid","Išsamumo įrodymai"],
  "What the dictionary says exists, what this export actually contains, and how deep each delivered category goes.":["Was laut Datenwörterbuch existiert, was dieser Export tatsächlich enthält und wie umfangreich jede gelieferte Kategorie ist.","Wat volgens het gegevenswoordenboek bestaat, wat deze export werkelijk bevat en hoe diep elke geleverde categorie gaat.","Kas pagal duomenų žodyną egzistuoja, ką šis eksportas iš tikrųjų turi ir kokia kiekvienos pateiktos kategorijos apimtis."],
  "Data provenance legend":["Legende der Datenherkunft","Legenda gegevensherkomst","Duomenų kilmės paaiškinimas"],
  "Observed":["Beobachtet","Waargenomen","Stebėta"],
  "Derived":["Abgeleitet","Afgeleid","Išvesta"],
  "Inferred":["Erschlossen","Afgeleid uit patroon","Nustatyta pagal požymius"],
  "observed":["beobachtet","waargenomen","stebėta"],
  "derived":["abgeleitet","afgeleid","išvesta"],
  "inferred":["erschlossen","geïnterpreteerd","nustatyta"],
  "Directly present in the export":["Direkt im Export enthalten","Rechtstreeks aanwezig in de export","Tiesiogiai pateikta eksporte"],
  "Calculated from observed samples":["Aus beobachteten Messwerten berechnet","Berekend uit waargenomen metingen","Apskaičiuota iš stebėtų mėginių"],
  "Undocumented channel meaning or assumption":["Undokumentierte Kanalbedeutung oder Annahme","Ongedocumenteerde kanaalbetekenis of aanname","Nedokumentuota kanalo reikšmė arba prielaida"],
  "Data inventory — every field in the export":["Datenbestand — jedes Feld im Export","Gegevensinventaris — elk veld in de export","Duomenų sąrašas — visi eksporto laukai"],
  "Overview":["Überblick","Overzicht","Apžvalga"],
  "Driving & charging":["Fahren & Laden","Rijden & laden","Važiavimas ir įkrovimas"],
  "Battery":["Batterie","Accu","Baterija"],
  "Thermal":["Thermik","Thermisch","Šiluma"],
  "Backend & config":["Backend & Konfiguration","Backend & configuratie","Sistemos ir konfigūracija"],
  "All diagnostics":["Alle Diagnosedaten","Alle diagnostiek","Visa diagnostika"],
  "Last 30 days":["Letzte 30 Tage","Laatste 30 dagen","Pastarosios 30 dienų"],
  "Last 7 days":["Letzte 7 Tage","Laatste 7 dagen","Pastarosios 7 dienos"],
  "Battery & range":["Batterie & Reichweite","Accu & bereik","Baterija ir nuvažiuojamas atstumas"],
  "Estimated range":["Geschätzte Reichweite","Geschat bereik","Numatomas nuvažiuojamas atstumas"],
  "Battery temperature":["Batterietemperatur","Accutemperatuur","Baterijos temperatūra"],
  "Battery care mode":["Batterieschutzmodus","Accubeschermingsmodus","Baterijos tausojimo režimas"],
  "Care mode charge cap":["Ladelimit im Schutzmodus","Laadlimiet beschermingsmodus","Tausojimo režimo įkrovos riba"],
  "Care notification":["Schutzhinweis","Beschermingsmelding","Tausojimo pranešimas"],
  "Charging":["Laden","Laden","Įkrovimas"],
  "State":["Status","Status","Būsena"],
  "Charge power":["Ladeleistung","Laadvermogen","Įkrovimo galia"],
  "Plug":["Stecker","Stekker","Kištukas"],
  "Mode":["Modus","Modus","Režimas"],
  "Maximum current":["Maximalstrom","Maximale stroom","Didžiausia srovė"],
  "Automatic plug unlock":["Automatische Steckerentriegelung","Stekker automatisch ontgrendelen","Automatinis kištuko atrakinimas"],
  "Charge mode":["Lademodus","Laadmodus","Įkrovimo režimas"],
  "Action state":["Aktionsstatus","Actiestatus","Veiksmo būsena"],
  "Charge type":["Ladeart","Laadtype","Įkrovimo tipas"],
  "Plug type":["Steckertyp","Stekkertype","Kištuko tipas"],
  "Infrastructure":["Infrastruktur","Infrastructuur","Infrastruktūra"],
  "Bidirectional (V2H)":["Bidirektional (V2H)","Bidirectioneel (V2H)","Dvikryptis įkrovimas (V2H)"],
  "Auxiliary & climate load":["Nebenverbraucher & Klimalast","Hulpverbruik & klimaatbelasting","Pagalbinė ir klimato apkrova"],
  "Residual network load":["Restnetzlast","Resterende netwerkbelasting","Likusi tinklo apkrova"],
  "Interior climatisation":["Innenraumklimatisierung","Interieurklimatisering","Salono klimato kontrolė"],
  "Battery climatisation":["Batterieklimatisierung","Accuklimatisering","Baterijos klimato kontrolė"],
  "24h budget start level":["Startwert des 24-h-Budgets","Startniveau 24-uursbudget","24 val. biudžeto pradinis lygis"],
  "Power budget warning":["Leistungsbudget-Warnung","Waarschuwing vermogensbudget","Galios biudžeto įspėjimas"],
  "Daily budget warning":["Tagesbudget-Warnung","Waarschuwing dagbudget","Dienos biudžeto įspėjimas"],
  "Vehicle":["Fahrzeug","Voertuig","Automobilis"],
  "Outdoor temperature":["Außentemperatur","Buitentemperatuur","Lauko temperatūra"],
  "Parking brake":["Parkbremse","Parkeerrem","Stovėjimo stabdys"],
  "Parking lights":["Parklicht","Parkeerlichten","Stovėjimo žibintai"],
  "Next service":["Nächster Service","Volgende onderhoudsbeurt","Kitas aptarnavimas"],
  "Climate":["Klima","Klimaat","Klimatas"],
  "Trigger":["Auslöser","Aanleiding","Priežastis"],
  "Target":["Solltemperatur","Doel","Tikslinė temperatūra"],
  "Window heating":["Scheibenheizung","Ruitverwarming","Langų šildymas"],
  "Run without external power":["Betrieb ohne externe Stromversorgung","Werken zonder externe stroom","Veikti be išorinio maitinimo"],
  "Start when unlocking":["Beim Entriegeln starten","Starten bij ontgrendelen","Paleisti atrakinant"],
  "Front zones":["Vordere Zonen","Voorzones","Priekinės zonos"],
  "Rear zones":["Hintere Zonen","Achterzones","Galinės zonos"],
  "Reported timer state":["Gemeldeter Timerstatus","Gemelde timerstatus","Pranešta laikmačio būsena"],
  "Charge timer option available":["Ladezeitplan verfügbar","Laadtimeroptie beschikbaar","Galima įkrovimo laikmačio parinktis"],
  "Charge + climate option":["Laden + Klima","Laden + klimaat","Įkrovimas ir klimatas"],
  "Connectivity":["Konnektivität","Connectiviteit","Ryšiai"],
  "Vehicle connected":["Fahrzeug verbunden","Voertuig verbonden","Automobilis prisijungęs"],
  "Backend domains active":["Backend-Domänen aktiv","Backenddomeinen actief","Aktyvios sistemų sritys"],
  "Communications unit":["Kommunikationseinheit","Communicatie-eenheid","Ryšio įrenginys"],
  "V2X communication":["V2X-Kommunikation","V2X-communicatie","V2X ryšys"],
  "Connection timestamp":["Verbindungszeitpunkt","Verbindingstijdstip","Prisijungimo laikas"],
  "Doors & closures":["Türen & Verschlüsse","Deuren & sluitingen","Durys ir uždarymai"],
  "Battery health":["Batteriezustand","Accugezondheid","Baterijos būklė"],
  "Battery evidence":["Batterienachweise","Accubewijs","Baterijos duomenys"],
  "Battery looks healthy":["Batterie wirkt gesund","Accu lijkt gezond","Baterija atrodo sveika"],
  "Battery shows normal wear":["Batterie zeigt normale Alterung","Accu vertoont normale slijtage","Baterija rodo įprastą dėvėjimąsi"],
  "Battery worth checking":["Batterie sollte geprüft werden","Accu verdient controle","Bateriją verta patikrinti"],
  "Not enough data to assess the battery":["Nicht genügend Daten zur Batteriebewertung","Onvoldoende gegevens om de accu te beoordelen","Nepakanka duomenų baterijai įvertinti"],
  "Healthy":["Gesund","Gezond","Sveika"], "Normal wear":["Normale Alterung","Normale slijtage","Įprastas dėvėjimasis"],
  "Check advised":["Prüfung empfohlen","Controle aanbevolen","Rekomenduojama patikrinti"],
  "Distance driven":["Gefahrene Strecke","Gereden afstand","Nuvažiuotas atstumas"],
  "Avg consumption":["Ø Verbrauch","Gem. verbruik","Vid. sąnaudos"],
  "Energy charged":["Geladene Energie","Geladen energie","Įkrauta energija"],
  "Regen recovered":["Rekuperierte Energie","Teruggewonnen regeneratie","Atgauta regeneruojant"],
  "Idle drain":["Verlust im Stand","Stilstandsverlies","Nuostolis stovint"],
  "Days with driving":["Tage mit Fahrten","Dagen met ritten","Dienos su važiavimu"],
  "Observed trips":["Beobachtete Fahrten","Waargenomen ritten","Stebėtos kelionės"],
  "Top speed":["Höchstgeschwindigkeit","Topsnelheid","Didžiausias greitis"],
  "Distance added to odometer":["Zum Kilometerzähler addierte Strecke","Aan kilometerstand toegevoegde afstand","Prie odometro pridėtas atstumas"],
  "When the car is driven":["Wann das Auto gefahren wird","Wanneer de auto rijdt","Kada automobilis važiuoja"],
  "When the vehicle was reporting":["Wann das Fahrzeug Daten meldete","Wanneer het voertuig rapporteerde","Kada automobilis siuntė duomenis"],
  "Speed distribution":["Geschwindigkeitsverteilung","Snelheidsverdeling","Greičio pasiskirstymas"],
  "Charging power curves":["Ladeleistungskurven","Laadvermogencurven","Įkrovimo galios kreivės"],
  "Charging power by state of charge":["Ladeleistung nach Ladestand","Laadvermogen per laadniveau","Įkrovimo galia pagal įkrovos lygį"],
  "Energy charged per day":["Geladene Energie pro Tag","Geladen energie per dag","Per dieną įkrauta energija"],
  "Energy charged per month":["Geladene Energie pro Monat","Geladen energie per maand","Per mėnesį įkrauta energija"],
  "Energy consumption per day":["Energieverbrauch pro Tag","Energieverbruik per dag","Energijos sąnaudos per dieną"],
  "Battery state of charge":["Batterieladestand","Acculaadniveau","Baterijos įkrovos lygis"],
  "Highest vs lowest cell voltage":["Höchste und niedrigste Zellspannung","Hoogste versus laagste celspanning","Didžiausia ir mažiausia celės įtampa"],
  "Battery cell imbalance":["Zellspannungsabweichung","Onbalans tussen accucellen","Baterijos celių disbalansas"],
  "HV battery current":["HV-Batteriestrom","HV-accustroom","Aukštos įtampos baterijos srovė"],
  "Cell spread by operating state":["Zellspreizung nach Betriebszustand","Celspreiding per bedrijfstoestand","Celių sklaida pagal veikimo būseną"],
  "Cell spread by state of charge":["Zellspreizung nach Ladestand","Celspreiding per laadniveau","Celių sklaida pagal įkrovos lygį"],
  "Pack voltage":["Batteriespannung","Accuspanning","Baterijos įtampa"],
  "Ambient temperature":["Umgebungstemperatur","Omgevingstemperatuur","Aplinkos temperatūra"],
  "Thermal management modes":["Thermomanagement-Modi","Thermische beheermodi","Šilumos valdymo režimai"],
  "Thermal sensor traces":["Verläufe der Temperatursensoren","Thermische sensorcurven","Šiluminių jutiklių kreivės"],
  "Coolant flow":["Kühlmittelfluss","Koelmiddelstroom","Aušinimo skysčio srautas"],
  "Coolant valve actuation":["Ansteuerung der Kühlmittelventile","Aansturing koelmiddelkleppen","Aušinimo vožtuvų valdymas"],
  "Highest cell":["Höchste Zelle","Hoogste cel","Aukščiausia celė"],
  "Lowest cell":["Niedrigste Zelle","Laagste cel","Žemiausia celė"],
  "Median cell spread":["Median der Zellspreizung","Mediane celspreiding","Celių sklaidos mediana"],
  "HV current":["HV-Strom","HV-stroom","Aukštos įtampos srovė"],
  "Charge power":["Ladeleistung","Laadvermogen","Įkrovimo galia"],
  "Charging (>5 A)":["Laden (>5 A)","Laden (>5 A)","Įkrovimas (>5 A)"],
  "Heavy load (<−50 A)":["Hohe Last (<−50 A)","Zware belasting (<−50 A)","Didelė apkrova (<−50 A)"],
  "Light load":["Leichte Last","Lichte belasting","Maža apkrova"],
  "Idle (±5 A)":["Ruhezustand (±5 A)","Rust (±5 A)","Ramybė (±5 A)"],
  "Charging ledger":["Ladeprotokoll","Laadlogboek","Įkrovimų žurnalas"],
  "Observed trip ledger":["Fahrtenprotokoll","Logboek waargenomen ritten","Stebėtų kelionių žurnalas"],
  "Parked drain events":["Standverlust-Ereignisse","Stilstandsverlies","Išsikrovimas stovint"],
  "Movement across sampling gaps":["Bewegung in Messlücken","Beweging tijdens meetgaten","Judėjimas matavimo spragose"],
  "Remote actions, reports and errors":["Fernaktionen, Berichte und Fehler","Acties op afstand, rapporten en fouten","Nuotoliniai veiksmai, ataskaitos ir klaidos"],
  "Full raw-export timeline; not limited by the diagnostic range filter":["Vollständiger Rohdatenverlauf; nicht durch den Diagnosezeitraum begrenzt","Volledige tijdlijn van de ruwe export; niet beperkt door het diagnostische filter","Visa neapdoroto eksporto laiko juosta; diagnostikos laikotarpio filtras netaikomas"],
  "Vehicle configuration snapshot":["Momentaufnahme der Fahrzeugkonfiguration","Momentopname voertuigconfiguratie","Automobilio konfigūracijos momentinė būsena"],
  "Coverage by delivered category":["Abdeckung nach gelieferter Kategorie","Dekking per geleverde categorie","Aprėptis pagal pateiktą kategoriją"],
  "Dictionary fields cited but not delivered":["Genannte, aber nicht gelieferte Wörterbuchfelder","Genoemde maar niet geleverde woordenboekvelden","Nurodyti, bet nepateikti žodyno laukai"],
  "Checked against VW's Data Dictionary V4.0 (bundled) and the JSON export":["Abgleich mit VWs Data Dictionary V4.0 (integriert) und dem JSON-Export","Gecontroleerd aan de hand van VW's Data Dictionary V4.0 (ingebouwd) en de JSON-export","Patikrinta pagal integruotą VW duomenų žodyną V4.0 ir JSON eksportą"],
  "Dictionary keys":["Wörterbuchschlüssel","Woordenboeksleutels","Žodyno raktai"],
  "Dictionary-matched keys":["Wörterbuchtreffer","Sleutels met woordenboekmatch","Su žodynu sutapę raktai"],
  "Numeric diagnostic records":["Numerische Diagnoseeinträge","Numerieke diagnostische records","Skaitiniai diagnostikos įrašai"],
  "Diagnostic lag":["Diagnoseverzug","Diagnostische achterstand","Diagnostikos atsilikimas"],
  "Yes":["Ja","Ja","Taip"], "No":["Nein","Nee","Ne"],
  "On":["Ein","Aan","Įjungta"], "Off":["Aus","Uit","Išjungta"],
  "Engaged":["Aktiviert","Ingeschakeld","Įjungtas"], "Released":["Gelöst","Vrijgegeven","Atleistas"],
  "Running":["Aktiv","Actief","Veikia"], "Shutting down":["Wird heruntergefahren","Wordt afgesloten","Išjungiama"],
  "Activated":["Aktiviert","Geactiveerd","Aktyvinta"], "Deactivated":["Deaktiviert","Gedeactiveerd","Išjungta"],
  "Available":["Verfügbar","Beschikbaar","Pasiekiama"], "Unavailable":["Nicht verfügbar","Niet beschikbaar","Nepasiekiama"],
  "Maximum":["Maximum","Maximum","Didžiausia"], "Manual":["Manuell","Handmatig","Rankinis"],
  "Disconnected":["Nicht verbunden","Niet aangesloten","Atjungta"], "Connected":["Verbunden","Aangesloten","Prijungta"],
  "Unlocked":["Entriegelt","Ontgrendeld","Atrakinta"], "Locked":["Verriegelt","Vergrendeld","Užrakinta"],
  "Open":["Offen","Open","Atidaryta"], "Closed":["Geschlossen","Gesloten","Uždaryta"],
  "Not ready for charging":["Nicht ladebereit","Niet gereed om te laden","Nepasiruošęs įkrauti"],
  "No reason":["Kein Grund","Geen reden","Nėra priežasties"],
  "Immediately default":["Sofort (Standard)","Direct (standaard)","Iškart (numatyta)"],
  "Charge mode selection":["Lademodus-Auswahl","Keuze laadmodus","Įkrovimo režimo pasirinkimas"],
  "Inspection":["Inspektion","Inspectie","Patikra"],
  "DC fast":["DC-Schnellladen","DC-snelladen","Greitasis DC įkrovimas"],
  "Slow / scheduled":["Langsam / geplant","Langzaam / gepland","Lėtas / suplanuotas"],
  "No valve samples in this export.":["Keine Ventilmesswerte in diesem Export.","Geen klepmetingen in deze export.","Šiame eksporte nėra vožtuvų mėginių."],
  "No detailed time-based charging curve is available in this range.":["Für diesen Zeitraum ist keine detaillierte zeitbasierte Ladekurve verfügbar.","Voor deze periode is geen gedetailleerde laadcurve in de tijd beschikbaar.","Šiame laikotarpyje nėra išsamios įkrovimo galios kreivės pagal laiką."],
  "No mode telemetry":["Keine Modus-Telemetrie","Geen modustelemetrie","Nėra režimo telemetrijos"],
  "Climate ran — likely conditioning":["Klimatisierung aktiv — vermutlich Vorkonditionierung","Klimaatregeling actief — waarschijnlijk conditionering","Klimato kontrolė veikė — tikėtinas kondicionavimas"],
  "Quiet park":["Ruhiger Parkzeitraum","Rustige parkeerperiode","Ramus stovėjimas"],
  "No movement telemetry":["Keine Bewegungstelemetrie","Geen bewegingstelemetrie","Nėra judėjimo telemetrijos"],
  "Raw peak discharge":["Rohe Entladespitze","Ruwe piekontlading","Neapdorotas didžiausias iškrovimas"],
  "Raw 5th percentile":["Rohwert, 5. Perzentil","Ruw 5e percentiel","Neapdorotas 5-asis procentilis"],
  "Raw 95th percentile":["Rohwert, 95. Perzentil","Ruw 95e percentiel","Neapdorotas 95-asis procentilis"],
  "Raw peak positive":["Roher positiver Spitzenwert","Ruwe positieve piek","Neapdorotas didžiausias teigiamas dydis"],
  "°C, 30-min averages of inferred channel 180806":["°C, 30-Minuten-Mittelwerte des erschlossenen Kanals 180806","°C, gemiddelden van 30 minuten van afgeleid kanaal 180806","°C, nustatyto kanalo 180806 30 min. vidurkiai"],
  "L/min, 10-min averages of inferred channel 546697":["L/min, 10-Minuten-Mittelwerte des erschlossenen Kanals 546697","L/min, gemiddelden van 10 minuten van afgeleid kanaal 546697","L/min, nustatyto kanalo 546697 10 min. vidurkiai"],
  "Min °C":["Min. °C","Min. °C","Min. °C"], "Max °C":["Max. °C","Max. °C","Maks. °C"],
  "Ventilation":["Lüftung","Ventilatie","Vėdinimas"],
  "Cabin cooling + HGK":["Innenraumkühlung + HGK","Interieurkoeling + HGK","Salono vėsinimas + HGK"],
  "Cabin cooling":["Innenraumkühlung","Interieurkoeling","Salono vėsinimas"],
  "Combined heating (heat pump)":["Kombiniertes Heizen (Wärmepumpe)","Gecombineerde verwarming (warmtepomp)","Kombinuotas šildymas (šilumos siurblys)"],
  "Shutdown":["Abschaltung","Uitschakeling","Išjungimas"],
  "Air heating (heat pump)":["Luftheizung (Wärmepumpe)","Luchtverwarming (warmtepomp)","Oro šildymas (šilumos siurblys)"],
  "Coolant valve 543814":["Kühlmittelventil 543814","Koelmiddelklep 543814","Aušinimo vožtuvas 543814"],
  "Coolant valve 544790":["Kühlmittelventil 544790","Koelmiddelklep 544790","Aušinimo vožtuvas 544790"],
  "Success":["Erfolgreich","Geslaagd","Pavyko"],
  "true":["Ja","Ja","Taip"], "false":["Nein","Nee","Ne"],
  "daily median spread between highest and lowest cell, mV — small and stable is healthy":["Täglicher Median der Differenz zwischen höchster und niedrigster Zellspannung in mV — klein und stabil ist gesund","dagelijkse mediane spreiding tussen hoogste en laagste cel, mV — klein en stabiel is gezond","dienos sklaidos tarp didžiausios ir mažiausios celės mediana, mV — maža ir stabili reikšmė yra geras ženklas"],
  "A, 2-min averages of inferred channel 546774; raw full-window extremes are summarized below":["A, 2-Minuten-Mittelwerte des erschlossenen Kanals 546774; rohe Extremwerte des gesamten Zeitraums stehen unten","A, gemiddelden van 2 minuten van afgeleid kanaal 546774; ruwe extremen over het hele venster staan hieronder","A, nustatyto kanalo 546774 2 min. vidurkiai; viso laikotarpio neapdoroti ekstremumai pateikti žemiau"],
  "95th-percentile spread while charging, under load, and at rest — divergence under load is the earliest weak-cell sign":["95. Perzentil der Spreizung beim Laden, unter Last und im Ruhezustand — Auseinanderdriften unter Last ist das früheste Zeichen einer schwachen Zelle","95e percentiel van de spreiding tijdens laden, onder belasting en in rust — uiteenlopen onder belasting is het vroegste teken van een zwakke cel","95-asis sklaidos procentilis įkraunant, esant apkrovai ir ramybės būsenoje — išsiskyrimas esant apkrovai yra ankstyviausias silpnos celės požymis"],
  "95th-percentile spread within each SoC band":["95. Perzentil der Spreizung in jedem SoC-Bereich","95e percentiel van de spreiding binnen elke SoC-band","95-asis sklaidos procentilis kiekviename SoC intervale"],
  "V, 20-min averages — mean cell voltage × 96 series cells":["V, 20-Minuten-Mittelwerte — mittlere Zellspannung × 96 Reihenzellen","V, gemiddelden van 20 minuten — gemiddelde celspanning × 96 cellen in serie","V, 20 min. vidurkiai — vidutinė celės įtampa × 96 nuoseklios celės"],
  "share of samples in range, channel 543919":["Anteil der Messwerte im Zeitraum, Kanal 543919","aandeel metingen in de periode, kanaal 543919","laikotarpio mėginių dalis, kanalas 543919"],
  "20-min averages; exact component locations are not documented":["20-Minuten-Mittelwerte; genaue Einbauorte der Komponenten sind nicht dokumentiert","gemiddelden van 20 minuten; exacte componentlocaties zijn niet gedocumenteerd","20 min. vidurkiai; tikslios komponentų vietos nedokumentuotos"],
  "inferred channels 543814 / 544790 — periods where the valve was commanded; actuation tracks the heat-pump modes":["erschlossene Kanäle 543814 / 544790 — Zeiträume mit Ventilansteuerung; die Ansteuerung folgt den Wärmepumpenmodi","afgeleide kanalen 543814 / 544790 — perioden waarin de klep werd aangestuurd; de aansturing volgt de warmtepompmodi","nustatyti kanalai 543814 / 544790 — laikotarpiai, kai vožtuvas buvo valdomas; valdymas atitinka šilumos siurblio režimus"],
  "Gaps with evidence are not promoted to trips: one gap can hide several drives, and the sparse samples cover only a fraction of the distance. The window shows when movement is proven, not its full extent.":["Lücken mit Nachweisen werden nicht zu Fahrten erklärt: Eine Lücke kann mehrere Fahrten verbergen, und die wenigen Messwerte decken nur einen Teil der Strecke ab. Das Fenster zeigt, wann Bewegung belegt ist, nicht deren gesamten Umfang.","Meetgaten met bewijs worden niet tot ritten verheven: één gat kan meerdere ritten verbergen en de schaarse metingen dekken slechts een deel van de afstand. Het venster toont wanneer beweging is bewezen, niet de volledige omvang.","Spragos su įrodymais nelaikomos kelionėmis: vienoje spragoje gali būti kelios kelionės, o reti mėginiai apima tik dalį atstumo. Laikotarpis rodo, kada judėjimas patvirtintas, bet ne visą jo apimtį."],
  "Unknown":["Unbekannt","Onbekend","Nežinoma"],
  "explicit":["explizit","expliciet","aiški"],
  "factory default":["Werkseinstellung","fabrieksinstelling","gamyklinė nuostata"],
  "Vehicle platform MEB confirmed by the export — the 96-series battery analysis applies.":["Fahrzeugplattform MEB durch den Export bestätigt — die Batterieanalyse mit 96 Reihenzellen ist anwendbar.","Voertuigplatform MEB bevestigd door de export — de accuanalyse met 96 cellen in serie is van toepassing.","Eksportas patvirtina MEB automobilio platformą — taikoma 96 nuoseklių celių baterijos analizė."],
  "This export carries the vehicle's charging history and activity timestamps rather than odometer-based trips — distance and trip panels are omitted.":["Dieser Export enthält die Ladehistorie und Aktivitätszeitpunkte des Fahrzeugs statt kilometerzählerbasierter Fahrten — Strecken- und Fahrtbereiche werden ausgelassen.","Deze export bevat de laadgeschiedenis en activiteitstijdstippen van het voertuig in plaats van ritten op basis van de kilometerstand — afstands- en ritpanelen worden weggelaten.","Šiame eksporte yra automobilio įkrovimo istorija ir veiklos laikai, bet nėra pagal odometrą nustatytų kelionių — atstumo ir kelionių skydeliai nerodomi."],
  "Reported charging energy provides a useful consistency check, but not battery-side capacity or SoH. This export contains no current or cell-voltage diagnostics.":["Die gemeldete Ladeenergie ermöglicht eine nützliche Plausibilitätsprüfung, aber keine batterieseitige Kapazitäts- oder SOH-Bestimmung. Dieser Export enthält keine Strom- oder Zellspannungsdiagnose.","De gemelde laadenergie biedt een nuttige consistentiecontrole, maar geen capaciteit aan accuzijde of SOH. Deze export bevat geen stroom- of celspanningsdiagnostiek.","Pranešta įkrovimo energija leidžia naudingai patikrinti nuoseklumą, bet ne baterijos talpą ar SOH. Šiame eksporte nėra srovės ar celių įtampos diagnostikos."],
  "Capacity is measured from charging sessions. This export contains no cell-voltage diagnostics, so cell-balance panels are omitted.":["Die Kapazität wird aus Ladevorgängen gemessen. Dieser Export enthält keine Zellspannungsdiagnose; Zellbalance-Bereiche werden daher ausgelassen.","De capaciteit wordt gemeten uit laadsessies. Deze export bevat geen celspanningsdiagnostiek, dus celbalanspanelen worden weggelaten.","Talpa matuojama iš įkrovimo sesijų. Šiame eksporte nėra celių įtampos diagnostikos, todėl celių balanso skydeliai nerodomi."],
  "Timestamp-only speed records; values were withheld, so these are not confirmed trips":["Geschwindigkeitseinträge nur mit Zeitstempel; Werte wurden nicht geliefert, daher sind dies keine bestätigten Fahrten","Snelheidsrecords met alleen tijdstippen; waarden zijn achtergehouden, dus dit zijn geen bevestigde ritten","Greičio įrašuose yra tik laikas; reikšmės nepateiktos, todėl tai nėra patvirtintos kelionės"],
  "charge power per session as reported by the vehicle, 5-min averages":["vom Fahrzeug gemeldete Ladeleistung je Sitzung, 5-Minuten-Mittelwerte","laadvermogen per sessie zoals gemeld door het voertuig, gemiddelden van 5 minuten","automobilio pranešta kiekvienos sesijos įkrovimo galia, 5 min. vidurkiai"],
  "battery-side charge power per session, 5-min averages — taper and pauses become visible":["batterieseitige Ladeleistung je Sitzung, 5-Minuten-Mittelwerte — Drosselung und Pausen werden sichtbar","laadvermogen aan accuzijde per sessie, gemiddelden van 5 minuten — afbouw en pauzes worden zichtbaar","kiekvienos sesijos įkrovimo galia baterijos pusėje, 5 min. vidurkiai — matomas galios mažėjimas ir pauzės"],
  "vehicle-reported power against vehicle-reported SoC; each curve follows one charging session":["vom Fahrzeug gemeldete Leistung gegenüber gemeldetem SoC; jede Kurve entspricht einem Ladevorgang","door het voertuig gemeld vermogen tegenover gemelde SoC; elke curve volgt één laadsessie","automobilio pranešta galia pagal praneštą SoC; kiekviena kreivė atitinka vieną įkrovimo sesiją"],
  "kWh per day from the vehicle's own daily aggregation":["kWh pro Tag aus der fahrzeugeigenen Tagesaggregation","kWh per dag uit de eigen dagaggregatie van het voertuig","kWh per dieną iš paties automobilio dienos suvestinės"],
  "kWh per month from the vehicle's own monthly aggregation":["kWh pro Monat aus der fahrzeugeigenen Monatsaggregation","kWh per maand uit de eigen maandaggregatie van het voertuig","kWh per mėnesį iš paties automobilio mėnesio suvestinės"],
  "%, observed only at charging-session boundaries and in recent charging curves; not continuous driving history":["%, nur an den Grenzen von Ladevorgängen und in aktuellen Ladekurven beobachtet; kein durchgehender Fahrverlauf","%, alleen waargenomen aan de grenzen van laadsessies en in recente laadcurven; geen doorlopende rijhistorie","%, stebėta tik įkrovimo sesijų ribose ir naujausiose įkrovimo kreivėse; tai nėra nenutrūkstama važiavimo istorija"],
  "%, combined diagnostic samples and observed charging-session values":["%, kombinierte Diagnosemesswerte und beobachtete Werte aus Ladevorgängen","%, gecombineerde diagnostische metingen en waargenomen laadsessiewaarden","%, sujungti diagnostikos mėginiai ir stebėtos įkrovimo sesijų reikšmės"],
  "%, inferred diagnostic channel 180886 — sampled only while the car is awake":["%, erschlossener Diagnosekanal 180886 — nur gemessen, wenn das Fahrzeug aktiv ist","%, afgeleid diagnostisch kanaal 180886 — alleen bemonsterd wanneer de auto wakker is","%, nustatytas diagnostikos kanalas 180886 — matuota tik automobiliui esant aktyviam"],
  "Diagnostic estimate from charging sessions and cell voltages — not an official state-of-health measurement.":["Diagnostische Schätzung aus Ladevorgängen und Zellspannungen — keine offizielle SOH-Messung.","Diagnostische schatting uit laadsessies en celspanningen — geen officiële SOH-meting.","Diagnostinis įvertis iš įkrovimo sesijų ir celių įtampos — ne oficialus SOH matavimas."],
  "The reported-energy ratio is not part of the verdict above; that verdict relies on cell-balance evidence only.":["Das Verhältnis der gemeldeten Energie fließt nicht in die obige Bewertung ein; diese stützt sich ausschließlich auf die Zellbalance.","De verhouding van de gemelde energie maakt geen deel uit van het oordeel hierboven; dat berust uitsluitend op celbalansbewijs.","Praneštos energijos santykis neįtrauktas į aukščiau pateiktą vertinimą; jis remiasi tik celių balanso duomenimis."],
  "This export contains no battery-current or cell-voltage history, so it cannot support a battery-health verdict.":["Dieser Export enthält keinen Verlauf von Batteriestrom oder Zellspannung und erlaubt daher keine Batteriezustandsbewertung.","Deze export bevat geen geschiedenis van accustroom of celspanning en kan daarom geen oordeel over de accugezondheid ondersteunen.","Šiame eksporte nėra baterijos srovės ar celių įtampos istorijos, todėl baterijos būklės įvertinti negalima."],
  "Time (UTC+3)":["Zeit (UTC+3)","Tijd (UTC+3)","Laikas (UTC+3)"],
  "End":["Ende","Einde","Pabaiga"], "End / duration":["Ende / Dauer","Einde / duur","Pabaiga / trukmė"],
  "Energy":["Energie","Energie","Energija"], "Avg power":["Ø Leistung","Gem. vermogen","Vid. galia"],
  "Elapsed / active":["Dauer / aktiv","Verstreken / actief","Trukmė / aktyvu"],
  "Plugged in":["Angeschlossen","Aangesloten","Prijungta"],
  "Charge start":["Ladebeginn","Begin laden","Įkrovimo pradžia"],
  "Immediatelydefault":["Sofort (Standard)","Direct (standaard)","Iškart (numatyta)"],
  "Speed samples":["Geschwindigkeitsmesswerte","Snelheidsmetingen","Greičio mėginiai"],
  "Distance":["Strecke","Afstand","Atstumas"], "Ambient":["Umgebung","Omgeving","Aplinka"],
  "Peak current":["Spitzenstrom","Piekstroom","Didžiausia srovė"],
  "Avg / max speed":["Ø / max. Geschwindigkeit","Gem. / max. snelheid","Vid. / didž. greitis"],
  "Moving":["In Bewegung","In beweging","Judėjimas"],
  "Est. consumption":["Geschätzter Verbrauch","Geschat verbruik","Numatomos sąnaudos"],
  "∫I·V check":["∫I·V-Gegenprobe","∫I·V-controle","∫I·V patikra"],
  "SoC used":["SoC-Verbrauch","Gebruikte SoC","Panaudotas SoC"],
  "capacity not measurable — details on the Battery tab":["Kapazität nicht messbar — Details im Batterie-Tab","capaciteit niet meetbaar — details op het tabblad Accu","talpos išmatuoti negalima — išsamiau Baterijos skirtuke"],
  "Capacity could not be measured (needs a ≥30% charge while the car reports current) — verdict based on cell balance only":["Die Kapazität konnte nicht gemessen werden (erfordert eine Ladung um ≥30 %, während das Fahrzeug Stromwerte meldet) — Bewertung nur anhand der Zellbalance","De capaciteit kon niet worden gemeten (vereist een lading van ≥30% terwijl de auto stroom rapporteert) — oordeel alleen gebaseerd op celbalans","Talpos išmatuoti nepavyko (reikia ≥30 % įkrovimo, kai automobilis teikia srovės duomenis) — vertinimas pagrįstas tik celių balansu"],
  "vehicle report":["Fahrzeugbericht","voertuigrapport","automobilio ataskaita"],
  "remote action":["Fernaktion","actie op afstand","nuotolinis veiksmas"],
  "error":["Fehler","fout","klaida"],
  "factory setting is used":["Werkseinstellung wird verwendet","fabrieksinstelling wordt gebruikt","naudojama gamyklinė nuostata"],
  "Factory default":["Werkseinstellung","Fabrieksinstelling","Gamyklinė nuostata"],
  "Unlock all doors":["Alle Türen entriegeln","Alle portieren ontgrendelen","Atrakinti visas duris"],
  "Climatization backend error":["Backend-Fehler der Klimatisierung","Backendfout klimaatregeling","Klimato sistemos serverio klaida"],
  "Undated":["Ohne Datum","Zonder datum","Be datos"],
  "Date":["Datum","Datum","Data"], "Day":["Tag","Dag","Diena"], "Time":["Zeit","Tijd","Laikas"],
  "Kind":["Art","Soort","Tipas"], "Event":["Ereignis","Gebeurtenis","Įvykis"], "Detail":["Detail","Detail","Informacija"],
  "Field":["Feld","Veld","Laukas"], "Fields":["Felder","Velden","Laukai"], "Records":["Einträge","Records","Įrašai"],
  "First":["Erster","Eerste","Pirmas"], "Last":["Letzter","Laatste","Paskutinis"],
  "Description":["Beschreibung","Beschrijving","Aprašymas"], "Sample value":["Beispielwert","Voorbeeldwaarde","Pavyzdinė reikšmė"],
  "Raw fields":["Rohfelder","Ruwe velden","Neapdoroti laukai"], "Example raw field":["Beispiel-Rohfeld","Voorbeeld ruw veld","Neapdoroto lauko pavyzdys"],
  "Field / indexed pattern":["Feld / indexiertes Muster","Veld / geïndexeerd patroon","Laukas / indeksuotas šablonas"],
  "Interpreted value":["Interpretierter Wert","Geïnterpreteerde waarde","Interpretuota reikšmė"], "Raw":["Rohwert","Ruw","Neapdorota"],
  "Source":["Quelle","Bron","Šaltinis"], "Dictionary description":["Wörterbuchbeschreibung","Beschrijving uit woordenboek","Žodyno aprašymas"],
  "Category":["Kategorie","Categorie","Kategorija"], "In dictionary":["Im Wörterbuch","In woordenboek","Žodyne"], "In export":["Im Export","In export","Eksporte"],
  "Speed band":["Geschwindigkeitsbereich","Snelheidsband","Greičio intervalas"], "Samples":["Messwerte","Metingen","Mėginiai"], "Share":["Anteil","Aandeel","Dalis"],
  "Across gaps":["In Messlücken","Over meetgaten","Per spragas"], "Start":["Start","Start","Pradžia"], "Until":["Bis","Tot","Iki"],
  "Type":["Typ","Type","Tipas"], "Duration":["Dauer","Duur","Trukmė"], "Energy in":["Energie hinein","Energie in","Įkrauta energija"],
  "Start SoC":["Start-Ladestand","Begin-SoC","Pradinis SoC"], "End SoC":["End-Ladestand","Eind-SoC","Galutinis SoC"],
  "Average kW":["Ø kW","Gemiddeld kW","Vidutinė kW"], "Peak kW":["Spitze kW","Piek kW","Didžiausia kW"],
  "Curve points":["Kurvenpunkte","Curvepunten","Kreivės taškai"], "SoC window":["Ladestandsfenster","SoC-venster","SoC intervalas"],
  "Current coverage":["Stromabdeckung","Stroomdekking","Srovės aprėptis"], "Measured usable":["Gemessen nutzbar","Gemeten bruikbaar","Išmatuota naudingoji talpa"],
  "Energy in / SoC gained":["Energie hinein / SoC-Zuwachs","Energie in / gewonnen SoC","Energija / SoC prieaugis"],
  "Park start":["Parkbeginn","Begin parkeren","Stovėjimo pradžia"], "SoC lost":["SoC-Verlust","SoC-verlies","Prarastas SoC"],
  "Rate":["Rate","Tempo","Sparta"], "Mode samples":["Modus-Messwerte","Modusmetingen","Režimo mėginiai"], "Climate share":["Klimaanteil","Klimaataandeel","Klimato dalis"], "Reading":["Bewertung","Duiding","Vertinimas"],
  "%/day":["%/Tag","%/dag","%/d."],
  "Last sample":["Letzter Messwert","Laatste meting","Paskutinis mėginys"], "Next sample":["Nächster Messwert","Volgende meting","Kitas mėginys"],
  "Gap":["Lücke","Meetgat","Spraga"], "Distance added":["Hinzugefügte Strecke","Toegevoegde afstand","Pridėtas atstumas"],
  "Movement evidence in gap":["Bewegungsnachweis in der Lücke","Bewegingsbewijs in meetgat","Judėjimo įrodymai spragoje"],
  "Likely movement window":["Wahrscheinliches Bewegungsfenster","Waarschijnlijk bewegingsvenster","Tikėtinas judėjimo laikotarpis"],
  "Median":["Median","Mediaan","Mediana"], "Maximum":["Maximum","Maximum","Didžiausia"],
  "Highest mV":["Höchste mV","Hoogste mV","Didžiausia mV"], "Lowest mV":["Niedrigste mV","Laagste mV","Mažiausia mV"],
  "Median spread mV":["Median-Spannweite mV","Mediane spreiding mV","Sklaidos mediana mV"],
  "Paired samples":["Gepaarte Messwerte","Gekoppelde metingen","Suporuoti mėginiai"], "SoC band":["SoC-Bereich","SoC-band","SoC intervalas"],
  "Vehicle label":["Fahrzeugbezeichnung","Voertuiglabel","Automobilio žyma"], "Channel":["Kanal","Kanaal","Kanalas"],
  "Sensor":["Sensor","Sensor","Jutiklis"], "Valve":["Ventil","Klep","Vožtuvas"], "Actuated share":["Ansteuerungsanteil","Aangestuurd aandeel","Valdymo dalis"], "Transitions":["Übergänge","Overgangen","Perėjimai"],
  "20-min buckets":["20-Min.-Intervalle","vakken van 20 min.","20 min. intervalai"],
  "fewer":["weniger","minder","mažiau"], "more":["mehr","meer","daugiau"],
  "driven":["gefahren","gereden","nuvažiuota"], "charged":["geladen","geladen","įkrauta"],
  "of moving time":["der Bewegungszeit","van bewegende tijd","judėjimo laiko"],
  "speed samples":["Geschwindigkeitsmesswerte","snelheidsmetingen","greičio mėginiai"],
  "reporting events":["Meldeereignisse","rapportagegebeurtenissen","duomenų siuntimo įvykiai"],
  "Front left door":["Tür vorne links","Portier linksvoor","Priekinės kairės durys"],
  "Front right door":["Tür vorne rechts","Portier rechtsvoor","Priekinės dešinės durys"],
  "Rear left door":["Tür hinten links","Portier linksachter","Galinės kairės durys"],
  "Rear right door":["Tür hinten rechts","Portier rechtsachter","Galinės dešinės durys"],
  "Front left window":["Fenster vorne links","Ruit linksvoor","Priekinis kairysis langas"],
  "Front right window":["Fenster vorne rechts","Ruit rechtsvoor","Priekinis dešinysis langas"],
  "Rear left window":["Fenster hinten links","Ruit linksachter","Galinis kairysis langas"],
  "Rear right window":["Fenster hinten rechts","Ruit rechtsachter","Galinis dešinysis langas"],
  "Hood":["Motorhaube","Motorkap","Variklio dangtis"], "Trunk":["Kofferraum","Kofferbak","Bagažinė"],
  "Numeric diagnostics":["Numerische Diagnosedaten","Numerieke diagnostiek","Skaitinė diagnostika"],
  "Configuration":["Konfiguration","Configuratie","Konfigūracija"],
  "Remote actions & reports":["Fernaktionen & Berichte","Acties op afstand & rapporten","Nuotoliniai veiksmai ir ataskaitos"],
  "Service & maintenance":["Service & Wartung","Service & onderhoud","Aptarnavimas ir priežiūra"],
  "Warnings & DTCs":["Warnungen & Fehlercodes","Waarschuwingen & DTC's","Įspėjimai ir gedimų kodai"],
  "GPS or route coordinates":["GPS- oder Routenkoordinaten","GPS- of routecoördinaten","GPS arba maršruto koordinatės"],
  "tyre-pressure values":["Reifendruckwerte","bandenspanningswaarden","padangų slėgio reikšmės"],
  "direct battery SOH or capacity":["direkter Batterie-SOH oder Kapazität","directe accu-SOH of capaciteit","tiesioginis baterijos SOH arba talpa"],
  "warning or DTC records":["Warnungs- oder Fehlercode-Einträge","waarschuwings- of DTC-records","įspėjimų arba gedimų kodų įrašai"],
  "repair history":["Reparaturhistorie","reparatiehistorie","remonto istorija"]
};

/* These longer pieces occur inside sentences containing values. Keeping them
 * separate avoids translating arbitrary raw payload text. */
const DASHBOARD_FRAGMENTS = {
  "Speed samples":["Geschwindigkeitsmesswerte","Snelheidsmetingen","Greičio mėginiai"],
  "front left door":["Tür vorne links","portier linksvoor","priekinės kairės durys"],
  "front right door":["Tür vorne rechts","portier rechtsvoor","priekinės dešinės durys"],
  "rear left door":["Tür hinten links","portier linksachter","galinės kairės durys"],
  "rear right door":["Tür hinten rechts","portier rechtsachter","galinės dešinės durys"],
  "front left window":["Fenster vorne links","ruit linksvoor","priekinis kairysis langas"],
  "front right window":["Fenster vorne rechts","ruit rechtsvoor","priekinis dešinysis langas"],
  "rear left window":["Fenster hinten links","ruit linksachter","galinis kairysis langas"],
  "rear right window":["Fenster hinten rechts","ruit rechtsachter","galinis dešinysis langas"],
  "locked not safe":["verriegelt, nicht safe","vergrendeld, niet safe","užrakinta, neapsaugota"],
  "locked safe":["sicher verriegelt","veilig vergrendeld","saugiai užrakinta"],
  "unlocked":["entriegelt","ontgrendeld","atrakinta"],
  "locked":["verriegelt","vergrendeld","užrakinta"],
  "closed":["geschlossen","gesloten","uždaryta"],
  "open":["offen","open","atidaryta"],
  "measured":["gemessene","gemeten","išmatuotą"],
  "assumed":["angenommene","aangenomen","numanomą"],
  "inspection":["Inspektion","inspectie","patikra"],
  " km/h":[" km/h"," km/u"," km/h"],
  " kWh/100km":[" kWh/100 km"," kWh/100 km"," kWh/100 km"],
  " %/day":[" %/Tag"," %/dag"," %/d."],
  " /h":[" /h"," /u"," /val."],
  " — no data":[" — keine Daten"," — geen gegevens"," — nėra duomenų"],
  " · identifiers redacted":[" · Kennungen geschwärzt"," · identificatoren afgeschermd"," · identifikatoriai paslėpti"],
  " · times in ":[" · Zeiten in "," · tijden in "," · laikas pagal "],
  " fields · diagnostics ":[" Felder · Diagnosedaten "," velden · diagnostiek "," laukai · diagnostika "],
  " records, ":[" Einträge, "," records, "," įrašai, "],
  " · EU Data Act export · ":[" · EU-Data-Act-Export · "," · EU Data Act-export · "," · ES Duomenų akto eksportas · "],
  "Captured between ":["Erfasst zwischen ","Vastgelegd tussen ","Užfiksuota tarp "],
  "Captured ":["Erfasst ","Vastgelegd ","Užfiksuota "],
  " · hover a row for its exact timestamp":[" · Zeile für den exakten Zeitpunkt berühren"," · beweeg over een rij voor het exacte tijdstip"," · užveskite ant eilutės tiksliam laikui"],
  "% charged":[" % geladen","% geladen"," % įkrauta"],
  "target ":["Ziel ","doel ","tikslas "],
  "discharge ":["Entladen ","ontladen ","iškrovimas "],
  "home storage ":["Hausspeicher ","thuisopslag ","namų kaupiklis "],
  "Left ":["Links ","Links ","Kairėje "],
  "right ":["rechts ","rechts ","dešinėje "],
  "% open":[" % geöffnet","% open"," % atidaryta"],
  "snapshot only, not a live vehicle state":["nur Momentaufnahme, kein Live-Fahrzeugstatus","alleen een momentopname, geen live voertuigstatus","tik momentinė būsena, ne tiesioginė automobilio būsena"],
  "As of ":["Stand ","Stand per ","Būsena "],
  " days in range":[" Tagen im Zeitraum"," dagen in de periode"," laikotarpio dienų"],
  "highest sampled in range":["höchster Messwert im Zeitraum","hoogste meting in de periode","didžiausia laikotarpio reikšmė"],
  "odometer movement with ≤30 min sample continuity":["Kilometerzähler-Bewegung bei ≤30 Min. Messkontinuität","kilometerbeweging met ≤30 min meetcontinuïteit","odometro pokytis, kai mėginiai ne rečiau kaip kas 30 min."],
  "from SoC delta across ":["aus SoC-Differenz über ","uit SoC-verschil over ","iš SoC pokyčio per "],
  " trips · ":[" Fahrten · "," ritten · "," keliones · "],
  " km observed":[" km beobachtet"," km waargenomen"," km stebėta"],
  "reported charging sessions in range":["gemeldete Ladevorgänge im Zeitraum","gemelde laadsessies in de periode","laikotarpyje praneštos įkrovimo sesijos"],
  "charge events in range · pack ":["Ladevorgänge im Zeitraum · Batterie ","laadgebeurtenissen in periode · accu ","įkrovimo įvykiai laikotarpyje · baterija "],
  " regen of traction energy · ":[" Rekuperation der Antriebsenergie · "," regeneratie van tractie-energie · "," traukos energijos atgauta · "],
  " trips with current coverage":[" Fahrten mit Stromabdeckung"," ritten met stroomdekking"," kelionės su srovės aprėptimi"],
  "estimated from ":["geschätzt aus ","geschat uit ","apskaičiuota iš "],
  " parked intervals ≥8 h":[" Parkintervallen ≥8 h"," parkeerintervallen ≥8 u"," stovėjimo intervalų ≥8 val."],
  "Consumption values are the vehicle's own normalized figures (dictionary unit: 1/h) — comparable between exports, not directly convertible to watts.":["Die Verbrauchswerte sind fahrzeugeigene normierte Größen (Wörterbucheinheit: 1/h) — zwischen Exporten vergleichbar, aber nicht direkt in Watt umrechenbar.","De verbruikswaarden zijn genormaliseerde voertuigwaarden (woordenboekeenheid: 1/u) — vergelijkbaar tussen exports, niet rechtstreeks om te rekenen naar watt.","Sąnaudų reikšmės yra paties automobilio normalizuoti dydžiai (žodyno vienetas: 1/val.) — palyginami tarp eksportų, bet tiesiogiai į vatus nekonvertuojami."],
  "Timer IDs are delivered without array indexes; the single reported state cannot be assigned to one specific timer.":["Timer-IDs werden ohne Array-Indizes geliefert; der einzelne gemeldete Status kann keinem bestimmten Timer zugeordnet werden.","Timer-ID's worden zonder array-indexen geleverd; de ene gemelde status kan niet aan een specifieke timer worden toegewezen.","Laikmačių ID pateikti be masyvo indeksų; vienintelės būsenos negalima priskirti konkrečiam laikmačiui."],
  "Distance reconciles to the odometer; ":["Strecke mit dem Kilometerzähler abgeglichen; ","Afstand afgestemd op de kilometerstand; ","Atstumas suderintas su odometru; "],
  " km assigned to observed trips and ":[" km beobachteten Fahrten und "," km toegewezen aan waargenomen ritten en "," km priskirta stebėtoms kelionėms ir "],
  " km retained in sampling gaps":[" km Messlücken zugeordnet"," km behouden in meetgaten"," km palikta matavimo spragose"],
  "Daily allocation reconciles to the full odometer delta; hatched context is listed in the table as sampling-gap km":["Die tägliche Zuordnung entspricht der gesamten Kilometerzählerdifferenz; schraffierter Kontext ist in der Tabelle als Kilometer in Messlücken ausgewiesen.","Dagtoewijzing sluit aan op het volledige verschil in kilometerstand; gearceerde context staat in de tabel als kilometers in meetgaten.","Dienos paskirstymas suderintas su visu odometro pokyčiu; brūkšniuotas kontekstas lentelėje rodomas kaip kilometrai matavimo spragose."],
  "Share of observed moving samples; irregular sampling means this is not exact time share":["Anteil beobachteter Bewegungsmesswerte; wegen unregelmäßiger Messung kein exakter Zeitanteil.","Aandeel waargenomen bewegende metingen; door onregelmatige bemonstering is dit geen exact tijdsaandeel.","Stebėtų judėjimo mėginių dalis; dėl netolygaus matavimo tai nėra tiksli laiko dalis."],
  "share of samples while moving (":["Anteil der Messwerte während der Fahrt (","aandeel metingen tijdens beweging (","mėginių dalis judant ("],
  " samples > 0 km/h in range)":[" Messwerte > 0 km/h im Zeitraum)"," metingen > 0 km/u in periode)"," mėginiai > 0 km/h laikotarpyje)"],
  " paired samples · median ":[" gepaarte Messwerte · Median "," gekoppelde metingen · mediaan "," suporuotų mėginių · mediana "],
  " · peak ":[" · Spitze "," · piek "," · didžiausia "],
  " min actuated":[" Min. angesteuert"," min aangestuurd"," min. valdytas"],
  "settings found · ":["Einstellungen gefunden · ","instellingen gevonden · ","nustatymų rasta · "],
  " explicit values · raw encodings retained when the dictionary is ambiguous":[" explizite Werte · Rohcodierungen bleiben erhalten, wenn das Wörterbuch mehrdeutig ist"," expliciete waarden · ruwe coderingen blijven behouden als het woordenboek dubbelzinnig is"," aiškių reikšmių · neapdorotos koduotės paliekamos, kai žodynas dviprasmis"],
  "versus ":["gegenüber ","tegenover ","palyginti su "],
  " unique keys delivered":[" gelieferten eindeutigen Schlüsseln"," geleverde unieke sleutels"," pateiktų unikalių raktų"],
  " delivered keys":[" gelieferten Schlüsseln"," geleverde sleutels"," pateiktų raktų"],
  " records across ":[" Einträge in "," records over "," įrašų per "],
  " undocumented channels":[" undokumentierten Kanälen"," ongedocumenteerde kanalen"," nedokumentuotų kanalų"],
  "last high-volume diagnostic sample before export creation":["letzter umfangreicher Diagnosemesswert vor Erstellung des Exports","laatste omvangrijke diagnostische meting vóór aanmaak van de export","paskutinis didelės apimties diagnostikos mėginys prieš sukuriant eksportą"],
  "Raw export spans ":["Rohdatenexport umfasst ","Ruwe export loopt van ","Neapdorotas eksportas apima "],
  "; high-volume diagnostics span ":["; umfangreiche Diagnosedaten umfassen ","; omvangrijke diagnostiek loopt van ","; didelės apimties diagnostika apima "],
  "Not found in this export: ":["In diesem Export nicht gefunden: ","Niet gevonden in deze export: ","Šiame eksporte nerasta: "],
  " — the vehicle-reported charging history and activity timestamps this package does carry are shown instead.":[" — stattdessen werden die im Paket vorhandene fahrzeugeigene Ladehistorie und die Aktivitätszeitpunkte gezeigt."," — in plaats daarvan worden de voertuiglaadgeschiedenis en activiteitstijdstippen getoond die dit pakket wel bevat."," — vietoje jų rodoma pakete esanti automobilio įkrovimo istorija ir veiklos laikai."],
  " by weekday and hour, ":[" nach Wochentag und Stunde, "," per weekdag en uur, "," pagal savaitės dieną ir valandą, "],
  "odometer / distance history":["Kilometerzähler-/Streckenverlauf","kilometerstand-/afstandshistorie","odometro / atstumo istorija"],
  "speed values":["Geschwindigkeitswerte","snelheidswaarden","greičio reikšmės"],
  "cell voltages":["Zellspannungen","celspanningen","celių įtampos"],
  "battery current":["Batteriestrom","accustroom","baterijos srovė"],
  "GPS or route coordinates":["GPS- oder Routenkoordinaten","GPS- of routecoördinaten","GPS arba maršruto koordinatės"],
  "tyre-pressure values":["Reifendruckwerte","bandenspanningswaarden","padangų slėgio reikšmės"],
  "direct battery SOH or capacity":["direkter Batterie-SOH oder Kapazität","directe accu-SOH of capaciteit","tiesioginis baterijos SOH arba talpa"],
  "warning or DTC records":["Warnungs- oder Fehlercode-Einträge","waarschuwings- of DTC-records","įspėjimų arba gedimų kodų įrašai"],
  "repair history":["Reparaturhistorie","reparatiehistorie","remonto istorija"],
  ". Identifiers are redacted in this HTML by default.":[". Kennungen sind in diesem HTML standardmäßig geschwärzt.",". Identificatoren zijn standaard afgeschermd in deze HTML.",". Šiame HTML identifikatoriai pagal numatymą paslėpti."],
  "Built offline from ":["Offline erstellt aus ","Offline opgebouwd uit ","Sukurta neprisijungus iš "],
  " · everything computed in your own browser — nothing was uploaded · ":[" · vollständig in Ihrem Browser berechnet — nichts wurde hochgeladen · "," · alles berekend in uw eigen browser — niets is geüpload · "," · viskas apskaičiuota jūsų naršyklėje — niekas neįkelta · "],
  "observed, derived and inferred values are labelled throughout · ":["beobachtete, abgeleitete und erschlossene Werte sind durchgehend gekennzeichnet · ","waargenomen, afgeleide en geïnterpreteerde waarden zijn overal gemarkeerd · ","stebėtos, išvestos ir nustatytos reikšmės visur pažymėtos · "],
  "the car only reports while awake — solid line segments are measured, dashed segments bridge not-reported periods":["das Fahrzeug meldet nur im Wachzustand — durchgezogene Linien sind gemessen, gestrichelte überbrücken Zeiträume ohne Meldung","de auto rapporteert alleen wanneer hij wakker is — doorgetrokken lijnen zijn gemeten, stippellijnen overbruggen perioden zonder rapportage","automobilis duomenis siunčia tik būdamas aktyvus — ištisinės linijos yra matuotos, brūkšninės jungia laikotarpius be duomenų"],
  " · identifiers redacted by default.":[" · Kennungen standardmäßig geschwärzt."," · identificatoren standaard afgeschermd."," · identifikatoriai pagal numatymą paslėpti."],
  "sessions":["Sitzungen","sessies","sesijos"],
  "median":["Median","mediaan","mediana"]
};

const DASHBOARD_PATTERNS = [
  [/^Vehicle platform (.+) reported — the MEB-specific battery capacity analysis may not apply to this vehicle\.$/,
    ["Fahrzeugplattform $1 gemeldet — die MEB-spezifische Kapazitätsanalyse ist für dieses Fahrzeug möglicherweise nicht anwendbar.","Voertuigplatform $1 gemeld — de MEB-specifieke capaciteitsanalyse is mogelijk niet van toepassing op dit voertuig.","Pranešta automobilio platforma $1 — MEB skirta baterijos talpos analizė šiam automobiliui gali netikti."]],
  [/^High-frequency diagnostic history ends (.+) days before this package was created — the snapshot cards are newer than the charts and ledgers\.$/,
    ["Der hochfrequente Diagnoseverlauf endet $1 Tage vor Erstellung dieses Pakets — die Momentaufnahmen sind neuer als Diagramme und Protokolle.","De hoogfrequente diagnostische historie eindigt $1 dagen vóór dit pakket is gemaakt — de momentopnamen zijn nieuwer dan de grafieken en logboeken.","Didelio dažnio diagnostikos istorija baigiasi likus $1 d. iki paketo sukūrimo — momentinės kortelės yra naujesnės už diagramas ir žurnalus."]],
  [/^Not delivered in this export: (.+)\. The related panels are omitted rather than shown empty(.*)$/,
    ["In diesem Export nicht geliefert: $1. Die zugehörigen Bereiche werden ausgelassen statt leer angezeigt$2","Niet geleverd in deze export: $1. De bijbehorende panelen worden weggelaten in plaats van leeg getoond$2","Šiame eksporte nepateikta: $1. Susiję skydeliai nerodomi tušti, o visai praleidžiami$2"]],
  [/^⚠ This package is nearly empty — only (.+) snapshot records arrived and none of the diagnostic or charging history the portal is supposed to deliver\..*$/,
    ["⚠ Dieses Paket ist nahezu leer — nur $1 Momentaufnahmen sind angekommen und weder Diagnose- noch Ladehistorie, die das Portal liefern sollte. Das ist ein bekanntes Problem des VW-Exportdienstes, nicht Ihres Fahrzeugs oder dieses Werkzeugs. Fordern Sie den Export im Portal erneut an (vollständige Pakete benötigen oft mehrere Versuche) und erwägen Sie eine Beschwerde über das Kontaktformular. Nach EU Data Act (Art. 4–5) und DSGVO (Art. 15/20) haben Sie Anspruch auf die vollständigen Daten. Alle angekommenen Daten werden unten gezeigt; der Tab Paketprüfung nennt genau, was fehlt.","⚠ Dit pakket is bijna leeg — er zijn slechts $1 momentopnamen binnengekomen en geen diagnostische of laadgeschiedenis die het portaal hoort te leveren. Dit is een bekend probleem van VW's exportdienst, niet van uw auto of dit hulpmiddel. Vraag de export opnieuw aan in het portaal (volledige pakketten vereisen vaak meerdere pogingen) en overweeg een klacht via het contactformulier. Volgens de EU Data Act (art. 4–5) en AVG (art. 15/20) hebt u recht op de volledige gegevens. Alles wat wel aankwam staat hieronder; het tabblad Pakketaudit vermeldt precies wat ontbreekt.","⚠ Šis paketas beveik tuščias — gauti tik $1 momentinės būsenos įrašai ir nėra diagnostikos ar įkrovimo istorijos, kurią turėtų pateikti portalas. Tai žinoma VW eksporto paslaugos problema, o ne jūsų automobilio ar šio įrankio gedimas. Paprašykite eksporto portale dar kartą (pilnam paketui dažnai reikia kelių bandymų) ir apsvarstykite skundą per portalo kontaktų formą. Pagal ES Duomenų aktą (4–5 str.) ir BDAR (15/20 str.) turite teisę į visus duomenis. Visi gauti duomenys rodomi žemiau; Paketo audito skirtuke tiksliai nurodyta, ko trūksta."]],
  [/^(.+)% charged$/,["$1 % geladen","$1% geladen","Įkrauta $1 %"]],
  [/^target (.+)%$/,["Ziel $1 %","doel $1%","tikslas $1 %"]],
  [/^in (.+) days \((.+)\)$/,["in $1 Tagen ($2)","over $1 dagen ($2)","po $1 dienų ($2)"]],
  [/^in (.+) days$/,["in $1 Tagen","over $1 dagen","po $1 dienų"]],
  [/^(.+) charge events$/,["$1 Ladevorgänge","$1 laadgebeurtenissen","$1 įkrovimo įvykiai"]],
  [/^Sensor (.+)$/,["Sensor $1","Sensor $1","Jutiklis $1"]],
  [/^(.+) km observed · (.+) km across sampling gaps$/,["$1 km beobachtet · $2 km in Messlücken","$1 km waargenomen · $2 km in meetgaten","$1 km stebėta · $2 km matavimo spragose"]],
  [/^(.+) · (.+) h (.+) min$/,["$1 · $2 h $3 min","$1 · $2 u $3 min","$1 · $2 val. $3 min."]],
  [/^(.+) h (.+) min$/,["$1 h $2 min","$1 u $2 min","$1 val. $2 min."]],
  [/^(.+) of (.+) min$/,["$1 von $2 min","$1 van $2 min","$1 iš $2 min."]],
  [/^(.+) min$/,["$1 min","$1 min","$1 min."]],
  [/^(.+) h$/,["$1 h","$1 u","$1 val."]],
  [/^≈(.+) kWh charging-energy proxy · SoH unavailable — details on the Battery tab$/,["≈$1 kWh Ladeenergie-Näherung · SOH nicht verfügbar — Details im Batterie-Tab","≈$1 kWh laadenergieproxy · SOH niet beschikbaar — details op het tabblad Accu","≈$1 kWh įkrovimo energijos pakaitinis rodiklis · SOH nėra — išsamiau Baterijos skirtuke"]],
  [/^≈(.+) kWh usable measured · (.+) mV cell imbalance — details on the Battery tab$/,["≈$1 kWh nutzbar gemessen · $2 mV Zellabweichung — Details im Batterie-Tab","≈$1 kWh bruikbaar gemeten · $2 mV celonbalans — details op het tabblad Accu","≈$1 kWh išmatuotos naudingosios talpos · $2 mV celių disbalansas — išsamiau Baterijos skirtuke"]],
  [/^≈ (.+) km per full charge at this rate$/,["≈ $1 km pro Vollladung bei diesem Verbrauch","≈ $1 km per volle lading bij dit verbruik","≈ $1 km su pilna įkrova esant tokioms sąnaudoms"]],
  [/^SoC lost while parked ≥ 8 h \((.+) days observed\)$/,["SoC-Verlust beim Parken ≥ 8 h ($1 Tage beobachtet)","SoC-verlies tijdens parkeren ≥ 8 u ($1 dagen waargenomen)","SoC praradimas stovint ≥ 8 val. (stebėta $1 d.)"]],
  [/^kWh\/100km from SoC drop while driving, using the (.+) (.+) kWh usable capacity — days with ≥20 km$/,["kWh/100 km aus dem SoC-Abfall während der Fahrt mit der $1en nutzbaren Kapazität von $2 kWh — Tage mit ≥20 km","kWh/100 km uit SoC-daling tijdens het rijden met de $1 bruikbare capaciteit van $2 kWh — dagen met ≥20 km","kWh/100 km pagal SoC sumažėjimą važiuojant, naudojant $1 $2 kWh naudingąją talpą — dienos su ≥20 km"]],
  [/^~(.+) kWh\/100km \(SoC samples stale\)$/,["~$1 kWh/100 km (SoC-Messwerte veraltet)","~$1 kWh/100 km (SoC-metingen verouderd)","~$1 kWh/100 km (SoC mėginiai pasenę)"]],
  [/^~(.+) kWh\/100km · (.+)% cov$/,["~$1 kWh/100 km · $2 % Abdeckung","~$1 kWh/100 km · $2% dekking","~$1 kWh/100 km · $2 % aprėptis"]],
  [/^~(.+) kWh · (.+)% cov$/,["~$1 kWh · $2 % Abdeckung","~$1 kWh · $2% dekking","~$1 kWh · $2 % aprėptis"]],
  [/^On · IDs (.+)$/,["Ein · IDs $1","Aan · ID's $1","Įjungta · ID $1"]],
  [/^Off · IDs (.+)$/,["Aus · IDs $1","Uit · ID's $1","Išjungta · ID $1"]],
  [/^of (.+) days in range$/,["von $1 Tagen im Zeitraum","van $1 dagen in de periode","iš $1 laikotarpio dienų"]],
  [/^of (.+) delivered keys$/,["von $1 gelieferten Schlüsseln","van $1 geleverde sleutels","iš $1 pateiktų raktų"]],
  [/^(.+) records across (.+) undocumented channels$/,["$1 Einträge in $2 undokumentierten Kanälen","$1 records over $2 ongedocumenteerde kanalen","$1 įrašų per $2 nedokumentuotų kanalų"]],
  [/^(.+) settings found · (.+) explicit values · raw encodings retained when the dictionary is ambiguous$/,["$1 Einstellungen gefunden · $2 explizite Werte · Rohcodierungen bleiben erhalten, wenn das Wörterbuch mehrdeutig ist","$1 instellingen gevonden · $2 expliciete waarden · ruwe coderingen blijven behouden als het woordenboek dubbelzinnig is","Rasta $1 nustatymų · $2 aiškių reikšmių · neapdorotos koduotės paliekamos, kai žodynas dviprasmis"]],
  [/^(.+) charging sessions in range, reported by the vehicle itself — energy, power and SoC window are the car's own figures$/,["$1 Ladevorgänge im Zeitraum, vom Fahrzeug selbst gemeldet — Energie, Leistung und SoC-Fenster sind fahrzeugeigene Werte","$1 laadsessies in de periode, gemeld door het voertuig zelf — energie, vermogen en SoC-venster zijn voertuigwaarden","$1 įkrovimo sesijos laikotarpyje, praneštos paties automobilio — energija, galia ir SoC intervalas yra automobilio reikšmės"]],
  [/^(.+) continuous SoC-rise events in range; consecutive samples may be at most 30 minutes apart\. Energy\/power use the (.+) (.+) kWh usable capacity\.$/,["$1 zusammenhängende SoC-Anstiege im Zeitraum; aufeinanderfolgende Messwerte liegen höchstens 30 Minuten auseinander. Energie/Leistung verwenden die $2e nutzbare Kapazität von $3 kWh.","$1 aaneengesloten SoC-stijgingen in de periode; opeenvolgende metingen liggen maximaal 30 minuten uit elkaar. Energie/vermogen gebruikt de $2 bruikbare capaciteit van $3 kWh.","$1 nenutrūkstami SoC augimo įvykiai laikotarpyje; gretimi mėginiai nutolę ne daugiau kaip 30 min. Energijai ir galiai naudojama $2 $3 kWh naudingoji talpa."]],
  [/^(.+) movement clusters in range, built from odometer edges with ≤30-minute continuity and split at sustained charging stops$/,["$1 Bewegungscluster im Zeitraum, aus Kilometerzählerabschnitten mit höchstens 30 Minuten Abstand gebildet und bei längeren Ladestopps geteilt","$1 bewegingsclusters in de periode, opgebouwd uit kilometerstandsranden met maximaal 30 minuten continuïteit en gesplitst bij langdurige laadstops","$1 judėjimo grupės laikotarpyje, sudarytos iš odometro atkarpų su ne ilgesniu kaip 30 min. tarpu ir atskirtos per ilgesnius įkrovimo sustojimus"]],
  [/^(.+) parks of ≥ 8 h with no odometer movement; thermal-mode samples inside each park hint at why charge was lost$/,["$1 Parkzeiträume von ≥ 8 h ohne Kilometerzählerbewegung; Thermomodus-Messwerte deuten auf die Ursache des Ladeverlusts hin","$1 parkeerperioden van ≥ 8 u zonder kilometerbeweging; thermische modusmetingen geven een aanwijzing voor het laadverlies","$1 stovėjimo laikotarpiai po ≥ 8 val. be odometro pokyčio; šiluminio režimo mėginiai nurodo galimą įkrovos praradimo priežastį"]],
  [/^(.+) km is proven by odometer change, but cannot be assigned to exact trips — (.+) km falls in gaps with partial timing evidence, (.+) km with none$/,["$1 km sind durch die Kilometerzähleränderung belegt, lassen sich aber keinen exakten Fahrten zuordnen — $2 km liegen in Lücken mit teilweisen Zeitnachweisen, $3 km ohne solche Nachweise","$1 km is bewezen door verandering van de kilometerstand, maar kan niet aan exacte ritten worden toegewezen — $2 km valt in gaten met gedeeltelijk tijdsbewijs, $3 km zonder bewijs","$1 km patvirtinta odometro pokyčiu, bet negalima priskirti konkrečioms kelionėms — $2 km yra spragose su daliniais laiko įrodymais, $3 km be jų"]],
  [/^(.+) moving speed samples?( · up to (.+) km\/h)?$/,["$1 Geschwindigkeitsmesswerte in Bewegung$2","$1 bewegende snelheidsmetingen$2","$1 judėjimo greičio mėginiai$2"]],
  [/^(.+) moving speed samples? · up to (.+) km\/h · (.+) battery-discharge samples?$/,["$1 Geschwindigkeitsmesswerte in Bewegung · bis $2 km/h · $3 Batterieentlade-Messwerte","$1 bewegende snelheidsmetingen · tot $2 km/u · $3 metingen van accuontlading","$1 judėjimo greičio mėginiai · iki $2 km/h · $3 baterijos iškrovimo mėginiai"]],
  [/^(.+) moving speed samples? · (.+) battery-discharge samples?$/,["$1 Geschwindigkeitsmesswerte in Bewegung · $2 Batterieentlade-Messwerte","$1 bewegende snelheidsmetingen · $2 metingen van accuontlading","$1 judėjimo greičio mėginiai · $2 baterijos iškrovimo mėginiai"]],
  [/^(.+) battery-discharge samples?$/,["$1 Batterieentlade-Messwerte","$1 metingen van accuontlading","$1 baterijos iškrovimo mėginiai"]],
  [/^(.+) paired samples · median (.+) mV$/,["$1 gepaarte Messwerte · Median $2 mV","$1 gekoppelde metingen · mediaan $2 mV","$1 suporuotų mėginių · mediana $2 mV"]],
  [/^(.+) samples \((.+)\)$/,["$1 Messwerte ($2)","$1 metingen ($2)","$1 mėginių ($2)"]],
  [/^(.+) sessions$/,["$1 Sitzungen","$1 sessies","$1 sesijos"]],
  [/^(.+) sessions? even exceed the largest pack option, demonstrating the uncertainty\.$/,["$1 Sitzung(en) überschreiten sogar die größte Batterieoption und verdeutlichen die Unsicherheit.","$1 sessie(s) overschrijden zelfs de grootste accuoptie en tonen de onzekerheid.","$1 sesija (-os) viršija net didžiausią baterijos variantą ir parodo neapibrėžtumą."]],
  [/^(.+) shipped with (.+) kWh usable packs — the charging-energy proxy is consistent with the (.+) kWh variant, but cannot measure remaining capacity\.$/,["$1 wurde mit nutzbaren Batterien von $2 kWh ausgeliefert — die Ladeenergie-Näherung passt zur $3-kWh-Variante, kann aber die verbleibende Kapazität nicht messen.","$1 werd geleverd met bruikbare accu's van $2 kWh — de laadenergieproxy past bij de variant van $3 kWh, maar kan de resterende capaciteit niet meten.","$1 buvo tiekiamas su $2 kWh naudingosios talpos baterijomis — įkrovimo energijos pakaitinis rodiklis atitinka $3 kWh variantą, bet negali išmatuoti likusios talpos."]],
  [/^(.+) shipped with (.+) kWh usable packs — the measured capacity matches the (.+) kWh pack\.$/,["$1 wurde mit nutzbaren Batterien von $2 kWh ausgeliefert — die gemessene Kapazität passt zur $3-kWh-Batterie.","$1 werd geleverd met bruikbare accu's van $2 kWh — de gemeten capaciteit past bij de accu van $3 kWh.","$1 buvo tiekiamas su $2 kWh naudingosios talpos baterijomis — išmatuota talpa atitinka $3 kWh bateriją."]],
  [/^Reported charging energy \/ SoC gained has a (.+) kWh median(.*), but the export does not document where energy is metered; charging losses, auxiliaries and 1% SoC rounding mean this ratio cannot support a battery-capacity or SoH conclusion\.$/,["Gemeldete Ladeenergie / SoC-Zuwachs hat einen Median von $1 kWh$2, aber der Export dokumentiert den Messpunkt nicht; Ladeverluste, Nebenverbraucher und die SoC-Rundung auf 1 % verhindern eine Aussage zu Batteriekapazität oder SOH.","Gemelde laadenergie / gewonnen SoC heeft een mediaan van $1 kWh$2, maar de export documenteert niet waar de energie wordt gemeten; laadverliezen, hulpverbruikers en SoC-afronding op 1% maken een conclusie over accucapaciteit of SOH onmogelijk.","Praneštos įkrovimo energijos / SoC prieaugio mediana yra $1 kWh$2, tačiau eksportas nenurodo energijos matavimo vietos; įkrovimo nuostoliai, pagalbiniai vartotojai ir SoC apvalinimas iki 1 % neleidžia spręsti apie baterijos talpą ar SOH."]],
  [/^This table divides the vehicle's reported session energy by SoC gained\. The JSON does not document where that energy is metered, and charging losses, auxiliaries and 1% SoC rounding affect the ratio\. (.*)\. The descriptive median is (.+) kWh; it is not used as battery capacity or state of health\.$/,["Diese Tabelle teilt die vom Fahrzeug gemeldete Sitzungsenergie durch den SoC-Zuwachs. Die JSON-Datei dokumentiert den Messpunkt nicht; Ladeverluste, Nebenverbraucher und die SoC-Rundung auf 1 % beeinflussen das Verhältnis. $1. Der beschreibende Median beträgt $2 kWh; er wird nicht als Batteriekapazität oder SOH verwendet.","Deze tabel deelt de door het voertuig gemelde sessie-energie door de gewonnen SoC. De JSON documenteert niet waar de energie wordt gemeten; laadverliezen, hulpverbruikers en SoC-afronding op 1% beïnvloeden de verhouding. $1. De beschrijvende mediaan is $2 kWh; deze wordt niet gebruikt als accucapaciteit of SOH.","Ši lentelė dalija automobilio praneštą sesijos energiją iš SoC prieaugio. JSON nenurodo energijos matavimo vietos; santykį veikia įkrovimo nuostoliai, pagalbiniai vartotojai ir SoC apvalinimas iki 1 %. $1. Aprašomoji mediana yra $2 kWh; ji nenaudojama kaip baterijos talpa ar SOH."]],
  [/^Elapsed is charging start to stop; active excludes pauses; plugged-in time uses connection to disconnection\. Average power is the vehicle's reported figure and may be based on active time\.$/,["Dauer bezeichnet Ladebeginn bis Ladeende; aktiv schließt Pausen aus; angeschlossene Zeit reicht vom Verbinden bis zum Trennen. Die Durchschnittsleistung ist der vom Fahrzeug gemeldete Wert und kann auf der aktiven Zeit basieren.","Verstreken tijd loopt van laadstart tot laadstop; actief sluit pauzes uit; aangesloten tijd loopt van aansluiten tot loskoppelen. Gemiddeld vermogen is de door het voertuig gemelde waarde en kan op actieve tijd zijn gebaseerd.","Trukmė skaičiuojama nuo įkrovimo pradžios iki pabaigos; aktyvus laikas neapima pauzių; prijungimo laikas — nuo prijungimo iki atjungimo. Vidutinę galią praneša automobilis ir ji gali būti pagrįsta aktyviu laiku."]],
  [/^Raw export spans (.+) to (.+); high-volume diagnostics span (.+) to (.+)$/,["Rohdatenexport umfasst $1 bis $2; umfangreiche Diagnosedaten umfassen $3 bis $4","Ruwe export loopt van $1 tot $2; omvangrijke diagnostiek loopt van $3 tot $4","Neapdorotas eksportas apima $1–$2; didelės apimties diagnostika apima $3–$4"]],
  [/^Cells are well balanced \((.+) mV median spread, (.+) mV worst\)$/,["Die Zellen sind gut ausgeglichen ($1 mV Median-Spannweite, $2 mV schlechtester Wert)","De cellen zijn goed in balans ($1 mV mediane spreiding, $2 mV slechtste waarde)","Celės gerai subalansuotos ($1 mV sklaidos mediana, $2 mV blogiausia reikšmė)"]],
  [/^Cell imbalance is elevated but acceptable \((.+) mV median, (.+) mV worst\)$/,["Die Zellabweichung ist erhöht, aber akzeptabel ($1 mV Median, $2 mV schlechtester Wert)","De celonbalans is verhoogd maar aanvaardbaar ($1 mV mediaan, $2 mV slechtste waarde)","Celių disbalansas padidėjęs, bet priimtinas ($1 mV mediana, $2 mV blogiausia reikšmė)"]],
  [/^Cell imbalance is high \((.+) mV median, (.+) mV worst\) — worth a service check$/,["Die Zellabweichung ist hoch ($1 mV Median, $2 mV schlechtester Wert) — eine Werkstattprüfung ist sinnvoll","De celonbalans is hoog ($1 mV mediaan, $2 mV slechtste waarde) — controle bij een garage is verstandig","Celių disbalansas didelis ($1 mV mediana, $2 mV blogiausia reikšmė) — verta patikrinti servise"]],
  [/^Measured usable capacity ≈ (.+) kWh — about (.+)% of the (.+) kWh nominal pack$/,["Gemessene nutzbare Kapazität ≈ $1 kWh — etwa $2 % der nominalen $3-kWh-Batterie","Gemeten bruikbare capaciteit ≈ $1 kWh — ongeveer $2% van de nominale accu van $3 kWh","Išmatuota naudingoji talpa ≈ $1 kWh — apie $2 % nominalios $3 kWh baterijos"]],
  [/^Measured usable capacity ≈ (.+) kWh \(~(.+)% of nominal\) — normal aging$/,["Gemessene nutzbare Kapazität ≈ $1 kWh (~$2 % des Nennwerts) — normale Alterung","Gemeten bruikbare capaciteit ≈ $1 kWh (~$2% van nominaal) — normale veroudering","Išmatuota naudingoji talpa ≈ $1 kWh (~$2 % nominalios) — įprastas senėjimas"]],
  [/^Measured usable capacity ≈ (.+) kWh \(~(.+)% of nominal\) — approaching the 70% warranty threshold$/,["Gemessene nutzbare Kapazität ≈ $1 kWh (~$2 % des Nennwerts) — nähert sich der 70-%-Garantiegrenze","Gemeten bruikbare capaciteit ≈ $1 kWh (~$2% van nominaal) — nadert de garantiedrempel van 70%","Išmatuota naudingoji talpa ≈ $1 kWh (~$2 % nominalios) — artėja prie 70 % garantinės ribos"]],
  [/^How capacity was measured: battery current × pack voltage integrated over each charging session, divided by the SoC gained — measured at the battery terminals\. The median \((.+) kWh\) drives derived energy figures; wider SoC windows and fuller current coverage make an estimate more reliable\.$/,["So wurde die Kapazität gemessen: Batteriestrom × Batteriespannung über jeden Ladevorgang integriert, geteilt durch den SoC-Zuwachs — gemessen an den Batterieklemmen. Der Median ($1 kWh) bestimmt die abgeleiteten Energiewerte; größere SoC-Fenster und vollständigere Stromabdeckung machen die Schätzung zuverlässiger.","Zo is de capaciteit gemeten: accustroom × accuspanning geïntegreerd over elke laadsessie, gedeeld door de gewonnen SoC — gemeten aan de accupolen. De mediaan ($1 kWh) bepaalt de afgeleide energiewaarden; grotere SoC-vensters en vollere stroomdekking maken de schatting betrouwbaarder.","Talpa išmatuota taip: kiekvienos įkrovimo sesijos baterijos srovė × baterijos įtampa integruota ir padalyta iš SoC prieaugio — matuota ties baterijos gnybtais. Mediana ($1 kWh) naudojama išvestiniams energijos dydžiams; platesni SoC intervalai ir išsamesnė srovės aprėptis didina įverčio patikimumą."]],
  [/^Time \((.+)\)$/,["Zeit ($1)","Tijd ($1)","Laikas ($1)"]]
];

function setDashboardLanguage(value){
  const lang = String(value || "en").slice(0,2).toLowerCase();
  DASHBOARD_LANGUAGE = DASHBOARD_LANGUAGE_TAGS[lang] ? lang : "en";
  return DASHBOARD_LANGUAGE;
}
function dashboardLocale(){ return DASHBOARD_LANGUAGE_TAGS[DASHBOARD_LANGUAGE] || DASHBOARD_LANGUAGE_TAGS.en; }
function dashboardTranslation(value){
  if (value == null || DASHBOARD_LANGUAGE === "en") return value == null ? value : String(value);
  const source = String(value), idx = {de:0,nl:1,lt:2}[DASHBOARD_LANGUAGE];
  if (DASHBOARD_TEXT[source]) return DASHBOARD_TEXT[source][idx];
  let out = source;
  for (const [pattern, translations] of DASHBOARD_PATTERNS){
    if (pattern.test(source)) { out = source.replace(pattern, translations[idx]); break; }
  }
  const entries = Object.entries(DASHBOARD_FRAGMENTS).sort((a,b) => b[0].length - a[0].length);
  for (const [from, translations] of entries) if (out.includes(from)) out = out.split(from).join(translations[idx]);
  const inline = Object.entries(DASHBOARD_TEXT).filter(([key]) => key.length >= 20)
    .sort((a,b) => b[0].length - a[0].length);
  for (const [from, translations] of inline) if (out.includes(from)) out = out.split(from).join(translations[idx]);
  return out;
}
function localizeDashboard(root){
  if (!root || DASHBOARD_LANGUAGE === "en") return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes){
    const parent = node.parentElement;
    if (!parent || parent.closest("[data-no-dashboard-i18n]") ||
        parent.matches("table.configTable td:nth-child(2), table.configTable td:nth-child(4), table.configTable td:nth-child(6)") ||
        parent.matches("table.eventTable td:nth-child(3), table.eventTable td:nth-child(4)") ||
        parent.matches("table.expectedFieldsTable td:nth-child(1)") ||
        parent.matches("#invWrap td:nth-child(1), #invWrap td:nth-child(4), #invWrap td:nth-child(5), #invWrap td:nth-child(6)")) continue;
    const match = node.nodeValue.match(/^(\s*)([\s\S]*?)(\s*)$/);
    if (match && match[2]) node.nodeValue = match[1] + dashboardTranslation(match[2]) + match[3];
  }
  for (const element of root.querySelectorAll("[title], [aria-label]")){
    for (const attr of ["title","aria-label"]){
      if (element.hasAttribute(attr)) element.setAttribute(attr, dashboardTranslation(element.getAttribute(attr)));
    }
  }
}
function dashboardWeekdays(){
  const fmt = new Intl.DateTimeFormat(dashboardLocale(), {weekday:"short",timeZone:"UTC"});
  return Array.from({length:7}, (_,i) => fmt.format(new Date(Date.UTC(2024,0,1+i))));
}
const REPORT_LANGUAGE = "__REPORT_LANGUAGE__";
setDashboardLanguage(REPORT_LANGUAGE);
document.documentElement.lang = DASHBOARD_LANGUAGE;
document.title = document.querySelector("header.page h1").textContent + " — " + dashboardTranslation("vehicle data");

/* ---------- helpers ---------- */
function loc(t){ return new Date((t + OFF) * 1000); }        // read with UTC getters
const shortDateFmt = new Intl.DateTimeFormat(dashboardLocale(), {month:"short",day:"numeric",timeZone:"UTC"});
const dateTimeFmt = new Intl.DateTimeFormat(dashboardLocale(), {year:"numeric",month:"2-digit",day:"2-digit",
  hour:"2-digit",minute:"2-digit",hourCycle:"h23",timeZone:"UTC"});
function fmtD(t){ return shortDateFmt.format(loc(t)); }
function fmtDT(t){ const d = loc(t); return shortDateFmt.format(d) + ", " +
    String(d.getUTCHours()).padStart(2,"0") + ":" + String(d.getUTCMinutes()).padStart(2,"0"); }
function fmtFull(t){ return t ? dateTimeFmt.format(loc(t)) : dashboardTranslation("Undated"); }
function fmtT(t){ const d = loc(t);
  return String(d.getUTCHours()).padStart(2,"0") + ":" + String(d.getUTCMinutes()).padStart(2,"0"); }
function fmtN(v, dec){ return Number(v).toLocaleString(dashboardLocale(),
  {minimumFractionDigits:dec||0, maximumFractionDigits:dec||0}); }
function dayKeyToT(k){ return Date.parse(k + "T00:00:00Z") / 1000 - OFF; }
function el(tag, cls, text){ const e = document.createElement(tag);
  if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const PROV_TIP = { observed:"Directly present in the export",
  derived:"Calculated from observed samples",
  inferred:"Undocumented channel meaning or assumption" };
function prov(kind){ const e = el("span","prov " + kind,kind); e.title = PROV_TIP[kind] || ""; return e; }
function svgEl(tag, attrs){ const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; }
function niceTicks(lo, hi, n){
  if (lo === hi){ hi = lo + 1; }
  const span = hi - lo, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  let step = mag; for (const m of [1,2,2.5,5,10]) if (m*mag >= step0){ step = m*mag; break; }
  const out = []; for (let v = Math.ceil(lo/step)*step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function dayTicks(t0, t1, maxN){
  const out = []; const d0 = loc(t0); d0.setUTCHours(0,0,0,0);
  const days = Math.ceil((t1 - t0) / 86400);
  const step = Math.max(1, Math.ceil(days / maxN));
  for (let t = d0.getTime()/1000 - OFF; t <= t1; t += step*86400) if (t >= t0) out.push(t);
  return out;
}
function timeTicks(t0, t1, maxN){
  const span = t1 - t0;
  if (span > 2 * 86400) return dayTicks(t0, t1, maxN).map(t => [t, fmtD(t)]);
  const steps = [900, 1800, 3600, 7200, 10800, 21600, 43200];
  const step = steps.find(s => span / s <= Math.max(2, maxN)) || 86400;
  const out = [];
  for (let t = Math.ceil((t0 + OFF)/step)*step - OFF; t <= t1; t += step) if (t >= t0) out.push([t, fmtT(t)]);
  return out;
}

/* ---------- theme ---------- */
const themeBtn = document.getElementById("themeBtn");
const themes = ["auto","light","dark"]; let themeIdx = 0;
themeBtn.addEventListener("click", () => {
  themeIdx = (themeIdx + 1) % 3; const t = themes[themeIdx];
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  themeBtn.textContent = "Theme: " + t;
});

/* ---------- tooltip ---------- */
function makeTip(viz){
  const tt = el("div","tt"); viz.appendChild(tt);
  return {
    show(px, py, whenTxt, rows){
      tt.textContent = "";
      if (whenTxt) tt.appendChild(el("div","when", dashboardTranslation(whenTxt)));
      for (const r of rows){
        const row = el("div","row");
        if (r.color){ const k = el("span","key"); k.style.borderTopColor = r.color; row.appendChild(k); }
        row.appendChild(el("span","v", dashboardTranslation(r.value)));
        if (r.name) row.appendChild(el("span","n", dashboardTranslation(r.name)));
        tt.appendChild(row);
      }
      tt.style.display = "block";
      const w = viz.clientWidth, tw = tt.offsetWidth;
      tt.style.left = Math.min(Math.max(4, px + 14), w - tw - 4) + "px";
      tt.style.top = Math.max(0, py - tt.offsetHeight - 12) + "px";
    },
    hide(){ tt.style.display = "none"; }
  };
}

/* ---------- chart card scaffold ---------- */
function card(parent, title, sub, tableSpec, provenance){
  const c = el("div","card"), h = el("header"), g = el("div","grow");
  g.appendChild(el("h2", null, title));
  if (sub) g.appendChild(el("div","sub", sub));
  h.appendChild(g);
  if (provenance) h.appendChild(prov(provenance));
  const viz = el("div","viz");
  let tw = null;
  if (tableSpec){
    const btn = el("button","tblBtn","Table"); btn.type = "button";
    btn.setAttribute("aria-pressed","false");
    h.appendChild(btn);
    tw = el("div","tableWrap"); tw.style.display = "none";
    tw.appendChild(buildTable(tableSpec.head, tableSpec.rows || [], tableSpec.numCols));
    btn.addEventListener("click", () => {
      const on = btn.getAttribute("aria-pressed") === "true";
      btn.setAttribute("aria-pressed", String(!on));
      tw.style.display = on ? "none" : "block";
      viz.style.display = on ? "block" : "none";
    });
  }
  c.appendChild(h); c.appendChild(viz);
  if (tw) c.appendChild(tw);
  parent.appendChild(c);
  return { root:c, viz,
    setRows(rows){ if (!tw) return; tw.textContent = "";
      tw.appendChild(buildTable(tableSpec.head, rows, tableSpec.numCols)); },
    setSub(t){ const s = g.querySelector(".sub"); if (s) s.textContent = t; } };
}
function buildTable(head, rows, numCols){
  const t = el("table","dv"), tr = el("tr");
  head.forEach((hd,i) => tr.appendChild(el("th", (numCols||[]).includes(i) ? "num" : null, hd)));
  const thead = el("thead"); thead.appendChild(tr); t.appendChild(thead);
  const tb = el("tbody");
  for (const r of rows){ const row = el("tr");
    r.forEach((cell,i) => row.appendChild(el("td",(numCols||[]).includes(i) ? "num" : null, String(cell))));
    tb.appendChild(row); }
  t.appendChild(tb);
  return t;
}
function metricStrip(items){
  const wrap = el("div","metricStrip");
  for (const [label,value] of items){ const cell = el("div");
    cell.appendChild(el("div","n",value)); cell.appendChild(el("div","l",label)); wrap.appendChild(cell); }
  return wrap;
}
function setMetrics(root,items){ const old = root.querySelector(".metricStrip"); if (old) old.remove();
  root.appendChild(metricStrip(items)); }
function quantile(values,p){ if (!values.length) return null; const a=values.slice().sort((x,y)=>x-y);
  return a[Math.round((a.length-1)*p)]; }

/* ---------- line chart ---------- */
function lineChart(viz, cfg){
  viz.textContent = "";
  const W = Math.max(320, viz.clientWidth), H = cfg.h || 240;
  const padL = 44, padR = cfg.padR || 54, padT = 12, padB = 26;
  const svg = svgEl("svg",{ viewBox:`0 0 ${W} ${H}`, width:W, height:H });
  viz.appendChild(svg);
  const x0 = padL, x1 = W - padR, y0 = H - padB, y1 = padT;
  const t0 = cfg.t0, t1 = cfg.t1;
  const X = t => x0 + (t - t0) / (t1 - t0 || 1) * (x1 - x0);
  const vis = cfg.series.filter(s => s.on !== false && s.pts.length);
  let lo = cfg.yMin, hi = cfg.yMax;
  if (lo == null || hi == null){
    let a = Infinity, b = -Infinity;
    for (const s of vis) for (const p of s.pts){ if (p[1] < a) a = p[1]; if (p[1] > b) b = p[1]; }
    if (a === Infinity){ a = 0; b = 1; }
    const m = (b - a) * 0.08 + 0.01;
    if (lo == null) lo = a - m; if (hi == null) hi = b + m;
  }
  const yt = niceTicks(lo, hi, cfg.yTickN || 4);
  lo = Math.min(lo, yt[0]); hi = Math.max(hi, yt[yt.length-1]);
  const tickDec = Math.max(cfg.yDec||0, (yt.length > 1 && yt[1]-yt[0] < 1) ? 1 : 0);
  const Y = v => y0 - (v - lo) / (hi - lo || 1) * (y0 - y1);
  const grid = svgEl("g",{class:"grid"});
  for (const v of yt){
    grid.appendChild(svgEl("line",{x1:x0,x2:x1,y1:Y(v),y2:Y(v)}));
    const tx = svgEl("text",{x:x0-7,y:Y(v)+3.5,"text-anchor":"end"});
    tx.textContent = fmtN(v, tickDec); svg.appendChild(tx);
  }
  svg.appendChild(grid);
  svg.appendChild(svgEl("line",{class:"base",x1:x0,x2:x1,y1:y0,y2:y0}));
  const xTicks = cfg.xTicks || timeTicks(t0,t1,Math.floor((x1-x0)/78));
  for (const [t, lbl] of xTicks){
    const tx = svgEl("text",{x:X(t),y:H-8,"text-anchor":"middle"});
    tx.textContent = lbl; svg.appendChild(tx);
  }
  const gap = (cfg.gapH || 12) * 3600;
  const endLbls = [];
  vis.forEach((s) => {
    let d = "", bridge = "", prevP = null;
    for (const p of s.pts){
      if (prevP != null && p[0] - prevP[0] > gap){
        d += " M";
        bridge += "M" + X(prevP[0]).toFixed(1) + " " + Y(prevP[1]).toFixed(1) +
                  " L" + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1) + " ";
      } else {
        d += d ? " L" : "M";
      }
      d += X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1);
      prevP = p;
    }
    /* dashed bridges across not-reported periods — visually continuous, honestly distinct */
    if (bridge){
      const bp = svgEl("path",{class:(s.cls==="s2"?"l2":"l1") + " bridge", d:bridge});
      if (s.color) bp.style.stroke = s.color;
      svg.appendChild(bp);
    }
    const path = svgEl("path",{class:s.cls==="s2"?"l2":"l1", d});
    if (s.color) path.style.stroke = s.color;
    svg.appendChild(path);
    const lastP = s.pts[s.pts.length-1];
    svg.appendChild(svgEl("circle",{class:"ring",cx:X(lastP[0]),cy:Y(lastP[1]),r:6}));
    const dot = svgEl("circle",{class:s.cls==="s2"?"f2":"f1",cx:X(lastP[0]),cy:Y(lastP[1]),r:4});
    if (s.color) dot.style.fill = s.color;
    svg.appendChild(dot);
    endLbls.push({x:X(lastP[0])+8, y:Y(lastP[1])+3.5,
      text:fmtN(lastP[1], cfg.yDec||0) + (cfg.unitShort||"")});
  });
  /* direct end labels — skipped when they would collide (legend/tooltip/table carry the values) */
  const collide = endLbls.some((a,i) => endLbls.some((b,j) => j > i && Math.abs(a.y-b.y) < 14));
  if (!collide) for (const l of endLbls){
    const lbl = svgEl("text",{class:"endlbl",x:l.x,y:l.y});
    lbl.textContent = l.text; svg.appendChild(lbl);
  }
  /* crosshair */
  const tip = makeTip(viz);
  const xs = vis.length ? vis[0].pts.map(p => p[0]) : [];
  const hl = svgEl("line",{class:"xhair",y1:y1,y2:y0,x1:-9,x2:-9});
  svg.appendChild(hl);
  const ov = svgEl("rect",{x:x0,y:y1,width:x1-x0,height:y0-y1,fill:"transparent",tabindex:"0"});
  svg.appendChild(ov);
  let idx = -1;
  function showIdx(i){
    if (i < 0 || !xs.length){ tip.hide(); hl.setAttribute("x1",-9); hl.setAttribute("x2",-9); return; }
    idx = i; const t = xs[i]; const px = X(t);
    hl.setAttribute("x1",px); hl.setAttribute("x2",px);
    const rows = [];
    const css = getComputedStyle(document.documentElement);
    for (const s of vis){
      let best = null, bd = Infinity;
      for (const p of s.pts){ const d = Math.abs(p[0]-t); if (d < bd){ bd = d; best = p; } }
      if (best && bd <= gap) rows.push({
        color:s.color || css.getPropertyValue(s.cls==="s2"?"--s2":"--s1").trim(),
        value:fmtN(best[1], cfg.ttDec != null ? cfg.ttDec : (cfg.yDec||0)) + (cfg.unitShort||""),
        name:s.name });
    }
    const tipTitle = cfg.xTip ? cfg.xTip(t) : fmtDT(t);
    tip.show(px, Y(hi)+30, tipTitle + (rows.length?"":" — no data"), rows);
  }
  function nearest(t){
    let lo2 = 0, hi2 = xs.length-1;
    while (hi2 - lo2 > 1){ const m = (lo2+hi2)>>1; (xs[m] < t) ? lo2 = m : hi2 = m; }
    return (t - xs[lo2] <= xs[hi2] - t) ? lo2 : hi2;
  }
  ov.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect();
    const t = t0 + (e.clientX - r.left - x0) / (x1 - x0) * (t1 - t0);
    if (xs.length) showIdx(nearest(t));
  });
  ov.addEventListener("pointerleave", () => showIdx(-1));
  ov.addEventListener("keydown", e => {
    if (e.key === "ArrowRight"){ showIdx(Math.min(xs.length-1, (idx<0?0:idx+1))); e.preventDefault(); }
    if (e.key === "ArrowLeft"){ showIdx(Math.max(0, (idx<0?xs.length-1:idx-1))); e.preventDefault(); }
  });
  ov.addEventListener("blur", () => showIdx(-1));
}

/* ---------- column chart ---------- */
function colChart(viz, cfg){
  viz.textContent = "";
  const W = Math.max(320, viz.clientWidth), H = cfg.h || 240;
  const padL = 40, padR = 10, padT = 14, padB = 26;
  const svg = svgEl("svg",{ viewBox:`0 0 ${W} ${H}`, width:W, height:H });
  viz.appendChild(svg);
  const x0 = padL, x1 = W - padR, y0 = H - padB, y1 = padT;
  const items = cfg.items;
  const n = Math.max(1, items.length);
  const band = (x1 - x0) / n;
  const bw = Math.min(24, Math.max(3, band - 2));
  let hi = Math.max(1, ...items.map(d => d.v));
  const yt = niceTicks(0, hi, 4); hi = Math.max(hi, yt[yt.length-1]);
  const tickDec = (yt[1] - yt[0]) < 1 ? 1 : 0;
  const Y = v => y0 - v / hi * (y0 - y1);
  const grid = svgEl("g",{class:"grid"});
  for (const v of yt){ if (v === 0) continue;
    grid.appendChild(svgEl("line",{x1:x0,x2:x1,y1:Y(v),y2:Y(v)}));
    const tx = svgEl("text",{x:x0-7,y:Y(v)+3.5,"text-anchor":"end"});
    tx.textContent = fmtN(v, tickDec); svg.appendChild(tx);
  }
  svg.appendChild(grid);
  svg.appendChild(svgEl("line",{class:"base",x1:x0,x2:x1,y1:y0,y2:y0}));
  const lblEvery = Math.ceil(items.length / Math.floor((x1-x0)/62));
  const tip = makeTip(viz);
  let maxIdx = 0; items.forEach((d,i) => { if (d.v > items[maxIdx].v) maxIdx = i; });
  items.forEach((d, i) => {
    const cx = x0 + band * i + band/2;
    const h = y0 - Y(d.v);
    const r = Math.min(2, h), x = cx - bw/2, y = Y(d.v);
    const path = h <= 0 ? null : svgEl("path",{class:"f1",
      d:`M${x} ${y0} L${x} ${y+r} Q${x} ${y} ${x+r} ${y} L${x+bw-r} ${y} Q${x+bw} ${y} ${x+bw} ${y+r} L${x+bw} ${y0} Z`});
    if (path) svg.appendChild(path);
    if (i === maxIdx && d.v > 0 && cfg.labelMax !== false){
      const lb = svgEl("text",{class:"barlbl",x:cx,y:y-5,"text-anchor":"middle"});
      lb.textContent = fmtN(d.v) + (cfg.unitShort||""); svg.appendChild(lb);
    }
    if (i % lblEvery === 0){
      const tx = svgEl("text",{x:cx,y:H-8,"text-anchor":"middle"});
      tx.textContent = d.label; svg.appendChild(tx);
    }
    const hit = svgEl("rect",{x:x0+band*i,y:y1,width:band,height:y0-y1,fill:"transparent"});
    hit.addEventListener("pointermove", () => {
      if (path) path.style.opacity = ".75";
      tip.show(cx, y, d.tipWhen || d.label, [{value:fmtN(d.v,cfg.dec||0)+(cfg.unitShort||""), name:cfg.valName, color:getComputedStyle(document.documentElement).getPropertyValue("--s1").trim()}]);
    });
    hit.addEventListener("pointerleave", () => { if (path) path.style.opacity = ""; tip.hide(); });
    svg.appendChild(hit);
  });
}

/* ---------- horizontal bars ---------- */
function hBarChart(viz, cfg){
  viz.textContent = "";
  const W = Math.max(320, viz.clientWidth);
  const rowH = 30, padT = 4;
  const items = cfg.items;
  const H = items.length * rowH + padT + 6;
  const svg = svgEl("svg",{ viewBox:`0 0 ${W} ${H}`, width:W, height:H });
  viz.appendChild(svg);
  const labW = Math.min(210, W * 0.4);
  const x0 = labW, x1 = W - 56;
  const hi = Math.max(...items.map(d => d.v), 1e-9);
  const tip = makeTip(viz);
  items.forEach((d, i) => {
    const cy = padT + rowH * i + rowH/2;
    const lb = svgEl("text",{x:x0-10,y:cy+3.5,"text-anchor":"end"});
    lb.textContent = d.label; lb.style.fill = "var(--ink-2)"; svg.appendChild(lb);
    const bw = Math.max(2, (x1 - x0) * d.v / hi), bh = 16, y = cy - bh/2, r = 2;
    const bar = svgEl("path",{class:"f1",
      d:`M${x0} ${y} L${x0+bw-r} ${y} Q${x0+bw} ${y} ${x0+bw} ${y+r} L${x0+bw} ${y+bh-r} Q${x0+bw} ${y+bh} ${x0+bw-r} ${y+bh} L${x0} ${y+bh} Z`});
    svg.appendChild(bar);
    const val = svgEl("text",{class:"barlbl",x:x0+bw+7,y:cy+3.5});
    val.textContent = d.valText; svg.appendChild(val);
    const hit = svgEl("rect",{x:0,y:padT+rowH*i,width:W,height:rowH,fill:"transparent"});
    hit.addEventListener("pointermove", () => { bar.style.opacity = ".75";
      tip.show(x0+bw, y, d.label, [{value:d.tipText || d.valText}]); });
    hit.addEventListener("pointerleave", () => { bar.style.opacity = ""; tip.hide(); });
    svg.appendChild(hit);
  });
  svg.appendChild(svgEl("line",{class:"base",x1:x0,x2:x0,y1:padT,y2:H-6}));
}

/* ---------- heatmap ---------- */
const SEQ_LIGHT = ["#cde2fb","#9ec5f4","#6da7ec","#3987e5","#256abf","#184f95","#0d366b"];
const SEQ_DARK  = ["#0d366b","#184f95","#256abf","#3987e5","#6da7ec","#9ec5f4","#cde2fb"];
function isDark(){
  const t = document.documentElement.dataset.theme;
  if (t) return t === "dark";
  return matchMedia("(prefers-color-scheme: dark)").matches;
}
function heatChart(viz, cfg){
  viz.textContent = "";
  const W = Math.max(320, viz.clientWidth);
  const padL = 40, padR = 8, padT = 6, rowH = 22;
  const H = 7 * rowH + padT + 44;
  const svg = svgEl("svg",{ viewBox:`0 0 ${W} ${H}`, width:W, height:H });
  viz.appendChild(svg);
  const cw = (W - padL - padR) / 24;
  const max = Math.max(1, ...cfg.grid.flat());
  const seq = isDark() ? SEQ_DARK : SEQ_LIGHT;
  const tip = makeTip(viz);
  for (let d = 0; d < 7; d++){
    const ly = padT + d * rowH + rowH/2 + 3.5;
    const lb = svgEl("text",{x:padL-8,y:ly,"text-anchor":"end"});
    lb.textContent = dashboardWeekdays()[d]; svg.appendChild(lb);
    for (let h = 0; h < 24; h++){
      const n = cfg.grid[d][h];
      const x = padL + h * cw, y = padT + d * rowH;
      let cell;
      if (n === 0){
        cell = svgEl("rect",{class:"hempty",x:x+1,y:y+1,width:cw-2,height:rowH-2,rx:2});
      } else {
        const bin = Math.min(6, Math.floor(Math.sqrt(n) / Math.sqrt(max) * 7));
        cell = svgEl("rect",{class:"hcell",x, y, width:cw, height:rowH, rx:3, fill:seq[bin]});
      }
      cell.addEventListener("pointermove", () => tip.show(x + cw/2, y,
        dashboardWeekdays()[d] + " " + String(h).padStart(2,"0") + ":00–" + String(h+1).padStart(2,"0") + ":00",
        [{value: fmtN(n), name: cfg.unitName}]));
      cell.addEventListener("pointerleave", () => tip.hide());
      svg.appendChild(cell);
    }
  }
  const axY = padT + 7*rowH + 14;
  for (let h = 0; h <= 24; h += 3){
    const tx = svgEl("text",{x:padL + h*cw, y:axY, "text-anchor":"middle"});
    tx.textContent = String(h).padStart(2,"0"); svg.appendChild(tx);
  }
  /* scale legend */
  const lgY = axY + 12, lgX = padL, sw = 16;
  const lo = svgEl("text",{x:lgX, y:lgY+9}); lo.textContent = "fewer"; svg.appendChild(lo);
  for (let i = 0; i < 7; i++)
    svg.appendChild(svgEl("rect",{x:lgX+38+i*(sw+2), y:lgY, width:sw, height:10, rx:2, fill:seq[i]}));
  const hiT = svgEl("text",{x:lgX+38+7*(sw+2)+4, y:lgY+9}); hiT.textContent = "more"; svg.appendChild(hiT);
}

/* ---------- valve actuation timeline ---------- */
function valveTimeline(viz, cfg){
  viz.textContent = "";
  const rows = cfg.rows.filter(r => r.transitions.length);
  if (!rows.length){ viz.appendChild(el("div","sub","No valve samples in this export.")); return; }
  const W = Math.max(320, viz.clientWidth);
  const rowH = 34, padT = 6, padL = Math.min(170, W * 0.32), padR = 12, axH = 22;
  const H = rows.length * rowH + padT + axH;
  const svg = svgEl("svg",{viewBox:`0 0 ${W} ${H}`, width:W, height:H});
  viz.appendChild(svg);
  const x0 = padL, x1 = W - padR;
  const t0 = cfg.t0, t1 = cfg.t1;
  const X = t => x0 + (t - t0) / (t1 - t0 || 1) * (x1 - x0);
  for (const [t, lbl] of timeTicks(t0, t1, Math.floor((x1-x0)/78))){
    const tx = svgEl("text",{x:X(t), y:H-6, "text-anchor":"middle"});
    tx.textContent = lbl; svg.appendChild(tx);
  }
  const tip = makeTip(viz);
  rows.forEach((r, ri) => {
    const cy = padT + ri*rowH + rowH/2;
    const lb = svgEl("text",{x:x0-10, y:cy+3.5, "text-anchor":"end"});
    lb.textContent = r.label; lb.style.fill = "var(--ink-2)"; svg.appendChild(lb);
    svg.appendChild(svgEl("line",{class:"base", x1:x0, x2:x1, y1:cy, y2:cy}));
    const trans = r.transitions;
    for (let i = 0; i < trans.length; i++){
      if (trans[i][1] !== 1) continue;
      const segA = trans[i][0];
      const segB = i + 1 < trans.length ? trans[i+1][0] : (r.lastT != null ? r.lastT : segA);
      const a2 = Math.max(segA, t0), b2 = Math.min(segB, t1);
      if (b2 <= a2) continue;
      const bar = svgEl("rect",{class:"f1", x:X(a2).toFixed(1), y:cy-6,
        width:Math.max(1.5, X(b2)-X(a2)).toFixed(1), height:12, rx:2});
      bar.addEventListener("pointermove", () => tip.show(X(a2), cy-12, r.label,
        [{value: fmtDT(segA) + " – " + fmtDT(segB)}, {value: fmtN((segB-segA)/60) + " min actuated"}]));
      bar.addEventListener("pointerleave", () => tip.hide());
      svg.appendChild(bar);
    }
  });
}

/* ---------- sparkline ---------- */
function sparkline(parent, values){
  const W = 120, H = 28;
  const svg = svgEl("svg",{viewBox:`0 0 ${W} ${H}`, width:W, height:H});
  if (values.length > 1){
    const hi = Math.max(...values, 1);
    const pts = values.map((v,i) => (i/(values.length-1)*W).toFixed(1) + "," + (H-3 - v/hi*(H-6)).toFixed(1));
    svg.appendChild(svgEl("polyline",{points:pts.join(" "), class:"l1", "stroke-width":1.5}));
  }
  parent.appendChild(svg);
}

/* ================= page assembly ================= */
const pretty = value => value == null ? "—" : String(value).replaceAll("_"," ").toLowerCase()
    .replace(/^\w/, c => c.toUpperCase());
document.getElementById("headSub").textContent =
  "VIN " + DATA.vin + " · EU Data Act export · " + fmtN(DATA.nRecords) + " records, " +
  DATA.nFields + " fields · diagnostics " + fmtD(DATA.tMin) + " – " + fmtD(DATA.tMax) +
  " · times in " + DATA.tzLabel + (DATA.identifiersRedacted ? " · identifiers redacted" : "");

/* ---- overview context notes ---- */
(function(){
  const wrap = document.getElementById("ovNotes"); if (!wrap) return;
  const notes = [];
  const lag = DATA.completeness ? DATA.completeness.diagnosticLagDays : null;
  if (lag != null && lag >= 1)
    notes.push("High-frequency diagnostic history ends " + fmtN(lag,1) +
      " days before this package was created — the snapshot cards are newer than the charts and ledgers.");
  if (DATA.status.platform)
    notes.push(DATA.status.platform === "MEB"
      ? "Vehicle platform MEB confirmed by the export — the 96-series battery analysis applies."
      : "Vehicle platform " + DATA.status.platform + " reported — the MEB-specific battery capacity analysis may not apply to this vehicle.");
  if (DATA.packageIncomplete){
    wrap.appendChild(el("div","note crit",
      "⚠ This package is nearly empty — only " + fmtN(DATA.nRecords) + " snapshot records arrived and " +
      "none of the diagnostic or charging history the portal is supposed to deliver. That is a known " +
      "failure of VW's export service, not a problem with your car or with this tool. Request the export " +
      "again from the portal (complete packages often take two or more attempts) and consider complaining " +
      "through the portal's contact form — under the EU Data Act (Arts. 4–5) and GDPR (Arts. 15/20) you " +
      "are entitled to the full data. Everything that did arrive is shown below; the Package audit tab " +
      "lists exactly what is missing to cite."));
    wrap.style.marginBottom = "14px";
  }
  const missing = [];
  if (!DATA.daily.length) missing.push("odometer / distance history");
  if (!DATA.speedRaw.length) missing.push("speed values");
  if (!DATA.cellMax.length && !DATA.cellMin.length) missing.push("cell voltages");
  if (!DATA.current.length) missing.push("battery current");
  if (missing.length && !DATA.packageIncomplete)
    notes.push("Not delivered in this export: " + missing.join(", ") +
      ". The related panels are omitted rather than shown empty" +
      ((DATA.activity || []).length
        ? " — the vehicle-reported charging history and activity timestamps this package does carry are shown instead." : "."));
  if (notes.length) wrap.style.marginBottom = "14px";
  for (const n of notes) wrap.appendChild(el("div","note",n));
})();

/* ---- current status cards ---- */
(function(){
  const S = DATA.status, wrap = document.getElementById("statusCards");
  const prettyValue = value => value == null ? null : pretty(value);
  const itemValue = item => item && item.value != null ? item.value : null;
  const itemTime = item => item && item.time ? item.time : null;
  const yesNo = value => value == null ? null
    : String(value).toLowerCase() === "true" ? "Yes"
    : String(value).toLowerCase() === "false" ? "No" : pretty(value);
  const onOff = value => value == null ? null
    : String(value).toLowerCase() === "true" ? "On"
    : String(value).toLowerCase() === "false" ? "Off" : pretty(value);
  const latestTime = (...items) => {
    const times = items.map(itemTime).filter(Boolean);
    return times.length ? Math.max(...times) : null;
  };
  const capturedLabel = times => {
    times = times.filter(Boolean).sort((a,b) => a-b);
    if (!times.length) return null;
    const first = times[0], last = times[times.length-1];
    return first === last ? "Captured " + fmtDT(first)
      : "Captured between " + fmtDT(first) + " and " + fmtDT(last);
  };
  function rowsCard(title, rows, extraTop){
    const c = el("div","card");
    const h = el("header"), g = el("div","grow"); g.appendChild(el("h2",null,title));
    h.appendChild(g); h.appendChild(prov("observed")); c.appendChild(h);
    if (extraTop) c.appendChild(extraTop);
    const rl = el("div","rows"); rl.style.marginTop = "8px";
    const captureTimes = [];
    for (const [k,v,t] of rows){ if (v == null) continue;
      const r = el("div","r"); r.appendChild(el("span","k",k)); r.appendChild(el("span","v",v));
      if (t){ r.title = "Captured " + fmtFull(t); captureTimes.push(t); }
      rl.appendChild(r); }
    c.appendChild(rl);
    const capture = capturedLabel(captureTimes);
    if (capture) c.appendChild(el("div","foot",capture + " · hover a row for its exact timestamp"));
    wrap.appendChild(c); return c;
  }
  /* battery */
  const meter = el("div","meter");
  const soc = Number(S.soc);
  const track = el("div","track"), fill = el("div","fill");
  fill.style.width = Math.max(0, Math.min(100, soc)) + "%";
  track.appendChild(fill);
  if (S.targetSoc){ const tick = el("div","tick"); tick.style.left = S.targetSoc + "%";
    tick.title = "target " + S.targetSoc + "%"; track.appendChild(tick); }
  meter.appendChild(track);
  const lbls = el("div","lbls");
  lbls.appendChild(el("span",null, soc + "% charged"));
  if (S.targetSoc) lbls.appendChild(el("span",null,"target " + S.targetSoc + "%"));
  meter.appendChild(lbls);
  const bc = rowsCard("Battery & range", [
    ["Estimated range", S.range != null ? fmtN(S.range) + " km" : null],
    ["Battery temperature", S.battTempMin != null ? S.battTempMin + "–" + S.battTempMax + " °C" : null],
    ["Battery care mode", pretty(S.careMode)],
    ["Care mode charge cap", S.careThreshold != null ? S.careThreshold + "%" : null],
    ["Care notification", S.careNotification && S.careNotification !== "INVALID" ? pretty(S.careNotification) : null],
  ], meter);
  /* charging */
  const CP = S.chargingPolicy || {};
  const hideInvalid = value => value == null ||
    ["INVALID","UNDEFINED","UNKNOWN"].indexOf(String(value).toUpperCase()) !== -1 ? null : pretty(value);
  const v2hParts = [];
  if (itemValue(CP.v2hDischarge) != null) v2hParts.push("discharge " + yesNo(itemValue(CP.v2hDischarge)).toLowerCase());
  if (itemValue(CP.v2hHomeStorage) != null) v2hParts.push("home storage " + yesNo(itemValue(CP.v2hHomeStorage)).toLowerCase());
  rowsCard("Charging", [
    ["State", pretty(S.chargeState)],
    ["Charge power", S.chargePower != null ? fmtN(S.chargePower,1) + " kW" : null],
    ["Plug", S.plugConn != null ? pretty(S.plugConn) + " · " + pretty(S.plugLock) : null],
    ["Mode", pretty(S.chargeModeSel)],
    ["Maximum current", prettyValue(itemValue(CP.maxCurrent)), itemTime(CP.maxCurrent)],
    ["Automatic plug unlock", onOff(itemValue(CP.autoUnlock)), itemTime(CP.autoUnlock)],
    ["Charge mode", prettyValue(itemValue(CP.chargeMode)), itemTime(CP.chargeMode)],
    ["Action state", prettyValue(itemValue(CP.actionState)), itemTime(CP.actionState)],
    ["Charge type", hideInvalid(itemValue(CP.chargeType)), itemTime(CP.chargeType)],
    ["Plug type", hideInvalid(itemValue(CP.plugType)), itemTime(CP.plugType)],
    ["Infrastructure", prettyValue(itemValue(CP.infrastructure)), itemTime(CP.infrastructure)],
    ["Bidirectional (V2H)", v2hParts.length ? v2hParts.join(" · ") : null,
      latestTime(CP.v2hDischarge, CP.v2hHomeStorage)],
  ]);
  /* auxiliary & climate load */
  const AX = S.aux || {};
  const auxRows = [
    ["Residual network load", itemValue(AX.residual) != null ? itemValue(AX.residual) + " /h" : null, itemTime(AX.residual)],
    ["Interior climatisation", itemValue(AX.interiorClima) != null ? itemValue(AX.interiorClima) + " /h" : null, itemTime(AX.interiorClima)],
    ["Battery climatisation", itemValue(AX.batteryClima) != null ? itemValue(AX.batteryClima) + " /h" : null, itemTime(AX.batteryClima)],
    ["24h budget start level", itemValue(AX.budgetStartLevel) != null ? itemValue(AX.budgetStartLevel) + "%" : null, itemTime(AX.budgetStartLevel)],
    ["Power budget warning", yesNo(itemValue(AX.warnedPower)), itemTime(AX.warnedPower)],
    ["Daily budget warning", yesNo(itemValue(AX.warnedBudget)), itemTime(AX.warnedBudget)],
  ];
  if (auxRows.some(row => row[1] != null)){
    const auxCard = rowsCard("Auxiliary & climate load", auxRows);
    auxCard.appendChild(el("div","foot",
      "Consumption values are the vehicle's own normalized figures (dictionary unit: 1/h) — comparable between exports, not directly convertible to watts."));
  }
  /* vehicle */
  const PL = S.parkingLights || {};
  const parkingLightValues = [
    itemValue(PL.left) != null ? "Left " + pretty(itemValue(PL.left)).toLowerCase() : null,
    itemValue(PL.right) != null ? "right " + pretty(itemValue(PL.right)).toLowerCase() : null,
  ].filter(Boolean);
  const vc = rowsCard("Vehicle", [
    ["Outdoor temperature", S.outdoorTemp != null ? S.outdoorTemp + " °C" : null],
    ["Parking brake", S.parkingBrake === "true" ? "Engaged" : (S.parkingBrake === "false" ? "Released" : null)],
    ["Parking lights", parkingLightValues.length ? parkingLightValues.join(" · ") : null,
      latestTime(PL.left, PL.right)],
    ["Next service", S.serviceDueDays != null ?
        "in " + fmtN(S.serviceDueDays) + " days" + (S.serviceType ? " (" + pretty(S.serviceType).replace("Service type ","") + ")" : "") : null],
  ]);
  if (S.odometer != null){
    const hero = el("div",null);
    hero.style.cssText = "font-size:48px;font-weight:650;line-height:1.1;margin:2px 0 6px";
    hero.textContent = fmtN(S.odometer);
    const u = el("small",null," km"); u.style.cssText = "font-size:18px;font-weight:500;color:var(--ink-2)";
    hero.appendChild(u);
    vc.insertBefore(hero, vc.children[1]);
  }

  /* climate */
  const CL = S.climate || {};
  const zonePair = (left, right) => {
    const parts = [];
    if (itemValue(left) != null) parts.push("Left " + onOff(itemValue(left)).toLowerCase());
    if (itemValue(right) != null) parts.push("right " + onOff(itemValue(right)).toLowerCase());
    return parts.length ? parts.join(" · ") : null;
  };
  const timerIds = CL.timerIds || [];
  const timerState = itemValue(CL.timerEnabled) != null
    ? onOff(itemValue(CL.timerEnabled)) + (timerIds.length ? " · IDs " + timerIds.join(", ") : "")
    : (timerIds.length ? "IDs " + timerIds.join(", ") : null);
  const climateRows = [
    ["State", prettyValue(itemValue(CL.state)), itemTime(CL.state)],
    ["Trigger", prettyValue(itemValue(CL.trigger)), itemTime(CL.trigger)],
    ["Target", itemValue(CL.target) != null ? fmtN(itemValue(CL.target),1) + " °C" : null,
      itemTime(CL.target)],
    ["Window heating", onOff(itemValue(CL.windowHeating)), itemTime(CL.windowHeating)],
    ["Run without external power", yesNo(itemValue(CL.withoutExternalPower)),
      itemTime(CL.withoutExternalPower)],
    ["Start when unlocking", yesNo(itemValue(CL.atUnlock)), itemTime(CL.atUnlock)],
    ["Front zones", zonePair(CL.zoneFrontLeft, CL.zoneFrontRight),
      latestTime(CL.zoneFrontLeft, CL.zoneFrontRight)],
    ["Rear zones", zonePair(CL.zoneRearLeft, CL.zoneRearRight),
      latestTime(CL.zoneRearLeft, CL.zoneRearRight)],
    ["Reported timer state", timerState,
      Math.max(itemTime(CL.timerEnabled) || 0, CL.timerIdsCapturedAt || 0) || null],
    ["Charge timer option available", yesNo(itemValue(CL.chargeTimerOption)),
      itemTime(CL.chargeTimerOption)],
    ["Charge + climate option", yesNo(itemValue(CL.chargeClimateTimerOption)),
      itemTime(CL.chargeClimateTimerOption)],
  ];
  if (climateRows.some(row => row[1] != null)){
    const climateCard = rowsCard("Climate", climateRows);
    if (timerIds.length > 1 && itemValue(CL.timerEnabled) != null)
      climateCard.appendChild(el("div","foot",
        "Timer IDs are delivered without array indexes; the single reported state cannot be assigned to one specific timer."));
  }

  /* connectivity */
  const CN = S.connectivity || {};
  const shutdown = itemValue(CN.osShutdown);
  const connectivityRows = [
    ["Vehicle connected", yesNo(itemValue(CN.vehicleConnected)), itemTime(CN.vehicleConnected)],
    ["Backend domains active", yesNo(itemValue(CN.activeDomains)), itemTime(CN.activeDomains)],
    ["Communications unit", shutdown == null ? null
      : String(shutdown).toLowerCase() === "true" ? "Shutting down" : "Running", itemTime(CN.osShutdown)],
    ["V2X communication", prettyValue(itemValue(CN.v2x)), itemTime(CN.v2x)],
    ["Connection timestamp", CN.lastConnection ? fmtDT(CN.lastConnection) : null, CN.lastConnection],
  ];
  if (connectivityRows.some(row => row[1] != null)) rowsCard("Connectivity", connectivityRows);

  /* closures */
  const cc = el("div","card"), cch = el("header"), ccg = el("div","grow");
  ccg.appendChild(el("h2",null,"Doors & closures")); cch.appendChild(ccg); cch.appendChild(prov("observed")); cc.appendChild(cch);
  const chips = el("div","chips");
  const closureTimes = [];
  if (S.closures && S.closures.length){
    for (const item of S.closures){
      const parts = [];
      if (item.state != null) parts.push(pretty(item.state).toLowerCase());
      if (item.lock != null) parts.push(pretty(item.lock).toLowerCase());
      if (item.openPct != null) parts.push(fmtN(item.openPct) + "% open");
      if (!parts.length) continue;
      const open = String(item.state).toUpperCase() === "OPEN" || Number(item.openPct) > 0;
      const chip = el("span","chip" + (open ? " warn" : ""));
      chip.appendChild(el("span","dot"));
      chip.appendChild(el("span",null, item.label + ": " + parts.join(" · ")));
      if (item.time){ chip.title = "Captured " + fmtFull(item.time); closureTimes.push(item.time); }
      chips.appendChild(chip);
    }
  } else {
    for (const k in S.doors){ const v = S.doors[k]; if (!v) continue;
      const open = v.toUpperCase() === "OPEN";
      const chip = el("span","chip" + (open ? " warn" : ""));
      chip.appendChild(el("span","dot"));
      chip.appendChild(el("span",null, k + ": " + v.toLowerCase()));
      chips.appendChild(chip); }
  }
  cc.appendChild(chips);
  const closureCapture = capturedLabel(closureTimes);
  cc.appendChild(el("div","foot",(closureCapture ? closureCapture + " · " : "") +
    "snapshot only, not a live vehicle state"));
  if (chips.children.length) wrap.appendChild(cc);
  if (S.capturedAt){
    for (const c of [bc]) c.appendChild(el("div","foot","As of " + fmtDT(S.capturedAt)));
  }
})();

/* ---- range filter state ---- */
let range = [DATA.tMin, DATA.tMax];
const presets = [
  ["All diagnostics", DATA.tMin],
  ["Last 30 days", DATA.tMax - 30*86400],
  ["Last 7 days", DATA.tMax - 7*86400],
];
const fWrap = document.getElementById("filters");
presets.forEach(([name, from], i) => {
  const b = el("button",null,name); b.type = "button";
  b.setAttribute("aria-pressed", String(i === 0));
  b.addEventListener("click", () => {
    fWrap.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed","false"));
    b.setAttribute("aria-pressed","true");
    range = [Math.max(DATA.tMin, from), DATA.tMax];
    renderAll();
  });
  fWrap.appendChild(b);
});

const inR = t => t >= range[0] && t <= range[1];
const fPts = pts => pts.filter(p => inR(p[0]));

/* ---- KPIs ---- */
function renderKpis(){
  const wrap = document.getElementById("kpis"); wrap.textContent = "";
  const days = DATA.daily.filter(d => inR(dayKeyToT(d.d) + 43200));
  const dist = days.reduce((a,d) => a + d.km, 0);
  const drives = DATA.trips.filter(s => inR(s.start));
  const gapKm = days.reduce((a,d) => a + d.gapKm, 0);
  const speeds = fPts(DATA.speedRaw).map(p => p[1]);
  const vmax = speeds.length ? Math.round(Math.max(...speeds)) : null;
  const totDays = Math.max(1, Math.round((range[1] - range[0]) / 86400));
  function tile(label, valueTxt, unit, ctx, sparkVals){
    const c = el("div","card kpi");
    c.appendChild(el("div","label",label));
    const v = el("div","value", valueTxt);
    if (unit){ v.appendChild(el("small",null," " + unit)); }
    c.appendChild(v);
    if (ctx) c.appendChild(el("div","ctx",ctx));
    if (sparkVals) sparkline(c, sparkVals);
    wrap.appendChild(c);
  }
  const cons = DATA.consumption.filter(c => inR(dayKeyToT(c.d) + 43200));
  const cKm = cons.reduce((a,c) => a + c.km, 0);
  const cE = cons.reduce((a,c) => a + c.socUsed, 0) / 100 * DATA.packKwh;
  const chg = DATA.charges.filter(c => inR(c.start));
  const chgE = chg.reduce((a,c) => a + c.kwh, 0);
  const dp = DATA.drainPairs.filter(p => p[0] >= range[0] && p[1] <= range[1]);
  const drainDays = dp.reduce((a,p) => a + (p[1]-p[0]), 0) / 86400;
  const drainRate = drainDays > 0.5 ? dp.reduce((a,p) => a + p[2], 0) / drainDays : null;
  if (DATA.health){
    const KPI_HEALTH = { good:"Healthy", fair:"Normal wear", attention:"Check advised", unknown:"Unknown" };
    const hc = el("div","card kpi");
    hc.appendChild(el("div","label","Battery health"));
    const hv = el("div","value");
    hv.appendChild(el("span","hdot " + (DATA.health.verdict || "unknown")));
    hv.appendChild(document.createTextNode(KPI_HEALTH[DATA.health.verdict] || "Unknown"));
    hc.appendChild(hv);
    const capContext = DATA.measuredKwh
      ? "≈" + DATA.measuredKwh + " kWh usable measured"
      : DATA.capacityProxy
        ? "≈" + DATA.capacityProxy.medianKwh + " kWh charging-energy proxy · SoH unavailable"
        : "capacity not measurable";
    hc.appendChild(el("div","ctx",
      capContext +
      (DATA.spreadStats && DATA.spreadStats.median != null ? " · " + DATA.spreadStats.median + " mV cell imbalance" : "") +
      " — details on the Battery tab"));
    wrap.appendChild(hc);
  }
  if (DATA.daily.length)
    tile("Distance driven", fmtN(dist), "km",
         fmtN(Math.max(0,dist-gapKm)) + " km observed · " + fmtN(gapKm) + " km across sampling gaps",
         days.map(d => d.km));
  if (DATA.consumption.length)
    tile("Avg consumption", cKm ? fmtN(cE / cKm * 100, 1) : "—", "kWh/100km",
         cKm ? "≈ " + fmtN(DATA.packKwh / (cE / cKm * 100) * 100) + " km per full charge at this rate"
             : "days with ≥20 km, using the " + PACK_SHORT + " " + DATA.packKwh + " kWh usable");
  if (DATA.charges.length)
    tile("Energy charged", chgE ? fmtN(chgE) : "—", "kWh",
         chg.length + " charge events" + (DATA.priceKwh && chgE
           ? " · ~" + DATA.currency + fmtN(chgE * DATA.priceKwh) + " at " + DATA.currency + DATA.priceKwh + "/kWh" : ""));
  const ivTrips = drives.filter(s => s.tractionKwh != null);
  const tracE = ivTrips.reduce((a,s) => a + s.tractionKwh, 0);
  const regenE = ivTrips.reduce((a,s) => a + s.regenKwh, 0);
  if (DATA.trips.some(s => s.tractionKwh != null))
    tile("Regen recovered", tracE > 0 ? fmtN(regenE / tracE * 100) : "—", "%",
         tracE > 0 ? "~" + fmtN(regenE,1) + " kWh recovered of " + fmtN(tracE,1) +
           " kWh traction (∫I·V over " + ivTrips.length + " trips)"
         : "needs trips with battery-current coverage");
  if (DATA.drainPairs.length)
    tile("Idle drain", drainRate != null ? fmtN(drainRate, 1) : "—", "%/day",
         "SoC lost while parked ≥ 8 h (" + fmtN(drainDays) + " days observed)");
  if (DATA.daily.length)
    tile("Days with driving", String(days.filter(d => d.km > 0).length), null, "of " + totDays + " days in range");
  if (DATA.trips.length)
    tile("Observed trips", String(drives.length), null, "odometer movement with ≤30 min sample continuity");
  if (DATA.speedRaw.length)
    tile("Top speed", vmax != null ? fmtN(vmax) : "—", "km/h", "highest sampled in range");
}

/* ---- charts ---- */
const driveChartsWrap = document.getElementById("driveCharts");
const batteryChartsWrap = document.getElementById("batteryCharts");
const thermalChartsWrap = document.getElementById("thermalCharts");
let cards = null;
function makeCards(){
  driveChartsWrap.textContent = ""; batteryChartsWrap.textContent = ""; thermalChartsWrap.textContent = "";
  const c = {};
  c.daily = card(driveChartsWrap, "Distance added to odometer", "Daily allocation reconciles to the full odometer delta; hatched context is listed in the table as sampling-gap km", {
    head:["Date","km","Across gaps"], numCols:[1,2] }, "derived");
  c.heat = card(driveChartsWrap,
    DATA.speedRaw.length ? "When the car is driven" : "When the vehicle was reporting",
    (DATA.speedRaw.length ? "Speed samples" : "Timestamp-only " + (DATA.activitySource || "activity") +
      " records; values were withheld, so these are not confirmed trips") +
    " by weekday and hour, " + DATA.tzLabel, {
    head:["Day", ...Array.from({length:24},(_,h)=>String(h))], numCols:Array.from({length:24},(_,h)=>h+1) }, "derived");
  c.hist = card(driveChartsWrap, "Speed distribution", "Share of observed moving samples; irregular sampling means this is not exact time share", {
    head:["Speed band","Samples","Share"], numCols:[1,2] }, "derived");
  c.chgCurves = card(driveChartsWrap, "Charging power curves",
    (DATA.charges.length && DATA.charges[0].confidence === "observed"
      ? "charge power per session as reported by the vehicle, 5-min averages"
      : "battery-side charge power per session, 5-min averages — taper and pauses become visible"), {
    head:["Start","Type","SoC","Peak kW","Curve points"], numCols:[3,4] });
  c.chgCurves.root.style.gridColumn = "1 / -1";
  c.chgSocCurves = card(driveChartsWrap, "Charging power by state of charge",
    "vehicle-reported power against SoC for sessions that include an SoC curve", {
    head:["Start","Type","SoC range","Peak kW","Curve points"], numCols:[3,4] }, "observed");
  c.chgSocCurves.root.style.gridColumn = "1 / -1";
  if (DATA.chargedDaily && DATA.chargedDaily.length){
    c.chgDaily = card(driveChartsWrap, "Energy charged per day",
      "kWh per day from the vehicle's own daily aggregation", {
      head:["Date","kWh"], numCols:[1] }, "observed");
  }
  if (DATA.chargedMonthly && DATA.chargedMonthly.length){
    c.chgMonthly = card(driveChartsWrap, "Energy charged per month",
      "kWh per month from the vehicle's own monthly aggregation", {
      head:["Month","kWh"], numCols:[1] }, "observed");
  }
  c.cons = card(driveChartsWrap, "Energy consumption per day",
    "kWh/100km from SoC drop while driving, using the " + PACK_SHORT + " " + DATA.packKwh + " kWh usable capacity — days with ≥20 km", {
    head:["Date","km","SoC used","kWh/100km","Ambient"], numCols:[1,2,3,4] }, "derived");

  const socStructured = DATA.socSource === "structured_charging";
  const socMixed = DATA.socSource === "mixed";
  c.soc = card(batteryChartsWrap, "Battery state of charge",
    socStructured
      ? "%, observed only at charging-session boundaries and in recent charging curves; not continuous driving history"
      : socMixed
        ? "%, combined diagnostic samples and observed charging-session values"
        : "%, inferred diagnostic channel 180886 — sampled only while the car is awake", {
    head:["Time (" + DATA.tzLabel + ")","SoC %"], numCols:[1] }, socStructured ? "observed" : "inferred");
  c.cells = card(batteryChartsWrap, "Highest vs lowest cell voltage", "mV, 20-min averages of undocumented channels 543765 / 545776", {
    head:["Time","Highest mV","Lowest mV"], numCols:[1,2] }, "inferred");
  c.spread = card(batteryChartsWrap, "Battery cell imbalance",
    "daily median spread between highest and lowest cell, mV — small and stable is healthy", {
    head:["Date","Median spread mV"], numCols:[1] }, "derived");
  c.current = card(batteryChartsWrap, "HV battery current", "A, 2-min averages of inferred channel 546774; raw full-window extremes are summarized below", {
    head:["Time","A"], numCols:[1] }, "inferred");
  c.spreadState = card(batteryChartsWrap, "Cell spread by operating state",
    "95th-percentile spread while charging, under load, and at rest — divergence under load is the earliest weak-cell sign", {
    head:["State","Paired samples","Median mV","P95 mV"], numCols:[1,2,3] });
  c.spreadSoc = card(batteryChartsWrap, "Cell spread by state of charge", "95th-percentile spread within each SoC band", {
    head:["SoC band","Samples","Median mV","P95 mV"], numCols:[1,2,3] }, "derived");
  c.packV = card(batteryChartsWrap, "Pack voltage", "V, 20-min averages — mean cell voltage × 96 series cells", {
    head:["Time","V"], numCols:[1] }, "derived");

  c.amb = card(thermalChartsWrap, "Ambient temperature", "°C, 30-min averages of inferred channel 180806", {
    head:["Time","°C"], numCols:[1] }, "inferred");
  c.modes = card(thermalChartsWrap, "Thermal management modes", "share of samples in range, channel 543919", {
    head:["Mode","Vehicle label","Samples"], numCols:[2] }, "inferred");
  c.thermal = card(thermalChartsWrap, "Thermal sensor traces", "20-min averages; exact component locations are not documented", {
    head:["Sensor","Channel","20-min buckets","Min °C","Median °C","P95 °C","Max °C"], numCols:[2,3,4,5,6] }, "inferred");
  c.coolant = card(thermalChartsWrap, "Coolant flow", "L/min, 10-min averages of inferred channel 546697", {
    head:["Time","L/min"], numCols:[1] }, "inferred");
  c.valves = card(thermalChartsWrap, "Coolant valve actuation",
    "inferred channels 543814 / 544790 — periods where the valve was commanded; actuation tracks the heat-pump modes", {
    head:["Valve","Samples","Actuated share","Transitions"], numCols:[1,3] }, "inferred");
  /* legend for the 2-series chart (toggle to isolate) */
  const lg = el("div","legend");
  const state = { max:true, min:true };
  [["max","Highest cell","--s1"],["min","Lowest cell","--s2"]].forEach(([id,name,varName]) => {
    const b = el("button"); b.type = "button"; b.setAttribute("aria-pressed","true");
    const k = el("span","key"); k.style.borderTopColor = "var(" + varName + ")";
    b.appendChild(k); b.appendChild(el("span",null,name));
    b.addEventListener("click", () => {
      state[id] = !state[id];
      if (!state.max && !state.min){ state[id] = true; return; }
      b.setAttribute("aria-pressed", String(state[id]));
      drawCells();
    });
    lg.appendChild(b);
  });
  c.cells.root.appendChild(lg);
  c.cellState = state;
  c.thermal.root.style.gridColumn = "1 / -1";   // small multiples need the full row
  if (DATA.currentStats.samples) c.current.root.appendChild(metricStrip([
    ["Raw peak discharge",fmtN(Math.abs(DATA.currentStats.min),1) + " A"],
    ["Raw 5th percentile",fmtN(DATA.currentStats.p05,1) + " A"],
    ["Raw 95th percentile",fmtN(DATA.currentStats.p95,1) + " A"],
    ["Raw peak positive",fmtN(DATA.currentStats.max,1) + " A"]]));
  /* different export formats carry different data — detach panels whose
     series is absent instead of rendering them empty. The card objects stay
     so the renderers can keep drawing into the detached nodes harmlessly. */
  const present = {
    daily: DATA.daily.length, heat: DATA.speedRaw.length || (DATA.activity || []).length,
    hist: DATA.speedRaw.length, chgCurves: DATA.charges.some(c => c.powerCurve && c.powerCurve.length),
    chgSocCurves: DATA.charges.some(c => c.socPowerCurve && c.socPowerCurve.length),
    cons: DATA.consumption.length,
    soc: DATA.soc.length, cells: DATA.cellMax.length || DATA.cellMin.length,
    spread: DATA.spread.length, current: DATA.current.length,
    spreadState: DATA.spreadCurRaw.length, spreadSoc: DATA.spreadSocRaw.length,
    packV: DATA.packVoltage.length,
    amb: DATA.ambient.length, modes: DATA.modeRaw.length,
    thermal: DATA.thermal.some(s => s.pts.length), coolant: DATA.coolantFlow.length,
    valves: (DATA.valves || []).some(v => v.transitions.length),
  };
  for (const k in present) if (c[k] && !present[k]) c[k].root.remove();
  return c;
}

function drawCells(){
  const s = cards.cellState;
  lineChart(cards.cells.viz, {
    t0:range[0], t1:range[1], gapH:12, unitShort:"", yDec:0,
    series:[
      {name:"Highest cell", cls:"s1", on:s.max, pts:fPts(DATA.cellMax)},
      {name:"Lowest cell", cls:"s2", on:s.min, pts:fPts(DATA.cellMin)},
    ]});
}

function renderCharts(){
  const t0 = range[0], t1 = range[1];
  const days = DATA.daily.filter(d => inR(dayKeyToT(d.d) + 43200));
  colChart(cards.daily.viz, {
    items: days.map(d => ({ label:fmtD(dayKeyToT(d.d) + 43200), v:d.km, tipWhen:d.d })),
    unitShort:" km", valName:"driven" });
  cards.daily.setRows(days.map(d => [d.d, d.km, d.gapKm]));

  const soc = fPts(DATA.soc);
  lineChart(cards.soc.viz, { t0, t1, gapH:12, yMin:0, yMax:100, unitShort:"%",
    series:[{name:"State of charge", cls:"s1", pts:soc}] });
  cards.soc.setRows(soc.map(p => [fmtDT(p[0]), p[1]]));

  drawCells();
  const cmax = fPts(DATA.cellMax), cminByT = new Map(DATA.cellMin);
  cards.cells.setRows(cmax.map(p => [fmtDT(p[0]), p[1], cminByT.get(p[0]) ?? ""]));

  const amb = fPts(DATA.ambient);
  lineChart(cards.amb.viz, { t0, t1, gapH:12, unitShort:"°", ttDec:1,
    series:[{name:"Ambient", cls:"s1", pts:amb}] });
  cards.amb.setRows(amb.map(p => [fmtDT(p[0]), p[1]]));

  /* heatmap + speed histogram from raw speed samples in range;
     structured exports deliver value-less speed events — still honest
     evidence of when the car was active */
  const spd = fPts(DATA.speedRaw.length ? DATA.speedRaw : (DATA.activity || []));
  const grid = Array.from({length:7}, () => Array(24).fill(0));
  for (const [t] of spd){ const d = loc(t); grid[(d.getUTCDay()+6)%7][d.getUTCHours()]++; }
  heatChart(cards.heat.viz, { grid,
    unitName:DATA.speedRaw.length ? "speed samples" : "reporting events" });
  cards.heat.setRows(grid.map((row,d) => [dashboardWeekdays()[d], ...row]));

  const moving = spd.filter(p => p[1] > 0.5).map(p => p[1]);
  const top = Math.max(1, Math.floor(Math.max(0, ...moving)/10) + 1);
  const hist = Array(top).fill(0);
  for (const v of moving) hist[Math.min(top-1, Math.floor(v/10))]++;
  cards.hist.setSub("share of samples while moving (" + fmtN(moving.length) + " samples > 0 km/h in range)");
  colChart(cards.hist.viz, {
    items: hist.map((n,i) => ({ label:String(i*10), v:+(n/(moving.length||1)*100).toFixed(1),
      tipWhen:(i*10) + "–" + (i*10+10) + " km/h" })),
    unitShort:"%", valName:"of moving time", dec:1 });
  cards.hist.setRows(hist.map((n,i) => [(i*10) + "–" + (i*10+10) + " km/h", n,
    (n/(moving.length||1)*100).toFixed(1) + "%"]));

  /* thermal mode share in range */
  const mc = Array(DATA.modeNames.length).fill(0);
  let mTot = 0;
  for (const [t,i] of DATA.modeRaw) if (inR(t)){ mc[i]++; mTot++; }
  const mrows = DATA.modeNames.map((m,i) => ({...m, n:mc[i]}))
    .filter(m => m.n > 0).sort((a,b) => b.n - a.n);
  hBarChart(cards.modes.viz, {
    items: mrows.map(m => ({ label:m.label, v:m.n, valText:(m.n/(mTot||1)*100).toFixed(1) + "%",
      tipText:fmtN(m.n) + " samples (" + m.raw + ")" }))});
  cards.modes.setRows(mrows.map(m => [m.label, m.raw, m.n]));

  if (cards.chgDaily){
    const cd = DATA.chargedDaily.filter(d => inR(dayKeyToT(d.d) + 43200));
    colChart(cards.chgDaily.viz, {
      items: cd.map(d => ({ label:fmtD(dayKeyToT(d.d) + 43200), v:d.kwh, tipWhen:d.d })),
      unitShort:" kWh", valName:"charged" });
    cards.chgDaily.setRows(cd.map(d => [d.d, d.kwh]));
  }
  if (cards.chgMonthly){
    const cm = DATA.chargedMonthly.filter(d => {
      const start = dayKeyToT(d.d + "-01");
      const md = new Date((start + OFF) * 1000);
      const next = Date.UTC(md.getUTCFullYear(), md.getUTCMonth()+1, 1) / 1000 - OFF;
      return start <= range[1] && next >= range[0];
    });
    colChart(cards.chgMonthly.viz, {
      items: cm.map(d => ({ label:d.d, v:d.kwh, tipWhen:d.d })),
      unitShort:" kWh", valName:"charged" });
    cards.chgMonthly.setRows(cm.map(d => [d.d, d.kwh]));
  }

  /* per-day consumption */
  const cons = DATA.consumption.filter(c => inR(dayKeyToT(c.d) + 43200));
  colChart(cards.cons.viz, {
    items: cons.map(c => ({ label:fmtD(dayKeyToT(c.d) + 43200), v:c.kwh100,
      tipWhen:c.d + " · " + c.km + " km" })),
    unitShort:"", valName:"kWh/100km", dec:1 });
  cards.cons.setRows(cons.map(c => [c.d, c.km, c.socUsed + "%", c.kwh100,
    c.ambientC != null ? c.ambientC + " °C" : "—"]));

  /* cell imbalance */
  const sp = fPts(DATA.spread);
  lineChart(cards.spread.viz, { t0, t1, gapH:36, yMin:0, unitShort:" mV",
    series:[{name:"Median cell spread", cls:"s1", pts:sp}] });
  cards.spread.setRows(sp.map(p => [fmtD(p[0]), p[1]]));
  const spValues = fPts(DATA.spreadRaw).map(p => p[1]);
  if (spValues.length) setMetrics(cards.spread.root,[
    ["Median",fmtN(quantile(spValues,.5),1)+" mV"],["P95",fmtN(quantile(spValues,.95),1)+" mV"],
    ["P99",fmtN(quantile(spValues,.99),1)+" mV"],["Maximum",fmtN(Math.max(...spValues),1)+" mV"]]);

  /* battery current */
  const amps = fPts(DATA.current);
  lineChart(cards.current.viz, { t0, t1, gapH:12, unitShort:" A", yDec:0,
    series:[{name:"HV current", cls:"s1", pts:amps}] });
  cards.current.setRows(amps.map(p => [fmtDT(p[0]), p[1]]));

  const socBands = Array.from({length:5},()=>[]);
  for (const [t,spread,socValue] of DATA.spreadSocRaw) if (inR(t))
    socBands[Math.min(4,Math.floor(socValue/20))].push(spread);
  const spreadSocRows = socBands.map((values,i) => values.length ? ({
    band:(i*20)+"–"+(i*20+20)+"%", samples:values.length,
    median:+quantile(values,.5).toFixed(1), p95:+quantile(values,.95).toFixed(1)}) : null).filter(Boolean);
  hBarChart(cards.spreadSoc.viz, {items:spreadSocRows.map(s => ({
    label:s.band, v:s.p95, valText:s.p95.toFixed(1) + " mV",
    tipText:fmtN(s.samples) + " paired samples · median " + s.median.toFixed(1) + " mV"}))});
  cards.spreadSoc.setRows(spreadSocRows.map(s => [s.band,s.samples,s.median,s.p95]));

  /* cell spread by operating state (paired with concurrent battery current) */
  const ST_ORDER = ["Charging (>5 A)","Heavy load (<−50 A)","Light load","Idle (±5 A)"];
  const stBins = Object.fromEntries(ST_ORDER.map(k => [k, []]));
  for (const [t, spv, amp] of DATA.spreadCurRaw) if (inR(t)){
    const key = amp > 5 ? ST_ORDER[0] : amp < -50 ? ST_ORDER[1] : amp < -5 ? ST_ORDER[2] : ST_ORDER[3];
    stBins[key].push(spv);
  }
  const stRows = ST_ORDER.filter(k => stBins[k].length >= 15).map(k => ({
    state:k, n:stBins[k].length,
    median:+quantile(stBins[k],.5).toFixed(1), p95:+quantile(stBins[k],.95).toFixed(1)}));
  hBarChart(cards.spreadState.viz, {items:stRows.map(s => ({
    label:s.state, v:s.p95, valText:s.p95.toFixed(1) + " mV",
    tipText:fmtN(s.n) + " paired samples · median " + s.median.toFixed(1) + " mV"}))});
  cards.spreadState.setRows(stRows.map(s => [s.state,s.n,s.median,s.p95]));

  /* charging power curves — one facet per session with current samples */
  {
    const viz = cards.chgCurves.viz; viz.textContent = "";
    const grid = el("div","facets"); viz.appendChild(grid);
    const chgSess = DATA.charges.filter(cc => inR(cc.start) && cc.powerCurve && cc.powerCurve.length >= 4);
    const pHi = Math.max(1, ...chgSess.flatMap(cc => cc.powerCurve.map(p => p[1])));
    const fv = chgSess.map(cc => {
      const f = el("div","facet");
      f.appendChild(el("div","flabel",
        fmtDT(cc.start) + " · " + cc.type + " · " + cc.socFrom + "→" + cc.socTo + "%" +
        (cc.peakKw != null ? " · peak " + cc.peakKw + " kW" : "")));
      const v = el("div","viz"); f.appendChild(v); grid.appendChild(f);
      return v;
    });
    chgSess.forEach((cc, i) => lineChart(fv[i], {
      t0:cc.start, t1:cc.end, gapH:0.5, h:120, yTickN:2, padR:40,
      yMin:0, yMax:pHi * 1.08, unitShort:" kW", ttDec:1,
      series:[{name:"Charge power", cls:"s1", pts:cc.powerCurve}] }));
    if (!chgSess.length)
      viz.appendChild(el("div","sub","No detailed time-based charging curve is available in this range."));
    cards.chgCurves.setRows(chgSess.map(cc => [fmtDT(cc.start), cc.type,
      cc.socFrom + "% → " + cc.socTo + "%", cc.peakKw != null ? cc.peakKw : "—", cc.powerCurve.length]));
  }

  /* charging power against SoC — the export's separate SoC-curve view */
  {
    const viz = cards.chgSocCurves.viz; viz.textContent = "";
    const grid = el("div","facets"); viz.appendChild(grid);
    const sessions = DATA.charges.filter(cc => inR(cc.start) && cc.socPowerCurve && cc.socPowerCurve.length >= 4);
    const pHi = Math.max(1, ...sessions.flatMap(cc => cc.socPowerCurve.map(p => p[1])));
    const facets = sessions.map(cc => {
      const f = el("div","facet");
      f.appendChild(el("div","flabel",fmtDT(cc.start) + " · " + cc.type));
      const v = el("div","viz"); f.appendChild(v); grid.appendChild(f); return v;
    });
    sessions.forEach((cc, i) => {
      const pts = cc.socPowerCurve.slice().sort((a,b) => a[0]-b[0]);
      const lo = Math.max(0, Math.floor(pts[0][0] / 10) * 10);
      const hi = Math.min(100, Math.ceil(pts[pts.length-1][0] / 10) * 10);
      const ticks = []; for (let s=lo; s<=hi; s+=10) ticks.push([s, s + "%"]);
      lineChart(facets[i], {t0:lo, t1:hi, h:120, yTickN:2, padR:40,
        yMin:0, yMax:pHi*1.08, unitShort:" kW", ttDec:1,
        xTicks:ticks, xTip:s => "SoC " + fmtN(s) + "%",
        series:[{name:"Charge power", cls:"s1", pts}]});
    });
    cards.chgSocCurves.setRows(sessions.map(cc => {
      const pts=cc.socPowerCurve;
      return [fmtDT(cc.start),cc.type,pts[0][0]+"% → "+pts[pts.length-1][0]+"%",
        Math.max(...pts.map(p=>p[1])),pts.length];
    }));
  }

  /* thermal traces — small multiples, one facet per sensor, shared y scale */
  {
    const viz = cards.thermal.viz; viz.textContent = "";
    const grid = el("div","facets"); viz.appendChild(grid);
    const sets = DATA.thermal.map(s => ({ s, pts: fPts(s.pts) }));
    let flo = Infinity, fhi = -Infinity;
    for (const {pts} of sets) for (const p of pts){ if (p[1] < flo) flo = p[1]; if (p[1] > fhi) fhi = p[1]; }
    if (flo === Infinity){ flo = 0; fhi = 1; }
    const fpad = (fhi - flo) * 0.06 + 0.5;
    /* two passes: mount every facet first so the grid's column width is final,
       THEN render — lineChart measures clientWidth at render time */
    const facetVizes = sets.map(({s}) => {
      const f = el("div","facet");
      f.appendChild(el("div","flabel", s.label + " · " + s.channel));
      const fviz = el("div","viz"); f.appendChild(fviz);
      grid.appendChild(f);
      return fviz;
    });
    sets.forEach(({s, pts}, i) => {
      lineChart(facetVizes[i], { t0, t1, gapH:12, h:120, yTickN:2, padR:40, unitShort:"°", ttDec:1,
        yMin:flo - fpad, yMax:fhi + fpad,
        series:[{name:s.label + " · " + s.channel, cls:"s1", pts}] });
    });
  }
  cards.thermal.setRows(DATA.thermal.map(s => { const values=fPts(s.pts).map(p=>p[1]);
    return values.length ? [s.label,s.channel,values.length,Math.min(...values).toFixed(1),
      quantile(values,.5).toFixed(1),quantile(values,.95).toFixed(1),Math.max(...values).toFixed(1)]
      : [s.label,s.channel,0,"—","—","—","—"]; }));

  const flow = fPts(DATA.coolantFlow);
  lineChart(cards.coolant.viz, { t0, t1, gapH:12, yMin:0, unitShort:" L/min", ttDec:1,
    series:[{name:"Coolant flow", cls:"s1", pts:flow}] });
  cards.coolant.setRows(flow.map(p => [fmtDT(p[0]),p[1]]));

  const pv = fPts(DATA.packVoltage);
  lineChart(cards.packV.viz, { t0, t1, gapH:12, unitShort:" V", yDec:0, ttDec:1,
    series:[{name:"Pack voltage", cls:"s1", pts:pv}] });
  cards.packV.setRows(pv.map(p => [fmtDT(p[0]), p[1]]));

  valveTimeline(cards.valves.viz, { t0, t1, rows: DATA.valves || [] });
  cards.valves.setRows((DATA.valves || []).map(v => [v.label, fmtN(v.samples),
    v.onPct != null ? v.onPct + "%" : "—", fmtN(v.transitions.length)]));
  localizeDashboard(document.body);
}

/* ---- driving and charging ledgers ---- */
function renderDriveTables(){
  const wrap = document.getElementById("driveTables"); wrap.textContent = "";

  const chg = DATA.charges.filter(c => inR(c.start)).slice().reverse();
  const chgReported = chg.length && chg[0].confidence === "observed";
  const cc = el("div","card");
  const cch = el("header"), ccg = el("div","grow"); ccg.appendChild(el("h2",null,"Charging ledger"));
  ccg.appendChild(el("div","sub", chgReported
    ? chg.length + " charging sessions in range, reported by the vehicle itself — energy, power and SoC window are the car's own figures"
    : chg.length + " continuous SoC-rise events in range; consecutive samples may be at most 30 minutes apart. " +
      "Energy/power use the " + PACK_SHORT + " " + DATA.packKwh + " kWh usable capacity."));
  cch.appendChild(ccg); cch.appendChild(prov(chgReported ? "observed" : "derived")); cc.appendChild(cch);
  const cs = el("div","tableWrap"); cs.style.marginTop = "8px";
  const withCost = DATA.priceKwh != null;
  const fmtMinutes = value => {
    if (value == null) return "—";
    const mins = Math.round(value);
    return mins >= 60 ? Math.floor(mins/60) + " h " + (mins%60) + " min" : mins + " min";
  };
  cs.appendChild(buildTable(
    ["Start","End","SoC","Energy","Avg power","Type","Elapsed / active","Plugged in","Mode"]
      .concat(withCost ? ["Est. cost"] : []),
    chg.map(c => {
      const elapsed = c.elapsedMin != null ? c.elapsedMin : (c.end-c.start)/60;
      const elapsedActive = fmtMinutes(elapsed) +
        (c.activeMin != null ? " / " + fmtMinutes(c.activeMin) : "");
      const row = [fmtDT(c.start), fmtT(c.end),
        c.socFrom + "% → " + c.socTo + "%",
        "~" + fmtN(c.kwh,1) + " kWh", "~" + fmtN(c.kw,1) + " kW", c.type,
        elapsedActive, fmtMinutes(c.connectedMin),
        (c.chargeModes || []).map(pretty).join(", ") || "—"];
      if (withCost) row.push("~" + DATA.currency + fmtN(c.kwh * DATA.priceKwh, 2));
      return row;
    }), withCost ? [3,4,6,7,8,9] : [3,4,6,7,8]));
  cc.appendChild(cs);
  if (chgReported) cc.appendChild(el("div","foot",
    "Elapsed is charging start to stop; active excludes pauses; plugged-in time uses connection to disconnection. " +
    "Average power is the vehicle's reported figure and may be based on active time."));
  if (DATA.charges.length) wrap.appendChild(cc);

  const trips = DATA.trips.filter(s => inR(s.start)).slice().reverse();
  const c = el("div","card");
  const th = el("header"), tg = el("div","grow"); tg.appendChild(el("h2",null,"Observed trip ledger"));
  tg.appendChild(el("div","sub",trips.length + " movement clusters in range, built from odometer edges with ≤30-minute continuity and split at sustained charging stops"));
  th.appendChild(tg); th.appendChild(prov("derived")); c.appendChild(th);
  const scroll = el("div","tableWrap"); scroll.style.marginTop = "8px";
  scroll.appendChild(buildTable(
    ["Start","End / duration","Distance","Avg / max speed","Moving","SoC","Est. consumption","∫I·V check","Ambient","Peak current"],
    trips.map(s => {
      const mins = Math.round((s.end - s.start)/60);
      const dur = mins >= 60 ? Math.floor(mins/60) + " h " + (mins%60) + " min" : mins + " min";
      const current = (s.peakDischargeA != null ? "−" + s.peakDischargeA + " A" : "—") +
        (s.peakRegenA != null ? " / +" + s.peakRegenA + " A" : "");
      const spd = (s.avgMoving != null ? s.avgMoving : "—") + " / " +
        (s.vmax != null ? s.vmax : "—") + " km/h";
      const mov = s.movingMin != null
        ? s.movingMin + " of " + mins + " min" : "—";
      const cons2 = s.kwh100 != null
        ? "~" + s.kwh100 + " kWh/100km" + (s.consConf === "fair" ? " (SoC samples stale)" : "") : "—";
      const iv = s.ivKwh100 != null
        ? "~" + s.ivKwh100 + " kWh/100km · " + s.ivCoveragePct + "% cov"
        : (s.ivKwh != null ? "~" + s.ivKwh + " kWh · " + s.ivCoveragePct + "% cov" : "—");
      return [fmtDT(s.start), fmtT(s.end) + " · " + dur,
        fmtN(s.dist,1) + " km", spd, mov,
        (s.socFrom != null && s.socTo != null) ? s.socFrom + "% → " + s.socTo + "%" : "—",
        cons2, iv, s.ambientC != null ? s.ambientC + " °C" : "—", current];
    }), [2,4,6,7,8]));
  c.appendChild(scroll);
  if (DATA.trips.length) wrap.appendChild(c);

  /* parked drain events, annotated with the thermal-mode mix */
  const climaIdx = new Set(DATA.modeNames.map((m,i) => /Heizen|Kuehlen/.test(m.raw) ? i : -1).filter(i => i >= 0));
  const dp = DATA.drainPairs.filter(p => p[0] >= range[0] && p[1] <= range[1]).slice().reverse();
  const dc = el("div","card"), dh = el("header"), dg = el("div","grow");
  dg.appendChild(el("h2",null,"Parked drain events"));
  dg.appendChild(el("div","sub",dp.length + " parks of ≥ 8 h with no odometer movement; thermal-mode samples inside each park hint at why charge was lost"));
  dh.appendChild(dg); dh.appendChild(prov("derived")); dc.appendChild(dh);
  const dw = el("div","tableWrap"); dw.style.marginTop = "8px";
  dw.appendChild(buildTable(
    ["Park start","Until","Duration","SoC lost","Rate","Mode samples","Climate share","Reading"],
    dp.map(([t0,t1,drop]) => {
      const days = (t1 - t0) / 86400;
      let n = 0, clima = 0;
      for (const [t,i] of DATA.modeRaw) if (t >= t0 && t <= t1){ n++; if (climaIdx.has(i)) clima++; }
      const share = n ? clima / n * 100 : null;
      const reading = n === 0 ? "No mode telemetry" :
        share >= 10 ? "Climate ran — likely conditioning" : "Quiet park";
      return [fmtDT(t0), fmtDT(t1), Math.round(days * 24) + " h", drop + "%",
        (drop / days).toFixed(1) + " %/day", fmtN(n),
        share != null ? share.toFixed(0) + "%" : "—", reading];
    }), [2,3,4,5,6]));
  dc.appendChild(dw);
  if (DATA.drainPairs.length) wrap.appendChild(dc);

  const gaps = DATA.movementGaps.filter(g => g.end >= range[0] && g.start <= range[1]).slice().reverse();
  const gc = el("div","card"), gh = el("header"), gg = el("div","grow");
  gg.appendChild(el("h2",null,"Movement across sampling gaps"));
  const gPartial = gaps.filter(g => g.timing === "partial");
  const gPartialKm = gPartial.reduce((a,g)=>a+g.dist,0);
  const gNoneKm = gaps.reduce((a,g)=>a+g.dist,0) - gPartialKm;
  gg.appendChild(el("div","sub",fmtN(gaps.reduce((a,g)=>a+g.dist,0)) +
    " km is proven by odometer change, but cannot be assigned to exact trips — " +
    fmtN(gPartialKm) + " km falls in gaps with partial timing evidence, " + fmtN(gNoneKm) + " km with none"));
  gh.appendChild(gg); gh.appendChild(prov("derived")); gc.appendChild(gh);
  const gs = el("div","tableWrap"); gs.appendChild(buildTable(
    ["Last sample","Next sample","Gap","Distance added","Movement evidence in gap","Likely movement window"],
    gaps.map(g => {
      const ev = [];
      if (g.movingSamples) ev.push(g.movingSamples + " moving speed sample" + (g.movingSamples === 1 ? "" : "s") +
        (g.vmaxInGap != null ? " · up to " + g.vmaxInGap + " km/h" : ""));
      if (g.loadSamples) ev.push(g.loadSamples + " battery-discharge sample" + (g.loadSamples === 1 ? "" : "s"));
      return [fmtDT(g.start), fmtDT(g.end), g.hours + " h", g.dist + " km",
        ev.length ? ev.join(" · ") : "No movement telemetry",
        g.evidenceFrom != null ? fmtDT(g.evidenceFrom) + " – " + fmtDT(g.evidenceTo) : "—"];
    }),[2,3]));
  gc.appendChild(gs);
  gc.appendChild(el("div","foot","Gaps with evidence are not promoted to trips: one gap can hide several drives, " +
    "and the sparse samples cover only a fraction of the distance. The window shows when movement is proven, not its full extent."));
  if (DATA.movementGaps.length) wrap.appendChild(gc);
}

/* ---- full-export activity and settings ---- */
function renderActivity(){
  const wrap = document.getElementById("activityTables"); wrap.textContent = "";
  const ec = el("div","card"), eh = el("header"), eg = el("div","grow");
  eg.appendChild(el("h2",null,"Remote actions, reports and errors"));
  eg.appendChild(el("div","sub","Full raw-export timeline; not limited by the diagnostic range filter"));
  eh.appendChild(eg); eh.appendChild(prov("observed")); ec.appendChild(eh);
  const ew = el("div","tableWrap");
  const eventTable = buildTable(
    ["Time ("+DATA.tzLabel+")","Kind","Event","Detail"],
    DATA.events.map(e => [fmtFull(e.time),e.kind,e.event,e.detail || "—"]));
  eventTable.classList.add("eventTable"); ew.appendChild(eventTable); ec.appendChild(ew);
  if (DATA.events.length) wrap.appendChild(ec);

  const cfg = el("div","card"), ch = el("header"), cg = el("div","grow");
  const explicit = DATA.configuration.filter(c => c.source === "explicit").length;
  cg.appendChild(el("h2",null,"Vehicle configuration snapshot"));
  cg.appendChild(el("div","sub",DATA.configuration.length + " settings found · " + explicit +
    " explicit values · raw encodings retained when the dictionary is ambiguous"));
  ch.appendChild(cg); ch.appendChild(prov("observed")); cfg.appendChild(ch);
  const configTable = buildTable(
    ["Time","Field","Interpreted value","Raw","Source","Dictionary description"],
    DATA.configuration.map(c => [fmtFull(c.time),c.field,c.value,c.raw,c.source,c.description || "—"]));
  configTable.classList.add("configTable");
  const cw = el("div","tableWrap"); cw.appendChild(configTable);
  cfg.appendChild(cw);
  if (DATA.configuration.length) wrap.appendChild(cfg);
}

/* ---- package completeness ---- */
function renderEvidence(){
  const C = DATA.completeness, cards = document.getElementById("evidenceCards"); cards.textContent = "";
  function evidenceCard(label,value,context,kind){ const c = el("div","card kpi");
    const h = el("header"), g = el("div","grow"); g.appendChild(el("div","label",label));
    h.appendChild(g); h.appendChild(prov(kind)); c.appendChild(h);
    c.appendChild(el("div","value",value)); c.appendChild(el("div","ctx",context)); cards.appendChild(c); }
  evidenceCard("Dictionary keys",C.dictionaryKeys == null ? "—" : fmtN(C.dictionaryKeys),
    "versus " + fmtN(C.exportKeys) + " unique keys delivered","observed");
  evidenceCard("Dictionary-matched keys",fmtN(C.matchedKeys),
    "of " + fmtN(C.exportKeys) + " delivered keys","derived");
  evidenceCard("Numeric diagnostic records",fmtN(C.numericPct,1) + "%",
    fmtN(C.numericRecords) + " records across " + C.numericFields + " undocumented channels","derived");
  evidenceCard("Diagnostic lag",C.diagnosticLagDays == null ? "—" : fmtN(C.diagnosticLagDays,1) + " days",
    "last high-volume diagnostic sample before export creation","derived");

  const tables = document.getElementById("evidenceTables"); tables.textContent = "";
  const cov = el("div","card"), covh = el("header"), covg = el("div","grow");
  covg.appendChild(el("h2",null,"Coverage by delivered category"));
  covg.appendChild(el("div","sub","Raw export spans " + fmtFull(C.rawMin) + " to " + fmtFull(C.rawMax) +
    "; high-volume diagnostics span " + fmtFull(C.diagnosticMin) + " to " + fmtFull(C.diagnosticMax)));
  covh.appendChild(covg); covh.appendChild(prov("observed")); cov.appendChild(covh);
  const covw = el("div","tableWrap"); covw.appendChild(buildTable(
    ["Category","Fields","Records","First","Last"],C.coverage.map(r =>
      [r.label,fmtN(r.fields),fmtN(r.records),fmtFull(r.first),fmtFull(r.last)]),[1,2]));
  cov.appendChild(covw); tables.appendChild(cov);

  const miss = el("div","card"), mh = el("header"), mg = el("div","grow");
  mg.appendChild(el("h2",null,"Dictionary fields cited but not delivered"));
  mg.appendChild(el("div","sub","Checked against VW's Data Dictionary V4.0 (bundled) and the JSON export"));
  mh.appendChild(mg); mh.appendChild(prov("derived")); miss.appendChild(mh);
  const mw = el("div","tableWrap");
  const expectedFieldsTable = buildTable(
    ["Field","In dictionary","In export"],C.expectedFields.map(f =>
      [f.field,f.dictionary ? "Yes" : "No",f.export ? "Yes" : "No"]));
  expectedFieldsTable.classList.add("expectedFieldsTable"); mw.appendChild(expectedFieldsTable);
  miss.appendChild(mw);
  const note = el("div","note","Not found in this export: " + C.notFound.join(", ") +
    ". Identifiers are redacted in this HTML by default.");
  miss.appendChild(note); tables.appendChild(miss);
}

/* ---- inventory ---- */
(function(){
  const wrap = document.querySelector("#invWrap .tableWrap");
  wrap.appendChild(buildTable(
    ["Field / indexed pattern","Raw fields","Records","Description","Sample value","Example raw field","First","Last"],
    DATA.inventory.map(f => [f.field, fmtN(f.variants || 1), fmtN(f.n), f.desc || "", f.sample,
      f.example !== f.field ? f.example : "", f.first ? fmtD(f.first) : "", f.last ? fmtD(f.last) : ""]), [1,2]));
})();

document.getElementById("pageFoot").textContent =
  "Built offline from " + DATA.exportFile + " by build_dashboard.py · observed, derived and inferred values are labelled throughout · " +
  "the car only reports while awake — solid line segments are measured, dashed segments bridge not-reported periods" +
  (DATA.identifiersRedacted ? " · identifiers redacted by default." : ".");

/* ---- battery health card ---- */
const HEALTH_LABEL = {
  good:"Battery looks healthy", fair:"Battery shows normal wear",
  attention:"Battery worth checking", unknown:"Not enough data to assess the battery" };
(function(){
  const H = DATA.health; if (!H) return;
  const wrap = document.getElementById("healthCards");
  const c = el("div","card");
  const head = el("header"), g = el("div","grow");
  const proxyMode = DATA.capacityMethod === "reported_proxy";
  g.appendChild(el("h2",null,proxyMode ? "Battery evidence" : "Battery health"));
  head.appendChild(g); head.appendChild(prov("derived")); c.appendChild(head);
  const badge = el("div","healthBadge");
  const dot = el("span","hdot " + (H.verdict || "unknown"));
  badge.appendChild(dot);
  badge.appendChild(el("span",null, HEALTH_LABEL[H.verdict] || HEALTH_LABEL.unknown));
  c.appendChild(badge);
  const rl = el("div"); rl.style.cssText = "display:grid;gap:5px;margin-top:10px";
  for (const [,text] of H.reasons) rl.appendChild(el("div","hreason", text));
  c.appendChild(rl);
  if (DATA.packNote) c.appendChild(el("div","foot", DATA.packNote + "."));
  if (DATA.capEstimates && DATA.capEstimates.length){
    const reportedCaps = DATA.capEstimates.every(e => e.coveragePct == null);
    const excluded = DATA.capEstimates.filter(e => e.usedForMedian === false).length;
    const tw = el("div","tableWrap"); tw.style.marginTop = "10px";
    tw.appendChild(buildTable(
      ["Charge start","Type","SoC window","Duration","Energy in","Current coverage","Samples",
       reportedCaps ? "Energy in / SoC gained" : "Measured usable"],
      DATA.capEstimates.map(e => [fmtDT(e.start), e.type || "—",
        e.socFrom + "% → " + e.socTo + "%", e.hours + " h", "~" + e.kwhIn + " kWh",
        e.coveragePct != null ? e.coveragePct + "%" : "—",
        e.samples != null ? fmtN(e.samples) : "—",
        e.capKwh + " kWh" + (e.abovePackMax || e.plausible === false ? " ⚠" : "")]), [3,4,5,6,7]));
    c.appendChild(tw);
    if (reportedCaps){
      const typeText = DATA.capacityProxy.byType.map(r =>
        r.type + " " + r.medianKwh + " kWh median (" + r.sessions + " sessions)").join("; ");
      c.appendChild(el("div","foot",
        "This table divides the vehicle's reported session energy by SoC gained. The JSON does not document " +
        "where that energy is metered, and charging losses, auxiliaries and 1% SoC rounding affect the ratio. " +
        typeText + ". " + (DATA.capacityProxy.abovePackMax
          ? "⚠ " + DATA.capacityProxy.abovePackMax + " session(s) even exceed the largest pack option, demonstrating the uncertainty. "
          : "") + "The descriptive median is " + DATA.capacityProxy.medianKwh +
        " kWh; it is not used as battery capacity or state of health."));
    } else {
      c.appendChild(el("div","foot",
        "How capacity was measured: battery current × pack voltage integrated over each charging session, " +
        "divided by the SoC gained — measured at the battery terminals. " +
        (excluded ? "⚠ " + excluded + " physically implausible session(s) are excluded. " : "") +
        "The median (" + DATA.measuredKwh + " kWh) drives derived energy figures; wider SoC windows and fuller " +
        "current coverage make an estimate more reliable."));
    }
  }
  c.appendChild(el("div","foot",proxyMode
    ? (DATA.spreadStats
      ? "The reported-energy ratio is not part of the verdict above; that verdict relies on cell-balance evidence only."
      : "This export contains no battery-current or cell-voltage history, so it cannot support a battery-health verdict.")
    : "Diagnostic estimate from charging sessions and cell voltages — not an official state-of-health measurement."));
  wrap.appendChild(c);
})();

/* ---- tabs ---- */
const TABS = [
  ["overview", "Overview"],
  ["driving", "Driving & charging"],
  ["battery", "Battery"],
  ["thermal", "Thermal"],
  ["backend", "Backend & config"],
  ["audit", "Package audit"],
];
const NO_FILTER_TABS = new Set(["backend","audit"]);   // full-export sections, range filter doesn't apply
const hiddenTabs = new Set();
const tabBar = document.getElementById("tabs");
let activeTab = null;
TABS.forEach(([id,label]) => {
  const b = el("button",null,label);
  b.type = "button"; b.id = "tab-" + id;
  b.setAttribute("role","tab");
  b.setAttribute("aria-selected","false");
  b.setAttribute("aria-controls","panel-" + id);
  b.addEventListener("click", () => setTab(id));
  tabBar.appendChild(b);
});
tabBar.addEventListener("keydown", e => {
  const i = TABS.findIndex(t => t[0] === activeTab);
  const step = dir => { let j = i;
    do { j = (j + dir + TABS.length) % TABS.length; } while (hiddenTabs.has(TABS[j][0]));
    setTab(TABS[j][0]); };
  if (e.key === "ArrowRight"){ step(1); e.preventDefault(); }
  if (e.key === "ArrowLeft"){ step(-1); e.preventDefault(); }
});
function setTab(id, skipRender){
  activeTab = id;
  TABS.forEach(([tid]) => {
    const btn = document.getElementById("tab-" + tid);
    btn.setAttribute("aria-selected", String(tid === id));
    if (tid === id && !skipRender) btn.focus({ preventScroll:true });
    document.getElementById("panel-" + tid).classList.toggle("active", tid === id);
  });
  fWrap.style.display = NO_FILTER_TABS.has(id) ? "none" : "flex";
  if (history.replaceState) history.replaceState(null, "", "#" + id);
  /* charts laid out while their panel was hidden have no width — re-measure */
  if (!skipRender) renderCharts();
}

/* ---- render + resize + theme redraw ---- */
function renderAll(){ renderKpis(); renderCharts(); renderDriveTables(); localizeDashboard(document.body); }
cards = makeCards();
renderActivity();
renderEvidence();
/* hide tabs that ended up with no content for this export format */
TABS.forEach(([tid]) => {
  if (tid === "overview" || tid === "audit") return;
  if (!document.getElementById("panel-" + tid).querySelector(".card")){
    hiddenTabs.add(tid);
    document.getElementById("tab-" + tid).style.display = "none";
  }
});
/* format-appropriate section intros */
if (!DATA.trips.length && DATA.charges.length){
  const p = document.querySelector("#panel-driving .sectionHead p");
  if (p) p.textContent = "This export carries the vehicle's charging history and activity timestamps rather than odometer-based trips — distance and trip panels are omitted.";
}
if (!DATA.cellMax.length && DATA.capEstimates.length){
  const p = document.querySelector("#panel-battery .sectionHead p");
  if (p) p.textContent = DATA.capacityMethod === "reported_proxy"
    ? "Reported charging energy provides a useful consistency check, but not battery-side capacity or SoH. This export contains no current or cell-voltage diagnostics."
    : "Capacity is measured from charging sessions. This export contains no cell-voltage diagnostics, so cell-balance panels are omitted.";
}
const initialTab = location.hash.replace("#","");
setTab(TABS.some(t => t[0] === initialTab) && !hiddenTabs.has(initialTab) ? initialTab : "overview", true);
renderAll();
localizeDashboard(document.body);
addEventListener("hashchange", () => {
  const h = location.hash.replace("#","");
  if (h !== activeTab && TABS.some(t => t[0] === h) && !hiddenTabs.has(h)) setTab(h);
});
let rT = null;
addEventListener("resize", () => { clearTimeout(rT); rT = setTimeout(renderCharts, 150); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderCharts);
themeBtn.addEventListener("click", renderCharts);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("exports", nargs="*",
                    help="export .json file(s) — several are merged into one archive "
                         "(default: all WV*.json next to this script)")
    ap.add_argument("-o", "--out", default=None, help="output HTML path (default: dashboard.html next to export)")
    ap.add_argument("--price-kwh", type=float, default=None,
                    help="electricity price per kWh — adds cost estimates to charging data")
    ap.add_argument("--currency", default="€", help="currency symbol for --price-kwh (default €)")
    ap.add_argument("--csv", action="store_true", help="also write cleaned per-series CSV files")
    ap.add_argument("--include-identifiers", action="store_true",
                    help="include full VIN and backend/user identifiers in HTML (redacted by default)")
    ap.add_argument("--utc-offset", type=float, default=None,
                    help="display timezone as hours from UTC (default: auto-detected from the vehicle clock)")
    ap.add_argument("--pack-kwh", type=float, default=None,
                    help="usable battery capacity in kWh (default: measured only with battery-side evidence; otherwise inferred/assumed)")
    ap.add_argument("--vehicle-title", default=None,
                    help="vehicle name shown in the dashboard header (default: detected from the VIN)")
    ap.add_argument("--language", choices=("en", "de", "nl", "lt"), default="en",
                    help="dashboard report language (default: en)")
    args = ap.parse_args()

    paths = args.exports
    if not paths:
        here = os.path.dirname(os.path.abspath(__file__))
        # WV* covers Volkswagen VIN prefixes (WVW cars, WVG SUVs, WV1/WV2 commercial)
        paths = glob.glob(os.path.join(here, "WV*.json")) or \
            sorted(glob.glob(os.path.join(here, "*.json")), key=os.path.getmtime)[-1:]
        if not paths:
            sys.exit("no export .json found — pass its path as the first argument")
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(paths[0])), "dashboard.html")
    csv_dir = os.path.splitext(out)[0] + "_csv" if args.csv else None
    build(paths, out, price_kwh=args.price_kwh, currency=args.currency,
          csv_dir=csv_dir, include_identifiers=args.include_identifiers,
          vehicle_title=args.vehicle_title, pack_kwh=args.pack_kwh,
          utc_offset=args.utc_offset, language=args.language)


if __name__ == "__main__":
    main()
