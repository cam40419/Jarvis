"""Standard-library-only Git guard, executed inside the leased container.

The transport sends this source to an isolated Python interpreter. Nothing here
is executed on the Simon host by the production transport.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def relative_path(value: Any, *, allow_dot: bool = True) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("Expected a bounded relative path")
    if value == "." and allow_dot:
        return value
    if (
        value.startswith(("/", "-")) or "\\" in value or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or part.casefold() == ".git"
               for part in value.split("/"))
    ):
        raise ValueError("Paths must stay inside the repository and exclude Git metadata")
    return value


def branch_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", value) is None
        or ".." in value or "//" in value or value.endswith((".", "/"))
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        or value == "HEAD"
    ):
        raise ValueError("Expected a simple branch name")
    return value


def checked_path(root: Path, relative: str) -> Path:
    candidate = root
    for part in relative.split("/"):
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Symbolic links cannot be used as repository paths")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Repository path escaped its workspace")
    return candidate


def check_repository(repository: Path) -> None:
    metadata = repository / ".git"
    if metadata.is_symlink() or not metadata.is_dir():
        raise ValueError("A standalone repository with a local .git directory is required")
    # Git follows metadata paths independently of the worktree. Reject linked
    # worktrees, alternates, symlinks, and oversized metadata before invoking Git.
    forbidden = {"commondir", "config.worktree", "objects/info/alternates",
                 "objects/info/http-alternates"}
    count = 0
    for parent, directories, files in os.walk(metadata, followlinks=False):
        for name in (*directories, *files):
            count += 1
            child = Path(parent) / name
            if count > 100000 or child.is_symlink():
                raise ValueError("Repository metadata is unsupported or too large")
            if child.relative_to(metadata).as_posix() in forbidden:
                raise ValueError("Linked repositories and alternate object stores are unsupported")
    config = metadata / "config"
    if not config.is_file() or config.stat().st_size > 65536:
        raise ValueError("Repository configuration is missing or too large")
    parser = configparser.RawConfigParser(strict=True)
    parser.read_string(config.read_text(encoding="utf-8"))
    if parser.defaults():
        raise ValueError("Unsupported repository configuration")
    allowed = {
        "core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates",
                 "ignorecase", "precomposeunicode", "symlinks", "autocrlf", "safecrlf"},
        "user": {"name", "email"},
        "extensions": {"objectformat"},
    }
    for section in parser.sections():
        normalized = section.lower()
        keys = allowed.get(normalized)
        if re.fullmatch(r'remote "[A-Za-z0-9_-]+"', section):
            keys = {"url", "fetch"}
        elif re.fullmatch(r'branch "[A-Za-z0-9._/-]+"', section):
            keys = {"remote", "merge", "rebase"}
        if keys is None or not set(parser[section]) <= keys:
            raise ValueError("Repository config contains unsupported executable/external options")
        if normalized == "core" and parser.get(section, "bare", fallback="false") != "false":
            raise ValueError("Bare repositories are unsupported")


def validate_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "status": set(), "diff": {"staged", "paths"}, "log": {"limit"},
        "branches": set(), "init": {"branch"}, "branch": {"name"},
        "switch": {"name"}, "add": {"paths"}, "commit": {"message"},
    }
    if operation not in fields or set(arguments) - fields[operation] - {"repository"}:
        raise ValueError("Unsupported Git operation or arguments")
    result = dict(arguments)
    result["repository"] = relative_path(arguments.get("repository", "."))
    if operation in {"branch", "switch"}:
        result["name"] = branch_name(arguments.get("name"))
    if operation == "init":
        result["branch"] = branch_name(arguments.get("branch", "main"))
    if operation in {"diff", "add"}:
        paths = arguments.get("paths", [])
        if (not isinstance(paths, list) or len(paths) > 50
                or (operation == "add" and not paths)):
            raise ValueError("Expected between one and fifty explicit paths to stage")
        result["paths"] = [relative_path(path) for path in paths]
    if operation == "diff":
        staged = arguments.get("staged", False)
        if type(staged) is not bool:
            raise ValueError("staged must be boolean")
        result["staged"] = staged
    if operation == "log":
        limit = arguments.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History limit must be between one and one hundred")
        result["limit"] = limit
    if operation == "commit":
        message = arguments.get("message")
        if (not isinstance(message, str) or not message.strip() or len(message) > 4000
                or any(ord(char) < 32 and char not in "\n\t" for char in message)):
            raise ValueError("Commit message must be nonempty text up to 4000 characters")
    return result


def git_command(
    operation: str, arguments: dict[str, Any], repository: Path, *,
    executable: str = "/usr/bin/git", author_name: str, author_email: str,
) -> list[str]:
    prefix = [
        executable, "--no-pager", "--literal-pathspecs", "--no-optional-locks",
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
        "-c", "gc.auto=0", "-c", "maintenance.auto=false",
        "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
        "-c", "core.quotePath=true", "-c", "color.ui=false",
        "-c", "user.name=" + author_name, "-c", "user.email=" + author_email,
    ]
    if operation == "init":
        return [*prefix, "init", "--template=", "--initial-branch=" + arguments["branch"],
                "--", str(repository)]
    prefix += ["--git-dir=" + str(repository / ".git"), "--work-tree=" + str(repository)]
    if operation == "status":
        return [*prefix, "status", "--short", "--branch", "--untracked-files=normal",
                "--ignore-submodules=all"]
    if operation == "diff":
        return [*prefix, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all",
                *(["--cached"] if arguments["staged"] else []), "--", *arguments["paths"]]
    if operation == "log":
        return [*prefix, "log", "--no-decorate", "--no-show-signature",
                "--format=%h %s", "-n", str(arguments["limit"]), "--"]
    if operation == "branches":
        return [*prefix, "branch", "--list", "--no-color"]
    if operation == "branch":
        return [*prefix, "branch", "--no-track", "--", arguments["name"]]
    if operation == "switch":
        return [*prefix, "switch", "--no-guess", "--", arguments["name"]]
    if operation == "add":
        return [*prefix, "add", "--", *arguments["paths"]]
    if operation == "commit":
        return [*prefix, "commit", "--no-gpg-sign", "--no-verify", "--cleanup=verbatim",
                "-m", arguments["message"], "--"]
    raise ValueError("Unsupported Git operation")


def run(request: dict[str, Any], workspace: Path, *, executable: str = "/usr/bin/git") -> int:
    operation = request["operation"]
    arguments = validate_arguments(operation, request["arguments"])
    repository = checked_path(workspace, arguments["repository"])
    if operation == "init":
        if (repository / ".git").exists() or (repository / ".git").is_symlink():
            raise ValueError("Repository already exists; init never overwrites repository metadata")
        repository.mkdir(parents=True, exist_ok=True)
    else:
        check_repository(repository)
    for path in arguments.get("paths", []):
        checked_path(repository, path)
    command = git_command(
        operation, arguments, repository, executable=executable,
        author_name=request["author_name"], author_email=request["author_email"],
    )
    # Explicit environment removes inherited config, credentials, askpass, editors,
    # shell helpers, replacement object refs, and global/system configuration.
    environment = {
        "PATH": str(Path(executable).parent), "HOME": str(workspace), "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": "", "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_AUTHOR_NAME": request["author_name"], "GIT_AUTHOR_EMAIL": request["author_email"],
        "GIT_COMMITTER_NAME": request["author_name"],
        "GIT_COMMITTER_EMAIL": request["author_email"],
    }
    if os.name == "nt":  # Enables the same guard to be tested with local Git for Windows.
        environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    try:
        result = subprocess.run(
            command, cwd=repository, env=environment, stdin=subprocess.DEVNULL,
            timeout=request["timeout_seconds"], check=False,
        )
    except subprocess.TimeoutExpired:
        print("Git command timed out; inspect repository before retrying", file=sys.stderr)
        return 124
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(run(json.loads(sys.argv[1]), Path("/workspace")))
    except (ValueError, OSError, configparser.Error):
        print("Git operation rejected: invalid path or unsupported repository configuration",
              file=sys.stderr)
        sys.exit(2)
