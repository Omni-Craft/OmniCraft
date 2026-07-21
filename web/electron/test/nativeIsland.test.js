// Tests for the native island's process lifetime (src/nativeIsland.js), run
// with `node --test` (no extra deps).
//
// The island is a separate app, so the failure that matters is not a wrong
// pixel — it is a process left behind. A second island stacked on the first,
// or one still drawing over the menu bar after the shell quit, is something
// the user cannot close from the app they just closed.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { NativeIslandController, resolveIslandApp } = require("../src/nativeIsland");

/** A spawn that records calls and hands back a controllable fake child. */
function fakeSpawn() {
  const calls = [];
  const children = [];
  const spawnFn = (command, args, options) => {
    calls.push({ command, args, options });
    const handlers = {};
    const child = {
      killed: false,
      on: (event, handler) => {
        handlers[event] = handler;
      },
      kill: () => {
        child.killed = true;
        handlers.exit?.(0);
      },
      emitError: (error) => handlers.error?.(error),
      emitExit: () => handlers.exit?.(1),
    };
    children.push(child);
    return child;
  };
  return { spawnFn, calls, children };
}

/** A controller wired to a fake bundle that is always present. */
function controller(overrides = {}) {
  const spawn = fakeSpawn();
  const warnings = [];
  const instance = new NativeIslandController({
    resolve: () => ({ path: "/fake/OmniCraftNotch.app" }),
    spawnFn: spawn.spawnFn,
    onWarn: (message) => warnings.push(message),
    ...overrides,
  });
  return { instance, spawn, warnings };
}

describe("starting and stopping", () => {
  it("runs the binary inside the bundle, not the bundle directory", () => {
    const { instance, spawn } = controller();

    assert.deepEqual(instance.start(), { started: true });
    assert.equal(spawn.calls.length, 1);
    assert.equal(spawn.calls[0].command, "/fake/OmniCraftNotch.app/Contents/MacOS/OmniCraftNotch");
  });

  it("does not stack a second island on the first", () => {
    const { instance, spawn } = controller();

    instance.start();
    instance.start();

    assert.equal(spawn.calls.length, 1, "already running means nothing to start");
  });

  it("leaves nothing running after stop", () => {
    const { instance, spawn } = controller();
    instance.start();

    instance.stop();

    assert.equal(spawn.children[0].killed, true);
    assert.equal(instance.running, false);
  });

  it("stopping when nothing runs is not an error", () => {
    const { instance } = controller();

    assert.doesNotThrow(() => instance.stop());
  });

  it("can start again after the island exited on its own", () => {
    const { instance, spawn } = controller();
    instance.start();

    spawn.children[0].emitExit();

    assert.equal(instance.running, false, "a dead child is not a running island");
    instance.start();
    assert.equal(spawn.calls.length, 2, "and a new one can take its place");
  });
});

describe("when the island cannot run", () => {
  it("reports why instead of pretending it started", () => {
    const { instance, warnings } = controller({
      resolve: () => ({ path: null, reason: "ainda não foi construída" }),
    });

    const result = instance.start();

    assert.deepEqual(result, { started: false, reason: "ainda não foi construída" });
    assert.equal(instance.running, false);
    assert.match(warnings[0], /ainda não foi construída/);
  });

  it("survives a spawn that throws", () => {
    const { instance, warnings } = controller({
      spawnFn: () => {
        throw new Error("EACCES");
      },
    });

    const result = instance.start();

    assert.equal(result.started, false);
    assert.equal(instance.running, false);
    assert.match(warnings[0], /EACCES/);
  });
});

describe("following the setting", () => {
  it("starts when switched on and stops when switched off", () => {
    const { instance, spawn } = controller();

    instance.apply(true);
    assert.equal(instance.running, true);

    instance.apply(false);
    assert.equal(instance.running, false);
    assert.equal(spawn.children[0].killed, true);
  });
});

describe("finding the bundle", () => {
  it("says the island is macOS-only elsewhere", () => {
    const found = resolveIslandApp({ platform: "win32" });

    assert.equal(found.path, null);
    assert.match(found.reason, /macOS/);
  });

  it("reports a build step rather than a mystery when it is absent", () => {
    const found = resolveIslandApp({ platform: "darwin", appPath: "/nowhere/web/electron" });

    assert.equal(found.path, null);
    assert.match(found.reason, /make-app\.sh/);
  });
});
