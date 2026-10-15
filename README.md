# Evie pricing engine

A demand model and pricing dashboard built for Evie, HOURCAR's electric
carshare service. It estimates how members respond to changes in trip rates,
monthly fees and plan structure, forecasts trips and revenue under a proposed
tariff, and searches for prices that maximize revenue while keeping trip volume
within set bounds.

HOURCAR has shut down, so this repository is archived and no longer maintained.
It is published in case the approach is useful to other carshare operators or
researchers.

## What's here

`demand_engine.py` is the model: demand estimation, forecasting and price
optimization. `app.py` serves a small local dashboard on top of it (the
frontend is in `static/`) with three tabs: model validation, what-if pricing
and optimization.

`data_prep/` turns raw trip and invoice exports into the CSVs the model reads.
`worker/`, `wrangler.jsonc` and the `Dockerfile` are what we used to host the
dashboard on Cloudflare.

The method is written up in [`docs/methodology-guide.md`](docs/methodology-guide.md).
The other guides cover [building the input data](docs/data-creation-guide.md),
[using the dashboard](docs/dashboard-guide.md) and the
[Cloudflare deployment](docs/cloudflare-deployment.md).

## Data

No data is included. The inputs are per-member trip and billing records, so
they stay private. To run the model on your own data, produce these four files
in `data/` (the data creation guide describes the columns):

- `clean_trips_with_invoice_plan_and_segment.csv`
- `user_month_regular.csv`
- `user_month_student.csv`
- `user_month_access.csv`

## Running it

Needs Python 3.10 or newer.

```
python -m pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:8766. Everything runs locally.

## License

The code is MIT licensed, see [`LICENSE`](LICENSE).

HOURCAR, Evie, the Evie logo (`static/evie-logo.png`) and the favicons are
trademarks of HOURCAR and are not covered by that license. Don't use them to
brand your own product or deployment, or in a way that suggests HOURCAR
endorses it. If you reuse the code, swap in your own branding.
