"""Strategy-agnostic coordination of consecutive asset commitments.

The coordinator deliberately separates policy from persistence:

* :class:`ActivationSequencePolicy` evaluates an ordered series of generic
  asset-slot commitments.  It knows nothing about strategies, asset types,
  NODES, or PostgreSQL.
* :class:`AssetCommitmentCoordinator` obtains committed slots from a
  repository and applies the supplied policy independently to every asset.

The repository is expected to expose ``get_committed_asset_slots``.  The
existing :class:`BidRecordRepository` implements that method from the current
bid, bid-asset, and market-ledger tables; no strategy-specific schema is
required.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def _as_utc_naive(value: datetime) -> datetime:
    """Normalize database/API datetimes to the repository's naive-UTC form."""
    if not isinstance(value, datetime):
        raise TypeError("slot values must be datetime instances")
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class ActivationSequenceDecision:
    """Result of evaluating one asset for one target slot."""

    asset_id: str
    target_slot: datetime
    eligible: bool
    reason_code: str
    reason: str
    previous_consecutive_slots: int
    prospective_consecutive_slots: int
    blocked_until_slot: Optional[datetime] = None
    next_cooldown_until_slot: Optional[datetime] = None
    conflicting_committed_slots: Tuple[datetime, ...] = ()


@dataclass(frozen=True)
class ActivationSequencePolicy:
    """Generic maximum-consecutive/cooldown policy for slotted commitments."""

    slot_minutes: int
    max_consecutive_slots: int
    cooldown_slots: int

    def __post_init__(self) -> None:
        if self.slot_minutes <= 0:
            raise ValueError("slot_minutes must be > 0")
        if self.max_consecutive_slots < 1:
            raise ValueError("max_consecutive_slots must be >= 1")
        if self.cooldown_slots < 0:
            raise ValueError("cooldown_slots must be >= 0")

    @property
    def slot_duration(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)

    @property
    def release_slots(self) -> int:
        # At least one released slot is needed to break a consecutive control
        # sequence, even when an explicit extra cooldown was configured as 0.
        return max(1, self.cooldown_slots)

    @property
    def history_slots(self) -> int:
        """Bounded history sufficient to reconstruct the current state."""
        return self.max_consecutive_slots + self.release_slots + 2

    def evaluate(
        self,
        asset_id: str,
        target_slot: datetime,
        committed_slots: Iterable[datetime],
    ) -> ActivationSequenceDecision:
        """Evaluate whether ``asset_id`` may be committed at ``target_slot``.

        Only commitments strictly before the target are considered.  A
        commitment found inside a previously established release window is
        recorded as a historical conflict and conservatively extends release.
        """
        target = _as_utc_naive(target_slot)
        normalized_slots = set()
        for slot in committed_slots:
            normalized = _as_utc_naive(slot)
            if normalized < target:
                normalized_slots.add(normalized)
        slots = sorted(normalized_slots)
        duration = self.slot_duration

        previous_count = 0
        last_allowed_slot = None
        blocked_until = None
        conflicts: List[datetime] = []

        for slot in slots:
            if blocked_until is not None and slot < blocked_until:
                conflicts.append(slot)
                # A legacy or concurrently-created commitment inside a release
                # window is itself a delivery obligation.  Extend the window
                # conservatively so a finite history query cannot interpret a
                # long invalid run as an early restart.
                blocked_until = max(
                    blocked_until,
                    slot + duration * (self.release_slots + 1),
                )
                continue

            if blocked_until is not None and slot >= blocked_until:
                previous_count = 0
                last_allowed_slot = None
                blocked_until = None

            if last_allowed_slot is not None and slot == last_allowed_slot + duration:
                previous_count += 1
            else:
                previous_count = 1

            last_allowed_slot = slot
            if previous_count >= self.max_consecutive_slots:
                blocked_until = slot + duration * (self.release_slots + 1)

        if blocked_until is not None and target < blocked_until:
            return ActivationSequenceDecision(
                asset_id=asset_id,
                target_slot=target,
                eligible=False,
                reason_code="sequence_cooldown",
                reason=(
                    f"maximum of {self.max_consecutive_slots} consecutive slots "
                    f"reached; asset is released until {blocked_until.isoformat()}"
                ),
                previous_consecutive_slots=previous_count,
                prospective_consecutive_slots=0,
                blocked_until_slot=blocked_until,
                conflicting_committed_slots=tuple(conflicts),
            )

        if blocked_until is not None and target >= blocked_until:
            previous_count = 0
            last_allowed_slot = None
            blocked_until = None

        is_continuation = (
            last_allowed_slot is not None
            and target == last_allowed_slot + duration
        )
        prospective_count = previous_count + 1 if is_continuation else 1
        next_cooldown_until = None
        if prospective_count >= self.max_consecutive_slots:
            next_cooldown_until = target + duration * (self.release_slots + 1)

        return ActivationSequenceDecision(
            asset_id=asset_id,
            target_slot=target,
            eligible=True,
            reason_code=(
                "eligible_continuation" if is_continuation else "eligible_new_sequence"
            ),
            reason=(
                f"continuation slot {prospective_count} of "
                f"{self.max_consecutive_slots}"
                if is_continuation
                else "new activation sequence"
            ),
            previous_consecutive_slots=previous_count if is_continuation else 0,
            prospective_consecutive_slots=prospective_count,
            next_cooldown_until_slot=next_cooldown_until,
            conflicting_committed_slots=tuple(conflicts),
        )


