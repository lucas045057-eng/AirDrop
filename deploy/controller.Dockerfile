ARG AIRDROP_DOCKER_CLI_IMAGE=docker:27.5.1-cli
FROM ${AIRDROP_DOCKER_CLI_IMAGE} AS docker_cli

FROM python:3.12-slim
WORKDIR /app
COPY requirements-lock.txt .
ARG PYPI_INDEX=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PYPI_INDEX" -r requirements-lock.txt
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
RUN chmod 0755 /usr/local/bin/docker /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY . .
CMD ["python", "-m", "runner.controller_service"]
