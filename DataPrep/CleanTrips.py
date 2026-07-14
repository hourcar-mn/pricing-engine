"""
01_clean_trips_attach_invoice_plan_and_segment.py

Inputs
------
Trips_2026-06-08.csv
Invoices_2026-06-08.csv

Place both raw CSV files in the same folder as this script, or update
RAW_DATA_DIR / TRIPS_CSV_NAME / INVOICES_CSV_NAME in the settings block below.

Output
------
01_clean_trips_invoice/clean_trips_with_invoice_plan_and_segment.csv

If WRITE_DASHBOARD_COPY is True, the same file is also written one folder up so
the dashboard can read it directly.

Purpose
-------
Create a clean trip-level file using only trips and invoices.

The script does five things:

1. Clean trips:
   - keep FREE_FLOATING trips only;
   - drop canceled trips;
   - drop trips shorter than MIN_DURATION_MINUTES;
   - drop trips shorter than MIN_DISTANCE_MILES.

2. Attach invoice plan:
   - merge invoice MobilityPlan by TripID;
   - normalize plan names into:
       Free Plan, Everyday, Access, Student;
   - treat Adventure / missing / unknown plans as unusable;
   - keep a TripID only if it has exactly one usable normalized plan.

3. Assign user segment from all observed usable invoice plans:
   - Regular if user only used Free Plan and/or Everyday;
   - Access if user only used Free Plan and/or Access, and used Access at least once;
   - Student if user only used Free Plan and/or Student, and used Student at least once;
   - Conflicting otherwise.

4. Attach posted pricing parameters at the time of trip:
   - PaidPlanPricingAtTrip: segment-specific paid plan price parameters;
   - FreePlanPricingAtTrip: Free Plan price parameters;
   - UNKNOWN_POSTED_PRICE for trips before known posted pricing;
   - CONFLICTING_USER_SEGMENT for paid-plan pricing when user segment is Conflicting.

5. Write a minimal output file.
"""

from pathlib import Path
import json
import pandas as pd


# =============================================================================
# SETTINGS
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
RAW_DATA_DIR = SCRIPT_DIR

TRIPS_CSV_NAME = "Trips_2026-06-08.csv"
INVOICES_CSV_NAME = "Invoices_2026-06-08.csv"

TRIPS_CSV = RAW_DATA_DIR / TRIPS_CSV_NAME
INVOICES_CSV = RAW_DATA_DIR / INVOICES_CSV_NAME

OUTPUT_DIR = SCRIPT_DIR / "01_clean_trips_invoice"
OUTPUT_CSV = OUTPUT_DIR / "clean_trips_with_invoice_plan_and_segment.csv"

WRITE_DASHBOARD_COPY = True
DASHBOARD_COPY_CSV = SCRIPT_DIR.parent / "clean_trips_with_invoice_plan_and_segment.csv"

SERVICE_TYPE_KEEP = "FREE_FLOATING"

MIN_DURATION_MINUTES = 3.0
MIN_DISTANCE_MILES = 0.5

MAX_DURATION_MINUTES = 10_000_000.0
MAX_DISTANCE_MILES = 10_000_000.0

TRIP_ID_COL = "TripID"
USER_ID_COL = "UserID"
TRIP_START_COL = "StartDateUTC"
DISTANCE_COL = "TripDistance_mi"
DURATION_COL = "TripDuration"
SERVICE_TYPE_COL = "ServiceType"
CANCEL_COL = "IsCancel"


# =============================================================================
# EVIE POSTED PRICING RULES
# =============================================================================

# To add a future price change:
# 1. Add a new EVIE_RULES_START_* timestamp.
# 2. Add a new EVIE_PRICING_RULES entry with all four plans and all tariff keys.
# 3. Add the new regime check at the top of pricing_regime_at_trip().
# 4. Make the same pricing-rule update in CreateUserMonthData.py.

EVIE_RULES_START_2023 = pd.Timestamp("2023-03-31")
EVIE_RULES_START_2024 = pd.Timestamp("2024-03-04")
EVIE_RULES_START_2025_MARCH = pd.Timestamp("2025-03-03")
EVIE_RULES_START_2025_JULY = pd.Timestamp("2025-07-01")
EVIE_RULES_START_2025_AUGUST = pd.Timestamp("2025-08-06")

