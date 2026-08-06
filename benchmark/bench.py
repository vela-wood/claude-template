"""Benchmark one converter over benchmark/ PDFs vs PyMuPDF ground truth.

Usage: python bench.py {markitdown|anydoc} <dir>
Prints one TSV line per file: relpath, pages, truth_words, miss_rate|SCAN|ERROR, seconds
Missing rate = unique ground-truth words (3+ letters) absent from converter output.
"""
import sys, re, time, pathlib
import fitz

mode, root = sys.argv[1], pathlib.Path(sys.argv[2])

if mode == "markitdown":
    from markitdown import MarkItDown
    mid = MarkItDown()
    convert = lambda p: mid.convert(str(p)).text_content
else:
    import anydoc
    convert = lambda p: anydoc.to_markdown(str(p))

def words(t):
    return re.findall(r"[a-z]{3,}", t.lower())

for p in sorted(root.rglob("*.pdf")) + sorted(root.rglob("*.PDF")):
    rel = p.relative_to(root)
    try:
        d = fitz.open(p)
        truth = " ".join(pg.get_text() for pg in d)
        npages = len(d)
        d.close()
    except Exception:
        print(f"{rel}\t?\t?\tERROR-truth\t0")
        continue
    uniq = set(words(truth))
    if len(uniq) < 30:
        print(f"{rel}\t{npages}\t{len(uniq)}\tSCAN\t0")
        continue
    t0 = time.perf_counter()
    try:
        out = convert(p)
    except Exception as e:
        print(f"{rel}\t{npages}\t{len(uniq)}\tERROR:{type(e).__name__}\t{time.perf_counter()-t0:.2f}")
        continue
    dt = time.perf_counter() - t0
    have = set(words(out))
    squashed = re.sub(r"[^a-z]", "", out.lower())
    missing = [w for w in uniq if w not in have and w not in squashed]
    print(f"{rel}\t{npages}\t{len(uniq)}\t{len(missing)/len(uniq):.4f}\t{dt:.2f}")
