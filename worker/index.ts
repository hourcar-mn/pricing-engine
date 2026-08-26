import { Container } from "@cloudflare/containers";

export interface Env {
  ENGINE: DurableObjectNamespace<EngineContainer>;

  R2_ENDPOINT: string;
  R2_BUCKET: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
}

/**
 * The dashboard API (app.py), one shared instance.
 *
 * One instance, not one per user, on purpose: optimization job state lives in
 * app.py's in-memory OPTIMIZATION_JOBS dict, so the poll for a job has to reach
 * the same process that started it. max_instances is 1 in wrangler.jsonc for
 * the same reason.
 */
export class EngineContainer extends Container<Env> {
  defaultPort = 8000;

  // The four CSVs take ~25s to pull and parse, and the parsed frames then sit
  // in the process (demand_engine caches them). Staying up through an analyst's
  // idle gap is cheaper than making the next click pay that again.
  sleepAfter = "30m";

  // R2 is reached over its public S3 endpoint, not a binding -- see r2_sync.py
  enableInternet = true;

  // answered without touching the data files, so the container reports ready
  // immediately and only the first real request waits on the R2 pull
  pingEndpoint = "/health";

  envVars = {
    R2_ENDPOINT: this.env.R2_ENDPOINT,
    R2_BUCKET: this.env.R2_BUCKET,
    R2_ACCESS_KEY_ID: this.env.R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY: this.env.R2_SECRET_ACCESS_KEY,
  };
}

export default {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);

    // Only /api/* reaches the Worker (run_worker_first in wrangler.jsonc);
    // static/ is served straight from assets. app.py already owns the /api
    // prefix, so the path goes through untouched.
    return env.ENGINE.getByName("engine").fetch(
      new Request(`http://container${url.pathname}${url.search}`, request)
    );
  },
};
