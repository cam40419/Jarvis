"""Offline planning and explicit text execution: python -m simon.agent_platform --help."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError as PydanticError

from simon.adapters.memory import InMemoryStore
from simon.adapters.model_endpoints import ModelEndpointClient, ModelEndpointError
from simon.domain.agent_platform import PlanTeamRequest
from simon.domain.errors import DomainError
from simon.domain.model_routing import RoutingRequest, TextGenerationRequest
from simon.domain.models import ActorContext, Channel
from simon.services.agent_platform import (
    AgentPlatformService,
    load_manifest,
    platform_credentials,
)
from simon.services.model_router import ModelRouter, ModelRoutingError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate configuration without contacting any service")
    plan = commands.add_parser("plan", help="Preview a team; API is used for persisted plans")
    plan.add_argument("--request", type=Path, required=True)
    plan.add_argument("--workspace-id", type=UUID, required=True)
    plan.add_argument("--actor-id", type=UUID, required=True)
    plan.add_argument("--scopes", default="jobs:read,jobs:write")
    plan.add_argument("--state-dir", type=Path, default=Path(".local/agents"))
    for action in ("route", "run-text"):
        command = commands.add_parser(
            action,
            help="Preview model selection"
            if action == "route"
            else "Explicitly send one text request; may incur API cost",
        )
        command.add_argument("--depth", type=int, default=2)
        command.add_argument("--importance", type=int, default=2)
        command.add_argument("--local-only", action="store_true")
        command.add_argument("--model", help="Configured endpoint ID override")
        command.add_argument("--input-tokens", type=int, default=4000)
        command.add_argument("--output-tokens", type=int, default=1024)
        command.add_argument("--budget-usd", type=float)
        if action == "run-text":
            command.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "agents": len(manifest.agents),
                        "teams": len(manifest.teams),
                        "models": len(manifest.models),
                        "tools": len(manifest.tools),
                        "environments": len(manifest.environments),
                    }
                )
            )
            return
        credentials = platform_credentials()
        if args.command == "plan":
            service = AgentPlatformService(
                InMemoryStore(),
                manifest,
                state_dir=args.state_dir,
                environ=credentials,
            )
            actor = ActorContext(
                actor_id=args.actor_id,
                household_id=args.workspace_id,
                channel=Channel.WORKER,
                scopes=frozenset(filter(None, args.scopes.split(","))),
            )
            request = PlanTeamRequest.model_validate_json(args.request.read_bytes())
            print(service.plan(actor, request).model_dump_json(indent=2))
            return
        decision = ModelRouter(manifest.models, environ=credentials).route(
            RoutingRequest(
                depth=args.depth,
                importance=args.importance,
                privacy="local_only" if args.local_only else "allow_cloud",
                model_override=args.model,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                budget_usd=args.budget_usd,
            )
        )
        if args.command == "route":
            print(decision.model_dump_json(indent=2))
            return
        result = ModelEndpointClient(manifest.models, environ=credentials).generate(
            decision,
            TextGenerationRequest(
                prompt=args.prompt_file.read_text(encoding="utf-8"),
                max_output_tokens=args.output_tokens,
            ),
        )
        print(result.model_dump_json(indent=2))
    except PydanticError:
        parser.exit(2, "Agent platform input is invalid; check its schema.\n")
    except OSError:
        parser.exit(2, "Agent platform input file is unavailable.\n")
    except (DomainError, ModelRoutingError, ModelEndpointError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
