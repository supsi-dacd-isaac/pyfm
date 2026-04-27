FROM python:3.11-slim

LABEL maintainer="SUPSI"
LABEL description="AEM Server Simulator - Logs incoming HTTP requests"

WORKDIR /app

# Copy the server script
COPY server.py .

# No additional dependencies needed - uses only stdlib

# Environment variables with defaults
ENV AEM_SERVER_HOST=0.0.0.0
ENV AEM_SERVER_PORT=6000
ENV AEM_SERVER_USER=""
ENV AEM_SERVER_PASSWORD=""

# Expose the default port
EXPOSE 6000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:6000/health')" || exit 1

# Run the server
CMD ["python", "-u", "server.py"]

