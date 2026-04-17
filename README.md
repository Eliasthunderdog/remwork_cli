# remwork-cli

Remote work CLI for syncing code, allocating GPU clusters, setting up containers, and running commands on remote Slurm nodes. Single-file Python tool with no external dependencies.

## Install

```bash
pip install .
```

This installs the `remwork-cli` command, creates `~/.local/remwork_cli/` with a default `remotes.json`, and adds shell completion for bash/zsh.

Or run directly without installing:

```bash
python remwork_cli.py <command> [args]
```

Requires `rsync` and `ssh` locally and on the remote. Allocation/run commands require Slurm on the remote. Container commands require enroot on the remote.

## Configuring remote nodes

Remote nodes are defined in `remotes.json`. The config is resolved in this order:

1. `--config` / `-c` flag
2. `REMWORK_CONFIG` environment variable
3. `~/.local/remwork_cli/remotes.json` (created by install)
4. `./remotes.json` in the current directory

### remotes.json

Start by copying the example:

```bash
cp remotes.json.example remotes.json
```

Each remote defines an SSH-accessible machine:

```json
{
  "default_directory": "/home/me/projects",
  "remotes": [
    {
      "name": "dgx",
      "host": "dgx-login.internal",
      "directory": "/home/me/projects",
      "username": "me",
      "key": "~/.ssh/id_ed25519",
      "port": 22
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Label used in CLI commands (e.g. `remwork-cli upload dgx`) |
| `host` | yes | Hostname or IP address |
| `directory` | no | Absolute path on the remote for uploads. Falls back to `default_directory` |
| `username` | no | SSH user. Uses local username if omitted |
| `key` | no | Path to SSH private key. Uses default SSH keys if omitted |
| `port` | no | SSH port (default: 22) |

`default_directory` is a top-level fallback used when a remote has no `directory` set.

### Import from SSH config

If you already have hosts defined in `~/.ssh/config`, import them:

```bash
remwork-cli export-ssh-config --default-dir /home/me/projects
```

This writes all non-wildcard Host blocks into `remotes.json`.

### Verify your remotes

```bash
remwork-cli list              # show all configured remotes
remwork-cli edit              # open config in $EDITOR
```

## Per-project workspace config

Place a `.remworkconf` file in your project directory to set defaults for all commands. CLI flags always override these values.

```bash
cp .remworkconf.example .remworkconf
```

```json
{
  "remote": "dgx",
  "folder": "my-project",
  "upload": {
    "exclude": ["*.pyc", "__pycache__", "*.egg-info"],
    "dry_run": false
  },
  "allocate": {
    "account": "my-project-account",
    "partition": "gpu",
    "nodes": 1,
    "gpus": 4,
    "gpus_per_node": 4,
    "ntasks_per_node": 4,
    "time": "2:00:00",
    "job_name": "my-project",
    "cpus_per_task": 8,
    "mem": "32G"
  },
  "container": {
    "image": "nvcr.io/nvidia/pytorch:24.01-py3",
    "name": "my-env",
    "mounts": ["/data:/data", "/home/me/projects:/workspace"],
    "env": ["NCCL_DEBUG=INFO"],
    "setup_script": "/workspace/my-project/setup_env.sh"
  }
}
```

| Key | Description |
|-----|-------------|
| `remote` | Default remote name for all commands |
| `folder` | Subfolder under the remote directory for uploads and as the working directory for `run` |
| `upload.exclude` | Additional rsync exclude patterns (`.git` is always excluded) |
| `upload.dry_run` | Default to dry-run mode for uploads |
| `allocate.*` | Default Slurm parameters (see [Allocating resources](#allocating-resources)) |
| `container.*` | Container configuration (see [Setting up containers](#setting-up-containers)) |
| `remotes.<name>.*` | Per-remote config (see [Per-remote profiles](#per-remote-profiles)) |

With `.remworkconf` in place, most commands need zero arguments:

```bash
remwork-cli upload
remwork-cli allocate
remwork-cli setup
remwork-cli run -- python train.py
```

### Per-remote profiles

When working with multiple remotes that need different Slurm params or container setups, add a `remotes` dict. Each key is a remote name with its own `folder`, `allocate`, `container`, and `upload` config:

```json
{
  "remote": "dgx-a",
  "remotes": {
    "dgx-a": {
      "folder": "my-project",
      "allocate": {
        "account": "team-a",
        "partition": "gpu",
        "nodes": 1,
        "gpus-per-node": 4,
        "time": "2:00:00"
      },
      "container": {
        "image": "nvcr.io/nvidia/pytorch:24.01-py3",
        "name": "my-env",
        "mounts": ["/data:/data", "/home/me/projects:/workspace"],
        "setup_script": "/workspace/my-project/setup_env.sh"
      }
    },
    "dgx-b": {
      "folder": "my-project",
      "allocate": {
        "account": "team-b",
        "partition": "batch",
        "nodes": 2,
        "gpus-per-node": 8,
        "time": "8:00:00"
      },
      "container": {
        "image": "nvcr.io/nvidia/nemo:24.01",
        "name": "nemo-env",
        "mounts": ["/lustre:/data", "/home/me/projects:/workspace"],
        "setup_script": "/workspace/my-project/setup_nemo.sh"
      }
    }
  }
}
```

When a remote has an entry under `remotes`, that entry is used as-is — the top-level `allocate`/`container`/`upload` sections are ignored for that remote. Remotes without an entry still fall back to the top-level config.

```bash
remwork-cli allocate dgx-a    # uses remotes.dgx-a.allocate
remwork-cli allocate dgx-b    # uses remotes.dgx-b.allocate
remwork-cli setup dgx-a       # uses remotes.dgx-a.container
remwork-cli run dgx-b -- python train.py  # uses remotes.dgx-b.container
```

## Uploading code

Sync your local directory to the remote via rsync:

```bash
remwork-cli upload                         # uses .remworkconf defaults
remwork-cli upload dgx                     # explicit remote
remwork-cli upload dgx my-project          # into <remote dir>/my-project
remwork-cli upload --dry-run               # preview what would be transferred
remwork-cli upload --dir /path/to/src      # upload a different local directory
```

`.git/` is always excluded from uploads.

## Allocating resources

Allocate compute resources on a remote Slurm cluster. This submits a persistent hold job via `sbatch` that stays alive until you release it, letting you run multiple commands against the same allocation.

```bash
remwork-cli allocate dgx --partition gpu --gpus 4 --time 2:00:00
remwork-cli allocate                       # uses .remworkconf defaults
remwork-cli allocate --wait                # block until RUNNING if queued
```

The job ID is tracked in `.remwork_allocation` (auto-managed, gitignored). If a previous allocation has been completed, cancelled, or timed out, it is automatically cleaned up.

### Slurm flags

| Flag | Short | Description |
|------|-------|-------------|
| `--partition` | `-p` | Slurm partition |
| `--account` | `-A` | Slurm account/project |
| `--nodes` | `-N` | Number of nodes |
| `--gpus` | `-G` | Number of GPUs (`--gres=gpu:N`) |
| `--gpus-per-node` | | GPUs per node |
| `--ntasks-per-node` | | Tasks per node |
| `--time` | `-t` | Time limit (e.g. `2:00:00`) |
| `--job-name` | `-J` | Job name |
| `--cpus-per-task` | | CPUs per task |
| `--mem` | | Memory (e.g. `32G`) |
| `--extra-args` | | Additional Slurm arguments (quoted string) |

All of these can be set as defaults in `.remworkconf` under the `allocate` key.

## Setting up containers

After allocating resources, set up an enroot container on the compute nodes. This imports a Docker image, creates a named container, and optionally runs a setup script to install dependencies.

### Container configuration

Configure the container in `.remworkconf` under the `container` key:

```json
{
  "container": {
    "image": "nvcr.io/nvidia/pytorch:24.01-py3",
    "name": "my-env",
    "mounts": ["/data:/data", "/home/me/projects:/workspace"],
    "env": ["NCCL_DEBUG=INFO", "CUDA_VISIBLE_DEVICES=0,1,2,3"],
    "setup_script": "/workspace/my-project/setup_env.sh"
  }
}
```

| Field | Description |
|-------|-------------|
| `image` | Docker/enroot image URI to import (e.g. from NGC, Docker Hub) |
| `name` | Name for the enroot container, used by `setup` and `run` |
| `mounts` | Bind mounts in `host:container` format, passed to `enroot start --mount` |
| `env` | Environment variables in `KEY=VALUE` format, passed to `enroot start --env` |
| `setup_script` | Script to run inside the container during setup (path as seen inside the container) |

### Running setup

```bash
remwork-cli setup                          # uses .remworkconf container defaults
remwork-cli setup --image nvcr.io/nvidia/pytorch:24.01-py3 --name my-env
remwork-cli setup --setup-script /workspace/my-project/setup_env.sh
remwork-cli setup --force                  # re-import even if image is cached
```

Setup runs three steps on the compute node:

1. **Import** — `enroot import docker://<image>` (skipped if the `.sqsh` file already exists, unless `--force`)
2. **Create** — `enroot create --name <name>` from the imported image
3. **Setup script** — `enroot start` the container with mounts and env vars applied, then runs the setup script