EVIE_PRICING_RULES = {
    "2023": {
        "Everyday":  {"per_minute": 0.195,  "per_hour": 9.75,    "per_day": 84.50,  "included_miles": 100.0, "extra_mile": 0.4875, "trip_start_fee": 0, "monthly_fee": 7, "monthly_credit": 7},
        "Access":    {"per_minute": 0.180,  "per_hour": 9.00,    "per_day": 78.00,  "included_miles": 200.0, "extra_mile": 0.4500, "trip_start_fee": 0, "monthly_fee": 1, "monthly_credit": 6},
        "Student":   {"per_minute": 0.180,  "per_hour": 9.00,    "per_day": 78.00,  "included_miles": 100.0, "extra_mile": 0.4500, "trip_start_fee": 0, "monthly_fee": 6, "monthly_credit": 6},
        "Free Plan": {"per_minute": 0.300,  "per_hour": 15.00,   "per_day": 130.00, "included_miles": 0.0,   "extra_mile": 0.7500, "trip_start_fee": 0, "monthly_fee": 0, "monthly_credit": 0},
    },
    "2024": {
        "Everyday":  {"per_minute": 0.2145, "per_hour": 10.66,   "per_day": 91.00,  "included_miles": 100.0, "extra_mile": 0.4875, "trip_start_fee": 0, "monthly_fee": 7, "monthly_credit": 7},
        "Access":    {"per_minute": 0.1980, "per_hour": 9.84,    "per_day": 84.00,  "included_miles": 200.0, "extra_mile": 0.4500, "trip_start_fee": 0, "monthly_fee": 1, "monthly_credit": 6},
        "Student":   {"per_minute": 0.1980, "per_hour": 9.84,    "per_day": 84.00,  "included_miles": 100.0, "extra_mile": 0.4900, "trip_start_fee": 0, "monthly_fee": 6, "monthly_credit": 6},
        "Free Plan": {"per_minute": 0.3300, "per_hour": 16.40,   "per_day": 140.00, "included_miles": 0.0,   "extra_mile": 0.7500, "trip_start_fee": 0, "monthly_fee": 0, "monthly_credit": 0},
    },
    "2025_MARCH": {
        "Everyday":  {"per_minute": 0.2275, "per_hour": 10.9785, "per_day": 93.73,  "included_miles": 200.0, "extra_mile": 0.5070, "trip_start_fee": 0, "monthly_fee": 7, "monthly_credit": 0},
        "Access":    {"per_minute": 0.2100, "per_hour": 10.1340, "per_day": 86.52,  "included_miles": 200.0, "extra_mile": 0.4680, "trip_start_fee": 0, "monthly_fee": 1, "monthly_credit": 0},
        "Student":   {"per_minute": 0.2100, "per_hour": 10.1340, "per_day": 86.52,  "included_miles": 200.0, "extra_mile": 0.4680, "trip_start_fee": 0, "monthly_fee": 6, "monthly_credit": 0},
        "Free Plan": {"per_minute": 0.3500, "per_hour": 16.89,   "per_day": 144.20, "included_miles": 10.0,  "extra_mile": 0.7800, "trip_start_fee": 0, "monthly_fee": 0, "monthly_credit": 0},
    },
    "2025_JULY": {
        "Everyday":  {"per_minute": 0.2275, "per_hour": 10.9785, "per_day": 93.73,  "included_miles": 200.0, "extra_mile": 0.5070, "trip_start_fee": 1, "monthly_fee": 7, "monthly_credit": 0},
        "Access":    {"per_minute": 0.2100, "per_hour": 10.1340, "per_day": 86.52,  "included_miles": 200.0, "extra_mile": 0.4680, "trip_start_fee": 1, "monthly_fee": 1, "monthly_credit": 0},
        "Student":   {"per_minute": 0.2100, "per_hour": 10.1340, "per_day": 86.52,  "included_miles": 200.0, "extra_mile": 0.4680, "trip_start_fee": 1, "monthly_fee": 6, "monthly_credit": 0},
        "Free Plan": {"per_minute": 0.3500, "per_hour": 16.89,   "per_day": 144.20, "included_miles": 10.0,  "extra_mile": 0.7800, "trip_start_fee": 1, "monthly_fee": 0, "monthly_credit": 0},
    },
    "2025_AUGUST": {
        "Everyday":  {"per_minute": 0.2957, "per_hour": 14.27,   "per_day": 121.85, "included_miles": 200.0, "extra_mile": 0.6590, "trip_start_fee": 1, "monthly_fee": 7, "monthly_credit": 0},
        "Access":    {"per_minute": 0.2730, "per_hour": 13.17,   "per_day": 112.48, "included_miles": 200.0, "extra_mile": 0.6080, "trip_start_fee": 1, "monthly_fee": 1, "monthly_credit": 0},
        "Student":   {"per_minute": 0.2730, "per_hour": 13.17,   "per_day": 112.48, "included_miles": 200.0, "extra_mile": 0.6080, "trip_start_fee": 1, "monthly_fee": 6, "monthly_credit": 0},
        "Free Plan": {"per_minute": 0.4550, "per_hour": 21.96,   "per_day": 187.46, "included_miles": 10.0,  "extra_mile": 1.0140, "trip_start_fee": 1, "monthly_fee": 0, "monthly_credit": 0},
    },
}

