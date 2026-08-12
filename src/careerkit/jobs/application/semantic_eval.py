from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
from typing import Callable, Final, Literal, Mapping, Protocol, Sequence, cast
import unicodedata

EvalStatus = Literal['pass', 'fail', 'insufficient_data', 'unavailable']
SemanticLabel = Literal['backend', 'non_backend', 'ambiguous']
DatasetSplit = Literal['calibration', 'holdout']
EvidenceTier = Literal['synthetic', 'private_gold_locked']
PeakRssStatus = Literal['ok', 'unavailable']

QUEUE_SCHEMA: Final = 'semantic-filter-label-queue/v1'
GOLD_SCHEMA: Final = 'semantic-filter-eval/v1'
REPORT_SCHEMA: Final = 'semantic-filter-score-report/v1'
COMPARISON_SCHEMA: Final = 'semantic-filter-comparison-report/v1'
SCORE_CONTRACT_DIGEST: Final = 'semantic-score-contract/v1'
FAMILY_AGGREGATION_CONTRACT_DIGEST: Final = 'semantic-family-aggregation/v1'
CALIBRATION_PERCENT: Final = 20
KEEP_LABELS: Final[frozenset[str]] = frozenset({'backend', 'ambiguous'})
SUPPORTED_LABELS: Final[frozenset[str]] = frozenset({'backend', 'non_backend', 'ambiguous'})
REQUIRED_SLICES: Final[tuple[str, ...]] = (
    'korean',
    'english',
    'mixed',
    'generic_software',
    'ai_ml',
    'data',
    'devops_sre',
    'frontend',
    'qa_product',
    'hardware_embedded',
    'bracket_prefixed',
    'seniority_marked',
)
REQUIRED_LATENCY_KEYS: Final[frozenset[str]] = frozenset(
    {'cold_load_seconds', 'warm_p50_seconds', 'warm_p95_seconds', 'throughput_titles_per_second'}
)
REQUIRED_RESOURCE_KEYS: Final[frozenset[str]] = frozenset({'peak_rss_bytes', 'peak_rss_status'})
REQUIRED_SLICE_KEYS: Final[frozenset[str]] = frozenset(
    {'cases', 'families', 'keep_families', 'reject_families', 'keep_family_miss_rate_upper_bound'}
)
REQUIRED_FAMILY_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {'family_id', 'split', 'has_keep_case', 'missed_keep', 'all_non_backend', 'correct_rejection'}
)
REQUIRED_CASE_SCORE_KEYS: Final[frozenset[str]] = frozenset(
    {
        'case_id',
        'family_id',
        'split',
        'label',
        'slices',
        'quick_filter_outcome',
        'quick_filter_config_digest',
        'score',
    }
)
REQUIRED_SCORE_KEYS: Final[frozenset[str]] = frozenset(
    {'title', 'normalized_title', 'backend_score', 'non_backend_score', 'relative_score', 'reject'}
)
REQUIRED_COMPARISON_KEYS: Final[frozenset[str]] = frozenset(
    {'candidate_gains', 'candidate_losses', 'mcnemar_p_value', 'reason'}
)
REQUIRED_COMPARISON_BINDING_KEYS: Final[frozenset[str]] = frozenset(
    {
        'dataset_digest',
        'split_digest',
        'family_lock_digest',
        'incumbent_report_digest',
        'candidate_report_digest',
        'incumbent_provenance',
        'candidate_provenance',
    }
)
REQUIRED_COMPARISON_REPORT_KEYS: Final[frozenset[str]] = frozenset(
    {
        'schema',
        'status',
        'evidence_tier',
        'authorizes_production_change',
        'lock_validated',
        'provenance',
        'counts',
        'metrics',
        'confidence_intervals',
        'slices',
        'latency',
        'resource_cost',
        'family_results',
        'case_scores',
        'comparison',
        'binding',
    }
)


@dataclass(frozen=True)
class SemanticTitleScore:
    title: str
    normalized_title: str
    backend_score: float
    non_backend_score: float
    relative_score: float
    reject: bool


@dataclass(frozen=True)
class SemanticModelProvenance:
    model_name: str
    model_revision: str
    sentence_transformers_version: str
    anchor_digest: str
    keyword_override_digest: str
    dataset_digest: str
    split_digest: str
    family_lock_digest: str
    git_sha: str
    command: str
    score_contract_digest: str
    family_aggregation_contract_digest: str = FAMILY_AGGREGATION_CONTRACT_DIGEST
    dataset_schema: str = GOLD_SCHEMA
    dataset_version: str = ''


class SemanticScorer(Protocol):
    def prepare(self) -> None: ...
    def score_title(self, title: str) -> SemanticTitleScore: ...
    def provenance(self) -> SemanticModelProvenance: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class SemanticQueueCase:
    case_id: str
    family_id: str
    title: str
    label: SemanticLabel | None
    split: DatasetSplit
    slices: tuple[str, ...]
    quick_filter_outcome: str
    quick_filter_config_digest: str
    normalized_title: str


@dataclass(frozen=True)
class SemanticQueuePayload:
    schema: str
    source_queue_digest: str
    captured_at: str
    cases: tuple[SemanticQueueCase, ...]
    capture_provenance: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticEvalCase:
    case_id: str
    family_id: str
    title: str
    label: SemanticLabel
    split: DatasetSplit
    slices: tuple[str, ...]
    quick_filter_outcome: str
    quick_filter_config_digest: str
    normalized_title: str


@dataclass(frozen=True)
class SemanticFamilyOutcome:
    family_id: str
    split: DatasetSplit
    has_keep_case: bool
    missed_keep: bool
    all_non_backend: bool
    correct_rejection: bool


@dataclass(frozen=True)
class SemanticCaseScore:
    case_id: str
    family_id: str
    split: DatasetSplit
    label: SemanticLabel
    slices: tuple[str, ...]
    quick_filter_outcome: str
    quick_filter_config_digest: str
    score: SemanticTitleScore


@dataclass(frozen=True)
class SemanticEvalDataset:
    schema: str
    dataset_version: str
    cases: tuple[SemanticEvalCase, ...]
    source_queue_digests: tuple[str, ...]
    human_confirmation: Mapping[str, object]
    lock_timestamp: str
    lock_digest: str
    evidence_tier: EvidenceTier
    dataset_digest: str
    split_digest: str
    family_lock_digest: str
    locked: bool
    queue_case_count: int = 0


@dataclass(frozen=True)
class SemanticEvalReport:
    schema: str
    status: EvalStatus
    evidence_tier: EvidenceTier
    authorizes_production_change: bool
    provenance: SemanticModelProvenance
    counts: Mapping[str, int]
    metrics: Mapping[str, float]
    confidence_intervals: Mapping[str, float]
    slices: Mapping[str, Mapping[str, float | int]]
    latency: Mapping[str, float]
    resource_cost: Mapping[str, int | None | str]
    family_results: tuple[SemanticFamilyOutcome, ...]
    case_scores: tuple[SemanticCaseScore, ...]
    lock_validated: bool


@dataclass(frozen=True)
class SemanticComparisonReport:
    schema: str
    status: EvalStatus
    evidence_tier: EvidenceTier
    authorizes_production_change: bool
    provenance: SemanticModelProvenance
    counts: Mapping[str, int]
    metrics: Mapping[str, float]
    confidence_intervals: Mapping[str, float]
    slices: Mapping[str, Mapping[str, float | int]]
    latency: Mapping[str, float]
    resource_cost: Mapping[str, int | None | str]
    family_results: tuple[SemanticFamilyOutcome, ...]
    case_scores: tuple[SemanticCaseScore, ...]
    comparison: Mapping[str, int | float | str]
    lock_validated: bool


