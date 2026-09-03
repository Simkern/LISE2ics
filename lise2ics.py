#!/usr/bin/env python3
"""
lise2ics.py - pull a teaching schedule out of lise.ensam.eu (Aurion) and write
an .ics file.

Adapted from Azuxul/LISE-ICS-agenda-generator, with three changes:
  * authenticated session taken from a copied cURL, so it works on your own
    teacher planning rather than the public group chooser
  * ViewState is re-read from each response and fed into the next request,
    which is what makes looping over many weeks possible
  * title parsing anchored on the HH:MM - HH:MM - NhNN triple instead of
    fixed field indices (your events have an empty intervenant field that
    shifts every index)

Setup:
  1. Log into LISE, open your planning, F12 -> Network -> Fetch/XHR
  2. Click a week arrow, right-click the POST to Planning.xhtml
  3. Copy -> Copy as cURL, save it as request.txt

Run:
  python3 lise2ics.py request.txt --from 2026-09-01 --to 2027-07-15
  python3 lise2ics.py request.txt --from 2026-09-01 --to 2027-07-15 \
      --group "FIP GE1" --out ge1.ics

Needs: pip install requests
Needs plan2ics.py in the same folder (it provides the parsing + ICS writer).
"""

import sys, os, re, json, html, time, argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plan2ics import parse_title, build_ics, extract_events   # noqa: E402

VIEWSTATE_RE = re.compile(
    r'<update id="[^"]*javax\.faces\.ViewState[^"]*">(.*?)</update>', re.S)


# --- cURL -------------------------------------------------------------------

def parse_curl(text):
    import shlex
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    tokens = shlex.split(text)
    url, headers, cookies, body = None, {}, {}, None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            k, _, v = tokens[i].partition(":")
            k, v = k.strip(), v.strip()
            if k.lower() == "cookie":
                for p in v.split(";"):
                    if "=" in p:
                        ck, cv = p.split("=", 1)
                        cookies[ck.strip()] = cv.strip()
            elif k.lower() not in ("content-length", "host"):
                headers[k] = v
        elif t in ("-b", "--cookie"):
            i += 1
            for p in tokens[i].split(";"):
                if "=" in p:
                    ck, cv = p.split("=", 1)
                    cookies[ck.strip()] = cv.strip()
        elif t in ("-d", "--data", "--data-raw", "--data-binary"):
            i += 1
            body = tokens[i]
        elif t in ("-X", "--request"):
            i += 1
        elif t.startswith("http"):
            url = t
        i += 1
    if not url or body is None:
        sys.exit("Could not read a URL and a POST body from that cURL file.")
    return url, headers, cookies, dict(parse_qsl(body, keep_blank_values=True))


# --- request shaping --------------------------------------------------------

def detect_component(params):
    for k in params:
        if k.endswith("_start"):
            return k[:-len("_start")]
    sys.exit("No *_start parameter in the request body - did you copy the "
             "planning POST, or some other request?")


def uses_millis(params, comp):
    v = params.get(f"{comp}_start", "")
    return v.isdigit() and len(v) >= 10


def stamp(d, millis):
    if not millis:
        return d.strftime("%Y-%m-%d")
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))


