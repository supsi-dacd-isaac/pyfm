from datetime import datetime, timedelta, timezone
import logging

import pytest

from classes.asset_commitment_coordinator import (
    ActivationSequencePolicy,
    AssetCommitmentCoordinator,
    resolve_activation_sequence_policy,
)
from classes.bid_record_repository import BidRecordRepository


SLOT = datetime(2026, 10, 13, 5, 30)


def _slot(offset):
    return SLOT + timedelta(minutes=15 * offset)


def test_two_plus_two_allows_two_blocks_two_and_reopens_on_fifth_slot():
    policy = ActivationSequencePolicy(
        slot_minutes=15,
        max_consecutive_slots=2,
        cooldown_slots=2,
    )

    first = policy.evaluate("asset-x", _slot(0), [])
    second = policy.evaluate("asset-x", _slot(1), [_slot(0)])
    third = policy.evaluate("asset-x", _slot(2), [_slot(0), _slot(1)])
    fourth = policy.evaluate("asset-x", _slot(3), [_slot(0), _slot(1)])
    fifth = policy.evaluate("asset-x", _slot(4), [_slot(0), _slot(1)])

    assert first.eligible is True
    assert first.prospective_consecutive_slots == 1
    assert second.eligible is True
    assert second.prospective_consecutive_slots == 2
    assert second.next_cooldown_until_slot == _slot(4)
    assert third.eligible is False
    assert third.blocked_until_slot == _slot(4)
    assert fourth.eligible is False
    assert fourth.blocked_until_slot == _slot(4)
    assert fifth.eligible is True
    assert fifth.reason_code == "eligible_new_sequence"


def test_four_plus_two_uses_the_same_policy_without_strategy_knowledge():
    policy = ActivationSequencePolicy(15, 4, 2)
    commitments = [_slot(i) for i in range(4)]

    assert policy.evaluate("arbitrary-resource", _slot(4), commitments).eligible is False
    assert policy.evaluate("arbitrary-resource", _slot(5), commitments).eligible is False
    reopened = policy.evaluate("arbitrary-resource", _slot(6), commitments)
    assert reopened.eligible is True
    assert reopened.prospective_consecutive_slots == 1


def test_non_consecutive_commitment_starts_a_new_sequence():
    policy = ActivationSequencePolicy(15, 2, 2)
    decision = policy.evaluate("asset-x", _slot(3), [_slot(0)])

    assert decision.eligible is True
    assert decision.reason_code == "eligible_new_sequence"
    assert decision.previous_consecutive_slots == 0
    assert decision.prospective_consecutive_slots == 1


def test_legacy_commitment_inside_cooldown_extends_release_window():
    policy = ActivationSequencePolicy(15, 2, 2)
    commitments = [_slot(0), _slot(1), _slot(2)]

    decision = policy.evaluate("asset-x", _slot(4), commitments)

    assert decision.eligible is False
    assert decision.blocked_until_slot == _slot(5)
    assert decision.conflicting_committed_slots == (_slot(2),)


def test_timezone_aware_commitments_are_normalized_to_utc():
    policy = ActivationSequencePolicy(15, 2, 2)
    aware_first = _slot(0).replace(tzinfo=timezone.utc)
    aware_target = _slot(1).replace(tzinfo=timezone.utc)

    decision = policy.evaluate("asset-x", aware_target, [aware_first])

    assert decision.eligible is True
    assert decision.reason_code == "eligible_continuation"
    assert decision.target_slot.tzinfo is None


@pytest.mark.parametrize(
    "kwargs, expected_error",
    [
        ({"slot_minutes": 0, "max_consecutive_slots": 2, "cooldown_slots": 2}, "slot_minutes"),
        ({"slot_minutes": 15, "max_consecutive_slots": 0, "cooldown_slots": 2}, "max_consecutive_slots"),
        ({"slot_minutes": 15, "max_consecutive_slots": 2, "cooldown_slots": -1}, "cooldown_slots"),
    ],
)
def test_policy_rejects_invalid_limits(kwargs, expected_error):
    with pytest.raises(ValueError, match=expected_error):
        ActivationSequencePolicy(**kwargs)


def test_duplicate_current_and_future_slots_do_not_change_prior_sequence():
    policy = ActivationSequencePolicy(15, 2, 2)

    decision = policy.evaluate(
        "asset-x",
        _slot(2),
        [_slot(3), _slot(1), _slot(2), _slot(1)],
    )

    assert decision.eligible is True
    assert decision.reason_code == "eligible_continuation"
    assert decision.previous_consecutive_slots == 1
    assert decision.prospective_consecutive_slots == 2


