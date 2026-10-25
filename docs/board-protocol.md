# The pl Board Protocol

The pl board protocol defines standard MCP tools for tracking boards. When an MCP server implements these tools or answers `pl_protocol`, pl can list, read, create, update, move and delete cards directly without requiring custom tool mappings.

## 1. Tool Table

A conforming server provides nine standard tools prefixed with `pl_`:

| Tool | Parameters | Returns | Purpose |
| --- | --- | --- | --- |
| `pl_protocol` | `board_id?` (string) | `{"version": 1, "boards": [{"id": "...", "name": "..."}]}` | Discover protocol version and available boards |
| `pl_columns` | `board_id?` (string) | `{"columns": [{"id": "...", "title": "..."}]}` | List board columns |
| `pl_ensure_column` | `title` (string), `description?` (string), `board_id?` (string) | `{"column": {"id": "...", "title": "..."}}` | Ensure a column exists (idempotent) |
| `pl_cards` | `cursor?` (string), `limit?` (int), `assigned_to?` (string), `updated_since?` (string), `board_id?` (string) | `{"cards": [...], "next_cursor": "..." \| null}` | List cards on the board with cursor paging |
| `pl_card` | `id` (string), `board_id?` (string) | `{"card": {...}}` | Fetch a single card by id |
| `pl_create` | `column_id` (string), `title` (string), `description?` (string), `tags?` (list[string]), `metadata?` (object), `assigned_to?` (string), `board_id?` (string) | `{"card": {...}}` | Create a new card in the given column |
| `pl_update` | `id` (string), `fields` (object), `board_id?` (string) | `{"card": {...}}` | Update card fields; replaces metadata whole |
| `pl_move` | `id` (string), `column_id` (string), `board_id?` (string) | `{"card": {...}}` | Move a card to a different column |
| `pl_delete` | `id` (string), `board_id?` (string) | `{"deleted": true}` | Delete a card by id |

All replies return JSON in the tool result's first text part.

## 2. Card Contract

A card object returned by `pl_card`, `pl_cards`, `pl_create`, `pl_update`, and `pl_move` contains:

- `id`: unique card identifier string.
- `board_id`: the board identifier.
- `list_id`: the column identifier where the card resides.
- `title`: card title string.
- `description`: card description text (holds `# PIPELINE:` Markdown sections).
- `tags`: list of string tags.
- `metadata`: JSON object stored and returned as-is (supports nested structures and unicode).
- `assigned_to`: string identifying the assignee, or null.
- `updated_at`: ISO 8601 timestamp string including timezone offset (e.g., `2026-10-23T12:00:00+00:00`).

### Paging Contract
`pl_cards` returns a page of cards and a `next_cursor` token. The client passes `cursor` to retrieve subsequent pages until `next_cursor` is null. Every card must be returned exactly once across pages.

## 3. Error Codes

When a tool encounters an error, it returns an error result or message prefixed with a standard error code:

- `not_found`: the requested card id or board id does not exist.
- `invalid`: invalid column id, unknown field name, or malformed parameter.
- `conflict`: concurrent edit detected; pl re-reads the card and retries the write once.
- `forbidden`: operation is unauthorized or permission is denied.

## 4. Conformance Rules (R1–R11)

The conformance test suite (`pl board check <server>`) validates implementations against eleven rules:

- **R1**: `pl_protocol` returns version 1 and boards.
- **R2**: `pl_columns` returns `{id, title}` rows.
- **R3**: `pl_create` returns a card with every key and an `updated_at` that carries an offset.
- **R4**: metadata round-trips exactly (nested objects, unicode, arrays, and 40 keys).
- **R5**: `pl_update` merges fields and replaces metadata whole.
- **R6**: paging returns every card once with `limit=2` and 5 cards.
- **R7**: `pl_move` changes `list_id`.
- **R8**: an unknown id errors with `not_found`.
- **R9**: an unknown column errors with `invalid`.
- **R10**: `pl_ensure_column` is idempotent.
- **R11**: `pl_delete` returns `{deleted: true}` and the card is gone.
