#!/usr/bin/env bash
# Deploy the server and (optionally) the Fargate image, verifying at each step.
#
# Written because deploying by hand went wrong three times in one afternoon:
#
#   1. `git pull` refused, blocked by an untracked file, and the failure scrolled past in a
#      chain of other output. The server ran the old code for the next twenty minutes.
#   2. `git checkout -- .` silently reverted files that had been scp'd in, so the image was
#      built from code that had already been thrown away.
#   3. A rebuild produced a BYTE-IDENTICAL image. It pushed happily and shipped nothing.
#      Only the unchanged ECR digest gave it away.
#
# Every one of those is silent. So this script asserts instead of hoping: the working tree is
# clean before it pulls, HEAD matches origin after, the expected code is present in the build
# context, and the pushed digest actually CHANGED.
#
# Usage:  tools/deploy.sh [--image] [--expect <string that must appear in audio_qc.py>]
set -euo pipefail

HOST="${DQC_HOST:-ubuntu@13.205.42.228}"
KEY="${DQC_KEY:-dialogue-qc-key.pem}"
APP="~/app"
IMAGE=0
EXPECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image)  IMAGE=1; shift ;;
    --expect) EXPECT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ssh_() { ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" "$@"; }

say "local checks"
LOCAL_HEAD="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "branch $BRANCH at ${LOCAL_HEAD:0:8}"
if ! git diff --quiet -- backend deploy tools; then
  echo "ERROR: uncommitted changes under backend/ deploy/ tools/ — commit them, or the box" >&2
  echo "       will run something different from what you think you deployed." >&2
  git diff --stat -- backend deploy tools >&2
  exit 1
fi
if [ -f tests/test_engine_regression.py ]; then
  echo "running the regression suite before touching the server..."
  python tests/test_engine_regression.py >/dev/null || {
    echo "ERROR: regression tests fail — refusing to deploy." >&2; exit 1; }
  echo "  regression suite OK"
fi

say "pull on the server"
# Untracked files that shadow tracked ones are the thing that blocks a pull. Report them
# rather than blowing them away: one of them may be work someone has not committed yet.
ssh_ "cd $APP && git fetch -q origin && \
  BLOCKERS=\$(git status --porcelain | grep '^??' | awk '{print \$2}' | \
             while read f; do git ls-tree -r --name-only origin/$BRANCH | grep -qx \"\$f\" && echo \"\$f\"; done); \
  if [ -n \"\$BLOCKERS\" ]; then echo 'ERROR: untracked files shadow tracked ones:'; echo \"\$BLOCKERS\"; exit 1; fi; \
  git checkout -q $BRANCH 2>/dev/null || git checkout -q -b $BRANCH origin/$BRANCH; \
  git reset -q --hard origin/$BRANCH"

REMOTE_HEAD="$(ssh_ "cd $APP && git rev-parse HEAD")"
if [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
  echo "ERROR: server is at ${REMOTE_HEAD:0:8}, expected ${LOCAL_HEAD:0:8} — did you push?" >&2
  exit 1
fi
echo "server HEAD matches local (${REMOTE_HEAD:0:8})"

if [ -n "$EXPECT" ]; then
  ssh_ "cd $APP && grep -q -- '$EXPECT' backend/audio_qc.py" \
    || { echo "ERROR: '$EXPECT' is not in the deployed audio_qc.py" >&2; exit 1; }
  echo "expected code present: '$EXPECT'"
fi

say "restart the API"
ssh_ "sudo systemctl restart dialogue-qc && sleep 3 && systemctl is-active dialogue-qc"
ssh_ "curl -sf localhost:8765/api/healthz >/dev/null && echo '  healthz OK'" \
  || echo "  WARNING: healthz did not answer (it may be behind Caddy only)"

if [ "$IMAGE" = "1" ]; then
  say "rebuild the Fargate image"
  BEFORE="$(ssh_ "sudo docker images --no-trunc --format '{{.ID}}' \
            848005667477.dkr.ecr.ap-south-1.amazonaws.com/dialogue-qc-sonar:latest | head -1" || true)"
  ssh_ "cd $APP && set -a && . ./.env 2>/dev/null; set +a; \
    cp backend/audio_qc.py backend/audio_report.py backend/audio_jobs.py \
       backend/series_registry.json ~/sonar_build/backend/ && \
    cp deploy/sonar/audio_qc_run.py ~/sonar_build/audio_qc_run.py && \
    IMG=848005667477.dkr.ecr.ap-south-1.amazonaws.com/dialogue-qc-sonar:latest && \
    .venv/bin/python -c \"
import boto3, base64
t = boto3.client('ecr', region_name='ap-south-1').get_authorization_token()['authorizationData'][0]['authorizationToken']
print(base64.b64decode(t).decode().split(':', 1)[1])\" | \
      sudo docker login --username AWS --password-stdin \
        848005667477.dkr.ecr.ap-south-1.amazonaws.com >/dev/null 2>&1 && \
    sudo docker build -q -t \"\$IMG\" ~/sonar_build >/dev/null && \
    sudo docker push \"\$IMG\" | tail -1"
  AFTER="$(ssh_ "sudo docker images --no-trunc --format '{{.ID}}' \
           848005667477.dkr.ecr.ap-south-1.amazonaws.com/dialogue-qc-sonar:latest | head -1" || true)"
  if [ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ]; then
    echo "ERROR: the image is byte-identical to the previous one." >&2
    echo "       Nothing was shipped. The build context did not contain your change." >&2
    exit 1
  fi
  echo "image changed: ${BEFORE:7:12} -> ${AFTER:7:12}"
fi

say "deployed ${LOCAL_HEAD:0:8} ($BRANCH)"