SEGMENT_PAID_PLAN = {
    "Regular": "Everyday",
    "Access": "Access",
    "Student": "Student",
}


# =============================================================================
# HELPERS
# =============================================================================

def print_stage(title):
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)


def print_drop(label, before, after):
    dropped = before - after
    pct = 0 if before == 0 else 100 * dropped / before
    print(f"{label:<60} {before:>12,} -> {after:>12,}  dropped {dropped:>12,} ({pct:5.1f}%)")


def parse_datetime(series):
    return pd.to_datetime(series, errors="coerce", utc=True).dt.tz_convert(None)


def normalize_cancel_flag(x):
    if pd.isna(x):
        return False
    return str(x).strip().upper() in {"TRUE", "T", "1", "YES", "Y"}


def normalize_plan_name(plan):
    if pd.isna(plan):
        return None

    p = str(plan).strip()
    low = p.lower()

    if "adventure" in low or "error" in low:
        return None

    p = (
        p.replace(" PLUS", "")
         .replace(" plus", "")
         .replace("+PLUS", "")
         .replace("+plus", "")
         .strip()
    )
    low = p.lower()

    if "student" in low:
        return "Student"
    if "access" in low:
        return "Access"
    if "everyday" in low or "standard" in low:
        return "Everyday"
    if "free" in low:
        return "Free Plan"

    return None


def classify_user_segment(plans):
    observed = set(plans.dropna().astype(str))

    if observed.issubset({"Free Plan", "Everyday"}):
        return "Regular"

    if observed.issubset({"Free Plan", "Access"}) and "Access" in observed:
        return "Access"

    if observed.issubset({"Free Plan", "Student"}) and "Student" in observed:
        return "Student"

    return "Conflicting"


def pricing_regime_at_trip(trip_dt):
    if pd.isna(trip_dt):
        return None

    if trip_dt >= EVIE_RULES_START_2025_AUGUST:
        return "2025_AUGUST"
    if trip_dt >= EVIE_RULES_START_2025_JULY:
        return "2025_JULY"
    if trip_dt >= EVIE_RULES_START_2025_MARCH:
        return "2025_MARCH"
    if trip_dt >= EVIE_RULES_START_2024:
        return "2024"
    if trip_dt >= EVIE_RULES_START_2023:
        return "2023"

    return None


def compact_price_string(regime, plan):
    payload = {"regime": regime, "plan": plan}
    payload.update(EVIE_PRICING_RULES[regime][plan])
    return json.dumps(payload, separators=(",", ":"))


def get_pricing_strings(trip_dt, user_segment):
    regime = pricing_regime_at_trip(trip_dt)

    if regime is None:
        return "UNKNOWN_POSTED_PRICE", "UNKNOWN_POSTED_PRICE"

    free_price = compact_price_string(regime, "Free Plan")

    if user_segment == "Conflicting":
        return "CONFLICTING_USER_SEGMENT", free_price

    paid_plan = SEGMENT_PAID_PLAN.get(user_segment)
    if paid_plan is None:
        return "UNKNOWN_USER_SEGMENT", free_price

    paid_price = compact_price_string(regime, paid_plan)
    return paid_price, free_price


