import sys, json, os, shutil, re, platform, uuid, secrets, string, csv

_FONT_BODY = "Helvetica Neue" if platform.system() == "Darwin" else "Liberation Sans"
_FONT_MONO = "Menlo"          if platform.system() == "Darwin" else "DejaVu Sans Mono"
from datetime import datetime, timedelta, date as _date
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QTextEdit, QLineEdit, QPushButton,
    QLabel, QMessageBox, QSplitter, QFrame, QStyle, QStyledItemDelegate,
    QMenu, QDialog, QFileDialog, QScrollArea, QStackedWidget,
)

import base64 as _base64, hashlib as _hashlib
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QRectF, QPoint, QPointF, QEvent, QPropertyAnimation, QEasingCurve, QUrl, QThread, Signal
from PySide6.QtGui import QFont, QPalette, QColor, QBrush, QFontMetrics, QPainter, QTextOption, QTextCursor, QTextCharFormat, QTextBlockFormat, QTextTableFormat, QTextTableCellFormat, QTextLength, QTextFrameFormat, QTextFormat, QShortcut, QKeySequence, QPen, QPainterPath, QSyntaxHighlighter, QPixmap, QTextImageFormat, QTextDocument, QDesktopServices

# ── Config ────────────────────────────────────────────────────────────────────
_HERE        = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__)
CONFIG_FILE  = os.path.join(_HERE, "config.json")

def _find_or_create_notes_folder():
    default = os.path.expanduser("~/notes")
    os.makedirs(default, exist_ok=True)
    return default

def _save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def _load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    root = _find_or_create_notes_folder()
    cfg = {"root": root}
    _save_config(cfg)
    return cfg

_CFG  = _load_config()
ROOT  = _CFG["root"]
os.makedirs(ROOT, exist_ok=True)

# ── AI config (zero-cost when disabled) ───────────────────────────────────────
_AI_CFG      = _CFG.get("ai", {})
_AI_ENABLED  = _AI_CFG.get("enabled", False)
_AI_ENDPOINT = _AI_CFG.get("endpoint", "http://localhost:11434")
_AI_MODEL    = _AI_CFG.get("model", "llama3.2")
_AI_EMBED    = _AI_CFG.get("embed_model", "nomic-embed-text")

if _AI_ENABLED:
    try:
        import chromadb as _chromadb; _CHROMA_OK = True
    except ImportError:
        _CHROMA_OK = False
    try:
        import requests as _requests; _REQUESTS_OK = True
    except ImportError:
        _REQUESTS_OK = False
else:
    _CHROMA_OK = False
    _REQUESTS_OK = False

# ── Filesystem storage ────────────────────────────────────────────────────────
_SKIP_DIRS  = {"Attachments", "attachments", "images", ".git"}
_SKIP_FILES = {".DS_Store"}

_IMAGE_EXTS   = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp', '.heic', '.heif'}
_ATT_PATH_PROP = int(QTextFormat.Property.UserProperty) + 100
_ATT_NAME_PROP = int(QTextFormat.Property.UserProperty) + 101
_LINK_HREF_PROP = int(QTextFormat.Property.UserProperty) + 102
ALL_NB       = "__all__"
ALL_NB_LABEL = "All Notes"
TRASH_NB     = "Recently Deleted"
PASSWORDS_NB = "Passwords"
BUDGET_NB    = "Budget"
_SPECIAL_NBS = {PASSWORDS_NB, BUDGET_NB}


def nb_path(notebook):
    return os.path.join(ROOT, notebook)

def note_path(notebook, title):
    return os.path.join(ROOT, notebook, title + ".md")

def list_notebooks():
    """Return notebook names (subdirs), TRASH always last."""
    if not os.path.isdir(ROOT):
        return []
    names = []
    for d in sorted(os.listdir(ROOT)):
        full = os.path.join(ROOT, d)
        if os.path.isdir(full) and not d.startswith(".") and d not in _SKIP_DIRS:
            if d != TRASH_NB:
                names.append(d)
    if os.path.isdir(os.path.join(ROOT, TRASH_NB)):
        names.append(TRASH_NB)
    return names

def load_notebook(notebook):
    """Return {title: {path, modified, preview, body}} for one notebook."""
    folder = nb_path(notebook)
    result = {}
    if not os.path.isdir(folder):
        return result
    for fname in os.listdir(folder):
        if fname in _SKIP_FILES or not fname.endswith(".md"):
            continue
        fp      = os.path.join(folder, fname)
        if not os.path.isfile(fp):
            continue
        title   = fname[:-3]
        mtime   = datetime.fromtimestamp(os.stat(fp).st_mtime).isoformat(timespec="seconds")
        try:
            raw = open(fp, encoding="utf-8", errors="ignore").read()
        except OSError:
            raw = ""
        preview = next((l.strip() for l in raw.split("\n") if l.strip() and not l.startswith("#")), "")[:140]
        result[title] = {"path": fp, "modified": mtime, "preview": preview, "body": raw.lower()}
    return result

def load_all():
    """Return {path: {title, notebook, modified, preview}}."""
    result = {}
    for nb in list_notebooks():
        for title, info in load_notebook(nb).items():
            result[info["path"]] = {**info, "title": title, "notebook": nb}
    return result


def read_note(path):
    with open(path, encoding="utf-8") as f:
        return f.read()

def _body(raw):
    """Strip the leading '# Title' line so the editor shows only the body."""
    lines = raw.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)

# ── Attachment helpers ────────────────────────────────────────────────────────
def _att_dir(note_path):
    return os.path.join(os.path.dirname(note_path), 'Attachments')

def _copy_att(note_path, src):
    ext     = os.path.splitext(src)[1]
    display = os.path.splitext(os.path.basename(src))[0]
    d       = _att_dir(note_path)
    os.makedirs(d, exist_ok=True)
    name = str(uuid.uuid4()).upper() + ext
    shutil.copy2(src, os.path.join(d, name))
    return f'Attachments/{name}', display

def _render_attachment(cur, note_dir, rel_path, display_name):
    ext      = os.path.splitext(rel_path)[1].lower()
    abs_path = os.path.join(note_dir, rel_path)
    doc      = cur.document()
    if ext in _IMAGE_EXTS and os.path.exists(abs_path):
        pix = QPixmap(abs_path)
        if not pix.isNull():
            doc_w = int(doc.textWidth())
            max_w = (doc_w - 40) if doc_w > 40 else 600
            if pix.width() > max_w:
                pix = pix.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
            url = QUrl(f'att:{rel_path}')
            doc.addResource(QTextDocument.ResourceType.ImageResource, url, pix)
            fmt = QTextImageFormat()
            fmt.setName(url.toString())
            fmt.setWidth(pix.width()); fmt.setHeight(pix.height())
            fmt.setProperty(_ATT_PATH_PROP, rel_path)
            fmt.setProperty(_ATT_NAME_PROP, display_name)
            cur.insertImage(fmt)
            return
    ext_label = ext.upper().lstrip('.') or 'FILE'
    char_fmt  = QTextCharFormat()
    char_fmt.setFontFamilies([_FONT_EDITOR]); char_fmt.setFontPointSize(13.0)
    char_fmt.setForeground(QColor(T2)); char_fmt.setBackground(QColor(BG2))
    char_fmt.setProperty(_ATT_PATH_PROP, rel_path)
    char_fmt.setProperty(_ATT_NAME_PROP, display_name)
    cur.insertText(f'📎  {display_name}  ·  {ext_label}', char_fmt)

# ── Markdown ↔ rich-text helpers ─────────────────────────────────────────────
_URL_RE = re.compile(r'https?://[^\s<>"\'\[\]()]+')

def _insert_with_urls(cur, text, base_fmt):
    last = 0
    for m in _URL_RE.finditer(text):
        if m.start() > last:
            cur.insertText(text[last:m.start()], base_fmt)
        url_fmt = QTextCharFormat(base_fmt)
        url_fmt.setAnchor(True); url_fmt.setAnchorHref(m.group())
        url_fmt.setForeground(QColor(ACC)); url_fmt.setFontUnderline(True)
        cur.insertText(m.group(), url_fmt)
        last = m.end()
    if last < len(text):
        cur.insertText(text[last:], base_fmt)

_INLINE_RE = re.compile(
    r'\[(?P<lnkt>[^\]]+)\]\((?P<lnku>[^)]+)\)'  # [text](url)
    r'|\*\*\*(?P<bi2>.+?)\*\*\*'
    r'|\*\*(?P<b>.+?)\*\*'
    r'|\*(?P<it>.+?)\*'
    r'|__(?P<u>.+?)__'
    r'|~~(?P<s>.+?)~~'
    r'|`(?P<mo>[^`]+)`'
    r'|(?P<plain>[^*_~`\[\n]+)'
    r'|(?P<ch>.)'
)

def _apply_table_data(cur, rows):
    """Insert a QTextTable at cur's position, fill it with rows, advance cur past the table."""
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    nrows = len(rows)
    fmt = QTextTableFormat()
    fmt.setCellPadding(6)
    fmt.setCellSpacing(0)
    fmt.setBorder(0)
    fmt.setColumnWidthConstraints([
        QTextLength(QTextLength.Type.PercentageLength, 100.0 / ncols)
        for _ in range(ncols)
    ])
    table = cur.insertTable(nrows, ncols, fmt)
    char_fmt = QTextCharFormat()
    char_fmt.setFontFamilies([_FONT_EDITOR]); char_fmt.setFontPointSize(14.0)
    char_fmt.setForeground(QColor(T1))
    for r, row_data in enumerate(rows):
        cf = _table_cell_fmt(r == 0)
        for c in range(ncols):
            cell = table.cellAt(r, c)
            cell.setFormat(cf)
            ccur = cell.firstCursorPosition()
            text = row_data[c] if c < len(row_data) else ''
            if text:
                ccur.insertText(text, char_fmt)
    end = table.lastCursorPosition()
    end.movePosition(QTextCursor.MoveOperation.NextBlock)
    cur.setPosition(end.position())

def _apply_markdown(editor, text, note_dir=None):
    editor.blockSignals(True)
    doc = editor.document()
    doc.clear()
    opt = QTextOption(Qt.AlignmentFlag.AlignLeft)
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    doc.setDefaultTextOption(opt)
    cur = QTextCursor(doc)
    lines = text.split('\n')
    i = 0
    in_fresh = True  # document starts with one empty block
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Attachment line: ![name](Attachments/...)
        if note_dir and stripped.startswith('![') and 'Attachments/' in stripped:
            m = re.match(r'!\[([^\]]*)\]\((Attachments/[^)]+)\)', stripped)
            if m:
                if not in_fresh:
                    cur.insertBlock(); cur.setBlockFormat(QTextBlockFormat())
                _render_attachment(cur, note_dir, m.group(2), m.group(1))
                in_fresh = False; i += 1; continue
        # GFM table block
        if stripped.startswith('|') and stripped.count('|') >= 2:
            tbl_lines = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                tbl_lines.append(lines[i]); i += 1
            rows = []
            for tl in tbl_lines:
                cells = [c.strip() for c in tl.strip().strip('|').split('|')]
                if cells and any(c.strip() for c in cells) and all(re.match(r'^[-: ]+$', c) for c in cells if c.strip()):
                    continue
                rows.append(cells)
            if rows:
                if not in_fresh:
                    if cur.block().text():
                        cur.insertBlock()
                    cur.setBlockFormat(QTextBlockFormat())
                _apply_table_data(cur, rows)
                in_fresh = True
            continue
        # Regular line
        if not in_fresh:
            cur.insertBlock()
        cur.setBlockFormat(QTextBlockFormat())
        base_fmt = QTextCharFormat()
        if line.startswith('- [x] ') or line.startswith('- [ ] '):
            checked = line[3] == 'x'
            blk = QTextBlockFormat(); blk.setLeftMargin(_CL_LEFT_MARGIN); blk.setBottomMargin(4)
            cur.setBlockFormat(blk)
            cur.insertText('​', _make_circle_fmt(checked))
            base_fmt.setFontFamilies([_FONT_EDITOR]); base_fmt.setFontPointSize(14.0)
            base_fmt.setFontWeight(QFont.Weight.Normal)
            line = line[6:]
        elif line.startswith('### '):
            base_fmt.setFontFamilies([_FONT_EDITOR]); base_fmt.setFontPointSize(15.0)
            base_fmt.setFontWeight(QFont.Weight.DemiBold); line = line[4:]
        elif line.startswith('## '):
            base_fmt.setFontFamilies([_FONT_EDITOR]); base_fmt.setFontPointSize(17.0)
            base_fmt.setFontWeight(QFont.Weight.Bold); line = line[3:]
        elif line.startswith('# '):
            base_fmt.setFontFamilies([_FONT_EDITOR]); base_fmt.setFontPointSize(20.0)
            base_fmt.setFontWeight(QFont.Weight.Bold); line = line[2:]
        else:
            base_fmt.setFontFamilies([_FONT_EDITOR]); base_fmt.setFontPointSize(14.0)
            base_fmt.setFontWeight(QFont.Weight.Normal)
        for m in _INLINE_RE.finditer(line):
            lnkt  = m.group('lnkt')
            lnku  = m.group('lnku')
            bi2   = m.group('bi2')
            b     = m.group('b')
            it    = m.group('it')
            u     = m.group('u')
            s     = m.group('s')
            mo    = m.group('mo')
            plain = m.group('plain')
            ch    = m.group('ch')
            if lnkt is not None:
                fmt = QTextCharFormat(base_fmt)
                fmt.setAnchor(True); fmt.setAnchorHref(lnku)
                fmt.setForeground(QColor(ACC)); fmt.setFontUnderline(True)
                cur.insertText(lnkt, fmt)
            elif bi2 is not None:
                fmt = QTextCharFormat(base_fmt); fmt.setFontWeight(QFont.Weight.Bold); fmt.setFontItalic(True)
                cur.insertText(bi2, fmt)
            elif b is not None:
                fmt = QTextCharFormat(base_fmt); fmt.setFontWeight(QFont.Weight.Bold)
                cur.insertText(b, fmt)
            elif it is not None:
                fmt = QTextCharFormat(base_fmt); fmt.setFontItalic(True)
                cur.insertText(it, fmt)
            elif u is not None:
                fmt = QTextCharFormat(base_fmt); fmt.setFontUnderline(True)
                cur.insertText(u, fmt)
            elif s is not None:
                fmt = QTextCharFormat(base_fmt); fmt.setFontStrikeOut(True)
                cur.insertText(s, fmt)
            elif mo is not None:
                fmt = QTextCharFormat(base_fmt)
                fmt.setFontFamilies([_FONT_MONO]); fmt.setFontPointSize(13)
                cur.insertText(mo, fmt)
            elif plain:
                _insert_with_urls(cur, plain, base_fmt)
            elif ch:
                cur.insertText(ch, base_fmt)
        in_fresh = False
        i += 1
    editor.blockSignals(False)

def _to_markdown(editor):
    doc = editor.document()
    lines = []
    visited_tables = set()
    for bi in range(doc.blockCount()):
        block = doc.findBlockByNumber(bi)
        tmp = QTextCursor(block)
        table = tmp.currentTable()
        if table is not None:
            tid = table.firstPosition()
            if tid not in visited_tables:
                visited_tables.add(tid)
                nrows, ncols = table.rows(), table.columns()
                for r in range(nrows):
                    cells = []
                    for c in range(ncols):
                        cell = table.cellAt(r, c)
                        cells.append(cell.firstCursorPosition().block().text())
                    if r == 0:
                        lines.append('| ' + ' | '.join(cells) + ' |')
                        lines.append('| ' + ' | '.join(['---'] * ncols) + ' |')
                    else:
                        lines.append('| ' + ' | '.join(cells) + ' |')
            continue
        # Attachment block: first fragment carries _ATT_PATH_PROP (images and file chips)
        it0 = block.begin()
        if not it0.atEnd():
            cf0 = it0.fragment().charFormat()
            att_path = cf0.property(_ATT_PATH_PROP)
            if att_path:
                lines.append(f'![{cf0.property(_ATT_NAME_PROP) or ""}]({att_path})')
                continue

        prefix = ''; parts = []
        is_checklist = False
        it = block.begin(); first_frag = True
        while not it.atEnd():
            frag = it.fragment()
            text = frag.text()
            if not text: it += 1; continue
            if first_frag:
                first_frag = False
                cf = frag.charFormat()
                if _is_circle_fmt(cf):
                    prefix = '- [x] ' if cf.property(_CL_CHECKED_KEY) else '- [ ] '
                    is_checklist = True
                    it += 1; continue
            fmt    = frag.charFormat()
            sz     = fmt.fontPointSize()
            if sz <= 0:
                sz = fmt.font().pointSizeF()
            wt     = fmt.fontWeight()
            if not prefix and sz > 0:
                if   sz >= 19:    prefix = '# '
                elif sz >= 16:    prefix = '## '
                elif sz >= 14.5:  prefix = '### '
            is_hdg = bool(prefix) and not is_checklist
            href   = fmt.anchorHref()
            if href:
                parts.append(text if text == href else f'[{text}]({href})')
                it += 1; continue
            mono   = fmt.font().family() == _FONT_MONO
            bold   = (wt >= QFont.Weight.Bold) and not is_hdg
            ital   = fmt.fontItalic()
            und    = fmt.fontUnderline()
            stk    = fmt.fontStrikeOut() and not is_checklist
            chunk  = text
            if mono:
                chunk = f'`{chunk}`'
            else:
                if stk: chunk = f'~~{chunk}~~'
                if und:  chunk = f'__{chunk}__'
                if bold and ital: chunk = f'***{chunk}***'
                elif bold:        chunk = f'**{chunk}**'
                elif ital:        chunk = f'*{chunk}*'
            parts.append(chunk)
            it += 1
        lines.append(prefix + ''.join(parts))
    return '\n'.join(lines)


# ── Autocorrect ───────────────────────────────────────────────────────────────
_autocorrect = None

def _init_autocorrect():
    global _autocorrect
    try:
        from autocorrect import Speller as _Speller
        _autocorrect = _Speller()
    except Exception:
        pass

def _rows_to_gfm(rows):
    """Convert a list of row lists to a GFM table string."""
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    def fmt_row(r):
        cells = [r[i] if i < len(r) else '' for i in range(ncols)]
        return '| ' + ' | '.join(cells) + ' |'
    lines = [fmt_row(rows[0]), '| ' + ' | '.join(['---'] * ncols) + ' |']
    for r in rows[1:]:
        lines.append(fmt_row(r))
    return '\n'.join(lines) + '\n'

# ── Password note helpers ─────────────────────────────────────────────────────
_PW_MARKER       = '<!-- password-note -->'
_STRENGTH_COLORS = ['', '#ff453a', '#ff9f0a', '#30d158']

def _is_password_note(content: str) -> bool:
    for line in content.split('\n')[:6]:
        if line.strip() == _PW_MARKER:
            return True
    return False

def _pw_make_verifier(master_pw: str) -> str:
    salt = os.urandom(16)
    h    = _hashlib.pbkdf2_hmac('sha256', master_pw.encode(), salt, 260_000)
    return _base64.b64encode(salt + h).decode()

def _pw_verify(master_pw: str, verifier: str) -> bool:
    try:
        raw  = _base64.b64decode(verifier)
        salt, stored = raw[:16], raw[16:]
        h    = _hashlib.pbkdf2_hmac('sha256', master_pw.encode(), salt, 260_000)
        return h == stored
    except Exception:
        return False

def _pw_strength(pw: str) -> int:
    if not pw: return 0
    s = 0
    if len(pw) >= 8:  s += 1
    if len(pw) >= 14: s += 1
    if re.search(r'[A-Z]', pw) and re.search(r'[a-z]', pw): s += 1
    if re.search(r'[0-9]', pw):          s += 1
    if re.search(r'[^A-Za-z0-9]', pw):   s += 1
    return 1 if s <= 2 else (2 if s <= 4 else 3)

def _pw_generate() -> str:
    pool = string.ascii_letters + string.digits + '!@#$%^&*-_'
    pw   = [secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice('!@#$%^&*-_')]
    pw  += [secrets.choice(pool) for _ in range(12)]
    secrets.SystemRandom().shuffle(pw)
    return ''.join(pw)

def _parse_pw_rows(content: str) -> list:
    rows = []
    for line in content.split('\n'):
        if not line.startswith('|'): continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if all(re.match(r'^-+$', c) for c in cells if c): continue
        rows.append(cells)
    data = rows[1:] if len(rows) > 1 else []
    return [[r[i] if i < len(r) else '' for i in range(3)] for r in data] or [['', '', '']]

    return ''


# ── Generate-password popup ───────────────────────────────────────────────────
class _GenPopup(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(
            f"QFrame{{background:{BG2};border:1px solid {DIV};border-radius:8px;}}"
        )
        self._cb = None
        lay = QVBoxLayout(self); lay.setContentsMargins(4, 4, 4, 4)
        btn = QPushButton("⟳   Generate password")
        btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{T1};border:none;"
            f"border-radius:6px;font-size:13px;text-align:left;padding:6px 12px;}}"
            f"QPushButton:hover{{background:{SEL};}}"
        )
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._accept)
        lay.addWidget(btn)
        self.adjustSize(); self.hide()

    def show_below(self, widget, callback):
        self._cb = callback
        gpos = widget.mapToGlobal(QPoint(0, widget.height() + 4))
        self.move(gpos); self.adjustSize(); self.show(); self.raise_()

    def _accept(self):
        if self._cb: self._cb(_pw_generate())
        self.hide()


