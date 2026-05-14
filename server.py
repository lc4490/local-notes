"""
Local Notes — Flask web server for Tailscale access.

    pip install flask requests
    python3 server.py

Access at http://<tailscale-ip>:5000 from any Tailscale device.
On PC, notes are auto-committed to git after every save.
"""
import json, os, re, subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response, stream_with_context

# ── Config ─────────────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
_CFG      = json.loads(Path(os.path.join(_HERE, "config.json")).read_text()) if \
            os.path.exists(os.path.join(_HERE, "config.json")) else {}
ROOT      = os.path.expanduser(_CFG.get("root", "~/notes/"))
_AI_CFG   = _CFG.get("ai", {})
_AI_URL   = _AI_CFG.get("endpoint", "http://localhost:11434")
_AI_MODEL = _AI_CFG.get("model", "llama3.2")
TRASH_NB  = "Recently Deleted"
_SKIP     = {"Attachments", ".git"}
_NO_WRITE = {"Passwords", "Budget"}

app = Flask(__name__)

# ── File helpers ───────────────────────────────────────────────────────────────
def nb_path(nb):
    return os.path.join(ROOT, nb)

def _safe_path(path):
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(ROOT)):
        return None
    return real

def _title(raw):
    for line in raw.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""

def _preview(raw, n=140):
    lines = [l.strip() for l in raw.split("\n")[1:] if l.strip()]
    body  = " ".join(lines)
    return body[:n] + ("…" if len(body) > n else "")

def _safe_filename(name):
    return re.sub(r'[/\\:*?"<>|]', "", name).strip() or "Untitled"

def list_notebooks():
    if not os.path.isdir(ROOT):
        return []
    nbs = sorted(
        d for d in os.listdir(ROOT)
        if os.path.isdir(os.path.join(ROOT, d))
        and not d.startswith(".") and d not in _SKIP
        and d != TRASH_NB and d not in _NO_WRITE
    )
    if os.path.isdir(os.path.join(ROOT, TRASH_NB)):
        nbs.append(TRASH_NB)
    return nbs

def load_notebook(nb):
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

def git_commit(path, title):
    """Auto-commit a changed note. Silent — never blocks a save."""
    try:
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            subprocess.run(["git", "init"], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=ROOT, capture_output=True)
        rel = os.path.relpath(path, ROOT)
        subprocess.run(["git", "add", rel], cwd=ROOT, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"update: {title}"],
            cwd=ROOT, capture_output=True,
        )
    except Exception:
        pass

# ── API ────────────────────────────────────────────────────────────────────────
@app.route("/api/notebooks")
def api_notebooks():
    return jsonify(list_notebooks())

@app.route("/api/notes")
def api_notes():
    nb = request.args.get("notebook")
    if nb:
        return jsonify(load_notebook(nb))
    all_notes = [n for nb in list_notebooks() for n in load_notebook(nb)]
    return jsonify(sorted(all_notes, key=lambda n: n["modified"], reverse=True))

@app.route("/api/note")
def api_get_note():
    path = _safe_path(request.args.get("path", ""))
    if not path or not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    raw   = Path(path).read_text(encoding="utf-8")
    title = _title(raw)
    lines = raw.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return jsonify({"path": path, "title": title, "body": "\n".join(lines)})

@app.route("/api/note", methods=["POST"])
def api_create_note():
    d      = request.json
    nb     = d.get("notebook", "Notes")
    if nb in _NO_WRITE:
        return jsonify({"error": "read-only"}), 403
    title  = d.get("title", "Untitled")
    body   = d.get("body", "")
    folder = nb_path(nb)
    os.makedirs(folder, exist_ok=True)
    path   = os.path.join(folder, _safe_filename(title) + ".md")
    Path(path).write_text(f"# {title}\n\n{body}", encoding="utf-8")
    git_commit(path, title)
    return jsonify({"path": path})

@app.route("/api/note", methods=["PUT"])
def api_update_note():
    path = _safe_path(request.args.get("path", ""))
    if not path or not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    nb = os.path.basename(os.path.dirname(path))
    if nb in _NO_WRITE:
        return jsonify({"error": "read-only"}), 403
    d        = request.json
    title    = d.get("title", "Untitled")
    body     = d.get("body", "")
    new_path = os.path.join(nb_path(nb), _safe_filename(title) + ".md")
    Path(new_path).write_text(f"# {title}\n\n{body}", encoding="utf-8")
    if new_path != path and os.path.exists(path):
        os.remove(path)
    git_commit(new_path, title)
    return jsonify({"path": new_path})

