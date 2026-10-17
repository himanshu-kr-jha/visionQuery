import type { operations } from "./schema";

type Answers = operations["health"]["responses"];

/** The api's health report, as it answers both when healthy (200) and when not (503). */
export type Health = Answers[200 | 503]["content"]["application/json"];

const ANSWERED = new Set([200, 503]);

/** Ask the api whether the Deployment can serve; rejects when no health report came back. */
export async function fetchHealth(signal: AbortSignal): Promise<Health> {
  const response = await fetch("/health", { signal, headers: { Accept: "application/json" } });
  if (!ANSWERED.has(response.status)) {
    throw new Error(`/health answered ${String(response.status)}`);
  }
  return (await response.json()) as Health;
}
