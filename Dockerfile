# Hugging Face Space (Docker SDK) image for the Humanpath Streamlit app.
# The full Boston walk graph (~2.2 GB resident) needs more RAM than Streamlit
# Community Cloud's 1 GB cap allows; HF free CPU-basic gives 16 GB, so it fits.
# The graph itself is NOT in the image — app/streamlit_app.py downloads it from a
# GitHub Release on first run into ./data (see get_graph).
FROM python:3.13-slim

WORKDIR /app

# build-essential/git for any source builds; curl for the healthcheck. geopandas/
# shapely/pyproj/pyogrio ship manylinux wheels with GDAL/GEOS/PROJ bundled, so no
# system GDAL is required.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# The app imports the `walkability` package from the repo root (it adds the root
# to sys.path itself), so copy the whole repo rather than a single dir.
COPY . .

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health

ENTRYPOINT ["streamlit", "run", "app/streamlit_app.py", \
            "--server.port=8501", "--server.address=0.0.0.0"]
