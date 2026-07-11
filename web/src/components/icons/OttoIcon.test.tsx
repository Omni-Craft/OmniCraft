import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OttoIcon } from "./OttoIcon";

afterEach(cleanup);

describe("OttoIcon", () => {
  it("renders the mascot image, decorative by default", () => {
    const { container } = render(<OttoIcon className="otto-working h-4" />);
    const img = container.querySelector("img");
    // The animation is opt-in via className, so the spread must reach the img.
    expect(img).toHaveClass("otto-working");
    expect(img).toHaveAttribute("src");
    // Decorative by default (empty alt) so a status pin's aria-live region only
    // ever announces the "Working…" text, not the mascot.
    expect(img).toHaveAttribute("alt", "");
  });

  it("lets callers label it as a meaningful image for the hero render", () => {
    const { container } = render(
      <OttoIcon role="img" aria-label="OmniCraft" aria-hidden={false} />,
    );
    const img = container.querySelector("img");
    // NewChatDialog renders the mascot as a meaningful image; the override only
    // works while the spread stays after the alt default.
    expect(img).toHaveAttribute("role", "img");
    expect(img).toHaveAttribute("aria-label", "OmniCraft");
    expect(img).toHaveAttribute("aria-hidden", "false");
  });
});
