#!/usr/bin/env python3
"""Build a 300-work, inventory-screened Scriptorium Reading Room ZIP."""
from __future__ import annotations
import argparse, csv, html, re, unicodedata, zipfile, tomllib, subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urljoin
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SOURCE_REPO = "HistoricalChristianFaith/Writings-Database"
SOURCE_BRANCH = "master"
TARGET = 300
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Scriptorium-reading-room-acquisition/1.0"})

def clean(s):
    s = html.unescape(s or "").replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[\t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())

def raw_url(path):
    return f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_BRANCH}/" + quote(path, safe="/")

def repo_url(path):
    return f"https://github.com/{SOURCE_REPO}/tree/{SOURCE_BRANCH}/" + quote(path, safe="/")

def natural_key(s):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", s)]

def html_to_text(raw):
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.find("article") or soup.body or soup
    for node in list(root.find_all(True)):
        if getattr(node, "attrs", None) is None:
            continue
        ident = str(node.get("id") or "").lower()
        classes = " ".join(node.get("class") or []).lower()
        if ident.startswith(("fn", "footnote")) or any(k in classes for k in ("footnote", "endnote", "apparatus", "editorial-note")):
            node.decompose()
        elif node.name in ("script", "style", "nav", "header", "footer", "img", "svg", "button", "input"):
            node.decompose()
        elif node.name == "sup" and node.find("a", href=re.compile(r"^#(?:fn|r)\d*", re.I)):
            node.decompose()
    for node in list(root.find_all("a")):
        href = node.get("href") or ""
        txt = node.get_text(" ", strip=True)
        if href.startswith(("#fn", "#r")) or re.fullmatch(r"\[?\d+\]?", txt):
            node.decompose()
    blocks, buf = [], []
    def flush():
        nonlocal buf
        t = clean(" ".join(buf)); buf = []
        if t and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", t, re.I):
            blocks.append(t)
    def walk(node):
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if t: buf.append(t)
            return
        if not isinstance(node, Tag): return
        n = (node.name or "").lower()
        if n in ("br", "hr"): flush()
        elif n in ("h1","h2","h3","h4","h5","h6"):
            flush()
            t = clean(node.get_text(" ", strip=True))
            if t: blocks.append("#" * min(4, max(2, int(n[1]))) + " " + t)
        elif n in ("p","li","blockquote","pre"):
            flush()
            t = clean(node.get_text(" ", strip=True))
            if t and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", t, re.I):
                blocks.append(t)
        elif n in ("div","section","article","ul","ol","table","tr"):
            flush()
            for child in node.children: walk(child)
            flush()
        else:
            for child in node.children: walk(child)
    walk(root); flush()
    return clean("\n\n".join(blocks))

def read_html(root, rel):
    return html_to_text((root / rel).read_text(encoding="utf-8", errors="replace"))

def read_html_from_heading(root, rel, heading_pattern):
    raw = (root / rel).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.find("article") or soup.body or soup
    target = next((h for h in content.find_all(re.compile(r"^h[1-6]$", re.I)) if re.search(heading_pattern, h.get_text(" ", strip=True), re.I)), None)
    if target is None:
        raise RuntimeError(f"Could not find clean-text start heading /{heading_pattern}/ in {rel}")
    for sibling in list(target.parent.contents):
        if sibling is target:
            break
        if isinstance(sibling, Tag) and sibling.name == "h1":
            continue
        sibling.extract()
    return html_to_text(str(content))

ALIASES = {
    "Gregory the Dialogist": "Gregory the Great",
    "John Damascene": "John of Damascus",
    "Palladius of Galatia": "Palladius of Helenopolis",
    "Cyprian": "Cyprian of Carthage",
    "Tertullian": "Tertullian of Carthage",
    "Origen": "Origen of Alexandria",
    "Ambrose": "Ambrose of Milan",
    "Leo": "Leo the Great",
    "Theodoret": "Theodoret of Cyrus",
    "Athanasius": "Athanasius of Alexandria",
    "Hermias Sozomen": "Sozomen",
    "Sozomen": "Sozomen",
    "Gildas the Wise": "Gildas",
    "Gildas": "Gildas",
    "Aristides the Philosopher": "Aristides of Athens",
    "Gregory the Dialogist": "Gregory the Great",
    "Gregory the Great": "Gregory the Great",
    "Gregory of Neocaesarea": "Gregory of Neocaesarea",
    "Gregory the Wonderworker": "Gregory of Neocaesarea",
    "Salvian of Marseille": "Salvian the Presbyter",
    "Palladius of Helenopolis": "Palladius of Helenopolis",
    "Palladius of Galatia": "Palladius of Helenopolis",
    "Salvian of Marseilles": "Salvian the Presbyter",
    "Irenaeus": "Irenaeus of Lyons",
    "Lactantius": "Lactantius",
    "Lucius Caecilius Firmianus Lactantius": "Lactantius",
    "Asterius of Amasea": "Asterius of Amasea (Bishop of Amasia)",
    "Shepherd of Hermas": "Hermas",
    "Hermas": "Hermas",
    "Ephraim": "Ephrem the Syrian",
    "Ephrem": "Ephrem the Syrian",
    "Novatian": "Novatian of Rome",
    "Novatian of Rome": "Novatian of Rome",
    "Caius Presbyter of Rome": "Caius of Rome",
    "Vincent of Lerins": "Vincent of Lérins",
    "Archelaus": "Archelaus of Carrhae",
    "Archelaus of Carrhae": "Archelaus of Carrhae",
    "Augustine": "Augustine of Hippo",
    "St. Augustine": "Augustine of Hippo",
    "St Augustine": "Augustine of Hippo",
    "Chrysostom": "John Chrysostom",
    "St. John Chrysostom": "John Chrysostom",
    "St John Chrysostom": "John Chrysostom",
    "St. Ambrose": "Ambrose of Milan",
    "St Ambrose": "Ambrose of Milan",
    "St. Justin Martyr": "Justin Martyr",
    "St Justin Martyr": "Justin Martyr",
    "St. Ignatius": "Ignatius of Antioch",
    "St Ignatius": "Ignatius of Antioch",
    "Aphrahat": "Aphrahat the Persian Sage",
    "Gregory Nazianzen": "Gregory of Nazianzus",
    "Gregory of Nazianzen": "Gregory of Nazianzus",
    "Tatian": "Tatian the Assyrian",
    "Hippolytus": "Hippolytus of Rome",
    "Methodius": "Methodius of Olympus",
    "Sulpitius Severus": "Sulpicius Severus",
    "Gennadius of Marseilles": "Gennadius of Massilia",
    "Victorinus": "Victorinus of Pettau",
    "Gregory Thaumaturgus": "Gregory of Neocaesarea",
    "St. Gregory Thaumaturgus": "Gregory of Neocaesarea",
    "Eusebius": "Eusebius of Caesarea",
    "Basil": "Basil of Caesarea",
    "Clemens Alexandrinus": "Clement of Alexandria",
    "Mar Jacob": "Jacob of Serugh",
    "Africanus": "Julius Africanus",
    "St. Leo": "Leo the Great",
    "Bardesanes": "Bardaisan of Edessa (dialogue recorded by Philip; attribution qualified)",
    "Clement": "Clement of Rome",
    "Venantius": "Venantius Fortunatus",
    "Bardaisan": "Bardaisan of Edessa (dialogue recorded by Philip; attribution qualified)"
}
def author_key(a):
    a = (a or "").strip()
    return norm(ALIASES.get(a, a))

TITLE_EQUIVALENTS = {
    norm("A Treatise to Prove that No One Can Harm the Man Who Does Not Injure Himself"):
        norm("No One Can Harm the Man Who Does Not Injure Himself"),
    norm("A Treatise on Grace and Free Will"): norm("On Grace and Free Will"),
    norm("A Treatise on Faith and the Creed"): norm("On Faith and the Creed"),
    norm("Disputation of the First Day"):
        norm("Acts or Disputation Against Fortunatus the Manichaean Day 1"),
    norm("Disputation of the Second Day"):
        norm("Acts or Disputation Against Fortunatus the Manichaean Day 2"),
    norm("Of the Work of Monks"): norm("On the Work of Monks"),
    norm("Of Holy Virginity"): norm("On Holy Virginity"),
    norm("On the Catechising of the Uninstructed"): norm("On Catechising the Uninstructed"),
    norm("Of Two Souls"): norm("Concerning Two Souls, Against the Manichaeans"),
    norm("Care of the Dead"): norm("On Care to Be Had for the Dead"),
    norm("A Treatise on the Care of the Dead"): norm("On Care to Be Had for the Dead"),
    norm("Handbook on Faith, Hope and Love"): norm("Enchiridion"),
    norm("The Paedagogus"): norm("The Instructor"),
    norm("Paedagogus"): norm("The Instructor"),
    norm("The Stromata"): norm("The Stromata, or Miscellanies"),
    norm("On the Sovereignty of God"): norm("On the Sole Government of God"),
    norm("The Sovereignty of God"): norm("On the Sole Government of God"),
    norm("First Apology"): norm("The First Apology of Justin"),
    norm("The First Apology"): norm("The First Apology of Justin"),
    norm("Apologia ad Constantium"): norm("Defence Before Constantius"),
    norm("Correspondence with Pope Innocent I"): norm("Correspondence with Innocent, Bishop of Rome"),
    norm("The Fifth Theological Oration"): norm("Oration 31"),
    norm("Fifth Theological Oration"): norm("Oration 31"),
    norm("The First Theological Oration"): norm("Oration 27"),
    norm("First Theological Oration"): norm("Oration 27"),
    norm("On Two Souls"): norm("Concerning Two Souls, Against the Manichaeans"),
    norm("Care for the Dead"): norm("On Care to Be Had for the Dead"),
    norm("Enchiridion"): norm("Handbook on Faith, Hope and Love"),
    norm("Paedagogus"): norm("The Instructor"),
    norm("On the Instructor"): norm("The Instructor"),
    norm("Stromata"): norm("The Stromata, or Miscellanies"),
    norm("De Monarchia"): norm("On the Sole Government of God"),
    norm("The First Apology"): norm("The First Apology of Justin"),
    norm("Apologia de Fuga"): norm("Defence of His Flight"),
    norm("De Sententia Dionysii"): norm("On the Opinion of Dionysius"),
    norm("Ad Episcopus Aegypti et Libyae"): norm("To the Bishops of Egypt"),
    norm("Ad Afros Epistola Synodica"): norm("To the Bishops of Africa"),
    norm("De Spiritu Sancto"): norm("On the Holy Spirit"),
}

