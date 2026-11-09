#!/usr/bin/env python3
# monitor.py
# Texas Legislature filing monitor. Pulls bill histories and introduced text
# from the Legislature's public FTP server, categorizes each bill (full text
# where posted, caption otherwise), and writes JSON for the static dashboard
# in docs/. Standard library only.
#
#   python monitor.py                      # live run against the FTP server
#   python monitor.py --source tests/fixtures --out /tmp/out   # offline run
#   python monitor.py --all-types          # include HCR/SCR/HR/SR as well

import argparse
import ftplib
import html
import io
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

FTP_HOST = "ftp.legis.state.tx.us"
CLASSIFIER_VERSION = 2          # bump after editing CATEGORIES to re-score every bill
MIN_SCORE = 2.0                 # below this a bill is left Uncategorized
SECONDARY_RATIO = 0.6           # other categories this close to the top one are kept as secondary
CAPTION_WEIGHT = 3.0            # a caption hit counts this much relative to a body hit
MAX_EVENTS = 1000

# --------------------------------------------------------------------------
# Taxonomy. Each category has:
#   codes  - Texas statutory codes the bill text amends (name -> weight)
#   strong - phrases that nearly settle the category on their own (weight 2)
#   terms  - supporting vocabulary (weight 1)
# Entries are regex fragments matched case-insensitively at a word start, so
# "teacher" also matches "teachers" and "annex" matches "annexation".
# --------------------------------------------------------------------------
CATEGORIES = {
    "Public Education": {
        "codes": {"Education Code": 1.5},
        "strong": ["school district", "public school", "charter school", "texas education agency",
                   "foundation school program", "education savings account", "school board",
                   "board of trustees of (?:a|an|the) (?:independent )?school", "prekindergarten",
                   "classroom teacher", "school safety", "instructional material", "staar"],
        "terms": ["teacher", "student", "school", "curriculum", "campus", "educator", "kindergarten",
                  "truan", "superintendent", "average daily attendance", "special education"],
    },
    "Higher Education": {
        "codes": {"Education Code": 1.0},
        "strong": ["institution(?:s)? of higher education", "public junior college", "community college",
                   "university system", "higher education coordinating board", "tuition",
                   "texas a&m", "university of texas", "medical school", "student loan"],
        "terms": ["universit", "college", "undergraduate", "faculty", "tenure", "financial aid",
                  "board of regents", "degree program"],
    },
    "Elections & Voting": {
        "codes": {"Election Code": 3.0},
        "strong": ["voter registration", "early voting", "ballot", "voting by mail", "polling place",
                   "election judge", "voter", "redistrict", "political contribution", "campaign finance",
                   "primary election", "election(?:s)? administrator", "voting system"],
        "terms": ["election", "candidate", "precinct", "voting", "political party", "runoff", "recount"],
    },
    "Criminal Justice & Public Safety": {
        "codes": {"Penal Code": 2.5, "Code of Criminal Procedure": 2.5},
        "strong": ["criminal offense", "criminal penalt", "peace officer", "law enforcement", "bail",
                   "community supervision", "parole", "department of criminal justice", "felony",
                   "misdemeanor", "human trafficking", "fentanyl", "controlled substance",
                   "sex offender", "capital murder", "death penalty", "county jail", "inmate",
                   "correctional officer", "body.worn camera", "habeas corpus", "criminal history"],
        "terms": ["offense", "prosecut", "defendant", "arrest", "punishment", "convict", "police",
                  "sheriff", "magistrate", "confinement", "victim", "juvenile"],
    },
    "Courts & Civil Law": {
        "codes": {"Civil Practice and Remedies Code": 2.5, "Estates Code": 2.0},
        "strong": ["civil action", "civil liabilit", "district court", "court of appeals", "judicial district",
                   "statutory county court", "probate", "guardianship", "tort", "attorney's fees",
                   "business court", "jury service", "arbitration", "wrongful death", "juror"],
        "terms": ["court", "judge", "lawsuit", "damages", "plaintiff", "litigation", "judicial",
                  "justice of the peace", "clerk"],
    },
    "Property Tax": {
        "codes": {"Tax Code": 1.0},
        "strong": ["ad valorem", "appraisal district", "property tax", "homestead exemption",
                   "residence homestead", "appraised value", "appraisal review board",
                   "no-new-revenue", "voter-approval tax rate", "tax increment"],
        "terms": ["appraisal", "taxing unit", "homestead", "chief appraiser", "tax rate", "exemption from"],
    },
    "Taxes & State Budget": {
        "codes": {"Tax Code": 1.0},
        "strong": ["sales and use tax", "franchise tax", "sales tax", "general appropriations",
                   "appropriation", "comptroller of public accounts", "economic stabilization fund",
                   "severance tax", "hotel occupancy tax", "motor fuel tax", "state budget",
                   "legislative budget board", "trust fund", "private activity bond", "public securit"],
        "terms": ["tax", "revenue", "comptroller", "fiscal", "rebate", "state fund", "treasury"],
    },
    "Health Care": {
        "codes": {"Health and Safety Code": 1.5},
        "strong": ["medicaid", "hospital", "health care provider", "health benefit plan", "physician",
                   "mental health", "prescription drug", "pharmac", "nursing facilit", "telehealth",
                   "telemedicine", "public health", "vaccin", "immuniz", "health and human services commission",
                   "emergency medical", "behavioral health", "assisted living", "breast-feeding",
                   "hospice", "palliative"],
        "terms": ["patient", "medical", "health", "nurse", "disease", "clinic", "dental", "opioid"],
    },
    "Abortion & Reproductive Health": {
        "codes": {},
        "strong": ["abortion", "abortion-inducing", "unborn child", "in vitro", "contracepti",
                   "maternal mortality", "pregnan", "perinatal", "postpartum"],
        "terms": ["reproductive", "fertility", "maternal", "prenatal"],
    },
    "Human Services & Family": {
        "codes": {"Family Code": 2.5, "Human Resources Code": 2.0},
        "strong": ["child protective", "department of family and protective services", "foster care",
                   "child support", "conservatorship", "adoption", "child care", "supplemental nutrition",
                   "family violence", "parent-child relationship", "suit affecting", "elderly",
                   "persons? with (?:a )?disabilit", "child abuse", "homeless"],
        "terms": ["child", "parent", "famil", "marriage", "divorce", "foster", "guardian", "aging"],
    },
    "Immigration & Border": {
        "codes": {},
        "strong": ["border security", "illegal(?:ly)? (?:entry|immigra|alien|present)", "immigration",
                   "unlawfully present", "undocumented", "e-verify", "sanctuary", "international border",
                   "border wall", "texas-mexico border", "citizenship status", "removal of (?:an )?alien"],
        "terms": ["border", "alien", "immigra", "noncitizen", "asylum"],
    },
    "Energy & Utilities": {
        "codes": {"Utilities Code": 2.5, "Natural Resources Code": 1.5},
        "strong": ["ercot", "electric(?:ity)? (?:grid|generation|utilit|cooperative|market)",
                   "public utility commission", "railroad commission", "oil and gas", "natural gas",
                   "power region", "transmission and distribution", "renewable energy", "nuclear",
                   "battery energy storage", "pipeline", "electric vehicle charging", "geothermal",
                   "uranium", "mining"],
        "terms": ["electric", "utilit", "energy", "solar", "wind", "petroleum", "broadband", "telecommunicat"],
    },
    "Water & Environment": {
        "codes": {"Water Code": 2.5},
        "strong": ["groundwater", "water development board", "commission on environmental quality",
                   "wastewater", "flood", "water suppl", "air quality", "emissions", "solid waste",
                   "river authority", "desalination", "produced water", "aquifer", "pollut"],
        "terms": ["water", "environment", "drought", "reservoir", "contamina", "recycl", "landfill"],
    },
    "Transportation": {
        "codes": {"Transportation Code": 2.5},
        "strong": ["department of transportation", "motor vehicle", "driver's license", "toll",
                   "highway", "vehicle registration", "department of motor vehicles", "high-speed rail",
                   "commercial driver", "license plate", "autonomous vehicle", "airport", "railroad crossing"],
        "terms": ["vehicle", "traffic", "road", "transit", "driver", "bridge", "aviation", "seaport", "port authorit"],
    },
    "Local Government & Special Districts": {
        "codes": {"Local Government Code": 2.0, "Special District Local Laws Code": 3.0},
        "strong": ["municipal utility district", "municipal management district", "commissioners court",
                   "home-rule", "general-law municipalit", "annex", "extraterritorial jurisdiction",
                   "emergency services district", "special district", "county government",
                   "creation of the .{0,80}? district"],
        "terms": ["municipalit", "county", "city council", "ordinance", "political subdivision", "local government"],
    },
    "State Government & Ethics": {
        "codes": {"Government Code": 0.5},
        "strong": ["state agenc", "sunset", "public information", "open meetings", "ethics commission",
                   "lobby", "state employee", "employees retirement system", "teacher retirement system",
                   "attorney general", "secretary of state", "state auditor", "governor",
                   "constitutional amendment", "legislative session", "state contract", "procurement"],
        "terms": ["agency", "commission", "state officer", "rulemaking", "advisory committee", "interim study"],
    },
    "Business, Finance & Insurance": {
        "codes": {"Business & Commerce Code": 2.0, "Finance Code": 2.5, "Insurance Code": 2.5,
                  "Business Organizations Code": 2.5},
        "strong": ["insurer", "insurance polic", "deceptive trade", "consumer protection", "financial institution",
                   "bank", "credit union", "corporation", "limited liability", "securities", "windstorm",
                   "department of insurance", "payday", "digital asset", "cryptocurrenc", "economic development",
                   "currency", "stored value card", "small business"],
        "terms": ["insurance", "business", "consumer", "commerce", "loan", "contract", "merchant", "franchise"],
    },
    "Labor & Occupations": {
        "codes": {"Labor Code": 2.5, "Occupations Code": 2.0},
        "strong": ["minimum wage", "workers' compensation", "unemployment", "workforce commission",
                   "occupational licens", "department of licensing and regulation", "employment discrimination",
                   "paid (?:sick|family|parental) leave", "labor union", "collective bargaining",
                   "license holder", "apprentice"],
        "terms": ["employee", "employer", "employment", "wage", "licens", "workforce", "worker"],
    },
    "Housing & Land Use": {
        "codes": {"Property Code": 2.0},
        "strong": ["landlord", "tenant", "evict", "property owners' association", "zoning", "eminent domain",
                   "affordable housing", "residential lease", "housing", "squatter", "real property",
                   "manufactured home", "short-term rental", "subdivision plat", "building code"],
        "terms": ["property owner", "lease", "dwelling", "land use", "deed", "foreclos", "condemn", "rent"],
    },
    "Firearms": {
        "codes": {},
        "strong": ["firearm", "handgun", "license to carry", "ammunition", "red flag", "extreme risk protective",
                   "gun", "suppressor", "concealed carry"],
        "terms": ["weapon", "rifle", "shooting range"],
    },
    "Technology & Privacy": {
        "codes": {},
        "strong": ["artificial intelligence", "personal data", "data privacy", "social media", "cybersecurity",
                   "biometric", "data center", "deepfake", "digital service", "automated license plate",
                   "facial recognition", "data broker", "app store", "age verification", "surveillance"],
        "terms": ["online", "internet", "software", "algorithm", "privacy", "electronic device", "digital"],
    },
    "Agriculture & Wildlife": {
        "codes": {"Agriculture Code": 2.5, "Parks and Wildlife Code": 2.5},
        "strong": ["department of agriculture", "livestock", "parks and wildlife", "hunting", "fishing",
                   "farm", "ranch", "state park", "animal health commission", "hemp", "feral hog", "cruelty to animals"],
        "terms": ["agricultur", "crop", "wildlife", "animal", "cattle", "timber", "rural"],
    },
    "Alcohol, Gambling & Cannabis": {
        "codes": {"Alcoholic Beverage Code": 3.0},
        "strong": ["alcoholic beverage", "casino", "sports wagering", "lottery", "gambling", "bingo",
                   "cannabis", "marihuana", "marijuana", "compassionate.use", "tetrahydrocannabinol",
                   "e-cigarette", "tobacco", "vape", "consumable hemp"],
        "terms": ["liquor", "beer", "wine", "wager", "gaming", "distiller", "brewer"],
    },
    "Commemorative & State Symbols": {
        "codes": {},
        "strong": ["designating .{0,60}? as .{0,80}?(?:day|week|month)", "official state", "state bird",
                   "state symbol", "heritage (?:day|week|month)", "memorial highway", "state holiday",
                   "place designation"],
        "terms": ["commemorat", "recognition day", "in honor of"],
    },
    "Veterans & Military": {
        "codes": {},
        "strong": ["veteran", "military", "national guard", "state guard", "armed forces", "servicemember",
                   "service member", "texas military department", "disabled veteran"],
        "terms": ["military base", "deploy", "surviving spouse"],
    },
}


