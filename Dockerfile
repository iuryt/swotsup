# The explorer's server, with the pixi environment `serve` from the lockfile: the same versions as the tests.
# Build context: the repo root (pyproject.toml, pixi.lock, app/; slurm/ec2.sh packs just these).
# The store is read with the AWS credentials of the environment (an instance role, or AWS_* as secrets).
FROM ghcr.io/prefix-dev/pixi:0.78.0 AS build
WORKDIR /srv
COPY pyproject.toml pixi.lock ./
# --frozen: the lockfile as is (the dev environment's path dependency is not in the image)
RUN pixi install --frozen -e serve && pixi shell-hook --frozen -e serve -s bash > /shell-hook.sh

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /srv/.pixi/envs/serve /srv/.pixi/envs/serve
COPY --from=build /shell-hook.sh /shell-hook.sh
COPY app /srv/app
# the release shown in About
COPY pyproject.toml /srv/
WORKDIR /srv
USER 1000
ENV AWS_REGION=us-east-1
EXPOSE 7860
ENTRYPOINT ["/bin/bash", "-c", "source /shell-hook.sh && exec \"$@\"", "--"]
CMD ["uvicorn", "server:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "7860"]
