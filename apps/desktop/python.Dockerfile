FROM python:3.10.8-slim
ENV PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
COPY apps/intelligence/pyproject.toml ./
COPY apps/intelligence/src ./src
RUN pip install --no-cache-dir .
COPY apps/intelligence/agent-service.example.json apps/intelligence/autonomous-bindings.example.json /desktop/
COPY apps/intelligence/tests/java_integration_peer.py /desktop/
COPY apps/desktop/configure.py apps/desktop/policy.json apps/desktop/serve.py /desktop/
ENTRYPOINT ["python", "/desktop/serve.py"]
