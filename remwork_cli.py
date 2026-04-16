#!/usr/bin/env python3
"""
Remote work CLI: upload directories and allocate resources on remote targets.
Configuration: JSON file with remote host, directory, username, and SSH key paths.
Workspace config: .remworkconf in the current directory for per-project defaults.
Allocation state: .remwork_allocation tracks active Slurm jobs per remote.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


CONFIG_ENV = "REMWORK_CONFIG"
DEFAULT_CONFIG = os.path.expanduser("~/.local/remwork_cli/remotes.json")
DEFAULT_SSH_CONFIG = "~/.ssh/config"
WORKSPACE_CONFIG = ".remworkconf"
ALLOCATION_STATE = ".remwork_allocation"


def expand_path(path: str) -> str:
    """Expand ~ and environment variables in path."""
    return os.path.expanduser(os.path.expandvars(path))


# ── Workspace config (.remworkconf) ──────────────────────────────────────────

def load_workspace_config() -> dict:
    """Load .remworkconf from the current directory. Returns empty dict if not found."""
    path = os.path.join(os.getcwd(), WORKSPACE_CONFIG)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"Warning: could not parse {path}", file=sys.stderr)
        return {}


def resolve_remote(remote_name: Optional[str], ws: dict) -> str:
    """Resolve remote name from CLI arg or workspace config."""
    name = remote_name or ws.get("remote")
    if not name:
        print(
            "No remote specified. Pass a remote name or set 'remote' in .remworkconf.",
            file=sys.stderr,
        )
        sys.exit(1)
    return name


# ── Allocation state (.remwork_allocation) ───────────────────────────────────

def _allocation_path() -> str:
    return os.path.join(os.getcwd(), ALLOCATION_STATE)


def load_all_allocations() -> dict:
    """Load all allocations keyed by remote name. Returns empty dict if none."""
    path = _allocation_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_allocation(remote_name: str) -> Optional[str]:
    """Return the stored job ID for a remote, or None."""
    return load_all_allocations().get(remote_name, {}).get("job_id")


def save_allocation(remote_name: str, job_id: str) -> None:
    """Save a job ID for the given remote."""
    allocs = load_all_allocations()
    allocs[remote_name] = {"job_id": job_id}
    with open(_allocation_path(), "w") as f:
        json.dump(allocs, f, indent=2)


def clear_allocation(remote_name: str) -> None:
    """Remove the allocation entry for a remote. Delete the file if empty."""
    allocs = load_all_allocations()
    allocs.pop(remote_name, None)
    path = _allocation_path()
    if allocs:
        with open(path, "w") as f:
            json.dump(allocs, f, indent=2)
    elif os.path.isfile(path):
        os.remove(path)


# ── Remotes config (remotes.json) ────────────────────────────────────────────

def _load_config_safe(config_path: Optional[str]) -> Optional[dict]:
    """Load config without exiting; returns None on missing/invalid (for completion)."""
    path = _resolve_config_path(config_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data.get("remotes"), list) else None
    except Exception:
        return None


def _resolve_config_path(config_path: Optional[str]) -> str:
    """Resolve config file path: explicit > env > default (with cwd fallback)."""
    if config_path:
        return expand_path(config_path)
    if os.environ.get(CONFIG_ENV):
        return expand_path(os.environ[CONFIG_ENV])
    p = expand_path(DEFAULT_CONFIG)
    if os.path.isfile(p):
        return p
    cwd = os.path.join(os.getcwd(), "remotes.json")
    return cwd if os.path.isfile(cwd) else p


def load_config(config_path: Optional[str]) -> dict:
    """Load remotes config from JSON file."""
    path = _resolve_config_path(config_path)
    if not os.path.isfile(path):
        print(f"Config file not found: {path}", file=sys.stderr)
        print(
            f"Set {CONFIG_ENV}, pass --config, or run from a dir with remotes.json. "
            f"Install creates {expand_path(DEFAULT_CONFIG)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(path, "r") as f:
        data = json.load(f)
    if "remotes" not in data:
        print("Config must contain a 'remotes' array.", file=sys.stderr)
        sys.exit(1)
    return data


def get_remote_directory(remote: dict, config: dict) -> str:
    """Resolve remote directory; use config default_directory when remote's directory is empty."""
    directory = (remote.get("directory") or "").strip()
    if not directory:
        directory = (config.get("default_directory") or "").strip()
    return directory


def find_remote(name: str, data: dict) -> dict:
    """Look up a remote by name; exit with error if not found."""
    remotes = {r["name"]: r for r in data["remotes"] if r.get("name")}
    if name not in remotes:
        print(f"Unknown remote: {name}", file=sys.stderr)
        print("Available:", ", ".join(remotes.keys()) or "none", file=sys.stderr)
        sys.exit(1)
    return remotes[name]


# ── SSH helpers ──────────────────────────────────────────────────────────────

def build_ssh_cmd(remote: dict, tty: bool = False) -> List[str]:
    """Build SSH command to connect to a remote."""
    user = remote.get("username", "")
    host = remote.get("host", "")
    key = remote.get("key")
    port = remote.get("port", 22)
    cmd = ["ssh"]
    if tty:
        cmd.append("-t")
    cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
    if key:
        cmd.extend(["-i", expand_path(key)])
    if port != 22:
        cmd.extend(["-p", str(port)])
    target = f"{user}@{host}" if user else host
    cmd.append(target)
    return cmd


