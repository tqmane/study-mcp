import os
import base64
from typing import Any

import fitz
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent

MOODLE_URL = os.environ["MOODLE_URL"].rstrip("/")
MOODLE_TOKEN = os.environ["MOODLE_TOKEN"]
PAPERLESS_URL = os.environ["PAPERLESS_URL"].rstrip("/")
PAPERLESS_TOKEN = os.environ["PAPERLESS_TOKEN"]
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "17311"))

mcp = MCPServer(
    "Study Manager",
    instructions=(
        "Use Moodle for courses, activities, completion, grades, assignments and calendar. "
        "Use Paperless-ngx for grade reports, mock-exam PDFs and OCR text. "
        "Write tools should only make the specific change requested by the user."
    ),
)


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif value is not None:
        out[prefix] = value


async def moodle_call(function: str, params: dict[str, Any] | None = None) -> Any:
    payload: dict[str, Any] = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": function,
        "moodlewsrestformat": "json",
    }
    if params:
        flat: dict[str, Any] = {}
        for k, v in params.items():
            _flatten(k, v, flat)
        payload.update(flat)

    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(
            f"{MOODLE_URL}/webservice/rest/server.php",
            data=payload,
        )
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(
            f"Moodle API error {data.get('errorcode')}: {data.get('message')}"
        )
    return data


def paperless_headers(json_accept: bool = True) -> dict[str, str]:
    h = {"Authorization": f"Token {PAPERLESS_TOKEN}"}
    if json_accept:
        h["Accept"] = "application/json"
    return h


async def paperless_get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(
            f"{PAPERLESS_URL}{path}",
            params=params,
            headers=paperless_headers(),
        )
        r.raise_for_status()
        return r.json()


