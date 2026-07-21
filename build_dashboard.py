#!/usr/bin/env python3
"""Build an interactive HTML dashboard from a Volkswagen EU Data Act export.

Get your data: register at https://eu-data-act.drivesomethinggreater.com/de/en
(Volkswagen's EU Data Act portal), request the historical data package for your
vehicle and download the zip when notified — the link expires after a few days.
See README.md for the full walkthrough.

Usage:
    python3 build_dashboard.py [export.json ...] [-o dashboard.html]

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
    '1023421177-10-96':
        'Data set that contains information about the unit of distance measurement used in the vehicle. 0x0 = km 0x1 = miles',
    '1023421179-0-98':
        'Data set that contains information about the temperature unit settings. 0x0 = Celsius 0x1 = Fahrenheit',
    '1023421180-0-99':
        'Data set that specifies the volume unit. 0x0 = Liter 0x1 = Gallon (UK) 0x2 = Gallon (US)',
    '1023421181-0-100':
        'Data set that contains information about the unit of measurement used for fuel consumption. 0x0 = mpg_UK 0x1 = mpg_US 0x2 = l_per_100km 0x3 = km_per_l',
    '1023421182-10-101':
        'Data set that contains information about the pressure unit settings. 0x0 = bar 0x1 = PSI 0x2 = kPa',
    '1023421183-0-102':
        'Data set that specifies the unit of measurement for gas consumption. 0x0 = kg_per_100km 0x1 = km_per_kg 0x2 = m3_per_100km 0x3 = km_per_m3 0x4 = miles_per_lbs 0x5 = miles_per_yard3 0x6 = miles_per_kg (DF3.5) 0x7 = miles_',
    '1023421184-0-103':
        'Data set that contains information about the unit of mass used in the vehicle, represented as an integer value. 0x0 = kg 0x1 = lbs',
    '1023421185-10-104':
        'Data set that specifies the format in which the date is displayed in the vehicle. 0x0 = day / month / year 0x1 = month / day / year 0x2 = year / month / day',
    '1023421186-0-105':
        'Data set that contains information about the time display format settings in the vehicle. 0x0 = 24h 0x1 = 12h AM/PM',
    '1023421187-10-106':
        'Data set that specifies the unit of measurement for electric energy consumption or efficiency in a vehicle. 0x0 = kWh_per_100km 0x1 = km_per_kWh 0x2 = kWh_per_100miles 0x3 = miles_per_kWh 0x4 = miles_per_gallon_equival e',
    '1056976645-10-261':
        'Data set that provides information about the activation status of the Travel Assist Augmented Reality (AR) feature. On/Off',
    '1056976901-10-157':
        'Data set that contains information about the rotation angle of the Head- Up Display (HUD) in percentage values. 0% - 100%',
    '1056976903-10-9':
        'Data set that contains information about the activation status of the alternative color design feature. On/Off',
    '1056976905-10-9':
        'Data set that contains information about the activation status of the Head-Up Display (HUD). On/Off',
    '1056976909-10-9':
        'Data set that provides information about the activation status of warning messages. On/Off',
    '1224751772-10-170':
        "Data set that contains information about the user's preference for online linking settings, which can be configured as off, manual, or automatic. 2: off; 1: manual; 0: automatic",
    '1224751773-10-171':
        'Online Audio Quality 1: low; 0: high',
    '1224751775-0-173':
        'Data set that contains information about the sorting preferences for FM station lists. 0: Alphabetic; 1: Grouped; 2: Frequency; 3: Genre; 4: HdRadioFirst',
    '1224751780-0-176':
        'Data set that contains information about the FM alternative frequency setting, indicating whether it is enabled or disabled. 0 = off; 1 = on',
    '1224751781-0-29':
        'Additional Online Metadata 0 = off; 1 = on',
    '1224751784-0-179':
        'Rds Regional Setting 1: Fixed; 2: Automatic',
    '1224751785-0-180':
        'Data set that contains information about the configuration of arrow key functionality, specifying whether it is set to navigate through a station list or a preset list. 1: Station List; 0: Preset List',
    '1224751786-10-8':
        'Traffic Program Settings 0 = off; 1 = on',
    '1224751787-0-29':
        'Data set that contains information about the activation status of the radio text feature. 0 = off; 1 = on',
    '1224751789-0-29':
        'Data set that contains information about the activation status of other announcements, represented as a boolean value. 0 = off; 1 = on',
    '1224751791-0-182':
        'Data set that contains information about the activation status of the Radio Data System (RDS) feature. 0 = off; 1 = on',
    '1224751792-0-29':
        'Data set that contains information about the automatic selection of station logos, indicating whether this feature is enabled or disabled. 0 = off; 1 = on',
    '1224751793-0-183':
        'Logo Region Setting -1 = no user selection; 1= automatic; 2-255: specific country or region according to the data base mapping',
    '1224751797-0-185':
        "Show Stations Settings Data type: byte, '1: FM, 0: FM/DAB u",
    '1224751798-0-29':
        'Setting DAB Soft Linking 0 = off; 1 = on',
    '1224751803-0-188':
        'Data set that contains information about the last situation mode (LSM) for Digital Audio Broadcasting (DAB). This includes details such as ensemble identifiers, service identifiers, slideshow availability, program type c',
    '1224751804-0-16':
        'Data set that contains information about the last situation mode related to web-based media playback, including details such as the name of the media, station identification, whether the media is a podcast, the episode k',
    '1224751806-0-190':
        'Data set that contains structured information, including a frequency value and a name field, related to AM_TI LSM. struct { AM_TILSM.freq: int64;, AM_TILSM.name[16]: utf8;, } struct',
    '1224751822-0-399':
        'Data set that contains information about the activation status of the DAB slideshow feature. 0 = off; 1 = on',
    '1358973503-0-29':
        'Data set that contains information about the activation status of the wake-up phrase functionality. 0 = off; 1 = on',
    '1358973506-0-254':
        'Data set that contains information about the type of voice setting configured in the vehicle.',
    '1358973513-0-257':
        'Data set that contains configuration information for the main menu, specifically the arrangement of all tiles. struct{long:64[64]} struct',
    '1358973514-0-258':
        'Data set that contains structured information represented as an array of eight elements, each consisting of a 32-bit integer and a long integer, intended for use in a control center interface. struct{ int32 long }[8] str',
    '1358973516-0-256':
        'Data set that contains information about whether the time display is enabled or disabled in standby mode. 0 = off; 1 = on',
    '1358973520-0-262':
        'Offclock Layout OffclockLayout',
    '1358973521-0-263':
        'Homscreen Page ID Note: value range added automatically',
    '1358973522-0-264':
        'Additional Keyboard Languages long:64; Note: value range automatically added',
    '1358973524-0-29':
        'Data set that contains information about the activation status of the end tone in the voice dialogue system. 0 = off; 1 = on',
    '1358973530-0-29':
        'Data set that contains information about the activation status of the end tone for voice control. 0 = off; 1 = on',
    '1358973531-0-29':
        'Data set that contains information about the input tone status in the voice dialogue system. 0 = off; 1 = on',
    '1358973568-0-453':
        'Data set that contains information about the configuration of top bar favorites in the vehicle\'s user interface. [{datatype:", description":"struct{int32 long}[8]", min:", max":"", stepsize:", default":"", datalength:"}]',
    '1358973572-0-640':
        'Data set that contains information about a customizable wake-up phrase, represented as a string data type.',
    '1358973574-0-636':
        'Second Static Wakeup Phrase 0 = off; 1 = on',
    '1358973629-0-8':
        'Data set that contains information about the activation status of a custom wake-up phrase. 0 = off; 1 = on',
    '150999945-0-36':
        'Off Operation 0 = off; 1 = on u',
    '150999947-0-36':
        'Data set that contains information about the status of the rear lock. 0 = off; 1 = on u',
    '150999949-0-36':
        'Auto Zone Front Driver 0 = off; 1 = on u',
    '150999953-0-55':
        'Data set that contains the target temperature values for the driver zone. 10 â\x80 ¦ 35.5 Â°C (columns Y - AA actual physical values, not raw values? conversion raw value - &gt; physical value?) u',
    '150999954-0-55':
        'Data set that contains the target temperature values for the passenger zone. 10 â\x80 ¦ 35.5 Â°C (columns Y - AA actual physical values, not raw values? conversion raw value - &gt; physical value?) u',
    '150999955-0-55':
        'Data set that provides information about the target temperature value for Zone 3, 10 â\x80 ¦ 35.5 Â°C (columns Y - AA actual physical values, not raw values? conversion raw value - &gt; physical value?) u',
    '150999968-0-36':
        'Sync Status 0 = off; 1 = on u',
    '150999974-0-36':
        "Data set that contains information about the automatic recirculation setting in the vehicle's climate control system. 0 = off; 1 = on u",
    '1526730784-0-8':
        "Data set that contains information about the activation status of the driver's seat entry and exit assistance feature. 0 = off; 1 = on",
    '16778242-1-37':
        'Data set that contains information about the duration of the "Coming Home" lighting feature. Illumination duration u',
    '16778243-1-36':
        'Data set that contains information about the status of the "Coming Home" feature, indicating whether it is activated or deactivated. 0 = off; 1 = on u',
    '16778244-1-37':
        'Data set that contains information about the duration of the "Leaving Home" lighting feature. Illumination duration u',
    '16778245-1-36':
        'Data set that contains information about the activation status of the "Leaving Home" feature. 0 = off; 1 = on u',
    '16778247-1-36':
        'Data set that provides information about the activation status of the dynamic cornering light system. 0 = off; 1 = on u',
    '16778250-1-36':
        'Data set that contains information about the activation status of the Dynamic Light Assist feature, specifically for Audi Matrix-Beam headlights. 0 = off; 1 = on u',
    '16778254-1-41':
        'Data set that contains information about the activation status of automatic windshield wiping when rain is detected. bit field: 0 = automatic wiping off; 1 = automatic wiping on when rain is detected u',
    '16778257-1-43':
        'Data set that contains information about the sensitivity settings of the light sensor, indicating the activation time based on predefined sensitivity levels. 0=sensitive; 1=normal; 2=intensitive u',
    '16778259-1-36':
        'Data set that contains information about the exterior ambient lighting status related to the keyless entry system. 0 = off; 1 = on u',
    '16778260-1-36':
        'Data set that contains information about the activation status of an additional exterior ambient light. 0 = off; 1 = on u',
    '16778261-1-44':
        'Data set that allows configuration of the number of blinking cycles for a specific vehicle function. 2=2 times blinking; 3=3 times blinking; 4=4 times blinking; 5=5 times blinking u',
    '16778263-1-45':
        'Data set that contains information about the status of the ComingHome feature, indicating whether it is set to a classic mode or a staged mode. 0 = classic ComingHome; 1 = staged ComingHome u',
    '16778264-1-46':
        'Data set that provides information about the status of the "LeavingHome" functionality, indicating whether it is in a classic mode or a staged mode. 0 = classic LeavingHome; 1 = staged LeavingHome u',
    '184555379-0-65':
        'Data set that contains information about the level of the steering wheel heating, indicating its intensity or whether it is turned off. Level 0 â\x80 ¦ 3 (0 = Off) u',
    '251666280-0-82':
        'Data set that contains information about the activation status of the Side Assist system, also referred to as Blind Spot Detection (BSD). On/Off u',
    '285221676-0-82':
        'Data set that contains information about the activation status of the Rear Cross Traffic Alert (RCTA) system. On/Off u',
    '285221774-0-91':
        'Volume Front Sound Emitter 1 = quiet, 9 = loud u',
    '285221776-0-91':
        'Data set that contains information about the volume level of the rear sound emitter. 1 = quiet, 9 = loud u',
    '352341537-0-36':
        'Data set that provides information about the activation status of vehicle-to-everything (V2X) communication. 0 = off; 1 = on u',
    '83889081-1-36':
        'Data set that contains information about the activation status of the passenger-side mirror tilt function when the vehicle is in reverse gear. 0 = off; 1 = on u',
    '83889082-1-36':
        'Data set that contains information about the synchronization setting for mirror adjustments. 0 = off; 1 = on u',
    '83889083-1-36':
        'Data set that contains information about the configuration setting for folding mirrors during parking. 0 = off; 1 = on u',
    'UserID':
        'Unique userID used for customer identification -',
    'activeDomains':
        'Last car readiness had active domains set to true of false Boolean',
    'autoUnlockPlugWhenCharged':
        'The value indicating if the charge plug is to be automatically unlocked (or not) once the charging is completed. string (enum)',
    'batteryClimatizationConsumption':
        'normalized value of energy consumption for battery climtization forÂ standardÂ mode (non-comfort-related) 1/h float32',
    'batteryStatus.cruisingRange.engineType':
        'The type of engine / power / fuel, based on energy source. string (enum)',
    'batteryStatus.cruisingRange.range':
        'The range of the corresponding engine. km',
    'batteryStatus.cruisingRange.unitBeforeConversion':
        'The cruising range unit reported by the vehicle.',
    'batteryStatus.currentSOC_pct':
        'The current SOC of HV- Battery between 0 and 100% SOC with a resolution of 1%. % integer (0-100)',
    'budgetStartBatteryLevel':
        'Pre ID.S3 available budget at start of 24hr period',
    'budgetStartTime':
        'Pre ID.S3 start time for 24hr measurement period seconds Timestamp',
    'carCapturedTimeStamp.nanos':
        'The nano seconds of the UTC',
    'carCapturedTimeStamp.seconds':
        'The seconds of the UTC',
    'careMode':
        'The value indicates if the Battery Charging Care Mode functionality is on or off. string (enum)',
    'causedBy':
        'The reason the report was sent by the vehicle. string (enum)',
    'chargeModeSelection':
        'The value indicating if the vehicle shall start charging immediately once preconditions are met or if the vehicle shall start charging whenever a timer is active. string (enum)',
    'chargingStatus.actionState':
        'The state describes if the vehicle is charging immediately without a certain goal, or based on a timer/profile. string (enum)',
    'chargingStatus.chargeMode':
        'The mode of an ongoing charging process. string (enum)',
    'chargingStatus.chargePower_kW':
        'The actual charge power to the HV battery in kW. kW number (-500 to 500)',
    'chargingStatus.chargeType':
        'The type of current the connected power supply provides and is used for charging. string (enum)',
    'chargingStatus.chargingScenario':
        'The scenario of why the vehicle is charging or waiting to charge. string (enum)',
    'chargingStatus.currentChargeState':
        'The State of Charging process. string (enum)',
    'chargingStatus.profileChargeReason':
        'The specific reason why the charging process is currently running when a profile is active. string (enum)',
    'chargingStatus.updateReason':
        'The reason for the report being sent from the vehicle. string (enum)',
    'connectionTimestamp':
        'Used for managing 24h budget cycle in Pre ME3 vehicles seconds Timestamp',
    'cruise_range_primary_info.unit':
        'Information regarding the cruise range primary of the vehicle with subcategory unit Enum',
    'cruise_range_primary_info.value':
        'Information regarding the cruise range primary of the vehicle with subcategory value',
    'door_info.front_left.door_lock_status.value':
        'Information regarding the door of the vehicle with subcategory front left with subcategory door lock status with subcategory value Enum',
    'door_info.front_left.door_status.value':
        'Information regarding the door of the vehicle with subcategory front left with subcategory door status with subcategory value Enum',
    'door_info.front_right.door_lock_status.value':
        'Information regarding the door of the vehicle with subcategory front right with subcategory door lock status with subcategory value Enum',
    'door_info.front_right.door_status.value':
        'Information regarding the door of the vehicle with subcategory front right with subcategory door status with subcategory value Enum',
    'door_info.rear_left.door_lock_status.value':
        'Information regarding the door of the vehicle with subcategory rear left with subcategory door lock status with subcategory value Enum',
    'door_info.rear_left.door_status.value':
        'Information regarding the door of the vehicle with subcategory rear left with subcategory door status with subcategory value Enum',
    'door_info.rear_right.door_lock_status.value':
        'Information regarding the door of the vehicle with subcategory rear right with subcategory door lock status with subcategory value Enum',
    'door_info.rear_right.door_status.value':
        'Information regarding the door of the vehicle with subcategory rear right with subcategory door status with subcategory value Enum',
    'envelope.[*].context.backendCapturedTimestamp.nanos':
        'Fractions of a second at nanosecond resolution, complementing the seconds field. nano seconds',
    'envelope.[*].context.backendCapturedTimestamp.seconds':
        'Specifies the seconds, of UTC time since Unix epoch 1970-01- 01T00:00:00Z, at which this report was saved in the backend. seconds',
    'envelope.[*].context.carCapturedTimeStamp.nanos':
        'Fractions of a second at nanosecond resolution, complementing the seconds field. nano seconds',
    'envelope.[*].context.carCapturedTimeStamp.seconds':
        'Indicates the seconds, of UTC time since Unix epoch 1970-01- 01T00:00:00Z, at which the vehicle sent this report to the backend. seconds',
    'envelope.[*].context.causedBy':
        'If the report contains an error then this field describes what cause the error, for instance EDIT_CLIMA_TIMERS.',
    'envelope.[*].context.errorContext.errorType':
        'Provides information about the error type.',
    'envelope.[*].context.messageId':
        'A generated string that uniquely identifies the vehicle message.',
    'envelope.[*].context.payloadType':
        'Specified the type of the report, which this context is part of, for instance CLIMA_SETTINGS_REPOR T.',
    'envelope.[*].context.trackingIdentifier':
        'String to be used for tracking purposes.',
    'envelope.[*].report.backendError.errorDescription':
        'A description describing the error state of the backend.',
    'envelope.[*].report.backendError.errorNumber':
        'A number that represents a error code in the backend.',
    'envelope.[*].report.climatizationElementSettings.isClimatizationAtUnlock':
        'A settings value that describes if the climatization should start after opening the doors with the car key.',
    'envelope.[*].report.climatizationElementSettings.zoneFrontLeftEnabled':
        'A settings value that describes if front left zone (seat) should be acclimatized.',
    'envelope.[*].report.climatizationElementSettings.zoneFrontRightEnabled':
        'A settings value that describes if front right zone (seat) should be acclimatized.',
    'envelope.[*].report.climatizationElementSettings.zoneRearLeftEnabled':
        'A settings value that describes if rear left zone (seat) should be acclimatized.',
    'envelope.[*].report.climatizationElementSettings.zoneRearRightEnabled':
        'A settings value that describes if rear right zone (seat) should be acclimatized.',
    'envelope.[*].report.climatizationMode':
        'Describes climatization mode. If \\"UNDEFINED\\" then not applicable for that vehicle brand and or model.',
    'envelope.[*].report.climatizationWithoutExternalPower':
        'A settings value that determines if the infrastructure is inactive or available. If the battery is low (less than 20%), climatization will not be started.',
    'envelope.[*].report.instrumentClusterTime':
        'Describes the time with time zone as set by the user inside the car. date-time string (date-time)',
    'envelope.[*].report.messageId':
        'A value that is used to identify a message.',
    'envelope.[*].report.remainingClimatizationTime_min.nanos':
        'Fractions of a second at nanosecond resolution, complementing the seconds field. nano seconds',
    'envelope.[*].report.remainingClimatizationTime_min.seconds':
        'Describes how long time (in seconds) it is left until the climatization has (approximately) reached the climatization goal. seconds',
    'envelope.[*].report.status':
        'Describes what the vehicle is doing to reach the wanted temperature, e.g. cooling, heating or ventilating.',
    'envelope.[*].report.targetTemperature.temperature':
        'A settings value that describes what temperature the vehicle shall reach while climatization is active. integer (0-100)',
    'envelope.[*].report.targetTemperature.unit':
        'The temperature unit used, can be either celsius or fahrenheit.',
    'envelope.[*].report.timers.id':
        'A number that is used to identify the different timers.',
    'envelope.[*].report.timers.isEnabled':
        'A value that describes if the timer is enabled or not.',
    'envelope.[*].report.trigger':
        'Describes why the climatization has started, e.g. climatization timer, charging profile timer or immediately by user.',
    'envelope.[*].report.windowHeatingState':
        'Describes the window heating status, e.g. invalid, off or on.',
    'hasWarnedDailyPowerBudget':
        'Pre ID.S3 indicates daily energy budget is almost used up Boolean',
    'hasWarnedPowerLevel':
        'Indicates energy budget is almost used up Boolean',
    'home_storage_charging':
        'The option to start bi- directional DC charging where the vehicle offers to either provide energy to the home storage or store energy surplus is currently available.',
    'hood_info.hood_lock_status.value':
        'Information regarding the hood of the vehicle with subcategory hood lock status with subcategory value Enum',
    'hood_info.hood_status.value':
        'Information regarding the hood of the vehicle with subcategory hood status with subcategory value Enum',
    'hvbatterytemperature_info.max_temperature.unit':
        'Information regarding the hvbatterytemperature of the vehicle with subcategory max temperature with subcategory unit Enum',
    'hvbatterytemperature_info.max_temperature.value':
        'Information regarding the hvbatterytemperature of the vehicle with subcategory max temperature with subcategory value',
    'hvbatterytemperature_info.min_temperature.unit':
        'Information regarding the hvbatterytemperature of the vehicle with subcategory min temperature with subcategory unit Enum',
    'hvbatterytemperature_info.min_temperature.value':
        'Information regarding the hvbatterytemperature of the vehicle with subcategory min temperature with subcategory value',
    'hvsoc_info.value':
        'Information regarding the hvsoc of the vehicle with subcategory value %',
    'immediate_charging':
        'The option to start charging immediately is currently available.',
    'immediate_discharging':
        'The option to start bi- directional DC charging to discharge the vehicle to provide power to the home storage is currently available.',
    'instrumentClusterTime':
        'The time that is adjusted inside the vehicle. h string (date-time)',
    'interiorClimatizationConsumption':
        'value of interior climatization consumption 1/h float32',
    'isConnected':
        'vehicle is considered connected Boolean',
    'maxChargingCurrent':
        'The value indicating if the vehicle shall use max or a reduced amount of current while charging. A string (enum)',
    'mileage_info.unit':
        'Information regarding the mileage of the vehicle with subcategory unit Enum',
    'mileage_info.value':
        'Information regarding the mileage of the vehicle with subcategory value',
    'only_own_current':
        'The option to start charging with Home Energy Management System (HEMS) is currently available.',
    'osShutdown':
        'Communications unit is shutting down Boolean',
    'outdoortemperature_info.unit':
        'Information regarding the outdoortemperature of the vehicle with subcategory unit Enum',
    'outdoortemperature_info.value':
        'Information regarding the outdoortemperature of the vehicle with subcategory value',
    'parking_brake_info.value':
        'Information regarding the parking brake of the vehicle with subcategory value',
    'parking_lights_info.left_status.value':
        'Information regarding the parking lights of the vehicle with subcategory left status with subcategory value Enum',
    'parking_lights_info.right_status.value':
        'Information regarding the parking lights of the vehicle with subcategory right status with subcategory value Enum',
    'payloadType':
        'The type of report sent by the vehicle. string (enum)',
    'plugStatusItem.chargingPlugType':
        'The type of plug. string (enum)',
    'plugStatusItem.flapLockState':
        'The flap lock state.',
    'plugStatusItem.flap_open_state':
        'The flap open state. string (enum)',
    'plugStatusItem.infrastructureState':
        'The current state of infrastructure. string (enum)',
    'plugStatusItem.plugConnectionState':
        'The current plug connection state. string (enum)',
    'plugStatusItem.plugLockState':
        'The current lock state of the plug. string (enum)',
    'plugStatusItem.plugPosition':
        'The position of the plug. string (enum)',
    'preferred_charging_times':
        'The option to start charging using preferred charging times is currently available.',
    'resetUseHV':
        'Indicates a factory reset has occurred and the useHV option should be reset when the vehicle is back online Boolean',
    'residualConsumption':
        'value of energy consumption of residual car network components 1/h float32',
    'service_maintenance_info.due_in_time.value':
        'Information regarding the service maintenance of the vehicle with subcategory due in time with subcategory value',
    'service_maintenance_info.service_type':
        'Information regarding the service maintenance of the vehicle with subcategory service type',
    'state.notification':
        'The current battery charging care mode notification state. string (enum)',
    'state.threshold':
        'The maximum charge limit of SOC enforced while battery care mode is active. % SOC',
    'targetSoc_pct':
        'The maximum charge level the battery should be charged as specified by the user. The allowed range is between 25% and 100%. %',
    'timer_charging':
        'The option to start charging with a timer is currently available.',
    'timer_charging_climatization':
        'The option to start charging and start climatization with a timer is currently available.',
    'trunk_lid_info.trunk_lid_lock_status.value':
        'Information regarding the trunk lid of the vehicle with subcategory trunk lid lock status with subcategory value Enum',
    'trunk_lid_info.trunk_lid_status.value':
        'Information regarding the trunk lid of the vehicle with subcategory trunk lid status with subcategory value Enum',
    'unlock_all':
        'Remote Unlock All Doors',
    'useHVMessageId':
        'used for managing acknowledgement ofÂ HVÂ setting Sring',
    'vehicleError.errorDescription':
        'The description of the error code from the car.',
    'vehicleError.errorNumber':
        'The number that represents the error code from the car.',
    'vehicleIdentifier':
        'The unique identifier of the vehicle used in the Device Platform backend.',
    'vehiclePlatform':
        'The type of vehicle platform. string (enum)',
    'window_info.front_left.window_percentage_open.value':
        'Information regarding the window of the vehicle with subcategory front left with subcategory window percentage open with subcategory value %',
    'window_info.front_left.window_status.value':
        'Information regarding the window of the vehicle with subcategory front left with subcategory window status with subcategory value Enum',
    'window_info.front_right.window_percentage_open.value':
        'Information regarding the window of the vehicle with subcategory front right with subcategory window percentage open with subcategory value %',
    'window_info.front_right.window_status.value':
        'Information regarding the window of the vehicle with subcategory front right with subcategory window status with subcategory value Enum',
    'window_info.rear_left.window_percentage_open.value':
        'Information regarding the window of the vehicle with subcategory rear left with subcategory window percentage open with subcategory value %',
    'window_info.rear_left.window_status.value':
        'Information regarding the window of the vehicle with subcategory rear left with subcategory window status with subcategory value Enum',
    'window_info.rear_right.window_percentage_open.value':
        'Information regarding the window of the vehicle with subcategory rear right with subcategory window percentage open with subcategory value %',
    'window_info.rear_right.window_status.value':
        'Information regarding the window of the vehicle with subcategory rear right with subcategory window status with subcategory value Enum',
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
          include_identifiers=False, vehicle_title=None, pack_kwh=None, utc_offset=None):
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
                   .replace("__VEHICLE_TITLE__", safe_title)
    with open(out_path, "w") as f:
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
<html lang="en">
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

/* ---------- helpers ---------- */
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
function loc(t){ return new Date((t + OFF) * 1000); }        // read with UTC getters
function fmtD(t){ const d = loc(t); return MON[d.getUTCMonth()] + " " + d.getUTCDate(); }
function fmtDT(t){ const d = loc(t);
  return MON[d.getUTCMonth()] + " " + d.getUTCDate() + ", " +
    String(d.getUTCHours()).padStart(2,"0") + ":" + String(d.getUTCMinutes()).padStart(2,"0"); }
function fmtFull(t){ if (!t) return "Undated"; const d = loc(t);
  return d.getUTCFullYear() + "-" + String(d.getUTCMonth()+1).padStart(2,"0") + "-" +
    String(d.getUTCDate()).padStart(2,"0") + " " + String(d.getUTCHours()).padStart(2,"0") + ":" +
    String(d.getUTCMinutes()).padStart(2,"0"); }
function fmtT(t){ const d = loc(t);
  return String(d.getUTCHours()).padStart(2,"0") + ":" + String(d.getUTCMinutes()).padStart(2,"0"); }
function fmtN(v, dec){ return Number(v).toLocaleString("en-US",
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
      if (whenTxt) tt.appendChild(el("div","when", whenTxt));
      for (const r of rows){
        const row = el("div","row");
        if (r.color){ const k = el("span","key"); k.style.borderTopColor = r.color; row.appendChild(k); }
        row.appendChild(el("span","v", r.value));
        if (r.name) row.appendChild(el("span","n", r.name));
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
    lb.textContent = DOW[d]; svg.appendChild(lb);
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
        DOW[d] + " " + String(h).padStart(2,"0") + ":00–" + String(h+1).padStart(2,"0") + ":00",
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
  const pretty = s => s == null ? "—" : String(s).replaceAll("_"," ").toLowerCase()
      .replace(/^\w/, c => c.toUpperCase());
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
  cards.heat.setRows(grid.map((row,d) => [DOW[d], ...row]));

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
  const ew = el("div","tableWrap"); ew.appendChild(buildTable(
    ["Time ("+DATA.tzLabel+")","Kind","Event","Detail"],
    DATA.events.map(e => [fmtFull(e.time),e.kind,e.event,e.detail || "—"]))); ec.appendChild(ew);
  if (DATA.events.length) wrap.appendChild(ec);

  const cfg = el("div","card"), ch = el("header"), cg = el("div","grow");
  const explicit = DATA.configuration.filter(c => c.source === "explicit").length;
  cg.appendChild(el("h2",null,"Vehicle configuration snapshot"));
  cg.appendChild(el("div","sub",DATA.configuration.length + " settings found · " + explicit +
    " explicit values · raw encodings retained when the dictionary is ambiguous"));
  ch.appendChild(cg); ch.appendChild(prov("observed")); cfg.appendChild(ch);
  const cw = el("div","tableWrap"); cw.appendChild(buildTable(
    ["Time","Field","Interpreted value","Raw","Source","Dictionary description"],
    DATA.configuration.map(c => [fmtFull(c.time),c.field,c.value,c.raw,c.source,c.description || "—"])));
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
  const mw = el("div","tableWrap"); mw.appendChild(buildTable(
    ["Field","In dictionary","In export"],C.expectedFields.map(f =>
      [f.field,f.dictionary ? "Yes" : "No",f.export ? "Yes" : "No"])));
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
function renderAll(){ renderKpis(); renderCharts(); renderDriveTables(); }
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
          utc_offset=args.utc_offset)


if __name__ == "__main__":
    main()
