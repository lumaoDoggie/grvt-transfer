FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

# Default state dir for containers (mount /app/data if you want persistence)
ENV GRVT_STATE_DIR=/app/data/bot

CMD ["grvt-transfer", "run"]