def _compile():
    out = {}
    for cat, spec in CATEGORIES.items():
        terms = [(re.compile(r"\b(?:%s)" % t, re.I), 2.0, t) for t in spec["strong"]]
        terms += [(re.compile(r"\b(?:%s)" % t, re.I), 1.0, t) for t in spec["terms"]]
        codes = []
        for name, w in spec["codes"].items():
            # keep "Government Code" from matching inside "Local Government Code"
            pre = r"(?<!Local )" if name == "Government Code" else ""
            codes.append((re.compile(pre + r"\b" + re.escape(name) + r"\b"), w, name))
        out[cat] = (terms, codes)
    return out


_COMPILED = _compile()


def classify(caption, body):
    """Score a bill against every category. Returns a dict for the bill record."""
    caption, body = caption or "", body or ""
    scores, evidence = {}, {}
    for cat, (terms, codes) in _COMPILED.items():
        s, ev = 0.0, []
        for rx, w, label in terms:
            nc = min(len(rx.findall(caption)), 2)
            nb = len(rx.findall(body)) if body else 0
            if nc or nb:
                val = w * (CAPTION_WEIGHT * nc + (1 + math.log(nb) if nb else 0))
                s += val
                ev.append((val, label))
        for rx, w, label in codes:
            n = len(rx.findall(body)) if body else 0
            if n:
                val = w * (1 + math.log(n))
                s += val
                ev.append((val, label))
        if s:
            scores[cat] = s
            evidence[cat] = [l for _, l in sorted(ev, reverse=True)[:5]]
    basis = "text" if body else "caption"
    if not scores or max(scores.values()) < MIN_SCORE:
        return {"category": "Uncategorized", "secondary": [], "confidence": 0.0,
                "evidence": [], "basis": basis, "clf": CLASSIFIER_VERSION}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_cat, top = ranked[0]
    return {
        "category": top_cat,
        "secondary": [c for c, s in ranked[1:3] if s >= SECONDARY_RATIO * top],
        "confidence": round(top / sum(scores.values()), 2),
        "evidence": evidence[top_cat],
        "basis": basis,
        "clf": CLASSIFIER_VERSION,
    }


