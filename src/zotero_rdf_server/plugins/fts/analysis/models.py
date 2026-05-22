from pydantic import BaseModel, Field

from typing import Annotated, Literal, Optional, Tuple
from fastapi import Query
import json

class ResultAnalysisParams(BaseModel):
    perform_analysis: bool = Field(
        default=False,
        description="If true, perform NLP analysis on hits.",
    )
    analyze_field: Optional[str] = Field(
        default=None,
        description="Field from each hit used for per-document keyword extraction. Defaults to the search field.",
    )
    analysis_mode: Literal["index_documents", "hits_documents", "both"] = Field(
        default="both",
        description="TF-IDF mode: index_documents, hits_documents, or both.",
    )
    analyze_top_terms: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of top TF-IDF terms per hit.",
    )
    analyze_min_token_length: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Minimum token length.",
    )
    analyze_max_tokens_per_doc: int = Field(
        default=0,
        ge=0,
        le=5000,
        description="Maximum number of tokens kept per document after tokenization. 0 means unlimited.",
    )
    analyze_use_char_ngrams: bool = Field(
        default=True,
        description="Use character n-grams instead of word tokens for TF-IDF.",
    )
    analyze_char_ngram_range: Tuple[int, int] = Field(
        default=(3, 5),
        description="Character n-gram range (min_n, max_n).",
    )
    analyze_tfidf_max_features: Optional[int] = Field(
        default=None,
        description="Maximum number of TF-IDF features.",
    )
    analyze_tfidf_min_df: int = Field(
        default=1,
        ge=1,
        description="Minimum document frequency for TF-IDF.",
    )
    analyze_tfidf_max_df: float = Field(
        default=1.0,
        gt=0.0,
        le=1.0,
        description="Maximum document frequency for TF-IDF.",
    )

    cluster_enabled: bool = Field(
        default=True,
        description="If true, cluster hits based on analysis vectors.",
    )
    cluster_count: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Requested number of clusters for KMeans.",
    )
    cluster_source: Literal["hits_documents", "index_documents"] = Field(
        default="hits_documents",
        description="Which analysis branch to use for clustering.",
    )
    cluster_label_source: Literal["hits_documents", "index_documents"] = Field(
        default="hits_documents",
        description="Which analysis branch to use for cluster labels.",
    )
    cluster_use_svd: bool = Field(
        default=True,
        description="Apply TruncatedSVD before clustering.",
    )
    cluster_svd_components: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of SVD components.",
    )
    cluster_label_top_terms: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Number of terms used to build the cluster label.",
    )
    cluster_projection_method: Literal["umap", "tsne", "pca"] = Field(
        default="umap",
        description="Which projection method.",
    )

    projection_source: Literal["cluster_matrix", "meta_onehot", "neighbors"] = Field(
        default="cluster_matrix",
        description=(
            "Source used for 2D projection. "
            "'cluster_matrix' uses the existing clustering matrix, "
            "'meta_onehot' builds a one-hot meta matrix, "
            "'neighbors' builds a projection from neighbor distances."
        ),
    )
    projection_neighbors_mode: Literal[
        "knn_vector",
        "os_knn",
        "mlt",
        "page_parent",
        "meta_onehot",
        "hybrid",
    ] = Field(
        default="knn_vector",
        description="Neighbor mode used when projection_source='neighbors'.",
    )
    projection_distance_fill_value: float = Field(
        default=1.0,
        ge=0.0,
        description="Fallback distance used for missing neighbor pairs in projection distance matrices.",
    )
    projection_distance_symmetrize: bool = Field(
        default=True,
        description="If true, symmetrize the projection distance matrix.",
    )

    neighbors_enabled: bool = Field(
        default=False,
        description="If true, compute neighbors for each hit.",
    )
    neighbors_mode: Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot", "hybrid"] = Field(
        default="knn_vector",
        description="Neighbor computation mode.",
    )
    neighbors_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of neighbors per hit.",
    )
    neighbors_metric: Literal["cosine", "euclidean"] = Field(
        default="cosine",
        description="Distance metric for knn_vector mode.",
    )

    neighbors_row_id_field: Optional[str] = Field(
        default=None,
        description="Field path used as external document id for MLT/os_knn mode. Defaults to hit['_id'].",
    )
    neighbors_mlt_index: Optional[str] = Field(
        default=None,
        description="OpenSearch index used for MLT mode.",
    )
    neighbors_mlt_fields: list[str] = Field(
        default_factory=lambda: ["text"],
        description="Fields used for MLT mode.",
    )
    neighbors_mlt_min_term_freq: int = Field(
        default=1,
        ge=0,
        le=100,
        description="MLT min_term_freq.",
    )
    neighbors_mlt_min_doc_freq: int = Field(
        default=1,
        ge=0,
        le=100,
        description="MLT min_doc_freq.",
    )
    neighbors_mlt_max_query_terms: int = Field(
        default=25,
        ge=1,
        le=100,
        description="MLT max_query_terms.",
    )
    neighbors_mlt_minimum_should_match: str = Field(
        default="30%",
        description="MLT minimum_should_match.",
    )

    neighbors_os_knn_index: Optional[str] = Field(
        default=None,
        description="OpenSearch index used for os_knn mode.",
    )
    neighbors_os_knn_vector_field: str = Field(
        default="vector",
        description="OpenSearch knn_vector field used for os_knn mode.",
    )
    neighbors_os_knn_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=10000,
        description="k parameter for OpenSearch kNN query. Falls back to neighbors_k.",
    )
    neighbors_os_knn_ef_search: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional ef_search for OpenSearch kNN mode.",
    )

    neighbors_parent_field: str = Field(
        default="meta.parent",
        description="Parent field used for page_parent mode.",
    )
    neighbors_page_field: str = Field(
        default="page",
        description="Page field used for page_parent mode.",
    )
    neighbors_meta_onehot_key: str = Field(
        default="meta",
        description="Meta key used for meta_onehot mode. Empty means meta key in _source.",
    )
    neighbors_meta_onehot_fields: list[str] = Field(
        default_factory=list,
        description="Meta fields used for meta_onehot mode. Empty means all meta fields.",
    )

    neighbors_hybrid_modes: list[Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot"]] = Field(
        default_factory=lambda: ["knn_vector", "mlt"],
        description="Neighbor modes combined when neighbors_mode='hybrid'.",
    )
    neighbors_hybrid_weights: dict[str, float] = Field(
        default_factory=lambda: {"knn_vector": 1.0, "mlt": 1.0},
        description="Per-mode weights for hybrid neighbor fusion.",
    )