def _ssh_run_capture(remote: dict, remote_cmd: str) -> subprocess.CompletedProcess:
    """SSH into remote, run a command, and capture stdout. Stderr passes through."""
    ssh_cmd = build_ssh_cmd(remote)
    return subprocess.run(ssh_cmd + [remote_cmd], stdout=subprocess.PIPE, text=True)


def _ssh_run_interactive(remote: dict, remote_cmd: str) -> int:
    """SSH into remote with TTY and run a command interactively."""
    ssh_cmd = build_ssh_cmd(remote, tty=True)
    return subprocess.call(ssh_cmd + [remote_cmd])


# ── Slurm job state helpers ─────────────────────────────────────────────────

# States where the job is still in the scheduler but not yet running
_SLURM_QUEUED_STATES = {"PENDING", "CONFIGURING", "REQUEUED"}

# States where the job is usable for srun --jobid
_SLURM_RUNNING_STATES = {"RUNNING"}

# States where the job is winding down or done — no longer usable
_SLURM_TERMINAL_STATES = {
    "COMPLETING", "COMPLETED", "CANCELLED", "FAILED",
    "TIMEOUT", "PREEMPTED", "NODE_FAIL", "OUT_OF_MEMORY",
    "DEADLINE", "SUSPENDED",
}


def query_job_state(remote: dict, job_id: str) -> Optional[str]:
    """Query Slurm job state via squeue.

    Returns the state string (RUNNING, PENDING, ...) or None if the job is no
    longer in the scheduler (completed, cancelled, etc.).
    """
    cmd = f"squeue --job={shlex.quote(job_id)} --noheader --format=%T"
    result = _ssh_run_capture(remote, cmd)
    state = result.stdout.strip()
    if not state or result.returncode != 0:
        return None
    # squeue may return multiple lines for job arrays; take the first
    return state.splitlines()[0].strip()


def _check_stale_allocation(remote: dict, remote_name: str, job_id: str) -> Optional[str]:
    """Check whether a stored allocation is still alive.

    Returns the current state if the job is still in the scheduler, or None
    after cleaning up the stale entry.
    """
    state = query_job_state(remote, job_id)
    if state is None:
        clear_allocation(remote_name)
        return None
    upper = state.upper()
    if upper in _SLURM_TERMINAL_STATES:
        clear_allocation(remote_name)
        return None
    return upper


def wait_for_running(
    remote: dict,
    job_id: str,
    remote_name: str,
    poll_interval: int = 10,
) -> str:
    """Poll until a job reaches RUNNING (or is gone). Returns final state or exits."""
    while True:
        state = query_job_state(remote, job_id)
        if state is None:
            clear_allocation(remote_name)
            print(f"\nJob {job_id} is no longer in the scheduler.", file=sys.stderr)
            sys.exit(1)
        upper = state.upper()
        if upper in _SLURM_RUNNING_STATES:
            print(f"\rJob {job_id}: {upper}                    ")
            return upper
        if upper in _SLURM_TERMINAL_STATES:
            clear_allocation(remote_name)
            print(f"\nJob {job_id} entered terminal state: {upper}", file=sys.stderr)
            sys.exit(1)
        print(f"\rJob {job_id}: {upper} — waiting...", end="", flush=True)
        time.sleep(poll_interval)


# ── list ─────────────────────────────────────────────────────────────────────

def list_remotes(config_path: Optional[str]) -> None:
    """Print configured remote targets."""
    data = load_config(config_path)
    remotes = data["remotes"]
    if not remotes:
        print("No remotes configured.")
        return
    print(f"{'Name':<20} {'User@Host':<35} {'Remote directory'}")
    print("-" * 80)
    for r in remotes:
        name = r.get("name", "?")
        user = r.get("username", "")
        host = r.get("host", "")
        user_host = f"{user}@{host}" if user else host
        directory = get_remote_directory(r, data)
        if not directory:
            directory = "(not set)"
        port = r.get("port")
        if port and port != 22:
            user_host += f":{port}"
        print(f"{name:<20} {user_host:<35} {directory}")


# ── upload ───────────────────────────────────────────────────────────────────

def build_rsync_cmd(
    remote: dict,
    local_dir: str,
    dry_run: bool,
    extra_excludes: Optional[List[str]] = None,
) -> List[str]:
    """Build rsync command and args (no shell)."""
    user = remote.get("username", "")
    host = remote.get("host", "")
    directory = remote.get("directory", "").rstrip("/")
    key = remote.get("key")
    port = remote.get("port", 22)
    if not host or not directory:
        raise ValueError("host and directory are required")
    dest = f"{user}@{host}:{directory}" if user else f"{host}:{directory}"
    ssh_cmd_parts = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        ssh_cmd_parts.extend(["-i", expand_path(key)])
    if port != 22:
        ssh_cmd_parts.extend(["-p", str(port)])
    ssh_cmd = " ".join(ssh_cmd_parts)
    # Ensure remote directory exists (mkdir -p) before rsync
    rsync_path = "mkdir -p " + shlex.quote(directory) + " && rsync"
    args = [
        "rsync",
        "-avz",
        "--progress",
        "--exclude",
        ".git",
    ]
    for pattern in extra_excludes or []:
        args.extend(["--exclude", pattern])
    args.extend([
        "-e",
        ssh_cmd,
        "--rsync-path",
        rsync_path,
        f"{local_dir.rstrip(os.sep)}{os.sep}",
        dest + "/",
    ])
    if dry_run:
        args.insert(1, "--dry-run")
        args.insert(2, "-v")
    return args


