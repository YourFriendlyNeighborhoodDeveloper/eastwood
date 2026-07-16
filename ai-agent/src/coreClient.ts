import { loadConfig } from "./config.js";
import { logger } from "./observability.js";

let warnedMissingKey = false;

/**
 * Authenticated fetch against eastwood-core.
 *
 * Every operational /api route on core requires `Authorization: Bearer
 * <EASTWOOD_API_KEY>`; attaching it here — and only here — keeps tool handlers
 * free of auth plumbing. A missing key is not attached (core rejects with 401
 * and the calling tool fails honestly); the key itself is never logged.
 */
export async function coreFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const cfg = loadConfig();
  const headers = new Headers(init.headers);
  if (cfg.eastwoodApiKey) {
    headers.set("Authorization", `Bearer ${cfg.eastwoodApiKey}`);
  } else if (!warnedMissingKey) {
    warnedMissingKey = true;
    logger.warn(
      "EASTWOOD_API_KEY is not set — calls to eastwood-core's authenticated /api routes will be refused"
    );
  }
  return fetch(`${cfg.eastwoodCoreBaseUrl}${path}`, { ...init, headers });
}
