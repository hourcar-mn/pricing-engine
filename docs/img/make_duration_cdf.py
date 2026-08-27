"""Regenerate docs/img/trip-duration-cdf.svg from the clean trip file.

Run from the repo root after a data refresh:

    python docs/img/make_duration_cdf.py

This is a documentation-build script, not part of the dashboard. It needs
matplotlib, which is intentionally not in requirements.txt because the
dashboard itself does not use it.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "path"  # outline text so GitHub renders it

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

IMG_DIR = Path(__file__).resolve().parent
TRIP_CSV = IMG_DIR.parent.parent / "data" / "clean_trips_with_invoice_plan_and_segment.csv"
OUTPUT_SVG = IMG_DIR / "trip-duration-cdf.svg"

MODELED_SEGMENTS = ["Regular", "Access", "Student"]
MAX_MINUTES = 480
COLORS = ["#1f7a7a", "#d64545", "#4a4ab0", "#2e7d32", "#e08a1e", "#8e44ad"]

trips = pd.read_csv(
    TRIP_CSV,
    usecols=["UserSegment", "TripStartDT", "TripDurationMinutes",
             "FreePlanPricingAtTrip"],
)
trips = trips[trips["UserSegment"].isin(MODELED_SEGMENTS)]

# Regime label lives inside the posted-pricing JSON; special values are plain
# strings such as UNKNOWN_POSTED_PRICE and carry no regime.
priced = trips["FreePlanPricingAtTrip"].str.startswith("{")
trips = trips[priced].copy()
trips["Regime"] = trips["FreePlanPricingAtTrip"].map(
    lambda s: json.loads(s)["regime"])

fig, ax = plt.subplots(figsize=(8.0, 4.9), dpi=100)

# Evaluate the CDF on a one-minute grid rather than at every trip, so the SVG
# stays small instead of carrying one vertex per trip.
grid = np.arange(0, MAX_MINUTES + 1)

# Order regimes chronologically, not alphabetically, so the legend reads in
# the order the prices were actually posted.
regime_order = (trips.groupby("Regime")["TripStartDT"].min()
                .sort_values().index.tolist())

for color, regime in zip(COLORS, regime_order):
    group = trips[trips["Regime"] == regime]
    durations = np.sort(group["TripDurationMinutes"].to_numpy())
    cdf = np.searchsorted(durations, grid, side="right") / durations.size
    assert np.all(np.diff(cdf) >= 0) and cdf[0] == 0.0 and cdf[-1] <= 1.0
    ax.plot(grid, cdf, color=color, linewidth=1.4,
            label=f"{regime} (n={durations.size:,})")

ax.set_xlim(0, MAX_MINUTES)
ax.set_ylim(0, 1.02)
ax.set_xticks(np.arange(0, MAX_MINUTES + 1, 60))
ax.set_yticks([0.0, 0.25, 0.50, 0.75, 1.00])
ax.set_xlabel("Trip duration (minutes)", fontsize=9)
ax.set_ylabel("Empirical CDF", fontsize=9)
ax.set_title("Empirical Trip-Duration CDF by Pricing Regime",
             fontsize=11, fontweight="bold", loc="left", pad=24)
ax.text(0, 1.015, "Modeled segments only; x-axis shown through "
        f"{MAX_MINUTES} minutes", transform=ax.transAxes, fontsize=8,
        color="#555555", va="bottom")
ax.grid(True, color="#dddddd", linewidth=0.6)
ax.set_axisbelow(True)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
ax.legend(fontsize=8, frameon=False, loc="center left",
          bbox_to_anchor=(1.01, 0.75))

fig.tight_layout()
OUTPUT_SVG.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUTPUT_SVG, format="svg", bbox_inches="tight", facecolor="white")
print(f"Wrote: {OUTPUT_SVG}  regimes: {trips['Regime'].nunique()}  "
      f"trips: {len(trips):,}")
