FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock \
    && groupadd --gid 10001 notifier \
    && useradd --uid 10001 --gid 10001 --no-create-home notifier
COPY notifier ./notifier
USER 10001:10001
EXPOSE 9000
CMD ["python", "-m", "notifier.production", "api", "--host", "0.0.0.0"]
