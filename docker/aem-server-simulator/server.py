#!/usr/bin/env python3
"""
AEM Server Simulator

A simple REST server that accepts GET/POST requests and prints out
the request details including headers, query parameters, and body.

This is useful for testing and debugging the control commands sent
by the flexibility manager to the AEM API.

Usage:
    python server.py [--port 6000] [--host 0.0.0.0]

Environment variables:
    AEM_SERVER_PORT: Server port (default: 6000)
    AEM_SERVER_HOST: Server host (default: 0.0.0.0)
    AEM_SERVER_USER: Basic auth username (optional)
    AEM_SERVER_PASSWORD: Basic auth password (optional)
"""

import os
import json
import logging
import base64
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import argparse

# Log file path
LOG_DIR = os.environ.get("AEM_SERVER_LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "aem-server-simulator.log")

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging with two handlers:
# - Console: summary only (no payload)
# - File: full details including payload (INFO level, no DEBUG)

# Create formatters
log_format = "%(asctime)s :: %(levelname)s :: %(message)s"

# Console handler (summary only)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(log_format))

# File handler (full details, but INFO level - no DEBUG)
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(log_format))

# Create logger
logger = logging.getLogger("aem-server")
logger.setLevel(logging.DEBUG)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Create a separate logger for payload (file only, uses INFO level)
payload_logger = logging.getLogger("aem-server.payload")
payload_logger.setLevel(logging.INFO)
payload_logger.addHandler(file_handler)
payload_logger.propagate = False  # Don't propagate to console

