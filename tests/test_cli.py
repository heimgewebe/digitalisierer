from digitalisierer.cli import main


def test_cli_entrypoint_is_callable() -> None:
    assert callable(main)
