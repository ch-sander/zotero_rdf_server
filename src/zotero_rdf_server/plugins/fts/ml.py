from sklearn.neighbors import NearestNeighbors
from typing import Any, Dict, List, Literal, Optional
from collections import defaultdict
import math
from .endpoints import ResultAnalysisParams
from .helpers import ensure_import
from copy import deepcopy
import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple
from .search import logger, os_mtermvectors, sort_hits_by_cluster_and_score

NeighborMode = Literal[
    "knn_vector",
    "mlt",
    "page_parent",
    "meta_onehot",
    "hybrid",
]

try:
    ensure_import("scikit-learn", requirements=None)
    ensure_import("rapidfuzz", requirements=None)
    ensure_import("scipy", requirements=None)
    from rapidfuzz import fuzz
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    from scipy.sparse import csr_matrix
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import normalize as sk_normalize
except Exception as e:
    logger.error(f"Failed to import ML packages: {e}")

def _stable_neighbor_id(hit: Dict[str, Any], fallback: int | str):
    return _get_nested(hit, "_source.__row_index__", fallback)

def _terms_from_tv_doc(
    tv_doc: Dict[str, Any],
    *,
    field: str,
    min_token_length: int,
    max_tokens_per_doc: int,
) -> List[Tuple[str, Dict[str, Any]]]:
    field_data = tv_doc.get("term_vectors", {}).get(field, {})
    terms = field_data.get("terms", {})

    items = [
        (term, stats)
        for term, stats in terms.items()
        if len(term) >= min_token_length
    ]

    if max_tokens_per_doc > 0:
        items = sorted(
            items,
            key=lambda item: item[1].get("term_freq", 0),
            reverse=True,
        )[:max_tokens_per_doc]

    return items

def _pseudo_doc_from_terms(term_items: List[Tuple[str, Dict[str, Any]]]) -> str:
    tokens: List[str] = []
    for term, stats in term_items:
        tf = int(stats.get("term_freq", 0) or 0)
        if tf > 0:
            tokens.extend([term] * tf)
    return " ".join(tokens)

def _build_readable_local_terms(
    *,
    doc_ids: List[str],
    tv_by_id: Dict[str, Dict[str, Any]],
    field: str,
    analysis: ResultAnalysisParams,
) -> Dict[str, List[Dict[str, Any]]]:
    local_doc_freq = Counter()
    usable_docs = []

    for doc_id in doc_ids:
        tv_doc = tv_by_id.get(doc_id)
        if not tv_doc or not tv_doc.get("found"):
            continue

        term_items = _terms_from_tv_doc(
            tv_doc,
            field=field,
            min_token_length=analysis.analyze_min_token_length,
            max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
        )
        usable_docs.append((doc_id, term_items))

        for term, _ in term_items:
            local_doc_freq[term] += 1

    n_docs = max(1, len(usable_docs))
    out: Dict[str, List[Dict[str, Any]]] = {}

    for doc_id, term_items in usable_docs:
        total_terms = sum(stats.get("term_freq", 0) for _, stats in term_items) or 1
        scored = []

        for term, stats in term_items:
            tf = stats.get("term_freq", 0)
            df = local_doc_freq.get(term, 0)
            tf_norm = tf / total_terms
            idf = math.log((1 + n_docs) / (1 + df)) + 1.0
            score = tf_norm * idf

            scored.append({
                "term": term,
                "score": round(score, 6),
                "doc_freq": df,
                "term_freq": tf,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out[doc_id] = scored[:analysis.analyze_top_terms]

    return out

def _build_local_tfidf_from_vectorizer(
    *,
    doc_ids: List[str],
    tv_by_id: Dict[str, Dict[str, Any]],
    field: str,
    analysis: ResultAnalysisParams,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, float]]]:
    pseudo_docs: List[str] = []
    ordered_doc_ids: List[str] = []

    for doc_id in doc_ids:
        tv_doc = tv_by_id.get(doc_id)
        if not tv_doc or not tv_doc.get("found"):
            continue

        term_items = _terms_from_tv_doc(
            tv_doc,
            field=field,
            min_token_length=analysis.analyze_min_token_length,
            max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
        )

        pseudo_doc = _pseudo_doc_from_terms(term_items).strip()
        if not pseudo_doc:
            continue

        ordered_doc_ids.append(doc_id)
        pseudo_docs.append(pseudo_doc)

    if not pseudo_docs:
        return {}, {}

    use_char_ngrams = getattr(analysis, "analyze_use_char_ngrams", True)
    char_ngram_range = getattr(analysis, "analyze_char_ngram_range", (3, 5))
    tfidf_max_features = getattr(analysis, "analyze_tfidf_max_features", None)
    tfidf_min_df = getattr(analysis, "analyze_tfidf_min_df", 1)
    tfidf_max_df = getattr(analysis, "analyze_tfidf_max_df", 1.0)

    vectorizer_kwargs = dict(
        lowercase=True,
        norm="l2",
        use_idf=True,
        smooth_idf=True,
        sublinear_tf=False,
        min_df=tfidf_min_df,
        max_df=tfidf_max_df,
        max_features=tfidf_max_features,
    )

    if use_char_ngrams:
        vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=char_ngram_range,
            **vectorizer_kwargs,
        )
    else:
        vectorizer = TfidfVectorizer(
            analyzer="word",
            token_pattern=r"(?u)\b\w+\b",
            **vectorizer_kwargs,
        )

    try:
        matrix = vectorizer.fit_transform(pseudo_docs)
    except ValueError:
        return {}, {}

    if matrix.shape[1] == 0:
        return {}, {}

    features = vectorizer.get_feature_names_out()

    doc_top_terms: Dict[str, List[Dict[str, Any]]] = {}
    doc_vectors: Dict[str, Dict[str, float]] = {}

    top_n = max(1, int(analysis.analyze_top_terms))

    for row_idx, doc_id in enumerate(ordered_doc_ids):
        row = matrix.getrow(row_idx)
        indices = row.indices
        data = row.data

        vector = {
            str(features[col_idx]): round(float(score), 6)
            for col_idx, score in zip(indices, data)
        }
        doc_vectors[doc_id] = vector

        if len(indices) <= top_n:
            top_pairs = sorted(vector.items(), key=lambda x: x[1], reverse=True)
        else:
            top_idx = sorted(
                zip(indices, data),
                key=lambda x: x[1],
                reverse=True,
            )[:top_n]
            top_pairs = [
                (str(features[col_idx]), round(float(score), 6))
                for col_idx, score in top_idx
            ]

        doc_top_terms[doc_id] = [
            {
                "term": term,
                "score": score,
            }
            for term, score in top_pairs
        ]

    return doc_top_terms, doc_vectors

