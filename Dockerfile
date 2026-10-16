FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY trialviz ./trialviz
COPY demo ./demo
COPY examples/queries.json ./examples/queries.json
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "trialviz.app:app", "--host", "0.0.0.0", "--port", "8000"]
