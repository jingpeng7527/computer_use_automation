"""The mock legacy credit-union servicing console -- the target surface
discovery and replay drive.

Deliberately legacy-styled (系统设计 sec 4.5), one markup pattern per layer
the locator model needs to exercise:

    layer 1 (role + accessible name)  a real <button type="submit">
    layer 2 (label anchor)            an unlabeled <input> found only by
                                       the plain text in the table cell
                                       next to it
    layer 3 (css)                     a <div onclick=...> acting as a
                                       control, no role, no aria-label,
                                       but a stable (non-hashed) class name

The flow is search -> results -> detail, matching the assignment's own
"non-trivial multi-step flow" suggestion, and giving all three layers a
real reason to exist rather than a contrived one.

Beyond the happy path (member 12345), this app can be made to produce the
divergences the hardening pass (agent/hardening.py) needs to derive a real
error taxonomy from observed behaviour, not a guessed list:

    unknown member id         -> search page re-renders with a plain,
                                  detectable "No member records match"
                                  message (a legitimate business outcome)
    ?inject=500 on /members/search -> a genuine HTTP 500 with "System
                                  Error" text (a hard failure)
    ?interstitial=1 on /members/search (first hit only, see
                                  _interstitial_shown below) -> a real
                                  "Session Warning" dialog that has to be
                                  dismissed before the search page loads
                                  (a recoverable condition, RecoveryAction
                                  do="dismiss_dialog")
    ?slow=1 on /members/search (first hit only, see _slow_load_shown
                                  below) -> a "Loading, please wait" page
                                  with nothing to click -- resolves on a
                                  bare retry (a recoverable condition,
                                  RecoveryAction do="wait")
    member_id 99001 / 99002       -> the detail page withholds the balance
                                  row entirely for a frozen or
                                  permission-restricted account (two more
                                  business outcomes, ACCOUNT_FROZEN /
                                  PERMISSION_DENIED, derived the same
                                  observed-divergence way as not-found)

The detail page also carries two fixtures unrelated to this capability's
own step path, present for realism/testability rather than because this
flow needs them: a decorative nav element with no accessible name at all
(a locator-exhaustion case), and a real "Change Credit Limit" button
(role+name resolve it easily at layer 1 -- what's interesting about it is
its IRREVERSIBLE risk tier, exercised by safety/risk.py, not its locator).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import tenant_b
from .data import MEMBERS

app = FastAPI(title="Member Servicing Console")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Northgate Federal Credit Union: a second tenant on the SAME vendor
# product, older build -- the live fixture for REPORT.md sec 4's
# multi-tenant reuse claim (see tenant_b.py for what differs and why).
app.include_router(tenant_b.router, prefix="/tenant-b")

# Per-process, not per-request: the interstitial shows on the FIRST
# ?interstitial=1 hit this server has seen and never again after that,
# regardless of how many more times the same flagged URL is requested.
# This is what makes a `dismiss_dialog` recovery (which re-navigates to
# the SAME url_template on retry -- see replay/engine.py's _apply_match)
# actually resolve: the retry has to hit a server that has moved on, not
# one that would show the same dialog forever. Each test/demo run starts
# a fresh uvicorn process, so this always begins unshown.
_interstitial_shown = {"count": 0}
# Same per-process-first-hit-only shape as _interstitial_shown, for the
# same structural reason (see that comment) -- a `wait` recovery re-runs
# the step's own action unmodified, so the divergence has to clear on its
# own by the second hit, not because anything was clicked.
_slow_load_shown = {"count": 0}


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/members/search", status_code=303)


@app.get("/members/search")
def search_form(
    request: Request,
    not_found: str | None = None,
    inject: str | None = None,
    interstitial: str | None = None,
    slow: str | None = None,
):
    if inject == "500":
        return PlainTextResponse(
            "System Error 500: an internal failure occurred while loading this page.",
            status_code=500,
        )
    if interstitial and _interstitial_shown["count"] == 0:
        _interstitial_shown["count"] += 1
        return templates.TemplateResponse(request, "session_warning.html", {})
    if slow and _slow_load_shown["count"] == 0:
        _slow_load_shown["count"] += 1
        return templates.TemplateResponse(request, "loading.html", {})
    return templates.TemplateResponse(request, "search.html", {"not_found": not_found})


@app.post("/members/search")
def search_submit(member_id: str = Form(...)) -> RedirectResponse:
    if member_id not in MEMBERS:
        return RedirectResponse(url=f"/members/search?not_found={member_id}", status_code=303)
    return RedirectResponse(url=f"/members/{member_id}/results", status_code=303)


@app.get("/members/{member_id}/results")
def results(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(url="/members/search", status_code=303)
    return templates.TemplateResponse(
        request, "results.html", {"member_id": member_id, "member": member}
    )


@app.get("/members/{member_id}/detail")
def detail(request: Request, member_id: str, inject: str | None = None):
    if inject == "500":
        return PlainTextResponse(
            "System Error 500: an internal failure occurred while loading this page.",
            status_code=500,
        )
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(url="/members/search", status_code=303)
    return templates.TemplateResponse(
        request, "detail.html", {"member_id": member_id, "member": member}
    )


@app.post("/members/{member_id}/change-limit")
def change_limit(member_id: str) -> PlainTextResponse:
    # Never actually reached: policy.yaml's IRREVERSIBLE tier refuses the
    # click on this control before the browser ever submits the form.
    return PlainTextResponse("limit changed")
