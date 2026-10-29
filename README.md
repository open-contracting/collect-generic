## Generic data collection

### Polish procurement spiders

#### `poland`

Per-procedure file collector for `ezamowienia.gov.pl`. For each tender, writes:

- `tender.json`: the `Search/GetTender` response.
- `documents.json`: the `Search/GetTenderDocuments` response, which lists every document version, including deleted ones, with `createDate`, `publishedDate`, `deleteDate` and `deleteReason`.
- `notices.json`: the tender's contract notice and notice changes, found by listing its organization's notices in `Board/Search` and keeping those with its `tenderId`.
- `notices/<number>.{html,json}`: each notice's `Board/GetNoticeHtmlBody`, raw and parsed.
- `attachments/<documentId>__<filename>`: each document that still has a file, from `Tender/DownloadDocument/{tenderId}/{documentId}`.

Entry modes:

- By default, walks `Search/SearchTenders`, which only indexes procedures in state `Initiated`.
- `-a from_date=2025-03-01 -a until_date=2025-03-31`: walks contract notices in `Board/Search` by publication date (both days included), which reaches closed procedures too. `from_date` defaults to 2021-01-01, when e-Zamówienia opened.

Both write under `data/poland/<crawl_directory>/<tenderId>/`, and skip tenders whose `tender.json` is already on disk.

- `-a notice_numbers=<csv>`: collects the tender documents of UZP controls, for `oecd-poland/analysis/poc/tender_document_requests.csv`. The CSV needs `control_id` and `notice_ids` (`; `-separated BZP, OJ S or eForms notice numbers, normalised as in `poland_uzp_wyniki_kontroli`). Optional `cn_split`, `art124_split` and `missing_identifier` columns set each control's priority group (`cn_test`, `art124_test`, `dev`, `art124_train`, `cn_aux`, assigned in that order), and `-a priority=cn_test,art124_test` selects groups. `-a sample=N` takes the first N controls in priority order.

In the `notice_numbers` mode, each notice is resolved:

- BZP notices via `Board/Search?NoticeNumber=…`. If the procedure isn't run on e-Zamówienia (`isProceedingOnPlatform: false`), the documents URL is read from the notice's *Adres strony internetowej prowadzonego postępowania*.
- TED notices via the TED Search API: the documents URL (BT-15, or else the restricted-documents or submission URL), and the procedure's other notices (by `procedure-identifier`, for eForms). Pre-eForms notices have the documents URL only in the notice XML (`URL_DOCUMENT`).

A documents URL on `ezamowienia.gov.pl` gives the tender ID. The files go under `data/poland_tender_documents/<crawl_directory>/<control_id>/<tenderId>/`.

