---
name: citation-hallucination-audit
description: Batch-verify the references of a manuscript and compute a "citation hallucination rate". Extracts DOIs from a citation list, queries CrossRef and DataCite, and issues a five-level verdict (MATCH / REVIEW / MISMATCH / NOT_FOUND / UNKNOWN). Use when the user asks "citation hallucination rate", "are these references real", "did the AI invent these citations", "batch-check these DOIs", "how many references in this paper are fake", or when auditing AI-assisted manuscripts.
agent_created: true
---

# Citation Hallucination Audit

## When to use

- Verifying whether the references in a manuscript are real
- Producing a "citation hallucination rate" as a self-check or as a meta-research metric
- Auditing manuscripts that were drafted with LLM assistance

## The concept (standard framing to give the user)

LLMs fabricate references in three escalating ways:

| Type | What it looks like | Verdict |
|---|---|---|
| Pure fabrication | The paper does not exist; the DOI resolves to nothing | `NOT_FOUND` |
| **Title swap** | The DOI is real, authors and journal are correct, but the title belongs to a *different* paper | `MISMATCH` |
| Exists but irrelevant | Real paper, does not support the claim it is cited for | not machine-detectable |

**A fabricated DOI is usually obvious** — the format is wrong. A **valid DOI attached to the wrong
title is not**, which is why format validation is worthless here and the only reliable check is
hitting the registration API.

A real case caught by this tool: authors, journal, volume, pages and DOI all correct, only the title
replaced with that of another paper. No reference manager would flag it.

**Rate = `(MISMATCH + NOT_FOUND) / judgeable entries`.**
`UNKNOWN` (network / rate-limit failures) must **never** enter the numerator — a failed lookup is not
evidence of fabrication.

## Algorithm

```
locate reference section -> regex-extract DOIs -> de-duplicate
    -> attach each DOI to its full reference entry  (see "Pitfalls")
    -> CrossRef /works/{doi}
        |-- 200 -> compare title against the entry
        |         |-- >= 65%  -> MATCH
        |         |-- 45-65%  -> REVIEW
        |         `-- <  45%  -> MISMATCH
        `-- 404 -> DataCite /dois/{doi}   (covers arXiv, Zenodo, figshare)
                   |-- 200 -> same comparison
                   `-- 404 -> NOT_FOUND
```

Title overlap = `|title words ∩ entry words| / |title words|`, lowercase words of length >= 4.

## Pitfalls — avoid these four

Each of these produced a massive false-positive rate during development.

**1. Do not split reference entries by line.**
`.docx` -> text conversion turns `</w:p>` into `\n`, which routinely separates the title from its
DOI. The entry context ends up empty and the entire paper scores 100% hallucination.

**2. Never use a fixed character window around the DOI.**
"260 characters before the DOI" fails on long titles — the window reaches into the *previous* entry.
In testing this flagged 28 of 30 citations as swapped titles. None of them were.

**3. Some entries contain no title at all** (e.g. `19. AJPM Focus 2026; 5: 100434.`).
Their overlap is necessarily 0%. Fall back to `REVIEW` when the entry has fewer than 6 long words —
do not accuse the author of a swapped title.

**4. Coverage alone is not enough — a valid citation may only carry the main title.**
Many reference styles drop the subtitle. Scoring purely as "what fraction of the database title
appears in the entry" then sinks a perfectly correct citation below threshold (observed: a correct
citation scored 38%). Always also compute the hit rate of the **first six content words** of the
database title and take the maximum of the two measures. A swapped title still scores near zero on
both, so this adds no false negatives.

Working approach: flatten the reference block to one string, split on numbering boundaries
(`(?=\b\d{1,3}\.\s+[A-Z])`), merge DOI-less fragments into the preceding entry, attach each DOI to
the entry containing it, and fall back to a positional window only if fewer than 3 entries were found.

## Running it

Script: `scripts/citation_audit.py` — standard library only, no third-party dependencies.

```bash
# single manuscript
python scripts/citation_audit.py manuscript.docx

# with machine-readable and Markdown output
python scripts/citation_audit.py manuscript.docx --json out.json --md out.md

# ad-hoc reference text
python scripts/citation_audit.py --text "Smith J. ... doi:10.1038/xxx"
```

Supported: `.docx` (read straight from `word/document.xml`, no python-docx), `.txt`, `.md`, `.csv`.

For batches, loop over files with `subprocess`. **Sharing one cache across manuscripts pays off
enormously**: a first pass over 137 citations took ~8 minutes, while every re-run afterwards took
0.3 seconds — which also means re-running after a threshold change is essentially free.

## Hard constraints

1. **Send a `mailto:` User-Agent** to enter the CrossRef polite pool. Set `CITATION_AUDIT_CONTACT`
   to your own address; otherwise the default placeholder is used and you are throttled harder.
2. **Retry at least 5 times with exponential backoff** `sleep(3 * (attempt + 1))`, and keep the
   inter-request interval at 2.0s by default. CrossRef is very sensitive to burst load.
3. **arXiv DOIs live in DataCite, not CrossRef** (`10.48550/arXiv.*`). A CrossRef-only implementation
   reports every arXiv citation as fabricated. Always fall back to the second database.
4. **Cache to `.citation_audit_cache.json`**, shared across runs. Disable with `--no-cache`.
5. **Reference-section slicing** is heuristic (looks for `References`, `Bibliography`,
   `参考文献`). If it cannot find the heading it warns you — confirm manually before trusting the count.
6. **Each DOI counts once.** The denominator is unique entries, not occurrences.
7. **Only DOI-bearing entries are verified.** Report the unverified remainder explicitly; never let it
   disappear silently. Covering it needs a second layer (title+year reverse lookup against CrossRef).

## Output

Console progress (`OK` / `~~` / `!!` / `XX` / `??` per DOI) plus a report containing: entry count,
share with a DOI, the five category counts, the **hallucination rate**, the REVIEW list, and full
detail on every MISMATCH / NOT_FOUND item — including the database's real title, so the user can
compare side by side.

## Platform notes

- Works on Windows, macOS and Linux. On Windows the managed Python path and
  `PYTHONIOENCODING=utf-8` are recommended when running under Git Bash, otherwise non-ASCII output
  can be mangled.
- Under a restricted shell, `rm` / `grep` / `head` may be blocked by a shim. Use Python
  (`os.remove`, string filtering) instead of shell utilities.
- When writing Python that contains Chinese text, use `「」` or `“”` for inner quotes — a bare
  ASCII `"` inside a double-quoted string truncates it and raises a SyntaxError.
