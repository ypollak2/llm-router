from module import is_adult

def test_boundary():
    assert is_adult(18)
    assert not is_adult(17)
