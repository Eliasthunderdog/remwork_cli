# Upload CLI

Upload the current directory to remote targets using **rsync** over SSH. Targets are defined in a JSON config file; you can list them and upload to any by name.

## Setup

1. Copy the example config and edit with your remotes:

   ```bash
   cp remotes.json.example remotes.json
   ```

2. Ensure `rsync` and `ssh` are installed (standard on macOS/Linux).

## Config format (`remotes.json`)

```json
{
  "default_directory": "/home/me/projects",
  "remotes": [
    {
      "name": "my-server",
      "host": "example.com",
      "directory": "/home/me/projects",
      "username": "me",
      "key": "~/.ssh/id_ed25519",
      "port": 22
    },
    {
      "name": "other-server",
      "host": "other.com",
      "username": "me",
      "key": "~/.ssh/id_ed25519"
    }
  ]
}
```

- **default_directory**: Optional. Used when a remote has no `directory` (or it is empty).
- **name**: Label used in CLI (e.g. `upload my-server`).
- **host**: Remote hostname or IP.
- **directory**: Absolute path on the remote. If empty or omitted, `default_directory` is used.
- **username**: SSH user (optional if same as local).
- **key**: Path to SSH private key (optional; uses default SSH keys if omitted).
- **port**: SSH port (optional; default 22).

## Usage

- **List remotes**

  ```bash
  python upload_cli.py list
  python upload_cli.py -c /path/to/remotes.json list
  ```

- **Edit config file** (opens in vim, or `$EDITOR` if set)

  ```bash
  python upload_cli.py edit
  python upload_cli.py -c /path/to/remotes.json edit
  ```

- **Upload current directory to a remote**

  ```bash
  python upload_cli.py upload my-server
  python upload_cli.py upload my-server my-project
  ```
  With a **folder** name (e.g. `my-project`), files go to `<remote directory>/my-project`. Without it, files go directly into the remote directory.

  ```bash
  python upload_cli.py upload my-server my-project --dir /path/to/local/dir
  python upload_cli.py upload my-server my-project --dry-run
  ```

- **Config file**

  - Default: `remotes.json` in the current directory.
  - Override: `--config` / `-c` or environment variable `UPLOAD_CONFIG`.

## Run as executable

```bash
chmod +x upload_cli.py
./upload_cli.py list
./upload_cli.py upload my-server
```

Or from another directory:

```bash
UPLOAD_CONFIG=/path/to/remotes.json python /path/to/upload_cli.py list
```

---

## Alternatives to rsync

| Tool | Pros | Cons |
|------|------|------|
| **rsync** (this CLI) | Incremental sync, only changed files, efficient over SSH, resume-friendly | Requires rsync on both sides |
| **scp** | Simple, no extra daemon | Copies everything each time; no incremental |
| **sftp** | Standard, scriptable | No built-in “sync”; you must walk dirs and compare |
| **rclone** | Many backends (S3, GCS, SFTP, etc.), optional encryption | Extra binary; config is different from “host + path” |
| **Paramiko (SFTP in Python)** | Pure Python, no rsync/scp needed | You implement sync logic and progress; slower for big trees |

**Recommendation:** For “upload this directory to a remote path over SSH,” **rsync** is usually best (speed, incremental, bandwidth). Use **scp** only for one-off or very small uploads. Use **rclone** if you need cloud storage or many backends; use **Paramiko** if you must avoid external binaries.
