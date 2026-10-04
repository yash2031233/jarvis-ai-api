"""Example plugin — copy this file to make your own tool.

Any .py file in ./plugins (or in your Jarvis data folder's /plugins) is loaded at startup.
Decorate a function with @tool: its signature becomes the schema the model sees, its
docstring becomes the description. Raise ToolError(message, hint=...) to help the model
self-correct.
"""

import random

from jarvis.hands import ToolError, tool


@tool(risk="low", tags=["dice", "roll", "random", "coin", "flip"],
      examples=["roll_dice(sides=20)", "roll_dice(sides=6, count=3)"])
def roll_dice(sides: int = 6, count: int = 1) -> dict:
    """Roll one or more dice (use sides=2 for a coin flip)."""
    if not 2 <= sides <= 1000 or not 1 <= count <= 100:
        raise ToolError("Unsupported dice.", hint="sides 2-1000, count 1-100")
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"rolls": rolls, "total": sum(rolls)}