# ── Password row widget ───────────────────────────────────────────────────────
class _PwRow(QWidget):
    def __init__(self, parent, website='', username='', password='', on_changed=None, gen_popup=None):
        super().__init__(parent)
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        BG2, DIV, T1, ACC = _t["BG2"], _t["DIV"], _t["T1"], _t["ACC"]
        self._on_changed  = on_changed
        self._gen_popup   = gen_popup
        self._hovering    = False
        self._del_cb      = None
        self._on_ping     = None  # set by owner; called on any interaction

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        field_ss = (
            f"QLineEdit{{background:{BG2};border:1px solid {DIV};"
            f"border-radius:6px;color:{T1};font-size:13px;"
            f"font-family:'{_FONT_BODY}';padding:5px 8px;}}"
            f"QLineEdit:focus{{border-color:{ACC};}}"
        )
        def _field(ph, val=''):
            f = QLineEdit(val); f.setPlaceholderText(ph)
            f.setStyleSheet(field_ss); f.setMinimumHeight(30)
            return f

        def _left_field(ph, val=''):
            f = _field(ph, val)
            _orig_focus_out = f.focusOutEvent
            def _focus_out(ev):
                _orig_focus_out(ev)
                f.setCursorPosition(0)
            f.focusOutEvent = _focus_out
            f.setCursorPosition(0)
            return f

        self._site = _left_field("Website",  website)
        self._user = _left_field("Username", username)
        self._pw   = _left_field("Password", password)
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)

        self._dot = QLabel('●')
        self._dot.setFixedWidth(12)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dot.setStyleSheet("color:transparent;background:transparent;font-size:10px;")

        self._del_btn = QPushButton('×'); self._del_btn.setFixedSize(24, 28)
        self._del_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{T2};border:none;"
            f"border-radius:6px;font-size:16px;padding:0;}}"
            f"QPushButton:hover{{color:#ff453a;background:{SEL};}}"
        )
        self._del_btn.clicked.connect(lambda: self._del_cb() if self._del_cb else None)

        pw_wrap = QWidget(); pw_wrap.setStyleSheet("background:transparent;")
        pwl = QHBoxLayout(pw_wrap); pwl.setContentsMargins(0,0,0,0); pwl.setSpacing(4)
        pwl.addWidget(self._pw); pwl.addWidget(self._dot)

        lay.addWidget(self._site, 3)
        lay.addWidget(self._user, 3)
        lay.addWidget(pw_wrap,    3)
        lay.addWidget(self._del_btn)

        for f in (self._site, self._user, self._pw):
            f.textChanged.connect(self._field_changed)
        self._update_dot()

        # hover = reveal
        orig_enter = self._pw.enterEvent
        orig_leave = self._pw.leaveEvent
        def _enter(ev):
            self._hovering = True
            self._pw.setEchoMode(QLineEdit.EchoMode.Normal)
            self._ping()
            orig_enter(ev)
        def _leave(ev):
            self._hovering = False
            self._pw.setEchoMode(QLineEdit.EchoMode.Password)
            orig_leave(ev)
        self._pw.enterEvent = _enter
        self._pw.leaveEvent = _leave

        # click when masked = copy; click when revealed = normal cursor
        orig_press = self._pw.mousePressEvent
        def _pw_click(ev, _orig=orig_press):
            self._ping()
            if self._pw.text():
                QApplication.clipboard().setText(self._pw.text())
                self._flash_copied()
            _orig(ev)
        self._pw.mousePressEvent = _pw_click

        # any field focus = ping timer
        for f in (self._site, self._user, self._pw):
            orig_foc = f.focusInEvent
            def _foc(ev, _orig=orig_foc):
                self._ping(); _orig(ev)
            f.focusInEvent = _foc

        # focus on empty field = offer generate
        orig_focus_in = self._pw.focusInEvent
        orig_focus_out = self._pw.focusOutEvent
        def _focus_in(ev):
            orig_focus_in(ev)
            if not self._pw.text() and self._gen_popup:
                self._gen_popup.show_below(self._pw, self._accept_generate)
        def _focus_out(ev):
            orig_focus_out(ev)
            if self._gen_popup: self._gen_popup.hide()
        self._pw.focusInEvent  = _focus_in
        self._pw.focusOutEvent = _focus_out

    def _accept_generate(self, pw):
        self._pw.setText(pw)

    def _flash_copied(self):
        win = self._pw.window()
        lbl = QLabel("Copied!", win)
        lbl.setStyleSheet(
            f"QLabel{{background:{ACC};color:white;border-radius:6px;"
            f"padding:4px 10px;font-size:12px;font-weight:600;}}"
        )
        lbl.adjustSize()
        gpos = self._pw.mapToGlobal(
            QPoint(self._pw.width() // 2 - lbl.width() // 2, -lbl.height() - 4))
        lbl.move(win.mapFromGlobal(gpos))
        lbl.show(); lbl.raise_()
        QTimer.singleShot(2000, lbl.deleteLater)

    def _ping(self):
        if self._on_ping: self._on_ping()

    def _field_changed(self):
        self._update_dot()
        if self._pw.text() and self._gen_popup:
            self._gen_popup.hide()
        if self._on_changed: self._on_changed()

    def _update_dot(self):
        s = _pw_strength(self._pw.text())
        col = _STRENGTH_COLORS[s] if s else 'transparent'
        self._dot.setStyleSheet(f"color:{col};background:transparent;font-size:10px;")

    def get_data(self):
        return [self._site.text(), self._user.text(), self._pw.text()]


# ── Password note view ────────────────────────────────────────────────────────
class PasswordNoteView(QWidget):
    _AUTO_LOCK_MS = 30_000

    def __init__(self, parent):
        super().__init__(parent)
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        self._path = None
        self._rows: list[_PwRow] = []
        self.setStyleSheet(f"background:{_t['BG0']};")

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self._stack     = QStackedWidget()
        self._gen_popup = _GenPopup(self)
        root.addWidget(self._stack)
        self._stack.addWidget(self._build_lock_page())
        self._stack.addWidget(self._build_table_page())

        self._lock_timer = QTimer(singleShot=True)
        self._lock_timer.timeout.connect(self.lock)

    def _build_lock_page(self):
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        BG0, BG2, DIV, T1, ACC = _t["BG0"], _t["BG2"], _t["DIV"], _t["T1"], _t["ACC"]
        w = QWidget(); w.setStyleSheet(f"background:{BG0};")
        v = QVBoxLayout(w); v.setAlignment(Qt.AlignmentFlag.AlignCenter); v.setSpacing(14)

        icon = QLabel("🔒"); icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size:52px;background:transparent;")
        lbl  = QLabel("Password Manager"); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color:{T1};font-size:18px;font-weight:600;background:transparent;")

        f_ss = (f"QLineEdit{{background:{BG2};border:1px solid {DIV};border-radius:8px;"
                f"color:{T1};font-size:14px;font-family:'{_FONT_BODY}';padding:0 12px;}}"
                f"QLineEdit:focus{{border-color:{ACC};}}")
        self._pw_input = QLineEdit(); self._pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_input.setPlaceholderText("Master password")
        self._pw_input.setFixedSize(280, 36); self._pw_input.setStyleSheet(f_ss)
        self._pw_input.returnPressed.connect(self._try_unlock)

        unlock_btn = QPushButton("Unlock"); unlock_btn.setFixedSize(280, 36)
        unlock_btn.setDefault(True)
        unlock_btn.setStyleSheet(
            f"QPushButton{{background:{ACC};color:white;border:none;"
            f"border-radius:8px;font-size:14px;font-weight:600;}}"
            f"QPushButton:hover{{background:#2a94ff;}}")
        unlock_btn.clicked.connect(self._try_unlock)

        self._lock_err = QLabel(""); self._lock_err.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lock_err.setStyleSheet("color:#ff453a;font-size:12px;background:transparent;")

        v.addStretch()
        for ww in (icon, lbl): v.addWidget(ww)
        v.addSpacing(6)
        for ww in (self._pw_input, unlock_btn, self._lock_err):
            v.addWidget(ww, alignment=Qt.AlignmentFlag.AlignHCenter)
        v.addStretch()
        return w

    # ── table page ────────────────────────────────────────────────────────────
    def _build_table_page(self):
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        BG0, BG2, DIV, T1, T2, ACC = _t["BG0"], _t["BG2"], _t["DIV"], _t["T1"], _t["T2"], _t["ACC"]
        w = QWidget(); w.setStyleSheet(f"background:{BG0};")
        v = QVBoxLayout(w); v.setContentsMargins(32, 16, 32, 16); v.setSpacing(8)

        # search bar + scope toggles
        search_row = QWidget(); search_row.setStyleSheet("background:transparent;")
        sl = QHBoxLayout(search_row); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(6)
        self._pw_search = QLineEdit()
        self._pw_search.setPlaceholderText("Search passwords…")
        self._pw_search.setFixedHeight(28)
        self._pw_search.setStyleSheet(
            f"QLineEdit{{background:{BG2};color:{T1};border:1px solid {DIV};"
            f"border-radius:6px;padding:0 8px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:{ACC};}}")
        self._pw_search.textChanged.connect(self._filter_rows)
        sl.addWidget(self._pw_search, 1)

        def _scope_btn(label, scope):
            b = QPushButton(label); b.setCheckable(True); b.setFixedHeight(28)
            b.setStyleSheet(
                f"QPushButton{{background:{BG2};color:{T1};border:1px solid {DIV};"
                f"border-radius:6px;font-size:11px;padding:0 10px;}}"
                f"QPushButton:checked{{background:{ACC};color:#fff;border-color:{ACC};}}"
                f"QPushButton:hover{{color:{T1};}}")
            b.setProperty("scope", scope)
            b.clicked.connect(lambda _checked, s=scope: self._set_search_scope(s))
            return b

        self._scope_all  = _scope_btn("All",      "all")
        self._scope_site = _scope_btn("Website",  "site")
        self._scope_user = _scope_btn("Username", "user")
        self._scope_all.setChecked(True)
        self._pw_search_scope = "all"
        sl.addWidget(self._scope_all)
        sl.addWidget(self._scope_site)
        sl.addWidget(self._scope_user)
        v.addWidget(search_row)

        # column labels
        col_hdr = QWidget(); col_hdr.setStyleSheet("background:transparent;")
        chl = QHBoxLayout(col_hdr); chl.setContentsMargins(0,0,0,4); chl.setSpacing(8)
        for name, stretch in [("Website", 3), ("Username", 3), ("Password  ●", 3)]:
            lbl = QLabel(name)
            lbl.setStyleSheet(f"color:{T2};font-size:11px;font-weight:600;background:transparent;")
            chl.addWidget(lbl, stretch)
        chl.addSpacing(72)  # gen + del + eye buttons
        v.addWidget(col_hdr)

        # scrollable row area
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        self._row_container = QWidget(); self._row_container.setStyleSheet("background:transparent;")
        self._row_layout    = QVBoxLayout(self._row_container)
        self._row_layout.setContentsMargins(0,0,0,0); self._row_layout.setSpacing(4)
        self._row_layout.addStretch()
        scroll.setWidget(self._row_container)
        v.addWidget(scroll, 1)

        # add entry button
        add_btn = QPushButton("+ Add Entry"); add_btn.setFixedHeight(32)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{ACC};"
            f"border:1px dashed {DIV};border-radius:8px;font-size:13px;}}"
            f"QPushButton:hover{{background:{BG2};}}")
        add_btn.clicked.connect(lambda: self._add_row())
        v.addWidget(add_btn)
        return w

    # ── public API ────────────────────────────────────────────────────────────
    def load(self, path, content):
        self._path = path
        self._pw_input.clear(); self._lock_err.clear()
        self._lock_timer.stop()
        verifier = _CFG.get('master_pw_verifier')
        if verifier is None:
            # no master password set yet — go straight to table
            self._populate(_parse_pw_rows(content))
        else:
            self._stack.setCurrentIndex(0)
            QTimer.singleShot(50, self._pw_input.setFocus)

    def lock(self, focus=True):
        self._save_now()
        self._lock_timer.stop()
        self._pw_input.clear(); self._lock_err.clear()
        self._stack.setCurrentIndex(0)
        if focus:
            QTimer.singleShot(50, self._pw_input.setFocus)

    def get_body(self):
        rows  = [rw.get_data() for rw in self._rows]
        table = _rows_to_gfm([['Website', 'Username', 'Password']] + rows)
        return f"{_PW_MARKER}\n{table}"

    # ── internals ─────────────────────────────────────────────────────────────
    def _populate(self, rows):
        for rw in self._rows: rw.deleteLater()
        self._rows = []
        self._pw_search.blockSignals(True)
        self._pw_search.clear()
        self._pw_search.blockSignals(False)
        for r in rows: self._add_row(*r)
        self._stack.setCurrentIndex(1)
        self._lock_timer.start(self._AUTO_LOCK_MS)

    def _try_unlock(self):
        pw = self._pw_input.text().strip()
        if not pw: return
        verifier = _CFG.get('master_pw_verifier')
        if verifier is None:
            _CFG['master_pw_verifier'] = _pw_make_verifier(pw)
            _save_config(_CFG)
        elif not _pw_verify(pw, verifier):
            self._lock_err.setText("Incorrect password")
            self._pw_input.clear(); self._pw_input.setFocus(); return
        raw = ""
        try:    raw = read_note(self._path)
        except Exception: pass
        self._populate(_parse_pw_rows(_body(raw)))

    def _add_row(self, website='', username='', password=''):
        rw = _PwRow(self._row_container, website, username, password,
                    on_changed=self._on_changed, gen_popup=self._gen_popup)
        rw._del_cb  = lambda: self._del_row(rw)
        rw._on_ping = self._reset_timer
        self._row_layout.insertWidget(self._row_layout.count() - 1, rw)
        self._rows.append(rw)
        self._reset_timer()

    def _del_row(self, rw):
        if len(self._rows) <= 1:
            rw._site.clear(); rw._user.clear(); rw._pw.clear()
            self._on_changed(); return
        self._rows.remove(rw); rw.deleteLater()
        self._on_changed()

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import CSV", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        imported = 0
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url  = (row.get('url') or row.get('URL') or '').strip()
                    user = (row.get('username') or row.get('Username') or row.get('USERNAME') or '').strip()
                    pw   = (row.get('password') or row.get('Password') or row.get('PASSWORD') or '').strip()
                    if url or user or pw:
                        self._add_row(url, user, pw)
                        imported += 1
        except Exception as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        # remove the initial blank row if it's the only empty one and we imported something
        if imported and len(self._rows) > 1:
            first = self._rows[0]
            if not first._site.text() and not first._user.text() and not first._pw.text():
                self._rows.remove(first); first.deleteLater()
        if imported:
            self._on_changed()

    def _set_search_scope(self, scope):
        self._pw_search_scope = scope
        for btn, s in [(self._scope_all, "all"), (self._scope_site, "site"), (self._scope_user, "user")]:
            btn.setChecked(s == scope)
        self._filter_rows(self._pw_search.text())

    def _filter_rows(self, query):
        q = query.lower().strip()
        for rw in self._rows:
            if not q:
                rw.setVisible(True)
                continue
            site  = rw._site.text().lower()
            user  = rw._user.text().lower()
            if self._pw_search_scope == "site":
                rw.setVisible(q in site)
            elif self._pw_search_scope == "user":
                rw.setVisible(q in user)
            else:
                rw.setVisible(q in site or q in user)

    def _on_changed(self):
        self._reset_timer(); self._save_now()

    def _reset_timer(self):
        self._lock_timer.start(self._AUTO_LOCK_MS)

    def _save_now(self):
        if not self._path or self._stack.currentIndex() == 0: return
        title = os.path.basename(self._path)[:-3]
        write_note(self._path, f"# {title}\n\n{self.get_body()}")


# ── Budget note helpers ───────────────────────────────────────────────────────
_BDG_MARKER = '<!-- budget-note -->'

def _is_budget_note(content: str) -> bool:
    for line in content.split('\n')[:6]:
        if line.strip() == _BDG_MARKER:
            return True
    return False

def _parse_budget_data(content: str):
    """Return (initial: float, target: float, rows: list of [desc, amount_str])."""
    initial = 0.0; target = 0.0
    rows = []
    in_table = False
    header_done = False
    for line in content.split('\n'):
        s = line.strip()
        if s.startswith('INITIAL:'):
            try: initial = float(s[8:])
            except ValueError: pass
            continue
        if s.startswith('TARGET:'):
            try: target = float(s[7:])
            except ValueError: pass
            continue
        if s.startswith('|') and '---' in s:
            in_table = True; header_done = True; continue
        if s.startswith('|') and not header_done:
            in_table = True; continue
        if in_table and s.startswith('|'):
            parts = [c.strip() for c in s.strip('|').split('|')]
            if len(parts) >= 2:
                rows.append([
                    parts[0],                              # desc
                    parts[1],                              # amount
                    parts[2] if len(parts) > 2 else '',   # date
                    parts[3] if len(parts) > 3 else '',   # category
                    parts[4] if len(parts) > 4 else '',   # fixed
                ])
    return initial, target, rows

def _budget_to_str(initial: float, target: float, rows: list) -> str:
    lines = [_BDG_MARKER, f'INITIAL:{initial}', f'TARGET:{target}',
             '| Description | Amount | Date | Category | Fixed |', '|---|---|---|---|---|']
    for r in rows:
        def _c(i): return (r[i] if len(r) > i else '').replace('|', '-')
        lines.append(f'| {_c(0)} | {_c(1)} | {_c(2)} | {_c(3)} | {_c(4)} |')
    return '\n'.join(lines) + '\n'

def _bdg_parse_amount(s: str):
    try: return float(s.replace(',', '').replace('$', '').strip())
    except ValueError: return None


class _VertBarChart(QWidget):
    """Vertical bar chart drawn with QPainter. Call update_data([(label, value), ...])."""
    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = color
        self._items = []
        self.setStyleSheet("background:transparent;")
        self.setMinimumHeight(180)

    def update_data(self, items):
        self._items = list(items)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        if not self._items:
            p.setPen(QColor(T3))
            p.setFont(QFont(_FONT_BODY, 12))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No transactions")
            p.end()
            return

        pad_t  = 28    # room above bars for amount labels
        pad_b  = 52    # room below bars for rotated category labels
        pad_lr = 16

        n       = len(self._items)
        avail_w = w - 2 * pad_lr
        slot_w  = avail_w / n
        bar_w   = max(10.0, min(48.0, slot_w * 0.62))
        chart_h = h - pad_t - pad_b
        if chart_h <= 0:
            p.end(); return

        max_val   = max(v for _, v in self._items) or 1.0
        bar_color = QColor(self._color)
        amt_font  = QFont(_FONT_BODY, 8)
        lbl_font  = QFont(_FONT_BODY, 9)
        fm_lbl    = QFontMetrics(lbl_font)

        for i, (label, val) in enumerate(self._items):
            cx = pad_lr + (i + 0.5) * slot_w
            bx = cx - bar_w / 2

            if val > 0:
                bh = max(2.0, chart_h * val / max_val)
                by = pad_t + chart_h - bh

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bar_color)
                r = min(3.0, bar_w / 4)
                p.drawRoundedRect(QRectF(bx, by, bar_w, bh), r, r)

                p.setPen(QColor(T2))
                p.setFont(amt_font)
                p.drawText(QRectF(cx - slot_w / 2, by - 22, slot_w, 18),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                           f"${val:,.0f}")

            # label below — rotated 35° (drawn for every day, including zeros)
            p.save()
            p.translate(cx, pad_t + chart_h + 6)
            p.rotate(35)
            p.setPen(QColor(T1))
            p.setFont(lbl_font)
            p.drawText(0, 0, fm_lbl.elidedText(label, Qt.TextElideMode.ElideRight, 80))
            p.restore()

        p.end()


class _FixToggle(QPushButton):
    """Small painted lock button — active = fixed expense, excluded from daily avg."""
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setChecked(checked)
        self.setFixedSize(26, 26)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Fixed expense — excluded from daily average")
        self.setStyleSheet("QPushButton{background:transparent;border:none;}")

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.isChecked()
        color  = QColor('#ff9f0a') if active else QColor(T3)
        cx, cy = self.width() / 2, self.height() / 2
        iw, ih = 10.0, 11.0
        x, y   = cx - iw / 2, cy - ih / 2
        # shackle arc
        pen = QPen(color); pen.setWidthF(1.8 if active else 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(x + iw*0.15, y, iw*0.70, ih*0.55), 0, 180*16)
        # body
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color if active else Qt.BrushStyle.NoBrush)
        if not active:
            pen2 = QPen(color); pen2.setWidthF(1.5); p.setPen(pen2)
        p.drawRoundedRect(QRectF(x, y + ih*0.42, iw, ih*0.58), 2.0, 2.0)
        p.end()


class _BdgRow(QWidget):
    def __init__(self, parent, desc='', amount='', date='', category='', fixed=False, on_changed=None):
        super().__init__(parent)
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        BG2, DIV, T1, T3, ACC = _t["BG2"], _t["DIV"], _t["T1"], _t["T3"], _t["ACC"]
        self._on_changed = on_changed
        self._del_cb     = None
        self._fixed      = fixed

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2); lay.setSpacing(6)

        field_ss = (
            f"QLineEdit{{background:{BG2};border:1px solid {DIV};"
            f"border-radius:6px;color:{T1};font-size:13px;"
            f"font-family:'{_FONT_BODY}';padding:5px 8px;}}"
            f"QLineEdit:focus{{border-color:{ACC};}}"
        )

        self._amt = QLineEdit(amount)
        def _left_field(widget):
            orig = widget.focusOutEvent
            def _fo(ev):
                orig(ev); widget.setCursorPosition(0)
            widget.focusOutEvent = _fo
            widget.setCursorPosition(0)
            return widget

        self._amt = _left_field(QLineEdit(amount))
        self._amt.setPlaceholderText("0.00")
        self._amt.setFixedWidth(95); self._amt.setMinimumHeight(30)
        self._amt.setStyleSheet(field_ss)

        self._desc = _left_field(QLineEdit(desc))
        self._desc.setPlaceholderText("Description")
        self._desc.setStyleSheet(field_ss); self._desc.setMinimumHeight(30)

        self._cat = _left_field(QLineEdit(category))
        self._cat.setPlaceholderText("Category")
        self._cat.setFixedWidth(90); self._cat.setMinimumHeight(30)
        self._cat.setStyleSheet(field_ss)

        self._date = _left_field(QLineEdit(date))
        self._date.setPlaceholderText("MM/DD")
        self._date.setFixedWidth(65); self._date.setMinimumHeight(30)
        self._date.setStyleSheet(field_ss)

        self._fix_btn = _FixToggle(fixed)
        self._fix_btn.clicked.connect(self._on_fix_toggled)

        del_btn = QPushButton("✕")
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        del_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{T3};border:none;font-size:12px;}}"
            f"QPushButton:hover{{color:#ff453a;}}")
        del_btn.clicked.connect(lambda: self._del_cb and self._del_cb())

        lay.addWidget(self._fix_btn)
        lay.addWidget(self._amt)
        lay.addWidget(self._desc, 1)
        lay.addWidget(self._cat)
        lay.addWidget(self._date)
        lay.addWidget(del_btn)

        self._amt.textChanged.connect(self._on_amt_changed)
        self._desc.textChanged.connect(self._emit)
        self._cat.textChanged.connect(self._emit)
        self._date.textChanged.connect(self._emit)

        for f in (self._amt, self._desc, self._cat, self._date):
            f.returnPressed.connect(lambda: self._new_row_cb and self._new_row_cb())

        self._new_row_cb = None
        self._update_amt_color()

    def _emit(self):
        if self._on_changed: self._on_changed()

    def _on_fix_toggled(self):
        self._fixed = self._fix_btn.isChecked()
        self._fix_btn.update()
        self._emit()

    def _on_amt_changed(self):
        self._update_amt_color()
        self._emit()

    def _update_amt_color(self):
        v = _bdg_parse_amount(self._amt.text())
        if v is None or v == 0:
            color = T1
        elif v < 0:
            color = '#ff453a'
        else:
            color = '#30d158'
        self._amt.setStyleSheet(
            f"QLineEdit{{background:{BG2};border:1px solid {DIV};"
            f"border-radius:6px;color:{color};font-size:13px;"
            f"font-family:'{_FONT_BODY}';padding:5px 8px;}}"
            f"QLineEdit:focus{{border-color:{ACC};}}")

    def get_data(self):
        return [self._desc.text(), self._amt.text(), self._date.text(),
                self._cat.text(), '1' if self._fixed else '']


