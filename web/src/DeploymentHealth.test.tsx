import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DeploymentHealth } from "./DeploymentHealth";
import type { Health } from "./api/health";

function answer(status: number, health: Health) {
  const fetch = vi.fn(() => Promise.resolve(Response.json(health, { status })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("DeploymentHealth", () => {
  it("asks the api for /health and says the Deployment is healthy", async () => {
    const fetch = answer(200, {
      status: "healthy",
      database: { status: "connected", problem: null },
    });

    render(<DeploymentHealth />);

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("Database connected")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/health", expect.anything());
  });

  it("says why the database cannot be reached when the api answers 503", async () => {
    answer(503, {
      status: "unhealthy",
      database: { status: "unreachable", problem: "authentication failed" },
    });

    render(<DeploymentHealth />);

    expect(await screen.findByText("Unhealthy")).toBeInTheDocument();
    expect(screen.getByText("Database unreachable: authentication failed")).toBeInTheDocument();
  });

  it("says so when the api itself cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    render(<DeploymentHealth />);

    expect(await screen.findByText("The api could not be reached")).toBeInTheDocument();
  });

  it("does not take an answer it cannot read for a health report", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("Bad Gateway", { status: 502 }))),
    );

    render(<DeploymentHealth />);

    expect(await screen.findByText("The api could not be reached")).toBeInTheDocument();
  });
});
