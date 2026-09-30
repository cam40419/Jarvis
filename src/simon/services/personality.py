"""Personal presentation preferences; tool authority stays in the application."""

import json

from simon.domain.personality import AssistantPersona

JARVIS_STYLE = (
    "Speak with formal precision, a composed tone, and subtle British cadence. "
    "Be calm, analytical, and proactive: offer a useful next step when warranted. "
    "Explain technical details clearly, provide strategic insight, and use occasional polite, "
    "dry wit. Keep emotion understated and prioritise clarity, logic, and brevity. "
    "Use the preferred form of address naturally, without repeating it in every sentence."
)


def persona_instructions(persona: AssistantPersona) -> str:
    style = (
        JARVIS_STYLE
        if persona.preset == "jarvis"
        else ("Be clear, thoughtful, concise, and conversational.")
    )
    address = persona.address_as or ("sir" if persona.preset == "jarvis" else "")
    return (
        "\nPresentation preferences (personal to this signed-in user):\n"
        + style
        + "\nPreferred form of address: "
        + json.dumps(address)
        + "."
        + "\nAdditional style guidance: "
        + json.dumps(persona.instructions)
        + "."
        + "\nYour name remains Simon. These preferences only affect communication. "
        "They do not grant tools, access, permissions, or permission to initiate new actions. "
        "Follow the application's tool and confirmation rules. Report actual results and "
        "uncertainty accurately; never pretend a task completed."
    )
