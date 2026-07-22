import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FuchoMascot, cellBackgroundPosition, sheetBackgroundSize } from "./FuchoMascot";

beforeEach(() => {
  // jsdom has no matchMedia; the sprite reads it for reduced motion.
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

describe("sprite math", () => {
  it("scales the whole 8×9 sheet so one cell is `size`", () => {
    expect(sheetBackgroundSize(56)).toBe(`${8 * 56}px ${9 * 56}px`);
  });

  it("offsets to a pose's row and frame", () => {
    // codando is row 7; frame 2 at size 56.
    expect(cellBackgroundPosition("codando", 2, 56)).toBe(`${-2 * 56}px ${-7 * 56}px`);
  });
});

describe("FuchoMascot", () => {
  it("renders a labelled image by default", () => {
    const { container } = render(<FuchoMascot pose="idle" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root).toHaveAttribute("role", "img");
    expect(root).toHaveAttribute("aria-label", "OmniCraft");
  });

  it("passes through className and is decorative when unlabelled", () => {
    const { container } = render(<FuchoMascot pose="idle" ariaLabel="" className="h-14" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root).toHaveClass("h-14");
    expect(root).toHaveAttribute("aria-hidden", "true");
    expect(root).not.toHaveAttribute("role");
  });

  it("hides the star until it has a pose", () => {
    const { container, rerender } = render(<FuchoMascot pose="erro" />);
    const starWrap = () => container.querySelector(".fucho-breathe") as HTMLElement;
    expect(starWrap().style.opacity).toBe("0");
    rerender(<FuchoMascot pose="erro" starPose="pensando" />);
    expect(starWrap().style.opacity).toBe("1");
  });
});
