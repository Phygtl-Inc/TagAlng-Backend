import unittest
from unittest.mock import MagicMock, patch

from app.community_discovery import (
    CONFIRMED_VIA_GROUNDING,
    CONFIRMED_VIA_JOIN,
    _discovery_status_line,
    discover_communities,
    join_community,
    joined_via_label,
    mail_join_to_members,
    notify_members_of_join,
    set_membership,
)


def _chain(data=None, count=None, insert_data=None):
    """A supabase table mock. `insert_data` gets its OWN sub-chain: sharing one
    execute() between select and insert lets the inserted row leak back into the
    reads (which silently broke the key-collision assertion once already)."""
    m = MagicMock()
    for method in ("select", "eq", "neq", "is_", "in_", "or_", "limit", "order", "update"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data if data is not None else [], count=count)
    ins = MagicMock()
    ins.execute.return_value = MagicMock(data=insert_data if insert_data is not None else [])
    m.insert.return_value = ins
    return m


def _sb(tables: dict, rpc_data=None):
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables.get(name, _chain([]))
    sb.rpc.return_value = MagicMock(
        execute=MagicMock(return_value=MagicMock(data=rpc_data if rpc_data is not None else []))
    )
    return sb


_RPC_ROWS = [
    {
        "place_id": "p1",
        "name": "OrangeTheory Narcoossee",
        "address": "9145 Narcoossee Rd",
        "place_type": "fitness",
        "zip": "32827",
        "member_count": 34,
        "member_types": ["fitness"],
        "distance_meters": 900.0,
        "distance_text": "11 min walk",
        "is_member": False,
    },
    {
        "place_id": "p2",
        "name": "St. Luke's",
        "address": None,
        "place_type": None,
        "zip": "32827",
        "member_count": 12,
        "member_types": ["faith"],
        "distance_meters": 3000.0,
        "distance_text": "1.9 mi away",
        "is_member": True,
    },
]


class TestDiscoveryStatusLine(unittest.TestCase):
    def test_stranger_counts_read_as_people(self) -> None:
        self.assertEqual(_discovery_status_line(34, "11 min walk", False), "34 people · 11 min walk")
        self.assertEqual(_discovery_status_line(1, None, False), "1 person")

    def test_own_place_never_reads_as_n_strangers(self) -> None:
        # member_count includes the caller, so "12 people" would overstate by one.
        self.assertEqual(_discovery_status_line(12, "1.9 mi away", True), "You + 11 others · 1.9 mi away")
        self.assertEqual(_discovery_status_line(1, None, True), "You're in")


class TestDiscoverCommunities(unittest.TestCase):
    @patch("app.community_discovery.service_client")
    def test_rows_carry_place_facts_and_no_identities(self, sb) -> None:
        sb.return_value = _sb({}, rpc_data=_RPC_ROWS)
        rows = discover_communities("u1")
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["place_name"], "OrangeTheory Narcoossee")
        self.assertEqual(first["relation"], "gym")
        self.assertEqual(first["member_count"], 34)
        self.assertEqual(first["distance_text"], "11 min walk")
        for row in rows:
            for leaked in ("nickname", "avatar_url", "peer_user_id", "members"):
                self.assertNotIn(leaked, row)

    @patch("app.community_discovery.service_client")
    def test_place_type_falls_back_to_what_members_call_it(self, sb) -> None:
        sb.return_value = _sb({}, rpc_data=_RPC_ROWS)
        rows = discover_communities("u1")
        # St. Luke's has no advisory place_type; the members' own framing fills in.
        self.assertEqual(rows[1]["place_type"], "faith")
        self.assertEqual(rows[1]["relation"], "place of worship")

    @patch("app.community_discovery.service_client")
    def test_rpc_failure_reads_as_nothing_yet(self, sb) -> None:
        client = MagicMock()
        client.rpc.side_effect = RuntimeError("no such function")
        sb.return_value = client
        self.assertEqual(discover_communities("u1"), [])

    @patch("app.community_discovery.service_client")
    def test_query_and_radius_reach_the_rpc(self, sb) -> None:
        client = _sb({}, rpc_data=[])
        sb.return_value = client
        discover_communities("u1", query="  orange ", limit=99)
        args = client.rpc.call_args[0][1]
        self.assertEqual(args["p_query"], "orange")
        self.assertEqual(args["p_limit"], 40)  # capped
        self.assertEqual(args["p_user_id"], "u1")


