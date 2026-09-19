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

GPUs bill until terminated, and a crashed script does not stop the meter. Three
independent layers cover this:

1. **Server-side auto-terminate.** `gpu_batch.py` creates instances through the
   REST API (`POST /v1/instances`) with `auto_terminate_hours` (default 2; set
   `--auto-terminate-hours`). The countdown starts when the instance reaches
   `running`, and the platform terminates it even if your Mac or this process
   disappears. Creation uses REST rather than `gpu instances create` precisely
   because the released CLI (v1.3) does not expose the flag.
2. **Script teardown.** Instances created by the script are deleted at the end
   unless `--keep` is passed; on failure the script prints the exact `delete`
   command and leaves the instance up for debugging.
3. **Spending limits.** A durable outer bound that applies to everything:

```bash
gpu spend-limit --monthly 50 --daily 10    # requires org-admin
gpu spend-limit -o json                    # show limit and month/day spend
```

`gpu_batch.py` prints the spending-limit state up front and re-lists instances
at the end (and on failure), reporting anything still running.

## Custom images (optional)

The REST API accepts `image` as an alternative to `environment` — GPU.ai
verifies and digest-pins it before provisioning. Pass it with `--image`:

```bash
tools/gpu_batch.py --request examples/song.json --seeds 4 --create \
  --image ghcr.io/you/yue2-renderer:0.1
```

With `--image` the script assumes YuE2 is already installed at `--remote-dir`
(default `/root/YuE2`) and skips clone/bootstrap. Images must be publicly
pullable, `linux/amd64`, and on-demand only. GPU.ai publishes no image-size
limit or pull-time guarantee, so start with a **thin image** (CUDA + Python +
YuE2 + deps, no weights) and let the ~7.3 GB of weights download per instance;
only bake weights in if measured cold starts show host-side layer reuse.

## Handing a run to another agent

When another agent is going to render a prepared song, give it a brief like the
one below. The essentials are: the exact tool, the no-spend dry run, the
requirement to show the price and wait for an explicit yes before creating, and
the truncation check.

```
You're running a YuE2 seed sweep on a GPU.ai instance. Work in the YuE2 repo.

Goal: render <N> seeds of the prepared song <request.json or pack directory>
and download the audio.

Tools (already in the repo):
- tools/gpu_batch.py    stages the pack, creates the instance, deploys, runs
                        `yue2 batch`, downloads results, terminates.
- tools/expand_seeds.py expands a request into one row per seed, with a unique
                        id `<id>_seed<seed>` and the seed recorded.
- docs/gpu-ai.md        the full workflow.

Prereqs: the `gpu` CLI is installed and authenticated (GPUAI_API_KEY is in the
repo .env; gpu_batch.py reads it) and an SSH key is registered.

Steps:
1. Validate staging with no spend:
     tools/gpu_batch.py --request <path> --seeds <N> --create --dry-run
2. Money guardrail - do not skip: show the exact POST /instances JSON body and
   the hourly price the script printed, then WAIT for an explicit "yes". A
   blanket "go ahead" is not confirmation.
3. Run it:
     tools/gpu_batch.py --request <path> --seeds <N> --create
   It creates via REST with a 2-hour server-side auto-terminate, deletes the
   instance at the end, and reports any stray instances.
4. Report back: instance id, seeds rendered, the local results directory, and
   whether any result.json has "truncated": true.

Do not: commit, push, edit pyproject.toml, pass --keep, or auto-retry a failed
create. If the create fails, stop and report the error.

Results land in runs/<slug>/<id>_seed<seed>/ (audio.flac, score.abc,
request.json, result.json). Each result.json holds the seed and timing.

If REST create fails, create manually and reuse the instance:
  gpu instances create --type rtx_a6000 --environment certified:pytorch@2.11 \
    --count 1 --tier on_demand --ssh-key-id <key-id> --name yue2-render
  tools/gpu_batch.py --request <path> --seeds <N> --instance <id> --terminate
```

Two values to pin down before sending: the seed count (and whether the default
`--seed-start 831001` is fine), and whether the input is a single request JSON
or a directory. Prefer a directory when the request has an `abc_path` —
`expand_seeds.py` copies the ABC into the staged pack automatically.

Long songs need a high enough `semantic_sampling.max_tokens` in the request
(e.g. 14000). If `result.json` shows `truncated.semantic: true`, the take was
cut short; raise it and re-render rather than accepting it.

## Notes

- The certified PyTorch environment ships torch 2.11, while `pyproject.toml`
  pins `torch==2.10.0`. `gpu_bootstrap.sh` installs the package with
  `--no-deps` so the environment torch is used as-is; keep the plain
  `pip install .` path when exact 2.10 reproducibility matters.
- Model weights (~7.3 GB) download to `$HF_HOME` (`.hf-cache/`) on first
  generation. They do not survive instance deletion.
- GPU.ai's docs describe `--image` and `--auto-terminate-hours` on `gpu
  instances create`, but the released CLI (v1.3, also the Homebrew stable) does
  not implement them. The REST API does, so `gpu_batch.py` uses REST for
  creation and the CLI for SSH, pricing, and teardown.