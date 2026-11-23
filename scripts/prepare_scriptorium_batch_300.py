#!/usr/bin/env python3
"""Build a 300-work, inventory-screened Scriptorium Reading Room ZIP."""
from __future__ import annotations
import argparse, csv, html, re, unicodedata, zipfile, tomllib, subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urljoin, unquote, urlparse
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

def strip_editorial_apparatus(text):
    text = clean(text)
    # If a complete treatise is followed by an appended fragment, retain only the complete work.
    fragment_heading = re.search(r"(?im)^#{1,6}\s+.*\bfragment\b.*$", text)
    if fragment_heading:
        text = text[:fragment_heading.start()].rstrip()
    # Remove footnote/endnote sections and source-site apparatus flattened from HTML.
    cut_patterns = [
        r"(?im)^\s*(?:#{1,6}\s*)?(?:footnotes?|endnotes?|notes to (?:the )?text)\s*$",
        r"(?im)^\s*\[(?:selected\s+)?footnotes?[^\]]*\]\s*$",
        r"(?im)^\s*\[(?:most of the )?footnotes?[^\]]*(?:moved|renumbered|end|selected)[^\]]*\]\s*$",
        r"(?im)^\s*(?:#{1,6}\s*)?(?:about this page|contact information|sticky footer)\s*$",
        r"(?im)^\s*early church fathers\s*[-–—]\s*additional texts\s*$",
        r"(?im)^\s*this text was transcribed by\b",
        r"(?im)^\s*the text was transcribed by\b",
        r"(?im)^\s*contact us\s*\|\s*advertise with new advent\b",
    ]
    cut = len(text)
    for pattern in cut_patterns:
        m = re.search(pattern, text)
        if m:
            cut = min(cut, m.start())
    text = text[:cut].rstrip()

    # Consecutive numbered notes sometimes appear without a "Footnotes" heading.
    m = re.search(r"(?m)^\s*\[1\]\s+", text)
    if m:
        tail = text[m.start():]
        numbered = re.findall(r"(?m)^\s*\[\d+\]\s+", tail)
        if len(numbered) >= 5:
            text = text[:m.start()].rstrip()

    # Remove call numbers and printed page/column markers.
    text = re.sub(r"(?<![A-Za-z0-9])\[\d+\](?![A-Za-z0-9])", "", text)
    text = re.sub(r"\[(?:col\.?\s*\d+|P\.\s*\d+|l\.\s*\d+|RP)\]", "", text, flags=re.I)
    text = re.sub(r"\|\s*\d{1,4}\s*\|", "", text)

    # Remove detached source/transcription/translator notices, not authorial prefaces.
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text):
        p = paragraph.strip()
        if not p:
            continue
        if re.match(r"^(?:commissioned by\b|\[?translated by\b|\[?transcribed by\b|\[?migne\s+pg\b|\[?p\.?\s*\d+\s*\]?\s+indicates\b|this text was transcribed by\b|the text was transcribed by\b)", p, re.I):
            continue
        if re.search(r"the footnotes are [^.;]+[.;].*(?:page numbers are omitted|obvious misprints|transcribed from)", p, re.I):
            continue
        if re.match(r"^(?:public domain\s*[-–—]|copyright\s*©|sticky footer)", p, re.I):
            continue
        paragraphs.append(p)
    return clean("\n\n".join(paragraphs))


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
        elif node.name == "sup":
            node.decompose()
    for node in list(root.find_all("a")):
        href = node.get("href") or ""
        txt = node.get_text(" ", strip=True)
        if href.startswith(("#fn", "#r")) or re.fullmatch(r"\[?\d+\]?", txt):
            node.decompose()
    blocks, buf = [], []
    def editorial_block(t):
        # Old editions often insert an editor's one-paragraph "Argument" before a letter/homily.
        # It is not part of the author's text, unlike chapter headings and the numbered work itself.
        return bool(re.match(r"^(?:argument\s*[.—:\-]|editor(?:'s)?\s+(?:note|introduction)\b|translator(?:'s)?\s+(?:note|introduction)\b)", t, re.I))

    def flush():
        nonlocal buf
        t = clean(" ".join(buf)); buf = []
        if t and not editorial_block(t) and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", t, re.I):
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
            if t and not editorial_block(t) and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", t, re.I):
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
    "Hegemonius": "Archelaus of Carrhae",
    "Macarius of Egypt": "Pseudo-Macarius",
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
    "Bardaisan": "Bardaisan of Edessa (dialogue recorded by Philip; attribution qualified)",
    "Possidius": "Possidius of Calama",
    "Pseudo-Clement": "Pseudo-Clement (anonymous)",
    "Aristides": "Aristides of Athens",
    "Commodianus": "Commodian",
    "Venantius": "Venantius Honorius Clementianus Fortunatus (Venantius Fortunatus)",
    "Venantius Fortunatus": "Venantius Honorius Clementianus Fortunatus (Venantius Fortunatus)",
    "Caius": "Caius of Rome",
    "Arnobius": "Arnobius of Sicca",
    "Anonymous; transmitted under Clement's name": "Pseudo-Clement (anonymous)",
}
def canonical_author(a):
    a = (a or "").strip()
    return ALIASES.get(a, a)

