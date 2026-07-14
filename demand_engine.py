"""Demand-estimation and pricing-analysis engine for the EVIE dashboard.

This module is the service layer behind ``app.py``.  The browser sends a plain
configuration dictionary, and the public entry points return JSON-compatible
objects for validation, what-if analysis, and constrained optimization.

The pipeline is:

1. Load the segment-specific user-month file and the shared trip file.
2. Convert each positive-use user-month into a basket of observed trips.
3. Build empirical kernels from historical baskets and reprice them to compute
   expected product costs.
4. Estimate the logit/entropy demand model through share inversion.
5. Use the fitted model for in-sample validation, counterfactual pricing, or
   search-based price optimization.

The code avoids writing files and avoids external services.  Inputs are the CSV
files in this folder; outputs are returned to the dashboard API.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# File locations, default model controls, and tariff parameter names
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TRIP_FILE = BASE_DIR / "clean_trips_with_invoice_plan_and_segment.csv"
USER_MONTH_FILES = {
    "Regular": BASE_DIR / "user_month_regular.csv",
    "Student": BASE_DIR / "user_month_student.csv",
    "Access": BASE_DIR / "user_month_access.csv",
}
SEGMENT_PLANS = {
    "Regular": ["Everyday", "Free Plan"],
    "Student": ["Student", "Free Plan"],
    "Access": ["Access", "Free Plan"],
}
EPSILON = 0.1
MIN_SUPPORT = 30
LEGACY_MONTH_SUPPORT = 10
PRICING_PARAMETERS = [
    "trip_start_fee",
    "per_minute",
    "per_hour",
    "per_day",
    "included_miles",
    "extra_mile",
    "monthly_fee",
    "monthly_credit",
]
TIME_PARAMETERS = ["per_minute", "per_hour", "per_day"]
DEFAULT_MODEL_SPEC = {
    "include_intercept": True,
    "include_price": True,
    "include_count_fe": True,
    "include_plan_fe": True,
    "include_month_fe": True,
}
MODEL_SPEC_LABELS = {
    "include_intercept": "Intercept",
    "include_price": "Expected user payment",
    "include_count_fe": "Trip-count-bin fixed effects",
    "include_plan_fe": "Plan fixed effects",
    "include_month_fe": "Month-of-year fixed effects",
}
DEFAULT_KERNEL_SPEC = {
    "fallback_style": "legacy",
    "condition_segment": True,
    "condition_plan": True,
    "condition_count_bin": True,
    "condition_month_of_year": True,
    "relax_order": ["plan", "segment", "month_of_year"],
    "allow_low_support": False,
    "min_support": MIN_SUPPORT,
    "legacy_month_support": LEGACY_MONTH_SUPPORT,
}
ROUNDING_INCREMENTS = {
    "trip_start_fee": 0.05,
    "per_minute": 0.005,
    "per_hour": 0.25,
    "per_day": 1.0,
    "included_miles": 1.0,
    "extra_mile": 0.01,
    "monthly_fee": 0.25,
    "monthly_credit": 0.25,
    "time_multiplier": 0.005,
}


@dataclass(frozen=True)
class CountBin:
    """Trip-count interval used to define a product within a plan."""

    label: str
    lo: int
    hi: Optional[int]


@dataclass
class FastKernelSource:
    """Compact numeric representation of one empirical kernel source."""

    trip_counts: np.ndarray
    basket_ids: np.ndarray
    durations: np.ndarray
    distances: np.ndarray
    n_baskets: int


@dataclass
class FastForecastContext:
    """Precomputed arrays used to evaluate many pricing candidates quickly."""

    template: pd.DataFrame
    sources: List[FastKernelSource]
    source_ids: np.ndarray
    plan_is_free: np.ndarray
    utility_without_price: np.ndarray
    price_slope: np.ndarray
    month_index: np.ndarray
    month_labels: List[str]
    month_market_size: np.ndarray
    product_labels: List[str]
    kernel_details: List[dict]


# Default product definition: plan crossed with these trip-count bins.
BINS = [
    CountBin("01", 1, 1),
    CountBin("02", 2, 2),
    CountBin("03", 3, 3),
    CountBin("04", 4, 4),
    CountBin("05", 5, 5),
    CountBin("06", 6, 6),
    CountBin("07", 7, 7),
    CountBin("08", 8, 8),
    CountBin("09-12", 9, 12),
    CountBin("13-20", 13, 20),
    CountBin("21+", 21, None),
]


def default_bins() -> List[CountBin]:
    return [CountBin(item.label, item.lo, item.hi) for item in BINS]


# ---------------------------------------------------------------------------
# Basic parsing, JSON cleanup, and pricing helpers
# ---------------------------------------------------------------------------

def normalize_bin_label(lo: int, hi: Optional[int], original: Optional[str] = None) -> str:
    if original:
        return str(original).strip().replace(" ", "")
    if hi is None:
        return f"{lo}+"
    if lo == hi:
        return f"{lo:02d}"
    return f"{lo:02d}-{hi:02d}" if lo < 10 and hi < 10 else f"{lo}-{hi}"


def parse_count_bin_item(item) -> CountBin:
    if isinstance(item, dict):
        lo = int(item.get("lo", item.get("min")))
        raw_hi = item.get("hi", item.get("max"))
        hi = None if raw_hi in (None, "", "None") else int(raw_hi)
        label = item.get("label") or normalize_bin_label(lo, hi)
    else:
        label = str(item).strip()
        if not label:
            raise ValueError("Trip-count bin entries cannot be blank.")
        compact = label.replace(" ", "")
        if compact.endswith("+"):
            lo = int(compact[:-1])
            hi = None
        elif "-" in compact:
            parts = compact.split("-")
            if len(parts) != 2:
                raise ValueError(f"Invalid trip-count bin {label!r}. Use formats like 01, 09-12, or 21+.")
            lo = int(parts[0])
            hi = int(parts[1])
        else:
            lo = int(compact)
            hi = lo
        label = normalize_bin_label(lo, hi, compact)
    if lo < 1:
        raise ValueError("Trip-count bins must start at 1 or higher.")
    if hi is not None and hi < lo:
        raise ValueError(f"Invalid trip-count bin {label!r}: upper bound is below lower bound.")
    return CountBin(str(label), int(lo), None if hi is None else int(hi))


def bins_from_config(config: Optional[dict]) -> List[CountBin]:
    raw = (config or {}).get("bin_spec")
    if raw in (None, "", []):
        return default_bins()
    if isinstance(raw, str):
        items = []
        for line in raw.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                items.append(line)
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("Trip-count bins must be provided as text lines or a list.")
    if not items:
        raise ValueError("At least one trip-count bin is required.")
    if len(items) > 40:
        raise ValueError("Too many trip-count bins. Use 40 or fewer bins.")
    bins = [parse_count_bin_item(item) for item in items]
    labels = [item.label for item in bins]
    if len(set(labels)) != len(labels):
        raise ValueError("Trip-count bin labels must be unique.")
    if bins[0].lo != 1:
        raise ValueError("Trip-count bins must start at 1.")
    prev_hi = 0
    for idx, item in enumerate(bins):
        if prev_hi is None:
            raise ValueError("Only the final trip-count bin can be open-ended.")
        if item.lo <= prev_hi:
            raise ValueError(f"Trip-count bin {item.label!r} overlaps with the previous bin.")
        if item.lo > prev_hi + 1:
            raise ValueError(f"Trip-count bin {item.label!r} leaves a gap after {prev_hi}.")
        if item.hi is None:
            if idx != len(bins) - 1:
                raise ValueError("Only the final trip-count bin can be open-ended.")
            prev_hi = None
        else:
            prev_hi = item.hi
    if bins[-1].hi is not None:
        raise ValueError("The final trip-count bin must be open-ended, e.g. 21+.")
    return bins


def json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, (np.floating,)):
        if not np.isfinite(value):
            return None
        return float(value)
    if value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return str(value)
    if isinstance(value, (pd.Period,)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def normal_p_value(z: float) -> float:
    if not np.isfinite(z):
        return float("nan")
    return math.erfc(abs(float(z)) / math.sqrt(2.0))


def month_add(year_month: str, n: int) -> str:
    period = pd.Period(year_month, freq="M") + n
    return str(period)


def parse_pricing(value: object) -> Optional[dict]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.startswith("UNKNOWN"):
        return None
    return json.loads(value)


def plan_pricing_from_row(row: pd.Series, plan: str) -> Optional[dict]:
    if plan == "Free Plan":
        return parse_pricing(row["FreePlanPricingAtMonth"])
    return parse_pricing(row["PaidPlanPricingAtMonth"])


def price_trip_array(durations: np.ndarray, distances: np.ndarray, params: dict) -> np.ndarray:
    """Price an array of trips under one plan's tariff parameters."""
    durations = np.asarray(durations, dtype=float)
    distances = np.asarray(distances, dtype=float)
    time_charge = np.minimum.reduce(
        [
            float(params["per_minute"]) * durations,
            float(params["per_hour"]) * np.ceil(durations / 60.0),
            float(params["per_day"]) * np.ceil(durations / 1440.0),
        ]
    )
    mileage_charge = float(params["extra_mile"]) * np.maximum(
        0.0, distances - float(params["included_miles"])
    )
    return float(params["trip_start_fee"]) + time_charge + mileage_charge


def price_basket(durations: Iterable[float], distances: Iterable[float], params: dict) -> Tuple[float, float]:
    """Return trip revenue and monthly out-of-pocket payment for one basket."""
    charges = price_trip_array(np.asarray(list(durations), dtype=float), np.asarray(list(distances), dtype=float), params)
    trip_revenue = float(np.sum(charges))
    user_payment = float(params["monthly_fee"]) + max(0.0, trip_revenue - float(params["monthly_credit"]))
    return trip_revenue, user_payment


