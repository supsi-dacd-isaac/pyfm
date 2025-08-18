# import section
import random
import sys
from datetime import datetime, timedelta
from influxdb import DataFrameClient
import pandas as pd

from classes.nodes_interface import NODESInterface


class Player:
    """
    Player (Flexibility Service Provider) class
    """

    def __init__(self, player_cfg, main_cfg, logger):
        """
        Constructor
        """
        self.cfg = player_cfg
        self.main_cfg = main_cfg
        self.logger = logger
        self.nodes_interface = NODESInterface(main_cfg["nodesAPI"], logger)
        self.nodes_interface.set_token(player_cfg)
        self.markets = []
        self.grid_nodes = []
        self.organization = None
        self.grid_area = None

        self.logger.info(
            "Connection to InfluxDb server on socket [%s:%s]"
            % (main_cfg["influxDB"]["host"], main_cfg["influxDB"]["port"])
        )
        try:
            self.influx_client = DataFrameClient(
                host=main_cfg["influxDB"]["host"],
                port=main_cfg["influxDB"]["port"],
                password=main_cfg["influxDB"]["password"],
                username=main_cfg["influxDB"]["user"],
                database=main_cfg["influxDB"]["database"],
                ssl=main_cfg["influxDB"]["ssl"],
            )
        except Exception as e:
            self.logger.error("EXCEPTION: %s" % str(e))
            sys.exit(3)
        self.logger.info("Connection successful")

    def set_markets(self, filter_dict=None):
        self.markets = self.get_nodes_api_info("markets", filter_dict)

    @staticmethod
    def get_adjusted_time(granularity, shift):
        current_time = datetime.utcnow()
        adj_time = current_time.replace(
            minute=(current_time.minute // granularity) * granularity
        )
        adj_time = adj_time.replace(second=0, microsecond=0)
        adj_time += timedelta(minutes=shift)
        return adj_time

    def set_organization(self, filter_dict=None):
        orgs = self.get_nodes_api_info("organizations", filter_dict)
        if len(orgs) == 1:
            self.organization = orgs[0]
        else:
            self.logger.error(
                "Unable to get organization information with this filter: %s"
                % filter_dict
            )

    def set_grid_area(self, filter_dict=None):
        ga = self.get_nodes_api_info("GridAreas", filter_dict)
        if len(ga) == 1:
            self.grid_area = ga[0]
        else:
            self.logger.error(
                "Unable to get the grid area information with this filter: %s"
                % filter_dict
            )

    def set_grid_nodes(self, filter_dict=None):
        self.grid_nodes = self.get_nodes_api_info("gridnodes", filter_dict)

    def get_resolutions(self):
        return self.nodes_interface.get_request(
            "%s%s"
            % (self.nodes_interface.cfg["mainEndpoint"], "settlements/resolutions")
        )

    def get_orders(self, filter_dict=None):
        return self.get_nodes_api_info("orders", filter_dict)

    def get_contracts(self, filter_dict=None):
        return self.get_nodes_api_info("longflexcontracts", filter_dict)

    def get_nodes_api_info(self, request_type, filter_dict=None):
        if filter_dict is not None:
            filter_str = "?"
            for k in filter_dict.keys():
                filter_str += "%s=%s&" % (k, filter_dict[k])
            filter_str = filter_str[:-1]
        else:
            filter_str = ""
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "%s%s" % (request_type, filter_str),
            )
        )
        if "items" in res.keys():
            return res["items"]
        else:
            return []

    def calculate_unit_price(self, dt_slot):
        price = 0.0
        if self.cfg["orderSection"]["unitPrice"]["source"] == "constant":
            price = self.cfg["orderSection"]["unitPrice"]["constant"]
        elif self.cfg["orderSection"]["unitPrice"]["source"] == "forecast":
            slot_utc = pd.Timestamp(dt_slot).tz_localize("UTC")
            forecasting = self.get_forecast()
            forecasting_min = min(forecasting)
            forecasting_max = max(forecasting)
            forecasting_dt = forecasting[forecasting.index >= slot_utc].iloc[0]
            ratio = (forecasting_dt - forecasting_min) / (
                forecasting_max - forecasting_min
            )
            price = (
                self.cfg["orderSection"]["unitPrice"]["forecast_base"]
                + ratio * self.cfg["orderSection"]["unitPrice"]["forecast_multiplier"]
            )
        else:
            self.logger.error(
                "Option '%s' not available for unit price calculation"
                % self.cfg["orderSection"]["unitPrice"]["source"]
            )
            return 0.0

        # As the month progresses risk of a peak is higher so we increase bidding price
        if "day_of_month_max_increase" in self.cfg["orderSection"]["unitPrice"]:
            month_days = (
                datetime(datetime.now().year, datetime.now().month % 12 + 1, 1)
                - timedelta(days=1)
            ).day
            current_day = datetime.now().day
            day_of_month_max_increase = (
                current_day
                / month_days
                * self.cfg["orderSection"]["unitPrice"]["day_of_month_max_increase"]
            )
            price += day_of_month_max_increase
        return round(price, 2)

    def demand_flexibility(self, dt_slot):
        # Get demanded quantity
        if self.cfg["flexibilitySource"] == "random":
            demanded_flexibility = self.get_random_quantity()
        elif self.cfg["flexibilitySource"] == "db":
            demanded_flexibility = self.get_quantity_from_db(dt_slot)
        elif self.cfg["flexibilitySource"] == "forecast":
            demanded_flexibility = self.get_quantity_from_forecast(dt_slot)
        else:
            self.logger.error(
                "Option '%s' not available for demanding flexibility "
                "strategy" % self.cfg["flexibilitySource"]
            )
            return False

        # Check that demanded flexibility is not zero
        if demanded_flexibility == 0.0:
            self.logger.info(
                "No flexibility demanded for slot %s"
                % dt_slot.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            return False

        # Get the node id configured to demand flexibility
        node_id = None
        for n in self.grid_nodes:
            if n["name"] in self.cfg["orderSection"]["nodeName"]:
                node_id = n["id"]

        body = {
            "ownerOrganizationId": self.organization["id"],
            "periodFrom": dt_slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "periodTo": (dt_slot + timedelta(minutes=15)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "validTo": (dt_slot + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "marketId": self.markets[0]["id"],
            "gridNodeId": node_id,
            "quantity": demanded_flexibility,
            "unitPrice": self.calculate_unit_price(dt_slot),
        }
        body.update(self.cfg["orderSection"]["mainSettings"])
        response = self.nodes_interface.post_request(
            "%s%s" % (self.nodes_interface.cfg["mainEndpoint"], "orders"), body
        )
        return self.handle_response(response, body)

    def get_random_quantity(self):
        index = random.randint(
            0, len(self.cfg["orderSection"]["quantities"]["random"]) - 1
        )
        return self.cfg["orderSection"]["quantities"]["random"][index]

    def get_quantity_from_db(self, dt_slot):
        start_dt = dt_slot - timedelta(
            days=self.cfg["orderSection"]["quantities"]["db"]["daysToGoBack"]
        )
        start_dt_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_dt_str = (
            start_dt + timedelta(minutes=self.main_cfg["fm"]["granularity"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        str_fields = ""
        for f in self.cfg["orderSection"]["quantities"]["db"]["fields"]:
            str_fields = "%s mean(%s)," % (str_fields, f)
        str_fields = str_fields[1:-1]

        query = (
            "SELECT %s from %s WHERE energy_community='%s' AND device_name='%s' AND time>='%s' AND time<'%s' "
            "GROUP BY time(%im)"
        ) % (
            str_fields,
            self.main_cfg["influxDB"]["measurement"],
            self.cfg["orderSection"]["quantities"]["db"]["community"],
            self.cfg["orderSection"]["quantities"]["db"]["device"],
            start_dt_str,
            end_dt_str,
            self.main_cfg["fm"]["granularity"],
        )

        self.logger.info("Query: %s" % query)
        try:
            res = self.influx_client.query(query)
            df_data = res[self.main_cfg["influxDB"]["measurement"]]
            return round(df_data.sum(axis=1).values[0] / 1e3, 3)
        except Exception as e:
            self.logger.error("EXCEPTION: %s" % str(e))
            return False

    def get_forecast(self):
        """Get forecasting timeseries for the day."""
        if self.cfg["forecast"]["source"] == "file":
            df = pd.read_csv(self.cfg["forecast"]["filename"], sep=",")
            df["slot"] = pd.to_datetime(df["slot"])
            df.set_index("slot", inplace=True)
            forecasting = df["quantity"]
            forecasting.index = forecasting.index.tz_localize("UTC")
        elif self.cfg["forecast"]["source"] == "aem":
            from aemDataManagement.data_storage.influxdb import Influx

            start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(
                days=1
            )  # TODO now we use previous day, update when forecasting will be available
            end = start + timedelta(days=1)
            aemInflux = Influx(
                influxdb_credentials=self.main_cfg["aemInfluxDB"]["token"],
                config=self.main_cfg["aemInfluxDB"],
            )
            forecasting = (
                aemInflux.read_key(
                    start, end, "sgim-aem003", "property", "CH1ActivePowL1"
                )["value"]
                .resample("15T")
                .mean()
            )  # TODO update when forecasting will be available
            forecasting.index = forecasting.index + timedelta(
                days=1
            )  # TODO remove when forecasting will be available
        else:
            self.logger.error(
                "Option '%s' not available for forecasting input "
                % self.cfg["forecast"]["source"]
            )
            raise Exception(
                "Option '%s' not available for forecasting input "
                % self.cfg["forecast"]["source"]
            )
        return forecasting

    def get_monthly_peak(self):
        """Get the peak value for the current month, supporting multiple sources (file, aem)."""
        if self.cfg["peak"]["source"] == "file":
            df = pd.read_csv(self.cfg["peak"]["filename"], sep=",")
            peak_value = df["quantity"].max()
        elif self.cfg["peak"]["source"] == "aem":
            from aemDataManagement.data_storage.influxdb import Influx

            start = datetime.utcnow().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            end = datetime.utcnow()
            aemInflux = Influx(
                influxdb_credentials=self.main_cfg["aemInfluxDB"]["token"],
                config=self.main_cfg["aemInfluxDB"],
            )
            history = (
                aemInflux.read_key(
                    start, end, "sgim-aem003", "property", "CH1ActivePowL1"
                )["value"]
                .resample("15T")
                .mean()
            )
            peak_value = history.max()
        else:
            self.logger.error(
                'Option "%s" not available for peak input ' % self.cfg["peak"]["source"]
            )
            raise Exception(
                'Option "%s" not available for peak input ' % self.cfg["peak"]["source"]
            )
        return peak_value

    def get_quantity_from_forecast(self, dt_slot):
        """Cut the forecasted value based on the configs or monthly peak."""
        forecasting = self.get_forecast()
        slot_utc = pd.Timestamp(dt_slot).tz_localize("UTC")
        forecasted_value = forecasting[forecasting.index >= slot_utc].iloc[0]
        cut_value = self.cfg["orderSection"]["quantities"]["forecasting_cut"]
        monthly_peak = self.get_monthly_peak()
        if monthly_peak > cut_value:
            self.logger.info(
                "Monthly peak %s kW is greater than cut value %s kW, using monthly peak."
                % (monthly_peak, cut_value)
            )
            cut_value = monthly_peak
        cut_value -= self.cfg["orderSection"]["quantities"][
            "cut_margin"
        ]  # Safety margin
        # As the month progresses risk of a peak is higher...
        if "day_of_month_cut_max_increase" in self.cfg["orderSection"]["quantities"]:
            month_days = (
                datetime(datetime.now().year, datetime.now().month % 12 + 1, 1)
                - timedelta(days=1)
            ).day
            current_day = datetime.now().day
            cut_value -= (
                current_day
                / month_days
                * self.cfg["orderSection"]["quantities"][
                    "day_of_month_cut_max_increase"
                ]
            )

        self.logger.info(
            "Forecasted value: %.3f kW, Threshold cut value: %.3f kW"
            % (forecasted_value, cut_value)
        )
        return round(max(forecasted_value - cut_value, 0.0) / 1000, 3)

    def check_demand_price_forecasting(self, dt, demand, quantity_to_sell):
        """
        Check if the demand price is acceptable for the player to sell flexibility based on forecasted value for the day.
        """
        forecasting = self.get_forecast()

        forecasting_min = min(forecasting)
        forecasting_max = max(forecasting)
        forecasting_dt = forecasting[forecasting.index >= dt].iloc[0]
        potential_ratio = (forecasting_dt - forecasting_min) / (
            forecasting_max - forecasting_min
        )
        potential = (
            potential_ratio
            * quantity_to_sell
            * self.cfg["pricing"]["forecasting_multiplier"]
        )
        min_price = self.cfg["pricing"]["activationCost"] + potential
        if demand["unitPrice"] * quantity_to_sell < min_price:
            self.logger.info(
                "Demand price %.3f is lower than the minimum acceptable price %.3f, order not accepted."
                % (demand["unitPrice"] * quantity_to_sell, min_price)
            )
            return False
        self.logger.info(
            "Demand price %.3f is acceptable, potential %.3f, minimum price %.3f"
            % (demand["unitPrice"] * quantity_to_sell, min_price)
        )
        return True

    def check_demand_price(self, dt, demand, quantity_to_sell):
        """
        Check if the demand price is acceptable for the player to sell flexibility.
        """
        if self.cfg["pricing"]["source"] == "constant":
            min_price = self.cfg["pricing"]["constant"] * quantity_to_sell
            if demand["unitPrice"] * quantity_to_sell < min_price:
                self.logger.info(
                    "Demand price %.3f is lower than the minimum acceptable price %.3f, order not accepted."
                    % (demand["unitPrice"] * quantity_to_sell, min_price)
                )
                return False
            self.logger.info(
                "Demand price %.3f is acceptable, minimum price %.3f"
                % (demand["unitPrice"] * quantity_to_sell, min_price)
            )
            return True
        elif self.cfg["pricing"]["source"] == "forecast":
            return self.check_demand_price_forecasting(dt, demand, quantity_to_sell)
        else:
            self.logger.error(
                "Option '%s' not available for demand price checking"
                % self.cfg["pricing"]["source"]
            )
            return False

    def sell_flexibility(self, dt, p_id, dso_demand):
        selling_result = {}
        for k_regulation_type in ["Up", "Down"]:
            quantity_to_sell = self.calculate_quantity_to_sell_basic(
                dt, dso_demand[k_regulation_type], self.baselines[p_id]["quantity"]
            )

            self.logger.info(
                "Portfolio: %s, Regulation: %s, Bidded flexibility: %s"
                % (p_id, k_regulation_type, quantity_to_sell)
            )
            # Check if the quantity to sell is greater than zero and if the demand price is acceptable
            if quantity_to_sell > 0 and self.check_demand_price(
                dt, dso_demand, quantity_to_sell
            ):
                body = {
                    "ownerOrganizationId": self.organization["id"],
                    "periodFrom": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "periodTo": (dt + timedelta(minutes=15)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "validTo": (dt + timedelta(minutes=15)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "marketId": self.markets[0]["id"],
                    "assetPortfolioId": p_id,
                    "regulationType": k_regulation_type,
                    "quantity": quantity_to_sell,
                    "unitPrice": dso_demand["unitPrice"],
                }
                body.update(self.cfg["orderSection"]["mainSettings"])
                response = self.nodes_interface.post_request(
                    "%s%s" % (self.nodes_interface.cfg["mainEndpoint"], "orders"), body
                )
                if response is not False:
                    selling_result[k_regulation_type] = body
                else:
                    selling_result[k_regulation_type] = False
            else:
                selling_result[k_regulation_type] = False
        return selling_result

    def print_user_info(self, user_info):
        self.logger.info("user id: %s" % user_info["user"]["id"])
        self.logger.info("user givenname: %s" % user_info["user"]["givenName"])
        self.logger.info("user familyname: %s" % user_info["user"]["familyName"])
        self.logger.info("user type: %s" % user_info["user"]["userType"])

    def print_player_info(self):
        self.logger.info("market id: %s" % self.markets[0]["id"])
        self.logger.info("market name: %s" % self.markets[0]["name"])

    def get_flexibility_quantities(
        self, slot_time, granularity, order_type, quantity_type
    ):
        # Deprecated method, get_flexibility_requests should be used instead (return a list of requests instad of a sum)
        filter_dict = {
            "ownerOrganizationId": self.organization["id"],
            "periodFrom": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "periodTo": (slot_time + timedelta(minutes=granularity)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "type": order_type,
            "quantityType": quantity_type,
        }
        orders = self.get_orders(filter_dict=filter_dict)

        quantity_up = 0.0
        quantity_dn = 0.0
        for order in orders:
            # Only not-settled order will be considered
            if order["completionType"] is None:
                if order["regulationType"] == "Down":
                    quantity_dn += float(order["quantity"])
                elif order["regulationType"] == "Up":
                    quantity_up += float(order["quantity"])

        quantity_up = round(quantity_up, 3)
        quantity_dn = round(quantity_dn, 3)
        self.logger.info(
            "Flexibility demanded by the DSO (%s): Up = %.3f MW, Down = %.3f MW"
            % (quantity_type, quantity_up, quantity_dn)
        )
        return {"Up": quantity_up, "Down": quantity_dn}

    def get_flexibility_requests(
        self, slot_time, granularity, order_type, quantity_type
    ):
        # Deprecated method
        filter_dict = {
            "ownerOrganizationId": self.organization["id"],
            "periodFrom": slot_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "periodTo": (slot_time + timedelta(minutes=granularity)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "type": order_type,
            "quantityType": quantity_type,
        }
        orders = self.get_orders(filter_dict=filter_dict)

        requests = []
        for order in orders:
            request = {}
            # Only not-settled order will be considered
            if order["completionType"] is None:
                if order["regulationType"] == "Down":
                    request["Down"] = float(order["quantity"])
                    request["Up"] = 0.0
                elif order["regulationType"] == "Up":
                    request["Up"] = float(order["quantity"])
                    request["Down"] = 0.0
                request["unitPrice"] = float(order["unitPrice"])
                requests.append(request)
                self.logger.info(
                    "Flexibility demanded by the DSO (%s): Up = %.3f MW, Down = %.3f MW, Price = %.3f"
                    % (
                        quantity_type,
                        request["Up"],
                        request["Down"],
                        request["unitPrice"],
                    )
                )
        return requests

    def calculate_quantity_to_sell_basic(self, timeslot, demand, baseline_time_series):
        if demand > 0:
            # Basic approach, only the baseline value of the timeslot is taking into account,
            # past and future are not considered
            baseline = baseline_time_series.loc[timeslot.strftime("%Y-%m-%dT%H:%M:%SZ")]
            self.logger.info(
                "Baseline: %.3f MW (%i%% to be sold)"
                % (baseline, self.cfg["orderSection"]["quantityPercBaseline"])
            )
            marketable_quantity = round(
                baseline * self.cfg["orderSection"]["quantityPercBaseline"] / 1e2, 3
            )

            if marketable_quantity <= demand:
                # Everything is sold
                return marketable_quantity
            else:
                # Only a part of the flexibility is sold covering the entire demand
                return demand
        else:
            return 0.0

    @staticmethod
    def handle_response(response, body):
        if response is True:
            return body
        else:
            return response