# =============================================================================
# STEP 1: LOAD AND CLEAN TRIPS
# =============================================================================

print_stage("STEP 1: Load and clean trips")

trips = pd.read_csv(TRIPS_CSV, low_memory=False)

print(f"Raw trip rows: {len(trips):,}")
print(f"Raw unique users: {trips[USER_ID_COL].nunique():,}")
print(f"Raw unique TripIDs: {trips[TRIP_ID_COL].nunique():,}")

required_trip_cols = [
    TRIP_ID_COL,
    USER_ID_COL,
    TRIP_START_COL,
    DISTANCE_COL,
    DURATION_COL,
    SERVICE_TYPE_COL,
    CANCEL_COL,
]

missing_trip_cols = [c for c in required_trip_cols if c not in trips.columns]
if missing_trip_cols:
    raise ValueError(f"Missing required trip columns: {missing_trip_cols}")

trips["TripID"] = trips[TRIP_ID_COL]
trips["UserID"] = trips[USER_ID_COL]
trips["TripStartDT"] = parse_datetime(trips[TRIP_START_COL])
trips["TripDistanceMiles"] = pd.to_numeric(trips[DISTANCE_COL], errors="coerce")
trips["TripDurationMinutes"] = pd.to_numeric(trips[DURATION_COL], errors="coerce")

before = len(trips)
trips = trips[
    trips["TripID"].notna()
    & trips["UserID"].notna()
    & trips["TripStartDT"].notna()
].copy()
print_drop("Require TripID, UserID, valid trip start", before, len(trips))

before = len(trips)
trips = trips[
    trips[SERVICE_TYPE_COL].astype(str).str.upper().eq(SERVICE_TYPE_KEEP)
].copy()
print_drop(f"Keep ServiceType == {SERVICE_TYPE_KEEP}", before, len(trips))

before = len(trips)
trips = trips[~trips[CANCEL_COL].map(normalize_cancel_flag)].copy()
print_drop("Drop canceled trips", before, len(trips))

before = len(trips)
trips = trips[
    trips["TripDurationMinutes"].between(
        MIN_DURATION_MINUTES,
        MAX_DURATION_MINUTES,
        inclusive="both",
    )
].copy()
print_drop(f"Keep duration >= {MIN_DURATION_MINUTES} minutes", before, len(trips))

before = len(trips)
trips = trips[
    trips["TripDistanceMiles"].between(
        MIN_DISTANCE_MILES,
        MAX_DISTANCE_MILES,
        inclusive="both",
    )
].copy()
print_drop(f"Keep distance >= {MIN_DISTANCE_MILES} miles", before, len(trips))

before = len(trips)
trips = trips.sort_values(["TripID", "TripStartDT"]).drop_duplicates("TripID", keep="first").copy()
print_drop("Drop duplicate TripID rows in trip file", before, len(trips))

trips["YearMonth"] = trips["TripStartDT"].dt.to_period("M")
trips["MonthOfYear"] = trips["TripStartDT"].dt.month.astype(int)

print("\nClean trip rows before invoice merge:", f"{len(trips):,}")
print("Clean trip users before invoice merge:", f"{trips['UserID'].nunique():,}")


# =============================================================================
# STEP 2: LOAD AND COLLAPSE INVOICE PLANS
# =============================================================================

print_stage("STEP 2: Load and collapse invoice plans")

invoices = pd.read_csv(
    INVOICES_CSV,
    usecols=["TripID", "MobilityPlan"],
    low_memory=False,
).copy()

invoices = invoices.rename(columns={"MobilityPlan": "RawMobilityPlan"})

print(f"Raw invoice rows: {len(invoices):,}")
print(f"Raw unique invoice TripIDs: {invoices['TripID'].nunique():,}")
print(f"Duplicate invoice rows by TripID: {invoices['TripID'].duplicated().sum():,}")

print("\nRaw invoice MobilityPlan counts:")
print(invoices["RawMobilityPlan"].value_counts(dropna=False).to_string())

invoices["PlanUsedForTrip"] = invoices["RawMobilityPlan"].map(normalize_plan_name)

