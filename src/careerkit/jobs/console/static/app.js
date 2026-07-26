const form = document.querySelector("#search-form");
const results = document.querySelector("#results");
const status = document.querySelector("#status");
const resultsHeading = document.querySelector("#results-heading");
const detailHeading = document.querySelector("#detail-heading");
const detailMeta = document.querySelector("#detail-meta");
const jdContent = document.querySelector("#jd-content");
const screeningContent = document.querySelector("#screening-content");
const backButton = document.querySelector("#back-to-results");
const refreshButton = document.querySelector("#refresh-index");
const pagination = document.querySelector("#pagination");
const pageInfo = document.querySelector("#page-info");
const pagePrev = document.querySelector("#page-prev");
const pageNext = document.querySelector("#page-next");
const PAGE_SIZE = 50;
let currentOffset = 0;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  currentOffset = 0;
  await runSearch(false);
});

refreshButton.addEventListener("click", async () => runSearch(true));

async function runSearch(refresh) {
  const parameters = new URLSearchParams(new FormData(form));
  for (const [key, value] of [...parameters.entries()]) {
    if (!value) parameters.delete(key);
  }
  if (refresh) {
    parameters.set("refresh", "1");
    currentOffset = 0;
  }
  parameters.set("limit", PAGE_SIZE);
  parameters.set("offset", currentOffset);
  status.textContent = refresh ? "인덱스를 갱신하는 중…" : "검색 중…";
  results.replaceChildren();
  try {
    const response = await fetch(`/api/jobs?${parameters.toString()}`);
    if (!response.ok) throw new Error(await errorMessage(response, "검색 요청에 실패했습니다."));
    const payload = await response.json();
    if (payload.total === 0) {
      const jobIdValue = parameters.get("job_id");
      const emptyDiv = document.createElement("li");
      emptyDiv.className = "empty-state";
      emptyDiv.textContent = jobIdValue
        ? `공고 ID ${jobIdValue}에 해당하는 레코드가 없습니다`
        : "조건에 맞는 레코드가 없습니다";
      results.append(emptyDiv);
    } else {
      for (const item of payload.items) results.append(resultItem(item));
    }
    updatePagination(payload.total);
    status.textContent = `${payload.refreshed ? "인덱스를 갱신했습니다. " : ""}${payload.total}건을 찾았습니다.`;
    resultsHeading.focus();
  } catch (error) {
    status.textContent = error.message;
    updatePagination(0);
  }
}

function updatePagination(total) {
  if (total <= PAGE_SIZE) {
    pagination.classList.add("hidden");
    return;
  }
  pagination.classList.remove("hidden");
  const currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  pageInfo.textContent = `${currentPage} / ${totalPages}`;
  pagePrev.disabled = currentOffset === 0;
  pageNext.disabled = currentOffset + PAGE_SIZE >= total;
}

pagePrev.addEventListener("click", () => {
  currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
  runSearch(false);
});
pageNext.addEventListener("click", () => {
  currentOffset += PAGE_SIZE;
  runSearch(false);
});

const VERDICT_LABELS = {recommended: "추천", hold: "보류", not_recommended: "비추천"};
const STATUS_LABELS = {pending: "대기", applied: "지원", interview: "면접", offer: "오퍼", rejected: "탈락"};

function resultItem(item) {
  const row = document.createElement("li");
  const card = document.createElement("button");
  card.type = "button";
  card.className = "result-card";
  card.dataset.verdict = item.screening_verdict ?? "null";
  card.addEventListener("click", () => showDetail(item.platform, item.job_id));

  const topRow = document.createElement("div");
  topRow.className = "card-top-row";
  const platformTag = document.createElement("span");
  platformTag.className = "platform-tag";
  platformTag.textContent = item.platform.toUpperCase();
  const jobId = document.createElement("span");
  jobId.className = "job-id";
  jobId.textContent = `#${item.job_id}`;
  topRow.append(platformTag, jobId);

  const title = document.createElement("div");
  title.className = "card-title";
  const company = document.createElement("span");
  company.className = "card-company";
  company.textContent = item.company;
  const position = document.createElement("span");
  position.className = "card-position";
  position.textContent = item.position;
  title.append(company, position);

  const bottomRow = document.createElement("div");
  bottomRow.className = "card-bottom-row";
  const verdictBadge = document.createElement("span");
  const verdictKey = item.screening_verdict ?? "null";
  verdictBadge.className = `badge badge-verdict-${verdictKey}`;
  verdictBadge.textContent = VERDICT_LABELS[item.screening_verdict] ?? "미생성";
  const statusBadge = document.createElement("span");
  statusBadge.className = "badge badge-status";
  statusBadge.textContent = STATUS_LABELS[item.application_status] ?? item.application_status;
  bottomRow.append(verdictBadge, statusBadge);
  if (item.posting_status === "closed") {
    const closed = document.createElement("span");
    closed.className = "posting-closed";
    closed.textContent = "마감";
    bottomRow.append(closed);
  }

  card.append(topRow, title, bottomRow);
  row.append(card);
  return row;
}

async function showDetail(platform, jobId) {
  status.textContent = "상세를 불러오는 중…";
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(platform)}/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error(await errorMessage(response, "상세 요청에 실패했습니다."));
    const detail = await response.json();

    detailMeta.replaceChildren();
    const metaTitle = document.createElement("div");
    metaTitle.textContent = `[${detail.platform}] ${detail.job_id} · ${detail.company} · ${detail.position}`;
    detailMeta.append(metaTitle);

    const verdictKey = detail.screening_verdict ?? "null";
    const verdictBadge = document.createElement("span");
    const isVerdictCapped = detail.verdict_capped === true && detail.screening_verdict === "hold";
    verdictBadge.className = `badge badge-verdict-${verdictKey}`;
    if (isVerdictCapped) verdictBadge.classList.add("badge-capped");
    verdictBadge.textContent = isVerdictCapped
      ? "보류 (대기)"
      : (VERDICT_LABELS[detail.screening_verdict] ?? "미생성");
    detailMeta.append(verdictBadge);

    if (detail.screening_provider) {
      const providerSpan = document.createElement("span");
      providerSpan.className = "detail-provider";
      providerSpan.textContent = `provider: ${detail.screening_provider}`;
      detailMeta.append(providerSpan);
    }

    jdContent.textContent = detail.jd_markdown;

    if (detail.has_screening) {
      screeningContent.textContent = detail.screening_markdown;
      screeningContent.className = "";
    } else {
      screeningContent.textContent = "스크리닝 결과가 아직 없습니다.";
      screeningContent.className = "screening-absent";
    }

    status.textContent = "상세를 열었습니다.";
    detailHeading.focus();
  } catch (error) {
    status.textContent = error.message;
  }
}

async function errorMessage(response, fallback) {
  try {
    const payload = await response.json();
    return payload.error ?? fallback;
  } catch {
    return fallback;
  }
}

backButton.addEventListener("click", () => resultsHeading.focus());

const themeToggle = document.querySelector("#theme-toggle");
themeToggle.addEventListener("click", () => {
  const current = document.documentElement.dataset.theme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("jd-console-theme", next);
  themeToggle.textContent = next === "dark" ? "☼" : "◐";
});
const effectiveTheme = document.documentElement.dataset.theme ||
  (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
themeToggle.textContent = effectiveTheme === "dark" ? "☼" : "◐";