def analysis_from_mtermvectors(
    *,
    hits: List[Dict[str, Any]],
    tv_resp: Dict[str, Any],
    analysis: ResultAnalysisParams,
    field_fallback: str,
) -> List[Dict[str, Any]]:
    analysis_field = analysis.analyze_field or field_fallback
    docs = tv_resp.get("docs", [])

    tv_by_id: Dict[str, Dict[str, Any]] = {
        d.get("_id"): d for d in docs if d.get("_id")
    }

    def wants_global() -> bool:
        return analysis.analysis_mode in ("global", "both")

    def wants_local() -> bool:
        return analysis.analysis_mode in ("local", "both")

    def build_global_scored_terms(
        *,
        filtered_items: List[Tuple[str, Dict[str, Any]]],
        n_docs: int,
    ) -> List[Dict[str, Any]]:
        total_terms = sum(stats.get("term_freq", 0) for _, stats in filtered_items) or 1

        scored: List[Dict[str, Any]] = []
        for term, stats in filtered_items:
            term_freq = stats.get("term_freq", 0)
            doc_freq = stats.get("doc_freq", 0)

            tf_norm = term_freq / total_terms
            idf = math.log((1 + n_docs) / (1 + doc_freq)) + 1.0
            score = tf_norm * idf

            scored.append(
                {
                    "term": term,
                    "score": round(score, 6),
                    "doc_freq": doc_freq,
                    "term_freq": term_freq,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:analysis.analyze_top_terms]

    doc_ids = [h.get("_id") for h in hits if h.get("_id")]

    local_top_terms_by_id = {}
    local_vectors_by_id = {}

    if wants_local():
        local_top_terms_by_id = _build_readable_local_terms(
            doc_ids=doc_ids,
            tv_by_id=tv_by_id,
            field=analysis_field,
            analysis=analysis,
        )

        _, local_vectors_by_id = _build_local_tfidf_from_vectorizer(
            doc_ids=doc_ids,
            tv_by_id=tv_by_id,
            field=analysis_field,
            analysis=analysis,
        )

    enriched: List[Dict[str, Any]] = []

    for hit in hits:
        hit_copy = dict(hit)
        doc_id = hit.get("_id")
        tv_doc = tv_by_id.get(doc_id)

        derived: Dict[str, Any] = {
            "field": analysis_field,
            "mode": analysis.analysis_mode,
            "top_terms": analysis.analyze_top_terms,
            "min_token_length": analysis.analyze_min_token_length,
            "max_tokens_per_doc": analysis.analyze_max_tokens_per_doc,
            "terms_total": 0,
        }

        if tv_doc and tv_doc.get("found"):
            term_items = _terms_from_tv_doc(
                tv_doc,
                field=analysis_field,
                min_token_length=analysis.analyze_min_token_length,
                max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
            )
            derived["terms_total"] = len(term_items)

            if wants_global():
                field_data = tv_doc.get("term_vectors", {}).get(analysis_field, {})
                field_stats = field_data.get("field_statistics", {})
                global_n_docs = field_stats.get("doc_count", 1) or 1

                global_top = build_global_scored_terms(
                    filtered_items=term_items,
                    n_docs=global_n_docs,
                )
                derived["global"] = {
                    "key_terms": [item["term"] for item in global_top],
                    "key_terms_details": global_top,
                }

            if wants_local():
                local_top = local_top_terms_by_id.get(doc_id, [])
                derived["local"] = {
                    "key_terms": [item["term"] for item in local_top],
                    "key_terms_details": local_top,
                    # "vector": local_vectors_by_id.get(doc_id, {}),
                }
                # include_vector = getattr(analysis, "analyze_include_vector", False)
                # if include_vector:
                #     derived["local"]["vector"] = local_vectors_by_id.get(doc_id, {})

        hit_copy.setdefault("_source", {})
        hit_copy["_source"].setdefault("analysis", {})
        hit_copy["_source"]["analysis"].update(derived)
        enriched.append(hit_copy)

    return enriched, local_vectors_by_id

def _extract_term_scores_legacy(
    hit: Dict[str, Any],
    *,
    analysis_branch: str,
    prefer_vector: bool = False,
) -> Dict[str, float]:
    source = hit.get("_source", {})
    analysis = source.get("analysis", {})
    branch = analysis.get(analysis_branch, {})

    if prefer_vector:
        vector = branch.get("vector") or {}
        if vector:
            return {str(term): float(score) for term, score in vector.items()}

    details = branch.get("key_terms_details") or []
    if details:
        out: Dict[str, float] = {}
        for item in details:
            term = item.get("term")
            score = item.get("score", 0.0)
            if term:
                out[str(term)] = float(score)
        return out

    key_terms = branch.get("key_terms") or []
    if key_terms:
        return {str(term): 1.0 for term in key_terms if term}

    vector = branch.get("vector") or {}
    return {str(term): float(score) for term, score in vector.items()}

def _extract_term_scores(
    hit: Dict[str, Any],
    *,
    analysis_branch: str,
    prefer_vector: bool = False,
    cluster_vectors_by_id: Dict[str, Dict[str, float]] | None = None,
) -> Dict[str, float]:
    if prefer_vector and cluster_vectors_by_id:
        doc_id = hit.get("_id")
        external_vector = cluster_vectors_by_id.get(doc_id) if doc_id else None
        if external_vector:
            return {str(term): float(score) for term, score in external_vector.items()}

    source = hit.get("_source", {})
    analysis = source.get("analysis", {})
    branch = analysis.get(analysis_branch, {})

    if prefer_vector:
        vector = branch.get("vector") or {}
        if vector:
            return {str(term): float(score) for term, score in vector.items()}

    details = branch.get("key_terms_details") or []
    if details:
        out: Dict[str, float] = {}
        for item in details:
            term = item.get("term")
            score = item.get("score", 0.0)
            if term:
                out[str(term)] = float(score)
        return out

    key_terms = branch.get("key_terms") or []
    if key_terms:
        return {str(term): 1.0 for term in key_terms if term}

    vector = branch.get("vector") or {}
    return {str(term): float(score) for term, score in vector.items()}

def _canonical_term(group: List[Tuple[str, float]]) -> str:
    terms = [term for term, _ in group]

    def centrality(term: str) -> float:
        return sum(fuzz.ratio(term, other) for other in terms)

    ranked = sorted(
        group,
        key=lambda x: (
            -x[1],
            -centrality(x[0]),
            len(x[0]),
            x[0],
        )
    )
    return ranked[0][0]

def _merge_similar_terms(
    term_scores: Dict[str, float],
    *,
    similarity_threshold: int = 90,
) -> Dict[str, float]:
    items = sorted(term_scores.items(), key=lambda x: x[1], reverse=True)
    groups: List[List[Tuple[str, float]]] = []

    for term, score in items:
        best_group_idx = None
        best_sim = -1

        for gi, group in enumerate(groups):
            sim = max(fuzz.ratio(term, existing_term) for existing_term, _ in group)
            if sim >= similarity_threshold and sim > best_sim:
                best_group_idx = gi
                best_sim = sim

        if best_group_idx is None:
            groups.append([(term, score)])
        else:
            groups[best_group_idx].append((term, score))

    merged: Dict[str, float] = {}
    for group in groups:
        canonical = _canonical_term(group)
        merged[canonical] = sum(score for _, score in group)

    return merged

def _build_cluster_label(
    *,
    cluster_term_scores: List[Dict[str, float]],
    all_term_scores: List[Dict[str, float]],
    top_label_terms: int = 10,
    fuzzy_merge: bool = True,
    similarity_threshold: int = 88,
) -> Tuple[str, List[str]]:
    cluster_agg = Counter()
    for term_scores in cluster_term_scores:
        cluster_agg.update(term_scores)

    hit_df = Counter()
    for term_scores in all_term_scores:
        for term in term_scores.keys():
            hit_df[term] += 1

    n_hits = max(1, len(all_term_scores))
    scored = {}

    for term, tf in cluster_agg.items():
        df = hit_df.get(term, 0)
        idf = math.log((1 + n_hits) / (1 + df)) + 1.0
        scored[term] = tf * idf

    if fuzzy_merge:
        scored = _merge_similar_terms(
            scored,
            similarity_threshold=similarity_threshold,
        )

    top_terms = [
        term for term, _ in sorted(
            scored.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_label_terms]
    ]

    label = ", ".join(top_terms) if top_terms else "unlabeled"
    return label, top_terms

def _compute_neighbors_knn(*, clustering_matrix, usable_hit_refs, k=10, metric="cosine"):
    if clustering_matrix is None:
        raise ValueError("clustering_matrix is required for mode='knn_vector'")

    nn = NearestNeighbors(n_neighbors=min(k + 1, len(usable_hit_refs)), metric=metric)
    nn.fit(clustering_matrix)
    distances, indices = nn.kneighbors(clustering_matrix)

    out = {}

    for row_idx, (dist_row, ind_row) in enumerate(zip(distances, indices)):
        hit_idx = usable_hit_refs[row_idx]
        ids = []
        dists = []

        for dist, neighbor_row in zip(dist_row, ind_row):
            if neighbor_row == row_idx:
                continue
            neighbor_hit_idx = usable_hit_refs[neighbor_row]
            ids.append(neighbor_hit_idx)
            dists.append(float(dist))

        out[hit_idx] = {"ids": ids[:k], "distances": dists[:k]}

    return out

def _extract_os_id(hit: Dict[str, Any], row_id_field: str | None = None) -> str | None:
    if row_id_field:
        parts = row_id_field.split(".")
        cur = hit
        for p in parts:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    return hit.get("_id")

def _compute_neighbors_mlt(
    *,
    hits,
    usable_hit_refs,
    k,
    client,
    index,
    fields,
    min_term_freq,
    min_doc_freq,
    max_query_terms,
    minimum_should_match,
    row_id_field=None,
):
    if client is None:
        raise ValueError("client is required for mode='mlt'")
    if not index:
        from .search import DEFAULT_ALIAS
        index = DEFAULT_ALIAS
        logger.warning(f"Using index: {DEFAULT_ALIAS}")

    # Mapping OpenSearch _id -> local hit index
    os_id_to_hit_idx = {}
    for hit_idx in usable_hit_refs:
        os_id = _extract_os_id(hits[hit_idx], row_id_field=row_id_field)
        if os_id is not None:
            os_id_to_hit_idx[str(os_id)] = hit_idx

    out = {}

    for hit_idx in usable_hit_refs:
        os_id = _extract_os_id(hits[hit_idx], row_id_field=row_id_field)
        if os_id is None:
            out[hit_idx] = {"ids": [], "distances": []}
            continue

        body = {
            "size": k + 1,
            "query": {
                "bool": {
                    "must": [
                        {
                            "more_like_this": {
                                "fields": fields,
                                "like": [{"_index": index, "_id": str(os_id)}],
                                "min_term_freq": min_term_freq,
                                "min_doc_freq": min_doc_freq,
                                "max_query_terms": max_query_terms,
                                "minimum_should_match": minimum_should_match,
                            }
                        }
                    ],
                    "must_not": [{"ids": {"values": [str(os_id)]}}],
                }
            },
        }

        resp = client.search(index=index, body=body)
        hits_res = resp.get("hits", {}).get("hits", [])

        ids = []
        dists = []

        for neighbor in hits_res:
            neighbor_os_id = str(neighbor.get("_id"))
            neighbor_hit_idx = os_id_to_hit_idx.get(neighbor_os_id)
            if neighbor_hit_idx is None:
                continue

            score = float(neighbor.get("_score") or 0.0)

            distance = 1.0 / (score + 1e-9)

            ids.append(neighbor_hit_idx)
            dists.append(distance)

            if len(ids) >= k:
                break

        out[hit_idx] = {"ids": ids, "distances": dists}

    return out

def _get_nested(d, path, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p, default)
    return cur

from collections.abc import Mapping

def _flatten_to_tokens(value, prefix=""):
    tokens = []

    if isinstance(value, Mapping):
        for k, v in value.items():
            new_prefix = f"{prefix}.{k}" if prefix else str(k)
            tokens.extend(_flatten_to_tokens(v, new_prefix))
        return tokens

    if isinstance(value, list):
        for item in value:
            if isinstance(item, (Mapping, list, tuple, set)):
                tokens.extend(_flatten_to_tokens(item, prefix))
            else:
                tokens.append((prefix, item))
        return tokens

    if isinstance(value, (tuple, set)):
        for item in value:
            if isinstance(item, (Mapping, list, tuple, set)):
                tokens.extend(_flatten_to_tokens(item, prefix))
            else:
                tokens.append((prefix, item))
        return tokens

    if value is not None:
        tokens.append((prefix, value))

    return tokens

def _compute_neighbors_page_parent(
    *,
    hits,
    usable_hit_refs,
    k,
    parent_field="meta.parent",
    page_field="page",
):
    by_parent = defaultdict(list)

    for hit_idx in usable_hit_refs:
        hit = hits[hit_idx]
        parent = _get_nested(hit, f"_source.{parent_field}")
        page = _get_nested(hit, f"_source.{page_field}")

        if parent is None:
            continue

        try:
            page_num = int(page) if page is not None else None
        except Exception:
            page_num = None

        by_parent[parent].append((hit_idx, page_num))

    out = {}
    for parent, items in by_parent.items():
        logger.info("group=%r size=%d pages=%r", parent, len(items), [p for _, p in items[:20]])
    for parent, items in by_parent.items():
        for hit_idx, page_num in items:
            candidates = []
            for other_idx, other_page in items:
                if other_idx == hit_idx:
                    continue

                if page_num is not None and other_page is not None:
                    distance = abs(page_num - other_page)
                else:
                    distance = 1.0

                candidates.append((other_idx, float(distance)))

            candidates.sort(key=lambda x: (x[1], x[0]))
            out[hit_idx] = {
                "ids": [c[0] for c in candidates[:k]],
                "distances": [c[1] for c in candidates[:k]],
            }

    for hit_idx in usable_hit_refs:
        out.setdefault(hit_idx, {"ids": [], "distances": []})

    return out

from scipy.sparse import csr_matrix
from sklearn.preprocessing import normalize as sk_normalize
from sklearn.neighbors import NearestNeighbors
import json

def _compute_neighbors_meta_onehot(
    *,
    hits,
    usable_hit_refs,
    k,
    meta_fields=None,
):
    vocab = {}
    rows = []
    cols = []
    data = []

    use_all_meta = not meta_fields
 
    for row_idx, hit_idx in enumerate(usable_hit_refs):
        hit = hits[hit_idx]

        if use_all_meta:
            logger.warning("Using all meta fields!")
            meta_value = _get_nested(hit, "_source.meta")
            if not isinstance(meta_value, dict):
                continue

            flat_items = _flatten_to_tokens(meta_value, prefix="meta")

            for field_path, scalar_value in flat_items:
                if scalar_value is None:
                    continue
                if isinstance(scalar_value, (dict, list, tuple, set)):
                    continue

                token = f"{field_path}={scalar_value}"
                col_idx = vocab.setdefault(token, len(vocab))
                rows.append(row_idx)
                cols.append(col_idx)
                data.append(1.0)

        else:
            for field in meta_fields:
                value = _get_nested(hit, f"_source.{field}")
                if value is None:
                    continue

                if isinstance(value, dict):
                    continue

                values = value if isinstance(value, list) else [value]

                for v in values:
                    if v is None:
                        continue
                    if isinstance(v, (dict, list, tuple, set)):
                        continue

                    token = f"{field}={v}"
                    col_idx = vocab.setdefault(token, len(vocab))
                    rows.append(row_idx)
                    cols.append(col_idx)
                    data.append(1.0)

    if not vocab:
        return {hit_idx: {"ids": [], "distances": []} for hit_idx in usable_hit_refs}

    X = csr_matrix(
        (data, (rows, cols)),
        shape=(len(usable_hit_refs), len(vocab)),
        dtype=float,
    )
    X = sk_normalize(X, norm="l2", copy=False)

    nn = NearestNeighbors(
        n_neighbors=min(k + 1, len(usable_hit_refs)),
        metric="cosine",
    )
    nn.fit(X)
    distances, indices = nn.kneighbors(X)

    out = {}
    for row_idx, (dist_row, ind_row) in enumerate(zip(distances, indices)):
        hit_idx = usable_hit_refs[row_idx]
        ids = []
        dists = []

        for dist, neighbor_row in zip(dist_row, ind_row):
            if neighbor_row == row_idx:
                continue
            ids.append(usable_hit_refs[neighbor_row])
            dists.append(float(dist))

        out[hit_idx] = {"ids": ids[:k], "distances": dists[:k]}

    return out

def _compute_neighbors_meta_onehot_deprecated(*, hits, usable_hit_refs, k, meta_fields = ['meta.parent_tag', 'meta.parent_creators']):
    vocab = {}
    rows = []
    cols = []
    data = []

    
    logger.info(meta_fields)
    for row_idx, hit_idx in enumerate(usable_hit_refs):
        hit = hits[hit_idx]
        for field in meta_fields:
            value = _get_nested(hit, f"_source.{field}")
            if value is None:
                continue

            values = value if isinstance(value, list) else [value]

            for v in values:
                token = f"{field}={v}"
                col_idx = vocab.setdefault(token, len(vocab))
                rows.append(row_idx)
                cols.append(col_idx)
                data.append(1.0)

    logger.info("meta_fields=%r", meta_fields)

    for field in meta_fields:
        sample_value = _get_nested(hits[usable_hit_refs[0]], f"_source.{field}")
        logger.info("field=%r resolved=%r type=%r", field, sample_value, type(sample_value).__name__)

    if not vocab:
        return {hit_idx: {"ids": [], "distances": []} for hit_idx in usable_hit_refs}

    X = csr_matrix((data, (rows, cols)), shape=(len(usable_hit_refs), len(vocab)), dtype=float)
    X = sk_normalize(X, norm="l2", copy=False)

    nn = NearestNeighbors(n_neighbors=min(k + 1, len(usable_hit_refs)), metric="cosine")
    nn.fit(X)
    distances, indices = nn.kneighbors(X)

    out = {}
    for row_idx, (dist_row, ind_row) in enumerate(zip(distances, indices)):
        hit_idx = usable_hit_refs[row_idx]
        ids = []
        dists = []

        for dist, neighbor_row in zip(dist_row, ind_row):
            if neighbor_row == row_idx:
                continue
            ids.append(usable_hit_refs[neighbor_row])
            dists.append(float(dist))

        out[hit_idx] = {"ids": ids[:k], "distances": dists[:k]}

    return out

def _normalize_distance_list(ids, distances):
    if not distances:
        return {}
    dmin = min(distances)
    dmax = max(distances)
    if math.isclose(dmin, dmax):
        return {i: 0.0 for i in ids}
    return {i: (d - dmin) / (dmax - dmin) for i, d in zip(ids, distances)}


def _compute_neighbors_hybrid(
    *,
    hits,
    usable_hit_refs,
    clustering_matrix,
    k,
    metric,
    row_id_field,
    mlt_client,
    mlt_index,
    mlt_fields,
    hybrid_modes,
    hybrid_weights,
    parent_field,
    page_field,
    meta_onehot_fields,
    mlt_min_term_freq,
    mlt_min_doc_freq,
    mlt_max_query_terms,
    mlt_minimum_should_match,
):
    per_mode = {}

    for mode in hybrid_modes:
        per_mode[mode] = _compute_neighbors(
            mode=mode,
            hits=hits,
            usable_hit_refs=usable_hit_refs,
            clustering_matrix=clustering_matrix,
            k=k * 3,
            metric=metric,
            row_id_field=row_id_field,
            mlt_client=mlt_client,
            mlt_index=mlt_index,
            mlt_fields=mlt_fields,
            parent_field=parent_field,
            page_field=page_field,
            meta_onehot_fields=meta_onehot_fields,
            mlt_min_term_freq=mlt_min_term_freq,
            mlt_min_doc_freq=mlt_min_doc_freq,
            mlt_max_query_terms=mlt_max_query_terms,
            mlt_minimum_should_match=mlt_minimum_should_match,
        )

    out = {}

    for hit_idx in usable_hit_refs:
        scores = defaultdict(float)

        for mode, neighbors in per_mode.items():
            payload = neighbors.get(hit_idx, {"ids": [], "distances": []})
            norm = _normalize_distance_list(payload["ids"], payload["distances"])
            weight = float(hybrid_weights.get(mode, 1.0))

            for nid, ndist in norm.items():
                scores[nid] += weight * ndist

        ranked = sorted(scores.items(), key=lambda x: (x[1], x[0]))[:k]
        out[hit_idx] = {
            "ids": [nid for nid, _ in ranked],
            "distances": [float(d) for _, d in ranked],
        }

    return out

def _compute_neighbors(
    *,
    mode: NeighborMode,
    hits: List[Dict[str, Any]],
    usable_hit_refs: List[int],
    clustering_matrix=None,
    k: int = 10,
    metric: str = "cosine",
    row_id_field: Optional[str] = None,
    mlt_client=None,
    mlt_index: Optional[str] = None,
    mlt_fields: Optional[List[str]] = None,
    mlt_min_term_freq: int = 1,
    mlt_min_doc_freq: int = 1,
    mlt_max_query_terms: int = 25,
    mlt_minimum_should_match: str = "30%",
    parent_field: str = "meta.parent",
    page_field: str = "page",
    meta_onehot_fields: Optional[List[str]] = None,
    hybrid_modes: Optional[List[str]] = None,
    hybrid_weights: Optional[Dict[str, float]] = None,
) -> Dict[int, Dict[str, List[Any]]]:
    if mode == "knn_vector":
        return _compute_neighbors_knn(
            clustering_matrix=clustering_matrix,
            usable_hit_refs=usable_hit_refs,
            k=k,
            metric=metric,
        )

    if mode == "mlt":
        return _compute_neighbors_mlt(
            hits=hits,
            usable_hit_refs=usable_hit_refs,
            k=k,
            client=mlt_client,
            index=mlt_index,
            fields=mlt_fields or ["text"],
            min_term_freq=mlt_min_term_freq,
            min_doc_freq=mlt_min_doc_freq,
            max_query_terms=mlt_max_query_terms,
            minimum_should_match=mlt_minimum_should_match,
            row_id_field=row_id_field,
        )

    if mode == "page_parent":
        return _compute_neighbors_page_parent(
            hits=hits,
            usable_hit_refs=usable_hit_refs,
            k=k,
            parent_field=parent_field,
            page_field=page_field,
        )

    if mode == "meta_onehot":
        return _compute_neighbors_meta_onehot(
            hits=hits,
            usable_hit_refs=usable_hit_refs,
            k=k,
            meta_fields=meta_onehot_fields or [],
        )

    if mode == "hybrid":
        return _compute_neighbors_hybrid(
            hits=hits,
            usable_hit_refs=usable_hit_refs,
            clustering_matrix=clustering_matrix,
            k=k,
            metric=metric,
            row_id_field=row_id_field,
            mlt_client=mlt_client,
            mlt_index=mlt_index,
            mlt_fields=mlt_fields or ["text"],
            hybrid_modes=hybrid_modes or ["knn_vector", "mlt"],
            hybrid_weights=hybrid_weights or {"knn_vector": 1.0, "mlt": 1.0},
            parent_field=parent_field,
            page_field=page_field,
            meta_onehot_fields=meta_onehot_fields or [],
            mlt_min_term_freq=mlt_min_term_freq,
            mlt_min_doc_freq=mlt_min_doc_freq,
            mlt_max_query_terms=mlt_max_query_terms,
            mlt_minimum_should_match=mlt_minimum_should_match,
        )

    raise ValueError(f"Unsupported neighbor mode: {mode}")

def cluster_hits_by_analysis(
    hits: List[Dict[str, Any]],
    *,
    analysis: ResultAnalysisParams,
    random_state: int = 42,
    normalize_vectors: bool = None,
    return_vector: bool = False,
    return_projection: bool = None,
    return_neighbors:bool = False,
    mlt_client=None,
    cluster_vectors_by_id: Dict[str, Dict[str, float]] | None = None,
) -> List[Dict[str, Any]]:
    if not hits or not analysis.cluster_enabled:
        return hits
    
    normalize_vectors = normalize_vectors if normalize_vectors is not None else bool(getattr(analysis, "cluster_normalize_vectors", True))
    return_projection = return_projection if return_projection is not None else bool(getattr(analysis, "analysis_return_projection", True))

    hits_copy = [deepcopy(hit) for hit in hits]

    def _set_unclustered(hit: Dict[str, Any]) -> None:
        hit.setdefault("_source", {})
        hit["_source"].setdefault("analysis", {})
        hit["_source"]["analysis"]["cluster"] = {
            "id": None,
            "label": "unclustered",
            "label_terms": [],
            "size": 0,
            "source": analysis.cluster_source,
            "label_source": analysis.cluster_label_source,
        }

    cluster_term_scores_per_hit: List[Dict[str, float]] = [
        _extract_term_scores(
            hit,
            analysis_branch=analysis.cluster_source,
            prefer_vector=True,   # Clustering,
            cluster_vectors_by_id=cluster_vectors_by_id,
        )
        for hit in hits_copy
    ]

    label_term_scores_per_hit: List[Dict[str, float]] = [
        _extract_term_scores(
            hit,
            analysis_branch=analysis.cluster_label_source,
            prefer_vector=False,  # Labels
        )
        for hit in hits_copy
    ]

    usable_indices = [
        i for i, term_scores in enumerate(cluster_term_scores_per_hit) if term_scores
    ]
    usable_index_set = set(usable_indices)

    if not usable_indices:
        for hit in hits_copy:
            _set_unclustered(hit)
        return hits_copy

    vocabulary = sorted(
        {
            term
            for i in usable_indices
            for term in cluster_term_scores_per_hit[i].keys()
        }
    )
    if not vocabulary:
        for hit in hits_copy:
            _set_unclustered(hit)
        return hits_copy

    term_to_col = {term: idx for idx, term in enumerate(vocabulary)}

    rows: List[int] = []
    cols: List[int] = []
    data: List[float] = []
    usable_hit_refs: List[int] = []

    for row_idx, hit_idx in enumerate(usable_indices):
        term_scores = cluster_term_scores_per_hit[hit_idx]
        if not term_scores:
            continue

        added_any = False
        for term, score in term_scores.items():
            score_f = float(score)
            if score_f == 0.0:
                continue
            col_idx = term_to_col.get(term)
            if col_idx is None:
                continue
            rows.append(row_idx)
            cols.append(col_idx)
            data.append(score_f)
            added_any = True

        if added_any:
            usable_hit_refs.append(hit_idx)

    if not usable_hit_refs:
        for hit in hits_copy:
            _set_unclustered(hit)
        return hits_copy

    matrix = csr_matrix(
        (data, (rows, cols)),
        shape=(len(usable_indices), len(vocabulary)),
        dtype=float,
    )

    nonzero_row_mask = matrix.getnnz(axis=1) > 0
    if not nonzero_row_mask.any():
        for hit in hits_copy:
            _set_unclustered(hit)
        return hits_copy

    if not all(nonzero_row_mask):
        kept_row_indices = [i for i, keep in enumerate(nonzero_row_mask) if keep]
        matrix = matrix[kept_row_indices]
        usable_hit_refs = [usable_indices[i] for i in kept_row_indices]
    else:
        usable_hit_refs = list(usable_indices)

    if normalize_vectors:
        matrix = sk_normalize(matrix, norm="l2", copy=False)

    effective_k = max(1, min(int(analysis.cluster_count), matrix.shape[0]))
    hit_index_to_row = {
        hit_idx: row_idx for row_idx, hit_idx in enumerate(usable_hit_refs)
    }

    if matrix.shape[0] == 1:
        only_idx = usable_hit_refs[0]
        label, label_terms = _build_cluster_label(
            cluster_term_scores=[label_term_scores_per_hit[only_idx]],
            all_term_scores=label_term_scores_per_hit,
            top_label_terms=analysis.cluster_label_top_terms,
        )

        for i, hit in enumerate(hits_copy):
            hit.setdefault("_source", {})
            hit["_source"].setdefault("analysis", {})
            if i == only_idx:
                hit["_source"]["analysis"]["cluster"] = {
                    "id": 0,
                    "label": label,
                    "label_terms": label_terms,
                    "size": 1,
                    "source": analysis.cluster_source,
                    "label_source": analysis.cluster_label_source,
                }
            else:
                _set_unclustered(hit)
        return hits_copy

    clustering_matrix = matrix
    use_svd = getattr(analysis, "cluster_use_svd", True)

    if use_svd and matrix.shape[0] > 1 and matrix.shape[1] > 1:
        requested_components = int(getattr(analysis, "cluster_svd_components", 100))
        max_components = min(matrix.shape[0] - 1, matrix.shape[1] - 1)
        n_components = max(1, min(requested_components, max_components))

        if n_components >= 1:
            svd = TruncatedSVD(
                n_components=n_components,
                random_state=random_state,
            )
            clustering_matrix = svd.fit_transform(matrix)
            # logger.info(clustering_matrix)

            if normalize_vectors:
                clustering_matrix = sk_normalize(
                    clustering_matrix,
                    norm="l2",
                    copy=False,
                )
    
    logger.debug(clustering_matrix)
    kmeans = KMeans(
        n_clusters=effective_k,
        random_state=random_state,
        n_init="auto",
    )
    labels = kmeans.fit_predict(clustering_matrix)

    neighbors_per_hit = None

    if return_neighbors:
        neighbors_per_hit = _compute_neighbors(
            mode=getattr(analysis, "neighbors_mode", "knn_vector"),
            hits=hits_copy,
            usable_hit_refs=usable_hit_refs,
            clustering_matrix=clustering_matrix,
            k=int(getattr(analysis, "neighbors_k", 10)),
            metric=getattr(analysis, "neighbors_metric", "cosine"),
            row_id_field=getattr(analysis, "neighbors_row_id_field", None),

            # MLT
            mlt_client=mlt_client,
            mlt_index=getattr(analysis, "neighbors_mlt_index", None),
            mlt_fields=getattr(analysis, "neighbors_mlt_fields", ["text"]),
            mlt_min_term_freq=int(getattr(analysis, "neighbors_mlt_min_term_freq", 1)),
            mlt_min_doc_freq=int(getattr(analysis, "neighbors_mlt_min_doc_freq", 1)),
            mlt_max_query_terms=int(getattr(analysis, "neighbors_mlt_max_query_terms", 25)),
            mlt_minimum_should_match=str(getattr(analysis, "neighbors_mlt_minimum_should_match", "30%")),

            # Meta
            parent_field=getattr(analysis, "neighbors_parent_field", "meta.parent"),
            page_field=getattr(analysis, "neighbors_page_field", "page"),
            meta_onehot_fields=getattr(analysis, "neighbors_meta_onehot_fields", []), # ['meta.parent_tag', 'meta.parent_creators']

            # Hybrid
            hybrid_modes=getattr(analysis, "neighbors_hybrid_modes", ["knn_vector", "mlt"]),
            hybrid_weights=getattr(
                analysis,
                "neighbors_hybrid_weights",
                {"knn_vector": 1.0, "mlt": 1.0},
            ),
        )


    cluster_docs: Dict[int, List[int]] = defaultdict(list)
    for row_idx, cluster_id in enumerate(labels):
        hit_idx = usable_hit_refs[row_idx]
        cluster_docs[int(cluster_id)].append(hit_idx)

    cluster_meta: Dict[int, Dict[str, Any]] = {}
    for cluster_id, hit_indices in cluster_docs.items():
        cluster_label_scores = [label_term_scores_per_hit[i] for i in hit_indices]
        label, label_terms = _build_cluster_label(
            cluster_term_scores=cluster_label_scores,
            all_term_scores=label_term_scores_per_hit,
            top_label_terms=analysis.cluster_label_top_terms,
        )
        cluster_meta[cluster_id] = {
            "id": int(cluster_id),
            "label": label,
            "label_terms": label_terms,
            "size": len(hit_indices),
            "source": analysis.cluster_source,
            "label_source": analysis.cluster_label_source,
        }

    if return_projection:
        projection_2d, projection_method = _compute_projection_2d(
            clustering_matrix=clustering_matrix,
            method=getattr(analysis, "cluster_projection_method", "umap"),
            random_state=random_state,
        )


    for i, hit in enumerate(hits_copy):
        hit.setdefault("_source", {})
        hit["_source"].setdefault("analysis", {})

        if i not in usable_index_set or i not in hit_index_to_row:
            _set_unclustered(hit)
            continue

        row_idx = hit_index_to_row[i]
        cluster_id = int(labels[row_idx])
        if return_vector:
            row_vector = clustering_matrix[row_idx]

            if hasattr(row_vector, "toarray"):  # sparse row
                row_vector = row_vector.toarray().ravel()
            else:  # ndarray row
                row_vector = row_vector.ravel()
            
            cluster_result = {
                **cluster_meta[cluster_id],
                "vector": row_vector.tolist(),
            }
        else:
            cluster_result = cluster_meta[cluster_id]

        if return_projection:
                hit["_source"]["analysis"]["projection"] = {
                    "x": float(projection_2d[row_idx][0]),
                    "y": float(projection_2d[row_idx][1]),
                    "method": projection_method,
                }

        hit["_source"]["analysis"]["cluster"] = cluster_result
        if neighbors_per_hit is not None:
            hit["_source"]["analysis"]["neighbors"] = neighbors_per_hit.get(
                i,
                {"ids": [], "distances": []},
            )

    return hits_copy

def _compute_projection_2d(
    clustering_matrix,
    method: str = "pca",
    random_state: int | None = None,
) -> tuple[list[list[float]], str]:
    import numpy as np

    if clustering_matrix is None:
        return [], method

    if hasattr(clustering_matrix, "toarray"):
        projection_input = clustering_matrix.toarray()
    else:
        projection_input = np.asarray(clustering_matrix)

    n_samples = projection_input.shape[0]

    if n_samples == 0:
        return [], method

    if n_samples == 1:
        return [[0.0, 0.0]], method

    method = (method or "pca").lower()

    if method == "pca":
        from sklearn.decomposition import PCA

        model = PCA(n_components=2, random_state=random_state)
        projection = model.fit_transform(projection_input)

    elif method == "tsne":
        from sklearn.manifold import TSNE

        perplexity = min(30, max(1, n_samples - 1))

        model = TSNE(
            n_components=2,
            random_state=random_state,
            init="pca",
            learning_rate="auto",
            perplexity=perplexity,
        )
        projection = model.fit_transform(projection_input)

    elif method == "umap":
        ensure_import("umap-learn", requirements=None)
        import umap

        n_neighbors = min(15, max(2, n_samples - 1))

        model = umap.UMAP(
            n_components=2,
            random_state=random_state,
            n_neighbors=n_neighbors,
        )
        projection = model.fit_transform(projection_input)

    else:
        raise ValueError(f"Unsupported projection method: {method}")

    projection = np.asarray(projection, dtype=float)

    return projection.tolist(), method
