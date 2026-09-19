#!/usr/bin/env bash
# Set up YuE2 on a GPU.ai instance launched with the certified PyTorch environment.
#
# The certified:pytorch environment already ships torch, CUDA, cuDNN, Triton,
# and NumPy, so this script only installs the small pure-Python dependencies and
# the local package. That skips the ~2.5 GB torch/nvidia download that dominates
# a plain `pip install .`.
#
# Launch with (from your local machine):
#
#   gpu instances create \
#     --type rtx_a6000 \
#     --environment certified:pytorch@2.11 \
#     --count 1 --tier on_demand \
#     --ssh-key-id 3cba2d0e-f9cd-480b-9cec-705fb8f587c1 \
#     --name yue2-render
#
# Then copy the repo over and run this script on the instance:
#
#   rsync -az -e "ssh -p <port>" --exclude .venv --exclude .hf-cache \
#     ./ root@frp.gpu.ai:/root/YuE2/
#   gpu instances ssh <id> -- 'cd /root/YuE2 && bash tools/gpu_bootstrap.sh'
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: $PYTHON_BIN not found; run this on the GPU instance." >&2
  exit 1
fi

# The certified:pytorch environment provides torch system-wide. Inheriting it
# with --system-site-packages means we never re-download it.
if ! "$PYTHON_BIN" -c "import torch" >/dev/null 2>&1; then
  echo "error: system Python has no torch." >&2
  echo "Launch the instance with --environment certified:pytorch@2.11." >&2
  exit 1
fi

"$PYTHON_BIN" -c "import torch; print('using torch', torch.__version__, 'cuda', torch.cuda.is_available())"

rm -rf .venv
"$PYTHON_BIN" -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip

# Same pins as pyproject.toml, minus torch (inherited from the environment) and
# minus the fast/test extras. Keep this list in sync with [project.dependencies].
.venv/bin/pip install \
  "transformers==4.57.6" \
  "huggingface-hub==0.36.2" \
  "safetensors==0.7.0" \
  "tiktoken==0.12.0" \
  "numpy==2.2.6" \
  "soundfile==0.13.1" \
  "accelerate==1.13.0"

# Install the package itself without letting pip pull the pinned torch==2.10.0
# (the environment's torch is newer and already loaded above).
.venv/bin/pip install --no-deps -e .

export HF_HOME="$ROOT/.hf-cache"
.venv/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('torch', torch.__version__, 'cuda ok')"

cat <<EOF

Setup complete.

Next:
  export HF_HOME="$ROOT/.hf-cache"
  .venv/bin/yue2 generate --device cuda --request examples/song.json --output runs/first-song

Model weights (~7.3 GB) download to \$HF_HOME on first use.
EOF