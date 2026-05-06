# import section
import os
import copy
import math
import pandas as pd
from datetime import datetime, timedelta

from classes.asset import Asset
from classes.player import Player
from classes.portfolio import Portfolio


class FSP(Player):
    """
    FSP (Flexibility Service Provider) class
    """

    def __init__(self, fsp_cfg, main_cfg, logger):
        """
        Constructor
        """
        super().__init__(fsp_cfg, main_cfg, logger)

        # Get identifier of NODES platform
        res = self.get_organization_id()
        self.nodes_id = res["items"][0]["id"]

        # Portfolios owned by the FSP
        self.portfolios = {}
        for p in self.get_portfolios()["items"]:
            self.portfolios[p["id"]] = Portfolio(p["id"], p)

        # Asset owned by the FSP
        self.assets = {}
        for a in self.get_assets()["items"]:
            self.assets[a["id"]] = Asset(a["id"], a)

        # Get asset assigned to portfolios
        self.assets_ids, self.assets_mpids = self.get_assets_portfolios_assignments()

        # Assign MPID to assets
        self.set_assets_mpids()

        # Assign assets to portfolios
        self.set_portfolios_assets()

        # Baselines
        self.baselines = {}

    def set_assets_mpids(self):
        for k_p in self.portfolios.keys():
            for a_id in self.assets_ids[k_p]:
                self.assets[a_id].set_mpid(self.assets_mpids[k_p][a_id])

    def set_portfolios_assets(self):
        for k_p in self.portfolios.keys():
            tmp_assets = []
            for a_id in self.assets_ids[k_p]:
                tmp_assets.append(self.assets[a_id])
            self.portfolios[k_p].set_assets(tmp_assets)

    def download_baselines(self, slot_time):
        slot_time_from = slot_time - timedelta(
            hours=self.cfg["baselines"]["fromBeforeNowHours"]
        )
        slot_time_to = slot_time + timedelta(
            hours=self.cfg["baselines"]["toAfterNowHours"]
        )

        from_str = slot_time_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = slot_time_to.strftime("%Y-%m-%dT%H:%M:%SZ")

        bs = {}
        for p_k in self.portfolios.keys():
            res = self.nodes_interface.get_request(
                "%s%s"
                % (
                    self.nodes_interface.cfg["mainEndpoint"],
                    "BaselineIntervals/portfoliobaseline?"
                    "assetPortfolioId=%s&"
                    "periodFrom=%s&"
                    "periodTo=%s&"
                    "resolutionInMinutes=%i"
                    % (p_k, from_str, to_str, self.main_cfg["fm"]["granularity"]),
                )
            )
            df = pd.DataFrame(res)
            df["periodFrom"] = pd.to_datetime(df["periodFrom"], utc=True).dt.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            df.set_index("periodFrom", inplace=True)

            bs[p_k] = df
        self.baselines = bs

    def update_portfolio_baseline(self, portfolio_id, baseline_dataframe):
        tmp_baseline_file = "%s%s%s.csv" % (
            self.cfg["baselines"]["tmpFolder"],
            os.sep,
            portfolio_id,
        )
        baseline_dataframe.to_csv(tmp_baseline_file, index=False)

        self.logger.info(
            "Update baseline of portfolio %s, period [%s-%s]"
            % (
                portfolio_id,
                baseline_dataframe["periodTo"].iloc[0],
                baseline_dataframe["periodTo"].iloc[-1],
            )
        )
        
        # Print baseline summary before uploading
        quantities = baseline_dataframe["quantity"].values
        times = pd.to_datetime(baseline_dataframe["periodFrom"]).dt.strftime("%H:%M")
        self.logger.info(
            "Baseline times: [%s]",
            ", ".join(times)
        )
        self.logger.info(
            "Baseline values (MW): [%s]",
            ", ".join([f"{q:.6f}" for q in quantities])
        )
        self.logger.info(
            "Baseline statistics (MW): avg=%.6f, stdev=%.6f, min=%.6f, max=%.6f",
            quantities.mean(),
            quantities.std(),
            quantities.min(),
            quantities.max()
        )

        endpoint = "%s%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            "BaselineIntervals/import",
        )
        return self.nodes_interface.post_csv_file_request(endpoint, tmp_baseline_file)

    def save_portfolio_baseline_to_influx(self, portfolio_id, baseline_dataframe, bs_cfg):
        influx_cfg = self.main_cfg["influxDB"]
        if not influx_cfg.get("saveBaselineMeasurement", False):
            self.logger.info(
                "InfluxDB baseline saving disabled; baseline for portfolio %s "
                "will not be saved",
                portfolio_id,
            )
            return True

        measurement = influx_cfg.get("baselineMeasurement")
        if not measurement:
            self.logger.warning(
                "InfluxDB baselineMeasurement not configured; baseline will not be saved"
            )
            return True

        df = baseline_dataframe.copy()
        df["periodFrom"] = pd.to_datetime(df["periodFrom"], utc=True)
        df["periodTo"] = pd.to_datetime(df["periodTo"], utc=True).dt.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        df.rename(
            columns={
                "assetPortfolioId": "asset_portfolio_id",
                "periodFrom": "period_from",
                "periodTo": "period_to",
                "quantityType": "quantity_type",
            },
            inplace=True,
        )
        df["quantity_w"] = df["quantity"] * 1e6
        df.drop(columns=["quantity"], inplace=True)
        df["fsp_id"] = self.cfg["id"]
        df["fsp_name"] = self.cfg["name"]
        df["market_name"] = self.main_cfg["fm"]["marketName"]
        df["source"] = bs_cfg["source"]
        df["granularity_minutes"] = self.main_cfg["fm"]["granularity"]
        df.set_index("period_from", inplace=True)

        tag_columns = [
            "asset_portfolio_id",
            "fsp_id",
            "fsp_name",
            "market_name",
            "source",
            "quantity_type",
        ]
        field_columns = ["quantity_w", "period_to", "granularity_minutes"]

        try:
            result = self.influx_client.write_points(
                df[tag_columns + field_columns],
                measurement,
                tag_columns=tag_columns,
                field_columns=field_columns,
                time_precision=influx_cfg.get("timePrecision", "ms"),
            )
        except Exception as e:
            self.logger.error(
                "Error saving baseline for portfolio %s to InfluxDB measurement %s: %s",
                portfolio_id,
                measurement,
                str(e),
            )
            return False
        if result is False:
            self.logger.error(
                "InfluxDB write returned false for portfolio %s measurement %s",
                portfolio_id,
                measurement,
            )
            return False

        self.logger.info(
            "Saved %i baseline points to InfluxDB measurement %s for portfolio %s",
            len(df),
            measurement,
            portfolio_id,
        )
        return True

    def get_assets_portfolios_assignments(self):
        tmp_assets_grid_assignments = {}
        for elem in self.get_assets_grid_assignments()["items"]:
            tmp_assets_grid_assignments[elem["id"]] = elem

        tmp_assets_portfolios_assignments = {}
        for p_k in self.portfolios.keys():
            tmp_assets_portfolios_assignments[
                p_k
            ] = self.get_assets_assigned_to_portfolio(p_k)

        # Cycle over the portfolios that have at least an assignment
        # assets_portfolios_assignments = {}
        assets_mpids = {}
        assets_ids = {}
        for k_p in tmp_assets_portfolios_assignments.keys():
            assets_mpids[k_p] = {}
            assets_ids[k_p] = []
            # Cycle over the asset assigned to the portfolio
            for p_assignment in tmp_assets_portfolios_assignments[k_p]["items"]:
                asset_id = tmp_assets_grid_assignments[
                    p_assignment["assetGridAssignmentId"]
                ]["assetId"]
                # assets_portfolios_assignments[asset_id] = k_p
                # assets_mpids[k_p].append(tmp_assets_grid_assignments[p_assignment['assetGridAssignmentId']]['mpid'])
                assets_mpids[k_p][asset_id] = tmp_assets_grid_assignments[
                    p_assignment["assetGridAssignmentId"]
                ]["mpid"]
                assets_ids[k_p].append(asset_id)
        return assets_ids, assets_mpids

    def get_organization_id(self):
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "organizations?name=%s" % self.cfg["name"],
            )
        )
        return res

    def get_assets(self):
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "assets?operatedByOrganizationId=%s" % self.nodes_id,
            )
        )
        return res

    def get_portfolios(self):
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "AssetPortfolios?managedByOrganizationId=%s" % self.nodes_id,
            )
        )
        return res

    def get_assets_assigned_to_portfolio(self, portfolio_id):
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "assetportfolioassignments?assetPortfolioId=%s" % portfolio_id,
            )
        )
        return res

    def get_assets_grid_assignments(self):
        res = self.nodes_interface.get_request(
            "%s%s"
            % (
                self.nodes_interface.cfg["mainEndpoint"],
                "assetgridassignments?managedByOrganizationId=%s" % self.nodes_id,
            )
        )
        return res

    def delete_baseline_interval(self, portfolio_id, from_period, to_period):
        endpoint = "%s%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            "BaselineIntervals?assetPortfolioId=%s&periodFrom=%s&periodTo=%s"
            % (portfolio_id, from_period, to_period),
        )
        res = self.nodes_interface.delete_request(endpoint)
        return res

    @staticmethod
    def calc_from_to_period(days):
        from_dt = datetime.utcnow()
        to_dt = from_dt + timedelta(days=days)
        return from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), to_dt.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def update_baselines(self, bs_cfg):
        current_time = datetime.utcnow()
        adjusted_time = current_time.replace(
            minute=(current_time.minute // 15) * 15, second=0, microsecond=0
        ) + timedelta(minutes=bs_cfg["shiftMinutes"])

        # Cycle over the portfolios
        for k_p in self.portfolios.keys():
            if bs_cfg["source"] == "file":
                df = self.create_df_baseline_from_file(
                    self.portfolios[k_p], adjusted_time, bs_cfg["fileSettings"]
                )
            elif bs_cfg["source"] == "db":
                df = self.create_df_baseline_from_db(
                    self.portfolios[k_p], adjusted_time, bs_cfg["dbSettings"]
                )
            else:
                self.logger.error(
                    "Baseline source option '%s' not available" % bs_cfg["source"]
                )
                return False
            if df is None:
                self.logger.warning(
                    "Skipping baseline update for portfolio %s: no data available",
                    k_p,
                )
                continue
            if self.update_portfolio_baseline(k_p, df) is False:
                self.logger.error("Baseline upload failed for portfolio %s", k_p)
                return False
            if self.save_portfolio_baseline_to_influx(k_p, df, bs_cfg) is False:
                return False
        return True

    def _build_zero_baseline_dataframe(self, portfolio, adjusted_time, bs_cfg):
        """
        Build a baseline DataFrame with zero quantities for portfolios with no assets.
        
        :param portfolio: Portfolio object
        :param adjusted_time: Adjusted start time (already shifted to current time)
        :param bs_cfg: Baseline configuration settings
        :return: DataFrame with zero baseline values
        """
        # Start from adjusted_time (current time), not from the past
        start_dt = adjusted_time
        end_dt = start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        granularity = self.main_cfg["fm"]["granularity"]
        
        # Generate time slots for FUTURE dates
        rows = []
        current_dt = start_dt
        while current_dt < end_dt:
            next_dt = current_dt + timedelta(minutes=granularity)
            rows.append({
                "assetPortfolioId": portfolio.id,
                "periodFrom": current_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "periodTo": next_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "quantity": 0.0,
                "quantityType": "Power",
            })
            current_dt = next_dt
        
        return pd.DataFrame(rows)

    def create_df_baseline_from_db(self, portfolio, adjusted_time, bs_cfg):
        start_dt = adjusted_time - timedelta(days=bs_cfg["daysToGoBack"])
        start_dt_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_dt_str = (
            start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Get asset mapping from root config (not dbSettings)
        asset_mapping = self.main_cfg.get("asset_mapping", {})
        
        # Build list of assets with their mapping info
        # Each entry: (site, device_name, field)
        asset_queries = []
        for asset in portfolio.assets:
            asset_name = asset.metadata.get("name")
            if asset_name and asset_name in asset_mapping:
                mapping = asset_mapping[asset_name]
                # Support both old format (string) and new format (dict)
                if isinstance(mapping, dict):
                    device_name = mapping.get("device_name_tag")
                    field = mapping.get("field", "active_power")
                else:
                    # Old format: mapping is just the device_name string
                    device_name = mapping
                    field = "active_power"
                
                # Site is the MPID (e.g., ECM63)
                site = asset.mpid
                if site and device_name:
                    asset_queries.append((site, device_name, field, asset_name))
                    self.logger.info(
                        "Asset %s -> site=%s, device=%s, field=%s",
                        asset_name, site, device_name, field
                    )
        
        if not asset_queries:
            self.logger.info(
                "Portfolio %s has no mapped assets; using zero baseline", portfolio.id
            )
            return self._build_zero_baseline_dataframe(portfolio, adjusted_time, bs_cfg)
        
        # Query each asset individually (different assets may have different fields)
        assets_measurement = self.main_cfg["influxDB"].get(
            "assetsMeasurement", "assets_data"
        )
        aggregated_data = {}
        
        for site, device_name, field, asset_name in asset_queries:
            query = (
                "SELECT MEAN(%s) FROM %s WHERE "
                "time>='%s' AND time<'%s' AND site='%s' AND device_name='%s' "
                "GROUP BY time(%im)"
            ) % (
                field,
                assets_measurement,
                start_dt_str,
                end_dt_str,
                site,
                device_name,
                self.main_cfg["fm"]["granularity"],
            )
            self.logger.info("Query for %s: %s" % (asset_name, query))
            
            try:
                res = self.influx_client.query(query)
                if res:
                    for key, df_data in res.items():
                        for idx in df_data.index:
                            timestamp = idx
                            value = df_data.loc[idx, "mean"]
                            if pd.notna(value):
                                if timestamp not in aggregated_data:
                                    aggregated_data[timestamp] = 0.0
                                aggregated_data[timestamp] += value
                                self.logger.debug(
                                    "  %s @ %s: %.2f W",
                                    asset_name, timestamp, value
                                )
            except Exception as e:
                self.logger.error(
                    "Error querying asset %s: %s", asset_name, str(e)
                )
                continue
        
        if not aggregated_data:
            self.logger.warning(
                "No valid data after aggregation for portfolio %s", portfolio.id
            )
            return None
        
        # Build DataFrame from aggregated data
        timestamps = sorted(aggregated_data.keys())
        df_data_bs = pd.DataFrame({
            "timestamp": timestamps,
            "quantity": [aggregated_data[ts] for ts in timestamps]
        })
        df_data_bs.set_index("timestamp", inplace=True)
        
        # CRITICAL: Shift timestamps from past to future (persistence model)
        df_data_bs.index = df_data_bs.index + pd.DateOffset(
            days=bs_cfg["daysToGoBack"]
        )

        # Handle columns and indexes
        df_data_bs["periodFrom"] = df_data_bs.index
        df_data_bs["periodTo"] = df_data_bs["periodFrom"] + pd.Timedelta(
            minutes=self.main_cfg["fm"]["granularity"]
        )
        df_data_bs.insert(loc=0, column="assetPortfolioId", value=portfolio.id)
        df_data_bs.insert(loc=1, column="quantityType", value="Power")
        
        # Convert from W to MW (divide by 1e6)
        df_data_bs["quantity"] = df_data_bs["quantity"] / 1e6
        df_data_bs.reset_index(drop=True, inplace=True)

        df_data_bs = df_data_bs[
            [
                "assetPortfolioId",
                "periodFrom",
                "periodTo",
                "quantity",
                "quantityType",
            ]
        ]
        return df_data_bs