The `.sqsh` image is stored at `<remote directory>/.enroot/` and reused across setup calls.

### Writing a setup script

The setup script runs inside the container with all configured mounts and environment variables. Use it to install pip packages, configure tools, or prepare the environment:

```bash
#!/bin/bash
# setup_env.sh — runs inside the container during 'remwork-cli setup'

pip install -r /workspace/my-project/requirements.txt
pip install wandb deepspeed

# Any one-time configuration
wandb login --relogin $WANDB_KEY
```

Place the script somewhere that will be visible inside the container (i.e., under a configured mount point). The `setup_script` path in `.remworkconf` should be the path as seen from inside the container.

## Running commands

Execute commands on the allocated compute nodes:

```bash
remwork-cli run -- python train.py --epochs 10
remwork-cli run -- python eval.py --checkpoint best.pt
remwork-cli run dgx -- bash                # interactive shell on the node
remwork-cli run --wait -- python train.py  # wait if allocation is still queued
remwork-cli run --no-container -- nvidia-smi   # bypass container, run on host
remwork-cli run --jobid 12345 -- nvidia-smi    # use a specific job ID
```

Everything after `--` is the command. It is executed via `srun --jobid --overlap` on the allocated nodes.

### Container auto-wrapping

When `container.name` is set in `.remworkconf`, commands are automatically wrapped with `enroot start --rw` using the configured mounts and env vars. This means your commands run inside the container by default:

