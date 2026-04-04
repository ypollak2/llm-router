FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir claude-code-llm-router

CMD ["llm-router"]
