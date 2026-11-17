"""
Template for a feature plugin (files starting with "_" are not loaded).

Copy to software/portal/features/<id>.py, and put the page script in
software/portal/static/features/<id>.js. The full contract is in the
"feature plugins" section of server.py; in short:

  FEATURE   what the launcher shows (a tile in its group, linking to `route`)
  ROUTES    the feature's /api/ endpoints and who may call them
  setup()   optional, once at start

Page script (static/features/<id>.js):

    Badger.register("/notes", function (ctx) {
      if (!Badger.isMember()) return Badger.needMember("Notes");
      Badger.api("/api/notes").then(function (r) {
        Badger.h("<h1>Notes</h1>" + r.items.map(function (n) { return "<p>" + Badger.esc(n.text) + "</p>"; }).join(""));
      });
    });

Uploads: mark the route "raw": True and send it with Badger.upload(path, blob) — the body
is not parsed; read req.rfile (exactly Content-Length bytes, already checked against max_bytes).
Downloads: return None after ctx.send_bytes(req, data, ctype) / ctx.send_file(req, path, ctype, name).
Data lives in ctx.data_dir() (one folder per feature); its own config is ctx.cfg() → [<id>] in prepper.toml.
"""
import json
import time

FEATURE = {"title": "Notes", "icon": "📝", "group": "family", "route": "/notes",
           "script": "/features/notes.js", "access": "member", "description": "Example feature"}


def _file(ctx):
    return ctx.data_dir() / "notes.json"


def list_notes(req, ctx, args):
    return {"items": ctx.read_json(_file(ctx), []) or []}


def add_note(req, ctx, args):
    text = str((args["data"] or {}).get("text", "")).strip()[:500]
    if not text:
        return 400, {"error": "empty"}
    items = ctx.read_json(_file(ctx), []) or []
    items.append({"text": text, "by": args["member"]["name"], "at": time.time()})
    ctx.atomic_write(_file(ctx), json.dumps(items))
    return {"ok": True}


ROUTES = [
    {"method": "GET", "path": "/api/notes", "handler": list_notes, "access": "member"},
    {"method": "POST", "path": "/api/notes", "handler": add_note, "access": "member"},
]
