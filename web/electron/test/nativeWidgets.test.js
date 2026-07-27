// Behavior tests for the widgets controller (src/nativeWidgets.js).
//
// The class exists to keep exactly one process alive and never orphan it, and
// to make "which panels" part of the launch — so what is asserted here is the
// lifetime, not the drawing.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { NativeWidgetsController, resolveWidgetsApp } = require("../src/nativeWidgets");

/** A spawn double: records the calls and hands back a fake child. */
function harness({ path = "/fake/OmniCraftWidgets.app", url = "http://127.0.0.1:6767" } = {}) {
  const spawned = [];
  const killed = [];
  const controller = new NativeWidgetsController({
    resolve: () => (path ? { path } : { path: null, reason: "não construídos" }),
    spawnFn: (binary, args) => {
      const child = {
        binary,
        args,
        on: () => {},
        kill: () => killed.push(binary),
      };
      spawned.push(child);
      return child;
    },
    serverUrl: () => url,
    onWarn: () => {},
  });
  return { controller, spawned, killed };
}

describe("nativeWidgets — o que sobe", () => {
  it("passa o servidor e um --widget por painel", () => {
    const h = harness();
    h.controller.start(["board", "uso"]);
    assert.deepEqual(h.spawned[0].args, [
      "--live",
      "-OmniCraftFeedBaseURL",
      "http://127.0.0.1:6767",
      "--widget",
      "board",
      "--widget",
      "uso",
    ]);
  });

  it("não sobe processo sem painel nenhum", () => {
    // Um processo sem janela é um processo que a pessoa não vê nem fecha.
    const h = harness();
    const resultado = h.controller.start([]);
    assert.equal(resultado.started, false);
    assert.equal(h.spawned.length, 0);
  });

  it("ignora um painel que este build não conhece", () => {
    const h = harness();
    h.controller.start(["board", "capivara"]);
    assert.deepEqual(h.spawned[0].args.filter((a) => a !== "--widget").slice(3), ["board"]);
  });

  it("reinicia quando a seleção muda, e não quando repete", () => {
    // Quais painéis abrem faz parte do lançamento: mudar a escolha só aparece
    // na tela se o processo subir de novo.
    const h = harness();
    h.controller.start(["board"]);
    h.controller.start(["board"]);
    assert.equal(h.spawned.length, 1, "mesma seleção não relança");

    h.controller.start(["board", "uso"]);
    assert.equal(h.spawned.length, 2);
    assert.equal(h.killed.length, 1, "o processo anterior foi encerrado");
  });

  it("desligar encerra o que estava de pé", () => {
    const h = harness();
    h.controller.apply(true, ["board"]);
    assert.equal(h.controller.running, true);
    h.controller.apply(false, ["board"]);
    assert.equal(h.controller.running, false);
    assert.equal(h.killed.length, 1);
  });

  it("diz o motivo em vez de falhar calado quando não há bundle", () => {
    const h = harness({ path: null });
    assert.deepEqual(h.controller.start(["board"]), { started: false, reason: "não construídos" });
  });

  it("fora do macOS não há o que resolver", () => {
    const encontrado = resolveWidgetsApp({ platform: "linux" });
    assert.equal(encontrado.path, null);
    assert.match(encontrado.reason, /macOS/);
  });
});
