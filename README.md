# Study MCP

Full read/write MCP bridge for a personal Moodle + Paperless-ngx study stack.

## Architecture

```text
ChatGPT Plugin
    |
OpenAI Secure MCP Tunnel
    |
http://127.0.0.1:17311/mcp
    |
Study MCP
  |-- Moodle REST
  |-- Paperless REST
  +-- local SQLite (/data/study.sqlite3)
```

## Portainer

Add these stack environment variables:

```text
MOODLE_TOKEN=<Study MCP Moodle token>
PAPERLESS_TOKEN=<Paperless admin API token>
```

Paste `portainer-service.yml` inside the existing `services:` block.

Also add this to the existing top-level `volumes:` block:

```yaml
  study-mcp-data:
```

Then update the stack.

## OAI Tunnel upstream

```text
http://127.0.0.1:17311/mcp
```

## Tool groups

### Moodle read
- health
- list_courses
- get_courses_by_field
- get_my_courses
- get_course_contents
- get_enrolled_users
- get_users_by_field
- get_course_completion
- get_activity_completion
- get_grades
- get_assignments
- get_submission_status
- get_upcoming_calendar
- get_calendar_events

### Moodle write
- complete_activity
- create_calendar_event
- update_calendar_event_start_day
- create_courses
- update_courses
- delete_courses
- moodle_call_raw

Course create/update/delete require these functions to be added to the Moodle **Study MCP** external service:
- `core_course_create_courses`
- `core_course_update_courses`
- `core_course_delete_courses`

### Paperless read
- list_documents
- search_documents
- get_document
- get_document_text
- get_document_metadata
- get_document_page_image
- list_trash
- get_paperless_tasks
- list_tags
- list_correspondents
- list_document_types
- list_storage_paths
- paperless_api_get

### Paperless write
- update_document_metadata
- set_document_tags
- bulk_edit_documents
- reprocess_documents
- delete_document
- restore_documents
- upload_document_base64
- create_tag / update_tag / delete_tag
- create_correspondent / update_correspondent / delete_correspondent
- create_document_type / update_document_type / delete_document_type
- paperless_api_write

### Local study tracking R/W
- log_study
- list_study_logs
- update_study_log
- delete_study_log
- set_mastery
- list_mastery
- delete_mastery
- create_goal
- list_goals
- update_goal
- delete_goal
- get_study_overview

Destructive named tools require `confirm=true`.
