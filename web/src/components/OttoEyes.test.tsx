import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OttoEyes } from "./OttoEyes";

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi
      .fn()
      .mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("OttoEyes", () => {
  it("renders the animated mascot with image semantics", () => {
    const { container } = render(<OttoEyes className="shrink-0" />);
    const root = container.firstElementChild as HTMLElement;
    // The new-chat hero is a meaningful image; losing the label would silently
    // hide the brand mascot from screen readers.
    expect(root).toHaveAttribute("role", "img");
    expect(root).toHaveAttribute("aria-label", "OmniCraft");
    expect(root).toHaveClass("shrink-0");
  });
});