def get_result_analysis_params_legacy(
    perform_analysis: Annotated[
        bool,
        Query(description="Perform NLP analysis on hits."),
    ] = False,
    analyze_field: Annotated[
        Optional[str],
        Query(description="Field used for per-hit TF-IDF analysis. Defaults to the search field."),
    ] = None,
    analysis_mode: Annotated[
        Literal["index_documents", "hits_documents", "both"],
        Query(description="TF-IDF mode: index_documents, hits_documents, or both."),
    ] = "both",
    analyze_top_terms: Annotated[
        int,
        Query(description="Number of top TF-IDF terms per hit.", ge=1, le=20),
    ] = 5,
    analyze_min_token_length: Annotated[
        int,
        Query(description="Minimum token length.", ge=1, le=50),
    ] = 4,
    analyze_max_tokens_per_doc: Annotated[
        int,
        Query(description="Maximum number of tokens per document. 0 means unlimited.", ge=0, le=5000),
    ] = 0,
    analyze_use_char_ngrams: Annotated[
        bool,
        Query(description="Use character n-grams instead of word tokens."),
    ] = True,
    analyze_char_ngram_min: Annotated[
        int,
        Query(description="Min n for char n-grams.", ge=1, le=10),
    ] = 3,
    analyze_char_ngram_max: Annotated[
        int,
        Query(description="Max n for char n-grams.", ge=1, le=10),
    ] = 5,
    analyze_tfidf_max_features: Annotated[
        Optional[int],
        Query(description="Max TF-IDF features.", ge=1),
    ] = None,
    analyze_tfidf_min_df: Annotated[
        int,
        Query(description="Min document frequency.", ge=1),
    ] = 1,
    analyze_tfidf_max_df: Annotated[
        float,
        Query(description="Max document frequency.", gt=0.0, le=1.0),
    ] = 1.0,

    cluster_enabled: Annotated[
        bool,
        Query(description="Cluster hits based on analysis vectors."),
    ] = False,
    cluster_count: Annotated[
        int,
        Query(description="Requested number of clusters for KMeans.", ge=1, le=50),
    ] = 5,
    cluster_source: Annotated[
        Literal["hits_documents", "index_documents"],
        Query(description="Which analysis branch to use for clustering."),
    ] = "hits_documents",
    cluster_label_source: Annotated[
        Literal["hits_documents", "index_documents"],
        Query(description="Which analysis branch to use for cluster labels."),
    ] = "hits_documents",
    cluster_use_svd: Annotated[
        bool,
        Query(description="Apply SVD before clustering."),
    ] = True,
    cluster_svd_components: Annotated[
        int,
        Query(description="Number of SVD components.", ge=1, le=1000),
    ] = 100,
    cluster_label_top_terms: Annotated[
        int,
        Query(description="Number of terms used for cluster labels.", ge=1, le=100),
    ] = 3,
    cluster_projection_method: Annotated[
        Literal["umap", "tsne", "pca"],
        Query(description="Which projection method for projection."),
    ] = "umap",

    projection_source: Annotated[
        Literal["cluster_matrix", "meta_onehot", "neighbors"],
        Query(description="Projection source: cluster_matrix, meta_onehot, or neighbors."),
    ] = "cluster_matrix",
    projection_neighbors_mode: Annotated[
        Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot", "hybrid"],
        Query(description="Neighbor mode used when projection_source='neighbors'."),
    ] = "knn_vector",
    projection_distance_fill_value: Annotated[
        float,
        Query(description="Fallback distance for missing neighbor pairs in projection distance matrices.", ge=0.0),
    ] = 1.0,
    projection_distance_symmetrize: Annotated[
        bool,
        Query(description="Symmetrize projection distance matrix."),
    ] = True,

    neighbors_enabled: Annotated[
        bool,
        Query(description="Compute neighbors for each hit."),
    ] = False,
    neighbors_mode: Annotated[
        Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot", "hybrid"],
        Query(description="Neighbor computation mode."),
    ] = "knn_vector",
    neighbors_k: Annotated[
        int,
        Query(description="Number of neighbors per hit.", ge=1, le=100),
    ] = 10,
    neighbors_metric: Annotated[
        Literal["cosine", "euclidean"],
        Query(description="Distance metric for knn_vector mode."),
    ] = "cosine",
    neighbors_row_id_field: Annotated[
        Optional[str],
        Query(description="Field path used as external id for MLT/os_knn mode. Defaults to _id."),
    ] = None,
    neighbors_mlt_index: Annotated[
        Optional[str],
        Query(description="OpenSearch index used for MLT mode."),
    ] = None,
    neighbors_mlt_fields: Annotated[
        list[str],
        Query(description="Fields used for MLT mode."),
    ] = ["text"],
    neighbors_mlt_min_term_freq: Annotated[
        int,
        Query(description="MLT min_term_freq.", ge=0, le=100),
    ] = 1,
    neighbors_mlt_min_doc_freq: Annotated[
        int,
        Query(description="MLT min_doc_freq.", ge=0, le=100),
    ] = 1,
    neighbors_mlt_max_query_terms: Annotated[
        int,
        Query(description="MLT max_query_terms.", ge=1, le=100),
    ] = 25,
    neighbors_mlt_minimum_should_match: Annotated[
        str,
        Query(description="MLT minimum_should_match."),
    ] = "30%",

    neighbors_os_knn_index: Annotated[
        Optional[str],
        Query(description="OpenSearch index used for os_knn mode."),
    ] = None,
    neighbors_os_knn_vector_field: Annotated[
        str,
        Query(description="OpenSearch knn_vector field used for os_knn mode."),
    ] = "vector",
    neighbors_os_knn_k: Annotated[
        Optional[int],
        Query(description="k parameter for OpenSearch kNN query. Falls back to neighbors_k.", ge=1, le=10000),
    ] = None,
    neighbors_os_knn_ef_search: Annotated[
        Optional[int],
        Query(description="Optional ef_search for OpenSearch kNN mode.", ge=1),
    ] = None,

    neighbors_parent_field: Annotated[
        str,
        Query(description="Parent field used for page_parent mode."),
    ] = "meta.parent",
    neighbors_page_field: Annotated[
        str,
        Query(description="Page field used for page_parent mode."),
    ] = "page",
    neighbors_meta_onehot_key: Annotated[
        str,
        Query(description="Meta key used for meta_onehot mode. Empty means all meta key in _source."),
    ] = "meta",
    neighbors_meta_onehot_fields: Annotated[
        list[str],
        Query(description="Meta fields used for meta_onehot mode. Empty means all meta fields."),
    ] = [],

    neighbors_hybrid_modes: Annotated[
        list[Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot"]],
        Query(description="Neighbor modes combined when neighbors_mode='hybrid'."),
    ] = ["knn_vector", "mlt"],
    # neighbors_hybrid_weights: Annotated[
    #     dict[str, float],
    #     Query(description="Per-mode weights for hybrid neighbor fusion."),
    # ] = {"knn_vector": 1.0, "mlt": 1.0},
) -> ResultAnalysisParams:
    return ResultAnalysisParams(
        perform_analysis=perform_analysis,
        analyze_field=analyze_field,
        analysis_mode=analysis_mode,
        analyze_top_terms=analyze_top_terms,
        analyze_min_token_length=analyze_min_token_length,
        analyze_max_tokens_per_doc=analyze_max_tokens_per_doc,
        analyze_use_char_ngrams=analyze_use_char_ngrams,
        analyze_char_ngram_range=(analyze_char_ngram_min, analyze_char_ngram_max),
        analyze_tfidf_max_features=analyze_tfidf_max_features,
        analyze_tfidf_min_df=analyze_tfidf_min_df,
        analyze_tfidf_max_df=analyze_tfidf_max_df,

        cluster_enabled=cluster_enabled,
        cluster_count=cluster_count,
        cluster_source=cluster_source,
        cluster_label_source=cluster_label_source,
        cluster_label_top_terms=cluster_label_top_terms,
        cluster_use_svd=cluster_use_svd,
        cluster_svd_components=cluster_svd_components,
        cluster_projection_method=cluster_projection_method,

        projection_source=projection_source,
        projection_neighbors_mode=projection_neighbors_mode,
        projection_distance_fill_value=projection_distance_fill_value,
        projection_distance_symmetrize=projection_distance_symmetrize,

        neighbors_enabled=neighbors_enabled,
        neighbors_mode=neighbors_mode,
        neighbors_k=neighbors_k,
        neighbors_metric=neighbors_metric,
        neighbors_row_id_field=neighbors_row_id_field,

        neighbors_mlt_index=neighbors_mlt_index,
        neighbors_mlt_fields=neighbors_mlt_fields,
        neighbors_mlt_min_term_freq=neighbors_mlt_min_term_freq,
        neighbors_mlt_min_doc_freq=neighbors_mlt_min_doc_freq,
        neighbors_mlt_max_query_terms=neighbors_mlt_max_query_terms,
        neighbors_mlt_minimum_should_match=neighbors_mlt_minimum_should_match,

        neighbors_os_knn_index=neighbors_os_knn_index,
        neighbors_os_knn_vector_field=neighbors_os_knn_vector_field,
        neighbors_os_knn_k=neighbors_os_knn_k,
        neighbors_os_knn_ef_search=neighbors_os_knn_ef_search,

        neighbors_parent_field=neighbors_parent_field,
        neighbors_page_field=neighbors_page_field,
        neighbors_meta_onehot_key=neighbors_meta_onehot_key,
        neighbors_meta_onehot_fields=neighbors_meta_onehot_fields,

        neighbors_hybrid_modes=neighbors_hybrid_modes,
        # neighbors_hybrid_weights=neighbors_hybrid_weights,
    )

def get_result_analysis_params(
    perform_analysis: Annotated[
        bool,
        Query(description="Perform NLP analysis on hits."),
    ] = False,
    analyze_field: Annotated[
        Optional[str],
        Query(description="Field used for per-hit TF-IDF analysis. Defaults to the search field."),
    ] = None,
    analysis_mode: Annotated[
        Literal["index_documents", "hits_documents", "both"],
        Query(description="TF-IDF mode: index_documents, hits_documents, or both."),
    ] = "both",
    analyze_top_terms: Annotated[
        int,
        Query(description="Number of top TF-IDF terms per hit.", ge=1, le=20),
    ] = 5,
    analyze_min_token_length: Annotated[
        int,
        Query(description="Minimum token length.", ge=1, le=50),
    ] = 4,
    analyze_max_tokens_per_doc: Annotated[
        int,
        Query(description="Maximum number of tokens per document. 0 means unlimited.", ge=0, le=5000),
    ] = 0,
    analyze_use_char_ngrams: Annotated[
        bool,
        Query(description="Use character n-grams instead of word tokens."),
    ] = True,
    analyze_char_ngram_min: Annotated[
        int,
        Query(description="Min n for char n-grams.", ge=1, le=10),
    ] = 3,
    analyze_char_ngram_max: Annotated[
        int,
        Query(description="Max n for char n-grams.", ge=1, le=10),
    ] = 5,
    analyze_tfidf_max_features: Annotated[
        Optional[int],
        Query(description="Max TF-IDF features.", ge=1),
    ] = None,
    analyze_tfidf_min_df: Annotated[
        int,
        Query(description="Min document frequency.", ge=1),
    ] = 1,
    analyze_tfidf_max_df: Annotated[
        float,
        Query(description="Max document frequency.", gt=0.0, le=1.0),
    ] = 1.0,

    cluster_enabled: Annotated[
        bool,
        Query(description="Cluster hits based on analysis vectors."),
    ] = False,
    cluster_count: Annotated[
        int,
        Query(description="Requested number of clusters for KMeans.", ge=1, le=50),
    ] = 5,
    cluster_source: Annotated[
        Literal["hits_documents", "index_documents"],
        Query(description="Which analysis branch to use for clustering."),
    ] = "hits_documents",
    cluster_label_source: Annotated[
        Literal["hits_documents", "index_documents"],
        Query(description="Which analysis branch to use for cluster labels."),
    ] = "hits_documents",
    cluster_use_svd: Annotated[
        bool,
        Query(description="Apply SVD before clustering."),
    ] = True,
    cluster_svd_components: Annotated[
        int,
        Query(description="Number of SVD components.", ge=1, le=1000),
    ] = 100,
    cluster_label_top_terms: Annotated[
        int,
        Query(description="Number of terms used for cluster labels.", ge=1, le=100),
    ] = 3,
    cluster_projection_method: Annotated[
        Literal["umap", "tsne", "pca"],
        Query(description="Which projection method for projection."),
    ] = "umap",

    projection_source: Annotated[
        Literal["cluster_matrix", "meta_onehot", "neighbors"],
        Query(description="Projection source: cluster_matrix, meta_onehot, or neighbors."),
    ] = "cluster_matrix",
    projection_neighbors_mode: Annotated[
        Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot", "hybrid"],
        Query(description="Neighbor mode used when projection_source='neighbors'."),
    ] = "knn_vector",
    projection_distance_fill_value: Annotated[
        float,
        Query(description="Fallback distance for missing neighbor pairs in projection distance matrices.", ge=0.0),
    ] = 1.0,
    projection_distance_symmetrize: Annotated[
        bool,
        Query(description="Symmetrize projection distance matrix."),
    ] = True,

    neighbors_enabled: Annotated[
        bool,
        Query(description="Compute neighbors for each hit."),
    ] = False,
    neighbors_mode: Annotated[
        Literal["knn_vector", "os_knn", "mlt", "page_parent", "meta_onehot", "hybrid"],
        Query(description="Neighbor computation mode."),
    ] = "knn_vector",
    neighbors_k: Annotated[
        int,
        Query(description="Number of neighbors per hit.", ge=1, le=100),
    ] = 10,
    neighbors_metric: Annotated[
        Literal["cosine", "euclidean"],
        Query(description="Distance metric for knn_vector mode."),
    ] = "cosine",
    neighbors_row_id_field: Annotated[
        Optional[str],
        Query(description="Field path used as external id for MLT/os_knn mode. Defaults to _id."),
    ] = None,
    neighbors_mlt_index: Annotated[
        Optional[str],
        Query(description="OpenSearch index used for MLT mode."),
    ] = None,
    neighbors_mlt_fields: Annotated[
        Optional[list[str]],
        Query(description="Fields used for MLT mode."),
    ] = None,
    neighbors_mlt_min_term_freq: Annotated[
        int,
        Query(description="MLT min_term_freq.", ge=0, le=100),
    ] = 1,
    neighbors_mlt_min_doc_freq: Annotated[
        int,
        Query(description="MLT min_doc_freq.", ge=0, le=100),
    ] = 1,
    neighbors_mlt_max_query_terms: Annotated[
        int,
        Query(description="MLT max_query_terms.", ge=1, le=100),
    ] = 25,
    neighbors_mlt_minimum_should_match: Annotated[
        str,
        Query(description="MLT minimum_should_match."),
    ] = "30%",

    neighbors_os_knn_index: Annotated[
        Optional[str],
        Query(description="OpenSearch index used for os_knn mode."),
    ] = None,
    neighbors_os_knn_vector_field: Annotated[
        str,
        Query(description="OpenSearch knn_vector field used for os_knn mode."),
    ] = "vector",
    neighbors_os_knn_k: Annotated[
        Optional[int],
        Query(description="k parameter for OpenSearch kNN query. Falls back to neighbors_k.", ge=1, le=10000),
    ] = None,
    neighbors_os_knn_ef_search: Annotated[
        Optional[int],
        Query(description="Optional ef_search for OpenSearch kNN mode.", ge=1),
    ] = None,

    neighbors_parent_field: Annotated[
        str,
        Query(description="Parent field used for page_parent mode."),
    ] = "meta.parent",
    neighbors_page_field: Annotated[
        str,
        Query(description="Page field used for page_parent mode."),
    ] = "page",
    neighbors_meta_onehot_key: Annotated[
        Optional[str],
        Query(description="Meta key used for meta_onehot mode. Empty means meta key in _source."),
    ] = "meta",
    neighbors_meta_onehot_fields: Annotated[
        Optional[list[str]],
        Query(description="Meta fields used for meta_onehot mode. Empty means all meta fields."),
    ] = None,

    neighbors_hybrid_modes: Annotated[
        Optional[list[str]],
        Query(description="Neighbor modes combined when neighbors_mode='hybrid'."),
    ] = None,
    neighbors_hybrid_weights_json: Annotated[
        Optional[str],
        Query(description='JSON object with per-mode weights, e.g. {"knn_vector": 1.0, "mlt": 1.0}'),
    ] = None,
) -> ResultAnalysisParams:
    parsed_hybrid_weights = {"knn_vector": 1.0, "mlt": 1.0}
    if neighbors_hybrid_weights_json:
        parsed_hybrid_weights = json.loads(neighbors_hybrid_weights_json)

    parsed_hybrid_modes = neighbors_hybrid_modes or ["knn_vector", "mlt"]

    return ResultAnalysisParams(
        perform_analysis=perform_analysis,
        analyze_field=analyze_field,
        analysis_mode=analysis_mode,
        analyze_top_terms=analyze_top_terms,
        analyze_min_token_length=analyze_min_token_length,
        analyze_max_tokens_per_doc=analyze_max_tokens_per_doc,
        analyze_use_char_ngrams=analyze_use_char_ngrams,
        analyze_char_ngram_range=(analyze_char_ngram_min, analyze_char_ngram_max),
        analyze_tfidf_max_features=analyze_tfidf_max_features,
        analyze_tfidf_min_df=analyze_tfidf_min_df,
        analyze_tfidf_max_df=analyze_tfidf_max_df,

        cluster_enabled=cluster_enabled,
        cluster_count=cluster_count,
        cluster_source=cluster_source,
        cluster_label_source=cluster_label_source,
        cluster_label_top_terms=cluster_label_top_terms,
        cluster_use_svd=cluster_use_svd,
        cluster_svd_components=cluster_svd_components,
        cluster_projection_method=cluster_projection_method,

        projection_source=projection_source,
        projection_neighbors_mode=projection_neighbors_mode,
        projection_distance_fill_value=projection_distance_fill_value,
        projection_distance_symmetrize=projection_distance_symmetrize,

        neighbors_enabled=neighbors_enabled,
        neighbors_mode=neighbors_mode,
        neighbors_k=neighbors_k,
        neighbors_metric=neighbors_metric,
        neighbors_row_id_field=neighbors_row_id_field,

        neighbors_mlt_index=neighbors_mlt_index,
        neighbors_mlt_fields=neighbors_mlt_fields or ["text"],
        neighbors_mlt_min_term_freq=neighbors_mlt_min_term_freq,
        neighbors_mlt_min_doc_freq=neighbors_mlt_min_doc_freq,
        neighbors_mlt_max_query_terms=neighbors_mlt_max_query_terms,
        neighbors_mlt_minimum_should_match=neighbors_mlt_minimum_should_match,

        neighbors_os_knn_index=neighbors_os_knn_index,
        neighbors_os_knn_vector_field=neighbors_os_knn_vector_field,
        neighbors_os_knn_k=neighbors_os_knn_k,
        neighbors_os_knn_ef_search=neighbors_os_knn_ef_search,

        neighbors_parent_field=neighbors_parent_field,
        neighbors_page_field=neighbors_page_field,
        neighbors_meta_onehot_key=neighbors_meta_onehot_key or "meta",
        neighbors_meta_onehot_fields=neighbors_meta_onehot_fields or [],

        neighbors_hybrid_modes=parsed_hybrid_modes,
        neighbors_hybrid_weights=parsed_hybrid_weights,
    )