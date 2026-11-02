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
# bytecode compiled once here: a Lambda machine (the same image) cannot write it, and would compile at every start
RUN /srv/.pixi/envs/serve/bin/python -m compileall -q -j 0 /srv/.pixi/envs/serve/lib /srv/app > /dev/null; true
# the release shown in About
COPY pyproject.toml /srv/
WORKDIR /srv
USER 1000
ENV AWS_REGION=us-east-1
EXPOSE 7860
ENTRYPOINT ["/bin/bash", "-c", "source /shell-hook.sh && exec \"$@\"", "--"]
# one thread per numerical call: requests run many at once, and a library using every core for each fights the others
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
CMD ["uvicorn", "server:app", "--app-dir", "app", "--host", "0.0.0.0", "--port", "7860"]
