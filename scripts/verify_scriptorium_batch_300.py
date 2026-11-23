#!/usr/bin/env python3
"""Independent second-pass duplicate audit for a 300-work Scriptorium batch."""
from __future__ import annotations
import argparse, csv, re, unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlparse

ALIASES = {
    "tertullian": "tertullian of carthage",
    "cyprian": "cyprian of carthage",
    "gregory the dialogist": "gregory the great",
    "gregory the great": "gregory the great",
    "hermias sozomen": "sozomen",
    "sozomen": "sozomen",
    "gildas the wise": "gildas",
    "gildas": "gildas",
    "aristides the philosopher": "aristides of athens",
    "palladius of galatia": "palladius of helenopolis",
    "palladius of helenopolis": "palladius of helenopolis",
    "john damascene": "john of damascus",
    "pope leo i": "leo the great",
    "leo": "leo the great",
    "athanasius": "athanasius of alexandria",
    "ambrose": "ambrose of milan",
    "origen": "origen of alexandria",
    "theodoret": "theodoret of cyrus",
    "lactantius": "lactantius",
    "lucius caecilius firmianus lactantius": "lactantius",
    "salvian of marseilles": "salvian the presbyter",
    "salvian of marseille": "salvian the presbyter",
    "irenaeus": "irenaeus of lyons",
    "shepherd of hermas": "hermas",
    "hermas": "hermas",
    "ephraim": "ephrem the syrian",
    "ephrem": "ephrem the syrian",
    "novatian": "novatian of rome",
    "caius presbyter of rome": "caius of rome",
    "vincent of lerins": "vincent of lerins",
    "archelaus": "archelaus of carrhae",
    "hegemonius": "archelaus of carrhae",
    "macarius of egypt": "pseudo-macarius",
    "archelaus of carrhae": "archelaus of carrhae",
    "asterius of amasea": "asterius of amasea bishop of amasia",
    "macarius of egypt": "pseudo-macarius",
    "pseudo-macarius": "pseudo-macarius",
    "augustine": "augustine of hippo",
    "st augustine": "augustine of hippo",
    "chrysostom": "john chrysostom",
    "st john chrysostom": "john chrysostom",
    "st ambrose": "ambrose of milan",
    "aphrahat": "aphrahat the persian sage",
    "gregory nazianzen": "gregory of nazianzus",
    "gregory of nazianzen": "gregory of nazianzus",
    "tatian": "tatian the assyrian",
    "hippolytus": "hippolytus of rome",
    "methodius": "methodius of olympus",
    "sulpitius severus": "sulpicius severus",
    "gennadius of marseilles": "gennadius of massilia",
    "victorinus": "victorinus of pettau",
    "gregory thaumaturgus": "gregory of neocaesarea",
    "st gregory thaumaturgus": "gregory of neocaesarea",
    "eusebius": "eusebius of caesarea",
    "basil": "basil of caesarea",
    "clemens alexandrinus": "clement of alexandria",
    "mar jacob": "jacob of serugh",
    "africanus": "julius africanus",
    "st justin martyr": "justin martyr",
    "st ignatius": "ignatius of antioch",
    "bardesanes": "bardaisan of edessa dialogue recorded by philip attribution qualified",
    "bardaisan": "bardaisan of edessa dialogue recorded by philip attribution qualified",
    "possidius": "possidius of calama",
    "pseudo-clement": "pseudo-clement anonymous",
    "aristides": "aristides of athens",
    "commodianus": "commodian",
    "venantius": "venantius honorius clementianus fortunatus venantius fortunatus",
    "venantius fortunatus": "venantius honorius clementianus fortunatus venantius fortunatus",
    "clement": "clement of rome",
    "venantius": "venantius honorius clementianus fortunatus venantius fortunatus",
    "caius": "caius of rome",
    "arnobius": "arnobius of sicca",
    "anonymous transmitted under clements name": "pseudo-clement anonymous",
}

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()

def author_key(s: str) -> str:
    n = norm(s)
    return norm(ALIASES.get(n, n))


def global_title_key(value: str) -> str:
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


def source_path_key(value: str) -> str:
    value = unquote(str(value or "")).replace("\\", "/").strip()
    if not value:
        return ""
    if "://" in value:
        path = urlparse(value).path
        for marker in ("/blob/master/", "/master/"):
            if marker in path:
                value = path.split(marker, 1)[1]
                break
        else:
            value = path
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold())

