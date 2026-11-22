#!/usr/bin/env python3
"""Build a 200-work, inventory-screened Scriptorium Reading Room ZIP."""
from __future__ import annotations
import argparse, csv, html, re, unicodedata, zipfile, tomllib, subprocess
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urljoin
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SOURCE_REPO = "HistoricalChristianFaith/Writings-Database"
SOURCE_BRANCH = "master"
TARGET = 200
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
}
def author_key(a):
    a = (a or "").strip()
    return norm(ALIASES.get(a, a))

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
    def __init__(self, root, inventory_path):
        self.root = root; self.items = []; self.known = defaultdict(list); self.seen = set()
        self.skipped = defaultdict(int)
        with inventory_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                ak = author_key(r.get("attributed_author", "")); t = r.get("title", "")
                if ak and t: self.known[ak].append(t)

    def duplicate(self, author, title):
        ak = author_key(author); nt = norm(title)
        for old in self.known.get(ak, []):
            no = norm(old)
            if nt == no: return True
            if min(len(nt), len(no)) >= 10 and SequenceMatcher(None, nt, no).ratio() >= 0.88: return True
            a = re.search(r"\b(letter|sermon|homily)\s*(\d+)\b", title, re.I)
            o = re.search(r"\b(letter|sermon|homily)\s*(\d+)\b", old, re.I)
            if a and o and a.group(1).casefold() == o.group(1).casefold() and a.group(2) == o.group(2):
                wa = set(re.findall(r"[a-z]{4,}", title.casefold()))
                wo = set(re.findall(r"[a-z]{4,}", old.casefold()))
                if wa & wo: return True
        # Additional alias/title safeguards for composite works whose catalogue title differs.
        t = title.casefold()
        if author == "Clement of Alexandria" and "stromata" in t and any("stromata" in x.casefold() for x in self.known.get(ak, [])): return True
        return False

    def add(self, author, title, body, rels, meta=None, min_words=120):
        body = clean(body); words = len(re.findall(r"\S+", body))
        if len(body) < 700 or words < min_words:
            self.skipped["too_short"] += 1; return False
        if self.duplicate(author, title):
            self.skipped["inventory_duplicate"] += 1; return False
        key = (author_key(author), norm(title))
        if key in self.seen:
            self.skipped["batch_duplicate"] += 1; return False
        self.seen.add(key)
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
    rels=[f"Cyril of Alexandria/Five tomes against Nestorius/Book {n}.html" for n in range(1,6)]
    if all((root/x).is_file() for x in rels):
        b.add_group("Cyril of Alexandria","Five Tomes Against Nestorius",rels,"Cyril's five-book polemic against Nestorius, combined as one work with book divisions retained.",500)
    # Clement's multi-book works; never import individual book leaves as standalone titles.
    for title, files in [
        ("The Instructor (Paedagogus), Books I–III",[f"The Instructor Book {n}.html" for n in range(1,4)]),
        ("The Stromata, Books I–VIII",[f"The Stromata Book {n}.html" for n in range(1,9)]),
    ]:
        rels=[f"Clement of Alexandria/{x}" for x in files]
        if all((root/x).is_file() for x in rels):
            b.add_group("Clement of Alexandria",title,rels,"Complete surviving multi-book work, presented with original book headings retained.",500)
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

def build_auto(b):
    # Quotas keep the batch broad: existing authors' missing writings plus complete works from smaller authors.
    priority=[
        ("Leo the Great",28),("John Chrysostom",28),("Origen of Alexandria",35),
        ("Augustine of Hippo",18),("Jerome",16),("Athanasius of Alexandria",12),
        ("Basil of Caesarea",12),("Cyril of Alexandria",10),("Tertullian",10),
        ("Cyprian of Carthage",10),("Hilary of Poitiers",6),("Eusebius of Caesarea",8),
        ("Sulpicius Severus",4),("Clement of Alexandria",4),("Tatian the Assyrian",2),
        ("Victor Vitensis",2),("Gennadius of Marseilles",2),("Aphrahat the Persian Sage",3),
        ("Asterius of Amasea",3),("Evagrius Scholasticus",3),("Philoxenus of Mabbug",4),
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
    keys=[(author_key(x["author"]),norm(x["title"])) for x in items]
    if len(keys)!=len(set(keys)): raise RuntimeError("Duplicate author/title identity remained.")
    with (out/"manifest.csv").open("w",encoding="utf-8-sig",newline="") as f:
        fields=["title","author","date","translator","editor","source_edition","source_url","source_files","words","characters","rights_status","description","rights_notes"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for x in items:
            w.writerow({"title":x["title"],"author":x["author"],"date":x["date"],"translator":x["translator"],"editor":x["editor"],
                        "source_edition":x["edition"],"source_url":x["url"],"source_files":"; ".join(x["rels"]),
                        "words":len(re.findall(r"\S+",x["body"])),"characters":len(x["body"]),"rights_status":"public_domain",
                        "description":x["description"],"rights_notes":x["rights_notes"]})
    zp=out/"scriptorium-reading-room-batch-200-2026-11-22.zip"
    with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for x in items:
            fields=[("Title",x["title"]),("Author",x["author"]),("Date",x["date"]),("Translator",x["translator"]),
                    ("Editor",x["editor"]),("Source Edition",x["edition"]),("Source URL",x["url"]),
                    ("Description",x["description"]),("Rights Status","public_domain"),("Rights Notes",x["rights_notes"])]
            header="\n".join(f"{k}: {v}" for k,v in fields if v)+"\n\n"
            name=unicodedata.normalize("NFKD",f'{x["author"]} - {x["title"]}')
            name="".join(c for c in name if not unicodedata.combining(c))
            name=re.sub(r"[^A-Za-z0-9()' -]+","-",name)
            name=re.sub(r"\s+"," ",name).strip(" .-")[:145].rstrip(" .-")+".txt"
            z.writestr(name,(header+x["body"]+"\n").encode("utf-8"))
    counts=defaultdict(int)
    for x in items: counts[x["author"]]+=1
    readme=["# Scriptorium Reading Room — Batch of 200","",f"- Standalone texts: **{len(items)}**",
            "- The ZIP contains 200 separate TXT files; manifest and README are outside the ZIP.",
            "- Duplicate screening: exact November 22, 2026 inventory, author aliases, title similarity and within-batch checks.",
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
    print(f"TOTAL_WORDS={sum(len(re.findall(r'\\S+',x['body'])) for x in items)}")
    print(f"ZIP_BYTES={zp.stat().st_size}")
    print("AUTHOR_COUNTS="+"; ".join(f"{a}:{n}" for a,n in sorted(counts.items())))
    print("SKIPPED_COUNTS="+"; ".join(f"{k}:{v}" for k,v in sorted(b.skipped.items())))

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source-dir",required=True,type=Path)
    p.add_argument("--inventory-csv",required=True,type=Path)
    p.add_argument("--output-dir",required=True,type=Path)
    a=p.parse_args()
    b=Builder(a.source_dir,a.inventory_csv)
    add_pelagius(b)
    add_manual(b)
    build_auto(b)
    print(f"ELIGIBLE_BEFORE_TRIM={len(b.items)}")
    write_outputs(b,a.output_dir)

if __name__=="__main__":
    main()
