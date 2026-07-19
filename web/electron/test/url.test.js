// Tests for the shared desktop URL helpers (src/url.js), run with
// `node --test` (no extra deps). Covers the scheme-defaulting that lets a
// pasted workspace URL (schemeless, /omnicraft suffix from the internal user
// guide) connect, the plain-http warning, and the workspace probe/expansion.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  defaultSchemeFor,
  normalizeUrl,
  isPlainHttpRemote,
  expandDatabricksWorkspaceUrl,
  hudRouteUrl,
  hudNavigationDecision,
  WORKSPACE_UI_PATH,
} = require("../src/url");

describe("hudRouteUrl", () => {
  it("appends /hud to a root server URL", () => {
    assert.equal(hudRouteUrl("http://localhost:6767"), "http://localhost:6767/hud");
    assert.equal(hudRouteUrl("http://localhost:6767/"), "http://localhost:6767/hud");
  });

  it("resolves against the ORIGIN, not the current route", () => {
    // The URL handed in is whatever the window navigated to. Appending to
    // that path yields /chat/123/hud — a route the SPA does not have — so
    // the HUD would open on a 404 for anyone not sitting on the root.
    assert.equal(hudRouteUrl("http://localhost:6767/chat/123"), "http://localhost:6767/hud");
    assert.equal(
      hudRouteUrl("http://localhost:6767/c/conv_abc/files/deep/path"),
      "http://localhost:6767/hud",
    );
  });

  it("preserves a recognized mount prefix", () => {
    // A workspace deploy really does serve the SPA (and therefore /hud)
    // under the mount; dropping the prefix lands on the workspace's own 404.
    assert.equal(
      hudRouteUrl("https://dbc-x.cloud.databricks.com/ml/omnicrafts/"),
      "https://dbc-x.cloud.databricks.com/ml/omnicrafts/hud",
    );
    // …including once the window has navigated inside the mount.
    assert.equal(
      hudRouteUrl(`https://dbc-x.cloud.databricks.com${WORKSPACE_UI_PATH}/chat/123`),
      `https://dbc-x.cloud.databricks.com${WORKSPACE_UI_PATH}/hud`,
    );
  });

  it("drops query and hash so nothing about auth can ride in the URL", () => {
    assert.equal(
      hudRouteUrl("http://localhost:6767/?token=secret#/x"),
      "http://localhost:6767/hud",
    );
  });

  it("throws on an unusable server URL rather than navigating to garbage", () => {
    assert.throws(() => hudRouteUrl(""));
    assert.throws(() => hudRouteUrl("ftp://example.com"));
  });
});

describe("hudNavigationDecision", () => {
  const PINNED = "http://localhost:6767";

  it("allows the pinned origin's own routes", () => {
    assert.equal(hudNavigationDecision(PINNED, "http://localhost:6767/hud"), "allow");
    assert.equal(
      hudNavigationDecision(PINNED, "http://localhost:6767/login?return_to=%2Fhud"),
      "allow",
    );
  });

  it("sends another origin to the real browser instead of the floating strip", () => {
    // A chromeless always-on-top window has no address bar, so a foreign page
    // rendered there is an unattributable overlay above every other app.
    assert.equal(hudNavigationDecision(PINNED, "https://evil.example.com/login"), "external");
    // Same host, different port or scheme is still a different origin.
    assert.equal(hudNavigationDecision(PINNED, "http://localhost:9999/"), "external");
    assert.equal(hudNavigationDecision(PINNED, "https://localhost:6767/"), "external");
  });

  it("blocks non-web schemes and unparseable targets outright", () => {
    assert.equal(hudNavigationDecision(PINNED, "file:///etc/passwd"), "block");
    assert.equal(hudNavigationDecision(PINNED, "vscode://file/x.py"), "block");
    assert.equal(hudNavigationDecision(PINNED, "not a url"), "block");
  });
});

describe("defaultSchemeFor", () => {
  it("defaults remote hosts to https", () => {
    assert.equal(defaultSchemeFor("dbc-x.cloud.databricks.com/omnicraft"), "https");
    assert.equal(defaultSchemeFor("example.com"), "https");
  });

  it("defaults loopback hosts to http", () => {
    assert.equal(defaultSchemeFor("localhost:6767"), "http");
    assert.equal(defaultSchemeFor("127.0.0.1:6767"), "http");
    assert.equal(defaultSchemeFor("[::1]:6767"), "http");
  });

  it("defaults unparseable input to https", () => {
    assert.equal(defaultSchemeFor("exa mple"), "https");
  });
});

