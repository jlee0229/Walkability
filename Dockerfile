# Hugging Face Space (Docker SDK) image for Humanpath — serves the PWA
# (pwa/server.py: FastAPI + the CSR routing substrate + the installable
# MapLibre frontend), which is the live site for both desktop and mobile.
# The Streamlit app (app/streamlit_app.py) remains a local-dev tool.
#
# Graphs are NOT in the image — the server downloads each city's compact
# *.csr.pkl (~32–65 MB) from the data-v1 GitHub Release at startup into
# ./data. Port stays 8501 to match the Space's app_port front matter.
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# PWA deps only (fastapi/uvicorn/numpy/networkx/requests) — no streamlit/
# osmnx/geopandas, so the image is small and rebuilds are fast.
COPY pwa/requirements.txt ./pwa/requirements.txt
RUN pip3 install --no-cache-dir -r pwa/requirements.txt

# server.py imports the walkability package from the repo root via sys.path.
COPY . .

EXPOSE 8501
ENV HOME=/app

HEALTHCHECK CMD curl --fail http://localhost:8501/healthz

CMD ["uvicorn", "pwa.server:app", "--host", "0.0.0.0", "--port", "8501"]
