# Jotso company extraction

Verified: 2026-10-07. Base URL: https://jotso.net.

Use this source when requested, or when existing sources lack headcount or average salary.
The installed `career-jobs company fetch` command does not support `--platform jotso`.
Use public HTTP requests, then the packaged `company apply` writer.

## Observed routes

| Purpose | Request | Result |
|---|---|---|
| Search UI | `GET /search` | Company-name input |
| Company search | `GET /api/search?q={URL-encoded-name}` | JSON with `results`, `industries`, and `regions` |
| Company detail | `GET /company/{bizNo}` | HTML with rendered metrics, JSON-LD, and embedded chart data |
| Metric definitions | `GET /about-metrics` | Source definitions and estimation limits |

Anonymous browser searches and cookie-free HTTP replay succeeded during verification.
No company-detail JSON endpoint was observed. Do not invent `/api/company/{id}`.
Next.js requests containing `?_rsc=...` are rendering traffic, not a stable company API.
Search pagination, rate limits, and a complete result-count contract remain unverified.
Do not add guessed `page`, `limit`, or cursor parameters.

## 1. Search and identify the legal entity

The agent runs these commands from the workspace root.
Store temporary captures under an ignored subdirectory of `private/company_info/`.

```bash
capture_dir='private/company_info/.jotso-capture'
mkdir -p "$capture_dir"
company_name='REPLACE_WITH_COMPANY_NAME'
curl --fail --silent --show-error --max-time 30 --get \
  'https://jotso.net/api/search' --data-urlencode "q=$company_name" \
  -o "$capture_dir/search.json"
jq '.results[] | {bizNo,bizName,industry,region,members,closed,mergedInto,matchedAlias,matchedAliasType}' \
  "$capture_dir/search.json"
```

`results` contains company candidates. `industries` and `regions` are separate result groups.
Observed company fields include `bizNo`, `bizName`, `industry`, `region`, `members`, `closed`, `mergedInto`, `matchedAlias`, `matchedAliasType`, and `grade`.
The samples had null alias and merger fields; their non-null structures remain unverified.

Treat `bizNo` as an opaque site identifier. Preserve its string value, including leading zeros.
Do not interpret it as a legal business-registration number.
Match the legal name, industry, and location against the intended company before selecting a candidate.
A matching brand name alone is insufficient. Never automatically select the first result.
Use known legal names or aliases for a narrower search when results are ambiguous or empty.
A failed request is not an empty result. Record unavailable access separately.
Preserve closure or merger indications for verification; they do not prove legal dissolution.

## 2. Read the detail page

```bash
company_id='REPLACE_WITH_SELECTED_BIZNO'
curl --fail --silent --show-error --max-time 30 \
  "https://jotso.net/company/$company_id" -o "$capture_dir/detail.html"
```

Read the target page's heading, industry, address, headcount, salary label, and reference period.
Do not extract metrics from related-company cards, rankings, or recommendations elsewhere on the page.

The HTML contains `script[type="application/ld+json"]` with an `Organization` in `@graph`.
Observed fields include `name`, `description`, `address`, `numberOfEmployees`, and `url`.
The organization's `url` points to Jotso; do not store it as the company's own homepage.
Use the heading and detail metrics to verify the JSON-LD entity.

For precise chart values, parse JSON arguments from `self.__next_f.push(...)` without executing JavaScript.
Join the string payloads from entries whose first element is `1`.
Locate the chart properties containing `ts`; decode the object with a JSON parser.
Do not assume the whole React Flight stream is JSON or rely on fixed component IDs.
If its shape changes or several chart objects match, use the visible page instead of guessing.

| Observed property | Meaning | Unit or interpretation |
|---|---|---|
| `ts.ym[i]` | Reference month | `YYYY-MM` |
| `ts.cnt[i]` | National Pension subscribers | People; not necessarily total employees |
| `ts.sal[i]` | Estimated annual salary | KRW per year; divide by 10,000 for 만원 |
| `ts.hire[i]` | Monthly pension acquisitions | People; proxy for entries |
| `ts.loss[i]` | Monthly pension losses | People; proxy for exits |
| `dartSalary[].year` | Disclosure year | Keep separate from pension reference month |
| `dartSalary[].value` | Disclosed average annual salary | KRW per year; divide by 10,000 for 만원 |

Require equal lengths for all five `ts` arrays before joining them by index.
Choose the latest valid reference month, not an assumed array position.
Cross-check its headcount and rounded salary against the displayed values.
`dartSalary` can be null. A valid disclosure value and `ts.sal` can differ for the same company.
Preserve their labels and periods. Never substitute the chart estimate for a displayed disclosure salary.

Only calculate twelve-month entry/exit totals from twelve consecutive, complete reference months.
Label these totals as National Pension acquisitions/losses.
Missing, null, or invalid values remain unknown. Do not turn them into zero or carry forward an older salary silently.
Treat zero salary as unavailable unless the page explicitly establishes its meaning.
A zero headcount requires its reference month and status context.

## 3. Preserve source meaning

Store `직원수` with `국민연금 가입자` and its reference month.
Store salary as `평균 연봉 (국민연금 추정)` or `평균 연봉 (DART 공시)` with the applicable period.
Preserve the displayed salary in 만원 or clearly label a rounded conversion from raw KRW.

The site's pension estimate uses insurance assessments. It is not an offered salary or a seniority-specific salary.
The [metric definitions](https://jotso.net/about-metrics) describe contribution ceilings and excluded compensation as estimation limits.
Do not hardcode the page's contribution rate or ceiling into a new calculation.

The detail footer and metric guide used different time-window descriptions for turnover during verification.
Do not copy turnover into a resignation-rate field or use it for automatic risk flags.
Keep any retained turnover value with its exact label and source period, noting the discrepancy.
Exclude the site's subjective scores, slogans, and inferred business judgments from factual company fields.
The earliest pension record is not an incorporation date.

Jotso is a supplementary source. Fill missing fields after confirming the company identity.
Retain existing values when their period or measurement differs; attach a separate sourced observation for comparison.
Do not replace a disclosed salary with a pension estimate to improve field completeness.

## 4. Apply and verify the candidate

Prepare a candidate Markdown file under the capture directory using the parent skill's company template.
Include the detail URL, extraction date, each metric's period, and its original source label.
Preserve existing source information when supplementing a company record.

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company apply \
  --company-name "$company_name" --input "$capture_dir/candidate.md"
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file '{company_slug}.md'
```

Read the resulting file to confirm identity, units, labels, and source periods survived the merge.
Validation does not establish company identity or estimation accuracy. Do not invent fields to clear missing-field warnings.

## Browser fallback and discovery limits

Use the active product browser when HTTP access fails or the rendered labels need verification.
In T3, call `preview_status`, then `preview_open` if necessary.
Open `/search`, enter the company name, select the verified entity, and inspect the detail page.
The salary disclosure and the `인원`, `연봉`, and entry/exit tabs expose additional context.
Do not replay authentication, watch, feed-event, or analytics requests as company-extraction endpoints.

The verification used T3 fetch observations, two search responses, and two salary-detail cases.
The browser exposed no CDP attachment address. The browser-to-api filter/infer pipeline consumed adapted observations, not a browser-trace capture.
Its inferred schema is sample-based, not an official API contract.
Keep raw captures and generated reports private. Re-observe requests if the response shape changes.
