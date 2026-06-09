## Generic data collection

### Polish procurement spiders

#### `poland`

Per-procedure file collector for `ezamowienia.gov.pl`. Walks `Search/SearchTenders` and writes
each tender's JSON metadata, tender-document index, raw + parsed BZP announcement, and binary
attachments under `data/poland/<crawl_directory>/<tenderId>/`.

Caveats:

- `Search/SearchTenders` only indexes procedures in state `Initiated`. Tenders that have moved
  on to award / agreement / performance stages aren't reachable from this catalogue, and the
  follow-up notices (award, update, performance) aren't captured. See the
  [open follow-up issue](#follow-up-issues) for details.

#### `poland_kio_orzeczenia`

Walks `/Home/Details/<id>` on `orzeczenia.uzp.gov.pl` and writes one row per ruling (both KIO
appeals and KIO/KD control opinions). Discovers the upper ID bound by binary search at startup;
pass `-a max_id=N` to skip the probe.

Caveats:

- The `articles` field comes from the `Kluczowe przepisy ustawy Pzp` block — an editorial short
  list maintained by the search engine, not an exhaustive extraction of every article cited in
  the ruling's body. Use it for "which articles drive disputes", not "every mention".
- The same KIO archive includes citations under both the current Pzp (2019, in force from 2021)
  and the prior Pzp 2004. Articles that exist in only one regime can show up with sub-clause
  combinations that look wrong in the other (e.g., `art. 16 ust. 1` makes sense in Pzp 2004 but
  not in Pzp 2019, where art. 16 has no `ust.`). The spider preserves these as-is.
- Some pre-2017 rulings have neither an `issue_date` in the metadata nor a `/YY` year suffix in
  the case number; those are dropped because their date cannot be determined.

#### `poland_uzp_kontrole`

Walks the 17 violation-category landing pages under `gov.pl/web/uzp/informacje-o-wynikach-…`.
Writes one row per finding with title, violation summary (carrying inline PZP article
references) and the attached PDF URLs.

Caveats:

- Scope is "postępowania wszczęte od 28.07.2016" only. The pre-2016 archive is a separate page
  not crawled by this spider.
- Contracting-authority names in UZP findings are often anonymised, which limits joining
  findings back to specific named tenders.
- No `from_date` filter — the full corpus is re-fetched on every run (it's small: ~400 records
  in seconds).

#### `poland_cpv`

Joins KIO rulings and UZP findings to their procurement CPV codes. For each record produced
by `poland_kio_orzeczenia` and `poland_uzp_kontrole`, downloads the associated PDF, extracts
the first 20 pages with PyMuPDF, regexes out BZP / TED notice numbers, and looks each up:

- BZP via `mo-board/api/v1/Board/Search?NoticeNumber=…` (Polish procurements).
- TED via `POST api.ted.europa.eu/v3/notices/search` (above-EU procurements).

Writes one JSONL row per (source, record_id, notice) under
`data/poland_cpv/<crawl>/joined.json`, with `notice_kind` set to `BZP` or `TED` so you can
tell which source resolved the CPV. CPV codes are normalised to the 8-digit base form (no
trailing check digit) so BZP- and TED-resolved codes are directly comparable.

PDF and lookup responses are cached under `data/poland_cpv/_httpcache/`, so re-runs after
regex / extraction tweaks don't re-hit the servers.

PyMuPDF is AGPL-3.0 licensed (the only AGPL dep in this stack). Combining this repo with
PyMuPDF means the combined work effectively has to comply with AGPL terms (source disclosure
including for network-deployed modified versions). Chosen here for re-processing speed —
PyMuPDF extracts the first 20 pages in ~80 ms vs ~3 s for pdfminer, which matters once the
PDFs are cached and we're iterating on the regex / extraction logic.

Caveats:

- Many KIO rulings are procedural ("umorzenie postępowania") and don't cite the notice in the
  PDF text at all; the spider records these with `pdf_status=no_notice`.
- Some KIO/UZP PDFs are scanned images — text extraction returns empty, surfaced as
  `pdf_status=empty`. OCR is out of scope.
- TED's current Search API does not reliably index pre-migration (~pre-2019) notices, so
  older TED references show up as `lookup_hit=false`. About half our UZP TED references are
  in that older range.
- UZP findings are partly anonymised and the notice number may be redacted in the published
  "Informacja o wyniku kontroli" PDF.

#### Operational notes

- `orzeczenia.uzp.gov.pl` has been observed to rate-limit large bursts (one run with
  `CONCURRENT_REQUESTS=32` against the full 34k ID range slowed to ~1 req/sec mid-crawl). If
  you hit it, throttle with `-s DOWNLOAD_DELAY=0.1 -s CONCURRENT_REQUESTS=8`.
- A full `poland_cpv` run is the slowest piece — PDF parsing in the Scrapy event loop is
  sequential, so ~10k records take several hours. The HTTP cache means re-runs after that are
  fast.

### `polandpzp` command

Aggregates PZP-article distributions from the latest `poland_kio_orzeczenia` and `poland_uzp_kontrole`
crawls under `FILES_STORE`. Writes `pzp_article_counts.csv`, `pzp_subclause_counts.csv` and
`uzp_category_breakdown.csv` to the cwd or `--output-dir`. When a `poland_cpv` crawl is also
present, additionally writes `cpv_counts.csv`.

```
scrapy polandpzp                                # latest crawls → CSVs in cwd
scrapy polandpzp --output-dir ./out
scrapy polandpzp --kio-crawl 20260603_142206    # specific crawl dirs
```
