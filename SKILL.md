---
name: remwork
description: Use when the user wants to upload code to a remote server, allocate Slurm compute resources, set up enroot containers, or run commands on a remote node. Triggers on mentions of remwork, remwork-cli, rsync to remote, Slurm allocation, enroot container setup, remote task running, or when a `.remworkconf` file is present in the workspace.
---

# remwork-cli skill

Wraps the `remwork-cli` command so Claude can drive a remote-work workflow: sync code → allocate GPUs → set up a container → run commands → release. Single CLI, stdlib-only Python. See `remwork-cli --help` for the full option list; this skill covers the patterns Claude should follow.

## When to use

Trigger this skill when the user asks to:
- **Upload / sync / push** a local directory to a remote host (implicitly or explicitly via rsync)
- **Allocate / reserve / request** GPU or CPU resources on a Slurm cluster
- **Set up** an enroot container (import image, run setup script) on the allocated node
- **Run / execute** a command on the allocated node (training, eval, interactive shell)
- **Check status** of an active allocation, or **release / cancel** one
- Any remote compute task in a workspace that already has a `.remworkconf`

If no `.remworkconf` exists in the cwd, check whether the user wants one created before running commands that rely on defaults.

## Preflight

Before running the first `remwork-cli` command in a session:

1. Verify the CLI is on PATH: `remwork-cli --help` (if missing, tell the user to `pip install .` from the repo at `/Users/zhenghangr/work_dir/utils/upload`)
2. `remwork-cli list` — confirm the target remote exists
3. If the user named a remote that isn't listed, stop and ask rather than guessing

Read `.remworkconf` (if present) to understand project defaults before overriding with flags.

### Preflight before `allocate`

**Always run `remwork-cli status [remote]` before `allocate`.** There are three cases:

