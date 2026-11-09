#!/usr/bin/env python3
# ats_probe.py
# 1) Reads each company's own website for links to an ATS (strongest evidence).
# 2) Probes candidate ATS boards (given, or generated slugs) on public endpoints.
# 3) Scores whether each board really belongs to the company.
#
# Endpoints are the ones Corda Scout reads in production (Nov 2026), except
# BambooHR and the EU Greenhouse/Lever API hosts, which Scout does not use (verify).
#
# Input CSV columns: company, website, candidates
#   candidates (optional): ';'-separated ats:code pairs, e.g.
#   greenhouse:fxpro;workday:acme|wd5|External;comeet:plus500|A1.00F
# Output: CSV on stdout, one row per board that answered.
#
#   pip install requests
#   python ats_probe.py companies.csv > boards.csv

import csv
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, unquote

import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (compatible; company-research/1.0)"
TIMEOUT = 20
PAUSE = 0.6  # seconds between requests; Workable, Join and Freshteam rate-limit hard


def get(url, **kw):
    time.sleep(PAUSE)
    try:
        return S.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)
    except requests.RequestException:
        return None


def post(url, payload, **kw):
    time.sleep(PAUSE)
    try:
        return S.post(url, json=payload, timeout=TIMEOUT, **kw)
    except requests.RequestException:
        return None


def jget(url):
    r = get(url, headers={"Accept": "application/json"})
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def host_of(url):
    h = urlparse(url if "://" in url else "https://" + url).hostname or ""
    return h.lower()[4:] if h.lower().startswith("www.") else h.lower()


