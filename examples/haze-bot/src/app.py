"""
A Bot that reads Singapore's haze PSI, written in Pydantic AI.

It is the Pydantic AI example with one difference: a tool of its own. ``get_singapore_psi`` runs in
this process and calls data.gov.sg directly through ``psi_reader.py``, a standard-library script that
also works on its own from the command line. Tools the surface sends in a run (the browser, files)
still arrive as external tools and stream back to OpenBot to run through the governed gateway, so the
Bot can do both.

Its voice comes from caveman (https://github.com/JuliusBrussee/caveman): drop the filler, keep every
number. A PSI answer is mostly numbers, so that is a lot of filler and very little else.
"""

import asyncio
import os

from pydantic_ai import Agent
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import psi_reader

MODEL = os.environ.get("BOT_MODEL", "gpt-5.5")

# The caveman rules, cut down to what a PSI answer needs. The last two lines are caveman's own
# "auto-clarity" exception: a health warning is the one place a dropped word can hurt somebody.
INSTRUCTIONS = (
    "You report Singapore's haze PSI. Respond terse like smart caveman. All technical substance "
    "stay. Only fluff die.\n"
    "- Answer first. No greeting, no preamble, no sign-off.\n"
    "- Drop articles, filler and hedging. Fragments fine.\n"
    "- Keep every number, region, band and reading time exactly as the tool returned them.\n"
    "- Worst region first.\n\n"
    "For any reading, call get_singapore_psi first. NEVER state a PSI value you did not just get "
    "from that tool in this conversation. If the tool returns an error, say what it said and stop: "
    "no guessed numbers.\n\n"
    "When any region is Unhealthy or worse, add one plain full sentence saying so, and point to "
    "NEA's advisory at haze.gov.sg for what to do. Do not invent health advice of your own."
)

agent = Agent(f"openai:{MODEL}", instructions=INSTRUCTIONS)


@agent.tool_plain
async def get_singapore_psi() -> dict:
    """Get the latest 24-hour PSI and 1-hour PM2.5 for Singapore's five regions from data.gov.sg.

    Returns ``summary``, one caveman line with every region worst first, and ``reading`` with the
    full numbers. On failure returns ``error`` with the reason instead.
    """
    try:
        # urllib blocks, so it runs on a worker thread rather than stalling every other run.
        result = await asyncio.to_thread(psi_reader.fetch_live)
    except psi_reader.PSIError as error:
        return {"error": str(error)}
    return {"summary": psi_reader.short_summary(result), "reading": result}


async def ag_ui(request: Request) -> Response:
    """One POST carrying a ``RunAgentInput``, a stream of AG-UI events back.

    Guarded by ``REQUIRE_KEY`` when it is set, the same as the other examples.
    """
    required_key = os.environ.get("REQUIRE_KEY")
    if required_key and request.headers.get("authorization") != required_key:
        print("refused a run: wrong or missing key")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await AGUIAdapter.dispatch_request(request, agent=agent)


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "framework": "pydantic-ai", "bot": "haze-psi"})


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/ag-ui", ag_ui, methods=["POST"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "4610"))
    print(f"haze-bot listening on http://localhost:{port}/ag-ui (model {MODEL})")
    uvicorn.run(app, host="0.0.0.0", port=port)
