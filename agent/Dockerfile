FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        iproute2 \
        iptables \
        procps \
    && rm -rf /var/lib/apt/lists/*

COPY awg_agent ./awg_agent

RUN pip install --upgrade pip \
    && pip install aiohttp

EXPOSE 9101

CMD ["python", "-m", "awg_agent"]
