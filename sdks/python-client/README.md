# omnicraft-client

Python client SDK for the [omnicraft](https://github.com/omnicraft-ai/omnicraft)
server API.

`omnicraft-client` is a typed client for driving omnicraft sessions over the
server's HTTP + SSE API — creating sessions, sending turns, and streaming
responses. It shares the `StreamEvent` / `SessionStreamEventType` types that the
server emits, so streamed envelopes are validated against a single source of
truth.

It is released in lockstep with the core `omnicraft` package at a matching
version:

```bash
pip install omnicraft-client
```

See the [omnicraft repository](https://github.com/omnicraft-ai/omnicraft) for full
documentation.
