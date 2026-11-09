#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sessconv — Traductor de sesiones entre Qoder, CodeBuddy y OpenCode.

Convierte una sesión de un formato a otro preservando el contexto
(mensajes usuario/asistente, razonamiento, tool calls/results, cwd y título)
para poder retomar la conversación con otra herramienta cuando se acaba el uso.

Solo depende de la stdlib de Python 3. No requiere inferencia para funcionar.

Uso:
  sessconv list    [--from {qoder|codebuddy|opencode}] [--cwd DIR] [--json]
  sessconv inspect <id-o-ruta> --from TOOL [--json]
  sessconv convert <id-o-ruta> --from TOOL --to TOOL [--cwd DIR] [--title T] [--dry-run] [--print-id]
  sessconv roundtrip <id> --from TOOL --via TOOL2
  sessconv selftest
"""

import argparse
import glob
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

TOOLS = ("qoder", "codebuddy", "opencode")

HOME = os.path.expanduser("~")
QODER_ROOT = os.path.join(HOME, ".qoder", "projects")
CODEBUDDY_ROOT = os.path.join(HOME, ".codebuddy", "projects")


# --------------------------------------------------------------------------
# Identificadores / utilidades
# --------------------------------------------------------------------------

def new_id_qoder():
    return str(uuid.uuid4())


def new_id_codebuddy():
    # UUIDv7: 48 bits de ms + 80 bits aleatorios
    ts = int(time.time() * 1000)
    rand = secrets.token_bytes(10)
    b = bytearray(ts.to_bytes(6, "big") + rand)
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def new_id_opencode():
    return "ses_" + "".join(secrets.choice(_B62) for _ in range(26))


def new_msg_id():
    return str(uuid.uuid4())


def slug_qoder(cwd):
    return cwd.replace("/", "-")


def slug_codebuddy(cwd):
    return cwd.replace("/", "-").lstrip("-")


def to_iso(ms):
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms % 1000):03d}Z"


def from_iso(s):
    if not s:
        return int(time.time() * 1000)
    s = s.strip()
    # acepta "2026-11-01T19:29:22.367Z"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?Z?", s)
    if m:
        frac = m.group(7) or "0"
        ms = int((frac + "000")[:3])
        dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
            tzinfo=timezone.utc,
        )
        return int(dt.timestamp() * 1000) + ms
    return int(time.time() * 1000)


def now_ms():
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Mapeo de herramientas (nombre + argumentos)
# --------------------------------------------------------------------------
# Canónico usa nombres estilo Claude (Qoder/CodeBuddy). OpenCode usa minúsculas.

TOOL_TO_OPENCODE = {
    "Bash": "shell",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "webfetch",
    "ViewImage": "read",
    "Agent": "agent",
    "Task": "agent",
    "EnterPlanMode": "enter_plan_mode",
    "ExitPlanMode": "exit_plan_mode",
    "AskUserQuestion": "ask_user_question",
    "Skill": "skill",
}
TOOL_FROM_OPENCODE = {}
for _k, _v in TOOL_TO_OPENCODE.items():
    if _v not in TOOL_FROM_OPENCODE:
        TOOL_FROM_OPENCODE[_v] = _k

# Renombrado de claves de argumentos entre canónico (Claude) y OpenCode.
ARG_RENAME_TO_OPENCODE = {
    "Read": {"file_path": "path"},
    "Write": {"file_path": "path"},
    "Edit": {"file_path": "path", "old_string": "oldString", "new_string": "newString"},
}
ARG_RENAME_FROM_OPENCODE = {
    "read": {"path": "file_path"},
    "write": {"path": "file_path"},
    "edit": {"path": "file_path", "oldString": "old_string", "newString": "new_string"},
}


def canonical_tool_name(name):
    if not name:
        return name
    if name in TOOL_TO_OPENCODE:
        return name
    return TOOL_FROM_OPENCODE.get(name, name)


def to_opencode_tool_name(name):
    if name in TOOL_TO_OPENCODE:
        return TOOL_TO_OPENCODE[name]
    return name


def remap_args(args, mapping):
    if not args or not isinstance(args, dict):
        return args
    if not mapping:
        return dict(args)
    out = {}
    for k, v in args.items():
        out[mapping.get(k, k)] = v
    return out


def canonical_args(name, args):
    return remap_args(args, ARG_RENAME_FROM_OPENCODE.get(name))


def opencode_args(name, args):
    return remap_args(args, ARG_RENAME_TO_OPENCODE.get(name))


# --------------------------------------------------------------------------
# Modelo canónico
# --------------------------------------------------------------------------

def make_session(cwd, title=None, model=None):
    t = now_ms()
    return {
        "id": None,
        "title": title or "Imported session",
        "cwd": cwd,
        "model": model,
        "created_at": t,
        "updated_at": t,
        "messages": [],
    }


def add_message(session, role, timestamp, parts):
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    msg = {"role": role, "timestamp": timestamp or now_ms(), "parts": parts}
    session["messages"].append(msg)
    return msg


# Bloques canónicos
def p_text(text):
    return {"type": "text", "text": text or ""}


def p_reasoning(text):
    return {"type": "reasoning", "text": text or ""}


def p_tool_call(id, name, args, status="completed"):
    return {"type": "tool_call", "id": id, "name": canonical_tool_name(name),
            "args": args if isinstance(args, dict) else {}, "status": status}


def p_tool_result(call_id, name, output, is_error=False):
    return {"type": "tool_result", "call_id": call_id, "name": canonical_tool_name(name),
            "output": output or "", "is_error": bool(is_error)}


# --------------------------------------------------------------------------
# Localización de sesiones
# --------------------------------------------------------------------------

def find_session_file(tool, ref):
    if os.path.isfile(ref):
        return ref
    root = QODER_ROOT if tool == "qoder" else CODEBUDDY_ROOT
    if not os.path.isdir(root):
        return None
    pattern = os.path.join(root, "*", ref + ".jsonl")
    matches = glob.glob(pattern)
    if matches:
        return sorted(matches, key=os.path.getmtime, reverse=True)[0]
    # por ruta parcial del id
    pattern2 = os.path.join(root, "*", "*" + os.path.basename(ref) + ".jsonl")
    matches2 = glob.glob(pattern2)
    return sorted(matches2, key=os.path.getmtime, reverse=True)[0] if matches2 else None


def title_from_file(tool, path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "ai-title":
                    return o.get("aiTitle")
        # fallback: primer texto de usuario
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if tool == "qoder":
                    m = o.get("message")
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str) and not c.startswith("<"):
                            return c[:80]
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    return b.get("text", "")[:80]
                else:
                    if o.get("type") == "message" and o.get("role") == "user":
                        c = o.get("content")
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "input_text":
                                    return b.get("text", "")[:80]
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# LECTURA: Qoder
# --------------------------------------------------------------------------

def read_qoder(path):
    cwd = None
    title = None
    messages = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "ai-title":
                title = o.get("aiTitle")
                continue
            if t in ("workspace-directories", "runtime-config", "worktree-state",
                     "active-leaf", "attachment", "file-history-snapshot",
                     "last-prompt"):
                if o.get("directories"):
                    cwd = cwd or (o["directories"][0] if o["directories"] else cwd)
                continue
            if t == "user":
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if o.get("isMeta") or (isinstance(content, str) and "<local-command-caveat>" in content):
                    continue
                parts = []
                if isinstance(content, str):
                    if content.strip():
                        parts.append(p_text(content))
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text" and b.get("text"):
                            parts.append(p_text(b["text"]))
                        elif bt == "tool_result":
                            out = b.get("content")
                            if isinstance(out, list):
                                out = "\n".join(
                                    x.get("text", "") for x in out if isinstance(x, dict)
                                )
                            parts.append(p_tool_result(
                                b.get("tool_use_id"), b.get("name"), out,
                                is_error=(b.get("is_error"))))
                if parts:
                    messages.append(("user", o.get("cwd"), from_iso(o.get("timestamp")) if o.get("timestamp") else now_ms(), parts))
                    cwd = cwd or o.get("cwd")
            elif t == "assistant":
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                parts = []
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "thinking":
                            parts.append(p_reasoning(b.get("thinking")))
                        elif bt == "text" and b.get("text"):
                            parts.append(p_text(b["text"]))
                        elif bt == "tool_use":
                            parts.append(p_tool_call(
                                b.get("id"), b.get("name"), b.get("input"), "completed"))
                if parts:
                    messages.append(("assistant", o.get("cwd"), from_iso(o.get("timestamp")) if o.get("timestamp") else now_ms(), parts))
                    cwd = cwd or o.get("cwd")
    sess = make_session(cwd or os.getcwd(), title)
    for role, c, ts, parts in messages:
        add_message(sess, role, ts, parts)
    sess["id"] = os.path.splitext(os.path.basename(path))[0]
    return sess


# --------------------------------------------------------------------------
# LECTURA: CodeBuddy
# --------------------------------------------------------------------------

def read_codebuddy(path):
    cwd = None
    title = None
    messages = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "ai-title":
                title = o.get("aiTitle")
                continue
            if t in ("file-history-snapshot", "turn-metrics", "session-meta",
                     "summary"):
                continue
            ts = o.get("timestamp") or now_ms()
            if t == "message" and o.get("role") == "user":
                c = o.get("content")
                parts = []
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "input_text":
                            parts.append(p_text(b.get("text", "")))
                if parts:
                    messages.append(("user", o.get("cwd"), ts, parts))
                    cwd = cwd or o.get("cwd")
            elif t == "message" and o.get("role") == "assistant":
                c = o.get("content")
                parts = []
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict):
                            if b.get("type") == "output_text" and b.get("text"):
                                parts.append(p_text(b["text"]))
                if parts:
                    messages.append(("assistant", o.get("cwd"), ts, parts))
                    cwd = cwd or o.get("cwd")
            elif t == "reasoning":
                parts = []
                rc = o.get("rawContent") or []
                txt = "".join(x.get("text", "") for x in rc if isinstance(x, dict))
                if not txt and o.get("content"):
                    cc = o.get("content")
                    if isinstance(cc, list):
                        txt = "".join(x.get("text", "") for x in cc if isinstance(x, dict))
                if txt:
                    messages.append(("assistant", o.get("cwd"), ts, [p_reasoning(txt)]))
                    cwd = cwd or o.get("cwd")
            elif t == "function_call":
                parts = [p_tool_call(o.get("callId") or o.get("id"), o.get("name"),
                                    _safe_json(o.get("arguments")), "completed")]
                messages.append(("assistant", o.get("cwd"), ts, parts))
                cwd = cwd or o.get("cwd")
            elif t == "function_call_result":
                out = o.get("output")
                text = ""
                if isinstance(out, dict):
                    text = out.get("text", "")
                elif isinstance(out, str):
                    text = out
                parts = [p_tool_result(o.get("callId") or o.get("id"), o.get("name"),
                                      text, is_error=(o.get("status") == "error"))]
                messages.append(("user", o.get("cwd"), ts, parts))
                cwd = cwd or o.get("cwd")
    sess = make_session(cwd or os.getcwd(), title)
    for role, c, ts, parts in messages:
        add_message(sess, role, ts, parts)
    sess["id"] = os.path.splitext(os.path.basename(path))[0]
    return sess


def _safe_json(s):
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            return {}
    return {}


# --------------------------------------------------------------------------
# LECTURA: OpenCode (vía CLI export)
# --------------------------------------------------------------------------

def read_opencode(ref, cwd=None):
    data = None
    if os.path.isfile(ref):
        with open(ref, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        cmd = ["opencode", "session", "export", ref]
        try:
            out = subprocess.run(cmd, cwd=cwd or os.getcwd(), capture_output=True,
                                 text=True, timeout=60)
        except Exception as e:
            sys.exit(f"error: no se pudo exportar sesión opencode: {e}")
        if out.returncode != 0:
            sys.exit(f"error: opencode session export falló:\n{out.stderr}")
        data = json.loads(out.stdout)
    info = data.get("info", {})
    messages = []
    for m in data.get("messages", []):
        mt = m.get("type")
        ts = (m.get("time") or {}).get("created") or now_ms()
        if mt == "user":
            txt = m.get("text", "")
            if txt:
                messages.append(("user", info.get("location", {}).get("directory"), ts, [p_text(txt)]))
        elif mt == "assistant":
            parts = []
            for c in m.get("content", []):
                ct = c.get("type")
                if ct == "reasoning":
                    parts.append(p_reasoning(c.get("text")))
                elif ct == "text":
                    parts.append(p_text(c.get("text")))
                elif ct == "tool":
                    name = c.get("name")
                    inp = (c.get("state") or {}).get("input") or {}
                    parts.append(p_tool_call(c.get("id"), name, inp, "completed"))
                    st = c.get("state") or {}
                    content = st.get("content") or []
                    out_txt = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    if st.get("status") == "error":
                        err = st.get("error") or {}
                        out_txt = err.get("message", "") or out_txt
                    if out_txt or st.get("status") == "error":
                        parts.append(p_tool_result(c.get("id"), name, out_txt,
                                                  is_error=(st.get("status") == "error")))
            if parts:
                messages.append(("assistant", info.get("location", {}).get("directory"), ts, parts))
    sess = make_session(info.get("location", {}).get("directory") or os.getcwd(),
                        info.get("title"))
    model = (info.get("model") or {}).get("id")
    sess["model"] = model
    sess["created_at"] = (info.get("time") or {}).get("created") or sess["created_at"]
    sess["updated_at"] = (info.get("time") or {}).get("updated") or sess["updated_at"]
    for role, c, ts, parts in messages:
        add_message(sess, role, ts, parts)
    sess["id"] = info.get("id") or ref
    return sess


# --------------------------------------------------------------------------
# ESCRITURA: Qoder
# --------------------------------------------------------------------------

def write_qoder(sess, cwd, out_path=None, dry_run=False):
    sid = new_id_qoder()
    target_cwd = cwd or sess["cwd"] or os.getcwd()
    if out_path is None:
        d = os.path.join(QODER_ROOT, slug_qoder(target_cwd))
        out_path = os.path.join(d, sid + ".jsonl")
    if os.path.exists(out_path):
        sys.exit(f"error: el destino ya existe: {out_path} (no se sobreescribe)")
    lines = []
    lines.append({"type": "workspace-directories", "sessionId": sid,
                  "directories": [target_cwd]})
    lines.append({"type": "runtime-config", "sessionId": sid, "model": sess["model"] or "qfmodel",
                  "reasoningEffort": None, "contextWindow": 1000000, "generation": None,
                  "timestamp": now_ms()})
    lines.append({"type": "worktree-state", "sessionId": sid, "worktreeSession": None})
    if sess["title"]:
        lines.append({"type": "ai-title", "sessionId": sid, "aiTitle": sess["title"]})

    prev_uuid = None
    last_assistant_uuid = None
    for msg in sess["messages"]:
        role = msg["role"]
        ts = msg["timestamp"]
        if role == "assistant":
            uuid_ = new_msg_id()
            content = []
            for p in msg["parts"]:
                if p["type"] == "reasoning":
                    content.append({"type": "thinking", "thinking": p["text"], "signature": ""})
                elif p["type"] == "text":
                    content.append({"type": "text", "text": p["text"]})
                elif p["type"] == "tool_call":
                    content.append({"type": "tool_use", "id": p["id"], "name": p["name"],
                                    "input": p["args"]})
            line = {"type": "assistant", "uuid": uuid_, "timestamp": to_iso(ts),
                    "message": {"id": "chatcmpl-" + uuid_.replace("-", "")[:24],
                                "type": "message", "role": "assistant",
                                "model": sess["model"] or "qfmodel", "stop_reason": None,
                                "stop_sequence": None, "content": content},
                    "parentUuid": prev_uuid, "isSidechain": False,
                    "cwd": target_cwd, "sessionId": sid, "userType": "external",
                    "entrypoint": "cli", "version": "1.1.85"}
            lines.append(line)
            prev_uuid = uuid_
            last_assistant_uuid = uuid_
            # OpenCode embebe tool_result dentro del assistant: sacarlo a un user message
            _tr = [p for p in msg["parts"] if p["type"] == "tool_result"]
            for tr in _tr:
                r_uuid = new_msg_id()
                lines.append({
                    "type": "user", "uuid": r_uuid, "timestamp": to_iso(ts),
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tr["call_id"], "content": tr["output"]}]},
                    "sourceToolAssistantUUID": uuid_,
                    "parentUuid": prev_uuid, "isSidechain": False,
                    "cwd": target_cwd, "sessionId": sid, "userType": "external",
                    "entrypoint": "cli", "version": "1.1.85",
                    "toolUseResult": {"kind": "error" if tr["is_error"] else "completed",
                                      "stdout": tr["output"]}})
                prev_uuid = r_uuid
        else:  # user
            uuid_ = new_msg_id()
            content = []
            tool_results = []
            for p in msg["parts"]:
                if p["type"] == "text":
                    content.append(p["text"])
                elif p["type"] == "tool_result":
                    tool_results.append(p)
            # texto
            if content:
                text = "\n".join(content)
                line = {"type": "user", "uuid": uuid_, "timestamp": to_iso(ts),
                        "message": {"role": "user", "content": text},
                        "parentUuid": prev_uuid, "isSidechain": False,
                        "cwd": target_cwd, "sessionId": sid, "userType": "external",
                        "entrypoint": "cli", "version": "1.1.85"}
                lines.append(line)
                prev_uuid = uuid_
            # resultados de tool
            for tr in tool_results:
                r_uuid = new_msg_id()
                content_blocks = [{"type": "tool_result", "tool_use_id": tr["call_id"],
                                  "content": tr["output"]}]
                line = {"type": "user", "uuid": r_uuid, "timestamp": to_iso(ts),
                        "message": {"role": "user", "content": content_blocks},
                        "sourceToolAssistantUUID": last_assistant_uuid,
                        "parentUuid": prev_uuid, "isSidechain": False,
                        "cwd": target_cwd, "sessionId": sid, "userType": "external",
                        "entrypoint": "cli", "version": "1.1.85",
                        "toolUseResult": {"kind": "error" if tr["is_error"] else "completed",
                                          "stdout": tr["output"]}}
                lines.append(line)
                prev_uuid = r_uuid
    lines.append({"type": "active-leaf", "sessionId": sid, "leafUuid": prev_uuid,
                  "explicit": False, "timestamp": now_ms()})

    text = "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n"
    if dry_run:
        sys.stdout.write(text)
        return text, sid
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return out_path, sid


# --------------------------------------------------------------------------
# ESCRITURA: CodeBuddy
# --------------------------------------------------------------------------

def write_codebuddy(sess, cwd, out_path=None, dry_run=False):
    sid = new_id_codebuddy()
    target_cwd = cwd or sess["cwd"] or os.getcwd()
    if out_path is None:
        d = os.path.join(CODEBUDDY_ROOT, slug_codebuddy(target_cwd))
        out_path = os.path.join(d, sid + ".jsonl")
    if os.path.exists(out_path):
        sys.exit(f"error: el destino ya existe: {out_path} (no se sobreescribe)")

    lines = []
    step = {"n": 0}
    prev_id = None

    def nid():
        return new_msg_id()

    def add(line):
        nonlocal prev_id
        line["id"] = line.get("id") or nid()
        line["parentId"] = prev_id
        line["timestamp"] = line.get("timestamp") or now_ms()
        line["sessionId"] = sid
        line["cwd"] = target_cwd
        line.setdefault("_meta", {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
                                  "baggage": f"codebuddy.session_id={sid}"})
        lines.append(line)
        prev_id = line["id"]

    if sess["title"]:
        add({"type": "ai-title", "aiTitle": sess["title"]})

    for msg in sess["messages"]:
        role = msg["role"]
        ts = msg["timestamp"]
        if role == "assistant":
            for p in msg["parts"]:
                if p["type"] == "reasoning":
                    step["n"] += 1
                    add({"type": "reasoning",
                         "providerData": {"agent": "cli", "stepSeq": step["n"],
                                         "model": sess["model"] or "deepseek-v4.1-flash"},
                         "content": [], "rawContent": [{"type": "reasoning_text", "text": p["text"]}]})
                elif p["type"] == "tool_call":
                    step["n"] += 1
                    add({"type": "function_call",
                         "providerData": {"agent": "cli", "stepSeq": step["n"],
                                         "model": sess["model"] or "deepseek-v4.1-flash"},
                         "callId": p["id"], "name": p["name"],
                         "arguments": json.dumps(p["args"], ensure_ascii=False)})
                elif p["type"] == "text":
                    step["n"] += 1
                    add({"type": "message", "role": "assistant", "status": "completed",
                         "providerData": {"agent": "cli", "stepSeq": step["n"],
                                         "model": sess["model"] or "deepseek-v4.1-flash"},
                         "content": [{"type": "output_text", "text": p["text"]}]})
                elif p["type"] == "tool_result":
                    # OpenCode embebe tool_result en el assistant
                    add({"type": "function_call_result", "name": p["name"], "callId": p["call_id"],
                         "status": "error" if p["is_error"] else "completed",
                         "output": {"type": "text", "text": p["output"]}})
        else:  # user
            texts = [p["text"] for p in msg["parts"] if p["type"] == "text"]
            results = [p for p in msg["parts"] if p["type"] == "tool_result"]
            for txt in texts:
                add({"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": txt}]})
            for tr in results:
                add({"type": "function_call_result", "name": tr["name"], "callId": tr["call_id"],
                     "status": "error" if tr["is_error"] else "completed",
                     "output": {"type": "text", "text": tr["output"]}})

    text = "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n"
    if dry_run:
        sys.stdout.write(text)
        return text, sid
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return out_path, sid


# --------------------------------------------------------------------------
# ESCRITURA: OpenCode (vía CLI import)
# --------------------------------------------------------------------------

def _opencode_msg_id():
    # OpenCode usa ids con prefijo msg_ + base62.
    return "msg_" + "".join(secrets.choice(_B62) for _ in range(26))


def _attach_tool_result(tool_parts, part, ts):
    """Incrusta un tool_result en el tool_call correspondiente (OpenCode los une)."""
    c = tool_parts.get(part["call_id"])
    if c is None:
        return False
    if part["is_error"]:
        # OpenCode valida un objeto `error` distinto para los fallos.
        c["state"]["status"] = "error"
        c["state"].pop("content", None)
        c["state"].pop("metadata", None)
        c["state"]["error"] = {"type": "tool.execution",
                               "message": part["output"] or "tool failed"}
    else:
        c["state"]["status"] = "completed"
        c["state"]["content"] = (
            [{"type": "text", "text": part["output"]}] if part["output"] else [])
        c["state"]["metadata"] = {"status": "completed", "truncated": False,
                                  "exit": 0}
    c["time"]["completed"] = ts
    return True


def build_opencode_export(sess, cwd=None):
    messages = []
    tool_parts = {}
    target_cwd = cwd or sess["cwd"] or os.getcwd()
    for msg in sess["messages"]:
        role = msg["role"]
        ts = msg["timestamp"]
        if role == "user":
            # Los tool_result de Qoder/CodeBuddy llegan en mensajes de usuario:
            # se incrustan en el tool_call del assistant anterior.
            for p in msg["parts"]:
                if p["type"] == "tool_result":
                    _attach_tool_result(tool_parts, p, ts)
            texts = [p["text"] for p in msg["parts"] if p["type"] == "text"]
            if not texts:
                continue
            messages.append({"id": _opencode_msg_id(), "time": {"created": ts},
                             "text": "\n".join(texts), "files": [], "agents": [],
                             "type": "user"})
        else:  # assistant
            content = []
            for p in msg["parts"]:
                if p["type"] == "reasoning":
                    content.append({"type": "reasoning", "text": p["text"],
                                    "native": {"reasoningField": "reasoning_content"},
                                    "time": {"created": ts, "completed": ts}})
                elif p["type"] == "text":
                    content.append({"type": "text", "text": p["text"]})
                elif p["type"] == "tool_call":
                    c = {"type": "tool", "id": p["id"],
                         "name": to_opencode_tool_name(p["name"]),
                         "executed": False,
                         "state": {"status": "completed",
                                   "input": opencode_args(p["name"], p["args"]),
                                   "content": []},
                         "time": {"created": ts, "ran": ts, "completed": ts}}
                    content.append(c)
                    tool_parts[p["id"]] = c
                elif p["type"] == "tool_result":
                    _attach_tool_result(tool_parts, p, ts)
            if not content:
                continue
            messages.append({"id": _opencode_msg_id(), "time": {"created": ts, "completed": ts},
                             "type": "assistant", "agent": "build",
                             "model": {"id": sess["model"] or "qfmodel",
                                       "providerID": "opencode-go"},
                             "content": content, "finish": "stop", "rawFinish": "stop",
                             "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                                    "cache": {"read": 0, "write": 0}}})
    export = {
        "info": {
            # OpenCode exige un id nuevo y un projectID no vacío; al importar
            # con --directory resuelve/crea el proyecto real del directorio.
            "id": new_id_opencode(),
            "projectID": hashlib.sha1(target_cwd.encode("utf-8")).hexdigest(),
            "agent": "build",
            "model": {"id": sess["model"] or "qfmodel", "providerID": "opencode-go"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
            "outcome": "succeeded",
            "time": {"created": sess["created_at"], "updated": sess["updated_at"]},
            "title": sess["title"],
            "location": {"directory": target_cwd},
        },
        "messages": messages,
    }
    return export


def write_opencode(sess, cwd, dry_run=False):
    target_cwd = cwd or sess["cwd"] or os.getcwd()
    export = build_opencode_export(sess, target_cwd)
    if dry_run:
        sys.stdout.write(json.dumps(export, ensure_ascii=False, indent=2))
        return None, None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(export, tf, ensure_ascii=False)
        tmp = tf.name
    try:
        cmd = ["opencode", "session", "import", tmp, "--directory", target_cwd]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    if out.returncode != 0:
        sys.exit(f"error: opencode session import falló:\n{out.stderr}\n{out.stdout}")
    m = re.search(r"(ses_[A-Za-z0-9]+)", out.stdout + out.stderr)
    new_id = m.group(1) if m else None
    return None, new_id


# --------------------------------------------------------------------------
# Dispatcher de lectura/escritura
# --------------------------------------------------------------------------

def read_session(tool, ref, cwd=None):
    if tool == "qoder":
        path = find_session_file("qoder", ref)
        if not path:
            sys.exit(f"error: no se encontró la sesión qoder '{ref}'")
        return read_qoder(path)
    if tool == "codebuddy":
        path = find_session_file("codebuddy", ref)
        if not path:
            sys.exit(f"error: no se encontró la sesión codebuddy '{ref}'")
        return read_codebuddy(path)
    if tool == "opencode":
        return read_opencode(ref, cwd)
    sys.exit("error: tool de origen no válido")


def write_session(tool, sess, cwd, dry_run=False):
    if tool == "qoder":
        return write_qoder(sess, cwd, dry_run=dry_run)
    if tool == "codebuddy":
        return write_codebuddy(sess, cwd, dry_run=dry_run)
    if tool == "opencode":
        return write_opencode(sess, cwd, dry_run=dry_run)
    sys.exit("error: tool de destino no válido")


# --------------------------------------------------------------------------
# Comandos CLI
# --------------------------------------------------------------------------

def infer_tool(ref):
    b = os.path.basename(ref)
    if b.startswith("ses_"):
        return "opencode"
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-", b):
        return "codebuddy"
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-", b):
        return "qoder"
    if ref.endswith(".json"):
        return "opencode"
    if ref.endswith(".jsonl"):
        return None  # ambiguo: requiere --from
    return None


def cmd_list(args):
    tool = args.from_tool
    cwd = args.cwd or os.getcwd()
    if tool == "opencode":
        out = subprocess.run(["opencode", "session", "list", "--format", "json"],
                             cwd=cwd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            sys.exit(f"error: opencode session list falló:\n{out.stderr}")
        rows = json.loads(out.stdout)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"  {r['id']}  {r.get('title','')}  ({r.get('updated')})")
        return
    root = QODER_ROOT if tool == "qoder" else CODEBUDDY_ROOT
    if not os.path.isdir(root):
        print("[]")
        return
    rows = []
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        c = title_from_file(tool, path)
        rows.append({"id": os.path.splitext(os.path.basename(path))[0],
                     "title": c, "path": path})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in sorted(rows, key=lambda x: x["path"], reverse=True):
            print(f"  {r['id']}  {r['title'] or '(sin título)'}")


def cmd_inspect(args):
    tool = args.from_tool or infer_tool(args.ref)
    if not tool:
        sys.exit("error: especifica --from {qoder|codebuddy|opencode}")
    sess = read_session(tool, args.ref, args.cwd)
    summary = {
        "id": sess["id"],
        "title": sess["title"],
        "cwd": sess["cwd"],
        "model": sess["model"],
        "num_messages": len(sess["messages"]),
        "user": sum(1 for m in sess["messages"] if m["role"] == "user"),
        "assistant": sum(1 for m in sess["messages"] if m["role"] == "assistant"),
        "tool_calls": sum(1 for m in sess["messages"] for p in m["parts"] if p["type"] == "tool_call"),
        "tool_results": sum(1 for m in sess["messages"] for p in m["parts"] if p["type"] == "tool_result"),
        "messages": [
            {"role": m["role"],
             "parts": [{"type": p["type"], **({k: p[k] for k in ("name", "id", "call_id", "is_error") if k in p})}
                       for p in m["parts"]]}
            for m in sess["messages"]
        ],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"id: {summary['id']}")
        print(f"título: {summary['title']}")
        print(f"cwd: {summary['cwd']}")
        print(f"modelo: {summary['model']}")
        print(f"mensajes: {summary['num_messages']} (user={summary['user']}, assistant={summary['assistant']})")
        print(f"tool calls: {summary['tool_calls']}  tool results: {summary['tool_results']}")


def cmd_convert(args):
    from_tool = args.from_tool or infer_tool(args.ref)
    if not from_tool:
        sys.exit("error: no se pudo inferir el formato de origen; usa --from")
    if args.from_tool is None and from_tool == "opencode" and not (args.ref.endswith(".json") or os.path.isfile(args.ref)):
        # id opencode: ok
        pass
    sess = read_session(from_tool, args.ref, args.cwd)
    if args.title:
        sess["title"] = args.title
    elif not sess["title"]:
        sess["title"] = "imported"
    if args.cwd:
        sess["cwd"] = args.cwd
    path, new_id = write_session(args.to_tool, sess, args.cwd, dry_run=args.dry_run)
    if args.dry_run:
        return
    if args.to_tool == "opencode":
        print(f"Convertido ({from_tool} → opencode). Nueva sesión: {new_id}")
        print(f"  Reanudar: opencode --session {new_id}")
    else:
        print(f"Convertido ({from_tool} → {args.to_tool}). Archivo: {path}")
        print(f"  Reanudar: {args.to_tool} --resume {new_id}")


def cmd_roundtrip(args):
    import contextlib
    import io
    from_tool = args.from_tool or infer_tool(args.ref)
    sess = read_session(from_tool, args.ref, args.cwd)
    # emitir en via y volver a leer el string (sin volcar el jsonl a stdout)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if args.via == "qoder":
            text, _ = write_qoder(sess, sess["cwd"], dry_run=True)
        elif args.via == "codebuddy":
            text, _ = write_codebuddy(sess, sess["cwd"], dry_run=True)
        else:
            sys.exit("roundtrip vía opencode requiere import real; usa convert --dry-run")
    if args.via == "qoder":
        reread = read_qoder_str(text)
    else:
        reread = read_codebuddy_str(text)

    def counter(s):
        c = {"text": 0, "reasoning": 0, "tool_call": 0, "tool_result": 0}
        for m in s["messages"]:
            for p in m["parts"]:
                c[p["type"]] = c.get(p["type"], 0) + 1
        return c

    oc = counter(sess)
    nc = counter(reread)
    print(f"origen:    {oc}")
    print(f"{args.via}: {nc}")
    print("OK" if oc == nc else "DIFERENCIA en partes")


def read_qoder_str(text):
    import io
    lines = [l for l in text.splitlines() if l.strip()]
    # reusar lógica: escribir a temp y leer
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as tf:
        tf.write(text); p = tf.name
    try:
        return read_qoder(p)
    finally:
        os.remove(p)


def read_codebuddy_str(text):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as tf:
        tf.write(text); p = tf.name
    try:
        return read_codebuddy(p)
    finally:
        os.remove(p)


def cmd_selftest(args):
    sess = make_session(os.getcwd(), "SESSCONV-TEST fixture")
    add_message(sess, "user", now_ms(), [p_text("Hola, crea un archivo de prueba")])
    add_message(sess, "assistant", now_ms(), [
        p_reasoning("Voy a crear el archivo."),
        p_tool_call("call_test1", "Write", {"file_path": "/tmp/x.txt", "content": "hola"}),
        p_text("Listo, creé el archivo."),
    ])
    add_message(sess, "user", now_ms(), [
        p_tool_result("call_test1", "Write", "File written: /tmp/x.txt"),
        p_text("Gracias, ahora lee lo que escribiste."),
    ])
    add_message(sess, "assistant", now_ms(), [
        p_tool_call("call_test2", "Read", {"file_path": "/tmp/x.txt"}),
    ])
    add_message(sess, "user", now_ms(), [
        p_tool_result("call_test2", "Read", "hola"),
    ])
    print(f"fixture: {len(sess['messages'])} mensajes")
    import contextlib
    import io
    for via in ("qoder", "codebuddy"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            text, _ = (write_qoder if via == "qoder" else write_codebuddy)(sess, sess["cwd"], dry_run=True)
        reread = (read_qoder_str if via == "qoder" else read_codebuddy_str)(text)
        op = sum(len(m["parts"]) for m in sess["messages"])
        np_ = sum(len(m["parts"]) for m in reread["messages"])
        status = "OK" if op == np_ else "FALLO"
        print(f"  {via}: orig={op} reread={np_} -> {status}")
    # opencode build validity
    exp = build_opencode_export(sess)
    ok = (
        exp["info"]["id"].startswith("ses_")
        and bool(exp["info"].get("projectID"))
        and all(m["id"].startswith("msg_") for m in exp["messages"])
        and all("type" in m and ("content" in m or "text" in m) for m in exp["messages"])
    )
    print(f"  opencode: build OK={ok}, mensajes={len(exp['messages'])}")
    print("selftest completado")


# --------------------------------------------------------------------------
# Argumentos
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="sessconv",
        description="Traductor de sesiones Qoder ⇄ CodeBuddy ⇄ OpenCode (sin inferencia).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Listar sesiones de un tool")
    pl.add_argument("--from", dest="from_tool", choices=TOOLS, required=True)
    pl.add_argument("--cwd", default=None)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pi = sub.add_parser("inspect", help="Inspeccionar una sesión (modelo canónico)")
    pi.add_argument("ref")
    pi.add_argument("--from", dest="from_tool", choices=TOOLS, default=None)
    pi.add_argument("--cwd", default=None)
    pi.add_argument("--json", action="store_true")
    pi.set_defaults(func=cmd_inspect)

    pc = sub.add_parser("convert", help="Convertir una sesión entre formatos")
    pc.add_argument("ref")
    pc.add_argument("--from", dest="from_tool", choices=TOOLS, default=None)
    pc.add_argument("--to", dest="to_tool", choices=TOOLS, required=True)
    pc.add_argument("--cwd", default=None)
    pc.add_argument("--title", default=None)
    pc.add_argument("--dry-run", action="store_true")
    pc.add_argument("--print-id", action="store_true", dest="print_id")
    pc.set_defaults(func=cmd_convert)

    pr = sub.add_parser("roundtrip", help="Fidelidad en memoria (sin escribir)")
    pr.add_argument("ref")
    pr.add_argument("--from", dest="from_tool", choices=TOOLS, default=None)
    pr.add_argument("--via", choices=("qoder", "codebuddy", "opencode"), required=True)
    pr.add_argument("--cwd", default=None)
    pr.set_defaults(func=cmd_roundtrip)

    ps = sub.add_parser("selftest", help="Prueba interna con fixture")
    ps.set_defaults(func=cmd_selftest)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
