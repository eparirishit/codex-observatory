FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends zstd && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY observatory.py /app/
COPY static /app/static
USER 1000:1000
EXPOSE 8765
ENTRYPOINT ["python", "observatory.py"]
CMD ["--container", "--codex-home", "/data/codex", "--link-file", "/tmp/observatory-link"]
