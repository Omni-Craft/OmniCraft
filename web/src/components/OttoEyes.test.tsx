import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OttoEyes } from "./OttoEyes";

afterEach(cleanup);

describe("OttoEyes", () => {
  it("renders the mascot with image semantics", () => {
    const { container } = render(<OttoEyes className="h-18" />);
    const img = container.querySelector("img");
    // The new-chat hero is a meaningful image, so the wrapper overrides
    // OttoIcon's decorative default; losing it would silently hide the brand
    // image from screen readers.
    expect(img).toHaveAttribute("role", "img");
    expect(img).toHaveAttribute("aria-label", "OmniCraft");
    expect(img).toHaveAttribute("aria-hidden", "false");
    expect(img).toHaveClass("h-18");
    expect(img).toHaveAttribute("src");
  });
});
