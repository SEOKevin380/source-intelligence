FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SOURCE_INTELLIGENCE_DATA_DIR=/data/source-intelligence \
    NEWSWIRE_WORKBENCH_HOME=/data/source-intelligence/newswire-workbench

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

RUN mkdir -p /data/source-intelligence/newswire-workbench

CMD ["sh", "-c", "streamlit run app.py --server.headless=true --server.address=0.0.0.0 --server.port=${PORT:-8080} --browser.gatherUsageStats=false"]