def numbered_series_signature(value: str):
    raw = (value or "").casefold()
    patterns = [
        (r"\bconference\s*([0-9]+|[ivxlcdm]+)\b", "conference"),
        (r"^\s*([0-9]+)\.\s*(?:the\s+)?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\w+)\s+conference\b", "conference"),
        (r"^\s*([0-9]+)\.\s*(?:the\s+)?conference\b", "conference"),
        (r"\b(treatise|epistle|letter|sermon|homily|demonstration|oration|dialogue)\s*([0-9]+|[ivxlcdm]+)\b", None),
    ]
    roman={"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
    def integer(v):
        if v.isdigit(): return int(v)
        total=0; prev=0
        for c in reversed(v):
            n=roman.get(c,0); total += -n if n < prev else n; prev=max(prev,n)
        return total
    for pattern,fixed in patterns:
        m=re.search(pattern,raw,re.I)
        if not m: continue
        if fixed: return fixed,integer(m.group(1))
        kind=m.group(1).casefold()
        if kind in ("letter","epistle"): kind="epistle"
        return kind,integer(m.group(2))
    return None

def special_overlap(candidate: str, existing: str, author: str | None = None) -> bool:
    ak=author_key(author or "")
    c=(candidate or "").casefold()
    e=(existing or "").casefold()
    if ak == author_key("Pseudo-Macarius") and "fifty spiritual homilies" in c and "fifty spiritual homilies" in e:
        return True
    if ak == author_key("Aphrahat the Persian Sage") and "demonstrations" in e and "demonstration" in c:
        return True
    if ak == author_key("Cyprian of Carthage"):
        if re.search(r"\btreatise\s*12\b",c) and re.search(r"\btreatise\s*12\b",e):
            return True
        if any(x in c for x in ("seventh council of carthage","baptism of heretics")) and any(x in e for x in ("seventh council of carthage","baptism of heretics")):
            return True
    if ak == author_key("Aristides of Athens") and "apology" in c and "apology" in e:
        return True
    if ak == author_key("Lactantius") and "workmanship of god" in c and "workmanship of god" in e:
        return True
    if ak == author_key("Eusebius of Caesarea") and "constantine" in c and "constantine" in e and "oration" in c and "oration" in e:
        return True
    if ak == author_key("Basil of Caesarea") and "on the spirit" in c and "holy spirit" in e:
        return True
    if ak == author_key("Ignatius of Antioch"):
        def simple(x):
            x=re.sub(r"^(?:epistle\s+to\s+|ignatius\s+to\s+|to\s+)", "", x.strip())
            x=re.sub(r"^st\.?\s+", "", x)
            x=re.sub(r"\s*(?:\(|—|-)?\s*(?:longer recension|syriac version)\)?$", "", x).strip()
            return norm(x)
        if simple(c)==simple(e): return True
    stripped=re.sub(r"^\s*sermon\s+\d+\s*[-:—–]*\s*", "", c, flags=re.I)
    if stripped and norm(stripped)==norm(e):
        return True
    if ak == author_key("Sulpicius Severus"):
        def conv(x):
            return re.sub(r"\bdialogue\s+(i|ii|iii)\b", lambda m:"dialogue "+str({"i":1,"ii":2,"iii":3}[m.group(1)]), x)
        if norm(conv(c))==norm(conv(e)): return True
    return False

def roman_int(v: str) -> int | None:
    if not v: return None
    if v.isdigit(): return int(v)
    vals = {"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
    total=0; prev=0
    for ch in reversed(v.casefold()):
        n=vals.get(ch,0)
        total += -n if n < prev else n
        prev=max(prev,n)
    return total

def division(s: str):
    s=(s or "").casefold()
    m=re.search(
        r"(?:[,;:]?\s+)(book|books|part|volume|vol\.?|tome|letter|letters|epistle|epistles|sermon|sermons|homily|homilies|discourse|discourses|chapter|chapters|dialogue|dialogues)\s*"
        r"(?:no\.?\s*)?([0-9]+|[ivxlcdm]+)(?:\s*[-–—]\s*([0-9]+|[ivxlcdm]+))?\s*$",
        s,re.I
    )
    if not m: return None
    kind=m.group(1).casefold().rstrip("s.")
    if kind in ("vol","volume","tome"): kind="volume"
    if kind in ("letter","epistle"): kind="epistle"
    if kind in ("sermon",): kind="sermon"
    if kind in ("homily",): kind="homily"
    a=roman_int(m.group(2)); b=roman_int(m.group(3)) or a
    return kind,a,b

def variants(s: str, author: str | None = None) -> set[str]:
    raw=(s or "").casefold().strip()
    values={raw}
    values.add(re.sub(r"\s*\([^)]*\)", "", raw))
    values.add(re.sub(r"\s*\[[^]]*\]", "", raw))
    if author and re.search(r"\s+[—–]\s+", raw):
        prefix, suffix = re.split(r"\s+[—–]\s+", raw, maxsplit=1)
        stop={"the","of","saint","st","pope"}
        author_tokens=set(norm(author).split())
        author_tokens.update(norm(ALIASES.get(norm(author), author)).split())
        suffix_tokens=set(norm(suffix).split())-stop
        if suffix_tokens and suffix_tokens.issubset(author_tokens):
            values.add(prefix)
    expanded=set(values)
    for x in list(values):
        x=re.sub(r"\bcommentary on (?:the )?gospel of\b","commentary on",x)
        x=re.sub(r"\bcommentary on the acts of the apostles\b","commentary on acts",x)
        x=re.sub(r"\bof the work of monks\b","on the work of monks",x)
        x=re.sub(r"\bon the catechising of the uninstructed\b","on catechising the uninstructed",x)
        x=re.sub(r"^a treatise on\b","on",x)
        x=re.sub(r"^the treatise on\b","on",x)
        x = re.sub(r"\bexpositions? on (?:the book of )?psalms?,?\s*psalm\s*(\d+)\b", r"exposition on psalm \1", x)
        x = re.sub(r"^the\s+", "", x)
        x = re.sub(r"\bdisputation with (?:the )?heresiarch manes\b", "disputation with manes", x)
        x = re.sub(r"^the\s+", "", x)
        x = re.sub(r"\bdisputation with (?:the )?heresiarch manes\b", "disputation with manes", x)
        if author and author_key(author) == author_key("Clement of Rome") and "letter to the corinthians" in x:
            expanded.add("first epistle of clement to the corinthians")
        expanded.add(x)
    def roman_to_arabic(match):
        value = match.group(2).casefold()
        vals = {"i":1,"v":5,"x":10,"l":50,"c":100,"d":500,"m":1000}
        total=0; prev=0
        for ch in reversed(value):
            n=vals[ch]
            total += -n if n < prev else n
            prev=max(prev,n)
        return f"{match.group(1)} {total}"
    final=set()
    for x in list(expanded):
        x=re.sub(r"\\b(book|part|volume|discourse|homily|homilies|oration|letter|epistle)\\s+([ivxlcdm]+)\\b",roman_to_arabic,x)
        final.add(x)
        pm=re.search(r"\\(\\s*(oration|homily|book|discourse|letter|epistle)\\s+([0-9]+|[ivxlcdm]+)\\s*\\)",x)
        if pm:
            n=roman_to_arabic(pm) if not pm.group(2).isdigit() else f"{pm.group(1)} {pm.group(2)}"
            final.add(n)
    keys={norm(x) for x in final if norm(x)}
    equiv={

        norm("Commonitorium"):norm("A Commonitory"),
        norm("A Commonitory"):norm("A Commonitory"),
        norm("Against Publishing the Errors"):norm("Against Publishing the Errors of the Brethren"),
        norm("Against Publishing the Errors of the Brethren"):norm("Against Publishing the Errors of the Brethren"),
        norm("Fifth Theological Oration (Oration 31)"):norm("Oration 31 — The Fifth Theological Oration"),
        norm("Oration 31 — The Fifth Theological Oration"):norm("Oration 31 — The Fifth Theological Oration"),
        norm("Homily on the Paralytic"):norm("On the Paralytic Let Down Through the Roof"),
        norm("On the Paralytic Let Down Through the Roof"):norm("On the Paralytic Let Down Through the Roof"),
        norm("Counter-Statements to Cyril's 12 Anathemas"):norm("Counter-Statements of Theodoret"),
        norm("Counter-Statements of Theodoret"):norm("Counter-Statements of Theodoret"),
        norm("Sermon 33 on the New Testament"):norm("Sermon 0XXXIIL"),
        norm("Sermon 0XXXIIL"):norm("Sermon 0XXXIIL"),
        norm("The Oration of the Emperor Constantine"):norm("Oration of Constantine"),
        norm("Oration of Constantine"):norm("Oration of Constantine"),
        norm("Councils of Ariminum and Seleucia"):norm("De Synodis"),
        norm("De Synodis"):norm("De Synodis"),
        norm("The Pearl-Seven Hymns on the Faith"):norm("The Pearl"),
        norm("The Pearl"):norm("The Pearl"),
        norm("1. First Conference of Abbot Moses"):norm("Conference 1"),
        norm("Conference 1"):norm("Conference 1"),
        norm("2. Second Conference of Abbot Moses"):norm("Conference 2"),
        norm("Conference 2"):norm("Conference 2"),
        norm("3. Conference of Abbot Paphnutius"):norm("Conference 3"),
        norm("Conference 3"):norm("Conference 3"),
        norm("4. Conference of Abbot Daniel"):norm("Conference 4"),
        norm("Conference 4"):norm("Conference 4"),
        norm("5. Conference of Abbot Serapion"):norm("Conference 5"),
        norm("Conference 5"):norm("Conference 5"),
        norm("6. Conference of Abbot Theodore"):norm("Conference 6"),
        norm("Conference 6"):norm("Conference 6"),
        norm("7. First Conference of Abbot Serenus"):norm("Conference 7"),
        norm("Conference 7"):norm("Conference 7"),
        norm("8. The Second Conference of Abbot Serenus"):norm("Conference 8"),
        norm("Conference 8"):norm("Conference 8"),
        norm("9. The First Conference of Abbot Isaac"):norm("Conference 9"),
        norm("Conference 9"):norm("Conference 9"),
        norm("Commonitorium"):norm("A Commonitory"),
        norm("A Commonitory"):norm("A Commonitory"),
        norm("Against Publishing the Errors"):norm("Against Publishing the Errors of the Brethren"),
        norm("Against Publishing the Errors of the Brethren"):norm("Against Publishing the Errors of the Brethren"),
        norm("Of Two Souls"):norm("Concerning Two Souls, Against the Manichaeans"),
        norm("Care of the Dead"):norm("On Care to Be Had for the Dead"),
        norm("A Treatise on the Care of the Dead"):norm("On Care to Be Had for the Dead"),
        norm("Handbook on Faith, Hope and Love"):norm("Enchiridion"),
        norm("The Paedagogus"):norm("The Instructor"),
        norm("Paedagogus"):norm("The Instructor"),
        norm("The Stromata"):norm("The Stromata, or Miscellanies"),
        norm("On the Sovereignty of God"):norm("On the Sole Government of God"),
        norm("The Sovereignty of God"):norm("On the Sole Government of God"),
        norm("First Apology"):norm("The First Apology of Justin"),
        norm("The First Apology"):norm("The First Apology of Justin"),
        norm("Apologia ad Constantium"):norm("Defence Before Constantius"),
        norm("Correspondence with Pope Innocent I"):norm("Correspondence with Innocent, Bishop of Rome"),
        norm("Fifth Theological Oration"):norm("Oration 31"),
        norm("The Fifth Theological Oration"):norm("Oration 31"),
        norm("First Theological Oration"):norm("Oration 27"),
        norm("The First Theological Oration"):norm("Oration 27"),
        norm("On Two Souls"):norm("Concerning Two Souls, Against the Manichaeans"),
        norm("Care for the Dead"):norm("On Care to Be Had for the Dead"),
        norm("Enchiridion"):norm("Handbook on Faith, Hope and Love"),
        norm("On the Instructor"):norm("The Instructor"),
        norm("Stromata"):norm("The Stromata, or Miscellanies"),
        norm("De Monarchia"):norm("On the Sole Government of God"),
        norm("Apologia de Fuga"):norm("Defence of His Flight"),
        norm("De Sententia Dionysii"):norm("On the Opinion of Dionysius"),
        norm("Ad Episcopus Aegypti et Libyae"):norm("To the Bishops of Egypt"),
        norm("Ad Afros Epistola Synodica"):norm("To the Bishops of Africa"),
        norm("De Spiritu Sancto"):norm("On the Holy Spirit"),
    }
    keys.update(equiv[k] for k in list(keys) if k in equiv)
    return keys

def base_key(s: str) -> str:
    x=(s or "").casefold()
    x=re.sub(r"\s*\([^)]*\)", "", x)
    x=re.sub(r"\s*\[[^]]*\]", "", x)
    x=re.sub(
        r"(?:[,;:]?\s+)(?:book|books|part|volume|vol\.?|tome|dialogue|dialogues)\s*(?:no\.?\s*)?(?:[0-9]+|[ivxlcdm]+)(?:\s*[-–—]\s*(?:[0-9]+|[ivxlcdm]+))?\s*$",
        "",x,flags=re.I
    )
    x=re.sub(
        r"(?:[,;:]?\s+)(?:sermon|sermons|homily|homilies|chapter|chapters|letter|letters|epistle|epistles|dialogue|dialogues)\s+[0-9]+(?:\s*[-–—]\s*[0-9]+)?\s*$",
        "",x,flags=re.I
    )
    x=re.sub(r"\bcommentary on (?:the )?gospel of\b","commentary on",x)
    x=re.sub(r"\bcommentary on the acts of the apostles\b","commentary on acts",x)
    return norm(x)

def is_generic_collection(title: str) -> bool:
    n=norm(title)
    return n in {"letters","epistles","correspondence","homilies","sermons","commentaries","works"}


def work_overlap(candidate: str, existing: str, author: str | None = None) -> bool:
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
    return False

def title_match(candidate: str, existing: str, author: str | None = None) -> bool:
    if special_overlap(candidate, existing, author): return True
    if work_overlap(candidate, existing, author):
        return True
    if variants(candidate, author) & variants(existing, author): return True
    cd,ed=division(candidate),division(existing)
    cb,eb=base_key(candidate),base_key(existing)
    if cb and cb==eb:
        # Different numbered volumes remain distinct unless one inventory title denotes a full range.
        if cd and ed:
            if cd[0]!=ed[0]: return False
            return not (cd[2] < ed[1] or ed[2] < cd[1])
        return True
    # Existing collection headings can already contain individually enumerated items.
    en=norm(existing)
    cn=norm(candidate)
    if en in {"letters","epistles","correspondence"} and re.search(r"\b(letter|epistle)\b",cn):
        return True
    if en in {"homilies","sermons"} and re.search(r"\b(homily|sermon)\b",cn):
        return True
    # A correspondence volume may already be represented by separately catalogued named letters.
    if "olympias" in cn and re.search(r"\b(letters|correspondence)\b",cn) and "olympias" in en and re.search(r"\bto olympias\b",en):
        return True
    # An existing grouped set of psalm homilies covers each component homily by the same author.
    if author and author_key(author)==author_key("Hilary of Poitiers") and "homilies on psalms" in en and "homily on psalm" in cn:
        old_nums=set(re.findall(r"\d+",existing)); new_nums=set(re.findall(r"\d+",candidate))
        if new_nums and new_nums.issubset(old_nums):
            return True
    # Treat numbered items within an explicitly overlapping range as present.
    if cd and ed and cd[0]==ed[0] and cd[1] is not None and ed[1] is not None:
        if cd[1] >= ed[1] and cd[1] <= ed[2] and cb==eb: return True
    # Do not fuzzy-match adjacent numbered works or sermons from different years.
    if re.findall(r"\d+", candidate) != re.findall(r"\d+", existing):
        return False
    # Conservative fuzzy fallback after punctuation / editor subtitles are removed.
    ca=norm(re.sub(r"\s*\([^)]*\)","",candidate))
    ea=norm(re.sub(r"\s*\([^)]*\)","",existing))
    return min(len(ca),len(ea))>=22 and SequenceMatcher(None,ca,ea).ratio()>=0.96

def read_rows(path: Path, delimiter: str=";"):
    with path.open(encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f,delimiter=delimiter))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--inventory-csv",required=True,type=Path)
    ap.add_argument("--manifest-csv",required=True,type=Path)
    ap.add_argument("--report",required=True,type=Path)
    ap.add_argument("--prior-batch-manifest",required=True,type=Path)
    args=ap.parse_args()
    inv=read_rows(args.inventory_csv,";")
    batch=read_rows(args.manifest_csv,",")
    by_author=defaultdict(list)
    global_titles=set()
    for row in inv:
        ak=author_key(row.get("attributed_author",""))
        title=row.get("title","").strip()
        if ak and title: by_author[ak].append(title)
        if title: global_titles.add(global_title_key(title))
    prior=read_rows(args.prior_batch_manifest,",")
    for row in prior:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        if ak and title: by_author[ak].append(title)
        if title: global_titles.add(global_title_key(title))
    inventory_paths=defaultdict(list)
    for row in inv:
        key=source_path_key(row.get("source_url",""))
        if key: inventory_paths[key].append(f'{row.get("attributed_author","")} — {row.get("title","")}')
    prior_paths=defaultdict(list)
    for row in prior:
        vals=[row.get("source_url",""), *(row.get("source_files","") or "").split(";")]
        for value in vals:
            key=source_path_key(value)
            if key: prior_paths[key].append(f'{row.get("author","")} — {row.get("title","")}')
    inventory_hits=[]
    for row in batch:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        path_values=[row.get("source_url",""), *(row.get("source_files","") or "").split(";")]
        path_matches=[]
        for value in path_values:
            key=source_path_key(value)
            if key:
                path_matches += inventory_paths.get(key,[]) + prior_paths.get(key,[])
        sig=numbered_series_signature(title)
        title_matches=[old for old in by_author.get(ak,[]) if (
            title_match(title,old,row.get("author",""))
            or (sig and sig==numbered_series_signature(old))
        )]
        gkey=global_title_key(title)
        global_match=(len(gkey)>=16 and gkey in global_titles)
        hits=sorted(set(path_matches + title_matches + (["cross-author exact title match"] if global_match else [])))
        if hits:
            inventory_hits.append((row.get("author",""),title,hits))
    batch_hits=[]
    seen=defaultdict(list)
    seen_paths={}
    seen_global=set()
    for row in batch:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        for old in seen[ak]:
            sig=numbered_series_signature(title)
            old_sig=numbered_series_signature(old)
            if title_match(title,old,row.get("author","")) or special_overlap(title,old,row.get("author","")) or (sig and sig==old_sig):
                batch_hits.append((row.get("author",""),title,old))
        gkey=global_title_key(title)
        if len(gkey)>=16 and gkey in seen_global:
            batch_hits.append((row.get("author",""),title,"same exact title under another author"))
        seen_global.add(gkey)
        for value in [row.get("source_url",""), *(row.get("source_files","") or "").split(";")]:
            key=source_path_key(value)
            if key and key in seen_paths:
                batch_hits.append((row.get("author",""),title,seen_paths[key]))
            elif key:
                seen_paths[key]=f'{row.get("author","")} — {title}'
        seen[ak].append(title)
    required=["title","author","source_edition","source_url","words","characters","rights_status","description","rights_notes"]
    missing_metadata=[]
    for i,row in enumerate(batch,1):
        absent=[k for k in required if not row.get(k)]
        if absent: missing_metadata.append((i,row.get("title",""),absent))
    report=[]
    report += [
        "# Independent Scriptorium Batch Audit",
        "",
        f"- Inventory entries checked: {len(inv)}",
        f"- Batch manifest entries checked: {len(batch)}",
        f"- Previous 200-draft manifest entries checked: {len(prior)}",
        f"- Inventory/prior-batch title OR source-path matches: {len(inventory_hits)}",
        f"- Within-batch title OR source-path duplicates: {len(batch_hits)}",
        f"- Missing required metadata fields: {len(missing_metadata)}",
        "",
    ]
    if inventory_hits:
        report += ["## Inventory overlaps",""]
        for author,title,hits in inventory_hits:
            report.append(f"- **{author} — {title}** overlaps: {', '.join(hits)}")
    if batch_hits:
        report += ["","## Within-batch duplicates",""]
        for author,title,old in batch_hits:
            report.append(f"- **{author} — {title}** overlaps **{old}**")
    if missing_metadata:
        report += ["","## Missing metadata",""]
        for i,title,fields in missing_metadata:
            report.append(f"- Row {i}: {title}: {', '.join(fields)}")
    if not inventory_hits and not batch_hits and not missing_metadata:
        report.append("PASS — no duplicate title/author identities, source-path identities, or missing required metadata were detected.")
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text("\n".join(report)+"\n",encoding="utf-8")
    print(f"AUDIT_INVENTORY_ROWS={len(inv)}")
    print(f"AUDIT_BATCH_ROWS={len(batch)}")
    print(f"AUDIT_INVENTORY_DUPLICATES={len(inventory_hits)}")
    print(f"AUDIT_WITHIN_BATCH_DUPLICATES={len(batch_hits)}")
    print(f"AUDIT_MISSING_METADATA={len(missing_metadata)}")
    for author,title,hits in inventory_hits:
        print(f"INVENTORY_DUPLICATE={author} | {title} | MATCHES={'; '.join(hits)}")
    for author,title,old in batch_hits:
        print(f"BATCH_DUPLICATE={author} | {title} | MATCHES={old}")
    if len(batch)!=300 or len(prior)!=200 or inventory_hits or batch_hits or missing_metadata:
        raise SystemExit("Independent audit failed; inspect the report before import.")

if __name__=="__main__":
    main()
