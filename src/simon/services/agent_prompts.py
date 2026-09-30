"""Render operator-owned agent templates without evaluating task or reference text."""

import json
from collections.abc import Mapping
from string import Template

from pydantic import ValidationError as PydanticValidationError

from simon.domain.agent_platform import AgentProfile, AgentTaskSpec
from simon.domain.errors import ValidationError
from simon.domain.models import StrictModel


class AgentPromptError(ValidationError):
    code = "agent_prompt_error"


class PreparedAgentPrompt(StrictModel):
    system: str
    prompt: str


def _substitute_bounded(template: Template, values: Mapping[str, str], limit: int) -> str:
    """Measure expansions before constructing the result, including repeated placeholders."""
    length = len(template.template)
    for match in template.pattern.finditer(template.template):
        name = match.group("named") or match.group("braced")
        if name is not None:
            if name not in values:
                raise AgentPromptError("Prompt template variable has no value: " + name)
            replacement_length = len(values[name])
        elif match.group("escaped") is not None:
            replacement_length = 1
        else:
            raise AgentPromptError("Prompt template contains an invalid placeholder")
        length += replacement_length - len(match.group())
    if length > limit:
        raise AgentPromptError("Rendered agent input exceeds max_input_chars")
    return template.substitute(values)


def render_agent_prompt(
    profile: AgentProfile,
    task: AgentTaskSpec,
    dependency_outputs: Mapping[str, str],
    context_name: str = "",
) -> PreparedAgentPrompt:
    """Keep trusted instructions in system and all task/dependency values in user input.

    The limit counts Python Unicode characters in system plus prompt, not tokens. A
    worker must also account for protocol instructions, tool results and later history.
    No provider call, file read or external template lookup occurs here.
    """
    try:
        # Frozen Pydantic objects can still contain mutable dictionaries. Validate a
        # fresh copy so post-construction changes cannot bypass the template contract.
        profile = AgentProfile.model_validate(profile.model_dump())
        task = AgentTaskSpec.model_validate(task.model_dump())
    except PydanticValidationError:
        raise AgentPromptError("Agent profile or task prompt configuration is invalid") from None
    if task.agent_id != profile.id:
        raise AgentPromptError("Task references a different agent profile")
    unknown = task.prompt_variables.keys() - profile.prompt_defaults.keys()
    if unknown:
        raise AgentPromptError("Task overrides undeclared prompt variables: " + ", ".join(
            sorted(unknown)
        ))
    if not isinstance(context_name, str):
        raise AgentPromptError("Context name must be text")
    if set(dependency_outputs) != set(task.depends_on):
        raise AgentPromptError("Dependency outputs must match the task's declared dependencies")
    if any(not isinstance(value, str) for value in dependency_outputs.values()):
        raise AgentPromptError("Dependency outputs must be text")

    sections = [profile.instructions]
    if profile.output_instructions:
        sections.append(profile.output_instructions)
    if profile.output_format == "json":
        sections.append(
            "The final answer must be a single valid JSON value, with no Markdown code "
            "fences or text outside that value."
        )
    else:
        sections.append("Provide the final answer as text.")
    system = "\n\n".join(sections)
    suffix = (
        "\n\nAdditional task instructions:\n" + task.additional_instructions
        if task.additional_instructions else ""
    )
    limit = profile.max_input_chars - len(system) - len(suffix)
    if limit < 0:
        raise AgentPromptError("Rendered agent input exceeds max_input_chars")

    template = Template(profile.prompt_template)
    referenced = set(template.get_identifiers())
    dependencies = ""
    if "dependencies" in referenced:
        # Reject oversized raw data before allocating its escaped JSON representation.
        # The final substitution check additionally counts escaping and object keys.
        if sum(len(name) + len(value) for name, value in dependency_outputs.items()) > limit:
            raise AgentPromptError("Rendered agent input exceeds max_input_chars")
        dependencies = json.dumps(
            {name: dependency_outputs[name] for name in task.depends_on},
            ensure_ascii=False, indent=2,
        )
    values = {
        **profile.prompt_defaults,
        **task.prompt_variables,
        "objective": task.objective,
        "dependencies": dependencies,
        "context": context_name,
    }
    prompt = _substitute_bounded(template, values, limit) + suffix
    return PreparedAgentPrompt(system=system, prompt=prompt)