def test_zero_extra_cooldown_still_requires_one_released_slot():
    policy = ActivationSequencePolicy(15, 2, 0)
    commitments = [_slot(0), _slot(1)]

    assert policy.evaluate("asset-x", _slot(2), commitments).eligible is False
    reopened = policy.evaluate("asset-x", _slot(3), commitments)
    assert reopened.eligible is True
    assert reopened.reason_code == "eligible_new_sequence"


class FakeCommitmentRepository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_committed_asset_slots(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


def test_coordinator_evaluates_assets_independently():
    repository = FakeCommitmentRepository([
        {"asset_id": "A", "slot_start": _slot(0)},
        {"asset_id": "A", "slot_start": _slot(1)},
        {"asset_id": "B", "slot_start": _slot(1)},
    ])
    coordinator = AssetCommitmentCoordinator(
        repository, logging.getLogger("test-coordinator")
    )

    decisions = coordinator.evaluate_assets(
        fsp_id="any-fsp",
        asset_ids=["A", "B"],
        target_slot=_slot(2),
        policy=ActivationSequencePolicy(15, 2, 2),
    )

    assert decisions["A"].eligible is False
    assert decisions["B"].eligible is True
    assert decisions["B"].prospective_consecutive_slots == 2
    assert coordinator.eligible_assets(decisions) == ["B"]
    assert repository.calls[0]["fsp_id"] == "any-fsp"
    assert repository.calls[0]["before_slot"] == _slot(2)


def test_coordinator_deduplicates_candidates_and_ignores_unknown_malformed_rows():
    repository = FakeCommitmentRepository([
        {"asset_id": "A", "slot_start": _slot(1)},
        {"asset_id": "unknown", "slot_start": _slot(1)},
        {"asset_id": "B", "slot_start": "not-a-datetime"},
        {"slot_start": _slot(1)},
    ])
    coordinator = AssetCommitmentCoordinator(repository)

    decisions = coordinator.evaluate_assets(
        fsp_id="fsp",
        asset_ids=["A", "A", "B"],
        target_slot=_slot(2),
        policy=ActivationSequencePolicy(15, 2, 2),
    )

    assert list(decisions) == ["A", "B"]
    assert decisions["A"].reason_code == "eligible_continuation"
    assert decisions["B"].reason_code == "eligible_new_sequence"
    assert repository.calls[0]["asset_ids"] == ["A", "B"]
    assert repository.calls[0]["from_slot"] == _slot(-4)


def test_coordinator_with_no_candidates_skips_repository_query(caplog):
    repository = FakeCommitmentRepository([])
    coordinator = AssetCommitmentCoordinator(repository)

    with caplog.at_level(logging.INFO):
        decisions = coordinator.evaluate_assets(
            fsp_id="fsp",
            asset_ids=[],
            target_slot=_slot(2),
            policy=ActivationSequencePolicy(15, 2, 2),
        )

    assert decisions == {}
    assert repository.calls == []
    assert "ACTIVATION_SEQUENCE_HISTORY_SKIPPED" in caplog.text


def test_coordinator_logs_query_block_decision_and_summary(caplog):
    repository = FakeCommitmentRepository([
        {"asset_id": "A", "slot_start": _slot(0)},
        {"asset_id": "A", "slot_start": _slot(1)},
    ])
    coordinator = AssetCommitmentCoordinator(repository)

    with caplog.at_level(logging.INFO):
        coordinator.evaluate_assets(
            fsp_id="fsp",
            asset_ids=["A"],
            target_slot=_slot(2),
            policy=ActivationSequencePolicy(15, 2, 2),
        )

    assert "ACTIVATION_SEQUENCE_HISTORY_QUERY" in caplog.text
    assert "semantics=posted_sell_reservation_not_dispatch_outcome" in caplog.text
    assert "ACTIVATION_SEQUENCE_HISTORY_LOADED" in caplog.text
    assert "ACTIVATION_SEQUENCE_DECISION state=BLOCKED" in caplog.text
    assert "ACTIVATION_SEQUENCE_SUMMARY" in caplog.text
    assert "blocked_assets=['A']" in caplog.text


def test_policy_resolution_depends_on_keys_not_strategy_or_asset_type():
    config = {
        "arbitrarySettings": {
            "maxConsecutiveActivationSlots": 7,
            "cooldownSlotsAfterMaxActivation": 3,
        }
    }

    policy = resolve_activation_sequence_policy(config, slot_minutes=5)

    assert policy == ActivationSequencePolicy(5, 7, 3)


def test_policy_resolution_returns_none_when_strategy_does_not_opt_in():
    assert resolve_activation_sequence_policy(
        {"unrelated": {"limit": 2}}, slot_minutes=15
    ) is None


def test_policy_resolution_rejects_partial_or_ambiguous_configuration():
    with pytest.raises(ValueError, match="Incomplete"):
        resolve_activation_sequence_policy(
            {"settings": {"maxConsecutiveActivationSlots": 2}},
            slot_minutes=15,
        )

    with pytest.raises(ValueError, match="Multiple"):
        resolve_activation_sequence_policy(
            {
                "first": {
                    "maxConsecutiveActivationSlots": 2,
                    "cooldownSlotsAfterMaxActivation": 2,
                },
                "second": {
                    "maxConsecutiveActivationSlots": 4,
                    "cooldownSlotsAfterMaxActivation": 2,
                },
            },
            slot_minutes=15,
        )


def test_policy_resolution_can_apply_caller_supplied_generic_defaults():
    policy = resolve_activation_sequence_policy(
        {
            "settings": {
                "maxConsecutiveActivationSlots": "invalid",
                "cooldownSlotsAfterMaxActivation": 1,
            }
        },
        slot_minutes=15,
        default_max_consecutive_slots=4,
        default_cooldown_slots=2,
        enabled_when_missing=True,
    )

    assert policy == ActivationSequencePolicy(15, 4, 1)

    missing = resolve_activation_sequence_policy(
        {"settings": {"unrelated": True}},
        slot_minutes=15,
        default_max_consecutive_slots=2,
        default_cooldown_slots=2,
        enabled_when_missing=True,
    )
    assert missing == ActivationSequencePolicy(15, 2, 2)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.params = None
        self.closed = False

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor


class FailingCursor(FakeCursor):
    def execute(self, query, params):
        super().execute(query, params)
        raise RuntimeError("database unavailable")


def test_repository_reads_live_generic_commitments_without_schema_changes():
    cursor = FakeCursor([
        {
            "asset_id": "A",
            "slot_start": _slot(0),
            "slot_end": _slot(1),
            "bid_record_id": "bid-1",
        }
    ])
    repository = BidRecordRepository.__new__(BidRecordRepository)
    repository.conn = FakeConnection(cursor)
    repository.logger = logging.getLogger("test-repository")

    rows = repository.get_committed_asset_slots(
        fsp_id="fsp",
        asset_ids=["A"],
        from_slot=_slot(-2),
        before_slot=_slot(2),
    )

    assert rows[0]["asset_id"] == "A"
    assert "market_ledger" in cursor.query
    assert "ml.side = 'Sell'" in cursor.query
    assert "ml.flexibility_quantity > 0" in cursor.query
    assert "CREATE TABLE" not in cursor.query
    assert "ALTER TABLE" not in cursor.query
    assert cursor.params == ("fsp", ["A"], _slot(-2), _slot(2))
    assert cursor.closed is True


def test_repository_empty_asset_list_does_not_open_cursor():
    class ConnectionThatMustNotBeUsed:
        def cursor(self, **kwargs):
            raise AssertionError("cursor must not be opened")

    repository = BidRecordRepository.__new__(BidRecordRepository)
    repository.conn = ConnectionThatMustNotBeUsed()
    repository.logger = logging.getLogger("test-repository-empty")

    assert repository.get_committed_asset_slots(
        fsp_id="fsp",
        asset_ids=[],
        from_slot=_slot(-2),
        before_slot=_slot(2),
    ) == []


def test_repository_query_error_is_propagated_and_cursor_is_closed():
    cursor = FailingCursor([])
    repository = BidRecordRepository.__new__(BidRecordRepository)
    repository.conn = FakeConnection(cursor)
    repository.logger = logging.getLogger("test-repository-error")

    with pytest.raises(RuntimeError, match="database unavailable"):
        repository.get_committed_asset_slots(
            fsp_id="fsp",
            asset_ids=["A"],
            from_slot=_slot(-2),
            before_slot=_slot(2),
        )

    assert cursor.closed is True
