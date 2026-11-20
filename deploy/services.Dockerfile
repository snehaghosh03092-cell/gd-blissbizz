ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/blissbiz/backend
COPY backend/requirements.lock ./
RUN python3 -m pip install --no-cache-dir -r requirements.lock
COPY backend/ ./
COPY libs/blissbiz_common/ /opt/blissbiz/libs/blissbiz_common/
COPY services/ /opt/blissbiz/services/
COPY deploy/healthcheck.py /opt/blissbiz/healthcheck.py
RUN python3 -m pip install --no-deps . /opt/blissbiz/libs/blissbiz_common /opt/blissbiz/services/* \
    && python3 -m pip check \
    && python3 -c "from agent_service.guide import GUIDE, sections; assert GUIDE.is_file() and sections()" \
    && useradd --uid 10001 --create-home blissbiz \
    && mkdir -p /data/uploads && chown blissbiz:blissbiz /data/uploads
RUN chmod -R a+rX /opt/blissbiz
USER blissbiz
RUN DATABASE_URL=mysql+pymysql://build:REDACTED-EXAMPLE-CREDENTIAL@127.0.0.1/build python3 -m alembic heads
CMD ["python3", "-m", "uvicorn", "gateway_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