- **Active allocation (RUNNING or PENDING) exists** — do NOT call `allocate`. The CLI will refuse anyway (remwork_cli.py:534), but Claude should recognize the situation from `status` and tell the user. If PENDING, offer to wait on the existing job (`run --wait` with `run_in_background: true`, or a background poll of `status`) instead of submitting a new one. If the user wants different params, they must `release` first.
- **Stale entry** (`.remwork_allocation` has a job that's no longer in `squeue`) — `status` auto-cleans it. Safe to `allocate`.
- **No allocation** — safe to `allocate`.

This preflight is cheap (one `squeue` query) and prevents Claude from ever hitting the "already has an active allocation" error after a timeout or a reconnect.

## Core commands

All commands accept an optional positional `remote` name. If omitted, the default from `.remworkconf` is used.

| Command | What it does |
|---------|--------------|
| `remwork-cli list` | Show configured remotes |
| `remwork-cli upload [remote] [folder] [--dry-run] [--dir PATH]` | rsync cwd (or `--dir`) to `<remote.directory>/<folder>` |
| `remwork-cli allocate [remote] [slurm-flags] [--wait]` | `sbatch --wrap "sleep infinity"` to hold resources |
| `remwork-cli setup [remote] [--image URI] [--name NAME] [--setup-script PATH] [--force]` | `enroot import` → `create` → run setup script |
| `remwork-cli run [remote] [--wait] [--no-container] [--jobid ID] -- <cmd>` | `srun --jobid --overlap` (auto-wrapped with `enroot start` if `container.name` is set) |
| `remwork-cli status [remote]` | Show allocation state from `squeue` |
| `remwork-cli release [remote]` | `scancel` the held job |

Slurm flags for `allocate`: `--partition/-p`, `--account/-A`, `--nodes/-N`, `--gpus/-G`, `--gpus-per-node`, `--ntasks-per-node`, `--time/-t`, `--job-name/-J`, `--cpus-per-task`, `--mem`, `--extra-args "..."`.

## Typical workflow

```bash
remwork-cli upload                      # 1. sync code
remwork-cli allocate --wait             # 2. hold GPUs (background mode, see below)
remwork-cli setup                       # 3. import image + run setup script
remwork-cli run -- python train.py      # 4. execute inside container
remwork-cli run -- python eval.py       # ... repeat as needed
remwork-cli release                     # 5. free the allocation
```

When iterating on code, the loop is `upload` → `run`. The allocation persists across `run` calls until released.

### Handling long Slurm queues

`allocate --wait` and `run --wait` poll `squeue` forever until the job is RUNNING. Claude's foreground Bash tool times out after 10 min max, so for anything that might queue longer:

1. Launch the `--wait` command with **`run_in_background: true`** — Claude gets notified on completion, no timeout.
2. While waiting, continue with other work (reading code, preparing the next `run` command, drafting the setup script).
3. If the user asks for status mid-wait, use `remwork-cli status` in a separate foreground Bash call — it reads the saved job_id and queries `squeue` without touching the background waiter.

The job_id is saved to `.remwork_allocation` the moment `sbatch` returns, before the wait loop starts. So even if the background process is killed (terminal closed, machine reboot), the allocation is recoverable via `status` / `release`.

## Usage rules for Claude

- **Never guess a remote name.** Always `remwork-cli list` first if unsure, and present the options.
- **Use `--dry-run` on `upload`** the first time in a session so the user sees what will transfer before the real sync. Drop it on subsequent syncs once behavior is confirmed.
- **Use `--wait`** on `allocate` and on the first `run` after allocation — cluster queues make PENDING→RUNNING non-instant, and `run` fails on PENDING without it.
- **Always run `--wait` calls with `run_in_background: true`.** `remwork-cli`'s wait loop has no internal timeout, but Claude's foreground Bash tool caps at 10 min. A long Slurm queue will kill the foreground subprocess. Background mode has no such cap — Claude is notified when the command completes. This applies to `allocate --wait` and `run --wait`.
- **Never retry `allocate` on a Bash timeout.** `sbatch` runs and the job_id is persisted to `.remwork_allocation` *before* the wait loop starts, so a timeout does not mean the allocation failed. Running `allocate` again would submit a duplicate hold job. Recovery path: `remwork-cli status` to see the saved job, then either keep waiting (background `status` loop) or `release` if unwanted.
- **Command after `--` is literal.** Everything following `--` is executed on the compute node. Quote paths with spaces; don't add shell redirections unless the user asked for them.
- **Respect `.remworkconf`.** If it sets `remote`, `folder`, `allocate`, or `container`, don't repeat those as flags unless overriding intentionally — prefer terse commands that rely on the config.
- **Before `release`**, confirm with the user when there's an active long-running job (check `status` first). Cancelling a running training job is destructive.
- **Before re-running `setup --force`**, confirm with the user — it re-imports the image, which is slow and unnecessary if the `.sqsh` already exists.
- **Container auto-wrapping**: when `container.name` is set, `run` puts the command inside the container with configured mounts/env. Only pass `--no-container` if the user wants host-level execution (e.g., `nvidia-smi` on the bare node).
- **Working directory for `run`**: inside a container, cwd is `/workspace/<folder>`; on bare host, it's `<remote.directory>/<folder>`. Use paths relative to that, not absolute local paths.
- **Don't tail logs with `run`** for long training jobs — `run` is synchronous and ties up the foreground. For long-running work, suggest the user redirect output (`... > train.log 2>&1 &`) or attach via a separate interactive `run -- bash` session.
- **`.remwork_allocation` is state**, not config. Don't edit it by hand; let `allocate`/`release`/`status` manage it.

## Configuration files

| File | Role |
|------|------|
| `~/.local/remwork_cli/remotes.json` | Global remote definitions (host, user, key, directory) |
| `./remotes.json` | Per-repo override of the above |
| `./.remworkconf` | Per-workspace defaults: remote, folder, upload excludes, Slurm params, container config |
| `./.remwork_allocation` | Runtime state: active Slurm job IDs per remote (auto-managed, gitignored) |

`.remworkconf` supports per-remote profiles under a `remotes` dict — each entry has its own `folder`, `allocate`, `container`, `upload`. When a remote has an entry there, the top-level sections are ignored for that remote.

Creating a new `.remworkconf` from scratch: copy from `/Users/zhenghangr/work_dir/utils/upload/.remworkconf.example` if the user has that repo, otherwise write one based on the schema above.

## Requirements on the remote

- `rsync` and `ssh` — for `upload`
- Slurm (`sbatch`, `srun`, `squeue`, `scancel`) — for `allocate`/`run`/`status`/`release`
- `enroot` — for `setup` and container-wrapped `run`

If any of these are missing on a remote, surface the error to the user rather than retrying — it's an environment problem, not a CLI issue.
