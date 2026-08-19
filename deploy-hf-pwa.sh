#!/usr/bin/env bash
# Deploy the Humanpath PWA to a Hugging Face Docker Space.
#
# Mirrors deploy-hf.sh (clean single-commit snapshot, oversized dev binaries
# stripped) but swaps in pwa/Dockerfile as the root Dockerfile, since HF only
# builds the root one. One-time setup:
#
#   1. Create a new Space (SDK: Docker) at huggingface.co/new-space,
#      e.g. <user>/humanpath-pwa
#   2. git remote add hf-pwa https://huggingface.co/spaces/<user>/humanpath-pwa
#   3. Make sure boston_walk_enriched.csr.pkl is uploaded to the data-v1
#      GitHub Release (the Space downloads it on first boot):
#        gh release upload data-v1 data/osm/boston_walk_enriched.csr.pkl
#
# Then:  ./deploy-hf-pwa.sh
set -euo pipefail

REMOTE="${HF_PWA_REMOTE:-hf-pwa}"

EXCLUDE=(
  "Research/walkability_day3_summary.docx"
  "archive/notebooks/boston_overlayed_pedestrian.png"
)

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "error: git remote '$REMOTE' not set. Create the Docker Space first, then:" >&2
  echo "  git remote add $REMOTE https://huggingface.co/spaces/<user>/humanpath-pwa" >&2
  exit 1
fi

START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi

echo "Building clean PWA deploy snapshot…"
git branch -D hf-pwa-deploy >/dev/null 2>&1 || true
git checkout --orphan hf-pwa-deploy >/dev/null 2>&1
git rm -q --cached "${EXCLUDE[@]}" 2>/dev/null || true
# HF builds the ROOT Dockerfile — swap in the PWA one.
cp pwa/Dockerfile Dockerfile
git add Dockerfile
git commit -q -m "Humanpath PWA — HF Space deploy snapshot"

echo "Pushing to $REMOTE (forces remote main)…"
git push "$REMOTE" hf-pwa-deploy:main --force

git checkout -f "$START_BRANCH" >/dev/null 2>&1
echo "Done. HF will rebuild (~2 min). The Space URL is installable as a PWA."
