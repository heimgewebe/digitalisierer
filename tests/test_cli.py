from digitalisierer.cli import main


def test_cli_entrypoint_is_callable() -> None:
    assert callable(main)


def test_doctor_is_reporting_command() -> None:
    assert main(["doctor"]) == 0
