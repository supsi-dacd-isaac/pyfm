import logging
import sys
import types
from datetime import datetime, timedelta
from importlib.machinery import ModuleSpec
from importlib.util import find_spec


try:
    psycopg2_missing = find_spec("psycopg2") is None
except ValueError:
    psycopg2_missing = True
if psycopg2_missing:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_stub.__spec__ = ModuleSpec("psycopg2", loader=None)
    psycopg2_extras_stub.__spec__ = ModuleSpec("psycopg2.extras", loader=None)
    psycopg2_extras_stub.RealDictCursor = object
    psycopg2_stub.extras = psycopg2_extras_stub
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_extras_stub


from classes.bid_record_repository import BidRecordRepository  # noqa: E402


class InMemoryBidRecordCursor:
    def __init__(self, conn):
        self.conn = conn
        self._one = None
        self._many = []

    def execute(self, sql, params=None):
        params = params or ()
        normalized = " ".join(sql.lower().split())

        if normalized.startswith("select id from public.bid_records"):
            fsp_id, slot_start = params
            record = self.conn.find_record(fsp_id, slot_start)
            self._one = (record["id"],) if record else None
            return

        if normalized.startswith("insert into public.bid_records"):
            (
                fsp_id,
                slot_start,
                slot_end,
                strategy_id,
                strategy_name,
                strategy_description,
                total_quantity_mw,
                dso_offered_price,
                fsp_min_price,
                actual_price,
                currency,
            ) = params
            record_id = f"bid-{len(self.conn.records) + 1}"
            self.conn.records[record_id] = {
                "id": record_id,
                "fsp_id": fsp_id,
                "slot_start": slot_start,
                "slot_end": slot_end,
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "strategy_description": strategy_description,
                "total_quantity_mw": total_quantity_mw,
                "dso_offered_price": dso_offered_price,
                "fsp_min_price": fsp_min_price,
                "actual_price": actual_price,
                "currency": currency,
                "status": "pending",
            }
            self._one = (record_id,)
            return

        if normalized.startswith("update public.bid_records"):
            (
                strategy_id,
                strategy_name,
                strategy_description,
                total_quantity_mw,
                dso_offered_price,
                fsp_min_price,
                actual_price,
                currency,
                record_id,
            ) = params
            self.conn.records[record_id].update(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": strategy_name,
                    "strategy_description": strategy_description,
                    "total_quantity_mw": total_quantity_mw,
                    "dso_offered_price": dso_offered_price,
                    "fsp_min_price": fsp_min_price,
                    "actual_price": actual_price,
                    "currency": currency,
                    "status": "pending",
                }
            )
            return

        if normalized.startswith("delete from public.bid_record_orders"):
            record_id = params[0]
            self.conn.orders = [
                row for row in self.conn.orders if row["bid_record_id"] != record_id
            ]
            return

        if normalized.startswith("delete from public.bid_record_assets"):
            record_id = params[0]
            self.conn.assets = [
                row for row in self.conn.assets if row["bid_record_id"] != record_id
            ]
            return

        if normalized.startswith("insert into public.bid_record_orders"):
            (
                bid_record_id,
                portfolio,
                regulation_type,
                quantity_mw,
                unit_price,
                time_slot_name,
                period_from,
                period_to,
            ) = params
            self.conn.orders.append(
                {
                    "bid_record_id": bid_record_id,
                    "portfolio": portfolio,
                    "regulation_type": regulation_type,
                    "quantity_mw": quantity_mw,
                    "unit_price": unit_price,
                    "time_slot_name": time_slot_name,
                    "period_from": period_from,
                    "period_to": period_to,
                }
            )
            return

        if normalized.startswith("insert into public.bid_record_assets"):
            (
                bid_record_id,
                asset_id,
                description,
                asset_type,
                available_flexibility_kw,
                flexibility_factor,
                reference_power_kw,
                reference_power_source,
            ) = params
            self.conn.assets.append(
                {
                    "bid_record_id": bid_record_id,
                    "asset_id": asset_id,
                    "description": description,
                    "asset_type": asset_type,
                    "available_flexibility_kw": available_flexibility_kw,
                    "flexibility_factor": flexibility_factor,
                    "reference_power_kw": reference_power_kw,
                    "reference_power_source": reference_power_source,
                }
            )
            return

        if normalized.startswith("select * from public.bid_records"):
            fsp_id, slot_start = params
            record = self.conn.find_record(fsp_id, slot_start)
            self._one = dict(record) if record else None
            return

        if normalized.startswith("select * from public.bid_record_orders"):
            record_id = params[0]
            self._many = [
                dict(row) for row in self.conn.orders if row["bid_record_id"] == record_id
            ]
            return

        if normalized.startswith("select * from public.bid_record_assets"):
            record_id = params[0]
            self._many = [
                dict(row) for row in self.conn.assets if row["bid_record_id"] == record_id
            ]
            return

        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many

    def close(self):
        pass


