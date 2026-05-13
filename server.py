"""
Local Notes — Tailscale web server.

    pip install fastapi uvicorn
    uvicorn server:app --host 0.0.0.0 --port 8000

Access at http://<tailscale-ip>:8000 from any device on your Tailscale network.
"""
import json, os, re
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError:
    raise SystemExit("Run:  pip install fastapi uvicorn")

# ── Config ─────────────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.path.join(_HERE, "config.json")
_CFG      = json.loads(Path(_CFG_PATH).read_text()) if os.path.exists(_CFG_PATH) else {}
ROOT      = os.path.expanduser(_CFG.get("root", "~/notes/"))
_AI_CFG   = _CFG.get("ai", {})
_AI_URL   = _AI_CFG.get("endpoint", "http://localhost:11434")
_AI_MODEL = _AI_CFG.get("model", "llama3.2")
TRASH_NB    = "Recently Deleted"
_SKIP       = {"Attachments", ".git"}
_READ_ONLY  = {"Passwords", "Budget"}   # never allow writes to these via web

app = FastAPI(title="Local Notes")

# ── File helpers ───────────────────────────────────────────────────────────────
def nb_path(nb: str) -> str:
    return os.path.join(ROOT, nb)

def _safe_path(path: str) -> str:
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(ROOT)):
        raise HTTPException(403, "Path outside notes root")
    return real

def _title(raw: str) -> str:
    for line in raw.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""

def _preview(raw: str, n: int = 140) -> str:
    lines = [l.strip() for l in raw.split("\n")[1:] if l.strip()]
    body  = " ".join(lines)
    return body[:n] + ("…" if len(body) > n else "")

def _safe_filename(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "", name).strip() or "Untitled"

def list_notebooks() -> list[str]:
    if not os.path.isdir(ROOT):
        return []
    nbs = sorted(
        d for d in os.listdir(ROOT)
        if os.path.isdir(os.path.join(ROOT, d))
        and not d.startswith(".") and d not in _SKIP
        and d != TRASH_NB and d not in _READ_ONLY
    )
    if os.path.isdir(os.path.join(ROOT, TRASH_NB)):
        nbs.append(TRASH_NB)
    return nbs

def load_notebook(nb: str) -> list[dict]:
    folder = nb_path(nb)
    if not os.path.isdir(folder):
        return []
    notes = []
    for fname in os.listdir(folder):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(folder, fname)
        try:
            raw      = Path(path).read_text(encoding="utf-8")
            modified = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            notes.append({
                "path":     path,
                "title":    _title(raw) or fname[:-3],
                "modified": modified,
                "preview":  _preview(raw),
                "notebook": nb,
            })
        except Exception:
            pass
    return sorted(notes, key=lambda n: n["modified"], reverse=True)

# ── REST API ───────────────────────────────────────────────────────────────────
@app.get("/api/notebooks")
def get_notebooks():
    return list_notebooks()

@app.get("/api/notes")
def get_notes(notebook: Optional[str] = None):
    if notebook:
        return load_notebook(notebook)
    all_notes = [n for nb in list_notebooks() for n in load_notebook(nb)]
    return sorted(all_notes, key=lambda n: n["modified"], reverse=True)

@app.get("/api/note")
def get_note(path: str):
    path = _safe_path(path)
    if not os.path.exists(path):
        raise HTTPException(404, "Note not found")
    raw   = Path(path).read_text(encoding="utf-8")
    title = _title(raw)
    lines = raw.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return {"path": path, "title": title, "body": "\n".join(lines)}

class _NoteCreate(BaseModel):
    notebook: str
    title:    str
    body:     str = ""

class _NoteUpdate(BaseModel):
    title: str
    body:  str

def _assert_writable(nb: str):
    if nb in _READ_ONLY:
        raise HTTPException(403, f"'{nb}' is read-only via the web interface")

@app.post("/api/note")
def create_note(d: _NoteCreate):
    _assert_writable(d.notebook)
    folder = nb_path(d.notebook)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _safe_filename(d.title) + ".md")
    Path(path).write_text(f"# {d.title}\n\n{d.body}", encoding="utf-8")
    return {"path": path}

