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

    def get_portfolio_asset_names(self, portfolio_id):
        portfolio = self.portfolios.get(portfolio_id)
        if portfolio is None:
            return []
        asset_names = []
        for asset in portfolio.assets:
            asset_name = asset.metadata.get("name")
            if asset_name:
                asset_names.append(asset_name)
        return asset_names

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

    def log_portfolio_baseline_summary(self, portfolio_id, baseline_dataframe):
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
        contribution_details = baseline_dataframe.attrs.get("asset_contributions", {})
        if not contribution_details:
            return

        for _, row in baseline_dataframe.iterrows():
            target_slot = row["periodFrom"]
            slot_contributions = contribution_details.get(target_slot, [])
            if not slot_contributions:
                continue
            portfolio_total_w = sum(
                contribution.get("value_w", 0.0)
                for contribution in slot_contributions
            )
            self.logger.info("Baseline addends for %s:", target_slot)
            for contribution in slot_contributions:
                self.logger.info(
                    "  %s: %.2f W (%.6f MW), source=%s, go_back=%s min, status=%s%s",
                    contribution["asset_name"],
                    contribution["value_w"],
                    contribution["value_w"] / 1e6,
                    contribution["source_time"],
                    contribution["go_back_minutes"],
                    contribution["status"],
                    (
                        ", reason=%s" % contribution["missing_reason"]
                        if contribution.get("missing_reason")
                        else ""
                    ),
                )
            self.logger.info(
                "  portfolio total: %.2f W (%.6f MW)",
                portfolio_total_w,
                portfolio_total_w / 1e6,
            )

    @staticmethod
    def _parse_baseline_value_multiplier(bs_cfg):
        raw_multiplier = bs_cfg.get("valueMultiplier", 1.0)
        if isinstance(raw_multiplier, bool):
            raise ValueError("baseline.valueMultiplier must be a finite number")
        try:
            value_multiplier = float(raw_multiplier)
        except (TypeError, ValueError) as exc:
            raise ValueError("baseline.valueMultiplier must be a finite number") from exc
        if not math.isfinite(value_multiplier):
            raise ValueError("baseline.valueMultiplier must be a finite number")
        return value_multiplier

    @staticmethod
    def _build_baseline_upload_dataframe(baseline_dataframe, value_multiplier):
        upload_dataframe = baseline_dataframe.copy(deep=True)
        upload_dataframe.attrs = baseline_dataframe.attrs.copy()
        upload_quantities = upload_dataframe["quantity"].astype(float) * value_multiplier
        upload_dataframe["quantity"] = upload_quantities.mask(
            upload_quantities == 0.0, 0.0
        )
        return upload_dataframe

    def _log_baseline_upload_value_summary(
        self, portfolio_id, calculated_dataframe, upload_dataframe, value_multiplier
    ):
        calculated_values = pd.to_numeric(
            calculated_dataframe["quantity"], errors="coerce"
        )
        upload_values = pd.to_numeric(upload_dataframe["quantity"], errors="coerce")
        if calculated_values.empty or upload_values.empty:
            self.logger.info(
                "Baseline value transformation portfolio=%s: valueMultiplier=%s, no quantities",
                portfolio_id,
                value_multiplier,
            )
            return

        self.logger.info(
            "Baseline value transformation portfolio=%s: valueMultiplier=%s, "
            "calculated range=%.6f to %.6f MW, upload range=%.6f to %.6f MW",
            portfolio_id,
            value_multiplier,
            calculated_values.min(),
            calculated_values.max(),
            upload_values.min(),
            upload_values.max(),
        )

    def update_portfolio_baseline(
        self, portfolio_id, baseline_dataframe, value_multiplier=1.0
    ):
        upload_dataframe = self._build_baseline_upload_dataframe(
            baseline_dataframe, value_multiplier
        )
        self._log_baseline_upload_value_summary(
            portfolio_id, baseline_dataframe, upload_dataframe, value_multiplier
        )

        tmp_baseline_file = "%s%s%s.csv" % (
            self.cfg["baselines"]["tmpFolder"],
            os.sep,
            portfolio_id,
        )
        upload_dataframe.to_csv(tmp_baseline_file, index=False)

        self.log_portfolio_baseline_summary(portfolio_id, upload_dataframe)

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

    def update_baselines(self, bs_cfg, dry_run=False):
        try:
            value_multiplier = self._parse_baseline_value_multiplier(bs_cfg)
        except ValueError as exc:
            self.logger.error("Invalid baseline configuration: %s", str(exc))
            return False
        self.logger.info("Baseline value multiplier: %s", value_multiplier)

        granularity_minutes = self.main_cfg["fm"]["granularity"]
        current_time_utc = datetime.utcnow()
        adjusted_time = current_time_utc.replace(
            minute=(current_time_utc.minute // granularity_minutes)
            * granularity_minutes,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=bs_cfg["shiftMinutes"])
        db_settings = bs_cfg.get("dbSettings", {})
        self.logger.info(
            "Baseline update selected source=%s strategy=%s current_time_utc=%s target_start=%s shiftMinutes=%s upcomingHoursToQuery=%s",
            bs_cfg["source"],
            db_settings.get("strategy", "day_persistence")
            if bs_cfg["source"] == "db"
            else "n/a",
            current_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            adjusted_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            bs_cfg["shiftMinutes"],
            db_settings.get("upcomingHoursToQuery", "n/a"),
        )

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
            if df is False:
                self.logger.error(
                    "Baseline update aborted for portfolio %s due to configuration or query errors",
                    k_p,
                )
                return False
            if dry_run:
                self.logger.info(
                    "[DRY-RUN] Built baseline for portfolio %s; upload and InfluxDB save skipped",
                    k_p,
                )
                upload_df = self._build_baseline_upload_dataframe(
                    df, value_multiplier
                )
                self._log_baseline_upload_value_summary(
                    k_p, df, upload_df, value_multiplier
                )
                self.log_portfolio_baseline_summary(k_p, upload_df)
                continue
            if self.update_portfolio_baseline(k_p, df, value_multiplier) is False:
                self.logger.error("Baseline upload failed for portfolio %s", k_p)
                return False
            if self.save_portfolio_baseline_to_influx(k_p, df, bs_cfg) is False:
                return False
        return True

    def _resolve_asset_mapping_entry(self, asset_name):
        asset_mapping = self.main_cfg.get("asset_mapping", {})
        mapping = asset_mapping.get(asset_name)
        if mapping is None:
            return None
        if isinstance(mapping, dict):
            return {
                "device_name": mapping.get("device_name_tag"),
                "field": mapping.get("field", "active_power"),
                "site": mapping.get("pod"),
                "baseline_persistence_go_back_minutes": mapping.get(
                    "baseline_persistence_go_back_minutes"
                ),
            }
        return {
            "device_name": mapping,
            "field": "active_power",
            "site": None,
            "baseline_persistence_go_back_minutes": None,
        }

    def _build_portfolio_asset_queries(self, portfolio):
        asset_queries = []
        for asset in portfolio.assets:
            asset_name = asset.metadata.get("name")
            if not asset_name:
                continue
            mapping_entry = self._resolve_asset_mapping_entry(asset_name)
            if mapping_entry is None:
                self.logger.warning(
                    "Asset %s is not configured in asset_mapping; skipping baseline query",
                    asset_name,
                )
                continue

            device_name = mapping_entry.get("device_name")
            field = mapping_entry.get("field", "active_power")
            site = asset.mpid or mapping_entry.get("site")
            if site and device_name:
                asset_queries.append(
                    {
                        "asset_name": asset_name,
                        "site": site,
                        "device_name": device_name,
                        "field": field,
                        "baseline_persistence_go_back_minutes": mapping_entry.get(
                            "baseline_persistence_go_back_minutes"
                        ),
                    }
                )
                self.logger.info(
                    "Asset %s -> site=%s, device=%s, field=%s",
                    asset_name,
                    site,
                    device_name,
                    field,
                )
            else:
                self.logger.warning(
                    "Asset %s missing site or device_name mapping; baseline query skipped",
                    asset_name,
                )
        return asset_queries

    def _query_grouped_asset_series(
        self,
        site,
        device_name,
        field,
        start_dt_utc,
        end_dt_utc,
        granularity_minutes,
        asset_name=None,
    ):
        assets_measurement = self.main_cfg["influxDB"].get(
            "assetsMeasurement", "assets_data"
        )
        query = (
            "SELECT MEAN(%s) FROM %s WHERE "
            "time>='%s' AND time<'%s' AND site='%s' AND device_name='%s' "
            "GROUP BY time(%im)"
        ) % (
            field,
            assets_measurement,
            start_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            site,
            device_name,
            granularity_minutes,
        )
        if asset_name:
            self.logger.info("Query for %s: %s", asset_name, query)
        else:
            self.logger.info("Query: %s", query)

        aggregated_values = {}
        try:
            res = self.influx_client.query(query)
            if res:
                for _, df_data in res.items():
                    for idx in df_data.index:
                        value = df_data.loc[idx, "mean"]
                        if pd.notna(value):
                            timestamp = pd.Timestamp(idx)
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.tz_localize("UTC")
                            else:
                                timestamp = timestamp.tz_convert("UTC")
                            aggregated_values[timestamp] = float(value)
        except Exception as e:
            if asset_name:
                self.logger.error("Error querying asset %s: %s", asset_name, str(e))
            else:
                self.logger.error("Error querying grouped series: %s", str(e))
            return None

        if not aggregated_values:
            return pd.Series(dtype=float)

        series = pd.Series(aggregated_values, dtype=float)
        series.sort_index(inplace=True)
        return series

    def _validate_slot_persistence_alignment(
        self, persistence_go_back_minutes, config_key="persistenceGoBackMinutes"
    ):
        granularity_minutes = self.main_cfg["fm"]["granularity"]
        if persistence_go_back_minutes <= 0:
            self.logger.error(
                "Invalid %s=%s; it must be positive",
                config_key,
                persistence_go_back_minutes,
            )
            return False
        if persistence_go_back_minutes % granularity_minutes != 0:
            self.logger.error(
                "Invalid %s=%s for fm.granularity=%s; values must align exactly",
                config_key,
                persistence_go_back_minutes,
                granularity_minutes,
            )
            return False
        return True

    def _parse_slot_persistence_go_back_minutes(self, raw_value, config_key):
        try:
            go_back_minutes = int(raw_value)
        except (TypeError, ValueError):
            self.logger.error(
                "Invalid %s=%s; value must be an integer",
                config_key,
                raw_value,
            )
            return None
        if self._validate_slot_persistence_alignment(
            go_back_minutes, config_key
        ) is False:
            return None
        return go_back_minutes

    def _parse_slot_persistence_max_slots(self, max_slots_to_upload):
        if max_slots_to_upload is None:
            return None
        if isinstance(max_slots_to_upload, str):
            normalized_value = max_slots_to_upload.strip().lower()
            if normalized_value == "all":
                return None
            try:
                max_slots_to_upload = int(normalized_value)
            except ValueError as exc:
                raise ValueError(
                    "maxSlotsToUpload must be a positive integer, 0, null, or 'all'"
                ) from exc
        if isinstance(max_slots_to_upload, bool) or not isinstance(
            max_slots_to_upload, int
        ):
            raise ValueError(
                "maxSlotsToUpload must be a positive integer, 0, null, or 'all'"
            )
        if max_slots_to_upload <= 0:
            return None
        return max_slots_to_upload

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
        strategy = bs_cfg.get("strategy", "day_persistence")
        if strategy in [None, "", "legacy", "day_persistence"]:
            return self._create_df_baseline_from_db_day_persistence(
                portfolio, adjusted_time, bs_cfg
            )
        if strategy == "slot_persistence":
            return self._create_df_baseline_from_db_slot_persistence(
                portfolio, adjusted_time, bs_cfg
            )
        self.logger.error(
            "Baseline dbSettings strategy option '%s' not available", strategy
        )
        return False

    def _create_df_baseline_from_db_day_persistence(self, portfolio, adjusted_time, bs_cfg):
        start_dt = adjusted_time - timedelta(days=bs_cfg["daysToGoBack"])
        start_dt_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_dt_str = (
            start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        asset_queries = self._build_portfolio_asset_queries(portfolio)
        if not asset_queries:
            self.logger.info(
                "Portfolio %s has no mapped assets; using zero baseline", portfolio.id
            )
            return self._build_zero_baseline_dataframe(portfolio, adjusted_time, bs_cfg)

        aggregated_data = {}

        for asset_query in asset_queries:
            series = self._query_grouped_asset_series(
                site=asset_query["site"],
                device_name=asset_query["device_name"],
                field=asset_query["field"],
                start_dt_utc=start_dt,
                end_dt_utc=start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"]),
                granularity_minutes=self.main_cfg["fm"]["granularity"],
                asset_name=asset_query["asset_name"],
            )
            if series is None:
                continue
            for timestamp, value in series.items():
                if timestamp not in aggregated_data:
                    aggregated_data[timestamp] = 0.0
                aggregated_data[timestamp] += value
                self.logger.debug(
                    "  %s @ %s: %.2f W",
                    asset_query["asset_name"],
                    timestamp,
                    value,
                )

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

    def _create_df_baseline_from_db_slot_persistence(self, portfolio, adjusted_time, bs_cfg):
        persistence_go_back_minutes = self._parse_slot_persistence_go_back_minutes(
            bs_cfg.get("persistenceGoBackMinutes", 90),
            "baseline.dbSettings.persistenceGoBackMinutes",
        )
        if persistence_go_back_minutes is None:
            return False
        missing_measurement_policy = bs_cfg.get(
            "missingMeasurementPolicy", "fail_portfolio"
        )
        granularity_minutes = self.main_cfg["fm"]["granularity"]
        upcoming_hours_to_query = bs_cfg["upcomingHoursToQuery"]
        upload_only_computable_horizon = bs_cfg.get(
            "uploadOnlyComputableHorizon", True
        )
        try:
            max_slots_to_upload = self._parse_slot_persistence_max_slots(
                bs_cfg.get("maxSlotsToUpload", 1)
            )
        except ValueError as exc:
            self.logger.error("Invalid slot_persistence configuration: %s", str(exc))
            return False

        if missing_measurement_policy not in (
            "fail_portfolio",
            "skip_asset",
            "zero_fill_asset",
        ):
            self.logger.error(
                "Invalid missingMeasurementPolicy=%s for slot_persistence; expected fail_portfolio, skip_asset, or zero_fill_asset",
                missing_measurement_policy,
            )
            return False

        target_start_utc = pd.Timestamp(adjusted_time, tz="UTC")
        target_end_utc = target_start_utc + pd.Timedelta(hours=upcoming_hours_to_query)
        target_slot_index = pd.date_range(
            start=target_start_utc,
            end=target_end_utc - pd.Timedelta(minutes=granularity_minutes),
            freq=f"{granularity_minutes}min",
        )
        if target_slot_index.empty:
            self.logger.error(
                "Baseline slot_persistence produced an empty nominal target horizon for portfolio=%s",
                portfolio.id,
            )
            return None

        self.logger.info(
            "Baseline DB strategy=slot_persistence, portfolio=%s, current_time_target_start=%s, nominal target horizon=[%s-%s), nominal slots=%s, default persistenceGoBackMinutes=%s, uploadOnlyComputableHorizon=%s, maxSlotsToUpload=%s, missingMeasurementPolicy=%s",
            portfolio.id,
            target_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            target_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            target_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            len(target_slot_index),
            persistence_go_back_minutes,
            upload_only_computable_horizon,
            "all" if max_slots_to_upload is None else max_slots_to_upload,
            missing_measurement_policy,
        )
        self.logger.info(
            "Baseline slot_persistence portfolio=%s first_target_slot=%s default_first_source_time=%s",
            portfolio.id,
            target_slot_index[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            (
                target_slot_index[0]
                - pd.Timedelta(minutes=persistence_go_back_minutes)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        asset_queries = self._build_portfolio_asset_queries(portfolio)
        if not asset_queries:
            self.logger.info(
                "Portfolio %s has no mapped assets; using zero baseline", portfolio.id
            )
            selected_target_slot_index = target_slot_index
            if upload_only_computable_horizon and max_slots_to_upload is not None:
                selected_target_slot_index = selected_target_slot_index[
                    :max_slots_to_upload
                ]
            self.logger.info(
                "Baseline slot_persistence computable horizon portfolio=%s: no mapped assets, slots before maxSlotsToUpload cap=%s, slots after cap=%s",
                portfolio.id,
                len(target_slot_index),
                len(selected_target_slot_index),
            )
            return pd.DataFrame(
                {
                    "assetPortfolioId": portfolio.id,
                    "periodFrom": [
                        ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                        for ts in selected_target_slot_index
                    ],
                    "periodTo": [
                        (ts + pd.Timedelta(minutes=granularity_minutes)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                        for ts in selected_target_slot_index
                    ],
                    "quantity": [0.0 for _ in selected_target_slot_index],
                    "quantityType": "Power",
                }
            )

        aligned_values_by_asset = {}
        asset_go_back_minutes = {}
        asset_source_time_by_target = {}
        skipped_assets = set()
        total_missing_measurements = 0

        for asset_query in asset_queries:
            asset_name = asset_query["asset_name"]
            asset_go_back = self._parse_slot_persistence_go_back_minutes(
                asset_query.get("baseline_persistence_go_back_minutes")
                or persistence_go_back_minutes,
                "asset_mapping.%s.baseline_persistence_go_back_minutes" % asset_name,
            )
            if asset_go_back is None:
                return False

            asset_source_offset = pd.Timedelta(minutes=asset_go_back)
            asset_source_start_utc = target_start_utc - asset_source_offset
            asset_source_end_utc = target_end_utc - asset_source_offset
            asset_lagged_index = pd.DatetimeIndex(target_slot_index - asset_source_offset)
            asset_go_back_minutes[asset_name] = asset_go_back
            asset_source_time_by_target[asset_name] = pd.Series(
                asset_lagged_index, index=target_slot_index
            )

            self.logger.info(
                "Baseline slot_persistence asset=%s portfolio=%s baseline_go_back_minutes=%s source_window=[%s-%s) first_target_slot=%s first_source_time=%s",
                asset_name,
                portfolio.id,
                asset_go_back,
                asset_source_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                asset_source_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                target_slot_index[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                asset_lagged_index[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            series = self._query_grouped_asset_series(
                site=asset_query["site"],
                device_name=asset_query["device_name"],
                field=asset_query["field"],
                start_dt_utc=asset_source_start_utc.to_pydatetime(),
                end_dt_utc=asset_source_end_utc.to_pydatetime(),
                granularity_minutes=granularity_minutes,
                asset_name=asset_name,
            )
            if series is None:
                return False
            if series.empty:
                self.logger.warning(
                    "Missing baseline source measurements for asset=%s portfolio=%s target range=[%s-%s) source range=[%s-%s)",
                    asset_name,
                    portfolio.id,
                    target_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    target_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    asset_source_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    asset_source_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                if (
                    missing_measurement_policy == "fail_portfolio"
                    and not upload_only_computable_horizon
                ):
                    self.logger.error(
                        "Baseline slot_persistence failed for portfolio=%s because asset=%s has no source measurements with baseline_go_back_minutes=%s",
                        portfolio.id,
                        asset_name,
                        asset_go_back,
                    )
                    return None
                if missing_measurement_policy == "skip_asset":
                    skipped_assets.add(asset_name)
                    continue

            aligned_values = series.reindex(asset_lagged_index)
            aligned_values_by_asset[asset_name] = pd.Series(
                aligned_values.to_numpy(), index=target_slot_index
            )
            missing_mask = aligned_values.isna()
            available_count = len(aligned_values) - int(missing_mask.sum())
            self.logger.info(
                "Baseline slot_persistence asset=%s portfolio=%s available_source_measurements=%s/%s baseline_go_back_minutes=%s",
                asset_name,
                portfolio.id,
                available_count,
                len(aligned_values),
                asset_go_back,
            )
            if missing_mask.any():
                first_missing_source = asset_lagged_index[missing_mask.argmax()]
                first_missing_target = target_slot_index[missing_mask.argmax()]
                missing_count = int(missing_mask.sum())
                total_missing_measurements += missing_count
                self.logger.warning(
                    "Missing baseline source measurement for asset=%s portfolio=%s target_slot=%s baseline_source_time=%s baseline_go_back_minutes=%s (missing %s/%s slots)",
                    asset_name,
                    portfolio.id,
                    first_missing_target.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    first_missing_source.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    asset_go_back,
                    missing_count,
                    len(aligned_values),
                )
                if (
                    missing_measurement_policy == "fail_portfolio"
                    and not upload_only_computable_horizon
                ):
                    self.logger.error(
                        "Baseline slot_persistence failed for portfolio=%s because required source data is missing for asset=%s",
                        portfolio.id,
                        asset_name,
                    )
                    return None

        if not aligned_values_by_asset:
            self.logger.error(
                "Baseline slot_persistence could not build portfolio=%s because no asset contributed usable source measurements",
                portfolio.id,
            )
            return None

        computable_target_slots = []
        first_gap_target = None
        first_gap_sources = {}
        first_gap_missing_assets = []
        for target_slot_utc in target_slot_index:
            slot_missing_assets = []
            slot_value_count = 0
            for asset_name, aligned_values in aligned_values_by_asset.items():
                aligned_value = aligned_values.loc[target_slot_utc]
                if pd.notna(aligned_value):
                    slot_value_count += 1
                elif missing_measurement_policy in (
                    "fail_portfolio",
                    "skip_asset",
                ):
                    slot_missing_assets.append(asset_name)

            if missing_measurement_policy == "fail_portfolio":
                slot_is_computable = (
                    slot_value_count == len(aligned_values_by_asset)
                    and not slot_missing_assets
                )
            elif missing_measurement_policy == "zero_fill_asset":
                slot_is_computable = True
            else:
                slot_is_computable = slot_value_count > 0

            if slot_is_computable:
                computable_target_slots.append(target_slot_utc)
                continue

            first_gap_target = target_slot_utc
            first_gap_missing_assets = slot_missing_assets
            first_gap_sources = {
                asset_name: source_times.loc[target_slot_utc]
                for asset_name, source_times in asset_source_time_by_target.items()
            }
            break

        computable_slots_before_cap = len(computable_target_slots)
        if upload_only_computable_horizon:
            selected_target_slots = computable_target_slots
            if max_slots_to_upload is not None:
                selected_target_slots = selected_target_slots[:max_slots_to_upload]
        else:
            selected_target_slots = list(target_slot_index)

        selected_target_slot_index = pd.DatetimeIndex(selected_target_slots)
        if selected_target_slot_index.empty:
            if first_gap_target is not None:
                self.logger.error(
                    "Baseline slot_persistence portfolio=%s has no computable target slots; first target slot=%s source_times_by_asset=%s missing_assets=%s total_missing_measurements=%s",
                    portfolio.id,
                    first_gap_target.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    {
                        asset_name: source_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                        for asset_name, source_time in first_gap_sources.items()
                    },
                    ",".join(first_gap_missing_assets) or "n/a",
                    total_missing_measurements,
                )
            else:
                self.logger.error(
                    "Baseline slot_persistence portfolio=%s has no computable target slots",
                    portfolio.id,
                )
            return None

        if upload_only_computable_horizon:
            detected_end = selected_target_slot_index[-1] + pd.Timedelta(
                minutes=granularity_minutes
            )
            self.logger.info(
                "Baseline slot_persistence detected computable target horizon portfolio=%s: contiguous range=[%s-%s), slots before maxSlotsToUpload cap=%s, slots after cap=%s",
                portfolio.id,
                selected_target_slot_index[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                detected_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                computable_slots_before_cap,
                len(selected_target_slot_index),
            )
            if first_gap_target is not None:
                self.logger.info(
                    "Baseline slot_persistence computable horizon stops before target_slot=%s source_times_by_asset=%s missing_assets=%s",
                    first_gap_target.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    {
                        asset_name: source_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                        for asset_name, source_time in first_gap_sources.items()
                    },
                    ",".join(first_gap_missing_assets) or "n/a",
                )
        else:
            self.logger.info(
                "Baseline slot_persistence uploadOnlyComputableHorizon=false for portfolio=%s; using nominal target horizon slots=%s; detected computable prefix slots=%s; maxSlotsToUpload not applied",
                portfolio.id,
                len(selected_target_slot_index),
                computable_slots_before_cap,
            )

        self.logger.info(
            "Baseline slot_persistence missing measurement summary portfolio=%s: total_missing_measurements=%s",
            portfolio.id,
            total_missing_measurements,
        )

        aggregated_data = {target_ts: 0.0 for target_ts in selected_target_slot_index}
        slot_contribution_counts = {
            target_ts: 0 for target_ts in selected_target_slot_index
        }
        contribution_details = {
            target_ts.strftime("%Y-%m-%dT%H:%M:%SZ"): []
            for target_ts in selected_target_slot_index
        }
        contributing_assets = 0

        for asset_name, aligned_values in aligned_values_by_asset.items():
            contribution_count = 0
            contribution_sum_w = 0.0
            selected_aligned_values = aligned_values.reindex(selected_target_slot_index)
            for target_slot_utc, aligned_value in selected_aligned_values.items():
                source_time = asset_source_time_by_target[asset_name].loc[
                    target_slot_utc
                ]
                target_slot_str = target_slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                source_time_str = source_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                if pd.notna(aligned_value):
                    contribution_value_w = float(aligned_value)
                    contribution_status = "measured"
                    missing_reason = None
                elif missing_measurement_policy == "zero_fill_asset":
                    contribution_value_w = 0.0
                    contribution_status = "missing_zero_fallback"
                    missing_reason = "source measurement missing"
                    self.logger.warning(
                        "Baseline slot_persistence zero-fill contribution: portfolio=%s asset=%s target_slot=%s source_time=%s go_back=%s min",
                        portfolio.id,
                        asset_name,
                        target_slot_str,
                        source_time_str,
                        asset_go_back_minutes.get(
                            asset_name, persistence_go_back_minutes
                        ),
                    )
                else:
                    contribution_details[target_slot_str].append(
                        {
                            "asset_name": asset_name,
                            "target_slot": target_slot_str,
                            "source_time": source_time_str,
                            "go_back_minutes": asset_go_back_minutes.get(
                                asset_name, persistence_go_back_minutes
                            ),
                            "value_w": 0.0,
                            "status": "skipped"
                            if missing_measurement_policy == "skip_asset"
                            else "missing_fail",
                            "missing_reason": "source measurement missing",
                        }
                    )
                    continue

                aggregated_data[target_slot_utc] += contribution_value_w
                contribution_details[target_slot_str].append(
                    {
                        "asset_name": asset_name,
                        "target_slot": target_slot_str,
                        "source_time": source_time_str,
                        "go_back_minutes": asset_go_back_minutes.get(
                            asset_name, persistence_go_back_minutes
                        ),
                        "value_w": contribution_value_w,
                        "status": contribution_status,
                        "missing_reason": missing_reason,
                    }
                )
                if pd.notna(aligned_value) or missing_measurement_policy == "zero_fill_asset":
                    slot_contribution_counts[target_slot_utc] += 1
                    contribution_count += 1
                    contribution_sum_w += contribution_value_w

            if contribution_count > 0:
                contributing_assets += 1
                self.logger.info(
                    "Baseline slot_persistence contribution: portfolio=%s asset=%s baseline_go_back_minutes=%s contributed %s/%s slots, total=%.2f W",
                    portfolio.id,
                    asset_name,
                    asset_go_back_minutes.get(asset_name, persistence_go_back_minutes),
                    contribution_count,
                    len(selected_target_slot_index),
                    contribution_sum_w,
                )

        if contributing_assets == 0:
            self.logger.error(
                "Baseline slot_persistence could not build portfolio=%s because no asset contributed usable source measurements",
                portfolio.id,
            )
            return None

        empty_target_slots = [
            target_slot_utc
            for target_slot_utc, contribution_count in slot_contribution_counts.items()
            if contribution_count == 0
        ]
        if empty_target_slots:
            self.logger.error(
                "Baseline slot_persistence cannot upload portfolio=%s because %s target slots have no usable asset measurements; first missing target slot=%s",
                portfolio.id,
                len(empty_target_slots),
                empty_target_slots[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            return None

        if any(pd.isna(value) for value in aggregated_data.values()):
            self.logger.error(
                "Baseline slot_persistence produced NaN values for portfolio=%s",
                portfolio.id,
            )
            return None

        if not aggregated_data:
            self.logger.error(
                "Baseline slot_persistence produced no target slots for portfolio=%s",
                portfolio.id,
            )
            return None

        df_data_bs = pd.DataFrame(
            {
                "assetPortfolioId": portfolio.id,
                "periodFrom": [ts.strftime("%Y-%m-%dT%H:%M:%SZ") for ts in selected_target_slot_index],
                "periodTo": [
                    (ts + pd.Timedelta(minutes=granularity_minutes)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    for ts in selected_target_slot_index
                ],
                "quantity": [
                    aggregated_data[target_ts] / 1e6 for target_ts in selected_target_slot_index
                ],
                "quantityType": "Power",
            }
        )
        df_data_bs.attrs["asset_contributions"] = contribution_details
        self.logger.info(
            "Baseline slot_persistence final number of uploaded slots portfolio=%s: %s",
            portfolio.id,
            len(df_data_bs),
        )
        self.logger.info(
            "Baseline slot_persistence final intervals portfolio=%s: [%s-%s]",
            portfolio.id,
            df_data_bs["periodFrom"].iloc[0],
            df_data_bs["periodTo"].iloc[-1],
        )
        self.logger.info(
            "Baseline slot_persistence portfolio=%s values_w=[%s] values_mw=[%s]",
            portfolio.id,
            ", ".join(
                ["%.2f" % (aggregated_data[target_ts]) for target_ts in selected_target_slot_index]
            ),
            ", ".join(["%.6f" % quantity for quantity in df_data_bs["quantity"]]),
        )
        return df_data_bs
