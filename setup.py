"""Install remwork-cli with default config dir and optional shell completion."""
from setuptools import setup
from setuptools.command.install import install
import os
import json


CONFIG_DIR = os.path.expanduser("~/.local/remwork_cli")
DEFAULT_REMOTES = {"default_directory": "", "remotes": []}


def ensure_config_dir():
    """Create ~/.local/remwork_cli and default remotes.json if missing."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    config_path = os.path.join(CONFIG_DIR, "remotes.json")
    if not os.path.isfile(config_path):
        with open(config_path, "w") as f:
            json.dump(DEFAULT_REMOTES, f, indent=2)
        print(f"Created default config: {config_path}")


def add_completion_to_shell(shell, line_or_lines, marker):
    """Append completion block to .bashrc or .zshrc if not already present."""
    rc = os.path.expanduser(f"~/.{shell}rc")
    if not os.path.isfile(rc):
        return False
    try:
        with open(rc, "r") as f:
            content = f.read()
    except OSError:
        return False
    if marker in content:
        return False
    lines = line_or_lines if isinstance(line_or_lines, list) else [line_or_lines]
    block = "\n# remwork-cli completion\n" + "\n".join(lines) + "\n"
    try:
        with open(rc, "a") as f:
            f.write(block)
        return True
    except OSError:
        return False


class install_with_setup(install):
    """Create config dir, default remotes.json, and configure shell completion."""

    def run(self):
        install.run(self)
        ensure_config_dir()
        bash_line = "complete -C remwork-cli remwork-cli"
        if add_completion_to_shell("bash", bash_line, "complete -C remwork-cli"):
            print("Added remwork-cli completion to ~/.bashrc")
        zsh_lines = [
            "if command -v remwork-cli &>/dev/null; then",
            "  _remwork_cli() { reply=($(remwork-cli __complete_zsh ${words[@]})) }",
            "  compdef _remwork_cli remwork-cli",
            "fi",
        ]
        if add_completion_to_shell("zsh", zsh_lines, "compdef _remwork_cli"):
            print("Added remwork-cli completion to ~/.zshrc")
        print(f"\nConfig directory: {CONFIG_DIR}")
        print("Run 'remwork-cli edit' to edit remotes, or 'remwork-cli export-ssh-config -o ~/.local/remwork_cli/remotes.json' to import from SSH config.")


setup(
    name="remwork-cli",
    version="0.2.0",
    description="Remote work CLI: upload directories and allocate resources on remote targets (rsync + Slurm)",
    author="remwork-cli",
    py_modules=["remwork_cli"],
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "remwork-cli=remwork_cli:main",
        ],
    },
    cmdclass={"install": install_with_setup},
)