@app.route("/api/note", methods=["DELETE"])
def api_delete_note():
    path = _safe_path(request.args.get("path", ""))
    if not path or not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    nb = os.path.basename(os.path.dirname(path))
    if nb in _NO_WRITE:
        return jsonify({"error": "read-only"}), 403
    os.remove(path)
    return jsonify({"ok": True})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    messages = request.json.get("messages", [])
    def generate():
        try:
            import requests as _req
            r = _req.post(
                f"{_AI_URL}/api/chat",
                json={"model": _AI_MODEL, "messages": messages, "stream": True},
                stream=True, timeout=120,
            )
            for line in r.iter_lines():
                if not line: continue
                data  = json.loads(line)
                token = data.get("message", {}).get("content", "")
                if token: yield token
                if data.get("done"): break
        except Exception as e:
            yield f"\n[Error: {e}]"
    return Response(stream_with_context(generate()), mimetype="text/plain")

# ── Web UI ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return _HTML

# ── HTML ───────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Local Notes</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg0:#1c1c1e;--bg1:#2c2c2e;--bg2:#3a3a3c;--t1:#f5f5f7;--t2:#aeaeb2;--t3:#636366;--div:#38383a;--acc:#9F832A;--sel:#3a3a3c}
html,body{height:100%;overflow:hidden}
body{background:var(--bg0);color:var(--t1);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;flex-direction:column}
#app{display:flex;flex:1;overflow:hidden}

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