class InMemoryBidRecordConnection:
    def __init__(self):
        self.records = {}
        self.orders = []
        self.assets = []

    def cursor(self, cursor_factory=None):
        return InMemoryBidRecordCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def find_record(self, fsp_id, slot_start):
        return next(
            (
                record
                for record in self.records.values()
                if record["fsp_id"] == fsp_id and record["slot_start"] == slot_start
            ),
            None,
        )


def _repository(conn):
    repo = BidRecordRepository.__new__(BidRecordRepository)
    repo.conn = conn
    repo.logger = logging.getLogger("test_bid_record_repository_reference_power")
    return repo


def test_save_bid_record_persists_reference_power_fields(caplog):
    conn = InMemoryBidRecordConnection()
    repo = _repository(conn)
    slot_start = datetime(2026, 5, 26, 11, 0)

    with caplog.at_level(logging.INFO):
        bid_id = repo.save_bid_record(
            fsp_id="fsp-1",
            slot_start=slot_start,
            slot_end=slot_start + timedelta(minutes=15),
            orders=[],
            strategy_id="strategy_10",
            assets_to_activate=[
                {
                    "asset_id": "ECM63.2",
                    "description": "EV 2",
                    "asset_type": "ev_charger",
                    "available_flexibility_kw": 2.99,
                    "flexibility_factor": 0.5,
                    "reference_power_kw": 5.98,
                    "reference_power_source": "recent_profile_baseline",
                }
            ],
        )

    assert bid_id == "bid-1"
    assert conn.assets[0]["reference_power_kw"] == 5.98
    assert conn.assets[0]["reference_power_source"] == "recent_profile_baseline"
    assert "Saving bid-record asset" in caplog.text
    assert "reference_power_kw=5.98" in caplog.text


def test_save_bid_record_persists_null_reference_power_for_legacy_assets():
    conn = InMemoryBidRecordConnection()
    repo = _repository(conn)
    slot_start = datetime(2026, 5, 26, 11, 0)

    repo.save_bid_record(
        fsp_id="fsp-1",
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=15),
        orders=[],
        strategy_id="strategy_8",
        assets_to_activate=[
            {
                "asset_id": "ECM63.1",
                "description": "EV 1",
                "asset_type": "ev_charger",
                "available_flexibility_kw": 2.0,
                "flexibility_factor": 0.5,
            }
        ],
    )

    assert conn.assets[0]["reference_power_kw"] is None
    assert conn.assets[0]["reference_power_source"] is None


def test_get_bid_record_returns_reference_power_fields_after_round_trip():
    conn = InMemoryBidRecordConnection()
    repo = _repository(conn)
    slot_start = datetime(2026, 5, 26, 11, 0)

    repo.save_bid_record(
        fsp_id="fsp-1",
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=15),
        orders=[],
        strategy_id="strategy_10",
        strategy_name="Recent profile EV",
        assets_to_activate=[
            {
                "asset_id": "ECM63.2",
                "description": "EV 2",
                "asset_type": "ev_charger",
                "available_flexibility_kw": 2.99,
                "flexibility_factor": 0.5,
                "reference_power_kw": 5.98,
                "reference_power_source": "recent_profile_baseline",
            }
        ],
    )

    record = repo.get_bid_record("fsp-1", slot_start)
    asset = record["assets_to_activate"][0]

    assert asset["reference_power_kw"] == 5.98
    assert asset["reference_power_source"] == "recent_profile_baseline"
    assert record["strategy"]["id"] == "strategy_10"
