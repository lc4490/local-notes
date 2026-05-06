# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
python3 notes.py
```

**Never launch the app from within a Claude session** — kill existing instances instead:
```bash
pkill -f "python3 notes.py"
```

## Dependencies

```bash
pip install PySide6 autocorrect
pip install cryptography  # optional — enables password encryption
```

## Architecture

Single-file desktop app (`notes.py`) built with PySide6/Qt6. Three-panel layout mimicking Apple Notes dark mode.

**Storage:** Filesystem-based. Notes are `.md` files inside subdirectories of the root folder (configured in `config.json`). Each subdirectory is a notebook. The root defaults to `~/notes/`.

**UI layout:** `QSplitter` with three panels — sidebar (notebooks), note list, editor. All palette constants are module-level: `BG0/BG1/BG2/DIV/SEL/T1/T2/T3/ACC`.

**Data flow:** `load_all()` builds `NotesApp.current_notes` (`{path: {title, notebook, modified, preview}}`). `_flush_save()` writes editor contents back to disk — debounced 600ms, gated by `_dirty` flag.

**Config:** `config.json` stores `root` (notes folder path) and `pinned` (list of pinned note paths).

## Note Types

Three note types, detected on open in `_open_note()`:

| Type | Detection | View |
|------|-----------|------|
| Regular | default | `QTextEdit` editor |
| Password | `_is_password_note(raw)` | `PasswordNoteView` |
| Budget | `_is_budget_note(raw)` / `_BDG_MARKER` | `BudgetNoteView` |

**Special notebooks** (`_SPECIAL_NBS = {PASSWORDS_NB, BUDGET_NB}`) are protected from rename/delete and appear as a separate sidebar section with custom icons.

## File Format

```
# Title

{body}
```

Budget notes embed structured data in the body:
```
<!-- budget-note -->
INITIAL:6637.02
TARGET:5000.0
| Description | Amount | Date | Category | Fixed |
|---|---|---|---|---|
| Rent | -1116 | 5/3 | Rent | 1 |
```

## Budget Tracker

- `_parse_budget_data(content)` → `(initial, target, rows)`
- `_budget_to_str(initial, target, rows)` → str
- Row field order in UI: **Fixed toggle | Amount | Description | Category | Date | Delete**
- Stats bar: BALANCE, SPENT, REMAINING, DAILY AVG, PROJECTED
- Fixed expenses (`fixed='1'`) are excluded from daily avg and the Days bar chart
- Sheet tabs (Transactions / Categories / Days) use `QStackedWidget`
- `_VertBarChart` draws vertical bars with `QPainter`; days chart fills day 1 → most recent entry, zero-spend days show label only with no bar

## Password Manager

- Passwords stored as GFM table rows in the note body
- Import CSV via the attach button (repurposed when a password note is active)
- Auto-locks when clicking away to another note
- Website/username fields use `setCursorPosition(0)` on focus-out so overflow starts from the left

## Pinned Notes

- Stored in `config.json["pinned"]` as a list of file paths
- `self._pinned = set(_CFG.get("pinned", []))`
- Shown above date-bucket sections with a "Pinned" header and a blue dot indicator
- Pin/unpin via right-click context menu on any note

## Autocorrect

Uses `autocorrect.Speller` — conservative, leaves "app", "isn", "dont" alone:
```python
from autocorrect import Speller as _Speller
_autocorrect = _Speller()
```
Fires on space, enter, `.`, `,`, `!`, `?`. Preserves original capitalisation.

## Context Menu

`_on_editor_context_menu` provides a styled Cut / Copy / Paste / Select All menu. Uses `_make_styled_menu(parent)` / `_MENU_SS` for consistent dark styling — apply to all `QMenu` instances throughout the app.

## Critical Known Issues & Fixes

### Editor font must be "Helvetica Neue"
`QFont("", 14)` resolves to SF Pro on macOS, which has OpenType `calt` that renders digits differently in mixed alphanumeric strings. **Always use `QFont("Helvetica Neue", 14)` for the editor.**

### Use `setPlainText` / `toPlainText`, never `setMarkdown` / `toMarkdown`
Qt's markdown parser corrupts passwords and plain text containing `-5H`, `^s`, `*word*` etc. The `_body(raw)` helper strips the `# Title` header before display; `_flush_save()` reconstructs `# {title}\n\n{body}` on save.

### Text alignment must be set explicitly
Qt on macOS defaults to full justification. Always call:
```python
document().setDefaultTextOption(QTextOption(Qt.AlignmentFlag.AlignLeft))
```

### Table column widths are per-column, not uniform
Never use `col_x[1] - col_x[0]` as a uniform column width. Derive per-column pixel widths from the stored percentage constraints:
```python
pcts = [c.rawValue() for c in table.format().columnWidthConstraints()]
total_px = (col_x[1] - col_x[0]) / max(pcts[0], 0.1) * 100.0
col_widths = [total_px * p / 100.0 for p in pcts]
```

### Sidebar selection after notebook creation
Always call `_switch_notebook(nb)` **before** `_load_sidebar()` when creating a new notebook, or `_sync_sidebar_selection` will run against the old `current_nb`.

### Budget note save timer after deletion
When deleting a budget note, stop `_budget_view._save_timer` and set `_budget_view._path = None` before hiding the view, otherwise the debounced save will recreate the file.

### Performance
Avoid connecting expensive operations directly to `textChanged` — it fires on every keystroke. Spell-checking via `QSyntaxHighlighter` and per-keystroke `toPlainText()` calls both caused significant lag and were removed. If re-adding either, debounce with `QTimer`.
