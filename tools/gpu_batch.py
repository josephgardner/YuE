#!/usr/bin/env python3
"""Render N seeds per song request on a GPU.ai instance, then pull the results.

Workflow:
  1. expand each request into N seed rows and stage a clean pack (JSONL + any
     ABC files referenced by `abc_path`);
  2. use an existing instance (--instance) or create one (--create, with a
     price confirmation);
  3. clone/update the repo and bootstrap it with tools/gpu_deploy.sh;
  4. upload the pack and run `yue2 batch` on the instance;
  5. download runs/batch locally;
  6. terminate the instance if it was created here (or with --terminate).

Examples:
  tools/gpu_batch.py --request examples/song.json --seeds 4 --create
  tools/gpu_batch.py --request song-packs/my-song --seeds 4 --instance gpu-abc123
  tools/gpu_batch.py --request examples/song.json --seeds 4 --create --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expand_seeds import build_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "https://github.com/josephgardner/YuE.git"
DEFAULT_ENVIRONMENT = "certified:pytorch@2.11"


def log(message):
    print(f"==> {message}", flush=True)


def run(cmd, *, dry_run=False, capture=False, check=True, echo=True):
    display = " ".join(str(c) for c in cmd)
    if dry_run:
        log(f"[dry-run] {display}")
        if capture:
            return ""
        return None
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if capture:
        if echo and result.stdout:
            sys.stderr.write(result.stdout)
        if check and result.returncode != 0:
            if result.stderr:
                sys.stderr.write(result.stderr)
            raise SystemExit(f"command failed ({result.returncode}): {display}")
        return result.stdout
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {display}")
    return None


def gpu_json(args):
    # Read-only lookups (pricing, keys, instances get) run even under --dry-run.
    out = run(["gpu", *args, "-o", "json"], capture=True, echo=False)
    if not out:
        return None
    return json.loads(out)


def gpu_json_soft(args):
    """Like gpu_json but returns None instead of exiting when the call fails."""
    result = subprocess.run(["gpu", *args, "-o", "json"], text=True, capture_output=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def report_spend_limit():
    data = gpu_json_soft(["spend-limit"])
    if data is None:
        log("no GPU.ai spending limit configured — set a backstop so a crashed run "
            "cannot bill indefinitely:")
        log("  gpu spend-limit --monthly 50 --daily 10")
        return None
    log(f"spend limit: {json.dumps(data)}")
    return data


def report_strays():
    instances = gpu_json_soft(["instances", "list"]) or []
    active = [i for i in instances if i.get("status") not in {"terminated", "deleting"}]
    if not active:
        log("no GPU.ai instances left running")
        return []
    log(f"{len(active)} instance(s) still not terminated (billing):")
    for i in active:
        iid = i.get("id")
        log(f"  {iid}  {i.get('name', '')}  {i.get('status')}  "
            f"${i.get('price_per_hour') or 0:.4f}/hr  (gpu instances delete {iid})")
    return active


def slugify(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "pack"


def resolve_ssh_key(explicit):
    if explicit:
        return explicit
    keys = gpu_json(["keys", "list"]) or []
    if not keys:
        raise SystemExit("no SSH keys registered; run `gpu keys create` or pass --ssh-key-id")
    preferred = [k for k in keys if k.get("attach_on_provision")] or keys
    return preferred[0]["id"]


def confirm_create(gpu_type, tier, environment, ssh_key_id, *, dry_run=False):
    prices = gpu_json(["pricing", "--gpu-type", gpu_type]) or []
    candidates = [p for p in prices if p.get("available") and p.get("tier") == tier]
    if not candidates and not dry_run:
        raise SystemExit(f"no available {gpu_type} capacity on {tier}")
    if candidates:
        cheapest = min(candidates, key=lambda p: p["price_per_hour"])
        price = cheapest["price_per_hour"]
        log(f"cheapest {gpu_type} ({tier}): ${price:.4f}/hr in {cheapest.get('region')}")
    else:
        price = 0.0

    cmd = [
        "gpu", "instances", "create",
        "--type", gpu_type,
        "--environment", environment,
        "--count", "1",
        "--tier", tier,
        "--ssh-key-id", ssh_key_id,
        "--name", "yue2-batch",
    ]
    print("will run:\n  " + " ".join(cmd) + f"\n  at ~${price:.4f}/hr")
    if dry_run:
        return "gpu-dryrun"
    reply = input("create this billed instance? type 'yes' to continue: ").strip().lower()
    if reply != "yes":
        raise SystemExit("aborted")
    out = run(["gpu", "instances", "create", *cmd[3:], "-o", "json"], capture=True)
    data = json.loads(out)
    iid = data.get("id") or data.get("resource_id") or (data.get("instance") or {}).get("id")
    if not iid:
        raise SystemExit(f"could not parse instance id from create output: {data}")
    return iid


def connection(iid, *, dry_run=False):
    if dry_run:
        return "frp.gpu.ai", 22
    data = gpu_json(["instances", "get", iid]) or {}
    conn = data.get("connection") or {}
    return conn.get("hostname"), conn.get("port")


def rsync(local, remote_dir, iid, *, dry_run=False, delete=False, pull=False):
    host, port = connection(iid, dry_run=dry_run)
    if host is None:
        if dry_run:
            host, port = "frp.gpu.ai", 22
        else:
            raise SystemExit(f"instance {iid} has no connection; is it running?")
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p {port}"
    remote = f"root@{host}:{remote_dir.rstrip('/')}/"
    source, target = (remote, local.rstrip("/") + "/") if pull else (local.rstrip("/") + "/", remote)
    if shutil.which("rsync"):
        cmd = ["rsync", "-az"]
        if delete:
            cmd.append("--delete")
        cmd += ["-e", ssh, source, target]
    else:
        cmd = ["scp", "-r", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-P", str(port),
               source.rstrip("/"), target.rstrip("/")]
    run(cmd, dry_run=dry_run)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--request", action="append", required=True, help="request JSON file or directory (repeatable)")
    ap.add_argument("--seeds", type=int, default=1, help="seeds per request")
    ap.add_argument("--seed-start", type=int, default=831001, help="first seed")
    ap.add_argument("--instance", help="existing GPU.ai instance id")
    ap.add_argument("--create", action="store_true", help="create a new instance (billed)")
    ap.add_argument("--gpu-type", default="rtx_a6000")
    ap.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    ap.add_argument("--tier", default="on_demand", choices=("on_demand", "spot"))
    ap.add_argument("--ssh-key-id")
    ap.add_argument("--keep", action="store_true", help="do not terminate the instance")
    ap.add_argument("--terminate", action="store_true", help="terminate even if --instance was supplied")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="git URL to deploy")
    ap.add_argument("--remote-dir", default="/root/YuE2")
    ap.add_argument("--download-dir", help="local directory for results")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.instance and args.create:
        raise SystemExit("pass --instance or --create, not both")
    if not args.instance and not args.create:
        raise SystemExit("pass --instance <id> or --create")

    report_spend_limit()

    stage = Path(tempfile.mkdtemp(prefix="yue2-pack-"))
    report = build_manifest(args.request, args.seeds, args.seed_start, stage=stage)
    first_id = json.loads(Path(report["output"]).read_text().splitlines()[0])["id"].rsplit("_seed", 1)[0]
    slug = slugify(f"{first_id}_{args.seeds}x{args.seed_start}")
    log(f"staged {report['rows']} rows for seeds {report['seeds'][0]}..{report['seeds'][1]} in {stage}")

    created = False
    if args.instance:
        iid = args.instance
    else:
        ssh_key = args.ssh_key_id or resolve_ssh_key(None)
        iid = confirm_create(args.gpu_type, args.tier, args.environment, ssh_key, dry_run=args.dry_run)
        created = True
    log(f"instance {iid}")

    try:
        log("deploying repo + bootstrap")
        run(["bash", str(ROOT / "tools" / "gpu_deploy.sh"), iid, "--repo", args.repo,
             "--dir", args.remote_dir], dry_run=args.dry_run)

        remote_pack = f"{args.remote_dir}/packs/{slug}"
        log(f"uploading pack to {remote_pack}")
        if not args.dry_run:
            run(["gpu", "instances", "ssh", iid, "--", f"mkdir -p '{remote_pack}'"])
        else:
            log("[dry-run] gpu instances ssh ... mkdir -p")
        rsync(str(stage), remote_pack, iid, dry_run=args.dry_run)

        log("running yue2 batch")
        run(["gpu", "instances", "ssh", iid, "--",
             f"cd '{args.remote_dir}' && HF_HOME='{args.remote_dir}/.hf-cache' "
             f".venv/bin/yue2 batch --input 'packs/{slug}/batch.jsonl' --output 'runs/batch'"],
            dry_run=args.dry_run, check=False)

        download_dir = Path(args.download_dir) if args.download_dir else (ROOT / "runs" / slug)
        if not args.dry_run:
            download_dir.mkdir(parents=True, exist_ok=True)
        log(f"downloading results to {download_dir}")
        rsync(str(download_dir), f"{args.remote_dir}/runs/batch", iid,
              dry_run=args.dry_run, pull=True)
    except BaseException:
        if created and not args.dry_run:
            log(f"FAILED — {iid} is still running and billing. Stop it with:")
            log(f"  gpu instances delete {iid}")
        report_strays()
        raise

    should_terminate = (created or args.terminate) and not args.keep
    if should_terminate:
        log(f"terminating {iid}")
        run(["gpu", "instances", "delete", iid], dry_run=args.dry_run, check=False)
    else:
        log(f"leaving {iid} running (use --terminate/--keep to change)")

    strays = report_strays()
    print(json.dumps({
        "instance": iid,
        "created": created,
        "terminated": should_terminate,
        "seeds": report["seeds"],
        "rows": report["rows"],
        "manifest": report["output"],
        "results": str(download_dir),
        "strays": [s.get("id") for s in strays],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())