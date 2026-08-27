# Evie Demand Dashboard Cloudflare Deployment

The same app that runs locally also runs on Cloudflare, so analysts can use it
without a local Python setup. Three pieces:

- **Workers** serves `static/` directly and forwards `/api/*` to the container.
- **R2** holds the four input CSVs. They are too big for the image and contain
  per-user data, so they are never baked in.
- **Containers** runs `app.py` unchanged, starting on demand and sleeping after
  30 minutes idle.

Nothing about the local workflow changes: `r2_sync.py` no-ops when the `R2_*`
variables are unset, so `python app.py` still reads `data/` off disk.

## Before Anything Reaches The Internet

These files are per-user trip and invoice records. Put **Cloudflare Access** in
front of the deployed hostname, restricted to the staff who should see them,
before sharing the URL. The Worker itself does no authentication.

## One-Time Setup

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

## Deploying

```powershell
npm install
npx wrangler deploy
```

After a data_prep refresh, re-run `python r2_sync.py push`. The container picks
the new files up the next time it starts cold; restart it to force that.

## What To Expect

The first request after the container has been idle takes about 50 seconds --
it pulls ~410MB from R2 and parses it. Requests after that are immediate,
because the parsed data stays in the process. Optimization runs already use the
job-and-poll endpoints, so they are not affected by any request timeout.

If optimization wall-clock becomes the complaint, raise `instance_type` in
`wrangler.jsonc` from `standard-3` to `standard-4`.
