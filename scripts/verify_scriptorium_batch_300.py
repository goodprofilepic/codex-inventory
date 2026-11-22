#!/usr/bin/env python3
"""Independent second-pass duplicate audit for a 300-work Scriptorium batch."""
from __future__ import annotations
import argparse, csv, re, unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

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
    "archelaus of carrhae": "archelaus of carrhae",
    "asterius of amasea": "asterius of amasea bishop of amasia",
    "macarius of egypt": "pseudo-macarius",
    "pseudo-macarius": "pseudo-macarius",
}

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()

def author_key(s: str) -> str:
    n = norm(s)
    return norm(ALIASES.get(n, n))

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
        r"(?:[,;:]?\s+)(book|books|part|volume|vol\.?|tome|letter|letters|epistle|epistles|sermon|sermons|homily|homilies|discourse|discourses|chapter|chapters)\s*"
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
        expanded.add(x)
    return {norm(x) for x in expanded if norm(x)}

def base_key(s: str) -> str:
    x=(s or "").casefold()
    x=re.sub(r"\s*\([^)]*\)", "", x)
    x=re.sub(r"\s*\[[^]]*\]", "", x)
    x=re.sub(
        r"(?:[,;:]?\s+)(?:book|books|part|volume|vol\.?|tome)\s*(?:no\.?\s*)?(?:[0-9]+|[ivxlcdm]+)(?:\s*[-–—]\s*(?:[0-9]+|[ivxlcdm]+))?\s*$",
        "",x,flags=re.I
    )
    x=re.sub(
        r"(?:[,;:]?\s+)(?:sermon|sermons|homily|homilies|chapter|chapters|letter|letters|epistle|epistles)\s+[0-9]+(?:\s*[-–—]\s*[0-9]+)?\s*$",
        "",x,flags=re.I
    )
    x=re.sub(r"\bcommentary on (?:the )?gospel of\b","commentary on",x)
    x=re.sub(r"\bcommentary on the acts of the apostles\b","commentary on acts",x)
    return norm(x)

def is_generic_collection(title: str) -> bool:
    n=norm(title)
    return n in {"letters","epistles","correspondence","homilies","sermons","commentaries","works"}

def title_match(candidate: str, existing: str, author: str | None = None) -> bool:
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
    for row in inv:
        ak=author_key(row.get("attributed_author",""))
        title=row.get("title","").strip()
        if ak and title: by_author[ak].append(title)
    prior=read_rows(args.prior_batch_manifest,",")
    for row in prior:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        if ak and title: by_author[ak].append(title)
    inventory_hits=[]
    for row in batch:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        hits=[old for old in by_author.get(ak,[]) if title_match(title,old,row.get("author",""))]
        if hits:
            inventory_hits.append((row.get("author",""),title,hits))
    batch_hits=[]
    seen=defaultdict(list)
    for row in batch:
        ak=author_key(row.get("author",""))
        title=row.get("title","").strip()
        for old in seen[ak]:
            if title_match(title,old,row.get("author","")):
                batch_hits.append((row.get("author",""),title,old))
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
        f"- Inventory or previous-batch author/title matches: {len(inventory_hits)}",
        f"- Within-batch author/title duplicates: {len(batch_hits)}",
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
        report.append("PASS — no duplicate title/author identities or missing required metadata were detected.")
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
