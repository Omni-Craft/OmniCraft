"""The push observer fires alongside the primary observer, independently."""

from __future__ import annotations

from omnicraft.runtime import pending_elicitations as pe


def test_both_observers_receive_elicitation_events() -> None:
    primary: list[tuple[str, str]] = []
    push: list[tuple[str, str]] = []
    pe.set_elicitation_observer(lambda cid, ev: primary.append((cid, ev["type"])))
    pe.set_push_observer(lambda cid, ev: push.append((cid, ev["type"])))
    try:
        pe.record_publish(
            "conv_1", {"type": "response.elicitation_request", "elicitation_id": "el_1"}
        )
        assert primary == [("conv_1", "response.elicitation_request")]
        assert push == [("conv_1", "response.elicitation_request")]

        # Clearing one leaves the other working.
        pe.set_elicitation_observer(None)
        pe.record_publish(
            "conv_2", {"type": "response.elicitation_request", "elicitation_id": "el_2"}
        )
        assert len(primary) == 1  # unchanged
        assert push[-1] == ("conv_2", "response.elicitation_request")
    finally:
        pe.set_elicitation_observer(None)
        pe.set_push_observer(None)
        pe.reset_for_tests()