describe("normalizeUrl", () => {
  it("defaults a schemeless workspace /omnicraft URL to https", () => {
    assert.equal(
      normalizeUrl("dbc-a5d4177a-49dc.cloud.databricks.com/omnicraft"),
      "https://dbc-a5d4177a-49dc.cloud.databricks.com/omnicraft",
    );
  });

  it("defaults a bare remote host to https", () => {
    assert.equal(
      normalizeUrl("example.cloud.databricks.com"),
      "https://example.cloud.databricks.com/",
    );
  });

  it("defaults loopback hosts to http", () => {
    assert.equal(normalizeUrl("localhost:6767"), "http://localhost:6767/");
    assert.equal(normalizeUrl("127.0.0.1:6767"), "http://127.0.0.1:6767/");
    assert.equal(normalizeUrl("[::1]:6767"), "http://[::1]:6767/");
  });

  it("preserves an explicit scheme (even http to a remote host)", () => {
    assert.equal(normalizeUrl("http://localhost:6767"), "http://localhost:6767/");
    assert.equal(normalizeUrl("https://example.com"), "https://example.com/");
    assert.equal(normalizeUrl("http://example.databricks.com"), "http://example.databricks.com/");
  });

  it("trims surrounding whitespace", () => {
    assert.equal(normalizeUrl("  example.com/omnicraft  "), "https://example.com/omnicraft");
  });

  it("rejects empty input", () => {
    assert.throws(() => normalizeUrl(""), /server URL is empty/);
    assert.throws(() => normalizeUrl("   "), /server URL is empty/);
  });

  it("rejects a non-http(s) scheme", () => {
    assert.throws(() => normalizeUrl("ftp://example.com"), /unsupported scheme/);
  });
});

describe("isPlainHttpRemote", () => {
  it("does not warn for a bare remote host (now https)", () => {
    assert.equal(isPlainHttpRemote("example.databricks.com"), false);
    assert.equal(isPlainHttpRemote("dbc-x.cloud.databricks.com/omnicraft"), false);
  });

  it("warns for an explicit http:// to a remote host", () => {
    assert.equal(isPlainHttpRemote("http://example.databricks.com"), true);
  });

  it("does not warn for loopback hosts", () => {
    assert.equal(isPlainHttpRemote("localhost:6767"), false);
    assert.equal(isPlainHttpRemote("http://localhost:6767"), false);
    assert.equal(isPlainHttpRemote("http://127.0.0.1:6767"), false);
  });

  it("does not warn for https or empty/invalid input", () => {
    assert.equal(isPlainHttpRemote("https://example.databricks.com"), false);
    assert.equal(isPlainHttpRemote(""), false);
    assert.equal(isPlainHttpRemote("ht tp://nope"), false);
  });
});

/**
 * Run `fn` with `globalThis.fetch` swapped for `stub` and `AbortSignal.timeout`
 * neutralized (no real timer), restoring both afterward.
 */
async function withFetch(stub, fn) {
  const realFetch = globalThis.fetch;
  const realTimeout = AbortSignal.timeout;
  globalThis.fetch = stub;
  AbortSignal.timeout = () => new AbortController().signal;
  try {
    return await fn();
  } finally {
    globalThis.fetch = realFetch;
    AbortSignal.timeout = realTimeout;
  }
}

/** A minimal Response stand-in exposing only `.headers.get`. */
function fakeResponse(serverHeader) {
  return { headers: { get: (name) => (name === "server" ? serverHeader : null) } };
}

describe("expandDatabricksWorkspaceUrl", () => {
  it("expands a bare https Databricks workspace root to the UI mount", async () => {
    const calls = [];
    await withFetch(
      async (url, opts) => {
        calls.push({ url, method: opts.method });
        return fakeResponse("databricks");
      },
      async () => {
        const out = await expandDatabricksWorkspaceUrl("https://ws.cloud.databricks.com/");
        assert.equal(out, `https://ws.cloud.databricks.com${WORKSPACE_UI_PATH}`);
      },
    );
    // Probed the root with a HEAD request.
    assert.deepEqual(calls, [{ url: "https://ws.cloud.databricks.com/", method: "HEAD" }]);
  });

  it("leaves a non-Databricks root unchanged", async () => {
    await withFetch(
      async () => fakeResponse("nginx"),
      async () => {
        assert.equal(
          await expandDatabricksWorkspaceUrl("https://example.com"),
          "https://example.com",
        );
      },
    );
  });

  it("leaves a URL that already carries a path untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        const url = "https://ws.cloud.databricks.com/omnicraft";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
      },
    );
    assert.equal(probed, false);
  });

  it("leaves a Databricks Apps host untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        const url = "https://my-app-123.aws.databricksapps.com/";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
        assert.equal(
          await expandDatabricksWorkspaceUrl("https://databricksapps.com/"),
          "https://databricksapps.com/",
        );
      },
    );
    assert.equal(probed, false);
  });

  it("leaves a non-https URL untouched, without probing", async () => {
    let probed = false;
    await withFetch(
      async () => {
        probed = true;
        return fakeResponse("databricks");
      },
      async () => {
        assert.equal(
          await expandDatabricksWorkspaceUrl("http://localhost:6767/"),
          "http://localhost:6767/",
        );
      },
    );
    assert.equal(probed, false);
  });

  it("falls back to the input when the probe fails", async () => {
    await withFetch(
      async () => {
        throw new Error("ECONNREFUSED");
      },
      async () => {
        const url = "https://unreachable.example.com";
        assert.equal(await expandDatabricksWorkspaceUrl(url), url);
      },
    );
  });

  it("returns unparseable input unchanged", async () => {
    assert.equal(await expandDatabricksWorkspaceUrl("not a url"), "not a url");
  });
});
