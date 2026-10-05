# study-mcp

MCP bridge for the user's Moodle + Paperless-ngx study stack.

## Portainer
Add `MOODLE_TOKEN` and `PAPERLESS_TOKEN` to the existing `study` stack environment variables, then paste the contents of `portainer-service.yml` inside the existing `services:` block.

OpenAI Secure MCP Tunnel upstream:

`http://127.0.0.1:17311/mcp`

## Included write tools
- `complete_activity`
- `create_calendar_event`
- `update_document_title`
- `set_document_tags`

## Read / analysis tools
- Moodle courses, contents, completion, grades, assignments, submissions, upcoming calendar
- Paperless listing/search/OCR metadata
- PDF page rendering as MCP image content
- Combined study overview
