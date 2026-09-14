"""WebAdapter: the only place in this codebase that imports Playwright.

Headed, not headless (REPORT.md sec 1): human takeover of the *same* live
session is a core requirement, and a visible window gives that for free --
automation stops touching the page, the operator clicks the window that is
already open.

observe() tags every candidate element with a `data-cua-id` attribute in one
round trip and returns a plain SurfaceSnapshot; resolve() hands that
snapshot to the pure resolve_against_snapshot() in resolve.py and does
nothing else. act() re-locates a resolved node by that same attribute --
the adapter never resolves an `{{input.x}}` / `{{ctx.x}}` reference itself,
only ever a literal `value` the caller already worked out.
"""

from __future__ import annotations

import time

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from cua.schema import Action, Click, Condition, Navigate, Read, Select, Target, TypeText, Wait

from .protocol import InteractiveNode, Location, ResolutionResult, SurfaceAdapter, SurfaceSnapshot
from .resolve import evaluate_condition, resolve_against_snapshot

# One evaluate() call builds the whole snapshot and tags each node in place.
# Heuristic, not a full accessibility tree: it covers exactly the roles this
# project's locator layers need (button/link/textbox/combobox/heading), plus
# a text-only fallback for legacy label cells that have no role at all.
_OBSERVE_JS = """
() => {
  function role(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'input' || tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    return null;
  }
  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'a' || /^h[1-6]$/.test(tag)) {
      return el.textContent.trim() || null;
    }
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (el.id) {
        const lbl = document.querySelector(`label[for="${el.id}"]`);
        if (lbl) return lbl.textContent.trim();
      }
      return null;
    }
    return null;
  }
  const nodes = [];
  let i = 0;
  document.querySelectorAll('body, body *').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const hasOnclick = el.hasAttribute('onclick');
    const interactive = ['button', 'a', 'input', 'select', 'textarea'].includes(tag) || hasOnclick;
    const ownText = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join(' ')
      .trim();
    const r = role(el);
    if (!interactive && !ownText && !r) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    const nodeId = 'n' + i++;
    el.setAttribute('data-cua-id', nodeId);
    let value = null;
    if (tag === 'input' || tag === 'select' || tag === 'textarea') value = el.value;
    nodes.push({
      node_id: nodeId,
      role: r,
      name: accessibleName(el),
      text: ownText,
      value: value,
      tag: tag,
      attrs: { id: el.id || '', class: el.className || '' },
      bbox: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
      interactive: interactive,
    });
  });
  return nodes;
}
"""


class WebAdapter(SurfaceAdapter):
    def __init__(self, headless: bool = False) -> None:
        self._pw: Playwright = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless)
        self.page: Page = self._browser.new_page()

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()

    def observe(self) -> SurfaceSnapshot:
        raw = self.page.evaluate(_OBSERVE_JS)
        return [InteractiveNode.model_validate(n) for n in raw]

    def resolve(self, target: Target) -> ResolutionResult:
        return resolve_against_snapshot(target, self.observe())

    def act(
        self,
        action: Action,
        resolution: ResolutionResult | None = None,
        value: str | None = None,
    ) -> str | None:
        if isinstance(action, Navigate):
            self.page.goto(value if value is not None else action.url_template)
            return None
        if isinstance(action, Wait):
            return None  # the executor calls wait_for() itself; nothing to do here

        if resolution is None or not resolution.resolved or resolution.node is None:
            raise LookupError(f"{action.type!r} acts on a control, but nothing was resolved")
        locator = self.page.locator(f'[data-cua-id="{resolution.node.node_id}"]')

        if isinstance(action, Click):
            locator.click()
            return None
        if isinstance(action, TypeText):
            if value is None:
                raise ValueError("TypeText requires a resolved literal `value`")
            if action.clear_first:
                locator.fill("")
            locator.fill(value)
            if action.submit:
                locator.press("Enter")
            return None
        if isinstance(action, Select):
            if value is None:
                raise ValueError("Select requires a resolved literal `value`")
            kwargs = {"label": value} if action.by == "label" else {action.by: value}
            locator.select_option(**kwargs)
            return None
        if isinstance(action, Read):
            if action.attribute:
                return locator.get_attribute(action.attribute)
            return locator.inner_text()
        raise TypeError(f"unhandled action type: {action!r}")

    def wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        location = self.location()
        while True:
            snapshot = self.observe()
            if evaluate_condition(condition, snapshot, location):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def location(self) -> Location:
        url = self.page.url
        # split "scheme://host:port" from "/path?query"
        scheme_sep = url.find("://")
        if scheme_sep == -1:
            return Location(origin="", path=url)
        path_start = url.find("/", scheme_sep + 3)
        if path_start == -1:
            return Location(origin=url, path="/")
        return Location(origin=url[:path_start], path=url[path_start:])
