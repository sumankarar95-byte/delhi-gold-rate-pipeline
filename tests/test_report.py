from goldpipe.report import inr


def test_indian_number_format():
    assert inr(133482) == "₹1,33,482"
    assert inr(12345678) == "₹1,23,45,678"
    assert inr(999) == "₹999"
    assert inr(-1500) == "-₹1,500"