def load_queue_payload(payload: Mapping[str, object]) -> SemanticQueuePayload:
    if payload.get('schema') != QUEUE_SCHEMA:
        raise ValueError('unsupported schema')
    cases = tuple(_load_queue_case(item) for item in _require_list(payload, 'cases'))
    _validate_unique_case_ids(cases)
    return SemanticQueuePayload(
        schema=QUEUE_SCHEMA,
        source_queue_digest=_require_non_empty_string(payload, 'source_queue_digest'),
        captured_at=_require_non_empty_string(payload, 'captured_at'),
        cases=cases,
        capture_provenance=_load_capture_provenance(payload.get('capture_provenance')),
    )


class SemanticCaptureSink(Protocol):
    def capture(
        self,
        title: str,
        *,
        quick_filter_outcome: Literal['eligible'],
        quick_filter_config_digest: str,
    ) -> None: ...

    def mark_incomplete(self, error_code: str) -> None: ...

    def record_source_outcome(
        self,
        platform: str,
        *,
        complete: bool,
        stop_reason: str | None,
        pages_fetched: int,
    ) -> None: ...


@dataclass
class SemanticEvalCaptureSink:
    output_path: Path
    allowed_roots: Sequence[Path]
    seed: int
    file_store: object | None = None
    _captured_titles: list[dict[str, str]] = field(default_factory=list)
    _source_outcomes: dict[str, dict[str, object]] = field(default_factory=dict)
    _incomplete_error_code: str | None = None

    @property
    def incomplete_error_code(self) -> str | None:
        return self._incomplete_error_code

    @property
    def source_outcomes(self) -> Mapping[str, Mapping[str, object]]:
        return self._source_outcomes

    def capture(
        self,
        title: str,
        *,
        quick_filter_outcome: Literal['eligible'],
        quick_filter_config_digest: str,
    ) -> None:
        self._captured_titles.append(
            {
                'title': title,
                'quick_filter_outcome': quick_filter_outcome,
                'quick_filter_config_digest': quick_filter_config_digest,
            }
        )

    def mark_incomplete(self, error_code: str) -> None:
        severity = {
            'semantic_unavailable': 1,
            'search_incomplete': 2,
            'platform_failure': 3,
        }
        if self._incomplete_error_code is None or severity.get(error_code, 0) > severity.get(self._incomplete_error_code, 0):
            self._incomplete_error_code = error_code

    def record_source_outcome(
        self,
        platform: str,
        *,
        complete: bool,
        stop_reason: str | None,
        pages_fetched: int,
    ) -> None:
        previous = self._source_outcomes.get(platform)
        if previous is None:
            self._source_outcomes[platform] = {
                'complete': complete,
                'stop_reason': stop_reason,
                'pages_fetched': pages_fetched,
            }
            return
        self._source_outcomes[platform] = {
            'complete': bool(previous['complete']) and complete,
            'stop_reason': stop_reason or cast(str | None, previous['stop_reason']),
            'pages_fetched': cast(int, previous['pages_fetched']) + pages_fetched,
        }

    def build_payload(self, *, captured_at: str | None = None) -> SemanticQueuePayload:
        return build_semantic_capture_queue(
            titles=self._captured_titles,
            source_outcomes=self._source_outcomes,
            seed=self.seed,
            captured_at=captured_at or _utc_now(),
        )

    def publish(self, *, captured_at: str | None = None) -> Path:
        if self._incomplete_error_code is not None:
            raise ValueError(self._incomplete_error_code)
        from careerkit.jobs.adapters.semantic_eval_files import SemanticEvalFileStore

        file_store = self.file_store
        if file_store is None:
            file_store = SemanticEvalFileStore(allowed_roots=self.allowed_roots)
            self.file_store = file_store
        payload = self.build_payload(captured_at=captured_at)
        return cast(SemanticEvalFileStore, file_store).write_new_json(
            self.output_path,
            queue_payload_to_dict(payload),
            purpose='semantic queue',
        )


def build_semantic_capture_queue(
    *,
    titles: Sequence[Mapping[str, object]],
    source_outcomes: Mapping[str, Mapping[str, object]],
    seed: int,
    captured_at: str,
) -> SemanticQueuePayload:
    deduped: dict[str, Mapping[str, object]] = {}
    for item in titles:
        title = _require_non_empty_string(item, 'title')
        normalized_title = normalize_eval_title(title)
        candidate = {
            'title': title,
            'normalized_title': normalized_title,
            'quick_filter_outcome': _require_non_empty_string(item, 'quick_filter_outcome'),
            'quick_filter_config_digest': _require_non_empty_string(item, 'quick_filter_config_digest'),
        }
        existing = deduped.get(normalized_title)
        if existing is None or _capture_title_sort_key(title) < _capture_title_sort_key(cast(str, existing['title'])):
            deduped[normalized_title] = candidate
    ordered_items = sorted(
        deduped.values(),
        key=lambda item: (
            hashlib.sha256(f'{seed}\0{cast(str, item["normalized_title"])}'.encode('utf-8')).hexdigest(),
            cast(str, item['normalized_title']),
        ),
    )
    cases = tuple(
        SemanticQueueCase(
            case_id=_capture_case_id(cast(str, item['normalized_title'])),
            family_id=_capture_family_id(cast(str, item['title'])),
            title=cast(str, item['title']),
            label=None,
            split=_provisional_split(seed, _capture_family_id(cast(str, item['title']))),
            slices=_capture_slices(cast(str, item['title'])),
            quick_filter_outcome=cast(str, item['quick_filter_outcome']),
            quick_filter_config_digest=cast(str, item['quick_filter_config_digest']),
            normalized_title=cast(str, item['normalized_title']),
        )
        for item in ordered_items
    )
    provenance = {
        'seed': seed,
        'platforms': {
            platform: {
                'complete': _require_bool(outcome, 'complete'),
                'stop_reason': _optional_non_empty_string(outcome, 'stop_reason'),
                'pages_fetched': _require_int(outcome, 'pages_fetched'),
            }
            for platform, outcome in sorted(source_outcomes.items())
        },
    }
    material = {
        'captured_at': captured_at,
        'cases': [
            {
                'case_id': case.case_id,
                'family_id': case.family_id,
                'title': case.title,
                'label': case.label,
                'split': case.split,
                'slices': list(case.slices),
                'quick_filter_outcome': case.quick_filter_outcome,
                'quick_filter_config_digest': case.quick_filter_config_digest,
            }
            for case in cases
        ],
        'capture_provenance': provenance,
    }
    source_queue_digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return SemanticQueuePayload(
        schema=QUEUE_SCHEMA,
        source_queue_digest=source_queue_digest,
        captured_at=captured_at,
        cases=cases,
        capture_provenance=provenance,
    )


def queue_payload_to_dict(payload: SemanticQueuePayload) -> dict[str, object]:
    return {
        'schema': payload.schema,
        'source_queue_digest': payload.source_queue_digest,
        'captured_at': payload.captured_at,
        'cases': [
            {
                'case_id': case.case_id,
                'family_id': case.family_id,
                'title': case.title,
                'label': case.label,
                'split': case.split,
                'slices': list(case.slices),
                'quick_filter_outcome': case.quick_filter_outcome,
                'quick_filter_config_digest': case.quick_filter_config_digest,
            }
            for case in payload.cases
        ],
        'capture_provenance': dict(payload.capture_provenance),
    }


def authoritative_split(dataset_version: str, family_id: str) -> DatasetSplit:
    digest = hashlib.sha256(f'{dataset_version}\0{family_id}'.encode('utf-8')).digest()
    bucket = int.from_bytes(digest[:8], 'big') % 100
    return 'calibration' if bucket < CALIBRATION_PERCENT else 'holdout'


