# import section
import os
import copy
import math
import logging
import pandas as pd
from datetime import datetime, timedelta
from influxdb import InfluxDBClient

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
        self._baseline_logging_context = {}

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

        self._log_baseline_dataframe_summary(portfolio_id, baseline_dataframe)

        endpoint = "%s%s" % (
            self.nodes_interface.cfg["mainEndpoint"],
            "BaselineIntervals/import",
        )
        return self.nodes_interface.post_csv_file_request(endpoint, tmp_baseline_file)

    def _log_baseline_dataframe_summary(
        self,
        portfolio_id,
        baseline_dataframe,
        log_prefix="Update baseline of",
    ):
        period_from = pd.to_datetime(baseline_dataframe["periodFrom"], utc=True)
        period_to = pd.to_datetime(baseline_dataframe["periodTo"], utc=True)
        quantities = baseline_dataframe["quantity"].astype(float).values
        first_times, last_times = self._format_timestamp_list_for_log(period_from)

        self.logger.info(
            "%s portfolio %s, period [%s-%s), rows=%i"
            % (
                log_prefix,
                portfolio_id,
                period_from.iloc[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                period_to.iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
                len(baseline_dataframe),
            )
        )
        if last_times:
            self.logger.info("Baseline first timestamps: [%s]", ", ".join(first_times))
            self.logger.info("Baseline last timestamps: [%s]", ", ".join(last_times))
        else:
            self.logger.info("Baseline timestamps: [%s]", ", ".join(first_times))
        if len(quantities) <= 8 or self.logger.isEnabledFor(logging.DEBUG):
            self.logger.info(
                "Baseline values (MW): [%s]",
                ", ".join([f"{q:.6f}" for q in quantities]),
            )
        self.logger.info(
            "Baseline statistics (MW): min=%.6f, mean=%.6f, max=%.6f",
            quantities.min(),
            quantities.mean(),
            quantities.max(),
        )
        source_summary = self._baseline_logging_context.pop(portfolio_id, None)
        if source_summary:
            self.logger.info(
                "Baseline source summary for portfolio %s: %s",
                portfolio_id,
                source_summary,
            )

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
        self.logger.info(
            "Saving baseline to %s: Nodes quantity is in MW, converting to quantity_w for InfluxDB storage",
            measurement,
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

    @staticmethod
    def _get_baseline_strategy(bs_cfg):
        return bs_cfg.get("strategy", "persistence")

    @staticmethod
    def _get_missing_asset_policy(bs_cfg):
        return bs_cfg.get(
            "missingAssetPolicy",
            bs_cfg.get("missing_asset_policy", "persistence_fallback"),
        )

    @staticmethod
    def _get_aem_forecast_actual_settings(bs_cfg):
        return bs_cfg.get(
            "aemForecastActualSettings",
            bs_cfg.get("aem_forecast_actual_settings", {}),
        )

    @staticmethod
    def _is_dry_run(bs_cfg):
        return bs_cfg.get("dryRun", bs_cfg.get("dry_run", False))

    def _get_portfolio_asset_specs(self, portfolio):
        asset_mapping = self.main_cfg.get("asset_mapping", {})
        asset_specs = []

        for asset in portfolio.assets:
            asset_name = asset.metadata.get("name")
            if not asset_name:
                self.logger.warning(
                    "Portfolio %s contains an asset without a name; skipping",
                    portfolio.id,
                )
                continue

            mapping = asset_mapping.get(asset_name, {})
            if mapping and not isinstance(mapping, dict):
                mapping = {"device_name_tag": mapping}

            if not mapping:
                self.logger.warning(
                    "Asset %s is missing from asset_mapping; "
                    "AEM baseline can still use asset_label=%s but persistence "
                    "fallback is unavailable",
                    asset_name,
                    asset_name,
                )

            asset_specs.append(
                {
                    "asset_name": asset_name,
                    "asset_label": mapping.get(
                        "aem_baseline_asset_label",
                        mapping.get("baseline_asset_label", asset_name),
                    ),
                    "site": asset.mpid or mapping.get("pod"),
                    "device_name": mapping.get("device_name_tag"),
                    "field": mapping.get("field", "active_power"),
                    "type": mapping.get("type"),
                }
            )

        return asset_specs

    def _build_baseline_dataframe_from_w_series(self, portfolio, quantity_by_timestamp_w):
        granularity = self.main_cfg["fm"]["granularity"]
        rows = []
        for period_from in sorted(quantity_by_timestamp_w.keys()):
            period_to = period_from + timedelta(minutes=granularity)
            rows.append(
                {
                    "assetPortfolioId": portfolio.id,
                    "periodFrom": period_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "periodTo": period_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    # Source values are kept in W internally and converted to MW
                    # only for the Nodes baseline payload.
                    "quantity": quantity_by_timestamp_w[period_from] / 1e6,
                    "quantityType": "Power",
                }
            )
        return pd.DataFrame(rows)

    def _query_influx_raw_rows(self, query):
        try:
            result = InfluxDBClient.query(self.influx_client, query)
        except Exception as e:
            self.logger.error("Error executing raw InfluxDB query: %s", str(e))
            return None

        rows = []
        for series in result.raw.get("series", []):
            columns = series.get("columns", [])
            for values in series.get("values", []):
                rows.append(dict(zip(columns, values)))
        return rows

    def _is_quarter_hour_aligned(self, timestamp):
        granularity = self.main_cfg["fm"]["granularity"]
        return (
            timestamp.minute % granularity == 0
            and timestamp.second == 0
            and timestamp.microsecond == 0
        )

    def _build_expected_baseline_timestamps(self, adjusted_time, horizon_hours):
        granularity = self.main_cfg["fm"]["granularity"]
        if not self._is_quarter_hour_aligned(adjusted_time):
            self.logger.error(
                "Baseline start timestamp %s is not aligned to %i-minute intervals",
                adjusted_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                granularity,
            )
            return None

        slot_count = int((horizon_hours * 60) / granularity)
        if horizon_hours * 60 != slot_count * granularity:
            self.logger.error(
                "Baseline horizon %s hours is not divisible by granularity %i minutes",
                horizon_hours,
                granularity,
            )
            return None
        if slot_count < 1:
            self.logger.error(
                "Baseline horizon %s hours produces no quarter-hour slots with granularity %i minutes",
                horizon_hours,
                granularity,
            )
            return None
        if slot_count > 4096:
            self.logger.error(
                "Baseline horizon %s hours is unexpectedly large (%i quarter-hour slots)",
                slot_count,
                horizon_hours,
            )
            return None

        return [
            adjusted_time + timedelta(minutes=granularity * slot_idx)
            for slot_idx in range(slot_count)
        ]

    @staticmethod
    def _get_aem_max_inspection_hours(aem_cfg):
        return aem_cfg.get(
            "maxInspectionHours",
            aem_cfg.get("max_inspection_hours", 30),
        )

    def _classify_aem_asset_specs(self, portfolio, asset_specs, aem_cfg):
        configured_required = set(
            aem_cfg.get(
                "requiredAemForecastAssets",
                aem_cfg.get("required_aem_forecast_assets", []),
            )
        )
        configured_optional = set(
            aem_cfg.get(
                "optionalAemForecastAssets",
                aem_cfg.get("optional_aem_forecast_assets", []),
            )
        )
        if configured_required & configured_optional:
            self.logger.error(
                "Portfolio %s AEM config contains overlapping required/optional assets: %s",
                portfolio.id,
                sorted(configured_required & configured_optional),
            )
            return None

        portfolio_asset_names = {asset_spec["asset_name"] for asset_spec in asset_specs}
        unknown_required = sorted(configured_required - portfolio_asset_names)
        unknown_optional = sorted(configured_optional - portfolio_asset_names)
        if unknown_required or unknown_optional:
            self.logger.error(
                "Portfolio %s AEM config references non-portfolio assets: required=%s optional=%s",
                portfolio.id,
                unknown_required,
                unknown_optional,
            )
            return None

        if configured_required or configured_optional:
            self.logger.info(
                "AEM asset classification for portfolio %s uses configured required/optional asset lists",
                portfolio.id,
            )
        else:
            self.logger.info(
                "AEM asset classification for portfolio %s defaults ev_charger assets to optional and all others to required",
                portfolio.id,
            )

        required_asset_specs = []
        optional_asset_specs = []
        classified_asset_specs = []
        for asset_spec in asset_specs:
            classification = "required"
            if configured_required or configured_optional:
                if asset_spec["asset_name"] in configured_optional:
                    classification = "optional"
                elif asset_spec["asset_name"] in configured_required:
                    classification = "required"
                else:
                    classification = "required"
            elif asset_spec.get("type") == "ev_charger":
                classification = "optional"

            asset_spec_with_classification = dict(asset_spec)
            asset_spec_with_classification["aem_requirement"] = classification
            classified_asset_specs.append(asset_spec_with_classification)
            if classification == "required":
                required_asset_specs.append(asset_spec_with_classification)
            else:
                optional_asset_specs.append(asset_spec_with_classification)

        if not required_asset_specs:
            self.logger.error(
                "Portfolio %s has no required AEM forecast assets; unable to derive a contiguous AEM upload horizon",
                portfolio.id,
            )
            return None

        self.logger.info(
            "AEM required assets for portfolio %s: %s",
            portfolio.id,
            [asset_spec["asset_name"] for asset_spec in required_asset_specs],
        )
        self.logger.info(
            "AEM optional assets for portfolio %s: %s",
            portfolio.id,
            [asset_spec["asset_name"] for asset_spec in optional_asset_specs],
        )
        return classified_asset_specs, required_asset_specs, optional_asset_specs

    def _detect_contiguous_aem_horizon(
        self, portfolio, required_asset_series, expected_timestamps
    ):
        contiguous_timestamps = []
        stopping_timestamp = None
        stopping_assets = []

        for timestamp in expected_timestamps:
            missing_required_assets = [
                asset_name
                for asset_name, values_by_timestamp in required_asset_series.items()
                if timestamp not in values_by_timestamp
            ]
            if missing_required_assets:
                stopping_timestamp = timestamp
                stopping_assets = missing_required_assets
                break
            contiguous_timestamps.append(timestamp)

        if not contiguous_timestamps:
            self.logger.error(
                "No valid upcoming AEM forecast horizon available for portfolio %s: required coverage is missing at the first timestamp %s from assets %s",
                portfolio.id,
                expected_timestamps[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                stopping_assets,
            )
            return None

        return contiguous_timestamps, stopping_timestamp, stopping_assets

    @staticmethod
    def _format_timestamp_list_for_log(timestamps, count=4):
        timestamp_strings = [
            pd.Timestamp(timestamp).strftime("%Y-%m-%dT%H:%M:%SZ")
            for timestamp in timestamps
        ]
        if len(timestamp_strings) <= count * 2:
            return timestamp_strings, []
        return timestamp_strings[:count], timestamp_strings[-count:]

    def _validate_unique_aem_asset_labels(self, portfolio, asset_specs):
        labels_to_assets = {}
        for asset_spec in asset_specs:
            labels_to_assets.setdefault(asset_spec["asset_label"], []).append(
                asset_spec["asset_name"]
            )

        duplicated_labels = {
            asset_label: asset_names
            for asset_label, asset_names in labels_to_assets.items()
            if len(asset_names) > 1
        }
        if duplicated_labels:
            self.logger.error(
                "Portfolio %s contains duplicated AEM asset labels: %s",
                portfolio.id,
                duplicated_labels,
            )
            return False
        return True

    def _query_aem_asset_series_w(
        self, asset_spec, measurement, start_dt, end_dt, expected_timestamp_set
    ):
        query = (
            "SELECT value FROM %s WHERE asset_label='%s' "
            "AND time>='%s' AND time<'%s'"
        ) % (
            measurement,
            asset_spec["asset_label"],
            start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.logger.info(
            "AEM query for asset %s (asset_label=%s): %s",
            asset_spec["asset_name"],
            asset_spec["asset_label"],
            query,
        )
        rows = self._query_influx_raw_rows(query)
        if rows is None:
            return False

        values_by_timestamp = {}
        for row in rows:
            timestamp_raw = row.get("time")
            value = row.get("value")
            if timestamp_raw is None or value is None:
                continue

            timestamp = pd.to_datetime(timestamp_raw, utc=True).to_pydatetime()
            timestamp = timestamp.replace(tzinfo=None)
            if not self._is_quarter_hour_aligned(timestamp):
                self.logger.error(
                    "AEM measurement %s returned a non-aligned timestamp for asset %s: %s",
                    measurement,
                    asset_spec["asset_name"],
                    timestamp_raw,
                )
                return False
            if timestamp not in expected_timestamp_set:
                self.logger.error(
                    "AEM measurement %s returned an unexpected timestamp for asset %s: %s",
                    measurement,
                    asset_spec["asset_name"],
                    timestamp_raw,
                )
                return False
            if timestamp in values_by_timestamp:
                self.logger.error(
                    "AEM measurement %s returned duplicate timestamp %s for asset %s",
                    measurement,
                    timestamp_raw,
                    asset_spec["asset_name"],
                )
                return False
            values_by_timestamp[timestamp] = float(value)

        return values_by_timestamp

    def _query_persistence_asset_series_w(self, asset_spec, adjusted_time, bs_cfg):
        if not asset_spec.get("site") or not asset_spec.get("device_name"):
            self.logger.warning(
                "Persistence fallback unavailable for %s: missing site/device mapping",
                asset_spec["asset_name"],
            )
            return {}

        start_dt = adjusted_time - timedelta(days=bs_cfg["daysToGoBack"])
        end_dt = start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        assets_measurement = self.main_cfg["influxDB"].get(
            "assetsMeasurement", "assets_data"
        )
        query = (
            "SELECT MEAN(%s) FROM %s WHERE "
            "time>='%s' AND time<'%s' AND site='%s' AND device_name='%s' "
            "GROUP BY time(%im)"
        ) % (
            asset_spec["field"],
            assets_measurement,
            start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            asset_spec["site"],
            asset_spec["device_name"],
            self.main_cfg["fm"]["granularity"],
        )
        self.logger.info(
            "Persistence fallback query for asset %s: %s",
            asset_spec["asset_name"],
            query,
        )

        try:
            res = self.influx_client.query(query)
        except Exception as e:
            self.logger.error(
                "Error querying persistence fallback for asset %s: %s",
                asset_spec["asset_name"],
                str(e),
            )
            return False

        values_by_timestamp = {}
        for _, df_data in res.items():
            if "mean" not in df_data.columns or df_data.empty:
                continue
            for idx in df_data.index:
                value = df_data.loc[idx, "mean"]
                if pd.isna(value):
                    continue
                timestamp = pd.to_datetime(idx, utc=True).to_pydatetime()
                timestamp = timestamp.replace(tzinfo=None) + timedelta(
                    days=bs_cfg["daysToGoBack"]
                )
                if not self._is_quarter_hour_aligned(timestamp):
                    self.logger.error(
                        "Persistence fallback returned a non-aligned timestamp for asset %s: %s",
                        asset_spec["asset_name"],
                        idx,
                    )
                    return False
                if timestamp in values_by_timestamp:
                    self.logger.error(
                        "Persistence fallback returned duplicate timestamp %s for asset %s",
                        timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        asset_spec["asset_name"],
                    )
                    return False
                values_by_timestamp[timestamp] = float(value)

        return values_by_timestamp

    def create_df_baseline_with_strategy(self, portfolio, adjusted_time, bs_cfg):
        strategy = self._get_baseline_strategy(bs_cfg)
        if strategy == "persistence":
            return self.create_df_baseline_from_persistence(
                portfolio, adjusted_time, bs_cfg["dbSettings"]
            )
        if strategy == "aem_forecast_actual":
            return self.create_df_baseline_from_aem_forecast_actual(
                portfolio, adjusted_time, bs_cfg
            )

        self.logger.error("Baseline strategy option '%s' not available", strategy)
        return False

    def update_baselines(self, bs_cfg):
        current_time = datetime.utcnow()
        adjusted_time = current_time.replace(
            minute=(current_time.minute // 15) * 15, second=0, microsecond=0
        ) + timedelta(minutes=bs_cfg["shiftMinutes"])
        strategy = self._get_baseline_strategy(bs_cfg)
        missing_asset_policy = self._get_missing_asset_policy(bs_cfg)
        dry_run = self._is_dry_run(bs_cfg)

        self.logger.info(
            "Selected baseline strategy=%s source=%s missing_asset_policy=%s",
            strategy,
            bs_cfg.get("source"),
            missing_asset_policy,
        )
        self.logger.info("Baseline dry_run=%s", dry_run)
        self.logger.info(
            "Upcoming quarter-hour timestamp for baseline update: %s",
            adjusted_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        # Cycle over the portfolios
        for k_p in self.portfolios.keys():
            if bs_cfg["source"] == "file":
                df = self.create_df_baseline_from_file(
                    self.portfolios[k_p], adjusted_time, bs_cfg["fileSettings"]
                )
            elif bs_cfg["source"] == "db":
                df = self.create_df_baseline_with_strategy(
                    self.portfolios[k_p], adjusted_time, bs_cfg
                )
            else:
                self.logger.error(
                    "Baseline source option '%s' not available" % bs_cfg["source"]
                )
                return False
            if df is False:
                return False
            if df is None:
                self.logger.warning(
                    "Skipping baseline update for portfolio %s: no data available",
                    k_p,
                )
                continue
            if dry_run:
                self.logger.info(
                    "Dry-run enabled: baseline payload for portfolio %s is built and logged but not uploaded to Nodes or stored to InfluxDB",
                    k_p,
                )
                self._log_baseline_dataframe_summary(
                    k_p,
                    df,
                    log_prefix="Dry-run baseline for",
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
        start_dt = adjusted_time
        end_dt = start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        granularity = self.main_cfg["fm"]["granularity"]

        rows = []
        current_dt = start_dt
        while current_dt < end_dt:
            next_dt = current_dt + timedelta(minutes=granularity)
            rows.append(
                {
                    "assetPortfolioId": portfolio.id,
                    "periodFrom": current_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "periodTo": next_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "quantity": 0.0,
                    "quantityType": "Power",
                }
            )
            current_dt = next_dt

        return pd.DataFrame(rows)

    def create_df_baseline_from_persistence(self, portfolio, adjusted_time, bs_cfg):
        start_dt = adjusted_time - timedelta(days=bs_cfg["daysToGoBack"])
        start_dt_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_dt_str = (
            start_dt + timedelta(hours=bs_cfg["upcomingHoursToQuery"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        asset_queries = []
        for asset_spec in self._get_portfolio_asset_specs(portfolio):
            if asset_spec.get("site") and asset_spec.get("device_name"):
                asset_queries.append(asset_spec)
                self.logger.info(
                    "Persistence asset %s -> site=%s, device=%s, field=%s",
                    asset_spec["asset_name"],
                    asset_spec["site"],
                    asset_spec["device_name"],
                    asset_spec["field"],
                )
            else:
                self.logger.warning(
                    "Skipping persistence asset %s: missing site/device mapping",
                    asset_spec["asset_name"],
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
        self.logger.info(
            "Persistence source values are in W from measurement %s; converting aggregated values to MW for Nodes baseline import",
            assets_measurement,
        )
        aggregated_data = {}
        
        for asset_spec in asset_queries:
            query = (
                "SELECT MEAN(%s) FROM %s WHERE "
                "time>='%s' AND time<'%s' AND site='%s' AND device_name='%s' "
                "GROUP BY time(%im)"
            ) % (
                asset_spec["field"],
                assets_measurement,
                start_dt_str,
                end_dt_str,
                asset_spec["site"],
                asset_spec["device_name"],
                self.main_cfg["fm"]["granularity"],
            )
            self.logger.info(
                "Persistence query for %s: %s",
                asset_spec["asset_name"],
                query,
            )
            
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
                                    asset_spec["asset_name"],
                                    timestamp,
                                    value,
                                )
            except Exception as e:
                self.logger.error(
                    "Error querying asset %s: %s",
                    asset_spec["asset_name"],
                    str(e),
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

    def create_df_baseline_from_db(self, portfolio, adjusted_time, bs_cfg):
        return self.create_df_baseline_from_persistence(
            portfolio, adjusted_time, bs_cfg
        )

    def create_df_baseline_from_aem_forecast_actual(
        self, portfolio, adjusted_time, bs_cfg
    ):
        aem_cfg = self._get_aem_forecast_actual_settings(bs_cfg)
        measurement = aem_cfg.get("measurement", "hyp_baseline_forecast_actual")
        missing_asset_policy = self._get_missing_asset_policy(bs_cfg)
        if missing_asset_policy not in {"persistence_fallback", "skip"}:
            self.logger.error(
                "Missing asset policy option '%s' not available for aem_forecast_actual. Supported values: persistence_fallback, skip",
                missing_asset_policy,
            )
            return False

        persistence_cfg = bs_cfg["dbSettings"]
        max_inspection_hours = self._get_aem_max_inspection_hours(aem_cfg)
        expected_timestamps = self._build_expected_baseline_timestamps(
            adjusted_time, max_inspection_hours
        )
        if expected_timestamps is None:
            return False

        start_dt = expected_timestamps[0]
        max_end_dt = expected_timestamps[-1] + timedelta(
            minutes=self.main_cfg["fm"]["granularity"]
        )
        expected_timestamp_set = set(expected_timestamps)

        asset_specs = self._get_portfolio_asset_specs(portfolio)
        if not asset_specs:
            self.logger.info(
                "Portfolio %s has no assets; using zero baseline for AEM forecast actual",
                portfolio.id,
            )
            return self._build_zero_baseline_dataframe(
                portfolio, adjusted_time, persistence_cfg
            )
        if not self._validate_unique_aem_asset_labels(portfolio, asset_specs):
            return False
        classified_asset_specs = self._classify_aem_asset_specs(
            portfolio, asset_specs, aem_cfg
        )
        if classified_asset_specs is None:
            return False
        asset_specs, required_asset_specs, optional_asset_specs = classified_asset_specs

        self.logger.info(
            "AEM forecast actual lookup for portfolio %s, measurement=%s, max_query_horizon=[%s,%s), inspection_rows=%i, portfolio_assets=%i",
            portfolio.id,
            measurement,
            start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            max_end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            len(expected_timestamps),
            len(asset_specs),
        )
        self.logger.info(
            "AEM forecast actual queries are restricted to portfolio asset_label filters; no broad all-assets query is used"
        )
        self.logger.info(
            "Baseline source units: AEM measurement %s and persistence source %s are in W; aggregated baseline stays in W internally, converts W->MW for Nodes upload, and stores W in portfolios_baselines",
            measurement,
            self.main_cfg["influxDB"].get("assetsMeasurement", "assets_data"),
        )
        fallback_query_cfg = copy.deepcopy(persistence_cfg)
        fallback_query_cfg["upcomingHoursToQuery"] = max_inspection_hours
        aem_series_by_asset = {}
        required_asset_series = {}
        raw_asset_summaries = []

        for asset_spec in asset_specs:
            aem_values_by_timestamp = self._query_aem_asset_series_w(
                asset_spec,
                measurement,
                start_dt,
                max_end_dt,
                expected_timestamp_set,
            )
            if aem_values_by_timestamp in (None, False):
                return aem_values_by_timestamp

            aem_series_by_asset[asset_spec["asset_name"]] = aem_values_by_timestamp
            if asset_spec["aem_requirement"] == "required":
                required_asset_series[asset_spec["asset_name"]] = aem_values_by_timestamp

            sorted_asset_timestamps = sorted(aem_values_by_timestamp.keys())
            raw_asset_summaries.append(
                {
                    "asset_name": asset_spec["asset_name"],
                    "asset_label": asset_spec["asset_label"],
                    "aem_requirement": asset_spec["aem_requirement"],
                    "points_found": len(sorted_asset_timestamps),
                    "first_timestamp": (
                        sorted_asset_timestamps[0].strftime("%Y-%m-%dT%H:%M:%SZ")
                        if sorted_asset_timestamps
                        else "<none>"
                    ),
                    "last_timestamp": (
                        sorted_asset_timestamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ")
                        if sorted_asset_timestamps
                        else "<none>"
                    ),
                }
            )

        for asset_summary in raw_asset_summaries:
            self.logger.info(
                "AEM asset availability %s (asset_label=%s, requirement=%s): points_found=%i first_timestamp=%s last_timestamp=%s",
                asset_summary["asset_name"],
                asset_summary["asset_label"],
                asset_summary["aem_requirement"],
                asset_summary["points_found"],
                asset_summary["first_timestamp"],
                asset_summary["last_timestamp"],
            )

        detected_horizon = self._detect_contiguous_aem_horizon(
            portfolio, required_asset_series, expected_timestamps
        )
        if detected_horizon is None:
            return False
        upload_timestamps, stopping_timestamp, stopping_assets = detected_horizon
        horizon_end_dt = upload_timestamps[-1] + timedelta(
            minutes=self.main_cfg["fm"]["granularity"]
        )
        tail_rows_not_uploaded = len(expected_timestamps) - len(upload_timestamps)
        self.logger.info(
            "Detected contiguous AEM upload horizon for portfolio %s: rows=%i horizon=[%s,%s)",
            portfolio.id,
            len(upload_timestamps),
            upload_timestamps[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            horizon_end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if stopping_timestamp is not None:
            self.logger.info(
                "AEM upload horizon stops at first missing required forecast timestamp %s due to assets %s",
                stopping_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                stopping_assets,
            )
        if tail_rows_not_uploaded > 0:
            self.logger.info(
                "AEM unavailable tail beyond the detected horizon is intentionally not uploaded: %i quarter-hour rows inside max query horizon [%s,%s)",
                tail_rows_not_uploaded,
                stopping_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                if stopping_timestamp is not None
                else horizon_end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        else:
            self.logger.info(
                "AEM forecast coverage spans the full configured maximum query horizon for portfolio %s",
                portfolio.id,
            )

        aggregated_quantity_w = {}
        contribution_counts = {timestamp: 0 for timestamp in upload_timestamps}
        source_summary = {
            "aem_points": 0,
            "persistence_fallback_points": 0,
            "skipped_points": 0,
            "unfilled_points": 0,
        }
        asset_log_summaries = []

        for asset_spec in asset_specs:
            aem_values_by_timestamp = aem_series_by_asset[asset_spec["asset_name"]]
            fallback_values_by_timestamp = None
            missing_count = sum(
                1 for timestamp in upload_timestamps if timestamp not in aem_values_by_timestamp
            )
            if asset_spec["aem_requirement"] == "required" and missing_count > 0:
                self.logger.error(
                    "Required AEM asset %s is missing %i timestamps inside the detected upload horizon",
                    asset_spec["asset_name"],
                    missing_count,
                )
                return False
            fallback_count = 0
            skipped_count = 0
            unfilled_count = 0

            if (
                asset_spec["aem_requirement"] == "optional"
                and missing_count > 0
                and missing_asset_policy == "persistence_fallback"
            ):
                fallback_values_by_timestamp = self._query_persistence_asset_series_w(
                    asset_spec, adjusted_time, fallback_query_cfg
                )
                if fallback_values_by_timestamp in (None, False):
                    return fallback_values_by_timestamp
                unexpected_fallback_timestamps = sorted(
                    set(fallback_values_by_timestamp.keys()) - expected_timestamp_set
                )
                if unexpected_fallback_timestamps:
                    self.logger.error(
                        "Persistence fallback returned timestamps outside the maximum AEM inspection horizon for asset %s: %s",
                        asset_spec["asset_name"],
                        [
                            timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                            for timestamp in unexpected_fallback_timestamps[:5]
                        ],
                    )
                    return False

            for timestamp in upload_timestamps:
                if timestamp in aem_values_by_timestamp:
                    aggregated_quantity_w[timestamp] = (
                        aggregated_quantity_w.get(timestamp, 0.0)
                        + aem_values_by_timestamp[timestamp]
                    )
                    contribution_counts[timestamp] += 1
                    source_summary["aem_points"] += 1
                    continue

                if (
                    asset_spec["aem_requirement"] == "optional"
                    and missing_asset_policy == "persistence_fallback"
                ):
                    fallback_value = None
                    if fallback_values_by_timestamp is not None:
                        fallback_value = fallback_values_by_timestamp.get(timestamp)
                    if fallback_value is not None:
                        aggregated_quantity_w[timestamp] = (
                            aggregated_quantity_w.get(timestamp, 0.0)
                            + fallback_value
                        )
                        contribution_counts[timestamp] += 1
                        fallback_count += 1
                        source_summary["persistence_fallback_points"] += 1
                    else:
                        unfilled_count += 1
                        source_summary["unfilled_points"] += 1
                elif asset_spec["aem_requirement"] == "optional":
                    skipped_count += 1
                    source_summary["skipped_points"] += 1
                else:
                    self.logger.error(
                        "Required AEM asset %s unexpectedly has no value at %s inside the detected upload horizon",
                        asset_spec["asset_name"],
                        timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    return False

            asset_log_summaries.append(
                {
                    "asset_name": asset_spec["asset_name"],
                    "asset_label": asset_spec["asset_label"],
                    "aem_requirement": asset_spec["aem_requirement"],
                    "horizon_points": len(upload_timestamps),
                    "aem_points": len(upload_timestamps) - missing_count,
                    "missing_points": missing_count,
                    "fallback_points": fallback_count,
                    "skipped_points": skipped_count,
                    "unfilled_points": unfilled_count,
                }
            )

        for asset_summary in asset_log_summaries:
            self.logger.info(
                "AEM asset contribution %s (asset_label=%s, requirement=%s): horizon_points=%i aem_points=%i missing_points=%i fallback_points=%i skipped_points=%i unfilled_points=%i",
                asset_summary["asset_name"],
                asset_summary["asset_label"],
                asset_summary["aem_requirement"],
                asset_summary["horizon_points"],
                asset_summary["aem_points"],
                asset_summary["missing_points"],
                asset_summary["fallback_points"],
                asset_summary["skipped_points"],
                asset_summary["unfilled_points"],
            )

        asset_count = len(asset_specs)
        full_coverage_timestamps = sum(
            1 for count in contribution_counts.values() if count == asset_count
        )
        partial_coverage_timestamps = sum(
            1 for count in contribution_counts.values() if 0 < count < asset_count
        )
        no_coverage_timestamps = sum(
            1 for count in contribution_counts.values() if count == 0
        )

        df = self._build_baseline_dataframe_from_w_series(
            portfolio, aggregated_quantity_w
        )
        final_row_count = len(df)
        self.logger.info(
            "AEM expected inspection timestamps for portfolio %s: %i",
            portfolio.id,
            len(expected_timestamps),
        )
        self.logger.info(
            "AEM coverage summary within the detected upload horizon for portfolio %s: full=%i partial=%i none=%i",
            portfolio.id,
            full_coverage_timestamps,
            partial_coverage_timestamps,
            no_coverage_timestamps,
        )
        self.logger.info(
            "AEM final payload rows for portfolio %s: %i",
            portfolio.id,
            final_row_count,
        )
        self.logger.info(
            "Optional assets inside the detected AEM horizon are handled with missing_asset_policy=%s",
            missing_asset_policy,
        )

        if final_row_count == 0:
            self.logger.error(
                "AEM forecast actual produced no payload rows for portfolio %s after applying missing_asset_policy=%s",
                portfolio.id,
                missing_asset_policy,
            )
            return False

        period_from = pd.to_datetime(df["periodFrom"], utc=True)
        if final_row_count != len(upload_timestamps):
            self.logger.error(
                "AEM forecast actual baseline for portfolio %s produced %i rows instead of the detected upload horizon length %i",
                portfolio.id,
                final_row_count,
                len(upload_timestamps),
            )
            return False
        if period_from.duplicated().any():
            self.logger.error(
                "AEM forecast actual baseline for portfolio %s contains duplicate timestamps",
                portfolio.id,
            )
            return False
        if pd.isna(df["quantity"]).any():
            self.logger.error(
                "AEM forecast actual baseline for portfolio %s contains NaN/None values",
                portfolio.id,
            )
            return False
        expected_period_from = pd.to_datetime(
            [timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") for timestamp in upload_timestamps],
            utc=True,
        )
        if list(period_from) != list(expected_period_from):
            self.logger.error(
                "AEM forecast actual baseline for portfolio %s is not a contiguous quarter-hour block starting from the upcoming quarter-hour",
                portfolio.id,
            )
            return False
        if no_coverage_timestamps > 0:
            self.logger.error(
                "AEM detected upload horizon for portfolio %s contains %i timestamps with no contributing assets",
                portfolio.id,
                no_coverage_timestamps,
            )
            return False

        self.logger.info(
            "AEM forecast actual strategy produced a contiguous forecast baseline for portfolio %s with missing_asset_policy=%s",
            portfolio.id,
            missing_asset_policy,
        )
        self.logger.info(
            "AEM source summary for portfolio %s: %s",
            portfolio.id,
            source_summary,
        )
        if (
            missing_asset_policy == "persistence_fallback"
            and source_summary["unfilled_points"] > 0
        ):
            self.logger.error(
                "AEM persistence_fallback mode could not fill %i optional asset-slot pairs inside the detected upload horizon for portfolio %s",
                source_summary["unfilled_points"],
                portfolio.id,
            )
            return False

        self._baseline_logging_context[portfolio.id] = source_summary
        return df