class TestJoinProvenance(unittest.TestCase):
    """The product question: joined in Lana, or added after they mentioned it?"""

    def _tables(self, existing=None):
        return {
            "places": _chain(
                [{"id": "p1", "name": "OrangeTheory Narcoossee", "place_type": "fitness"}]
            ),
            "circle_affiliations": _chain(existing if existing is not None else []),
        }

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_fresh_join_is_marked_as_a_lana_join(self, sb, _after) -> None:
        affs = _chain([], insert_data=[{"id": "new1"}])
        sb.return_value = _sb({**self._tables(), "circle_affiliations": affs})
        out = join_community("u1", "p1")
        row = affs.insert.call_args[0][0]
        self.assertEqual(row["source"], CONFIRMED_VIA_JOIN)
        self.assertEqual(row["confirmed_via"], CONFIRMED_VIA_JOIN)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["place_ref"], "p1")
        self.assertEqual(row["circle_type"], "fitness")
        self.assertFalse(out["promoted_from_candidate"])
        self.assertEqual(joined_via_label(out["confirmed_via"], out["source"]), "Joined in Lana")

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_joining_a_place_they_mentioned_keeps_the_chat_origin(self, sb, _after) -> None:
        # They said "my gym" in conversation → parked candidate. Tapping Join is what
        # closes it, so source stays chat_extraction and confirmed_via records the tap.
        existing = [
            {
                "id": "a9",
                "circle_key": "orangetheory_narcoossee",
                "circle_type": "fitness",
                "place_ref": None,
                "status": "suggested",
                "source": "chat_extraction",
                "confirmed_via": None,
            }
        ]
        affs = _chain(existing)
        sb.return_value = _sb({**self._tables(existing), "circle_affiliations": affs})
        out = join_community("u1", "p1")
        patch_row = affs.update.call_args[0][0]
        self.assertEqual(patch_row["confirmed_via"], CONFIRMED_VIA_JOIN)
        self.assertEqual(patch_row["status"], "confirmed")
        self.assertEqual(patch_row["place_ref"], "p1")
        self.assertNotIn("source", patch_row)  # origin is never rewritten
        self.assertEqual(out["source"], "chat_extraction")
        self.assertTrue(out["promoted_from_candidate"])
        self.assertEqual(out["affiliation_id"], "a9")

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_already_a_member_is_a_no_op(self, sb, _after) -> None:
        existing = [
            {
                "id": "a1",
                "circle_key": "orangetheory_narcoossee",
                "circle_type": "fitness",
                "place_ref": "p1",
                "status": "confirmed",
                "source": "profile_add",
                "confirmed_via": "profile_add",
            }
        ]
        affs = _chain(existing)
        sb.return_value = _sb({**self._tables(existing), "circle_affiliations": affs})
        out = join_community("u1", "p1")
        self.assertTrue(out["already_member"])
        affs.insert.assert_not_called()
        affs.update.assert_not_called()
        self.assertEqual(out["source"], "profile_add")

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_key_collision_does_not_break_the_unique_index(self, sb, _after) -> None:
        # A DIFFERENT place already holds the slug (two gyms with the same name).
        existing = [
            {
                "id": "a1",
                "circle_key": "orangetheory_narcoossee",
                "circle_type": "fitness",
                "place_ref": "pOther",
                "status": "confirmed",
                "source": "profile_add",
                "confirmed_via": "profile_add",
            }
        ]
        affs = _chain(existing, insert_data=[{"id": "new2"}])
        sb.return_value = _sb({**self._tables(existing), "circle_affiliations": affs})
        join_community("u1", "p1")
        self.assertEqual(affs.insert.call_args[0][0]["circle_key"], "orangetheory_narcoossee_2")

    @patch("app.community_discovery.service_client")
    def test_unknown_place_is_rejected(self, sb) -> None:
        sb.return_value = _sb({"places": _chain([])})
        with self.assertRaises(ValueError) as err:
            join_community("u1", "nope")
        self.assertEqual(str(err.exception), "place_not_found")

    def test_place_id_is_required(self) -> None:
        with self.assertRaises(ValueError) as err:
            join_community("u1", "")
        self.assertEqual(str(err.exception), "place_required")


