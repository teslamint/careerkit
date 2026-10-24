# 1. Collect 직행 postings only through the browser extension

## Status

Accepted

## Context

직행 (zighang.com) aggregates job postings and publishes some of its own.
Its robots.txt files restrict automated access:

- api.zighang.com/robots.txt has `Disallow: /` for every user agent.
- zighang.com/robots.txt disallows `/api/`.

The other platforms in careerkit use a search adapter, an HTTP extractor, and a
status probe. Each of these sends requests from careerkit itself. For 직행, each
would read pages or API paths that the site disallows.

A 직행 detail page is a Next.js page. The posting data is in the page HTML, in
React Server Component chunks (`self.__next_f.push` scripts). The user's browser
already loads this HTML when the user opens the posting.

Many 직행 postings are copies of postings on other platforms. The posting data
carries the original posting URL (`redirectUrl` and the dedup apply options).

## Decision

careerkit sends no HTTP requests to zighang.com or api.zighang.com.

- The browser extension reads the posting from the detail page that the user has
  open. The badge collects only the RSC chunk scripts and sends them with the
  collect request. The extension holds a host permission for zighang.com so that
  the side panel can read the tab URL. A host permission sends no request. The
  code enforces the zero-request rule: nothing in the extension calls `fetch`,
  and the native host never fetches 직행.
- The native host checks the captured page: the size cap (in UTF-8 bytes) and the
  parse. The parser reads only complete posting objects whose id equals the URL id.
  It rejects the page when no such object exists, when two of them differ, or when
  a field it reads holds an unresolved RSC reference (a string that starts with
  `$`).
- There is no 직행 search adapter, sitemap crawl, API client, or posting-status probe.
  Maintenance skips 직행 records in the closed-posting check.
- When the original posting URL is on a supported platform (Wanted, Remember,
  GroupBy, Saramin), the host collects the original URL instead. The host
  rebuilds that URL from an allowlist of exact hosts and anchored paths. It never
  passes the raw redirect URL on. When the posting marks a current apply option,
  only that option counts. Otherwise, the host records the posting under the 직행
  key.
- When the posting body has no text (for example, image-only content), the JD
  uses 직행's summary text. The JD source line then reads "직행 (사이트 요약)".

Rejected alternatives:

- Fetch the detail page or the API from careerkit. Rejected because robots.txt
  disallows it.
- Crawl the sitemap to find postings. Rejected for the same reason, and because
  search is not needed when the user picks the posting.

## Consequences

- 직행 postings are collected only one at a time, when the user opens them. The
  auto pipeline cannot find or screen them.
- careerkit cannot detect when a 직행 posting closes.
- A client-side navigation inside 직행 keeps the scripts of the first page. The
  badge then asks the user to reload the page.
- The side panel cannot read the page, so its collect button fails for 직행 URLs.
  Its rescreen button works on records that already exist.
- After an origin redirect, the record has the original URL. A lookup by the
  직행 URL does not find it. The badge shows the collect button again, and a second
  collect returns the existing record as a duplicate. The poll after the first
  collect also cannot find the record; the completion push still reaches the tab.
- A JD built from the summary text is a 직행 summary, not the posting body. It can
  omit requirements. Screening of such a JD needs this caveat in mind.
- A change to 직행's page structure breaks parsing. The host then returns an error
  and records nothing.
