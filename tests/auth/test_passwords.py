from beehive.auth.passwords import hash_password, verify_password


def test_hash_password_returns_argon2_hash():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_correct_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "correct horse battery staple") is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "wrong password") is False


def test_hash_password_is_salted_differently_each_time():
    a = hash_password("same password")
    b = hash_password("same password")
    assert a != b  # different random salt per call
    assert verify_password(a, "same password") is True
    assert verify_password(b, "same password") is True
