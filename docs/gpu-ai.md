# Running YuE2 on GPU.ai

GPU.ai instances are ephemeral — there is no persistent volume or snapshot and
`gpu instances create` cannot boot a custom image (only `certified:<framework>`,
`raw-vm:<os>`, or a curated template). So a fresh instance always starts with an
empty disk, and Docker does not help: a pull would re-download the same layers
over the same network.

The way to avoid reinstalling on every launch is to use the **certified PyTorch
environment**, which already ships torch, CUDA, cuDNN, Triton, and NumPy. The
bootstrap script then installs only the small pure-Python dependencies and the
package itself, skipping the ~2.5 GB torch/nvidia download that otherwise
dominates setup.

An RTX A6000 (48 GB) is enough for the 24 GB default; a song takes about 35 s.

## Launch the instance

Creating an instance is billed per hour. Pin the type so you know the rate.

```bash
gpu instances create \
  --type rtx_a6000 \
  --environment certified:pytorch@2.11 \
  --count 1 --tier on_demand \
  --ssh-key-id <key-id> \
  --name yue2-render
```

## Set up and run

With an instance id from `gpu instances list`:

```bash
tools/gpu_deploy.sh <instance-id>              # clone + bootstrap
tools/gpu_deploy.sh <instance-id> --generate   # also render examples/song.json
```

The script clones this fork on the instance, runs `tools/gpu_bootstrap.sh`, and
optionally kickstarts a generation. Run `tools/gpu_bootstrap.sh` directly if the
repo is already on the instance.

## Pull artifacts

```bash
gpu instances ssh <instance-id> -- 'tar -C /root/YuE2 -cf - runs' | tar -xf -
```

## Stop paying

```bash
gpu instances delete <instance-id>
gpu instances list -o json          # verify nothing is still running
```

## Notes

- The certified PyTorch environment ships torch 2.11, while `pyproject.toml`
  pins `torch==2.10.0`. `gpu_bootstrap.sh` installs the package with
  `--no-deps` so the environment torch is used as-is; keep the plain
  `pip install .` path when exact 2.10 reproducibility matters.
- Model weights (~7.3 GB) download to `$HF_HOME` (`.hf-cache/`) on first
  generation. They do not survive instance deletion.