def resolve_activation_sequence_policy(
    config: Optional[Mapping],
    slot_minutes: int,
    default_max_consecutive_slots: Optional[int] = None,
    default_cooldown_slots: Optional[int] = None,
    enabled_when_missing: bool = False,
) -> Optional[ActivationSequencePolicy]:
    """Find generic sequence settings in a strategy configuration mapping.

    The lookup is based only on the setting names, not on a strategy ID,
    flexibility method, or asset type.  At most one nested settings mapping is
    accepted.  Callers may provide generic defaults to preserve an existing
    validation/fallback contract; otherwise partial or invalid settings fail.
    """
    defaults_available = (
        default_max_consecutive_slots is not None
        and default_cooldown_slots is not None
    )
    if not isinstance(config, Mapping):
        if enabled_when_missing and defaults_available:
            return ActivationSequencePolicy(
                slot_minutes=int(slot_minutes),
                max_consecutive_slots=int(default_max_consecutive_slots),
                cooldown_slots=int(default_cooldown_slots),
            )
        return None

    required = {
        "maxConsecutiveActivationSlots",
        "cooldownSlotsAfterMaxActivation",
    }
    candidates: List[Mapping] = []

    def visit(value) -> None:
        if not isinstance(value, Mapping):
            return
        present = required.intersection(value.keys())
        if present:
            if present != required and not defaults_available:
                missing = sorted(required - present)
                raise ValueError(
                    "Incomplete activation sequence configuration; missing "
                    + ", ".join(missing)
                )
            candidates.append(value)
            return
        for child in value.values():
            if isinstance(child, Mapping):
                visit(child)

    visit(config)
    if not candidates:
        if enabled_when_missing and defaults_available:
            return ActivationSequencePolicy(
                slot_minutes=int(slot_minutes),
                max_consecutive_slots=int(default_max_consecutive_slots),
                cooldown_slots=int(default_cooldown_slots),
            )
        return None
    if len(candidates) > 1:
        raise ValueError(
            "Multiple activation sequence configurations found in one strategy"
        )

    settings = candidates[0]
    raw_max = settings.get(
        "maxConsecutiveActivationSlots",
        default_max_consecutive_slots,
    )
    raw_cooldown = settings.get(
        "cooldownSlotsAfterMaxActivation",
        default_cooldown_slots,
    )

    def normalize(raw_value, default_value, minimum):
        try:
            normalized = int(raw_value)
            if normalized < minimum:
                raise ValueError("sequence value outside its valid range")
            return normalized
        except (TypeError, ValueError):
            if default_value is None:
                raise
            return int(default_value)

    max_slots = normalize(raw_max, default_max_consecutive_slots, 1)
    cooldown_slots = normalize(raw_cooldown, default_cooldown_slots, 0)

    return ActivationSequencePolicy(
        slot_minutes=int(slot_minutes),
        max_consecutive_slots=max_slots,
        cooldown_slots=cooldown_slots,
    )


