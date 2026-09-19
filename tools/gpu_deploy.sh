#!/usr/bin/env bash
# One-command YuE2 setup on an existing GPU.ai instance.
#
# Clones this fork on the instance (or fast-forwards it) and runs
# tools/gpu_bootstrap.sh, which reuses the certified PyTorch environment's
# preinstalled torch/CUDA instead of re-downloading it.
#
#   tools/gpu_deploy.sh <instance-id>
#   tools/gpu_deploy.sh <instance-id> --generate
#   tools/gpu_deploy.sh <instance-id> --repo https://github.com/you/YuE.git --dir /root/YuE2
#
# The instance must have been launched with --environment certified:pytorch@2.11.
# Creation is deliberately not automated here because it is a billed command:
#
#   gpu instances create --type rtx_a6000 \
#     --environment certified:pytorch@2.11 \
#     --count 1 --tier on_demand \
#     --ssh-key-id <key-id> --name yue2-render
#
set -euo pipefail

INSTANCE="${1:-}"
if [ -z "$INSTANCE" ]; then
  echo "usage: $0 <instance-id> [--generate] [--repo <git-url>] [--dir <path>]" >&2
  exit 2
fi
shift

REPO_URL="https://github.com/josephgardner/YuE.git"
REMOTE_DIR="/root/YuE2"
BRANCH="main"
GENERATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --generate) GENERATE=1; shift ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --dir) REMOTE_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "==> syncing $REPO_URL ($BRANCH) to $INSTANCE:$REMOTE_DIR"
gpu instances ssh "$INSTANCE" -- "
  set -e
  if [ -d '$REMOTE_DIR/.git' ]; then
    git -C '$REMOTE_DIR' fetch --depth 1 origin '$BRANCH'
    git -C '$REMOTE_DIR' checkout -q '$BRANCH'
    git -C '$REMOTE_DIR' reset --hard 'origin/$BRANCH'
  else
    git clone --depth 1 --branch '$BRANCH' '$REPO_URL' '$REMOTE_DIR'
  fi
"

echo "==> bootstrapping"
gpu instances ssh "$INSTANCE" -- "bash '$REMOTE_DIR/tools/gpu_bootstrap.sh'"

if [ "$GENERATE" = 1 ]; then
  echo "==> generating examples/song.json"
  gpu instances ssh "$INSTANCE" -- "
    cd '$REMOTE_DIR' &&
    HF_HOME='$REMOTE_DIR/.hf-cache' .venv/bin/yue2 generate \
      --device cuda --request examples/song.json --output runs/first-song
  "
fi

cat <<EOF

Done. Pull generated artifacts with:
  gpu instances ssh $INSTANCE -- 'tar -C $REMOTE_DIR -cf - runs' | tar -xf -
EOF