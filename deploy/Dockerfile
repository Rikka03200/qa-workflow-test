FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY webapp/requirements-web.txt /app/webapp/requirements-web.txt
RUN pip install --no-cache-dir -r /app/webapp/requirements-web.txt

COPY . /app

EXPOSE 8800

CMD ["python", "-m", "webapp.main"]
