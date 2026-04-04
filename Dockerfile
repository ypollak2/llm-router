FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir claude-code-llm-router

# Bind to all interfaces so Glama / Docker can reach the SSE endpoint
ENV FASTMCP_HOST=0.0.0.0
ENV FASTMCP_PORT=8000

EXPOSE 8000

CMD ["llm-router-sse"]
