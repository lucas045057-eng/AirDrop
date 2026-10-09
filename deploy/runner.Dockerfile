FROM python:3.12-slim
WORKDIR /opt/runner
ARG PYPI_INDEX=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PYPI_INDEX" 'pydantic==2.14.0' 'openpyxl==3.1.5' && useradd --uid 10001 --create-home runner
RUN mkdir /bus && chown runner:runner /bus
COPY runner/ runner/
COPY adapters/ adapters/
COPY packages/ packages/
USER runner
ENV AIRDROP_ISOLATED_RUNNER=1 AIRDROP_RUNNER_DIR=/bus PYTHONDONTWRITEBYTECODE=1
CMD ["python","-m","runner.service"]
