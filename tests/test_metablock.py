from pl.trackers import metablock


def test_join_then_split_round_trips_and_keeps_the_text_above():
    text = "# PIPELINE: INPUT\nLine one\n\nLine two with unicode \u2014 and spaces  "
    meta = {"pipeline_mode": "auto", "worker": {"stage": "spec", "attempts": 1}}
    joined = metablock.join(text, meta)
    assert joined.startswith(text + "\n\n" + metablock.MARK + "\n```json\n")
    assert joined.endswith("\n```\n")
    got_text, got_meta, broken = metablock.split(joined)
    assert got_text == text
    assert got_meta == meta
    assert broken is None


def test_no_block_is_empty_metadata():
    desc = "# PIPELINE: INPUT\nJust an ordinary description\n"
    assert metablock.split(desc) == (desc, {}, None)


def test_a_broken_block_is_reported_with_what_was_found():
    text = "# PIPELINE: INPUT\nKeep this text"
    for tail in (
        f"{metablock.MARK}\n```json\n{{not valid json\n```\n",
        f"{metablock.MARK}\n```json\n[1, 2, 3]\n```\n",
        f'{metablock.MARK}\n```json\n{{"a": 1}}\n```\ntrailing text\n',
        f"{metablock.MARK}\nno fence here",
        metablock.MARK,
    ):
        got_text, got_meta, broken = metablock.split(f"{text}\n\n{tail}")
        assert got_text == text
        assert got_meta == {}
        assert broken and tail.splitlines()[-1] in broken


def test_only_the_last_marker_counts():
    above = f"Example marker:\n\n{metablock.MARK}\n```json\n{{broken\n```\n\nReal text below."
    joined = metablock.join(above, {"pipeline_mode": "auto"})
    got_text, got_meta, broken = metablock.split(joined)
    assert got_text == above
    assert got_meta == {"pipeline_mode": "auto"}
    assert broken is None
