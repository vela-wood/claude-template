---
name: superdoc-redlines
description: CLI tool for AI agents to apply tracked changes and comments to DOCX files using ID-based editing
---

# SuperDoc Redlines Skill

## Overview

This tool allows AI agents to programmatically edit Word documents with:
- **Tracked changes** (insertions/deletions visible in Word's review mode)
- **Comments** (annotations attached to document blocks)

Uses **ID-based editing** for deterministic, position-independent edits.

---

## Instructions

### Step 1: Extract Document Structure

```bash
node superdoc-redline.mjs extract --input contract.docx --output contract-ir.json
```

This produces `contract-ir.json` with block IDs like `b001`, `b002`, etc.

### Step 2: Read Document (for analysis)

```bash
# Read entire document (or first chunk if large)
node superdoc-redline.mjs read --input contract.docx

# Read specific chunk for large documents
node superdoc-redline.mjs read --input contract.docx --chunk 1

# Get document stats only
node superdoc-redline.mjs read --input contract.docx --stats-only
```

Output is JSON to stdout - parse it to understand document structure.

### Step 3: Create Edits File

Create `edits.md` using the markdown specification below.

### Step 4: Validate (Optional)

```bash
node superdoc-redline.mjs validate --input contract.docx --edits edits.md
```

Exit code `0` = valid, `1` = issues found.

### Step 5: Apply Edits

```bash
node superdoc-redline.mjs apply \
  --input contract.docx \
  --output redlined.docx \
  --edits edits.md \
  --strict
```

Result: `redlined.docx` with tracked changes visible in Microsoft Word.

**Apply options:**
- `--strict` - Treat truncation/corruption warnings as errors (recommended)
- `--skip-invalid` - Skip invalid edits instead of failing (apply valid ones)
- `-q, --quiet-warnings` - Suppress content reduction warnings
- `--verbose` - Enable detailed logging for debugging
- `--no-track-changes` - Disable track changes mode
- `--no-validate` - Skip validation before applying

---

## Decision Flow

Use this flowchart to determine the correct approach:

### 1. How big is the document?

```
Run: node superdoc-redline.mjs read --input doc.docx --stats-only

If estimatedTokens < 100000:
  → Read whole document: node superdoc-redline.mjs read --input doc.docx

If estimatedTokens >= 100000:
  → Use chunked reading:
    1. node superdoc-redline.mjs read --input doc.docx --chunk 0
    2. Check hasMore in response
    3. Continue with --chunk 1, --chunk 2, etc. until hasMore: false
```

### 2. What operation do I need?

```
Want to CHANGE existing text?
  → Add row: `| b### | replace | true/false | ... |`
  → Add corresponding `### b### newText` section with full replacement text

Want to REMOVE a clause entirely?
  → Add row: `| b### | delete | - | ... |`

Want to ADD a reviewer note WITHOUT changing text?
  → Add row: `| b### | comment | - | your note |`

Want to INSERT new content after a block?
  → Add row: `| b### | insert | - | ... |`
  → Add corresponding `### b### insertText` section
```

### 3. Should I use word-level diff?

```
Making small changes (currency symbols, names, dates)?
  → Use "diff": true (default) - produces minimal tracked changes

Rewriting entire clause with new structure?
  → Use "diff": false - replaces whole block content
```

### 4. How to handle errors?

```
"Block ID not found":
  → If your blockId is not in seqId format (e.g., b001):
    Use seqId values from extracted output.
  → Verify blockId exists in extracted IR (use seqId column)
  → Check for typos (b001 vs B001 - case sensitive)
  → Re-extract IR if document changed

"Truncation warning":
  → Re-generate edit with COMPLETE newText
  → Use markdown format instead of JSON for large edits

"Validation failed":
  → Check required fields are present
  → Verify operation type is valid
  → Ensure newText is not empty for replace operations
```

---

## Critical Constraints

<critical_constraints>

**MUST follow these rules:**

1. **Block IDs are case-sensitive** — Use `b001`, NOT `B001` or `B-001`

2. **Field names are exact** — Use these EXACT names:
   - `blockId` (not `id`, `block_id`, or `blockID`)
   - `operation` (not `type`, `op`, or `action`)
   - `newText` (not `replaceText`, `text`, or `new_text`)
   - `afterBlockId` (not `insertAfter` or `after_block_id`)

3. **`newText` MUST be COMPLETE** — Include the ENTIRE replacement text, not just the changed portion. Truncated text will produce incorrect diffs.

4. **One operation per block** — Don't create multiple edits for the same blockId

5. **Output format is markdown-only** — Do not generate JSON edit objects in agent output. Author edits in `edits.md`.

6. **Insert uses `afterBlockId`** — NOT `blockId`. The new block is inserted AFTER the specified block.

7. **Use `seqId` in edit files** — Always use `seqId` format (`b001`, `b025`, etc.).

</critical_constraints>

---

## Common Mistakes

| ❌ Wrong | ✅ Correct | Notes |
|----------|-----------|-------|
| JSON edit object output | Markdown edits table + text sections | Agent output must be markdown-only |
| `B001` | `b001` | IDs are lowercase and case-sensitive |
| Missing `### b### newText` section for replace ops | Include every replacement text section | Replace needs full new text |
| Extra `##` heading after `## Replacement Text` | Keep only `### b### newText` / `### b### insertText` entries | Parser may treat extra headings as boundaries |
| Multiple rows for same block and operation conflict | One clear operation per block unless intentionally distinct | Avoid merge conflicts and ambiguous intent |

---

## Edit Operations

| Operation | Required Markdown Elements | Description |
|-----------|----------------------------|-------------|
| `replace` | Table row (`Block`, `Op=replace`, optional `Diff`/`Comment`) + `### b### newText` section | Replace block content (uses word-level diff) |
| `delete` | Table row (`Block`, `Op=delete`) | Delete block entirely |
| `comment` | Table row (`Block`, `Op=comment`, comment text) | Add comment to block (no text change) |
| `insert` | Table row (`Block`, `Op=insert`) + `### b### insertText` section | Insert new block after specified block |

### Optional Fields

| Markdown Element | Applies To | Description |
|-------|-----------|-------------|
| `Comment` column | All | Attach rationale or reviewer note |
| `Diff` column | `replace` | Use word-level diff (`true` by default) |

---

## Output Contract (Markdown Only)

Generate edits in markdown format only (`edits.md`). Do not emit JSON edit arrays/objects as the primary output.

Use this structure:

```markdown
## Edits Table

| Block | Op | Diff | Comment |
|-------|-----|------|---------|
| b257 | delete | - | DELETE TULRCA |
| b165 | replace | true | Change to Singapore |

## Replacement Text

### b165 newText
Business Day: a day in Singapore when banks are open.
```

If JSON is explicitly needed by downstream tooling, convert at the command line:

```bash
node superdoc-redline.mjs parse-edits -i edits.md -o edits.json
```

---

## Expected Outputs

### Successful Apply

```json
{
  "success": true,
  "applied": 5,
  "skipped": [],
  "warnings": [],
  "outputFile": "redlined.docx"
}
```

### Apply with Warnings

```json
{
  "success": true,
  "applied": 4,
  "skipped": [
    { "blockId": "b999", "reason": "Block ID not found" }
  ],
  "warnings": [
    { "blockId": "b050", "warning": "Possible truncation detected in newText" }
  ],
  "outputFile": "redlined.docx"
}
```

### Validation Error

```json
{
  "success": false,
  "valid": false,
  "issues": [
    { "blockId": "b999", "error": "Block ID not found in document" },
    { "index": 2, "error": "Missing required field: newText" }
  ]
}
```

### Read Document Output

```json
{
  "success": true,
  "totalChunks": 1,
  "currentChunk": 0,
  "hasMore": false,
  "nextChunkCommand": null,
  "document": {
    "metadata": { "filename": "doc.docx", "blockRange": { "start": "b001", "end": "b150" } },
    "outline": [
      { "title": "1. Definitions", "level": 1, "seqId": "b001" }
    ],
    "blocks": [
      { "seqId": "b001", "type": "heading", "level": 1, "text": "1. Definitions" },
      { "seqId": "b002", "type": "paragraph", "text": "\"Agreement\" means..." }
    ]
  }
}
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Validation error, edit failed, or `--strict` warning |

---

## ID Formats

**Always use seqId.**

| Format | Example | Status |
|--------|---------|--------|
| **seqId** | `b001`, `b025`, `b100` | **Required** — stable, human-readable, portable across CLI commands |

---

## Large Documents (Chunking)

For documents with many blocks:

```bash
# Check if chunking needed
node superdoc-redline.mjs read --input large.docx --stats-only
# Returns: { blockCount, estimatedTokens, recommendedChunks }

# Read chunks sequentially
node superdoc-redline.mjs read --input large.docx --chunk 0
# Returns: { hasMore: true, nextChunkCommand: "..." }

node superdoc-redline.mjs read --input large.docx --chunk 1
# Continue until hasMore: false
```

Each chunk includes the full document outline for context.

---

## Multi-Agent Workflow

For parallel review:

```bash
# 1. Extract once
node superdoc-redline.mjs extract -i contract.docx -o ir.json

# 2. Each sub-agent produces markdown edits
# edits-agent-a.md, edits-agent-b.md

# 3. Convert each markdown file to JSON for merge/apply commands that require JSON inputs
node superdoc-redline.mjs parse-edits -i edits-agent-a.md -o edits-agent-a.json
node superdoc-redline.mjs parse-edits -i edits-agent-b.md -o edits-agent-b.json

# 4. Merge (use --normalize if sub-agents use inconsistent field names)
node superdoc-redline.mjs merge \
  edits-agent-a.json edits-agent-b.json \
  -o merged.json \
  -c error \
  --normalize

# 5. Apply merged edits (use --skip-invalid to continue past bad edits)
node superdoc-redline.mjs apply -i contract.docx -o redlined.docx -e merged.json --skip-invalid
```

**Merge options:**
- `-c error` - Fail if same block edited by multiple agents (safest, recommended)
- `-c first` - Keep first agent's edit
- `-c last` - Keep last agent's edit
- `-c combine` - Merge comments, use first for other operations
- `-n, --normalize` - Fix inconsistent field names (type→operation, etc.)

> **⚠️ Block Range Assignment Warning**
>
> Don't assign sequential block ranges (b001-b300, b301-b600, etc.) without considering clause type distribution. Legal documents have clause types scattered throughout - governing law may appear in definitions, main body, and schedules.
>
> **Best practice:** During discovery, map clause types to actual block locations, then assign agents by clause type grouping. See `skills/CONTRACT-REVIEW-AGENTIC-SKILL.md` for detailed guidance.

---


## Track Changes

**Track changes is ON by default.** Output files open in Microsoft Word with all edits visible as revisions.

| What You See | Meaning |
|--------------|---------|
| Underlined text | Insertion |
| ~~Strikethrough text~~ | Deletion |
| Author name in margin | Who made the change |

### Customize Author

```bash
node superdoc-redline.mjs apply -i doc.docx -o out.docx -e edits.md \
  --author-name "AI Counsel" \
  --author-email "ai@velawood.com"
```

### Disable Track Changes

For direct edits (no revision marks):

```bash
node superdoc-redline.mjs apply -i doc.docx -o out.docx -e edits.md --no-track-changes
```

### Word-Level Diff

`replace` operations use word-level diff by default - only changed words are marked, not the entire block. In markdown format, set the `Diff` column to `false` for a full-block replacement.

---

## Markdown Edit Format

Always draft edits in markdown format first instead of JSON - it's more resilient to generation errors:

Example:

```markdown
## Edits Table

| Block | Op | Diff | Comment |
|-------|-----|------|---------|
| b015 | replace | true | Governing law: move to Singapore |
| b078 | delete | - | TUPE Regulations not applicable in Singapore |
| b045 | replace | true | Replace Companies House with ACRA |
| b102 | comment | - | REVIEW: Consider force majeure provisions |

## Replacement Text

### b015 newText
This Agreement shall be governed by and construed in accordance with the laws of Singapore.

### b045 newText
The Seller shall register the transfer with ACRA within 14 days.
```

**Important:** Do NOT add `## sections` (like `## Notes` or `## Summary`) after `## Replacement Text` — the parser stops at these headings, so any trailing sections will be excluded from the last edit's newText.

**Advantages over JSON:**
- No syntax errors from missing commas
- Partial output still parseable
- Human-readable for review

```bash
# Convert markdown to JSON
node superdoc-redline.mjs parse-edits -i edits.md -o edits.json

# Apply directly from markdown (auto-detects)
node superdoc-redline.mjs apply -i doc.docx -o out.docx -e edits.md
```

---

## CLI Quick Reference

| Command | Purpose |
|---------|---------|
| `extract -i doc.docx -o ir.json` | Get block IDs |
| `read -i doc.docx` | Read for LLM (JSON to stdout) |
| `read -i doc.docx --stats-only` | Check document size |
| `read -i doc.docx --chunk N` | Read specific chunk |
| `validate -i doc.docx -e edits.md` | Validate markdown edits |
| `apply ... --strict` | Fail on truncation warnings |
| `apply ... --skip-invalid` | Skip bad edits, apply good ones |
| `apply ... -q` | Suppress content reduction warnings |
| `apply ... --verbose` | Debug position mapping |
| `apply -i doc.docx -o out.docx -e edits.md` | Apply json edits |
| `apply -i doc.docx -o out.docx -e edits.md` | Apply from markdown (preferred) |
| `merge a.json b.json -o merged.json -c error` | Merge agent edits (strict) |
| `merge ... --normalize` | Fix inconsistent field names |
| `parse-edits -i edits-buyer-favorable.md -o edits-buyer-favorable.json` | Convert markdown edits to JSON when JSON is explicitly needed |
| `to-markdown -i edits.json -o edits.md` | Convert JSON to markdown |

---
## Requirements

- Node.js 18+
- npm dependencies installed (`npm install` in tool directory)

# Sample workflow

## Task

Revise contract.docx (~54k tokens, 1196 blocks) to be as buyer-favorable as possible, producing a redlined DOCX with tracked changes.

### 1. Stats Check

```bash
node superdoc-redline.mjs read --input contract.docx --stats-only
```

Returns block count, estimated tokens, and recommended chunk counts. Used this to determine the document needed 2 chunks to read.

### 2. Focused Reading

```bash
node superdoc-redline.mjs read --input contract.docx --chunk 0
node superdoc-redline.mjs read --input contract.docx --chunk 1
```

Each chunk returns JSON with an outline and an array of blocks. Each block has a `seqId` (e.g., `b464`), `type` (paragraph, heading, tableCell), and `text`. 

I then piped the JSON output through the below Python script to print a condensed `seqId [type]: preview` format for all blocks:

```python
import json, sys
      data = json.load(sys.stdin)
      print(f'Chunk: {data[\"currentChunk\"]}, hasMore: {data[\"hasMore\"]}')
      if data.get('nextChunkCommand'):
          print(f'Next: {data[\"nextChunkCommand\"]}')
      print()
      # Print outline
      print('=== OUTLINE ===')
      for item in data['document'].get('outline', []):
          indent = '  ' * (item.get('level', 1) - 1)
          print(f'{indent}{item[\"seqId\"]}: {item[\"title\"]}')
      print()
      # Print blocks
      print('=== BLOCKS ===')
      for b in data['document']['blocks']:
          text = b.get('text', '')
          preview = text[:150].replace('\n', ' ')
          btype = b.get('type', 'para')
          lvl = f' L{b[\"level\"]}' if 'level' in b else ''
          print(f'{b[\"seqId\"]} [{btype}{lvl}]: {preview}')
```

I then extracted the full text of ~30 blocks targeted for editing by filtering on specific seqIds.

### 3. Edits Authoring (Manual)

I wrote `edits-buyer-favorable.md` in the markdown edit format:

- **Edits Table**: A markdown table with columns `Block | Op | Diff | Comment` — one row per edit (18 replaces + 7 comments = 25 total).
- **Replacement Text**: A section with `### bXXX newText` headings, each followed by the complete replacement text for that block.

The replacement text was crafted by taking the original block text from the read output and modifying it. No template or generation tool was used — I composed each edit based on legal analysis and direction from the user.

### 4. Parse Markdown to JSON

```bash
node superdoc-redline.mjs parse-edits -i edits-buyer-favorable.md -o edits-buyer-favorable.json
```

Converts the markdown format into the JSON schema the tool expects. Output: 25 edits. I had to manually fix the `version` field in the JSON (the parser left it empty; it needs to be `"0.2.0"`).

### 5. Validation

```bash
node superdoc-redline.mjs validate --input contract.docx --edits edits-buyer-favorable.json
```

Result: 16 valid, 9 invalid. The 9 "invalid" edits were false positives — the validator flagged `"Unclosed quote at end"` on any block whose replacement text contained defined terms in quotes (e.g., `"Fundamental Representations" means...`). These are legitimate legal drafting patterns, not truncation.

### 6. Apply (First Attempt)

```bash
node superdoc-redline.mjs apply --input contract.docx --output redlined.docx --edits edits-buyer-favorable.json -q
```

Applied 16, skipped the same 9 flagged by validation. The `-q` flag suppresses content reduction warnings but does not override invalid-edit skipping.

### 7. Apply (Second Attempt — Successful)

```bash
node superdoc-redline.mjs apply --input contract.docx --output redlined.docx --edits edits-buyer-favorable.json --no-validate -q
```

The `--no-validate` flag bypasses pre-apply validation entirely. All 25 edits applied. Output is a DOCX with tracked changes visible in Word.

## Key Flags Used

| Flag | Purpose |
|------|---------|
| `--stats-only` | Size check without reading content |
| `--chunk N` | Read specific chunk of a large document |
| `-q` | Suppress content reduction warnings |
| `--no-validate` | Skip validation before applying (needed for blocks with quoted defined terms) |
| `--strict` | Treat warnings as errors (did not use) |
| `--skip-invalid` | Apply valid edits, skip invalid ones (did not use) |

## Lessons Learned

1. **Quote detection false positives**: The validator flags replacement text containing quotation marks (common in legal defined terms like `"Knowledge"`, `"Material Adverse Effect"`) as "unclosed quotes at end." Workaround: use `--no-validate` when edits contain quoted defined terms.
2. **Version field**: The `parse-edits` markdown-to-JSON converter may leave the `version` field empty. Always include `"version": "0.2.0"` in the root object.
3. **Markdown format is more resilient**: The markdown edit format avoids JSON syntax errors (missing commas, escaping issues) and is easier to author for large edit sets.
4. **Block IDs are stable**: The `seqId` values from `read` can be used directly in edits without needing to run `extract` separately.