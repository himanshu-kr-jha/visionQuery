import type { operations } from "./schema";

type Answers = operations["health"]["responses"];

/** The api's health report, as the generated types describe it. */
type GeneratedHealth = Answers[200 | 503]["content"]["application/json"];

/**
 * The report as the page reads it, checked at runtime. `problem` is a string here, where the
 * generated type has the api's fixed categories: the page shows it as it came, so a category
 * added to the api later still shows.
 */
export interface Health {
  readonly status: "healthy" | "unhealthy";
  readonly database: {
    readonly status: "connected" | "unreachable";
    readonly problem: string | null;
  };
}

// Compile-time only: fails the type check when the api's report stops fitting what is read.
export const generatedFitsWhatIsRead = (health: GeneratedHealth): Health => health;

/** Why no health report could be shown, in words for the Operator. */
export class HealthError extends Error {}

const ANSWERED = new Set([200, 503]);
const NOT_A_REPORT = "The Deployment answered with something that is not a health report";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** How long the api has to answer before the page stops waiting and says it cannot be reached. */
export const FETCH_TIMEOUT_MS = 5000;

function agrees(
  httpStatus: number,
  status: "healthy" | "unhealthy",
  databaseStatus: "connected" | "unreachable",
  problem: string | null,
): boolean {
  if (status === "healthy") {
    return httpStatus === 200 && databaseStatus === "connected" && problem === null;
  }
  return httpStatus === 503;
}

/**
 * The body as a `Health`, or a `HealthError` saying it is not one. The HTTP status must agree
 * with the body: 200 is a healthy report with a connected database and no problem, 503 an
 * unhealthy one, so a report that contradicts its own status is not trusted either way.
 */
export function parseHealth(body: unknown, httpStatus: number): Health {
  if (isRecord(body) && isRecord(body["database"])) {
    const status = body["status"];
    const database = body["database"];
    const databaseStatus = database["status"];
    const problem = database["problem"];
    if (
      (status === "healthy" || status === "unhealthy") &&
      (databaseStatus === "connected" || databaseStatus === "unreachable") &&
      (problem === null || typeof problem === "string") &&
      agrees(httpStatus, status, databaseStatus, problem)
    ) {
      return { status, database: { status: databaseStatus, problem } };
    }
  }
  throw new HealthError(NOT_A_REPORT);
}

/**
 * Ask the api whether the Deployment is ready to serve. Rejects with a `HealthError` when no
 * health report came back, and with the network's own error when the api could not be reached.
 */
export async function fetchHealth(signal: AbortSignal): Promise<Health> {
  // The page's own signal (it is leaving) or the deadline (the api is stalled) ends the request.
  const response = await fetch("/health", {
    signal: AbortSignal.any([signal, AbortSignal.timeout(FETCH_TIMEOUT_MS)]),
    headers: { Accept: "application/json" },
  });
  if (!ANSWERED.has(response.status)) {
    throw new HealthError("The Deployment could not be reached");
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new HealthError(NOT_A_REPORT);
  }
  return parseHealth(body, response.status);
}