print("\nNormalized invoice plan counts:")
print(invoices["PlanUsedForTrip"].value_counts(dropna=False).to_string())

usable_pairs = (
    invoices.loc[
        invoices["PlanUsedForTrip"].notna(),
        ["TripID", "PlanUsedForTrip"],
    ]
    .drop_duplicates()
)

plan_counts_by_trip = (
    usable_pairs
    .groupby("TripID", as_index=False)
    .size()
    .rename(columns={"size": "NumUsablePlansForTrip"})
)

all_invoice_tripids = invoices[["TripID"]].drop_duplicates()

invoice_diag = all_invoice_tripids.merge(plan_counts_by_trip, on="TripID", how="left")
invoice_diag["InvoiceTripIDFound"] = True
invoice_diag["NumUsablePlansForTrip"] = (
    invoice_diag["NumUsablePlansForTrip"]
    .fillna(0)
    .astype(int)
)

print("\nInvoice TripID diagnostics:")
print("TripIDs with exactly one usable plan:",
      f"{(invoice_diag['NumUsablePlansForTrip'] == 1).sum():,}")
print("TripIDs with zero usable plans:",
      f"{(invoice_diag['NumUsablePlansForTrip'] == 0).sum():,}")
print("TripIDs with conflicting usable plans:",
      f"{(invoice_diag['NumUsablePlansForTrip'] > 1).sum():,}")

clean_invoice_plans = usable_pairs.merge(
    plan_counts_by_trip.loc[
        plan_counts_by_trip["NumUsablePlansForTrip"] == 1,
        ["TripID"],
    ],
    on="TripID",
    how="inner",
).drop_duplicates("TripID")

if clean_invoice_plans["TripID"].duplicated().any():
    raise RuntimeError("Invoice plan collapse failed: duplicate TripID remains.")

print("\nCollapsed invoice plan rows available for merge:",
      f"{len(clean_invoice_plans):,}")


# =============================================================================
# STEP 3: MERGE INVOICE PLAN ONTO CLEAN TRIPS
# =============================================================================

print_stage("STEP 3: Merge invoice plan onto clean trips")

before = len(trips)

merged = trips.merge(invoice_diag, on="TripID", how="left")
merged["InvoiceTripIDFound"] = merged["InvoiceTripIDFound"].fillna(False)
merged["NumUsablePlansForTrip"] = merged["NumUsablePlansForTrip"].fillna(-1).astype(int)

merged = merged.merge(clean_invoice_plans, on="TripID", how="left")

after = len(merged)

if before != after:
    raise RuntimeError(
        f"Trip row count changed during invoice merge: {before:,} -> {after:,}."
    )

print(f"Trips after merge: {len(merged):,}")
print(f"Trips with usable invoice plan: {merged['PlanUsedForTrip'].notna().sum():,}")
print(f"Trips missing usable invoice plan: {merged['PlanUsedForTrip'].isna().sum():,}")

missing_plan = merged[merged["PlanUsedForTrip"].isna()].copy()

no_invoice = missing_plan[~missing_plan["InvoiceTripIDFound"]]
zero_usable = missing_plan[
    missing_plan["InvoiceTripIDFound"]
    & (missing_plan["NumUsablePlansForTrip"] == 0)
]
conflicting_invoice = missing_plan[
    missing_plan["InvoiceTripIDFound"]
    & (missing_plan["NumUsablePlansForTrip"] > 1)
]

print("\nSplit of trips missing usable invoice plan:")
print(f"No invoice row for TripID:              {len(no_invoice):,}")
print(f"Invoice exists but zero usable plans:   {len(zero_usable):,}")
print(f"Invoice has conflicting usable plans:   {len(conflicting_invoice):,}")

if len(zero_usable) > 0:
    bad_invoice_rows = invoices[invoices["TripID"].isin(set(zero_usable["TripID"]))].copy()
    print("\nRaw MobilityPlan counts among zero-usable-plan dropped trips:")
    print(bad_invoice_rows["RawMobilityPlan"].value_counts(dropna=False).to_string())

print("\nPlan counts before dropping missing:")
print(merged["PlanUsedForTrip"].value_counts(dropna=False).to_string())

