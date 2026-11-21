#!/usr/bin/env python3
"""Prepare a rights-screened Scriptorium Reading Room ZIP from public-domain patristic sources."""
from __future__ import annotations
import argparse, csv, html, re, subprocess, unicodedata, zipfile
from pathlib import Path
from urllib.parse import quote
from bs4 import BeautifulSoup, NavigableString, Tag

SOURCE_REPO = "HistoricalChristianFaith/Writings-Database"
SOURCE_BRANCH = "master"
TODAY = "2026-11-21"

def raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_BRANCH}/" + quote(path, safe="/")

def clean(value: str) -> str:
    value = html.unescape(value or "").replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[\t ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()

def title_text(tag: Tag) -> str:
    clone = BeautifulSoup(str(tag), "html.parser")
    for x in clone.find_all("sup"): x.decompose()
    for x in clone.find_all("a"):
        if (x.get("href") or "").startswith(("#fn", "#r")): x.decompose()
    return clean(clone.get_text(" ", strip=True))

def html_to_text(raw: str, skip_title: str | None = None) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.find("article") or soup.body or soup
    for node in list(root.find_all(True)):
        if getattr(node, "attrs", None) is None:
            continue
        ident = str(node.get("id") or "").lower()
        if ident.startswith(("fn", "footnote")) or node.name in ("script","style","nav","header","footer","img","svg"):
            node.decompose()
        elif node.name == "sup" and node.find("a", href=re.compile(r"^#(?:fn|r)\d*", re.I)):
            node.decompose()
    for node in list(root.find_all("a")):
        if (node.get("href") or "").startswith(("#fn", "#r")): node.decompose()

    blocks, buffer = [], []
    first_heading = True
    def flush():
        nonlocal buffer
        value = clean(" ".join(x for x in buffer if x))
        buffer = []
        if value and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", value, re.I):
            blocks.append(value)
    def walk(node):
        nonlocal first_heading
        if isinstance(node, NavigableString):
            value = str(node).strip()
            if value: buffer.append(value)
            return
        if not isinstance(node, Tag): return
        name = (node.name or "").lower()
        if name in ("br","hr"):
            flush()
        elif name in ("h1","h2","h3","h4","h5","h6"):
            flush()
            value = title_text(node)
            if not value: return
            if skip_title and first_heading and value.casefold() == skip_title.casefold():
                first_heading = False
                return
            first_heading = False
            level = min(4, max(2, int(name[1])))
            blocks.append("#" * level + " " + value)
        elif name in ("p","li","blockquote","pre"):
            flush()
            value = clean(node.get_text(" ", strip=True))
            if value and not re.match(r"^(?:translated by|transcribed by|turned into english by|commissioned and uploaded by)\b", value, re.I):
                blocks.append(value)
        elif name in ("div","section","article","ul","ol","table","tr"):
            flush()
            for child in node.children: walk(child)
            flush()
        else:
            for child in node.children: walk(child)
    walk(root)
    flush()
    return clean("\n\n".join(blocks))

def read_html(root: Path, rel: str, skip_title: str | None = None) -> str:
    path = root / rel
    if not path.is_file(): raise FileNotFoundError(f"Missing source: {rel}")
    return html_to_text(path.read_text(encoding="utf-8", errors="replace"), skip_title)

def safe_filename(author: str, title: str) -> str:
    value = unicodedata.normalize("NFKD", f"{author} - {title}")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^A-Za-z0-9()' -]+", "-", value)
    return re.sub(r"\s+", " ", value).strip(" .-")[:145].rstrip(" .-") + ".txt"

def header(work: dict) -> str:
    fields = [
        ("Title", work["title"]), ("Author", work["author"]), ("Date", work.get("date","")),
        ("Translator", work.get("translator","")), ("Editor", work.get("editor","")),
        ("Source edition", work.get("edition","")), ("Source URL", work["url"]),
        ("Publisher", work.get("publisher","")), ("Publication date", work.get("publication_date","")),
        ("Container title", work.get("container_title","")), ("Volume", work.get("volume","")),
        ("Pages", work.get("pages","")), ("Access date", TODAY),
        ("Description", work.get("description","")), ("Attribution note", work.get("attribution_note","")),
        ("Rights status", "public_domain"), ("Rights notes", work["rights_notes"]),
    ]
    return "\n".join(f"{key}: {value}" for key,value in fields if value) + "\n\n"

def add(items, root, author, title, body, rel, min_chars=250, min_words=40, **meta):
    body = clean(body)
    words = len(re.findall(r"\S+", body))
    if len(body) < min_chars or words < min_words:
        raise ValueError(f"Text too short/incomplete: {author} — {title} ({words} words)")
    items.append({"author":author,"title":title,"body":body,"source_path":rel,"url":raw_url(rel),**meta})

def add_severus(root, items):
    rel = "Severus of Antioch/Letters/Letters 1-61.html"
    soup = BeautifulSoup((root/rel).read_text(encoding="utf-8",errors="replace"),"html.parser")
    article = soup.find("article") or soup
    headings = article.find_all("h2")
    if len(headings) < 63: raise ValueError(f"Expected 61 Severus items; found {max(0,len(headings)-2)}")
    for i, h in enumerate(headings[2:]):
        label = title_text(h)
        siblings = [str(h)]
        for sibling in h.next_siblings:
            if isinstance(sibling,Tag) and sibling.name == "h2": break
            siblings.append(str(sibling))
        body = html_to_text("".join(siblings))
        roman = re.match(r"^\s*([IVXLCDM]+)\s*[\.\-—]", label, re.I)
        num = roman.group(1).upper() if roman else str(i+1)
        detail = re.sub(r"^\s*[IVXLCDM]+\s*[\.\-—]+\s*","",label,flags=re.I).strip(" .—-")
        date = "Sixth-century correspondence; individual date sometimes uncertain"
        m = re.search(r"\[([^\]]{3,40})\]",label)
        if m and re.search(r"\d{3,4}",m.group(1)): date=m.group(1).strip(" .")
        add(items,root,"Severus of Antioch",f"Letter / Extract {num} — {detail[:115]}",f"## {label}\n\n{body}",rel,
            date=date,translator="E. W. Brooks",
            edition="A Collection of Letters of Severus of Antioch from Numerous Syriac Manuscripts, Patrologia Orientalis XII, fasc. 2 (1915); transcribed by Roger Pearse (2003)",
            container_title="Patrologia Orientalis",volume="XII, fasc. 2",
            description="One separately numbered letter or extract from the 1915 Syriac-source collection. Where the source heading says 'from the letter', this is an excerpt, not a claim to a complete original letter.",
            attribution_note="The historic source presents these as letters or extracts of Severus; preserve each item's original scope and date.",
            rights_notes="The source transcription explicitly says 'Public domain — copy freely'. English translation by E. W. Brooks (1915); source and translator are credited.",
            min_chars=50, min_words=8)
    if len([x for x in items if x["author"]=="Severus of Antioch"]) != 61:
        raise ValueError("Severus split failed its 61-item check")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source-dir",required=True,type=Path)
    p.add_argument("--output-dir",required=True,type=Path)
    args=p.parse_args()
    root,out=args.source_dir,args.output_dir
    out.mkdir(parents=True,exist_ok=True)
    items=[]

    # 61 separately numbered letters and extracts of Severus of Antioch.
    add_severus(root,items)

    # Salvian's eight books are one complete treatise, not eight unrelated works.
    salvian_paths=[f"Salvian the Presbyter/On the Government of God/Book {n}.html" for n in range(1,9)]
    salvian=[]
    romans=["I","II","III","IV","V","VI","VII","VIII"]
    for n,rel in enumerate(salvian_paths):
        salvian.append(f"## Book {romans[n]}\n\n"+read_html(root,rel,skip_title=f"Book {n+1}"))
    add(items,root,"Salvian of Marseille","On the Government of God","\n\n".join(salvian),salvian_paths[0],
        date="c. AD 440s",translator="Eva M. Sanford",
        edition="On the Government of God (New York: Columbia University Press, 1930), eight books; transcribed by Roger Pearse (2005)",
        publisher="Columbia University Press",publication_date="1930",volume="Eight books",
        description="Complete surviving treatise in eight books, kept together as one work with clear book headings.",
        rights_notes="The source transcription explicitly says 'Public domain — copy freely'. The 1930 U.S. edition has entered the U.S. public domain; translator and edition are credited.")

    # Vincent: retain the treatise, not the nineteenth-century editor's biographical introduction.
    rel="Vincent of Lérins/THE COMMONITORY/A Commonitory.html"
    soup=BeautifulSoup((root/rel).read_text(encoding="utf-8",errors="replace"),"html.parser")
    article=soup.find("article") or soup
    start=next((h for h in article.find_all(["h1","h2","h3","h4"]) if title_text(h).startswith("For the Antiquity and Universality")),None)
    if not start: raise ValueError("Could not locate the start of Vincent's main treatise")
    body=html_to_text("".join([str(start),*[str(x) for x in start.next_siblings]]))
    add(items,root,"Vincent of Lérins","A Commonitory",body,rel,
        date="c. AD 434",translator="C. A. Heurtley",editor="Philip Schaff and Henry Wace",
        edition="Nicene and Post-Nicene Fathers, Second Series, Vol. XI (1894)",container_title="Nicene and Post-Nicene Fathers, Second Series",volume="XI",publication_date="1894",
        description="The Commonitory, beginning with its main treatise; the nineteenth-century editor's biographical preface is omitted.",
        attribution_note="The historic edition explains that the author writes under the name Peregrinus and that attribution to Vincent of Lérins rests on Gennadius and later tradition.",
        rights_notes="Historic 1894 English edition; public domain. Translator and series editors are credited.")

    # Eugippius: omit the edition's table of contents.
    rel="Eugippius/Life of St. Severinus/English translation.html"
    soup=BeautifulSoup((root/rel).read_text(encoding="utf-8",errors="replace"),"html.parser")
    article=soup.find("article") or soup
    toc=next((h for h in article.find_all(["h1","h2","h3","h4"]) if title_text(h).casefold()=="table of chapters"),None)
    if toc:
        next_main=next((h for h in toc.find_all_next(["h1","h2","h3","h4"]) if title_text(h).upper().startswith("THE LIFE OF SAINT SEVERINUS")),None)
        if next_main:
            node=toc
            while node and node is not next_main:
                nxt=node.next_sibling
                node.decompose()
                node=nxt
    body=html_to_text(str(article),skip_title="English translation")
    add(items,root,"Eugippius","The Life of Saint Severinus",body,rel,
        date="c. AD 511",translator="George W. Robinson",
        edition="The Life of Saint Severinus, translated by George W. Robinson (Cambridge: Harvard University Press, 1914)",
        publisher="Harvard University Press",publication_date="1914",
        description="Eugippius's Life of Severinus, including the author's dedicatory letter to Paschasius and the surviving narrative chapters; the edition's table of contents is removed.",
        rights_notes="The 1914 Harvard University Press English edition is in the U.S. public domain; title page and translator credit are preserved in metadata.")

    # Eusebius of Emesa.
    rel="Eusebius of Emesa/Homily on the sufferings and death of our Lord.html"
    add(items,root,"Eusebius of Emesa","Homily on the Sufferings and Death of Our Lord",read_html(root,rel,skip_title="Homily on the sufferings and death of our Lord"),rel,
        date="Fourth century",translator="S. C. Malan",
        edition="Meditations for Every Wednesday and Friday in Lent on a Prayer of S. Ephraim (London, 1859), pp. 215–231",
        publisher="London",publication_date="1859",pages="215–231",
        description="A homily attributed to Eusebius of Emesa, preserved in the nineteenth-century English edition.",
        attribution_note="Traditional attribution to Eusebius of Emesa; consult the source edition for its transmission history.",
        rights_notes="Historic 1859 English edition; public domain.")

    # Severian of Gabala: an HTML sermon from the old Bareille translation.
    rel="Severian of Gabala/Homilies/On Genesis/Sermon 1 - on the First Day.html"
    add(items,root,"Severian of Gabala","Sermon 1 — On the First Day of Creation",read_html(root,rel,skip_title="Sermon 1 - on the First Day"),rel,
        date="Late fourth century",
        translator="Roger Pearse (English rendering from J. Bareille's French translation)",
        edition="J. Bareille, OEuvres complètes de S. Jean Chrysostome, Paris (1865); English rendering by Roger Pearse (2010)",
        publication_date="2010",
        description="First homily on the six days of creation, translated into English by Roger Pearse from J. Bareille's nineteenth-century French version.",
        rights_notes="The source transcription explicitly says Roger Pearse's 2010 English rendering is public domain and may be copied freely.")

    # Four more Severian homilies: Bryson Sewell translations released to the public domain.
    docs=[
        ("Severian of Gabala/Homilies/On Faith and the Natural Law/Translation-Sewell-Pearse.doc","On Faith and the Natural Law","2014"),
        ("Severian of Gabala/Homilies/On Peace/Translation-Sewell-Pearse.doc","On Peace","2014"),
        ("Severian of Gabala/Homilies/On Repentance and Compunction/Translation-Sewell-Pearse.doc","On Repentance and Compunction","2014"),
        ("Severian of Gabala/Homilies/On the Holy Spirit/Translation-Sewell-Pearse.doc","On the Holy Spirit","2014"),
    ]
    for rel,title,year in docs:
        path=root/rel
        if not path.is_file(): raise FileNotFoundError(f"Missing Severian Word text: {rel}")
        result=subprocess.run(["antiword","-w","180",str(path)],check=True,capture_output=True,text=True,errors="replace")
        body=clean(result.stdout)
        if len(body)<1200: raise ValueError(f"antiword extraction incomplete for {rel}")
        add(items,root,"Severian of Gabala",title,body,rel,
            date="Late fourth century",translator="Bryson Sewell",
            edition=f"English translation commissioned by Roger Pearse and released into the public domain ({year})",
            publication_date=year,
            description=f"English translation of Severian of Gabala's {title.lower()}.",
            rights_notes="The translator and commissioning editor explicitly released this English translation into the public domain. Source Word file is cited; text was extracted for Reading Room import.")

    # Leontius of Byzantium.
    rel="Leontius of Byzantium/Against the frauds of the Apollinarists/English translation.html"
    add(items,root,"Leontius of Byzantium","Against the Forgeries of the Apollinarists",read_html(root,rel,skip_title="English translation"),rel,
        date="Sixth century",translator="Bryson Sewell",
        edition="English translation commissioned by Roger Pearse (2013)",publication_date="2013",
        description="Leontius's polemic against writings falsely attributed to orthodox authors in the Apollinarian controversy.",
        rights_notes="The source transcription explicitly states that the commissioned English translation is public domain and may be copied freely.")

    # Dionysius Exiguus.
    rel="Dionysius Exiguus/On Easter, or, The Paschal Cycle.html"
    add(items,root,"Dionysius Exiguus","On Easter, or, The Paschal Cycle",read_html(root,rel,skip_title="On Easter, or, The Paschal Cycle"),rel,
        date="c. AD 525",translator="Michael Deckers",
        edition="English translation and explanatory comments by Michael Deckers (2006)",publication_date="2006",
        description="Dionysius Exiguus's treatise on the Paschal cycle, with the translator's explanatory notes retained where part of the source.",
        rights_notes="The translator explicitly placed the 2006 English translation in the public domain.")

    # Gildas: surviving fragments and associated texts from historic nineteenth-century editions.
    gildas_items = [
        ("Gildas the Wise/Fragments of Letters/English translation.html",
         "Fragments from Lost Letters of Gildas",
         "Surviving fragments assigned to Gildas, transmitted through later canonical collections; fragmentary and not a complete epistolary work.",
         "Fragments of Letters are printed from Haddan and Stubbs, Councils and Ecclesiastical Documents Relating to Great Britain and Ireland, Vol. I (1869), and Wasserschleben, Irische Kanonensammlung, 2nd ed. (1885).",
         "The source presents these as extracts transmitted under Gildas's name. Treat them as attributed fragments, not complete or independently authenticated letters."),
        ("Gildas the Wise/Lorica/English translation.html",
         "The Lorica of Gildas",
         "A short protective hymn traditionally associated with Gildas.",
         "Haddan and Stubbs, Councils and Ecclesiastical Documents Relating to Great Britain and Ireland, Vol. I (1869), Appendix C.",
         "The source's own introduction notes that a manuscript colophon attributes the hymn to Lathacan and that the connection with Gildas rests on a separate Irish tradition. Authorship is uncertain."),
        ("Gildas the Wise/Penitential/English translation.html",
         "The Penitential of Gildas",
         "A penitential text transmitted under Gildas's name; retain its traditional attribution as disputed.",
         "Wasserschleben, Bussordnungen (1851), reprinted in Haddan and Stubbs, Councils and Ecclesiastical Documents Relating to Great Britain and Ireland, Vol. I (1869), p. 113.",
         "The title reflects traditional transmission, not secure individual authorship. Preserve the source's note that penitential rules circulated in British and Irish ecclesiastical contexts."),
    ]
    for rel, title, description, edition, attribution in gildas_items:
        add(items,root,"Gildas the Wise",title,read_html(root,rel,skip_title="English translation"),rel,
            date="Sixth century; exact dates disputed",
            translator="Not separately credited in the source edition",
            editor="A. W. Haddan and William Stubbs",
            edition=edition,
            publication_date="1869",
            description=description,
            attribution_note=attribution,
            rights_notes="The English text derives from nineteenth-century editions (1869; with 1851/1885 source material as specified), now public domain. The source transcription does not separately identify an individual translator.")

    # John of Ephesus: import only the two surviving book texts available in this package.
    for rel,title in [
        ("John of Ephesus/Ecclesiastical History, Third Part/Book 2.html","Ecclesiastical History, Third Part — Book II"),
        ("John of Ephesus/Ecclesiastical History, Third Part/Book 5.html","Ecclesiastical History, Third Part — Book V"),
    ]:
        add(items,root,"John of Ephesus",title,read_html(root,rel),rel,
            date="Sixth century",translator="R. Payne Smith",
            edition="The Third Part of the Ecclesiastical History of John, Bishop of Ephesus, translated from Syriac (Oxford: Oxford University Press, 1860)",
            publisher="Oxford University Press",publication_date="1860",
            description="One surviving book of Part III in the 1860 English translation. This entry covers only the book named in its title, not the complete Third Part.",
            rights_notes="The 1860 English translation is public domain. The source repository contains only selected books from Part III; scope is stated explicitly.")

    # Dioscorus of Alexandria.
    rel="Dioscorus of Alexandria/Letter to Shenouda concerning an Origenist monk.html"
    add(items,root,"Dioscorus of Alexandria","Letter to Shenouda Concerning an Origenist Monk",read_html(root,rel,skip_title="Letter to Shenouda concerning an Origenist monk"),rel,
        date="Fifth century",translator="Herbert Thompson",
        edition="Herbert Thompson, 'Dioscorus and Shenoute', Recueil d'études égyptologiques dédiées à la mémoire de Jean-François Champollion (BEHE 234; Paris: Librairie Ancienne Honoré Champion, 1922), pp. 367–376; transcribed and slightly modernised by Roger Pearse (2011)",
        publisher="Librairie Ancienne Honoré Champion",publication_date="1922",pages="367–376",
        description="Correspondence concerning an Origenist monk, preserving material associated with Dioscorus of Alexandria and Shenoute.",
        attribution_note="The source introduction describes letters by Dioscorus and replies by Shenoute; do not imply every sentence is authored by Dioscorus alone.",
        rights_notes="The 1922 source edition and English transcription are identified by the source as public domain.")

    # Philip of Side's surviving fragments.
    rel="Philip of Side/Fragments/English translation.html"
    add(items,root,"Philip of Side","Fragments of the Christian History",read_html(root,rel,skip_title="English translation"),rel,
        date="Fifth century",translator="Andrew Eastbourne",
        edition="English translation commissioned and released into the public domain by Roger Pearse (2010–2011)",publication_date="2011",
        description="Collection of surviving fragments assigned to Philip of Side, with fragment numbers retained.",
        attribution_note="The source explicitly labels several fragment groups as questionable or only tentatively attributed to Philip. These are fragments, not a complete work.",
        rights_notes="Roger Pearse states that the commissioned English translation was made public domain on 19 March 2011.")

    # A separate anonymous dialogue, not a work authored by Philip of Side.
    rel="Philip of Side/Fragments/Appendix- The Religious Discussion at the Court of the Sassanids.html"
    add(items,root,"Anonymous (Religious Discussion at the Court of the Sassanids)","Religious Discussion at the Court of the Sassanids",read_html(root,rel,skip_title="Appendix- The Religious Discussion at the Court of the Sassanids"),rel,
        date="Late antique; precise date debated",translator="Andrew Eastbourne",
        edition="Text based on Bratke's 1899 edition and Bringel's 2007 edition; English translation commissioned and released into the public domain by Roger Pearse (2010–2011)",
        publication_date="2011",
        description="An anonymous religious dialogue preserved with scholarly apparatus. Included as a separate anonymous work, not under Philip of Side.",
        attribution_note="The text includes passages associated with fragments of Philip of Side, but the dialogue itself is not authored by Philip.",
        rights_notes="Roger Pearse states that the commissioned English translation was made public domain in 2011.")

    # Identity safeguard.
    seen=set()
    for item in items:
        key=(re.sub(r"\W+","",item["author"]).casefold(),re.sub(r"\W+","",item["title"]).casefold())
        if key in seen: raise ValueError(f"Duplicate item identity: {item['author']} — {item['title']}")
        seen.add(key)

    with (out/"manifest.csv").open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["title","author","date","translator","source_edition","source_url","source_path","characters","words","rights_status","notes"])
        writer.writeheader()
        for item in items:
            writer.writerow({
                "title":item["title"],"author":item["author"],"date":item.get("date",""),
                "translator":item.get("translator",""),"source_edition":item.get("edition",""),
                "source_url":item["url"],"source_path":item["source_path"],
                "characters":len(item["body"]),"words":len(re.findall(r"\S+",item["body"])),
                "rights_status":"public_domain","notes":item.get("attribution_note","") or item.get("description","")
            })
    zip_path=out/"scriptorium-early-christian-batch-01.zip"
    with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for item in items:
            z.writestr(safe_filename(item["author"],item["title"]),(header(item)+item["body"]+"\n").encode("utf-8"))
    (out/"README.md").write_text(
        "# Scriptorium Early Christian Acquisition — Batch 01\n\n"
        f"- **Works prepared:** {len(items)}\n"
        "- **Severus of Antioch:** 61 separately numbered letters or extracts\n"
        "- Also includes Salvian's eight-book treatise, Vincent of Lérins, Eugippius, Eusebius of Emesa, five items by Severian of Gabala, Leontius of Byzantium, Dionysius Exiguus, Hegemonius (traditional Archelaus attribution), two selected books of John of Ephesus, Dioscorus of Alexandria, Philip of Side fragments, and the anonymous Sassanid religious dialogue.\n\n"
        "## Import into Scriptorium\n\n"
        "1. Open the admin Reading Room import tool.\n"
        "2. Select the file named scriptorium-early-christian-batch-01.zip and choose Validate files.\n"
        "3. Review the previews, then choose Save drafts. The importer streams each writing separately and does not overwrite existing published works.\n"
        "4. Check saved drafts in the Standalone Text Drafts manager. Publish only after confirming the text, metadata, and rights notes.\n\n"
        "The ZIP contains only individual TXT work files. The CSV manifest and this README are outside the ZIP so they cannot be mistaken for Reading Room texts.\n"
        "Disputed attributions and fragmentary records are explicitly qualified in each text header.\n",
        encoding="utf-8")
    total_words=sum(len(re.findall(r"\S+",x["body"])) for x in items)
    print(f"PREPARED_WORKS={len(items)}")
    print("SEVERUS_ITEMS=61")
    print(f"TOTAL_WORDS={total_words}")
    print(f"ZIP_BYTES={zip_path.stat().st_size}")

if __name__=="__main__":
    main()
