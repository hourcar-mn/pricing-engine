"""
02_create_user_month_panels.py

Input
-----
01_clean_trips_invoice/clean_trips_with_invoice_plan_and_segment.csv

Run CleanTrips.py first. By default, this script reads the intermediate clean
trip file from the same folder as this script.

Outputs
-------
02_user_month_panels/user_month_all_segments.csv
02_user_month_panels/user_month_regular.csv
02_user_month_panels/user_month_access.csv
02_user_month_panels/user_month_student.csv

If WRITE_DASHBOARD_COPIES is True, the clean dashboard inputs are also written
one folder up so the dashboard can read them directly.

Purpose
-------
Build user-month panel data from the baseline clean trip file.

Logic
-----
1. Drop Conflicting users.
2. Keep pre-2023-04 trips only for market-membership history.
3. Start the output panel in 2023-04.
4. Create one row for every non-conflicting user and every output month.
5. Mark InMarket = 1 if the user used EVIE in the current month or in the prior K months.
6. Aggregate monthly trips, distance, duration, and modal monthly plan.
7. Attach monthly posted paid-plan and free-plan prices using the pricing rule active for the most days in the month.

Notes
-----
- For positive-use months, PlanUsedThisMonth is the most-used trip plan in that month.
- If there is a tie, the segment's paid plan is preferred over Free Plan.
- For zero-trip in-market months, PlanUsedThisMonth = Free Plan.
- For zero-trip not-in-market months, PlanUsedThisMonth = Not In Market.
"""

from pathlib import Path
import calendar
import json

import numpy as np
import pandas as pd


# =============================================================================
# SETTINGS
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_CSV = SCRIPT_DIR / "01_clean_trips_invoice" / \
    "clean_trips_with_invoice_plan_and_segment.csv"
OUTPUT_DIR = SCRIPT_DIR / "02_user_month_panels"

WRITE_DASHBOARD_COPIES = True
DASHBOARD_OUTPUT_DIR = SCRIPT_DIR.parent / "dashboard_data"

# Set this to lower (i.e. 12 months) to decrease market size
K_MONTHS_IN_MARKET = 24
INCLUDE_CURRENT_MONTH_USE = True

FIRST_OUTPUT_MONTH = "2023-04"
# None means use the last month in the baseline trip file.
LAST_OUTPUT_MONTH = None

SEGMENTS_TO_KEEP = ["Regular", "Access", "Student"]


# =============================================================================
# EVIE POSTED PRICING RULES
# =============================================================================

# To add a future price change:
# 1. Add a new EVIE_RULES_START_* timestamp.
# 2. Add a new EVIE_PRICING_RULES entry with all four plans and all tariff keys.
# 3. Add the new regime check at the top of pricing_regime_at_day().
# 4. Make the same pricing-rule update in CleanTrips.py.

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


def compact_price_string(regime, plan):
    payload = {"regime": regime, "plan": plan}
    payload.update(EVIE_PRICING_RULES[regime][plan])
    return json.dumps(payload, separators=(",", ":"))


def pricing_regime_at_day(day):
    day_ts = pd.Timestamp(day)
    if day_ts >= EVIE_RULES_START_2025_AUGUST:
        return "2025_AUGUST"
    if day_ts >= EVIE_RULES_START_2025_JULY:
        return "2025_JULY"
    if day_ts >= EVIE_RULES_START_2025_MARCH:
        return "2025_MARCH"
    if day_ts >= EVIE_RULES_START_2024:
        return "2024"
    if day_ts >= EVIE_RULES_START_2023:
        return "2023"
    return None


def majority_pricing_regime_for_month(month):
    month = pd.Period(month, freq="M")
    n_days = calendar.monthrange(month.year, month.month)[1]

    regimes = []
    for day_num in range(1, n_days + 1):
        day = pd.Timestamp(year=month.year, month=month.month, day=day_num)
        regimes.append(pricing_regime_at_day(day))

    counts = pd.Series(regimes).value_counts(dropna=False)
    known_counts = counts[counts.index.notna()]

    if len(known_counts) == 0:
        return None

    return known_counts.idxmax()


def monthly_pricing_strings(month, segment):
    regime = majority_pricing_regime_for_month(month)

    if regime is None:
        return "UNKNOWN_POSTED_PRICE", "UNKNOWN_POSTED_PRICE", "UNKNOWN_POSTED_PRICE"

    paid_plan = SEGMENT_PAID_PLAN[segment]
    paid_price = compact_price_string(regime, paid_plan)
    free_price = compact_price_string(regime, "Free Plan")

    return regime, paid_price, free_price


