# Web interface for Free Games Claimer Remaster (fork-only, see webui/README.md).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt /srv/webui/requirements.txt
RUN pip install -r /srv/webui/requirements.txt
COPY . /srv/webui

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

# --proxy-headers only trusts X-Forwarded-* from WEBUI_TRUSTED_PROXIES (default: nobody).
CMD exec uvicorn webui.app.main:create_app --factory --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "${WEBUI_TRUSTED_PROXIES:-127.0.0.1}" \
    --no-server-header --log-level info