def title_of(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def hit(ats, code, board_url, name="", jobs=0, titles=(), text="", note=""):
    return {"ats": ats, "code": code, "board_url": board_url, "board_name": name or "",
            "jobs": jobs, "sample_titles": " | ".join([t for t in titles if t][:5]),
            "text": text or "", "note": note}


# ------------------------------------------------------------------ probes

def p_greenhouse(code):
    for api, board in (("boards-api.greenhouse.io", "job-boards.greenhouse.io"),
                       ("boards-api.eu.greenhouse.io", "job-boards.eu.greenhouse.io")):  # EU API host: verify
        meta = jget(f"https://{api}/v1/boards/{code}")
        if not isinstance(meta, dict) or "name" not in meta:
            continue
        data = jget(f"https://{api}/v1/boards/{code}/jobs?content=true") or {}
        jobs = data.get("jobs", [])
        text = " ".join((j.get("content") or "") + " " + (j.get("absolute_url") or "") for j in jobs[:25])
        return hit("greenhouse", code, f"https://{board}/{code}", meta.get("name"), len(jobs),
                   [j.get("title", "") for j in jobs], text)
    return None


def p_lever(code):
    for api, board in (("api.lever.co", "jobs.lever.co"), ("api.eu.lever.co", "jobs.eu.lever.co")):  # EU API: verify
        data = jget(f"https://{api}/v0/postings/{code}?mode=json")
        if not isinstance(data, list):
            continue
        if not data:  # an empty list proves nothing; the board page must answer too
            page = get(f"https://{board}/{code}")
            if page is None or page.status_code != 200:
                continue
        text = " ".join((p.get("descriptionPlain") or "") + " " + (p.get("additionalPlain") or "") for p in data[:25])
        return hit("lever", code, f"https://{board}/{code}", "", len(data), [p.get("text", "") for p in data],
                   text, "Lever returns no company name: confirm by job text, email domain or a site link")
    return None


def p_ashby(code):
    data = jget(f"https://api.ashbyhq.com/posting-api/job-board/{code}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    jobs = [j for j in data["jobs"] if j.get("isListed") is not False]
    page = get(f"https://jobs.ashbyhq.com/{code}")
    name = title_of(page.text) if page is not None and page.status_code == 200 else ""
    text = " ".join((j.get("descriptionPlain") or "") for j in jobs[:25])
    return hit("ashby", code, f"https://jobs.ashbyhq.com/{code}", name, len(jobs),
               [j.get("title", "") for j in jobs], text, "name is the board page title")


def p_workable(code):
    data = jget(f"https://apply.workable.com/api/v1/widget/accounts/{code}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    jobs = data["jobs"]
    return hit("workable", code, f"https://apply.workable.com/{code}/", data.get("name"), len(jobs),
               [j.get("title", "") for j in jobs], " ".join(j.get("url", "") for j in jobs))


def p_smartrecruiters(code):
    data = jget(f"https://api.smartrecruiters.com/v1/companies/{code}/postings?limit=100")
    if not isinstance(data, dict):
        return None
    posts = data.get("content", [])
    if not posts:
        # Unknown codes answer 200 with an empty list. A real company keeps its
        # own page on careers.smartrecruiters.com; an unknown one is redirected away.
        r = get(f"https://careers.smartrecruiters.com/{code}")
        if r is None or r.status_code != 200 or host_of(r.url) != "careers.smartrecruiters.com":
            return None
    names = sorted({(p.get("company") or {}).get("name") for p in posts} - {None})
    return hit("smartrecruiters", code, f"https://careers.smartrecruiters.com/{code}", ", ".join(names),
               data.get("totalFound", len(posts)), [p.get("name", "") for p in posts])


def _xml_vals(xml, tag):
    return [re.sub(r"^<!\[CDATA\[|\]\]>$", "", v.strip())
            for v in re.findall(rf"<{tag}>(.*?)</{tag}>", xml, re.S)]


def p_personio(code):
    for tld in ("de", "com"):
        r = get(f"https://{code}.jobs.personio.{tld}/xml?language=en")
        if r is None or r.status_code != 200 or not re.search(r"<workzag-jobs|<position>", r.text):
            continue
        positions = re.findall(r"<position>(.*?)</position>", r.text, re.S)
        titles = [(_xml_vals(p, "name") or [""])[0] for p in positions]
        names = sorted(set(v for p in positions for v in _xml_vals(p, "subcompany") if v))
        return hit("personio", code, f"https://{code}.jobs.personio.{tld}", ", ".join(names),
                   len(positions), titles, r.text[:20000])
    return None


def p_teamtailor(code):
    data = jget(f"https://{code}.teamtailor.com/jobs.json")
    if not isinstance(data, dict) or "items" not in data:
        return None
    items = data["items"]
    names = sorted({((i.get("_jobposting") or {}).get("hiringOrganization") or {}).get("name")
                    for i in items} - {None})
    text = " ".join((i.get("content_html") or "") for i in items[:25])
    return hit("teamtailor", code, f"https://{code}.teamtailor.com/jobs",
               ", ".join(names) or data.get("title", ""), len(items), [i.get("title", "") for i in items], text)


def p_recruitee(code):
    data = jget(f"https://{code}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    offers = data["offers"]
    names = sorted({o.get("company_name") for o in offers} - {None})  # field name: verify
    text = " ".join((o.get("description") or "") for o in offers[:25])
    return hit("recruitee", code, f"https://{code}.recruitee.com/", ", ".join(names), len(offers),
               [o.get("title", "") for o in offers], text)


def p_breezy(code):
    r = get(f"https://{code}.breezy.hr/json", headers={"Accept": "application/json"})
    if r is None or r.status_code != 200 or host_of(r.url) != f"{code}.breezy.hr":
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    names = sorted({(p.get("company") or {}).get("name") if isinstance(p.get("company"), dict)
                    else p.get("company") for p in data} - {None})
    return hit("breezy", code, f"https://{code}.breezy.hr/", ", ".join(names), len(data),
               [p.get("name", "") for p in data])


def p_pinpoint(code):
    data = jget(f"https://{code}.pinpointhq.com/postings.json")
    if data is None:
        return None
    rows = data.get("data", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    titles = [(r.get("title") or (r.get("attributes") or {}).get("title") or "") for r in rows]
    return hit("pinpoint", code, f"https://{code}.pinpointhq.com/", "", len(rows), titles,
               note="response shape not re-checked for this handoff; confirm by title page")


def p_bamboohr(code):
    # NOT used by Corda Scout. Pattern widely used; verify before trusting.
    r = get(f"https://{code}.bamboohr.com/careers/list", headers={"Accept": "application/json"})
    if r is None or r.status_code != 200 or host_of(r.url) != f"{code}.bamboohr.com":
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    rows = data.get("result", []) if isinstance(data, dict) else []
    return hit("bamboohr", code, f"https://{code}.bamboohr.com/careers", "", len(rows),
               [x.get("jobOpeningName", "") for x in rows], note="unverified endpoint")


ZOHO_REMOVED = re.compile(r"joblist has been removed|lista de tareas se ha eliminado|^\s*[!\u00a1]?\s*(oops|vaya)", re.I)


def p_zohorecruit(code):
    for tld in ("com", "in", "eu"):  # a company lives on exactly one
        r = get(f"https://{code}.zohorecruit.{tld}/jobs/Careers/rss")
        if r is None or r.status_code != 200:
            continue
        body = r.text
        if ZOHO_REMOVED.search(body) or not re.search(r"<rss[\s>][\s\S]*<channel[\s>]", body):
            continue  # 200 + rss content type + "joblist removed" is NOT a board
        name = (_xml_vals(body, "title") or [""])[0]  # channel title, e.g. "X - Careers"
        items = re.findall(r"<item[\s>].*?</item>", body, re.S)
        titles = [(_xml_vals(i, "title") or [""])[0] for i in items]
        return hit("zohorecruit", code, f"https://{code}.zohorecruit.{tld}/jobs/Careers", name,
                   len(items), titles, body[:20000])
    return None


def p_comeet(code):
    # code must be slug|uid, e.g. plus500|A1.00F. The slug alone cannot be read.
    if "|" not in code:
        return None
    slug, uid = code.split("|", 1)
    page = get(f"https://www.comeet.com/jobs/{slug}/{uid}")
    if page is None or page.status_code != 200:
        return None
    m = re.search(r'"token"\s*:\s*"([A-Za-z0-9]{16,})"', page.text)
    if not m:
        return None
    data = jget(f"https://www.comeet.com/careers-api/2.0/company/{uid}/positions?token={m.group(1)}&details=true")
    if not isinstance(data, list):
        return None
    names = sorted({p.get("company_name") for p in data} - {None})
    return hit("comeet", code, f"https://www.comeet.com/jobs/{slug}/{uid}", ", ".join(names), len(data),
               [p.get("name", "") for p in data])


def p_workday(code):
    # code must be tenant|wdN|site, e.g. acme|wd5|External
    parts = code.split("|")
    if len(parts) != 3:
        return None
    tenant, wd, site = parts
    r = post(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
             {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    posts = data.get("jobPostings", [])
    return hit("workday", code, f"https://{tenant}.{wd}.myworkdayjobs.com/{site}", tenant,
               data.get("total", len(posts)), [p.get("title", "") for p in posts],
               note="Workday name is the tenant only; confirm on the board page")


PROBES = {
    "greenhouse": p_greenhouse, "lever": p_lever, "ashby": p_ashby, "workable": p_workable,
    "smartrecruiters": p_smartrecruiters, "personio": p_personio, "teamtailor": p_teamtailor,
    "recruitee": p_recruitee, "breezy": p_breezy, "pinpoint": p_pinpoint, "bamboohr": p_bamboohr,
    "zohorecruit": p_zohorecruit, "comeet": p_comeet, "workday": p_workday,
}
GUESSABLE = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "personio",
             "teamtailor", "recruitee", "breezy", "pinpoint", "bamboohr", "zohorecruit"]

# ------------------------------------------------ evidence from the company site

ATS_URL = re.compile(
    r"https?://[a-z0-9.-]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|"
    r"personio\.(?:com|de)|teamtailor\.com|breezy\.hr|myworkdayjobs\.com|myworkdaysite\.com|recruitee\.com|"
    r"applytojob\.com|pinpointhq\.com|homerun\.co|freshteam\.com|zohorecruit\.(?:com|eu|in)|comeet\.com|"
    r"bamboohr\.com|icims\.com|jobvite\.com|hibob\.com|join\.com|rippling\.com|dayforcehcm\.com|"
    r"softgarden\.(?:io|de)|careers-page\.com|recruiterflow\.com|gohire\.io|eightfold\.ai|paylocity\.com|"
    r"successfactors\.(?:com|eu)|taleo\.net|oraclecloud\.com|peopleforce\.io|mokahr\.com)"
    r"[^\"'<>\s\\)]*", re.I)
HREF = re.compile(r"(?:href|src|data-src|data-url)\s*=\s*[\"']([^\"'<>\s]+)[\"']", re.I)
RESERVED = {"", "www", "jobs", "job", "apply", "careers", "career", "embed", "api", "j", "o", "p",
            "help", "support", "app", "static", "cdn", "en", "en-us", "de", "fr", "search"}


def code_from(url):
    """(ats, code) for a board URL, or (ats, None) when the URL names a posting only."""
    u = urlparse(url)
    h = (u.hostname or "").lower()
    p = [unquote(x) for x in u.path.split("/") if x]
    q = parse_qs(u.query)
    sub = h.split(".")[0]
    first = p[0] if p else ""
    if h.endswith("greenhouse.io"):
        if "for" in q:
            return "greenhouse", q["for"][0]
        return "greenhouse", first if first.lower() not in RESERVED else None
    if h.endswith("lever.co"):
        return "lever", first or None
    if h.endswith("ashbyhq.com"):
        return "ashby", first or None
    if h == "apply.workable.com":
        return "workable", first if first.lower() not in RESERVED else None
    if h.endswith("smartrecruiters.com"):
        return "smartrecruiters", first or None
    if re.search(r"\.jobs\.personio\.(com|de)$", h):
        return "personio", sub
    if h.endswith(".teamtailor.com"):
        return "teamtailor", sub
    if h.endswith(".breezy.hr"):
        return "breezy", sub
    if h.endswith(".recruitee.com"):
        return "recruitee", sub
    if h.endswith(".pinpointhq.com"):
        return "pinpoint", sub
    if h.endswith(".bamboohr.com"):
        return "bamboohr", sub
    if re.search(r"\.zohorecruit\.(com|eu|in)$", h):
        return "zohorecruit", sub
    if h.endswith("comeet.com") and len(p) >= 3 and p[0] == "jobs":
        return "comeet", f"{p[1]}|{p[2]}"
    m = re.match(r"^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$", h)
    if m:
        site = next((x for x in p if not re.match(r"^[a-z]{2}-[A-Za-z]{2}$", x) and x.lower() != "job"), None)
        return "workday", f"{m.group(1)}|{m.group(2)}|{site}" if site else None
    m = re.match(r"^(wd\d+)\.myworkdaysite\.com$", h)
    if m and len(p) >= 3 and p[0].lower() == "recruiting":
        return "workday", f"{p[1]}|{m.group(1)}|{p[2]}"
    return h, None  # an ATS this script cannot read; keep the URL as evidence


def site_boards(website):
    """Board links the company itself publishes: home, /careers, /jobs, careers.{domain}."""
    d = host_of(website)
    if not d or "." not in d:
        return []
    pages = [f"https://{d}/", f"https://{d}/careers", f"https://{d}/jobs", f"https://careers.{d}/"]
    found, seen_pages = {}, set()
    for page in pages:
        if page in seen_pages:
            continue
        seen_pages.add(page)
        r = get(page)
        if r is None or r.status_code != 200:
            continue
        html = r.text
        links = set(ATS_URL.findall(html))
        for m in HREF.findall(html):
            if ATS_URL.match(m):
                links.add(m)
        if "gh_jid=" in html:
            found.setdefault(("greenhouse", None), page)  # Greenhouse on own domain; slug not in URL
        for link in links:
            ats, code = code_from(link.replace("&amp;", "&"))
            found.setdefault((ats, code), link)
        if page == pages[0]:  # follow the home page's own careers link once
            for m in HREF.findall(html):
                if re.search(r"(^|/)(careers?|jobs|join-us|work-with-us|vacancies|open-positions)(/|$)", m, re.I) \
                        and (m.startswith("/") or d in host_of(m)):
                    nxt = m if m.startswith("http") else f"https://{d}{m}"
                    if nxt not in seen_pages:
                        pages.append(nxt)
                    break
    return [{"ats": a, "code": c, "url": u} for (a, c), u in found.items()]


# ------------------------------------------------------------------ scoring

STOP = {"ltd", "limited", "inc", "llc", "group", "holdings", "global", "international", "markets",
        "trading", "broker", "brokers", "capital", "fx", "plc", "sa", "ag", "gmbh", "co", "company", "the"}


def gen_slugs(company, website):
    words = re.findall(r"[a-z0-9]+", company.lower())
    core = [w for w in words if w not in STOP] or words
    root = host_of(website).split(".")[0] if website else ""
    out = []
    for s in (root, "".join(words), "-".join(words), "".join(core), "-".join(core)):
        if s and len(s) >= 3 and s not in out:
            out.append(s)
    return out


def score(row, company, website, from_site):
    cn, bn = norm(company), norm(row["board_name"])
    dom = host_of(website) if website else ""
    text = row["text"].lower()
    name_ok = len(cn) >= 3 and len(bn) >= 4 and (cn in bn or bn in cn)
    dom_ok = bool(dom) and dom in text
    if from_site and (name_ok or dom_ok or row["jobs"] == 0):
        return "High", "linked from company site"
    if name_ok and dom_ok:
        return "High", "board name and company domain both match"
    if from_site:
        return "Medium", "linked from site, but name/domain not seen on board"
    if name_ok and row["jobs"] > 0:
        return "Medium", "board name matches; domain not seen in job text"
    if dom_ok:
        return "Medium", "company domain in job text; board name differs (subsidiary?)"
    return "Reject", "no name or domain match (likely same-slug other company)"


def main(path):
    cols = ["company", "website", "ats", "code", "board_url", "board_name", "jobs", "sample_titles",
            "confidence", "reason", "found_via", "note"]
    w = csv.DictWriter(sys.stdout, fieldnames=cols)
    w.writeheader()
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            company = (rec.get("company") or "").strip()
            website = (rec.get("website") or "").strip()
            cands, via = [], {}
            for b in site_boards(website) if website else []:
                if b["ats"] in PROBES and b["code"]:
                    cands.append((b["ats"], b["code"]))
                    via[(b["ats"], b["code"])] = "site:" + b["url"]
                else:  # unreadable ATS or posting-only URL: still worth recording
                    w.writerow({"company": company, "website": website, "ats": b["ats"], "code": b["code"] or "",
                                "board_url": b["url"], "confidence": "Medium", "reason": "linked from company site",
                                "found_via": "site", "note": "not probed: no reader, or URL names a posting only"})
            for c in (rec.get("candidates") or "").split(";"):
                if ":" in c:
                    a, code = c.split(":", 1)
                    cands.append((a.strip().lower(), code.strip()))
                    via.setdefault((a.strip().lower(), code.strip()), "given")
            if not cands:
                for s in gen_slugs(company, website):
                    for a in GUESSABLE:
                        cands.append((a, s))
                        via.setdefault((a, s), "guessed slug")
            seen = set()
            for ats, code in cands:
                if (ats, code.lower()) in seen or ats not in PROBES:
                    continue
                seen.add((ats, code.lower()))
                row = PROBES[ats](code)
                if not row:
                    continue
                found_via = via.get((ats, code), "")
                conf, why = score(row, company, website, found_via.startswith("site:"))
                w.writerow({"company": company, "website": website, "ats": ats, "code": code,
                            "board_url": row["board_url"], "board_name": row["board_name"], "jobs": row["jobs"],
                            "sample_titles": row["sample_titles"], "confidence": conf, "reason": why,
                            "found_via": found_via, "note": row["note"]})
                sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python ats_probe.py companies.csv > boards.csv")
    main(sys.argv[1])
