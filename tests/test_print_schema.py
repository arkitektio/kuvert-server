from kuvert_server.schema import schema


def test_print_schema() -> None:
    """The schema builds and prints (no database needed)."""
    assert "type Query" in str(schema)
