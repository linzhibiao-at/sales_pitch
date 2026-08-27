FROM artifactory.anta.com/docker-base-image/python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && pip uninstall -y pytest

COPY backend/ backend/
COPY config.yaml .
COPY config/ config/
COPY prompt/ prompt/

EXPOSE 8080

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