class TestMembershipIntent(unittest.TestCase):
    """§19: "I'm a member — I go here" vs "Not yet — just curious for now"."""

    def _tables(self, existing=None):
        return {
            "places": _chain([{"id": "p1", "name": "OrangeTheory", "place_type": "fitness"}]),
            "circle_affiliations": _chain(existing if existing is not None else []),
        }

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_curious_is_not_written_as_membership(self, sb, after) -> None:
        affs = _chain([], insert_data=[{"id": "new1"}])
        sb.return_value = _sb({**self._tables(), "circle_affiliations": affs})
        out = join_community("u1", "p1", membership="curious")
        # status='curious' is what every member count, roster and matcher excludes.
        self.assertEqual(affs.insert.call_args[0][0]["status"], "curious")
        self.assertEqual(out["status"], "curious")
        # And Lana does not then ask what she enjoys most about a place she doesn't go to.
        after.assert_not_called()

    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_member_is_still_the_default(self, sb, after) -> None:
        affs = _chain([], insert_data=[{"id": "new1"}])
        sb.return_value = _sb({**self._tables(), "circle_affiliations": affs})
        self.assertEqual(join_community("u1", "p1")["status"], "confirmed")
        self.assertEqual(affs.insert.call_args[0][0]["status"], "confirmed")
        after.assert_called_once()

    @patch("app.community_discovery.service_client")
    def test_the_sheet_can_promote_a_curious_row(self, sb) -> None:
        affs = _chain([{"id": "a1", "place_ref": "p1", "status": "curious"}])
        sb.return_value = _sb({"circle_affiliations": affs})
        out = set_membership("u1", "a1", "member")
        self.assertEqual(affs.update.call_args[0][0], {"status": "confirmed"})
        self.assertEqual(out, {"affiliation_id": "a1", "place_id": "p1", "membership": "member"})

    @patch("app.community_discovery.service_client")
    def test_repeating_the_same_answer_writes_nothing(self, sb) -> None:
        affs = _chain([{"id": "a1", "place_ref": "p1", "status": "curious"}])
        sb.return_value = _sb({"circle_affiliations": affs})
        self.assertEqual(set_membership("u1", "a1", "curious")["membership"], "curious")
        affs.update.assert_not_called()

    @patch("app.community_discovery.service_client")
    def test_an_ungrounded_candidate_is_not_a_community(self, sb) -> None:
        sb.return_value = _sb({"circle_affiliations": _chain([{"id": "a1", "place_ref": None}])})
        with self.assertRaises(ValueError) as err:
            set_membership("u1", "a1", "member")
        self.assertEqual(str(err.exception), "place_required")

    @patch("app.community_discovery.service_client")
    def test_someone_elses_row_is_not_found(self, sb) -> None:
        affs = _chain([])
        sb.return_value = _sb({"circle_affiliations": affs})
        with self.assertRaises(ValueError) as err:
            set_membership("u1", "a1", "member")
        self.assertEqual(str(err.exception), "affiliation_not_found")
        self.assertIn(("user_id", "u1"), [c.args for c in affs.eq.call_args_list])


