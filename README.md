# Evie Demand Dashboard Folder

This folder contains the local Evie demand dashboard, the demand-estimation
engine, the current model input files, and the scripts/documents needed to
refresh those inputs when new raw data are available.

## Folder Contents

- `app.py` - local dashboard server and API routes.
- `data/` - the four CSV input files the dashboard reads.
- `demand_engine.py` - demand estimation, empirical kernels, forecasting, and
  pricing optimization logic.
- `run_dashboard.py` - launches the dashboard.
- `static/` - browser interface files used by the local dashboard.
- `data_prep/` - scripts for creating the dashboard input CSVs from raw company
  trip and invoice exports.
- `docs/` - methodology, data creation, and dashboard use documentation.
- `requirements.txt` - Python package requirements.
- `r2_sync.py` - pulls the input files from R2 in the deployed container; a
  no-op locally.
- `worker/`, `wrangler.jsonc`, `Dockerfile` - the Cloudflare deployment. See
  [`docs/cloudflare-deployment.md`](docs/cloudflare-deployment.md).

## Required Dashboard Input Files

The dashboard reads these four CSV files from the `data/` folder:

- `data/clean_trips_with_invoice_plan_and_segment.csv`
- `data/user_month_regular.csv`
- `data/user_month_student.csv`
- `data/user_month_access.csv`

Current copies are included. To refresh them with new raw data, use the scripts
in `data_prep/` and follow `docs/data-creation-guide.md`.

## Python Setup

Use Python 3.10 or newer. From this folder, install the required packages:

```powershell
python -m pip install -r requirements.txt
```

## Running The Dashboard

From this folder:

```powershell
python app.py
```

Then open:

```text
http://127.0.0.1:8766
```

Run this way the dashboard is entirely local. It does not upload data or call
external services.

## Documentation

The detailed documentation is in `docs/`:

- [`methodology-guide.md`](docs/methodology-guide.md) - the demand model and
  pricing optimization method.
- [`data-creation-guide.md`](docs/data-creation-guide.md) - rebuilding the input
  CSVs from raw exports.
- [`dashboard-guide.md`](docs/dashboard-guide.md) - using the dashboard.
- [`cloudflare-deployment.md`](docs/cloudflare-deployment.md) - running it on
  Cloudflare instead of locally.