#list-panel{width:280px;min-width:280px;background:var(--bg0);border-right:1px solid var(--div);display:flex;flex-direction:column;overflow:hidden}
.list-header{padding:14px 14px 8px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.list-title{font-size:18px;font-weight:700}
.btn-new{background:var(--acc);color:#fff;border:none;border-radius:20px;padding:5px 14px;font-size:13px;cursor:pointer;font-weight:500}
.btn-new:active{opacity:.8}
.note-items{flex:1;overflow-y:auto}
.note-item{padding:11px 14px;border-bottom:1px solid var(--div);cursor:pointer;user-select:none}
.note-item:hover,.note-item.active{background:var(--sel)}
.ni-title{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ni-meta{display:flex;gap:8px;margin-top:3px;align-items:center}
.ni-date{font-size:11px;color:var(--t3)}
.ni-nb{font-size:11px;color:var(--acc)}
.ni-prev{font-size:12px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}

#editor{flex:1;display:flex;flex-direction:column;overflow:hidden}
.ed-bar{padding:6px 32px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--div);flex-shrink:0;min-height:36px}
.icon-btn{background:none;border:none;color:var(--t2);cursor:pointer;font-size:12px;padding:4px 8px;border-radius:6px;font-family:inherit}
.icon-btn:hover{background:var(--sel);color:var(--t1)}
#note-title{width:100%;background:transparent;border:none;padding:18px 32px 4px;font-size:22px;font-weight:700;color:var(--t1);outline:none;font-family:inherit;flex-shrink:0}
#note-body{flex:1;width:100%;background:transparent;border:none;padding:8px 32px 32px;font-size:15px;color:var(--t1);outline:none;resize:none;font-family:inherit;line-height:1.7}
#note-preview{flex:1;overflow-y:auto;padding:8px 32px 32px;font-size:15px;color:var(--t1);line-height:1.7;cursor:text;display:none;word-break:break-word}
#note-preview h1{font-size:1.6em;font-weight:700;margin:.5em 0 .3em}
#note-preview h2{font-size:1.3em;font-weight:700;margin:.5em 0 .3em}
#note-preview h3{font-size:1.1em;font-weight:700;margin:.4em 0 .2em}
#note-preview p{margin:.4em 0}
#note-preview strong{font-weight:700}
#note-preview em{font-style:italic}
#note-preview code{font-family:monospace;background:var(--bg2);padding:2px 5px;border-radius:4px;font-size:.88em}
#note-preview pre{background:var(--bg2);padding:12px;border-radius:8px;overflow-x:auto;margin:.5em 0;font-size:.88em}
#note-preview ul,#note-preview ol{margin:.4em 0 .4em 1.5em}
#note-preview li{margin:.15em 0}
#note-preview a{color:var(--acc)}
#note-preview blockquote{border-left:3px solid var(--acc);padding-left:12px;color:var(--t2);margin:.4em 0}
#note-preview hr{border:none;border-top:1px solid var(--div);margin:1em 0}
#note-preview .tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:.4em 0}
#note-preview table{border-collapse:collapse;width:max-content;min-width:100%}
#note-preview td,#note-preview th{border:1px solid var(--div);padding:6px 10px;text-align:left;font-size:.9em;white-space:nowrap}
#note-preview th{background:var(--bg2);font-weight:600}
#note-preview li.done{color:var(--t3);text-decoration:line-through}
#note-preview a.note-link{color:var(--acc);text-decoration:none;border-bottom:1px solid var(--acc)}
#note-preview li.cb{cursor:pointer;user-select:none}
#note-preview li.cb:active{opacity:.6}

#chat{width:300px;min-width:300px;background:var(--bg1);border-left:1px solid var(--div);display:none;flex-direction:column;overflow:hidden}
#chat.open{display:flex}
.chat-hdr{padding:14px 14px 12px;border-bottom:1px solid var(--div);font-weight:600;font-size:14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.chat-msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
.msg{padding:9px 12px;border-radius:12px;font-size:13px;line-height:1.5;max-width:88%;white-space:pre-wrap;word-break:break-word}
.msg.user{background:var(--acc);color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
.msg.ai{background:var(--bg2);color:var(--t1);align-self:flex-start;border-bottom-left-radius:3px}
.chat-foot{padding:10px;border-top:1px solid var(--div);display:flex;gap:8px;flex-shrink:0}
.chat-in{flex:1;background:var(--bg2);border:none;border-radius:8px;padding:8px 10px;color:var(--t1);font-size:13px;outline:none;resize:none;font-family:inherit;line-height:1.4}
.chat-send{background:var(--acc);color:#fff;border:none;border-radius:8px;padding:0 14px;cursor:pointer;font-size:16px;font-weight:500}

#toast{position:fixed;bottom:20px;right:20px;background:var(--bg2);color:var(--t2);padding:6px 14px;border-radius:20px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:99}
#toast.show{opacity:1}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg2);border-radius:3px}

@media(max-width:700px){
  #sidebar,#list-panel,#editor{position:fixed;inset:0;width:100%!important;min-width:unset!important;display:none}
  #sidebar.mv,#list-panel.mv,#editor.mv{display:flex}
  .mob-back{display:inline-flex!important}
  .ed-bar{padding:6px 14px}
  #note-title{padding:16px 14px 4px}
  #note-body{padding:8px 14px 24px}
  #note-preview{padding:8px 14px 24px}
  #chat{display:none!important}
}
.mob-back{display:none;background:none;border:none;color:var(--acc);cursor:pointer;font-size:14px;align-items:center;padding:0;margin-right:6px;font-family:inherit}
</style>
</head>
<body>
<div id="toast"></div>
<div id="app">

<div id="sidebar" class="mv">
  <div class="sidebar-top">
    <div class="app-title">
      <span>Local Notes</span>
      <button class="icon-btn" onclick="toggleChat()" style="font-size:15px;padding:2px 6px">✦</button>
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
    <button class="icon-btn" id="ed-toggle" onclick="toggleEditing()" style="margin-left:auto">Preview</button>
    <button class="icon-btn" onclick="deleteNote()" style="color:#ff453a">Delete</button>
  </div>
  <input id="note-title" placeholder="Title" oninput="onEdit()">
  <textarea id="note-body" placeholder="Start writing…" oninput="onEdit()"></textarea>
  <div id="note-preview" onclick="setEditing(true)"></div>
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
const S = { nb: null, note: null, notes: [], allNotes: [], notebooks: [], search: '', timer: null, chatLog: [], editing: false };

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
  S.allNotes  = await api('GET', '/api/notes');
  S.notes     = S.allNotes;
  renderSidebar();
  renderList();
  $('list-title').textContent = 'All Notes';
  if (window.innerWidth > 700) {
    $('list-panel').classList.add('mv');
    $('editor').classList.add('mv');
  }
}

async function loadNotes(nb) {
  S.nb    = nb;
  S.notes = nb ? S.allNotes.filter(n => n.notebook === nb) : S.allNotes;
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
  setEditing(window.innerWidth > 700);
  if (window.innerWidth <= 700) {
    $('list-panel').classList.remove('mv');
    $('editor').classList.add('mv');
  }
}

async function newNote() {
  const nb = S.nb || (S.notebooks[0] || 'Notes');
  const d  = await api('POST', '/api/note', { notebook: nb, title: 'Untitled', body: '' });
  await refreshAllNotes();
  S.notes = S.nb ? S.allNotes.filter(n => n.notebook === S.nb) : S.allNotes;
  renderSidebar();
  renderList();
  await openNote(d.path);
}

async function save() {
  if (!S.note) return;
  const title = $('note-title').value.trim() || 'Untitled';
  const body  = $('note-body').value;
  try {
    const res   = await api('PUT', `/api/note?path=${enc(S.note.path)}`, { title, body });
    S.note.path = res.path;
    $('save-lbl').textContent = 'Saved';
    setTimeout(() => { if ($('save-lbl').textContent === 'Saved') $('save-lbl').textContent = ''; }, 1500);
    await refreshAllNotes();
    S.notes = S.nb ? S.allNotes.filter(n => n.notebook === S.nb) : S.allNotes;
    renderSidebar();
    renderList();
  } catch(e) { toast('Save failed'); }
}

async function deleteNote() {
  if (!S.note || !confirm('Delete this note?')) return;
  await api('DELETE', `/api/note?path=${enc(S.note.path)}`);
  S.note = null;
  $('note-title').value = '';
  $('note-body').value  = '';
  setEditing(false);
  await refreshAllNotes();
  S.notes = S.nb ? S.allNotes.filter(n => n.notebook === S.nb) : S.allNotes;
  renderSidebar();
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

function renderSidebar() {
  const el = $('nb-list');
  el.innerHTML = '';
  [[null, 'All Notes']].concat(S.notebooks.map(nb => [nb, nb])).forEach(([nb, label]) => {
    const count = nb ? S.allNotes.filter(n => n.notebook === nb).length : S.allNotes.length;
    const div   = document.createElement('div');
    div.className = 'nb-item' + (S.nb === nb ? ' active' : '');
    div.innerHTML = `<span>${esc(label)}</span><span class="nb-count">${count}</span>`;
    div.onclick   = () => loadNotes(nb);
    el.appendChild(div);
  });
}

function renderList() {
  const el = $('note-items');
  const filtered = S.search
    ? S.notes.filter(n => n.title.toLowerCase().includes(S.search) || n.preview.toLowerCase().includes(S.search))
    : S.notes;
  if (!filtered.length) {
    el.innerHTML = `<div style="padding:32px;text-align:center;color:var(--t3);font-size:14px">${S.search?'No results':'No notes'}</div>`;
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

async function refreshAllNotes() {
  S.allNotes = await api('GET', '/api/notes');
}

function openNoteByLink(ref, e) {
  if (e) e.stopPropagation();
  // ref is either a relative path (Notebook/Note.md) or absolute (/full/path/Note.md)
  // try exact match first, then match by last two path components
  let note = S.allNotes.find(n => n.path === ref);
  if (!note) {
    const parts = ref.replace(/\\/g,'/').split('/').filter(Boolean);
    const tail  = parts.slice(-2).join('/');
    note = S.allNotes.find(n => n.path.replace(/\\/g,'/').endsWith('/' + tail));
  }
  if (note) openNote(note.path);
  else toast('Note not found');
}

function toggleCheckbox(n, e) {
  if (e) e.stopPropagation();
  const lines = $('note-body').value.split('\n');
  let idx = 0;
  for (let i = 0; i < lines.length; i++) {
    if (/^[-*+] \[[ xX]\] /.test(lines[i].trim())) {
      if (idx === n) {
        lines[i] = /\[x\]/i.test(lines[i])
          ? lines[i].replace(/\[x\]/i, '[ ]')
          : lines[i].replace(/\[ \]/, '[x]');
        break;
      }
      idx++;
    }
  }
  $('note-body').value = lines.join('\n');
  $('note-preview').innerHTML = mdRender($('note-body').value);
  onEdit();
}

function mdRender(src) {
  let cbIdx = 0;
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const inl = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\*(.+?)\*/g,'<em>$1</em>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\[([^\]]+)\]\(localnotes:\/\/([^)]+)\)/g, (_, txt, ref) =>
      `<a href="#" class="note-link" onclick="openNoteByLink(decodeURIComponent('${encodeURIComponent(ref)}'),event)">${txt}</a>`)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  const codes = [];
  src = src.replace(/```([\s\S]*?)```/g, (_, c) => { codes.push(c.trim()); return `\x02${codes.length-1}\x03`; });
  const lines = src.split('\n');
  let out = '', i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    // fenced code
    if (/^\x02\d+\x03$/.test(l.trim())) {
      out += `<pre><code>${esc(codes[+l.match(/\d+/)[0]])}</code></pre>`; i++; continue;
    }
    // heading
    const hm = l.match(/^(#{1,3}) (.+)$/);
    if (hm) { out += `<h${hm[1].length}>${inl(hm[2])}</h${hm[1].length}>`; i++; continue; }
    // GFM table
    if (/^\|.+\|/.test(l) && i+1 < lines.length && /^\|[-|: ]+\|/.test(lines[i+1])) {
      const ths = l.split('|').filter(c=>c.trim()).map(c=>`<th>${inl(c.trim())}</th>`).join('');
      i += 2;
      const trs = [];
      while (i < lines.length && /^\|.+\|/.test(lines[i])) {
        trs.push(`<tr>${lines[i].split('|').filter(c=>c.trim()).map(c=>`<td>${inl(c.trim())}</td>`).join('')}</tr>`);
        i++;
      }
      out += `<div class="tbl-wrap"><table><thead><tr>${ths}</tr></thead><tbody>${trs.join('')}</tbody></table></div>`; continue;
    }
    // unordered list
    if (/^[-*+] /.test(l.trim())) {
      const items = [];
      while (i < lines.length && /^[-*+] /.test(lines[i].trim())) {
        const t = lines[i].trim().slice(2);
        if (/^\[x\] /i.test(t)) items.push(`<li class="done cb" data-cb="${cbIdx++}" onclick="toggleCheckbox(+this.dataset.cb,event)">✓ ${inl(t.slice(4))}</li>`);
        else if (/^\[ \] /.test(t)) items.push(`<li class="cb" data-cb="${cbIdx++}" onclick="toggleCheckbox(+this.dataset.cb,event)">○ ${inl(t.slice(4))}</li>`);
        else items.push(`<li>${inl(t)}</li>`);
        i++;
      }
      out += `<ul>${items.join('')}</ul>`; continue;
    }
    // ordered list
    if (/^\d+\. /.test(l.trim())) {
      const items = [];
      while (i < lines.length && /^\d+\. /.test(lines[i].trim())) {
        items.push(`<li>${inl(lines[i].trim().replace(/^\d+\. /,''))}</li>`); i++;
      }
      out += `<ol>${items.join('')}</ol>`; continue;
    }
    // paragraph: collect until blank line or block-level element
    const para = [];
    while (i < lines.length) {
      const pl = lines[i];
      if (!pl.trim()) break;
      if (/^#{1,3} /.test(pl) || /^[-*+] /.test(pl.trim()) || /^\d+\. /.test(pl.trim()) || /^\|.+\|/.test(pl) || /^\x02\d+\x03$/.test(pl.trim())) break;
      para.push(pl); i++;
    }
    if (para.length) out += `<p>${para.map(inl).join('<br>')}</p>`;
  }
  return out;
}

function setEditing(on) {
  S.editing = on;
  $('note-body').style.display    = on ? 'block' : 'none';
  $('note-preview').style.display = on ? 'none'  : 'block';
  $('ed-toggle').textContent      = on ? 'Preview' : 'Edit';
  if (on) {
    $('note-body').focus();
  } else {
    $('note-preview').innerHTML = S.note ? mdRender($('note-body').value || '') : '';
  }
}

function toggleEditing() { setEditing(!S.editing); }

function toggleChat() { $('chat').classList.toggle('open'); }
function chatKey(e) { if (e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();} }

async function sendChat() {
  const inp  = $('chat-in');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  S.chatLog.push({ role: 'user', content: text });
  appendMsg('user', text);
  const el = appendMsg('ai', '…');
  try {
    const r   = await fetch('/api/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
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

function mobBack(from) {
  if (from === 'list') {
    $('list-panel').classList.remove('mv');
    $('sidebar').classList.add('mv');
  } else {
    $('editor').classList.remove('mv');
    $('list-panel').classList.add('mv');
  }
}

const enc  = s => encodeURIComponent(s);
const esc  = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function toast(msg) {
  const el=$('toast'); el.textContent=msg; el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 2000);
}
function relDate(iso) {
  const d=new Date(iso), now=new Date(), diff=now-d;
  if (diff<60e3)     return 'Just now';
  if (diff<3600e3)   return Math.floor(diff/60e3)+'m ago';
  if (diff<86400e3)  return Math.floor(diff/3600e3)+'h ago';
  if (diff<172800e3) return 'Yesterday';
  return d.toLocaleDateString();
}

init();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
