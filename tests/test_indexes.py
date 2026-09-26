"""The brief's vector-search filters must be declared as filter fields in the index definitions."""

from tokeneyezed.data.brief import failed_attempts_pipeline, memory_pipeline, skills_pipeline
from tokeneyezed.data.embeddings import DIMENSION
from tokeneyezed.data.indexes import INDEXES, ensure_search_indexes, filter_paths, normalized

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
        ("skills", skills_pipeline("s", VECTOR)),
    ):
        stage = vector_stage(pipeline)
        assert set(stage.get("filter", {})) <= declared(collection, stage["index"])


def test_vector_indexes_match_the_pinned_embedding_dimension() -> None:
    for indexes in INDEXES.values():
        for kind, definition in indexes.values():
            if kind == "vectorSearch":
                (vec,) = (f for f in definition["fields"] if f["type"] == "vector")
                assert vec["numDimensions"] == DIMENSION and vec["similarity"] == "cosine"


# Definitions in the shape Atlas returns from list_search_indexes() (with its defaults).
LIVE_ATTEMPTS_TEXT = {
    "mappings": {
        "dynamic": False,
        "fields": {"intent": {"type": "string"}, "diff_summary": {"type": "string"}},
    },
    "analyzer": "lucene.standard",
}
LIVE_SKILLS_VECTOR = {
    "fields": [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": DIMENSION,
            "similarity": "cosine",
            "quantization": "none",
        },
        {"type": "filter", "path": "session_id"},
    ]
}


def test_atlas_defaults_are_not_mistaken_for_drift() -> None:
    kind, code = INDEXES["attempts"]["attempts_text"]
    assert normalized(kind, LIVE_ATTEMPTS_TEXT) == normalized(kind, code)
    kind, code = INDEXES["skills"]["skills_vector"]
    assert normalized(kind, LIVE_SKILLS_VECTOR) == normalized(kind, code)


def test_a_real_change_is_still_drift() -> None:
    kind, code = INDEXES["skills"]["skills_vector"]
    changed = {"fields": [{**LIVE_SKILLS_VECTOR["fields"][0], "numDimensions": 512}]}
    assert normalized(kind, changed) != normalized(kind, code)
    kind, code = INDEXES["attempts"]["attempts_text"]
    assert normalized(kind, {**LIVE_ATTEMPTS_TEXT, "analyzer": "lucene.english"}) != normalized(
        kind, code
    )


class FakeSearchCollection:
    def __init__(self, live: list[dict]) -> None:
        self.live, self.updates, self.creates = live, [], []

    def list_search_indexes(self) -> list[dict]:
        return self.live

    def update_search_index(self, name: str, definition: dict) -> None:
        self.updates.append(name)

    def create_search_index(self, model) -> None:
        self.creates.append(model.document["name"])


class FakeSearchDB(dict):
    def list_collection_names(self) -> list[str]:
        return list(self)

    def create_collection(self, name: str) -> None:
        self[name] = FakeSearchCollection([])


def test_ensure_leaves_matching_live_indexes_alone() -> None:
    db = FakeSearchDB()
    for collection, indexes in INDEXES.items():
        live = []
        for name, (kind, definition) in indexes.items():
            stored = {"attempts_text": LIVE_ATTEMPTS_TEXT}.get(name) or normalized(kind, definition)
            live.append({"name": name, "latestDefinition": stored})
        db[collection] = FakeSearchCollection(live)
    actions = ensure_search_indexes(db)
    assert set(actions.values()) == {"unchanged"}
    assert all(not c.updates and not c.creates for c in db.values())


def test_interventions_vector_is_defined_with_its_check_filter() -> None:
    kind, definition = INDEXES["interventions"]["interventions_vector"]
    assert kind == "vectorSearch" and filter_paths(definition) == {"check"}
