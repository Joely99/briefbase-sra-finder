# main.py
import os
import re
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ---------- config / secrets ----------
load_dotenv()

SRA_API_KEY = os.getenv("SRA_API_KEY")  # add in Replit → Tools → Secrets
# Use Azure API Management hostname (avoids SSL glitch on microsites host)
BASE_URL = "https://sra-prod-api.azure-api.net/datashare/api/v1"

if not SRA_API_KEY:
    raise RuntimeError("Missing SRA_API_KEY. Add it in Replit → Tools → Secrets.")

HEADERS = {
    "Ocp-Apim-Subscription-Key": SRA_API_KEY,
    "Cache-Control": "no-cache",
}

# ---------- app ----------
app = FastAPI(title="BriefBase SRA Finder", version="0.2.0")

# open CORS for testing (tighten later to your domain)
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
    """Uppercase & collapse spaces/punct to aid comparisons."""
    pc = (pc or "").upper()
    pc = re.sub(r"\s+", " ", pc).strip()
    return pc

def outward_code(pc: str) -> str:
    """Return outward part (everything before the space) or last 3 chars as fallback."""
    pc = normalise_postcode(pc)
    if " " in pc:
        return pc.split(" ", 1)[0]
    # fallback: many APIs still work if we use everything except inward last 3
    return pc[:-3] if len(pc) > 3 else pc

def looks_active(org: Dict[str, Any]) -> bool:
    """Treat common SRA statuses as 'active'."""
    status = (org.get("AuthorisationStatus") or "").strip().lower()
    return any(w in status for w in ["authorised", "registered", "authorised body", "recognised body"])

def office_matches_postcode(office: Dict[str, Any], target_pc: str) -> bool:
    """Check if any office address outward code matches user's outward code."""
    addrs = office.get("Address", {}) or {}
    pc = addrs.get("PostCode") or ""
    if not pc:
        return False
    return outward_code(pc) == outward_code(target_pc)

def call_sra_json(path: str, *, timeout: int = 20) -> Dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=resp.status_code, detail=f"SRA API HTTP error: {e}")  # type: ignore
    except requests.exceptions.SSLError as e:
        # Specific hint if someone flips host back to microsites (SNI issue)
        raise HTTPException(status_code=502, detail=f"SRA API SSL error: {e}. "
                                                    f"Make sure BASE_URL uses azure-api.net.")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"SRA API network error: {e}")

# ---------- endpoints ----------
@app.get("/", summary="Root")
def root():
    return {"ok": True, "msg": "FastAPI is alive."}

@app.get("/health", summary="Health")
def health():
    return {"status": "ok"}

@app.get(
    "/search",
    summary="Find SRA-registered firms by postcode",
    description="Returns firms with an office whose outward postcode matches the supplied UK postcode.",
)
def search_firms(
    postcode: str = Query(..., description="UK postcode, e.g., SW1A 1AA or SW1A1AA")
):
    # basic validation to avoid junk inputs
    pc_clean = normalise_postcode(postcode)
    if not UK_PC_RE.match(pc_clean):
        raise HTTPException(status_code=422, detail="Please provide a valid UK postcode.")

    # 1) Pull organisations (this payload contains offices)
    data = call_sra_json("Organisations")

    # 2) Filter: active + at least one office matching outward code
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

