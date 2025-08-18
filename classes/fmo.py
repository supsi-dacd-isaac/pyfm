# import section
import pandas as pd


class FMO:
    """
    FMO (Flexibility Martket Operator) class
    """

    def __init__(self, cfg, logger, pgi):
        """
        Constructor
        :param cfg: Dictionary with the configurable settings
        :type cfg: dict
        :param logger: Logger
        :type logger: Logger object
        """
        # Market ledger
        self.cfg = cfg
        self.ledger = pd.DataFrame()
        self.logger = logger
        ledger = pd.DataFrame(
            columns=[
                "market_timeslot",
                "created_at",
                "player",
                "role",
                "case",
                "portfolio",
                "amount",
                "unit",
                "price",
            ]
        )
        ledger["market_timeslot"] = pd.to_datetime(ledger["market_timeslot"])
        ledger["created_at"] = pd.to_datetime(ledger["created_at"])
        ledger["player"] = ledger["player"].astype(str)
        ledger["role"] = ledger["role"].astype(str)
        ledger["portfolio"] = ledger["portfolio"].astype(str)
        ledger["case"] = ledger["case"].astype(str)
        ledger["amount"] = ledger["amount"].astype(float)
        ledger["unit"] = ledger["unit"].astype(str)
        ledger["price"] = ledger["price"].astype(float)
        self.ledger = ledger
        self.pgi = pgi

    def __execute_sql(self, sql):
        if self.pgi is not None:
            cur = self.pgi.conn.cursor()
            cur.execute(sql)
            cur.close()
        else:
            self.logger.warning("PostgreSQL interface not available, logging instead.")
            self.logger.info("FMO unexecuted query: %s" % sql)

    def add_entry_to_market_ledger(self, timeslot, player, portfolio, features):
        if portfolio is None:
            portfolio_id = "none"
            price = 0.0
        else:
            portfolio_id = portfolio
            price = float(features["unitPrice"])

        sql = (
            "INSERT INTO market_ledger "
            "(timeslot_market, player_id, player_role, portfolio_id, side, regulation, flexibility_quantity, "
            "flexibility_unit, price, currency) VALUES"
            "('%s','%s','%s','%s','%s','%s','%s','%s','%s','%s')"
            % (
                timeslot,
                player.cfg["id"],
                player.cfg["role"],
                portfolio_id,
                features["side"],
                features["regulationType"],
                features["quantity"],
                "MW",
                float(price),
                self.cfg["orderSection"]["mainSettings"]["currency"],
            )
        )
        self.__execute_sql(sql)
        return True

    def add_entry_to_contract_request_ledger(self, player, market_name, request_data):
        sql = (
            "INSERT INTO contract_request_ledger "
            "(request_name, contract_type, period_from, period_to, buyer_id, buyer_role, market_id, regulation, "
            "flexibility_quantity, flexibility_unit, price_availability, price_unit, crontab) VALUES"
            "('%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s')"
            % (
                request_data["name"],
                request_data["contractType"],
                request_data["periodFrom"],
                request_data["periodTo"],
                player["id"],
                player["role"],
                market_name,
                request_data["regulationType"],
                float(request_data["quantity"]),
                request_data["quantityType"],
                float(request_data["availabilityPrice"]),
                float(request_data["unitPrice"]),
                request_data["crontab"],
            )
        )
        self.__execute_sql(sql)
        return True

    def add_entry_to_contract_proposal_ledger(
        self,
        player,
        organization_metadata,
        portfolio_metadata,
        contract_request_metadata,
        request_data,
    ):
        sql = (
            "INSERT INTO contract_proposal_ledger "
            "(contract_name, "
            "period_from, "
            "period_to, "
            "seller_id, "
            "seller_role, "
            "seller_organization_id, "
            "portfolio_id, "
            "request_contract_name, "
            "flexibility_quantity, "
            "price_availability, "
            "price_unit, "
            "crontab, "
            "auto_create_expiry, "
            "auto_create_expiry_relative_to) VALUES "
            "('%s','%s','%s','%s','%s','%s','%s','%s','%s','%s',"
            "'%s','%s','%s','%s')"
            % (
                request_data["name"],
                request_data["periodFrom"],
                request_data["periodTo"],
                player["id"],
                player["role"],
                organization_metadata["name"],
                portfolio_metadata["name"],
                contract_request_metadata["name"],
                float(request_data["quantity"]),
                float(request_data["availabilityPrice"]),
                float(request_data["unitPrice"]),
                request_data["crontab"],
                float(request_data["autoCreateExpiry"]),
                request_data["autoCreateExpiryRelativeTo"],
            )
        )
        self.__execute_sql(sql)
        return True

    def update_contract_request(self, contract_proposal, body):
        if self.pgi is not None:
            select_query = (
                "SELECT id FROM contract_proposal_ledger WHERE contract_name = %s;"
            )

            cur = self.pgi.conn.cursor()
            cur.execute(select_query, (contract_proposal["name"],))

            # Fetch all results from the executed query
            results = cur.fetchall()

            signature = 1 if body["approvedByBuyer"] else 2

            if len(results) == 1:
                update_query = """
                    UPDATE contract_proposal_ledger
                    SET approved_by_buyer = %s, visibility = %s
                    WHERE id = %s;  -- Assuming 'id' is the primary key column
                """

                new_values = (signature, body["visibility"], results[0][0])
                cur.execute(update_query, new_values)
                self.pgi.conn.commit()
                return True
            else:
                self.logger.warning("Found %i contracts instead of 1" % len(results))
                return False
        else:
            self.logger.warning("PostgreSQL interface not available, logging instead.")
            self.logger.info("FMO unexecuted query: %s" % body)
            return False
