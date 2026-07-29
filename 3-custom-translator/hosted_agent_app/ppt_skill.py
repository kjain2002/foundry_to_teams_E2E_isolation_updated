"""PowerPoint skill instructions for the hosted agent — two modes.

These strings are injected as ``instructions`` on the Responses request (or
merged into the agent's system prompt) so the *hosted* Code Interpreter builds
the deck. Nothing here executes PowerPoint locally.

Two modes:
    * template mode      — a .pptx/.potx was uploaded to the Foundry container;
                           preserve its visual identity.
    * no-template mode   — design an original deck from the user's style words.

The agent must only claim a template was used when one was actually supplied.
"""

from __future__ import annotations

# Recognised free-form style keywords (used to echo intent back to the model).
KNOWN_STYLES = [
    "executive",
    "technical",
    "minimalist",
    "corporate",
    "bold",
    "highly visual",
    "microsoft-style",
    "dense",
    "spacious",
]

_COMMON = """\
You generate PowerPoint (.pptx) decks with the hosted Code Interpreter tool.
You MUST actually run Code Interpreter (python-pptx) inside the bound container
to produce the file. Do NOT describe a deck you did not build. When you finish,
the file must exist in the container so the client can download and verify it.
Save the deck with a clear name ending in .pptx.
"""

TEMPLATE_MODE = """\
TEMPLATE MODE — a PowerPoint template was supplied in the container.
1. Open the supplied template with python-pptx (Presentation(<template_path>)).
2. Inspect its slide layouts, theme, fonts, colours, placeholders and branding.
3. Build the deck on top of the template so its visual identity is preserved.
4. When an exact layout is unavailable, use the closest available layout.
5. Do not invent brand colours or fonts that conflict with the template.
6. In your final message, state clearly that the supplied template was used
   (name it) and which layouts you mapped to.
"""

NO_TEMPLATE_MODE = """\
NO-TEMPLATE MODE — no template was supplied; design an original deck.
1. Do NOT ask the user to upload a template.
2. Apply the user's style instructions (e.g. executive, technical, minimalist,
   corporate, bold, highly visual, Microsoft-style, specific brand colours,
   specific typography, dense or spacious layout).
3. If the user gave no style guidance, choose one consistent, readable,
   professional design and apply it coherently across every slide.
4. Use reusable layout helpers and a single coherent theme (master colours,
   fonts, spacing) rather than ad-hoc per-slide styling.
5. Do NOT claim any branding was inherited from a template.
6. In your final message, state that this is an ORIGINAL design based on the
   user's instructions, and summarise the theme you chose.
Only ask for a template if the user explicitly requires exact brand/template
fidelity that cannot be met without their source file.
"""


def build_ppt_instructions(
    has_template: bool,
    style_instructions: str | None = None,
    template_ref: str | None = None,
) -> str:
    """Return the instruction block for the current PPT request.

    Args:
        has_template: whether a .pptx/.potx template is available in the container.
        style_instructions: free-form user style words (no-template mode).
        template_ref: container file path / id of the template, if any.
    """
    parts = [_COMMON]
    if has_template:
        block = TEMPLATE_MODE
        if template_ref:
            block += f"\nThe template is available in the container as: {template_ref}\n"
        parts.append(block)
    else:
        parts.append(NO_TEMPLATE_MODE)
        if style_instructions and style_instructions.strip():
            parts.append(
                "User style instructions: " + style_instructions.strip()
            )
    return "\n".join(parts)


def looks_like_ppt_request(text: str) -> bool:
    """Heuristic: does this user turn ask for a slide deck / PPTX?"""
    t = (text or "").lower()
    return any(
        kw in t
        for kw in ("pptx", "powerpoint", "slide", "deck", "presentation", "slides")
    )
