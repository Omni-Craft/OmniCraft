import { describe, expect, it } from "vitest";

import { apiErrorMessage } from "./apiErrors";

describe("apiErrorMessage", () => {
  it("keeps the server's specifics after the Portuguese sentence", () => {
    // Dropping this detail would hide who needs which permission.
    const message = apiErrorMessage(
      { error: { code: "forbidden", message: "'rice' needs manage permission" } },
      "Falhou.",
    );
    expect(message).toBe("Você não tem permissão para esta ação: 'rice' needs manage permission");
  });

  it("drops boilerplate that only restates the code", () => {
    // The CSRF guard's sentence is jargon that adds nothing for a person.
    const message = apiErrorMessage(
      {
        error: {
          code: "forbidden",
          message: "Forbidden: this endpoint requires a trusted Origin header.",
        },
      },
      "Falhou.",
    );
    expect(message).toBe("Você não tem permissão para esta ação.");
  });

  it("translates when the server sends no message at all", () => {
    expect(apiErrorMessage({ error: { code: "runner_unavailable" } }, "Falhou.")).toBe(
      "O runner não está disponível agora.",
    );
  });

  it("falls back to the server message for a code it doesn't know", () => {
    // A code added server-side before this table degrades to the old
    // behaviour rather than to a wrong translation.
    const message = apiErrorMessage(
      { error: { code: "brand_new_code", message: "Something specific happened" } },
      "Falhou.",
    );
    expect(message).toBe("Something specific happened");
  });

  it("uses the caller's sentence when the body carries nothing", () => {
    expect(apiErrorMessage(null, "Falha ao agendar.")).toBe("Falha ao agendar.");
    expect(apiErrorMessage({}, "Falha ao agendar.")).toBe("Falha ao agendar.");
    expect(apiErrorMessage({ error: null }, "Falha ao agendar.")).toBe("Falha ao agendar.");
  });
});