def compute_lock_digest(source_queue_digests: Sequence[str], cases: Sequence[SemanticEvalCase]) -> str:
    material = {
        'source_queue_digests': list(source_queue_digests),
        'cases': [
            {
                'normalized_title': case.normalized_title,
                'label': case.label,
                'family_id': case.family_id,
                'split': case.split,
            }
            for case in sorted(cases, key=lambda item: (item.normalized_title, item.case_id))
        ],
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def load_dataset_payload(payload: Mapping[str, object]) -> SemanticEvalDataset:
    if payload.get('schema') != GOLD_SCHEMA:
        raise ValueError('unsupported schema')
    dataset_version = _require_non_empty_string(payload, 'dataset_version')
    source_queue_digests = tuple(_require_string_list(payload, 'source_queue_digests'))
    human_confirmation = _require_confirmation(_require_mapping(payload, 'human_confirmation'))
    cases = _canonicalize_gold_cases(_require_list(payload, 'cases'), dataset_version)
    lock_timestamp = _require_non_empty_string(payload, 'lock_timestamp')
    lock_digest = _require_non_empty_string(payload, 'lock_digest')
    evidence_tier = _parse_evidence_tier(_require_non_empty_string(payload, 'evidence_tier'))
    dataset_digest = _digest_cases(cases, dataset_version)
    split_digest = _digest_pairs([(case.family_id, case.split) for case in cases])
    family_lock_digest = _digest_pairs([(case.family_id, case.normalized_title) for case in cases])
    locked = compute_lock_digest(source_queue_digests, cases) == lock_digest
    if evidence_tier == 'private_gold_locked' and not locked:
        raise ValueError('private gold dataset requires a valid lock digest')
    return SemanticEvalDataset(
        schema=GOLD_SCHEMA,
        dataset_version=dataset_version,
        cases=cases,
        source_queue_digests=source_queue_digests,
        human_confirmation=human_confirmation,
        lock_timestamp=lock_timestamp,
        lock_digest=lock_digest,
        evidence_tier=evidence_tier,
        dataset_digest=dataset_digest,
        split_digest=split_digest,
        family_lock_digest=family_lock_digest,
        locked=locked,
    )


def build_gold_dataset(
    *,
    queue_payloads: Sequence[SemanticQueuePayload | Mapping[str, object]],
    gold_payload: Mapping[str, object],
) -> SemanticEvalDataset:
    queues = [item if isinstance(item, SemanticQueuePayload) else load_queue_payload(item) for item in queue_payloads]
    gold = load_dataset_payload(gold_payload)
    queue_digests = tuple(queue.source_queue_digest for queue in queues)
    if queue_digests != gold.source_queue_digests:
        raise ValueError('ordered source queue digests must match gold manifest')
    captured_titles = {case.normalized_title for queue in queues for case in queue.cases}
    for case in gold.cases:
        if case.normalized_title not in captured_titles:
            raise ValueError('gold case missing queue provenance')
    if compute_lock_digest(queue_digests, gold.cases) != gold.lock_digest:
        raise ValueError('lock digest mismatch')
    return SemanticEvalDataset(
        schema=gold.schema,
        dataset_version=gold.dataset_version,
        cases=gold.cases,
        source_queue_digests=gold.source_queue_digests,
        human_confirmation=gold.human_confirmation,
        lock_timestamp=gold.lock_timestamp,
        lock_digest=gold.lock_digest,
        evidence_tier=gold.evidence_tier,
        dataset_digest=gold.dataset_digest,
        split_digest=gold.split_digest,
        family_lock_digest=gold.family_lock_digest,
        locked=True,
        queue_case_count=len(gold.cases),
    )


def evaluate_dataset(
    dataset: SemanticEvalDataset,
    scorer: SemanticScorer,
    resource_sampler: Callable[[], Mapping[str, object]],
    clock: Callable[[], float],
) -> SemanticEvalReport:
    primary_error: BaseException | None = None
    report: SemanticEvalReport | None = None
    prepare_started = clock()
    prepare_finished = prepare_started
    case_scores: list[SemanticCaseScore] = []
    durations: list[float] = []
    try:
        scorer.prepare()
        prepare_finished = clock()
        provenance = scorer.provenance()
        current_mark = prepare_finished
        for case in dataset.cases:
            score = scorer.score_title(case.title)
            _validate_score(score)
            next_mark = clock()
            durations.append(next_mark - current_mark)
            current_mark = next_mark
            case_scores.append(
                SemanticCaseScore(
                    case_id=case.case_id,
                    family_id=case.family_id,
                    split=case.split,
                    label=case.label,
                    slices=case.slices,
                    quick_filter_outcome=case.quick_filter_outcome,
                    quick_filter_config_digest=case.quick_filter_config_digest,
                    score=score,
                )
            )
        resource_cost = _normalize_resource_sample(cast(Mapping[str, object], resource_sampler()))
        report = _build_report(
            schema=REPORT_SCHEMA,
            dataset=dataset,
            provenance=provenance,
            case_scores=tuple(case_scores),
            durations=durations,
            cold_load_seconds=prepare_finished - prepare_started,
            resource_cost=resource_cost,
        )
    except BaseException as exc:  # noqa: BLE001
        primary_error = exc
    try:
        scorer.close()
    except BaseException as close_error:  # noqa: BLE001
        if primary_error is None:
            raise ValueError('scorer close failed') from close_error
        primary_error.add_note(f'close failure: {close_error}')
    if primary_error is not None:
        raise primary_error
    assert report is not None
    return report


def compare_reports(
    dataset: SemanticEvalDataset,
    incumbent: SemanticEvalReport,
    candidate: SemanticEvalReport,
) -> SemanticComparisonReport:
    _validate_report_contract(dataset, incumbent)
    _validate_report_contract(dataset, candidate)
    incumbent_family = {item.family_id: item for item in incumbent.family_results}
    candidate_family = {item.family_id: item for item in candidate.family_results}
    gains = 0
    losses = 0
    for family_id, candidate_outcome in candidate_family.items():
        incumbent_outcome = incumbent_family[family_id]
        if candidate_outcome.split != 'holdout' or not candidate_outcome.all_non_backend:
            continue
        if candidate_outcome.correct_rejection and not incumbent_outcome.correct_rejection:
            gains += 1
        if incumbent_outcome.correct_rejection and not candidate_outcome.correct_rejection:
            losses += 1
    p_value = _exact_binomial_tail(gains, gains + losses)
    if not candidate.authorizes_production_change:
        status: EvalStatus = 'fail'
        reason = 'candidate_not_authorized'
    elif candidate.status != 'pass':
        status = 'fail'
        reason = 'candidate_failed_safety_gate'
    elif gains > losses and p_value < 0.05:
        status = 'pass'
        reason = 'candidate_improves_correct_rejections'
    else:
        status = 'fail'
        reason = 'candidate_failed_utility_gate'
    return SemanticComparisonReport(
        schema=COMPARISON_SCHEMA,
        status=status,
        evidence_tier=candidate.evidence_tier,
        authorizes_production_change=bool(status == 'pass' and candidate.authorizes_production_change),
        provenance=candidate.provenance,
        counts=candidate.counts,
        metrics=candidate.metrics,
        confidence_intervals=candidate.confidence_intervals,
        slices=candidate.slices,
        latency=candidate.latency,
        resource_cost=candidate.resource_cost,
        family_results=candidate.family_results,
        case_scores=candidate.case_scores,
        comparison={
            'candidate_gains': gains,
            'candidate_losses': losses,
            'mcnemar_p_value': p_value,
            'reason': reason,
        },
        lock_validated=candidate.lock_validated,
    )


def load_eval_report_payload(payload: Mapping[str, object], dataset: SemanticEvalDataset) -> SemanticEvalReport:
    if payload.get('schema') != REPORT_SCHEMA:
        raise ValueError('unsupported schema')
    provenance = _load_provenance(_require_mapping(payload, 'provenance'))
    evidence_tier = _parse_evidence_tier(_require_non_empty_string(payload, 'evidence_tier'))
    case_scores = tuple(_load_case_score(item) for item in _require_list(payload, 'case_scores'))
    _bind_case_scores_to_dataset(case_scores, dataset)
    report = _rebuild_from_case_scores(
        schema=REPORT_SCHEMA,
        dataset=dataset,
        provenance=provenance,
        case_scores=case_scores,
        latency=_coerce_float_mapping(
            _validate_exact_keys(_require_mapping(payload, 'latency'), REQUIRED_LATENCY_KEYS, 'exact latency keys')
        ),
        resource_cost=_parse_resource_cost(
            _validate_exact_keys(_require_mapping(payload, 'resource_cost'), REQUIRED_RESOURCE_KEYS, 'exact resource keys')
        ),
    )
    if evidence_tier != report.evidence_tier:
        raise ValueError('evidence tier mismatch')
    _validate_report_contract(dataset, report)
    if _parse_status(_require_non_empty_string(payload, 'status')) != report.status:
        raise ValueError('status evidence mismatch')
    if tuple(_load_family_result(item) for item in _require_list(payload, 'family_results')) != report.family_results:
        raise ValueError('family evidence mismatch')
    if _coerce_int_mapping(_require_mapping(payload, 'counts')) != dict(report.counts):
        raise ValueError('count evidence mismatch')
    if _coerce_float_mapping(_require_mapping(payload, 'metrics')) != dict(report.metrics):
        raise ValueError('metric evidence mismatch')
    if _coerce_float_mapping(_require_mapping(payload, 'confidence_intervals')) != dict(report.confidence_intervals):
        raise ValueError('confidence evidence mismatch')
    if _load_slices(_require_mapping(payload, 'slices')) != report.slices:
        raise ValueError('slice evidence mismatch')
    return report


def load_comparison_report_payload(
    payload: Mapping[str, object],
    dataset: SemanticEvalDataset,
    incumbent: SemanticEvalReport,
    candidate: SemanticEvalReport,
    *,
    incumbent_report_digest: str,
    candidate_report_digest: str,
) -> SemanticComparisonReport:
    if payload.get('schema') != COMPARISON_SCHEMA:
        raise ValueError('unsupported schema')
    _validate_exact_keys(payload, REQUIRED_COMPARISON_REPORT_KEYS, 'exact comparison report keys')
    report = compare_reports(dataset, incumbent, candidate)
    serialized = _load_comparison(_require_mapping(payload, 'comparison'))
    if serialized != dict(report.comparison):
        raise ValueError('comparison evidence mismatch')
    if _parse_status(_require_non_empty_string(payload, 'status')) != report.status:
        raise ValueError('comparison status mismatch')
    if _coerce_int_mapping(_require_mapping(payload, 'counts')) != dict(report.counts):
        raise ValueError('count evidence mismatch')
    if _coerce_float_mapping(_require_mapping(payload, 'metrics')) != dict(report.metrics):
        raise ValueError('metric evidence mismatch')
    if _coerce_float_mapping(_require_mapping(payload, 'confidence_intervals')) != dict(report.confidence_intervals):
        raise ValueError('confidence evidence mismatch')
    if _load_slices(_require_mapping(payload, 'slices')) != report.slices:
        raise ValueError('slice evidence mismatch')
    if _coerce_float_mapping(_require_mapping(payload, 'latency')) != dict(report.latency):
        raise ValueError('latency evidence mismatch')
    if _parse_resource_cost(_require_mapping(payload, 'resource_cost')) != dict(report.resource_cost):
        raise ValueError('resource cost evidence mismatch')
    if tuple(_load_family_result(item) for item in _require_list(payload, 'family_results')) != report.family_results:
        raise ValueError('family evidence mismatch')
    case_scores = tuple(_load_case_score(item) for item in _require_list(payload, 'case_scores'))
    if case_scores != report.case_scores:
        raise ValueError('case evidence mismatch')
    if _require_bool(payload, 'authorizes_production_change') != report.authorizes_production_change:
        raise ValueError('authorization evidence mismatch')
    if _require_bool(payload, 'lock_validated') != report.lock_validated:
        raise ValueError('lock evidence mismatch')
    if dict(_require_mapping(payload, 'provenance')) != _public_provenance(report.provenance):
        raise ValueError('provenance evidence mismatch')
    binding = _load_comparison_binding(_require_mapping(payload, 'binding'))
    if binding['dataset_digest'] != dataset.dataset_digest:
        raise ValueError('comparison dataset digest mismatch')
    if binding['split_digest'] != dataset.split_digest:
        raise ValueError('comparison split digest mismatch')
    if binding['family_lock_digest'] != dataset.family_lock_digest:
        raise ValueError('comparison family lock digest mismatch')
    if binding['incumbent_report_digest'] != incumbent_report_digest:
        raise ValueError('incumbent report digest mismatch')
    if binding['candidate_report_digest'] != candidate_report_digest:
        raise ValueError('candidate report digest mismatch')
    if binding['incumbent_provenance'] != _public_provenance(incumbent.provenance):
        raise ValueError('incumbent provenance mismatch')
    if binding['candidate_provenance'] != _public_provenance(candidate.provenance):
        raise ValueError('candidate provenance mismatch')
    return report


def aggregate_report_view(report: SemanticEvalReport | SemanticComparisonReport) -> dict[str, object]:
    payload: dict[str, object] = {
        'schema': report.schema,
        'status': report.status,
        'evidence_tier': report.evidence_tier,
        'authorizes_production_change': report.authorizes_production_change,
        'lock_validated': report.lock_validated,
        'provenance': _public_provenance(report.provenance),
        'counts': dict(report.counts),
        'metrics': dict(report.metrics),
        'confidence_intervals': dict(report.confidence_intervals),
        'slices': {name: dict(values) for name, values in report.slices.items()},
        'latency': dict(report.latency),
        'resource_cost': dict(report.resource_cost),
    }
    if isinstance(report, SemanticComparisonReport):
        payload['comparison'] = dict(report.comparison)
    return payload


def _build_report(
    *,
    schema: str,
    dataset: SemanticEvalDataset,
    provenance: SemanticModelProvenance,
    case_scores: tuple[SemanticCaseScore, ...],
    durations: Sequence[float],
    cold_load_seconds: float,
    resource_cost: Mapping[str, int | None | str],
) -> SemanticEvalReport:
    family_results = _family_results_from_case_scores(case_scores)
    counts = _counts_from_case_scores(case_scores, family_results)
    confidence_intervals = {
        'keep_family_miss_rate_upper_bound': _exact_upper_bound(
            counts['missed_keep_families'], counts['keep_families'], confidence=0.95
        )
    }
    metrics = {
        'screening_calls_avoided_per_1000_titles': (
            sum(1 for item in case_scores if item.score.reject) / len(case_scores) * 1000.0 if case_scores else 0.0
        )
    }
    slices = _slice_metrics_from_case_scores(case_scores)
    latency = _latency_from_durations(cold_load_seconds, durations)
    normalized_provenance = SemanticModelProvenance(
        model_name=provenance.model_name,
        model_revision=provenance.model_revision,
        sentence_transformers_version=provenance.sentence_transformers_version,
        anchor_digest=provenance.anchor_digest,
        keyword_override_digest=provenance.keyword_override_digest,
        dataset_digest=dataset.dataset_digest,
        split_digest=dataset.split_digest,
        family_lock_digest=dataset.family_lock_digest,
        git_sha=provenance.git_sha,
        command=provenance.command,
        score_contract_digest=provenance.score_contract_digest,
        family_aggregation_contract_digest=provenance.family_aggregation_contract_digest,
        dataset_schema=dataset.schema,
        dataset_version=dataset.dataset_version,
    )
    status = _status_from_components(
        counts=counts,
        confidence_intervals=confidence_intervals,
        provenance=normalized_provenance,
        evidence_tier=dataset.evidence_tier,
        locked=dataset.locked,
    )
    authorizes = bool(
        status == 'pass'
        and dataset.evidence_tier == 'private_gold_locked'
        and dataset.locked
        and _approved_contracts(normalized_provenance)
    )
    return SemanticEvalReport(
        schema=schema,
        status=status,
        evidence_tier=dataset.evidence_tier,
        authorizes_production_change=authorizes,
        provenance=normalized_provenance,
        counts=counts,
        metrics=metrics,
        confidence_intervals=confidence_intervals,
        slices=slices,
        latency=latency,
        resource_cost=dict(resource_cost),
        family_results=family_results,
        case_scores=case_scores,
        lock_validated=dataset.locked,
    )


def _rebuild_from_case_scores(
    *,
    schema: str,
    dataset: SemanticEvalDataset,
    provenance: SemanticModelProvenance,
    case_scores: tuple[SemanticCaseScore, ...],
    latency: Mapping[str, float],
    resource_cost: Mapping[str, int | None | str],
) -> SemanticEvalReport:
    _validate_case_evidence(case_scores)
    family_results = _family_results_from_case_scores(case_scores)
    counts = _counts_from_case_scores(case_scores, family_results)
    confidence_intervals = {
        'keep_family_miss_rate_upper_bound': _exact_upper_bound(
            counts['missed_keep_families'], counts['keep_families'], confidence=0.95
        )
    }
    metrics = {
        'screening_calls_avoided_per_1000_titles': (
            sum(1 for item in case_scores if item.score.reject) / len(case_scores) * 1000.0 if case_scores else 0.0
        )
    }
    slices = _slice_metrics_from_case_scores(case_scores)
    status = _status_from_components(
        counts=counts,
        confidence_intervals=confidence_intervals,
        provenance=provenance,
        evidence_tier=dataset.evidence_tier,
        locked=dataset.locked,
    )
    authorizes = bool(
        status == 'pass'
        and dataset.evidence_tier == 'private_gold_locked'
        and dataset.locked
        and _approved_contracts(provenance)
    )
    return SemanticEvalReport(
        schema=schema,
        status=status,
        evidence_tier=dataset.evidence_tier,
        authorizes_production_change=authorizes,
        provenance=provenance,
        counts=counts,
        metrics=metrics,
        confidence_intervals=confidence_intervals,
        slices=slices,
        latency=dict(latency),
        resource_cost=dict(resource_cost),
        family_results=family_results,
        case_scores=case_scores,
        lock_validated=dataset.locked,
    )


def _status_from_components(
    *,
    counts: Mapping[str, int],
    confidence_intervals: Mapping[str, float],
    provenance: SemanticModelProvenance,
    evidence_tier: EvidenceTier,
    locked: bool,
) -> EvalStatus:
    if not _approved_contracts(provenance):
        return 'unavailable'
    if evidence_tier == 'private_gold_locked' and not locked:
        return 'unavailable'
    if counts['keep_families'] == 0:
        return 'insufficient_data'
    upper_bound = confidence_intervals['keep_family_miss_rate_upper_bound']
    if upper_bound <= 0.01:
        return 'pass'
    if counts['missed_keep_families'] > 0:
        return 'fail'
    return 'insufficient_data'


def _approved_contracts(provenance: SemanticModelProvenance) -> bool:
    return (
        provenance.score_contract_digest == SCORE_CONTRACT_DIGEST
        and provenance.family_aggregation_contract_digest == FAMILY_AGGREGATION_CONTRACT_DIGEST
    )


def _canonicalize_gold_cases(raw_cases: Sequence[object], dataset_version: str) -> tuple[SemanticEvalCase, ...]:
    loaded = [_load_eval_case(item) for item in raw_cases]
    _validate_unique_case_ids(loaded)
    deduped: dict[str, SemanticEvalCase] = {}
    for case in sorted(loaded, key=lambda item: item.case_id):
        if case.split != authoritative_split(dataset_version, case.family_id):
            raise ValueError('authoritative split mismatch')
        existing = deduped.get(case.normalized_title)
        if existing is None:
            deduped[case.normalized_title] = case
            continue
        if existing.label != case.label or existing.family_id != case.family_id:
            raise ValueError('conflicting normalized duplicate')
    canonical = tuple(deduped.values())
    _validate_family_splits(canonical)
    return canonical


def _family_results_from_case_scores(case_scores: Sequence[SemanticCaseScore]) -> tuple[SemanticFamilyOutcome, ...]:
    grouped: dict[str, list[SemanticCaseScore]] = {}
    for item in case_scores:
        grouped.setdefault(item.family_id, []).append(item)
    results: list[SemanticFamilyOutcome] = []
    for family_id, members in grouped.items():
        results.append(
            SemanticFamilyOutcome(
                family_id=family_id,
                split=members[0].split,
                has_keep_case=any(member.label in KEEP_LABELS for member in members),
                missed_keep=any(member.label in KEEP_LABELS and member.score.reject for member in members),
                all_non_backend=all(member.label == 'non_backend' for member in members),
                correct_rejection=all(member.label == 'non_backend' and member.score.reject for member in members),
            )
        )
    return tuple(sorted(results, key=lambda item: item.family_id))


def _counts_from_case_scores(
    case_scores: Sequence[SemanticCaseScore],
    family_results: Sequence[SemanticFamilyOutcome],
) -> dict[str, int]:
    return {
        'total_cases': len(case_scores),
        'holdout_cases': sum(1 for item in case_scores if item.split == 'holdout'),
        'calibration_cases': sum(1 for item in case_scores if item.split == 'calibration'),
        'keep_cases': sum(1 for item in case_scores if item.label in KEEP_LABELS),
        'reject_cases': sum(1 for item in case_scores if item.label == 'non_backend'),
        'keep_families': sum(1 for item in family_results if item.split == 'holdout' and item.has_keep_case),
        'correct_rejection_families': sum(1 for item in family_results if item.split == 'holdout' and item.correct_rejection),
        'missed_keep_families': sum(1 for item in family_results if item.split == 'holdout' and item.missed_keep),
    }


def _slice_metrics_from_case_scores(
    case_scores: Sequence[SemanticCaseScore],
) -> dict[str, dict[str, float | int]]:
    buckets = {
        name: {'cases': 0, 'families': 0, 'keep_families': 0, 'reject_families': 0, 'keep_family_miss_rate_upper_bound': 1.0}
        for name in REQUIRED_SLICES
    }
    family_members: dict[str, list[SemanticCaseScore]] = {}
    for item in case_scores:
        family_members.setdefault(item.family_id, []).append(item)
        for slice_name in item.slices:
            bucket = buckets.setdefault(slice_name, {'cases': 0, 'families': 0, 'keep_families': 0, 'reject_families': 0, 'keep_family_miss_rate_upper_bound': 1.0})
            bucket['cases'] = int(bucket['cases']) + 1
    for slice_name, bucket in buckets.items():
        family_ids = {item.family_id for item in case_scores if slice_name in item.slices}
        keep_families = 0
        reject_families = 0
        missed = 0
        for family_id in family_ids:
            members = family_members[family_id]
            has_keep = any(member.label in KEEP_LABELS for member in members)
            is_reject = all(member.label == 'non_backend' and member.score.reject for member in members)
            is_missed = any(member.label in KEEP_LABELS and member.score.reject for member in members)
            if has_keep:
                keep_families += 1
            if is_reject:
                reject_families += 1
            if is_missed:
                missed += 1
        bucket['families'] = len(family_ids)
        bucket['keep_families'] = keep_families
        bucket['reject_families'] = reject_families
        bucket['keep_family_miss_rate_upper_bound'] = _exact_upper_bound(missed, keep_families, confidence=0.95)
    return buckets


def _latency_from_durations(cold_load_seconds: float, durations: Sequence[float]) -> dict[str, float]:
    total = sum(durations)
    return {
        'cold_load_seconds': cold_load_seconds,
        'warm_p50_seconds': _nearest_rank(durations, 0.50),
        'warm_p95_seconds': _nearest_rank(durations, 0.95),
        'throughput_titles_per_second': (len(durations) / total) if total > 0 else 0.0,
    }


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def _exact_upper_bound(misses: int, trials: int, *, confidence: float) -> float:
    if trials <= 0:
        return 1.0
    if misses >= trials:
        return 1.0
    alpha = 1.0 - confidence
    if misses == 0:
        return 1.0 - alpha ** (1.0 / trials)
    low = misses / trials
    high = min(1.0, max(low + 1e-12, 0.2))
    while _binomial_cdf(misses, trials, high) > alpha and high < 1.0:
        high = min(1.0, high * 2.0)
    for _ in range(80):
        mid = (low + high) / 2.0
        if _binomial_cdf(misses, trials, mid) > alpha:
            low = mid
        else:
            high = mid
    return high


def _binomial_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0 if k < n else 1.0
    logs = [_log_binomial_pmf(i, n, p) for i in range(k + 1)]
    maximum = max(logs)
    return min(1.0, math.exp(maximum) * sum(math.exp(value - maximum) for value in logs))


def _exact_binomial_tail(successes: int, trials: int) -> float:
    if trials <= 0:
        return 1.0
    logs = [_log_binomial_pmf(i, trials, 0.5) for i in range(successes, trials + 1)]
    maximum = max(logs)
    return min(1.0, math.exp(maximum) * sum(math.exp(value - maximum) for value in logs))


def _log_binomial_pmf(successes: int, trials: int, p: float) -> float:
    return (
        math.lgamma(trials + 1)
        - math.lgamma(successes + 1)
        - math.lgamma(trials - successes + 1)
        + successes * math.log(p)
        + (trials - successes) * math.log1p(-p)
    )


def _public_provenance(provenance: SemanticModelProvenance) -> dict[str, object]:
    return {
        'model_name': provenance.model_name,
        'model_revision': provenance.model_revision,
        'sentence_transformers_version': provenance.sentence_transformers_version,
        'anchor_digest': provenance.anchor_digest,
        'keyword_override_digest': provenance.keyword_override_digest,
        'dataset_digest': provenance.dataset_digest,
        'split_digest': provenance.split_digest,
        'family_lock_digest': provenance.family_lock_digest,
        'git_sha': provenance.git_sha,
        'command': _redact_command(provenance.command),
        'score_contract_digest': provenance.score_contract_digest,
        'family_aggregation_contract_digest': provenance.family_aggregation_contract_digest,
        'dataset_schema': provenance.dataset_schema,
        'dataset_version': provenance.dataset_version,
    }


def _redact_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    redacted: list[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token.startswith('--') and '=' in token:
            flag, value = token.split('=', 1)
            if _looks_private(value):
                redacted.append(f'{flag}=<redacted>')
            else:
                redacted.append(token)
            i += 1
            continue
        redacted.append(token)
        if token.startswith('--') and i + 1 < len(parts) and _looks_private(parts[i + 1]):
            redacted.append('<redacted>')
            i += 2
            continue
        i += 1
    return shlex.join(redacted)


def _looks_private(value: str) -> bool:
    normalized = value.strip('"\'')
    if normalized.startswith('private/'):
        return True
    # A leading '/private/' is the macOS filesystem root for /tmp and /var, not the
    # workspace private data directory, which never sits at the filesystem root.
    return '/private/' in normalized[1:]




def normalize_eval_title(title: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().strip().split())


def _load_capture_provenance(payload: object) -> Mapping[str, object]:
    if payload is None:
        return {}
    return _require_mapping_object(payload, 'capture_provenance')


def _capture_case_id(normalized_title: str) -> str:
    return f'case-{hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:16]}'


def _capture_family_id(title: str) -> str:
    normalized = normalize_eval_title(title)
    if normalized.startswith('[') and ']' in normalized:
        normalized = normalized.split(']', 1)[1].strip()
    tokens = [
        token
        for token in re.split(r'[^0-9a-z가-힣]+', normalized)
        if token and token not in {'senior', 'junior', 'staff', 'lead', 'principal', 'sr', 'jr', '시니어', '주니어'}
    ]
    family_key = ' '.join(tokens) or normalized
    return f'family-{hashlib.sha256(family_key.encode("utf-8")).hexdigest()[:16]}'


def _provisional_split(seed: int, family_id: str) -> DatasetSplit:
    digest = hashlib.sha256(f'{seed}\0{family_id}'.encode('utf-8')).digest()
    bucket = int.from_bytes(digest[:8], 'big') % 100
    return 'calibration' if bucket < CALIBRATION_PERCENT else 'holdout'


def _capture_slices(title: str) -> tuple[str, ...]:
    normalized = normalize_eval_title(title)
    slices: list[str] = []
    has_korean = bool(re.search(r'[가-힣]', title))
    has_english = bool(re.search(r'[A-Za-z]', title))
    if has_korean:
        slices.append('korean')
    if has_english:
        slices.append('english')
    if has_korean and has_english:
        slices.append('mixed')
    if title.lstrip().startswith('['):
        slices.append('bracket_prefixed')
    if any(
        token in normalized.split()
        for token in {'senior', 'junior', 'staff', 'lead', 'principal', 'sr', 'jr', '시니어', '주니어'}
    ):
        slices.append('seniority_marked')
    category = 'generic_software'
    if any(token in normalized for token in {'ai', 'ml', 'machine learning'}):
        category = 'ai_ml'
    elif 'data' in normalized or '데이터' in normalized:
        category = 'data'
    elif any(token in normalized for token in {'devops', 'sre', 'infra', '인프라'}):
        category = 'devops_sre'
    elif any(token in normalized for token in {'frontend', 'front-end', '프론트엔드', 'web ui'}):
        category = 'frontend'
    elif any(token in normalized for token in {'qa', 'product manager', 'pm', '품질'}):
        category = 'qa_product'
    elif any(token in normalized for token in {'embedded', 'firmware', 'hardware', '임베디드'}):
        category = 'hardware_embedded'
    slices.append(category)
    return tuple(dict.fromkeys(slices))


def _capture_title_sort_key(title: str) -> tuple[str, str]:
    return (normalize_eval_title(title), title)


def _optional_non_empty_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{key} must be a string or null')
    return value or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

def _validate_report_contract(dataset: SemanticEvalDataset, report: SemanticEvalReport) -> None:
    expected_case_ids = {case.case_id for case in dataset.cases}
    report_case_ids = {case.case_id for case in report.case_scores}
    if report_case_ids != expected_case_ids:
        raise ValueError('case evidence mismatch')
    report_family_ids = {item.family_id for item in report.family_results}
    expected_family_ids = {case.family_id for case in dataset.cases}
    if report_family_ids != expected_family_ids:
        raise ValueError('family evidence mismatch')
    provenance = report.provenance
    if provenance.dataset_schema != dataset.schema:
        raise ValueError('dataset schema mismatch')
    if provenance.dataset_version != dataset.dataset_version:
        raise ValueError('dataset version mismatch')
    if provenance.dataset_digest != dataset.dataset_digest:
        raise ValueError('dataset digest mismatch')
    if provenance.split_digest != dataset.split_digest:
        raise ValueError('split digest mismatch')
    if provenance.family_lock_digest != dataset.family_lock_digest:
        raise ValueError('family lock digest mismatch')
    if provenance.score_contract_digest != SCORE_CONTRACT_DIGEST:
        raise ValueError('score contract digest mismatch')
    if provenance.family_aggregation_contract_digest != FAMILY_AGGREGATION_CONTRACT_DIGEST:
        raise ValueError('family aggregation contract digest mismatch')
    if report.lock_validated != dataset.locked:
        raise ValueError('lock validation mismatch')


def _bind_case_scores_to_dataset(case_scores: Sequence[SemanticCaseScore], dataset: SemanticEvalDataset) -> None:
    dataset_cases = {case.case_id: case for case in dataset.cases}
    if {item.case_id for item in case_scores} != set(dataset_cases):
        raise ValueError('case evidence mismatch')
    for item in case_scores:
        case = dataset_cases[item.case_id]
        if item.family_id != case.family_id:
            raise ValueError('case evidence mismatch')
        if item.split != case.split:
            raise ValueError('case evidence mismatch')
        if item.label != case.label:
            raise ValueError('case evidence mismatch')
        if item.slices != case.slices:
            raise ValueError('case evidence mismatch')
        if item.quick_filter_outcome != case.quick_filter_outcome:
            raise ValueError('case evidence mismatch')
        if item.quick_filter_config_digest != case.quick_filter_config_digest:
            raise ValueError('case evidence mismatch')
        if item.score.normalized_title != case.normalized_title:
            raise ValueError('case evidence mismatch')
        if item.score.title != case.title:
            raise ValueError('case evidence mismatch')


def _validate_case_evidence(case_scores: Sequence[SemanticCaseScore]) -> None:
    seen: set[str] = set()
    family_splits: dict[str, DatasetSplit] = {}
    for item in case_scores:
        if item.case_id in seen:
            raise ValueError('duplicate case evidence')
        seen.add(item.case_id)
        previous = family_splits.get(item.family_id)
        if previous is not None and previous != item.split:
            raise ValueError('family evidence mismatch')
        family_splits[item.family_id] = item.split
        _validate_score(item.score)


def _validate_score(score: SemanticTitleScore) -> None:
    if score.normalized_title != normalize_eval_title(score.title):
        raise ValueError('non-finite or unavailable score')
    for value in (score.backend_score, score.non_backend_score, score.relative_score):
        if not math.isfinite(value):
            raise ValueError('non-finite or unavailable score')


def _normalize_resource_sample(sample: Mapping[str, object]) -> dict[str, int | None | str]:
    if 'peak_rss_bytes' in sample:
        value = sample['peak_rss_bytes']
        if value is None:
            return {'peak_rss_bytes': None, 'peak_rss_status': 'unavailable'}
        return {'peak_rss_bytes': int(_require_numeric_value(value, 'peak_rss_bytes')), 'peak_rss_status': 'ok'}
    if 'peak_rss' in sample:
        value = _require_numeric_value(sample.get('peak_rss'), 'peak_rss')
        platform = sample.get('platform')
        unit = sample.get('unit')
        if platform == 'linux' and unit == 'KiB':
            return {'peak_rss_bytes': int(value * 1024), 'peak_rss_status': 'ok'}
        if platform == 'darwin' and unit == 'bytes':
            return {'peak_rss_bytes': int(value), 'peak_rss_status': 'ok'}
        return {'peak_rss_bytes': None, 'peak_rss_status': 'unavailable'}
    return {'peak_rss_bytes': None, 'peak_rss_status': 'unavailable'}


def _load_provenance(payload: Mapping[str, object]) -> SemanticModelProvenance:
    return SemanticModelProvenance(
        model_name=_require_non_empty_string(payload, 'model_name'),
        model_revision=_require_non_empty_string(payload, 'model_revision'),
        sentence_transformers_version=_require_non_empty_string(payload, 'sentence_transformers_version'),
        anchor_digest=_require_non_empty_string(payload, 'anchor_digest'),
        keyword_override_digest=_require_non_empty_string(payload, 'keyword_override_digest'),
        dataset_digest=_require_non_empty_string(payload, 'dataset_digest'),
        split_digest=_require_non_empty_string(payload, 'split_digest'),
        family_lock_digest=_require_non_empty_string(payload, 'family_lock_digest'),
        git_sha=_require_non_empty_string(payload, 'git_sha'),
        command=_require_non_empty_string(payload, 'command'),
        score_contract_digest=_require_non_empty_string(payload, 'score_contract_digest'),
        family_aggregation_contract_digest=_require_non_empty_string(payload, 'family_aggregation_contract_digest'),
        dataset_schema=_require_non_empty_string(payload, 'dataset_schema'),
        dataset_version=_require_non_empty_string(payload, 'dataset_version'),
    )




def _load_queue_case(payload: object) -> SemanticQueueCase:
    mapping = _require_mapping_object(payload, 'case')
    label = mapping.get('label')
    if label is not None and label not in SUPPORTED_LABELS:
        raise ValueError('invalid label')
    title = _require_non_empty_string(mapping, 'title')
    slices = tuple(_require_string_list(mapping, 'slices'))
    if not slices:
        raise ValueError('at least one slice is required')
    return SemanticQueueCase(
        case_id=_require_non_empty_string(mapping, 'case_id'),
        family_id=_require_non_empty_string(mapping, 'family_id'),
        title=title,
        label=cast(SemanticLabel | None, label),
        split=_parse_split(_require_non_empty_string(mapping, 'split')),
        slices=slices,
        quick_filter_outcome=_require_non_empty_string(mapping, 'quick_filter_outcome'),
        quick_filter_config_digest=_require_non_empty_string(mapping, 'quick_filter_config_digest'),
        normalized_title=normalize_eval_title(title),
    )


def _load_eval_case(payload: object) -> SemanticEvalCase:
    queue_case = _load_queue_case(payload)
    if queue_case.label is None:
        raise ValueError('gold label is required')
    return SemanticEvalCase(
        case_id=queue_case.case_id,
        family_id=queue_case.family_id,
        title=queue_case.title,
        label=queue_case.label,
        split=queue_case.split,
        slices=queue_case.slices,
        quick_filter_outcome=queue_case.quick_filter_outcome,
        quick_filter_config_digest=queue_case.quick_filter_config_digest,
        normalized_title=queue_case.normalized_title,
    )

def _load_family_result(payload: object) -> SemanticFamilyOutcome:
    mapping = _require_mapping_object(payload, 'family_result')
    _validate_exact_keys(mapping, REQUIRED_FAMILY_RESULT_KEYS, 'exact family result keys')
    return SemanticFamilyOutcome(
        family_id=_require_non_empty_string(mapping, 'family_id'),
        split=_parse_split(_require_non_empty_string(mapping, 'split')),
        has_keep_case=_require_bool(mapping, 'has_keep_case'),
        missed_keep=_require_bool(mapping, 'missed_keep'),
        all_non_backend=_require_bool(mapping, 'all_non_backend'),
        correct_rejection=_require_bool(mapping, 'correct_rejection'),
    )


def _load_case_score(payload: object) -> SemanticCaseScore:
    mapping = _require_mapping_object(payload, 'case_score')
    _validate_exact_keys(mapping, REQUIRED_CASE_SCORE_KEYS, 'exact case score keys')
    score_mapping = _require_mapping(mapping, 'score')
    _validate_exact_keys(score_mapping, REQUIRED_SCORE_KEYS, 'exact score keys')
    return SemanticCaseScore(
        case_id=_require_non_empty_string(mapping, 'case_id'),
        family_id=_require_non_empty_string(mapping, 'family_id'),
        split=_parse_split(_require_non_empty_string(mapping, 'split')),
        label=_parse_label(_require_non_empty_string(mapping, 'label')),
        slices=tuple(_require_string_list(mapping, 'slices')),
        quick_filter_outcome=_require_non_empty_string(mapping, 'quick_filter_outcome'),
        quick_filter_config_digest=_require_non_empty_string(mapping, 'quick_filter_config_digest'),
        score=SemanticTitleScore(
            title=_require_non_empty_string(score_mapping, 'title'),
            normalized_title=_require_non_empty_string(score_mapping, 'normalized_title'),
            backend_score=_require_any_number(score_mapping, 'backend_score'),
            non_backend_score=_require_any_number(score_mapping, 'non_backend_score'),
            relative_score=_require_any_number(score_mapping, 'relative_score'),
            reject=_require_bool(score_mapping, 'reject'),
        ),
    )


def _load_comparison(payload: Mapping[str, object]) -> dict[str, int | float | str]:
    _validate_exact_keys(payload, REQUIRED_COMPARISON_KEYS, 'exact comparison keys')
    return {
        'candidate_gains': _require_int(payload, 'candidate_gains'),
        'candidate_losses': _require_int(payload, 'candidate_losses'),
        'mcnemar_p_value': _require_any_number(payload, 'mcnemar_p_value'),
        'reason': _require_non_empty_string(payload, 'reason'),
    }


def _load_comparison_binding(payload: Mapping[str, object]) -> dict[str, object]:
    _validate_exact_keys(payload, REQUIRED_COMPARISON_BINDING_KEYS, 'exact comparison binding keys')
    return {
        'dataset_digest': _require_non_empty_string(payload, 'dataset_digest'),
        'split_digest': _require_non_empty_string(payload, 'split_digest'),
        'family_lock_digest': _require_non_empty_string(payload, 'family_lock_digest'),
        'incumbent_report_digest': _require_non_empty_string(payload, 'incumbent_report_digest'),
        'candidate_report_digest': _require_non_empty_string(payload, 'candidate_report_digest'),
        'incumbent_provenance': dict(_require_mapping(payload, 'incumbent_provenance')),
        'candidate_provenance': dict(_require_mapping(payload, 'candidate_provenance')),
    }


def _load_slices(payload: Mapping[str, object]) -> dict[str, dict[str, float | int]]:
    slices: dict[str, dict[str, float | int]] = {}
    for name, raw in payload.items():
        mapping = _require_mapping_object(raw, f'slice:{name}')
        _validate_exact_keys(mapping, REQUIRED_SLICE_KEYS, 'exact slice keys')
        slices[str(name)] = {
            'cases': _require_int(mapping, 'cases'),
            'families': _require_int(mapping, 'families'),
            'keep_families': _require_int(mapping, 'keep_families'),
            'reject_families': _require_int(mapping, 'reject_families'),
            'keep_family_miss_rate_upper_bound': _require_any_number(mapping, 'keep_family_miss_rate_upper_bound'),
        }
    return slices


def _parse_resource_cost(payload: Mapping[str, object]) -> dict[str, int | None | str]:
    _validate_exact_keys(payload, REQUIRED_RESOURCE_KEYS, 'exact resource keys')
    status = _parse_peak_rss_status(_require_non_empty_string(payload, 'peak_rss_status'))
    if status == 'unavailable':
        if payload.get('peak_rss_bytes') is not None:
            raise ValueError('peak_rss_bytes must be null when unavailable')
        return {'peak_rss_bytes': None, 'peak_rss_status': status}
    return {'peak_rss_bytes': _require_int(payload, 'peak_rss_bytes'), 'peak_rss_status': status}


def _digest_cases(cases: Sequence[SemanticEvalCase], dataset_version: str) -> str:
    material = [
        {
            'case_id': case.case_id,
            'family_id': case.family_id,
            'normalized_title': case.normalized_title,
            'label': case.label,
            'split': case.split,
            'slices': list(case.slices),
        }
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    return hashlib.sha256(json.dumps([dataset_version, material], ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _digest_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps(sorted([list(pair) for pair in pairs]), ensure_ascii=False).encode('utf-8')).hexdigest()


def _validate_unique_case_ids(cases: Sequence[SemanticQueueCase | SemanticEvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f'duplicate case_id: {case.case_id}')
        seen.add(case.case_id)


def _validate_family_splits(cases: Sequence[SemanticEvalCase]) -> None:
    splits: dict[str, DatasetSplit] = {}
    for case in cases:
        previous = splits.get(case.family_id)
        if previous is not None and previous != case.split:
            raise ValueError(f'family split drift: {case.family_id}')
        splits[case.family_id] = case.split


def _require_confirmation(payload: Mapping[str, object]) -> Mapping[str, object]:
    confirmed_by = payload.get('confirmed_by')
    confirmed_at = payload.get('confirmed_at')
    if not isinstance(confirmed_by, str) or not confirmed_by:
        raise ValueError('human confirmation must be structured and non-empty')
    if not isinstance(confirmed_at, str) or not confirmed_at:
        raise ValueError('human confirmation must be structured and non-empty')
    return payload


def _require_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f'{key} must be a mapping')
    return cast(Mapping[str, object], value)


def _require_mapping_object(payload: object, key: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f'{key} must be a mapping')
    return cast(Mapping[str, object], payload)


def _require_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f'{key} must be a list')
    return value


def _require_non_empty_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{key} must be a non-empty string')
    return value


def _require_string_list(payload: Mapping[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{key} must be a list of strings')
    return cast(list[str], value)


def _require_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f'{key} must be a boolean')
    return value


def _require_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{key} must be an integer')
    return value


def _require_numeric_value(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{key} must be numeric')
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{key} must be finite and non-negative')
    return number


def _require_any_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{key} must be numeric')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{key} must be finite')
    return number


def _coerce_float_mapping(payload: Mapping[str, object]) -> dict[str, float]:
    return {key: _require_any_number(payload, key) for key in payload}


def _coerce_int_mapping(payload: Mapping[str, object]) -> dict[str, int]:
    return {key: _require_int(payload, key) for key in payload}


def _validate_exact_keys(payload: Mapping[str, object], expected: frozenset[str], message: str) -> Mapping[str, object]:
    if set(payload.keys()) != expected:
        raise ValueError(message)
    return payload


def _parse_evidence_tier(value: str) -> EvidenceTier:
    if value not in {'synthetic', 'private_gold_locked'}:
        raise ValueError('invalid evidence tier')
    return cast(EvidenceTier, value)


def _parse_split(value: str) -> DatasetSplit:
    if value not in {'calibration', 'holdout'}:
        raise ValueError('invalid split')
    return cast(DatasetSplit, value)


def _parse_label(value: str) -> SemanticLabel:
    if value not in SUPPORTED_LABELS:
        raise ValueError('invalid label')
    return cast(SemanticLabel, value)


def _parse_status(value: str) -> EvalStatus:
    if value not in {'pass', 'fail', 'insufficient_data', 'unavailable'}:
        raise ValueError('invalid status')
    return cast(EvalStatus, value)


def _parse_peak_rss_status(value: str) -> PeakRssStatus:
    if value not in {'ok', 'unavailable'}:
        raise ValueError('invalid peak rss status')
    return cast(PeakRssStatus, value)
