"""Prepare or apply a single-origin HTTPS configuration without exposing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import set_key

from simon.config import Settings
from simon.services.local_files import reject_links
from simon.services.safe_files import open_regular_nofollow


def _validate_tunnel_credentials(configuration: dict[str, Any]) -> None:
    """Recheck the exact credential file before apply and each proxy launch."""
    credentials = Path(configuration["credentials-file"])
    try:
        with open_regular_nofollow(credentials) as stream:
            content = stream.read(16385)
        if len(content) > 16384:
            raise ValueError
        data = json.loads(content)
        if (
            not isinstance(data, dict)
            or data.get("TunnelID") != configuration["tunnel"]
            or not isinstance(data.get("TunnelSecret"), str)
            or not data["TunnelSecret"]
        ):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError(
            "Tunnel credentials are missing, invalid, or do not match the tunnel",
        ) from None


def https_plan(
    origin: str,
    *,
    provider: Literal["cloudflare", "tailscale"],
    public_path: str = "",
    tunnel_id: str | None = None,
    credentials_file: Path | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(origin)
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.netloc != hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or len(hostname) > 253
        or "." not in hostname
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname) is None
        or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in hostname.split(".")
        )
        or hostname.endswith((".localhost", ".local"))
        or re.fullmatch(r"[0-9.]+", hostname)
    ):
        raise ValueError("Choose an exact HTTPS DNS origin on port 443, without a path")
    if re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)*", public_path) is None:
        raise ValueError("Use a simple path prefix such as /simon, or leave it empty")
    if provider not in {"cloudflare", "tailscale"}:
        raise ValueError("Unsupported HTTPS provider")
    settings = {
        "SIMON_ENVIRONMENT": "production",
        "SIMON_PUBLIC_ORIGIN": origin,
        "SIMON_PUBLIC_PATH": public_path,
        "SIMON_RP_ID": hostname,
        "SIMON_STORAGE_BACKEND": "postgres",
        "SIMON_DEV_LOGIN_ENABLED": "false",
    }
    plan: dict[str, Any] = {
        "version": 1,
        "provider": provider,
        "environment_updates": settings,
        "login_url": origin + public_path + "/login",
        "google_callback_url": origin + public_path + "/auth/google/callback",
        "local_health_url": "http://127.0.0.1:8000" + public_path + "/health/live",
        "health_host": hostname,
    }
    if provider == "tailscale":
        if not hostname.endswith(".ts.net") or tunnel_id or credentials_file:
            raise ValueError("Use the authenticated device's exact Tailscale HTTPS hostname")
        plan["serve_argv"] = ["tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:8000"]
    else:
        if tunnel_id is None or credentials_file is None:
            raise ValueError("Cloudflare requires an existing tunnel ID and credentials file")
        tunnel = str(UUID(tunnel_id))
        ingress: dict[str, Any] = {
            "hostname": hostname,
            "service": "http://127.0.0.1:8000",
            "originRequest": {"httpHostHeader": hostname},
        }
        if public_path:
            ingress["path"] = "^" + re.escape(public_path) + "(?:/.*)?$"
        plan["cloudflared_config"] = {
            "tunnel": tunnel,
            "credentials-file": str(credentials_file.absolute()),
            "edge-ip-version": "4",
            "metrics": "127.0.0.1:20242",
            "ingress": [ingress, {"service": "http_status:404"}],
        }
    return plan


def apply_https_plan(root: Path, plan: dict[str, Any]) -> Path:
    """Apply during an explicit maintenance window and retain a private rollback copy.

    This does not publish DNS, start a proxy, issue enrollment tokens, or alter
    credentials. The operator stops API/worker writers before invoking it.
    """
    root = root.absolute()
    reject_links(root)
    local = root / ".local"
    if not (local / "maintenance.request").is_file():
        raise ValueError("Stop services under .local/maintenance.request before applying HTTPS")
    env = root / ".env"
    reject_links(env)
    if not env.is_file():
        raise ValueError("The server .env file must already exist")
    updates = plan["environment_updates"]
    configuration = plan.get("cloudflared_config")
    # Rebuild the plan rather than trusting serialized executable/configuration fields.
    checked = https_plan(
        updates["SIMON_PUBLIC_ORIGIN"],
        provider=plan["provider"],
        public_path=updates["SIMON_PUBLIC_PATH"],
        tunnel_id=configuration["tunnel"] if configuration else None,
        credentials_file=Path(configuration["credentials-file"]) if configuration else None,
    )
    if checked != plan:
        raise ValueError("HTTPS plan was altered; generate a fresh plan")
    if configuration:
        _validate_tunnel_credentials(configuration)
    # Validate all existing configuration, retaining SecretStr values internally.
    current = Settings(_env_file=env)  # type: ignore[call-arg]
    values = current.model_dump(mode="python")
    values.update(
        environment="production",
        public_origin=updates["SIMON_PUBLIC_ORIGIN"],
        public_path=updates["SIMON_PUBLIC_PATH"],
        rp_id=updates["SIMON_RP_ID"],
        storage_backend="postgres",
        dev_login_enabled=False,
    )
    Settings(**values)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    backup = local / "https-backups" / stamp
    reject_links(backup)
    backup.mkdir(parents=True)
    shutil.copy2(env, backup / "server.env")
    config = local / "cloudflared.yml"
    plan_file = local / "https-plan.json"
    for target in (config, plan_file):
        reject_links(target)
        if target.exists():
            shutil.copy2(target, backup / target.name)
    # Stage complete files on the same filesystem before replacing the originals.
    with TemporaryDirectory(prefix="https-stage-", dir=local) as temporary:
        stage = Path(temporary)
        staged_env = stage / "server.env"
        shutil.copy2(env, staged_env)
        for key, value in updates.items():
            set_key(staged_env, key, value)
        staged_plan = stage / "https-plan.json"
        staged_plan.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        replacements = [(staged_plan, plan_file), (staged_env, env)]
        if configuration:
            staged_config = stage / "cloudflared.yml"
            # JSON is valid YAML and avoids untrusted YAML tags or interpolation.
            staged_config.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")
            replacements.insert(0, (staged_config, config))
        replaced = []
        try:
            for staged, target in replacements:
                os.replace(staged, target)
                replaced.append(target)
        except OSError:
            for target in reversed(replaced):
                saved = backup / ("server.env" if target == env else target.name)
                if saved.exists():
                    shutil.copy2(saved, target)
                else:
                    target.unlink()
            raise
    return backup


def validate_applied_https(root: Path) -> dict[str, Any]:
    """Check the persisted plan, production settings and proxy agree before launch."""
    root = root.absolute()
    plan_file = root / ".local" / "https-plan.json"
    reject_links(plan_file)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    updates = plan["environment_updates"]
    configuration = plan.get("cloudflared_config")
    checked = https_plan(
        updates["SIMON_PUBLIC_ORIGIN"],
        provider=plan["provider"],
        public_path=updates["SIMON_PUBLIC_PATH"],
        tunnel_id=configuration["tunnel"] if configuration else None,
        credentials_file=Path(configuration["credentials-file"]) if configuration else None,
    )
    if checked != plan:
        raise ValueError("The saved HTTPS plan is invalid")
    env = root / ".env"
    reject_links(env)
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    for key, expected in updates.items():
        actual = getattr(settings, key.removeprefix("SIMON_").lower())
        normalized = str(actual).lower() if isinstance(actual, bool) else str(actual)
        if normalized != expected:
            raise ValueError("Server settings and the HTTPS plan disagree")
    if configuration:
        _validate_tunnel_credentials(configuration)
        config = root / ".local" / "cloudflared.yml"
        reject_links(config)
        if json.loads(config.read_text(encoding="utf-8")) != configuration:
            raise ValueError("The proxy configuration and HTTPS plan disagree")
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("cloudflare", "tailscale"))
    parser.add_argument("--origin")
    parser.add_argument("--public-path", default="")
    parser.add_argument("--tunnel-id")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        if args.check:
            validate_applied_https(args.root)
            print("HTTPS origin, settings and proxy configuration agree")
            return
        if not args.provider or not args.origin:
            raise ValueError("Provider and origin are required when generating a plan")
        plan = https_plan(
            args.origin,
            provider=args.provider,
            public_path=args.public_path,
            tunnel_id=args.tunnel_id,
            credentials_file=args.credentials_file,
        )
        if args.apply:
            backup = apply_https_plan(args.root, plan)
            print(f"HTTPS configuration saved. Private rollback files: {backup}")
        else:
            print(json.dumps(plan, indent=2))
        print("Enroll a passkey on the HTTPS hostname and update the Google OAuth callback.")
    except Exception:
        # Configuration/credential parsing errors can contain secrets; never echo them.
        parser.exit(
            2,
            "HTTPS setup failed. Check origin, tunnel, settings and maintenance state.\n",
        )


if __name__ == "__main__":
    main()
