import { useEffect, useState } from "react";

import { fetchHealth, type Health } from "./api/health";

type State =
  | { readonly kind: "checking" }
  | { readonly kind: "answered"; readonly health: Health }
  | { readonly kind: "unreachable" };

/** Whether the Deployment can serve, as its api reports it once the page opens. */
export function DeploymentHealth() {
  const [state, setState] = useState<State>({ kind: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal).then(
      (health) => {
        setState({ kind: "answered", health });
      },
      () => {
        if (!controller.signal.aborted) setState({ kind: "unreachable" });
      },
    );
    return () => {
      controller.abort();
    };
  }, []);

  return (
    <section aria-labelledby="deployment-health">
      <h1 id="deployment-health">Deployment health</h1>
      <div role="status">
        <Report state={state} />
      </div>
    </section>
  );
}

function Report({ state }: { readonly state: State }) {
  switch (state.kind) {
    case "checking":
      return <p>Checking…</p>;
    case "unreachable":
      return <p>The api could not be reached</p>;
    case "answered": {
      const { status, database } = state.health;
      return (
        <>
          <p>{status === "healthy" ? "Healthy" : "Unhealthy"}</p>
          <p>
            {database.problem === null
              ? `Database ${database.status}`
              : `Database ${database.status}: ${database.problem}`}
          </p>
        </>
      );
    }
  }
}
