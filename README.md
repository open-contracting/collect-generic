# Collect Generic

## Polish procurement spiders

### `poland`

Per-procedure file collector for `ezamowienia.gov.pl`. For each tender, writes:

- `tender.json`: the `Search/GetTender` response.
- `documents.json`: the `Search/GetTenderDocuments` response, which lists every document version, including deleted ones, with `createDate`, `publishedDate`, `deleteDate` and `deleteReason`.
- `notices.json`: the tender's contract notice and notice changes, found by listing its organization's notices in `Board/Search` and keeping those with its `tenderId`.
- `notices/<number>.{html,json}`: each notice's `Board/GetNoticeHtmlBody`, raw and parsed.
- `attachments/<documentId>__<filename>`: each document that still has a file, from `Tender/DownloadDocument/{tenderId}/{documentId}`.

#### Entry modes

- By default, walks `Search/SearchTenders`, which only indexes procedures in state `Initiated`.
- `-a from_date=2025-03-01 -a until_date=2025-03-31`: walks contract notices in `Board/Search` by publication date (both days included), which reaches closed procedures too. `from_date` defaults to 2021-01-01, when e-Zamówienia opened.

Both write under `data/poland/<crawl_directory>/<tenderId>/`, and skip tenders whose `tender.json` is already on disk.

- `-a notice_numbers=<csv>`: collects the tender documents of UZP controls, for `oecd-poland/analysis/poc/tender_document_requests.csv`. The CSV needs `control_id` and `notice_ids` (`; `-separated BZP, OJ S or eForms notice numbers, normalised as in `poland_uzp_wyniki_kontroli`). Optional `cn_split`, `art124_split` and `missing_identifier` columns set each control's priority group (`cn_test`, `art124_test`, `dev`, `art124_train`, `cn_aux`, assigned in that order), and `-a priority=cn_test,art124_test` selects groups. `-a sample=N` takes the first N controls in priority order.

#### Notice resolution

In the `notice_numbers` mode, each notice is resolved (in `generic_scrapy/poland/ezamowienia.py` and `ted.py`):

- BZP notices via `Board/Search?NoticeNumber=…`. If the procedure isn't run on e-Zamówienia (`isProceedingOnPlatform: false`), the documents URL is read from the notice's *Adres strony internetowej prowadzonego postępowania*.
- TED notices via the TED Search API: the documents URL (BT-15, or else the restricted-documents or submission URL), and the procedure's other notices (by `procedure-identifier`, for eForms, keeping those with the same reference number or title, as some buyers reuse a procedure identifier; for pre-eForms notices, the corrigenda that cite the notice's OJ S number). The bid deadline is the notice's (for pre-eForms notices, the deadline for tenders or requests to participate), moved by each later eForms notice published before the deadline, or else set by the latest corrigendum that sets one. The reference number is the latest contract notice's, as later notices can correct it. Pre-eForms notices have the documents URL only in the notice XML (`URL_DOCUMENT`), and the reference number in the title (*numer postępowania*) or else `REFERENCE_NUMBER`.
- e-Zamówienia tender IDs (`ocds-148610-…`) and documents URLs (`https://…`) as is, for a procedure that its notices don't lead to: for example, a closed tender whose notices link only to e-Zamówienia's home page (the buyer's website often gives the ID), or a notice missing from Board/Search.

A documents URL on `ezamowienia.gov.pl` gives the tender ID. The files go under `data/poland_tender_documents/<control_id>/<tenderId>/` (set `-a output=…` to use another directory under `data/`, like `poland_kio_tender_documents`).

#### Platform adapters

Platform adapters are opt-in, with `-a platforms=marketplanet,logintrade,eb2b,smartpzp`. Check a platform's terms of use and robots.txt before collecting from it. Open Nexus prohibits automated downloading from `platformazakupowa.pl` and has reserved its database against text and data mining (art. 8a of the database protection act), so there is no adapter for it.

Each adapter is a mixin in `generic_scrapy/poland/<platform>.py`. On every platform, the spider downloads key documents first, and skips annexes and other files over 100 MB. A response whose body is an HTML page (e.g. *Podany plik nie został znaleziony*) is recorded as an error and not saved, unless the file is named `.htm` or `.html`.

Controls whose documents are on another platform, or on one of these without it in `-a platforms`, are `not_found` with `source` set to `platform:<host>`. Most documents URLs are the buyer's page on the platform, not the procedure's, and platforms can stop listing a procedure, or keep its files from the public.

##### Marketplanet

With `-a platforms=marketplanet`, a documents URL on a Marketplanet platform (`*.ezamawiajacy.pl`, `platformazakupowa.plk-sa.pl`, `zamowienia.*.lasy.gov.pl` or `zamowienia.um.warszawa.pl`) is either the procedure's page or the platform's home page, in which case the procedure is found by searching the notice lists linked from the home page (current, during and expired) for the notice's reference number (*Numer referencyjny*, or TED's `internal-identifier-proc`), and otherwise for its title, accepting only one procedure that started within 14 days of the notice. The spider saves the procedure page, its folders (like *Informacje o wynikach*) and every file in their files tables under `<control_id>/marketplanet-<id>/`, and takes the bid deadline from the page. A file's link expires a while after the page view, so the spider re-reads the pages and retries files that come back as an HTML page. Downloads aren't cached, for the same reason.

