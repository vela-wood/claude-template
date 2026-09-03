Proposed edits, ordered by how much friction they would have removed today.

**1. Automate the underscore-blank workaround in the wrapper.** This is the most repeated adeu failure across sessions (memory has five dated entries on it). Today it cost a probe, a source read of `engine.py`, and a hand-written XML post-pass. Make `run_redline.py apply` do it by default: rewrite `_{2,}` in every `new_text` to `XBLANK<n>X` before calling adeu, then rewrite `word/document.xml` back to underscores after. Ship today's `adeu/postprocess_docx.py` as `scripts/postprocess_docx.py`. Add a `--raw` flag to skip it.

**2. Fold the paragraph-mark sweep into the same post-pass.** Wholly deleted paragraphs whose target lost a trailing space leave a whitespace-only run, so the paragraph mark survives and an empty bullet or numbered item appears on accept. The post-pass already handles it. Document that JSON deletes otherwise do mark the paragraph deleted, which corrects the older memory note saying text-file apply was the only route.

**3. Make verification a required step with a script.** SKILL.md says "use a preview or diff flow" but adeu's previews show drifted context and its per-edit ✅ is not proof. Add `scripts/verify_apply.py`: extract the output with `--clean-view --page all`, strip extractor noise, and diff paragraph-by-paragraph against the intended text. Today's check caught the six missing blanks that the apply log called successful.

**4. Ship a sidecar-to-edits generator.** "Apply the edits I made in the .md to the .docx" is a recurring request, and a generator was written from scratch both on 8/27 and today. Promote `adeu/build_test_edits.py` to `scripts/sidecar_to_edits.py` with the pieces that were hard-won: order-preserving similarity alignment so a deleted paragraph does not steal its neighbor's anchor, unescaping `\_`, stripping `## ` and `- ` markers, keeping whitespace-only sentence-spacing changes, and the duplicate-paragraph handling below.

**5. Document the pitfalls that are stable adeu behavior, in SKILL.md rather than memory.** Memory recall is not guaranteed; the skill always loads. Add a short "Known limits" section:
- `extract` defaults to synthetic page 1. Always pass `--page all`, and `--clean-view` when building apply input.
- Extract output carries a `File Path` header and footer lines (`## `, `Page  of `, `---`, `## Footnotes`). They are not document text and break text-file apply.
- Text-file apply wipes existing markup and fails verification on `## **Heading**` paragraphs. Prefer JSON batches on anything but a pristine, heading-free doc.
- No occurrence selector and no `last` match mode. Deleting one of two identical paragraphs: `match_mode: "all"` then re-add the kept copy via `predecessor\n\nkept` on its neighbor.
- Multi-paragraph `target_text` is refused when body text sits on both sides of the break. Fuzzy `_+` does not swallow whole underscore runs in docx mode, so targets need the full run.
- A batch validates sequentially against intermediate state, and one failure rolls back everything. A `.unverified.docx` diagnostic is left behind on text-apply failure; delete it.
- Comments cannot attach inside footer parts or on paragraph-splitting edits.

**6. Fix the author default.** USERPREFS requires "Andrew Lin" but the wrapper falls through to the OS username `alin`. Have the wrapper set `ADEU_AUTHOR=Andrew Lin` when unset, or append `--author` if absent.

**7. Tidy the `adeu/` working folder.** It holds over 100 files with three naming schemes. Add a rule: `adeu/<docstem>/<purpose>_YYYYMMDD.<ext>`, and have the wrapper create the subfolder. Generic helpers move to `scripts/`.

**8. Smaller text fixes.** The skill's `edits.json` schema omits `match_mode` and `regex`, which the CLI supports and which were needed today. Note that `new_text` paragraph breaks inherit the anchor's list style, and that `insert_row` cells bypass the Markdown parser, so blanks survive there without the workaround.

Items 1 through 4 are code and I can implement them; 5 through 8 are text edits to SKILL.md and the wrapper.