async def paperless_patch_json(path: str, body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.patch(
            f"{PAPERLESS_URL}{path}",
            json=body,
            headers=paperless_headers(),
        )
        r.raise_for_status()
        return r.json()


async def paperless_get_bytes(path: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        r = await client.get(
            f"{PAPERLESS_URL}{path}",
            headers=paperless_headers(json_accept=False),
        )
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "application/octet-stream")


async def current_moodle_user() -> dict[str, Any]:
    return await moodle_call("core_webservice_get_site_info")


@mcp.tool()
async def health() -> dict[str, Any]:
    """Check Moodle and Paperless connectivity."""
    site = await current_moodle_user()
    docs = await paperless_get_json("/api/documents/", {"page_size": 1})
    return {
        "moodle": {
            "ok": True,
            "site": site.get("sitename"),
            "username": site.get("username"),
            "user_id": site.get("userid"),
        },
        "paperless": {
            "ok": True,
            "document_count": docs.get("count", 0),
        },
    }


@mcp.tool()
async def list_courses() -> Any:
    """List Moodle courses visible to the configured user."""
    return await moodle_call("core_course_get_courses")


@mcp.tool()
async def get_my_courses() -> Any:
    """Get courses in which the configured Moodle user is enrolled."""
    site = await current_moodle_user()
    return await moodle_call(
        "core_enrol_get_users_courses",
        {"userid": int(site["userid"])},
    )


@mcp.tool()
async def get_course_contents(course_id: int) -> Any:
    """Get sections, activities and resources in a Moodle course."""
    return await moodle_call("core_course_get_contents", {"courseid": course_id})


@mcp.tool()
async def get_course_completion(course_id: int, user_id: int = 0) -> Any:
    """Get course completion. user_id=0 means current Moodle user."""
    if not user_id:
        site = await current_moodle_user()
        user_id = int(site["userid"])
    return await moodle_call(
        "core_completion_get_course_completion_status",
        {"courseid": course_id, "userid": user_id},
    )


@mcp.tool()
async def get_activity_completion(course_id: int, user_id: int = 0) -> Any:
    """Get completion states for activities in a course."""
    if not user_id:
        site = await current_moodle_user()
        user_id = int(site["userid"])
    return await moodle_call(
        "core_completion_get_activities_completion_status",
        {"courseid": course_id, "userid": user_id},
    )


@mcp.tool()
async def complete_activity(cmid: int, completed: bool = True) -> Any:
    """WRITE: mark a manually tracked Moodle activity complete or incomplete."""
    return await moodle_call(
        "core_completion_update_activity_completion_status_manually",
        {"cmid": cmid, "completed": 1 if completed else 0},
    )


@mcp.tool()
async def get_grades(course_id: int, user_id: int = 0) -> Any:
    """Get grade items for a course. user_id=0 means current Moodle user."""
    if not user_id:
        site = await current_moodle_user()
        user_id = int(site["userid"])
    return await moodle_call(
        "gradereport_user_get_grade_items",
        {"courseid": course_id, "userid": user_id},
    )


@mcp.tool()
async def get_assignments(course_ids: list[int] | None = None) -> Any:
    """Get Moodle assignments. Leave course_ids empty for all visible courses."""
    params: dict[str, Any] = {}
    if course_ids:
        params["courseids"] = course_ids
    return await moodle_call("mod_assign_get_assignments", params)


@mcp.tool()
async def get_submission_status(assignment_id: int, user_id: int = 0) -> Any:
    """Get assignment submission status."""
    params: dict[str, Any] = {"assignid": assignment_id}
    if user_id:
        params["userid"] = user_id
    return await moodle_call("mod_assign_get_submission_status", params)


@mcp.tool()
async def get_upcoming_calendar(course_id: int = 0) -> Any:
    """Get Moodle upcoming calendar. course_id=0 means user/site-wide."""
    return await moodle_call(
        "core_calendar_get_calendar_upcoming_view",
        {"courseid": course_id},
    )


@mcp.tool()
async def create_calendar_event(
    name: str,
    timestart: int,
    description: str = "",
    course_id: int = 0,
    duration_seconds: int = 0,
) -> Any:
    """WRITE: create a Moodle calendar event. timestart is a Unix timestamp."""
    event = {
        "name": name,
        "description": description,
        "format": 1,
        "courseid": course_id,
        "groupid": 0,
        "repeats": 0,
        "eventtype": "course" if course_id else "user",
        "timestart": timestart,
        "timeduration": max(0, duration_seconds),
        "visible": 1,
        "sequence": 1,
    }
    return await moodle_call(
        "core_calendar_create_calendar_events",
        {"events": [event]},
    )


@mcp.tool()
async def list_documents(limit: int = 20) -> Any:
    """List recent Paperless documents."""
    limit = max(1, min(limit, 100))
    return await paperless_get_json(
        "/api/documents/",
        {"page_size": limit, "ordering": "-created"},
    )


@mcp.tool()
async def search_documents(query: str, limit: int = 20) -> Any:
    """Search Paperless document metadata and OCR text."""
    limit = max(1, min(limit, 100))
    return await paperless_get_json(
        "/api/documents/",
        {"query": query, "page_size": limit},
    )


@mcp.tool()
async def get_document(document_id: int) -> Any:
    """Get Paperless document metadata including OCR content."""
    return await paperless_get_json(f"/api/documents/{document_id}/")


@mcp.tool()
async def update_document_title(document_id: int, title: str) -> Any:
    """WRITE: change the title of a Paperless document."""
    return await paperless_patch_json(
        f"/api/documents/{document_id}/",
        {"title": title},
    )


@mcp.tool()
async def set_document_tags(document_id: int, tag_ids: list[int]) -> Any:
    """WRITE: replace the Paperless tags on a document with the given tag IDs."""
    return await paperless_patch_json(
        f"/api/documents/{document_id}/",
        {"tags": tag_ids},
    )


@mcp.tool()
async def get_document_page_image(
    document_id: int,
    page: int = 1,
    zoom: float = 1.6,
) -> list[ImageContent]:
    """Render a Paperless PDF page as PNG for visual inspection of tables/charts."""
    if page < 1:
        raise ValueError("page is 1-based and must be >= 1")
    zoom = max(0.8, min(float(zoom), 3.0))
    data, _ = await paperless_get_bytes(f"/api/documents/{document_id}/download/")
    pdf = fitz.open(stream=data, filetype="pdf")
    try:
        if page > pdf.page_count:
            raise ValueError(f"document has only {pdf.page_count} pages")
        p = pdf.load_page(page - 1)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        png = pix.tobytes("png")
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(png).decode("ascii"),
                mime_type="image/png",
            )
        ]
    finally:
        pdf.close()


@mcp.tool()
async def get_study_overview() -> dict[str, Any]:
    """Combine current Moodle courses with recent Paperless study documents."""
    site = await current_moodle_user()
    courses = await moodle_call(
        "core_enrol_get_users_courses",
        {"userid": int(site["userid"])},
    )
    docs = await paperless_get_json(
        "/api/documents/",
        {"page_size": 10, "ordering": "-created"},
    )
    return {
        "user": {
            "id": site.get("userid"),
            "username": site.get("username"),
            "fullname": site.get("fullname"),
        },
        "courses": courses,
        "recent_documents": docs.get("results", []),
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        json_response=True,
        stateless_http=True,
    )