```bash
# These are equivalent when container.name is set:
remwork-cli run -- python train.py
# Internally runs: srun --jobid=ID --overlap enroot start --rw --mount ... --env ... my-env bash -c 'cd /workspace/my-project && python train.py'
```

Pass `--no-container` to bypass the container and run directly on the host node.

### Working directory

The `run` command automatically sets the working directory based on the `folder` setting in `.remworkconf`:

- **Inside a container**: `cd /workspace/<folder>` (assumes `/workspace` mount)
- **On bare host**: `cd <remote directory>/<folder>`

### Job state checks

Before executing, `run` queries the Slurm scheduler to verify the job is alive:

- **RUNNING** — proceeds immediately
- **PENDING** — errors unless `--wait` is passed to block until running
- **Gone** (completed, cancelled, timed out, preempted) — cleans up stale state and reports

## Monitoring and releasing

### Check allocation status

```bash
remwork-cli status                         # all active allocations
remwork-cli status dgx                     # specific remote
```

Shows job ID, state, partition, nodes, GPU resources, time used, and time limit. Stale allocations (jobs no longer in the scheduler) are automatically cleaned up.

### Release allocation

```bash
remwork-cli release                        # uses .remworkconf default remote
remwork-cli release dgx
```

Cancels the Slurm job via `scancel` and cleans up the local state.

## Typical workflow

```bash
# 1. Sync code to the remote
remwork-cli upload

# 2. Allocate GPU resources
remwork-cli allocate -p gpu -G 4 -t 4:00:00

# 3. Set up the container environment (import image + install deps)
remwork-cli setup

# 4. Run experiments
remwork-cli run -- python train.py
remwork-cli upload                         # push code changes
remwork-cli run -- python train.py --lr 1e-4

# 5. Evaluate
remwork-cli run -- python eval.py

# 6. Done — free the GPUs
remwork-cli release
```
