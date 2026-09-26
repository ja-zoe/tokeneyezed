"""The brief's vector-search filters must be declared as filter fields in the index definitions."""

from tokeneyezed.data.brief import failed_attempts_pipeline, memory_pipeline, skills_pipeline
from tokeneyezed.data.embeddings import DIMENSION
from tokeneyezed.data.indexes import INDEXES, filter_paths

VECTOR = [0.1] * DIMENSION


def vector_stage(pipeline):
    return next(s["$vectorSearch"] for s in pipeline if "$vectorSearch" in s)


def declared(collection: str, index: str) -> set[str]:
    return filter_paths(INDEXES[collection][index][1])


def test_attempts_vector_declares_every_filtered_field() -> None:
    fusion = failed_attempts_pipeline("s", "g", "x", VECTOR)[0]["$rankFusion"]
    stage = vector_stage(fusion["input"]["pipelines"]["vector"])
    assert set(stage["filter"]) <= declared("attempts", stage["index"])


def test_memory_and_skills_vector_declare_every_filtered_field() -> None:
    for collection, pipeline in (
        ("memory", memory_pipeline("s", VECTOR)),
        ("skills", skills_pipeline(VECTOR)),
    ):
        stage = vector_stage(pipeline)
        assert set(stage.get("filter", {})) <= declared(collection, stage["index"])


def test_vector_indexes_match_the_pinned_embedding_dimension() -> None:
    for indexes in INDEXES.values():
        for kind, definition in indexes.values():
            if kind == "vectorSearch":
                (vec,) = (f for f in definition["fields"] if f["type"] == "vector")
                assert vec["numDimensions"] == DIMENSION and vec["similarity"] == "cosine"