def choose_monthly_plan(plan_series, segment):
    counts = plan_series.value_counts(dropna=True)

    if counts.empty:
        return "Free Plan", False

    max_count = counts.max()
    tied = sorted(counts[counts == max_count].index.astype(str).tolist())
    had_tie = len(tied) > 1

    paid_plan = SEGMENT_PAID_PLAN[segment]

    if paid_plan in tied:
        return paid_plan, had_tie
    if "Free Plan" in tied:
        return "Free Plan", had_tie
    return tied[0], had_tie


def period_to_str(df):
    out = df.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.PeriodDtype):
            out[col] = out[col].astype(str)
    return out


# =============================================================================
# STEP 1: LOAD BASELINE TRIPS
# =============================================================================

print_stage("STEP 1: Load baseline clean trips")

if not INPUT_CSV.exists():
    raise FileNotFoundError(f"Missing input file: {INPUT_CSV.resolve()}")

trips = pd.read_csv(INPUT_CSV, low_memory=False)

required_cols = [
    "TripID",
    "UserID",
    "UserSegment",
    "TripStartDT",
    "YearMonth",
    "MonthOfYear",
    "TripDurationMinutes",
    "TripDistanceMiles",
    "PlanUsedForTrip",
]

missing = [c for c in required_cols if c not in trips.columns]
if missing:
    raise ValueError(f"Input file is missing required columns: {missing}")

trips["TripStartDT"] = pd.to_datetime(trips["TripStartDT"], errors="coerce")
trips["YearMonth"] = pd.PeriodIndex(trips["YearMonth"].astype(str), freq="M")
trips["TripDurationMinutes"] = pd.to_numeric(
    trips["TripDurationMinutes"], errors="coerce")
trips["TripDistanceMiles"] = pd.to_numeric(
    trips["TripDistanceMiles"], errors="coerce")

print(f"Input trip rows: {len(trips):,}")
print(f"Input users: {trips['UserID'].nunique():,}")

print("\nInput trip rows by segment:")
print(trips["UserSegment"].value_counts(dropna=False).to_string())

print("\nInput trip rows by segment and plan:")
print(pd.crosstab(trips["UserSegment"], trips["PlanUsedForTrip"]).to_string())


# =============================================================================
# STEP 2: DROP CONFLICTING USERS
# =============================================================================

print_stage("STEP 2: Drop conflicting users")

conflicting_users = trips.loc[trips["UserSegment"]
                              == "Conflicting", "UserID"].drop_duplicates()
print(f"Conflicting users: {len(conflicting_users):,}")
print(
    f"Trips from conflicting users: {(trips['UserID'].isin(conflicting_users)).sum():,}")

trips = trips[~trips["UserID"].isin(conflicting_users)].copy()

print(f"Trips after dropping conflicting users: {len(trips):,}")
print(f"Users after dropping conflicting users: {trips['UserID'].nunique():,}")

print("\nRemaining trip rows by segment and plan:")
print(pd.crosstab(trips["UserSegment"], trips["PlanUsedForTrip"]).to_string())


# =============================================================================
# STEP 3: DEFINE USERS AND OUTPUT MONTHS
# =============================================================================

print_stage("STEP 3: Define users and output months")

first_output_month = pd.Period(FIRST_OUTPUT_MONTH, freq="M")
last_output_month = pd.Period(
    LAST_OUTPUT_MONTH, freq="M") if LAST_OUTPUT_MONTH is not None else trips["YearMonth"].max()

if last_output_month < first_output_month:
    raise ValueError(
        f"Invalid output month range: {first_output_month} to {last_output_month}")

output_months = pd.period_range(
    first_output_month, last_output_month, freq="M")
users = trips[["UserID", "UserSegment"]].drop_duplicates()

if users["UserID"].duplicated().any():
    raise RuntimeError(
        "A user appears in more than one non-conflicting segment.")

print(f"Output month range: {first_output_month} to {last_output_month}")
print(f"Number of output months: {len(output_months):,}")
print(f"Number of panel users: {len(users):,}")

print("\nPanel users by segment:")
print(users["UserSegment"].value_counts().to_string())


# =============================================================================
# STEP 4: BUILD FULL USER-MONTH GRID
# =============================================================================

print_stage("STEP 4: Build full user-month grid")

month_df = pd.DataFrame({"YearMonth": output_months})
users_tmp = users.copy()
users_tmp["__key"] = 1
month_df["__key"] = 1

panel = users_tmp.merge(month_df, on="__key").drop(columns="__key")
panel = panel.sort_values(
    ["UserSegment", "UserID", "YearMonth"]).reset_index(drop=True)
panel["MonthOfYear"] = panel["YearMonth"].dt.month.astype(int)

print(f"Full user-month rows: {len(panel):,}")


# =============================================================================
# STEP 5: COMPUTE MARKET MEMBERSHIP
# =============================================================================

print_stage("STEP 5: Compute market membership")

