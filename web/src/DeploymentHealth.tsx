import { useEffect, useState } from "react";

import { fetchHealth, HealthError, type Health } from "./api/health";
import "./DeploymentHealth.css";

type State =
  | { readonly kind: "checking" }
  | { readonly kind: "answered"; readonly health: Health }
  | { readonly kind: "error"; readonly message: string };

const UNREACHABLE = "The Deployment could not be reached";

/** Whether the Deployment is ready to serve, as its api reports it once the page opens. */
export function DeploymentHealth() {
  const [state, setState] = useState<State>({ kind: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal).then(
      (health) => {
        setState({ kind: "answered", health });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message: error instanceof HealthError ? error.message : UNREACHABLE,
        });
      },
    );
    return () => {
      controller.abort();
    };
  }, []);

  return (
    <section aria-labelledby="deployment-health">
      <h1 id="deployment-health">Deployment health</h1>
      <div role="status" aria-live="polite">
        <Report state={state} />
      </div>
    </section>
  );
}

// Each state says what it is in words and with its own mark, so none depends on colour.
function Report({ state }: { readonly state: State }) {
  switch (state.kind) {
    case "checking":
      return (
        <p className="report" data-state="checking">
          <span aria-hidden="true">… </span>Checking…
        </p>
      );
    case "error":
      return (
        <p className="report" data-state="error">
          <span aria-hidden="true">! </span>
          {state.message}
        </p>
      );
    case "answered": {
      const { status, database } = state.health;
      const healthy = status === "healthy";
      return (
        <div className="report" data-state={status}>
          <p>
            <span aria-hidden="true">{healthy ? "✓ " : "✗ "}</span>
            {healthy ? "Healthy" : "Unhealthy"}
          </p>
          <p>
            {database.problem === null
              ? `Database ${database.status}`
              : `Database ${database.status}: ${database.problem}`}
          </p>
        </div>
      );
    }
  }
}