class TestJoinTellsTheMembers(unittest.TestCase):
    """Joining is news to the people already there — the same fan-out the meets use."""

    def _notif_sb(self, roster, contacts):
        from tests.test_event_place import _chain as chain

        sb = MagicMock()
        tables = {
            "circle_affiliations": chain(roster),
            "users": chain(contacts),
        }
        sb.table.side_effect = lambda name: tables[name]
        return sb

    def _count_sb(self, count):
        """The member-count query lives on community_discovery's own client."""
        from tests.test_event_place import _chain as chain

        sb = MagicMock()
        row = chain([])
        row.execute.return_value = MagicMock(data=[], count=count)
        sb.table.return_value = row
        return sb

    @patch("app.community_discovery.service_client")
    @patch("app.notifications.send_email")
    @patch("app.notifications.service_client")
    @patch("app.notifications._user_contact", return_value=("j@x.com", "Ada"))
    def test_members_are_mailed_in_their_own_language(self, _contact, sb, mail, count_sb) -> None:
        count_sb.return_value = self._count_sb(3)
        sb.return_value = self._notif_sb(
            [{"user_id": "m1"}, {"user_id": "m2"}],
            [
                {"id": "m1", "email": "m1@x.com", "locale": None},
                {"id": "m2", "email": "m2@x.com", "locale": "es"},
            ],
        )
        self.assertEqual(mail_join_to_members("p1", "Lake Nona YMCA", "u1"), 2)
        subjects = [c.kwargs["subject"] for c in mail.call_args_list]
        # The joiner's nickname carries the subject line, in each reader's language.
        self.assertIn("Ada joined Lake Nona YMCA", subjects)
        self.assertIn("Ada se unió a Lake Nona YMCA", subjects)
        html = mail.call_args_list[0].kwargs["html"]
        self.assertIn("Ada joined Lake Nona YMCA", html)
        self.assertIn("3 now", html)  # the member count is what makes it feel like growth
        self.assertIn("Ada is in — say hi.", html)  # inbox preheader, not a body leak

    @patch("app.community_discovery.service_client")
    @patch("app.notifications.send_email")
    @patch("app.notifications.service_client")
    @patch("app.notifications._user_contact", return_value=("j@x.com", None))
    def test_a_joiner_without_a_nickname_is_still_announced(self, _c, sb, mail, count_sb) -> None:
        count_sb.return_value = self._count_sb(1)
        sb.return_value = self._notif_sb(
            [{"user_id": "m1"}], [{"id": "m1", "email": "m1@x.com", "locale": None}]
        )
        self.assertEqual(mail_join_to_members("p1", "Lake Nona YMCA", "u1"), 1)
        self.assertIn("A new neighbor joined", mail.call_args.kwargs["html"])
        self.assertEqual(
            mail.call_args.kwargs["subject"], "A new neighbor joined Lake Nona YMCA"
        )

    @patch("app.community_discovery.service_client")
    @patch("app.notifications.send_email")
    @patch("app.notifications.service_client")
    @patch("app.notifications._user_contact", return_value=("j@x.com", "Ada"))
    def test_members_without_an_email_are_skipped(self, _c, sb, mail, count_sb) -> None:
        count_sb.return_value = self._count_sb(1)
        sb.return_value = self._notif_sb(
            [{"user_id": "m1"}], [{"id": "m1", "email": None, "locale": None}]
        )
        self.assertEqual(mail_join_to_members("p1", "Lake Nona YMCA", "u1"), 0)
        mail.assert_not_called()

    @patch("app.community_discovery.threading.Thread")
    def test_an_unnamed_community_says_nothing(self, thread) -> None:
        notify_members_of_join("p1", "", "u1")
        notify_members_of_join("", "Lake Nona YMCA", "u1")
        thread.assert_not_called()

    @patch("app.community_discovery.notify_members_of_join")
    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_a_curious_tap_is_not_announced(self, sb, _after, notify) -> None:
        affs = _chain([], insert_data=[{"id": "new1"}])
        sb.return_value = _sb(
            {
                "places": _chain([{"id": "p1", "name": "OrangeTheory", "place_type": "fitness"}]),
                "circle_affiliations": affs,
            }
        )
        join_community("u1", "p1", membership="curious")
        notify.assert_not_called()

    @patch("app.community_discovery.notify_members_of_join")
    @patch("app.community_discovery._after_join")
    @patch("app.community_discovery.service_client")
    def test_a_real_join_is_announced(self, sb, _after, notify) -> None:
        affs = _chain([], insert_data=[{"id": "new1"}])
        sb.return_value = _sb(
            {
                "places": _chain([{"id": "p1", "name": "OrangeTheory", "place_type": "fitness"}]),
                "circle_affiliations": affs,
            }
        )
        join_community("u1", "p1")
        notify.assert_called_once_with("p1", "OrangeTheory", "u1")


