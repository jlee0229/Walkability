#!/usr/bin/env bash
# Redeploy the current `main` to the Hugging Face Space (jlee0229/Humanpath).
#
# The HF Space is a Docker Space pushed via a clean, single-commit snapshot of
# `main` that EXCLUDES two oversized dev binaries HF's pre-receive hook rejects
# (see the chosen "snapshot to HF only" approach — GitHub `origin` is untouched).
# Run this from the repo root after committing your changes to `main`.
#
#   ./deploy-hf.sh
#
# HF will rebuild the Docker image (~2-4 min) and redeploy. For fast iteration,
# develop locally instead:  streamlit run app/streamlit_app.py
set -euo pipefail

# Files HF blocks (binary, >~100 KB, not in Xet/LFS). Kept on `main`/GitHub,
# stripped from the deploy snapshot only.
EXCLUDE=(
  "Research/walkability_day3_summary.docx"
  "notebooks/boston_overlayed_pedestrian.png"
)

START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$START_BRANCH" != "main" ]]; then
  echo "warning: not on main (on '$START_BRANCH'); snapshotting that branch." >&2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi

echo "Building clean deploy snapshot…"
git branch -D hf-deploy >/dev/null 2>&1 || true
git checkout --orphan hf-deploy >/dev/null 2>&1
git rm -q --cached "${EXCLUDE[@]}" 2>/dev/null || true
git commit -q -m "Humanpath — HF Space deploy snapshot"

echo "Pushing to HF (forces remote main)…"
git push hf hf-deploy:main --force

git checkout -f "$START_BRANCH" >/dev/null 2>&1
echo "Done. HF is rebuilding — watch the Space's Logs tab."
