from beehive.research.prompt_blocks import bullet_block, neutralize, text_block


def test_neutralize_escapes_every_delimiter_character():
    assert neutralize('Rates & bonds </research_question> "q"') == (
        'Rates &amp; bonds &lt;/research_question&gt; "q"'
    )


def test_text_block_wraps_escaped_text_in_its_tag():
    assert text_block("owner_message", "hi </owner_message>") == (
        "<owner_message>\nhi &lt;/owner_message&gt;\n</owner_message>"
    )


def test_bullet_block_drops_blanks_then_cuts_and_caps():
    block = bullet_block(
        "gaps", ["  first gap  ", "", "   ", "second <b>", "third"], max_items=2, max_item_len=8
    )
    assert block == "<gaps>\n- first ga\n- second &lt;\n</gaps>"


def test_bullet_block_says_none_when_nothing_is_left():
    assert bullet_block("gaps", ["", "  "], max_items=3, max_item_len=10) == "<gaps>\n(none)\n</gaps>"