class AssetCommitmentCoordinator:
    """Apply a supplied sequence policy to generic repository commitments."""

    def __init__(self, repository, logger: Optional[logging.Logger] = None):
        if repository is None or not hasattr(repository, "get_committed_asset_slots"):
            raise ValueError(
                "repository must provide get_committed_asset_slots"
            )
        self.repository = repository
        self.logger = logger or logging.getLogger(__name__)

    def evaluate_assets(
        self,
        fsp_id: str,
        asset_ids: Sequence[str],
        target_slot: datetime,
        policy: ActivationSequencePolicy,
    ) -> Dict[str, ActivationSequenceDecision]:
        """Return an independent sequence decision for every candidate asset."""
        unique_assets = list(dict.fromkeys(asset_ids or []))
        if not unique_assets:
            self.logger.info(
                "ACTIVATION_SEQUENCE_HISTORY_SKIPPED fsp_id=%s "
                "target_slot=%s reason=no_candidate_assets",
                fsp_id,
                _as_utc_naive(target_slot).isoformat(),
            )
            return {}

        target = _as_utc_naive(target_slot)
        history_from = target - policy.slot_duration * policy.history_slots
        self.logger.info(
            "ACTIVATION_SEQUENCE_HISTORY_QUERY fsp_id=%s target_slot=%s "
            "history_from=%s candidate_assets=%s max_consecutive_slots=%d "
            "cooldown_slots=%d source=bid_record_assets+market_ledger_sell "
            "semantics=posted_sell_reservation_not_dispatch_outcome",
            fsp_id,
            target.isoformat(),
            history_from.isoformat(),
            unique_assets,
            policy.max_consecutive_slots,
            policy.cooldown_slots,
        )
        rows = self.repository.get_committed_asset_slots(
            fsp_id=fsp_id,
            asset_ids=unique_assets,
            from_slot=history_from,
            before_slot=target,
        )

        slots_by_asset: Dict[str, List[datetime]] = {
            asset_id: [] for asset_id in unique_assets
        }
        for row in rows or []:
            asset_id = row.get("asset_id")
            slot_start = row.get("slot_start")
            if asset_id in slots_by_asset and isinstance(slot_start, datetime):
                slots_by_asset[asset_id].append(slot_start)

        self.logger.info(
            "ACTIVATION_SEQUENCE_HISTORY_LOADED fsp_id=%s target_slot=%s "
            "row_count=%d slots_by_asset=%s",
            fsp_id,
            target.isoformat(),
            len(rows or []),
            {
                asset_id: sorted(
                    {
                        _as_utc_naive(slot).isoformat()
                        for slot in asset_slots
                    }
                )
                for asset_id, asset_slots in slots_by_asset.items()
            },
        )

        decisions = {
            asset_id: policy.evaluate(
                asset_id=asset_id,
                target_slot=target,
                committed_slots=slots_by_asset[asset_id],
            )
            for asset_id in unique_assets
        }

        for asset_id, decision in decisions.items():
            log = self.logger.info if decision.eligible else self.logger.warning
            log(
                "ACTIVATION_SEQUENCE_DECISION state=%s asset=%s "
                "target_slot=%s eligible=%s reason_code=%s "
                "previous_consecutive=%d prospective_consecutive=%d "
                "blocked_until=%s next_cooldown_until=%s "
                "conflicting_committed_slots=%s",
                "ELIGIBLE" if decision.eligible else "BLOCKED",
                asset_id,
                target.isoformat(),
                decision.eligible,
                decision.reason_code,
                decision.previous_consecutive_slots,
                decision.prospective_consecutive_slots,
                (
                    decision.blocked_until_slot.isoformat()
                    if decision.blocked_until_slot
                    else None
                ),
                (
                    decision.next_cooldown_until_slot.isoformat()
                    if decision.next_cooldown_until_slot
                    else None
                ),
                [slot.isoformat() for slot in decision.conflicting_committed_slots],
            )

        eligible_assets = self.eligible_assets(decisions)
        blocked_assets = [
            asset_id
            for asset_id, decision in decisions.items()
            if not decision.eligible
        ]
        summary_log = self.logger.warning if blocked_assets else self.logger.info
        summary_log(
            "ACTIVATION_SEQUENCE_SUMMARY fsp_id=%s target_slot=%s "
            "eligible_assets=%s blocked_assets=%s",
            fsp_id,
            target.isoformat(),
            eligible_assets,
            blocked_assets,
        )

        return decisions

    @staticmethod
    def eligible_assets(
        decisions: Mapping[str, ActivationSequenceDecision],
    ) -> List[str]:
        return [
            asset_id
            for asset_id, decision in decisions.items()
            if decision.eligible
        ]
