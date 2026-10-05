FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=17311 \
    PYTHONUNBUFFERED=1

EXPOSE 17311
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',17311),2); s.close()"

USER 65532:65532
CMD ["python", "/app/server.py"]
