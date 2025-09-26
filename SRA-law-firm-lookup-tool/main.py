# main.py
import os
import re
from typing import List, Dict, Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ---------- config / secrets ----------
load_dotenv()

SRA_API_KEY = os.getenv("SRA_API_KEY")

# On Render, azure-api.net DNS can be flaky; try microsites first, then azure-api.
SRA_HOSTS = [
    "https://sra-prod-api.microsites.uk/datashare/api/v1",
    "https://sra-prod-api.azure-api.net/datashare/api/v1",
]

if not SRA_API_KEY:
    raise RuntimeError("Missing SRA_API_KEY. Set it in Render → Environment.")

HEADERS = {
    "Ocp-Apim-Subscription-Key": SRA_API_KEY,
    "Cache-Control": "no-cache",
}

# ---------- app ----------
app = FastAPI(title="BriefBase SRA Finder", version="0.3.0")

# Open CORS during testing (tighten later to your domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- helpers ----------
UK_PC_RE = re.compile(
    r"""
    ^\s*
    (GIR\s?0AA|
     (?:[A-PR-UWYZ][0-9][0-9]?|
        [A-PR-UWYZ][A-HK-Y][0-9][0-9]?|
        [A-PR-UWYZ][0-9][A-HJKPSTUW]?|
        [A-PR-UWYZ][A-HK-Y][0-9][ABEHMNPRVWXY]?)
     \s?[0-9][ABD-HJLNP-UW-Z]{2})
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

def normalise_postcode(pc: str) -> str:
    pc = (pc or "").upper()
    pc = re.sub(r"\s+", " ", pc).strip()
    return pc

def outward_code(pc: str) -> str:
    pc = normalise_postcode(pc)
    if " " in pc:
        return pc.split(" ", 1)[0]
    return pc[:-3] if len(pc) > 3 else pc

def looks_active(org: Dict[str, Any]) -> bool:
    status = (org.get("AuthorisationStatus") or "").strip().lower()
    return any(w in status for w in [
        "authorised", "registered", "authorised body", "recognised body"
    ])

def office_matches_postcode(office: Dict[str, Any], target_pc: str) -> bool:
    addrs = office.get("Address", {}) or {}
    pc = addrs.get("PostCode") or ""
    if not pc:
        return False
    return outward_code(pc) == outward_code(target_pc)

def call_sra_json(path: str, *, timeout: int = 20) -> Dict[str, Any]:
    """
    Try each base host in order; return the first successful JSON.
    If all fail, surface the last error.
    """
    last_error = None
    for base in SRA_HOSTS:
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            # HTTP error with a response (e.g., 401/403/5xx)
            try:
                detail = resp.text[:500]  # type: ignore
            except Exception:
                detail = str(e)
            last_error = f"HTTPError on {base}: {detail}"
        except requests.exceptions.SSLError as e:
            last_error = f"SSLError on {base}: {e}"
        except requests.exceptions.RequestException as e:
            last_error = f"Network error on {base}: {e}"
        # Try next base
    raise HTTPException(status_code=502, detail=f"SRA API network error: {last_error}")

# ---------- endpoints ----------
@app.get("/", summary="Root")
def root():
    return {"ok": True, "msg": "FastAPI is alive."}

@app.get("/health", summary="Health")
def health():
    return {"status": "ok"}

@app.get("/probe", summary="Probe SRA hosts")
def probe():
    """
    Quick diagnostic: attempts a lightweight call to each SRA host
    and reports whether it succeeds or the error we get.
    """
    results = []
    for base in SRA_HOSTS:
        url = f"{base.rstrip('/')}/Organisations?$top=1"
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            ok = r.ok
            status = r.status_code
            body = (r.text or "")[:300]
            results.append({"host": base, "ok": ok, "status": status, "sample": body})
        except Exception as e:
            results.append({"host": base, "ok": False, "error": str(e)})
    return {"probe": results}

@app.get(
    "/search",
    summary="Find SRA-registered firms by postcode",
    description="Returns firms with an office whose outward postcode matches the supplied UK postcode.",
)
def search_firms(
    postcode: str = Query(..., description="UK postcode, e.g., SW1A 1AA or SW1A1AA")
):
    pc_clean = normalise_postcode(postcode)
    if not UK_PC_RE.match(pc_clean):
        raise HTTPException(status_code=422, detail="Please provide a valid UK postcode.")

    # Pull organisations (the payload includes Offices)
    data = call_sra_json("Organisations")

    # Filter for active orgs with at least one matching office outward code
    results: List[Dict[str, Any]] = []
    for org in data.get("value", []) or []:
        if not looks_active(org):
            continue
        for office in org.get("Offices", []) or []:
            if office_matches_postcode(office, pc_clean):
                addrs = office.get("Address", {}) or {}
                results.append(
                    {
                        "OrganisationID": org.get("OrganisationID"),
                        "Name": org.get("OrganisationName"),
                        "Email": org.get("Email") or org.get("GeneralEmail"),
                        "Phone": org.get("Phone"),
                        "Postcode": addrs.get("PostCode"),
                        "Address1": addrs.get("Address1"),
                        "Town": addrs.get("Town"),
                        "AuthorisationStatus": org.get("AuthorisationStatus"),
                    }
                )
                break  # one matching office is enough
    return {"count": len(results), "results": results}
