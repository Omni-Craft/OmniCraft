import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockHostFetch } = vi.hoisted(() => ({ mockHostFetch: vi.fn() }));

vi.mock("@/lib/host", () => ({ hostFetch: mockHostFetch }));

import { useRepoUpdate } from "./useRepoUpdate";

/** A QueryClient with retries off, so a rejected fetch settles at once. */
function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 500, json: async () => body } as unknown as Response;
}

afterEach(() => {
  vi.clearAllMocks();
  cleanup();
});

describe("useRepoUpdate", () => {
  it("reports an available update the server confirms", async () => {
    mockHostFetch.mockResolvedValue(
      jsonResponse({ running_commit: "abc", current_commit: "def", update_available: true }),
    );

    const { result } = renderHook(() => useRepoUpdate(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.update_available).toBe(true));
  });

  it("does not claim an update when the poll fails", async () => {
    // The banner prompts a restart — a network error is not evidence of one.
    mockHostFetch.mockRejectedValue(new Error("offline"));

    const { result } = renderHook(() => useRepoUpdate(), { wrapper: wrapper() });

    await waitFor(() => expect(mockHostFetch).toHaveBeenCalled());
    expect(result.current.update_available).toBe(false);
  });

  it("does not claim an update on a non-OK response", async () => {
    mockHostFetch.mockResolvedValue(jsonResponse({ update_available: true }, false));

    const { result } = renderHook(() => useRepoUpdate(), { wrapper: wrapper() });

    await waitFor(() => expect(mockHostFetch).toHaveBeenCalled());
    expect(result.current.update_available).toBe(false);
  });

  it("treats a malformed body as no update", async () => {
    // `update_available` present but not a boolean must not read as true.
    mockHostFetch.mockResolvedValue(jsonResponse({ update_available: "yes" }));

    const { result } = renderHook(() => useRepoUpdate(), { wrapper: wrapper() });

    await waitFor(() => expect(mockHostFetch).toHaveBeenCalled());
    expect(result.current.update_available).toBe(false);
  });
});