@app.put("/api/note")
def update_note(path: str, d: _NoteUpdate):
    path = _safe_path(path)
    if not os.path.exists(path):
        raise HTTPException(404, "Note not found")
    nb = os.path.basename(os.path.dirname(path))
    _assert_writable(nb)
    new_path = os.path.join(nb_path(nb), _safe_filename(d.title) + ".md")
    Path(new_path).write_text(f"# {d.title}\n\n{d.body}", encoding="utf-8")
    if new_path != path and os.path.exists(path):
        os.remove(path)
    return {"path": new_path}

@app.delete("/api/note")
def delete_note(path: str):
    path = _safe_path(path)
    if not os.path.exists(path):
        raise HTTPException(404, "Note not found")
    nb = os.path.basename(os.path.dirname(path))
    _assert_writable(nb)
    os.remove(path)
    return {"ok": True}

# ── Ollama chat (streaming) ────────────────────────────────────────────────────
class _ChatPayload(BaseModel):
    messages: list[dict]

@app.post("/api/chat")
def chat(payload: _ChatPayload):
    try:
        import requests
    except ImportError:
        raise HTTPException(500, "pip install requests")

    def generate():
        try:
            r = requests.post(
                f"{_AI_URL}/api/chat",
                json={"model": _AI_MODEL, "messages": payload.messages, "stream": True},
                stream=True, timeout=120,
            )
            for line in r.iter_lines():
                if not line:
                    continue
                data  = json.loads(line)
                token = data.get("message", {}).get("content", "")
                if token:
                    yield token
                if data.get("done"):
                    break
        except Exception as e:
            yield f"\n[Error: {e}]"

    return StreamingResponse(generate(), media_type="text/plain")

# ── HTML UI ────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Local Notes</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg0:#1c1c1e;--bg1:#2c2c2e;--bg2:#3a3a3c;--t1:#f5f5f7;--t2:#aeaeb2;--t3:#636366;--div:#38383a;--acc:#0a84ff;--sel:#3a3a3c}
html,body{height:100%;overflow:hidden}
body{background:var(--bg0);color:var(--t1);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;flex-direction:column}
#app{display:flex;flex:1;overflow:hidden}

/* Sidebar */
#sidebar{width:220px;min-width:220px;background:var(--bg1);border-right:1px solid var(--div);display:flex;flex-direction:column;overflow:hidden}
.sidebar-top{padding:16px 12px 8px}
.app-title{font-size:15px;font-weight:700;letter-spacing:-.3px;display:flex;align-items:center;justify-content:space-between}
.search{width:100%;background:var(--bg2);border:none;border-radius:8px;padding:7px 10px;color:var(--t1);font-size:13px;margin-top:10px;outline:none}
.search::placeholder{color:var(--t3)}
.nb-list{flex:1;overflow-y:auto;padding:4px 6px 8px}
.nb-item{padding:8px 10px;border-radius:8px;cursor:pointer;font-size:14px;color:var(--t2);display:flex;align-items:center;gap:6px;user-select:none}
.nb-item:hover{background:var(--sel);color:var(--t1)}
.nb-item.active{background:var(--sel);color:var(--t1);font-weight:600}
.nb-count{margin-left:auto;font-size:12px;color:var(--t3)}