def title_variants(value, author=None):
    """Generate conservative title variants while preserving topic, date and volume numbers."""
    raw = clean(value).casefold()
    variants = {raw}
    variants.add(re.sub(r"\s*\([^)]*\)", "", raw))
    variants.add(re.sub(r"\s*\[[^]]*\]", "", raw))
    # Remove a final dash suffix only when it is actually an author attribution.
    if author and re.search(r"\s+[—–]\s+", raw):
        prefix, suffix = re.split(r"\s+[—–]\s+", raw, maxsplit=1)
        stop = {"the", "of", "saint", "st", "pope"}
        author_tokens = set(norm(author).split())
        canonical = ALIASES.get(author, author)
        author_tokens.update(norm(canonical).split())
        suffix_tokens = set(norm(suffix).split()) - stop
        if suffix_tokens and suffix_tokens.issubset(author_tokens):
            variants.add(prefix)
    expanded = set(variants)
    for item in list(variants):
        x = re.sub(r"\bcommentary on (?:the )?gospel of\b", "commentary on", item)
        x = re.sub(r"\bcommentary on the acts of the apostles\b", "commentary on acts", x)
        x = re.sub(r"\bcorrespondence of (?:st\.?\s*)?chrysostom with\b", "correspondence with", x)
        x = re.sub(r"\bon the work of the monks\b", "on the work of monks", x)
        x = re.sub(r"\bon catechising of the uninstructed\b", "on catechising the uninstructed", x)
        x = re.sub(r"^a treatise on\b", "on", x)
        x = re.sub(r"^the treatise on\b", "on", x)
        x = re.sub(r"\bexpositions? on (?:the book of )?psalms?,?\s*psalm\s*(\d+)\b", r"exposition on psalm \1", x)
        def roman_to_arabic(match):
            value = match.group(2).lower()
            vals = {"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
            total = 0; prev = 0
            for ch in reversed(value):
                n = vals[ch]
                total += -n if n < prev else n
                prev = max(prev, n)
            return f"{match.group(1)} {total}"
        x = re.sub(r"\b(book|part|volume|discourse|homily|homilies|oration|letter|epistle)\s+([ivxlcdm]+)\b", roman_to_arabic, x)
        paren_num = re.search(r"\(\s*(oration|homily|book|discourse|letter|epistle)\s+([0-9]+|[ivxlcdm]+)\s*\)", x)
        if paren_num:
            number = roman_to_arabic(paren_num) if not paren_num.group(2).isdigit() else f"{paren_num.group(1)} {paren_num.group(2)}"
            expanded.add(number)
        x = re.sub(r"^the\s+", "", x)
        x = re.sub(r"\bdisputation with (?:the )?heresiarch manes\b", "disputation with manes", x)
        if author and author_key(author) == author_key("Clement of Rome") and "letter to the corinthians" in x:
            expanded.add("first epistle of clement to the corinthians")
        expanded.add(x)
    keys = set()
    for item in expanded:
        item = re.sub(r"\s+", " ", item).strip(" .,:;—–-")
        if not item:
            continue
        key = norm(item)
        keys.add(TITLE_EQUIVALENTS.get(key, key))
    return keys

def division_signature(value):
    """Return a trailing volume/letter/homily number when the title is a part of a series."""
    raw = clean(value).casefold()
    m = re.search(
        r"(?:[,;:]?\s+)(book|part|volume|vol\.?|tome|letter|epistle|sermon|homily|discourse)\s*"
        r"(?:no\.?\s*)?([0-9]+|[ivxlcdm]+)(?:\s*[-–—]\s*([0-9]+|[ivxlcdm]+))?\s*$",
        raw, re.I
    )
    if not m:
        return None
    kind = m.group(1).casefold().rstrip(".")
    if kind in ("vol", "volume", "tome"): kind = "volume"
    if kind == "letter": kind = "epistle"
    def as_int(v):
        if not v: return None
        if v.isdigit(): return int(v)
        vals = {"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
        total = 0; prev = 0
        for ch in reversed(v.lower()):
            n = vals.get(ch,0)
            total += -n if n < prev else n
            prev = max(prev,n)
        return total
    return (kind, as_int(m.group(2)), as_int(m.group(3)) or as_int(m.group(2)))

def title_base_keys(value):
    """Base title keys with a trailing book/volume/letter marker removed."""
    raw = clean(value).casefold()
    raw = re.sub(r"\s*\([^)]*\)", "", raw)
    raw = re.sub(r"\s*\[[^]]*\]", "", raw)
    raw = re.sub(
        r"(?:[,;:]?\s+)(?:book|part|volume|vol\.?|tome)\s*"
        r"(?:no\.?\s*)?(?:[0-9]+|[ivxlcdm]+)(?:\s*[-–—]\s*(?:[0-9]+|[ivxlcdm]+))?\s*$",
        "", raw, flags=re.I
    )
    raw = re.sub(
        r"(?:[,;:]?\s+)(?:sermons?|homilies?)\s+[0-9]+(?:\s*[-–—]\s*[0-9]+)?\s*$",
        "", raw, flags=re.I
    )
    raw = re.sub(r"\bcommentary on (?:the )?gospel of\b", "commentary on", raw)
    raw = re.sub(r"\bcommentary on the acts of the apostles\b", "commentary on acts", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .,:;—–-")
    key = norm(raw)
    return TITLE_EQUIVALENTS.get(key, key)

def titles_match(candidate, existing, author=None):
    ckeys = title_variants(candidate, author)
    ekeys = title_variants(existing, author)
    if ckeys & ekeys:
        return True

    cdiv = division_signature(candidate)
    ediv = division_signature(existing)
    cbase = title_base_keys(candidate)
    ebase = title_base_keys(existing)
    if cbase and cbase == ebase:
        # Different numbered books/parts are different gaps; a full-work title overlaps any part.
        if cdiv and ediv:
            if cdiv[0] != ediv[0]:
                return False
            return not (cdiv[2] < ediv[1] or ediv[2] < cdiv[1])
        return True

    # Catch numbered epistle/letter title variants (e.g. Cyprian's "Letter 1" vs "Epistle 1").
    numbered = re.search(r"\b(letter|epistle|sermon|homily|discourse)\s*([0-9]+)\b", candidate, re.I)
    if numbered:
        ctype = "epistle" if numbered.group(1).casefold() in ("letter","epistle") else numbered.group(1).casefold()
        cnum = int(numbered.group(2))
        old = re.search(r"\b(letter|epistle|sermon|homily|discourse)\s*([0-9]+)\b", existing, re.I)
        if old:
            etype = "epistle" if old.group(1).casefold() in ("letter","epistle") else old.group(1).casefold()
            if ctype == etype and cnum == int(old.group(2)):
                # The numbered item itself is the same; don't require the long catalogue subtitle to match.
                return True

    # Do not fuzzy-match adjacent books, Bible books or years (e.g. 1 Corinthians vs 2 Corinthians).
    if re.findall(r"\d+", candidate) != re.findall(r"\d+", existing):
        return False

    # Last-resort fuzzy match is stricter than the former 0.88 threshold.
    cflat = norm(re.sub(r"\s*\([^)]*\)", "", candidate))
    eflat = norm(re.sub(r"\s*\([^)]*\)", "", existing))
    if min(len(cflat), len(eflat)) >= 18 and SequenceMatcher(None, cflat, eflat).ratio() >= 0.94:
        return True
    return False

SERIES = {
    "John Chrysostom": ("c. AD 347–407", "Nicene and Post-Nicene Fathers, First Series (nineteenth-century English translation)"),
    "Origen of Alexandria": ("c. AD 185–253", "Ante-Nicene Fathers / historic public-domain English translations"),
    "Leo the Great": ("AD 440–461", "Nicene and Post-Nicene Fathers, Second Series, Vol. XII (1895)"),
    "Athanasius of Alexandria": ("c. AD 296–373", "Nicene and Post-Nicene Fathers, Second Series, Vol. IV (1892)"),
    "Augustine of Hippo": ("AD 354–430", "Nicene and Post-Nicene Fathers, First Series (nineteenth-century English translation)"),
    "Jerome": ("c. AD 347–420", "Nicene and Post-Nicene Fathers, Second Series, Vol. VI (1893)"),
    "Basil of Caesarea": ("c. AD 329–379", "Nicene and Post-Nicene Fathers, Second Series, Vol. VIII (1895)"),
    "Clement of Alexandria": ("c. AD 150–215", "Ante-Nicene Fathers, Vol. II (1885)"),
    "Cyprian of Carthage": ("c. AD 210–258", "Ante-Nicene Fathers, Vol. V (1886)"),
    "Hilary of Poitiers": ("c. AD 315–367", "Nicene and Post-Nicene Fathers, Second Series, Vol. IX (1899)"),
    "Eusebius of Caesarea": ("c. AD 260–339", "Nicene and Post-Nicene Fathers, Second Series, Vol. I (1890)"),
    "Cyril of Alexandria": ("c. AD 376–444", "Historic public-domain English translations of Cyril's works"),
    "Theophylact of Ohrid": ("c. AD 1050–1107", "Historic public-domain English translation of Theophylact's New Testament commentaries"),
    "Ambrosiaster": ("late fourth century", "Nicene and Post-Nicene Fathers, Second Series, Vol. X (1896)"),
    "Theodoret of Cyrus": ("c. AD 393–458", "Nicene and Post-Nicene Fathers, Second Series, Vol. III (1892)"),
    "Oecumenius": ("sixth century", "Historic public-domain English translation of Oecumenius' biblical commentaries"),
    "Rabanus Maurus": ("c. AD 780–856", "Historic public-domain edition transcribed in the source collection"),
    "Isidore of Seville": ("c. AD 560–636", "Historic English translation of the Chronicon"),
    "Macarius Magnes": ("late fourth century", "Historic English translation of the Apocriticus"),
    "Palladius of Galatia": ("c. AD 363–431", "W. K. Lowther Clarke, The Lausiac History (1918)"),
    "Severian of Gabala": ("late fourth century", "Public-domain English translations identified in the source collection"),
    "Tatian the Assyrian": ("second century", "Ante-Nicene Fathers, Vol. II (1885)"),
}

def metadata_for(author, rels, title, desc=""):
    date, edition = SERIES.get(author, ("Date uncertain; see source edition", "Public-domain historical edition transcribed in Writings-Database"))
    translator = ""; editor = ""
    if author == "Theodoret of Cyrus": translator, editor = "Blomfield Jackson", "Philip Schaff and Henry Wace"
    elif author == "John Chrysostom": translator, editor = "Various historic translators", "Philip Schaff and Henry Wace"
    elif author == "Athanasius of Alexandria": translator, editor = "Various historic translators", "Philip Schaff and Henry Wace"
    elif author == "Leo the Great": translator, editor = "Charles Lett Feltoe and other credited translators", "Philip Schaff and Henry Wace"
    elif author == "Clement of Alexandria": translator, editor = "William Wilson and other credited translators", "Alexander Roberts and James Donaldson"
    elif author == "Basil of Caesarea": translator, editor = "Various historic translators", "Philip Schaff and Henry Wace"
    src = raw_url(rels[0]) if len(rels) == 1 else repo_url(rels[0].split("/")[0])
    return {
        "date": date, "translator": translator, "editor": editor, "edition": edition,
        "url": src,
        "description": desc or f"{title}, presented as one continuous reading text with original internal divisions retained.",
        "rights_notes": "The Writings-Database repository README states that all works in the collection are public domain. The historic source edition is identified above; editorial footnotes and apparatus have been removed from the reading text. See the source URL for the underlying transcription."
    }

class Builder:
    def __init__(self, root, inventory_path, prior_batch_manifest=None):
        self.root = root; self.items = []; self.known = defaultdict(list); self.seen = set()
        self.skipped = defaultdict(int)
        with inventory_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                ak = author_key(r.get("attributed_author", "")); t = r.get("title", "")
                if ak and t: self.known[ak].append(t)
        self.prior_batch_rows = 0
        if prior_batch_manifest:
            with prior_batch_manifest.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    ak = author_key(r.get("author", "")); t = r.get("title", "")
                    if ak and t:
                        self.known[ak].append(t)
                        self.prior_batch_rows += 1
        print(f"PRIOR_BATCH_ROWS_EXCLUDED={self.prior_batch_rows}")

    def duplicate(self, author, title):
        ak = author_key(author)
        for old in self.known.get(ak, []):
            if titles_match(title, old, author):
                return True
        # Work-level safeguards for catalogued collections whose title differs from the imported edition.
        existing = self.known.get(ak, [])
        if author == "Clement of Alexandria" and "stromata" in title.casefold() and any("stromata" in x.casefold() for x in existing):
            return True
        if author == "John Chrysostom" and "correspondence" in title.casefold() and any("innocent" in x.casefold() and "correspondence" in x.casefold() for x in existing):
            return True
        return False

    def add(self, author, title, body, rels, meta=None, min_words=120):
        body = clean(body); words = len(re.findall(r"\S+", body))
        if len(body) < 700 or words < min_words:
            self.skipped["too_short"] += 1; return False
        if self.duplicate(author, title):
            self.skipped["inventory_duplicate"] += 1; return False
        ak = author_key(author)
        if any(author_key(item["author"]) == ak and titles_match(title, item["title"], author) for item in self.items):
            self.skipped["batch_duplicate"] += 1; return False
        m = meta or metadata_for(author, rels, title)
        self.items.append({
            "author": author, "title": title, "body": body, "rels": rels,
            "date": m.get("date",""), "translator": m.get("translator",""), "editor": m.get("editor",""),
            "edition": m.get("edition",""), "url": m.get("url",raw_url(rels[0])),
            "description": m.get("description",""), "rights_notes": m.get("rights_notes","")
        })
        return True

    def add_single(self, author, title, rel, desc="", min_words=120):
        body = read_html(self.root, rel)
        return self.add(author, title, body, [rel], metadata_for(author, [rel], title, desc), min_words)

    def add_group(self, author, title, rels, desc="", min_words=300):
        parts = []
        for rel in sorted(rels, key=natural_key):
            body = read_html(self.root, rel)
            if not body: continue
            stem = Path(rel).stem
            if re.search(r"(chapter|homily|book|part|discourse|letter)\s*\d+", stem, re.I):
                body = f"## {stem.replace('_',' ')}\n\n{body}"
            parts.append(body)
        if not parts: return False
        m = metadata_for(author, rels, title, desc or f"Complete surviving text presented as one work with original divisions retained.")
        m["url"] = repo_url(rels[0].split("/")[0])
        return self.add(author, title, "\n\n".join(parts), rels, m, min_words)

def add_pelagius(b):
    try:
        r = SESSION.get("https://www.sophiascriptura.com/data/", timeout=45); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = []
        for tr in soup.find_all("tr"):
            t = tr.get_text(" ", strip=True)
            if "Pelagius" not in t or "Exposition on" not in t or "Complete" not in t: continue
            title_a = next((a for a in tr.find_all("a") if a.get_text(" ", strip=True).startswith("Exposition on")), None)
            md_a = next((a for a in tr.find_all("a") if a.get_text(" ", strip=True) == "Markdown"), None)
            if title_a and md_a:
                rows.append((title_a.get_text(" ", strip=True), urljoin("https://www.sophiascriptura.com", title_a.get("href","")), urljoin("https://www.sophiascriptura.com", md_a.get("href",""))))
        for title, work_url, md_url in rows:
            try:
                read_url = re.sub(r"/index\.md(?:\?.*)?$", "/read/", md_url)
                resp = SESSION.get(read_url, timeout=60)
                if resp.status_code != 200 or len(resp.text) < 2000:
                    resp = SESSION.get(work_url.rstrip("/") + "/read/", timeout=60)
                resp.raise_for_status()
                ps = BeautifulSoup(resp.text, "html.parser")
                main = ps.find("main") or ps.find("article") or ps.body or ps
                for n in list(main.find_all(["nav","header","footer","button","input","script","style"])): n.decompose()
                out = []
                h1 = main.find("h1")
                if h1: out.append("## " + clean(h1.get_text(" ", strip=True)))
                skip = False
                for node in main.find_all(["h2","h3","h4","p"]):
                    val = clean(node.get_text(" ", strip=True))
                    if not val: continue
                    if node.name in ("h2","h3","h4"):
                        if re.search(r"notes and citation|notes on the original text|bibliography|sources and references", val, re.I):
                            skip = True; continue
                        if node.name == "h2": skip = False
                        if not skip and val.casefold() not in ("contents","full text"):
                            out.append(("## " if node.name == "h2" else "### ") + val)
                    elif not skip:
                        if re.match(r"^(?:Souter,\s*Texts and Studies|Cite \(SBL\)|Permanent link|Translation:|Summary:|Page \d+ of \d+)", val, re.I): continue
                        if val.startswith("The whole work on one page"): continue
                        out.append(val)
                body = clean("\n\n".join(out))
                if len(body.split()) < 120:
                    md = SESSION.get(md_url, timeout=60); md.raise_for_status()
                    body = clean(re.sub(r"(?m)^\s*(?:Souter, Texts and Studies|Cite \(SBL\)|Permanent link|Translation:|Summary:).*$", "", md.text))
                meta = {
                    "date": "c. AD 405–410",
                    "translator": "Sophia Scriptura English rendering (CC0); Latin text edited by Alexander Souter",
                    "editor": "Alexander Souter",
                    "edition": "Alexander Souter, ed., Pelagius's Expositions of Thirteen Epistles of St Paul, Texts and Studies 9.2 (Cambridge: Cambridge University Press, 1926); CC0 English rendering by Sophia Scriptura",
                    "url": work_url,
                    "description": f"Complete surviving exposition on {title.replace('Exposition on ','')}, from Pelagius' thirteen Pauline commentaries. Latin text follows Souter's 1926 public-domain edition; the parallel English rendering is marked CC0 by Sophia Scriptura. The source notes/citation apparatus is omitted.",
                    "rights_notes": "The 1926 Souter Latin edition is public domain. Sophia Scriptura labels its English rendering CC0. The commentary is authentically attributed to Pelagius and predates the later controversy."
                }
                b.add("Pelagius", title, body, [work_url], meta, min_words=120)
            except Exception as e:
                print(f"PELAGIUS_FETCH_SKIPPED={title}: {e}")
    except Exception as e:
        print(f"PELAGIUS_DISCOVERY_FAILED={e}")

def add_manual(b):
    root = b.root
    # Theophylact: one complete commentary per Bible book, not separate chapter imports.
    base = "Theophylact of Ohrid"
    d = root / base
    if d.exists():
        for child in sorted(d.iterdir(), key=lambda p: natural_key(p.name)):
            if not child.is_dir() or not child.name.startswith("Commentary on "): continue
            rels = []
            for p in child.glob("*.html"):
                if p.stem.casefold() in ("preface", "introduction", "index", "contents", "table of contents"):
                    continue
                if re.fullmatch(r"(?:Chapter\s+\d+|\d+\s+[A-Za-z]+|[A-Za-z]+)", p.stem, re.I):
                    rels.append(str(p.relative_to(root)).replace("\\","/"))
            rels = sorted(set(rels), key=natural_key)
            if rels:
                b.add_group(base, child.name, rels,
                    f"Complete surviving commentary on {child.name.replace('Commentary on ','')}; source chapters joined in canonical order. Prefaces and editorial apparatus are excluded.", 250)
    # Ambrosiaster works not present under this author in the latest inventory.
    for name, title in [
        ("Commentary on 2 Corinthians.html","Commentary on 2 Corinthians"),
        ("Commentary on 2 Thessalonians.html","Commentary on 2 Thessalonians"),
        ("Commentary on 2 Timothy.html","Commentary on 2 Timothy"),
        ("Commentary on Romans.html","Commentary on Romans"),
        ("Exorcism.html","Exorcism"),
        ("Questions on the Old and New Testaments.html","Questions on the Old and New Testaments"),
    ]:
        rel=f"Ambrosiaster/{name}"
        if (root/rel).is_file(): b.add_single("Ambrosiaster",title,rel,"A surviving work of the anonymous fourth-century Latin exegete conventionally called Ambrosiaster.")
    # Theodoret: two additional, distinct works.
    for name,title in [("Counter-Statements of Theodoret.html","Counter-Statements of Theodoret"),("Demonstrations by Syllogisms.html","Demonstrations by Syllogisms")]:
        rel=f"Theodoret of Cyrus/{name}"
        if (root/rel).is_file(): b.add_single("Theodoret of Cyrus",title,rel,"A distinct theological work by Theodoret, separate from his already-catalogued Church History and Eranistes dialogues.")
    # New or underrepresented authors with complete standalone texts.
    for author,title,rel,desc in [
        ("Macarius Magnes","Apocriticus","Macarius Magnes/Apocriticus/English Translation.html","Macarius Magnes' apologetic dialogue answering pagan objections to Christianity."),
        ("Palladius of Galatia","The Lausiac History","Palladius of Galatia/The Lausiac History/Text.html","Palladius' account of Egyptian and Palestinian monastic life; the main narrative is included without its modern introduction."),
        ("Isidore of Seville","Chronicon","Isidore of Seville/Chronicon/English translation.html","Isidore's chronological history in the historic English translation supplied by the source collection."),
        ("Aeneas of Gaza","Theophrastus","Aeneas of Gaza/Theophrastus/English translation.html","Aeneas of Gaza's Christian philosophical dialogue, presented as a continuous work."),
    ]:
        if (root/rel).is_file(): b.add_single(author,title,rel,desc)
    # Oecumenius: distinct complete biblical commentaries.
    for title,rel in [
        ("Commentary on Acts","Oecumenius/Commentary on Acts/Translation-Litteral.html"),
        ("Commentary on Galatians","Oecumenius/Commentary on Galatians/Translation-Litteral.html"),
        ("Commentary on Philemon","Oecumenius/Commentary on Philemon/Translation-Litteral.html"),
        ("Commentary on Revelation","Oecumenius/Commentary on Revelation/Translation-Litteral.html"),
        ("Commentary on the Catholic Epistles","Oecumenius/Commentary on the Catholic Epistles/Translation-Litteral.html"),
        ("Commentary on the Pastoral Epistles","Oecumenius/Commentary on the Pastoral Epistles/Translation-Litteral.html"),
    ]:
        if (root/rel).is_file(): b.add_single("Oecumenius",title,rel,f"Oecumenius' complete surviving {title.lower()}, using the public-domain translation preserved in the source collection.")
    # Rabanus Maurus' extant commentaries.
    for title,rel in [
        ("Commentary on 1 Maccabees","Rabanus Maurus/Commentary_on_1_Maccabees.html"),
        ("Commentary on 2 Maccabees","Rabanus Maurus/Commentary on 2 Maccabees.html"),
        ("Commentary on the Wisdom of Solomon","Rabanus Maurus/Commentary_on_the_Wisdom_Of_Solomon.html"),
    ]:
        if (root/rel).is_file(): b.add_single("Rabanus Maurus",title,rel,"A biblical commentary by Rabanus Maurus, kept as a continuous reading text.")
    # Cyril of Alexandria's five-book polemic, treated as one work.
    # Cyril's Five Tomes Against Nestorius are already present as five separate published records.
    # Clement's multi-book works; never import individual book leaves as standalone titles.
    # The Instructor and the Stromata are already present in the latest inventory; do not repackage them.
    # Four Severian translations explicitly released into the public domain.
    docs=[
        ("On Faith and the Natural Law","Severian of Gabala/Homilies/On Faith and the Natural Law/Translation-Sewell-Pearse.doc"),
        ("On Peace","Severian of Gabala/Homilies/On Peace/Translation-Sewell-Pearse.doc"),
        ("On Repentance and Compunction","Severian of Gabala/Homilies/On Repentance and Compunction/Translation-Sewell-Pearse.doc"),
        ("On the Holy Spirit","Severian of Gabala/Homilies/On the Holy Spirit/Translation-Sewell-Pearse.doc"),
    ]
    for title,rel in docs:
        p=root/rel
        if not p.is_file(): continue
        try:
            txt=subprocess.run(["antiword","-w","180",str(p)],check=True,capture_output=True,text=True,errors="replace").stdout
            meta=metadata_for("Severian of Gabala",[rel],title,f"English translation of Severian of Gabala's {title.lower()}.")
            meta.update({"translator":"Bryson Sewell","editor":"Roger Pearse","edition":"English translation commissioned by Roger Pearse and released into the public domain (2014).","rights_notes":"The source collection states that Bryson Sewell's English translation, commissioned by Roger Pearse, was released into the public domain."})
            b.add("Severian of Gabala",title,txt,[rel],meta,180)
        except Exception as e: print(f"SEVERIAN_DOC_SKIPPED={rel}: {e}")

def add_new_author_works(b):
    root = b.root

    # Archelaus of Carrhae: complete public-domain disputation, including the appended same-disputation fragment.
    rel = "Archelaus of Carrhae/The Acts of the Disputation with the Heresiarch Manes.html"
    if (root / rel).is_file():
        body = read_html(root, rel)
        meta = metadata_for("Archelaus of Carrhae", [rel], "The Acts of the Disputation with the Heresiarch Manes",
                            "The surviving account of Archelaus's public disputation with Mani, including the appended fragment from the same disputation.")
        meta.update({"date":"c. AD 278", "translator":"S. D. F. Salmond", "edition":"Ante-Nicene Fathers, Vol. VI (1886)",
                     "editor":"Alexander Roberts, James Donaldson, and A. Cleveland Coxe",
                     "rights_notes":"The English translation appeared in the 1886 Ante-Nicene Fathers. The source repository README states that all its works are public domain. Superscript footnote markers and apparatus are removed."})
        b.add("Archelaus of Carrhae", "The Acts of the Disputation with the Heresiarch Manes", body, [rel], meta, 1500)

    # Berengaudus: remove the translator's introduction and begin at the commentary proper.
    rel = "Berengaudus/Commentary on Revelation/English.html"
    if (root / rel).is_file():
        body = read_html_from_heading(root, rel, r"^On the First Vision$")
        meta = metadata_for("Berengaudus", [rel], "Exposition on the Seven Visions of the Book of Revelation",
                            "Berengaudus's exposition of Revelation, arranged in seven visions. The translator's introduction and editorial notes are excluded; the commentary's divisions are retained.")
        meta.update({"date":"ninth century (traditionally c. AD 892)", "translator":"John Litteral",
                     "edition":"English translation from Patrologia Latina, Vol. 17",
                     "rights_notes":"The translator's own notice states that this English translation was released into the public domain. The source repository README likewise states that all its works are public domain. Translator's introduction is excluded."})
        b.add("Berengaudus", "Exposition on the Seven Visions of the Book of Revelation", body, [rel], meta, 5000)

    # Possidius: retain his own authorial preface and all 31 chapters.
    rel = "Possidius/Life of St Augustine/English translation.html"
    if (root / rel).is_file():
        body = read_html_from_heading(root, rel, r"^LIFE OF SAINT AUGUSTINE WRITTEN BY THE BISHOP POSSIDIUS$")
        meta = metadata_for("Possidius", [rel], "Life of Saint Augustine",
                            "Possidius's complete biography of Augustine, preserving the author's preface and all 31 chapters.")
        meta.update({"date":"c. AD 437", "translator":"Herbert Theberath Weiskotten",
                     "edition":"H. T. Weiskotten, Sancti Augustini Vita scripta a Possidio Episcopo",
                     "rights_notes":"The source transcription identifies the Weiskotten edition and explicitly states 'Public domain — copy freely.' The author's own preface is retained; modern transcription credits are excluded."})
        b.add("Possidius", "Life of Saint Augustine", body, [rel], meta, 1200)

    # Pseudo-Macarius: one corpus of fifty homilies, with Mason's introduction removed.
    rel = "Macarius of Egypt/Fifty Spiritual Homilies.html"
    if (root / rel).is_file():
        body = read_html_from_heading(root, rel, r"^HOMILY I$")
        meta = metadata_for("Pseudo-Macarius", [rel], "Fifty Spiritual Homilies",
                            "The fifty spiritual homilies traditionally attributed to Macarius the Egyptian and conventionally catalogued as Pseudo-Macarius. The translator's introduction is omitted.")
        meta.update({"date":"fourth century (traditional attribution; authorship uncertain)", "translator":"A. J. Mason",
                     "edition":"Fifty Spiritual Homilies of St. Macarius the Egyptian (1921)",
                     "rights_notes":"The source transcription explicitly labels the 1921 English edition public domain in the United States. The translator's introduction is excluded; the homilies and their original headings are retained. Attribution is stated cautiously as Pseudo-Macarius."})
        b.add("Pseudo-Macarius", "Fifty Spiritual Homilies", body, [rel], meta, 10000)

    # Alexander of Lycopolis: complete 1886 public-domain English translation from New Advent.
    try:
        url = "https://www.newadvent.org/fathers/0618.htm"
        response = SESSION.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        marker = None
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "b", "strong"]):
            if re.match(r"^Chapter\s+1\b", tag.get_text(" ", strip=True), re.I):
                marker = tag
                break
        if marker is None:
            for node in soup.find_all(string=re.compile(r"^\s*Chapter\s+1\b", re.I)):
                marker = node.parent
                break
        if marker is None:
            raise RuntimeError("Chapter 1 anchor not found on New Advent page")
        container = marker
        while container.parent and (len(container.get_text(" ", strip=True)) < 15000 or not re.search(r"Chapter\s+10\b", container.get_text(" ", strip=True), re.I)):
            container = container.parent
        def trim_before(node, target):
            for child in list(node.contents):
                if child is target:
                    return True
                contains = child is target or (isinstance(child, Tag) and target in child.descendants)
                if contains:
                    trim_before(child, target)
                    return True
                child.extract()
            return False
        trim_before(container, marker)
        body = html_to_text(str(container))
        meta = metadata_for("Alexander of Lycopolis", ["New Advent: Church Fathers 0618"], "Of the Manicheans",
                            "Alexander of Lycopolis's surviving treatise examining Manichaean teaching, in the 1886 Ante-Nicene Fathers translation.")
        meta.update({"date":"early fourth century (c. AD 301)", "translator":"James B. H. Hawkins",
                     "editor":"Alexander Roberts, James Donaldson, and A. Cleveland Coxe",
                     "edition":"Ante-Nicene Fathers, Vol. VI (1886)", "url":url,
                     "rights_notes":"The English translation appeared in the 1886 Ante-Nicene Fathers, Vol. VI. The translator is credited as James B. H. Hawkins. Introductory notices and navigation are excluded; the treatise begins at Chapter 1."})
        b.add("Alexander of Lycopolis", "Of the Manicheans", body, ["New Advent: Church Fathers 0618"], meta, 1500)
        print("NEW_AUTHOR_SOURCE=Alexander of Lycopolis | Of the Manicheans")
    except Exception as e:
        print(f"NEW_AUTHOR_SOURCE_SKIPPED=Alexander of Lycopolis: {e}")

    # Maruthas's surviving English witness says it is only part of the whole work; omit it.

def html_to_text_newadvent(raw, title):
    soup = BeautifulSoup(raw, "html.parser")
    body = soup.body or soup
    for node in list(body.find_all(["script", "style", "nav", "header", "footer", "form", "svg", "img"])):
        node.decompose()
    # Remove links first, then their containing paragraphs. This avoids touching
    # detached nested tags whose attrs are cleared by BeautifulSoup.decompose().
    for node in list(body.find_all("a")):
        if getattr(node, "attrs", None) is None:
            continue
        tx = node.get_text(" ", strip=True).casefold()
        href = (node.get("href") or "").casefold()
        if "please help support the mission of new advent" in tx or "gumroad.com" in href:
            node.decompose()
    for node in list(body.find_all("p")):
        if getattr(node, "attrs", None) is None:
            continue
        tx = node.get_text(" ", strip=True).casefold()
        if ("please help support the mission of new advent" in tx and len(tx) < 650) or tx == "search:":
            node.decompose()
    wanted = norm(title)
    heading = None
    for h in body.find_all(["h1", "h2", "h3"]):
        txt = norm(h.get_text(" ", strip=True))
        if wanted and (wanted in txt or txt in wanted):
            heading = h
            break
    if heading is None:
        heading = body.find(["h1", "h2", "h3"])
    if heading is not None:
        def trim_before(node, target):
            for child in list(node.contents):
                if child is target:
                    return True
                contains = isinstance(child, Tag) and target in child.descendants
                if contains:
                    trim_before(child, target)
                    return True
                child.extract()
            return False
        trim_before(body, heading)
    # Explicit modern editorial introductions are not part of the primary work.
    intro = None
    for h in body.find_all(["h2", "h3", "h4", "b", "strong"]):
        t = clean(h.get_text(" ", strip=True)).casefold().strip(" .:")
        if t in {"introduction", "introductory note", "introductory notice", "translator's introduction", "translators introduction", "preface to the online edition", "editor's introduction", "editors introduction"}:
            intro = h
            break
    if intro is not None:
        # Only remove this page if the main text has a clear following division heading.
        following = None
        for h in body.find_all(["h2", "h3", "h4"]):
            if h is intro:
                after_intro = True
                continue
            if 'after_intro' in locals() and after_intro:
                if re.match(r"^(?:chapter\s+|[0-9]+[.)]\s|[ivxlcdm]+[.)]\s|letter\s+|epistle\s+|homily\s+|sermon\s+|demonstration\s+)", clean(h.get_text(" ", strip=True)), re.I):
                    following = h
                    break
        if following is None:
            return ""
        def remove_between(node, start, end):
            siblings = list(node.contents)
            if start in siblings and end in siblings:
                i, j = siblings.index(start), siblings.index(end)
                for item in siblings[i:j]:
                    item.extract()
                return True
            for child in siblings:
                if isinstance(child, Tag) and (start in child.descendants or child is start):
                    if remove_between(child, start, end):
                        return True
            return False
        remove_between(intro.parent, intro, following)
    return html_to_text(str(body))


def add_gap_review_candidates(b, gap_csv):
    candidates = []
    with gap_csv.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = (row.get("source_url") or "").strip().replace("http://", "https://")
            author = (row.get("author") or "").strip()
            title = clean(row.get("title") or "")
            domain = re.match(r"https?://([^/]+)", url, re.I)
            if not domain or domain.group(1).casefold() != "www.newadvent.org":
                continue
            if row.get("comparison_status") != "No exact title/author pair found":
                continue
            if row.get("rights_triage") != "candidate_standalone_review":
                continue
            if not author or author.casefold() in {"anonymous", "anonymous (early christian community)", "symmachus", "mara bar serapion", "dionysius", "tertullian", "theodotus"}:
                continue
            if author.casefold() == "clement" and title.casefold() == "two epistles on virginity":
                author = "Pseudo-Clement"
                title = "Two Epistles on Virginity (Pseudo-Clementine)"
            if author.casefold() == "pseudo-clement" and title.casefold() == "the second epistle":
                title = "Second Epistle of Clement to the Corinthians"
            if title.casefold() == "history of armenia" or title.casefold() == "clementine homilies, introductory epistles":
                continue
            if not title or not re.fullmatch(r"(?:18[0-9]{2}|19[0-2][0-9])", (row.get("edition_year") or "").strip()):
                continue
            low = title.casefold()
            if any(x in low for x in ("fragment", "fragments", "extract", "appendix", "index", "introduction", "preface", "spurious", "dubious", "false decretals")):
                continue
            if re.search(r"\b(?:book|part|volume|chapter|section|sections|chapters)\s+(?:[0-9]+|[ivxlcdm]+)\b", low):
                continue
            if re.search(r"\bchapters?\s+[0-9]+\s*[-–—]\s*[0-9]+\b", low):
                continue
            if len(re.findall(r"\S+", row.get("word_count") or "")) == 0:
                continue
            try:
                words = int(re.sub(r"[^0-9]", "", row.get("word_count") or "0"))
            except ValueError:
                continue
            if words < 1000:
                continue
            candidates.append({"author":author,"title":title,"url":url,"row":row,"words":words})
    # New author identities first, then a round-robin across established authors.
    grouped = defaultdict(list)
    for item in candidates:
        ak = author_key(item["author"])
        if b.duplicate(item["author"], item["title"]):
            b.skipped["gap_inventory_duplicate"] += 1
            continue
        if any(author_key(x["author"]) == ak and titles_match(item["title"], x["title"], item["author"]) for x in b.items):
            b.skipped["gap_batch_duplicate"] += 1
            continue
        grouped[ak].append(item)
    for values in grouped.values():
        values.sort(key=lambda x: (-x["words"], natural_key(x["title"])))
    known_keys = set(b.known)
    for item in b.items:
        known_keys.add(author_key(item["author"]))
    author_order = sorted(grouped, key=lambda ak: (ak in known_keys, -len(grouped[ak]), ak))
    ordered = []
    max_rounds = max((len(grouped[a]) for a in author_order), default=0)
    for round_no in range(max_rounds):
        for ak in author_order:
            if round_no < min(len(grouped[ak]), 18):
                ordered.append(grouped[ak][round_no])
    added = 0
    attempted = 0
    per_author = defaultdict(int)
    # Fetch a bounded candidate window concurrently, then preserve round-robin order
    # for deterministic author balance and titles in the final batch.
    fetch_window = ordered[:360]
    fetched = {}
    def fetch_page(item):
        try:
            response = requests.get(item["url"], timeout=15, headers={"User-Agent":"Scriptorium-reading-room-acquisition/1.0"})
            response.raise_for_status()
            return response.text, None
        except Exception as exc:
            return None, exc
    with ThreadPoolExecutor(max_workers=36) as pool:
        futures = {pool.submit(fetch_page, item): item for item in fetch_window}
        for future in as_completed(futures):
            fetched[id(futures[future])] = future.result()
    for item in fetch_window:
        if len(b.items) >= TARGET:
            break
        ak = author_key(item["author"])
        if per_author[ak] >= 18:
            continue
        attempted += 1
        raw, fetch_error = fetched.get(id(item), (None, RuntimeError("missing concurrent fetch result")))
        if fetch_error is not None:
            b.skipped["gap_fetch_or_parse_error"] += 1
            continue
        try:
            body = html_to_text_newadvent(raw, item["title"])
            if not body or len(re.findall(r"\S+", body)) < 850:
                b.skipped["gap_incomplete_or_short"] += 1
                continue
            row = item["row"]
            edition_year = row.get("edition_year", "")
            translator = row.get("translator", "")
            editor = row.get("editor", "")
            m = metadata_for(item["author"], [item["url"]], item["title"],
                             f"A complete standalone Christian historical text by {item['author']}, cleaned from the public-domain New Advent transcription; source introductions and editorial apparatus are omitted where identifiable.")
            m.update({"date":"Date of composition uncertain; see historical author and source edition",
                      "translator":translator, "editor":editor,
                      "edition":f"Public-domain English translation, {edition_year} edition (source credit recorded in the gap inventory)",
                      "url":item["url"],
                      "rights_notes":f"The source is a pre-1931 English edition ({edition_year}) hosted in New Advent's Church Fathers collection. The underlying historic translation is public domain. The text was screened for completeness and cleaned to remove site navigation, footnote apparatus, and explicit modern editorial introductions where identifiable."})
            if b.add(item["author"], item["title"], body, [item["url"]], m, 850):
                added += 1
                per_author[ak] += 1
        except Exception as e:
            b.skipped["gap_fetch_or_parse_error"] += 1
            print(f"GAP_CANDIDATE_SKIPPED={item['author']} | {item['title']} | {e}")
    print(f"GAP_REVIEW_CANDIDATES={len(candidates)}")
    print(f"GAP_REVIEW_ATTEMPTED={attempted}")
    print(f"GAP_REVIEW_ADDED={added}")
    print("GAP_REVIEW_AUTHOR_COUNTS=" + "; ".join(f"{a}:{n}" for a,n in sorted(((next((x['author'] for x in candidates if author_key(x['author'])==ak),ak),v) for ak,v in per_author.items()), key=lambda z:(-z[1],z[0]))))
def add_gap_fillers(b):
    root = b.root

    def group(author, title, rel_dir, desc, minimum=250):
        base = root / rel_dir
        if not base.is_dir(): return False
        rels = []
        for p in base.rglob("*.html"):
            stem = p.stem.casefold()
            low = str(p.relative_to(root)).casefold()
            if any(x in stem or x in low for x in (
                "translator preface", "translator's introduction", "translator’s introduction",
                "preface to the online edition", "introduction to the online edition",
                "table of contents", "contents", "index", "bibliography", "title page",
                "footnotes", "notes moved", "editor's preface", "editor’s preface",
                "list of authors"
            )):
                continue
            rels.append(str(p.relative_to(root)).replace("\\","/"))
        rels = sorted(set(rels), key=natural_key)
        if not rels: return False
        return b.add_group(author, title, rels, desc, min_words=minimum)

    def one(author, title, rel, desc, minimum=120):
        if (root / rel).is_file():
            return b.add_single(author, title, rel, desc, min_words=minimum)
        return False

    # Thomas Aquinas: the four Catena Aurea Gospels and the four complete divisions of the Summa.
    # Each collection stays intact; chapters/questions are retained as headings inside one file.
    for gospel in ("Matthew", "Mark", "Luke", "John"):
        group("Thomas Aquinas", f"Catena Aurea — Commentary on {gospel}",
              f"Thomas Aquinas/Catena Aurea/Commentary on {gospel}",
              f"Thomas Aquinas's Catena Aurea commentary on the Gospel of {gospel}, with the original chapter divisions retained.", 1000)
    for part in ("First Part", "First Part of the Second Part", "Second Part of the Second Part", "Third Part"):
        group("Thomas Aquinas", f"Summa Theologiae — {part}",
              f"Thomas Aquinas/Summa Theologiae/{part}",
              f"The complete {part} of Thomas Aquinas's Summa Theologiae, assembled from the source's question-and-article pages in canonical order.", 5000)
    for rel, title in [
        ("Thomas Aquinas/On Being and Essence.html", "On Being and Essence"),
        ("Thomas Aquinas/Compendium of Theology.html", "Compendium of Theology"),
        ("Thomas Aquinas/On the Perfection of the Spiritual Life.html", "On the Perfection of the Spiritual Life"),
        ("Thomas Aquinas/On the Unity of the Intellect Against the Averroists.html", "On the Unity of the Intellect Against the Averroists"),
        ("Thomas Aquinas/On the Eternity of the World.html", "On the Eternity of the World"),
        ("Thomas Aquinas/On Kingship to the King of Cyprus.html", "On Kingship to the King of Cyprus"),
    ]:
        one("Thomas Aquinas", title, rel, f"A distinct theological or philosophical work by Thomas Aquinas: {title}.", 200)

    # Bonaventure: separate complete works/collections, not chapter fragments.
    for title, rel_dir in [
        ("Breviloquium", "Bonaventure/Breviloquium"),
        ("Collations on the Ten Commandments", "Bonaventure/Collationes de Decem Praeceptis"),
        ("Collations on the Seven Gifts of the Holy Spirit", "Bonaventure/Collationes de Septem Donis"),
        ("Collations on the Hexaemeron", "Bonaventure/Collations on the Hexaemeron"),
        ("Commentary on Ecclesiastes", "Bonaventure/Commentary on Ecclesiastes"),
        ("Commentary on the Gospel of John", "Bonaventure/Commentary on John"),
        ("Commentary on the Gospel of Luke", "Bonaventure/Commentary on Luke"),
        ("Commentary on the Book of Wisdom", "Bonaventure/Commentary on Wisdom"),
        ("The Soul's Journey into God", "Bonaventure/Itinerarium Mentis in Deum"),
        ("Disputed Questions on the Mystery of the Trinity", "Bonaventure/Quaestiones Disputatae/De Mysterio Trinitatis"),
        ("Disputed Questions on Evangelical Perfection", "Bonaventure/Quaestiones Disputatae/De Perfectione Evangelica"),
        ("Disputed Questions on the Knowledge of Christ", "Bonaventure/Quaestiones Disputatae/De Scientia Christi"),
    ]:
        group("Bonaventure", title, rel_dir, f"A complete work or organized series of disputed questions by Bonaventure: {title}.", 250)
    one("Bonaventure", "The Reduction of the Arts to Theology",
        "Bonaventure/De Reductione Artium ad Theologiam/Opusculum de reductione artium ad theologiam.html",
        "Bonaventure's short theological treatise on the ordering of the arts toward theology.", 100)

    # A genuinely unrepresented author in the inventory: 13 Marian sermons by Francis de Sales.
    sales_dir = root / "Francis de Sales/SERMONS ON OUR LADY"
    if sales_dir.is_dir():
        for p in sorted(sales_dir.glob("*.html"), key=lambda x: natural_key(x.name)):
            title = p.stem.strip()
            one("Francis de Sales", title, str(p.relative_to(root)).replace("\\","/"),
                f"A sermon by Francis de Sales on the Blessed Virgin Mary: {title}.", 100)

    # Gregory the Dialogist: homilies and Ezekiel sermons are distinct from the already-catalogued
    # Dialogues, Moralia on Job, Register of Epistles and Pastoral Rule.
    one("Gregory the Dialogist", "Forty Homilies on the Gospels",
        "Gregory the Dialogist/40 Homilies on the Gospels.html",
        "Gregory the Great's collection of forty Gospel homilies, distinct from his Dialogues and Moralia on Job.", 1500)
    group("Gregory the Dialogist", "Homilies on Ezekiel",
          "Gregory the Dialogist/Homilies on Ezekiel",
          "Gregory the Great's two books of homilies on the prophet Ezekiel.", 1000)
    group("Gregory the Dialogist", "Commentary on 1 Kings",
          "Gregory the Dialogist/Commentary on 1 Kings",
          "The surviving books of Gregory the Great's commentary on 1 Kings, kept together.", 1000)

    # Bernard's Sermons on the Song of Songs are not the same as his letters or Malachy sermons.
    group("Bernard of Clairvaux", "Sermons on the Song of Songs",
          "Bernard of Clairvaux/Sermons on the Song of Songs",
          "Bernard of Clairvaux's sermons on the Song of Songs, with all surviving sermon divisions preserved.", 3000)

    # Gildas: distinct works beyond The Ruin of Britain already in the catalogue.
    one("Gildas", "Lorica", "Gildas the Wise/Lorica/English translation.html",
        "The penitential protective prayer traditionally attributed to Gildas, presented separately from The Ruin of Britain.", 80)
    one("Gildas", "Penitential", "Gildas the Wise/Penitential/English translation.html",
        "The penitential attributed to Gildas, a separate text from The Ruin of Britain.", 80)

    # New authors whose principal works are absent from the inventory.
    one("Didymus the Blind", "On Genesis", "Didymus the Blind/On Genesis.html",
        "Didymus the Blind's surviving exposition on Genesis.", 500)
    one("Eusebius of Emesa", "Homily on the Sufferings and Death of Our Lord",
        "Eusebius of Emesa/Homily on the sufferings and death of our Lord.html",
        "A homily attributed to Eusebius of Emesa on the Passion of Christ.", 100)
    one("Eusebius of Emesa", "On Repentance", "Eusebius of Emesa/On Repentance.html",
        "A separate homily or treatise on repentance attributed to Eusebius of Emesa.", 100)
    for title, rel in [
        ("A Declaration of Faith", "Gregory of Neocaesarea/A Declaration of Faith.html"),
        ("A Metaphrase of Ecclesiastes", "Gregory of Neocaesarea/A Metaphrase of the Book of Ecclesiastes.html"),
        ("Canonical Epistle", "Gregory of Neocaesarea/Canonical Epistle.html"),
        ("The Oration and Panegyric Addressed to Origen", "Gregory of Neocaesarea/The Oration and Panegyric Addressed to Origen.html"),
    ]:
        one("Gregory of Neocaesarea", title, rel, f"A distinct surviving work attributed to Gregory the Wonderworker: {title}.", 100)
    one("Leontius of Byzantium", "Against the Frauds of the Apollinarists",
        "Leontius of Byzantium/Against the frauds of the Apollinarists/English translation.html",
        "Leontius of Byzantium's theological polemic against Apollinarian falsifications.", 250)
    one("Aeneas of Gaza", "Letters", "Aeneas of Gaza/Letters.html",
        "The surviving correspondence of Aeneas of Gaza, presented as one collection.", 200)
    one("Vincent of Lérins", "A Commonitory",
        "Vincent of Lérins/THE COMMONITORY/A Commonitory.html",
        "Vincent of Lérins's treatise on the antiquity and universality of the Catholic faith; modern editorial introductions are not included.", 1000)

    # New gaps by authors already in the catalogue: these are specific missing items, not duplicate editions.
    for author, title, rel in [
        ("John Cassian", "The Second Conference of Abbot Chaeremon (Conference 12)",
         "John Cassian/Conferences of John Cassian - Part II/12. The Second Conference of Abbot Chaeremon.html"),
        ("John Cassian", "The Second Conference of Abbot Theonas (Conference 22)",
         "John Cassian/Conferences of John Cassian - Part III/22. The Second Conference of Abbot Theonas.html"),
        ("John Cassian", "The Institutes of the Coenobia — Book 6",
         "John Cassian/The Twelve Books on the Institutes of the Coenobia/Book 6.html"),
        ("Gregory of Nazianzus", "Oration 31 — The Fifth Theological Oration",
         "Gregory of Nazianzus/Orations/Oration 31.html"),
    ]:
        one(author, title, rel, f"A specific, previously missing item from {author}'s existing corpus: {title}.", 100)
    hilary_dir = "Hilary of Poitiers/Homilies on the Psalms"
    group("Hilary of Poitiers", "Homilies on Psalms 1, 53, and 130",
          hilary_dir,
          "The three surviving Hilary of Poitiers homilies on Psalms 1, 53, and 130. This is a distinct work collection, separate from De Trinitate and De Synodis.", 500)

    # Isidore's Chronicon is not currently catalogued; retain the translator's explicit public-domain credit.
    one("Isidore of Seville", "Chronicon",
        "Isidore of Seville/Chronicon/English translation.html",
        "Isidore of Seville's chronological history in the English translation that its translator explicitly released into the public domain.", 150)

    # Rabanus and Oecumenius are explicitly requested gap-fillers and are absent from the inventory.
    # Their source-repository README makes an explicit public-domain statement; translator/source credits
    # remain in each metadata header.
    for title, rel in [
        ("Commentary on Acts", "Oecumenius/Commentary on Acts/Translation-Litteral.html"),
        ("Commentary on Galatians", "Oecumenius/Commentary on Galatians/Translation-Litteral.html"),
        ("Commentary on Philemon", "Oecumenius/Commentary on Philemon/Translation-Litteral.html"),
        ("Commentary on Revelation", "Oecumenius/Commentary on Revelation/Translation-Litteral.html"),
        ("Commentary on the Catholic Epistles", "Oecumenius/Commentary on the Catholic Epistles/Translation-Litteral.html"),
        ("Commentary on the Pastoral Epistles", "Oecumenius/Commentary on the Pastoral Epistles/Translation-Litteral.html"),
    ]:
        one("Oecumenius", title, rel, f"Oecumenius's distinct surviving biblical {title.lower()}; translated text is attributed to John Litteral in the source.", 250)
    for title, rel in [
        ("Commentary on 1 Maccabees", "Rabanus Maurus/Commentary_on_1_Maccabees.html"),
        ("Commentary on 2 Maccabees", "Rabanus Maurus/Commentary on 2 Maccabees.html"),
        ("Commentary on the Wisdom of Solomon", "Rabanus Maurus/Commentary_on_the_Wisdom_Of_Solomon.html"),
    ]:
        one("Rabanus Maurus", title, rel, f"Rabanus Maurus's biblical commentary: {title}.", 250)

def build_auto(b):
    # Quotas keep the batch broad: existing authors' missing writings plus complete works from smaller authors.
    priority=[
        ("Leo the Great",32),("John Chrysostom",36),("Origen of Alexandria",38),
        ("Augustine of Hippo",25),("Jerome",22),("Athanasius of Alexandria",18),
        ("Basil of Caesarea",18),("Cyril of Alexandria",14),("Cyprian of Carthage",14),
        ("Hilary of Poitiers",8),("Eusebius of Caesarea",12),("Sulpicius Severus",6),
        ("Clement of Alexandria",6),("Tatian the Assyrian",3),("Victor Vitensis",4),
        ("Gennadius of Massilia",4),("Aphrahat the Persian Sage",5),("Asterius of Amasea",4),
        ("Evagrius Scholasticus",5),("Philoxenus of Mabbug",8),("Gregory of Nyssa",16),
        ("Bede",8),("Ephrem the Syrian",12),("John Cassian",10),("Hippolytus of Rome",10),
        ("Ignatius of Antioch",6),("Justin Martyr",6),("Methodius of Olympus",8),
        ("Cyril of Jerusalem",6),("Evagrius Ponticus",5),("Theophilus of Antioch",4),
        ("Chromatius of Aquileia",4),("John of Ephesus",3),("Salvian the Presbyter",4),
    ]
    folder_alias={"Cyprian of Carthage":"Cyprian","Tertullian of Carthage":"Tertullian"}
    for author,quota in priority:
        if len(b.items)>=TARGET: break
        folder=folder_alias.get(author,author); base=b.root/folder
        if not base.exists(): continue
        before=len(b.items); candidates=[]
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in (".html",".htm"): continue
            rel=str(p.relative_to(b.root)).replace("\\","/")
            low=rel.casefold(); stem=p.stem.strip()
            if any(x in low for x in ("/fragments","/pseudo-","/doubtful","/spurious","preface","introduction","translator","appendix","footnote","index","table of","title page","bibliography","errata","missing portion")):
                continue
            if re.fullmatch(r"(?:book|part|chapter)\s*\d+|(?:book|part)\s*[ivxlcdm]+",stem,re.I):
                continue
            if re.search(r"\bfrom\b",stem,re.I):
                # Files received from another correspondent are not credited to the folder's author.
                continue
            if not re.search(r"(?:homily|sermon|letter|epistle|treatise|oration|discourse|dialogue|life of|against |on |commentary|exposition|apology|rule|creed|history|council)",stem,re.I):
                if len(stem.split())<3: continue
            candidates.append(rel)
        for rel in sorted(candidates,key=natural_key):
            if len(b.items)-before>=quota or len(b.items)>=TARGET: break
            p=Path(rel); stem=p.stem.replace("_"," ").strip(); parent=p.parent.name
            mh=re.match(r"^Homily\s*0*(\d+)$",stem,re.I)
            mh2=re.match(r"^homily_0*(\d+)$",stem,re.I)
            ms=re.match(r"^Sermon\s*0*(\d+)(.*)$",stem,re.I)
            ml=re.match(r"^(?:Letter|Epistle)\s*0*(\d+)(.*)$",stem,re.I)
            if mh or mh2:
                n=int((mh or mh2).group(1))
                subject=parent.replace("On ","").replace("Homilies on ","").replace("Homilies ","").strip()
                title=f"Homily {n} on {subject}"
            elif ms: title=f"Sermon {int(ms.group(1))}{ms.group(2)}".strip()
            elif ml: title=f"Letter {int(ml.group(1))}{ml.group(2)}".strip()
            else: title=stem
            actual=author
            if folder=="Cyprian": actual="Cyprian of Carthage"
            if folder=="Tertullian": actual="Tertullian of Carthage"
            try:
                body=read_html(b.root,rel)
                desc=f"Standalone public-domain {('homily' if 'homily' in title.casefold() else 'sermon' if 'sermon' in title.casefold() else 'letter' if 'letter' in title.casefold() else 'work')} by {actual}, transcribed from the historic source edition. Editorial footnotes and apparatus are removed."
                meta=metadata_for(actual,[rel],title,desc)
                if actual=="Tatian the Assyrian":
                    meta.update({"edition":"Ante-Nicene Fathers, Vol. II (1885)","translator":"J. E. Ryland"})
                b.add(actual,title,body,[rel],meta,100)
            except Exception as e:
                print(f"AUTO_SKIPPED={rel}: {e}")

def write_outputs(b,out):
    out.mkdir(parents=True,exist_ok=True)
    if len(b.items)<TARGET:
        raise RuntimeError(f"Only {len(b.items)} new eligible works passed duplicate and quality checks; need {TARGET}.")
    items=b.items[:TARGET]
    for i, item in enumerate(items):
        ak = author_key(item["author"])
        if any(author_key(old["author"]) == ak and titles_match(item["title"], old["title"], item["author"]) for old in items[:i]):
            raise RuntimeError(f"Duplicate author/title identity remained: {item['author']} — {item['title']}")
    with (out/"manifest.csv").open("w",encoding="utf-8-sig",newline="") as f:
        fields=["title","author","date","translator","editor","source_edition","source_url","source_files","words","characters","rights_status","description","rights_notes"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for x in items:
            w.writerow({"title":x["title"],"author":x["author"],"date":x["date"],"translator":x["translator"],"editor":x["editor"],
                        "source_edition":x["edition"],"source_url":x["url"],"source_files":"; ".join(x["rels"]),
                        "words":len(re.findall(r"\S+",x["body"])),"characters":len(x["body"]),"rights_status":"public_domain",
                        "description":x["description"],"rights_notes":x["rights_notes"]})
    zp=out/"scriptorium-reading-room-batch-300-2026-11-22.zip"
    with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for x in items:
            fields=[("Title",x["title"]),("Author",x["author"]),("Date",x["date"]),("Translator",x["translator"]),
                    ("Editor",x["editor"]),("Source Edition",x["edition"]),("Source URL",x["url"]),
                    ("Description",x["description"]),("Rights Status","public_domain"),("Rights Notes",x["rights_notes"])]
            header="\n".join(f"{k}: {v}" for k,v in fields)+"\n\n"
            name=unicodedata.normalize("NFKD",f'{x["author"]} - {x["title"]}')
            name="".join(c for c in name if not unicodedata.combining(c))
            name=re.sub(r"[^A-Za-z0-9()' -]+","-",name)
            name=re.sub(r"\s+"," ",name).strip(" .-")[:145].rstrip(" .-")+".txt"
            z.writestr(name,(header+x["body"]+"\n").encode("utf-8"))
    counts=defaultdict(int)
    for x in items: counts[x["author"]]+=1
    readme=["# Scriptorium Reading Room — Batch of 300","",f"- Standalone texts: **{len(items)}**",
            "- The ZIP contains 300 separate TXT files; manifest and README are outside the ZIP.",
            "- Duplicate screening: exact November 22, 2026 inventory, author aliases, previous 200-draft manifest, title similarity and within-batch checks.",
            "- Theological works spanning books/chapters are kept together rather than split into artificial entries.",
            "- Metadata headers use the requested ten fields: Title, Author, Date, Translator, Editor, Source Edition, Source URL, Description, Rights Status, Rights Notes.",
            "- Footnotes, apparatus, navigation, online-editor introductions, indexes and editorial clutter are removed where identifiable.",
            "- Source repository README states that all its works are public domain; the Pelagius English renderings are additionally marked CC0 by Sophia Scriptura.",
            "","## Author counts",""]
    for a,n in sorted(counts.items(),key=lambda q:(-q[1],q[0].casefold())): readme.append(f"- {a}: {n}")
    readme += ["","## Heresiological coverage","",
        "- Pelagius: complete Pauline expositions included; Latin follows Souter (1926), with the site's CC0 English rendering alongside.",
        "- Arius: no independent complete book was found in the source collection; extant material is fragmentary or quoted by opponents, so no falsely complete Arius work was manufactured.",
        "- Nestorius: no additional work was added because the latest inventory already includes the Bazaar in five records and the letters to Celestine.",
        "- Theodoret's five-book Church History and three Eranistes dialogues were excluded as already catalogued.",
        "","## Import","",
        "1. Validate the ZIP in the Reading Room import tool.",
        "2. Review previews and save drafts first.",
        "3. Use the manifest to inspect titles, authors, source editions, word counts and rights notes before publishing.",""]
    (out/"README.md").write_text("\n".join(readme),encoding="utf-8")
    print(f"PREPARED_WORKS={len(items)}")
    print(f"TOTAL_WORDS={sum(len(re.findall(r'\S+',x['body'])) for x in items)}")
    print(f"ZIP_BYTES={zp.stat().st_size}")
    print("AUTHOR_COUNTS="+"; ".join(f"{a}:{n}" for a,n in sorted(counts.items())))
    print("SKIPPED_COUNTS="+"; ".join(f"{k}:{v}" for k,v in sorted(b.skipped.items())))

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source-dir",required=True,type=Path)
    p.add_argument("--inventory-csv",required=True,type=Path)
    p.add_argument("--output-dir",required=True,type=Path)
    p.add_argument("--prior-batch-manifest",required=True,type=Path)
    p.add_argument("--gap-review-csv",required=True,type=Path)
    a=p.parse_args()
    b=Builder(a.source_dir,a.inventory_csv,a.prior_batch_manifest)
    add_pelagius(b)
    add_manual(b)
    add_new_author_works(b)
    add_gap_review_candidates(b,a.gap_review_csv)
    add_gap_fillers(b)
    build_auto(b)
    print(f"ELIGIBLE_BEFORE_TRIM={len(b.items)}")
    write_outputs(b,a.output_dir)

if __name__=="__main__":
    main()
