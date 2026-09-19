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

## Seed sweeps (batch)

To test variation, render N seeds per request. `tools/expand_seeds.py` turns each
request into N rows with a unique `<id>_seed<seed>` and records the seed, and
`tools/gpu_batch.py` drives the whole cycle — stage, upload, batch, download,
terminate:

```bash
# create a fresh instance, render 4 seeds, pull results, tear down
tools/gpu_batch.py --request examples/song.json --seeds 4 --create

# reuse a running instance (kept alive unless --terminate)
tools/gpu_batch.py --request song-packs/my-song --seeds 4 --instance <instance-id>

# inspect the plan without spending anything
tools/gpu_batch.py --request examples/song.json --seeds 4 --create --dry-run
```

`--request` accepts a file or a directory (repeatable); a directory's request
JSONs are all expanded, and any `abc_path` files are copied into a self-contained
pack. Results land in `runs/<slug>/<id>_seed<seed>/`, each with the seed in
`request.json` and `result.json`. `--keep` leaves the instance running;
`--terminate` tears down one that was passed with `--instance`.

The lower-level pieces are reusable on their own:

```bash
tools/expand_seeds.py song-packs --seeds 4 --seed-start 831001 --stage /tmp/pack
gpu instances ssh <id> -- 'cd /root/YuE2 && HF_HOME=$PWD/.hf-cache \
  .venv/bin/yue2 batch --input packs/<slug>/batch.jsonl --output runs/batch'
```

## Stop paying

```bash
gpu instances delete <instance-id>
gpu instances list -o json          # verify nothing is still running
```

## Guardrails

GPUs bill until terminated, and a crashed script does not stop the meter.
`gpu_batch.py` prints the current spending limit up front and re-lists
instances at the end (and on failure), reporting anything still running, but
the durable backstop lives on the server:

```bash
gpu spend-limit --monthly 50 --daily 10    # requires org-admin
gpu spend-limit -o json                    # show limit and month/day spend
```

The daily cap is what saves you from an orphaned instance during an unattended
run; the monthly limit is the outer bound. `--auto-terminate-hours` on
`gpu instances create` would be the ideal per-run backstop, but it is not in
the current CLI release (see Notes).

## Notes

- The certified PyTorch environment ships torch 2.11, while `pyproject.toml`
  pins `torch==2.10.0`. `gpu_bootstrap.sh` installs the package with
  `--no-deps` so the environment torch is used as-is; keep the plain
  `pip install .` path when exact 2.10 reproducibility matters.
- Model weights (~7.3 GB) download to `$HF_HOME` (`.hf-cache/`) on first
  generation. They do not survive instance deletion.
- GPU.ai's docs describe `--image` (bring your own container), `--env`,
  `--port`, and `--auto-terminate-hours` on `gpu instances create`, but the
  current CLI release (v1.3, also the Homebrew stable) does not implement them.
  Until that ships, the certified PyTorch environment is the reproducible path,
  and spend limits are the only server-side backstop.