A file's `state` is the files table's heading (e.g. *Pierwotne dokumenty zamówienia* or *Zmiana dokumentów zamówienia*), and replaced files aren't marked as superseded.

##### Logintrade

With `-a platforms=logintrade`, a documents URL on `*.logintrade.net` is either the procedure's page (`zapytania_email,<id>,<hash>.html`) or the buyer's page, in which case the procedure is found by searching the buyer's statutory procedures (`/rejestracja/ustawowe.html`) for the reference number. The spider saves the procedure page and its attachments under `<control_id>/logintrade-<id>/`, and takes the bid deadline from the page.

Some buyers require a login for attachments, and some for every page, including the search (HTTP 401).

##### eB2B

With `-a platforms=eb2b`, a documents URL on `*.eb2b.com.pl`, `ezamowienia.krus.gov.pl` or `ezamowienia.ms.gov.pl` is either the procedure's page (`open-preview-auction.html/<id>`) or the buyer's page, in which case the procedure is found by filtering the buyer's public procedures (`/auction/auction/list`) on the reference number. The spider saves the procedure page and the attachments list (`/auction/auction/list-attachment`, as `attachments.json`), and downloads each attachment, under `<control_id>/eb2b-<id>/`. It takes the bid deadline from the procedure's data.

A file's `state` is the attachment's type (e.g. *Załączniki organizatora*). The spider requests Polish pages, as some procedures' English pages are an error page. Buyers' public lists show only procedures in progress, and a bidder's account adds only its own procedures, so archived procedures need the buyer or the PPO.

##### SmartPZP

With `-a platforms=smartpzp`, a documents URL on `portal.smartpzp.pl` is either the procedure's page (`<buyer>/public/postepowanie?postepowanie=<id>`) or the buyer's page, in which case the procedure is found by searching the buyer's procedures, finished or not, for the reference number, and opening it from the results. The pages are JSF views, so each control has its own session. The spider saves the procedure page and lists the documents table (*Dokumentacja Postępowania*) page by page, then downloads the documents in archives of up to 20 files with distinct names (or one at a time, as some buyers' archives fail), under `<control_id>/smartpzp-<id>/`. It takes the bid deadline from the page.

#### Output

The spider also saves the procedure's notices published before the bid deadline: each TED notice's Polish PDF, under `<control_id>/ted/<publication number>.pdf`, and, for a procedure run on another platform, its BZP notice's `Board/GetNoticeHtmlBody`, under `<control_id>/notices/`. Notices don't count towards a control's `status`.

The crawl writes, in the output directory:

- `manifest.json`: one JSON line per control, with `control_id`, `group`, `kind`, `notice_ids`, `source` (`ezamowienia`, `platform:<host>` or `unresolved`), `procedure_url`, `tender_ids`, `bid_deadline_original`, `bid_deadline_final`, `status` (`complete`, `partial` or `not_found`), `reason` and `documents`. Each row also has `first_appeal_at` (when the first `appeal` document was published: later documents can reveal the appeal's grounds, so model input should use only earlier ones), `collection` (`crawl`, or `manual` for documents collected by hand) and, if crawled, `crawl` (the crawl directory's name). Each document has `path` (relative to the output directory, or null if there's no file), `url`, `title`, `published_at`, `role` (`swz`, `opz`, `ppu`, `annex`, `swz_change`, `qa`, `appeal`, `notice`, `notice_change` or `other`, from its name; `appeal` covers appeals, calls to join them, the buyer's answers and KIO's rulings), `superseded` (deleted by the buyer), `state`, `after_deadline`, `size` (bytes, from a platform's files table) and `error` (why there's no file; an e-Zamówienia document that links to another site isn't followed, and counts as a missing file).
- `runs/<crawl_directory>.jsonl`: this crawl's rows, appended as each control finishes.
- `coverage.json`: counts by priority group and status, counts by source, the controls that aren't `complete`, and the controls skipped for having no notice number.

