# Hugging Face Space (Docker SDK) image for the Humanpath PWA.
# Deployed via deploy-hf-pwa.sh, which snapshots the repo with THIS file copied
# to the root as `Dockerfile` (HF only reads the root one). The graph is NOT in
# the image — pwa/server.py downloads boston_walk_enriched.csr.pkl (~32 MB) from
# the data-v1 GitHub Release on first start into ./data.
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pwa/requirements.txt ./pwa/requirements.txt
RUN pip3 install --no-cache-dir -r pwa/requirements.txt

# server.py imports the walkability package from the repo root via sys.path.
COPY . .

# HF Spaces routes traffic to 7860. /data must be writable for the graph download.
EXPOSE 7860
ENV HOME=/app

HEALTHCHECK CMD curl --fail http://localhost:7860/healthz

CMD ["uvicorn", "pwa.server:app", "--host", "0.0.0.0", "--port", "7860"]
