FROM python:3.14-slim

# Run as a non-root user. The process only makes outbound HTTPS calls and
# listens on one port, so it never needs to own anything in the image.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
# The dashboard markup is read at request time, so it ships as a file rather
# than a string baked into the module — editable as HTML, diffable as HTML.
COPY ui/ ./ui/
USER app

EXPOSE 8787
# The container has no shell in the healthcheck path on purpose — /health is
# the one route the bearer gate lets through, and it reads nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8787\")}/health', timeout=4).status==200 else 1)"

CMD ["python", "server.py"]