history_months = (
    trips
    .groupby(["UserID", "YearMonth"], as_index=False)
    .agg(HistoryTripCount=("TripID", "count"))
)
history_months["UsedThisMonth"] = (
    history_months["HistoryTripCount"] > 0).astype(int)

history_start_month = trips["YearMonth"].min()
rolling_months = pd.period_range(
    history_start_month, last_output_month, freq="M")

rolling_month_df = pd.DataFrame({"YearMonth": rolling_months})
users_roll = users[["UserID"]].copy()
users_roll["__key"] = 1
rolling_month_df["__key"] = 1

rolling_grid = users_roll.merge(
    rolling_month_df, on="__key").drop(columns="__key")
rolling_grid = rolling_grid.merge(
    history_months[["UserID", "YearMonth",
                    "HistoryTripCount", "UsedThisMonth"]],
    on=["UserID", "YearMonth"],
    how="left",
)

rolling_grid["HistoryTripCount"] = rolling_grid["HistoryTripCount"].fillna(
    0).astype(int)
rolling_grid["UsedThisMonth"] = rolling_grid["UsedThisMonth"].fillna(
    0).astype(int)
rolling_grid = rolling_grid.sort_values(
    ["UserID", "YearMonth"]).reset_index(drop=True)

rolling_grid["UsedInPriorKMonths"] = (
    rolling_grid
    .groupby("UserID")["UsedThisMonth"]
    .transform(lambda x: x.shift(1).rolling(window=K_MONTHS_IN_MARKET, min_periods=1).max())
    .fillna(0)
    .astype(int)
)

if INCLUDE_CURRENT_MONTH_USE:
    rolling_grid["InMarket"] = np.maximum(
        rolling_grid["UsedThisMonth"],
        rolling_grid["UsedInPriorKMonths"],
    ).astype(int)
else:
    rolling_grid["InMarket"] = rolling_grid["UsedInPriorKMonths"].astype(int)

market_flags = rolling_grid[[
    "UserID", "YearMonth", "HistoryTripCount", "UsedThisMonth", "UsedInPriorKMonths", "InMarket"
]]

panel = panel.merge(market_flags, on=["UserID", "YearMonth"], how="left")

for col in ["HistoryTripCount", "UsedThisMonth", "UsedInPriorKMonths", "InMarket"]:
    panel[col] = panel[col].fillna(0).astype(int)

print(f"K months in market: {K_MONTHS_IN_MARKET}")
print(f"Include current-month users: {INCLUDE_CURRENT_MONTH_USE}")
print("\nUser-month counts by InMarket:")
print(panel["InMarket"].value_counts(dropna=False).sort_index().to_string())


# =============================================================================
# STEP 6: AGGREGATE MONTHLY USAGE
# =============================================================================

print_stage("STEP 6: Aggregate monthly usage")

output_trips = trips[trips["YearMonth"].isin(output_months)].copy()

monthly_basic = (
    output_trips
    .groupby(["UserID", "YearMonth"], as_index=False)
    .agg(
        TripCount=("TripID", "count"),
        TotalDistanceMiles=("TripDistanceMiles", "sum"),
        TotalDurationMinutes=("TripDurationMinutes", "sum"),
    )
)

monthly_plan_rows = []
for (uid, ym), g in output_trips.groupby(["UserID", "YearMonth"]):
    segment = g["UserSegment"].iloc[0]
    plan, had_tie = choose_monthly_plan(g["PlanUsedForTrip"], segment)
    monthly_plan_rows.append({
        "UserID": uid,
        "YearMonth": ym,
        "PlanUsedThisMonth": plan,
        "HadMonthlyPlanTie": int(had_tie),
        "DistinctPlansUsedThisMonth": g["PlanUsedForTrip"].nunique(),
    })

monthly_plan = pd.DataFrame(monthly_plan_rows)
monthly_usage = monthly_basic.merge(
    monthly_plan, on=["UserID", "YearMonth"], how="left")
panel = panel.merge(monthly_usage, on=["UserID", "YearMonth"], how="left")

panel["TripCount"] = panel["TripCount"].fillna(0).astype(int)
panel["TotalDistanceMiles"] = panel["TotalDistanceMiles"].fillna(0.0)
panel["TotalDurationMinutes"] = panel["TotalDurationMinutes"].fillna(0.0)
panel["HadMonthlyPlanTie"] = panel["HadMonthlyPlanTie"].fillna(0).astype(int)
panel["DistinctPlansUsedThisMonth"] = panel["DistinctPlansUsedThisMonth"].fillna(
    0).astype(int)

zero_trip = panel["TripCount"] == 0
panel.loc[zero_trip & (panel["InMarket"] == 1),
          "PlanUsedThisMonth"] = "Free Plan"
panel.loc[zero_trip & (panel["InMarket"] == 0),
          "PlanUsedThisMonth"] = "Not In Market"