def periods(start, end, step):
    """Yield (from, to) chunks covering [start, end]."""
    cur = start
    while cur <= end:
        if step == "week":
            cur -= timedelta(days=cur.weekday())      # snap to Monday
            nxt = cur + timedelta(days=7)
        else:
            nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("curl_file")
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--out", default="planning.ics")
    ap.add_argument("--step", choices=["week", "month"], default="month",
                    help="request granularity; drop to week if month returns "
                         "nothing (default: month)")
    ap.add_argument("--view", default=None,
                    help="FullCalendar view sent to the server, e.g. month or "
                         "agendaWeek. Default: leave whatever the cURL had.")
    ap.add_argument("--group", action="append", default=[],
                    help="only keep events whose group contains this string; "
                         "repeatable")
    ap.add_argument("--teacher", action="append", default=[],
                    help="only keep events whose intervenant contains this "
                         "string, e.g. your surname; repeatable")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--calname", default="Enseignement")
    ap.add_argument("--dump", metavar="DIR",
                    help="also save each raw response here")
    args = ap.parse_args()

    d_from = datetime.strptime(args.d_from, "%Y-%m-%d").date()
    d_to = datetime.strptime(args.d_to, "%Y-%m-%d").date()

    url, headers, cookies, params = parse_curl(
        open(args.curl_file, encoding="utf-8").read())
    comp = detect_component(params)
    millis = uses_millis(params, comp)
    headers.setdefault("Content-Type",
                       "application/x-www-form-urlencoded; charset=UTF-8")
    headers.setdefault("Faces-Request", "partial/ajax")
    headers.setdefault("X-Requested-With", "XMLHttpRequest")

    print(f"component : {comp}", file=sys.stderr)
    print(f"date format: {'epoch millis' if millis else 'ISO'}", file=sys.stderr)

    if args.dump:
        os.makedirs(args.dump, exist_ok=True)

    session = requests.Session()
    session.cookies.update(cookies)

    seen, events = set(), []
    for a, b in periods(d_from, d_to, args.step):
        params[f"{comp}_start"] = stamp(a, millis)
        params[f"{comp}_end"] = stamp(b + timedelta(days=1), millis)
        # extras the LISE planning page sends alongside the range
        if "form:date_input" in params:
            params["form:date_input"] = a.strftime("%d/%m/%Y")
        if "form:week" in params:
            iso = a.isocalendar()
            params["form:week"] = f"{iso[1]}-{iso[0]}"
        if args.view and f"{comp}_view" in params:
            params[f"{comp}_view"] = args.view

        r = session.post(url, data=params, headers=headers, timeout=60)
        r.encoding = "utf-8"
        text = r.text

        if r.status_code != 200:
            sys.exit(f"HTTP {r.status_code} on {a} - session likely expired.")
        if "ViewExpiredException" in text or "ViewState could not be restored" in text:
            sys.exit(f"ViewState expired at {a}. Copy a fresh cURL and re-run; "
                     f"if it dies every time, raise --delay.")
        if "j_username" in text:
            sys.exit("Got the login page back - your JSESSIONID is stale.")

        if args.dump:
            with open(os.path.join(args.dump, f"{a}.xml"), "w",
                      encoding="utf-8") as f:
                f.write(text)

        # feed the rotated ViewState into the next request
        m = VIEWSTATE_RE.search(text)
        if m:
            vs = html.unescape(m.group(1).strip())
            if vs.startswith("<![CDATA["):
                vs = vs[9:-3].strip()
            params["javax.faces.ViewState"] = vs

        found = extract_events(text)
        new = 0
        for ev in found:
            key = ev.get("id") or json.dumps(ev, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
            new += 1
        print(f"  {a} -> {b}: {len(found):4d} events, {new:4d} new",
              file=sys.stderr)
        time.sleep(args.delay)

    def meta_of(e):
        return parse_title(e.get("title", ""), e.get("className"))

    if args.group:
        before = len(events)
        events = [e for e in events
                  if any(g.lower() in meta_of(e)["group"].lower()
                         for g in args.group)]
        print(f"group filter : kept {len(events)} of {before}", file=sys.stderr)

    if args.teacher:
        before = len(events)
        events = [e for e in events
                  if any(t.lower() in meta_of(e)["teacher"].lower()
                         for t in args.teacher)]
        print(f"teacher filter: kept {len(events)} of {before}", file=sys.stderr)

    if not events:
        sys.exit("No events. Try --step week, or check the date range.")

    events.sort(key=lambda e: e["start"])
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(events, calname=args.calname))
    print(f"\nWrote {len(events)} events to {args.out}  "
          f"({events[0]['start'][:10]} -> {events[-1]['start'][:10]})",
          file=sys.stderr)

    subjects, teachers = {}, {}
    for e in events:
        m = meta_of(e)
        subjects[m["subject"] or "?"] = subjects.get(m["subject"] or "?", 0) + 1
        teachers[m["teacher"] or "(none)"] = teachers.get(m["teacher"] or "(none)", 0) + 1
    print("\nBy subject:", file=sys.stderr)
    for s, n in sorted(subjects.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {s}", file=sys.stderr)
    print("\nBy intervenant:", file=sys.stderr)
    for s, n in sorted(teachers.items(), key=lambda x: -x[1])[:15]:
        print(f"  {n:4d}  {s}", file=sys.stderr)


if __name__ == "__main__":
    main()