# AEM Server Simulator

A simple REST server that accepts GET/POST requests and prints out the request details including headers, query parameters, and body.

This is useful for testing and debugging the control commands sent by the flexibility manager to the AEM API.

## Quick Start with Docker Compose

```bash
cd docker/aem-server-simulator

# Build and run
docker-compose up --build

# Run in detached mode (background)
docker-compose up -d --build

# View logs
docker-compose logs -f

# View log file
tail -f logs/aem-server-simulator.log

# Stop the service
docker-compose down
```

The service is configured with:
- Port: `6000`
- User: `supsi`
- Password: `supsi1234`
- Network: `pyfm_network` (shared with forwarder)

## Running with Forwarder

Both services use the shared `pyfm_network` Docker network. Start them in any order:

```bash
# Terminal 1: Start aem-server-simulator
cd docker/aem-server-simulator
docker-compose up -d --build

# Terminal 2: Start forwarder
cd docker/forwarder
docker-compose up -d --build
```

The forwarder will connect to aem-server-simulator using the container hostname `aem-server-simulator:6000`.

## Alternative: Run Locally (without Docker)

```bash
cd docker/aem-server-simulator
python server.py --port 6000 --user supsi --password supsi1234
```

## Configuration

### Command Line Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `0.0.0.0` | Server host to bind to |
| `--port` | `6000` | Server port |
| `--user` | (none) | Basic auth username (optional) |
| `--password` | (none) | Basic auth password (optional) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AEM_SERVER_HOST` | `0.0.0.0` | Server host |
| `AEM_SERVER_PORT` | `6000` | Server port |
| `AEM_SERVER_USER` | (empty) | Basic auth username |
| `AEM_SERVER_PASSWORD` | (empty) | Basic auth password |
| `AEM_SERVER_LOG_DIR` | `logs` | Directory for log files |

## Logging

All incoming requests are logged to both:
- **Console**: Standard output (visible in Docker logs)
- **File**: `logs/aem-server-simulator.log`

When using Docker, the logs directory is mounted as a volume, so logs persist on the host at:
- `docker/aem-server-simulator/logs/aem-server-simulator.log`

To view the log file:
```bash
tail -f docker/aem-server-simulator/logs/aem-server-simulator.log
```

## Available Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sensors` | Returns simulated sensor data |
| POST | `/control` | Accepts control commands |
| GET | `/health` | Health check endpoint |
| * | `/*` | Any other path - logs the request |

## Example Usage

### Test with curl

```bash
# GET sensor data (with authentication)
curl -u supsi:supsi1234 http://localhost:6000/sensors

# POST control command (with authentication)
curl -u supsi:supsi1234 -X POST http://localhost:6000/control \
  -H "Content-Type: application/json" \
  -d '{"command": "curtail", "asset_id": "HP1", "power_kw": 5.0}'

# Health check (no auth required)
curl http://localhost:6000/health
```

### Expected Server Output

```
======================================================================
INCOMING REQUEST
======================================================================
Timestamp:   2026-02-03T10:30:00.123456
Method:      POST
Path:        /control
Full URL:    /control
Client:      172.17.0.1:54321
----------------------------------------
Headers:
  Host: localhost:6000
  Content-Type: application/json
  Content-Length: 58
  Authorization: [REDACTED]
----------------------------------------
Body (58 bytes):
{
  "command": "curtail",
  "asset_id": "HP1",
  "power_kw": 5.0
}
======================================================================
```

## Integration with flexi_manager.py

Update the `aemAPI` section in `conf/private/conns.json`:

```json
"aemAPI": {
  "sensorsUrl": "http://localhost:6000/sensors",
  "controlUrl": "http://localhost:6000/control",
  "port": 6000,
  "user": "supsi",
  "password": "supsi1234",
  "requestTimeout": 5,
  "waitAfterRequest": 0.5
}
```

Note: Changed `https://localhost` to `http://localhost` for local testing.

