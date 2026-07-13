import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RootErrorBoundary } from "./RootErrorBoundary";

function Boom(): never {
  throw new Error("kaboom da renderização");
}

describe("RootErrorBoundary", () => {
  beforeEach(() => {
    // React logs the caught error; silence it so the test output stays clean.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when nothing throws", () => {
    render(
      <RootErrorBoundary>
        <div>conteúdo normal</div>
      </RootErrorBoundary>,
    );
    expect(screen.getByText("conteúdo normal")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the recovery screen (not a blank page) when a child throws", () => {
    render(
      <RootErrorBoundary>
        <Boom />
      </RootErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Algo deu errado")).toBeInTheDocument();
    expect(screen.getByText("kaboom da renderização")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recarregar" })).toBeInTheDocument();
  });

  it("reloads the page when the recover button is clicked", () => {
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });
    render(
      <RootErrorBoundary>
        <Boom />
      </RootErrorBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Recarregar" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