def author_key(a):
    return norm(canonical_author(a))

TITLE_EQUIVALENTS = {

    norm("Commonitorium"): norm("A Commonitory"),
    norm("A Commonitory"): norm("A Commonitory"),
    norm("Against Publishing the Errors"): norm("Against Publishing the Errors of the Brethren"),
    norm("Against Publishing the Errors of the Brethren"): norm("Against Publishing the Errors of the Brethren"),
    norm("Fifth Theological Oration (Oration 31)"): norm("Oration 31 — The Fifth Theological Oration"),
    norm("Oration 31 — The Fifth Theological Oration"): norm("Oration 31 — The Fifth Theological Oration"),
    norm("Homily on the Paralytic"): norm("On the Paralytic Let Down Through the Roof"),
    norm("On the Paralytic Let Down Through the Roof"): norm("On the Paralytic Let Down Through the Roof"),
    norm("Counter-Statements to Cyril's 12 Anathemas"): norm("Counter-Statements of Theodoret"),
    norm("Counter-Statements of Theodoret"): norm("Counter-Statements of Theodoret"),
    norm("Sermon 33 on the New Testament"): norm("Sermon 0XXXIIL"),
    norm("Sermon 0XXXIIL"): norm("Sermon 0XXXIIL"),
    norm("The Oration of the Emperor Constantine"): norm("Oration of Constantine"),
    norm("Oration of Constantine"): norm("Oration of Constantine"),
    norm("Councils of Ariminum and Seleucia"): norm("De Synodis"),
    norm("De Synodis"): norm("De Synodis"),
    norm("The Pearl-Seven Hymns on the Faith"): norm("The Pearl"),
    norm("The Pearl"): norm("The Pearl"),
    norm("1. First Conference of Abbot Moses"): norm("Conference 1"),
    norm("Conference 1"): norm("Conference 1"),
    norm("2. Second Conference of Abbot Moses"): norm("Conference 2"),
    norm("Conference 2"): norm("Conference 2"),
    norm("3. Conference of Abbot Paphnutius"): norm("Conference 3"),
    norm("Conference 3"): norm("Conference 3"),
    norm("4. Conference of Abbot Daniel"): norm("Conference 4"),
    norm("Conference 4"): norm("Conference 4"),
    norm("5. Conference of Abbot Serapion"): norm("Conference 5"),
    norm("Conference 5"): norm("Conference 5"),
    norm("6. Conference of Abbot Theodore"): norm("Conference 6"),
    norm("Conference 6"): norm("Conference 6"),
    norm("7. First Conference of Abbot Serenus"): norm("Conference 7"),
    norm("Conference 7"): norm("Conference 7"),
    norm("8. The Second Conference of Abbot Serenus"): norm("Conference 8"),
    norm("Conference 8"): norm("Conference 8"),
    norm("9. The First Conference of Abbot Isaac"): norm("Conference 9"),
    norm("Conference 9"): norm("Conference 9"),
    norm("Commonitorium"): norm("A Commonitory"),
    norm("A Commonitory"): norm("A Commonitory"),
    norm("Against Publishing the Errors"): norm("Against Publishing the Errors of the Brethren"),
    norm("Against Publishing the Errors of the Brethren"): norm("Against Publishing the Errors of the Brethren"),
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
        r"(?:[,;:]?\s+)(book|part|volume|vol\.?|tome|letter|epistle|sermon|homily|discourse|dialogue|chapters?)\s*"
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
        r"(?:[,;:]?\s+)(?:book|part|volume|vol\.?|tome|chapter|chapters|dialogue)\s*"
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


def work_overlap(candidate, existing, author=None):
    """Catch collection/component overlaps using punctuation-insensitive compact text."""
    ak = author_key(author or "")
    compact = lambda s: re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold())
    c = compact(candidate)
    e = compact(existing)
    if ak == author_key("John Chrysostom"):
        if c == compact("Letters to Olympias") and (compact("to olympias") in e or compact("to my lady") in e):
            return True
        if c == compact("To Theodore After His Fall") and e in {compact("Letter 1"), compact("Letter 2")}:
            return True
    if ak == author_key("Hilary of Poitiers"):
        if re.fullmatch(r"homilyonpsalm(1|53|130)", c) and compact("Homilies on Psalms 1, 53, and 130") in e:
            return True
    if ak == author_key("Ignatius of Antioch"):
        def simplify(v):
            x=re.sub(r"^(?:epistle\s+to\s+|ignatius\s+to\s+|to\s+)", "", v.casefold().strip())
            x=re.sub(r"^st\.?\s+", "", x)
            x=re.sub(r"\s*(?:\(|—|-)?\s*(?:longer recension|syriac version)\)?$", "", x).strip()
            return x
        if simplify(candidate) == simplify(existing):
            return True
    if ak == author_key("Aphrahat the Persian Sage"):
        if "demonstrations" in e and "demonstration" in c:
            return True
    if ak == author_key("Aristides of Athens"):
        if "apology" in c and "apology" in e:
            return True
    if ak == author_key("Lactantius"):
        if "workmanship of god" in c and "workmanship of god" in e:
            return True
    if ak == author_key("Eusebius of Caesarea"):
        if "constantine" in c and "constantine" in e and "oration" in c and "oration" in e:
            return True
    if ak == author_key("Basil of Caesarea"):
        if "on the spirit" in c and ("holy spirit" in e or "spiritus sancto" in e):
            return True
    if ak == author_key("Cyprian of Carthage"):
        if ("seventh council of carthage" in c or "baptism of heretics" in c) and ("seventh council of carthage" in e or "baptism of heretics" in e):
            return True
    if ak == author_key("Sulpicius Severus"):
        if re.sub(r"\\bdialogue\\s+(?:i|ii|iii)\\b", lambda m: "dialogue "+str({"i":1,"ii":2,"iii":3}[m.group(0).split()[-1]]), candidate.casefold()) == \
           re.sub(r"\\bdialogue\\s+(?:i|ii|iii)\\b", lambda m: "dialogue "+str({"i":1,"ii":2,"iii":3}[m.group(0).split()[-1]]), existing.casefold()):
            return True
    # Asterius's source labels put sermon numbers before the same descriptive work titles.
    stripped=re.sub(r"^\s*sermon\s+\d+\s*[-:—–]*\s*", "", candidate, flags=re.I)
    if stripped and norm(stripped)==norm(existing):
        return True
    return False

