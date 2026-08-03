#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
import json
import os

import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
from loguru import logger
from markdownify import markdownify as md
from mcp import ServerSession
from mcp.server import FastMCP
from mcp.server.fastmcp import Context

from pypowsybl_mcp.tools import PyPowsyblTool
from pypowsybl_mcp.utils.user_session_management import get_session_id

URL_BASE = "https://powsybl.readthedocs.io/projects/pypowsybl/en/latest/reference"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}

# Upper bound on the markdown returned inline in a tool response. Returning a
# very large blob (tens of thousands of tokens) inline can freeze clients that
# render it eagerly. The full document is always kept in the cache and stays
# reachable through its resource URI, so truncation here never loses data.
MAX_INLINE_CONTENT_CHARS = 40000

# Some pypowsybl names are misleading: the verb does not describe what the
# method operates on, or the name reads as a module-level function when it is
# really a method on a class (e.g. Network.disconnect). An agent that trusts
# the name instead of the behaviour will misuse the API. We prepend a short
# clarifying banner to the documentation of such methods, so the correction
# reaches the agent exactly when it is reading that method — a place it cannot
# skim past, unlike the tool docstring or the skill.
#
# Keyed by (class_object, method_name) exactly as passed to get_online_resource.
# Add an entry whenever a naming confusion is actually observed; there is no
# need to predict them all up front.
KNOWN_NAMING_PITFALLS = {
    ("network", "Network.disconnect"): (
        "Disconnects ONE network element (line, generator, load, ...) by "
        "opening the switches that isolate it, given the element id. It does "
        "NOT tear down or disconnect the Network object itself. This is a "
        "method on the Network class, not a module-level function."
    ),
    ("network", "Network.connect"): (
        "Reconnects ONE network element by closing the switches that isolate "
        "it, given the element id. It does NOT connect the Network object to "
        "anything. This is a method on the Network class, not a module-level "
        "function."
    ),
}


def _pitfall_banner(class_object: str, method_name: str) -> str:
    """Return a leading warning banner for a known misleading name, else ''."""
    note = KNOWN_NAMING_PITFALLS.get((class_object, method_name))
    if not note:
        return ""
    return f"> **Naming note (read before using):** {note}\n\n"


def _truncate_for_inline(content: str, uri: str) -> str:
    """Cap inline markdown, pointing to the resource URI for the full content."""
    if len(content) <= MAX_INLINE_CONTENT_CHARS:
        return content
    dropped = len(content) - MAX_INLINE_CONTENT_CHARS
    return (
        content[:MAX_INLINE_CONTENT_CHARS]
        + f"\n\n[... truncated {dropped} characters. Read the full document from "
        + f"its resource URI '{uri}' (or narrow the request to a single method).]"
    )


def _extract_main_content(html: str) -> str:
    """Return only the main documentation region of a doc page.

    ReadTheDocs pages wrap the documentation in a large layout (navigation,
    sidebar, footer, search box); converting the whole page to markdown
    drowns the actual content in noise. Falls back to the full page when no
    main region is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = (
        soup.find(attrs={"role": "main"}) or soup.find("main") or soup.find("article")
    )
    return str(main) if main else html


def _extract_method_index(html: str, class_object: str) -> str:
    """Return a compact markdown index of the methods listed on an overview page.

    A module overview page (e.g. ``network.html``) documents the whole API on a
    single page: dozens of ``autosummary`` tables holding hundreds of methods.
    Converting the whole page to markdown produces a ~70 KB / ~18k-token blob
    that overwhelms clients (empty tab, frozen session). Instead we keep only
    the method names, grouped by section, plus their one-line descriptions —
    enough for an agent to discover the API and then fetch a single method's
    full documentation with ``method_name``.

    Returns an empty string when the page has no ``autosummary`` table, so the
    caller can fall back to the full-page conversion.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = (
        soup.find(attrs={"role": "main"}) or soup.find("main") or soup.find("article")
    )
    if main is None:
        return ""

    prefix = f"pypowsybl.{class_object}."
    lines = [f"# pypowsybl.{class_object} - method index", ""]
    found = False
    for el in main.descendants:
        name = getattr(el, "name", None)
        if name in ("h2", "h3"):
            heading = el.get_text().replace("¶", "").strip()
            if heading:
                lines.extend(["", f"## {heading}"])
        elif name == "table" and "autosummary" in (el.get("class") or []):
            for link in el.find_all("a", class_="reference internal"):
                title = link.get("title", "")
                if not title.startswith(prefix):
                    continue
                method = title[len(prefix) :]
                desc = ""
                cell = link.find_parent("td")
                if cell is not None:
                    sibling = cell.find_next_sibling("td")
                    if sibling is not None:
                        desc = " ".join(sibling.get_text().split())
                lines.append(f"- {method} - {desc}" if desc else f"- {method}")
                found = True

    return "\n".join(lines) if found else ""


