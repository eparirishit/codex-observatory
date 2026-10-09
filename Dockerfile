FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY observatory.py /app/
COPY static /app/static
USER 1000:1000
EXPOSE 8765
ENTRYPOINT ["python", "observatory.py"]
CMD ["--container", "--codex-home", "/data/codex", "--link-file", "/tmp/observatory-link"]