class BudgetNoteView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._path    = None
        self._rows    = []
        self._initial = 0.0
        self._target  = 0.0
        self._save_timer = QTimer(self); self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)
        self._build_ui()

    def _build_ui(self):
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        BG0, BG2, DIV, T1, T2, ACC = _t["BG0"], _t["BG2"], _t["DIV"], _t["T1"], _t["T2"], _t["ACC"]
        v = QVBoxLayout(self); v.setContentsMargins(32, 16, 32, 16); v.setSpacing(10)
        self.setStyleSheet(f"background:{BG0};")

        # ── stats bar ────────────────────────────────────────────────────────
        stats_row = QWidget(); stats_row.setStyleSheet("background:transparent;")
        sl = QHBoxLayout(stats_row); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(0)

        def _stat(label):
            w = QWidget(); w.setStyleSheet(
                f"background:{BG2};border-radius:8px;")
            wl = QVBoxLayout(w); wl.setContentsMargins(12, 8, 12, 8); wl.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{T2};font-size:10px;font-weight:600;background:transparent;")
            val = QLabel("—")
            val.setStyleSheet(f"color:{T1};font-size:15px;font-weight:600;background:transparent;")
            wl.addWidget(lbl); wl.addWidget(val)
            return w, val

        self._stat_balance_w,   self._stat_balance   = _stat("BALANCE")
        self._stat_spent_w,     self._stat_spent     = _stat("SPENT")
        self._stat_remaining_w, self._stat_remaining = _stat("REMAINING")
        self._stat_daily_w,     self._stat_daily     = _stat("DAILY AVG")
        self._stat_proj_w,      self._stat_proj      = _stat("PROJECTED")

        all_stat_widgets = [self._stat_balance_w, self._stat_spent_w,
                            self._stat_remaining_w, self._stat_daily_w,
                            self._stat_proj_w]
        for i, w in enumerate(all_stat_widgets):
            sl.addWidget(w, 1)
            if i < len(all_stat_widgets) - 1:
                sep = QWidget(); sep.setFixedWidth(8)
                sep.setStyleSheet("background:transparent;")
                sl.addWidget(sep)
        v.addWidget(stats_row)

        # ── initial balance + spending target row ─────────────────────────────
        init_row = QWidget(); init_row.setStyleSheet("background:transparent;")
        il = QHBoxLayout(init_row); il.setContentsMargins(0, 0, 0, 0); il.setSpacing(16)

        _num_ss = (f"QLineEdit{{background:{BG2};border:1px solid {DIV};"
                   f"border-radius:6px;color:{T1};font-size:13px;"
                   f"font-family:'{_FONT_BODY}';padding:5px 8px;}}"
                   f"QLineEdit:focus{{border-color:{ACC};}}")

        def _num_field(placeholder):
            f = QLineEdit(); f.setPlaceholderText(placeholder)
            f.setFixedWidth(120); f.setFixedHeight(30)
            f.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            f.setStyleSheet(_num_ss)
            return f

        init_lbl = QLabel("Initial Balance")
        init_lbl.setStyleSheet(f"color:{T2};font-size:12px;font-weight:600;background:transparent;")
        self._init_field = _num_field("0.00")
        self._init_field.textChanged.connect(self._on_changed)

        tgt_lbl = QLabel("Spending Target")
        tgt_lbl.setStyleSheet(f"color:{T2};font-size:12px;font-weight:600;background:transparent;")
        self._target_field = _num_field("0.00")
        self._target_field.textChanged.connect(self._on_changed)

        il.addWidget(init_lbl); il.addWidget(self._init_field)
        il.addStretch()
        il.addWidget(tgt_lbl); il.addWidget(self._target_field)
        v.addWidget(init_row)

        # ── sheet tabs ────────────────────────────────────────────────────────
        tabs_row = QWidget(); tabs_row.setStyleSheet(f"background:{BG0};")
        tl = QHBoxLayout(tabs_row); tl.setContentsMargins(0, 4, 0, 0); tl.setSpacing(0)
        self._tab_btns = []
        for _idx, _name in enumerate(("Transactions", "Categories", "Days")):
            _btn = QPushButton(_name)
            _btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            _btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _btn.setFixedHeight(28)
            _btn.clicked.connect(lambda _, i=_idx: self._switch_tab(i))
            self._tab_btns.append(_btn)
            tl.addWidget(_btn)
        tl.addStretch()
        v.addWidget(tabs_row)
        self._active_tab = 0
        self._style_tabs()

        # ── stacked pages ─────────────────────────────────────────────────────
        self._stack = QStackedWidget(); self._stack.setStyleSheet(f"background:{BG0};")

        # page 0 — transactions
        pg0 = QWidget(); pg0.setStyleSheet("background:transparent;")
        pg0_v = QVBoxLayout(pg0); pg0_v.setContentsMargins(0, 0, 0, 0); pg0_v.setSpacing(6)

        div = QFrame(); div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color:{DIV};background:{DIV};border:none;max-height:1px;")
        pg0_v.addWidget(div)

        col_hdr = QWidget(); col_hdr.setStyleSheet("background:transparent;")
        chl = QHBoxLayout(col_hdr); chl.setContentsMargins(0, 0, 0, 2); chl.setSpacing(8)
        def _col_hdr_lbl(text, width=None, align=Qt.AlignmentFlag.AlignLeft):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color:{T2};font-size:11px;font-weight:600;background:transparent;")
            if width: lbl.setFixedWidth(width)
            lbl.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            return lbl
        chl.addSpacing(32)
        chl.addWidget(_col_hdr_lbl("Amount", 95))
        chl.addWidget(_col_hdr_lbl("Description"), 1)
        chl.addWidget(_col_hdr_lbl("Category", 90))
        chl.addWidget(_col_hdr_lbl("Date", 65, Qt.AlignmentFlag.AlignCenter))
        chl.addSpacing(32)
        pg0_v.addWidget(col_hdr)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        scroll_content = QWidget(); scroll_content.setStyleSheet("background:transparent;")
        sv = QVBoxLayout(scroll_content); sv.setContentsMargins(0, 0, 0, 0); sv.setSpacing(8)

        self._row_container = QWidget(); self._row_container.setStyleSheet("background:transparent;")
        self._row_layout    = QVBoxLayout(self._row_container)
        self._row_layout.setContentsMargins(0, 0, 0, 0); self._row_layout.setSpacing(4)
        self._row_layout.addStretch()
        sv.addWidget(self._row_container, 1)

        add_btn = QPushButton("+ Add Transaction"); add_btn.setFixedHeight(32)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{ACC};"
            f"border:1px dashed {DIV};border-radius:8px;font-size:13px;}}"
            f"QPushButton:hover{{background:{BG2};}}")
        add_btn.clicked.connect(lambda: self._add_row())
        sv.addWidget(add_btn)
        sv.addStretch()

        self._scroll.setWidget(scroll_content)
        pg0_v.addWidget(self._scroll, 1)
        self._stack.addWidget(pg0)

        # page 1 — categories chart
        self._cat_chart = _VertBarChart(ACC)
        self._stack.addWidget(self._cat_chart)

        # page 2 — days chart
        self._day_chart = _VertBarChart('#ff9f0a')
        self._stack.addWidget(self._day_chart)

        v.addWidget(self._stack, 1)

    # ── data ──────────────────────────────────────────────────────────────────
    def load(self, path, content):
        self._path = path
        initial, target, rows = _parse_budget_data(content)

        def _fmt_num(v):
            return str(v) if v != int(v) else str(int(v))

        for field, val in [(self._init_field, initial), (self._target_field, target)]:
            field.blockSignals(True)
            field.setText(_fmt_num(val) if val else "")
            field.blockSignals(False)

        for rw in self._rows: rw.deleteLater()
        self._rows = []
        for r in rows:
            self._add_row(
                r[0] if len(r) > 0 else '',
                r[1] if len(r) > 1 else '',
                r[2] if len(r) > 2 else '',
                r[3] if len(r) > 3 else '',
                r[4] if len(r) > 4 else '',
            )
        if not rows: self._add_row()
        self._update_stats()

    def get_body(self):
        def _parse_field(f):
            try: return float(f.text().replace(',', '').replace('$', ''))
            except ValueError: return 0.0
        initial = _parse_field(self._init_field)
        target  = _parse_field(self._target_field)
        rows = [rw.get_data() for rw in self._rows]
        return _budget_to_str(initial, target, rows)

    def _add_row(self, desc='', amount='', date='', category='', fixed=''):
        if not date:
            from datetime import date as _d
            date = _d.today().strftime('%-m/%-d')
        rw = _BdgRow(self._row_container, desc, amount, date, category,
                     fixed == '1', on_changed=self._on_changed)
        rw._del_cb     = lambda: self._del_row(rw)
        rw._new_row_cb = self._add_row_and_focus
        self._row_layout.insertWidget(self._row_layout.count() - 1, rw)
        self._rows.append(rw)

    def _add_row_and_focus(self):
        self._add_row()
        self._rows[-1]._amt.setFocus()
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()))

    def _del_row(self, rw):
        if len(self._rows) <= 1:
            rw._desc.clear(); rw._amt.clear()
            self._on_changed(); return
        self._rows.remove(rw); rw.deleteLater()
        self._on_changed()

    def _on_changed(self):
        self._update_stats()
        self._save_timer.start(600)

    def _update_stats(self):
        _t = _THEMES["notes24"] if _ACTIVE_THEME == "notes07" else _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
        T1, T2 = _t["T1"], _t["T2"]
        def _parse(f):
            try: return float(f.text().replace(',', '').replace('$', ''))
            except ValueError: return 0.0

        initial = _parse(self._init_field)
        target  = _parse(self._target_field)

        total = 0.0
        gross_spent = 0.0; gross_fixed = 0.0
        returns     = 0.0; fixed_returns = 0.0
        for rw in self._rows:
            v = _bdg_parse_amount(rw._amt.text())
            if v is not None:
                total += v
                if v < 0:
                    gross_spent += abs(v)
                    if rw._fixed:
                        gross_fixed += abs(v)
                elif v > 0:
                    returns += v
                    if rw._fixed:
                        fixed_returns += v

        balance    = initial + total
        net_spent  = max(0.0, gross_spent - returns)
        net_fixed  = max(0.0, gross_fixed - fixed_returns)
        remaining  = target - net_spent

        net_variable = max(0.0, net_spent - net_fixed)
        from datetime import date as _date2
        today = _date2.today()
        days_elapsed  = today.day
        days_in_month = (_date2(today.year + today.month // 12,
                                today.month % 12 + 1, 1) - _date2(today.year, today.month, 1)).days
        daily_avg     = net_variable / days_elapsed if days_elapsed else 0
        projected     = daily_avg * days_in_month + net_fixed

        def _fmt(v):
            return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"

        bal_color = '#30d158' if balance >= 0 else '#ff453a'
        rem_color = ('#30d158' if remaining >= 0 else '#ff453a') if target else T2

        self._stat_balance.setText(_fmt(balance))
        self._stat_balance.setStyleSheet(
            f"color:{bal_color};font-size:15px;font-weight:600;background:transparent;")
        self._stat_spent.setText(f"-${net_spent:,.2f}")
        self._stat_spent.setStyleSheet(
            f"color:#ff453a;font-size:15px;font-weight:600;background:transparent;")
        self._stat_remaining.setText(_fmt(remaining) if target else "—")
        self._stat_remaining.setStyleSheet(
            f"color:{rem_color};font-size:15px;font-weight:600;background:transparent;")
        daily_lbl = f"${daily_avg:,.2f}/day"
        if net_fixed:
            daily_lbl += f"\nexcl. ${net_fixed:,.0f} fixed"
        self._stat_daily.setText(daily_lbl)
        self._stat_daily.setStyleSheet(
            f"color:{T1};font-size:13px;font-weight:600;background:transparent;")
        proj_color = ('#30d158' if projected <= target else '#ff453a') if target else T1
        self._stat_proj.setText(f"${projected:,.2f}")
        self._stat_proj.setStyleSheet(
            f"color:{proj_color};font-size:15px;font-weight:600;background:transparent;")
        self._update_breakdowns()

    def _update_breakdowns(self):
        from collections import defaultdict
        cat_totals  = defaultdict(float)
        day_totals  = defaultdict(float)  # (m, d) -> variable spend
        all_dates   = set()               # (m, d) for every row with a parseable date

        for rw in self._rows:
            v        = _bdg_parse_amount(rw._amt.text())
            date_str = rw._date.text().strip()
            md = None
            try:
                parts = date_str.split('/')
                md = (int(parts[0]), int(parts[1]))
                all_dates.add(md)
            except Exception:
                pass
            if v is not None and v != 0:
                cat = rw._cat.text().strip() or "Uncategorised"
                # positive amounts (refunds) reduce category and day totals
                cat_totals[cat] += -v if v > 0 else abs(v)
                if not rw._fixed and md is not None:
                    day_totals[md] += -v if v > 0 else abs(v)

        # clip negatives (refunds exceeding spending in a bucket show as 0)
        cat_items = sorted(
            ((k, max(0.0, v)) for k, v in cat_totals.items() if v > 0),
            key=lambda x: x[1], reverse=True)
        self._cat_chart.update_data(cat_items)

        if all_dates:
            month, max_day = max(all_dates)
            day_items = [(f"{month}/{d}", max(0.0, day_totals.get((month, d), 0.0)))
                         for d in range(1, max_day + 1)]
        else:
            day_items = []
        self._day_chart.update_data(day_items)

    def _style_tabs(self):
        for i, btn in enumerate(self._tab_btns):
            if i == self._active_tab:
                btn.setStyleSheet(
                    f"QPushButton{{background:{BG2};color:{T1};border:none;"
                    f"border-bottom:2px solid {ACC};font-size:12px;"
                    f"font-weight:600;padding:0 14px;}}")
            else:
                btn.setStyleSheet(
                    f"QPushButton{{background:transparent;color:{T2};border:none;"
                    f"border-bottom:1px solid {DIV};font-size:12px;padding:0 14px;}}"
                    f"QPushButton:hover{{color:{T1};background:{BG2};}}")

    def _switch_tab(self, idx):
        self._active_tab = idx
        self._style_tabs()
        self._stack.setCurrentIndex(idx)

    def _save_now(self):
        if not self._path: return
        title = os.path.basename(self._path)[:-3]
        write_note(self._path, f"# {title}\n\n{self.get_body()}")


def write_note(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def create_note(notebook, title):
    os.makedirs(nb_path(notebook), exist_ok=True)
    fp = note_path(notebook, title)
    if os.path.exists(fp):
        raise FileExistsError(f'"{title}" already exists in {notebook}.')
    write_note(fp, "")
    return fp

def rename_note(old_path, new_title):
    new_fp = os.path.join(os.path.dirname(old_path), new_title + ".md")
    if os.path.exists(new_fp):
        raise FileExistsError(f'"{new_title}" already exists.')
    os.rename(old_path, new_fp)
    return new_fp

def move_note(path, dest_notebook):
    """Move a note file to dest_notebook; return new path. Raises FileExistsError on collision."""
    os.makedirs(nb_path(dest_notebook), exist_ok=True)
    base    = os.path.basename(path)
    new_fp  = os.path.join(nb_path(dest_notebook), base)
    if os.path.exists(new_fp):
        raise FileExistsError(f'"{base[:-3]}" already exists in {dest_notebook}.')
    shutil.move(path, new_fp)
    # move Attachments subfolder if present
    att_src = os.path.join(os.path.dirname(path), "Attachments")
    if os.path.isdir(att_src):
        att_dst = os.path.join(nb_path(dest_notebook), "Attachments")
        os.makedirs(att_dst, exist_ok=True)
        for f in os.listdir(att_src):
            s = os.path.join(att_src, f)
            d = os.path.join(att_dst, f)
            if not os.path.exists(d):
                shutil.move(s, d)
    return new_fp

def trash_note(path):
    trash = nb_path(TRASH_NB)
    os.makedirs(trash, exist_ok=True)
    base = os.path.basename(path)
    dest = os.path.join(trash, base)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(trash, f"{stem}_{int(datetime.now().timestamp())}{ext}")
    shutil.move(path, dest)

def create_notebook(name):
    os.makedirs(nb_path(name), exist_ok=True)

def note_count(notebook):
    folder = nb_path(notebook)
    if not os.path.isdir(folder):
        return 0
    return sum(1 for f in os.listdir(folder) if f.endswith(".md") and f not in _SKIP_FILES)

def total_count():
    return sum(note_count(nb) for nb in list_notebooks())

# ── Themes ────────────────────────────────────────────────────────────────────
_THEMES = {
    "notes26": {
        "BG0": "#1c1c1e", "BG1": "#242426", "BG2": "#2c2c2e",
        "DIV": "#38383a", "SEL": "#3a3a3c",
        "T1":  "#f2f2f7", "T2":  "#8e8e93", "T3":  "#48484a",
        "ACC": "#0a84ff", "TBL": "#505052",
    },
    "notes24": {
        "BG0": "#1c1c1e", "BG1": "#242426", "BG2": "#2c2c2e",
        "DIV": "#38383a", "SEL": "#3a3a3c",
        "T1":  "#f2f2f7", "T2":  "#8e8e93", "T3":  "#48484a",
        "ACC": "#9F832A", "TBL": "#505052",
    },
    "notes07": {
        "BG0": "#fef9c0", "BG1": "#242426", "BG2": "#2c2c2e",
        "DIV": "#38383a", "SEL": "#3a3a3c",
        "T1":  "#f2f2f7", "T2":  "#8e8e93", "T3":  "#48484a",
        "ACC": "#9F832A", "TBL": "#505052",
    },
}
_ACTIVE_THEME = {"dark": "notes26", "classic": "notes24"}.get(
    _CFG.get("theme", "notes26"), _CFG.get("theme", "notes26"))
_FONT_EDITOR = "Noteworthy" if _ACTIVE_THEME == "notes07" else _FONT_BODY

# palette globals — populated by _load_theme_globals()
BG0 = BG1 = BG2 = DIV = SEL = T1 = T2 = T3 = ACC = _TBL_BORDER = ""
_MENU_SS = _POPUP_STYLE = ""

def _draw_nb_icon(painter, cx, cy, is_trash, color, is_lock=False, is_budget=False):
    iw, ih = 15.0, 12.0
    x, y = cx - iw / 2, cy - ih / 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    if is_lock:
        # shackle arc
        pen = QPen(QColor(color)); pen.setWidthF(1.8); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(x+iw*0.25, y, iw*0.50, ih*0.55), 0, 180*16)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(x+iw*0.08, y+ih*0.42, iw*0.84, ih*0.58), 2.0, 2.0)
    elif is_budget:
        # dollar-sign bar chart hint: three vertical bars
        bw = iw * 0.18
        for i, h_frac in enumerate([0.55, 1.0, 0.75]):
            bh = ih * h_frac * 0.85
            bx = x + iw * (0.08 + i * 0.34)
            painter.drawRoundedRect(QRectF(bx, y + ih - bh, bw, bh), 1.5, 1.5)
    elif is_trash:
        painter.drawRoundedRect(QRectF(x+iw*0.30, y,          iw*0.40, ih*0.14), 1.0, 1.0)
        painter.drawRoundedRect(QRectF(x-iw*0.05, y+ih*0.17,  iw*1.10, ih*0.12), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(x+iw*0.08, y+ih*0.32,  iw*0.84, ih*0.68), 2.0, 2.0)
    else:
        painter.drawRoundedRect(QRectF(x,          y,          iw*0.44, ih*0.38), 2.0, 2.0)
        painter.drawRoundedRect(QRectF(x,          y+ih*0.28,  iw,      ih*0.72), 2.5, 2.5)
    painter.restore()
def _load_theme_globals():
    global BG0, BG1, BG2, DIV, SEL, T1, T2, T3, ACC, _TBL_BORDER, _MENU_SS, _POPUP_STYLE
    t = _THEMES.get(_ACTIVE_THEME, _THEMES["notes26"])
    BG0 = t["BG0"]; BG1 = t["BG1"]; BG2 = t["BG2"]
    DIV = t["DIV"]; SEL = t["SEL"]
    T1  = t["T1"];  T2  = t["T2"];  T3  = t["T3"]
    ACC = t["ACC"]; _TBL_BORDER = t["TBL"]
    _MENU_SS = f"""
QMenu {{
    background: {BG1};
    color: {T1};
    border: 1px solid {DIV};
    border-radius: 10px;
    padding: 5px 0px;
    font-size: 13px;
    font-family: '{_FONT_BODY}';
}}
QMenu::item {{
    padding: 6px 28px 6px 14px;
    border-radius: 5px;
    margin: 1px 4px;
}}
QMenu::item:selected {{ background: {SEL}; }}
QMenu::item:disabled {{ color: {T3}; }}
QMenu::separator {{ height: 1px; background: {DIV}; margin: 4px 0px; }}
"""
    _POPUP_STYLE = f"""
    QFrame      {{ background:{BG2}; border:1px solid {DIV}; border-radius:10px; }}
    QPushButton {{ background:transparent; color:{T1}; border:none; border-radius:6px;
                  text-align:left; padding:8px 14px; font-size:13px; }}
    QPushButton:hover {{ background:{SEL}; }}
"""

_load_theme_globals()

def _fmt_btn_ss(active: bool) -> str:
    if active:
        return (f"QPushButton{{background:{SEL};border:none;border-radius:6px;"
                f"color:{T1};text-align:center;}}"
                f"QPushButton:hover{{background:{DIV};}}")
    return (f"QPushButton{{background:transparent;border:none;border-radius:6px;"
            f"color:{T1};text-align:center;}}"
            f"QPushButton:hover{{background:{SEL};}}")

def _make_styled_menu(parent):
    m = QMenu(parent)
    m.setStyleSheet(_MENU_SS)
    return m

def _table_cell_fmt(header: bool) -> QTextTableCellFormat:
    fmt = QTextTableCellFormat()
    fmt.setBackground(QColor(BG2 if header else BG1))
    brush = QBrush(QColor(_TBL_BORDER))
    style = QTextFrameFormat.BorderStyle.BorderStyle_Solid
    fmt.setTopBorder(1.0);    fmt.setTopBorderStyle(style);    fmt.setTopBorderBrush(brush)
    fmt.setBottomBorder(1.0); fmt.setBottomBorderStyle(style); fmt.setBottomBorderBrush(brush)
    fmt.setLeftBorder(1.0);   fmt.setLeftBorderStyle(style);   fmt.setLeftBorderBrush(brush)
    fmt.setRightBorder(1.0);  fmt.setRightBorderStyle(style);  fmt.setRightBorderBrush(brush)
    return fmt

# ── Checklist inline object ───────────────────────────────────────────────────
_CL_CHECKED_KEY  = int(QTextFormat.Property.UserProperty) + 1
_CL_LEFT_MARGIN  = 26  # px reserved for circle; text starts this far right

