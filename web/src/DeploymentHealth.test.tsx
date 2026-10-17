import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DeploymentHealth } from "./DeploymentHealth";

function answer(status: number, body: unknown) {
  const fetch = vi.fn(() => Promise.resolve(Response.json(body, { status })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const HEALTHY = { status: "healthy", database: { status: "connected", problem: null } };
const UNHEALTHY = {
  status: "unhealthy",
  database: { status: "unreachable", problem: "authentication failed" },
};

/** The part of the page that announces the state, and the state it marks itself with. */
function report(): { text: string | null; state: string | null } {
  const region = screen.getByRole("status");
  return {
    text: region.textContent,
    state: region.querySelector(".report")?.getAttribute("data-state") ?? null,
  };
}

describe("DeploymentHealth", () => {
  it("is announced politely as a status region from the start, saying it is checking", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));

    render(<DeploymentHealth />);

    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(report().state).toBe("checking");
    expect(screen.getByText(/Checking/)).toBeInTheDocument();
  });

  it("asks the api for /health and says the Deployment is healthy", async () => {
    const fetch = answer(200, HEALTHY);

    render(<DeploymentHealth />);

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("Database connected")).toBeInTheDocument();
    expect(report().state).toBe("healthy");
    expect(fetch).toHaveBeenCalledWith("/health", expect.anything());
  });

  it("says why the database cannot be reached when the api answers 503", async () => {
    answer(503, UNHEALTHY);

    render(<DeploymentHealth />);

    expect(await screen.findByText("Unhealthy")).toBeInTheDocument();
    expect(screen.getByText("Database unreachable: authentication failed")).toBeInTheDocument();
    expect(report().state).toBe("unhealthy");
  });

  it("says so when the Deployment itself cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    render(<DeploymentHealth />);

    expect(await screen.findByText("The Deployment could not be reached")).toBeInTheDocument();
    expect(report().state).toBe("error");
  });

  it("does not take an answer that is not /health's for a health report", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("Bad Gateway", { status: 502 }))),
    );

    render(<DeploymentHealth />);

    expect(await screen.findByText("The Deployment could not be reached")).toBeInTheDocument();
    expect(report().state).toBe("error");
  });

  it.each([
    ["a 503 whose body is not JSON", () => new Response("<html>", { status: 503 })],
    ["a 503 with a JSON body of another shape", () => Response.json({ detail: "x" }, { status: 503 })],
    ["a 200 with a database that is not an object", () => Response.json({ status: "healthy", database: 1 })],
    ["a 200 with a status it does not know", () => Response.json({ ...HEALTHY, status: "great" })],
    [
      "a 200 with a problem that is not text",
      () => Response.json({ ...HEALTHY, database: { status: "connected", problem: 7 } }),
    ],
  ])("renders a clear error, not a half-read report, for %s", async (_name, response) => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response())));

    render(<DeploymentHealth />);

    expect(
      await screen.findByText("The Deployment answered with something that is not a health report"),
    ).toBeInTheDocument();
    expect(report().state).toBe("error");
    expect(report().text).not.toMatch(/undefined|Database/);
  });

  it("tells the four states apart by words and marks, not by colour alone", async () => {
    const seen = new Map<string, string | null>();

    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const checking = render(<DeploymentHealth />);
    seen.set("checking", report().text);
    checking.unmount();

    for (const [name, status, body] of [
      ["healthy", 200, HEALTHY],
      ["unhealthy", 503, UNHEALTHY],
    ] as const) {
      answer(status, body);
      const view = render(<DeploymentHealth />);
      await vi.waitFor(() => {
        expect(report().state).toBe(name);
      });
      seen.set(name, report().text);
      view.unmount();
    }

    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    render(<DeploymentHealth />);
    await vi.waitFor(() => {
      expect(report().state).toBe("error");
    });
    seen.set("error", report().text);

    expect(new Set(seen.values()).size).toBe(4);
    expect(seen.get("healthy")).toContain("✓");
    expect(seen.get("unhealthy")).toContain("✗");
  });
});
