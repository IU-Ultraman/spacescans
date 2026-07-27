"""Base install must allow `import spacescans` and access __version__."""
import pytest

def test_can_import_top_level():
    import spacescans
    assert spacescans.__version__ == "0.2.0"


def test_pipeline_re_exported():
    from spacescans import Pipeline
    assert callable(Pipeline)


def test_resolve_config_re_exported():
    from spacescans import resolve_config
    assert callable(resolve_config)