print(
    f"Output-month positive-use user-months: {(panel['TripCount'] > 0).sum():,}")
print(
    f"Monthly plan ties among positive-use months: {panel['HadMonthlyPlanTie'].sum():,}")
print("\nPlanUsedThisMonth counts:")
print(panel["PlanUsedThisMonth"].value_counts(dropna=False).to_string())
print("\nTripCount summary for in-market months:")
print(panel.loc[panel["InMarket"] == 1, "TripCount"].describe().to_string())


# =============================================================================
# STEP 7: ATTACH MONTHLY PRICING
# =============================================================================

print_stage("STEP 7: Attach monthly pricing")

pricing_rows = []
for month in output_months:
    for segment in SEGMENTS_TO_KEEP:
        regime, paid_price, free_price = monthly_pricing_strings(
            month, segment)
        pricing_rows.append({
            "YearMonth": month,
            "UserSegment": segment,
            "PricingRegime_Month": regime,
            "PaidPlanName": SEGMENT_PAID_PLAN[segment],
            "PaidPlanPricingAtMonth": paid_price,
            "FreePlanPricingAtMonth": free_price,
        })

pricing_table = pd.DataFrame(pricing_rows)
panel = panel.merge(pricing_table, on=["YearMonth", "UserSegment"], how="left")

if panel["PaidPlanPricingAtMonth"].isna().any():
    raise RuntimeError("Pricing merge failed for some user-month rows.")

print("\nPricing regimes by month:")
print(
    pricing_table[["YearMonth", "PricingRegime_Month"]]
    .drop_duplicates()
    .assign(YearMonth=lambda d: d["YearMonth"].astype(str))
    .to_string(index=False)
)
print("\nUser-month rows by pricing regime:")
print(panel["PricingRegime_Month"].value_counts(
    dropna=False).sort_index().to_string())


# =============================================================================
# STEP 8: FINAL CHECKS
# =============================================================================

print_stage("STEP 8: Final checks")

duplicate_rows = panel.duplicated(["UserID", "YearMonth"]).sum()
print(f"Duplicate UserID-YearMonth rows: {duplicate_rows:,}")
if duplicate_rows:
    raise RuntimeError("Panel contains duplicate UserID-YearMonth rows.")

missing_plan = panel["PlanUsedThisMonth"].isna().sum()
print(f"Rows with missing PlanUsedThisMonth: {missing_plan:,}")
if missing_plan:
    raise RuntimeError("Panel contains missing PlanUsedThisMonth values.")

print("\nPositive-use monthly plan by segment:")
print(pd.crosstab(
    panel.loc[panel["TripCount"] > 0, "UserSegment"],
    panel.loc[panel["TripCount"] > 0, "PlanUsedThisMonth"],
).to_string())
print("\nAll user-month rows by segment:")
print(panel["UserSegment"].value_counts().to_string())
print("\nIn-market user-month rows by segment:")
print(panel.loc[panel["InMarket"] == 1,
      "UserSegment"].value_counts().to_string())
print("\nPositive-use user-month rows by segment:")
print(panel.loc[panel["TripCount"] > 0,
      "UserSegment"].value_counts().to_string())


# =============================================================================
# STEP 9: WRITE OUTPUTS
# =============================================================================

print_stage("STEP 9: Write outputs")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

keep_cols = [
    "UserID",
    "UserSegment",
    "YearMonth",
    "MonthOfYear",
    "InMarket",
    "TripCount",
    "TotalDistanceMiles",
    "TotalDurationMinutes",
    "PlanUsedThisMonth",
    "PricingRegime_Month",
    "PaidPlanName",
    "PaidPlanPricingAtMonth",
    "FreePlanPricingAtMonth",
]

out_all = period_to_str(panel[keep_cols])

all_path = OUTPUT_DIR / "user_month_all_segments.csv"
out_all.to_csv(all_path, index=False)
print(f"Wrote: {all_path.resolve()}")
print(f"Rows: {len(out_all):,}")

for segment in SEGMENTS_TO_KEEP:
    segment_stub = segment.lower()
    out_seg = out_all[out_all["UserSegment"] == segment].copy()
    seg_path = OUTPUT_DIR / f"user_month_{segment_stub}.csv"
    out_seg.to_csv(seg_path, index=False)
    print(f"Wrote: {seg_path.resolve()}  rows: {len(out_seg):,}")

    if WRITE_DASHBOARD_COPIES:
        dashboard_path = DASHBOARD_OUTPUT_DIR / \
            f"user_month_{segment_stub}.csv"
        out_seg.to_csv(dashboard_path, index=False)
        print(
            f"Wrote dashboard copy: {dashboard_path.resolve()}  rows: {len(out_seg):,}")

print("\nDone.")
