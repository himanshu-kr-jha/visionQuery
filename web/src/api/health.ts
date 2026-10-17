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
    /** "checking" while the api has not finished its first look: nothing is known yet. */
    readonly status: DatabaseStatus;
    readonly problem: string | null;
  };
}

type DatabaseStatus = "connected" | "unreachable" | "checking";

// Compile-time only: fails the type check when the api's report stops fitting what is read.
export const generatedFitsWhatIsRead = (health: GeneratedHealth): Health => health;

/** Why no health report could be shown, in words for the Operator. */
export class HealthError extends Error {}

const ANSWERED = new Set([200, 503]);
const NOT_A_REPORT = "The Deployment answered with something that is not a health report";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * How long the api has to answer before the page stops waiting and says it cannot be reached.
 * Above the api's own deadline for looking at the database (`DEADLINE_SECONDS` in
 * `visionquery.api.probe`, 5 s), so a slow database is reported as such, not as an api that
 * cannot be reached; tests/test_web.py keeps the two apart.
 */
export const FETCH_TIMEOUT_MS = 8000;

/** How long the page waits before asking again when the api has not finished looking yet. */
export const CHECK_AGAIN_MS = 1000;

function agrees(
  httpStatus: number,
  status: "healthy" | "unhealthy",
  databaseStatus: DatabaseStatus,
  problem: string | null,
): boolean {
  if (status === "healthy") {
    return httpStatus === 200 && databaseStatus === "connected" && problem === null;
  }
  // Unhealthy: the database cannot be reached, and why, or nothing is known about it yet.
  return (
    httpStatus === 503 &&
    ((databaseStatus === "unreachable" && problem !== null) ||
      (databaseStatus === "checking" && problem === null))
  );
}

/**
 * The body as a `Health`, or a `HealthError` saying it is not one. The HTTP status must agree
 * with the body: 200 is a healthy report with a connected database and no problem, 503 an
 * unhealthy one whose database is unreachable for a named problem or still being checked, so a
 * report that contradicts its own status is not trusted either way.
 */
export function parseHealth(body: unknown, httpStatus: number): Health {
  if (isRecord(body) && isRecord(body["database"])) {
    const status = body["status"];
    const database = body["database"];
    const databaseStatus = database["status"];
    const problem = database["problem"];
    if (
      (status === "healthy" || status === "unhealthy") &&
      (databaseStatus === "connected" ||
        databaseStatus === "unreachable" ||
        databaseStatus === "checking") &&
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
