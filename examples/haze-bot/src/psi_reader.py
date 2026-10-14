#!/usr/bin/env python3
"""
Singapore haze PSI reader
=========================

A small command-line program that shows the latest PSI (Pollutant Standards
Index) readings for Singapore, using the free open data API from data.gov.sg.
No API key is needed.

How to run it:

    python3 psi_reader.py                       # fetch live data from the internet
    python3 psi_reader.py --file sample_psi.json  # read a saved response instead
    python3 psi_reader.py --json                # print machine-readable JSON

This file only uses Python's standard library, so there is nothing to install.
"""

# --- Imports -----------------------------------------------------------------
# Every module below ships with Python itself.
import argparse          # reads the command-line flags such as --file and --json
import http.client       # the error types for a broken HTTP conversation
import json              # turns JSON text into Python dicts/lists (and back)
import math              # math.isfinite() spots "not a number" and infinity
import socket            # lets us recognise a network timeout error
import ssl               # lets us recognise a security-certificate error
import sys               # gives us sys.exit() and sys.stderr
import urllib.error      # the error types urllib can raise
import urllib.request    # downloads a web page / API response

# --- Settings ------------------------------------------------------------------
# Values written in CAPITALS are "constants": settings we do not change while
# the program runs. Keeping them at the top makes them easy to find and edit.

# The newer (v2) API address. We try this one first.
PRIMARY_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"

# The older (v1) API address. We only use it if the v2 address fails.
FALLBACK_URL = "https://api.data.gov.sg/v1/environment/psi"

# How many seconds to wait for the server before giving up.
TIMEOUT_SECONDS = 15

# Some servers reject requests that do not say who is asking, so we send a
# polite "User-Agent" header describing this program.
USER_AGENT = "sg-psi-reader/1.0 (beginner Python example; standard library only)"

# The five regions we always want to show, in the order we want to show them.
REGIONS = ["north", "south", "east", "west", "central"]

