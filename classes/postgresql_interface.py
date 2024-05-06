# import section

import datetime
import json
import pytz
import pandas as pd
import psycopg2
import psycopg2.extras


class PostgreSQLInterface:
    """
    Class handling the interface with a PostgreSQL instance
    """

    def __init__(self, cfg, logger):
        """
        Constructor
        """
        # set the variables
        self.cfg = cfg
        self.logger = logger
        self. conn = psycopg2.connect(host=cfg['host'], port=cfg['port'],
                                      user=cfg['user'], password=cfg['password'],
                                      dbname=cfg['database'])
        self.conn.autocommit = True
        self.logger = logger

    @staticmethod
    def prepare_tso_query(filter):
        int_sql = "SELECT chg_sf_child.id FROM hive_catalog_rd.site AS chg_sf_child, " \
                  "hive_changelog_rd.site_family AS chg_sf, hive_catalog_rd.site AS chg_sf_parent " \
                  "WHERE chg_sf_parent.id=chg_sf.fk_site_parent AND chg_sf_child.id=chg_sf.fk_site_child AND " \
                  "chg_sf_parent.name = '%s' AND chg_sf_parent.active=TRUE AND chg_sf_child.active=TRUE" % filter['site_id']

        sql = "SELECT ctg_site_parent.name AS ss_name, ctg_site_child.city, ctg_site_child.name AS pod, " \
              "ctg_device.name AS device_name, ctg_device_type.name AS device_type, chg_device_parameter.properties AS device_properties, ctg_device.id AS device_id, chg_device_parameter.id AS device_parameter_id " \
              "FROM hive_catalog_rd.site AS ctg_site_parent, hive_catalog_rd.site AS ctg_site_child, " \
              "hive_catalog_rd.device AS ctg_device, hive_catalog_rd.device_type AS ctg_device_type," \
              "hive_changelog_rd.site_family AS chg_sf, hive_changelog_rd.site_device AS chg_site_device, " \
              "hive_changelog_rd.device_parameter AS chg_device_parameter " \
              "WHERE ctg_site_parent.id=chg_sf.fk_site_parent AND ctg_site_child.id=chg_sf.fk_site_child AND ctg_device.id = chg_site_device.fk_device AND " \
              "ctg_site_child.id = chg_site_device.fk_site AND ctg_device_type.name = chg_device_parameter.fk_device_type AND " \
              "ctg_device.id = chg_device_parameter.fk_device AND ctg_site_parent.id IN (%s) AND " \
              "chg_sf.active=TRUE AND chg_device_parameter.active=TRUE AND chg_site_device.active=TRUE" % int_sql
        if 'flexibility_type' in filter.keys():
            sql = '%s AND ctg_device_type.name = \'%s\'' % (sql, filter['flexibility_type'])
        sql = '%s ORDER BY ctg_site_parent.name, ctg_site_child.city, ctg_site_child.name' % sql
        return sql

    @staticmethod
    def prepare_ss_query(filter):
        sql = "SELECT ctg_site_parent.name AS ss_name, ctg_site_child.city, ctg_site_child.name AS pod, " \
              "ctg_device.name AS device_name, ctg_device_type.name AS device_type, chg_device_parameter.properties AS device_properties, ctg_device.id AS device_id, chg_device_parameter.id AS device_parameter_id " \
              "FROM hive_changelog_rd.site_family AS chg_sf, hive_catalog_rd.site AS ctg_site_parent, " \
              "hive_catalog_rd.site AS ctg_site_child, hive_catalog_rd.device AS ctg_device," \
              "hive_changelog_rd.site_device AS chg_site_device, " \
              "hive_changelog_rd.device_parameter AS chg_device_parameter, hive_catalog_rd.device_type AS ctg_device_type " \
              "WHERE ctg_site_parent.id=chg_sf.fk_site_parent AND ctg_site_child.id=chg_sf.fk_site_child AND " \
              "ctg_device.id = chg_site_device.fk_device AND ctg_site_child.id = chg_site_device.fk_site AND " \
              "ctg_device_type.name = chg_device_parameter.fk_device_type AND " \
              "ctg_device.id = chg_device_parameter.fk_device AND ctg_site_parent.name = '%s' AND " \
              "chg_sf.active=TRUE AND chg_device_parameter.active=TRUE" % filter['site_id']
        if 'flexibility_type' in filter.keys():
            sql = '%s AND ctg_device_type.name = \'%s\'' % (sql, filter['flexibility_type'])
        sql = '%s ORDER BY ctg_site_parent.name, ctg_site_child.city, ctg_site_child.name' % sql
        return sql

    @staticmethod
    def prepare_dso_query(filter):
        sql = "SELECT ctg_device.name AS device_name, ctg_device_type.name AS device_type, chg_device_parameter.properties AS device_properties, ctg_device.id AS device_id, chg_device_parameter.id AS device_parameter_id " \
              "FROM hive_catalog_rd.site AS ctg_site, hive_catalog_rd.device AS ctg_device, hive_changelog_rd.site_device AS chg_site_device, " \
              "hive_changelog_rd.device_parameter AS chg_device_parameter, hive_catalog_rd.device_type AS ctg_device_type " \
              "WHERE ctg_site.id = chg_site_device.fk_site and ctg_device.id = chg_site_device.fk_device AND " \
              "ctg_device_type.name = chg_device_parameter.fk_device_type AND ctg_device.id = chg_device_parameter.fk_device " \
              "AND ctg_site.name = '%s' AND chg_device_parameter.active = TRUE" % filter['site_id']

        if 'flexibility_type' in filter.keys():
            sql = '%s AND ctg_device_type.name = \'%s\'' % (sql, filter['flexibility_type'])
        sql = '%s ORDER BY ctg_device.name' % sql
        return sql

    def get_flexibility_list(self, filter):
        if filter['case'] == 'vcc':
            sql = self.prepare_tso_query(filter)
        elif filter['case'] == 'substation':
            sql = self.prepare_ss_query(filter)
        else:
            sql = self.prepare_dso_query(filter)

        cur = self.conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()

        flexi_list = []
        while row is not None:
            if filter['case'] != 'pod':
                flexi_list.append({
                    'substation': row[0],
                    'city': row[1],
                    'site_id': row[2],
                    'device_id': row[3],
                    'flexibility_type': row[4],
                    'flexibility_properties': row[5],
                    'id': row[6],
                    'device_parameter_id': row[7]
                })
            else:
                flexi_list.append({'device_name': row[0],'flexibility_type': row[1],'flexibility_properties': row[2]})
            row = cur.fetchone()
        cur.close()

        return flexi_list

    def get_flexibility_metadata(self, site_code, flexi_code):
        sql = 'SELECT ctg_device.name AS device_name, chg_device_parameter.properties AS device_properties, ' \
              'chg_device_parameter.id AS chg_device_parameter_id ' \
              'FROM hive_catalog_rd.site AS ctg_site, hive_catalog_rd.device AS ctg_device, ' \
              'hive_changelog_rd.site_device AS chg_site_device, ' \
              'hive_changelog_rd.device_parameter AS chg_device_parameter  ' \
              'WHERE ctg_site.id = chg_site_device.fk_site AND ctg_device.id = chg_site_device.fk_device AND ' \
              'ctg_device.id=chg_device_parameter.fk_device AND ctg_site.name = \'%s\' AND ' \
              'chg_device_parameter.fk_device_type = \'%s\' AND chg_site_device.active = TRUE AND ' \
              'chg_device_parameter.active = TRUE' % (site_code, flexi_code)
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount > 0:
            rows = cur.fetchall()
            cur.close()
            res_props = {}
            res_ids = {}
            for row in rows:
                res_props[row[0]] = row[1]
                res_ids[row[0]] = row[2]
            return res_props, res_ids
        else:
            cur.close()
            return None

    def get_table_row(self, id, table):
        sql = 'SELECT * FROM %s WHERE id = \'%s\'' % (table, id)
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount == 1:
            row = cur.fetchall()
            row_dict = [{k: v for k, v in record.items()} for record in row]
            cur.close()
            return row_dict[0]
        else:
            cur.close()
            return None

    def get_site_info_from_pod(self, pod):
        sql = 'SELECT * FROM postgres.hive_catalog_rd.site WHERE pod = \'%s\'' % pod
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount == 1:
            row = cur.fetchall()
            row_dict = [{k: v for k, v in record.items()} for record in row]
            cur.close()
            return row_dict[0]
        else:
            cur.close()
            return None

    def deactivate_device_parameter(self, device_parameter_id):
        today = datetime.datetime.today().strftime('%Y-%m-%d')
        sql = 'UPDATE hive_changelog_rd.device_parameter ' \
              'SET end_date = \'%s\', active=FALSE WHERE id = \'%s\'' % (today, device_parameter_id)
        cur = self.conn.cursor()
        cur.execute(sql)
        cur.close()

    def insert_device_parameter(self, device_id, device_type_id, organization_id, properties):
        today = datetime.datetime.today().strftime('%Y-%m-%d')
        sql = 'INSERT INTO hive_changelog_rd.device_parameter ' \
              '(fk_device, fk_device_type, fk_organization, start_date, properties) VALUES' \
              '(\'%s\',\'%s\',\'%s\',\'%s\',\'%s\')' % (device_id, device_type_id, organization_id, today,
                                                        json.dumps(properties))
        cur = self.conn.cursor()
        cur.execute(sql)
        cur.close()

    def save_forecast(self, preds, preds_quantiles, day_ahead_electricity_prices, run_dt):
        # Transform data index from local time to UTC
        preds = preds.copy()
        preds_quantiles = preds_quantiles.copy()
        day_ahead_electricity_prices = day_ahead_electricity_prices.copy()

        preds.index = preds.index.tz_convert(pytz.UTC)
        preds_quantiles.index = preds_quantiles.index.tz_convert(pytz.UTC)
        day_ahead_electricity_prices.index = day_ahead_electricity_prices.index.tz_convert(pytz.UTC)

        # Get site info given the POD
        site_info = self.get_site_info_from_pod(preds.columns[0])

        # Insert predictions in the DB
        id_forecast = self.insert_forecast_data(site_info['id'], run_dt, 'mean', preds[site_info['pod']])
        self.insert_forecast_data(site_info['id'], run_dt, 'day_ahead_price_mean', day_ahead_electricity_prices)
        for quantile in preds_quantiles.columns:
            self.insert_forecast_data(site_info['id'], run_dt, quantile[1], preds_quantiles[quantile[0]][quantile[1]])
        return id_forecast

    def save_forecast_controlled(self, vcp_id, group_responses_df, preds, scale_factor, run_dt):
        # Transform data index from local time to UTC
        group_responses_df = group_responses_df.copy()
        preds = preds.copy()
        group_responses_df.index = group_responses_df.index.tz_convert(pytz.UTC)
        preds.index = preds.index.tz_convert(pytz.UTC)

        # Get site info given the POD
        site_info = self.get_site_info_from_pod(vcp_id)

        for device in group_responses_df.loc[:, (slice(None), 'tot')].columns:
            function_name = 'controlled_{}_{}'.format(device[0], device[1])
            self.insert_forecast_data(site_info['id'], run_dt, function_name, group_responses_df[device])
        self.insert_forecast_data(site_info['id'], run_dt, 'controlled_all_tot', group_responses_df.groupby(level=1, axis=1).sum()['tot'])
        self.insert_forecast_data(site_info['id'], run_dt, 'controlled_all_baseline', group_responses_df.groupby(level=1, axis=1).sum()['baseline'])
        self.insert_forecast_data(site_info['id'], run_dt, 'controlled_all_diff', group_responses_df.groupby(level=1, axis=1).sum()['diff'])
        self.insert_forecast_data(site_info['id'], run_dt, 'controlled_plus_uncontrolled_all_tot', preds[site_info['pod']].add(scale_factor * group_responses_df.groupby(level=1, axis=1).sum()['tot']))
        self.insert_forecast_data(site_info['id'], run_dt, 'controlled_plus_uncontrolled_all_baseline', preds[site_info['pod']].add(scale_factor * group_responses_df.groupby(level=1, axis=1).sum()['baseline']))

    def insert_forecast_data(self, site_id, run_now, function, forecast_data):
        cur = self.conn.cursor()

        sql = 'INSERT INTO etl.orchestra_forecast (site_id, run_dt, function) VALUES ' \
              '(\'%s\',\'%s\',\'%s\') RETURNING id' % (site_id, run_now.strftime('%Y-%m-%d %H:%M:%S'), function)

        cur.execute(sql)
        last_inserted_row = cur.fetchone()

        sql = "INSERT INTO etl.orchestra_forecast_time_series (forecast_id, dt, value) VALUES "
        for elem in forecast_data.items():
            sql = '%s (\'%s\',\'%s\',%f),' % (sql, last_inserted_row[0], elem[0], elem[1])
        sql = sql[0:-1]
        cur.execute(sql)
        cur.close()
        return last_inserted_row[0]

    def check_prosumer(self, consumer_pod):
        sql = "SELECT ctg_site_child.name " \
              "FROM hive_catalog_rd.site AS ctg_site_parent, hive_catalog_rd.site AS ctg_site_child, " \
              "hive_changelog_rd.site_family AS chg_sf " \
              "WHERE ctg_site_parent.id = chg_sf.fk_site_parent AND ctg_site_child.id = chg_sf.fk_site_child AND " \
              "ctg_site_parent.name = \'%s\' AND chg_sf.active=TRUE" % consumer_pod
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount > 0:
            res = cur.fetchall()
            cur.close()
            return res[0][0]
        else:
            cur.close()
            return None

    def get_active_switching_tables(self):
        sql = "SELECT id, st_id  FROM etl.switching_table WHERE active=TRUE"
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount > 0:
            pg_ids = []
            sm_ids = []
            for row in cur:
                pg_ids.append(row[0])
                sm_ids.append(row[1])
            cur.close()
            return pg_ids, sm_ids
        else:
            cur.close()
            return None, None

    def get_switch_site_data(self, site_id, flexi_code):
        sql = "SELECT * FROM etl.switch_site " \
              "WHERE site_id = \'%s\' AND load = \'%s\' AND active=TRUE" % (site_id, flexi_code)
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)

        if cur.rowcount == 1:
            row = cur.fetchall()
            row_dict = [{k: v for k, v in record.items()} for record in row]
            cur.close()
            return row_dict[0]
        else:
            cur.close()
            return None

    def deactivate_switching_table(self, id_switching_table):
        sql = "UPDATE etl.switching_table SET active=FALSE WHERE id=\'%s\'" % id_switching_table
        cur = self.conn.cursor()
        res = cur.execute(sql)
        cur.close()
        return res

    def add_switching_table(self, st_evulution_id, st_number, st_name, properties):
        sql = 'INSERT INTO etl.switching_table (st_id, number, name, start_dt, properties) VALUES ' \
              '(%i, %i, \'%s\',\'%s\', \'%s\') RETURNING id' % (st_evulution_id, st_number, st_name,
                                                                datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                                                json.dumps(properties))
        cur = self.conn.cursor()
        cur.execute(sql)
        last_inserted_row = cur.fetchone()
        cur.close()
        return last_inserted_row[0]

    def add_scheduling_to_switching_table(self, st_id, new_scheduling):
        sql = 'INSERT INTO etl.switching_table_smart_manager_scheduling (fk_switching_table_id, trigger_time_utc, ' \
              'release_time_utc, valid_monday, valid_tuesday, valid_wednesday, valid_thursday, valid_friday, ' \
              'valid_saturday, valid_sunday, start_dt) VALUES (\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',' \
              '\'%s\',\'%s\',\'%s\')' % (st_id, str(new_scheduling['triggerTimeUtc']),
                                         str(new_scheduling['releaseTimeUtc']), new_scheduling['validMonday'],
                                         new_scheduling['validTuesday'], new_scheduling['validWednesday'],
                                         new_scheduling['validThursday'], new_scheduling['validFriday'],
                                         new_scheduling['validSaturday'], new_scheduling['validSunday'],
                                         datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'))
        cur = self.conn.cursor()
        res = cur.execute(sql)
        cur.close()
        return res

    def save_force_offs(self, st_id, df_scheduling):
        sql = "INSERT INTO etl.switching_table_force_off (switching_table_id, dt, force_off) VALUES "
        for elem in df_scheduling.iterrows():
            sql = '%s (\'%s\',\'%s\',\'%s\'),' % (sql, st_id, elem[0], elem[1]['force_off'])
        sql = sql[0:-1]
        cur = self.conn.cursor()
        res = cur.execute(sql)
        cur.close()
        return res

    def add_switch_to_switching_table(self, st_id, smart_manager_id, switch_number):
        sql = 'INSERT INTO etl.switching_table_smart_manager_switch (fk_switching_table_id, smart_manager_id, ' \
              'switch_port, start_dt) VALUES (\'%s\',\'%s\',%i,\'%s\')' % (st_id, smart_manager_id, switch_number,
                                                                           datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'))
        cur = self.conn.cursor()
        res = cur.execute(sql)
        cur.close()
        return res

    def get_site_info(self, id_site):
        sql = "SELECT ctg_site.*, ctg_site_type.name AS type " \
              "FROM hive_catalog_rd.site AS ctg_site, hive_catalog_rd.site_type AS ctg_site_type, hive_changelog_rd.site_parameter AS chg_site_parameter " \
              "WHERE ctg_site.id = chg_site_parameter.fk_site AND ctg_site_type.name = chg_site_parameter.fk_site_type AND chg_site_parameter.active = TRUE AND " \
              "ctg_site.pod = \'%s\'" % id_site

        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        row = cur.fetchall()
        cur.close()
        return row[0]

    def get_force_off(self, st_id, t_start_local, t_end_local):
        # Localize the start/emd time in UTC
        t_start_utc = t_start_local.astimezone(pytz.UTC)
        t_end_utc = t_end_local.astimezone(pytz.UTC)

        sql = "select stfo.dt, stfo.force_off " \
              "from etl.switching_table_force_off stfo " \
              "where stfo.switching_table_id = '%s' and stfo.dt >= '%s' and stfo.dt < '%s' " \
              "order by stfo.dt asc" % (
              st_id, t_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
              t_end_utc.strftime('%Y-%m-%d %H:%M:%S'))

        cur = self.conn.cursor()
        cur.execute(sql)

        dt = []
        force_off = []
        for row in cur:
            dt.append(row[0])
            force_off.append(row[1])

        data = pd.DataFrame(force_off, columns=['force_off'], index=dt, dtype=bool)
        return data

    def get_force_off_from_evulution_st_id(self, evu_st_id, t_start_local, t_end_local):
        # Localize the start/emd time in UTC
        t_start_utc = t_start_local.astimezone(pytz.UTC)
        t_end_utc = t_end_local.astimezone(pytz.UTC)

        sql = "select stfo.dt, stfo.force_off " \
              "from etl.switching_table_force_off stfo, etl.switching_table st " \
              "where stfo.switching_table_id = st.id and st.st_id = '%s' and stfo.dt >= '%s' and stfo.dt < '%s' " \
              "order by stfo.dt asc" % (
              evu_st_id, t_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
              t_end_utc.strftime('%Y-%m-%d %H:%M:%S'))

        cur = self.conn.cursor()
        cur.execute(sql)

        dt = []
        force_off = []
        for row in cur:
            dt.append(row[0])
            force_off.append(row[1])

        data = pd.DataFrame(force_off, columns=['force_off'], index=dt, dtype=bool)
        return data

    def get_force_off_history(self, t_start_local, t_end_local, id_smart_manager, relay):
        # Localize the start/emd time in UTC
        t_start_utc = t_start_local.astimezone(pytz.UTC)
        t_end_utc = t_end_local.astimezone(pytz.UTC)

        sql = "select stfo.dt, stfo.force_off, st.id " \
              "from etl.switching_table st, etl.switching_table_force_off stfo, etl.switching_table_smart_manager_switch stsms " \
              "where st.id = stfo.switching_table_id and st.id = stsms.fk_switching_table_id  and stfo.dt between '%s' and '%s' and " \
              "stsms.smart_manager_id = '%s' and stsms.switch_port=%i order by st.id asc, stfo.dt asc" % (t_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                                                                                                          t_end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                                                                                                          id_smart_manager, relay)

        cur = self.conn.cursor()
        cur.execute(sql)

        dt = []
        force_off = []
        st_ids = []
        for row in cur:
            dt.append(row[0])
            force_off.append(row[1])
            st_ids.append(row[2])

        data = pd.DataFrame(list(zip(dt, force_off, st_ids)), columns=['dt', 'force_off', 'switching_table_id'])
        data_id_dt = data.set_index('dt')

        # Clean the dataframe to avoid duplicate indexes, it should never happen
        data_id_dt = data_id_dt[~data_id_dt.index.duplicated(keep='last')]

        # Convert the downloaded data from UTC to local time
        if not data_id_dt.empty:
            data_id_dt.index = data_id_dt.index.tz_localize('UTC')
            data_id_dt.index = data_id_dt.index.tz_convert(tz=t_start_local.tzinfo)

        cur.close()
        return data_id_dt

    def get_force_off_properties(self, t_start_local, t_end_local, id_smart_manager, relay):
        """
        :param: t_start_local: pd.Timestamp, start time in local time
        :param: t_end_local: pd.Timestamp, end time in local time
        :param: id_smart_manager: str, id of the smart manager
        :param: relay: int, relay number
        :return: pd.DataFrame, index: dt, columns: properties
        """
        # Localize the start/emd time in UTC
        t_start_utc = t_start_local.astimezone(pytz.UTC)
        t_end_utc = t_end_local.astimezone(pytz.UTC)

        sql = """select stfo.dt, st.properties
                from etl.switching_table st
                join etl.switching_table_force_off stfo on st.id = stfo.switching_table_id
                join etl.switching_table_smart_manager_switch stsms on st.id = stsms.fk_switching_table_id
                where stfo.dt between '%s' and '%s'
                and stsms.smart_manager_id = '%s'
                and stsms.switch_port=%i
                and stfo.dt = (select max(stfo2.dt)
                               from etl.switching_table_force_off stfo2
                               where stfo2.switching_table_id = st.id)
                order by st.id desc, stfo.dt desc
                limit 1""" % (t_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                              t_end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                              id_smart_manager, relay)

        cur = self.conn.cursor()
        cur.execute(sql)

        dt = []
        properties = []
        for row in cur:
            dt.append(row[0])
            properties.append(row[1])

        data = pd.DataFrame(list(zip(dt, properties)), columns=['dt', 'properties'])
        data_id_dt = data.set_index('dt')

        # Clean the dataframe to avoid duplicate indexes, it should never happen
        data_id_dt = data_id_dt[~data_id_dt.index.duplicated(keep='last')]

        # Convert the downloaded data from UTC to local time
        if not data_id_dt.empty:
            data_id_dt.index = data_id_dt.index.tz_localize('UTC')
            data_id_dt.index = data_id_dt.index.tz_convert(tz=t_start_local.tzinfo)

        cur.close()
        return data_id_dt


    def get_forecast(self, t_start_local, t_end_local, site_pod, func):
        # Localize the start/emd time in UTC
        t_start_utc = t_start_local.astimezone(pytz.UTC)
        t_end_utc = t_end_local.astimezone(pytz.UTC)

        sql = "select ofer.run_dt, ofer_ts.dt, ofer_ts.value " \
              "from hive_catalog_rd.site s, etl.orchestra_forecast ofer, etl.orchestra_forecast_time_series ofer_ts " \
              "where s.id = ofer.site_id and ofer.id = ofer_ts.forecast_id  and ofer_ts.dt between '%s' and '%s' and " \
              "s.pod = '%s' and ofer.function='%s' order by ofer.id asc, ofer.run_dt asc, ofer_ts.dt" % (t_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                                                                                                         t_end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                                                                                                         site_pod, func)

        cur = self.conn.cursor()
        cur.execute(sql)

        run_dts = []
        dts = []
        values = []
        for row in cur:
            run_dts.append(row[0])
            dts.append(row[1])
            values.append(row[2])

        data = pd.DataFrame(list(zip(run_dts, dts, values)), columns =['run_dt', 'dt', 'value'])

        data_id_dt = data.set_index('dt')

        # Clean the dataframe to avoid duplicate indexes, it should never happen
        data_id_dt = data_id_dt[~data_id_dt.index.duplicated(keep='last')]

        # Convert the downloaded data from UTC to local time
        if not data_id_dt.empty:
            data_id_dt.index = data_id_dt.index.tz_localize('UTC')
            data_id_dt.index = data_id_dt.index.tz_convert(tz=t_start_local.tzinfo)

        cur.close()
        return data_id_dt

    def insert_correspondence_dp_st(self, st_id, dp_id):
        cur = self.conn.cursor()

        # Check if there is already a correspondence in the DB
        query = ("select id from etl.switching_table_device_parameter where fk_switching_table = '%s' and "
                 "fk_device_parameter = '%s'" % (st_id, dp_id))
        cur.execute(query)
        data = cur.fetchall()

        if len(data) > 0:
            self.logger.warning('Correspondence [%s:%s] already in table etl.switching_table_device_parameter' % (
            st_id, dp_id))
            return None
        else:
            # Insert data in the DB
            sql = ("INSERT INTO etl.switching_table_device_parameter(fk_switching_table, "
                   "fk_device_parameter) VALUES('%s','%s') RETURNING id" % (st_id, dp_id))
            cur.execute(sql)
            last_inserted_row = cur.fetchone()
            return last_inserted_row