def _make_circle_fmt(checked: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setProperty(_CL_CHECKED_KEY, checked)
    fmt.setFontFamilies([_FONT_EDITOR])
    fmt.setFontPointSize(14.0)  # match body height; zero-width so no advance
    fmt.setForeground(QColor(BG0))
    return fmt

def _is_circle_fmt(fmt: QTextCharFormat) -> bool:
    return isinstance(fmt.property(_CL_CHECKED_KEY), bool)

def _make_vcursor(block) -> QTextCursor:
    c = QTextCursor(block)
    c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
    return c

def _paint_circles(editor):
    _R = 7.5
    painter = QPainter(editor.viewport())
    if not painter.isActive():
        return
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    doc = editor.document()
    vr  = editor.viewport().rect()
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        if not it.atEnd():
            fmt = it.fragment().charFormat()
            if _is_circle_fmt(fmt):
                checked = fmt.property(_CL_CHECKED_KEY)
                cr = editor.cursorRect(_make_vcursor(block))
                if vr.intersects(cr):
                    cx = cr.left() - _CL_LEFT_MARGIN // 2
                    cy = cr.top()  + cr.height() / 2
                    r  = _R
                    if checked:
                        painter.setBrush(QColor(ACC))
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.drawEllipse(QPointF(cx, cy), r, r)
                        pen = QPen(QColor("white"), 1.8, Qt.PenStyle.SolidLine,
                                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                        painter.setPen(pen)
                        path = QPainterPath()
                        path.moveTo(cx - r*0.42, cy + r*0.05)
                        path.lineTo(cx - r*0.05, cy + r*0.42)
                        path.lineTo(cx + r*0.50, cy - r*0.38)
                        painter.drawPath(path)
                    else:
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(QPen(QColor(T2), 1.5))
                        painter.drawEllipse(QPointF(cx, cy), r, r)
        block = block.next()

def _paint_ruled_lines(editor):
    vp = editor.viewport()
    painter = QPainter(vp)
    pen = QPen(QColor("#c8c060")); pen.setWidth(1)
    painter.setPen(pen)
    scroll_y = editor.verticalScrollBar().value()
    doc = editor.document()
    dl = doc.documentLayout()
    line_h = editor.fontMetrics().lineSpacing()
    last_y = -1
    block = doc.begin()
    while block.isValid():
        blk_layout = block.layout()
        if blk_layout:
            blk_top = dl.blockBoundingRect(block).top()
            for i in range(blk_layout.lineCount()):
                line = blk_layout.lineAt(i)
                vp_y = blk_top + line.rect().bottom() - scroll_y
                if 0 <= vp_y <= vp.height():
                    painter.drawLine(0, int(vp_y), vp.width(), int(vp_y))
                last_y = vp_y
        block = block.next()
    # continue lines below last text block to fill the viewport
    y = last_y + line_h
    while y <= vp.height():
        painter.drawLine(0, int(y), vp.width(), int(y))
        y += line_h
    painter.end()


class NoteEditor(QTextEdit):
    note_link_clicked = Signal(str)  # emits note path for localnotes:// hrefs

    def insertFromMimeData(self, source):
        if source.hasText() and not source.hasHtml():
            text = source.text()
            if _URL_RE.search(text):
                cur = self.textCursor()
                base = QTextCharFormat(cur.charFormat())
                base.setAnchor(False); base.setAnchorHref("")
                base.setForeground(QColor(T1)); base.setFontUnderline(False)
                _insert_with_urls(cur, text, base)
                # reset to non-link format after insertion
                reset = QTextCharFormat(base)
                cur.insertText("", reset)
                self.setTextCursor(cur)
                return
        super().insertFromMimeData(source)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.MouseMove:
            anchor = self.anchorAt(event.position().toPoint())
            self.viewport().setCursor(
                Qt.CursorShape.PointingHandCursor if anchor
                else Qt.CursorShape.IBeamCursor
            )
        elif event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                anchor = self.anchorAt(event.position().toPoint())
                if anchor:
                    if anchor.startswith("localnotes://"):
                        self.note_link_clicked.emit(anchor[len("localnotes://"):])
                    else:
                        QDesktopServices.openUrl(QUrl(anchor))
                    return True
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Paint:
            if _ACTIVE_THEME == "notes07":
                _paint_ruled_lines(self)
            _paint_circles(self)
        return result

# ── Date helpers ──────────────────────────────────────────────────────────────
def fmt_date(iso):
    try:
        dt  = datetime.fromisoformat(iso)
        ago = (_date.today() - dt.date()).days
        if ago == 0: return dt.strftime("%H:%M")
        if ago == 1: return "Yesterday"
        if ago < 7:  return dt.strftime("%A")
        return dt.strftime("%m/%d/%y")
    except Exception:
        return ""

def date_bucket(iso):
    try:
        ago = (_date.today() - datetime.fromisoformat(iso).date()).days
        if ago == 0: return 0
        if ago == 1: return 1
        if ago < 7:  return 2
        return 3
    except Exception:
        return 3

BUCKET_LABELS = ["Today", "Yesterday", "Previous 7 Days", "Previous 30 Days"]

# ── List item roles ───────────────────────────────────────────────────────────
R_PREVIEW  = Qt.ItemDataRole.UserRole
R_DATE     = Qt.ItemDataRole.UserRole + 1
R_HEADER   = Qt.ItemDataRole.UserRole + 2
R_PATH     = Qt.ItemDataRole.UserRole + 3
R_NOTEBOOK = Qt.ItemDataRole.UserRole + 4
R_PINNED   = Qt.ItemDataRole.UserRole + 5

# ── Note list delegate ────────────────────────────────────────────────────────
class NoteDelegate(QStyledItemDelegate):
    ITEM_H   = 62
    HEADER_H = 30

    def sizeHint(self, option, index):
        return QSize(0, self.HEADER_H if index.data(R_HEADER) else self.ITEM_H)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        r = option.rect

        if index.data(R_HEADER):
            painter.fillRect(r, QColor(BG2))
            f = QFont(); f.setPointSizeF(11); f.setWeight(QFont.Weight.DemiBold)
            painter.setFont(f)
            painter.setPen(QColor(T2))
            painter.drawText(r.adjusted(14, 0, -8, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             index.data(Qt.ItemDataRole.DisplayRole) or "")
        else:
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            active   = bool(option.state & QStyle.StateFlag.State_Active)

            painter.fillRect(r, QColor(BG2))
            if selected:
                painter.setBrush(QColor(ACC) if active else QColor(SEL))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(r.adjusted(6, 3, -6, -3), 8, 8)
            painter.setPen(QColor(DIV))
            painter.drawLine(r.left() + 14, r.bottom(), r.right(), r.bottom())

            title   = index.data(Qt.ItemDataRole.DisplayRole) or ""
            preview = index.data(R_PREVIEW) or ""
            date_s  = index.data(R_DATE)    or ""
            pinned  = bool(index.data(R_PINNED))

            ft = QFont(); ft.setPointSizeF(13.5); ft.setWeight(QFont.Weight.DemiBold)
            painter.setFont(ft); painter.setPen(QColor(T1))
            tr = r.adjusted(14, 9, -14, 0); tr.setHeight(21)
            # leave room for pin dot on the right when pinned
            title_right = -14 - (14 if pinned else 0)
            painter.drawText(tr.adjusted(0, 0, title_right, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(ft).elidedText(title, Qt.TextElideMode.ElideRight,
                                                         tr.width() + title_right))
            if pinned:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(ACC))
                cx = r.right() - 14
                cy = r.top() + 9 + 10   # vertically centered on title row
                painter.drawEllipse(QPoint(cx, cy), 4, 4)

            fs = QFont(); fs.setPointSizeF(11.5)
            painter.setFont(fs); painter.setPen(QColor(T1) if selected else QColor(T2))
            fm  = QFontMetrics(fs)
            sr  = r.adjusted(14, 35, -14, -7)
            dw  = fm.horizontalAdvance(date_s)
            painter.drawText(sr.adjusted(0, 0, -(sr.width() - dw), 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, date_s)
            pr = sr.adjusted(dw + 6, 0, 0, 0)
            painter.drawText(pr, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             fm.elidedText(preview, Qt.TextElideMode.ElideRight, pr.width()))

        painter.restore()

# ── Sidebar delegate ──────────────────────────────────────────────────────────
R_NB_NAME  = Qt.ItemDataRole.UserRole
R_NB_COUNT = Qt.ItemDataRole.UserRole + 1
R_NB_SECT  = Qt.ItemDataRole.UserRole + 2   # section header flag

class SidebarDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(0, 22 if index.data(R_NB_SECT) else 30)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        r = option.rect

        if index.data(R_NB_SECT):
            painter.fillRect(r, QColor(BG1))
            f = QFont(); f.setPointSizeF(10.5); f.setWeight(QFont.Weight.DemiBold)
            painter.setFont(f); painter.setPen(QColor(T2))
            painter.drawText(r.adjusted(14, 0, -8, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             index.data(Qt.ItemDataRole.DisplayRole) or "")
        else:
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            active   = bool(option.state & QStyle.StateFlag.State_Active)

            if selected:
                painter.setBrush(QColor(ACC) if active else QColor(SEL))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(r.adjusted(6, 1, -6, -1), 6, 6)
            else:
                painter.fillRect(r, QColor(BG1))

            name  = index.data(Qt.ItemDataRole.DisplayRole) or ""
            count = index.data(R_NB_COUNT)

            if selected and active:
                text_color = "#fff"
            elif selected:
                text_color = ACC
            else:
                text_color = T1

            _draw_nb_icon(painter,
                          r.left() + 22, r.center().y(),
                          name == TRASH_NB, text_color,
                          is_lock=name == PASSWORDS_NB,
                          is_budget=name == BUDGET_NB)

            f = QFont(); f.setPointSizeF(13)
            painter.setFont(f)
            painter.setPen(QColor(text_color))
            painter.drawText(r.adjusted(38, 0, -40, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)

            if count is not None:
                fc = QFont(); fc.setPointSizeF(11.5)
                painter.setFont(fc)
                painter.setPen(QColor("#cce" if (selected and active) else (ACC if selected else T2)))
                painter.drawText(r.adjusted(0, 0, -10, 0),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                                 str(count))

        painter.restore()

# ── Folder name dialog ────────────────────────────────────────────────────────
class FolderDialog(QDialog):
    def __init__(self, parent=None, title="New Folder", initial=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(360, 160)
        p = self.palette()
        p.setColor(QPalette.ColorRole.Window, QColor(BG1))
        self.setPalette(p)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"""
            QLabel {{
                background: transparent; color: {T1}; font-size: 15px; font-weight: 600;
            }}
            QLineEdit {{
                background: {BG2}; color: {T1}; border: 1px solid {DIV};
                border-radius: 8px; padding: 8px 12px; font-size: 14px;
            }}
            QLineEdit:focus {{ border: 1px solid {ACC}; }}
            QPushButton {{
                border-radius: 8px; font-size: 13px;
                padding: 7px 0; font-weight: 500;
            }}
            QPushButton#ok {{
                background: {ACC}; color: #fff; border: none;
            }}
            QPushButton#ok:hover {{ background: #1a8eff; }}
            QPushButton#cancel {{
                background: {SEL}; color: {T1}; border: none;
            }}
            QPushButton#cancel:hover {{ background: {DIV}; }}
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        self._label = QLabel(title)
        v.addWidget(self._label)

        self._field = QLineEdit(initial)
        self._field.selectAll()
        v.addWidget(self._field)

        row = QHBoxLayout(); row.setSpacing(10)
        cancel = QPushButton("Cancel"); cancel.setObjectName("cancel")
        ok     = QPushButton("OK");     ok.setObjectName("ok")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        self._field.returnPressed.connect(self.accept)
        row.addWidget(cancel); row.addWidget(ok)
        v.addLayout(row)

    def value(self):
        return self._field.text().strip()

class ConfirmDialog(QDialog):
    def __init__(self, parent=None, title="", body="", confirm_label="Delete"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(360, 148)
        p = self.palette()
        p.setColor(QPalette.ColorRole.Window, QColor(BG1))
        self.setPalette(p)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"""
            QLabel       {{ background:transparent; }}
            QLabel#title {{ color:{T1}; font-size:15px; font-weight:600; }}
            QLabel#body  {{ color:{T2}; font-size:13px; }}
            QPushButton  {{ border-radius:8px; font-size:13px; padding:7px 0; font-weight:500; }}
            QPushButton#confirm {{ background:#ff3b30; color:#fff; border:none; }}
            QPushButton#confirm:hover {{ background:#ff5247; }}
            QPushButton#cancel  {{ background:{SEL}; color:{T1}; border:none; }}
            QPushButton#cancel:hover {{ background:{DIV}; }}
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(6)

        lbl_title = QLabel(title); lbl_title.setObjectName("title")
        lbl_body  = QLabel(body);  lbl_body.setObjectName("body")
        lbl_body.setWordWrap(True)
        v.addWidget(lbl_title)
        v.addWidget(lbl_body)
        v.addSpacing(10)

        row = QHBoxLayout(); row.setSpacing(10)
        cancel  = QPushButton("Cancel");       cancel.setObjectName("cancel")
        confirm = QPushButton(confirm_label);  confirm.setObjectName("confirm")
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        confirm.setDefault(True)
        row.addWidget(cancel); row.addWidget(confirm)
        v.addLayout(row)

class _LinkDialog(QDialog):
    def __init__(self, url="", display="", show_display=True, notes=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Link")
        self.setFixedSize(380, 320)
        p = self.palette()
        p.setColor(QPalette.ColorRole.Window, QColor(BG1))
        self.setPalette(p); self.setAutoFillBackground(True)
        ss = f"""
            QLabel   {{ background:transparent; color:{T1}; font-size:13px; font-weight:500; }}
            QLineEdit{{ background:{BG2}; color:{T1}; border:1px solid {DIV};
                        border-radius:8px; padding:8px 12px; font-size:14px; }}
            QLineEdit:focus {{ border:1px solid {ACC}; }}
            QListWidget {{ background:{BG2}; color:{T1}; border:1px solid {DIV};
                           border-radius:8px; font-size:13px; outline:none; }}
            QListWidget::item {{ padding:6px 10px; border-radius:6px; }}
            QListWidget::item:selected {{ background:{ACC}; color:#fff; }}
            QListWidget::item:hover:!selected {{ background:{SEL}; }}
            QPushButton {{ border-radius:8px; font-size:13px; padding:7px 0; font-weight:500; }}
            QPushButton#ok     {{ background:{ACC}; color:#fff; border:none; }}
            QPushButton#ok:hover {{ background:#1a8eff; }}
            QPushButton#cancel {{ background:{SEL}; color:{T1}; border:none; }}
            QPushButton#cancel:hover {{ background:{DIV}; }}
            QPushButton#tab {{ background:transparent; color:{T2}; border:none;
                               font-size:13px; font-weight:500; padding:4px 16px; border-radius:12px; }}
            QPushButton#tab:checked {{ background:{SEL}; color:{T1}; }}
        """
        self.setStyleSheet(ss)

        v = QVBoxLayout(self); v.setContentsMargins(24, 20, 24, 20); v.setSpacing(10)

        # mode toggle
        self._notes_data = notes or {}
        tog = QHBoxLayout(); tog.setSpacing(4)
        self._btn_url  = QPushButton("URL");  self._btn_url.setObjectName("tab")
        self._btn_note = QPushButton("Note"); self._btn_note.setObjectName("tab")
        self._btn_url.setCheckable(True);  self._btn_note.setCheckable(True)
        tog.addStretch(); tog.addWidget(self._btn_url); tog.addWidget(self._btn_note); tog.addStretch()
        v.addLayout(tog)

        # ── URL panel ──────────────────────────────────────────────────────────
        self._url_panel = QWidget(); up = QVBoxLayout(self._url_panel)
        up.setContentsMargins(0, 0, 0, 0); up.setSpacing(8)
        if show_display:
            up.addWidget(QLabel("Display text"))
            self._disp = QLineEdit(display); self._disp.setPlaceholderText("Link text")
            up.addWidget(self._disp)
        else:
            self._disp = None
        up.addWidget(QLabel("URL"))
        self._url = QLineEdit(url); self._url.setPlaceholderText("https://")
        up.addWidget(self._url)
        up.addStretch()
        v.addWidget(self._url_panel)

        # ── Note panel ─────────────────────────────────────────────────────────
        self._note_panel = QWidget(); np = QVBoxLayout(self._note_panel)
        np.setContentsMargins(0, 0, 0, 0); np.setSpacing(8)
        self._note_search = QLineEdit(); self._note_search.setPlaceholderText("Search notes…")
        np.addWidget(self._note_search)
        self._note_list = QListWidget()
        self._note_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        np.addWidget(self._note_list)
        v.addWidget(self._note_panel)

        self._populate_notes("")
        self._note_search.textChanged.connect(self._populate_notes)
        self._note_list.itemDoubleClicked.connect(lambda _: self.accept())

        # ── Buttons ────────────────────────────────────────────────────────────
        row = QHBoxLayout(); row.setSpacing(10)
        cancel = QPushButton("Cancel"); cancel.setObjectName("cancel")
        ok     = QPushButton("OK");     ok.setObjectName("ok")
        cancel.clicked.connect(self.reject); ok.clicked.connect(self.accept)
        self._url.returnPressed.connect(self.accept)
        row.addWidget(cancel); row.addWidget(ok)
        v.addLayout(row)

        self._btn_url.clicked.connect(lambda: self._set_mode("url"))
        self._btn_note.clicked.connect(lambda: self._set_mode("note"))

        # start in note mode if editing an existing localnotes:// link
        if url.startswith("localnotes://"):
            self._set_mode("note")
            self._preselect_note(url[len("localnotes://"):])
        else:
            self._set_mode("url")
            (self._disp or self._url).setFocus()

    def _set_mode(self, mode):
        self._btn_url.setChecked(mode == "url")
        self._btn_note.setChecked(mode == "note")
        self._url_panel.setVisible(mode == "url")
        self._note_panel.setVisible(mode == "note")
        if mode == "note":
            self._note_search.setFocus()

    def _populate_notes(self, query=""):
        self._note_list.clear()
        q = query.lower()
        sorted_notes = sorted(
            self._notes_data.items(),
            key=lambda x: x[1].get("modified", ""),
            reverse=True,
        )
        for path, info in sorted_notes:
            title = info.get("title", "") or os.path.basename(path)
            nb    = info.get("notebook", "")
            label = f"{title}  —  {nb}" if nb else title
            if not q or q in label.lower():
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, path)
                self._note_list.addItem(item)

    def _preselect_note(self, ref):
        if not os.path.isabs(ref):
            ref = os.path.normpath(os.path.join(ROOT, ref))
        for i in range(self._note_list.count()):
            if self._note_list.item(i).data(Qt.ItemDataRole.UserRole) == ref:
                self._note_list.setCurrentRow(i)
                break

    def result_url(self):
        if self._btn_note.isChecked():
            sel = self._note_list.currentItem()
            if sel:
                path = sel.data(Qt.ItemDataRole.UserRole)
                try:
                    rel = os.path.relpath(path, ROOT)
                except ValueError:
                    rel = path
                return "localnotes://" + rel
            return ""
        return self._url.text().strip()

    def result_display(self):
        if self._btn_note.isChecked():
            sel = self._note_list.currentItem()
            if sel:
                # return just the title part (before the " — notebook" suffix)
                return sel.text().split("  —  ")[0].strip()
            return ""
        return self._disp.text().strip() if self._disp else ""

# ── Toolbar popups ────────────────────────────────────────────────────────────
class _ToolPopup(QFrame):
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(_POPUP_STYLE)

    def show_below(self, btn):
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        self.adjustSize()
        self.move(pos.x(), pos.y() + 4)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        super().hideEvent(event)
        QApplication.instance().removeEventFilter(self)

    def eventFilter(self, _obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if not self.geometry().contains(event.globalPosition().toPoint()):
                self.hide()
                return True  # consume — don't forward to editor/list
        return False

    def _divider(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"background:{DIV}; max-height:1px; border:none; margin:4px 0;")
        return f

class FormatPopup(_ToolPopup):

    def __init__(self, editor, parent):
        super().__init__(parent)
        self._ed = editor
        v = QVBoxLayout(self); v.setContentsMargins(8, 10, 8, 10); v.setSpacing(2)
        self.setFixedWidth(230)

        # B / I / U / S row
        row_w = QWidget()
        row = QHBoxLayout(row_w); row.setContentsMargins(6, 2, 6, 2); row.setSpacing(6)
        self._fmt_btns = {}
        specs = [("B", "bold",          QFont.Weight.Bold,   False, False, False),
                 ("I", "italic",        QFont.Weight.Normal,  True,  False, False),
                 ("U", "underline",     QFont.Weight.Normal,  False, True,  False),
                 ("S", "strikethrough", QFont.Weight.Normal,  False, False, True)]
        for label, fmt_type, weight, italic, underline, strike in specs:
            b = QPushButton(label); b.setFixedSize(38, 34)
            f = QFont(_FONT_BODY, 14); f.setWeight(weight)
            f.setItalic(italic); f.setUnderline(underline)
            if strike: f.setStrikeOut(True)
            b.setFont(f)
            b.setStyleSheet(_fmt_btn_ss(False))
            b.clicked.connect(lambda _, ft=fmt_type: (self._wrap(ft), self.hide()))
            self._fmt_btns[fmt_type] = b
            row.addWidget(b)
        row.addStretch()

        self._btn_link = _ToolbarIconButton("link"); self._btn_link.setFixedSize(38, 34)
        self._btn_link.setStyleSheet(_fmt_btn_ss(False))
        self._btn_link.clicked.connect(self._insert_link)
        row.addWidget(self._btn_link)

        v.addWidget(row_w)
        v.addWidget(self._divider())

        # Style rows with checkmarks
        self._style_checks = {}
        for label, style_key, size, weight in [
            ("Title",      "title",      20, QFont.Weight.Bold),
            ("Heading",    "heading",    17, QFont.Weight.Bold),
            ("Subheading", "subheading", 15, QFont.Weight.DemiBold),
            ("Body",       "body",       14, QFont.Weight.Normal),
            ("Monostyled", "mono",       13, QFont.Weight.Normal),
        ]:
            rw = QWidget(); hl = QHBoxLayout(rw)
            hl.setContentsMargins(0, 0, 8, 0); hl.setSpacing(0)

            chk = QLabel("✓"); chk.setFixedWidth(28)
            chk.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk.setStyleSheet(f"color:{ACC};font-size:14px;background:transparent;")
            chk.setVisible(False)
            self._style_checks[style_key] = chk
            hl.addWidget(chk)

            b = QPushButton(label)
            f = QFont(_FONT_BODY, size); f.setWeight(weight)
            if style_key == "mono": f = QFont(_FONT_MONO, 13)
            b.setFont(f)
            b.setStyleSheet(f"QPushButton{{background:transparent;color:{T1};border:none;"
                            f"border-radius:6px;text-align:left;padding:6px 8px 6px 0;}}"
                            f"QPushButton:hover{{background:{SEL};}}")
            b.clicked.connect(lambda _, sk=style_key: (self._set_style(sk), self.hide()))
            hl.addWidget(b)
            v.addWidget(rw)

    def show_below(self, btn):
        self._refresh()
        super().show_below(btn)

    def _refresh(self):
        cur   = self._ed.textCursor()
        block = cur.block()

        # cur.charFormat() at block pos-0 returns the block-level default (no
        # explicit font/weight), not the first character's format.  Always read
        # from one position inside the block so we get the real char format.
        if block.length() > 1:
            tmp = QTextCursor(self._ed.document())
            tmp.setPosition(block.position() + 1)
            cf = tmp.charFormat()
        else:
            cf = cur.charFormat()

        # B / I / U / S
        wt = cf.fontWeight()
        states = {
            'bold':          int(wt) >= int(QFont.Weight.Bold),
            'italic':        cf.fontItalic(),
            'underline':     cf.fontUnderline(),
            'strikethrough': cf.fontStrikeOut(),
        }
        for ft, on in states.items():
            self._fmt_btns[ft].setStyleSheet(_fmt_btn_ss(on))
        self._btn_link.setStyleSheet(_fmt_btn_ss(bool(cf.anchorHref())))

        # Paragraph style
        sz = cf.fontPointSize()
        if sz <= 0:
            sz = cf.font().pointSizeF()
        if cf.font().family() == _FONT_MONO:
            active = 'mono'
        elif sz >= 19:
            active = 'title'
        elif sz >= 16:
            active = 'heading'
        elif sz >= 14.5:
            active = 'subheading'
        else:
            active = 'body'
        for sk, lbl in self._style_checks.items():
            lbl.setVisible(sk == active)

    def _wrap(self, fmt_type):
        cur = self._ed.textCursor()
        cf  = cur.charFormat()
        fmt = QTextCharFormat()
        if fmt_type == 'bold':
            fmt.setFontWeight(QFont.Weight.Normal if cf.fontWeight() >= QFont.Weight.Bold
                              else QFont.Weight.Bold)
        elif fmt_type == 'italic':
            fmt.setFontItalic(not cf.fontItalic())
        elif fmt_type == 'underline':
            fmt.setFontUnderline(not cf.fontUnderline())
        elif fmt_type == 'strikethrough':
            fmt.setFontStrikeOut(not cf.fontStrikeOut())
        cur.mergeCharFormat(fmt)
        self._ed.mergeCurrentCharFormat(fmt)

    def _set_style(self, style):
        cur = self._ed.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cur.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                         QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        if style == 'title':
            fmt.setFontFamilies([_FONT_BODY]); fmt.setFontPointSize(20.0); fmt.setFontWeight(QFont.Weight.Bold)
        elif style == 'heading':
            fmt.setFontFamilies([_FONT_BODY]); fmt.setFontPointSize(17.0); fmt.setFontWeight(QFont.Weight.Bold)
        elif style == 'subheading':
            fmt.setFontFamilies([_FONT_BODY]); fmt.setFontPointSize(15.0); fmt.setFontWeight(QFont.Weight.DemiBold)
        elif style == 'body':
            fmt.setFontFamilies([_FONT_BODY]); fmt.setFontPointSize(14.0); fmt.setFontWeight(QFont.Weight.Normal)
            fmt.setFontItalic(False); fmt.setFontUnderline(False); fmt.setFontStrikeOut(False)
        elif style == 'mono':
            fmt.setFontFamilies([_FONT_MONO]); fmt.setFontPointSize(13.0)
        cur.setCharFormat(fmt)
        self._ed.setTextCursor(cur)

    def _insert_link(self):
        self.hide()
        cur = self._ed.textCursor()
        existing_href = cur.charFormat().anchorHref()
        has_sel = cur.hasSelection()
        notes = load_all()

        if existing_href:
            dlg = _LinkDialog(url=existing_href, show_display=False, notes=notes, parent=self._ed.window())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new_url = dlg.result_url()
            if not new_url:
                self._remove_link(cur)
                return
            fmt = QTextCharFormat()
            fmt.setAnchorHref(new_url)
            # update display text if it was a note link and title changed
            if new_url.startswith("localnotes://") and not cur.hasSelection():
                fmt.setAnchor(True)
            cur.mergeCharFormat(fmt)
        elif has_sel:
            dlg = _LinkDialog(show_display=False, notes=notes, parent=self._ed.window())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            url = dlg.result_url()
            if not url:
                return
            fmt = QTextCharFormat()
            fmt.setAnchor(True); fmt.setAnchorHref(url)
            fmt.setForeground(QColor(ACC)); fmt.setFontUnderline(True)
            cur.mergeCharFormat(fmt)
        else:
            dlg = _LinkDialog(show_display=True, notes=notes, parent=self._ed.window())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            url = dlg.result_url()
            if not url:
                return
            display = dlg.result_display() or url
            fmt = QTextCharFormat()
            fmt.setFontFamilies([_FONT_EDITOR]); fmt.setFontPointSize(14.0)
            fmt.setAnchor(True); fmt.setAnchorHref(url)
            fmt.setForeground(QColor(ACC)); fmt.setFontUnderline(True)
            cur.insertText(display, fmt)
            reset = QTextCharFormat()
            reset.setFontFamilies([_FONT_EDITOR]); reset.setFontPointSize(14.0)
            reset.setAnchor(False); reset.setAnchorHref("")
            reset.setForeground(QColor(T1)); reset.setFontUnderline(False)
            cur.insertText("", reset)
        self._ed.setTextCursor(cur)

    def _remove_link(self, cur):
        fmt = QTextCharFormat()
        fmt.setAnchor(False); fmt.setAnchorHref("")
        fmt.setForeground(QColor(T1)); fmt.setFontUnderline(False)
        cur.mergeCharFormat(fmt)
        self._ed.setTextCursor(cur)

class ListPopup(_ToolPopup):
    def __init__(self, editor, parent):
        super().__init__(parent)
        self._ed = editor
        v = QVBoxLayout(self); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(2)
        self.setFixedWidth(200)
        for label, prefix in [("• Bulleted List", "- "),
                               ("– Dashed List",   "– "),
                               ("1. Numbered List","1. "),
                               ("> Block Quote",   "> ")]:
            b = QPushButton(label)
            b.clicked.connect(lambda _, p=prefix: (self._apply(p), self.hide()))
            v.addWidget(b)

    def _apply(self, prefix):
        cur = self._ed.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cur.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
        line = cur.selectedText()
        for p in ("- ", "– ", "1. ", "> "):
            if line.startswith(p):
                line = line[len(p):]
                break
        cur.insertText(f"{prefix}{line}")
        self._ed.setTextCursor(cur)

# ── Toolbar icon buttons ──────────────────────────────────────────────────────
class _ToolbarIconButton(QPushButton):
    def __init__(self, icon_name, parent=None):
        super().__init__(parent)
        self._icon = icon_name
        self.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;border-radius:7px;}}"
            f"QPushButton:hover{{background:{SEL};}}"
            f"QPushButton:pressed{{background:{DIV};}}"
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        self._ink = QColor(T3 if not self.isEnabled() else T1)
        if self._icon == "checklist":
            self._checklist(p, cx, cy)
        elif self._icon == "table":
            self._table(p, cx, cy)
        elif self._icon == "attach":
            self._attach(p, cx, cy)
        elif self._icon == "sidebar":
            self._panel_icon(p, cx, cy, col=0)
        elif self._icon == "notelist":
            self._panel_icon(p, cx, cy, col=1)
        elif self._icon == "link":
            self._link_icon(p, cx, cy)
        p.end()

    def _checklist(self, p, cx, cy):
        pen = QPen(self._ink, 1.5, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r, lcx = 4.5, cx - 6.5
        p.drawEllipse(QRectF(lcx - r, cy - r, r * 2, r * 2))
        p.drawLine(QPointF(lcx - 2, cy + 0.5), QPointF(lcx, cy + 2.5))
        p.drawLine(QPointF(lcx, cy + 2.5),     QPointF(lcx + 3, cy - 1.5))
        lx = cx + 1.5
        p.drawLine(QPointF(lx, cy - 3), QPointF(lx + 9, cy - 3))
        p.drawLine(QPointF(lx, cy + 3), QPointF(lx + 9, cy + 3))

    def _table(self, p, cx, cy):
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(self._ink)
        cell, gap, n = 4.5, 1.5, 3
        total = n * cell + (n - 1) * gap
        ox, oy = cx - total / 2, cy - total / 2
        for row in range(n):
            for col in range(n):
                x = ox + col * (cell + gap)
                y = oy + row * (cell + gap)
                p.drawRoundedRect(QRectF(x, y, cell, cell), 1.0, 1.0)

    def _attach(self, p, cx, cy):
        pen = QPen(self._ink, 1.7, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        outer = QPainterPath()
        outer.addRoundedRect(QRectF(cx - 4, cy - 8, 8, 16), 4, 4)
        p.drawPath(outer)
        iw, ih, ir = 4.0, 11.0, 2.0
        ix, iy = cx - iw / 2, cy - 5.5
        inner = QPainterPath()
        inner.moveTo(ix, iy + ih)
        inner.lineTo(ix, iy + ir)
        inner.quadTo(ix, iy, ix + ir, iy)
        inner.lineTo(ix + iw - ir, iy)
        inner.quadTo(ix + iw, iy, ix + iw, iy + ir)
        inner.lineTo(ix + iw, iy + ih)
        p.drawPath(inner)

    def _link_icon(self, p, cx, cy):
        pen = QPen(self._ink, 1.5, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        for dx, dy in ((-2.0, 2.0), (2.0, -2.0)):
            p.save()
            p.translate(cx + dx, cy + dy)
            p.rotate(-45)
            p.drawRoundedRect(QRectF(-5.0, -2.5, 10.0, 5.0), 2.5, 2.5)
            p.restore()

    def _panel_icon(self, p, cx, cy, col):
        # three-panel layout icon; col=0 highlights left, col=1 highlights middle
        pen = QPen(self._ink, 1.4, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        x0, y0, w, h = cx - 9, cy - 7, 18, 14
        p.drawRoundedRect(QRectF(x0, y0, w, h), 2, 2)
        d1, d2 = x0 + 6, x0 + 12
        p.drawLine(QPointF(d1, y0), QPointF(d1, y0 + h))
        p.drawLine(QPointF(d2, y0), QPointF(d2, y0 + h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._ink)
        if col == 0:
            p.drawRoundedRect(QRectF(x0 + 1.5, y0 + 1.5, 4, h - 3), 1.5, 1.5)
        else:
            p.drawRect(QRectF(d1 + 0.5, y0 + 1.5, 5, h - 3))

# ── Table column/row action toolbar ──────────────────────────────────────────
_TBL_BTN_SS = (
    f"QPushButton{{background:{BG2};color:{T2};border:1px solid {DIV};"
    f"border-radius:3px;font-size:9px;letter-spacing:0.5px;padding:0;}}"
    f"QPushButton:hover{{background:{SEL};color:{T1};}}"
)

_TBL_ROW_BTN_SS = (
    f"QPushButton{{background:{BG2};color:{T2};border:1px solid {DIV};"
    f"border-radius:3px;font-size:14px;padding:0;}}"
    f"QPushButton:hover{{background:{SEL};color:{T1};}}"
)

class _TableToolbar:
    """One '···' button above the active column, one '⋮' left of the active row."""
    _CW = 22; _CH = 18   # column button dims
    _RW = 18; _RH = 26   # row button dims
    _SNAP = 5             # px to snap to column divider

    def __init__(self, editor):
        self._ed = editor
        self._table_pos = None
        self._cur_col = 0
        self._cur_row = 0
        self._drag = None  # {table_pos, col, start_x, orig_widths} while resizing
        vp = editor.viewport()
        self._col_btn = QPushButton("•••", vp)
        self._col_btn.setFixedSize(self._CW, self._CH)
        self._col_btn.setStyleSheet(_TBL_BTN_SS)
        self._col_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._col_btn.hide()
        self._row_btn = QPushButton("⋮", vp)
        self._row_btn.setFixedSize(self._RW, self._RH)
        self._row_btn.setStyleSheet(_TBL_ROW_BTN_SS)
        self._row_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._row_btn.hide()
        self._col_btn.clicked.connect(self._col_menu)
        self._row_btn.clicked.connect(self._row_menu)
        editor.verticalScrollBar().valueChanged.connect(self._reposition)

    def refresh(self):
        if self._drag is not None:
            return
        cur = self._ed.textCursor()
        table = cur.currentTable()
        if table is None:
            self._col_btn.hide(); self._row_btn.hide()
            self._table_pos = None; return
        cell = table.cellAt(cur)
        self._cur_col = cell.column()
        self._cur_row = cell.row()
        self._table_pos = table.cellAt(0, 0).firstCursorPosition().position()
        self._reposition()

    def _table(self):
        if self._table_pos is None: return None
        cur = QTextCursor(self._ed.document())
        cur.setPosition(self._table_pos)
        return cur.currentTable()

    def _reposition(self):
        table = self._table()
        if table is None:
            self._col_btn.hide(); self._row_btn.hide(); return
        ed = self._ed
        ncols, nrows = table.columns(), table.rows()
        col = min(self._cur_col, ncols - 1)
        row = min(self._cur_row, nrows - 1)
        vr = ed.viewport().rect()

        def cr(r, c):
            return ed.cursorRect(table.cellAt(r, c).firstCursorPosition())

        # Column left edges; per-column pixel widths derived from % constraints
        col_x = [cr(0, c).left() - 6 for c in range(ncols)]
        if ncols > 1:
            pcts = [c.rawValue() for c in table.format().columnWidthConstraints()]
            total_px = (col_x[1] - col_x[0]) / max(pcts[0], 0.1) * 100.0
            col_widths = [total_px * p / 100.0 for p in pcts]
        else:
            col_widths = [max(vr.width() - col_x[0] - 6, 60)]

        # Row top edges and height
        row_y = [cr(r, 0).top() - 6 for r in range(nrows)]
        row_h = (row_y[1] - row_y[0]) if nrows > 1 else (cr(0, 0).height() + 12)

        # Table bottom: last cell cursor bottom + bottom cell padding
        table_bottom = cr(nrows - 1, 0).bottom() + 6

        # ── column button: centred below active column, just under table ──────
        bx = max(0, min(int(col_x[col] + (col_widths[col] - self._CW) / 2),
                        vr.width() - self._CW - 2))
        by = min(table_bottom + 4, vr.height() - self._CH - 2)
        self._col_btn.move(bx, by)
        vis_c = table_bottom > 0
        self._col_btn.setVisible(vis_c)
        if vis_c: self._col_btn.raise_()

        # ── row button: right of viewport, vertically centred on active row ──
        rt = row_y[row]
        rb = row_y[row + 1] if row + 1 < nrows else rt + row_h
        bx2 = vr.width() - self._RW - 4
        by2 = rt + (rb - rt - self._RH) // 2
        self._row_btn.move(bx2, by2)
        vis_r = 0 <= by2 < vr.height() - 2
        self._row_btn.setVisible(vis_r)
        if vis_r: self._row_btn.raise_()

    # ── column resize drag ────────────────────────────────────────────────────
    def divider_at(self, pos):
        """Return (table, col_idx) if pos is within _SNAP px of a column divider."""
        cur = self._ed.cursorForPosition(pos)
        table = cur.currentTable()
        if table is None:
            return None
        for c in range(table.columns() - 1):
            divider_x = self._ed.cursorRect(
                table.cellAt(0, c + 1).firstCursorPosition()).left() - 6
            if abs(pos.x() - divider_x) <= self._SNAP:
                return (table, c)
        return None

    def start_drag(self, table, col, x):
        self._drag = {
            'table_pos': table.cellAt(0, 0).firstCursorPosition().position(),
            'col': col, 'start_x': x,
            'orig_widths': [c.rawValue()
                            for c in table.format().columnWidthConstraints()],
        }

    def update_drag(self, x):
        if self._drag is None:
            return
        cur = QTextCursor(self._ed.document())
        cur.setPosition(self._drag['table_pos'])
        table = cur.currentTable()
        if table is None:
            self._drag = None; return
        col  = self._drag['col']
        orig = self._drag['orig_widths']
        vp_w = self._ed.viewport().width()
        delta = (x - self._drag['start_x']) / vp_w * 100.0
        MIN = 5.0
        w0 = max(MIN, orig[col] + delta)
        w1 = max(MIN, orig[col] + orig[col + 1] - w0)
        w0 = orig[col] + orig[col + 1] - w1
        new_w = list(orig); new_w[col] = w0; new_w[col + 1] = w1
        fmt = table.format()
        fmt.setColumnWidthConstraints([
            QTextLength(QTextLength.Type.PercentageLength, w) for w in new_w])
        table.setFormat(fmt)
        self._reposition()

    def end_drag(self):
        self._drag = None
        self._reposition()

    def _col_menu(self):
        table = self._table()
        if table is None: return
        col = min(self._cur_col, table.columns() - 1)
        menu = QMenu(self._ed); menu.setStyleSheet(_MENU_SS)
        menu.addAction("Add Column Before", lambda: self._add_col(col, True))
        menu.addAction("Add Column After",  lambda: self._add_col(col, False))
        menu.addSeparator()
        a = menu.addAction("Delete Column", lambda: self._del_col(col))
        if table.columns() <= 1: a.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Reset Column Widths", lambda: self._reset_col_widths(table))
        menu.exec(self._col_btn.mapToGlobal(QPoint(0, self._CH + 2)))

    def _row_menu(self):
        table = self._table()
        if table is None: return
        row = min(self._cur_row, table.rows() - 1)
        menu = QMenu(self._ed); menu.setStyleSheet(_MENU_SS)
        menu.addAction("Add Row Above", lambda: self._add_row(row, True))
        menu.addAction("Add Row Below", lambda: self._add_row(row, False))
        menu.addSeparator()
        a = menu.addAction("Delete Row", lambda: self._del_row(row))
        if table.rows() <= 1: a.setEnabled(False)
        menu.exec(self._row_btn.mapToGlobal(QPoint(self._RW + 2, 0)))

    def _reset_col_widths(self, table):
        ncols = table.columns()
        self._apply_col_widths(table, [100.0 / ncols] * ncols)
        self._reposition()

    @staticmethod
    def _apply_col_widths(table, new_widths):
        fmt = table.format()
        fmt.setColumnWidthConstraints([
            QTextLength(QTextLength.Type.PercentageLength, w) for w in new_widths])
        table.setFormat(fmt)

    def _add_col(self, col, before):
        table = self._table()
        if table is None: return
        orig = [c.rawValue() for c in table.format().columnWidthConstraints()]
        pos = col if before else col + 1
        table.insertColumns(pos, 1)
        for r in range(table.rows()):
            table.cellAt(r, pos).setFormat(_table_cell_fmt(r == 0))
        # new column takes an equal share; existing columns shrink proportionally
        ncols = table.columns()
        new_pct = 100.0 / ncols
        scale   = (100.0 - new_pct) / 100.0
        new_w = []
        for i in range(ncols):
            if i == pos:
                new_w.append(new_pct)
            else:
                new_w.append(orig[i if i < pos else i - 1] * scale)
        self._apply_col_widths(table, new_w)
        self._reposition()

    def _del_col(self, col):
        table = self._table()
        if table is None or table.columns() <= 1: return
        orig = [c.rawValue() for c in table.format().columnWidthConstraints()]
        table.removeColumns(col, 1)
        self._cur_col = min(self._cur_col, table.columns() - 1)
        # redistribute removed column's width proportionally to survivors
        remaining = [w for i, w in enumerate(orig) if i != col]
        total = sum(remaining)
        new_w = [w / total * 100.0 for w in remaining] if total else \
                [100.0 / table.columns()] * table.columns()
        self._apply_col_widths(table, new_w)
        self._reposition()

    def _add_row(self, row, above):
        table = self._table()
        if table is None: return
        pos = row if above else row + 1
        table.insertRows(pos, 1)
        for c in range(table.columns()):
            table.cellAt(pos, c).setFormat(_table_cell_fmt(pos == 0))
        self._reposition()

    def _del_row(self, row):
        table = self._table()
        if table is None or table.rows() <= 1: return
        table.removeRows(row, 1)
        self._cur_row = min(self._cur_row, table.rows() - 1)
        self._reposition()

# ── AI backend ───────────────────────────────────────────────────────────────
class _OllamaClient:
    def __init__(self, endpoint, model, embed_model):
        self._endpoint   = endpoint.rstrip("/")
        self._model      = model
        self._embed_model = embed_model

    def ping(self):
        if not _REQUESTS_OK:
            return False
        try:
            r = _requests.get(self._endpoint, timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def embed(self, text):
        if not _REQUESTS_OK:
            return None
        try:
            r = _requests.post(
                f"{self._endpoint}/api/embed",
                json={"model": self._embed_model, "input": text},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["embeddings"][0]
        except Exception:
            return None

    def chat_stream(self, messages):
        """Yields (token_str, is_done) pairs via Ollama streaming API."""
        if not _REQUESTS_OK:
            yield "[requests not installed]", True
            return
        try:
            r = _requests.post(
                f"{self._endpoint}/api/chat",
                json={"model": self._model, "messages": messages, "stream": True},
                stream=True,
                timeout=120,
            )
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line.decode("utf-8"))
                token = data.get("message", {}).get("content", "")
                done  = data.get("done", False)
                yield token, done
                if done:
                    break
        except Exception as e:
            yield f"\n\n[Error: {e}]", True


class _AIIndex:
    _DB_PATH    = os.path.join(_HERE, ".ai_index")
    _COLLECTION = "notes"

    def __init__(self, ollama: "_OllamaClient"):
        self._ollama = ollama
        self._col    = None
        if not _CHROMA_OK:
            return
        try:
            db = _chromadb.PersistentClient(path=self._DB_PATH)
            self._col = db.get_or_create_collection(
                self._COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            self._col = None

    def ready(self):
        return self._col is not None

    def total_chunks(self):
        try:
            return self._col.count() if self._col else 0
        except Exception:
            return 0

    def _chunk(self, body, max_chars=600):
        paras   = [p.strip() for p in re.split(r'\n{2,}', body) if p.strip()]
        chunks  = []
        current = ""
        for p in paras:
            if len(current) + len(p) + 2 < max_chars:
                current = (current + "\n\n" + p).strip() if current else p
            else:
                if current:
                    chunks.append(current)
                current = p
        if current:
            chunks.append(current)
        return chunks or [body[:max_chars]]

    def index_note(self, path, title, notebook, body):
        if not self.ready():
            return 0
        try:
            existing = self._col.get(where={"path": path})
            if existing["ids"]:
                self._col.delete(ids=existing["ids"])
        except Exception:
            pass
        chunks = self._chunk(body)
        n = 0
        for i, chunk in enumerate(chunks):
            emb = self._ollama.embed(chunk)
            if emb is None:
                continue
            self._col.upsert(
                ids=[f"{path}::{i}"],
                embeddings=[emb],
                documents=[chunk],
                metadatas=[{"path": path, "title": title, "notebook": notebook}],
            )
            n += 1
        return n

    def query(self, question, n_results=5):
        if not self.ready():
            return []
        emb = self._ollama.embed(question)
        if emb is None:
            return []
        try:
            count = self._col.count()
            if count == 0:
                return []
            results = self._col.query(
                query_embeddings=[emb],
                n_results=min(n_results, count),
                include=["documents", "metadatas"],
            )
            out, seen = [], set()
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                p = meta["path"]
                if p not in seen:
                    seen.add(p)
                    out.append({"path": p, "title": meta["title"],
                                "notebook": meta["notebook"], "text": doc})
            return out
        except Exception:
            return []


class _AIIndexWorker(QThread):
    progress = Signal(int, int)   # (done, total)
    finished = Signal(int)        # total chunks indexed

    def __init__(self, index: "_AIIndex", notes: dict, parent=None):
        super().__init__(parent)
        self._index = index
        self._notes = notes

    def run(self):
        items = [(p, i) for p, i in self._notes.items()
                 if i.get("notebook") != PASSWORDS_NB]
        total = len(items)
        chunks = 0
        for done, (path, info) in enumerate(items, 1):
            try:
                raw  = open(path, encoding="utf-8", errors="ignore").read()
                body = _body(raw)
                chunks += self._index.index_note(
                    path, info["title"], info.get("notebook", ""), body)
            except Exception:
                pass
            self.progress.emit(done, total)
        self.finished.emit(chunks)


class _AIQueryWorker(QThread):
    sources_ready = Signal(list)  # fired before first token
    token_ready   = Signal(str)
    done          = Signal()
    error         = Signal(str)

    def __init__(self, index: "_AIIndex", client: "_OllamaClient",
                 question: str, parent=None):
        super().__init__(parent)
        self._index    = index
        self._client   = client
        self._question = question

    def run(self):
        sources = self._index.query(self._question)
        self.sources_ready.emit(sources)

        context = "\n\n".join(
            f"### {s['title']}\n{s['text']}" for s in sources
        )
        system = (
            "You are a helpful assistant with access to the user's personal notes. "
            "Answer concisely based on the provided notes. "
            "If the notes don't contain the answer, say so."
        )
        user_msg = (
            f"Relevant notes:\n\n{context}\n\nQuestion: {self._question}"
            if context else self._question
        )
        messages = [
            {"role": "system",    "content": system},
            {"role": "user",      "content": user_msg},
        ]
        try:
            for token, is_done in self._client.chat_stream(messages):
                self.token_ready.emit(token)
                if is_done:
                    break
        except Exception as e:
            self.error.emit(str(e))
        self.done.emit()


# ── AI Chat Panel ─────────────────────────────────────────────────────────────
class _ChatBubble(QWidget):
    source_clicked = Signal(str)   # emits note path

    def __init__(self, text, is_user, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = text

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._bubble = QLabel(text)
        self._bubble.setWordWrap(True)
        self._bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._bubble.setFont(QFont(_FONT_BODY, 13))

        if is_user:
            self._bubble.setStyleSheet(
                f"background:{ACC};color:#ffffff;border-radius:14px;"
                f"padding:9px 13px;font-size:13px;"
            )
            row.addStretch()
            row.addWidget(self._bubble)
        else:
            self._bubble.setStyleSheet(
                f"background:{BG2};color:{T1};border-radius:14px;"
                f"padding:9px 13px;font-size:13px;"
            )
            avatar = QLabel("✦")
            avatar.setFixedSize(26, 26)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar.setStyleSheet(
                f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                f"stop:0 #5e5ce6,stop:1 {ACC});"
                f"color:#ffffff;border-radius:13px;font-size:12px;font-weight:700;"
            )
            row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(self._bubble, 1)

        v.addLayout(row)
        self._src_row_layout = None
        self._src_indent     = 34 if not is_user else 0

    def append_token(self, token: str):
        self._text += token
        self._bubble.setText(self._text)

    def set_sources(self, sources: list):
        """Adds clickable note chips below the bubble (called after streaming)."""
        if not sources or self._src_row_layout is not None:
            return
        src_row = QHBoxLayout()
        src_row.setContentsMargins(self._src_indent, 0, 0, 0)
        src_row.setSpacing(5)
        lbl = QLabel("Sources:")
        lbl.setStyleSheet(f"color:{T3};font-size:11px;background:transparent;")
        src_row.addWidget(lbl)
        for s in sources:
            chip = QPushButton(s["title"])
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton{{background:{SEL};color:{T2};border-radius:8px;"
                f"padding:2px 8px;font-size:11px;border:1px solid {DIV};"
                f"font-family:'{_FONT_BODY}';}}"
                f"QPushButton:hover{{background:{DIV};color:{T1};}}"
            )
            path = s["path"]
            chip.clicked.connect(lambda _, p=path: self.source_clicked.emit(p))
            src_row.addWidget(chip)
        src_row.addStretch()
        self.layout().addLayout(src_row)
        self._src_row_layout = src_row


class _AISettingsPanel(QWidget):
    saved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"background:{BG2};border-bottom:1px solid {DIV};"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        lbl_ss  = f"color:{T2};font-size:11px;background:transparent;"
        fld_ss  = (
            f"QLineEdit{{background:{BG1};color:{T1};border:1px solid {DIV};"
            f"border-radius:6px;padding:4px 8px;font-size:12px;"
            f"font-family:'{_FONT_BODY}';}}"
            f"QLineEdit:focus{{border:1px solid {ACC};}}"
        )

        for attr, label, default in (
            ("_fld_endpoint",   "Ollama endpoint",  _AI_ENDPOINT),
            ("_fld_model",      "Chat model",       _AI_MODEL),
            ("_fld_embed",      "Embed model",      _AI_EMBED),
        ):
            row_lbl = QLabel(label)
            row_lbl.setStyleSheet(lbl_ss)
            fld = QLineEdit(_AI_CFG.get(
                {"_fld_endpoint": "endpoint",
                 "_fld_model":    "model",
                 "_fld_embed":    "embed_model"}[attr], default))
            fld.setStyleSheet(fld_ss)
            setattr(self, attr, fld)
            v.addWidget(row_lbl)
            v.addWidget(fld)

        save_btn = QPushButton("Save & rebuild index")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(
            f"QPushButton{{background:{ACC};color:#fff;border-radius:8px;"
            f"padding:5px 10px;font-size:12px;border:none;}}"
            f"QPushButton:hover{{background:#1a94ff;}}"
        )
        save_btn.clicked.connect(self._save)
        v.addWidget(save_btn)

    def _save(self):
        _CFG["ai"] = {
            "enabled":     True,
            "endpoint":    self._fld_endpoint.text().strip(),
            "model":       self._fld_model.text().strip(),
            "embed_model": self._fld_embed.text().strip(),
        }
        _save_config(_CFG)
        self.saved.emit()


class _AIChatPanel(QWidget):
    open_note = Signal(str)   # emits path → NotesApp opens it

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("aiPanel")
        self.setStyleSheet(f"QWidget#aiPanel{{background:{BG1};border-left:1px solid {DIV};}}")
        self.setMinimumWidth(280)

        self._ollama      = _OllamaClient(_AI_ENDPOINT, _AI_MODEL, _AI_EMBED)
        self._index       = _AIIndex(self._ollama)
        self._query_worker: "_AIQueryWorker | None" = None
        self._stream_bubble: "_ChatBubble | None"   = None
        self._indexed_chunks = 0

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = QWidget()
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(f"background:{BG1};border-bottom:1px solid {DIV};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(14, 0, 10, 0)
        hl.setSpacing(8)

        icon_lbl = QLabel("✦")
        icon_lbl.setFixedSize(22, 22)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 #5e5ce6,stop:1 {ACC});"
            f"color:#ffffff;border-radius:11px;font-size:11px;font-weight:700;"
        )

        title_lbl = QLabel("Ask AI")
        title_lbl.setStyleSheet(
            f"color:{T1};font-size:14px;font-weight:600;background:transparent;")

        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"color:{T3};font-size:8px;background:transparent;")
        self._status_dot.setToolTip("No model connected")

        gear_btn = QPushButton("⚙")
        gear_btn.setFixedSize(24, 24)
        gear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gear_btn.setCheckable(True)
        gear_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{T2};font-size:13px;}}"
            f"QPushButton:hover{{color:{T1};}}"
            f"QPushButton:checked{{color:{ACC};}}"
        )
        gear_btn.clicked.connect(self._toggle_settings)
        self._gear_btn = gear_btn

        hl.addWidget(icon_lbl)
        hl.addWidget(title_lbl)
        hl.addStretch()
        hl.addWidget(self._status_dot)
        hl.addWidget(gear_btn)
        v.addWidget(hdr)

        # ── Settings panel (hidden by default) ───────────────────────────────
        self._settings = _AISettingsPanel()
        self._settings.hide()
        self._settings.saved.connect(self._on_settings_saved)
        v.addWidget(self._settings)

        # ── Index progress bar ────────────────────────────────────────────────
        self._progress_bar = QWidget()
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setStyleSheet(f"background:{DIV};")
        self._progress_bar.hide()
        self._progress_fill = QWidget(self._progress_bar)
        self._progress_fill.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 #5e5ce6,stop:1 {ACC});border-radius:1px;"
        )
        self._progress_fill.setGeometry(0, 0, 0, 3)
        v.addWidget(self._progress_bar)

        # ── Chat scroll area ──────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            f"QScrollArea{{background:{BG1};border:none;}}"
            f"QScrollBar:vertical{{background:{BG1};width:4px;border-radius:2px;}}"
            f"QScrollBar::handle:vertical{{background:{DIV};border-radius:2px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        scroll_content = QWidget()
        scroll_content.setStyleSheet(f"background:{BG1};")
        self._msg_layout = QVBoxLayout(scroll_content)
        self._msg_layout.setContentsMargins(12, 12, 12, 12)
        self._msg_layout.setSpacing(14)
        self._msg_layout.addStretch()
        self._scroll.setWidget(scroll_content)
        v.addWidget(self._scroll, 1)

        # ── Suggested prompts ─────────────────────────────────────────────────
        self._suggestions = QWidget()
        self._suggestions.setStyleSheet(f"background:{BG1};border-top:1px solid {DIV};")
        sl = QVBoxLayout(self._suggestions)
        sl.setContentsMargins(12, 8, 12, 8)
        sl.setSpacing(6)
        sl_lbl = QLabel("Suggested")
        sl_lbl.setStyleSheet(
            f"color:{T3};font-size:11px;font-weight:600;background:transparent;")
        sl.addWidget(sl_lbl)
        for p in ("Summarize my recent notes",
                  "What did I spend the most on?",
                  "What are my todos?"):
            btn = QPushButton(p)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{background:{BG2};color:{T1};border:1px solid {DIV};"
                f"border-radius:10px;padding:6px 12px;font-size:12px;"
                f"text-align:left;font-family:'{_FONT_BODY}';}}"
                f"QPushButton:hover{{background:{SEL};color:{T1};}}"
            )
            btn.clicked.connect(lambda _, txt=p: self._insert_suggestion(txt))
            sl.addWidget(btn)
        v.addWidget(self._suggestions)

        # ── Input bar ─────────────────────────────────────────────────────────
        input_bar = QWidget()
        input_bar.setStyleSheet(f"background:{BG1};border-top:1px solid {DIV};")
        input_bar.setFixedHeight(64)
        il = QHBoxLayout(input_bar)
        il.setContentsMargins(10, 10, 10, 10)
        il.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask about your notes…")
        self._input.setStyleSheet(
            f"QLineEdit{{background:{BG2};color:{T1};border:1px solid {DIV};"
            f"border-radius:12px;padding:7px 13px;font-size:13px;"
            f"font-family:'{_FONT_BODY}';}}"
            f"QLineEdit:focus{{border:1px solid {ACC};}}"
        )
        self._input.returnPressed.connect(self._send)

        self._send_btn = QPushButton("↑")
        self._send_btn.setFixedSize(32, 32)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setStyleSheet(
            f"QPushButton{{background:{ACC};color:#ffffff;border-radius:16px;"
            f"font-size:16px;font-weight:700;border:none;}}"
            f"QPushButton:hover{{background:#1a94ff;}}"
            f"QPushButton:pressed{{background:#0070e0;}}"
        )
        self._send_btn.clicked.connect(self._send)

        il.addWidget(self._input, 1)
        il.addWidget(self._send_btn)
        v.addWidget(input_bar)

        self._add_bubble(
            "Hi! I can search and summarize your notes. "
            + ("Connect Ollama to get started — open ⚙ settings above."
               if not _AI_ENABLED else
               "Indexing your notes in the background…"),
            is_user=False,
        )

        if _AI_ENABLED:
            self._ping_timer = QTimer(self)
            self._ping_timer.timeout.connect(self._ping_ollama)
            self._ping_timer.start(10_000)
            QTimer.singleShot(500, self._ping_ollama)

    # ── public API called by NotesApp ────────────────────────────────────────
    def notify_index_progress(self, done: int, total: int):
        if total == 0:
            return
        self._progress_bar.show()
        w = int(self._progress_bar.width() * done / total)
        self._progress_fill.setGeometry(0, 0, w, 3)

    def notify_index_done(self, chunks: int):
        self._indexed_chunks = chunks
        self._progress_bar.hide()
        self._progress_fill.setGeometry(0, 0, 0, 3)
        self._update_status_tooltip()

    def reindex_note(self, path: str, title: str, notebook: str, body: str):
        if not _AI_ENABLED or not self._index.ready():
            return
        import threading
        threading.Thread(
            target=self._index.index_note,
            args=(path, title, notebook, body),
            daemon=True,
        ).start()

    # ── internals ────────────────────────────────────────────────────────────
    def _ping_ollama(self):
        ok = self._ollama.ping()
        color = "#30d158" if ok else T3
        tip   = f"Ollama connected  ·  {self._indexed_chunks} chunks indexed" if ok \
                else "Ollama not running  ·  start with: ollama serve"
        self._status_dot.setStyleSheet(
            f"color:{color};font-size:8px;background:transparent;")
        self._status_dot.setToolTip(tip)

    def _update_status_tooltip(self):
        self._status_dot.setToolTip(
            f"{self._indexed_chunks} chunks indexed")

    def _toggle_settings(self, checked: bool):
        self._settings.setVisible(checked)

    def _on_settings_saved(self):
        self._gear_btn.setChecked(False)
        self._settings.hide()
        # reload runtime values from config
        global _AI_ENDPOINT, _AI_MODEL, _AI_EMBED, _AI_CFG
        _AI_CFG      = _CFG.get("ai", {})
        _AI_ENDPOINT = _AI_CFG.get("endpoint", "http://localhost:11434")
        _AI_MODEL    = _AI_CFG.get("model", "llama3.2")
        _AI_EMBED    = _AI_CFG.get("embed_model", "nomic-embed-text")
        self._ollama = _OllamaClient(_AI_ENDPOINT, _AI_MODEL, _AI_EMBED)
        self._index  = _AIIndex(self._ollama)
        self._add_bubble("Settings saved. Rebuild index to apply.", is_user=False)

    def _add_bubble(self, text, is_user) -> "_ChatBubble":
        bubble = _ChatBubble(text, is_user)
        bubble.source_clicked.connect(self.open_note)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        QTimer.singleShot(50, self._scroll_to_bottom)
        return bubble

    def _scroll_to_bottom(self):
        self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum())

    def _insert_suggestion(self, text):
        self._input.setText(text)
        self._input.setFocus()

    def _set_busy(self, busy: bool):
        self._input.setEnabled(not busy)
        self._send_btn.setEnabled(not busy)
        self._send_btn.setText("◼" if busy else "↑")

    def _send(self):
        text = self._input.text().strip()
        if not text or self._query_worker is not None:
            return
        self._input.clear()
        self._suggestions.hide()
        self._add_bubble(text, is_user=True)

        if not _AI_ENABLED or not self._index.ready() or not self._ollama.ping():
            self._add_bubble(
                "AI is not connected. Enable it in ⚙ settings and make sure "
                "Ollama is running (`ollama serve`).",
                is_user=False,
            )
            return

        # thinking indicator
        self._stream_bubble = self._add_bubble("", is_user=False)
        self._stream_bubble._bubble.setText("• • •")
        self._set_busy(True)

        worker = _AIQueryWorker(self._index, self._ollama, text, self)
        worker.sources_ready.connect(self._on_sources_ready)
        worker.token_ready.connect(self._on_token)
        worker.done.connect(self._on_query_done)
        worker.error.connect(self._on_query_error)
        self._query_worker = worker
        worker.start()

    def _on_sources_ready(self, sources: list):
        if self._stream_bubble:
            self._stream_bubble._bubble.setText("")
            self._stream_bubble._pending_sources = sources

    def _on_token(self, token: str):
        if self._stream_bubble:
            self._stream_bubble.append_token(token)
            self._scroll_to_bottom()

    def _on_query_done(self):
        if self._stream_bubble:
            sources = getattr(self._stream_bubble, "_pending_sources", [])
            self._stream_bubble.set_sources(sources)
        self._stream_bubble = None
        self._query_worker  = None
        self._set_busy(False)

    def _on_query_error(self, msg: str):
        if self._stream_bubble:
            self._stream_bubble._bubble.setText(f"Error: {msg}")
        self._on_query_done()


# ── Main window ───────────────────────────────────────────────────────────────
class NotesApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_nb    = "Notes"
        self.current_notes = {}
        self.sel_path      = None
        self._dirty        = False
        self._is_pw_note   = False
        self._is_bdg_note  = False
        self._search_query = ''
        self._save_timer   = QTimer(singleShot=True)
        self._save_timer.timeout.connect(self._flush_save)

        self._pinned = set(_CFG.get("pinned", []))

        self.setWindowTitle("Notes")
        self.resize(1100, 680)
        self._build_ui()
        self._load_sidebar()
        self._ai_panel.open_note.connect(self._open_note)
        if _AI_ENABLED and self._ai_panel._index.ready():
            self._start_index_worker()

        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._new_note)
        QShortcut(QKeySequence("Ctrl+Backspace"), self).activated.connect(self._delete_note)
        QShortcut(QKeySequence("Ctrl+Shift+N"), self).activated.connect(self._new_notebook)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self._toggle_search)
        QShortcut(QKeySequence("Ctrl+Meta+F"), self).activated.connect(self._toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self).activated.connect(self._toggle_sidebar_panel)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self).activated.connect(self._toggle_list_panel)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._exit_fullscreen)
        QTimer.singleShot(0, lambda: self._switch_notebook("Notes"))

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QSplitter(Qt.Orientation.Horizontal)
        root.setHandleWidth(0)
        self.setCentralWidget(root)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_list_panel())
        root.addWidget(self._build_editor_panel())
        self._ai_panel = _AIChatPanel()
        self._ai_panel.hide()
        root.addWidget(self._ai_panel)
        self._root_splitter = root
        root.setSizes([200, 260, 640, 320])
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 0)
        root.setStretchFactor(2, 1)
        root.setStretchFactor(3, 0)
        root.setCollapsible(0, True)
        root.setCollapsible(1, True)

    # ── Sidebar ───────────────────────────────────────────────────────────────
    def _build_sidebar(self):
        w = QWidget(); w.setObjectName("sidebar"); w.setFixedWidth(200)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(0)

        # header row — replaces the section-header list item for the root folder
        hdr = QWidget(); hdr.setFixedHeight(22)
        hdr.setStyleSheet(f"background:{BG1};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(14, 0, 6, 0); hl.setSpacing(0)
        self._root_lbl = QLabel(os.path.basename(ROOT) or "Notes")
        self._root_lbl.setStyleSheet(
            f"color:{T2};font-size:10.5px;font-weight:600;background:transparent;"
        )
        hl.addWidget(self._root_lbl)
        hl.addStretch()
        self._folder_btn = QPushButton("•••")
        self._folder_btn.setFixedSize(28, 20)
        self._folder_btn.setToolTip("Change notes folder")
        self._folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._folder_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{T2};"
            f"font-size:10px;letter-spacing:1px;border-radius:3px;padding:0;}}"
            f"QPushButton:hover{{color:{T1};background:{SEL};}}"
        )
        self._folder_btn.clicked.connect(self._show_sidebar_menu)
        hl.addWidget(self._folder_btn)
        v.addWidget(hdr)

        self.sidebar_list = QListWidget()
        self.sidebar_list.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_list.setItemDelegate(SidebarDelegate())
        self.sidebar_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_list.currentItemChanged.connect(self._on_sidebar_change)
        self.sidebar_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sidebar_list.customContextMenuRequested.connect(self._on_sidebar_context_menu)
        v.addWidget(self.sidebar_list)

        # bottom: "New Folder" button
        self._sidebar_bar = QWidget(); self._sidebar_bar.setFixedHeight(36)
        self._sidebar_bar.setStyleSheet(f"background:{BG1};")
        bl  = QHBoxLayout(self._sidebar_bar); bl.setContentsMargins(8, 0, 8, 0)
        self._new_folder_btn = QPushButton("+  New Folder")
        self._new_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_folder_btn.clicked.connect(self._show_new_folder_menu)
        self._new_folder_btn.setStyleSheet(
            f"QPushButton{{background:{BG2};color:{T1};border:1px solid {DIV};"
            f"border-radius:6px;font-size:12px;padding:4px 8px;}}"
            f"QPushButton:hover{{background:{SEL};color:{T1};}}"
        )
        bl.addWidget(self._new_folder_btn)
        v.addWidget(self._sidebar_bar)
        return w

    def _load_sidebar(self):
        self.sidebar_list.blockSignals(True)
        self.sidebar_list.clear()

        self._root_lbl.setText(os.path.basename(ROOT) or "Notes")
        self._add_sidebar_item(ALL_NB_LABEL, ALL_NB, total_count())

        notebooks = list_notebooks()
        for nb in notebooks:
            if nb in _SPECIAL_NBS or nb == TRASH_NB:
                continue
            self._add_sidebar_item(nb, nb, note_count(nb))

        # special notebooks (Passwords, Budget) with separator
        specials = [nb for nb in (PASSWORDS_NB, BUDGET_NB) if nb in notebooks]
        if specials:
            self._add_sidebar_section("")
            for nb in specials:
                self._add_sidebar_item(nb, nb, note_count(nb))

        # trash always last, with separator
        if TRASH_NB in notebooks:
            self._add_sidebar_section("")
            self._add_sidebar_item(TRASH_NB, TRASH_NB, note_count(TRASH_NB))

        self.sidebar_list.blockSignals(False)
        self._sync_sidebar_selection()

    def _add_sidebar_section(self, text):
        it = QListWidgetItem(text)
        it.setData(R_NB_SECT, True)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        self.sidebar_list.addItem(it)

    def _add_sidebar_item(self, label, nb_key, count):
        it = QListWidgetItem(label)
        it.setData(R_NB_NAME,  nb_key)
        it.setData(R_NB_COUNT, count)
        it.setData(R_NB_SECT,  False)
        self.sidebar_list.addItem(it)

    def _sync_sidebar_selection(self):
        self.sidebar_list.blockSignals(True)
        for i in range(self.sidebar_list.count()):
            it = self.sidebar_list.item(i)
            if it and not it.data(R_NB_SECT) and it.data(R_NB_NAME) == self.current_nb:
                self.sidebar_list.setCurrentItem(it)
                break
        self.sidebar_list.blockSignals(False)

    def _on_sidebar_change(self, current, _prev):
        if current and not current.data(R_NB_SECT):
            nb = current.data(R_NB_NAME)
            if nb and nb != self.current_nb:
                self._flush_save()
                self._switch_notebook(nb)

    _PROTECTED = {ALL_NB, "Notes", TRASH_NB, PASSWORDS_NB, BUDGET_NB}

    def _on_sidebar_context_menu(self, pos):
        it = self.sidebar_list.itemAt(pos)
        if not it or it.data(R_NB_SECT):
            return
        nb = it.data(R_NB_NAME)
        if nb in self._PROTECTED:
            return
        menu = QMenu(self); menu.setStyleSheet(_MENU_SS)
        menu.addAction("Rename", lambda: self._rename_notebook(nb))
        menu.addAction("Delete", lambda: self._delete_notebook(nb))
        menu.exec(self.sidebar_list.mapToGlobal(pos))

    def _rename_notebook(self, nb):
        dlg = FolderDialog(self, title="Rename Folder", initial=nb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dlg.value()
        if not new_name or new_name == nb:
            return
        new_path = nb_path(new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Exists", f'"{new_name}" already exists.')
            return
        os.rename(nb_path(nb), new_path)
        if self.current_nb == nb:
            self.current_nb = new_name
        self._load_sidebar()
        self._switch_notebook(self.current_nb)

    def _delete_notebook(self, nb):
        count = note_count(nb)
        plural = "s" if count != 1 else ""
        detail = f"This will move {count} note{plural} to Recently Deleted." if count else "The folder is empty."
        dlg = ConfirmDialog(self, title=f'Delete "{nb}"?', body=detail)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        trash = nb_path(TRASH_NB)
        os.makedirs(trash, exist_ok=True)
        folder = nb_path(nb)
        for fname in os.listdir(folder):
            if fname.endswith(".md") and fname not in _SKIP_FILES:
                src = os.path.join(folder, fname)
                dst = os.path.join(trash, fname)
                if os.path.exists(dst):
                    stem, ext = os.path.splitext(fname)
                    dst = os.path.join(trash, f"{stem}_{int(datetime.now().timestamp())}{ext}")
                shutil.move(src, dst)
        try:
            os.rmdir(folder)
        except OSError:
            pass
        if self.current_nb == nb:
            self.current_nb = ALL_NB
        self._load_sidebar()
        self._switch_notebook(self.current_nb)

    # ── Note list ─────────────────────────────────────────────────────────────
    def _build_list_panel(self):
        w = QWidget(); w.setObjectName("listPanel")
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        hdr = QWidget(); hdr.setFixedHeight(56); hdr.setObjectName("listHeader")
        hl  = QVBoxLayout(hdr); hl.setContentsMargins(14, 10, 14, 4); hl.setSpacing(1)
        self._list_title = QLabel("Notes")
        self._list_title.setStyleSheet(f"background:transparent;color:{T1};font-size:18px;font-weight:700;")
        self._list_count = QLabel("")
        self._list_count.setStyleSheet(f"background:transparent;color:{T2};font-size:11px;")
        hl.addWidget(self._list_title); hl.addWidget(self._list_count)
        v.addWidget(hdr)

        self._list_div = QFrame(); self._list_div.setFrameShape(QFrame.Shape.HLine)
        self._list_div.setStyleSheet(f"color:{DIV};background:{BG2};"); v.addWidget(self._list_div)

        self.note_list = QListWidget()
        self.note_list.setFrameShape(QFrame.Shape.NoFrame)
        self.note_list.setItemDelegate(NoteDelegate())
        self.note_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.note_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.note_list.verticalScrollBar().setSingleStep(8)
        self.note_list.currentItemChanged.connect(self._on_note_change)
        self.note_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.note_list.customContextMenuRequested.connect(self._on_note_context_menu)
        v.addWidget(self.note_list)

        bar = QWidget(); bar.setFixedHeight(40); bar.setObjectName("listBar")
        bl  = QHBoxLayout(bar); bl.setContentsMargins(10, 0, 10, 0); bl.setSpacing(6)
        self._new_btn = QPushButton("+  New Note")
        self._del_btn = QPushButton("Delete")
        for b in (self._new_btn, self._del_btn):
            b.setFixedHeight(28); b.setCursor(Qt.CursorShape.PointingHandCursor); bl.addWidget(b)
        self._new_btn.clicked.connect(self._new_note)
        self._del_btn.clicked.connect(self._delete_note)
        v.addWidget(bar)
        return w

    def _toggle_search(self):
        if self._search_bar.hasFocus():
            self._search_bar.clear()
            self._search_bar.clearFocus()
        else:
            self._search_bar.setFocus()
            self._search_bar.selectAll()

    def _toggle_ai_panel(self, checked):
        if checked:
            self._ai_panel.show()
            sizes = self._root_splitter.sizes()
            if sizes[3] < 60:
                sizes[2] = max(300, sizes[2] - 320)
                sizes[3] = 320
                self._root_splitter.setSizes(sizes)
            self._ai_panel._input.setFocus()
        else:
            self._ai_panel.hide()

    def _start_index_worker(self):
        notes = {p: i for p, i in self.current_notes.items()
                 if i.get("notebook") != PASSWORDS_NB}
        worker = _AIIndexWorker(self._ai_panel._index, notes, self)
        worker.progress.connect(self._ai_panel.notify_index_progress)
        worker.finished.connect(self._ai_panel.notify_index_done)
        worker.finished.connect(lambda _: worker.deleteLater())
        worker.start()

    def _toggle_panel(self, idx, default_w):
        sizes = list(self._root_splitter.sizes())
        if sizes[idx] > 0:
            setattr(self, f'_saved_panel_{idx}', sizes[idx])
            sizes[idx] = 0
        else:
            sizes[idx] = getattr(self, f'_saved_panel_{idx}', default_w)
        self._root_splitter.setSizes(sizes)

    def _toggle_sidebar_panel(self):
        self._toggle_panel(0, 200)

    def _toggle_list_panel(self):
        self._toggle_panel(1, 260)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()

    def _on_search_changed(self, text):
        self._search_query = text.strip()
        self._search_clear.setVisible(bool(text))
        self._rebuild_note_list()

    def _switch_notebook(self, nb_key):
        self.current_nb = nb_key
        self._flush_save()
        self._search_query = ''
        self._search_bar.blockSignals(True)
        self._search_bar.clear()
        self._search_bar.blockSignals(False)

        if nb_key == ALL_NB:
            self.current_notes = load_all()
        else:
            raw = load_notebook(nb_key)
            self.current_notes = {
                info["path"]: {**info, "title": title, "notebook": nb_key}
                for title, info in raw.items()
            }

        display_label = ALL_NB_LABEL if nb_key == ALL_NB else nb_key
        self._list_title.setText(display_label)
        count = len(self.current_notes)
        self._list_count.setText(f"{count} note{'s' if count != 1 else ''}")
        self._new_btn.setEnabled(nb_key != TRASH_NB)
        self._rebuild_note_list()

    def _on_note_context_menu(self, pos):
        item = self.note_list.itemAt(pos)
        if not item or item.data(R_HEADER):
            return
        path = item.data(R_PATH)
        if not path:
            return
        current_nb = self.current_notes[path]["notebook"]
        targets = [nb for nb in list_notebooks() if nb != current_nb and nb != TRASH_NB]

        menu = QMenu(self); menu.setStyleSheet(_MENU_SS)
        if path in self._pinned:
            menu.addAction("Unpin", lambda: self._unpin_note(path))
        else:
            menu.addAction("Pin to Top", lambda: self._pin_note(path))
        if targets:
            menu.addSeparator()
            move_menu = menu.addMenu("Move to")
            move_menu.setStyleSheet(_MENU_SS)
            for nb in targets:
                move_menu.addAction(nb, lambda _nb=nb: self._move_note(path, _nb))
        menu.addSeparator()
        menu.addAction("Delete", self._delete_note)
        menu.exec(self.note_list.mapToGlobal(pos))

    def _on_editor_context_menu(self, pos):
        menu = _make_styled_menu(self)

        # ── standard edit actions ──────────────────────────────────────────
        cur = self._editor.textCursor()
        cut_act  = menu.addAction("Cut",        self._editor.cut)
        copy_act = menu.addAction("Copy",       self._editor.copy)
        menu.addAction("Paste",                 self._editor.paste)
        menu.addSeparator()
        menu.addAction("Select All",            self._editor.selectAll)
        cut_act.setEnabled(cur.hasSelection())
        copy_act.setEnabled(cur.hasSelection())

        menu.exec(self._editor.mapToGlobal(pos))

    def _maybe_autocorrect(self):
        if _autocorrect is None:
            return
        cur = self._editor.textCursor()
        cur.select(QTextCursor.SelectionType.WordUnderCursor)
        word = re.sub(r'[^A-Za-z]', '', cur.selectedText())
        if len(word) < 3:
            return
        fix = _autocorrect(word)
        if not fix or fix == word:
            return
        if word.isupper():
            fix = fix.upper()
        elif word[0].isupper():
            fix = fix.capitalize()
        cur.insertText(fix)
        self._editor.setTextCursor(cur)

    def _pin_note(self, path):
        self._pinned.add(path)
        _CFG["pinned"] = list(self._pinned); _save_config(_CFG)
        self._rebuild_note_list()

    def _unpin_note(self, path):
        self._pinned.discard(path)
        _CFG["pinned"] = list(self._pinned); _save_config(_CFG)
        self._rebuild_note_list()

    def _move_note(self, path, dest_nb):
        self._flush_save()
        try:
            new_path = move_note(path, dest_nb)
        except FileExistsError as e:
            QMessageBox.warning(self, "Move Failed", str(e))
            return
        info = self.current_notes.pop(path)
        info["path"]     = new_path
        info["notebook"] = dest_nb
        if self.sel_path == path:
            self.sel_path = new_path
            self.current_notes[new_path] = info
            self._rebuild_note_list()
            self._open_note(new_path)
        else:
            self.current_notes[new_path] = info
            self._rebuild_note_list()
        self._load_sidebar()

    def _rebuild_note_list(self):
        self.note_list.blockSignals(True)
        self.note_list.clear()

        q = getattr(self, '_search_query', '').lower()
        all_paths = sorted(self.current_notes,
                           key=lambda p: self.current_notes[p]["modified"],
                           reverse=True)
        if q:
            sorted_paths = [p for p in all_paths
                            if q in self.current_notes[p].get("title", "").lower()
                            or q in self.current_notes[p].get("body", "")]
        else:
            sorted_paths = all_paths
        pinned_paths   = [p for p in sorted_paths if p in self._pinned]
        unpinned_paths = [p for p in sorted_paths if p not in self._pinned]

        def _add_note_item(path, pinned=False):
            info = self.current_notes[path]
            it = QListWidgetItem(info["title"])
            it.setData(R_PATH,     path)
            it.setData(R_NOTEBOOK, info["notebook"])
            it.setData(R_PREVIEW,  info["preview"])
            it.setData(R_DATE,     fmt_date(info["modified"]))
            it.setData(R_HEADER,   False)
            it.setData(R_PINNED,   pinned)
            self.note_list.addItem(it)

        if pinned_paths:
            hdr = QListWidgetItem("Pinned")
            hdr.setData(R_HEADER, True); hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            self.note_list.addItem(hdr)
            for path in pinned_paths:
                _add_note_item(path, pinned=True)

        last_bucket = -1
        for path in unpinned_paths:
            info = self.current_notes[path]
            b    = date_bucket(info["modified"])
            if b != last_bucket:
                last_bucket = b
                hdr = QListWidgetItem(BUCKET_LABELS[b])
                hdr.setData(R_HEADER, True); hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                self.note_list.addItem(hdr)
            _add_note_item(path)

        if self.sel_path and self.sel_path in self.current_notes:
            self._sync_note_selection()
        elif sorted_paths:
            self.note_list.blockSignals(False)
            self._open_note(sorted_paths[0])
            return

        self.note_list.blockSignals(False)

    def _promote_to_top(self):
        """Move the current note to the top of the list in-place (no clear/rebuild)."""
        if not self.sel_path or self.sel_path not in self.current_notes:
            return
        info = self.current_notes[self.sel_path]

        # Find the item's current row
        src_row = -1
        for i in range(self.note_list.count()):
            it = self.note_list.item(i)
            if it and not it.data(R_HEADER) and it.data(R_PATH) == self.sel_path:
                src_row = i
                break
        if src_row == -1:
            self._rebuild_note_list()
            return

        self.note_list.blockSignals(True)

        # Refresh preview and date on the item
        self.note_list.item(src_row).setData(R_PREVIEW, info["preview"])
        self.note_list.item(src_row).setData(R_DATE, fmt_date(info["modified"]))

        # Ensure a "Today" header is at row 0
        first = self.note_list.item(0)
        if first and first.data(R_HEADER) and first.text() == BUCKET_LABELS[0]:
            dst_row = 1
        else:
            hdr = QListWidgetItem(BUCKET_LABELS[0])
            hdr.setData(R_HEADER, True)
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            self.note_list.insertItem(0, hdr)
            src_row += 1  # shifted by the insert above
            dst_row = 1

        # Already at the right spot — nothing to move
        if src_row != dst_row:
            item = self.note_list.takeItem(src_row)
            self.note_list.insertItem(dst_row, item)

            # Remove any headers that are now empty (no note follows before the next header)
            i = 0
            while i < self.note_list.count():
                it = self.note_list.item(i)
                if it and it.data(R_HEADER):
                    nxt = self.note_list.item(i + 1)
                    if not nxt or nxt.data(R_HEADER):
                        self.note_list.takeItem(i)
                        continue
                i += 1

        self.note_list.setCurrentItem(self.note_list.item(dst_row))
        self.note_list.blockSignals(False)

        # Smooth scroll to top so the promoted note glides into view
        sb = self.note_list.verticalScrollBar()
        if sb.value() > 0:
            if hasattr(self, '_scroll_anim') and self._scroll_anim.state() == QPropertyAnimation.State.Running:
                self._scroll_anim.stop()
            self._scroll_anim = QPropertyAnimation(sb, b"value", self)
            self._scroll_anim.setDuration(300)
            self._scroll_anim.setStartValue(sb.value())
            self._scroll_anim.setEndValue(0)
            self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._scroll_anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _sync_note_selection(self):
        for i in range(self.note_list.count()):
            it = self.note_list.item(i)
            if it and not it.data(R_HEADER) and it.data(R_PATH) == self.sel_path:
                self.note_list.blockSignals(True)
                self.note_list.setCurrentItem(it)
                self.note_list.blockSignals(False)
                return

    def _on_note_change(self, current, _prev):
        if current and not current.data(R_HEADER):
            path = current.data(R_PATH)
            if path and path != self.sel_path:  # noqa: S1066
                self._flush_save()
                self._open_note(path)

    def eventFilter(self, obj, event):
        if obj is self._title_edit:
            if event.type() == QEvent.Type.FocusIn:
                if not self._is_pw_note:
                    self._toolbar.setEnabled(False)
            elif event.type() == QEvent.Type.FocusOut:
                if not self._is_pw_note:
                    self._toolbar.setEnabled(True)
        elif obj is self._editor and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter,
                               Qt.Key.Key_Period, Qt.Key.Key_Comma,
                               Qt.Key.Key_Exclam, Qt.Key.Key_Question):
                self._maybe_autocorrect()
            if event.key() == Qt.Key.Key_Tab:
                cur = self._editor.textCursor()
                table = cur.currentTable()
                if table is not None:
                    cell = table.cellAt(cur)
                    r, c = cell.row(), cell.column()
                    ncols, nrows = table.columns(), table.rows()
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        if c > 0:
                            self._editor.setTextCursor(table.cellAt(r, c - 1).firstCursorPosition())
                        elif r > 0:
                            self._editor.setTextCursor(table.cellAt(r - 1, ncols - 1).firstCursorPosition())
                    else:
                        if c < ncols - 1:
                            self._editor.setTextCursor(table.cellAt(r, c + 1).firstCursorPosition())
                        elif r < nrows - 1:
                            self._editor.setTextCursor(table.cellAt(r + 1, 0).firstCursorPosition())
                        else:
                            table.appendRows(1)
                            nr = table.rows()
                            for cc in range(ncols):
                                table.cellAt(nr - 1, cc).setFormat(_table_cell_fmt(False))
                            self._editor.setTextCursor(table.cellAt(nr - 1, 0).firstCursorPosition())
                    return True
            if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                cur = self._editor.textCursor()
                if not cur.hasSelection() and cur.currentTable() is None:
                    doc = self._editor.document()
                    probe = QTextCursor(doc)
                    probe.setPosition(cur.position())
                    if event.key() == Qt.Key.Key_Backspace:
                        probe.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
                    else:
                        probe.movePosition(QTextCursor.MoveOperation.NextCharacter)
                    table = probe.currentTable()
                    if table is not None:
                        del_cur = QTextCursor(doc)
                        del_cur.setPosition(table.firstPosition() - 1)
                        del_cur.setPosition(table.lastPosition() + 1,
                                            QTextCursor.MoveMode.KeepAnchor)
                        del_cur.removeSelectedText()
                        self._editor.setTextCursor(del_cur)
                        return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                cursor = self._editor.textCursor()
                block  = cursor.block()
                if self._block_is_checklist(block):
                    if len(block.text()) <= 1:
                        # empty checklist line → remove marker and exit list
                        c = QTextCursor(block)
                        c.setBlockFormat(QTextBlockFormat())
                        c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                        c.movePosition(QTextCursor.MoveOperation.Right,
                                       QTextCursor.MoveMode.KeepAnchor, 1)
                        c.removeSelectedText()
                        self._editor.setTextCursor(c)
                    else:
                        cursor.insertText('\n')
                        self._add_checklist(self._editor.textCursor().block())
                    return True
        elif obj is self._editor.viewport() and event.type() in (
                QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            tt  = self._tbl_toolbar
            pos = event.position().toPoint()
            if event.type() == QEvent.Type.MouseButtonRelease:
                if tt._drag is not None:
                    tt.end_drag()
                    return True
            elif event.type() == QEvent.Type.MouseMove:
                if tt._drag is not None:
                    tt.update_drag(pos.x())
                    return True
                cursor  = self._editor.cursorForPosition(pos)
                blk_col = cursor.position() - cursor.block().position()
                block   = cursor.block()
                on_circle = blk_col <= 1 and self._block_is_checklist(block)
                divider   = tt.divider_at(pos)
                if on_circle:
                    self._editor.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                elif divider is not None:
                    self._editor.viewport().setCursor(Qt.CursorShape.SplitHCursor)
                else:
                    self._editor.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                if event.button() == Qt.MouseButton.LeftButton and self.sel_path:
                    cursor = self._editor.cursorForPosition(pos)
                    cf = cursor.charFormat()
                    att_path = cf.property(_ATT_PATH_PROP)
                    if att_path:
                        abs_path = os.path.join(os.path.dirname(self.sel_path), att_path)
                        QDesktopServices.openUrl(QUrl.fromLocalFile(abs_path))
                        return True
            elif event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    divider = tt.divider_at(pos)
                    if divider is not None:
                        tt.start_drag(divider[0], divider[1], pos.x())
                        return True
                    cursor  = self._editor.cursorForPosition(pos)
                    blk_col = cursor.position() - cursor.block().position()
                    block   = cursor.block()
                    if blk_col <= 1 and self._block_is_checklist(block):
                        self._toggle_check_state(block)
                        return True
        return super().eventFilter(obj, event)

    # ── Editor ────────────────────────────────────────────────────────────────
    def _build_editor_panel(self):
        w = QWidget(); w.setObjectName("editorPanel")
        self._editor_panel = w
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        self._toolbar = QWidget(); tb = self._toolbar
        tb.setFixedHeight(44); tb.setObjectName("toolbar")
        tb.setStyleSheet(self._toolbar_ss())
        tl = QHBoxLayout(tb); tl.setContentsMargins(14, 0, 14, 0); tl.setSpacing(2)

        _tbtn_ss = (
            f"QPushButton{{background:transparent;color:{T1};border:none;"
            f"border-radius:7px;padding:0;}}"
            f"QPushButton:hover{{background:{SEL};}}"
            f"QPushButton:pressed{{background:{DIV};}}"
        )

        self._btn_sidebar  = _ToolbarIconButton("sidebar")
        self._btn_notelist = _ToolbarIconButton("notelist")
        self._btn_sidebar.clicked.connect(self._toggle_sidebar_panel)
        self._btn_notelist.clicked.connect(self._toggle_list_panel)

        self._btn_format = QPushButton("Aa")
        self._btn_format.setFont(QFont(_FONT_BODY, 14, QFont.Weight.Medium))
        self._btn_format.setStyleSheet(
            _tbtn_ss +
            f"QPushButton:disabled{{color:{T3};background:transparent;}}"
        )

        self._btn_checklist = _ToolbarIconButton("checklist")
        self._btn_table     = _ToolbarIconButton("table")
        self._btn_attach    = _ToolbarIconButton("attach")

        self._btn_checklist.clicked.connect(self._toggle_checklist)
        self._btn_table.clicked.connect(self._insert_table)
        self._btn_attach.clicked.connect(self._attach_action)

        for b in (self._btn_sidebar, self._btn_notelist, self._btn_format,
                  self._btn_checklist, self._btn_table, self._btn_attach):
            b.setFixedSize(40, 32)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            tl.addWidget(b)

        tl.addStretch()

        # ── Search bar (right side of toolbar) ──────────────────────────────
        search_pill = QWidget(); search_pill.setObjectName("searchPill")
        search_pill.setFixedSize(210, 28)
        search_pill.setStyleSheet(
            f"QWidget#searchPill{{"
            f"background:{SEL};border-radius:9px;"
            f"border:1px solid {DIV};}}"
        )
        sp = QHBoxLayout(search_pill); sp.setContentsMargins(8, 0, 6, 0); sp.setSpacing(5)

        self._search_icon = QLabel("⌕")
        self._search_icon.setStyleSheet(f"color:{T2};font-size:26px;background:transparent;")
        self._search_icon.setFixedWidth(14)

        self._search_bar = QLineEdit()
        self._search_bar.setFrame(False)
        self._search_bar.setPlaceholderText("Search")
        self._search_bar.setStyleSheet(
            f"QLineEdit{{background:transparent;border:none;color:{T1};"
            f"font-size:13px;font-family:'{_FONT_BODY}';"
            f"selection-background-color:{ACC};}}"
        )

        self._search_clear = QPushButton("×")
        self._search_clear.setFixedSize(15, 15)
        self._search_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_clear.setStyleSheet(
            f"QPushButton{{background:#636366;color:{BG0};border-radius:7px;"
            f"font-size:11px;font-weight:700;padding:0;}}"
            f"QPushButton:hover{{background:{T2};}}"
        )
        self._search_clear.hide()
        self._search_clear.clicked.connect(self._search_bar.clear)

        sp.addWidget(self._search_icon)
        sp.addWidget(self._search_bar)
        sp.addWidget(self._search_clear)

        self._search_bar.textChanged.connect(self._on_search_changed)
        tl.addWidget(search_pill)

        self._btn_ai = QPushButton("✦")
        self._btn_ai.setFixedSize(32, 32)
        self._btn_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_ai.setCheckable(True)
        self._btn_ai.setToolTip("Ask AI")
        self._btn_ai.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;border-radius:9px;"
            f"color:{T2};font-size:32px;margin-left:6px;}}"
            f"QPushButton:hover{{background:{SEL};color:{T1};}}"
            f"QPushButton:checked{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 #5e5ce6,stop:1 {ACC});color:#ffffff;border-radius:9px;}}"
        )
        self._btn_ai.clicked.connect(self._toggle_ai_panel)
        tl.addWidget(self._btn_ai)

        v.addWidget(tb)

        self._date_lbl = QLabel()
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._date_lbl.setStyleSheet(f"background:transparent;color:{'#666666' if _ACTIVE_THEME == 'notes07' else T2};font-size:11px;padding:10px 0 4px;")
        v.addWidget(self._date_lbl)

        title_row = QWidget(); title_row.setStyleSheet("background:transparent;")
        trl = QHBoxLayout(title_row)
        trl.setContentsMargins(36, 0, 36, 0); trl.setSpacing(3)
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Title")
        self._title_edit.setStyleSheet(
            f"QLineEdit{{background:transparent;border:none;color:{'#1a1a1a' if _ACTIVE_THEME == 'notes07' else T1};"
            f"font-family:{'Noteworthy' if _ACTIVE_THEME == 'notes07' else _FONT_BODY};"
            f"font-size:22px;font-weight:700;padding:2px 0 6px;}}"
        )
        self._title_edit.editingFinished.connect(self._on_title_changed)
        self._title_edit.installEventFilter(self)
        trl.addWidget(self._title_edit, 1)
        v.addWidget(title_row)

        self._editor = NoteEditor()
        self._editor.note_link_clicked.connect(
            lambda ref: self._open_note(
                ref if os.path.isabs(ref) else os.path.normpath(os.path.join(ROOT, ref))
            )
        )
        self._editor.setFrameShape(QFrame.Shape.NoFrame)
        self._editor.setFont(QFont(_FONT_BODY, 14))
        self._editor.setStyleSheet(
            f"QTextEdit{{background:{BG0};color:{'#1a1a1a' if _ACTIVE_THEME == 'notes07' else T1};border:none;padding:0 32px 32px;}}"
        )
        _opt = QTextOption(Qt.AlignmentFlag.AlignLeft)
        _opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        self._editor.document().setDefaultTextOption(_opt)
        self._tbl_toolbar = _TableToolbar(self._editor)
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.cursorPositionChanged.connect(self._fix_checklist_cursor)
        self._editor.cursorPositionChanged.connect(self._tbl_toolbar.refresh)
        self._editor.installEventFilter(self)
        self._editor.viewport().installEventFilter(self)
        self._editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._editor.customContextMenuRequested.connect(self._on_editor_context_menu)
        v.addWidget(self._editor)

        self._pw_view = PasswordNoteView(w)
        self._pw_view.hide()
        v.addWidget(self._pw_view)

        self._budget_view = BudgetNoteView(w)
        self._budget_view.hide()
        v.addWidget(self._budget_view)

        self._format_popup = FormatPopup(self._editor, self)
        self._btn_format.clicked.connect(
            lambda: self._format_popup.show_below(self._btn_format))

        QShortcut(QKeySequence("Ctrl+B"), self).activated.connect(
            lambda: self._format_popup._wrap('bold'))
        QShortcut(QKeySequence("Ctrl+I"), self).activated.connect(
            lambda: self._format_popup._wrap('italic'))
        QShortcut(QKeySequence("Ctrl+U"), self).activated.connect(
            lambda: self._format_popup._wrap('underline'))
        QShortcut(QKeySequence("Ctrl+K"), self).activated.connect(
            self._format_popup._insert_link)
        QShortcut(QKeySequence("Ctrl+Alt+Shift+V"), self).activated.connect(
            self._paste_match_style)
        self._editor.selectionChanged.connect(self._constrain_table_selection)

        return w

    def _toggle_checklist(self):
        cur = self._editor.textCursor()
        doc = self._editor.document()
        start = min(cur.anchor(), cur.position())
        end   = max(cur.anchor(), cur.position())
        s_bn  = doc.findBlock(start).blockNumber()
        e_bn  = doc.findBlock(end).blockNumber()
        if end > start and doc.findBlock(end).position() == end:
            e_bn = max(s_bn, e_bn - 1)
        blocks = [doc.findBlockByNumber(n) for n in range(s_bn, e_bn + 1)]
        all_checked = all(self._block_is_checklist(b) for b in blocks)
        cur.beginEditBlock()
        for b in blocks:
            if all_checked:
                self._remove_checklist(b)
            elif not self._block_is_checklist(b):
                self._add_checklist(b)
        cur.endEditBlock()

    def _insert_table(self):
        if not self.sel_path:
            return
        cur = self._editor.textCursor()
        fmt = QTextTableFormat()
        fmt.setCellPadding(6)
        fmt.setCellSpacing(0)
        fmt.setBorder(0)
        ncols = 2
        fmt.setColumnWidthConstraints([
            QTextLength(QTextLength.Type.PercentageLength, 50.0),
            QTextLength(QTextLength.Type.PercentageLength, 50.0),
        ])
        table = cur.insertTable(3, ncols, fmt)
        for r in range(3):
            for c in range(ncols):
                table.cellAt(r, c).setFormat(_table_cell_fmt(r == 0))
        self._editor.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
        self._editor.setFocus()

    def _attach_action(self):
        if not self.sel_path:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach Files")
        if not paths:
            return
        cur = self._editor.textCursor()
        for src in paths:
            rel_path, display_name = _copy_att(self.sel_path, src)
            # Insert on its own block
            if not cur.atBlockStart() or cur.block().text():
                cur.insertBlock(); cur.setBlockFormat(QTextBlockFormat())
            _render_attachment(cur, os.path.dirname(self.sel_path),
                               rel_path, display_name)
            cur.insertBlock(); cur.setBlockFormat(QTextBlockFormat())
        self._editor.setTextCursor(cur)
        self._editor.setFocus()

    def _block_is_checklist(self, block):
        if not block.isValid(): return False
        it = block.begin()
        if it.atEnd(): return False
        return _is_circle_fmt(it.fragment().charFormat())

    def _add_checklist(self, block):
        c = QTextCursor(block)
        c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        blk = QTextBlockFormat(); blk.setLeftMargin(_CL_LEFT_MARGIN); blk.setBottomMargin(4)
        c.setBlockFormat(blk)
        c.insertText('​', _make_circle_fmt(False))

    def _toggle_check_state(self, block):
        if not block.isValid(): return
        it = block.begin()
        if it.atEnd(): return
        fmt = it.fragment().charFormat()
        if not _is_circle_fmt(fmt): return
        checked = not bool(fmt.property(_CL_CHECKED_KEY))
        c = QTextCursor(block)
        c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        c.movePosition(QTextCursor.MoveOperation.Right,
                       QTextCursor.MoveMode.KeepAnchor, 1)
        c.setCharFormat(_make_circle_fmt(checked))

    def _fix_checklist_cursor(self):
        cur = self._editor.textCursor()
        if cur.hasSelection(): return
        block = cur.block()
        if not self._block_is_checklist(block): return
        if cur.position() - block.position() < 1: return
        if _is_circle_fmt(cur.charFormat()):
            body = QTextCharFormat()
            body.setFontFamilies([_FONT_BODY])
            body.setFontPointSize(14.0)
            body.setFontWeight(QFont.Weight.Normal)
            body.setForeground(QColor(T1))
            self._editor.setCurrentCharFormat(body)

    def _remove_checklist(self, block):
        if not block.isValid(): return
        if not self._block_is_checklist(block): return
        c = QTextCursor(block)
        c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        c.setBlockFormat(QTextBlockFormat())
        c.movePosition(QTextCursor.MoveOperation.Right,
                       QTextCursor.MoveMode.KeepAnchor, 1)
        c.removeSelectedText()

    def _paste_match_style(self):
        text = QApplication.clipboard().text()
        if text and self._editor.hasFocus():
            self._editor.insertPlainText(text)

    def _constrain_table_selection(self):
        if getattr(self, '_constraining_sel', False):
            return
        cur = self._editor.textCursor()
        if not cur.hasSelection():
            return
        doc = self._editor.document()
        anchor_cur = QTextCursor(doc)
        anchor_cur.setPosition(cur.anchor())
        table = anchor_cur.currentTable()
        if table is None:
            return
        cell = table.cellAt(anchor_cur)
        if not cell.isValid():
            return
        cell_start = cell.firstCursorPosition().position()
        cell_end   = cell.lastCursorPosition().position()
        pos = cur.position()
        if cell_start <= pos <= cell_end:
            return
        new_cur = QTextCursor(doc)
        new_cur.setPosition(cur.anchor())
        new_cur.setPosition(max(cell_start, min(pos, cell_end)),
                            QTextCursor.MoveMode.KeepAnchor)
        self._constraining_sel = True
        self._editor.setTextCursor(new_cur)
        self._constraining_sel = False

    def _open_note(self, path):
        if self._is_pw_note:
            self._pw_view.lock(focus=False)
        if self._is_bdg_note:
            self._budget_view._save_timer.stop()
            self._budget_view._save_now()

        self.sel_path = path
        info = self.current_notes.get(path, {})

        raw = ""
        try:    raw = read_note(path)
        except Exception: pass

        title = info.get("title") or next(
            (l[2:].strip() for l in raw.split("\n") if l.startswith("# ")), "") or ""
        self._title_edit.blockSignals(True)
        self._title_edit.setText(title)
        self._title_edit.setCursorPosition(0)
        self._title_edit.blockSignals(False)

        if _is_password_note(raw):
            self._set_special_note_panel(True)
            self._is_pw_note  = True
            self._is_bdg_note = False
            self._editor.hide(); self._date_lbl.hide()
            self._budget_view.hide()
            self._btn_format.setEnabled(False)
            self._btn_checklist.setEnabled(False)
            self._btn_table.setEnabled(False)
            self._btn_attach.setEnabled(True)
            self._btn_attach.clicked.disconnect()
            self._btn_attach.clicked.connect(self._pw_view._import_csv)
            self._pw_view.show()
            self._pw_view.load(path, _body(raw))
        elif _is_budget_note(raw):
            self._set_special_note_panel(True)
            self._is_pw_note  = False
            self._is_bdg_note = True
            self._editor.hide(); self._date_lbl.hide()
            self._pw_view.hide()
            self._btn_format.setEnabled(False)
            self._btn_checklist.setEnabled(False)
            self._btn_table.setEnabled(False)
            self._btn_attach.setEnabled(False)
            self._budget_view.show()
            self._budget_view.load(path, _body(raw))
        else:
            self._set_special_note_panel(False)
            self._is_pw_note  = False
            self._is_bdg_note = False
            self._pw_view.hide(); self._budget_view.hide()
            self._editor.show(); self._date_lbl.show()
            self._btn_format.setEnabled(True)
            self._btn_checklist.setEnabled(True)
            self._btn_table.setEnabled(True)
            self._btn_attach.clicked.disconnect()
            self._btn_attach.clicked.connect(self._attach_action)
            try:
                _apply_markdown(self._editor, _body(raw),
                                note_dir=os.path.dirname(path))
            except Exception:
                self._editor.blockSignals(True)
                self._editor.setPlainText("")
                self._editor.blockSignals(False)
            cur = self._editor.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            self._editor.setTextCursor(cur)
            try:
                dt = datetime.fromisoformat(info["modified"])
                self._date_lbl.setText(dt.strftime(f"%B {dt.day}, %Y at %H:%M"))
            except Exception:
                self._date_lbl.clear()

        self._dirty = False
        self._sync_note_selection()

    def _on_text_changed(self):
        self._dirty = True
        self._save_timer.start(600)

    def _flush_save(self):
        if self._is_pw_note or self._is_bdg_note:
            return
        if not self._dirty or not self.sel_path:
            return
        if not os.path.exists(os.path.dirname(self.sel_path)):
            return
        body = _to_markdown(self._editor)
        title = self.current_notes.get(self.sel_path, {}).get("title", "")
        content = f"# {title}\n\n{body}" if title else body
        write_note(self.sel_path, content)
        self._dirty = False
        if self.sel_path in self.current_notes:
            raw = next((l.strip() for l in body.split("\n") if l.strip()), "")
            preview = re.sub(r'^#{1,6}\s|[*_~`]', '', raw)[:140]
            self.current_notes[self.sel_path]["preview"] = preview
            self.current_notes[self.sel_path]["body"] = content.lower()
            self.current_notes[self.sel_path]["modified"] = datetime.now().isoformat(timespec="seconds")
            self._promote_to_top()
            nb = self.current_notes[self.sel_path].get("notebook", "")
            self._ai_panel.reindex_note(self.sel_path, title, nb, body)

    def _on_title_changed(self):
        new = self._title_edit.text().strip()
        if not self.sel_path or not new:
            return
        old_title = self.current_notes.get(self.sel_path, {}).get("title", "")
        if new == old_title:
            return
        try:
            new_path = rename_note(self.sel_path, new)
        except FileExistsError as e:
            QMessageBox.warning(self, "Exists", str(e))
            self._title_edit.setText(old_title)
            return
        # update state
        info = self.current_notes.pop(self.sel_path)
        info["title"] = new
        info["path"]  = new_path
        self.current_notes[new_path] = info
        self.sel_path = new_path
        self._rebuild_note_list()
        self._load_sidebar()

    # ── New / Delete ──────────────────────────────────────────────────────────
    def _show_new_folder_menu(self):
        menu = QMenu(self); menu.setStyleSheet(_MENU_SS)
        menu.addAction("New Folder",        self._new_notebook)
        menu.addSeparator()
        pw_action = menu.addAction("Password Manager", self._new_password_note)
        bdg_action = menu.addAction("Budget Tracker",  self._new_budget_note)
        # grey out if already exists
        if os.path.isdir(nb_path(PASSWORDS_NB)):
            pw_action.setEnabled(False)
        if os.path.isdir(nb_path(BUDGET_NB)):
            bdg_action.setEnabled(False)
        btn_widget = self.sender()
        pos = btn_widget.mapToGlobal(btn_widget.rect().topLeft())
        pos.setY(pos.y() - menu.sizeHint().height())
        menu.exec(pos)

    def _new_password_note(self):
        self._flush_save()
        nb = PASSWORDS_NB
        os.makedirs(nb_path(nb), exist_ok=True)
        title = "Passwords"
        fp = note_path(nb, title)
        if not os.path.exists(fp):
            create_note(nb, title)
        body    = f"{_PW_MARKER}\n{_rows_to_gfm([['Website','Username','Password'],['','','']])}"
        content = f"# {title}\n\n{body}"
        write_note(fp, content)
        info = {
            "path":     fp,
            "title":    title,
            "notebook": nb,
            "modified": datetime.now().isoformat(timespec="seconds"),
            "preview":  "Website · Username · Password",
            "body":     content.lower(),
        }
        self.current_notes[fp] = info
        self._switch_notebook(nb)
        self._load_sidebar()
        self._open_note(fp)

    def _new_budget_note(self):
        self._flush_save()
        nb = "Budget"
        os.makedirs(nb_path(nb), exist_ok=True)
        base, n, title = "Budget", 2, "Budget"
        while os.path.exists(note_path(nb, title)):
            title = f"{base} {n}"; n += 1
        fp = create_note(nb, title)
        body    = _budget_to_str(0.0, 0.0, [['', '']])
        content = f"# {title}\n\n{body}"
        write_note(fp, content)
        info = {
            "path":     fp,
            "title":    title,
            "notebook": nb,
            "modified": datetime.now().isoformat(timespec="seconds"),
            "preview":  "Budget Tracker",
            "body":     content.lower(),
        }
        self.current_notes[fp] = info
        self._switch_notebook(nb)
        self._load_sidebar()
        self._open_note(fp)
        self._title_edit.selectAll()
        self._title_edit.setFocus()

    def _new_note(self):
        self._flush_save()
        nb = self.current_nb if self.current_nb != ALL_NB else "Notes"
        os.makedirs(nb_path(nb), exist_ok=True)
        base, n, title = "New Note", 2, "New Note"
        while os.path.exists(note_path(nb, title)):
            title = f"{base} {n}"; n += 1
        fp = create_note(nb, title)
        info = {
            "path":     fp,
            "title":    title,
            "notebook": nb,
            "modified": datetime.now().isoformat(timespec="seconds"),
            "preview":  "",
            "body":     "",
        }
        self.current_notes[fp] = info
        self._rebuild_note_list()
        self._open_note(fp)
        self._load_sidebar()
        self._title_edit.selectAll()
        self._title_edit.setFocus()

    def _delete_note(self):
        focused = QApplication.focusWidget()
        if focused is self._editor:
            cur = self._editor.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.StartOfLine, QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            self._editor.setTextCursor(cur)
            return
        if focused is self._title_edit:
            self._title_edit.home(True)
            self._title_edit.backspace()
            return
        if focused is self._search_bar:
            self._search_bar.home(True)
            self._search_bar.backspace()
            return
        if self._is_pw_note and isinstance(focused, QLineEdit):
            focused.home(True)
            focused.backspace()
            return
        if self._is_bdg_note and isinstance(focused, QLineEdit):
            focused.clear()
            return
        if not self.sel_path:
            return
        title = self.current_notes.get(self.sel_path, {}).get("title", "this note")
        dlg = ConfirmDialog(self, title=f'Delete "{title}"?',
                            body="This note will be moved to Recently Deleted.")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        trash_note(self.sel_path)
        self.current_notes.pop(self.sel_path, None)
        self.sel_path = None
        self._is_pw_note  = False
        self._is_bdg_note = False
        self._budget_view._save_timer.stop()
        self._budget_view._path = None
        self._budget_view.hide()
        self._title_edit.clear()
        self._editor.clear()
        self._date_lbl.clear()
        self._rebuild_note_list()
        self._load_sidebar()

    def _show_sidebar_menu(self):
        menu = _make_styled_menu(self)
        for label, key in (("Notes '07", "notes07"),
                           ("Notes '24", "notes24"),
                           ("Notes '26", "notes26")):
            action = menu.addAction(label, lambda k=key: self._switch_theme(k))
            if _ACTIVE_THEME == key:
                action.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Change Notes Folder…", self._change_notes_folder)
        menu.exec(self._folder_btn.mapToGlobal(
            self._folder_btn.rect().bottomLeft()))

    def _switch_theme(self, theme_name):
        global _ACTIVE_THEME, _FONT_EDITOR
        _ACTIVE_THEME = theme_name
        _FONT_EDITOR = "Noteworthy" if _ACTIVE_THEME == "notes07" else _FONT_BODY
        _CFG["theme"] = theme_name
        _save_config(_CFG)
        _load_theme_globals()
        apply_dark_theme(QApplication.instance())
        self._refresh_inline_stylesheets()
        if self.sel_path and not self._is_pw_note and not self._is_bdg_note:
            self._open_note(self.sel_path)

    def _set_special_note_panel(self, special: bool):
        if special and _ACTIVE_THEME == "notes07":
            _t = _THEMES["notes24"]
            self._editor_panel.setStyleSheet(f"QWidget#editorPanel{{background:{_t['BG0']};}}")
            self._title_edit.setStyleSheet(
                f"QLineEdit{{background:transparent;border:none;color:{_t['T1']};"
                f"font-family:{_FONT_BODY};font-size:22px;font-weight:700;padding:2px 0 6px;}}")
            self._toolbar.setStyleSheet(
                f"QWidget{{background:{_t['BG0']};border-bottom:1px solid {_t['DIV']};}}")
        else:
            self._editor_panel.setStyleSheet("")
            self._toolbar.setStyleSheet(self._toolbar_ss())
            self._title_edit.setStyleSheet(
                f"QLineEdit{{background:transparent;border:none;"
                f"color:{'#1a1a1a' if _ACTIVE_THEME == 'notes07' else T1};"
                f"font-family:{'Noteworthy' if _ACTIVE_THEME == 'notes07' else _FONT_BODY};"
                f"font-size:22px;font-weight:700;padding:2px 0 6px;}}")

    def _toolbar_ss(self):
        if _ACTIVE_THEME == "notes07":
            return (
                "QWidget{background:qlineargradient("
                "x1:0,y1:0,x2:0,y2:1,"
                "stop:0 #5C443B,stop:1 #A07562);"
                "border-bottom:1px solid #3e2e28;}"
            )
        return f"QWidget{{background:{BG0};border-bottom:1px solid {DIV};}}"

    def _refresh_inline_stylesheets(self):
        self._toolbar.setStyleSheet(self._toolbar_ss())
        self._editor.setStyleSheet(
            f"QTextEdit{{background:{BG0};color:{'#1a1a1a' if _ACTIVE_THEME == 'notes07' else T1};border:none;padding:0 32px 32px;}}")
        self._title_edit.setStyleSheet(
            f"QLineEdit{{background:transparent;border:none;color:{'#1a1a1a' if _ACTIVE_THEME == 'notes07' else T1};"
            f"font-family:{'Noteworthy' if _ACTIVE_THEME == 'notes07' else _FONT_BODY};"
            f"font-size:22px;font-weight:700;padding:2px 0 6px;}}")
        self._date_lbl.setStyleSheet(
            f"background:transparent;color:{'#666666' if _ACTIVE_THEME == 'notes07' else T2};font-size:11px;padding:10px 0 4px;")
        self._search_bar.setStyleSheet(
            f"QLineEdit{{background:transparent;border:none;color:{T1};"
            f"font-size:13px;font-family:'{_FONT_BODY}';"
            f"selection-background-color:{ACC};}}")
        self._format_popup.setStyleSheet(_POPUP_STYLE)
        for btn in self._format_popup._fmt_btns.values():
            btn.setStyleSheet(_fmt_btn_ss(False))
        for chk in self._format_popup._style_checks.values():
            chk.setStyleSheet(f"color:{ACC};font-size:14px;background:transparent;")
        self._list_div.setStyleSheet(f"color:{DIV};background:{BG2};")
        self._sidebar_bar.setStyleSheet(f"background:{BG1};")
        self._new_folder_btn.setStyleSheet(
            f"QPushButton{{background:{BG2};color:{T1};border:1px solid {DIV};"
            f"border-radius:6px;font-size:12px;padding:4px 8px;}}"
            f"QPushButton:hover{{background:{SEL};color:{T1};}}"
        )
        self.sidebar_list.update()
        self.note_list.update()

    def _change_notes_folder(self):
        global ROOT
        new_root = QFileDialog.getExistingDirectory(
            self, "Choose Notes Folder", ROOT,
            QFileDialog.Option.ShowDirsOnly
        )
        if not new_root or new_root == ROOT:
            return
        ROOT = new_root
        _save_config({"root": ROOT})
        self._flush_save()
        self.current_notes = load_all()
        self.sel_path = None
        self._title_edit.clear()
        self._editor.clear()
        self._date_lbl.clear()
        self._load_sidebar()
        self._rebuild_note_list()

    def _new_notebook(self):
        dlg = FolderDialog(self, title="New Folder")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.value()
        if not name:
            return
        if os.path.exists(nb_path(name)):
            QMessageBox.warning(self, "Exists", f'"{name}" already exists.')
            return
        create_notebook(name)
        self._load_sidebar()
        self._switch_notebook(name)

    def closeEvent(self, event):
        self._flush_save()
        event.accept()


# ── Dark theme ────────────────────────────────────────────────────────────────
def apply_dark_theme(app: QApplication):
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(BG0))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(T1))
    pal.setColor(QPalette.ColorRole.Base,            QColor(BG0))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(BG2))
    pal.setColor(QPalette.ColorRole.Text,            QColor(T1))
    pal.setColor(QPalette.ColorRole.Button,          QColor(BG2))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(T1))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(ACC))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(T2))
    pal.setColor(QPalette.ColorRole.Mid,             QColor(DIV))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QMainWindow, QWidget  {{ background:{BG0}; color:{T1}; }}
        QSplitter::handle     {{ background:{DIV}; }}
        #sidebar   {{ background:{BG1}; border-right:1px solid {DIV}; }}
        #listPanel {{ background:{BG2}; border-right:1px solid {DIV}; }}
        #listHeader, #listBar {{ background:{BG2}; }}
        QListWidget           {{ background:transparent; border:none; outline:none; }}
        QListWidget::item:selected {{ background:transparent; }}
        QScrollBar:vertical   {{ background:{BG2}; width:6px; margin:0; }}
        QScrollBar::handle:vertical {{
            background:{T3}; border-radius:3px; min-height:24px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        #editorPanel {{ background:{BG0}; }}
        #toolbar     {{ background:{BG0}; border-bottom:1px solid {DIV}; }}
        QPushButton  {{
            background:transparent; color:{T2}; border:none;
            border-radius:5px; font-size:13px; padding:2px 6px;
        }}
        QPushButton:hover   {{ background:{SEL}; color:{T1}; }}
        QPushButton:pressed {{ background:{DIV}; }}
        #listBar QPushButton {{
            background:{SEL}; color:{T1}; border-radius:6px; font-size:12px;
        }}
        #listBar QPushButton:hover {{ background:#4a4a4c; }}
        #sidebar QPushButton {{
            background:{SEL}; color:{T1}; border-radius:6px;
            font-size:12px; padding:4px 8px;
        }}
        #sidebar QPushButton:hover {{ background:#4a4a4c; }}
    """)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_dark_theme(app)
    window = NotesApp()
    window.show()
    QTimer.singleShot(500, _init_autocorrect)
    sys.exit(app.exec())