class TestJoinablePlaceNames(unittest.TestCase):
    """Dev grounded street addresses and a place literally named "32827" as
    communities. Their owner's row keeps working; nobody else is offered them."""

    def test_addresses_and_zips_are_not_offered(self) -> None:
        from app.community_discovery import is_joinable_place_name

        for junk in ("692 Olde Camelot Cir #3282", "373 Tampa Ct", "10057 Selten Way #328",
                     "32827", "9395 Flowering Cottonwood Rd #32", ""):
            self.assertFalse(is_joinable_place_name(junk), junk)

    def test_real_places_survive_including_ones_with_numbers(self) -> None:
        from app.community_discovery import is_joinable_place_name

        for real in ("FIT 407 Lake Nona", "Lp Fit", "St. Luke's United Methodist Church",
                     "Heroes Community Park", "Book Club Bar", "PingPod"):
            self.assertTrue(is_joinable_place_name(real), real)

    @patch("app.community_discovery.service_client")
    def test_discovery_drops_addressish_rows(self, sb) -> None:
        rows = _RPC_ROWS + [
            {
                "place_id": "p9",
                "name": "10057 Selten Way #328",
                "place_type": None,
                "member_count": 1,
                "member_types": ["other"],
                "distance_text": "5 min walk",
                "is_member": False,
            }
        ]
        sb.return_value = _sb({}, rpc_data=rows)
        names = [r["place_name"] for r in discover_communities("u1")]
        self.assertNotIn("10057 Selten Way #328", names)
        self.assertEqual(len(names), 2)


class TestCommunitiesChatTurn(unittest.TestCase):
    """The lane that answers "show me communities around me" — before it existed the
    ask hit either the area-not-open host bridge or an attr peer search for
    neighbors "interested in community"."""

    _MINE = [
        {
            "id": "a1",
            "place_id": "pMine",
            "place_name": "Lp Fit",
            "member_count": 2,
            "active": True,
            "circle_type": "fitness",
            "added_at": "2026-07-01T00:00:00Z",
        }
    ]

    @patch("app.community_surface.communities_card", return_value={"items": [{"affiliation_id": "a1"}], "total": 1, "more_count": 0})
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply")
    def test_both_halves_reach_the_composer_as_facts(
        self, compose, mine, discover, _card
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = self._MINE
        discover.return_value = [
            {
                "place_id": "pNear",
                "place_name": "Heroes Community Park",
                "member_count": 3,
                "status_line": "3 people · 11 min walk",
                "is_member": False,
            },
            {"place_id": "pMine", "place_name": "Lp Fit", "is_member": True},
        ]
        compose.return_value = "composed"
        ctx: dict = {}
        out = communities_chat_turn("u1", message="show me communities around me", session_ctx=ctx)
        self.assertEqual(out, "composed")
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("Lp Fit", facts)          # what they're already in
        self.assertIn("Heroes Community Park", facts)  # what they could join
        # The card payloads ride along for the FE.
        self.assertEqual(len(ctx["community_discovery"]["communities"]), 1)  # is_member filtered
        self.assertIn("communities_card", ctx)

    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply")
    def test_empty_state_forbids_blaming_the_area(self, compose, _mine, _disc) -> None:
        from app.community_discovery import communities_chat_turn

        compose.return_value = "composed"
        communities_chat_turn("u1", message="any communities near me?", session_ctx={})
        kwargs = compose.call_args.kwargs
        facts = " ".join(kwargs["facts"]).lower()
        # The bug this replaces asserted "there aren't any local communities" from
        # area-gate facts that never counted anything.
        self.assertIn("do not", facts)
        self.assertIn("first", kwargs["fallback"].lower())

    @patch("app.community_surface.communities_card", return_value=None)
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply")
    def test_the_prose_summarises_and_the_cards_do_the_listing(
        self, compose, mine, discover, _card
    ) -> None:
        """QA 2026-08-18: six communities named, then four more, then the cards repeated
        all ten — a ten-line wall answering a one-line question."""
        from app.community_discovery import communities_chat_turn

        mine.return_value = [
            {"place_id": f"m{i}", "place_name": name, "member_count": 2}
            for i, name in enumerate(
                ["Fitness CF", "The Man Cave", "Life Time", "Lp Fit", "FIT 407", "St. Luke's"]
            )
        ]
        discover.return_value = [
            {
                "place_id": f"n{i}", "place_name": name, "member_count": 1,
                "status_line": "1 person · 2 mi away", "is_member": False,
            }
            for i, name in enumerate(["Mizu Sushi", "Trinity Church", "Crunch Fitness"])
        ]
        compose.return_value = "composed"
        communities_chat_turn("u1", message="can u show me communities around me", session_ctx={})
        kwargs = compose.call_args.kwargs
        facts = " ".join(kwargs["facts"])
        # Counts + ONE anchor name each side; the roll-call is what made it unreadable.
        self.assertIn("already in: 6", facts)
        self.assertIn("could join: 3", facts)
        for name in ("The Man Cave", "Life Time", "FIT 407", "Trinity Church", "Crunch Fitness"):
            self.assertNotIn(name, facts)
        self.assertIn("must NOT name them one by one", facts)
        self.assertNotIn("Trinity Church", kwargs["fallback"])
        self.assertEqual(kwargs["max_sentences"], 2)

    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=_MINE)
    @patch("app.community_surface.communities_card", return_value=None)
    @patch("app.reply_compose.compose_reply")
    def test_own_communities_are_named_when_nothing_new_nearby(
        self, compose, _card, _mine, _disc
    ) -> None:
        from app.community_discovery import communities_chat_turn

        compose.return_value = "composed"
        communities_chat_turn("u1", message="what communities am I in?", session_ctx={})
        self.assertIn("Lp Fit", compose.call_args.kwargs["fallback"])