# --------------------------------------------------------------------------
# Sources: the live FTP server, or a local directory laid out the same way.
# listdir() returns (name, is_dir, signature); the signature changes whenever
# the file does, which is how unchanged files are skipped between runs.
# --------------------------------------------------------------------------
class FTPSource:
    def __init__(self, host=FTP_HOST):
        self.host, self.ftp, self.use_mlsd = host, None, True
        self._connect()

    def _connect(self):
        self.ftp = ftplib.FTP(self.host, timeout=60)
        self.ftp.login()

    def _retry(self, fn):
        for attempt in range(3):
            try:
                return fn()
            except ftplib.error_perm:
                raise
            except (ftplib.Error, OSError, EOFError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
                try:
                    self._connect()
                except (ftplib.Error, OSError):
                    pass

    def listdir(self, path):
        if self.use_mlsd:
            try:
                rows = self._retry(lambda: list(self.ftp.mlsd(path)))
                return [(n, f.get("type") == "dir", f.get("modify", "") + "/" + f.get("size", ""))
                        for n, f in rows if f.get("type") in ("dir", "file")]
            except ftplib.error_perm:
                self.use_mlsd = False
        lines = []
        self._retry(lambda: (lines.clear(), self.ftp.retrlines("LIST " + path, lines.append)))
        out = []
        for ln in lines:
            if ln[:1].isdigit():                       # DOS style: date time size|<DIR> name
                p = ln.split(None, 3)
                if len(p) == 4:
                    out.append((p[3], p[2] == "<DIR>", ln))
            else:                                      # unix style
                p = ln.split(None, 8)
                if len(p) == 9 and p[8] not in (".", ".."):
                    out.append((p[8], ln.startswith("d"), ln))
        return out

    def read(self, path):
        def go():
            buf = io.BytesIO()
            self.ftp.retrbinary("RETR " + path, buf.write)
            return buf.getvalue()
        return self._retry(go)


class LocalSource:
    def __init__(self, root):
        self.root = root

    def listdir(self, path):
        full = os.path.join(self.root, path.lstrip("/"))
        if not os.path.isdir(full):
            return []
        out = []
        for e in os.scandir(full):
            st = e.stat()
            out.append((e.name, e.is_dir(), "%d/%d" % (st.st_mtime_ns, st.st_size)))
        return out

    def read(self, path):
        with open(os.path.join(self.root, path.lstrip("/")), "rb") as f:
            return f.read()


def walk(src, root, wanted):
    """Yield (path, name, signature) for every file under the wanted type folders."""
    try:
        tops = src.listdir(root)
    except ftplib.error_perm:
        return
    stack = [root + "/" + n for n, is_dir, _ in tops if is_dir and wanted(n)]
    while stack:
        d = stack.pop()
        for name, is_dir, sig in src.listdir(d):
            if is_dir:
                stack.append(d + "/" + name)
            else:
                yield d + "/" + name, name, sig


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
HIST_RE = re.compile(r"^([HS](?:B|JR|CR|R))\s*0*(\d+)\.xml$", re.I)
TEXT_RE = re.compile(r"^([HS](?:B|JR|CR|R))0*(\d+)([A-Z])\.html?$", re.I)
VERSION_PREF = "IHSEF"   # introduced first; later versions only if no introduced text exists


def decode(raw):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def iso_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_history(raw):
    root = ET.fromstring(raw)
    cap = root.find("caption")
    authors = [a.strip() for a in re.split(r"\s*\|\s*", root.findtext("authors") or "") if a.strip()]
    subjects = [re.sub(r"\s*\([A-Z]\d+\)\s*$", "", (s.text or "").strip())
                for s in root.iter("subject") if (s.text or "").strip()]
    actions = []
    for a in root.iter("action"):
        d = iso_date(a.findtext("date"))
        desc = (a.findtext("description") or "").strip()
        if d or desc:
            actions.append((d, desc))
    filed = next((d for d, desc in actions if d and desc.lower().startswith("filed")), None)
    dated = [d for d, _ in actions if d]
    if not filed and dated:
        filed = min(dated)
    return {
        "caption": re.sub(r"\s+", " ", (cap.text or "").strip()) if cap is not None else "",
        "authors": authors,
        "tlo_subjects": subjects,
        "filed": filed,
        "last_action": actions[-1][1] if actions else "",
        "last_action_date": actions[-1][0] if actions else None,
    }


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    p = _Stripper()
    p.feed(decode(raw))
    return re.sub(r"\s+", " ", html.unescape(" ".join(p.parts)).replace("\xa0", " ")).strip()


def caption_from_text(text):
    m = re.search(r"\b(relating to .{10,600}?)\.?\s+BE IT (?:ENACTED|RESOLVED)", text, re.S)
    if not m:
        m = re.search(r"\b(proposing a constitutional amendment .{10,600}?)\.?\s+BE IT RESOLVED", text, re.S)
    return (m.group(1)[0].upper() + m.group(1)[1:] + ".") if m else ""


def is_real_text(text):
    """The server posts a short placeholder page before a bill's text is ready."""
    return len(text) >= 300 and re.search(r"BE IT (?:ENACTED|RESOLVED)", text) is not None


def cached_text(src, cache_dir, bid, path, sig):
    """Stripped bill text, read from the local cache when the FTP signature still matches."""
    fn = os.path.join(cache_dir, bid.replace(" ", "") + ".txt")
    try:
        with open(fn, encoding="utf-8") as f:
            head, _, text = f.read().partition("\n")
        if head == sig:
            return text, False
    except OSError:
        pass
    text = html_to_text(src.read(path))
    with open(fn, "w", encoding="utf-8") as f:
        f.write(sig + "\n" + text)
    return text, True


def body_only(text):
    """Drop the header so the body score isn't just the caption counted twice."""
    m = re.search(r"BE IT (?:ENACTED|RESOLVED)[^:]{0,80}:", text)
    return text[m.end():][:200000] if m else text[:200000]


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_bills(path, bills):
    key = lambda b: (b["type"], b["number"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(json.dumps(b, ensure_ascii=False, sort_keys=True)
                                   for b in sorted(bills, key=key)) + "\n]\n")


def main():
    ap = argparse.ArgumentParser(description="Texas Legislature filing monitor")
    ap.add_argument("--session", default="90R")
    ap.add_argument("--source", help="local directory mirroring the FTP layout (default: live FTP)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data"))
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache"),
                    help="where stripped bill text is kept between runs")
    ap.add_argument("--all-types", action="store_true", help="include concurrent and simple resolutions")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)
    bills_path = os.path.join(args.out, "bills.json")
    bills = {b["id"]: b for b in load(bills_path, [])}
    events = load(os.path.join(args.out, "changes.json"), [])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    src = LocalSource(args.source) if args.source else FTPSource()
    base = "/bills/" + args.session
    wanted = (lambda n: True) if args.all_types else \
        (lambda n: n.lower().endswith(("_bills", "_joint_resolutions")))

    hist, texts = {}, {}
    for path, name, sig in walk(src, base + "/billhistory", wanted):
        m = HIST_RE.match(name)
        if m:
            hist[(m.group(1).upper(), int(m.group(2)))] = (path, sig)
    for path, name, sig in walk(src, base + "/billtext/html", wanted):
        m = TEXT_RE.match(name)
        if m:
            texts.setdefault((m.group(1).upper(), int(m.group(2))), {})[m.group(3).upper()] = (path, sig, name)
    print("listing: %d histories, %d bills with text" % (len(hist), len(texts)))

    n_new = n_text = n_fetch = n_err = 0
    for key in sorted(set(hist) | set(texts)):
        btype, num = key
        bid = "%s %d" % (btype, num)
        rec = bills.get(bid)
        is_new = rec is None
        if is_new:
            rec = {"id": bid, "type": btype, "number": num,
                   "chamber": "House" if btype[0] == "H" else "Senate",
                   "first_seen": now, "caption": "", "authors": [], "tlo_subjects": [],
                   "filed": None, "last_action": "", "last_action_date": None,
                   "has_text": False, "text_version": None, "text_url": None, "text_chars": 0,
                   "hist_sig": None, "text_sig": None, "category": None}
        had_text, old_cat, old_caption = rec["has_text"], rec.get("category"), rec["caption"]
        dirty, body = is_new, None
        try:
            if key in hist and hist[key][1] != rec["hist_sig"]:
                rec.update(parse_history(src.read(hist[key][0])))
                rec["hist_sig"] = hist[key][1]
                dirty = True
                n_fetch += 1
            tv = texts.get(key)
            choice = None
            if tv:
                ver = next((v for v in VERSION_PREF if v in tv), sorted(tv)[0])
                choice = tv[ver]
            need_text = choice and (choice[1] != rec["text_sig"]
                                    or dirty or rec.get("clf") != CLASSIFIER_VERSION)
            if need_text:
                full, fetched = cached_text(src, args.cache, bid, choice[0], choice[1])
                n_fetch += fetched
                rec["text_sig"] = choice[1]
                if is_real_text(full):
                    body = body_only(full)
                    if not rec["caption"]:
                        rec["caption"] = caption_from_text(full)
                    rec.update(has_text=True, text_version=ver, text_chars=len(full),
                               text_url="https://capitol.texas.gov/tlodocs/%s/billtext/html/%s"
                                        % (args.session, choice[2]))
                else:   # placeholder page: keep scoring from the caption until real text lands
                    rec.update(has_text=False, text_version=None, text_chars=0, text_url=None)
                dirty = True
        except Exception as e:   # one bad file shouldn't sink the run; it is retried next time
            n_err += 1
            print("  ! %s: %s: %s" % (bid, type(e).__name__, e), file=sys.stderr)
            if is_new and not rec["caption"]:
                continue
        if dirty or rec.get("clf") != CLASSIFIER_VERSION:
            rec.update(classify(rec["caption"], body))
            rec["updated"] = now
        bills[bid] = rec

        ev = {"ts": now, "bill": bid, "category": rec["category"]}
        if is_new:
            n_new += 1
            events.append(dict(ev, event="new", basis=rec["basis"]))
        else:
            if rec["has_text"] and not had_text:
                n_text += 1
                events.append(dict(ev, event="text_posted", was=old_cat))
            if old_caption and rec["caption"] != old_caption:
                events.append(dict(ev, event="caption_changed", was=old_cat))

    save_bills(bills_path, bills.values())
    with open(os.path.join(args.out, "changes.json"), "w", encoding="utf-8") as f:
        json.dump(events[-MAX_EVENTS:], f, ensure_ascii=False, indent=0)
    counts = {}
    for b in bills.values():
        counts[b["category"]] = counts.get(b["category"], 0) + 1
    meta = {"session": args.session, "total": len(bills),
            "with_text": sum(1 for b in bills.values() if b["has_text"]),
            "by_category": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}
    old_meta = load(os.path.join(args.out, "meta.json"), {})
    changed = n_new or n_text or {k: old_meta.get(k) for k in meta} != meta
    meta["checked"] = now if changed else old_meta.get("checked", now)   # avoids a commit when nothing moved
    meta["last_change"] = now if (n_new or n_text) else old_meta.get("last_change")
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    print("%d bills tracked | %d new | %d newly with text | %d files fetched | %d errors"
          % (len(bills), n_new, n_text, n_fetch, n_err))
    if (hist or texts) and n_err > max(5, (len(hist) + len(texts)) // 4):
        sys.exit(1)


if __name__ == "__main__":
    main()
