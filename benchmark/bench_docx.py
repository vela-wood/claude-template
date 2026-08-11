"""Benchmark a converter over .docx files vs raw document.xml ground truth.

Usage: python bench_docx.py {markitdown|anydoc} <dir-or-files...>
TSV per file: path, truth_words, miss_rate|ERROR, seconds
Ground truth = all w:t text in word/document.xml (body incl. tables;
tracked insertions counted, deletions excluded since converters accept them).
"""
import sys, re, time, pathlib, zipfile

mode = sys.argv[1]
paths = []
for a in sys.argv[2:]:
    p = pathlib.Path(a)
    paths += sorted(x for x in p.rglob("*.docx") if not x.name.startswith("~$")) if p.is_dir() else [p]

if mode == "markitdown":
    from markitdown import MarkItDown
    mid = MarkItDown()
    convert = lambda p: mid.convert(str(p)).text_content
else:
    import anydoc
    convert = lambda p: anydoc.to_markdown(str(p))

def truth_text(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    # drop tracked deletions (w:del blocks) — converters emit accepted text
    xml = re.sub(r"<w:del [^>]*>.*?</w:del>", " ", xml, flags=re.S)
    return " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))

def words(t):
    return re.findall(r"[a-z]{3,}", t.lower())

for p in paths:
    try:
        uniq = set(words(truth_text(p)))
    except Exception as e:
        print(f"{p}\t?\tERROR-truth:{type(e).__name__}\t0")
        continue
    if len(uniq) < 30:
        print(f"{p}\t{len(uniq)}\tTINY\t0")
        continue
    t0 = time.perf_counter()
    try:
        out = convert(p)
    except Exception as e:
        print(f"{p}\t{len(uniq)}\tERROR:{type(e).__name__}\t{time.perf_counter()-t0:.2f}")
        continue
    dt = time.perf_counter() - t0
    have = set(words(out))
    squashed = re.sub(r"[^a-z]", "", out.lower())
    missing = [w for w in uniq if w not in have and w not in squashed]
    print(f"{p}\t{len(uniq)}\t{len(missing)/len(uniq):.4f}\t{dt:.2f}")
