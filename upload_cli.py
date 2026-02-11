#!/usr/bin/env python3
"""
Upload current directory to remote targets via rsync.
Configuration: JSON file with remote host, directory, username, and SSH key paths.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


CONFIG_ENV = "UPLOAD_CONFIG"
DEFAULT_CONFIG = "./remotes.json"
DEFAULT_SSH_CONFIG = "~/.ssh/config"


def expand_path(path: str) -> str:
    """Expand ~ and environment variables in path."""
    return os.path.expanduser(os.path.expandvars(path))


def load_config(config_path: Optional[str]) -> dict:
    """Load remotes config from JSON file."""
    path = config_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    path = expand_path(path)
    if not os.path.isfile(path):
        print(f"Config file not found: {path}", file=sys.stderr)
        print(
            f"Set {CONFIG_ENV} or pass --config, or place {DEFAULT_CONFIG} in current dir.",
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


def build_rsync_cmd(remote: dict, local_dir: str, dry_run: bool) -> List[str]:
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
        "-e",
        ssh_cmd,
        "--rsync-path",
        rsync_path,
        f"{local_dir.rstrip(os.sep)}{os.sep}",
        dest + "/",
    ]
    if dry_run:
        args.insert(args.index("rsync") + 1, "--dry-run")
        args.insert(args.index("rsync") + 2, "-v")
    return args


def upload(
    remote_name: str,
    config_path: Optional[str],
    local_dir: Optional[str],
    folder: Optional[str],
    dry_run: bool,
) -> None:
    """Upload current (or given) directory to the named remote via rsync."""
    data = load_config(config_path)
    remotes = {r["name"]: r for r in data["remotes"] if r.get("name")}
    if remote_name not in remotes:
        print(f"Unknown remote: {remote_name}", file=sys.stderr)
        print("Available:", ", ".join(remotes.keys()) or "none", file=sys.stderr)
        sys.exit(1)
    remote = remotes[remote_name]
    effective_dir = get_remote_directory(remote, data)
    if not effective_dir:
        print("No directory set for this remote and no default_directory in config.", file=sys.stderr)
        sys.exit(1)
    if folder:
        effective_dir = effective_dir.rstrip("/") + "/" + folder.strip("/")
    remote_with_dir = {**remote, "directory": effective_dir}
    local = expand_path(local_dir or os.getcwd())
    if not os.path.isdir(local):
        print(f"Not a directory: {local}", file=sys.stderr)
        sys.exit(1)
    cmd = build_rsync_cmd(remote_with_dir, local, dry_run)
    if dry_run:
        print("Dry run. Would execute:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(rc)


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
            # continuation
            while line.endswith("\\"):
                line = line[:-1].strip() + " " + (f.readline().strip() if f else "")
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            key = (parts[0] if parts else "").lower()
            value = (parts[1] if len(parts) > 1 else "").strip().strip('"')
            if key == "host":
                current = {"host": value.split()[0] if value else "", "hostname": "", "user": "", "identityfile": "", "port": ""}
                blocks.append(current)
            elif current is not None and key in ("hostname", "user", "identityfile", "port"):
                current[key] = value
    return blocks


def export_ssh_config(
    ssh_config_path: Optional[str],
    output_config_path: Optional[str],
    default_directory: str,
) -> None:
    """Export ~/.ssh/config hosts into remotes.json. Directory is not in SSH config so a default is used."""
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
        remotes.append({
            "name": name,
            "host": hostname,
            "directory": default_directory if default_directory else None,
            "username": user or None,
            "key": key or None,
            "port": port if port != 22 else None,
        })
    # Drop None values for cleaner JSON
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
        print("Set directory for each remote (e.g. /home/you/projects) before using upload.", file=sys.stderr)


def edit_config(config_path: Optional[str]) -> None:
    """Open the config file in vim (or $EDITOR)."""
    path = config_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    path = expand_path(path)
    editor = os.environ.get("EDITOR", "vim")
    rc = subprocess.call([editor, path])
    if rc != 0:
        sys.exit(rc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload current directory to remote targets (rsync)."
    )
    parser.add_argument(
        "--config",
        "-c",
        metavar="FILE",
        help=f"Config JSON (default: {CONFIG_ENV} or {DEFAULT_CONFIG})",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    list_p = subparsers.add_parser("list", help="List configured remotes")
    list_p.set_defaults(func=lambda a: list_remotes(a.config))

    edit_p = subparsers.add_parser("edit", help="Edit config file in vim (or $EDITOR)")
    edit_p.set_defaults(func=lambda a: edit_config(a.config))

    up = subparsers.add_parser("upload", help="Upload current directory to a remote")
    up.add_argument(
        "remote",
        metavar="NAME",
        help="Remote name from config",
    )
    up.add_argument(
        "folder",
        metavar="FOLDER",
        nargs="?",
        default=None,
        help="Folder name under the remote directory to use as upload target (e.g. project-name)",
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
        help="Default remote directory for all hosts (not in SSH config); edit per host later if needed",
    )
    export_p.set_defaults(
        func=lambda a: export_ssh_config(
            a.ssh_config,
            a.output or a.config,
            getattr(a, "default_dir", "") or "",
        )
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
