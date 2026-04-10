def test_cli_module_import_smoke():
    from src.main import main

    assert callable(main)
