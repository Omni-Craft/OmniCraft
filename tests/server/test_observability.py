"""Unit tests for the cost aggregation helper."""

from __future__ import annotations

from omnicraft.server.routes.observability import aggregate_sessions


def test_aggregates_totals_by_model_and_top_sessions() -> None:
    sessions = [
        (
            "conv_a",
            "build",
            {
                "total_tokens": 1000,
                "total_cost_usd": 5.0,
                "by_model": {"opus": {"input_tokens": 600, "output_tokens": 400, "total_tokens": 1000, "total_cost_usd": 5.0}},
            },
        ),
        (
            "conv_b",
            "recon",
            {
                "total_tokens": 200,
                "total_cost_usd": 1.0,
                "by_model": {
                    "opus": {"input_tokens": 50, "output_tokens": 50, "total_tokens": 100, "total_cost_usd": 0.6},
                    "gpt": {"input_tokens": 50, "output_tokens": 50, "total_tokens": 100, "total_cost_usd": 0.4},
                },
            },
        ),
        ("conv_empty", "idle", {}),  # no usage -> excluded from priced sessions
    ]
    out = aggregate_sessions(sessions)

    assert out["total_usd"] == 6.0
    assert out["total_tokens"] == 1200
    assert out["session_count"] == 2  # conv_empty excluded

    by_model = {m["model"]: m for m in out["by_model"]}
    assert by_model["opus"]["usd"] == 5.6 and by_model["opus"]["total_tokens"] == 1100
    assert by_model["gpt"]["usd"] == 0.4
    # usd-desc ordering
    assert [m["model"] for m in out["by_model"]] == ["opus", "gpt"]

    # Top sessions: priciest first, empty one absent.
    assert [s["id"] for s in out["top_sessions"]] == ["conv_a", "conv_b"]


def test_tolerates_malformed_usage() -> None:
    out = aggregate_sessions(
        [("c", "t", {"total_cost_usd": "bad", "total_tokens": None, "by_model": "nope"})]
    )
    assert out["total_usd"] == 0.0 and out["total_tokens"] == 0
    assert out["by_model"] == [] and out["session_count"] == 0