def upload(
    remote_name: Optional[str],
    config_path: Optional[str],
    local_dir: Optional[str],
    folder: Optional[str],
    dry_run: bool,
) -> None:
    """Upload current (or given) directory to the named remote via rsync."""
    ws = load_workspace_config()
    name = resolve_remote(remote_name, ws)
    folder = folder or ws.get("folder")

    upload_conf = ws.get("upload", {})
    extra_excludes = upload_conf.get("exclude", [])
    if upload_conf.get("dry_run"):
        dry_run = True

    data = load_config(config_path)
    remote = find_remote(name, data)
    effective_dir = get_remote_directory(remote, data)
    if not effective_dir:
        print(
            "No directory set for this remote and no default_directory in config.",
            file=sys.stderr,
        )
        sys.exit(1)
    if folder:
        effective_dir = effective_dir.rstrip("/") + "/" + folder.strip("/")
    remote_with_dir = {**remote, "directory": effective_dir}
    local = expand_path(local_dir or os.getcwd())
    if not os.path.isdir(local):
        print(f"Not a directory: {local}", file=sys.stderr)
        sys.exit(1)
    cmd = build_rsync_cmd(remote_with_dir, local, dry_run, extra_excludes)
    if dry_run:
        print("Dry run. Would execute:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(rc)


# ── allocate ─────────────────────────────────────────────────────────────────

def _build_slurm_resource_args(
    partition: Optional[str] = None,
    account: Optional[str] = None,
    nodes: Optional[int] = None,
    gpus: Optional[int] = None,
    gpus_per_node: Optional[int] = None,
    ntasks_per_node: Optional[int] = None,
    time_limit: Optional[str] = None,
    job_name: Optional[str] = None,
    cpus_per_task: Optional[int] = None,
    mem: Optional[str] = None,
    extra_args: Optional[str] = None,
) -> List[str]:
    """Build Slurm resource arguments common to sbatch/salloc."""
    args = []
    if partition:
        args.extend(["--partition", partition])
    if account:
        args.extend(["--account", account])
    if nodes is not None:
        args.extend(["--nodes", str(nodes)])
    if gpus is not None:
        args.append(f"--gres=gpu:{gpus}")
    if gpus_per_node is not None:
        args.extend(["--gpus-per-node", str(gpus_per_node)])
    if ntasks_per_node is not None:
        args.extend(["--ntasks-per-node", str(ntasks_per_node)])
    if time_limit:
        args.extend(["--time", time_limit])
    if job_name:
        args.extend(["--job-name", job_name])
    if cpus_per_task is not None:
        args.extend(["--cpus-per-task", str(cpus_per_task)])
    if mem:
        args.extend(["--mem", mem])
    if extra_args:
        args.extend(shlex.split(extra_args))
    return args


def _resolve_slurm_params(
    ws: dict,
    partition: Optional[str],
    account: Optional[str],
    nodes: Optional[int],
    gpus: Optional[int],
    gpus_per_node: Optional[int],
    ntasks_per_node: Optional[int],
    time_limit: Optional[str],
    job_name: Optional[str],
    cpus_per_task: Optional[int],
    mem: Optional[str],
    extra_args: Optional[str],
) -> dict:
    """Merge CLI args over .remworkconf allocate defaults."""
    alloc_conf = ws.get("allocate", {})
    return {
        "partition": partition or alloc_conf.get("partition"),
        "account": account or alloc_conf.get("account"),
        "nodes": nodes if nodes is not None else alloc_conf.get("nodes"),
        "gpus": gpus if gpus is not None else alloc_conf.get("gpus"),
        "gpus_per_node": (
            gpus_per_node
            if gpus_per_node is not None
            else alloc_conf.get("gpus_per_node")
        ),
        "ntasks_per_node": (
            ntasks_per_node
            if ntasks_per_node is not None
            else alloc_conf.get("ntasks_per_node")
        ),
        "time_limit": time_limit or alloc_conf.get("time"),
        "job_name": job_name or alloc_conf.get("job_name"),
        "cpus_per_task": (
            cpus_per_task
            if cpus_per_task is not None
            else alloc_conf.get("cpus_per_task")
        ),
        "mem": mem or alloc_conf.get("mem"),
        "extra_args": extra_args or alloc_conf.get("extra_args"),
    }


def allocate(
    remote_name: Optional[str],
    config_path: Optional[str],
    wait: bool = False,
    partition: Optional[str] = None,
    account: Optional[str] = None,
    nodes: Optional[int] = None,
    gpus: Optional[int] = None,
    gpus_per_node: Optional[int] = None,
    ntasks_per_node: Optional[int] = None,
    time_limit: Optional[str] = None,
    job_name: Optional[str] = None,
    cpus_per_task: Optional[int] = None,
    mem: Optional[str] = None,
    extra_args: Optional[str] = None,
) -> None:
    """Allocate resources on a remote via Slurm sbatch (persistent hold job).

    The allocation is kept alive as a background Slurm job.  Use 'run' to
    execute commands within it, 'status' to inspect it, and 'release' to
    cancel it.
    """
    ws = load_workspace_config()
    name = resolve_remote(remote_name, ws)

    data = load_config(config_path)
    remote = find_remote(name, data)

    # Check for existing allocation — verify it's actually still alive
    existing = load_allocation(name)
    if existing:
        state = _check_stale_allocation(remote, name, existing)
        if state is not None:
            print(
                f"Remote '{name}' already has an active allocation "
                f"(job {existing}, state {state}).",
                file=sys.stderr,
            )
            print(
                "Run 'remwork-cli release' to free it first.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Stale allocation was cleaned up; proceed with new one
        print(f"Previous allocation (job {existing}) is no longer active, cleaned up.")

    params = _resolve_slurm_params(
        ws, partition, account, nodes, gpus, gpus_per_node,
        ntasks_per_node, time_limit, job_name,
        cpus_per_task, mem, extra_args,
    )

    # Build: sbatch --parsable --wrap 'sleep infinity' <resource-args>
    sbatch_parts = ["sbatch", "--parsable", "--wrap", shlex.quote("sleep infinity")]
    sbatch_parts.extend(_build_slurm_resource_args(**params))
    remote_cmd = " ".join(sbatch_parts)

    print(f"Submitting hold job on '{name}': {remote_cmd}")
    result = _ssh_run_capture(remote, remote_cmd)
    if result.returncode != 0:
        print("sbatch failed.", file=sys.stderr)
        sys.exit(result.returncode)

    job_id = result.stdout.strip()
    if not job_id:
        print("sbatch returned no job ID.", file=sys.stderr)
        sys.exit(1)

    save_allocation(name, job_id)

    # Check initial state
    state = query_job_state(remote, job_id)
    state_str = state.upper() if state else "UNKNOWN"

    if state_str in _SLURM_RUNNING_STATES:
        print(f"Job {job_id} on '{name}': RUNNING. Ready for 'run'.")
    elif state_str in _SLURM_QUEUED_STATES:
        print(f"Job {job_id} on '{name}': {state_str} (queued).")
        if wait:
            wait_for_running(remote, job_id, name)
            print(f"Job {job_id} is now RUNNING. Ready for 'run'.")
        else:
            print("Use 'remwork-cli status' to check, or pass --wait to block until running.")
    else:
        print(f"Job {job_id} on '{name}': {state_str}.")


# ── setup (enroot container) ────────────────────────────────────────────────

def _build_enroot_start_cmd(container_conf: dict, user_cmd: List[str]) -> str:
    """Build a shell-safe enroot start command string."""
    parts = ["enroot", "start", "--rw"]
    for m in container_conf.get("mounts", []):
        parts.extend(["--mount", m])
    for e in container_conf.get("env", []):
        parts.extend(["--env", e])
    parts.append(container_conf["name"])
    parts.extend(user_cmd)
    return " ".join(shlex.quote(p) for p in parts)


def _require_running_allocation(
    remote: dict, remote_name: str, job_id: str,
) -> None:
    """Exit if the allocation is not RUNNING."""
    state = _check_stale_allocation(remote, remote_name, job_id)
    if state is None:
        print(
            f"Allocation job {job_id} on '{remote_name}' is no longer active.",
            file=sys.stderr,
        )
        print("Run 'remwork-cli allocate' to create a new one.", file=sys.stderr)
        sys.exit(1)
    if state in _SLURM_QUEUED_STATES:
        print(
            f"Allocation job {job_id} is {state} (still queued). "
            "Wait until RUNNING before setup.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state not in _SLURM_RUNNING_STATES:
        print(
            f"Allocation job {job_id} is in unexpected state: {state}",
            file=sys.stderr,
        )
        sys.exit(1)


def setup_container(
    remote_name: Optional[str],
    config_path: Optional[str],
    image: Optional[str] = None,
    container_name: Optional[str] = None,
    setup_script: Optional[str] = None,
    force: bool = False,
) -> None:
    """Import an enroot container image, create it, and run an optional setup script."""
    ws = load_workspace_config()
    name = resolve_remote(remote_name, ws)
    container_conf = ws.get("container", {})

    image = image or container_conf.get("image")
    container_name = container_name or container_conf.get("name")
    setup_script = setup_script or container_conf.get("setup_script")
    mounts = container_conf.get("mounts", [])
    env_vars = container_conf.get("env", [])

    if not image:
        print(
            "No container image specified. "
            "Set 'container.image' in .remworkconf or pass --image.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not container_name:
        print(
            "No container name specified. "
            "Set 'container.name' in .remworkconf or pass --name.",
            file=sys.stderr,
        )
        sys.exit(1)

    job_id = load_allocation(name)
    if not job_id:
        print(
            f"No active allocation for remote '{name}'. "
            "Run 'remwork-cli allocate' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    data = load_config(config_path)
    remote = find_remote(name, data)
    _require_running_allocation(remote, name, job_id)

    # Use the remote's directory setting to store the sqsh image
    remote_dir = get_remote_directory(remote, data)
    if not remote_dir:
        print(
            "No directory set for this remote and no default_directory in config. "
            "Needed for storing the container image.",
            file=sys.stderr,
        )
        sys.exit(1)
    enroot_dir = shlex.quote(remote_dir.rstrip("/") + "/.enroot")

    srun_pfx = f"srun --jobid={shlex.quote(job_id)} --overlap"
    qname = shlex.quote(container_name)
    sqsh = f"{enroot_dir}/{qname}.sqsh"

    # ── Step 1: Import image ──
    # Check if sqsh already exists (on the compute node)
    check = _ssh_run_capture(
        remote, f"{srun_pfx} test -f {sqsh} && echo yes || echo no",
    )
    already_imported = "yes" in check.stdout

    if already_imported and not force:
        print(f"[1/3] Image already imported, skipping (use --force to re-import)")
    else:
        print(f"[1/3] Importing container image: {image}")
        rc = _ssh_run_interactive(
            remote,
            f"{srun_pfx} bash -c "
            + shlex.quote(
                f"mkdir -p {enroot_dir} && "
                f"enroot import --output {sqsh} docker://{image}"
            ),
        )
        if rc != 0:
            print("Image import failed.", file=sys.stderr)
            sys.exit(rc)

    # ── Step 2: Create container ──
    print(f"[2/3] Creating container: {container_name}")
    rc = _ssh_run_interactive(
        remote,
        f"{srun_pfx} bash -c "
        + shlex.quote(
            f"enroot remove --force {qname} 2>/dev/null || true; "
            f"enroot create --name {qname} {sqsh}"
        ),
    )
    if rc != 0:
        print("Container creation failed.", file=sys.stderr)
        sys.exit(rc)

    # ── Step 3: Run setup script ──
    if setup_script:
        print(f"[3/3] Running setup script: {setup_script}")
        enroot_cmd = _build_enroot_start_cmd(
            {"name": container_name, "mounts": mounts, "env": env_vars},
            [setup_script],
        )
        rc = _ssh_run_interactive(remote, f"{srun_pfx} {enroot_cmd}")
        if rc != 0:
            print("Setup script failed.", file=sys.stderr)
            sys.exit(rc)
    else:
        print("[3/3] No setup script specified, skipping.")

    print(f"Container '{container_name}' is ready. Use 'remwork-cli run' to execute commands.")


# ── run ──────────────────────────────────────────────────────────────────────

def run_cmd(
    remote_name: Optional[str],
    config_path: Optional[str],
    job_id: Optional[str],
    user_cmd: List[str],
    wait: bool = False,
    no_container: bool = False,
) -> None:
    """Run a command on an existing Slurm allocation via srun."""
    ws = load_workspace_config()
    name = resolve_remote(remote_name, ws)

    if not job_id:
        job_id = load_allocation(name)
    if not job_id:
        print(
            f"No active allocation for remote '{name}'. "
            "Run 'remwork-cli allocate' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not user_cmd:
        print(
            "No command specified. Usage: remwork-cli run [remote] -- <command>",
            file=sys.stderr,
        )
        sys.exit(1)

    data = load_config(config_path)
    remote = find_remote(name, data)

    # Verify the allocation is still alive and running
    state = _check_stale_allocation(remote, name, job_id)
    if state is None:
        print(
            f"Allocation job {job_id} on '{name}' is no longer active "
            "(completed, cancelled, or timed out).",
            file=sys.stderr,
        )
        print("Run 'remwork-cli allocate' to create a new one.", file=sys.stderr)
        sys.exit(1)

    if state in _SLURM_QUEUED_STATES:
        if wait:
            wait_for_running(remote, job_id, name)
        else:
            print(
                f"Allocation job {job_id} is {state} (still queued).",
                file=sys.stderr,
            )
            print(
                "Pass --wait to block until running, or check back later.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif state not in _SLURM_RUNNING_STATES:
        print(
            f"Allocation job {job_id} is in unexpected state: {state}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve the working directory for the command.
    # Inside a container the remote directory is mounted at /workspace,
    # so the project lives at /workspace/<folder>.
    # On the bare host the full path is <directory>/<folder>.
    folder = ws.get("folder")
    container_conf = ws.get("container", {})
    use_container = bool(container_conf.get("name")) and not no_container

    if use_container:
        workdir = "/workspace/" + folder.strip("/") if folder else "/workspace"
    else:
        host_dir = get_remote_directory(remote, data)
        if host_dir and folder:
            workdir = host_dir.rstrip("/") + "/" + folder.strip("/")
        elif host_dir:
            workdir = host_dir
        else:
            workdir = None

    # Build the user command, prepending cd if we have a working dir.
    # The cd must happen in the same shell context as the command — i.e.
    # *inside* the container when enroot is used, not outside it.
    if workdir:
        shell_cmd = (
            f"cd {shlex.quote(workdir)} && "
            + " ".join(shlex.quote(a) for a in user_cmd)
        )
        actual_cmd = ["bash", "-c", shell_cmd]
    else:
        actual_cmd = list(user_cmd)

    # Wrap in enroot container if configured
    if use_container:
        effective_cmd = _build_enroot_start_cmd(container_conf, actual_cmd)
        display = f"(container: {container_conf['name']}) {' '.join(user_cmd)}"
    else:
        effective_cmd = " ".join(shlex.quote(a) for a in actual_cmd)
        display = " ".join(user_cmd)

    srun_cmd = f"srun --jobid={shlex.quote(job_id)} --overlap {effective_cmd}"

    if workdir:
        print(f"Running on job {job_id} in {workdir}: {display}")
    else:
        print(f"Running on job {job_id}: {display}")
    rc = _ssh_run_interactive(remote, srun_cmd)
    if rc != 0:
        sys.exit(rc)


# ── status ───────────────────────────────────────────────────────────────────

def show_status(
    remote_name: Optional[str],
    config_path: Optional[str],
) -> None:
    """Show status of active Slurm allocations."""
    allocs = load_all_allocations()
    if not allocs:
        print("No active allocations.")
        return

    # If a specific remote is requested, filter to just that one
    if remote_name:
        targets = (
            {remote_name: allocs[remote_name]} if remote_name in allocs else {}
        )
        if not targets:
            print(f"No active allocation for remote '{remote_name}'.", file=sys.stderr)
            sys.exit(1)
    else:
        targets = dict(allocs)

    data = load_config(config_path)
    remotes_map = {r["name"]: r for r in data["remotes"] if r.get("name")}

    for rname, info in list(targets.items()):
        job_id = info.get("job_id", "?")
        if rname not in remotes_map:
            print(f"[{rname}] job {job_id} (remote no longer in config)")
            continue
        remote = remotes_map[rname]

        # Quick state check first
        state = query_job_state(remote, job_id)
        if state is None:
            print(
                f"[{rname}] job {job_id}: no longer in scheduler "
                "(completed, cancelled, or timed out) — cleaned up"
            )
            clear_allocation(rname)
            continue

        # Detailed view
        squeue_cmd = (
            f"squeue --job={shlex.quote(job_id)} "
            f"--Format=JobID:12,State:12,Partition:14,NumNodes:6,Gres:14,TimeUsed:12,TimeLimit:12 "
            f"--noheader"
        )
        result = _ssh_run_capture(remote, squeue_cmd)
        output = result.stdout.strip()
        header = f"{'JobID':<12} {'State':<12} {'Partition':<14} {'Nodes':<6} {'Gres':<14} {'Used':<12} {'Limit':<12}"
        print(f"[{rname}]")
        print(header)
        if output:
            print(output)
        else:
            print(f"  {job_id:<12} {state:<12} (detail unavailable)")


# ── release ──────────────────────────────────────────────────────────────────

def release(
    remote_name: Optional[str],
    config_path: Optional[str],
) -> None:
    """Cancel a Slurm allocation and remove its state."""
    ws = load_workspace_config()
    name = resolve_remote(remote_name, ws)

    job_id = load_allocation(name)
    if not job_id:
        print(f"No active allocation for remote '{name}'.", file=sys.stderr)
        sys.exit(1)

    data = load_config(config_path)
    remote = find_remote(name, data)

    scancel_cmd = f"scancel {shlex.quote(job_id)}"
    print(f"Cancelling job {job_id} on '{name}'...")
    result = _ssh_run_capture(remote, scancel_cmd)
    # scancel may return non-zero if job already finished — that's fine
    clear_allocation(name)
    if result.returncode == 0:
        print(f"Released job {job_id}.")
    else:
        print(f"scancel exited {result.returncode} (job may have already ended). State cleaned up.")


# ── export-ssh-config ────────────────────────────────────────────────────────

def _parse_ssh_config(path: str) -> List[dict]:
    """Parse OpenSSH config file into a list of host blocks (one dict per Host block)."""
    path = expand_path(path)
    if not os.path.isfile(path):
        return []
    blocks = []
    current = None
    with open(path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            while line.endswith("\\"):
                line = line[:-1].strip() + " " + (f.readline().strip() if f else "")
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            key = (parts[0] if parts else "").lower()
            value = (parts[1] if len(parts) > 1 else "").strip().strip('"')
            if key == "host":
                current = {
                    "host": value.split()[0] if value else "",
                    "hostname": "",
                    "user": "",
                    "identityfile": "",
                    "port": "",
                }
                blocks.append(current)
            elif current is not None and key in (
                "hostname",
                "user",
                "identityfile",
                "port",
            ):
                current[key] = value
    return blocks


def export_ssh_config(
    ssh_config_path: Optional[str],
    output_config_path: Optional[str],
    default_directory: str,
) -> None:
    """Export ~/.ssh/config hosts into remotes.json."""
    ssh_path = expand_path(ssh_config_path or DEFAULT_SSH_CONFIG)
    if not os.path.isfile(ssh_path):
        print(f"SSH config not found: {ssh_path}", file=sys.stderr)
        sys.exit(1)
    blocks = _parse_ssh_config(ssh_path)
    remotes = []
    for b in blocks:
        name = (b.get("host") or "").strip()
        if not name or name == "*":
            continue
        hostname = (b.get("hostname") or name).strip()
        user = (b.get("user") or "").strip()
        key = (b.get("identityfile") or "").strip()
        port_s = (b.get("port") or "").strip()
        port = int(port_s) if port_s.isdigit() else 22
        remotes.append(
            {
                "name": name,
                "host": hostname,
                "directory": default_directory if default_directory else None,
                "username": user or None,
                "key": key or None,
                "port": port if port != 22 else None,
            }
        )
    for r in remotes:
        for k in list(r):
            if r[k] is None:
                del r[k]
    out_path = output_config_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    out_path = expand_path(out_path)
    data = {"remotes": remotes}
    if default_directory:
        data["default_directory"] = default_directory
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Exported {len(remotes)} host(s) to {out_path}")
    if not default_directory:
        print(
            "Set directory for each remote (e.g. /home/you/projects) before using upload.",
            file=sys.stderr,
        )


# ── edit ─────────────────────────────────────────────────────────────────────

def edit_config(config_path: Optional[str]) -> None:
    """Open the config file in vim (or $EDITOR)."""
    path = _resolve_config_path(config_path)
    editor = os.environ.get("EDITOR", "vim")
    rc = subprocess.call([editor, path])
    if rc != 0:
        sys.exit(rc)


# ── Shell completion ─────────────────────────────────────────────────────────

COMMANDS = ("list", "edit", "upload", "allocate", "setup", "run", "status", "release", "export-ssh-config")

# Commands whose first positional arg is a remote name
_REMOTE_ARG_CMDS = {"upload", "allocate", "setup", "run", "status", "release"}


def _get_remote_names(config_path: Optional[str]) -> List[str]:
    """Return list of remote names from config; empty list on error."""
    data = _load_config_safe(config_path)
    if not data:
        return []
    return [r.get("name", "") for r in data.get("remotes", []) if r.get("name")]


def get_completions(words: List[str], cword: int, prefix: str) -> List[str]:
    """Return completions for the given word index and prefix."""
    if cword < 0 or cword >= len(words):
        return []
    effective = []
    effective_to_word = []
    i = 0
    config_path = None
    while i < len(words):
        if words[i] in ("-c", "--config") and i + 1 < len(words):
            config_path = words[i + 1]
            i += 2
            continue
        if words[i] in ("-h", "--help"):
            i += 1
            continue
        effective_to_word.append(i)
        effective.append(words[i])
        i += 1
    if cword not in effective_to_word:
        return []
    eff_idx = effective_to_word.index(cword)
    candidates = []
    if eff_idx == 1:
        candidates = [c for c in COMMANDS if c.startswith(prefix)]
    elif eff_idx == 2 and len(effective) > 1 and effective[1] in _REMOTE_ARG_CMDS:
        candidates = [n for n in _get_remote_names(config_path) if n.startswith(prefix)]
    return sorted(candidates)


def run_completion() -> bool:
    """If COMP_LINE/COMP_POINT set (bash), print completions and return True."""
    comp_line = os.environ.get("COMP_LINE", "")
    comp_point_s = os.environ.get("COMP_POINT", "0")
    try:
        comp_point = int(comp_point_s)
    except ValueError:
        return False
    if comp_point > len(comp_line):
        comp_point = len(comp_line)
    line_before = comp_line[:comp_point]
    words = line_before.split()
    if line_before.endswith(" "):
        prefix = ""
        words.append("")
        cword = len(words) - 1
    else:
        cword = len(words) - 1
        prefix = words[-1] if words else ""
    completions = get_completions(words, cword, prefix)
    for c in completions:
        print(c)
    return True


# ── main ─────────────────────────────────────────────────────────────────────

def _add_slurm_flags(parser: argparse.ArgumentParser) -> None:
    """Add the common Slurm resource flags to a subparser."""
    parser.add_argument("--partition", "-p", metavar="PART", help="Slurm partition")
    parser.add_argument("--account", "-A", metavar="ACCT", help="Slurm account/project")
    parser.add_argument("--nodes", "-N", type=int, metavar="N", help="Number of nodes")
    parser.add_argument(
        "--gpus", "-G", type=int, metavar="N", help="Number of GPUs (--gres=gpu:N)"
    )
    parser.add_argument(
        "--gpus-per-node", type=int, metavar="N", help="GPUs per node"
    )
    parser.add_argument(
        "--ntasks-per-node", type=int, metavar="N", help="Tasks per node"
    )
    parser.add_argument(
        "--time", "-t", metavar="TIME", dest="time_limit", help="Time limit (e.g. 2:00:00)"
    )
    parser.add_argument("--job-name", "-J", metavar="NAME", help="Job name")
    parser.add_argument("--cpus-per-task", type=int, metavar="N", help="CPUs per task")
    parser.add_argument("--mem", metavar="MEM", help="Memory (e.g. 32G)")
    parser.add_argument(
        "--extra-args", metavar="ARGS", help="Additional Slurm arguments (quoted string)"
    )


def main() -> None:
    # Zsh completion: called as remwork_cli.py __complete_zsh [words...]
    if len(sys.argv) >= 2 and sys.argv[1] == "__complete_zsh":
        words = sys.argv[2:]
        cword = len(words) - 1 if words else 0
        prefix = words[-1] if words else ""
        config_path = None
        i = 0
        while i < len(words):
            if words[i] in ("-c", "--config") and i + 1 < len(words):
                config_path = words[i + 1]
                i += 2
                continue
            i += 1
        for c in get_completions(words, cword, prefix):
            print(c)
        sys.exit(0)

    # Bash completion: COMP_LINE and COMP_POINT set by complete -C
    if os.environ.get("COMP_LINE") is not None:
        run_completion()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="Remote work CLI: upload directories and allocate resources on remote targets (rsync + Slurm)."
    )
    parser.add_argument(
        "--config",
        "-c",
        metavar="FILE",
        help=f"Config JSON (default: {CONFIG_ENV} or {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--install-completion",
        metavar="SHELL",
        choices=("bash", "zsh"),
        help="Print shell completion setup for bash or zsh",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # ── list ──
    subparsers.add_parser("list", help="List configured remotes").set_defaults(
        func=lambda a: list_remotes(a.config)
    )

    # ── edit ──
    subparsers.add_parser(
        "edit", help="Edit config file in vim (or $EDITOR)"
    ).set_defaults(func=lambda a: edit_config(a.config))

    # ── upload ──
    up = subparsers.add_parser("upload", help="Upload current directory to a remote")
    up.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name from config (default: .remworkconf 'remote')",
    )
    up.add_argument(
        "folder",
        metavar="FOLDER",
        nargs="?",
        default=None,
        help="Folder name under the remote directory (default: .remworkconf 'folder')",
    )
    up.add_argument(
        "--dir",
        "-d",
        metavar="DIR",
        default=None,
        help="Local directory to upload (default: current directory)",
    )
    up.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be transferred",
    )
    up.set_defaults(
        func=lambda a: upload(
            a.remote, a.config, a.dir, a.folder, getattr(a, "dry_run", False)
        )
    )

    # ── allocate ──
    alloc = subparsers.add_parser(
        "allocate",
        help="Allocate resources on a remote via Slurm (persistent hold job)",
    )
    alloc.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name from config (default: .remworkconf 'remote')",
    )
    alloc.add_argument(
        "--wait",
        "-w",
        action="store_true",
        help="Block until the allocation is RUNNING (useful when the job is queued)",
    )
    _add_slurm_flags(alloc)
    alloc.set_defaults(
        func=lambda a: allocate(
            a.remote,
            a.config,
            wait=a.wait,
            partition=a.partition,
            account=a.account,
            nodes=a.nodes,
            gpus=a.gpus,
            gpus_per_node=a.gpus_per_node,
            ntasks_per_node=a.ntasks_per_node,
            time_limit=a.time_limit,
            job_name=a.job_name,
            cpus_per_task=a.cpus_per_task,
            mem=a.mem,
            extra_args=a.extra_args,
        )
    )

    # ── setup ──
    setup_p = subparsers.add_parser(
        "setup",
        help="Set up an enroot container on the allocated remote",
    )
    setup_p.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name (default: .remworkconf 'remote')",
    )
    setup_p.add_argument(
        "--image",
        metavar="URI",
        default=None,
        help="Container image URI (e.g. nvcr.io/nvidia/pytorch:24.01-py3)",
    )
    setup_p.add_argument(
        "--name",
        metavar="NAME",
        dest="container_name",
        default=None,
        help="Container name for enroot",
    )
    setup_p.add_argument(
        "--setup-script",
        metavar="PATH",
        default=None,
        help="Script to run inside the container for environment setup",
    )
    setup_p.add_argument(
        "--force",
        action="store_true",
        help="Re-import the container image even if already cached",
    )
    setup_p.set_defaults(
        func=lambda a: setup_container(
            a.remote,
            a.config,
            image=a.image,
            container_name=a.container_name,
            setup_script=a.setup_script,
            force=a.force,
        )
    )

    # ── run ──
    run_p = subparsers.add_parser(
        "run",
        help="Run a command on an existing allocation (srun --jobid)",
    )
    run_p.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name (default: .remworkconf 'remote')",
    )
    run_p.add_argument(
        "--jobid",
        metavar="ID",
        default=None,
        help="Slurm job ID (default: read from .remwork_allocation)",
    )
    run_p.add_argument(
        "--wait",
        "-w",
        action="store_true",
        help="Block until the allocation is RUNNING before executing",
    )
    run_p.add_argument(
        "--no-container",
        action="store_true",
        help="Run directly on the node, bypassing the enroot container",
    )
    run_p.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command to run (after '--')",
    )
    run_p.set_defaults(
        func=lambda a: run_cmd(
            a.remote,
            a.config,
            a.jobid,
            # strip leading '--' from REMAINDER
            a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd,
            wait=a.wait,
            no_container=a.no_container,
        )
    )

    # ── status ──
    stat = subparsers.add_parser(
        "status",
        help="Show status of active Slurm allocations",
    )
    stat.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name (default: show all allocations)",
    )
    stat.set_defaults(func=lambda a: show_status(a.remote, a.config))

    # ── release ──
    rel = subparsers.add_parser(
        "release",
        help="Cancel a Slurm allocation (scancel)",
    )
    rel.add_argument(
        "remote",
        metavar="NAME",
        nargs="?",
        default=None,
        help="Remote name (default: .remworkconf 'remote')",
    )
    rel.set_defaults(func=lambda a: release(a.remote, a.config))

    # ── export-ssh-config ──
    export_p = subparsers.add_parser(
        "export-ssh-config",
        help="Export ~/.ssh/config into remotes.json",
    )
    export_p.add_argument(
        "--ssh-config",
        metavar="FILE",
        default=None,
        help=f"SSH config file (default: {DEFAULT_SSH_CONFIG})",
    )
    export_p.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        default=None,
        help=f"Output remotes.json path (default: --config or {DEFAULT_CONFIG})",
    )
    export_p.add_argument(
        "--default-dir",
        metavar="DIR",
        default="",
        help="Default remote directory for all hosts",
    )
    export_p.set_defaults(
        func=lambda a: export_ssh_config(
            a.ssh_config,
            a.output or a.config,
            getattr(a, "default_dir", "") or "",
        )
    )

    args = parser.parse_args()

    if getattr(args, "install_completion", None):
        invoked = os.path.basename(sys.argv[0])
        use_remwork_cli = invoked == "remwork-cli"
        script = os.path.abspath(__file__)
        shell = args.install_completion
        cmd_name = "remwork-cli" if use_remwork_cli else os.path.basename(script)
        if shell == "bash":
            cmd = (
                repr("remwork-cli")
                if use_remwork_cli
                else repr(sys.executable + " " + script)
            )
            print(
                '# Add to ~/.bashrc or run: eval "$(remwork-cli --install-completion bash)"'
            )
            print("complete -C", cmd, cmd_name)
        else:
            if use_remwork_cli:
                print(
                    "# Add to ~/.zshrc or run: source <(remwork-cli --install-completion zsh)"
                )
                print(
                    "_remwork_cli() { reply=($(remwork-cli __complete_zsh ${words[@]})) }"
                )
            else:
                print(
                    "# Add to ~/.zshrc or run: source <(python3 remwork_cli.py --install-completion zsh)"
                )
                print(
                    f"_remwork_cli() {{ reply=($({sys.executable} {script} __complete_zsh ${{words[@]}})) }}"
                )
            print("compdef _remwork_cli", cmd_name)
        sys.exit(0)

    if not args.command:
        parser.print_help()
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