class TestJoinReply(unittest.TestCase):
    """Tap (or type) an answer to "want to join one of these?"."""

    _PENDING = {
        "places": [
            {"place_id": "p1", "place_name": "Lp Fit"},
            {"place_id": "p2", "place_name": "Heroes Community Park"},
        ]
    }

    @patch("app.community_discovery.join_community")
    def test_chip_payload_joins_the_named_place(self, join) -> None:
        from app.community_discovery import read_join_reply

        join.return_value = {"place_id": "p2", "place_name": "Heroes Community Park"}
        ctx = {"community_join_pending": dict(self._PENDING)}
        out = read_join_reply("u1", "Join Heroes Community Park", ctx)
        self.assertEqual(join.call_args[0][1], "p2")
        self.assertIsNotNone(out)
        # One-turn offer: consumed, and cleared with None (never popped).
        self.assertIsNone(ctx["community_join_pending"])

    @patch("app.community_discovery.join_community")
    def test_typed_name_alone_counts_as_the_answer(self, join) -> None:
        from app.community_discovery import read_join_reply

        join.return_value = {"place_id": "p1", "place_name": "Lp Fit"}
        read_join_reply("u1", "lp fit", {"community_join_pending": dict(self._PENDING)})
        self.assertEqual(join.call_args[0][1], "p1")

    @patch("app.community_discovery.join_community")
    def test_bare_yes_with_several_offered_never_guesses(self, join) -> None:
        from app.community_discovery import read_join_reply

        out = read_join_reply("u1", "yes", {"community_join_pending": dict(self._PENDING)})
        self.assertIsNone(out)
        join.assert_not_called()  # writing the wrong membership is worse than re-asking

    @patch("app.community_discovery.join_community")
    def test_bare_yes_is_unambiguous_with_one_offer(self, join) -> None:
        from app.community_discovery import read_join_reply

        join.return_value = {"place_id": "p1", "place_name": "Lp Fit"}
        pending = {"places": [{"place_id": "p1", "place_name": "Lp Fit"}]}
        self.assertIsNotNone(read_join_reply("u1", "sure", {"community_join_pending": pending}))
        self.assertEqual(join.call_args[0][1], "p1")

    @patch("app.community_discovery.join_community")
    def test_unrelated_message_falls_through_and_disarms(self, join) -> None:
        from app.community_discovery import read_join_reply

        ctx = {"community_join_pending": dict(self._PENDING)}
        self.assertIsNone(read_join_reply("u1", "actually who's hosting this weekend?", ctx))
        join.assert_not_called()
        self.assertIsNone(ctx["community_join_pending"])

    def test_no_offer_armed_is_a_no_op(self) -> None:
        from app.community_discovery import read_join_reply

        self.assertIsNone(read_join_reply("u1", "Join Lp Fit", {}))

    @patch("app.reply_compose.compose_reply")
    def test_already_member_confirm_does_not_claim_a_new_join(self, compose) -> None:
        from app.community_discovery import join_confirm_reply

        compose.return_value = "composed"
        join_confirm_reply(
            {"place_name": "Lp Fit", "already_member": True}, session_ctx={}, message="join lp fit"
        )
        self.assertIn("already", compose.call_args.kwargs["fallback"].lower())


