#!/usr/bin/env python3
"""Affiliate job matching. Title rules from the user's brief, plus close variants.
Adds to each job: aff (True/False), am (matched phrase), rej (reject reason or '')."""
import json, re, os, glob, collections

INCLUDE = [  # (label, regex on title)
    ("Affiliate", r"\baffiliat\w*"),
    ("Partnerships", r"\bpartnerships?\b"),
    ("Partner Manager", r"\bpartners?\s+(?:business\s+|account\s+|relationship\s+|success\s+|program\s+|programme\s+)?manag\w*"),
    ("Partner Growth", r"\bpartners?\s+growth\b"),
    ("Partner Marketing", r"\bpartners?\s+marketing\b"),
    ("Partner Acquisition", r"\bpartners?\s+(?:acquisition|onboarding|development|sales|lead|director|executive|specialist)\b"),
    ("IB / Introducing Broker", r"\bIBs?\b|\bintroducing\s+brokers?\b"),
    ("Referral / Ambassador", r"\breferrals?\b|\bambassadors?\s+(?:program|programme|manager|lead)"),
    ("Channel Partner", r"\bchannel\s+partner"),
]
INC = [(l, re.compile(r, re.I if "IB" not in l else 0)) for l, r in INCLUDE]
IB_CI = re.compile(r"\bintroducing\s+brokers?\b", re.I)

LANG = ["German", "French", "Spanish", "Italian", "Dutch", "Portuguese", "Arabic", "Turkish", "Polish", "Swedish", "Danish",
        "Norwegian", "Finnish", "Russian", "Greek", "Czech", "Hungarian", "Romanian", "Hebrew", "Japanese", "Korean", "Chinese",
        "Mandarin", "Vietnamese", "Thai", "Indonesian"]
OTHER = ["Influencer", "Creator", "UGC", "Social Commerce", "TikTok", "TikTok Shop", "Live Shopping", "Instructor", "Professor",
         "Faculty", "Lecturer", "Provider", "Clinical", "Medical", "Nurse", "Therapist", "Counsellor", "Counselor"]
REJ = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(LANG + OTHER, key=len, reverse=True)) + r")\b", re.I)

import pycountry
_LANGS = sorted({l.name.split(" (")[0].split(";")[0].strip().lower() for l in pycountry.languages if getattr(l, "type", "L") == "L" and len(l.name) > 2} | {"mandarin", "cantonese", "farsi", "filipino", "tagalog", "swahili", "urdu", "somali", "malay", "uzbek", "mongolian", "kazakh", "tamil", "hindi", "bengali", "bahasa"}, key=len, reverse=True)
_LANGS = [l for l in _LANGS if l != "english" and re.fullmatch(r"[a-z\- ]+", l)]
LANG_ROLE = re.compile(r"\b(" + "|".join(re.escape(l) for l in _LANGS) + r")[\s-]*(?:speakers?|speaking|language|native|fluent)\b|\b(?:native|fluent)\s+(" + "|".join(re.escape(l) for l in _LANGS) + r")\b", re.I)
# "Partnership" titles that are clearly not affiliate/IB work (learned from the first review)
NOT_AFF_CONTEXT = re.compile(r"\b(payments?|sponsorships?|counsel|legal|listings?|tokeni[sz]ation|staking|crypto-as-a-service|banking|institution\w*|seo|analytics)\b|\b(product|media|data)\s+partnerships?\b", re.I)
NON_COMMERCIAL_ROLE = re.compile(r"\b(engineer\w*|(?<!business )developer|software|full[- ]stack|designer|qa|devops)\b", re.I)

EN = set("the and to of a in for with you your we our is are will be on as at or this that from by an have has can all team role experience".split())
def english_share(text):
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < 30: return None  # too short to judge
    return sum(w in EN for w in words) / len(words)

SIGNALS = {"affiliate": r"\baffiliat\w*", "ib": r"\bIBs?\b", "introducing broker": r"introducing brokers?", "cpa": r"\bCPA\b",
           "revenue share": r"rev(?:enue)?[- ]?share", "partner program": r"partner(?:ship)? program", "affiliate network": r"affiliate networks?",
           "sub-affiliate": r"sub[- ]affiliat", "partners portal": r"partners? (?:portal|area|cabinet)", "traffic": r"\btraffic\b",
           "conversion": r"\bconversions?\b", "rebate": r"\brebates?\b", "money manager": r"money managers?|\bPAMM\b|\bMAM\b",
           "media buying": r"media buy", "seo": r"\bSEO\b", "kol": r"\bKOLs?\b", "influencer": r"influencer"}
SIG = {k: re.compile(v, re.I if k not in ("ib", "cpa", "seo", "kol") else 0) for k, v in SIGNALS.items()}
def signals(desc):
    return [k for k, rx in SIG.items() if rx.search(desc or "")]

def classify(title, desc):
    m = None
    for label, rx in INC:
        if label == "IB / Introducing Broker":
            if rx.search(title) or IB_CI.search(title): m = label; break
        elif rx.search(title): m = label; break
    if not m: return False, "", ""
    r = REJ.search(title)
    if r: return True, m, f"Title contains \"{r.group(1)}\""
    lr = LANG_ROLE.search(title)
    if lr: return True, m, f"Language-specific role (\"{lr.group(0).strip()}\")"
    ec = NON_COMMERCIAL_ROLE.search(title)
    if ec: return True, m, f"Technical role (\"{ec.group(1)}\"), not affiliate work"
    nc = NOT_AFF_CONTEXT.search(title)
    if nc and m in ("Partnerships", "Partner Manager", "Partner Acquisition", "Channel Partner"): return True, m, f"Not affiliate work (\"{nc.group(0).strip()}\")"
    if re.search(r"[぀-ヿ一-鿿가-힯؀-ۿ฀-๿Ѐ-ӿ]", title or ""):
        pass  # bilingual titles are judged by the description language below
    share = english_share(desc)
    if share is not None and share < 0.08: return True, m, "Posting is not in English"
    return True, m, ""

if __name__ == "__main__":
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    B = os.path.join(ROOT, "data")
    idx = json.load(open(f"{B}/jobs_index.json"))
    D = {}
    for f in glob.glob(os.path.join(ROOT, "jobs", "desc_*.json")): D.update(json.load(open(f)))
    c = collections.Counter()
    for j in idx:
        a, m, rj = classify(j["t"], D.get(j["id"], ""))
        j["aff"], j["am"], j["rej"] = a, m, rj
        j["kw"] = signals(D.get(j["id"], ""))
        if a: c["matched"] += 1; c["rejected" if rj else "kept"] += 1; c["by " + m] += 1
    json.dump(idx, open(f"{B}/jobs_index.json", "w"), ensure_ascii=False, separators=(",", ":"))
    print(dict(c))
    for j in idx[:0]:
        if j["aff"]: print(("REJ " + j["rej"][:28]).ljust(36) if j["rej"] else "OK".ljust(36), j["am"].ljust(24), "|", j["c"][:18].ljust(18), "|", j["t"][:70])