def titles_match(candidate, existing, author=None):
    if work_overlap(candidate, existing, author):
        return True
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


def source_path_key(value):
    value = unquote(str(value or "")).replace("\\", "/").strip()
    if not value:
        return ""
    if "://" in value:
        path = urlparse(value).path
        # GitHub tree URLs identify directories/collections, not unique source files.
        if "/tree/" in path:
            return ""
        for marker in ("/blob/master/", "/master/"):
            if marker in path:
                value = path.split(marker, 1)[1]
                break
        else:
            value = path
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts=value.split("/")
    while parts and parts[0].casefold() in {"writings_db_snapshot","writings-database-master","writings-database"}:
        parts.pop(0)
    value="/".join(parts)
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold())

def numbered_series_signature(value):
    raw = clean(value or "").casefold()
    patterns = [
        (r"\bconference\s*([0-9]+|[ivxlcdm]+)\b", "conference"),
        (r"^\s*([0-9]+)\.\s*(?:the\s+)?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\w+)\s+conference\b", "conference"),
        (r"^\s*([0-9]+)\.\s*(?:the\s+)?conference\b", "conference"),
        (r"\b(treatise|epistle|letter|sermon|homily|demonstration|oration|dialogue)\s*([0-9]+|[ivxlcdm]+)\b", None),
    ]
    roman = {"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
    def to_int(v):
        if v.isdigit(): return int(v)
        total=0; prev=0
        for c in reversed(v):
            n=roman.get(c,0); total += -n if n < prev else n; prev=max(prev,n)
        return total
    for pattern, fixed in patterns:
        m=re.search(pattern,raw,re.I)
        if not m: continue
        if fixed:
            return (fixed,to_int(m.group(1)))
        kind=m.group(1).casefold()
        if kind in ("letter","epistle"): kind="epistle"
        return (kind,to_int(m.group(2)))
    return None

def source_is_known_compound(author, candidate, existing):
    ak=author_key(author)
    c=norm(candidate); e=norm(existing)
    if ak == author_key("Pseudo-Macarius") and "fiftyspiritualhomilies" in c and "fiftyspiritualhomilies" in e:
        return True
    if ak == author_key("Aphrahat the Persian Sage") and "demonstrations" in e and re.search(r"\bdemonstration\s+[0-9]+\b", candidate, re.I):
        return True
    if ak == author_key("Cyprian of Carthage") and re.search(r"\btreatise\s*12\b", candidate, re.I) and re.search(r"\btreatise\s*12\b", existing, re.I):
        return True
    if ak == author_key("Lactantius") and "workmanshipofgod" in c and "workmanshipofgod" in e:
        return True
    if ak == author_key("Cyprian of Carthage") and any(x in c for x in ("baptismofheretics","seventhcouncilofcarthage")) and any(x in e for x in ("baptismofheretics","seventhcouncilofcarthage")):
        return True
    if ak == author_key("Gregory the Great") and "dialogues" in c and "dialogues" in e:
        return True
    if ak == author_key("Basil of Caesarea"):
        if "hexaemeron" in c and "hexaemeron" in e and numbered_series_signature(candidate)==numbered_series_signature(existing):
            return True
    stripped=re.sub(r"^\s*sermon\s+\d+\s*[-:—–]*\s*", "", candidate, flags=re.I)
    if stripped and norm(stripped)==e:
        return True
    return False

def global_title_key(value):
    key = re.sub(r"^(?:the|a|an)", "", norm(value or ""))
    # Known title variants across anonymous/pseudonymous catalogue labels.
    if "protevangelium" in key or "protoevangelium" in key:
        return "protoevangeliumofjames"
    if "perpetua" in key and "felicit" in key:
        return "passionofperpetuaandfelicity"
    if "liturgy" in key and "james" in key:
        return "liturgyofsaintjames"
    if "liturgy" in key and "mark" in key and ("apostle" in key or "saint" in key):
        return "liturgyofsaintmark"
    return key

class Builder:
    def __init__(self, root, inventory_path, prior_batch_manifest=None):
        self.root = root; self.items = []; self.known = defaultdict(list); self.seen = set()
        self.known_paths = set(); self.seen_paths = set()
        self.known_global_titles = set(); self.seen_global_titles = set()
        self.skipped = defaultdict(int)
        with inventory_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                ak = author_key(r.get("attributed_author", "")); t = r.get("title", "")
                if ak and t: self.known[ak].append(t)
                if t: self.known_global_titles.add(global_title_key(t))
                sk=source_path_key(r.get("source_url",""))
                if sk: self.known_paths.add(sk)
        self.prior_batch_rows = 0
        if prior_batch_manifest:
            with prior_batch_manifest.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    ak = author_key(r.get("author", "")); t = r.get("title", "")
                    if ak and t:
                        self.known[ak].append(t)
                        self.known_global_titles.add(global_title_key(t))
                        self.prior_batch_rows += 1
                    for value in [r.get("source_url",""), *(r.get("source_files","") or "").split(";")]:
                        sk=source_path_key(value)
                        if sk: self.known_paths.add(sk)
        print(f"PRIOR_BATCH_ROWS_EXCLUDED={self.prior_batch_rows}")
        print(f"INVENTORY_AND_PRIOR_SOURCE_IDENTITIES={len(self.known_paths)}")

    def duplicate(self, author, title, rels=()):
        ak = author_key(author)
        for rel in rels:
            sk=source_path_key(rel)
            if sk and (sk in self.known_paths or sk in self.seen_paths):
                return True
        # Long exact title matches are also checked across catalogue author labels.
        global_key=global_title_key(title)
        if len(global_key)>=16 and (global_key in self.known_global_titles or global_key in self.seen_global_titles):
            return True
        for old in self.known.get(ak, []):
            if titles_match(title, old, author) or source_is_known_compound(author,title,old):
                return True
            c_sig=numbered_series_signature(title)
            o_sig=numbered_series_signature(old)
            if c_sig and o_sig and c_sig==o_sig:
                return True
        # Work-level safeguards for catalogued collections whose title differs from the imported edition.
        existing = self.known.get(ak, [])
        if author == "Clement of Alexandria" and "stromata" in title.casefold() and any("stromata" in x.casefold() for x in existing):
            return True
        if author == "John Chrysostom" and "correspondence" in title.casefold() and any("innocent" in x.casefold() and "correspondence" in x.casefold() for x in existing):
            return True
        # Individual letters to Olympias are already catalogued; don't add a retitled collection of them.
        if author == "John Chrysostom" and "olympias" in title.casefold() and any("olympias" in x.casefold() for x in existing):
            return True
        # A grouped set of Hilary's psalm homilies already covers its constituent sermon titles.
        if author == "Hilary of Poitiers" and re.search(r"homily\s+on\s+psalm\s+(1|53|130)\b", title, re.I):
            number = re.search(r"homily\s+on\s+psalm\s+(1|53|130)\b", title, re.I).group(1)
            if any("homilies on psalms" in x.casefold() and re.search(r"\b" + number + r"\b", x) for x in existing):
                return True
        return False

    def add(self, author, title, body, rels, meta=None, min_words=120):
        author = canonical_author(author)
        body = strip_editorial_apparatus(clean(body))
        # Keep this Reading Room batch focused on complete writings rather than
        # separately catalogued fragments or sermon excerpts.
        if re.search(r"\bfragments?\b", title, re.I) or "[FRAGMENT]" in body.upper():
            self.skipped["fragment_or_excerpt"] += 1
            return False
        if author_key(author) == author_key("Origen of Alexandria") and norm(title) in {"from greek","from latin"}:
            self.skipped["fragment_or_excerpt"] += 1
            return False
        if author_key(author) == author_key("Methodius of Olympus") and "banquet of the ten virgins" in title.casefold() and re.search(r"\bdiscourse\s*4\b", title, re.I):
            self.skipped["fragment_or_excerpt"] += 1
            return False
        if re.search(r"\b(?:dubious and spurious pieces|quotes on revelation|book\s*2\s*\(start\)|praefatio|from the discourse on the resurrection|new documents on philoxenus)\b", title, re.I):
            self.skipped["fragment_or_editorial"] += 1
            return False
        if author_key(author) == author_key("Malchion"):
            self.skipped["fragmentary_attribution"] += 1
            return False
        if author_key(author) == author_key("Basil of Caesarea") and re.search(r"homil(?:y|ies)\s+on\s+psalm", title, re.I):
            self.skipped["rights_hold"] += 1
            return False
        rights_identity = (author + " " + title + " " + " ".join(rels)).casefold()
        if any(name in rights_identity for name in (
            "john litteral", "translation-litteral", "gleason l. archer",
            "albert c. outler", "sister agnes clare way"
        )):
            self.skipped["rights_hold"] += 1
            return False
        if ("oecumenius" in rights_identity and ("litteral" in rights_identity or "translation" in rights_identity)):
            self.skipped["rights_hold"] += 1
            return False
        if "rabanus maurus" in rights_identity and ("maccabees" in rights_identity or "wisdom of solomon" in rights_identity):
            self.skipped["rights_hold"] += 1
            return False
        if "ambrosiaster" in rights_identity and ("commentary on romans" in rights_identity or "questions on the old and new testaments" in rights_identity):
            self.skipped["rights_hold"] += 1
            return False
        if "jerome" in rights_identity and "commentary on daniel" in rights_identity:
            self.skipped["rights_hold"] += 1
            return False
        if author_key(author) == author_key("Julius Africanus") and norm(title) == norm("Extant Works"):
            self.skipped["fragment_collection"] += 1
            return False
        words = len(re.findall(r"\S+", body))
        if len(body) < 700 or words < min_words:
            self.skipped["too_short"] += 1; return False
        if self.duplicate(author, title, rels):
            self.skipped["inventory_or_prior_source_duplicate"] += 1; return False
        ak = author_key(author)
        if any(
            author_key(item["author"]) == ak and (
                titles_match(title, item["title"], author)
                or source_is_known_compound(author, title, item["title"])
                or (numbered_series_signature(title) and numbered_series_signature(title)==numbered_series_signature(item["title"]))
            )
            for item in self.items
        ):
            self.skipped["batch_duplicate"] += 1; return False
        m = meta or metadata_for(author, rels, title)
        translator = (m.get("translator") or "").strip() or "Not identified in source edition"
        editor = (m.get("editor") or "").strip() or "Not identified in source edition"
        for rel in rels:
            sk=source_path_key(rel)
            if sk: self.seen_paths.add(sk)
        self.seen_global_titles.add(global_title_key(title))
        self.items.append({
            "author": author, "title": title, "body": body, "rels": rels,
            "date": m.get("date",""), "translator": translator, "editor": editor,
            "edition": m.get("edition",""), "url": m.get("url",raw_url(rels[0])),
            "description": m.get("description",""), "rights_notes": m.get("rights_notes","")
        })
        return True

    def add_single(self, author, title, rel, desc="", min_words=120):
        author = canonical_author(author)
        body = read_html(self.root, rel)
        return self.add(author, title, body, [rel], metadata_for(author, [rel], title, desc), min_words)

    def add_group(self, author, title, rels, desc="", min_words=300):
        author = canonical_author(author)
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
        body = re.split(r"(?im)^#{1,4}\s+A Fragment of the Same Disputation\s*$", body, maxsplit=1)[0].strip()
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
            if author.casefold() == "clement" and title.casefold() in {"two epistles on virginity", "the second epistle"}:
                author = "Pseudo-Clement"
                if title.casefold() == "two epistles on virginity":
                    title = "Two Epistles on Virginity (Pseudo-Clementine)"
                else:
                    title = "Second Epistle of Clement to the Corinthians"
            if author.casefold() == "pseudo-clement" and title.casefold() == "the second epistle":
                title = "Second Epistle of Clement to the Corinthians"
            if title.casefold() == "history of armenia" or title.casefold() == "clementine homilies, introductory epistles":
                continue
            if author.casefold() in {"philoxenus", "philoxenus of mabbug"} and title.casefold().startswith("new documents on philoxenus"):
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
            if round_no < min(len(grouped[ak]), 24):
                ordered.append(grouped[ak][round_no])
    added = 0
    attempted = 0
    per_author = defaultdict(int)
    # Fetch a bounded candidate window concurrently, then preserve round-robin order
    # for deterministic author balance and titles in the final batch.
    fetch_window = ordered[:240]
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
        if per_author[ak] >= 40:
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

    # Methodius's complete Banquet is a full work; never import Discourse 4 or the resurrection excerpt alone.
    one("Methodius of Olympus", "The Banquet of the Ten Virgins",
        "Methodius of Olympus/The Banquet of the Ten Virgins.html",
        "Methodius of Olympus's complete dialogue on virginity, retaining all eleven discourses in the source text.", 1200)

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
            if folder=="Ephrem the Syrian" and re.search(r"^Hymn\s*14\s*plus$",stem,re.I): continue
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


def build_extended_source_pool(b):
    """Fill the remaining slots from complete, nonduplicate texts across the source vault."""
    root=b.root
    skip_top={
        "Thomas Aquinas","Bonaventure","Theophylact of Ohrid","Shepherd of Hermas",
        "Didache","Epistle of Barnabas","Epistle to Diognetus","Optatus of Milevis",
        "Eugippius","John of Ephesus","Rabbula","Shenoute the Archimandrite",
        "Pachomius the Great","Odes of Solomon","Acts of Peter and Paul",
        "Protoevangelium of James","Passion of Saints Perpetua and Felicity",
        "Liturgy of Saint James","Liturgy of Saint Mark","Martyrdom of Colluthus",
        "Ibn al-Tayyib","On the Solstices and the Equinoxes",
        "Religious Discussion at the Court of the Sassanids",
        # Non-Christian/Jewish source material is not used to pad a Christian library batch.
        "Josephus","Philo of Alexandria","Book of Enoch","Book of Jubilees",
        "Book of Biblical Antiquities","Sibylline Oracles","Testaments of the Twelve Patriarchs",
        "Oresiesis-Heru-sa Ast","Papias the Lexicographer","Codex Veronensis",
        "Ammonius of Alexandria","Alexander of Lycopolis","James","John","Jude"
    }
    blocked=(
        "/fragments","/doubtful","/spurious","preface","introduction","translator",
        "appendix","footnote","index","table of","title page","bibliography",
        "errata","missing portion","new documents on philoxenus"
    )
    structural=re.compile(r"^(?:chapter|book|part|discourse|homily|sermon|letter|epistle|vision|commandment|similitude|demonstration|canon)\s*0*\d+\b",re.I)
    tops=[p for p in root.iterdir() if p.is_dir() and p.name not in skip_top]
    ranked=[]
    for top in tops:
        author=canonical_author(top.name)
        ranked.append((len(b.known.get(author_key(author),[])),top.name.casefold(),top,author))
    ranked.sort(key=lambda x:(x[0]>0,x[0],x[1]))
    added=0
    for known_count,_,top,base_author in ranked:
        if len(b.items)>=TARGET: break
        if top.name=="Apostolic Constitutions":
            # Book VIII includes the Ecclesiastical Canons already present in the catalogue.
            continue

        def author_for(directory):
            rel=directory.relative_to(root).as_posix().casefold()
            if top.name=="Tyrannius Rufinus" and "jerome's apology for himself against the books of rufinus" in rel:
                return "Jerome"
            if top.name=="Clement of Rome" and "pseudo-clement" in rel:
                return "Pseudo-Clement (anonymous)"
            if top.name=="Hegesippus" and "pseudo-hegesippus" in rel:
                return "Pseudo-Hegesippus (anonymous)"
            if top.name=="Testaments of the Twelve Patriarchs":
                return "Anonymous (Testaments of the Twelve Patriarchs)"
            return canonical_author(top.name)

        author=canonical_author(top.name)
        cap=30 if known_count==0 else 20
        author_added=0
        dirs=[top]+[p for p in top.rglob("*") if p.is_dir()]
        dirs.sort(key=lambda p:(len(p.relative_to(top).parts),natural_key(str(p))))
        for directory in dirs:
            if len(b.items)>=TARGET or author_added>=cap: break
            if any(p.is_dir() for p in directory.iterdir()): continue
            rel_dir=directory.relative_to(root).as_posix()
            if any(term in ("/"+rel_dir.casefold()) for term in blocked): continue
            files=[p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in (".html",".htm")]
            files=[p for p in files if not any(term in ("/"+p.relative_to(root).as_posix().casefold()) for term in blocked)]
            if not files: continue
            actual=author_for(directory)
            stems=[p.stem.replace("_"," ").strip() for p in files]
            if directory.name.casefold() == "from greek":
                # Origen's Greek source files are short papyrus/catena fragments, not a standalone work.
                continue
            if len(files)>=2 and all(structural.match(x) for x in stems):
                if top.name=="Origen of Alexandria" and directory.name.casefold()=="from latin":
                    title="Commentary on Romans"
                else:
                    title=directory.name.replace("_"," ").strip()
                if title.casefold() in {"first book - visions","second book - commandments","third book - similitudes"}:
                    continue
                rels=[p.relative_to(root).as_posix() for p in files]
                if b.add_group(actual,title,rels,
                    f"A complete multi-part Christian text by {actual}, with its surviving chapters or books kept together in canonical order.",500):
                    author_added+=1
                    added+=1
                continue
            for p in sorted(files,key=lambda q:natural_key(q.name)):
                if len(b.items)>=TARGET or author_added>=cap: break
                stem=p.stem.replace("_"," ").strip()
                if structural.match(stem): continue
                low=p.relative_to(root).as_posix().casefold()
                if any(term in ("/"+low) for term in blocked): continue
                if re.search(r"\bfrom\b",stem,re.I): continue
                title=directory.name.replace("_"," ").strip() if stem.casefold() in {"english translation","english","text","translation"} else stem
                if title.casefold() in {"readme","metadata","contents","index","translator's introduction","preface"}: continue
                rel=p.relative_to(root).as_posix()
                desc=f"A standalone Christian historical or theological work by {actual}, transcribed from the repository source edition. Editorial introductions, footnote apparatus and navigation are removed."
                if b.add_single(actual,title,rel,desc,300):
                    author_added+=1
                    added+=1
    # Texts filed by their work-title rather than a personal author.
    special=[
        ("Anonymous Christian author","The Acts of Peter and Paul","Acts of Peter and Paul/Acts of Peter and Paul.html","The ancient Christian apocryphal Acts of Peter and Paul.",500),
        ("Anonymous Christian author","The Protoevangelium of James","Protoevangelium of James/The Protevangelium of James.html","The early Christian infancy gospel traditionally known as the Protoevangelium of James.",500),
        ("Anonymous Christian author","The Passion of Saints Perpetua and Felicity","Passion of Saints Perpetua and Felicity/The Passion of the Holy Martyrs Perpetua and Felicitas.html","The early Christian martyr narrative of Perpetua, Felicity and their companions.",500),
        ("Anonymous Christian author","The Liturgy of Saint James","Liturgy of Saint James/The Divine Liturgy of James.html","The ancient liturgical text associated with the Liturgy of Saint James.",500),
        ("Anonymous Christian author","The Liturgy of Saint Mark","Liturgy of Saint Mark/The Divine Liturgy of the Holy Apostle and Evangelist Mark.html","The ancient liturgical text associated with the Liturgy of Saint Mark.",500),
        ("Anonymous Christian author","The Martyrdom of Colluthus","Martyrdom of Colluthus/Martyrdom of Colluthus.html","The Christian martyrdom narrative of Colluthus.",200),
    ]
    for author,title,rel,desc,minimum in special:
        if len(b.items)>=TARGET: break
        if (root/rel).is_file(): b.add_single(author,title,rel,desc,minimum)

    # Two complete pseudo-Clementine literary collections; attribution is explicitly pseudonymous.
    pseudo=root/"Clement of Rome"/"Pseudo-Clement"
    if pseudo.is_dir():
        for dirname,title in [("Clementine Homilies","Clementine Homilies"),("Clementine Recognitions","Clementine Recognitions"),("Two Epistles on Virginity","Two Epistles on Virginity")]:
            d=pseudo/dirname
            if not d.is_dir() or len(b.items)>=TARGET: continue
            rels=[p.relative_to(root).as_posix() for p in d.glob("*.html")]
            if len(rels)>=2:
                b.add_group("Pseudo-Clement (anonymous)",title,rels,
                    f"The complete pseudonymous Clementine {title.lower()}, preserved as an early Christian literary work.",1500)

    # Pseudo-Hegesippus is not the second-century Hegesippus.
    d=root/"Hegesippus"/"Pseudo-Hegesippus"/"On the ruin of the city of Jerusalem"
    if d.is_dir() and len(b.items)<TARGET:
        rels=[p.relative_to(root).as_posix() for p in d.glob("Book *.html")]
        if len(rels)>=4:
            b.add_group("Pseudo-Hegesippus (anonymous)","On the Ruin of the City of Jerusalem",rels,
                "A late antique Christian Latin retelling of the Jewish War, traditionally but incorrectly associated with Hegesippus.",2000)

    print(f"EXTENDED_SOURCE_POOL_ADDED={added}")

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
    zip_names=set()
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
            if name.casefold() in zip_names:
                raise RuntimeError(f"Duplicate ZIP filename generated: {name}")
            zip_names.add(name.casefold())
            z.writestr(name,(header+x["body"]+"\n").encode("utf-8"))
    counts=defaultdict(int)
    for x in items: counts[x["author"]]+=1
    readme=["# Scriptorium Reading Room — Batch of 300","",f"- Standalone texts: **{len(items)}**",
            "- The ZIP contains 300 separate TXT files; manifest and README are outside the ZIP.",
            "- Duplicate screening: exact November 22, 2026 inventory, previous 200-work manifest, author aliases, source-file identities, cross-author exact titles, numbered-series and compound-work overlaps, and within-batch checks.",
            "- Theological works spanning books/chapters are kept together rather than split into artificial entries.",
            "- Metadata headers use the requested ten fields: Title, Author, Date, Translator, Editor, Source Edition, Source URL, Description, Rights Status, Rights Notes.",
            "- Footnotes, apparatus, navigation, online-editor introductions, indexes and editorial clutter are removed where identifiable.",
            "- Rights-sensitive translations and incomplete excerpts are held back; only identified public-domain or explicitly released text editions are included.",
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
    # All 13 Pelagius Pauline expositions are already in the previous 200-work batch.
    # Do not refetch and reprocess the completed Pelagius corpus.
    add_manual(b)
    add_new_author_works(b)
    # Protect curated gaps in currently represented authors before broad web-candidate fills.
    add_gap_fillers(b)
    add_gap_review_candidates(b,a.gap_review_csv)
    build_auto(b)
    build_extended_source_pool(b)
    print(f"ELIGIBLE_BEFORE_TRIM={len(b.items)}")
    write_outputs(b,a.output_dir)

if __name__=="__main__":
    main()