When the crawl closes, its rows are merged into `manifest.json`: a row replaces the existing row for its control unless that row has a better status (or, with the same status, more files), or was collected manually. Delete a manual row to re-crawl its control.

#### Running

The spider collects 8 controls at a time, in priority order, and a control waiting out eB2B's download limit lets another start. To stop a crawl cleanly, send SIGUSR1 to the Scrapy process (not `uv`), like `kill -USR1 $(pgrep -f 'python.*scrapy crawl poland')`: the controls in progress finish, the others are skipped, and the crawl writes `coverage.json`. A re-run re-collects every control, but skips platform files already on disk.

Attachments and notice bodies are cached under `.scrapy/data/poland/_httpcache/`. API responses aren't cached, so re-runs see new documents.

#### Caveats

- e-Zamówienia deletes a procedure's files 3 months after it moves to *Zawarta umowa*, *Unieważnione*, *Anulowane* or *Rozstrzygnięte* (§8(12) of the platform's terms of use). Their documents stay in `documents.json` with state `DeletedAutomatically`, but the download returns HTTP 400 (*Brak załącznika dla wskazanego dokumentu*), so the spider doesn't request them. Files of procedures still in state `Initiated` are kept, however old.
- e-Zamówienia's tender search lists only tenders in progress. A TED notice that links only to e-Zamówienia's home page can't be matched to a closed tender, so the control is `not_found` with `source` set to `platform:ezamowienia.gov.pl`.
- `ted.europa.eu` can answer with an AWS WAF challenge (HTTP 202, empty body) instead of the notice XML, and the TED Search API with HTTP 429. Such controls are `unresolved`, with that reason. Re-run later.
- `Board/Search` returns at most 10 results per page, and can time out (the spider retries). The date mode makes about 100 requests per day of notices.
- `bid_deadline_final` comes from the e-Zamówienia tender's valid submission term or the platform's procedure page, when there is one, and otherwise from the TED notices. A buyer can extend the deadline without a TED notice, so if a document published after the TED deadline (other than an opening, award or appeal document) shows that it was extended, `bid_deadline_final` is empty.
- Follow-up notices (award, agreement, performance) aren't collected. See [#40](https://github.com/open-contracting/collect-generic/issues/40).

### `poland_kio_orzeczenia`

