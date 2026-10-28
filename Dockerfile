# Native launch.sh is recommended for host-only administration.
# Build context deliberately copies only code: never data/, .runtime/ or credentials.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRINTWATCH_DATA_DIR=/data \
    PRINTWATCH_GATEWAY_URL=http://127.0.0.1:1984
WORKDIR /app
COPY pyproject.toml ./
# Install declared runtime dependencies without requiring project packaging metadata.
RUN python -c 'import subprocess,tomllib; p=tomllib.load(open("pyproject.toml","rb")); subprocess.check_call(["python","-m","pip","install","--no-cache-dir",*p["project"]["dependencies"]])'
COPY app/ ./app/
COPY static/ ./static/
RUN python -c 'import os; os.makedirs("/data",exist_ok=True); os.chown("/data",10001,10001)'
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2)"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