A documents URL on `platformazakupowa.pl` is either the procedure's page (`/transakcja/<id>`) or the buyer's page (`/pn/<buyer>`), in which case the procedure is found by searching the buyer's procedures for the notice's reference number (*Numer referencyjny*, or TED's `internal-identifier-proc`). The spider saves the procedure page and every file in its attachments table under `<control_id>/platformazakupowa-<id>/`, and takes the bid deadline from the page. Files there are public and kept after the procedure closes.

The crawl writes:

- `manifest.json`: one JSON line per control, with `control_id`, `group`, `kind`, `notice_ids`, `source` (`ezamowienia`, `platform:<host>` or `unresolved`), `procedure_url`, `tender_ids`, `bid_deadline_original`, `bid_deadline_final`, `status` (`complete`, `partial` or `not_found`), `reason` and `documents`. Each document has `path` (relative to the crawl directory, or null if there's no file), `url`, `title`, `published_at`, `role` (`swz`, `opz`, `ppu`, `annex`, `swz_change`, `qa`, `notice`, `notice_change` or `other`, from its name), `superseded` (deleted by the buyer), `state` and `after_deadline`.
- `coverage.json`: counts by priority group and status, counts by source, the controls that aren't `complete`, and the controls skipped for having no notice number.

Attachments and notice bodies are cached under `.scrapy/data/poland/_httpcache/`. API responses aren't cached, so re-runs see new documents.

Caveats:

- e-Zamówienia deletes a procedure's files 3 months after it moves to *Zawarta umowa*, *Unieważnione*, *Anulowane* or *Rozstrzygnięte* (§8(12) of the platform's terms of use). Their documents stay in `documents.json` with state `DeletedAutomatically`, but the download returns HTTP 400 (*Brak załącznika dla wskazanego dokumentu*), so the spider doesn't request them. Files of procedures still in state `Initiated` are kept, however old.
- Only `platformazakupowa.pl` has an adapter. Controls whose documents are on another platform are `not_found` with `source` set to `platform:<host>`. Most documents URLs are the buyer's page on the platform, not the procedure's.
- On `platformazakupowa.pl`, a file's `state` is the attachments table's source column (e.g. *Proceeding* or *Public message*), and replaced files aren't marked as superseded.
- `ted.europa.eu` can answer with an AWS WAF challenge (HTTP 202, empty body) instead of the notice XML, and the TED Search API with HTTP 429. Such controls are `unresolved`, with that reason. Re-run later.
- `Board/Search` returns at most 10 results per page, and can time out (the spider retries). The date mode makes about 100 requests per day of notices.
- TED gives bid deadlines as dates. `bid_deadline_final` comes from the e-Zamówienia tender's valid submission term, when there is one.
- Follow-up notices (award, agreement, performance) aren't collected. See [#40](https://github.com/open-contracting/collect-generic/issues/40).

#### `poland_kio_orzeczenia`

Walks `/Home/Details/<id>` on `orzeczenia.uzp.gov.pl` and writes one row per ruling (both KIO appeals and KIO/KD control opinions). Discovers the upper ID bound by binary search at startup; pass `-a max_id=N` to skip the probe. Keeps only rulings from 2023 onward by default (compared by year); pass `-a from_date=2016-01-01` to go further back.

Caveats:

- The `articles` field comes from the `Kluczowe przepisy ustawy Pzp` block — an editorial short list maintained by the search engine, not an exhaustive extraction of every article cited in the ruling's body. Use it for "which articles drive disputes", not "every mention".
- The same KIO archive includes citations under both the current Pzp (2019, in force from 2021) and the prior Pzp 2004. Articles that exist in only one regime can show up with sub-clause combinations that look wrong in the other (e.g., `art. 16 ust. 1` makes sense in Pzp 2004 but not in Pzp 2019, where art. 16 has no `ust.`). The spider preserves these as-is.
- Some older rulings (mostly pre-2015) have neither an `issue_date` in the metadata nor a `/YY` year suffix in the case number; those are dropped because their date cannot be determined.

#### `poland_uzp_kontrole`

Walks the 17 violation-category landing pages under `gov.pl/web/uzp/informacje-o-wynikach-…`. Writes one row per finding with title, violation summary (carrying inline PZP article references) and the attached PDF URLs.

Caveats:

- Scope is "postępowania wszczęte od 28.07.2016" only. The pre-2016 archive is a separate page not crawled by this spider.
- Contracting-authority names in UZP findings are often anonymised, which limits joining findings back to specific named tenders.
- No `from_date` filter — the full corpus is re-fetched on every run (it's small: ~400 records in seconds).

#### `poland_uzp_wyniki_kontroli`

Collects every UZP control result PDF published on the year pages of [kontrole uprzednie](https://www.gov.pl/web/uzp/kontrole-uprzednie) (ex-ante, 2023 onward) and [kontrole doraźne](https://www.gov.pl/web/uzp/kontrole-od-2021-roku) (ad-hoc, 2021 onward), discovering the year pages from the parent pages' links. Unlike `poland_uzp_kontrole`, which crawls the anonymised findings by violation category, this crawls the full results per control.

For each file, saves the PDF and its text under `data/poland_uzp_wyniki_kontroli/<crawl_directory>/files/<uuid>.{pdf,txt}`, and writes one JSONL row per file to `files.json` with:

- `control_id`, normalised like `KU_12_25`, `KND_132_21` or `KUD_2_25` (`WKU` becomes `KU`). If a title omits the year, the year page's is used. A file about two controls (`KND_20_22_i_KND_21_22`) gets a row for each.
- `kind` (`ex-ante` or `ad-hoc`, from the parent page), `year`, `title`, `filename`, `url`, `size` and `path`.
- `file_kind`: `result`, `objections_ruling`, `correction`, `recommendations_confirmation`, `annex`, `kio_resolution`, `not_controlled` or `other`, from the title and filename, then the opening of the text.
- `text_status`: `text`, `ocr` or `empty`.
- The header fields `authority`, `contract_type`, `subject`, `procedure_type`, `procedure_start`, `notice` (as written), `notice_ids` (normalised), `value` and `result_date`, then `outcome` (`breach` or `no_breach`), `breach_statement`, `breached_articles` (like `art. 99 ust. 4`) and `law` (`2004` or `2019`).

At the end of the crawl, writes one row per control to `controls.json` and `controls.csv`, with the columns of `oecd-poland/analysis/uzp_manifest.csv`, plus `file_kinds`, `law`, `breached_articles` and `breach_statement`. In the CSV, multiple values are joined with `; `. Header fields come from the control's result files first. `outcome` is `not_controlled` if a file says so, `breach` if any result (one per part, for a control split into parts) finds a breach, and otherwise the first outcome found.

Notice numbers are normalised as `YYYY/BZP NNNNNNNN/VV`, `YYYY/S NNN-NNNNNN`, `NNNNNN-N-YYYY` (pre-2021 BZP) and `N-YYYY` (TED eForms, no leading zeros).

Breached articles are read from the breach statement, which opens with *stwierdza naruszenie*, *stwierdzono naruszenie* or *informuje o stwierdzonym naruszeniu* and closes at *UZASADNIENIE* (or, in objections rulings, *Pismem z dnia* or *Zamawiający wniósł*). Articles 596–620 (the control provisions), articles of other statutes and regulations, and articles cited as references (*o którym mowa w*) are excluded, and ranges (`ust. 4-6`) are expanded.

Spider arguments:

- `-a ocr=true`: OCR the PDFs that have no usable text layer, with `tesseract -l pol` on 200 dpi page renders. [Tesseract](https://tesseract-ocr.github.io/) and its Polish language data must be installed (on macOS, `brew install tesseract tesseract-lang`). OCR text is cached under `.scrapy/data/poland_uzp_wyniki_kontroli/_ocrcache/`, so re-runs don't repeat it.

PDFs are cached under `.scrapy/data/poland_uzp_wyniki_kontroli/_httpcache/`. The parent and year pages aren't cached, so new files are found on re-runs.

Caveats:

- About 15% of PDFs are scans, and a few more have a text layer in which every word with a Polish letter is missing (their fonts lack a Unicode map). Both are `text_status=empty` without `-a ocr=true`, and their fields are empty.
- For some ad-hoc controls, only the objections ruling is published, so the header fields are empty.
- `outcome` is that of the result, not of any later objections ruling. If the objections are upheld (*uwzględnienie zastrzeżeń*), read the ruling.
- The parsing is regular expressions over flattened text. OCR errors and unusual layouts can leave fields empty or wrong. A few breaches have no `breached_articles`, because the statement opens unusually (*dopuścił się naruszenia*) or its list is missing from the text layer.

#### `poland_cpv`

Joins KIO rulings and UZP findings to their procurement CPV codes. For each record produced by `poland_kio_orzeczenia` and `poland_uzp_kontrole`, downloads the associated PDF, extracts the first 20 pages with PyMuPDF, regexes out BZP / TED notice numbers, and looks each up:

- BZP via `mo-board/api/v1/Board/Search?NoticeNumber=…` (Polish procurements).
- TED via `POST api.ted.europa.eu/v3/notices/search` (above-EU procurements).

Writes one JSONL row per (source, record_id, notice) under `data/poland_cpv/<crawl>/joined.json`, with `notice_kind` set to `BZP` or `TED` so you can tell which source resolved the CPV. CPV codes are normalised to the 8-digit base form (no trailing check digit) so BZP- and TED-resolved codes are directly comparable.

PDF and lookup responses are cached under `.scrapy/data/poland_cpv/_httpcache/` (Scrapy resolves a relative `HTTPCACHE_DIR` against the project's `.scrapy` directory), so re-runs after regex / extraction tweaks don't re-hit the servers.

Spider arguments:

- `-a articles=99.1,99.4,433,436`: only process KIO rulings that cite these articles. `N.U` matches art. N ust. U (any pkt); a bare `N` matches art. N (any ust., any pkt).
- `-a kio_crawl=…` / `-a uzp_crawl=…`: read a specific `crawl_directory` instead of the latest.

PyMuPDF is AGPL-3.0 licensed (the only AGPL dep in this stack). Combining this repo with PyMuPDF means the combined work effectively has to comply with AGPL terms (source disclosure including for network-deployed modified versions). Chosen here for re-processing speed — PyMuPDF extracts the first 20 pages in ~80 ms vs ~3 s for pdfminer, which matters once the PDFs are cached and we're iterating on the regex / extraction logic.

Caveats:

- Many KIO rulings are procedural ("umorzenie postępowania") and don't cite the notice in the PDF text at all; the spider records these with `pdf_status=no_notice`.
- Some KIO/UZP PDFs are scanned images — text extraction returns empty, surfaced as `pdf_status=empty`. OCR is out of scope.
- TED's current Search API does not reliably index pre-migration (~pre-2019) notices, so older TED references show up as `lookup_hit=false`. About half our UZP TED references are in that older range.
- UZP findings are partly anonymised and the notice number may be redacted in the published "Informacja o wyniku kontroli" PDF.

#### Operational notes

- `orzeczenia.uzp.gov.pl` has been observed to rate-limit large bursts (one run with `CONCURRENT_REQUESTS=32` against the full 34k ID range slowed to ~1 req/sec mid-crawl). If you hit it, throttle with `-s DOWNLOAD_DELAY=0.1 -s CONCURRENT_REQUESTS=8`.
- A full `poland_cpv` run is the slowest piece — PDF parsing in the Scrapy event loop is sequential, so ~10k records take several hours. The HTTP cache means re-runs after that are fast.

### `polandpzp` command

Aggregates PZP-article distributions from the latest `poland_kio_orzeczenia` and `poland_uzp_kontrole` crawls under `FILES_STORE`. Writes `pzp_article_counts.csv`, `pzp_subclause_counts.csv` and `uzp_category_breakdown.csv` to the cwd or `--output-dir`. When a `poland_cpv` crawl is also present, additionally writes `cpv_counts.csv` and `cpv_division_by_dimension.csv` (cross-tab of CPV division × OPZ-quality dimension with three "simple-subset" filtered rows for divs 48/72/33).

```
scrapy polandpzp                                # latest crawls → CSVs in cwd
scrapy polandpzp --output-dir ./out
scrapy polandpzp --kio-crawl 20260603_142206    # specific crawl dirs (also --uzp-crawl, --cpv-crawl)
```