def register_resource_tools(mcp: FastMCP, pypowsybl_proxies: TTLCache):
    tools = ResourceTools(pypowsybl_proxies)
    tools.register_tools_with_mcp(mcp)


class ResourceTools(PyPowsyblTool):
    async def get_online_resource(
        self,
        class_object: str,
        method_name: str = "",
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Fetch official pypowsybl API documentation and return it as markdown.

        CALL THIS TOOL WHENEVER YOU HAVE A PYPOWSYBL API QUESTION. That means
        any time you need a method's exact name, signature, parameters,
        accepted values, return type, or behaviour, and any time you are about
        to write or generate pypowsybl code. Do NOT answer such questions from
        prior knowledge: the pypowsybl API changes across versions and your
        recollection may be outdated, incomplete, or wrong. This documentation
        is the single source of truth — consult it first, then answer.
        (If you already fetched the page earlier in this session, use
        `read_resource` instead to get it back instantly without re-downloading.)

        This downloads a page from the pypowsybl online documentation
        (https://powsybl.readthedocs.io), converts it to markdown, caches it
        server-side and returns the markdown content directly in the response.
        The same content stays available from the cache via the `read_resource`
        tool (or the resource URI) until it expires.

        Workflow to discover a pypowsybl API:
        1. Call this tool with method_name="" to fetch a compact index of all
           available methods of class_object, grouped by section with a
           one-line description each (cached under resource id "{class_object}").
           Only the index is returned, not the full per-method docs, so the
           response stays small.
        2. Read the returned "content" and identify the method you need.
        3. Call this tool again with that method_name to fetch its detailed
           documentation (cached under "{class_object}-{method_name}").

        This tool always fetches a fresh copy. There is no need to call
        `read_resource` first on a page you have never fetched — go straight
        here. Reserve `read_resource` for re-reading a page already fetched
        this session.

        BEWARE MISLEADING NAMES. Pypowsybl method names do not always describe
        what the method operates on. Treat the fetched signature, parameter
        list and description as authoritative over what the name suggests, and
        confirm two things before using a method:
        (a) its scope — a module-level function (e.g.
            `pypowsybl.network.create_empty`, fetched with
            method_name="create_empty") versus a method on a class such as
            Network (e.g. `pypowsybl.network.Network.disconnect`, fetched with
            method_name="Network.disconnect"); and
        (b) what it acts on. Example: `Network.disconnect` does NOT tear down
            the Network object — it opens the switches isolating ONE element
            (line, generator, ...) given that element's id.
        When a fetched page carries a "Naming note" banner, read it first: it
        flags a name whose behaviour is commonly misread.

        Parameters:
            class_object: str
                The pypowsybl module to document. One of: "network", "loadflow",
                "rao", "security", "sensitivity", "flowdecomposition", "dynamic",
                "shortcircuit", "voltage_initializer".
            method_name: str, optional
                Method of the module to document. This is a module-level
                function name (e.g. "create_empty") or a class-qualified method
                (e.g. "Network.disconnect") exactly as it appears in the API
                page path. Leave empty to fetch the module overview page listing
                all methods.

        Returns:
            str
                JSON string. On success: {"success": true, "message": ...,
                "uri": "resources://temp/...", "content": "<markdown>"} — the
                documentation is in "content". Very large documents are
                truncated in "content" (with a marker); read the full version
                from the resource "uri". On failure: {"success": false,
                "error": ...}; no exception is raised.

        Examples:
            - class_object="network", method_name="" -> overview of all network methods
            - class_object="network", method_name="create_empty" -> documentation
              of pypowsybl.network.create_empty
        """

        if class_object not in [
            "network",
            "loadflow",
            "rao",
            "security",
            "sensitivity",
            "flowdecomposition",
            "dynamic",
            "shortcircuit",
            "voltage_initializer",
        ]:
            msg = f"Class object '{class_object}' not found"
            logger.warning(msg)
            return json.dumps(
                {"success": False, "error": msg},
                indent=2,
            )

        if method_name:
            url = f"{URL_BASE}/api/pypowsybl.{class_object}.{method_name}.html"
        else:
            url = f"{URL_BASE}/{class_object}.html"

        try:
            session_id = get_session_id(ctx)
            proxy = self.get_proxy(session_id)

            async with httpx.AsyncClient(
                proxy=os.getenv("https_proxy") or os.getenv("HTTPS_PROXY") or None,
                follow_redirects=True,
            ) as client:
                response = await client.get(url, headers=HEADERS)
                response.raise_for_status()

            html_content = response.text
            if method_name:
                content = _pitfall_banner(class_object, method_name) + md(
                    _extract_main_content(html_content)
                )
                resource_id = f"{class_object}-{method_name}"
                message = "Resource fetched successfully"
            else:
                # Overview pages document the whole API on one page; returning
                # the full conversion (~18k tokens of nested tables) freezes
                # clients. Return a compact method index instead and let the
                # agent fetch a single method's full docs with method_name.
                content = _extract_method_index(html_content, class_object)
                if not content:
                    content = md(_extract_main_content(html_content))
                resource_id = class_object
                message = (
                    f"Method index for '{class_object}' fetched successfully. "
                    "Call get_online_resource again with one of these method "
                    "names to get its full documentation."
                )

            uri = proxy.save_markdown_resource(content, resource_id)

            logger.info(f"Resource fetched successfully: {uri}")
            return json.dumps(
                {
                    "success": True,
                    "message": message,
                    "uri": uri,
                    "content": _truncate_for_inline(content, uri),
                },
                indent=2,
            )
        except Exception as e:
            return json.dumps(
                {"success": False, "error": f"Error fetching resource: {e}"}, indent=2
            )

    async def read_resource(
        self,
        resource_id: str,
        ctx: Context[ServerSession, None] = None,
    ) -> str:
        """
        Read a documentation resource already fetched this session with `get_online_resource`.

        Looks the markdown up in the server-side cache and returns it directly,
        with no download. Use it to cheaply get back a page that was already
        fetched earlier in the session. If the page has not been fetched yet,
        this returns {"success": false, ...} — go straight to
        `get_online_resource` to fetch it. Do NOT call `read_resource` first on
        a page you have never fetched; it will only miss.

        Parameters:
            resource_id: str
                Identifier of the cached resource, or its full URI. Accepts both
                "network", "network-create_empty" and the URI form
                "resources://temp/network-create_empty".

        Returns:
            str
                JSON string. On success: {"success": true, "uri": ...,
                "content": "<markdown>"}. Very large documents are truncated in
                "content" (with a marker); read the full version from the
                resource "uri". When the resource is absent or has
                expired: {"success": false, "error": ...} — fall back to
                `get_online_resource` to (re)fetch it. No exception is raised.
        """
        resource_id = resource_id.strip().removeprefix("resources://temp/")

        session_id = get_session_id(ctx)
        proxy = self.get_proxy(session_id)

        content = proxy.resources.get(resource_id)
        if content is None:
            msg = (
                f"Resource '{resource_id}' not found or has expired; "
                "fetch it with get_online_resource."
            )
            return json.dumps({"success": False, "error": msg}, indent=2)

        uri = f"resources://temp/{resource_id}"
        return json.dumps(
            {
                "success": True,
                "uri": uri,
                "content": _truncate_for_inline(content, uri),
            },
            indent=2,
        )
