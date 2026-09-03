#!/usr/bin/env python3
"""
plan2ics.py - turn a PrimeFaces Schedule feed (Planning.xhtml) into an .ics file.

Usage:
    python3 plan2ics.py planning.ics capture1.xml capture2.xml ...
    python3 plan2ics.py planning.ics events.json
    cat capture.xml | python3 plan2ics.py planning.ics -

Accepts either raw JSON ({"events": [...]}) or a full JSF <partial-response>
XML blob; it finds the events payload inside either. Multiple files are merged
and de-duplicated on the event UUID, so overlapping week captures are fine.

Stdlib only. No dependencies.
"""

import sys, os, re, json, html
from datetime import datetime, timezone

# --- title parsing -----------------------------------------------------------
# Observed layout, " - " separated (some fields may be empty):
#   code - subject - type - teacher - room - HH:MM - HH:MM - NhNN - label - group
# We anchor on the "HH:MM - HH:MM - NhNN" triple so that a subject containing
# a hyphen (e.g. "Anglais - LV2") doesn't shift every other field.

SEP = re.compile(r"(?<=\s)-(?=\s)")      # a dash with whitespace on both sides
HHMM = re.compile(r"^\d{1,2}:\d{2}$")
DUR = re.compile(r"^\d+h\d{2}$")

TYPE_LABEL = {          # cosmetic; extend to taste
    "CM": "CM",
    "ED_TD": "TD",
    "ED_TP": "TP",
    "TP": "TP",
    "EXAM": "Examen",
}


def parse_title(title, class_name=None):
    """Return dict with code/subject/type/teacher/room/label/group (best effort).

    Two anchors keep this stable when fields contain hyphens or are empty:
      * the "HH:MM - HH:MM - NhNN" triple marks the middle of the string
      * class_name (the JSON className, e.g. "ED_TD") identifies the type
        field exactly, so a multi-word or hyphenated intervenant can't shift
        the subject. Falls back to counting back from the anchor if the
        className isn't present in the title.
    """
    parts = [p.strip() for p in SEP.split(title)]
    out = {"code": "", "subject": "", "type": "", "teacher": "",
           "room": "", "label": "", "group": "", "raw": title}

    anchor = None
    for i in range(len(parts) - 2):
        if HHMM.match(parts[i]) and HHMM.match(parts[i + 1]) and DUR.match(parts[i + 2]):
            anchor = i
            break

    if anchor is None or anchor < 4:
        # Unexpected shape: keep the whole string as the subject rather than
        # silently mangling it.
        out["subject"] = title
        return out

    out["code"] = parts[0]
    out["room"] = parts[anchor - 1]

    # locate the type field
    j = None
    if class_name:
        for k in range(1, anchor - 1):
            if parts[k] == class_name:
                j = k
                break
    if j is None:
        j = anchor - 3                      # positional fallback

    out["subject"] = " - ".join(parts[1:j]).strip()
    out["type"] = parts[j]
    out["teacher"] = " - ".join(parts[j + 1:anchor - 1]).strip()

    tail = parts[anchor + 3:]
    if tail:
        out["label"] = tail[0]
    if len(tail) > 1:
        out["group"] = " - ".join(tail[1:]).strip()
    return out


def summary_for(ev, meta):
    kind = TYPE_LABEL.get(meta["type"], meta["type"])
    bits = [b for b in (meta["subject"], kind) if b]
    s = " – ".join(bits) if bits else meta["raw"]
    if meta["group"]:
        s += f" ({meta['group']})"
    return s


def description_for(meta):
    lines = []
    for k, lbl in (("code", "Code"), ("label", "Intitulé"), ("group", "Groupe"),
                   ("teacher", "Intervenant"), ("type", "Type")):
        if meta[k]:
            lines.append(f"{lbl}: {meta[k]}")
    return "\n".join(lines)


# --- input extraction --------------------------------------------------------

UPDATE = re.compile(r"<update\b[^>]*>(.*?)</update>", re.S)


def extract_events(text):
    """Pull the events array out of raw JSON or a JSF partial-response."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text).get("events", [])

    events = []
    for body in UPDATE.findall(text):
        body = body.strip()
        if body.startswith("<![CDATA["):
            body = body[9:-3].strip()
        if not body.startswith("{"):
            continue
        try:
            payload = json.loads(html.unescape(body))
        except json.JSONDecodeError:
            continue
        events.extend(payload.get("events", []))
    return events


# --- ICS emission ------------------------------------------------------------

def esc(v):
    return (v.replace("\\", "\\\\").replace(";", r"\;")
             .replace(",", r"\,").replace("\n", r"\n"))


def fold(line):
    """RFC 5545 line folding, counted in octets, never splitting a UTF-8 char."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, start = [], 0
    limit = 75
    while start < len(b):
        end = min(start + limit, len(b))
        while end > start and end < len(b) and (b[end] & 0xC0) == 0x80:
            end -= 1
        out.append(b[start:end].decode("utf-8"))
        start = end
        limit = 74            # continuation lines carry a leading space
    return "\r\n ".join(out[:1] + [s for s in out[1:]])


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events, calname="Enseignement"):
    stamp = utc(datetime.now(timezone.utc))
    lines = ["BEGIN:VCALENDAR",
             "VERSION:2.0",
             "PRODID:-//plan2ics//Planning.xhtml//FR",
             "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH",
             f"X-WR-CALNAME:{esc(calname)}",
             "X-WR-TIMEZONE:Europe/Paris"]

    for ev in events:
        meta = parse_title(ev.get("title", ""), ev.get("className"))
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"])
        uid = ev.get("id") or f"{start:%Y%m%dT%H%M%S}-{abs(hash(ev.get('title','')))}"

        lines += ["BEGIN:VEVENT",
                  f"UID:{uid}@planning.local",
                  f"DTSTAMP:{stamp}",
                  f"DTSTART:{utc(start)}",
                  f"DTEND:{utc(end)}",
                  f"SUMMARY:{esc(summary_for(ev, meta))}"]
        if meta["room"]:
            lines.append(f"LOCATION:{esc(meta['room'])}")
        desc = description_for(meta)
        if desc:
            lines.append(f"DESCRIPTION:{esc(desc)}")
        if ev.get("className"):
            lines.append(f"CATEGORIES:{esc(ev['className'])}")
        lines.append("TRANSP:OPAQUE")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    out_path, sources = sys.argv[1], sys.argv[2:]

    seen, merged = set(), []
    for src in sources:
        text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
        found = extract_events(text)
        new = 0
        for ev in found:
            key = ev.get("id") or json.dumps(ev, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ev)
            new += 1
        print(f"  {src}: {len(found)} events, {new} new", file=sys.stderr)

    if not merged:
        print("No events found. Is the payload the right <update> block?", file=sys.stderr)
        sys.exit(2)

    merged.sort(key=lambda e: e["start"])
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(merged))

    span = f"{merged[0]['start'][:10]} → {merged[-1]['start'][:10]}"
    print(f"Wrote {len(merged)} events to {out_path}  ({span})", file=sys.stderr)


if __name__ == "__main__":
    main()