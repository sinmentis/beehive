from pathlib import Path


def test_civic_release_uses_locked_runtime_dependencies_and_pinned_base_images():
    recipe = (Path(__file__).parents[2] / "deploy" / "Containerfile.civic").read_text()
    bases = [line for line in recipe.splitlines() if line.startswith("FROM ")]
    assert len(bases) == 2
    assert all("@sha256:" in line for line in bases)
    assert "COPY pyproject.toml uv.lock README.md" in recipe
    assert "uv sync --locked --no-dev --extra ai --extra email --no-editable" in recipe
    assert "pip install" not in recipe
    assert 'ENV PATH="/app/.venv/bin:$PATH"' in recipe
    for unit in ("beehive-civic.container", "beehive-civic-maintenance.container"):
        config = (Path(__file__).parents[2] / "deploy" / "quadlet" / unit).read_text()
        assert "Image=localhost/beehive:civic\n" in config
        assert "Image=localhost/beehive:latest\n" not in config