# Configuration from environment (defaults)
SERVER_HOST = os.environ.get("AEM_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("AEM_SERVER_PORT", "6000"))

# Auth credentials - will be set by main()
_AUTH_CONFIG = {
    "user": os.environ.get("AEM_SERVER_USER", ""),
    "password": os.environ.get("AEM_SERVER_PASSWORD", "")
}


class AEMRequestHandler(BaseHTTPRequestHandler):
    """
    Simple HTTP request handler that logs all incoming requests.
    """

    def _check_auth(self) -> bool:
        """
        Check basic authentication if configured.

        :return: True if authenticated or no auth required
        """
        auth_user = _AUTH_CONFIG["user"]
        auth_password = _AUTH_CONFIG["password"]

        if not auth_user:
            return True  # No auth required

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False

        try:
            encoded = auth_header[6:]  # Remove "Basic " prefix
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            return username == auth_user and password == auth_password
        except Exception:
            return False

    def _send_unauthorized(self):
        """Send 401 Unauthorized response."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AEM Server Simulator"')
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {"error": "Unauthorized", "message": "Invalid credentials"}
        self.wfile.write(json.dumps(response).encode())

    def _log_request(self, method: str, body: bytes = None):
        """
        Log the full request details.

        Summary info goes to console and file.
        Body/payload goes only to file.

        :param method: HTTP method (GET, POST, etc.)
        :param body: Request body bytes (if any)
        """
        # Parse URL
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        # Get headers
        headers = dict(self.headers)

        # Build log message - summary to console + file
        separator = "=" * 70
        logger.info(separator)
        logger.info("INCOMING REQUEST")
        logger.info(separator)
        logger.info("Timestamp:   %s", datetime.now().isoformat())
        logger.info("Method:      %s", method)
        logger.info("Path:        %s", parsed.path)
        logger.info("Client:      %s:%s", self.client_address[0], self.client_address[1])

        # Log query parameters (to both)
        if query_params:
            logger.info("Query Params: %s", dict(query_params))

        # Log content length if body present
        if body:
            logger.info("Body Size:   %d bytes", len(body))

        logger.info(separator)

        # Log detailed info only to file via payload_logger
        payload_logger.info("-" * 40)
        payload_logger.info("Full URL:    %s", self.path)

        # Log headers to file only
        payload_logger.info("Headers:")
        for key, value in headers.items():
            if key.lower() == "authorization":
                payload_logger.info("  %s: [REDACTED]", key)
            else:
                payload_logger.info("  %s: %s", key, value)

        # Log body to file only
        if body:
            payload_logger.info("-" * 40)
            payload_logger.info("Body (%d bytes):", len(body))
            try:
                # Try to parse as JSON and log it on a single line
                body_json = json.loads(body.decode("utf-8"))
                payload_logger.info("%s", json.dumps(body_json, separators=(",", ":")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Log as raw string (single line)
                payload_logger.info("%s", body.decode("utf-8", errors="replace").replace("\n", " "))

        payload_logger.info(separator)

    def _send_response(self, status: int = 200, data: dict = None):
        """
        Send JSON response.

        :param status: HTTP status code
        :param data: Response data dictionary
        """
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if data is None:
            data = {
                "status": "ok",
                "message": "Request received",
                "timestamp": datetime.now().isoformat()
            }

        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_GET(self):
        """Handle GET requests."""
        # Determine response based on path
        parsed = urlparse(self.path)

        # Allow health check without authentication (and don't log it)
        if parsed.path == "/health":
            # Don't log health checks - they're too frequent
            response = {
                "status": "healthy",
                "uptime": "running",
                "timestamp": datetime.now().isoformat()
            }
            self._send_response(200, response)
            return

        # All other endpoints require authentication
        if not self._check_auth():
            self._send_unauthorized()
            return

        self._log_request("GET")


        if parsed.path == "/sensors":
            # Simulate sensor data response
            response = {
                "status": "ok",
                "endpoint": "sensors",
                "data": {
                    "temperature": 21.5,
                    "humidity": 45.0,
                    "power": 1250.0,
                    "timestamp": datetime.now().isoformat()
                }
            }
        else:
            response = {
                "status": "ok",
                "message": f"GET request received for {parsed.path}",
                "timestamp": datetime.now().isoformat()
            }

        self._send_response(200, response)

    def do_POST(self):
        """Handle POST requests."""
        if not self._check_auth():
            self._send_unauthorized()
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        self._log_request("POST", body)

        # Parse body if JSON
        body_data = None
        if body:
            try:
                body_data = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                body_data = {"raw": body.decode("utf-8", errors="replace")}

        # Determine response based on path
        parsed = urlparse(self.path)

        if parsed.path == "/control":
            response = {
                "status": "ok",
                "endpoint": "control",
                "message": "Control command received",
                "command_received": body_data,
                "timestamp": datetime.now().isoformat()
            }
        else:
            response = {
                "status": "ok",
                "message": f"POST request received for {parsed.path}",
                "body_received": body_data,
                "timestamp": datetime.now().isoformat()
            }

        self._send_response(200, response)

    def do_PUT(self):
        """Handle PUT requests."""
        if not self._check_auth():
            self._send_unauthorized()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        self._log_request("PUT", body)
        self._send_response(200, {
            "status": "ok",
            "message": "PUT request received",
            "timestamp": datetime.now().isoformat()
        })

    def do_DELETE(self):
        """Handle DELETE requests."""
        if not self._check_auth():
            self._send_unauthorized()
            return

        self._log_request("DELETE")
        self._send_response(200, {
            "status": "ok",
            "message": "DELETE request received",
            "timestamp": datetime.now().isoformat()
        })

    def do_PATCH(self):
        """Handle PATCH requests."""
        if not self._check_auth():
            self._send_unauthorized()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        self._log_request("PATCH", body)
        self._send_response(200, {
            "status": "ok",
            "message": "PATCH request received",
            "timestamp": datetime.now().isoformat()
        })

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.debug("%s - %s", self.client_address[0], format % args)


def run_server(host: str, port: int):
    """
    Run the HTTP server.

    :param host: Server host
    :param port: Server port
    """
    server_address = (host, port)
    httpd = HTTPServer(server_address, AEMRequestHandler)

    logger.info("=" * 70)
    logger.info("AEM SERVER SIMULATOR")
    logger.info("=" * 70)
    logger.info("Server starting on http://%s:%d", host, port)
    logger.info("")
    logger.info("Available endpoints:")
    logger.info("  GET  /sensors  - Simulated sensor data")
    logger.info("  POST /control  - Control commands")
    logger.info("  GET  /health   - Health check")
    logger.info("  *    /*        - Any other path (logs request)")
    logger.info("")
    if _AUTH_CONFIG["user"]:
        logger.info("Authentication: ENABLED (user: %s)", _AUTH_CONFIG["user"])
    else:
        logger.info("Authentication: DISABLED")
    logger.info("=" * 70)
    logger.info("Waiting for requests...")
    logger.info("")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("\nShutting down server...")
        httpd.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="AEM Server Simulator - Logs all incoming HTTP requests"
    )
    parser.add_argument(
        "--host",
        default=SERVER_HOST,
        help=f"Server host (default: {SERVER_HOST})"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=SERVER_PORT,
        help=f"Server port (default: {SERVER_PORT})"
    )
    parser.add_argument(
        "--user", "-u",
        default=_AUTH_CONFIG["user"],
        help="Basic auth username (optional)"
    )
    parser.add_argument(
        "--password",
        default=_AUTH_CONFIG["password"],
        help="Basic auth password (optional)"
    )

    args = parser.parse_args()

    # Update auth config with command line args
    _AUTH_CONFIG["user"] = args.user
    _AUTH_CONFIG["password"] = args.password

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
