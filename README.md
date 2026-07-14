# EVIE Demand Dashboard Folder

This folder contains the local EVIE demand dashboard, the demand-estimation
engine, the current model input files, and the scripts/documents needed to
refresh those inputs when new raw data are available.

## Folder Contents

- `app.py` - local dashboard server and API routes.
- `demand_engine.py` - demand estimation, empirical kernels, forecasting, and
  pricing optimization logic.
- `RUN_DASHBOARD_IN_SPYDER.py` - optional launcher for Spyder users.
- `static/` - browser interface files used by the local dashboard.
- `DataPrep/` - scripts for creating the dashboard input CSVs from raw company
  trip and invoice exports.
- `docs/` - methodology, data creation, and dashboard use documentation.
- `requirements.txt` - Python package requirements.

## Required Dashboard Input Files

The dashboard reads these four CSV files from this folder:

- `clean_trips_with_invoice_plan_and_segment.csv`
- `user_month_regular.csv`
- `user_month_student.csv`
- `user_month_access.csv`

Current copies are included. To refresh them with new raw data, use the scripts
in `DataPrep/` and follow `docs/EVIE_Data_Creation_Guide.tex`.

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

The dashboard runs locally on the user's machine. It does not upload data or
call external services.

## Documentation

The detailed documentation is in `docs/`:

- `EVIE_Demand_Estimation_Methodology_Company.tex`
- `EVIE_Data_Creation_Guide.tex`
- `EVIE_Dashboard_User_Guide.tex`

