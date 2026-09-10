"""The 'Heard you — X.' lead is spoken ONCE per draft (Tommaso QA, voice fork)."""

from app.reply_compose import readback


def test_readback_leads_once_then_goes_quiet():
    ctx: dict = {}
    assert readback(ctx, "tip_readback", "abc", "park on Lake Ranch") == (
        "Heard you — **park on Lake Ranch**. "
    )
    # same draft, later steps → nothing, even as the summary grows
    assert readback(ctx, "tip_readback", "abc", "park on Lake Ranch · park") == ""
    # a NEW draft leads again
    assert readback(ctx, "tip_readback", "def", "Rosetta's Bakery").startswith("Heard you")
    # flows don't share the stamp
    assert readback(ctx, "community_readback", "def", "Rosetta's Bakery").startswith("Heard you")
