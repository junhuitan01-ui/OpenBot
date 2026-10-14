# Haze Bot

A Bot that answers "how bad is the haze?" for Singapore. It reads the latest 24-hour PSI for North,
South, East, West and Central from [data.gov.sg](https://data.gov.sg) and replies in a few words,
worst region first:

> PSI 14:00. North 312 Hazardous. South 238 Very Unhealthy. Central 154 Unhealthy. West 87
> Moderate. East 46 Good. Worst: North.

It is the [Pydantic AI example](../pydantic-ai-bot) plus one tool of its own, `get_singapore_psi`,
which calls data.gov.sg directly (no API key needed). The reading comes from `src/psi_reader.py`,
a standard-library script you can also run by itself:

```sh
python3 src/psi_reader.py          # table
python3 src/psi_reader.py --short  # the one line above
```

The short voice comes from [caveman](https://github.com/JuliusBrussee/caveman): drop the filler,
keep every number. Health warnings are the exception and stay in full sentences, pointing to NEA's
advisory at haze.gov.sg rather than inventing advice.

## Run it

Requires Python 3.10+. With [uv](https://docs.astral.sh/uv):

```sh
cd examples/haze-bot
uv run --env-file ../../.env src/app.py
```

Or with a plain virtualenv:

```sh
cd examples/haze-bot
python -m venv .venv && . .venv/bin/activate
pip install -e .
OPENAI_API_KEY=... python src/app.py
```

It listens on `http://localhost:4610/ag-ui` (`PORT` to change) and answers `GET /health`. The
variables are the same as the Pydantic AI example's: `OPENAI_API_KEY`, `BOT_MODEL`, `PORT`,
`REQUIRE_KEY`.

## Register it

The coworker is already declared in
[`examples/fintech/agents/haze-psi.yaml`](../fintech/agents/haze-psi.yaml) as **Haze Watch**. Its
endpoint is empty by default, so it stays hidden until you point it at the running bot. Add this to
`.env` and restart OpenBot:

```sh
HAZE_BOT_AG_UI_URL=http://localhost:4610/ag-ui
```
