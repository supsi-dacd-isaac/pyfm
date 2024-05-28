# import section
import json
import requests
import http
import os


class NODESInterface:
    """
    NODES interface class
    """
    def __init__(self, cfg, logger):
        """
        Constructor
        :param cfg: Dictionary with the configurable settings
        :type cfg: dict
        :param logger: Logger
        :type logger: Logger object
        """
        # Main variables
        self.cfg = cfg
        self.logger = logger
        self.token_data = None
        self.headers = None

    def set_token(self, player_cfg):
        tkn_file_name = '%s%s%s_%s.json' % (self.cfg['tokenFilesFolder'], os.sep, player_cfg['role'], player_cfg['id'])
        if os.path.exists(tkn_file_name):
            self.get_local_token(tkn_file_name)

            # Check if the already existing token is working
            r = self.get_user_info()
            if 'invalid_token' in r['title']:
                self.logger.warning('Local token is expired, a new token will be requested to NODES API')
                os.unlink(tkn_file_name)
                self.get_new_token(player_cfg, tkn_file_name)
            else:
                self.logger.info('Token stored in %s works, no new token will be requested to NODES API' % tkn_file_name)
        else:
            self.logger.info('File %s does not exist or work, a new token will be requested to NODES API' % tkn_file_name)
            self.get_new_token(player_cfg, tkn_file_name)

    def get_local_token(self, tkn_file_name):
        with open(tkn_file_name, "r") as json_file:
            self.token_data = json.load(json_file)
            self.headers = {'Authorization': f'Bearer {self.token_data["access_token"]}'}

    def test_token(self):
        with open(self.cfg['tokenFile'], "r") as json_file:
            self.token_data = json.load(json_file)
            self.headers = {'Authorization': f'Bearer {self.token_data["access_token"]}'}

    def get_new_token(self, player_cfg, tkn_file_name):
        payload = {
            'grant_type': self.cfg['grantType'],
            'client_id': self.cfg['players'][player_cfg['id']]['clientId'],
            'client_secret': self.cfg['players'][player_cfg['id']]['secretId'],
            'scope': self.cfg['scope'],
        }

        try:
            response = requests.post(self.cfg['tokenEndpoint'], data=payload,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})

            if response.status_code == http.HTTPStatus.OK:
                self.token_data = response.json()
                self.headers = {'Authorization': f'Bearer {self.token_data["access_token"]}'}
                self.logger.info("Access Token: %s" % self.token_data['access_token'])

                # Save token data locally
                with open(tkn_file_name, "w") as json_file:
                    json.dump(self.token_data, json_file)
                return True
            else:
                self.logger.error("Failed to retrieve token: %i; %s" % (response.status_code, response.text))
                return False
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False

    def get_user_info(self):
        return self.get_request('%sme' % self.cfg['mainEndpoint'])

    def get_version(self):
        return self.get_request('%sapi-version-info' % self.cfg['mainEndpoint'])

    def get_request(self, endpoint):
        try:
            response = requests.get(endpoint, headers=self.headers)
            if response.status_code == http.HTTPStatus.OK:
                self.logger.info('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            else:
                self.logger.warning('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            return json.loads(response.text)
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False

    def delete_request(self, endpoint):
        try:
            response = requests.delete(endpoint, headers=self.headers)
            if response.status_code == http.HTTPStatus.OK:
                self.logger.info('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            else:
                self.logger.warning('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            return response.ok
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False

    def post_csv_file_request(self, endpoint, tmp_baseline_file):
        try:
            headers = self.headers
            headers['accept'] = 'application/json'
            files = { 'file': (tmp_baseline_file.split(os.sep)[-1], open(tmp_baseline_file, 'rb'), 'text/csv') }
            response = requests.post(endpoint, headers=headers, files=files)

            if response.status_code == http.HTTPStatus.OK:
                self.logger.info('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            else:
                self.logger.warning('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            return json.loads(response.text)
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False

    def post_request(self, endpoint, data):
        self.logger.info('Body request: %s' % data)
        try:
            response = requests.post(endpoint, headers=self.headers, json=data)
            if response.status_code == http.HTTPStatus.OK:
                self.logger.info('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            else:
                self.logger.warning('Endpoint: %s, status code: %i' % (endpoint, response.status_code))
            self.logger.info('Body response: %s' % response.text)
            return response.ok
        except Exception as e:
            self.logger.error('EXCEPTION: %s' % str(e))
            return False

