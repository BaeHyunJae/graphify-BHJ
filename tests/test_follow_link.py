"""Fork-local ``--follow-link``: index chosen in-root link folders (junctions/symlinks).

Each listed folder is a link inside the scan root whose target sits outside it.
Its files must be indexed under their LEXICAL in-root path (``Context/...``),
the choice must persist for flag-less ``graphify update`` runs, and links that
are not listed must stay unindexed exactly as before.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _link(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


def _graphify(cwd: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GRAPHIFY_BACKEND": "none", "GRAPHIFY_OUT": str(out)}
    env.pop("GRAPHIFY_FOLLOWED_LINK_DIRS", None)
    result = subprocess.run(
        [sys.executable, "-m", "graphify", *args],
        cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _graph(out: Path) -> tuple[dict[str, dict], list[dict]]:
    g = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in g["nodes"]}
    return nodes, g.get("links", g.get("edges", []))


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _ids_for(nodes: dict[str, dict], source_file: str) -> set[str]:
    return {nid for nid, n in nodes.items() if n.get("source_file") == source_file}


def _labels(nodes: dict[str, dict]) -> set[str]:
    return {str(n.get("label", "")) for n in nodes.values()}


def _assert_no_target_paths(out: Path, *targets: Path) -> None:
    """No stored artifact (graph, manifest, cache, build state) names a link target."""
    needles = set()
    for t in targets:
        for form in (str(t), t.as_posix(), str(t).replace("\\", "\\\\")):
            needles.add(form.lower())
    for f in out.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace").lower()
            for needle in needles:
                assert needle not in text, f"{f} stores link target path {needle}"


def _make_worktree(base: Path, name: str, symbol: str, shared: Path,
                   links: dict[str, Path]) -> Path:
    wt = base / name
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "thing.py").write_text(
        f"def {symbol}():\n    return 1\n", encoding="utf-8"
    )
    # Ignore rules still apply inside a followed folder.
    (wt / ".graphifyignore").write_text("Context/Private/\n", encoding="utf-8")
    _link(wt / "Context", shared)
    for link_name, target in links.items():
        _link(wt / link_name, target)
    return wt


def test_follow_link_indexes_listed_links_under_lexical_paths(tmp_path):
    shared = tmp_path / "shared_context"
    (shared / "Scopes").mkdir(parents=True)
    (shared / "Private").mkdir()
    rec = shared / "Scopes" / "rec.md"
    rec.write_text(
        "# Record\n\nSee [thing](../../src/thing.py), "
        "`src/thing.py::alpha_only` and `src/thing.py::beta_only`.\n",
        encoding="utf-8",
    )
    gone = shared / "Scopes" / "gone.md"
    gone.write_text("# Gone\n\nSee [thing](../../src/thing.py).\n", encoding="utf-8")
    (shared / "Private" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    audit = tmp_path / "audit_ext"
    audit.mkdir()
    check = audit / "check.py"
    check.write_text("def audit_fn():\n    return 2\n", encoding="utf-8")
    sentinel = tmp_path / "sentinel_ext"
    sentinel.mkdir()
    (sentinel / "sentinel.py").write_text("def sentinel_fn():\n    return 3\n", encoding="utf-8")

    wt_a = _make_worktree(tmp_path, "wt_a", "alpha_only", shared,
                          {"audit": audit, "other": sentinel})
    wt_b = _make_worktree(tmp_path, "wt_b", "beta_only", shared, {})
    out_a = tmp_path / "out_a" / "graphify-out"
    out_b = tmp_path / "out_b" / "graphify-out"

    _graphify(wt_a, out_a, "extract", ".", "--no-gitignore",
              "--follow-link", "Context", "--follow-link", "audit")
    _graphify(wt_a, out_a, "update", ".")
    _graphify(wt_b, out_b, "extract", ".", "--no-gitignore", "--follow-link", "Context")
    _graphify(wt_b, out_b, "update", ".")

    for out, mine, theirs in ((out_a, "alpha_only", "beta_only"),
                              (out_b, "beta_only", "alpha_only")):
        nodes, edges = _graph(out)
        rec_ids = _ids_for(nodes, "Context/Scopes/rec.md")
        assert rec_ids, sorted(n.get("source_file") for n in nodes.values())
        file_id = next(nid for nid in _ids_for(nodes, "src/thing.py")
                       if nodes[nid].get("label") == "thing.py")
        my_symbol = next(nid for nid, n in nodes.items()
                         if str(n.get("label", "")).startswith(mine))
        from_rec = {(e["target"], e.get("relation")) for e in edges if e["source"] in rec_ids}
        assert (file_id, "references") in from_rec
        assert (my_symbol, "references") in from_rec
        assert not any(theirs in str(n.get("label", "")) or theirs in nid
                       for nid, n in nodes.items())
        assert "Context/Scopes/rec.md" in _manifest(out)
        assert not any("Private" in str(n.get("source_file")) for n in nodes.values())

    nodes_a, _ = _graph(out_a)
    manifest_a = _manifest(out_a)
    assert _ids_for(nodes_a, "audit/check.py")
    assert "audit/check.py" in manifest_a
    # The unlisted junction stays unindexed.
    assert not any("sentinel" in str(n.get("source_file")) for n in nodes_a.values())
    assert not any("sentinel" in k for k in manifest_a)
    assert json.loads((out_a / ".graphify_build.json").read_text(encoding="utf-8"))[
        "follow_links"] == ["Context", "audit"]
    _assert_no_target_paths(out_a, shared, audit, sentinel)

    # Edit a record, delete another, edit the second followed folder; then a
    # fresh-process update WITHOUT --follow-link must keep both folders.
    gone_ids = _ids_for(nodes_a, "Context/Scopes/gone.md")
    assert gone_ids
    rec.write_text(
        "# Record\n\n## Added Heading\n\nSee [thing](../../src/thing.py).\n",
        encoding="utf-8",
    )
    gone.unlink()
    check.write_text("def audit_fn_renamed():\n    return 5\n", encoding="utf-8")
    _graphify(wt_a, out_a, "update", ".")

    nodes, edges = _graph(out_a)
    manifest = _manifest(out_a)
    assert _ids_for(nodes, "Context/Scopes/rec.md")
    assert "Added Heading" in _labels(nodes)
    assert _ids_for(nodes, "audit/check.py")
    assert any(lbl.startswith("audit_fn_renamed") for lbl in _labels(nodes))
    assert not any(lbl.startswith("audit_fn(") or lbl == "audit_fn" for lbl in _labels(nodes))
    assert not _ids_for(nodes, "Context/Scopes/gone.md")
    assert not set(nodes) & gone_ids
    assert not any(e["source"] in gone_ids or e["target"] in gone_ids for e in edges)
    assert "Context/Scopes/gone.md" not in manifest
    assert {"Context/Scopes/rec.md", "audit/check.py"} <= set(manifest)
    assert not any("sentinel" in k for k in manifest)
    _assert_no_target_paths(out_a, shared, audit, sentinel)


def test_without_follow_link_links_stay_unindexed(tmp_path):
    shared = tmp_path / "shared_context"
    shared.mkdir()
    (shared / "rec.md").write_text("# Record\n", encoding="utf-8")
    (shared / "linked.py").write_text("def linked_fn():\n    return 1\n", encoding="utf-8")
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "thing.py").write_text("def thing_fn():\n    return 1\n", encoding="utf-8")
    _link(wt / "Context", shared)
    out = tmp_path / "out" / "graphify-out"

    _graphify(wt, out, "extract", ".", "--no-gitignore")
    _graphify(wt, out, "update", ".")

    nodes, _ = _graph(out)
    assert _ids_for(nodes, "src/thing.py")
    assert not any(str(n.get("source_file", "")).startswith("Context") for n in nodes.values())
    assert not any(k.startswith("Context") for k in _manifest(out))
    build_cfg = json.loads((out / ".graphify_build.json").read_text(encoding="utf-8"))
    assert "follow_links" not in build_cfg
