// Invariants:
// - hidden=true → returns null.
// - canPrev/canNext drive the `disabled` attribute (asserted explicitly
//   to catch a regression to aria-disabled, which wouldn't block clicks).

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { UserMessageNav } from "./UserMessageNav";

function renderNav(props: Partial<React.ComponentProps<typeof UserMessageNav>>) {
  const merged = {
    goPrev: vi.fn(),
    goNext: vi.fn(),
    canPrev: true,
    canNext: true,
    hidden: false,
    ...props,
  };
  render(
    <TooltipProvider>
      <UserMessageNav {...merged} />
    </TooltipProvider>,
  );
  return merged;
}

afterEach(cleanup);

describe("UserMessageNav", () => {
  it("renders nothing when hidden", () => {
    renderNav({ hidden: true });
    expect(screen.queryByLabelText("Mensagem anterior do usuário")).toBeNull();
    expect(screen.queryByLabelText("Próxima mensagem do usuário")).toBeNull();
  });

  it("renders both buttons when there is content to navigate", () => {
    renderNav({});
    expect(screen.getByLabelText("Mensagem anterior do usuário")).toBeEnabled();
    expect(screen.getByLabelText("Próxima mensagem do usuário")).toBeEnabled();
  });

  it("disables Previous when canPrev=false", () => {
    const props = renderNav({ canPrev: false });
    const btn = screen.getByLabelText("Mensagem anterior do usuário");
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goPrev).not.toHaveBeenCalled();
  });

  it("disables Next when canNext=false", () => {
    const props = renderNav({ canNext: false });
    const btn = screen.getByLabelText("Próxima mensagem do usuário");
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goNext).not.toHaveBeenCalled();
  });

  it("invokes goPrev / goNext on click", () => {
    const props = renderNav({});
    fireEvent.click(screen.getByLabelText("Mensagem anterior do usuário"));
    fireEvent.click(screen.getByLabelText("Próxima mensagem do usuário"));
    expect(props.goPrev).toHaveBeenCalledOnce();
    expect(props.goNext).toHaveBeenCalledOnce();
  });
});
