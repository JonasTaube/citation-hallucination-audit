# Citation Hallucination Audit

**[中文说明 →](README.zh-CN.md)**

Batch-verify the references of a manuscript against **CrossRef** and **DataCite**, and compute a
**citation hallucination rate**.

A zero-dependency Python tool (standard library only) that answers one question:

> Of all the references in this paper, how many point to something that does not actually exist — or exists but is not what the citation claims?

---

## Why this exists

Large language models fabricate references in three escalating ways, and each is harder to catch than the last:

| Type | What it looks like | Verdict |
|---|---|---|
| **Pure fabrication** | The paper does not exist; the DOI resolves to nothing | `NOT_FOUND` |
| **Title swap** | The DOI is real, the authors and journal are correct — but the title belongs to a *different* paper | `MISMATCH` |
| **Exists but irrelevant** | The paper is real, but does not support the claim it is cited for | not machine-detectable |

The middle one is the dangerous one. A fabricated DOI usually has an obviously wrong format, so a
human skimming the reference list can spot it. A **valid DOI attached to the wrong title** survives
every manual check, every reference-manager import, and every DOI syntax validator. A reviewer who
clicks the link lands on a real paper page and sees nothing wrong — unless they read the title carefully.

Real example this tool caught in the wild (author, journal, volume, pages and DOI all correct —
only the title was replaced):

```
DOI            10.1002/sim.9884
Database says  Comparison of combination methods to create calibrated ensemble
               forecasts for seasonal influenza in the U.S.
Citation says  Practical considerations of the FluSight multiseason influenza
               forecasting challenge
```

No reference-manager would flag that. An API call does.

---

## Install

This repository is packaged as a **skill** for WorkBuddy / Claude-style agents.

```bash
git clone https://github.com/JonasTaube/citation-hallucination-audit.git
cp -r citation-hallucination-audit ~/.workbuddy/skills/
```

Or just take `scripts/citation_audit.py` — it is a standalone CLI with no dependencies.

---

## Usage

```bash
# Audit a manuscript
python scripts/citation_audit.py manuscript.docx

# Write machine-readable + Markdown output
python scripts/citation_audit.py manuscript.docx --json report.json --md report.md

# Audit a raw reference block
python scripts/citation_audit.py --text "Smith J. Title. Nature. 2020;1:2. doi:10.1038/xxxxx"

# Batch (cache is shared across runs, so repeats are near-instant)
for f in *.docx; do python scripts/citation_audit.py "$f" --json "${f%.docx}.json"; done
```

Supported inputs: `.docx` (read directly from `word/document.xml` — no `python-docx` needed),
`.txt`, `.md`, `.csv`.

Useful flags:

| Flag | Default | Meaning |
|---|---|---|
| `--interval` | `2.0` | Seconds between API calls. Keep it ≥1s to stay polite with CrossRef. |
| `--no-cache` | off | Disable the local `.citation_audit_cache.json`. |
| `--json` | — | Write a full JSON report (summary + per-citation detail). |
| `--md` | — | Write a Markdown report. |

### Set your contact address

CrossRef gives polite-pool access to clients that identify themselves. Set an env var so the
`mailto:` in the User-Agent points at you:

```bash
export CITATION_AUDIT_CONTACT="you@example.com"
```

---

## Output

```
====================================================================
Citation Hallucination Audit
manuscript: manuscript.docx
====================================================================

Reference entries (heuristic count):   44
  with a DOI (auto-verifiable):        41  (93.2%)
  without a DOI (not checked):          3  <-- needs manual review

Unique verifiable citations:           41

  [OK ] MATCH     exists, title matches         38
  [~~ ] REVIEW    partial title match            3  <-- confirm manually
  [!! ] MISMATCH  exists, wrong title            0
  [XX ] NOT_FOUND no record in either database   0
  [?? ] UNKNOWN   network error                  0

--------------------------------------------------------------------
Citation hallucination rate = (0 + 0) / 41 = 0.0%
--------------------------------------------------------------------
```

**Rate definition:** `(MISMATCH + NOT_FOUND) / judgeable entries`.
`UNKNOWN` is excluded from both numerator and denominator — a rate-limited lookup is not evidence
of misconduct.

`REVIEW` covers two benign cases and is **not** counted as hallucination:

- the title partially matches (subtitle added/removed, British vs American spelling, truncation);
- the citation entry contains no title at all (e.g. `19. AJPM Focus 2026; 5: 100434.`).

---

## How it works