# NEA health bands for the 24-hour PSI.
# Each entry is (highest PSI value in this band, name of the band).
# We check them from top to bottom and stop at the first one that fits.
BANDS = [
    (50, "Good"),
    (100, "Moderate"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
]
# Anything above 300 is "Hazardous" (handled in psi_band below).

# Shown when Python cannot check the website's security certificate.
SSL_HINT = (
    "Could not check the security certificate of data.gov.sg. "
    "Your internet may be fine: Python's certificate list is likely missing. "
    "On a Mac with Python from python.org, run 'Install Certificates.command' "
    "(in your Applications/Python 3.x folder) and try again."
)


# --- Our own error type ----------------------------------------------------------
class PSIError(Exception):
    """An error with a friendly message we can show straight to the user."""


# --- Step 1: get the raw data ---------------------------------------------------------
def fetch_url(url):
    """Download `url` and return the decoded JSON (a Python dict).

    Raises PSIError with a friendly message if anything goes wrong.
    """
    # Build the request and attach our User-Agent header.
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    try:
        # "with" makes sure the connection is closed when we are done.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw_bytes = response.read()
    except urllib.error.HTTPError as error:
        # The server answered, but with an error code such as 404 or 500.
        # (HTTPError must be caught before URLError, because it is a kind of URLError.)
        raise PSIError(f"The server replied with HTTP error {error.code} ({error.reason}).")
    except urllib.error.URLError as error:
        # We could not reach the server at all: no internet, DNS failure, timeout...
        if isinstance(error.reason, socket.timeout):
            raise PSIError(f"The server did not answer within {TIMEOUT_SECONDS} seconds.")
        if isinstance(error.reason, ssl.SSLError):
            # The internet works, but Python could not check the website's
            # security certificate. This is common with the python.org
            # installer on a Mac, which does not set up certificates itself.
            raise PSIError(SSL_HINT + f" (Details: {error.reason})")
        raise PSIError(
            "Could not connect to data.gov.sg. Check your internet connection. "
            f"(Details: {error.reason})"
        )
    except (socket.timeout, TimeoutError):
        # A timeout can also happen while reading the response.
        raise PSIError(f"The server did not answer within {TIMEOUT_SECONDS} seconds.")
    except ssl.SSLError as error:
        # A certificate problem can also show up without the URLError wrapper.
        raise PSIError(SSL_HINT + f" (Details: {error})")
    except (OSError, http.client.HTTPException) as error:
        # The connection broke while we were reading the answer, for example
        # "connection reset" or a response that stopped half-way.
        raise PSIError(f"The connection to the server broke while reading the data. (Details: {error!r})")

    # Turn the bytes into text, then the text into Python objects.
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # RecursionError happens if the JSON is nested absurdly deep.
        raise PSIError("The server sent something that is not valid JSON.")


def fetch_live():
    """Download AND check the data: try the v2 API first, then the older v1 API.

    We parse each answer straight away. That way a server that replies
    "200 OK" but with useless content also makes us try the next address.
    Returns the parsed result (see parse_response).
    """
    first_error = None
    for url in (PRIMARY_URL, FALLBACK_URL):
        try:
            return parse_response(fetch_url(url))
        except PSIError as error:
            # Remember only the first problem; it is usually the real cause.
            if first_error is None:
                first_error = error
    # Both addresses failed.
    raise first_error


def load_file(path):
    """Read a saved JSON response from a file on disk."""
    if path == "":
        raise PSIError("No file name given after --file.")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PSIError(f"File not found: {path}")
    except IsADirectoryError:
        raise PSIError(f"That is a folder, not a file: {path}")
    except PermissionError:
        raise PSIError(f"No permission to read: {path}")
    except UnicodeDecodeError:
        raise PSIError(f"{path} is not a UTF-8 text file.")
    except json.JSONDecodeError as error:
        # error.lineno and error.colno tell us where the JSON broke.
        raise PSIError(
            f"{path} does not contain valid JSON "
            f"(problem at line {error.lineno}, column {error.colno}: {error.msg})."
        )
    except RecursionError:
        # The JSON is nested so deeply that Python gives up reading it.
        raise PSIError(f"{path} is nested too deeply to read as JSON.")
    except OSError as error:
        # Any other problem from the operating system (bad file name,
        # disk error, ...). error.strerror is its human-readable message.
        raise PSIError(f"Could not read {path}: {error.strerror or error}")


# --- Step 2: understand the data ----------------------------------------------------------
def psi_band(value):
    """Return the NEA health band name for a 24-hour PSI value."""
    for upper_limit, name in BANDS:
        if value <= upper_limit:
            return name
    return "Hazardous"


def to_number(value, label):
    """Turn `value` into a usable number.

    Returns a pair: (number, problem).
      - number is an int/float, or None if we could not use the value.
      - problem is None, or a short sentence saying what was wrong.
    `label` names the value in that sentence, e.g. "North 24h PSI".
    """
    if value is None:
        return None, f"{label}: no value in the data."
    # bool is checked first: in Python True counts as 1, which we do not want.
    if isinstance(value, bool):
        return None, f"{label}: got {value!r}, which is not a number."
    if isinstance(value, str):
        # Some APIs send numbers as text, like "46". float() converts it,
        # and raises ValueError if the text is not a number at all.
        try:
            value = float(value)
        except ValueError:
            return None, f"{label}: got the text {value!r}, which is not a number."
    if not isinstance(value, (int, float)):
        return None, f"{label}: got {type(value).__name__} {value!r}, which is not a number."
    # math.isfinite() is False for NaN ("not a number") and for infinity.
    if not math.isfinite(value):
        return None, f"{label}: got {value!r}, which is not a real reading."
    # Show 46.0 as 46.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value, None


def item_time(item):
    """The timestamp text of one item, or "" if it has none."""
    if not isinstance(item, dict):
        return ""
    return str(item.get("timestamp") or item.get("updatedTimestamp") or "")


def parse_response(data):
    """Turn a v1 or v2 API response into one simple dict that is easy to print.

    The two API versions wrap the data slightly differently:
      v2: {"code": 0, "data": {"items": [ ... ]}}
      v1: {"items": [ ... ]}
    Inside "items" both look alike: a "timestamp" and a "readings" dict.
    """
    if not isinstance(data, dict):
        raise PSIError("Unexpected response shape: expected a JSON object at the top level.")

    # Work out which version we were given. Only v2 has "code" or "data".
    if "code" in data or "data" in data:
        # v2 reports problems with a non-zero "code" and an "errorMsg".
        # We check the code first, because an error reply may have no "data".
        code = data.get("code", 0)
        if code != 0:
            message = data.get("errorMsg") or "no message given"
            raise PSIError(f"The API reported an error (code {code}): {message}")
        inner = data.get("data")
        api_version = "v2"
    else:
        inner = data
        api_version = "v1"

    if not isinstance(inner, dict):
        raise PSIError("Unexpected response shape: 'data' is not a JSON object.")

    items = inner.get("items")
    if not isinstance(items, list) or len(items) == 0:
        raise PSIError("Unexpected response shape: no 'items' list with readings was found.")

    # The newest reading is normally the only item. If there are several, we
    # pick the one with the latest timestamp. (Timestamps are ISO-8601 text,
    # so comparing them as text gives the right order.) If no item has a
    # timestamp, we take the last item in the list.
    if any(item_time(item) for item in items):
        latest = max(items, key=item_time)
    else:
        latest = items[-1]
    if not isinstance(latest, dict):
        raise PSIError("Unexpected response shape: an item is not a JSON object.")

    readings = latest.get("readings")
    if not isinstance(readings, dict):
        raise PSIError("Unexpected response shape: the item has no 'readings'.")

    psi_24h = readings.get("psi_twenty_four_hourly")
    if not isinstance(psi_24h, dict):
        raise PSIError("Unexpected response shape: 'psi_twenty_four_hourly' is missing.")

    # 1-hour PM2.5 is optional: if it is missing we simply show "-".
    pm25_1h = readings.get("pm25_one_hourly")
    if not isinstance(pm25_1h, dict):
        pm25_1h = {}

    # Notes about values we could not use. We show them to the user so a
    # "-" in the table always has a reason.
    warnings = []

    # Build one row per region.
    rows = []
    for region in REGIONS:
        psi, problem = to_number(psi_24h.get(region), f"{region.title()} 24h PSI")
        if problem:
            warnings.append(problem)
        pm25 = None
        if region in pm25_1h:
            pm25, problem = to_number(pm25_1h[region], f"{region.title()} 1h PM2.5")
            if problem:
                warnings.append(problem)
        rows.append({
            "region": region,
            "psi_24h": psi,
            "band": psi_band(psi) if psi is not None else None,
            "pm25_1h": pm25,
        })

    known_values = [row["psi_24h"] for row in rows if row["psi_24h"] is not None]
    if not known_values:
        # Name the real cause, e.g. "North 24h PSI: got the text 'abc' ...".
        raise PSIError(
            "Unexpected response shape: no usable 24-hour PSI value for any region. "
            + " ".join(warnings)
        )

    # National PSI: use the API value if there is one (v1 has it).
    # Otherwise use the highest regional value and remember that we did so.
    national_estimated = False
    if "national" in psi_24h:
        national, problem = to_number(psi_24h["national"], "National 24h PSI")
        if problem:
            warnings.append(problem)
    else:
        national = None
    if national is None:
        national = max(known_values)
        national_estimated = True

    national_pm25 = None
    if "national" in pm25_1h:
        national_pm25, problem = to_number(pm25_1h["national"], "National 1h PM2.5")
        if problem:
            warnings.append(problem)

    return {
        "api_version": api_version,
        # v2 also has "updatedTimestamp"; fall back to it if "timestamp" is absent.
        "reading_time": latest.get("timestamp") or latest.get("updatedTimestamp") or "unknown",
        "regions": rows,
        "national": {
            "psi_24h": national,
            "band": psi_band(national),
            "pm25_1h": national_pm25,
            "estimated_from_highest_region": national_estimated,
        },
        "warnings": warnings,
    }


# --- Step 3: show the result -------------------------------------------------------------
def short_summary(result):
    """Caveman-style one-liner: every number kept, every extra word dropped.

    Regions are listed worst first, so the line answers "where is it bad?" before anything else.
    """
    when = result["reading_time"]
    # "2026-10-14T14:00:00+08:00" -> "14:00". Anything else is shown as it came.
    if "T" in when and len(when) >= 16:
        when = when[11:16]
    known = [row for row in result["regions"] if row["psi_24h"] is not None]
    known.sort(key=lambda row: row["psi_24h"], reverse=True)
    parts = [f"{row['region'].title()} {row['psi_24h']:g} {row['band']}" for row in known]
    worst = known[0]
    line = f"PSI {when}. " + ". ".join(parts) + f". Worst: {worst['region'].title()}."
    missing = [row["region"].title() for row in result["regions"] if row["psi_24h"] is None]
    if missing:
        line += " No data: " + ", ".join(missing) + "."
    return line


def show_value(value):
    """Format a number for the table, or '-' if we have no value."""
    if value is None:
        return "-"
    # Show whole numbers without a trailing ".0".
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def print_table(result):
    """Print the readings as a simple aligned table."""
    print("Singapore PSI (source: data.gov.sg, NEA)")
    print(f"Reading time: {result['reading_time']}")
    print()

    # The ":<10" and ":>8" parts pad each column: "<" = left-align, ">" = right-align.
    header = f"{'Region':<10} {'24h PSI':>8}  {'Health band':<15} {'1h PM2.5':>9}"
    print(header)
    print("-" * len(header))

    for row in result["regions"]:
        print(
            f"{row['region'].title():<10} "
            f"{show_value(row['psi_24h']):>8}  "
            f"{row['band'] or '-':<15} "
            f"{show_value(row['pm25_1h']):>9}"
        )

    print("-" * len(header))
    national = result["national"]
    label = "National*" if national["estimated_from_highest_region"] else "National"
    print(
        f"{label:<10} "
        f"{show_value(national['psi_24h']):>8}  "
        f"{national['band']:<15} "
        f"{show_value(national['pm25_1h']):>9}"
    )

    print()
    if national["estimated_from_highest_region"]:
        print("* The API gave no usable national value, so the highest regional 24h PSI is shown.")
    # Explain every value we had to skip.
    for warning in result["warnings"]:
        print(f"Note: {warning} Shown as '-'.")
    print("PM2.5 is in micrograms per cubic metre (µg/m³).")
    print("Bands (24h PSI): Good 0-50, Moderate 51-100, Unhealthy 101-200,")
    print("                 Very Unhealthy 201-300, Hazardous above 300.")


# --- The main program ---------------------------------------------------------------------
def main(argv=None):
    """Read the flags, get the data, and print it. Returns the exit code."""
    parser = argparse.ArgumentParser(
        description="Show the latest Singapore haze PSI readings from data.gov.sg.",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="read a saved JSON response from PATH instead of the internet",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of a table",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="print one short caveman-style line instead of a table",
    )
    args = parser.parse_args(argv)

    try:
        # "is not None" matters: an empty name (--file "") must not quietly
        # switch to the internet. load_file() reports it as an error instead.
        if args.file is not None:
            result = parse_response(load_file(args.file))
        else:
            # fetch_live() downloads and checks the data in one go.
            result = fetch_live()
    except PSIError as error:
        # Errors go to stderr so they do not mix with normal output.
        print(f"Error: {error}", file=sys.stderr)
        return 1  # any non-zero exit code means "something went wrong"

    if args.short:
        print(short_summary(result))
    elif args.json:
        # ensure_ascii=False keeps characters like "µ" readable.
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_table(result)
    return 0  # 0 means success


# This "if" is true only when the file is run directly (not imported).
if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # The user pressed Ctrl+C: stop quietly instead of printing a traceback.
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)
