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

Phase B is happy-path only: one member (12345), no error injection yet.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .data import MEMBERS

app = FastAPI(title="Member Servicing Console")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/members/search", status_code=303)


@app.get("/members/search")
def search_form(request: Request):
    return templates.TemplateResponse(request, "search.html", {})


@app.post("/members/search")
def search_submit(member_id: str = Form(...)) -> RedirectResponse:
    if member_id not in MEMBERS:
        # Happy-path only for now: a real "no such member" business outcome,
        # with something the discovery loop's hardening pass can detect,
        # lands in a later phase.
        return RedirectResponse(url="/members/search", status_code=303)
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
def detail(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(url="/members/search", status_code=303)
    return templates.TemplateResponse(
        request, "detail.html", {"member_id": member_id, "member": member}
    )