@lru_cache(maxsize=6)
def read_inputs(segment: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load and normalize the user-month and trip data for one segment."""
    if segment not in USER_MONTH_FILES:
        raise ValueError(f"Unknown segment {segment!r}")
    required = [TRIP_FILE, USER_MONTH_FILES[segment]]
    missing = [str(path.name) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Dashboard data files must be in the Dashboard folder. "
            f"Missing: {', '.join(missing)}"
        )
    user_cols = [
        "UserID",
        "UserSegment",
        "YearMonth",
        "MonthOfYear",
        "InMarket",
        "TripCount",
        "PlanUsedThisMonth",
        "PricingRegime_Month",
        "PaidPlanName",
        "PaidPlanPricingAtMonth",
        "FreePlanPricingAtMonth",
    ]
    trip_cols = [
        "TripID",
        "UserID",
        "UserSegment",
        "YearMonth",
        "MonthOfYear",
        "TripDurationMinutes",
        "TripDistanceMiles",
        "PlanUsedForTrip",
    ]
    user_month = pd.read_csv(USER_MONTH_FILES[segment], usecols=user_cols)
    trips = pd.read_csv(TRIP_FILE, usecols=trip_cols)
    user_month = user_month[user_month["UserSegment"].eq(segment)].copy()
    user_month["TripCount"] = pd.to_numeric(user_month["TripCount"], errors="coerce").fillna(0).astype(int)
    user_month["InMarket"] = pd.to_numeric(user_month["InMarket"], errors="coerce").fillna(0).astype(int)
    user_month["MonthOfYear"] = pd.to_numeric(user_month["MonthOfYear"], errors="coerce").astype(int)
    trips["MonthOfYear"] = pd.to_numeric(trips["MonthOfYear"], errors="coerce").astype(int)
    trips["TripDurationMinutes"] = pd.to_numeric(trips["TripDurationMinutes"], errors="coerce")
    trips["TripDistanceMiles"] = pd.to_numeric(trips["TripDistanceMiles"], errors="coerce")
    trips = trips[trips["TripDurationMinutes"].notna() & trips["TripDistanceMiles"].notna()].copy()
    cohort_keys = user_month[["UserID", "YearMonth"]].drop_duplicates()
    trips = trips.merge(cohort_keys, on=["UserID", "YearMonth"], how="inner")
    return user_month, trips


@lru_cache(maxsize=1)
def read_all_inputs() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load all user-month files plus the shared trip file for pooled kernels."""
    user_month_parts = []
    trip_parts = []
    for segment in USER_MONTH_FILES:
        user_month, trips = read_inputs(segment)
        user_month_parts.append(user_month)
        trip_parts.append(trips)
    return (
        pd.concat(user_month_parts, ignore_index=True, sort=False),
        pd.concat(trip_parts, ignore_index=True, sort=False).drop_duplicates("TripID"),
    )


def data_options() -> dict:
    """Return data-driven choices used to initialize the dashboard controls."""
    segments = []
    for segment in USER_MONTH_FILES:
        user_month, _trips = read_inputs(segment)
        months = sorted(user_month["YearMonth"].astype(str).unique())
        latest = months[-1]
        complete_months = months[:-1] if months else []
        segments.append(
            {
                "segment": segment,
                "plans": SEGMENT_PLANS[segment],
                "first_month": months[0],
                "latest_month": latest,
                "latest_complete_month": complete_months[-1] if complete_months else latest,
                "default_train_start": months[0],
                "default_train_end": "2025-07" if "2025-07" in months else (complete_months[-1] if complete_months else latest),
                "default_test_start": "2025-08" if "2025-08" in months else (complete_months[-1] if complete_months else latest),
                "default_test_end": complete_months[-1] if complete_months else latest,
            }
        )
    default_segment = next((row for row in segments if row["segment"] == "Regular"), segments[0])
    defaults = {
        "train_start": default_segment["default_train_start"],
        "train_end": default_segment["default_train_end"],
        "test_start": default_segment["default_test_start"],
        "test_end": default_segment["default_test_end"],
        "what_if_train_end": default_segment["latest_complete_month"],
        "forecast_start": month_add(default_segment["latest_complete_month"], 1),
    }
    return {"segments": segments, "bins": [b.label for b in BINS], "defaults": defaults}


def filter_latest(user_month: pd.DataFrame, trips: pd.DataFrame, drop_latest: bool) -> Tuple[pd.DataFrame, pd.DataFrame, str]:
    if not drop_latest:
        return user_month.copy(), trips.copy(), ""
    latest = max(user_month["YearMonth"].astype(str).unique())
    return (
        user_month[user_month["YearMonth"].ne(latest)].copy(),
        trips[trips["YearMonth"].ne(latest)].copy(),
        latest,
    )


def choose_validation_months(
    user_month: pd.DataFrame,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> Tuple[List[str], List[str]]:
    months = sorted(user_month["YearMonth"].astype(str).unique())
    train = [m for m in months if train_start <= m <= train_end]
    test = [m for m in months if test_start <= m <= test_end]
    if not train:
        raise ValueError("No training months in the selected range.")
    if not test:
        raise ValueError("No testing months in the selected range.")
    overlap = sorted(set(train).intersection(test))
    if overlap:
        raise ValueError(f"Training and testing months overlap: {overlap[:5]}")
    return train, test


def choose_train_months(user_month: pd.DataFrame, train_start: str, train_end: str) -> List[str]:
    months = sorted(user_month["YearMonth"].astype(str).unique())
    train = [m for m in months if train_start <= m <= train_end]
    if not train:
        raise ValueError("No training months in the selected range.")
    return train


# ---------------------------------------------------------------------------
# Baskets, observed revenue, and empirical kernel construction
# ---------------------------------------------------------------------------

def split_data(user_month: pd.DataFrame, trips: pd.DataFrame, train_months: List[str], extra_months: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    months = set(train_months + extra_months)
    window_um = user_month[user_month["YearMonth"].isin(months)].copy()
    window_trips = trips[trips["YearMonth"].isin(months)].copy()
    train_um = user_month[user_month["YearMonth"].isin(train_months)].copy()
    train_trips = trips[trips["YearMonth"].isin(train_months)].copy()
    return window_um, window_trips, train_um, train_trips


def assign_count_bin_series(counts: pd.Series, bins: Optional[List[CountBin]] = None) -> pd.Series:
    bins = default_bins() if bins is None else bins
    labels = pd.Series(pd.NA, index=counts.index, dtype="object")
    for count_bin in bins:
        mask = counts.ge(count_bin.lo)
        if count_bin.hi is not None:
            mask &= counts.le(count_bin.hi)
        labels.loc[mask] = count_bin.label
    return labels


def product_name(plan: str, count_bin: str) -> str:
    return f"{plan} [{count_bin}]"


def bin_sort_value(count_bin: str, bins: Optional[List[CountBin]] = None) -> int:
    bins = default_bins() if bins is None else bins
    return {b.label: i for i, b in enumerate(bins, start=1)}.get(str(count_bin), 999)


def build_month_pricing(user_month: pd.DataFrame, plans: List[str]) -> Dict[Tuple[str, str], dict]:
    out: Dict[Tuple[str, str], dict] = {}
    rows = user_month.sort_values("YearMonth").drop_duplicates("YearMonth")
    for _, row in rows.iterrows():
        for plan in plans:
            params = plan_pricing_from_row(row, plan)
            if params is None:
                raise ValueError(f"Missing pricing JSON for {row['YearMonth']}, {plan}")
            out[(str(row["YearMonth"]), plan)] = params
    return out


def build_baskets(user_month: pd.DataFrame, trips: pd.DataFrame, plans: Optional[List[str]] = None, bins: Optional[List[CountBin]] = None) -> pd.DataFrame:
    """Build one row per positive-use user-month with the month trip list.

    In the dashboard terminology, this row is a basket.  It is the historical
    object used by the kernel: the user's plan, segment, month, trip count, and
    the actual duration/distance vectors for all trips in that month.
    """
    bins = default_bins() if bins is None else bins
    in_market = user_month[user_month["InMarket"].eq(1)].copy()
    positive = in_market[in_market["TripCount"].gt(0)].copy()
    if plans is not None:
        positive = positive[positive["PlanUsedThisMonth"].isin(plans)].copy()
    trips = trips[trips["YearMonth"].isin(set(user_month["YearMonth"].astype(str).unique()))].copy()
    grouped = (
        trips.groupby(["UserID", "YearMonth"], sort=False)
        .agg(
            matched_trip_rows=("TripID", "size"),
            durations=("TripDurationMinutes", list),
            distances=("TripDistanceMiles", list),
        )
        .reset_index()
    )
    baskets = positive.merge(grouped, on=["UserID", "YearMonth"], how="left")
    baskets["matched_trip_rows"] = baskets["matched_trip_rows"].fillna(0).astype(int)
    baskets = baskets[baskets["matched_trip_rows"].gt(0)].copy()
    baskets["count_bin"] = assign_count_bin_series(baskets["TripCount"], bins)
    baskets = baskets[baskets["count_bin"].notna()].copy()
    return baskets


def compute_actual_revenue(baskets: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute observed trip revenue and user-payment revenue from baskets."""
    rows = []
    for row in baskets.itertuples(index=False):
        params = parse_pricing(row.FreePlanPricingAtMonth) if row.PlanUsedThisMonth == "Free Plan" else parse_pricing(row.PaidPlanPricingAtMonth)
        if params is None:
            continue
        trip_revenue, user_payment = price_basket(row.durations, row.distances, params)
        rows.append(
            {
                "YearMonth": str(row.YearMonth),
                "UserID": row.UserID,
                "plan": row.PlanUsedThisMonth,
                "TripCount": int(row.TripCount),
                "actual_trip_revenue": trip_revenue,
                "actual_user_payment_revenue": user_payment,
            }
        )
    basket_revenue = pd.DataFrame(rows)
    monthly = (
        basket_revenue.groupby("YearMonth", as_index=False)
        .agg(
            actual_trip_revenue=("actual_trip_revenue", "sum"),
            actual_user_payment_revenue=("actual_user_payment_revenue", "sum"),
            actual_positive_users=("UserID", "nunique"),
            actual_total_trips=("TripCount", "sum"),
        )
        if not basket_revenue.empty
        else pd.DataFrame(columns=["YearMonth", "actual_trip_revenue", "actual_user_payment_revenue", "actual_positive_users", "actual_total_trips"])
    )
    if not basket_revenue.empty:
        plan_trips = (
            basket_revenue.assign(plan_group=np.where(basket_revenue["plan"].eq("Free Plan"), "free_plan", "paid_plan"))
            .groupby(["YearMonth", "plan_group"], as_index=False)["TripCount"]
            .sum()
            .pivot(index="YearMonth", columns="plan_group", values="TripCount")
            .reset_index()
            .rename(columns={"free_plan": "actual_free_plan_trips", "paid_plan": "actual_paid_plan_trips"})
        )
        monthly = monthly.merge(plan_trips, on="YearMonth", how="left")
    for col in ["actual_free_plan_trips", "actual_paid_plan_trips"]:
        if col not in monthly:
            monthly[col] = 0.0
        monthly[col] = monthly[col].fillna(0.0)
    return monthly, basket_revenue


def kernel_spec_from_config(config: dict) -> dict:
    """Normalize kernel controls submitted by the dashboard."""
    raw = config.get("kernel_spec") or {}
    spec = dict(DEFAULT_KERNEL_SPEC)
    if str(raw.get("fallback_style", "")).lower() in {"legacy", "ranked"}:
        spec["fallback_style"] = str(raw["fallback_style"]).lower()
    for key in [
        "condition_segment",
        "condition_plan",
        "condition_month_of_year",
        "allow_low_support",
    ]:
        if key in raw:
            spec[key] = bool(raw[key])
    spec["condition_count_bin"] = True
    if "min_support" in raw:
        spec["min_support"] = max(1, int(float(raw["min_support"])))
    for key in ["legacy_month_support", "moy_min_support"]:
        if key in raw:
            spec["legacy_month_support"] = max(1, int(float(raw[key])))
    raw_order = raw.get("relax_order")
    if isinstance(raw_order, list):
        allowed = ["segment", "plan", "month_of_year"]
        cleaned = []
        for item in raw_order:
            item = str(item)
            if item in allowed and item not in cleaned:
                cleaned.append(item)
        for item in allowed:
            if item not in cleaned:
                cleaned.append(item)
        spec["relax_order"] = cleaned
    return spec


def kernel_condition_label(use_segment: bool, use_plan: bool, use_count: bool, use_moy: bool, low_support: bool = False) -> str:
    parts = []
    if use_segment:
        parts.append("segment")
    if use_moy:
        parts.append("moy")
    if use_plan:
        parts.append("plan" if use_segment else "plan_group")
    if use_count:
        parts.append("count_bin")
    if not parts:
        parts.append("all_positive")
    if low_support:
        parts.append("low_support")
    return "_".join(parts)


def kernel_condition_description(use_segment: bool, use_plan: bool, use_count: bool, use_moy: bool) -> str:
    parts = []
    if use_segment:
        parts.append("same segment")
    if use_moy:
        parts.append("same month-of-year")
    if use_plan:
        parts.append("same plan" if use_segment else "same paid/free plan group")
    if use_count:
        parts.append("same count bin")
    return ", ".join(parts) if parts else "all positive-use baskets"


def kernel_condition_candidates(kernel_spec: dict) -> List[Tuple[bool, bool, bool, bool]]:
    """Return fallback kernel conditions in the order they should be tried."""
    candidates = []
    seen = set()

    def add(item: Tuple[bool, bool, bool, bool]) -> None:
        if item not in seen:
            seen.add(item)
            candidates.append(item)

    if str(kernel_spec.get("fallback_style", "legacy")).lower() == "legacy":
        use_segment = bool(kernel_spec.get("condition_segment", True))
        use_plan = bool(kernel_spec.get("condition_plan", True))
        use_count = True
        use_moy = bool(kernel_spec.get("condition_month_of_year", True))

        def add_plan_month_relaxations(count_flag: bool) -> None:
            add((use_segment, use_plan, count_flag, use_moy))
            if use_plan:
                add((use_segment, False, count_flag, use_moy))
            if use_moy:
                add((use_segment, use_plan, count_flag, False))
            if use_plan and use_moy:
                add((use_segment, False, count_flag, False))

        add_plan_month_relaxations(use_count)
        add((use_segment, False, True, False))
        return candidates

    state = {
        "segment": bool(kernel_spec.get("condition_segment", True)),
        "plan": bool(kernel_spec.get("condition_plan", True)),
        "count_bin": True,
        "month_of_year": bool(kernel_spec.get("condition_month_of_year", True)),
    }
    raw_order = kernel_spec.get("relax_order", DEFAULT_KERNEL_SPEC["relax_order"])
    relax_order = []
    for item in raw_order:
        item = str(item)
        if item in state and item not in relax_order:
            relax_order.append(item)
    for item in ["plan", "segment", "month_of_year"]:
        if item not in relax_order:
            relax_order.append(item)

    def current_tuple() -> Tuple[bool, bool, bool, bool]:
        return (state["segment"], state["plan"], state["count_bin"], state["month_of_year"])

    add(current_tuple())
    for dimension in relax_order:
        if state.get(dimension, False):
            state[dimension] = False
            add(current_tuple())
    add((False, False, True, False))
    return candidates


def filter_kernel_source(
    baskets: pd.DataFrame,
    segment: str,
    plan: str,
    count_bin: str,
    month: str,
    use_segment: bool,
    use_plan: bool,
    use_count: bool,
    use_moy: bool,
) -> pd.DataFrame:
    source = baskets
    if use_segment and "UserSegment" in source:
        source = source[source["UserSegment"].eq(segment)]
    if use_plan:
        if use_segment or plan == "Free Plan":
            source = source[source["PlanUsedThisMonth"].eq(plan)]
        else:
            source = source[source["PlanUsedThisMonth"].ne("Free Plan")]
    if use_count:
        source = source[source["count_bin"].eq(count_bin)]
    if use_moy:
        source = source[source["MonthOfYear"].eq(int(str(month)[-2:]))]
    return source


def choose_kernel_source(
    baskets: pd.DataFrame,
    segment: str,
    plan: str,
    count_bin: str,
    month: str,
    kernel_spec: Optional[dict] = None,
) -> Tuple[pd.DataFrame, str, Tuple[bool, bool, bool, bool]]:
    """Select the historical basket rows used as the empirical kernel source."""
    spec = dict(DEFAULT_KERNEL_SPEC if kernel_spec is None else kernel_spec)
    candidates = kernel_condition_candidates(spec)
    supported = []
    for use_segment, use_plan, use_count, use_moy in candidates:
        source = filter_kernel_source(baskets, segment, plan, count_bin, month, use_segment, use_plan, use_count, use_moy)
        if str(spec.get("fallback_style", "legacy")).lower() == "legacy" and use_moy:
            threshold = int(spec.get("legacy_month_support", LEGACY_MONTH_SUPPORT))
        else:
            threshold = int(spec.get("min_support", MIN_SUPPORT))
        label = kernel_condition_label(use_segment, use_plan, use_count, use_moy)
        if len(source) >= threshold:
            return source, label, (use_segment, use_plan, use_count, use_moy)
        supported.append((source, label, (use_segment, use_plan, use_count, use_moy)))
    nonempty = [(source, label, flags) for source, label, flags in supported if len(source) > 0]
    if nonempty:
        source, label, flags = max(nonempty, key=lambda item: len(item[0]))
        return source, kernel_condition_label(*flags, low_support=True), flags
    empty_same_count = filter_kernel_source(baskets, segment, plan, count_bin, month, False, False, True, False)
    return empty_same_count, "count_bin_empty", (False, False, True, False)


def price_source(source_df: pd.DataFrame, params: dict) -> Tuple[float, float, float]:
    if source_df.empty:
        return 0.0, 0.0, 0.0
    return fast_price_source(fast_source_from_baskets(source_df), params)


def pricing_signature(params: dict) -> Tuple[float, ...]:
    return tuple(
        float(params[key])
        for key in [
            "trip_start_fee",
            "per_minute",
            "per_hour",
            "per_day",
            "included_miles",
            "extra_mile",
            "monthly_fee",
            "monthly_credit",
        ]
    )


def kernel_cache_key(
    segment: str,
    plan: str,
    count_bin: str,
    month: str,
    condition_flags: Tuple[bool, bool, bool, bool],
) -> Tuple[object, ...]:
    use_segment, use_plan, use_count, use_moy = condition_flags
    if use_plan:
        plan_key = plan if use_segment or plan == "Free Plan" else "__paid_plans__"
    else:
        plan_key = "__all_plans__"
    return (
        segment if use_segment else "__all_segments__",
        plan_key,
        count_bin if use_count else "__all_counts__",
        int(str(month)[-2:]) if use_moy else "__all_months__",
    )


def flatten_list_column(source_df: pd.DataFrame, column: str) -> np.ndarray:
    values = []
    if column not in source_df:
        return np.asarray(values, dtype=float)
    for item in source_df[column]:
        if isinstance(item, (list, tuple, np.ndarray)):
            values.extend([float(v) for v in item if pd.notna(v)])
    return np.asarray(values, dtype=float)


def fixed_histogram(values: np.ndarray, edges: List[float], suffix: str = "") -> List[dict]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    rows = []
    if len(values) == 0:
        return rows
    counts, _edges = np.histogram(values, bins=np.asarray(edges, dtype=float))
    for idx, count in enumerate(counts):
        lo = edges[idx]
        hi = edges[idx + 1]
        if np.isinf(hi):
            label = f"{lo:g}+{suffix}"
        else:
            label = f"{lo:g}-{hi:g}{suffix}"
        rows.append({"label": label, "count": int(count)})
    return rows


def trip_count_histogram(source_df: pd.DataFrame) -> List[dict]:
    if source_df.empty or "TripCount" not in source_df:
        return []
    counts = source_df["TripCount"].dropna().astype(int)
    if counts.empty:
        return []
    grouped = counts.value_counts().sort_index()
    if len(grouped) <= 25:
        return [{"label": str(int(k)), "count": int(v)} for k, v in grouped.items()]
    max_count = int(counts.max())
    edges = list(range(1, max_count + 6, 5))
    if edges[-1] <= max_count:
        edges.append(max_count + 1)
    hist, edge_arr = np.histogram(counts.to_numpy(dtype=float), bins=np.asarray(edges, dtype=float))
    return [
        {"label": f"{int(edge_arr[i])}-{int(edge_arr[i + 1] - 1)}", "count": int(hist[i])}
        for i in range(len(hist))
        if int(hist[i]) > 0
    ]


def summary_stats(values: np.ndarray) -> dict:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return {"n": 0, "mean": None, "p25": None, "median": None, "p75": None}
    return {
        "n": int(len(values)),
        "mean": float(np.mean(values)),
        "p25": float(np.percentile(values, 25)),
        "median": float(np.percentile(values, 50)),
        "p75": float(np.percentile(values, 75)),
    }


def compact_month_summary(values: pd.Series) -> str:
    months = sorted({str(v) for v in values.dropna().astype(str).tolist()})
    if not months:
        return ""
    if len(months) <= 5:
        return ", ".join(months)
    return f"{months[0]} to {months[-1]} ({len(months)} months)"


def compact_month_of_year_summary(values: pd.Series) -> str:
    months = sorted({int(v) for v in values.dropna().tolist()})
    if not months:
        return ""
    return ", ".join(f"{m:02d}" for m in months)


def kernel_source_payload(source_df: pd.DataFrame) -> dict:
    """Summarize the empirical kernel source for dashboard diagnostics."""
    durations = flatten_list_column(source_df, "durations")
    distances = flatten_list_column(source_df, "distances")
    trip_counts = source_df["TripCount"].dropna().astype(float).to_numpy(dtype=float) if "TripCount" in source_df else np.asarray([], dtype=float)
    return {
        "source_baskets": int(len(source_df)),
        "source_trip_rows": int(source_df["matched_trip_rows"].sum()) if "matched_trip_rows" in source_df else int(len(durations)),
        "source_calendar_months": compact_month_summary(source_df["YearMonth"]) if "YearMonth" in source_df else "",
        "source_months_of_year": compact_month_of_year_summary(source_df["MonthOfYear"]) if "MonthOfYear" in source_df else "",
        "trip_count_hist": trip_count_histogram(source_df),
        "duration_hist": fixed_histogram(durations, [0, 15, 30, 60, 120, 240, 480, 720, 1440, 2880, np.inf], " min"),
        "distance_hist": fixed_histogram(distances, [0, 1, 3, 5, 10, 20, 50, 100, 200, np.inf], " mi"),
        "trip_count_summary": summary_stats(trip_counts),
        "duration_summary": summary_stats(durations),
        "distance_summary": summary_stats(distances),
    }


def kernel_detail_row(
    month: str,
    product: str,
    plan: str,
    count_bin: str,
    source_label: str,
    source_df: pd.DataFrame,
    condition_flags: Tuple[bool, bool, bool, bool],
    source_payload: Optional[dict] = None,
) -> dict:
    use_segment, use_plan, use_count, use_moy = condition_flags
    payload = kernel_source_payload(source_df) if source_payload is None else dict(source_payload)
    return {
        "YearMonth": month,
        "product": product,
        "plan": plan,
        "count_bin": count_bin,
        "kernel_source": source_label,
        "condition_description": kernel_condition_description(use_segment, use_plan, use_count, use_moy),
        **payload,
    }


def expected_costs(
    baskets: pd.DataFrame,
    segment: str,
    month_pricing: Dict[Tuple[str, str], dict],
    months: List[str],
    plans: List[str],
    kernel_spec: Optional[dict] = None,
    bins: Optional[List[CountBin]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Compute expected product costs by repricing empirical kernel baskets."""
    bins = default_bins() if bins is None else bins
    rows = []
    diag = []
    source_cache: Dict[Tuple[object, ...], FastKernelSource] = {}
    payload_cache: Dict[Tuple[object, ...], dict] = {}
    value_cache: Dict[Tuple[Tuple[object, ...], Tuple[float, ...]], Tuple[float, float, float]] = {}
    for month in months:
        for plan in plans:
            params = month_pricing[(month, plan)]
            params_key = pricing_signature(params)
            for count_bin in [b.label for b in bins]:
                product = product_name(plan, count_bin)
                source, source_label, condition_flags = choose_kernel_source(baskets, segment, plan, count_bin, month, kernel_spec)
                source_key = kernel_cache_key(segment, plan, count_bin, month, condition_flags)
                if source_key not in source_cache:
                    source_cache[source_key] = fast_source_from_baskets(source)
                    payload_cache[source_key] = kernel_source_payload(source)
                cache_key = (source_key, params_key)
                if cache_key not in value_cache:
                    value_cache[cache_key] = fast_price_source(source_cache[source_key], params)
                trip_count, trip_rev, user_pay = value_cache[cache_key]
                payload = payload_cache[source_key]
                rows.append(
                    {
                        "YearMonth": month,
                        "plan": plan,
                        "count_bin": count_bin,
                        "product": product,
                        "expected_trip_count": trip_count,
                        "expected_trip_revenue": trip_rev,
                        "expected_user_payment": user_pay,
                        "kernel_source": source_label,
                        "source_baskets": payload["source_baskets"],
                        "source_trip_rows": payload["source_trip_rows"],
                    }
                )
                diag.append(kernel_detail_row(month, product, plan, count_bin, source_label, source, condition_flags, payload))
    return pd.DataFrame(rows), pd.DataFrame(diag)


# ---------------------------------------------------------------------------
# Share inversion, model specification, and fitted-demand prediction
# ---------------------------------------------------------------------------

def build_share_panel(
    user_month: pd.DataFrame,
    costs: pd.DataFrame,
    plans: List[str],
    months: Optional[List[str]] = None,
    market_sizes: Optional[Dict[str, float]] = None,
    bins: Optional[List[CountBin]] = None,
) -> pd.DataFrame:
    """Create the product-month panel used for logit share inversion."""
    bins = default_bins() if bins is None else bins
    if months is None:
        months = sorted(user_month["YearMonth"].astype(str).unique())
    in_market = user_month[user_month["InMarket"].eq(1)].copy()
    in_market = in_market[in_market["YearMonth"].isin(months)].copy()
    positive = in_market[in_market["TripCount"].gt(0) & in_market["PlanUsedThisMonth"].isin(plans)].copy()
    positive["count_bin"] = assign_count_bin_series(positive["TripCount"], bins)
    positive = positive[positive["count_bin"].notna()].copy()
    positive["product"] = [product_name(p, b) for p, b in zip(positive["PlanUsedThisMonth"], positive["count_bin"])]
    counts = positive.groupby(["YearMonth", "product"], as_index=False).size().rename(columns={"size": "observed_count"})
    if market_sizes is None:
        market_df = (
            in_market.groupby("YearMonth", as_index=False)
            .agg(
                market_size=("UserID", "nunique"),
                outside_count=("TripCount", lambda x: int((x == 0).sum())),
            )
        )
    else:
        market_df = pd.DataFrame(
            {
                "YearMonth": months,
                "market_size": [float(market_sizes[m]) for m in months],
                "outside_count": [np.nan for _m in months],
            }
        )
    product_rows = []
    for month in months:
        month_of_year = int(str(month)[-2:])
        for plan in plans:
            for count_bin in [b.label for b in bins]:
                product_rows.append(
                    {
                        "YearMonth": month,
                        "MonthOfYear": month_of_year,
                        "plan": plan,
                        "count_bin": count_bin,
                        "product": product_name(plan, count_bin),
                    }
                )
    panel = pd.DataFrame(product_rows)
    panel = panel.merge(counts, on=["YearMonth", "product"], how="left")
    panel["observed_count"] = panel["observed_count"].fillna(0.0)
    panel = panel.merge(market_df, on="YearMonth", how="left")
    panel["n_inside_products"] = len(plans) * len(bins)
    panel["observed_share"] = panel["observed_count"] / panel["market_size"]
    panel["outside_share"] = panel["outside_count"] / panel["market_size"]
    if panel["outside_count"].notna().all():
        panel["delta"] = np.log(panel["observed_count"] + EPSILON) - np.log(panel["outside_count"] + EPSILON)
    else:
        panel["delta"] = np.nan
    panel = panel.merge(
        costs[
            [
                "YearMonth",
                "product",
                "expected_trip_count",
                "expected_trip_revenue",
                "expected_user_payment",
                "kernel_source",
                "source_baskets",
                "source_trip_rows",
            ]
        ],
        on=["YearMonth", "product"],
        how="left",
    )
    if panel["expected_user_payment"].isna().any():
        missing = panel[panel["expected_user_payment"].isna()][["YearMonth", "product"]].head().to_dict("records")
        raise ValueError(f"Missing expected payments: {missing}")
    return panel


def model_spec_from_config(config: dict, require_price: bool = True) -> dict:
    """Normalize model specification controls from the dashboard."""
    raw = config.get("model_spec") or {}
    spec = dict(DEFAULT_MODEL_SPEC)
    for key in DEFAULT_MODEL_SPEC:
        if key in raw:
            spec[key] = bool(raw[key])
    if require_price:
        spec["include_price"] = True
    return spec


def coefficient_value(coef: pd.DataFrame, variable: str) -> Tuple[float, float]:
    if coef.empty or "variable" not in coef:
        return float("nan"), float("nan")
    row = coef[coef["variable"].eq(variable)]
    if row.empty:
        return float("nan"), float("nan")
    item = row.iloc[0]
    return float(item.get("coefficient", float("nan"))), float(item.get("p_value_normal", float("nan")))


def model_spec_for_dashboard(spec: dict, plans: List[str]) -> dict:
    terms = [label for key, label in MODEL_SPEC_LABELS.items() if spec.get(key, False)]
    references = {
        "outside_option": "No use / 0 trips has utility normalized to 0",
    }
    if spec.get("include_count_fe", False):
        references["count_bin"] = "01"
    if spec.get("include_plan_fe", False):
        references["plan"] = sorted(plans)[0]
    if spec.get("include_month_fe", False):
        references["month_of_year"] = "01"
    return {
        **spec,
        "epsilon": EPSILON,
        "terms": terms,
        "references": references,
        "formula": "delta_jt = log(q_jt + epsilon) - log(q_0t + epsilon)",
    }


def binning_for_dashboard(bins: Optional[List[CountBin]] = None) -> List[dict]:
    bins = default_bins() if bins is None else bins
    rows = []
    for count_bin in bins:
        if count_bin.hi is None:
            trip_range = f"{count_bin.lo}+"
        elif count_bin.lo == count_bin.hi:
            trip_range = str(count_bin.lo)
        else:
            trip_range = f"{count_bin.lo}-{count_bin.hi}"
        rows.append({"label": count_bin.label, "trip_range": trip_range})
    return rows


def kernel_rule_for_dashboard(kernel_spec: Optional[dict] = None) -> dict:
    spec = dict(DEFAULT_KERNEL_SPEC if kernel_spec is None else kernel_spec)
    candidates = kernel_condition_candidates(spec)
    labels = {
        "segment": "segment",
        "plan": "plan",
        "month_of_year": "month-of-year",
        "count_bin": "count bin",
    }
    return {
        "description": "Expected product costs are computed by repricing empirical training baskets selected by the fallback rule below.",
        "min_support": int(spec.get("min_support", MIN_SUPPORT)),
        "condition_segment": bool(spec.get("condition_segment", True)),
        "condition_plan": bool(spec.get("condition_plan", True)),
        "condition_count_bin": bool(spec.get("condition_count_bin", True)),
        "condition_month_of_year": bool(spec.get("condition_month_of_year", True)),
        "relax_order": list(spec.get("relax_order", DEFAULT_KERNEL_SPEC["relax_order"])),
        "relax_order_labels": [labels.get(item, item) for item in spec.get("relax_order", DEFAULT_KERNEL_SPEC["relax_order"])],
        "allow_low_support": bool(spec.get("allow_low_support", False)),
        "fallback_order": [
            kernel_condition_description(use_segment, use_plan, use_count, use_moy)
            for use_segment, use_plan, use_count, use_moy in candidates
        ],
    }


def build_design(panel: pd.DataFrame, model_spec: Optional[dict] = None, include_price: Optional[bool] = None, train_months: Optional[List[str]] = None) -> pd.DataFrame:
    """Build the regression design matrix for the deterministic utility."""
    spec = dict(DEFAULT_MODEL_SPEC if model_spec is None else model_spec)
    if include_price is not None:
        spec["include_price"] = bool(include_price)
    pieces = []
    if spec.get("include_intercept", True):
        pieces.append(pd.Series(1.0, index=panel.index, name="Intercept"))
    if spec.get("include_price", True):
        expected_payment = panel["expected_user_payment"].astype(float)
        pieces.append(expected_payment.rename("expected_user_payment"))
    if spec.get("include_count_fe", True):
        pieces.append(pd.get_dummies(panel["count_bin"].astype(str), prefix="count", drop_first=True, dtype=float))
    if spec.get("include_plan_fe", True):
        pieces.append(pd.get_dummies(panel["plan"].astype(str), prefix="plan", drop_first=True, dtype=float))
    if spec.get("include_month_fe", True):
        pieces.append(pd.get_dummies(panel["MonthOfYear"].astype(str).str.zfill(2), prefix="month", drop_first=True, dtype=float))
    if not pieces:
        return pd.DataFrame(index=panel.index)
    return pd.concat(pieces, axis=1)


def fit_wls_cluster(df: pd.DataFrame, y_col: str, x_cols: List[str], weight_col: str, cluster_col: str) -> Tuple[pd.DataFrame, np.ndarray, float]:
    """Fit weighted least squares with month-clustered standard errors."""
    y = df[y_col].to_numpy(dtype=float)
    X = df[x_cols].to_numpy(dtype=float)
    w = df[weight_col].to_numpy(dtype=float)
    sqrt_w = np.sqrt(np.maximum(w, 0.0))
    Xw = X * sqrt_w[:, None]
    yw = y * sqrt_w
    xtx = Xw.T @ Xw
    xtx_inv = np.linalg.pinv(xtx)
    beta = xtx_inv @ (Xw.T @ yw)
    fitted = X @ beta
    residual = y - fitted
    ybar = np.average(y, weights=w)
    sse = float(np.sum(w * residual**2))
    sst = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1.0 - sse / sst if sst > 0 else float("nan")

    meat = np.zeros((len(x_cols), len(x_cols)))
    clusters = df[cluster_col].astype(str).to_numpy()
    for cluster in np.unique(clusters):
        mask = clusters == cluster
        Xg = X[mask]
        wg = w[mask]
        eg = residual[mask]
        score = Xg.T @ (wg * eg)
        meat += np.outer(score, score)
    n = len(df)
    k = len(x_cols)
    g = len(np.unique(clusters))
    scale = (g / max(g - 1, 1)) * ((n - 1) / max(n - k, 1)) if g > 1 else 1.0
    cov = scale * xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    coef = pd.DataFrame({"variable": x_cols, "coefficient": beta, "cluster_se": se})
    coef["z_stat"] = coef["coefficient"] / coef["cluster_se"].replace(0, np.nan)
    coef["p_value_normal"] = coef["z_stat"].apply(normal_p_value)
    return coef, fitted, r2


def estimate_predict(panel: pd.DataFrame, train_months: List[str], include_price: bool = True, model_spec: Optional[dict] = None) -> Tuple[pd.DataFrame, pd.DataFrame, float]:
    """Estimate utility parameters and convert fitted utilities to shares."""
    model_df = panel.copy()
    model_df["split"] = np.where(model_df["YearMonth"].isin(train_months), "train", "test")
    X = build_design(model_df, model_spec=model_spec, include_price=include_price, train_months=train_months)
    if X.shape[1] == 0:
        raise ValueError("The selected model has no regressors. Include price, an intercept, or at least one fixed effect.")
    fit_df = pd.concat([model_df[["delta", "market_size", "YearMonth", "split"]], X], axis=1)
    train = fit_df[fit_df["split"].eq("train")].copy()
    coef, _fitted, train_r2 = fit_wls_cluster(train, "delta", list(X.columns), "market_size", "YearMonth")
    beta = coef.set_index("variable")["coefficient"].reindex(X.columns).fillna(0.0).to_numpy(dtype=float)
    model_df["delta_hat"] = X.to_numpy(dtype=float) @ beta
    model_df["exp_delta_hat"] = np.exp(np.clip(model_df["delta_hat"], -50, 50))
    denom = model_df.groupby("YearMonth")["exp_delta_hat"].transform("sum") + 1.0
    model_df["predicted_share"] = model_df["exp_delta_hat"] / denom
    model_df["predicted_count"] = model_df["market_size"] * model_df["predicted_share"]
    model_df["predicted_trip_count_component"] = model_df["predicted_count"] * model_df["expected_trip_count"]
    model_df["predicted_trip_revenue_component"] = model_df["predicted_count"] * model_df["expected_trip_revenue"]
    model_df["predicted_user_payment_component"] = model_df["predicted_count"] * model_df["expected_user_payment"]
    coef["model_variant"] = "with_price" if include_price else "no_price"
    return model_df, coef, train_r2


def utility_parts_for_counterfactual(
    panel: pd.DataFrame,
    coef: pd.DataFrame,
    train_months: List[str],
    model_spec: Optional[dict],
) -> Tuple[np.ndarray, np.ndarray]:
    beta = coef.set_index("variable")["coefficient"]
    zero_price = panel.copy()
    one_dollar = panel.copy()
    zero_price["expected_user_payment"] = 0.0
    one_dollar["expected_user_payment"] = 1.0
    x0 = build_design(zero_price, model_spec=model_spec, train_months=train_months)
    x1 = build_design(one_dollar, model_spec=model_spec, train_months=train_months)
    x0 = x0.reindex(columns=beta.index, fill_value=0.0)
    x1 = x1.reindex(columns=beta.index, fill_value=0.0)
    beta_vector = beta.reindex(x0.columns).fillna(0.0).to_numpy(dtype=float)
    utility_without_price = x0.to_numpy(dtype=float) @ beta_vector
    price_slope = (x1.to_numpy(dtype=float) - x0.to_numpy(dtype=float)) @ beta_vector
    return utility_without_price, price_slope


def revenue_by_month(predicted: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    predicted_monthly = (
        predicted.groupby("YearMonth", as_index=False)
        .agg(
            predicted_trip_revenue=("predicted_trip_revenue_component", "sum"),
            predicted_user_payment_revenue=("predicted_user_payment_component", "sum"),
            predicted_positive_users=("predicted_count", "sum"),
            predicted_total_trips=("predicted_trip_count_component", "sum"),
            observed_positive_users=("observed_count", "sum"),
            market_size=("market_size", "first"),
        )
    )
    plan_trips = (
        predicted.assign(plan_group=np.where(predicted["plan"].eq("Free Plan"), "free_plan", "paid_plan"))
        .groupby(["YearMonth", "plan_group"], as_index=False)["predicted_trip_count_component"]
        .sum()
        .pivot(index="YearMonth", columns="plan_group", values="predicted_trip_count_component")
        .reset_index()
        .rename(columns={"free_plan": "predicted_free_plan_trips", "paid_plan": "predicted_paid_plan_trips"})
    )
    monthly = predicted_monthly.merge(plan_trips, on="YearMonth", how="left").merge(actual, on="YearMonth", how="left").sort_values("YearMonth")
    monthly["actual_trip_revenue"] = monthly["actual_trip_revenue"].fillna(0.0)
    monthly["actual_user_payment_revenue"] = monthly["actual_user_payment_revenue"].fillna(0.0)
    monthly["actual_positive_users"] = monthly["actual_positive_users"].fillna(0.0)
    for col in [
        "predicted_free_plan_trips",
        "predicted_paid_plan_trips",
        "actual_total_trips",
        "actual_free_plan_trips",
        "actual_paid_plan_trips",
    ]:
        if col not in monthly:
            monthly[col] = 0.0
        monthly[col] = monthly[col].fillna(0.0)
    return monthly


def metrics_for_split(predicted: pd.DataFrame, monthly: pd.DataFrame, split: str) -> dict:
    """Compute revenue and trip-volume prediction errors for one split."""
    p = predicted[predicted["split"].eq(split)].copy()
    m = monthly[monthly["split"].eq(split)].copy()
    def mae(a):
        return float(np.mean(np.abs(a))) if len(a) else float("nan")
    def rmse(a):
        return float(np.sqrt(np.mean(np.square(a)))) if len(a) else float("nan")
    def mape(error, actual):
        denom = actual.replace(0, np.nan)
        return mae(error / denom)
    rev_err = m["predicted_user_payment_revenue"] - m["actual_user_payment_revenue"]
    trip_err = m["predicted_trip_revenue"] - m["actual_trip_revenue"]
    dem_err = m["predicted_positive_users"] - m["observed_positive_users"]
    total_trip_count_err = m["predicted_total_trips"] - m["actual_total_trips"]
    free_plan_trip_count_err = m["predicted_free_plan_trips"] - m["actual_free_plan_trips"]
    paid_plan_trip_count_err = m["predicted_paid_plan_trips"] - m["actual_paid_plan_trips"]
    product_err = p["predicted_count"] - p["observed_count"]
    share_err = p["predicted_share"] - p["observed_share"]
    return {
        "split": split,
        "n_months": int(m["YearMonth"].nunique()),
        "share_mae": mae(share_err),
        "share_rmse": rmse(share_err),
        "product_count_mae": mae(product_err),
        "product_count_rmse": rmse(product_err),
        "monthly_positive_users_mae": mae(dem_err),
        "monthly_positive_users_rmse": rmse(dem_err),
        "user_payment_revenue_mae": mae(rev_err),
        "user_payment_revenue_rmse": rmse(rev_err),
        "user_payment_revenue_mape": mape(rev_err, m["actual_user_payment_revenue"]),
        "total_trips_mae": mae(total_trip_count_err),
        "total_trips_rmse": rmse(total_trip_count_err),
        "total_trips_mape": mape(total_trip_count_err, m["actual_total_trips"]),
        "free_plan_trips_mae": mae(free_plan_trip_count_err),
        "free_plan_trips_rmse": rmse(free_plan_trip_count_err),
        "free_plan_trips_mape": mape(free_plan_trip_count_err, m["actual_free_plan_trips"]),
        "paid_plan_trips_mae": mae(paid_plan_trip_count_err),
        "paid_plan_trips_rmse": rmse(paid_plan_trip_count_err),
        "paid_plan_trips_mape": mape(paid_plan_trip_count_err, m["actual_paid_plan_trips"]),
        "trip_revenue_mae": mae(trip_err),
        "trip_revenue_rmse": rmse(trip_err),
    }


def metrics_for_dashboard(metric_rows: List[dict]) -> dict:
    out = {}
    for row in metric_rows:
        item = dict(row)
        item["positive_users_mae"] = item.get("monthly_positive_users_mae")
        item["positive_users_rmse"] = item.get("monthly_positive_users_rmse")
        item["product_demand_mae"] = item.get("product_count_mae")
        item["product_demand_rmse"] = item.get("product_count_rmse")
        out[item["split"]] = item
    return out


def coefficients_for_dashboard(coef: pd.DataFrame) -> List[dict]:
    out = coef.copy()
    if "p_value_normal" in out.columns:
        out["p_value"] = out["p_value_normal"]
    return out.to_dict("records")


def adjusted_r2(r2: float, n_obs: int, n_regressors: int) -> float:
    if not np.isfinite(r2) or n_obs <= n_regressors + 1:
        return float("nan")
    return 1.0 - (1.0 - float(r2)) * ((n_obs - 1) / max(n_obs - n_regressors - 1, 1))


def fit_summary_for_dashboard(panel: pd.DataFrame, train_months: List[str], coef: pd.DataFrame, r2: float) -> dict:
    train = panel[panel["YearMonth"].isin(train_months)].copy()
    n_obs = int(len(train))
    n_regressors = int(len(coef)) if coef is not None else 0
    return {
        "dependent_variable": "share-inverted mean utility index",
        "estimator": "weighted least squares on share-inverted utilities",
        "weight": "market size by month",
        "cluster_se": "standard errors clustered by YearMonth",
        "training_months": int(train["YearMonth"].nunique()) if not train.empty else 0,
        "inside_products": int(train["product"].nunique()) if "product" in train else 0,
        "training_observations": n_obs,
        "regressors": n_regressors,
        "r2": r2,
        "adjusted_r2": adjusted_r2(r2, n_obs, n_regressors),
    }


def kernel_summary_for_dashboard(kernel_diag: pd.DataFrame) -> List[dict]:
    if kernel_diag.empty:
        return []
    rows = (
        kernel_diag.groupby("kernel_source", as_index=False)
        .agg(
            products=("product", "nunique"),
            baskets=("source_baskets", "mean"),
            trip_rows=("source_trip_rows", "mean"),
        )
        .sort_values("kernel_source")
    )
    rows = rows.rename(columns={"kernel_source": "source"})
    return rows.to_dict("records")


def kernel_details_for_dashboard(kernel_diag: pd.DataFrame) -> List[dict]:
    if kernel_diag.empty:
        return []
    keep = [
        "YearMonth",
        "product",
        "plan",
        "count_bin",
        "kernel_source",
        "condition_description",
        "source_baskets",
        "source_trip_rows",
        "source_calendar_months",
        "source_months_of_year",
        "trip_count_hist",
        "duration_hist",
        "distance_hist",
        "trip_count_summary",
        "duration_summary",
        "distance_summary",
    ]
    return kernel_diag[[col for col in keep if col in kernel_diag.columns]].to_dict("records")


def add_validation_outside_row(product_summary: pd.DataFrame, monthly: pd.DataFrame, split: str = "test") -> pd.DataFrame:
    m = monthly[monthly["split"].eq(split)].copy()
    if m.empty:
        return product_summary
    outside_actual = m["market_size"] - m["observed_positive_users"]
    outside_predicted = m["market_size"] - m["predicted_positive_users"]
    outside = pd.DataFrame(
        [
            {
                "plan": "Outside option",
                "count_bin": "00",
                "product": "No use / outside option [0 trips]",
                "actual_test_positive_users": float(outside_actual.mean()),
                "predicted_test_positive_users": float(outside_predicted.mean()),
                "expected_user_payment": 0.0,
                "expected_trip_count": 0.0,
                "prediction_error": float(outside_predicted.mean() - outside_actual.mean()),
                "bin_sort": 0,
            }
        ]
    )
    return pd.concat([outside, product_summary], ignore_index=True, sort=False)


def add_forecast_outside_row(product_summary: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    current = monthly[monthly["scenario"].eq("Current pricing")].copy()
    what_if = monthly[monthly["scenario"].eq("What-if pricing")].copy()
    if current.empty or what_if.empty:
        return product_summary
    current_outside = current["market_size"] - current["predicted_positive_users"]
    what_if_outside = what_if["market_size"] - what_if["predicted_positive_users"]
    outside = pd.DataFrame(
        [
            {
                "plan": "Outside option",
                "count_bin": "00",
                "product": "No use / outside option [0 trips]",
                "current_positive_users": float(current_outside.mean()),
                "what_if_positive_users": float(what_if_outside.mean()),
                "change_positive_users": float(what_if_outside.mean() - current_outside.mean()),
                "current_predicted_trips": 0.0,
                "what_if_predicted_trips": 0.0,
                "change_predicted_trips": 0.0,
                "current_user_payment_revenue": 0.0,
                "what_if_user_payment_revenue": 0.0,
                "change_user_payment_revenue": 0.0,
                "current_expected_trip_count": 0.0,
                "what_if_expected_trip_count": 0.0,
                "current_expected_user_payment": 0.0,
                "what_if_expected_user_payment": 0.0,
                "bin_sort": 0,
            }
        ]
    )
    return pd.concat([outside, product_summary], ignore_index=True, sort=False)


def warnings_from_model(price_coef: float, price_p: float, metrics: List[dict], include_price: bool = True) -> List[dict]:
    """Create dashboard warnings from fitted demand-model diagnostics."""
    warnings = []
    if not include_price:
        warnings.append({"level": "warn", "message": "Price coefficient is not included. Demand will not respond to price changes in what-if or optimization runs."})
    elif not np.isfinite(price_coef):
        warnings.append({"level": "danger", "message": "Price coefficient was not estimated."})
    elif price_coef < 0:
        warnings.append({"level": "good", "message": f"Price coefficient is negative as expected ({price_coef:.5f})."})
    else:
        warnings.append({"level": "danger", "message": f"Price coefficient is non-negative ({price_coef:.5f}); counterfactual price responses are not credible."})
    if np.isfinite(price_p) and price_p > 0.10:
        warnings.append({"level": "warn", "message": f"Price coefficient is imprecise (p={price_p:.3f})."})
    test = next((m for m in metrics if m["split"] == "test"), None)
    if test and np.isfinite(test.get("user_payment_revenue_mape", np.nan)) and test["user_payment_revenue_mape"] > 0.25:
        warnings.append({"level": "warn", "message": f"Test revenue MAPE is high ({100 * test['user_payment_revenue_mape']:.1f}%)."})
    return warnings


def warnings_from_kernel(kernel_diag: pd.DataFrame, min_support: int) -> List[dict]:
    """Create dashboard warnings when empirical kernels are sparse."""
    if kernel_diag.empty:
        return [{"level": "warn", "message": "No kernel diagnostics were returned."}]
    zero = kernel_diag[kernel_diag["source_baskets"].le(0)] if "source_baskets" in kernel_diag else pd.DataFrame()
    sparse = kernel_diag[
        kernel_diag["source_baskets"].gt(0) & kernel_diag["source_baskets"].lt(int(min_support))
    ] if "source_baskets" in kernel_diag else pd.DataFrame()
    warnings = []
    if not zero.empty:
        products = sorted(zero["product"].astype(str).unique())[:5]
        warnings.append(
            {
                "level": "danger",
                "message": f"{len(zero)} product-month kernels have zero same-count-bin baskets. Products include: {', '.join(products)}. Consider widening those trip-count bins.",
            }
        )
    if not sparse.empty:
        min_baskets = int(sparse["source_baskets"].min())
        products = sorted(sparse["product"].astype(str).unique())[:5]
        warnings.append(
            {
                "level": "warn",
                "message": f"{len(sparse)} product-month kernels are below the minimum support of {int(min_support)} baskets, but still stay within the product count bin. Smallest support is {min_baskets}. Products include: {', '.join(products)}.",
            }
        )
    return warnings


def run_validation(config: dict) -> dict:
    """Run the model-validation workflow and return dashboard-ready output."""
    segment = config.get("segment", "Regular")
    plans = SEGMENT_PLANS[segment]
    bins = bins_from_config(config)
    model_spec = model_spec_from_config(config, require_price=False)
    kernel_spec = kernel_spec_from_config(config)
    user_month_raw, trips_raw = read_inputs(segment)
    user_month, trips, excluded_latest = filter_latest(user_month_raw, trips_raw, bool(config.get("drop_latest", True)))
    train_months, test_months = choose_validation_months(
        user_month,
        config["train_start"],
        config["train_end"],
        config["test_start"],
        config["test_end"],
    )
    user_month_window, trips_window, train_user_month, train_trips = split_data(user_month, trips, train_months, test_months)
    month_pricing = build_month_pricing(user_month_window, plans)
    kernel_user_month_raw, kernel_trips_raw = read_all_inputs()
    kernel_user_month = kernel_user_month_raw[kernel_user_month_raw["YearMonth"].isin(train_months)].copy()
    kernel_trips = kernel_trips_raw[kernel_trips_raw["YearMonth"].isin(train_months)].copy()
    train_baskets = build_baskets(kernel_user_month, kernel_trips, bins=bins)
    all_baskets = build_baskets(user_month_window, trips_window, plans, bins=bins)
    actual_monthly, _basket_revenue = compute_actual_revenue(all_baskets)
    costs, kernel_diag = expected_costs(train_baskets, segment, month_pricing, train_months + test_months, plans, kernel_spec=kernel_spec, bins=bins)
    panel = build_share_panel(user_month_window, costs, plans, train_months + test_months, bins=bins)
    include_price = bool(model_spec.get("include_price", True))
    pred_price, coef_price, r2_price = estimate_predict(panel, train_months, include_price=include_price, model_spec=model_spec)
    non_price_spec = dict(model_spec)
    non_price_spec["include_price"] = False
    can_fit_no_price = include_price and any(non_price_spec.get(k, False) for k in ["include_intercept", "include_count_fe", "include_plan_fe", "include_month_fe"])
    if can_fit_no_price:
        pred_noprice, coef_noprice, r2_noprice = estimate_predict(panel, train_months, include_price=False, model_spec=model_spec)
        monthly_noprice = revenue_by_month(pred_noprice, actual_monthly)
        monthly_noprice["split"] = np.where(monthly_noprice["YearMonth"].isin(train_months), "train", "test")
    else:
        coef_noprice = pd.DataFrame(columns=["variable", "coefficient", "cluster_se", "z_stat", "p_value_normal", "model_variant"])
        r2_noprice = float("nan")
        monthly_noprice = pd.DataFrame()
    monthly_price = revenue_by_month(pred_price, actual_monthly)
    monthly_price["split"] = np.where(monthly_price["YearMonth"].isin(train_months), "train", "test")
    metric_rows = [metrics_for_split(pred_price, monthly_price, "train"), metrics_for_split(pred_price, monthly_price, "test")]
    product_summary = (
        pred_price[pred_price["split"].eq("test")]
        .groupby(["plan", "count_bin", "product"], as_index=False)
        .agg(
            actual_test_positive_users=("observed_count", "mean"),
            predicted_test_positive_users=("predicted_count", "mean"),
            expected_user_payment=("expected_user_payment", "mean"),
            expected_trip_count=("expected_trip_count", "mean"),
        )
        .sort_values(["plan", "count_bin"])
    )
    product_summary["prediction_error"] = product_summary["predicted_test_positive_users"] - product_summary["actual_test_positive_users"]
    product_summary["bin_sort"] = product_summary["count_bin"].map(lambda value: bin_sort_value(value, bins))
    product_summary = add_validation_outside_row(product_summary, monthly_price, split="test")
    price_coef, price_p = coefficient_value(coef_price, "expected_user_payment")
    warning_rows = warnings_from_model(price_coef, price_p, metric_rows, include_price=include_price)
    warning_rows.extend(warnings_from_kernel(kernel_diag, kernel_spec.get("min_support", MIN_SUPPORT)))
    return json_safe(
        {
            "config": {
                "segment": segment,
                "train_start": train_months[0],
                "train_end": train_months[-1],
                "test_start": test_months[0],
                "test_end": test_months[-1],
                "excluded_latest_month": excluded_latest,
            },
            "segment": segment,
            "plans": plans,
            "model_spec": model_spec_for_dashboard(model_spec, plans),
            "binning": binning_for_dashboard(bins),
            "kernel_rule": kernel_rule_for_dashboard(kernel_spec),
            "excluded_latest_month": excluded_latest,
            "train_months": [train_months[0], train_months[-1]],
            "test_months": [test_months[0], test_months[-1]],
            "price_model_r2": r2_price,
            "no_price_model_r2": r2_noprice,
            "fit_summary": fit_summary_for_dashboard(panel, train_months, coef_price, r2_price),
            "price_coefficient": price_coef,
            "price_p_value": price_p,
            "coefficients": coefficients_for_dashboard(coef_price),
            "no_price_coefficients": coefficients_for_dashboard(coef_noprice),
            "metrics": metrics_for_dashboard(metric_rows),
            "metrics_rows": metric_rows,
            "warnings": warning_rows,
            "monthly": monthly_price.to_dict("records"),
            "monthly_no_price": monthly_noprice.to_dict("records"),
            "product_summary": product_summary.to_dict("records"),
            "kernel_summary": kernel_summary_for_dashboard(kernel_diag),
            "kernel_details": kernel_details_for_dashboard(kernel_diag),
        }
    )


def latest_pricing(segment: str, train_end: Optional[str] = None, drop_latest: bool = True) -> dict:
    """Return the latest observed pricing parameters for one segment."""
    plans = SEGMENT_PLANS[segment]
    user_month_raw, trips_raw = read_inputs(segment)
    user_month, _trips, _excluded = filter_latest(user_month_raw, trips_raw, drop_latest)
    if train_end:
        user_month = user_month[user_month["YearMonth"].le(train_end)].copy()
    latest_month = sorted(user_month["YearMonth"].astype(str).unique())[-1]
    row = user_month[user_month["YearMonth"].eq(latest_month)].iloc[0]
    pricing = {plan: plan_pricing_from_row(row, plan) for plan in plans}
    return json_safe({"segment": segment, "latest_pricing_month": latest_month, "plans": plans, "pricing": pricing})


def apply_pricing_adjustments(base: dict, submitted: dict) -> dict:
    params = base.copy()
    for key in ["per_minute", "per_hour", "per_day", "trip_start_fee", "included_miles", "extra_mile", "monthly_fee", "monthly_credit"]:
        if key in submitted:
            params[key] = float(submitted[key])
    if "time_multiplier" in submitted:
        mult = float(submitted["time_multiplier"])
        params["per_minute"] = float(params["per_minute"]) * mult
        params["per_hour"] = float(params["per_hour"]) * mult
        params["per_day"] = float(params["per_day"]) * mult
    return params


def market_size_forecast(user_month: pd.DataFrame, train_months: List[str], future_months: List[str], multiplier: float) -> Dict[str, float]:
    train = user_month[user_month["YearMonth"].isin(train_months) & user_month["InMarket"].eq(1)].copy()
    month_sizes = train.groupby("YearMonth")["UserID"].nunique()
    train = train.merge(month_sizes.rename("market_size"), left_on="YearMonth", right_index=True, how="left")
    moy_mean = train.groupby("MonthOfYear")["market_size"].mean().to_dict()
    fallback = float(month_sizes.mean())
    return {m: float(moy_mean.get(int(m[-2:]), fallback) * multiplier) for m in future_months}


def predict_counterfactual(
    coef: pd.DataFrame,
    train_months: List[str],
    forecast_panel: pd.DataFrame,
    model_spec: Optional[dict] = None,
) -> pd.DataFrame:
    X = build_design(forecast_panel, model_spec=model_spec, train_months=train_months)
    beta = coef.set_index("variable")["coefficient"]
    X = X.reindex(columns=beta.index, fill_value=0.0)
    out = forecast_panel.copy()
    out["delta_hat"] = X.to_numpy(dtype=float) @ beta.to_numpy(dtype=float)
    out["exp_delta_hat"] = np.exp(np.clip(out["delta_hat"], -50, 50))
    denom = out.groupby("YearMonth")["exp_delta_hat"].transform("sum") + 1.0
    out["predicted_share"] = out["exp_delta_hat"] / denom
    out["predicted_count"] = out["market_size"] * out["predicted_share"]
    out["predicted_trip_count_component"] = out["predicted_count"] * out["expected_trip_count"]
    out["predicted_trip_revenue_component"] = out["predicted_count"] * out["expected_trip_revenue"]
    out["predicted_user_payment_component"] = out["predicted_count"] * out["expected_user_payment"]
    return out


def summarize_forecast(predicted: pd.DataFrame, label: str) -> pd.DataFrame:
    monthly = (
        predicted.groupby("YearMonth", as_index=False)
        .agg(
            market_size=("market_size", "first"),
            predicted_positive_users=("predicted_count", "sum"),
            predicted_trips=("predicted_trip_count_component", "sum"),
            predicted_trip_revenue=("predicted_trip_revenue_component", "sum"),
            predicted_user_payment_revenue=("predicted_user_payment_component", "sum"),
        )
        .sort_values("YearMonth")
    )
    plan_trip = predicted.assign(
        plan_group=np.where(predicted["plan"].eq("Free Plan"), "free_plan", "paid_plan")
    )
    plan_trip = (
        plan_trip.groupby(["YearMonth", "plan_group"], as_index=False)["predicted_trip_count_component"]
        .sum()
        .pivot(index="YearMonth", columns="plan_group", values="predicted_trip_count_component")
        .reset_index()
        .rename(columns={"free_plan": "predicted_free_plan_trips", "paid_plan": "predicted_paid_plan_trips"})
    )
    monthly = monthly.merge(plan_trip, on="YearMonth", how="left")
    for col in ["predicted_free_plan_trips", "predicted_paid_plan_trips"]:
        if col not in monthly:
            monthly[col] = 0.0
        monthly[col] = monthly[col].fillna(0.0)
    monthly["scenario"] = label
    return monthly


def run_what_if(config: dict) -> dict:
    """Run the what-if pricing workflow and return dashboard-ready output."""
    segment = config.get("segment", "Regular")
    plans = SEGMENT_PLANS[segment]
    bins = bins_from_config(config)
    model_spec = model_spec_from_config(config, require_price=True)
    kernel_spec = kernel_spec_from_config(config)
    horizon = int(config.get("horizon_months", 12))
    market_multiplier = float(config.get("market_multiplier", config.get("market_size_multiplier", 1.0)))
    user_month_raw, trips_raw = read_inputs(segment)
    user_month, trips, excluded_latest = filter_latest(user_month_raw, trips_raw, bool(config.get("drop_latest", True)))
    months = sorted(user_month["YearMonth"].astype(str).unique())
    train_start = config.get("train_start", months[0])
    train_end = config.get("train_end", months[-1])
    train_months = choose_train_months(user_month, train_start, train_end)
    train_user_month = user_month[user_month["YearMonth"].isin(train_months)].copy()
    kernel_user_month_raw, kernel_trips_raw = read_all_inputs()
    kernel_user_month = kernel_user_month_raw[kernel_user_month_raw["YearMonth"].isin(train_months)].copy()
    kernel_trips = kernel_trips_raw[kernel_trips_raw["YearMonth"].isin(train_months)].copy()
    train_baskets = build_baskets(kernel_user_month, kernel_trips, bins=bins)

    observed_pricing = latest_pricing(segment, train_end=train_months[-1], drop_latest=bool(config.get("drop_latest", True)))
    base_params = observed_pricing["pricing"]
    adjustments = config.get("pricing", {})
    cf_params = {
        plan: apply_pricing_adjustments(base_params[plan], adjustments.get(plan, {}))
        for plan in plans
    }
    future_start = config.get("forecast_start") or month_add(train_months[-1], 1)
    future_months = [month_add(future_start, i) for i in range(horizon)]

    base_month_pricing = {(m, plan): base_params[plan] for m in future_months for plan in plans}
    cf_month_pricing = {(m, plan): cf_params[plan] for m in future_months for plan in plans}
    base_costs, base_diag = expected_costs(train_baskets, segment, base_month_pricing, future_months, plans, kernel_spec=kernel_spec, bins=bins)
    cf_costs, _cf_diag = expected_costs(train_baskets, segment, cf_month_pricing, future_months, plans, kernel_spec=kernel_spec, bins=bins)

    train_pricing = build_month_pricing(train_user_month, plans)
    train_costs, _train_kernel_diag = expected_costs(train_baskets, segment, train_pricing, train_months, plans, kernel_spec=kernel_spec, bins=bins)
    train_panel = build_share_panel(train_user_month, train_costs, plans, train_months, bins=bins)
    include_price = bool(model_spec.get("include_price", True))
    _train_pred, coef, train_r2 = estimate_predict(train_panel, train_months, include_price=include_price, model_spec=model_spec)

    market_sizes = market_size_forecast(user_month, train_months, future_months, market_multiplier)
    empty_future_um = pd.DataFrame(
        {
            "YearMonth": future_months,
            "MonthOfYear": [int(str(m)[-2:]) for m in future_months],
            "InMarket": 1,
            "UserID": range(len(future_months)),
            "TripCount": 0,
            "PlanUsedThisMonth": "",
        }
    )
    base_panel = build_share_panel(empty_future_um, base_costs, plans, future_months, market_sizes, bins=bins)
    cf_panel = build_share_panel(empty_future_um, cf_costs, plans, future_months, market_sizes, bins=bins)
    base_pred = predict_counterfactual(coef, train_months, base_panel, model_spec=model_spec)
    cf_pred = predict_counterfactual(coef, train_months, cf_panel, model_spec=model_spec)

    monthly = pd.concat([summarize_forecast(base_pred, "Current pricing"), summarize_forecast(cf_pred, "What-if pricing")], ignore_index=True)
    totals = (
        monthly.groupby("scenario", as_index=False)
        .agg(
            total_user_payment_revenue=("predicted_user_payment_revenue", "sum"),
            total_trip_revenue=("predicted_trip_revenue", "sum"),
            avg_positive_users=("predicted_positive_users", "mean"),
            avg_predicted_trips=("predicted_trips", "mean"),
        )
        .to_dict("records")
    )
    base_product = (
        base_pred.groupby(["plan", "count_bin", "product"], as_index=False)
        .agg(
            current_positive_users=("predicted_count", "mean"),
            current_predicted_trips=("predicted_trip_count_component", "mean"),
            current_user_payment_revenue=("predicted_user_payment_component", "mean"),
            current_expected_trip_count=("expected_trip_count", "mean"),
            current_expected_user_payment=("expected_user_payment", "mean"),
        )
    )
    cf_product = (
        cf_pred.groupby(["plan", "count_bin", "product"], as_index=False)
        .agg(
            what_if_positive_users=("predicted_count", "mean"),
            what_if_predicted_trips=("predicted_trip_count_component", "mean"),
            what_if_user_payment_revenue=("predicted_user_payment_component", "mean"),
            what_if_expected_trip_count=("expected_trip_count", "mean"),
            what_if_expected_user_payment=("expected_user_payment", "mean"),
        )
    )
    product_summary = base_product.merge(cf_product, on=["plan", "count_bin", "product"], how="outer").fillna(0.0)
    product_summary["change_positive_users"] = product_summary["what_if_positive_users"] - product_summary["current_positive_users"]
    product_summary["change_predicted_trips"] = product_summary["what_if_predicted_trips"] - product_summary["current_predicted_trips"]
    product_summary["change_user_payment_revenue"] = product_summary["what_if_user_payment_revenue"] - product_summary["current_user_payment_revenue"]
    product_summary["bin_sort"] = product_summary["count_bin"].map(lambda value: bin_sort_value(value, bins))
    product_summary = add_forecast_outside_row(product_summary, monthly)
    product_summary = product_summary.sort_values(["plan", "bin_sort"])
    price_coef, price_p = coefficient_value(coef, "expected_user_payment")
    warning_rows = warnings_from_model(price_coef, price_p, [], include_price=include_price)
    warning_rows.extend(warnings_from_kernel(base_diag, kernel_spec.get("min_support", MIN_SUPPORT)))
    return json_safe(
        {
            "config": {
                "segment": segment,
                "train_start": train_months[0],
                "train_end": train_months[-1],
                "forecast_start": future_months[0],
                "forecast_end": future_months[-1],
                "horizon_months": horizon,
                "market_multiplier": market_multiplier,
                "excluded_latest_month": excluded_latest,
            },
            "segment": segment,
            "plans": plans,
            "model_spec": model_spec_for_dashboard(model_spec, plans),
            "binning": binning_for_dashboard(bins),
            "kernel_rule": kernel_rule_for_dashboard(kernel_spec),
            "excluded_latest_month": excluded_latest,
            "train_months": [train_months[0], train_months[-1]],
            "forecast_months": [future_months[0], future_months[-1]],
            "train_r2": train_r2,
            "fit_summary": fit_summary_for_dashboard(train_panel, train_months, coef, train_r2),
            "price_coefficient": price_coef,
            "price_p_value": price_p,
            "coefficients": coefficients_for_dashboard(coef),
            "warnings": warning_rows,
            "base_pricing": base_params,
            "what_if_pricing": cf_params,
            "pricing": {
                "current": base_params,
                "what_if": cf_params,
            },
            "monthly": monthly.to_dict("records"),
            "totals": totals,
            "product_summary": product_summary.to_dict("records"),
            "kernel_summary": kernel_summary_for_dashboard(base_diag),
            "kernel_details": kernel_details_for_dashboard(base_diag),
        }
    )


# ---------------------------------------------------------------------------
# Fast counterfactual evaluation and constrained optimization
# ---------------------------------------------------------------------------

def fast_source_from_baskets(source_df: pd.DataFrame) -> FastKernelSource:
    """Convert a kernel basket frame into arrays for repeated repricing."""
    trip_counts = []
    basket_ids = []
    durations = []
    distances = []
    for basket_id, row in enumerate(source_df.itertuples(index=False)):
        durs = np.asarray(row.durations, dtype=float)
        dists = np.asarray(row.distances, dtype=float)
        trip_counts.append(float(row.TripCount))
        durations.extend(durs.tolist())
        distances.extend(dists.tolist())
        basket_ids.extend([basket_id] * len(durs))
    return FastKernelSource(
        trip_counts=np.asarray(trip_counts, dtype=float),
        basket_ids=np.asarray(basket_ids, dtype=int),
        durations=np.asarray(durations, dtype=float),
        distances=np.asarray(distances, dtype=float),
        n_baskets=len(trip_counts),
    )


def make_forecast_template(
    train_baskets: pd.DataFrame,
    segment: str,
    future_months: List[str],
    plans: List[str],
    market_sizes: Dict[str, float],
    coef: pd.DataFrame,
    train_months: List[str],
    model_spec: Optional[dict] = None,
    kernel_spec: Optional[dict] = None,
    bins: Optional[List[CountBin]] = None,
) -> FastForecastContext:
    """Precompute arrays shared by all counterfactual price evaluations."""
    bins = default_bins() if bins is None else bins
    rows = []
    sources: List[FastKernelSource] = []
    source_ids = []
    kernel_details = []
    source_cache: Dict[Tuple[object, ...], FastKernelSource] = {}
    source_index: Dict[Tuple[object, ...], int] = {}
    payload_cache: Dict[Tuple[object, ...], dict] = {}
    for month in future_months:
        for plan in plans:
            for count_bin in [b.label for b in bins]:
                product = product_name(plan, count_bin)
                source, source_label, condition_flags = choose_kernel_source(train_baskets, segment, plan, count_bin, month, kernel_spec)
                source_key = kernel_cache_key(segment, plan, count_bin, month, condition_flags)
                if source_key not in source_cache:
                    source_cache[source_key] = fast_source_from_baskets(source)
                    source_index[source_key] = len(sources)
                    sources.append(source_cache[source_key])
                    payload_cache[source_key] = kernel_source_payload(source)
                source_ids.append(source_index[source_key])
                rows.append(
                    {
                        "YearMonth": month,
                        "MonthOfYear": int(str(month)[-2:]),
                        "plan": plan,
                        "count_bin": count_bin,
                        "product": product,
                        "bin_sort": bin_sort_value(count_bin, bins),
                        "market_size": float(market_sizes[month]),
                        "kernel_source": source_label,
                        "source_baskets": int(len(source)),
                    }
                )
                kernel_details.append(kernel_detail_row(month, product, plan, count_bin, source_label, source, condition_flags, payload_cache[source_key]))
    template = pd.DataFrame(rows)
    utility_without_price, price_slope = utility_parts_for_counterfactual(
        template.assign(expected_user_payment=0.0),
        coef,
        train_months,
        model_spec,
    )
    month_labels = sorted(template["YearMonth"].astype(str).unique())
    month_index_map = {month: i for i, month in enumerate(month_labels)}
    month_index = template["YearMonth"].map(month_index_map).to_numpy(dtype=int)
    month_market_size = np.asarray([float(market_sizes[m]) for m in month_labels], dtype=float)
    return FastForecastContext(
        template=template,
        sources=sources,
        source_ids=np.asarray(source_ids, dtype=int),
        plan_is_free=template["plan"].eq("Free Plan").to_numpy(dtype=bool),
        utility_without_price=utility_without_price,
        price_slope=price_slope,
        month_index=month_index,
        month_labels=month_labels,
        month_market_size=month_market_size,
        product_labels=template["product"].astype(str).tolist(),
        kernel_details=kernel_details,
    )


def fast_price_source(source: FastKernelSource, params: dict) -> Tuple[float, float, float]:
    """Price a compact kernel source without rebuilding pandas objects."""
    if source.n_baskets == 0:
        return 0.0, 0.0, 0.0
    charges = price_trip_array(source.durations, source.distances, params)
    trip_sums = np.bincount(source.basket_ids, weights=charges, minlength=source.n_baskets)
    user_payments = float(params["monthly_fee"]) + np.maximum(0.0, trip_sums - float(params["monthly_credit"]))
    return (
        float(np.mean(source.trip_counts)),
        float(np.mean(trip_sums)),
        float(np.mean(user_payments)),
    )


def evaluate_forecast_context(context: FastForecastContext, params_by_plan: Dict[str, dict], label: str) -> dict:
    """Evaluate one tariff candidate against the precomputed forecast context."""
    n_rows = len(context.template)
    expected_trip_count = np.zeros(n_rows, dtype=float)
    expected_trip_revenue = np.zeros(n_rows, dtype=float)
    expected_user_payment = np.zeros(n_rows, dtype=float)
    price_cache: Dict[Tuple[int, Tuple[float, ...]], Tuple[float, float, float]] = {}
    for i, row in enumerate(context.template.itertuples(index=False)):
        params = params_by_plan[row.plan]
        source_id = int(context.source_ids[i])
        cache_key = (source_id, pricing_signature(params))
        if cache_key not in price_cache:
            price_cache[cache_key] = fast_price_source(context.sources[source_id], params)
        trip_count, trip_revenue, user_payment = price_cache[cache_key]
        expected_trip_count[i] = trip_count
        expected_trip_revenue[i] = trip_revenue
        expected_user_payment[i] = user_payment

    utility = context.utility_without_price + context.price_slope * expected_user_payment
    exp_utility = np.exp(np.clip(utility, -50, 50))
    inside_sum = np.bincount(context.month_index, weights=exp_utility, minlength=len(context.month_labels))
    denom = inside_sum + 1.0
    predicted_share = exp_utility / denom[context.month_index]
    market_size = context.month_market_size[context.month_index]
    predicted_count = market_size * predicted_share
    trip_count_component = predicted_count * expected_trip_count
    user_revenue_component = predicted_count * expected_user_payment
    trip_revenue_component = predicted_count * expected_trip_revenue

    monthly_inside = np.bincount(context.month_index, weights=predicted_count, minlength=len(context.month_labels))
    monthly_trips = np.bincount(context.month_index, weights=trip_count_component, minlength=len(context.month_labels))
    monthly_free_trips = np.bincount(
        context.month_index[context.plan_is_free],
        weights=trip_count_component[context.plan_is_free],
        minlength=len(context.month_labels),
    )
    monthly_paid_trips = np.bincount(
        context.month_index[~context.plan_is_free],
        weights=trip_count_component[~context.plan_is_free],
        minlength=len(context.month_labels),
    )
    monthly_user_revenue = np.bincount(context.month_index, weights=user_revenue_component, minlength=len(context.month_labels))
    monthly_trip_revenue = np.bincount(context.month_index, weights=trip_revenue_component, minlength=len(context.month_labels))
    monthly = pd.DataFrame(
        {
            "YearMonth": context.month_labels,
            "scenario": label,
            "market_size": context.month_market_size,
            "predicted_positive_users": monthly_inside,
            "predicted_no_use_users": context.month_market_size - monthly_inside,
            "predicted_trips": monthly_trips,
            "predicted_free_plan_trips": monthly_free_trips,
            "predicted_paid_plan_trips": monthly_paid_trips,
            "predicted_user_payment_revenue": monthly_user_revenue,
            "predicted_trip_revenue": monthly_trip_revenue,
        }
    )

    product = context.template[["plan", "count_bin", "product", "bin_sort"]].copy()
    product["predicted_count"] = predicted_count
    product["predicted_trips"] = trip_count_component
    product["predicted_user_payment_revenue"] = user_revenue_component
    product["expected_trip_count"] = expected_trip_count
    product["expected_user_payment"] = expected_user_payment
    product_summary = (
        product.groupby(["plan", "count_bin", "product", "bin_sort"], as_index=False)
        .agg(
            predicted_count=("predicted_count", "mean"),
            predicted_trips=("predicted_trips", "mean"),
            predicted_user_payment_revenue=("predicted_user_payment_revenue", "mean"),
            expected_trip_count=("expected_trip_count", "mean"),
            expected_user_payment=("expected_user_payment", "mean"),
        )
        .sort_values(["plan", "bin_sort"])
    )
    no_use_row = pd.DataFrame(
        [
            {
                "plan": "Outside option",
                "count_bin": "00",
                "product": "No use / outside option [0 trips]",
                "predicted_count": float(monthly["predicted_no_use_users"].mean()),
                "predicted_trips": 0.0,
                "predicted_user_payment_revenue": 0.0,
                "expected_trip_count": 0.0,
                "expected_user_payment": 0.0,
                "bin_sort": 0,
            }
        ]
    )
    product_summary = pd.concat([no_use_row, product_summary], ignore_index=True, sort=False)
    return {
        "label": label,
        "monthly": monthly,
        "product_summary": product_summary,
        "total_user_payment_revenue": float(monthly_user_revenue.sum()),
        "total_trip_revenue": float(monthly_trip_revenue.sum()),
        "total_inside_demand": float(monthly_inside.sum()),
        "total_predicted_trips": float(monthly_trips.sum()),
        "total_no_use": float(context.month_market_size.sum() - monthly_inside.sum()),
        "avg_monthly_inside_demand": float(monthly_inside.mean()),
        "avg_monthly_predicted_trips": float(monthly_trips.mean()),
        "avg_monthly_no_use": float((context.month_market_size - monthly_inside).mean()),
        "market_size_total": float(context.month_market_size.sum()),
    }


def selected_optimization_variables(config: dict, base_params: Dict[str, dict], plans: List[str]) -> Tuple[List[dict], List[dict]]:
    """Translate dashboard bounds into optimizer variables and fixed overrides."""
    raw_variables = config.get("variables") or []
    raw_by_key = {
        (str(raw.get("plan", "")), str(raw.get("param", ""))): raw
        for raw in raw_variables
    }
    tie_time_rates = bool(config.get("tie_time_rates", False))
    opt_variables = []
    fixed_overrides = []
    grouped_time_plans = set()
    if tie_time_rates:
        for plan in plans:
            has_enabled_time = any(
                bool(raw_by_key.get((plan, param), {}).get("enabled", False))
                for param in TIME_PARAMETERS
            )
            if not has_enabled_time:
                continue
            lower_mults = []
            upper_mults = []
            targets = []
            source_bounds = {}
            for param in TIME_PARAMETERS:
                raw = raw_by_key.get((plan, param), {})
                current = float(base_params[plan][param])
                if current <= 0:
                    raise ValueError(f"Cannot tie time rates for {plan}: {param} has non-positive current value.")
                lower = float(raw.get("lower", current))
                upper = float(raw.get("upper", current))
                if upper < lower:
                    raise ValueError(f"Upper bound is below lower bound for {plan} {param}")
                lower_mults.append(lower / current)
                upper_mults.append(upper / current)
                targets.append({"plan": plan, "param": param, "base": current})
                source_bounds[param] = {"lower": lower, "upper": upper, "current": current}
            lower = max(lower_mults)
            upper = min(upper_mults)
            if upper < lower:
                raise ValueError(
                    f"No common time-rate multiplier satisfies the bounds for {plan}. "
                    "Widen at least one time-rate bound or turn off proportional time rates."
                )
            item = {
                "kind": "time_multiplier",
                "plan": plan,
                "param": "time_multiplier",
                "label": f"{plan} time-rate multiplier",
                "lower": lower,
                "upper": upper,
                "current": 1.0,
                "targets": targets,
                "source_bounds": source_bounds,
            }
            if np.isclose(lower, upper):
                item["value"] = lower
                fixed_overrides.append(item)
            else:
                opt_variables.append(item)
            grouped_time_plans.add(plan)

    for raw in raw_variables:
        if not raw.get("enabled", False):
            continue
        plan = str(raw.get("plan", ""))
        param = str(raw.get("param", ""))
        if plan not in plans:
            raise ValueError(f"Unknown optimization plan: {plan}")
        if param not in PRICING_PARAMETERS:
            raise ValueError(f"Unknown pricing parameter: {param}")
        if tie_time_rates and plan in grouped_time_plans and param in TIME_PARAMETERS:
            continue
        lower = float(raw.get("lower"))
        upper = float(raw.get("upper"))
        if not np.isfinite(lower) or not np.isfinite(upper):
            raise ValueError(f"Non-finite bounds for {plan} {param}")
        if upper < lower:
            raise ValueError(f"Upper bound is below lower bound for {plan} {param}")
        current = float(base_params[plan][param])
        item = {
            "kind": "single",
            "plan": plan,
            "param": param,
            "label": f"{plan} {param}",
            "lower": lower,
            "upper": upper,
            "current": current,
            "targets": [{"plan": plan, "param": param}],
        }
        if np.isclose(lower, upper):
            item["value"] = lower
            fixed_overrides.append(item)
        else:
            opt_variables.append(item)
    return opt_variables, fixed_overrides


def params_from_vector(
    base_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    vector: np.ndarray,
) -> Dict[str, dict]:
    params = {plan: dict(values) for plan, values in base_params.items()}
    def apply_value(var: dict, value: float) -> None:
        if var.get("kind") == "time_multiplier":
            for target in var["targets"]:
                params[target["plan"]][target["param"]] = float(target["base"]) * float(value)
        else:
            params[var["plan"]][var["param"]] = float(value)

    for fixed in fixed_overrides:
        apply_value(fixed, float(fixed["value"]))
    for value, var in zip(vector, opt_variables):
        apply_value(var, float(value))
    return params


def latin_hypercube(n_points: int, n_dim: int, rng: np.random.Generator) -> np.ndarray:
    if n_points <= 0 or n_dim <= 0:
        return np.empty((0, n_dim))
    out = np.zeros((n_points, n_dim), dtype=float)
    for j in range(n_dim):
        out[:, j] = (rng.permutation(n_points) + rng.random(n_points)) / n_points
    return out


def constraint_status(result: dict, min_total_demand: float, min_monthly_demand: Optional[np.ndarray] = None) -> Tuple[bool, float, float]:
    demand = float(result["total_predicted_trips"])
    total_violation = max(0.0, float(min_total_demand) - demand)
    monthly_violation = 0.0
    if min_monthly_demand is not None:
        monthly_demand = result["monthly"]["predicted_trips"].to_numpy(dtype=float)
        monthly_violation = float(np.maximum(0.0, min_monthly_demand - monthly_demand).max())
    return total_violation <= 1e-8 and monthly_violation <= 1e-8, total_violation, monthly_violation


def penalized_score(
    result: dict,
    min_total_demand: float,
    baseline_revenue: float,
    min_monthly_demand: Optional[np.ndarray] = None,
) -> float:
    revenue = float(result["total_user_payment_revenue"])
    feasible, total_violation, monthly_violation = constraint_status(result, min_total_demand, min_monthly_demand)
    if feasible:
        return revenue
    penalty_per_user = max(abs(baseline_revenue) / max(min_total_demand, 1.0), 1.0) * 10.0
    return revenue - penalty_per_user * (total_violation + monthly_violation)


def make_evaluation_record(
    context: FastForecastContext,
    base_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    vector: np.ndarray,
    phase: str,
    run_id: int,
    seed: int,
    min_total_demand: float,
    min_monthly_demand: Optional[np.ndarray],
    baseline_revenue: float,
) -> dict:
    params = params_from_vector(base_params, opt_variables, fixed_overrides, vector)
    result = evaluate_forecast_context(context, params, "Optimized pricing")
    feasible, total_violation, monthly_violation = constraint_status(result, min_total_demand, min_monthly_demand)
    score = penalized_score(result, min_total_demand, baseline_revenue, min_monthly_demand)
    return {
        "phase": phase,
        "run_id": run_id,
        "seed": seed,
        "vector": vector.copy(),
        "params": params,
        "result": result,
        "feasible": bool(feasible),
        "score": float(score),
        "revenue": float(result["total_user_payment_revenue"]),
        "demand": float(result["total_predicted_trips"]),
        "total_violation": float(total_violation),
        "monthly_violation": float(monthly_violation),
    }


def optimize_vectors(
    context: FastForecastContext,
    base_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    baseline: dict,
    min_total_demand: float,
    min_monthly_demand: Optional[np.ndarray],
    max_evaluations: int,
    seed: int,
    run_id: int,
    progress_callback: Optional[Callable[[dict, int, int, int], None]] = None,
) -> Tuple[dict, List[dict]]:
    """Run one randomized search seed with local coordinate refinement."""
    rng = np.random.default_rng(seed)
    lower = np.asarray([v["lower"] for v in opt_variables], dtype=float)
    upper = np.asarray([v["upper"] for v in opt_variables], dtype=float)
    current = np.asarray([v["current"] for v in opt_variables], dtype=float)
    current = np.clip(current, lower, upper)
    baseline_revenue = float(baseline["total_user_payment_revenue"])
    evaluations: List[dict] = []
    best_feasible = None
    best_penalized = None

    def evaluate_vector(vector: np.ndarray, phase: str) -> dict:
        nonlocal best_feasible, best_penalized
        record = make_evaluation_record(
            context,
            base_params,
            opt_variables,
            fixed_overrides,
            vector,
            phase,
            run_id,
            seed,
            min_total_demand,
            min_monthly_demand,
            baseline_revenue,
        )
        evaluations.append(record)
        if record["feasible"] and (best_feasible is None or record["revenue"] > best_feasible["revenue"]):
            best_feasible = record
        if best_penalized is None or record["score"] > best_penalized["score"]:
            best_penalized = record
        if progress_callback is not None:
            progress_callback(record, len(evaluations), max_evaluations, run_id)
        return record

    if len(opt_variables) == 0:
        vector = np.empty(0, dtype=float)
        evaluate_vector(vector, "fixed")
        return (best_feasible or best_penalized), evaluations

    max_evaluations = max(int(max_evaluations), 5)
    starts = [current, (lower + upper) / 2.0]
    for vector in starts:
        if len(evaluations) < max_evaluations:
            evaluate_vector(vector, "start")

    n_global = max(0, min(max_evaluations - len(evaluations), int(max_evaluations * 0.7)))
    unit = latin_hypercube(n_global, len(opt_variables), rng)
    for row in unit:
        vector = lower + row * (upper - lower)
        evaluate_vector(vector, "global")
        if len(evaluations) >= max_evaluations:
            break

    incumbent = best_feasible or best_penalized
    step = (upper - lower) * 0.20
    min_step = np.maximum((upper - lower) * 0.005, 1e-6)
    while len(evaluations) < max_evaluations and np.any(step > min_step):
        improved = False
        base_vector = incumbent["vector"].copy()
        for j in range(len(opt_variables)):
            if len(evaluations) >= max_evaluations:
                break
            for direction in (1.0, -1.0):
                if len(evaluations) >= max_evaluations:
                    break
                trial = base_vector.copy()
                trial[j] = np.clip(trial[j] + direction * step[j], lower[j], upper[j])
                if np.allclose(trial, base_vector):
                    continue
                record = evaluate_vector(trial, "local")
                candidate = best_feasible or best_penalized
                if candidate is not incumbent:
                    incumbent = candidate
                    improved = True
                    base_vector = incumbent["vector"].copy()
        if not improved:
            step *= 0.5
    return (best_feasible or best_penalized), evaluations


def variable_step(var: dict) -> float:
    param = str(var.get("param", ""))
    if var.get("kind") == "time_multiplier":
        return ROUNDING_INCREMENTS["time_multiplier"]
    return ROUNDING_INCREMENTS.get(param, max((float(var["upper"]) - float(var["lower"])) * 0.01, 1e-6))


def round_vector(vector: np.ndarray, opt_variables: List[dict]) -> np.ndarray:
    rounded = np.asarray(vector, dtype=float).copy()
    for i, var in enumerate(opt_variables):
        step = variable_step(var)
        if step > 0:
            rounded[i] = round(rounded[i] / step) * step
        rounded[i] = float(np.clip(rounded[i], float(var["lower"]), float(var["upper"])))
    return rounded


def choose_better_record(current_best: Optional[dict], candidate: Optional[dict]) -> Optional[dict]:
    if candidate is None:
        return current_best
    if current_best is None:
        return candidate
    if candidate["feasible"] and not current_best["feasible"]:
        return candidate
    if current_best["feasible"] and not candidate["feasible"]:
        return current_best
    key = "revenue" if candidate["feasible"] else "score"
    return candidate if candidate[key] > current_best[key] else current_best


def try_scipy_polish(
    context: FastForecastContext,
    base_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    start_vector: np.ndarray,
    run_id: int,
    seed: int,
    min_total_demand: float,
    min_monthly_demand: Optional[np.ndarray],
    baseline_revenue: float,
) -> Tuple[Optional[dict], dict]:
    """Optionally polish a candidate with SciPy SLSQP when SciPy is installed."""
    if len(opt_variables) == 0:
        return None, {"available": False, "used": False, "message": "No free optimization variables."}
    try:
        from scipy.optimize import minimize  # type: ignore
    except Exception:
        return None, {"available": False, "used": False, "message": "SciPy is not installed in this Python environment."}

    bounds = [(float(v["lower"]), float(v["upper"])) for v in opt_variables]
    cache = {}

    def cached_eval(x: np.ndarray) -> dict:
        key = tuple(np.round(np.asarray(x, dtype=float), 10))
        if key not in cache:
            cache[key] = make_evaluation_record(
                context,
                base_params,
                opt_variables,
                fixed_overrides,
                np.asarray(x, dtype=float),
                "scipy",
                run_id,
                seed,
                min_total_demand,
                min_monthly_demand,
                baseline_revenue,
            )
        return cache[key]

    def objective(x: np.ndarray) -> float:
        return -cached_eval(x)["revenue"]

    constraints = [
        {"type": "ineq", "fun": lambda x: cached_eval(x)["demand"] - min_total_demand}
    ]
    if min_monthly_demand is not None:
        for idx in range(len(min_monthly_demand)):
            constraints.append(
                {
                    "type": "ineq",
                    "fun": lambda x, i=idx: float(cached_eval(x)["result"]["monthly"]["predicted_trips"].iloc[i] - min_monthly_demand[i]),
                }
            )
    try:
        result = minimize(
            objective,
            np.asarray(start_vector, dtype=float),
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 120, "ftol": 1e-6, "disp": False},
        )
        record = cached_eval(result.x)
        record["phase"] = "scipy"
        status = {
            "available": True,
            "used": True,
            "success": bool(result.success),
            "message": str(result.message),
            "evaluations": int(result.nfev) if hasattr(result, "nfev") else len(cache),
        }
        return record, status
    except Exception as exc:
        return None, {"available": True, "used": True, "success": False, "message": str(exc), "evaluations": len(cache)}


def run_local_sensitivity(
    context: FastForecastContext,
    base_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    final_vector: np.ndarray,
    final_record: dict,
    min_total_demand: float,
    min_monthly_demand: Optional[np.ndarray],
    baseline_revenue: float,
) -> List[dict]:
    rows = []
    for idx, var in enumerate(opt_variables):
        step = variable_step(var)
        for direction, sign in [("up", 1.0), ("down", -1.0)]:
            trial = np.asarray(final_vector, dtype=float).copy()
            trial[idx] = float(np.clip(trial[idx] + sign * step, float(var["lower"]), float(var["upper"])))
            if np.isclose(trial[idx], final_vector[idx]):
                continue
            record = make_evaluation_record(
                context,
                base_params,
                opt_variables,
                fixed_overrides,
                trial,
                f"sensitivity_{direction}",
                0,
                0,
                min_total_demand,
                min_monthly_demand,
                baseline_revenue,
            )
            rows.append(
                {
                    "variable": var.get("label", var.get("param", "")),
                    "direction": direction,
                    "step": float(trial[idx] - final_vector[idx]),
                    "candidate_value": float(trial[idx]),
                    "revenue_change": float(record["revenue"] - final_record["revenue"]),
                    "demand_change": float(record["demand"] - final_record["demand"]),
                    "feasible": bool(record["feasible"]),
                    "total_violation": float(record["total_violation"]),
                    "monthly_violation": float(record["monthly_violation"]),
                }
            )
    return rows


def convergence_rows(evaluations: List[dict], baseline_revenue: float, max_rows: int = 160) -> List[dict]:
    rows = []
    best = None
    n = len(evaluations)
    stride = max(1, n // max_rows)
    for idx, record in enumerate(evaluations, start=1):
        if record["feasible"] and (best is None or record["revenue"] > best):
            best = record["revenue"]
        if idx == 1 or idx == n or idx % stride == 0 or record["phase"] in ("rounded", "scipy"):
            rows.append(
                {
                    "evaluation": idx,
                    "run_id": record.get("run_id", 0),
                    "seed": record.get("seed", 0),
                    "phase": record["phase"],
                    "feasible": bool(record["feasible"]),
                    "revenue": float(record["revenue"]),
                    "demand": float(record["demand"]),
                    "best_feasible_revenue": float(best) if best is not None else None,
                    "best_feasible_lift": float(best - baseline_revenue) if best is not None else None,
                }
            )
    return rows


def run_diagnostics(evaluations: List[dict], baseline_revenue: float, baseline_demand: float) -> List[dict]:
    rows = []
    run_ids = sorted({int(e["run_id"]) for e in evaluations if int(e.get("run_id", 0)) > 0})
    for run_id in run_ids:
        items = [e for e in evaluations if int(e.get("run_id", 0)) == run_id]
        feasible = [e for e in items if e["feasible"]]
        best = max(feasible, key=lambda e: e["revenue"]) if feasible else max(items, key=lambda e: e["score"])
        phases = {str(item.get("phase", "")) for item in items}
        if "scipy" in phases:
            stage = "SciPy polish"
        elif phases == {"fixed"}:
            stage = "Fixed pricing"
        else:
            stage = "Direct search"
        rows.append(
            {
                "run_id": run_id,
                "stage": stage,
                "seed": best.get("seed", 0),
                "evaluations": len(items),
                "feasible_evaluations": len(feasible),
                "best_feasible": bool(best["feasible"]),
                "best_revenue": float(best["revenue"]),
                "revenue_lift": float(best["revenue"] - baseline_revenue),
                "demand_change": float(best["demand"] - baseline_demand),
            }
        )
    return rows


def optimization_parameter_rows(
    base_params: Dict[str, dict],
    optimized_params: Dict[str, dict],
    opt_variables: List[dict],
    fixed_overrides: List[dict],
    plans: List[str],
) -> List[dict]:
    bounds = {}
    for var in opt_variables + fixed_overrides:
        if var.get("kind") == "time_multiplier":
            for target in var["targets"]:
                source = var.get("source_bounds", {}).get(target["param"], {})
                bounds[(target["plan"], target["param"])] = {
                    "lower": source.get("lower"),
                    "upper": source.get("upper"),
                    "group": var.get("label", "time-rate multiplier"),
                }
        else:
            bounds[(var["plan"], var["param"])] = {
                "lower": var.get("lower"),
                "upper": var.get("upper"),
                "group": "",
            }
    rows = []
    for plan in plans:
        for param in PRICING_PARAMETERS:
            current = float(base_params[plan][param])
            optimized = float(optimized_params[plan][param])
            bound = bounds.get((plan, param), {})
            rows.append(
                {
                    "plan": plan,
                    "param": param,
                    "selected": (plan, param) in bounds,
                    "current": current,
                    "optimized": optimized,
                    "change": optimized - current,
                    "lower": bound.get("lower"),
                    "upper": bound.get("upper"),
                    "group": bound.get("group", ""),
                }
            )
    return rows


def combine_optimization_product_summary(baseline: dict, optimized: dict) -> pd.DataFrame:
    base = baseline["product_summary"].rename(
        columns={
            "predicted_count": "baseline_positive_users",
            "predicted_trips": "baseline_predicted_trips",
            "predicted_user_payment_revenue": "baseline_user_payment_revenue",
            "expected_trip_count": "baseline_expected_trip_count",
            "expected_user_payment": "baseline_expected_user_payment",
        }
    )
    opt = optimized["product_summary"].rename(
        columns={
            "predicted_count": "optimized_positive_users",
            "predicted_trips": "optimized_predicted_trips",
            "predicted_user_payment_revenue": "optimized_user_payment_revenue",
            "expected_trip_count": "optimized_expected_trip_count",
            "expected_user_payment": "optimized_expected_user_payment",
        }
    )
    keep = ["plan", "count_bin", "product", "bin_sort"]
    product = base.merge(opt, on=keep, how="outer").fillna(0.0)
    product["change_positive_users"] = product["optimized_positive_users"] - product["baseline_positive_users"]
    product["change_predicted_trips"] = product["optimized_predicted_trips"] - product["baseline_predicted_trips"]
    product["change_user_payment_revenue"] = product["optimized_user_payment_revenue"] - product["baseline_user_payment_revenue"]
    return product.sort_values(["plan", "bin_sort"])


def run_optimization(config: dict, progress_callback: Optional[Callable[[dict], None]] = None) -> dict:
    """Run constrained price optimization and return dashboard-ready output."""
    started_at = time.perf_counter()
    segment = config.get("segment", "Regular")
    plans = SEGMENT_PLANS[segment]
    bins = bins_from_config(config)
    model_spec = model_spec_from_config(config, require_price=True)
    kernel_spec = kernel_spec_from_config(config)
    horizon = int(config.get("horizon_months", 12))
    market_multiplier = float(config.get("market_multiplier", config.get("market_size_multiplier", 1.0)))
    max_demand_drop = float(config.get("max_demand_drop", 0.05))
    max_demand_drop = max(0.0, min(max_demand_drop, 1.0))
    max_evaluations = int(config.get("max_evaluations", 500))
    seed = int(config.get("seed", 123))
    num_seeds = max(1, int(config.get("num_seeds", 3)))
    round_solution = bool(config.get("round_solution", False))
    enforce_monthly_demand = bool(config.get("enforce_monthly_demand", False))
    use_scipy_polish = bool(config.get("use_scipy_polish", True))
    direct_search_budget = int(max_evaluations * num_seeds)
    completed_direct_evaluations = 0
    feasible_direct_evaluations = 0

    def emit_progress(stage: str, **extra) -> None:
        if progress_callback is None:
            return
        payload = {
            "stage": stage,
            "elapsed_seconds": time.perf_counter() - started_at,
            "direct_search_budget": direct_search_budget,
            "completed_evaluations": completed_direct_evaluations,
            "feasible_evaluations": feasible_direct_evaluations,
            "num_seeds": num_seeds,
            "max_evaluations_per_seed": max_evaluations,
        }
        payload.update(extra)
        progress_callback(payload)

    emit_progress("setup", phase="Reading data")

    user_month_raw, trips_raw = read_inputs(segment)
    user_month, trips, excluded_latest = filter_latest(user_month_raw, trips_raw, bool(config.get("drop_latest", True)))
    months = sorted(user_month["YearMonth"].astype(str).unique())
    train_start = config.get("train_start", months[0])
    train_end = config.get("train_end", months[-1])
    train_months = choose_train_months(user_month, train_start, train_end)
    train_user_month = user_month[user_month["YearMonth"].isin(train_months)].copy()
    kernel_user_month_raw, kernel_trips_raw = read_all_inputs()
    kernel_user_month = kernel_user_month_raw[kernel_user_month_raw["YearMonth"].isin(train_months)].copy()
    kernel_trips = kernel_trips_raw[kernel_trips_raw["YearMonth"].isin(train_months)].copy()
    train_baskets = build_baskets(kernel_user_month, kernel_trips, bins=bins)

    observed_pricing = latest_pricing(segment, train_end=train_months[-1], drop_latest=bool(config.get("drop_latest", True)))
    base_params = observed_pricing["pricing"]
    opt_variables, fixed_overrides = selected_optimization_variables(config, base_params, plans)

    future_start = config.get("forecast_start") or month_add(train_months[-1], 1)
    future_months = [month_add(future_start, i) for i in range(horizon)]
    emit_progress("setup", phase="Estimating demand model")
    train_pricing = build_month_pricing(train_user_month, plans)
    train_costs, _train_kernel_diag = expected_costs(train_baskets, segment, train_pricing, train_months, plans, kernel_spec=kernel_spec, bins=bins)
    train_panel = build_share_panel(train_user_month, train_costs, plans, train_months, bins=bins)
    include_price = bool(model_spec.get("include_price", True))
    _train_pred, coef, train_r2 = estimate_predict(train_panel, train_months, include_price=include_price, model_spec=model_spec)
    emit_progress("setup", phase="Building forecast kernels")
    market_sizes = market_size_forecast(user_month, train_months, future_months, market_multiplier)
    context = make_forecast_template(train_baskets, segment, future_months, plans, market_sizes, coef, train_months, model_spec=model_spec, kernel_spec=kernel_spec, bins=bins)
    baseline = evaluate_forecast_context(context, base_params, "Baseline pricing")
    min_allowed_demand = baseline["total_predicted_trips"] * (1.0 - max_demand_drop)
    min_monthly_demand = None
    if enforce_monthly_demand:
        min_monthly_demand = baseline["monthly"]["predicted_trips"].to_numpy(dtype=float) * (1.0 - max_demand_drop)

    all_evaluations: List[dict] = []
    best_record = None

    def on_direct_evaluation(record: dict, run_evaluations: int, run_budget: int, run_id: int) -> None:
        nonlocal completed_direct_evaluations, feasible_direct_evaluations
        completed_direct_evaluations += 1
        if record["feasible"]:
            feasible_direct_evaluations += 1
        emit_progress(
            "direct_search",
            phase=record.get("phase", "direct"),
            current_run=run_id,
            current_seed=record.get("seed"),
            run_evaluations=run_evaluations,
            run_budget=run_budget,
            best_revenue=record.get("revenue"),
            best_feasible=bool(record.get("feasible")),
        )

    emit_progress("direct_search", phase="Starting direct search", current_run=1 if num_seeds else 0)
    for run_idx in range(num_seeds):
        run_seed = seed + run_idx
        run_best, run_evaluations = optimize_vectors(
            context,
            base_params,
            opt_variables,
            fixed_overrides,
            baseline,
            min_allowed_demand,
            min_monthly_demand,
            max_evaluations,
            run_seed,
            run_idx + 1,
            progress_callback=on_direct_evaluation,
        )
        all_evaluations.extend(run_evaluations)
        best_record = choose_better_record(best_record, run_best)

    scipy_status = {"available": False, "used": False, "message": "SciPy polish not requested."}
    if use_scipy_polish and best_record is not None:
        emit_progress("scipy", phase="SciPy polish", current_run=num_seeds + 1, current_seed=seed + num_seeds)
        scipy_record, scipy_status = try_scipy_polish(
            context,
            base_params,
            opt_variables,
            fixed_overrides,
            best_record["vector"],
            num_seeds + 1,
            seed + num_seeds,
            min_allowed_demand,
            min_monthly_demand,
            baseline["total_user_payment_revenue"],
        )
        if scipy_record is not None:
            all_evaluations.append(scipy_record)
            best_record = choose_better_record(best_record, scipy_record)
        emit_progress("scipy", phase="SciPy polish complete", scipy=scipy_status)

    if best_record is None:
        raise ValueError("Optimization failed before evaluating any candidate.")

    continuous_record = best_record
    rounded_record = None
    final_record = continuous_record
    rounding_status = {
        "requested": round_solution,
        "applied": False,
        "feasible": None,
        "message": "Rounding was not requested.",
    }
    if round_solution and len(opt_variables) > 0:
        rounded_vector = round_vector(continuous_record["vector"], opt_variables)
        rounded_record = make_evaluation_record(
            context,
            base_params,
            opt_variables,
            fixed_overrides,
            rounded_vector,
            "rounded",
            0,
            seed,
            min_allowed_demand,
            min_monthly_demand,
            baseline["total_user_payment_revenue"],
        )
        all_evaluations.append(rounded_record)
        if rounded_record["feasible"]:
            final_record = rounded_record
            rounding_status = {
                "requested": True,
                "applied": True,
                "feasible": True,
                "message": "Rounded solution is feasible and is reported as the optimized solution.",
            }
        else:
            rounding_status = {
                "requested": True,
                "applied": False,
                "feasible": False,
                "message": "Rounded solution violated the trip-volume constraint; reporting the unrounded feasible solution.",
            }

    optimized_params = final_record["params"]
    optimized = final_record["result"]
    monthly = pd.concat([baseline["monthly"], optimized["monthly"]], ignore_index=True)
    product_summary = combine_optimization_product_summary(baseline, optimized)
    price_coef, price_p = coefficient_value(coef, "expected_user_payment")
    warnings = warnings_from_model(price_coef, price_p, [], include_price=include_price)
    warnings.extend(warnings_from_kernel(pd.DataFrame(context.kernel_details), kernel_spec.get("min_support", MIN_SUPPORT)))
    if not final_record["feasible"]:
        warnings.append(
            {
                "level": "danger",
                "message": "No feasible optimized solution was found within the evaluation budget; showing the best penalized candidate.",
            }
        )
    if round_solution and rounded_record is not None and not rounded_record["feasible"]:
        warnings.append({"level": "warn", "message": rounding_status["message"]})
    if enforce_monthly_demand:
        warnings.append({"level": "good", "message": "Trip-volume drop constraint is enforced separately in every forecast month."})
    if scipy_status.get("used") and scipy_status.get("available"):
        level = "good" if scipy_status.get("success") else "warn"
        warnings.append({"level": level, "message": f"SciPy polish: {scipy_status.get('message')}"})
    feasible_count = int(sum(1 for e in all_evaluations if e["feasible"]))
    revenue_lift = optimized["total_user_payment_revenue"] - baseline["total_user_payment_revenue"]
    demand_change = optimized["total_predicted_trips"] - baseline["total_predicted_trips"]
    demand_drop_fraction = -demand_change / baseline["total_predicted_trips"] if baseline["total_predicted_trips"] else 0.0
    baseline_monthly = baseline["monthly"]
    optimized_monthly = optimized["monthly"]
    baseline_free_trips = float(baseline_monthly["predicted_free_plan_trips"].sum())
    optimized_free_trips = float(optimized_monthly["predicted_free_plan_trips"].sum())
    baseline_paid_trips = float(baseline_monthly["predicted_paid_plan_trips"].sum())
    optimized_paid_trips = float(optimized_monthly["predicted_paid_plan_trips"].sum())
    horizon_count = max(len(future_months), 1)
    sensitivity_rows = run_local_sensitivity(
        context,
        base_params,
        opt_variables,
        fixed_overrides,
        final_record["vector"],
        final_record,
        min_allowed_demand,
        min_monthly_demand,
        baseline["total_user_payment_revenue"],
    )
    elapsed_seconds = time.perf_counter() - started_at
    return json_safe(
        {
            "config": {
                "segment": segment,
                "train_start": train_months[0],
                "train_end": train_months[-1],
                "forecast_start": future_months[0],
                "forecast_end": future_months[-1],
                "horizon_months": horizon,
                "market_multiplier": market_multiplier,
                "max_demand_drop": max_demand_drop,
                "max_evaluations": max_evaluations,
                "num_seeds": num_seeds,
                "seed": seed,
                "round_solution": round_solution,
                "enforce_monthly_demand": enforce_monthly_demand,
                "tie_time_rates": bool(config.get("tie_time_rates", False)),
                "use_scipy_polish": use_scipy_polish,
                "excluded_latest_month": excluded_latest,
            },
            "segment": segment,
            "plans": plans,
            "model_spec": model_spec_for_dashboard(model_spec, plans),
            "binning": binning_for_dashboard(bins),
            "kernel_rule": kernel_rule_for_dashboard(kernel_spec),
            "train_r2": train_r2,
            "fit_summary": fit_summary_for_dashboard(train_panel, train_months, coef, train_r2),
            "price_coefficient": price_coef,
            "price_p_value": price_p,
            "coefficients": coefficients_for_dashboard(coef),
            "warnings": warnings,
            "optimization_status": {
                "evaluations": len(all_evaluations),
                "direct_search_budget": direct_search_budget,
                "direct_search_evaluations": completed_direct_evaluations,
                "direct_search_feasible_evaluations": feasible_direct_evaluations,
                "feasible_evaluations": feasible_count,
                "selected_variables": len(opt_variables),
                "fixed_overrides": len(fixed_overrides),
                "best_feasible": bool(final_record["feasible"]),
                "solver": "multi-seed direct search + coordinate refinement",
                "procedure": "Evaluate current and midpoint starts, sample a Latin-hypercube global search for each seed, refine the best candidate by coordinate moves, then optionally run SciPy SLSQP polish.",
                "elapsed_seconds": elapsed_seconds,
                "scipy": scipy_status,
                "rounding": rounding_status,
            },
            "constraint": {
                "max_demand_drop": max_demand_drop,
                "constraint_quantity": "predicted trips",
                "baseline_predicted_trips": baseline["total_predicted_trips"],
                "minimum_allowed_predicted_trips": min_allowed_demand,
                "optimized_predicted_trips": optimized["total_predicted_trips"],
                "trip_change": demand_change,
                "trip_drop_fraction": demand_drop_fraction,
                "baseline_inside_demand": baseline["total_inside_demand"],
                "optimized_inside_demand": optimized["total_inside_demand"],
                "demand_change": demand_change,
                "demand_drop_fraction": demand_drop_fraction,
                "monthly_constraint_enforced": enforce_monthly_demand,
                "max_monthly_violation": final_record["monthly_violation"],
            },
            "summary": {
                "baseline_user_payment_revenue": baseline["total_user_payment_revenue"],
                "optimized_user_payment_revenue": optimized["total_user_payment_revenue"],
                "revenue_lift": revenue_lift,
                "revenue_lift_percent": revenue_lift / baseline["total_user_payment_revenue"] if baseline["total_user_payment_revenue"] else None,
                "baseline_inside_demand": baseline["total_inside_demand"],
                "optimized_inside_demand": optimized["total_inside_demand"],
                "baseline_predicted_trips": baseline["total_predicted_trips"],
                "optimized_predicted_trips": optimized["total_predicted_trips"],
                "trip_change": demand_change,
                "baseline_free_plan_trips": baseline_free_trips,
                "optimized_free_plan_trips": optimized_free_trips,
                "free_plan_trip_change": optimized_free_trips - baseline_free_trips,
                "baseline_paid_plan_trips": baseline_paid_trips,
                "optimized_paid_plan_trips": optimized_paid_trips,
                "paid_plan_trip_change": optimized_paid_trips - baseline_paid_trips,
                "baseline_avg_monthly_trips": baseline["total_predicted_trips"] / horizon_count,
                "optimized_avg_monthly_trips": optimized["total_predicted_trips"] / horizon_count,
                "avg_monthly_trip_change": demand_change / horizon_count,
                "baseline_avg_monthly_free_plan_trips": baseline_free_trips / horizon_count,
                "optimized_avg_monthly_free_plan_trips": optimized_free_trips / horizon_count,
                "avg_monthly_free_plan_trip_change": (optimized_free_trips - baseline_free_trips) / horizon_count,
                "baseline_avg_monthly_paid_plan_trips": baseline_paid_trips / horizon_count,
                "optimized_avg_monthly_paid_plan_trips": optimized_paid_trips / horizon_count,
                "avg_monthly_paid_plan_trip_change": (optimized_paid_trips - baseline_paid_trips) / horizon_count,
                "trip_drop_fraction": demand_drop_fraction,
                "demand_change": demand_change,
                "baseline_no_use": baseline["total_no_use"],
                "optimized_no_use": optimized["total_no_use"],
                "continuous_user_payment_revenue": continuous_record["revenue"],
                "continuous_predicted_trips": continuous_record["demand"],
                "continuous_inside_demand": continuous_record["result"]["total_inside_demand"],
                "rounded_user_payment_revenue": rounded_record["revenue"] if rounded_record is not None else None,
                "rounded_predicted_trips": rounded_record["demand"] if rounded_record is not None else None,
                "rounded_inside_demand": rounded_record["result"]["total_inside_demand"] if rounded_record is not None else None,
            },
            "pricing": {
                "baseline": base_params,
                "optimized": optimized_params,
            },
            "parameter_rows": optimization_parameter_rows(base_params, optimized_params, opt_variables, fixed_overrides, plans),
            "monthly": monthly.to_dict("records"),
            "product_summary": product_summary.to_dict("records"),
            "run_diagnostics": run_diagnostics(all_evaluations, baseline["total_user_payment_revenue"], baseline["total_predicted_trips"]),
            "convergence": convergence_rows(all_evaluations, baseline["total_user_payment_revenue"]),
            "sensitivity": sensitivity_rows,
            "kernel_summary": kernel_summary_for_dashboard(pd.DataFrame(context.kernel_details)),
            "kernel_details": context.kernel_details,
        }
    )
