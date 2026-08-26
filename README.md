# EVIE Demand Dashboard Folder

This folder contains the local EVIE demand dashboard, the demand-estimation
engine, the current model input files, and the scripts/documents needed to
refresh those inputs when new raw data are available.

## Folder Contents

- `app.py` - local dashboard server and API routes.
- `data/` - the four CSV input files the dashboard reads.
- `demand_engine.py` - demand estimation, empirical kernels, forecasting, and
  pricing optimization logic.
- `RUN_DASHBOARD.py` - launches the dashboard.
- `static/` - browser interface files used by the local dashboard.
- `DataPrep/` - scripts for creating the dashboard input CSVs from raw company
  trip and invoice exports.
- `docs/` - methodology, data creation, and dashboard use documentation.
- `requirements.txt` - Python package requirements.
- `r2_sync.py` - pulls the input files from R2 in the deployed container; a
  no-op locally.
- `worker/`, `wrangler.jsonc`, `Dockerfile` - the Cloudflare deployment. See
  [Deploying To Cloudflare](#deploying-to-cloudflare).

## Required Dashboard Input Files

The dashboard reads these four CSV files from the `data/` folder:

- `data/clean_trips_with_invoice_plan_and_segment.csv`
- `data/user_month_regular.csv`
- `data/user_month_student.csv`
- `data/user_month_access.csv`

Current copies are included. To refresh them with new raw data, use the scripts
in `DataPrep/` and follow `docs/DataCreationGuide.md`.

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

## Deploying To Cloudflare

The same app also runs on Cloudflare, so analysts can use it without a local
Python setup. Three pieces:

- **Workers** serves `static/` directly and forwards `/api/*` to the container.
- **R2** holds the four input CSVs. They are too big for the image and contain
  per-user data, so they are never baked in.
- **Containers** runs `app.py` unchanged, starting on demand and sleeping after
  30 minutes idle.

Nothing about the local workflow changes: `r2_sync.py` no-ops when the `R2_*`
variables are unset, so `python app.py` still reads `data/` off disk.

### Before Anything Reaches The Internet

These files are per-user trip and invoice records. Put **Cloudflare Access** in
front of the deployed hostname, restricted to the staff who should see them,
before sharing the URL. The Worker itself does no authentication.

### One-Time Setup

1. Create the R2 bucket (name it `pricing-engine`), then create an R2 API token
   with Object Read & Write on it.
2. Copy `.env.example` to `.env` and fill in the four `R2_*` values.
3. Upload the current input files:

   ```powershell
   python r2_sync.py push
   ```

4. Give the Worker the same credentials as secrets:

   ```powershell
   npx wrangler secret put R2_ENDPOINT
   npx wrangler secret put R2_BUCKET
   npx wrangler secret put R2_ACCESS_KEY_ID
   npx wrangler secret put R2_SECRET_ACCESS_KEY
   ```

### Deploying

```powershell
npm install
npx wrangler deploy
```

After a DataPrep refresh, re-run `python r2_sync.py push`. The container picks
the new files up the next time it starts cold; restart it to force that.

### What To Expect

The first request after the container has been idle takes about 50 seconds --
it pulls ~410MB from R2 and parses it. Requests after that are immediate,
because the parsed data stays in the process. Optimization runs already use the
job-and-poll endpoints, so they are not affected by any request timeout.

If optimization wall-clock becomes the complaint, raise `instance_type` in
`wrangler.jsonc` from `standard-3` to `standard-4`.

## Documentation

The detailed documentation is in `docs/`:

- [`MethodologyGuide.md`](docs/MethodologyGuide.md)
- [`DataCreationGuide.md`](docs/DataCreationGuide.md)
- [`DashBoardGuide.md`](docs/DashBoardGuide.md)