```
locate the reference section
    -> extract DOIs by regex, de-duplicate
    -> attach each DOI to its full reference entry  (see "Pitfalls" below)
    -> GET api.crossref.org/works/{doi}
         |-- 200 -> compare returned title with the citation entry (word overlap)
         |         |-- >= 65%  -> MATCH
         |         |-- 45-65%  -> REVIEW
         |         `-- <  45%  -> MISMATCH
         `-- 404 -> GET api.datacite.org/dois/{doi}
                    |-- 200 -> same comparison
                    `-- 404 -> NOT_FOUND
```

Title overlap takes the **maximum of two measures**:

- `full` — what fraction of the database title's words appear in the citation entry;
- `head` — the hit rate of the **first six content words** of the database title, which rescues
  citations that legitimately carry only the main title and drop the subtitle.

Counting lowercase words of four or more characters.

### Two databases, not one

arXiv DOIs (`10.48550/arXiv.*`) and dataset DOIs (Zenodo, figshare) are registered with **DataCite**,
not CrossRef. A CrossRef-only implementation reports every arXiv citation as a hallucination.

---

## Pitfalls (learned the hard way)

These four mistakes each produced *massive* false-positive rates during development — worth knowing
before you write your own version.

**1. Do not split reference entries by line.**
Converting `.docx` to text turns `</w:p>` into `\n`, which routinely separates a title from its DOI.
The context ends up empty and the whole paper scores 100% hallucination.

**2. Never use a fixed character window around the DOI.**
"Take the 260 characters before the DOI" fails on long titles: the window reaches back into the
*previous* entry, and 28 of 30 citations get flagged as mismatched. None of them were.

**3. Some citation entries contain no title at all** (e.g. `19. AJPM Focus 2026; 5: 100434.`).
Their title overlap is necessarily 0% — that is a formatting issue, not a swapped title.

**4. Coverage alone is not enough.** Many reference styles drop the subtitle. If you score purely as
"what fraction of the database title appears in the entry", a perfectly correct citation can fall
below threshold — one scored 38% in testing. Always also measure the hit rate of the title's first
six content words and take the better of the two. A genuinely swapped title still scores near zero
on both, so this costs no sensitivity.

The working approach: flatten the reference block into one string, split on numbering boundaries
(`(?=\b\d{1,3}\.\s+[A-Z])`), merge fragments that carry no DOI into the previous entry, then attach
each DOI to the entry it lives in.

## Rate limiting

CrossRef throttles aggressively and returns HTTP 429 under load. The client therefore:

- sends a `mailto:` User-Agent to enter the polite pool;
- retries up to 5 times with exponential backoff `sleep(3 * (attempt + 1))`;
- defaults to a 2-second interval between calls;
- caches every lookup to `.citation_audit_cache.json`, shared across manuscripts.

That cache is why re-running with adjusted thresholds is nearly free — a first pass over 137
citations took ~8 minutes; every subsequent re-run took 0.3 seconds.

---

## Limitations

Be honest about what this does **not** do:

- **Metadata only.** It verifies that a citation points at a real, correctly-titled paper. It cannot
  tell whether the paper actually supports the sentence citing it.
- **DOI-only coverage.** Entries without a DOI are counted and reported but not verified. Vancouver-style
  reference lists can leave half the list unchecked.
- **Heuristic entry counting.** The number of reference entries is estimated from numbering patterns;
  treat it as an approximation.
- **The 45/65 thresholds are tunable, not universal.** They are calibrated for English
  science/medicine reference lists. Re-tune with `MATCH_T` / `REVIEW_T` for other formats.
- **`REVIEW` items still need a human.** The tool narrows 60 citations down to 3 that deserve two
  minutes of attention; it does not make the final call.

---

## Related reading

- Gao CA, et al. Comparing scientific abstracts generated by ChatGPT to real abstracts with detectors
  and blinded human reviewers. *npj Digital Medicine*. 2023;6:75. https://doi.org/10.1038/s41746-023-00819-6
- Májovský M, et al. Artificial Intelligence Can Generate Fraudulent but Authentic-Looking Scientific
  Medical Articles: Pandora's Box Has Been Opened. *J Med Internet Res*. 2023;25:e46924. https://doi.org/10.2196/46924
- *Nature* editorial. Tools such as ChatGPT threaten transparent science; here are our ground rules
  for their use. 2023. https://doi.org/10.1038/d41586-023-00191-1

---

## License

MIT — see [LICENSE](LICENSE).
