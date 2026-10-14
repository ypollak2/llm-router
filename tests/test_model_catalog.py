from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from llm_router import model_catalog as mc


@pytest.fixture
def payload() -> bytes:
    return (Path(mc.__file__).parent / "catalog" / "models.json").read_bytes()


def test_bundled_fields() -> None:
    catalog = mc.load_bundled()
    assert catalog["schema"] == 1
    assert catalog["mem_source"] == "estimate: size + kv-cache allowance, not measured"
    expected = {
        "qwen2.5-coder:7b": 4.7,
        "qwen2.5-coder:14b": 9.0,
        "llama3.1:8b": 4.9,
        "qwen3:8b": 5.2,
        "gemma3:4b": 3.3,
    }
    assert {m["name"]: m["size_gb"] for m in catalog["models"]} == expected
    for model in catalog["models"]:
        assert model["runtime"] == "ollama"
        assert model["tool_calling"] is None
        assert model["quality"] == {}
        assert model["mem_gb_by_ctx"] == {
            "4096": model["size_gb"] + 1.0,
            "8192": model["size_gb"] + 1.5,
        }


def test_bundled_matches_pin(payload: bytes) -> None:
    assert sha256(payload).hexdigest() == mc.load_pins()["models.json"]


def test_fetched_and_ttl(tmp_path: Path, payload: bytes) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return payload

    kwargs = {
        "fetcher": fetcher,
        "cache_dir": tmp_path,
        "pins": {"models.json": sha256(payload).hexdigest()},
        "version": "1.2.3",
    }
    assert mc.load_catalog(now=100, **kwargs).source == "fetched"
    assert (tmp_path / "models.json").read_bytes() == payload
    assert json.loads((tmp_path / "fetched_at.json").read_bytes()) == {"fetched_at": 100}
    assert calls == [mc.RAW_URL_TEMPLATE.format(version="1.2.3")]
    assert mc.load_catalog(now=100 + mc.CATALOG_TTL_S - 1, **kwargs).source == "cache"
    assert len(calls) == 1
    assert mc.load_catalog(now=100 + mc.CATALOG_TTL_S + 1, **kwargs).source == "fetched"
    assert len(calls) == 2


def test_pin_mismatch(tmp_path: Path, payload: bytes) -> None:
    result = mc.load_catalog(
        fetcher=lambda url: payload + b" ",
        cache_dir=tmp_path,
        pins={"models.json": sha256(payload).hexdigest()},
    )
    assert result.source == "bundled"
    assert result.reason == "pin mismatch"
    assert result.catalog == mc.load_bundled()
    assert list(tmp_path.iterdir()) == []


def test_offline(tmp_path: Path) -> None:
    def fetcher(url: str) -> bytes:
        raise OSError("offline")

    result = mc.load_catalog(fetcher=fetcher, cache_dir=tmp_path)
    assert result.source == "bundled"
    assert result.reason == "offline"


def test_tampered_cache(tmp_path: Path, payload: bytes) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return payload

    kwargs = {
        "fetcher": fetcher,
        "cache_dir": tmp_path,
        "pins": {"models.json": sha256(payload).hexdigest()},
    }
    assert mc.load_catalog(now=100, **kwargs).source == "fetched"
    (tmp_path / "models.json").write_bytes(b'{"models": []}')
    assert mc.load_catalog(now=101, **kwargs).source == "fetched"
    assert len(calls) == 2
    assert (tmp_path / "models.json").read_bytes() == payload


