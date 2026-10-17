FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --chown=0:0 shared/__init__.py shared/path_policy.py /opt/app/shared/
COPY --chown=0:0 sandbox/tool.py /opt/app/tool.py
RUN chmod -R a-w /opt/app && mkdir -p /workspace && chown 1000:1000 /workspace
USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "85"]
