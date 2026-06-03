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

#### `kio_orzeczenia`

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

#### `uzp_kontrole`

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

#### Operational notes

- `orzeczenia.uzp.gov.pl` has been observed to rate-limit large bursts (one run with
  `CONCURRENT_REQUESTS=32` against the full 34k ID range slowed to ~1 req/sec mid-crawl). If
  you hit it, throttle with `-s DOWNLOAD_DELAY=0.1 -s CONCURRENT_REQUESTS=8`.
- PDF attachments referenced by KIO rulings and UZP findings are URL-captured but not
  downloaded. Resolving them to BZP notice numbers and CPV codes is the
  [CPV follow-up](#follow-up-issues).

### `polandpzp` command

Aggregates PZP-article distributions from the latest `kio_orzeczenia` and `uzp_kontrole`
crawls under `FILES_STORE`. Writes `pzp_article_counts.csv`, `pzp_subclause_counts.csv` and
`uzp_category_breakdown.csv` to the cwd or `--output-dir`.

```
scrapy polandpzp                                # latest crawls → CSVs in cwd
scrapy polandpzp --output-dir ./out
scrapy polandpzp --kio-crawl 20260603_142206    # specific crawl dirs
```

### Follow-up issues

- **Resolve KIO/UZP findings to CPV codes.** See the CPV follow-up plan checked in alongside
  this README.