def test_default_is_bundled_only(
    tmp_path: Path, payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(url: str) -> bytes:
        pytest.fail("implicit network access")

    monkeypatch.setattr(mc, "default_fetcher", forbidden)
    (tmp_path / "models.json").write_bytes(payload)
    (tmp_path / "fetched_at.json").write_text('{"fetched_at": 100}', encoding="utf-8")
    result = mc.load_catalog(
        now=101, cache_dir=tmp_path, pins={"models.json": sha256(payload).hexdigest()}
    )
    assert result.source == "bundled"


@pytest.mark.parametrize("payload", [b"invalid", b"[]", b'{"models": {}}'])
def test_invalid_fetched_catalog(tmp_path: Path, payload: bytes) -> None:
    result = mc.load_catalog(
        fetcher=lambda url: payload,
        cache_dir=tmp_path,
        pins={"models.json": sha256(payload).hexdigest()},
    )
    assert result.source == "bundled"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit, expected", [(-1, 1), (1, 1), (2, 2), (3, 3), (99, 3)])
def test_recommend_fit_and_limit(limit: int, expected: int) -> None:
    catalog = mc.load_bundled()
    results = mc.recommend({"usable_gb": 8}, catalog=catalog, limit=limit)
    assert len(results) == expected
    models = {model["name"]: model for model in catalog["models"]}
    for result in results:
        assert max(models[result.name]["mem_gb_by_ctx"].values()) <= 8
        assert result.name != "qwen2.5-coder:14b"
        assert "\n" not in result.reason
        assert "\r" not in result.reason


def test_priority_changes_order() -> None:
    speed = mc.recommend({"usable_gb": 8}, {"priority": "speed"})
    quality = mc.recommend({"usable_gb": 8}, {"priority": "quality"})
    balanced = mc.recommend({"usable_gb": 8})
    assert speed[0].size_gb < quality[0].size_gb
    assert quality == balanced


@pytest.mark.parametrize("answers", [None, {}, "invalid", {"priority": "unknown"}])
def test_default_answers(answers: object) -> None:
    assert mc.recommend({"usable_gb": 8}, answers) == mc.recommend({"usable_gb": 8})


@pytest.mark.parametrize("hardware", [{}, {"usable_gb": None}, {"usable_gb": 1}])
def test_no_capacity(hardware: dict) -> None:
    assert mc.recommend(hardware) == []


def test_code_without_measurements_fails_mark() -> None:
    assert mc.recommend(
        {"usable_gb": 8}, {"main_use": "code"}, pass_marks={"code": 0.7}
    ) == []


def test_unknown_code_and_hebrew() -> None:
    results = mc.recommend({"usable_gb": 8}, {"main_use": "code", "languages": ["en", "he"]})
    assert results
    for result in results:
        assert "unknown for code (no pass mark yet)" in result.reason.split("; ")
        assert "qualified" not in result.reason
        assert "unproven" not in result.reason
        assert "no measured quality yet" in result.reason
        assert "Hebrew is unmeasured" in result.reason


def test_measured_code_ranking_and_mark() -> None:
    catalog = deepcopy(mc.load_bundled())
    scores = [(0.8, 0.95), (0.99, 0.6), (0.95, 0.9), (0.65, 0.99), (0.7, 0.85)]
    for model, (edit_score, explanation_score) in zip(catalog["models"], scores):
        model["quality"]["edit_with_test"] = {
            "score": edit_score, "n": 10, "source": "fitness"
        }
        model["quality"]["code_explanation"] = {
            "score": explanation_score, "n": 10, "source": "fitness"
        }
    results = mc.recommend(
        {"usable_gb": 16},
        {"main_use": "code", "priority": "speed"},
        catalog=catalog,
        pass_marks={"code": 0.7},
    )
    assert [result.name for result in results] == [
        "llama3.1:8b", "qwen2.5-coder:7b", "gemma3:4b"
    ]
    names = {result.name for result in results}
    assert "qwen2.5-coder:14b" not in names
    assert "qwen3:8b" not in names
    for result, score in zip(results, [0.9, 0.8, 0.7]):
        assert f"measured code score {score:g}" in result.reason.split("; ")
    assert all("unproven" not in result.reason for result in results)
    catalog["models"][0]["quality"]["edit_with_test"]["n"] = 0
    results = mc.recommend(
        {"usable_gb": 16}, {"main_use": "code"}, catalog=catalog, pass_marks={"code": 0.7}
    )
    assert "qwen2.5-coder:7b" not in {result.name for result in results}


@pytest.mark.parametrize("priority", ["speed", "quality", "balanced"])
def test_main_use_without_code_mark_does_not_change_ranking(priority: str) -> None:
    catalog = deepcopy(mc.load_bundled())
    for model, score in zip(catalog["models"], [0.99, 0.8, 0.7, 0.6, 0.95]):
        for task in mc.CODE_TASKS:
            model["quality"][task] = {"score": score, "n": 10, "source": "fitness"}
    mixed = mc.recommend(
        {"usable_gb": 16},
        {"main_use": "mixed", "priority": priority},
        catalog=catalog,
    )
    for main_use in ("chat", "writing", "mixed"):
        assert mc.recommend(
            {"usable_gb": 16},
            {"main_use": main_use, "priority": priority},
            catalog=catalog,
        ) == mixed
    code = mc.recommend(
        {"usable_gb": 16},
        {"main_use": "code", "priority": priority},
        catalog=catalog,
    )
    assert [result.name for result in code] == [result.name for result in mixed]
    for result in code:
        assert "unknown for code (no pass mark yet)" in result.reason.split("; ")
        assert "measured code score" not in result.reason
        assert "qualified" not in result.reason


@pytest.mark.parametrize("missing_task", mc.CODE_TASKS)
def test_code_mark_excludes_model_missing_task(missing_task: str) -> None:
    catalog = deepcopy(mc.load_bundled())
    for model in catalog["models"]:
        for task in mc.CODE_TASKS:
            model["quality"][task] = {"score": 0.9, "n": 10, "source": "fitness"}
    del catalog["models"][0]["quality"][missing_task]
    results = mc.recommend(
        {"usable_gb": 8},
        {"main_use": "code"},
        catalog=catalog,
        pass_marks={"code": 0.7},
    )
    assert results
    assert "qwen2.5-coder:7b" not in {result.name for result in results}


def test_languages_only_change_reason_text() -> None:
    english = mc.recommend({"usable_gb": 8}, {"languages": ["en"]})
    hebrew = mc.recommend({"usable_gb": 8}, {"languages": ["en", "he"]})
    assert [(r.name, r.runtime, r.size_gb) for r in hebrew] == [
        (r.name, r.runtime, r.size_gb) for r in english
    ]
    for en_result, he_result in zip(english, hebrew):
        assert he_result.reason == en_result.reason + "; Hebrew is unmeasured"


def test_memory_at_the_largest_context_and_size_fallback() -> None:
    # Fit is judged at the largest listed context, not the smallest: the router
    # asks for a large window, so a model that fits only at 4k would swap.
    catalog = {"models": [
        {"name": "a", "runtime": "ollama", "size_gb": 2,
         "mem_gb_by_ctx": {"8192": 9, "4096": 3}, "quality": {}},
        {"name": "b", "runtime": "ollama", "size_gb": 4, "quality": {}},
    ]}
    assert {r.name for r in mc.recommend({"usable_gb": 4}, catalog=catalog)} == {"b"}
    assert {r.name for r in mc.recommend({"usable_gb": 9}, catalog=catalog)} == {"a", "b"}


def test_14b_is_not_recommended_where_it_fits_only_at_4k() -> None:
    names = {r.name for r in mc.recommend({"usable_gb": 10.2}, limit=3)}
    assert "qwen2.5-coder:14b" not in names
