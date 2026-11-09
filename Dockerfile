FROM debian:bookworm-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-cryptography curl ca-certificates tzdata openssl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sysman \
    && mkdir -p /data /certs /auth-key && chown sysman:sysman /data /certs /auth-key
WORKDIR /app
COPY server.py agent_runner.py remote_agent.py scheduling.py outhman_auth.py auth_admin.py ./

FROM runtime AS agent
COPY sample_agent/runner.py sample_agent/main.py ./sample_agent/
ENV SYSMAN_AGENT_DIR=/data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 9101
ENTRYPOINT ["python3", "sample_agent/runner.py"]
CMD ["--host", "0.0.0.0", "--port", "9101", "--cert", "/certs/server.crt", "--tls-key", "/certs/server.key"]

FROM runtime AS sysman
COPY static ./static
COPY README.md AGENT_INTEGRATION.md REMOTE_AGENTS.md deployment.md AUTHENTICATION.md ./
ENV SYSMAN_DATA_DIR=/data SYSMAN_AUTH_KEY_FILE=/auth-key/outhman.key SYSMAN_BIND=0.0.0.0 PORT=8765
USER 10001:10001
VOLUME ["/data", "/auth-key"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl --fail --silent http://127.0.0.1:8765/api/health || exit 1
ENTRYPOINT ["python3", "server.py"]