/* Note list */
#list-panel{width:280px;min-width:280px;background:var(--bg0);border-right:1px solid var(--div);display:flex;flex-direction:column;overflow:hidden}
.list-header{padding:14px 14px 8px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.list-title{font-size:18px;font-weight:700}
.btn-new{background:var(--acc);color:#fff;border:none;border-radius:20px;padding:5px 14px;font-size:13px;cursor:pointer;font-weight:500;white-space:nowrap}
.btn-new:active{opacity:.8}
.note-items{flex:1;overflow-y:auto}
.note-item{padding:11px 14px;border-bottom:1px solid var(--div);cursor:pointer;user-select:none}
.note-item:hover,.note-item.active{background:var(--sel)}
.ni-title{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ni-meta{display:flex;gap:8px;margin-top:3px;align-items:center}
.ni-date{font-size:11px;color:var(--t3)}
.ni-nb{font-size:11px;color:var(--acc)}
.ni-prev{font-size:12px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}

/* Editor */
#editor{flex:1;display:flex;flex-direction:column;overflow:hidden}
.ed-bar{padding:6px 32px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--div);flex-shrink:0;min-height:36px}
.icon-btn{background:none;border:none;color:var(--t2);cursor:pointer;font-size:12px;padding:4px 8px;border-radius:6px;font-family:inherit}
.icon-btn:hover{background:var(--sel);color:var(--t1)}
#note-title{width:100%;background:transparent;border:none;padding:18px 32px 4px;font-size:22px;font-weight:700;color:var(--t1);outline:none;font-family:inherit;flex-shrink:0}
#note-body{flex:1;width:100%;background:transparent;border:none;padding:8px 32px 32px;font-size:15px;color:var(--t1);outline:none;resize:none;font-family:inherit;line-height:1.7}
.empty{flex:1;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:15px}

/* Chat */
#chat{width:300px;min-width:300px;background:var(--bg1);border-left:1px solid var(--div);display:none;flex-direction:column;overflow:hidden}
#chat.open{display:flex}
.chat-hdr{padding:14px 14px 12px;border-bottom:1px solid var(--div);font-weight:600;font-size:14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.chat-msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
.msg{padding:9px 12px;border-radius:12px;font-size:13px;line-height:1.5;max-width:88%;white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--acc);color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
.msg.ai{background:var(--bg2);color:var(--t1);align-self:flex-start;border-bottom-left-radius:3px}
.chat-foot{padding:10px;border-top:1px solid var(--div);display:flex;gap:8px;flex-shrink:0}
.chat-in{flex:1;background:var(--bg2);border:none;border-radius:8px;padding:8px 10px;color:var(--t1);font-size:13px;outline:none;resize:none;font-family:inherit;line-height:1.4}
.chat-send{background:var(--acc);color:#fff;border:none;border-radius:8px;padding:0 14px;cursor:pointer;font-size:13px;font-weight:500}
.chat-send:active{opacity:.8}

/* Toast */
#toast{position:fixed;bottom:20px;right:20px;background:var(--bg2);color:var(--t2);padding:6px 14px;border-radius:20px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:99}
#toast.show{opacity:1}

/* Scrollbars */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg2);border-radius:3px}

/* Mobile */
@media(max-width:700px){
  #sidebar,#list-panel,#editor{position:fixed;inset:0;width:100%!important;min-width:unset!important;display:none}
  #sidebar.mv,#list-panel.mv,#editor.mv{display:flex}
  .mob-back{display:inline-flex!important}
  .ed-bar{padding:6px 14px}
  #note-title{padding:16px 14px 4px}
  #note-body{padding:8px 14px 24px}
  #chat{display:none!important}
}
.mob-back{display:none;background:none;border:none;color:var(--acc);cursor:pointer;font-size:14px;align-items:center;gap:2px;padding:0;margin-right:6px;font-family:inherit}
</style>
</head>
<body>
<div id="toast"></div>
<div id="app">

<div id="sidebar" class="mv">
  <div class="sidebar-top">
    <div class="app-title">
      <span>Local Notes</span>
      <button class="icon-btn" onclick="toggleChat()" title="AI Chat" style="font-size:15px;padding:2px 6px">✦</button>
    </div>
    <input class="search" id="search" placeholder="Search notes…" oninput="onSearch(this.value)">
  </div>
  <div class="nb-list" id="nb-list"></div>
</div>

<div id="list-panel">
  <div class="list-header">
    <div style="display:flex;align-items:center">
      <button class="mob-back" onclick="mobBack('list')">‹</button>
      <span class="list-title" id="list-title">All Notes</span>
    </div>
    <button class="btn-new" onclick="newNote()">+ New</button>
  </div>
  <div class="note-items" id="note-items"></div>
</div>

<div id="editor">
  <div class="ed-bar">
    <button class="mob-back" onclick="mobBack('editor')">‹</button>
    <span id="save-lbl" style="font-size:12px;color:var(--t3)"></span>
    <button class="icon-btn" onclick="deleteNote()" style="margin-left:auto;color:#ff453a">Delete</button>
  </div>
  <input id="note-title" placeholder="Title" oninput="onEdit()">
  <textarea id="note-body" placeholder="Start writing…" oninput="onEdit()"></textarea>
</div>

<div id="chat">
  <div class="chat-hdr">
    <span>✦ AI</span>
    <button class="icon-btn" onclick="toggleChat()">✕</button>
  </div>
  <div class="chat-msgs" id="chat-msgs"></div>
  <div class="chat-foot">
    <textarea class="chat-in" id="chat-in" placeholder="Ask anything…" rows="1" onkeydown="chatKey(event)"></textarea>
    <button class="chat-send" onclick="sendChat()">↑</button>
  </div>
</div>

</div>
<script>
const $ = id => document.getElementById(id);
const S = { nb: null, note: null, notes: [], notebooks: [], search: '', timer: null, chatLog: [] };

async function api(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body ? {'Content-Type':'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function init() {
  S.notebooks = await api('GET', '/api/notebooks');
  await loadNotes(null);
  if (window.innerWidth > 700) {
    $('list-panel').classList.add('mv');
    $('editor').classList.add('mv');
  }
}

async function loadNotes(nb) {
  S.nb = nb;
  const url = nb ? `/api/notes?notebook=${enc(nb)}` : '/api/notes';
  S.notes = await api('GET', url);
  renderSidebar();
  renderList();
  $('list-title').textContent = nb || 'All Notes';
  if (window.innerWidth <= 700) {
    $('sidebar').classList.remove('mv');
    $('list-panel').classList.add('mv');
    $('editor').classList.remove('mv');
  }
}

async function openNote(path) {
  const d = await api('GET', `/api/note?path=${enc(path)}`);
  S.note = d;
  $('note-title').value = d.title;
  $('note-body').value  = d.body;
  $('save-lbl').textContent = '';
  document.querySelectorAll('.note-item').forEach(el =>
    el.classList.toggle('active', el.dataset.path === path));
  if (window.innerWidth <= 700) {
    $('list-panel').classList.remove('mv');
    $('editor').classList.add('mv');
  }
}

async function newNote() {
  const nb = S.nb || (S.notebooks[0] || 'Notes');
  const d  = await api('POST', '/api/note', { notebook: nb, title: 'Untitled', body: '' });
  S.notes = await api('GET', S.nb ? `/api/notes?notebook=${enc(S.nb)}` : '/api/notes');
  renderList();
  await openNote(d.path);
}

async function save() {
  if (!S.note) return;
  const title = $('note-title').value.trim() || 'Untitled';
  const body  = $('note-body').value;
  try {
    const res = await api('PUT', `/api/note?path=${enc(S.note.path)}`, { title, body });
    S.note.path = res.path;
    $('save-lbl').textContent = 'Saved';
    setTimeout(() => { if ($('save-lbl').textContent === 'Saved') $('save-lbl').textContent = ''; }, 1500);
    S.notes = await api('GET', S.nb ? `/api/notes?notebook=${enc(S.nb)}` : '/api/notes');
    renderList();
  } catch(e) { toast('Save failed'); }
}

async function deleteNote() {
  if (!S.note || !confirm('Delete this note?')) return;
  await api('DELETE', `/api/note?path=${enc(S.note.path)}`);
  S.note = null;
  $('note-title').value = '';
  $('note-body').value  = '';
  $('save-lbl').textContent = '';
  S.notes = await api('GET', S.nb ? `/api/notes?notebook=${enc(S.nb)}` : '/api/notes');
  renderList();
  if (window.innerWidth <= 700) {
    $('editor').classList.remove('mv');
    $('list-panel').classList.add('mv');
  }
}

function onEdit() {
  $('save-lbl').textContent = '•••';
  clearTimeout(S.timer);
  S.timer = setTimeout(save, 800);
}

function onSearch(q) {
  S.search = q.toLowerCase();
  renderList();
}

// ── Render ─────────────────────────────────────────────────────────────────
function renderSidebar() {
  const el = $('nb-list');
  el.innerHTML = '';
  [[null, 'All Notes']].concat(S.notebooks.map(nb => [nb, nb])).forEach(([nb, label]) => {
    const count = nb ? S.notes.filter(n => n.notebook === nb).length : S.notes.length;
    const div   = document.createElement('div');
    div.className = 'nb-item' + (S.nb === nb ? ' active' : '');
    div.innerHTML = `<span>${esc(label)}</span><span class="nb-count">${count}</span>`;
    div.onclick   = () => loadNotes(nb);
    el.appendChild(div);
  });
}

function renderList() {
  const el  = $('note-items');
  const filtered = S.search
    ? S.notes.filter(n => n.title.toLowerCase().includes(S.search) || n.preview.toLowerCase().includes(S.search))
    : S.notes;
  if (!filtered.length) {
    el.innerHTML = `<div style="padding:32px;text-align:center;color:var(--t3);font-size:14px">${S.search ? 'No results' : 'No notes'}</div>`;
    return;
  }
  el.innerHTML = '';
  filtered.forEach(n => {
    const div = document.createElement('div');
    div.className = 'note-item' + (S.note && S.note.path === n.path ? ' active' : '');
    div.dataset.path = n.path;
    div.innerHTML = `
      <div class="ni-title">${esc(n.title)}</div>
      <div class="ni-meta">
        <span class="ni-date">${relDate(n.modified)}</span>
        ${S.nb ? '' : `<span class="ni-nb">${esc(n.notebook)}</span>`}
      </div>
      <div class="ni-prev">${esc(n.preview)}</div>`;
    div.onclick = () => openNote(n.path);
    el.appendChild(div);
  });
}

// ── Chat ───────────────────────────────────────────────────────────────────
function toggleChat() { $('chat').classList.toggle('open'); }

function chatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

async function sendChat() {
  const inp  = $('chat-in');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  S.chatLog.push({ role: 'user', content: text });
  appendMsg('user', text);
  const el = appendMsg('ai', '');
  try {
    const r   = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ messages: S.chatLog }),
    });
    const rd  = r.body.getReader();
    const dec = new TextDecoder();
    let full  = '';
    while (true) {
      const { done, value } = await rd.read();
      if (done) break;
      full += dec.decode(value);
      el.textContent = full;
      el.scrollIntoView({ block: 'end' });
    }
    S.chatLog.push({ role: 'assistant', content: full });
  } catch(e) { el.textContent = '[Error: ' + e.message + ']'; }
}

function appendMsg(role, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.textContent = text;
  $('chat-msgs').appendChild(el);
  el.scrollIntoView({ block: 'end' });
  return el;
}

// ── Mobile nav ─────────────────────────────────────────────────────────────
function mobBack(from) {
  if (from === 'list') {
    $('list-panel').classList.remove('mv');
    $('sidebar').classList.add('mv');
  } else if (from === 'editor') {
    $('editor').classList.remove('mv');
    $('list-panel').classList.add('mv');
  }
}

// ── Utils ──────────────────────────────────────────────────────────────────
function enc(s)  { return encodeURIComponent(s); }
function esc(s)  { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function toast(msg) {
  const el = $('toast'); el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2000);
}
function relDate(iso) {
  const d = new Date(iso), now = new Date(), diff = now - d;
  if (diff < 60e3)     return 'Just now';
  if (diff < 3600e3)   return Math.floor(diff / 60e3) + 'm ago';
  if (diff < 86400e3)  return Math.floor(diff / 3600e3) + 'h ago';
  if (diff < 172800e3) return 'Yesterday';
  return d.toLocaleDateString();
}

init();
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML
