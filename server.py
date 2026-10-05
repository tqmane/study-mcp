import base64
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import fitz
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent


MOODLE_URL = os.environ["MOODLE_URL"].rstrip("/")
MOODLE_TOKEN = os.environ["MOODLE_TOKEN"]
MOODLE_HOST_HEADER = os.getenv("MOODLE_HOST_HEADER", "").strip()
PAPERLESS_URL = os.environ["PAPERLESS_URL"].rstrip("/")
PAPERLESS_TOKEN = os.environ["PAPERLESS_TOKEN"]

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "17311"))
STUDY_DB_PATH = os.getenv("STUDY_DB_PATH", "/data/study.sqlite3")


mcp = MCPServer(
    "Study Manager",
    instructions=(
        "Full read/write study manager. Moodle is the source of truth for courses, "
        "activities, completion, grades, assignments and calendar. Paperless-ngx is "
        "the source of truth for grade reports, mock-exam PDFs and OCR text. The local "
        "study database stores study sessions, mastery ratings and goals. Use named "
        "tools when possible. Destructive tools require confirm=true."
    ),
)


# ---------------------------------------------------------------------------
# Local study DB
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(STUDY_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(STUDY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS study_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            studied_at TEXT NOT NULL,
            subject TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT '',
            material TEXT NOT NULL DEFAULT '',
            minutes INTEGER NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mastery (
            subject TEXT NOT NULL,
            topic TEXT NOT NULL,
            level REAL NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(subject, topic)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            due_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def rows(items: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(x) for x in items]


# ---------------------------------------------------------------------------
# Moodle helpers
# ---------------------------------------------------------------------------


def _flatten(prefix: str, value: Any, output: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}[{key}]" if prefix else str(key), item, output)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{index}]", item, output)
    elif value is not None:
        output[prefix] = value


async def moodle_call(
    function: str,
    params: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": function,
        "moodlewsrestformat": "json",
    }

    if params:
        flattened: dict[str, Any] = {}
        for key, value in params.items():
            _flatten(key, value, flattened)
        payload.update(flattened)

    headers: dict[str, str] = {}
    if MOODLE_HOST_HEADER:
        # Moodle enforces $CFG->wwwroot/SITE_URL. Keep the public host while
        # routing the TCP connection over the private Docker network.
        headers["Host"] = MOODLE_HOST_HEADER
        headers["X-Forwarded-Host"] = MOODLE_HOST_HEADER
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Port"] = "443"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        response = await client.post(
            f"{MOODLE_URL}/webservice/rest/server.php",
            data=payload,
            headers=headers,
        )

        if response.is_redirect:
            raise RuntimeError(
                "Moodle redirected the REST request "
                f"(HTTP {response.status_code}, "
                f"location={response.headers.get('location', '<missing>')!r}). "
                "MOODLE_URL is intended to be the private Docker URL; "
                "set MOODLE_HOST_HEADER to the hostname from Moodle SITE_URL."
            )

        if response.status_code >= 400:
            snippet = response.text[:1200].replace("\n", " ")
            raise RuntimeError(
                "Moodle REST HTTP failure "
                f"(HTTP {response.status_code}, "
                f"content-type={response.headers.get('content-type', '<missing>')!r}, "
                f"body={snippet!r})."
            )

        try:
            data = response.json()
        except ValueError as exc:
            snippet = response.text[:1200].replace("\n", " ")
            raise RuntimeError(
                "Moodle returned a non-JSON REST response "
                f"(HTTP {response.status_code}, "
                f"content-type={response.headers.get('content-type', '<missing>')!r}, "
                f"body={snippet!r}). "
                "Check MOODLE_URL, MOODLE_HOST_HEADER, Moodle SITE_URL, "
                "and reverse-proxy / Cloudflare Access configuration."
            ) from exc

    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(
            f"Moodle API error {data.get('errorcode')}: {data.get('message')}"
        )
    return data


async def current_moodle_user() -> dict[str, Any]:
    return await moodle_call("core_webservice_get_site_info")


# ---------------------------------------------------------------------------
# Paperless helpers
# ---------------------------------------------------------------------------


def paperless_headers(json_response: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Token {PAPERLESS_TOKEN}"}
    if json_response:
        headers["Accept"] = "application/json; version=10"
    return headers


def validate_api_path(path: str) -> str:
    if not path.startswith("/api/"):
        raise ValueError("Paperless API path must start with /api/")
    return path


async def paperless_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> Any:
    path = validate_api_path(path)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.request(
            method.upper(),
            f"{PAPERLESS_URL}{path}",
            params=params,
            json=body,
            headers=paperless_headers(),
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {"ok": True, "status_code": response.status_code}
        try:
            return response.json()
        except ValueError:
            return {
                "ok": True,
                "status_code": response.status_code,
                "text": response.text,
            }


async def paperless_bytes(path: str) -> tuple[bytes, str]:
    path = validate_api_path(path)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(
            f"{PAPERLESS_URL}{path}",
            headers=paperless_headers(False),
        )
        response.raise_for_status()
        return response.content, response.headers.get(
            "content-type", "application/octet-stream"
        )


# ---------------------------------------------------------------------------
# Health / raw power-user tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def health() -> dict[str, Any]:
    """Check Moodle, Paperless and the local study database without failing the whole check."""
    result: dict[str, Any] = {}

    try:
        site = await current_moodle_user()
        result["moodle"] = {
            "ok": True,
            "site": site.get("sitename"),
            "username": site.get("username"),
            "user_id": site.get("userid"),
        }
    except Exception as exc:
        result["moodle"] = {"ok": False, "error": str(exc)}

    try:
        docs = await paperless_json("GET", "/api/documents/", params={"page_size": 1})
        result["paperless"] = {
            "ok": True,
            "document_count": docs.get("count", 0),
        }
    except Exception as exc:
        result["paperless"] = {"ok": False, "error": str(exc)}

    try:
        conn = db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        result["study_db"] = {"ok": True, "path": STUDY_DB_PATH}
    except Exception as exc:
        result["study_db"] = {"ok": False, "error": str(exc)}

    return result


@mcp.tool()
async def diagnose_moodle() -> dict[str, Any]:
    """Diagnose the private Moodle REST path and return a redacted response summary."""
    payload = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": "core_webservice_get_site_info",
        "moodlewsrestformat": "json",
    }
    headers: dict[str, str] = {}
    if MOODLE_HOST_HEADER:
        headers["Host"] = MOODLE_HOST_HEADER
        headers["X-Forwarded-Host"] = MOODLE_HOST_HEADER
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Port"] = "443"

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                f"{MOODLE_URL}/webservice/rest/server.php",
                data=payload,
                headers=headers,
            )
        body = response.text[:1600].replace("\n", " ")
        return {
            "ok": response.status_code < 400 and not response.is_redirect,
            "moodle_url": MOODLE_URL,
            "host_header": MOODLE_HOST_HEADER or None,
            "token_length": len(MOODLE_TOKEN),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "location": response.headers.get("location"),
            "body_preview": body,
        }
    except Exception as exc:
        return {
            "ok": False,
            "moodle_url": MOODLE_URL,
            "host_header": MOODLE_HOST_HEADER or None,
            "token_length": len(MOODLE_TOKEN),
            "transport_error": repr(exc),
        }


@mcp.tool()
async def moodle_call_raw(function: str, params: dict[str, Any] | None = None) -> Any:
    """POWER USER: call any Moodle function exposed by the Study MCP external service."""
    return await moodle_call(function, params or {})


@mcp.tool()
async def paperless_api_get(
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """POWER USER READ: GET any Paperless /api/ endpoint."""
    return await paperless_json("GET", path, params=params)


@mcp.tool()
async def paperless_api_write(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    confirm: bool = False,
) -> Any:
    """POWER USER WRITE: POST/PATCH/PUT/DELETE a Paperless /api/ endpoint."""
    method = method.upper()
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        raise ValueError("method must be POST, PATCH, PUT, or DELETE")
    if method == "DELETE" and not confirm:
        raise ValueError("DELETE requires confirm=true")
    return await paperless_json(method, path, body=body)


# ---------------------------------------------------------------------------
# Moodle read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_courses() -> Any:
    """List Moodle courses visible to the configured user."""
    return await moodle_call("core_course_get_courses")


@mcp.tool()
async def get_courses_by_field(field: str, value: str) -> Any:
    """Find Moodle courses by id, ids, shortname, idnumber or category."""
    return await moodle_call(
        "core_course_get_courses_by_field",
        {"field": field, "value": value},
    )


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
async def get_enrolled_users(course_id: int) -> Any:
    """Get users enrolled in a Moodle course."""
    return await moodle_call(
        "core_enrol_get_enrolled_users",
        {"courseid": course_id},
    )


@mcp.tool()
async def get_users_by_field(field: str, values: list[str]) -> Any:
    """Find Moodle users by an allowed field such as id, username or email."""
    return await moodle_call(
        "core_user_get_users_by_field",
        {"field": field, "values": values},
    )


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
    """Get completion states for all activities in a course."""
    if not user_id:
        site = await current_moodle_user()
        user_id = int(site["userid"])
    return await moodle_call(
        "core_completion_get_activities_completion_status",
        {"courseid": course_id, "userid": user_id},
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
    """Get Moodle assignments; omit course_ids for all visible courses."""
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
async def get_calendar_events(
    events: dict[str, Any] | None = None,
    options: list[dict[str, Any]] | None = None,
) -> Any:
    """Get Moodle calendar events using Moodle's flexible events/options filters."""
    return await moodle_call(
        "core_calendar_get_calendar_events",
        {"events": events or {}, "options": options or []},
    )


# ---------------------------------------------------------------------------
# Moodle write tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def complete_activity(cmid: int, completed: bool = True) -> Any:
    """WRITE: mark a manually tracked Moodle activity complete or incomplete."""
    return await moodle_call(
        "core_completion_update_activity_completion_status_manually",
        {"cmid": cmid, "completed": 1 if completed else 0},
    )


@mcp.tool()
async def create_calendar_event(
    name: str,
    timestart: int,
    description: str = "",
    course_id: int = 0,
    duration_seconds: int = 0,
) -> Any:
    """WRITE: create a Moodle user/course calendar event. timestart is Unix time."""
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
async def update_calendar_event_start_day(event_id: int, day_timestamp: int) -> Any:
    """WRITE: move a Moodle event to another day without changing its time-of-day."""
    return await moodle_call(
        "core_calendar_update_event_start_day",
        {"eventid": event_id, "daytimestamp": day_timestamp},
    )


@mcp.tool()
async def create_courses(courses: list[dict[str, Any]]) -> Any:
    """WRITE: create Moodle courses. Requires core_course_create_courses in Study MCP service."""
    return await moodle_call("core_course_create_courses", {"courses": courses})


@mcp.tool()
async def update_courses(courses: list[dict[str, Any]]) -> Any:
    """WRITE: update Moodle courses. Each object must include id. Requires function permission."""
    return await moodle_call("core_course_update_courses", {"courses": courses})


@mcp.tool()
async def delete_courses(course_ids: list[int], confirm: bool = False) -> Any:
    """DESTRUCTIVE: delete Moodle courses. Requires core_course_delete_courses."""
    if not confirm:
        raise ValueError("delete_courses requires confirm=true")
    return await moodle_call(
        "core_course_delete_courses",
        {"courseids": course_ids},
    )


# ---------------------------------------------------------------------------
# Paperless document reads
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_documents(limit: int = 20, ordering: str = "-created") -> Any:
    """List Paperless documents."""
    return await paperless_json(
        "GET",
        "/api/documents/",
        params={"page_size": max(1, min(limit, 100)), "ordering": ordering},
    )


@mcp.tool()
async def search_documents(query: str, limit: int = 20) -> Any:
    """Full-text search Paperless OCR text and metadata."""
    return await paperless_json(
        "GET",
        "/api/documents/",
        params={"query": query, "page_size": max(1, min(limit, 100))},
    )


@mcp.tool()
async def get_document(document_id: int) -> Any:
    """Get complete Paperless document metadata and OCR content."""
    return await paperless_json("GET", f"/api/documents/{document_id}/")


@mcp.tool()
async def get_document_text(document_id: int) -> dict[str, Any]:
    """Return a compact Paperless document title plus OCR/extracted text."""
    item = await paperless_json("GET", f"/api/documents/{document_id}/")
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "created": item.get("created"),
        "original_file_name": item.get("original_file_name"),
        "content": item.get("content", ""),
    }


@mcp.tool()
async def get_document_metadata(document_id: int) -> Any:
    """Get Paperless file metadata for a document."""
    return await paperless_json(
        "GET",
        f"/api/documents/{document_id}/metadata/",
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
    data, _ = await paperless_bytes(f"/api/documents/{document_id}/download/")
    pdf = fitz.open(stream=data, filetype="pdf")
    try:
        if page > pdf.page_count:
            raise ValueError(f"document has only {pdf.page_count} pages")
        p = pdf.load_page(page - 1)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(pix.tobytes("png")).decode("ascii"),
                mime_type="image/png",
            )
        ]
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# Paperless document writes
# ---------------------------------------------------------------------------


@mcp.tool()
async def update_document_metadata(
    document_id: int,
    title: str | None = None,
    created: str | None = None,
    correspondent_id: int | None = None,
    document_type_id: int | None = None,
    storage_path_id: int | None = None,
    tag_ids: list[int] | None = None,
    archive_serial_number: int | None = None,
) -> Any:
    """WRITE: update common Paperless document metadata fields."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if created is not None:
        body["created"] = created
    if correspondent_id is not None:
        body["correspondent"] = correspondent_id
    if document_type_id is not None:
        body["document_type"] = document_type_id
    if storage_path_id is not None:
        body["storage_path"] = storage_path_id
    if tag_ids is not None:
        body["tags"] = tag_ids
    if archive_serial_number is not None:
        body["archive_serial_number"] = archive_serial_number
    if not body:
        raise ValueError("no metadata fields supplied")
    return await paperless_json(
        "PATCH",
        f"/api/documents/{document_id}/",
        body=body,
    )


@mcp.tool()
async def set_document_tags(document_id: int, tag_ids: list[int]) -> Any:
    """WRITE: replace all tags on a Paperless document."""
    return await paperless_json(
        "PATCH",
        f"/api/documents/{document_id}/",
        body={"tags": tag_ids},
    )


@mcp.tool()
async def bulk_edit_documents(
    document_ids: list[int],
    method: str,
    parameters: dict[str, Any] | None = None,
) -> Any:
    """WRITE: run a Paperless bulk edit such as add_tag/remove_tag/reprocess/set_document_type."""
    return await paperless_json(
        "POST",
        "/api/documents/bulk_edit/",
        body={
            "documents": document_ids,
            "method": method,
            "parameters": parameters or {},
        },
    )


@mcp.tool()
async def reprocess_documents(document_ids: list[int]) -> Any:
    """WRITE: re-run Paperless consumption/OCR processing for documents."""
    return await bulk_edit_documents(document_ids, "reprocess", {})


@mcp.tool()
async def delete_document(document_id: int, confirm: bool = False) -> Any:
    """DESTRUCTIVE: move a Paperless document to trash."""
    if not confirm:
        raise ValueError("delete_document requires confirm=true")
    return await paperless_json("DELETE", f"/api/documents/{document_id}/")


@mcp.tool()
async def list_trash(limit: int = 50) -> Any:
    """List Paperless documents currently in trash."""
    return await paperless_json(
        "GET",
        "/api/trash/",
        params={"page_size": max(1, min(limit, 100))},
    )


@mcp.tool()
async def restore_documents(document_ids: list[int]) -> Any:
    """WRITE: restore Paperless documents from trash."""
    return await paperless_json(
        "POST",
        "/api/trash/",
        body={"action": "restore", "documents": document_ids},
    )


@mcp.tool()
async def upload_document_base64(
    filename: str,
    data_base64: str,
    title: str = "",
    tag_ids: list[int] | None = None,
    correspondent_id: int = 0,
    document_type_id: int = 0,
) -> Any:
    """WRITE: upload a document to Paperless from base64 bytes."""
    if not filename.strip():
        raise ValueError("filename must not be empty")

    try:
        raw = base64.b64decode(data_base64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("data_base64 is not valid base64") from exc

    if not raw:
        raise ValueError("decoded document is empty")

    # httpx AsyncClient must receive form fields as a mapping when files= is
    # present. Passing a list[tuple] here is interpreted as a synchronous
    # request body stream by httpx 0.28 and raises:
    # "Attempted to send an sync request with an AsyncClient instance."
    #
    # A list value in a mapping is encoded as repeated multipart fields, which
    # is exactly what Paperless expects for multiple tags.
    fields: dict[str, Any] = {}
    if title:
        fields["title"] = title
    if correspondent_id:
        fields["correspondent"] = str(correspondent_id)
    if document_type_id:
        fields["document_type"] = str(document_type_id)
    if tag_ids:
        fields["tags"] = [str(tag) for tag in tag_ids]

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{PAPERLESS_URL}/api/documents/post_document/",
            headers=paperless_headers(False),
            data=fields,
            files={"document": (filename, raw, "application/octet-stream")},
        )

        if response.status_code >= 400:
            snippet = response.text[:1200].replace("\n", " ")
            raise RuntimeError(
                "Paperless document upload failed "
                f"(HTTP {response.status_code}, body={snippet!r})"
            )

        try:
            return response.json()
        except ValueError:
            return {"task_id": response.text.strip()}


@mcp.tool()
async def get_paperless_tasks(task_id: str = "", limit: int = 20) -> Any:
    """Get Paperless background tasks, optionally filtering by task UUID."""
    params: dict[str, Any] = {"page_size": max(1, min(limit, 100))}
    if task_id:
        params["task_id"] = task_id
    return await paperless_json("GET", "/api/tasks/", params=params)


# ---------------------------------------------------------------------------
# Paperless taxonomy CRUD
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_tags(limit: int = 100) -> Any:
    """List Paperless tags."""
    return await paperless_json("GET", "/api/tags/", params={"page_size": limit})


@mcp.tool()
async def create_tag(name: str, color: str = "#a6cee3") -> Any:
    """WRITE: create a Paperless tag."""
    return await paperless_json(
        "POST", "/api/tags/", body={"name": name, "color": color}
    )


@mcp.tool()
async def update_tag(tag_id: int, name: str | None = None, color: str | None = None) -> Any:
    """WRITE: update a Paperless tag."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if color is not None:
        body["color"] = color
    return await paperless_json("PATCH", f"/api/tags/{tag_id}/", body=body)


@mcp.tool()
async def delete_tag(tag_id: int, confirm: bool = False) -> Any:
    """DESTRUCTIVE: delete a Paperless tag."""
    if not confirm:
        raise ValueError("delete_tag requires confirm=true")
    return await paperless_json("DELETE", f"/api/tags/{tag_id}/")


@mcp.tool()
async def list_correspondents(limit: int = 100) -> Any:
    """List Paperless correspondents."""
    return await paperless_json(
        "GET", "/api/correspondents/", params={"page_size": limit}
    )


@mcp.tool()
async def create_correspondent(name: str) -> Any:
    """WRITE: create a Paperless correspondent."""
    return await paperless_json("POST", "/api/correspondents/", body={"name": name})


@mcp.tool()
async def update_correspondent(correspondent_id: int, name: str) -> Any:
    """WRITE: rename a Paperless correspondent."""
    return await paperless_json(
        "PATCH",
        f"/api/correspondents/{correspondent_id}/",
        body={"name": name},
    )


@mcp.tool()
async def delete_correspondent(correspondent_id: int, confirm: bool = False) -> Any:
    """DESTRUCTIVE: delete a Paperless correspondent."""
    if not confirm:
        raise ValueError("delete_correspondent requires confirm=true")
    return await paperless_json(
        "DELETE", f"/api/correspondents/{correspondent_id}/"
    )


@mcp.tool()
async def list_document_types(limit: int = 100) -> Any:
    """List Paperless document types."""
    return await paperless_json(
        "GET", "/api/document_types/", params={"page_size": limit}
    )


@mcp.tool()
async def create_document_type(name: str) -> Any:
    """WRITE: create a Paperless document type."""
    return await paperless_json("POST", "/api/document_types/", body={"name": name})


@mcp.tool()
async def update_document_type(document_type_id: int, name: str) -> Any:
    """WRITE: rename a Paperless document type."""
    return await paperless_json(
        "PATCH",
        f"/api/document_types/{document_type_id}/",
        body={"name": name},
    )


@mcp.tool()
async def delete_document_type(document_type_id: int, confirm: bool = False) -> Any:
    """DESTRUCTIVE: delete a Paperless document type."""
    if not confirm:
        raise ValueError("delete_document_type requires confirm=true")
    return await paperless_json(
        "DELETE", f"/api/document_types/{document_type_id}/"
    )


@mcp.tool()
async def list_storage_paths(limit: int = 100) -> Any:
    """List Paperless storage paths."""
    return await paperless_json(
        "GET", "/api/storage_paths/", params={"page_size": limit}
    )


# ---------------------------------------------------------------------------
# Local study-log R/W
# ---------------------------------------------------------------------------


@mcp.tool()
async def log_study(
    subject: str,
    minutes: int,
    topic: str = "",
    material: str = "",
    notes: str = "",
    studied_at: str = "",
) -> dict[str, Any]:
    """WRITE: record a study session in the local persistent study database."""
    if minutes <= 0:
        raise ValueError("minutes must be > 0")
    conn = db()
    try:
        cur = conn.execute(
            """
            INSERT INTO study_logs(studied_at, subject, topic, material, minutes, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (studied_at or now_iso(), subject, topic, material, minutes, notes),
        )
        conn.commit()
        item = conn.execute(
            "SELECT * FROM study_logs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(item)
    finally:
        conn.close()


@mcp.tool()
async def list_study_logs(
    limit: int = 50,
    subject: str = "",
) -> list[dict[str, Any]]:
    """Read recent study sessions, optionally filtered by subject."""
    conn = db()
    try:
        if subject:
            result = conn.execute(
                "SELECT * FROM study_logs WHERE subject = ? ORDER BY studied_at DESC LIMIT ?",
                (subject, max(1, min(limit, 500))),
            ).fetchall()
        else:
            result = conn.execute(
                "SELECT * FROM study_logs ORDER BY studied_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return rows(result)
    finally:
        conn.close()


@mcp.tool()
async def update_study_log(
    log_id: int,
    subject: str | None = None,
    topic: str | None = None,
    material: str | None = None,
    minutes: int | None = None,
    notes: str | None = None,
    studied_at: str | None = None,
) -> dict[str, Any]:
    """WRITE: modify an existing local study session."""
    changes = {
        "subject": subject,
        "topic": topic,
        "material": material,
        "minutes": minutes,
        "notes": notes,
        "studied_at": studied_at,
    }
    values = [(k, v) for k, v in changes.items() if v is not None]
    if not values:
        raise ValueError("no fields supplied")
    if minutes is not None and minutes <= 0:
        raise ValueError("minutes must be > 0")
    sql = ", ".join(f"{key} = ?" for key, _ in values)
    conn = db()
    try:
        conn.execute(
            f"UPDATE study_logs SET {sql} WHERE id = ?",
            [v for _, v in values] + [log_id],
        )
        conn.commit()
        item = conn.execute("SELECT * FROM study_logs WHERE id = ?", (log_id,)).fetchone()
        if item is None:
            raise ValueError("study log not found")
        return dict(item)
    finally:
        conn.close()


@mcp.tool()
async def delete_study_log(log_id: int, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: delete a local study session."""
    if not confirm:
        raise ValueError("delete_study_log requires confirm=true")
    conn = db()
    try:
        cur = conn.execute("DELETE FROM study_logs WHERE id = ?", (log_id,))
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        conn.close()


@mcp.tool()
async def set_mastery(
    subject: str,
    topic: str,
    level: float,
    notes: str = "",
) -> dict[str, Any]:
    """WRITE: set a 0-5 mastery rating for a subject/topic."""
    if not 0 <= level <= 5:
        raise ValueError("level must be between 0 and 5")
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO mastery(subject, topic, level, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(subject, topic) DO UPDATE SET
              level = excluded.level,
              notes = excluded.notes,
              updated_at = excluded.updated_at
            """,
            (subject, topic, level, notes, now_iso()),
        )
        conn.commit()
        item = conn.execute(
            "SELECT * FROM mastery WHERE subject = ? AND topic = ?",
            (subject, topic),
        ).fetchone()
        return dict(item)
    finally:
        conn.close()


@mcp.tool()
async def list_mastery(subject: str = "") -> list[dict[str, Any]]:
    """List mastery ratings, optionally filtered by subject."""
    conn = db()
    try:
        if subject:
            result = conn.execute(
                "SELECT * FROM mastery WHERE subject = ? ORDER BY level ASC, topic",
                (subject,),
            ).fetchall()
        else:
            result = conn.execute(
                "SELECT * FROM mastery ORDER BY subject, level ASC, topic"
            ).fetchall()
        return rows(result)
    finally:
        conn.close()


@mcp.tool()
async def delete_mastery(subject: str, topic: str, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: remove a mastery rating."""
    if not confirm:
        raise ValueError("delete_mastery requires confirm=true")
    conn = db()
    try:
        cur = conn.execute(
            "DELETE FROM mastery WHERE subject = ? AND topic = ?",
            (subject, topic),
        )
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        conn.close()


@mcp.tool()
async def create_goal(
    title: str,
    subject: str = "",
    target: str = "",
    due_date: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """WRITE: create a persistent study goal."""
    stamp = now_iso()
    conn = db()
    try:
        cur = conn.execute(
            """
            INSERT INTO goals(title, subject, target, due_date, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (title, subject, target, due_date, notes, stamp, stamp),
        )
        conn.commit()
        item = conn.execute("SELECT * FROM goals WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(item)
    finally:
        conn.close()


@mcp.tool()
async def list_goals(status: str = "") -> list[dict[str, Any]]:
    """List persistent study goals."""
    conn = db()
    try:
        if status:
            result = conn.execute(
                "SELECT * FROM goals WHERE status = ? ORDER BY due_date, id",
                (status,),
            ).fetchall()
        else:
            result = conn.execute(
                "SELECT * FROM goals ORDER BY status, due_date, id"
            ).fetchall()
        return rows(result)
    finally:
        conn.close()


@mcp.tool()
async def update_goal(
    goal_id: int,
    title: str | None = None,
    subject: str | None = None,
    target: str | None = None,
    due_date: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """WRITE: update a persistent study goal."""
    changes = {
        "title": title,
        "subject": subject,
        "target": target,
        "due_date": due_date,
        "status": status,
        "notes": notes,
    }
    values = [(k, v) for k, v in changes.items() if v is not None]
    if not values:
        raise ValueError("no fields supplied")
    values.append(("updated_at", now_iso()))
    sql = ", ".join(f"{key} = ?" for key, _ in values)
    conn = db()
    try:
        conn.execute(
            f"UPDATE goals SET {sql} WHERE id = ?",
            [v for _, v in values] + [goal_id],
        )
        conn.commit()
        item = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if item is None:
            raise ValueError("goal not found")
        return dict(item)
    finally:
        conn.close()


@mcp.tool()
async def delete_goal(goal_id: int, confirm: bool = False) -> dict[str, Any]:
    """DESTRUCTIVE: delete a persistent study goal."""
    if not confirm:
        raise ValueError("delete_goal requires confirm=true")
    conn = db()
    try:
        cur = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Combined dashboard
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_study_overview() -> dict[str, Any]:
    """Combine Moodle, Paperless and local study tracking into one dashboard payload."""
    site = await current_moodle_user()
    courses = await moodle_call(
        "core_enrol_get_users_courses",
        {"userid": int(site["userid"])},
    )
    docs = await paperless_json(
        "GET",
        "/api/documents/",
        params={"page_size": 10, "ordering": "-created"},
    )
    conn = db()
    try:
        logs = rows(
            conn.execute(
                "SELECT * FROM study_logs ORDER BY studied_at DESC LIMIT 20"
            ).fetchall()
        )
        mastery = rows(
            conn.execute(
                "SELECT * FROM mastery ORDER BY level ASC, updated_at DESC LIMIT 50"
            ).fetchall()
        )
        goals = rows(
            conn.execute(
                "SELECT * FROM goals WHERE status != 'done' ORDER BY due_date, id LIMIT 50"
            ).fetchall()
        )
    finally:
        conn.close()

    return {
        "user": {
            "id": site.get("userid"),
            "username": site.get("username"),
            "fullname": site.get("fullname"),
        },
        "courses": courses,
        "recent_documents": docs.get("results", []),
        "recent_study_logs": logs,
        "weakest_mastery": mastery,
        "active_goals": goals,
    }


if __name__ == "__main__":
    db().close()
    mcp.run(
        transport="streamable-http",
        host=MCP_HOST,
        port=MCP_PORT,
        json_response=True,
        stateless_http=True,
    )
