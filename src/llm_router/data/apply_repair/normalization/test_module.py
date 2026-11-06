from module import normalize

def test_normalize():
    assert normalize("  HeLLo  ") == "hello"
    assert normalize("") == ""
