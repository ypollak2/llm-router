"""Regression: RED2-10-01 (absolute paths) + RED2-10-03 (windsurf host)."""
import pathlib
from llm_router import install_manifest as im
from llm_router.commands import install as I


def test_record_stores_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    rel = pathlib.Path(".rules")
    rel.write_text("llm_router rules")
    im.record("created_file", rel, block="llm_router rules")
    recs = im._load()
    assert recs and pathlib.Path(recs[0]["path"]).is_absolute(), "manifest stored a relative path"
    # Uninstalling from a DIFFERENT cwd must still target the right file.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    (other / ".rules").write_text("UNRELATED USER FILE")
    im.apply_uninstall()
    assert (other / ".rules").read_text() == "UNRELATED USER FILE", "RED2-10-01: cross-cwd deleted wrong file"
    assert not (tmp_path / ".rules").exists(), "the actual llm_router file was not removed"


def test_windsurf_is_a_valid_host():
    assert "windsurf" in I._HOST_SNIPPETS, "RED2-10-03: windsurf not a recognized host"
    assert hasattr(I, "_install_windsurf_files")