Walks `/Home/Details/<id>` on `orzeczenia.uzp.gov.pl` and writes one row per ruling (both KIO appeals and KIO/KD control opinions). Discovers the upper ID bound by binary search at startup; pass `-a max_id=N` to skip the probe, and `-a min_id=N` to stop at an ID (e.g. the previous crawl's highest, to collect only new rulings). Keeps only rulings from 2023 onward by default (compared by year); pass `-a from_date=2016-01-01` to go further back.

#### Caveats

- The `articles` field comes from the `Kluczowe przepisy ustawy Pzp` block — an editorial short list maintained by the search engine, not an exhaustive extraction of every article cited in the ruling's body. Use it for "which articles drive disputes", not "every mention".
- The same KIO archive includes citations under both the current Pzp (2019, in force from 2021) and the prior Pzp 2004. Articles that exist in only one regime can show up with sub-clause combinations that look wrong in the other (e.g., `art. 16 ust. 1` makes sense in Pzp 2004 but not in Pzp 2019, where art. 16 has no `ust.`). The spider preserves these as-is.
- Some older rulings (mostly pre-2015) have neither an `issue_date` in the metadata nor a `/YY` year suffix in the case number; those are dropped because their date cannot be determined.
- `orzeczenia.uzp.gov.pl` has been observed to rate-limit large bursts (one run with `CONCURRENT_REQUESTS=32` against the full 34k ID range slowed to ~1 req/sec mid-crawl). If you hit it, throttle with `-s DOWNLOAD_DELAY=0.1 -s CONCURRENT_REQUESTS=8`.

### `poland_uzp_kontrole`

Walks the 17 violation-category landing pages under `gov.pl/web/uzp/informacje-o-wynikach-…`. Writes one row per finding with title, violation summary (carrying inline PZP article references) and the attached PDF URLs.

#### Caveats

- Scope is "postępowania wszczęte od 28.07.2016" only. The pre-2016 archive is a separate page not crawled by this spider.
- Contracting-authority names in UZP findings are often anonymised, which limits joining findings back to specific named tenders.
- No `from_date` filter — the full corpus is re-fetched on every run (it's small: ~400 records in seconds).

### `poland_uzp_wyniki_kontroli`

Collects every UZP control result PDF published on the year pages of [kontrole uprzednie](https://www.gov.pl/web/uzp/kontrole-uprzednie) (ex-ante, 2023 onward) and [kontrole doraźne](https://www.gov.pl/web/uzp/kontrole-od-2021-roku) (ad-hoc, 2021 onward), discovering the year pages from the parent pages' links. Unlike `poland_uzp_kontrole`, which crawls the anonymised findings by violation category, this crawls the full results per control.

For each file, saves the PDF and its text under `data/poland_uzp_wyniki_kontroli/<crawl_directory>/files/<uuid>.{pdf,txt}`, and writes one JSONL row per file to `files.json` with:

- `control_id`, normalised like `KU_12_25`, `KND_132_21` or `KUD_2_25` (`WKU` becomes `KU`). If a title omits the year, the year page's is used. A file about two controls (`KND_20_22_i_KND_21_22`) gets a row for each.
- `kind` (`ex-ante` or `ad-hoc`, from the parent page), `year`, `title`, `filename`, `url`, `size` and `path`.
- `file_kind`: `result`, `objections_ruling`, `correction`, `recommendations_confirmation`, `annex`, `kio_resolution`, `not_controlled` or `other`, from the title and filename, then the opening of the text.
- `text_status`: `text`, `ocr` or `empty`.
- The header fields `authority`, `contract_type`, `subject`, `procedure_type`, `procedure_start`, `notice` (as written), `notice_ids` (normalised), `value` (as written) and `result_date`; the value's figures, `value_figures` (each amount after *Wartość zamówienia* in the header, in PLN and EUR, with its label); `value_pln` and `value_eur`, the controlled procedure's value (the figure labelled as controlled, like *kontrolowane zamówienie*, or else the first); and `whole_value_pln`, `whole_value_eur` and `whole_value_label`, the first figure labelled as the whole contract or project, like *całkowita wartość zamówienia*, *część zamówienia o wartości* or *wartość całego projektu* (it can be the controlled procedure's value, and a project's value isn't necessarily a contract's); then `outcome` (`breach` or `no_breach`), `breach_statement`, `breached_articles` (like `art. 99 ust. 4`) and `law` (`2004` or `2019`).

At the end of the crawl, writes one row per control to `controls.json` and `controls.csv`, with the columns of `oecd-poland/analysis/uzp_manifest.csv`, plus `value_pln`, `value_eur`, `whole_value_pln`, `whole_value_eur`, `whole_value_label`, `value_figures`, `file_kinds`, `law`, `breached_articles` and `breach_statement`. In the CSV, multiple values are joined with `; `. Header fields come from the control's result files first. `outcome` is `not_controlled` if a file says so, `breach` if any result (one per part, for a control split into parts) finds a breach, and otherwise the first outcome found.

Notice numbers are normalised as `YYYY/BZP NNNNNNNN/VV`, `YYYY/S NNN-NNNNNN`, `NNNNNN-N-YYYY` (pre-2021 BZP) and `N-YYYY` (TED eForms, no leading zeros).

Breached articles are read from the breach statement, which opens with *stwierdza naruszenie*, *stwierdzono naruszenie* or *informuje o stwierdzonym naruszeniu* and closes at *UZASADNIENIE* (or, in objections rulings, *Pismem z dnia* or *Zamawiający wniósł*). Articles 596–620 (the control provisions), articles of other statutes and regulations, and articles cited as references (*o którym mowa w*) are excluded, and ranges (`ust. 4-6`) are expanded.

#### Spider arguments

- `-a ocr=true`: OCR the PDFs that have no usable text layer, with `tesseract -l pol` on 200 dpi page renders. [Tesseract](https://tesseract-ocr.github.io/) and its Polish language data must be installed (on macOS, `brew install tesseract tesseract-lang`). OCR text is cached under `.scrapy/data/poland_uzp_wyniki_kontroli/_ocrcache/`, so re-runs don't repeat it.

PDFs are cached under `.scrapy/data/poland_uzp_wyniki_kontroli/_httpcache/`. The parent and year pages aren't cached, so new files are found on re-runs.

#### Caveats

- About 15% of PDFs are scans, and a few more have a text layer in which every word with a Polish letter is missing (their fonts lack a Unicode map). Both are `text_status=empty` without `-a ocr=true`, and their fields are empty.
- For some ad-hoc controls, only the objections ruling is published, so the header fields are empty.
- `outcome` is that of the result, not of any later objections ruling. If the objections are upheld (*uwzględnienie zastrzeżeń*), read the ruling.
- The parsing is regular expressions over flattened text. OCR errors and unusual layouts can leave fields empty or wrong. A few breaches have no `breached_articles`, because the statement opens unusually (*dopuścił się naruszenia*) or its list is missing from the text layer.
- Value labels are read from the text around each figure, so check `value_figures` before relying on `whole_value_*`. Where the header is laid out in columns, the labels are separated from their figures, and the labels in `value_figures` can be empty or wrong.

### `poland_cpv`

Joins KIO rulings and UZP findings to their procurement CPV codes. For each record produced by `poland_kio_orzeczenia` and `poland_uzp_kontrole`, downloads the associated PDF, extracts the first 20 pages with PyMuPDF, regexes out BZP / TED notice numbers, and looks each up:

- BZP via `mo-board/api/v1/Board/Search?NoticeNumber=…` (Polish procurements).
- TED via `POST api.ted.europa.eu/v3/notices/search` (above-EU procurements).

Writes one JSONL row per (source, record_id, notice) under `data/poland_cpv/<crawl>/joined.json`, with `notice_kind` set to `BZP` or `TED` so you can tell which source resolved the CPV. CPV codes are normalised to the 8-digit base form (no trailing check digit) so BZP- and TED-resolved codes are directly comparable.

PDF and lookup responses are cached under `.scrapy/data/poland_cpv/_httpcache/` (Scrapy resolves a relative `HTTPCACHE_DIR` against the project's `.scrapy` directory), so re-runs after regex / extraction tweaks don't re-hit the servers.

#### Spider arguments

- `-a articles=99.1,99.4,433,436`: only process KIO rulings that cite these articles. `N.U` matches art. N ust. U (any pkt); a bare `N` matches art. N (any ust., any pkt).
- `-a kio_crawl=…` / `-a uzp_crawl=…`: read a specific `crawl_directory` instead of the latest.

PyMuPDF is AGPL-3.0 licensed (the only AGPL dep in this stack). Combining this repo with PyMuPDF means the combined work effectively has to comply with AGPL terms (source disclosure including for network-deployed modified versions). Chosen here for re-processing speed — PyMuPDF extracts the first 20 pages in ~80 ms vs ~3 s for pdfminer, which matters once the PDFs are cached and we're iterating on the regex / extraction logic.

#### Caveats

- Many KIO rulings are procedural ("umorzenie postępowania") and don't cite the notice in the PDF text at all; the spider records these with `pdf_status=no_notice`.
- Some KIO/UZP PDFs are scanned images — text extraction returns empty, surfaced as `pdf_status=empty`. OCR is out of scope.
- TED's current Search API does not reliably index pre-migration (~pre-2019) notices, so older TED references show up as `lookup_hit=false`. About half our UZP TED references are in that older range.
- UZP findings are partly anonymised and the notice number may be redacted in the published "Informacja o wyniku kontroli" PDF.
- A full run is slow: PDF parsing in the Scrapy event loop is sequential, so ~10k records take several hours. The HTTP cache means re-runs after that are fast.

## `polandpzp` command

Aggregates PZP-article distributions from the latest `poland_kio_orzeczenia` and `poland_uzp_kontrole` crawls under `FILES_STORE`. Writes `pzp_article_counts.csv`, `pzp_subclause_counts.csv` and `uzp_category_breakdown.csv` to the cwd or `--output-dir`. When a `poland_cpv` crawl is also present, additionally writes `cpv_counts.csv` and `cpv_division_by_dimension.csv` (cross-tab of CPV division × OPZ-quality dimension with three "simple-subset" filtered rows for divs 48/72/33).

```
scrapy polandpzp                                # latest crawls → CSVs in cwd
scrapy polandpzp --output-dir ./out
scrapy polandpzp --kio-crawl 20260603_142206    # specific crawl dirs (also --uzp-crawl, --cpv-crawl)
```