before = len(merged)
clean = merged[merged["PlanUsedForTrip"].notna()].copy()
print_drop("Drop trips missing usable invoice plan", before, len(clean))

print("\nFinal clean trip rows before segment assignment:", f"{len(clean):,}")
print("Final clean trip users before segment assignment:", f"{clean['UserID'].nunique():,}")


# =============================================================================
# STEP 4: ASSIGN USER SEGMENTS
# =============================================================================

print_stage("STEP 4: Assign user segments from observed invoice plans")

user_segments = (
    clean
    .groupby("UserID")["PlanUsedForTrip"]
    .apply(classify_user_segment)
    .reset_index(name="UserSegment")
)

print("\nUser counts by segment:")
print(user_segments["UserSegment"].value_counts(dropna=False).to_string())

clean = clean.merge(user_segments, on="UserID", how="left")

if clean["UserSegment"].isna().any():
    raise RuntimeError("Some clean trips failed to receive a UserSegment.")

print("\nTrip counts by user segment:")
print(clean["UserSegment"].value_counts(dropna=False).to_string())

print("\nTrip counts by user segment and trip plan:")
print(pd.crosstab(clean["UserSegment"], clean["PlanUsedForTrip"]).to_string())

print("\nObserved plan sets by user:")
plan_sets = (
    clean
    .groupby("UserID")["PlanUsedForTrip"]
    .apply(lambda x: " + ".join(sorted(set(x.dropna().astype(str)))))
)
print(plan_sets.value_counts().to_string())


# =============================================================================
# STEP 5: ATTACH PRICING PARAMETERS
# =============================================================================

print_stage("STEP 5: Attach pricing parameters")

paid_prices = []
free_prices = []

for trip_dt, segment in zip(clean["TripStartDT"], clean["UserSegment"]):
    paid_price, free_price = get_pricing_strings(trip_dt, segment)
    paid_prices.append(paid_price)
    free_prices.append(free_price)

clean["PaidPlanPricingAtTrip"] = paid_prices
clean["FreePlanPricingAtTrip"] = free_prices

print("Trips before known posted pricing:",
      f"{(clean['PaidPlanPricingAtTrip'] == 'UNKNOWN_POSTED_PRICE').sum():,}")
print("Trips with conflicting user segment paid-plan pricing:",
      f"{(clean['PaidPlanPricingAtTrip'] == 'CONFLICTING_USER_SEGMENT').sum():,}")

print("\nPaidPlanPricingAtTrip special values:")
print(
    clean["PaidPlanPricingAtTrip"]
    .loc[clean["PaidPlanPricingAtTrip"].isin(["UNKNOWN_POSTED_PRICE", "CONFLICTING_USER_SEGMENT", "UNKNOWN_USER_SEGMENT"])]
    .value_counts(dropna=False)
    .to_string()
)

print("\nFreePlanPricingAtTrip special values:")
print(
    clean["FreePlanPricingAtTrip"]
    .loc[clean["FreePlanPricingAtTrip"].eq("UNKNOWN_POSTED_PRICE")]
    .value_counts(dropna=False)
    .to_string()
)


# =============================================================================
# STEP 6: WRITE MINIMAL OUTPUT
# =============================================================================

print_stage("STEP 6: Write output")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

keep_cols = [
    "TripID",
    "UserID",
    "UserSegment",
    "TripStartDT",
    "YearMonth",
    "MonthOfYear",
    "TripDurationMinutes",
    "TripDistanceMiles",
    "PlanUsedForTrip",
    "PaidPlanPricingAtTrip",
    "FreePlanPricingAtTrip",
]

out = clean[keep_cols].copy()
out["YearMonth"] = out["YearMonth"].astype(str)

out.to_csv(OUTPUT_CSV, index=False)

print(f"Wrote: {OUTPUT_CSV.resolve()}")
print(f"Rows written: {len(out):,}")

if WRITE_DASHBOARD_COPY:
    out.to_csv(DASHBOARD_COPY_CSV, index=False)
    print(f"Wrote dashboard copy: {DASHBOARD_COPY_CSV.resolve()}")

print("\nFinal rows by YearMonth:")
print(out["YearMonth"].value_counts().sort_index().to_string())