class TestEmoji(unittest.TestCase):
    def test_type_maps_to_stable_card_art(self) -> None:
        from app.circles_flow import place_relation_emoji

        self.assertEqual(place_relation_emoji("fitness"), "🏋️")
        self.assertEqual(place_relation_emoji("faith"), "⛪")
        # Unknown / missing type gets a neutral pin, never a guess at what it is.
        self.assertEqual(place_relation_emoji(None), "📍")
        self.assertEqual(place_relation_emoji("not_a_type"), "📍")

    @patch("app.community_discovery.service_client")
    def test_discovery_rows_carry_the_emoji(self, sb) -> None:
        sb.return_value = _sb({}, rpc_data=_RPC_ROWS)
        rows = discover_communities("u1")
        self.assertEqual(rows[0]["emoji"], "🏋️")
        self.assertEqual(rows[1]["emoji"], "⛪")

    def test_join_chips_lead_with_the_emoji(self) -> None:
        from app.ui_actions import community_join_actions

        chips = community_join_actions(
            [{"place_name": "Lp Fit", "emoji": "🏋️"}, {"place_name": "Heroes Community Park"}]
        )
        self.assertEqual(chips[0]["label"], "🏋️ Join Lp Fit")
        # The payload stays the literal canonical string the join reader matches.
        self.assertEqual(chips[0]["message"], "Join Lp Fit")
        self.assertEqual(chips[1]["label"], "Join Heroes Community Park")


class TestJoinedViaLabels(unittest.TestCase):
    def test_each_path_reads_differently(self) -> None:
        self.assertEqual(joined_via_label(CONFIRMED_VIA_JOIN, "community_join"), "Joined in Lana")
        self.assertEqual(
            joined_via_label(CONFIRMED_VIA_GROUNDING, "chat_extraction"),
            "From something you told Lana",
        )
        self.assertEqual(joined_via_label("profile_add", "profile_add"), "You added it")
        self.assertEqual(joined_via_label("invite_self_confirm", "invite_confirmed"), "From an invite")

    def test_pre_migration_rows_fall_back_to_source(self) -> None:
        # confirmed_via is null on historical rows; source implies the same path.
        self.assertEqual(joined_via_label(None, "chat_extraction"), "From something you told Lana")
        self.assertIsNone(joined_via_label(None, None))


class TestGroundingStampsProvenance(unittest.TestCase):
    @patch("app.circles_flow._flush_parked_features")
    @patch("app.circles_flow._close_grounding_gap")
    @patch("app.circles_flow.upsert_canonical_place", return_value="p1")
    @patch("app.circles_flow.place_details", create=True)
    @patch("app.places.place_details")
    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._own_affiliation")
    def test_grounding_ask_is_recorded(
        self, own, sb, details, _details2, _upsert, _gap, _flush
    ) -> None:
        from app.circles_flow import ground_affiliation

        own.return_value = {"id": "a1", "circle_type": "fitness", "source": "chat_extraction"}
        details.return_value = {"place_id": "g1", "name": "OrangeTheory", "types": ["gym"]}
        table = _chain([])
        sb.return_value.table.return_value = table
        ground_affiliation("u1", "a1", "g1", open_enrichment_gap=False)
        self.assertEqual(table.update.call_args[0][0]["confirmed_via"], CONFIRMED_VIA_GROUNDING)

    @patch("app.circles_flow._flush_parked_features")
    @patch("app.circles_flow._close_grounding_gap")
    @patch("app.circles_flow.upsert_canonical_place", return_value="p1")
    @patch("app.places.place_details")
    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._own_affiliation")
    def test_profile_add_is_recorded(self, own, sb, details, _upsert, _gap, _flush) -> None:
        from app.circles_flow import ground_affiliation

        own.return_value = {"id": "a1", "circle_type": "fitness", "source": "profile_add"}
        details.return_value = {"place_id": "g1", "name": "OrangeTheory", "types": ["gym"]}
        table = _chain([])
        sb.return_value.table.return_value = table
        ground_affiliation("u1", "a1", "g1", open_enrichment_gap=False)
        self.assertEqual(table.update.call_args[0][0]["confirmed_via"], "profile_add")


if __name__ == "__main__":
    unittest.main()
