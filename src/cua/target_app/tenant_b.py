"""Northgate Federal Credit Union: a second tenant running the SAME vendor
product (acme_core) as the base app.py, mounted at /tenant-b/... -- the
live fixture for REPORT.md sec 4's multi-tenant reuse claim.

Same underlying data (MEMBERS), same overall flow (search -> results ->
detail), deliberately different markup wherever a real older build of the
same product would diverge:

    the member-id field's label       "Acct/Member #:" not "Member Number:"
    a mandatory branch selector       the base tenant's build has none at all
    the detail-view control           a differently-classed <div>, "Details"
                                       not "View" (still no accessible role,
                                       so still exercises the css fallback --
                                       just a DIFFERENT selector)
    the savings-balance row label     "SAV BAL" not "Savings Balance"

Deliberately UNCHANGED: the "Search" button's accessible name, and the
Member Detail heading text. Both survive across builds in the real world
(a submit button's role/name and a page's own heading are the most stable
things about a UI), and an overlay that "fixed" them anyway would be
overriding something that isn't actually broken.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .data import MEMBERS

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "tenant_b"))


@router.get("/members/search")
def search_form(request: Request, not_found: str | None = None):
    return templates.TemplateResponse(request, "search.html", {"not_found": not_found})


@router.post("/members/search")
def search_submit(member_id: str = Form(...), branch: str = Form(...)) -> RedirectResponse:
    # `branch` is real, required input on this tenant's form (the browser
    # itself won't submit without it -- see the <select required> in
    # search.html) but this mock has only one branch's worth of data to
    # serve, so the value doesn't change which record comes back.
    if member_id not in MEMBERS:
        return RedirectResponse(url=f"/tenant-b/members/search?not_found={member_id}", status_code=303)
    return RedirectResponse(url=f"/tenant-b/members/{member_id}/results", status_code=303)


@router.get("/members/{member_id}/results")
def results(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(url="/tenant-b/members/search", status_code=303)
    return templates.TemplateResponse(
        request, "results.html", {"member_id": member_id, "member": member}
    )


@router.get("/members/{member_id}/detail")
def detail(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        return RedirectResponse(url="/tenant-b/members/search", status_code=303)
    return templates.TemplateResponse(
        request, "detail.html", {"member_id": member_id, "member": member}
    )
