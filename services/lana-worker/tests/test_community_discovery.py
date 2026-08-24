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


class TestNamedRosterTurn(unittest.TestCase):
    """"Who is in Mizu Sushi" — the ask that returned the member COUNT four times while
    the UI rendered all seven names one tap away (QA 2026-08-20)."""

    _MIZU = {"place_id": "pMizu", "place_name": "Mizu Sushi & Steakhouse", "member_count": 6}

    def _roster(self, **over):
        base = {
            "place_id": "pMizu",
            "place_name": "Mizu Sushi & Steakhouse",
            "member_count": 3,
            "curious_count": 0,
            "members": [
                {"peer_user_id": "u1", "nickname": "jake", "me": True, "attributes": []},
                {
                    "peer_user_id": "u2",
                    "nickname": "Natasha",
                    "avatar_url": None,
                    "attributes": ["Reads the newspaper", "Registered Nurse", "Plays badminton"],
                    "actions": [{"id": "nudge", "label": "Nudge", "message": "nudge Natasha"}],
                },
                {"peer_user_id": "u3", "nickname": "Rust", "attributes": [], "actions": []},
            ],
        }
        base.update(over)
        return base

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_named_community_returns_the_people_not_the_count(
        self, compose, mine, _disc, members
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        members.return_value = self._roster()
        ctx: dict = {}
        out = communities_chat_turn(
            "u1",
            message="who are the members of the Mizu Sushi community",
            session_ctx=ctx,
            community_name="Mizu Sushi",
            community_ask="people",
        )
        self.assertEqual(out, "composed")
        # The people reached the card surface, self excluded.
        rows = ctx["peer_matches"]
        self.assertEqual([r["nickname"] for r in rows], ["Natasha", "Rust"])
        # Their own threads ride as the label + chips, and nothing invented a similarity.
        self.assertEqual(rows[0]["matching_peer_label"], "Reads the newspaper")
        self.assertEqual(rows[0]["trait_tags"], ["Registered Nurse", "Plays badminton"])
        self.assertIsNone(rows[0]["similarity_score"])
        # Somebody with nothing public still says something true.
        self.assertEqual(rows[1]["matching_peer_label"], "Goes to Mizu Sushi & Steakhouse")
        # Marked final, so stamp_peer_discovery_ctx leaves the chips alone.
        self.assertTrue(all(r["community_roster"] for r in rows))
        # The names are the composer's to anchor on, and a roster turn is not a join offer.
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("Natasha", facts)
        self.assertIsNone(ctx["community_join_pending"])
        # member_count counts the caller and excludes curious joiners; the cards do the
        # opposite. Both numbers are stated so the reply can't put one over the other.
        self.assertIn("People who go here: 3", facts)
        self.assertIn("Cards under your message: 2", facts)

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_curious_joiners_are_named_as_such_not_folded_in(
        self, compose, mine, _disc, members
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        members.return_value = self._roster(member_count=2, curious_count=1)
        communities_chat_turn(
            "u1", message="who is in Mizu Sushi", session_ctx={}, community_name="Mizu Sushi", community_ask="people"
        )
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("People who go here: 2", facts)
        self.assertIn("Cards under your message: 2", facts)
        self.assertIn("curious, not members", facts)

    @patch("app.community_surface.community_members", side_effect=ValueError("not_a_member"))
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_non_member_is_told_the_limit_and_offered_the_join(
        self, compose, _mine, disc, _members
    ) -> None:
        from app.community_discovery import communities_chat_turn

        disc.return_value = [dict(self._MIZU, is_member=False)]
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="who is in Mizu Sushi", session_ctx=ctx, community_name="Mizu Sushi", community_ask="people"
        )
        kwargs = compose.call_args.kwargs
        # §9: the limit is stated, not redirected around — and the join is armed so a
        # "yes" is the answer rather than a dead end.
        self.assertIn("NOT in it", " ".join(kwargs["facts"]))
        self.assertIn("can't show", kwargs["fallback"].lower())
        self.assertEqual(ctx["community_join_pending"]["places"][0]["place_id"], "pMizu")

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_only_member_is_not_told_the_place_is_dead(
        self, compose, mine, _disc, members
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        members.return_value = self._roster(
            member_count=1,
            members=[{"peer_user_id": "u1", "nickname": "jake", "me": True, "attributes": []}],
        )
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="who else is in there", session_ctx=ctx, community_name="Mizu Sushi", community_ask="people"
        )
        self.assertNotIn("peer_matches", ctx)
        self.assertIn("only one", " ".join(compose.call_args.kwargs["facts"]).lower())

    @patch("app.community_surface.communities_card", return_value=None)
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_unknown_name_says_so_instead_of_listing_something_else(
        self, compose, _mine, _disc, _card
    ) -> None:
        from app.community_discovery import communities_chat_turn

        communities_chat_turn(
            "u1",
            message="who is in the Pickleball Barn",
            session_ctx={},
            community_name="Pickleball Barn",
        )
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("Pickleball Barn", facts)
        self.assertIn("NO community by that name", facts)

    def test_roster_rows_survive_the_response_gate(self) -> None:
        """The rows reached the FE only once discovery.communities was allowlisted.

        Built, stamped, then filtered to [] by main._onboarding_fields, so Lana's line
        promised "cards for Tommaso db and Natasha just below" over an empty screen —
        the same drop social.propose_intro hit in 2026-08-18.
        """
        from app.ui_intent import PEER_DISCOVERY_ACTIVE_INTENTS

        rows = [{"peer_user_id": "u2"}]
        active = "discovery.communities"
        self.assertTrue(bool(rows) and active in PEER_DISCOVERY_ACTIVE_INTENTS)

    @patch("app.community_surface.communities_card", return_value=None)
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_list_turn_clears_a_previous_roster_cards(
        self, _compose, _mine, _disc, _card
    ) -> None:
        from app.community_discovery import communities_chat_turn

        # peer_matches is not turn-scoped, and this intent now renders it.
        ctx = {"peer_matches": [{"peer_user_id": "u2"}], "discovery_surface": {"x": 1}}
        communities_chat_turn("u1", message="what communities am i in", session_ctx=ctx)
        self.assertIsNone(ctx["peer_matches"])
        self.assertIsNone(ctx["discovery_surface"])

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_roster_drops_a_stale_scored_strip(self, _c, mine, _d, members) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        members.return_value = self._roster()
        ctx = {"discovery_surface": {"strong_count": 3, "status_label": "3 strong"}}
        communities_chat_turn(
            "u1", message="who is in Mizu Sushi", session_ctx=ctx, community_name="Mizu Sushi", community_ask="people"
        )
        # Nothing here compared two people, so there is no strip to show.
        self.assertIsNone(ctx["discovery_surface"])
        self.assertTrue(ctx["peer_matches"])

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_curious_rows_carry_their_tag_to_the_card(
        self, _compose, mine, _disc, members
    ) -> None:
        # The community screen has always tagged a curious joiner; the chat card rendered
        # them as a member because the field never left the roster row.
        mine.return_value = [self._MIZU]
        members.return_value = self._roster(
            member_count=2,
            curious_count=1,
            members=[
                {"peer_user_id": "u1", "nickname": "jake", "me": True, "attributes": []},
                {"peer_user_id": "u2", "nickname": "Natasha", "membership": "member",
                 "attributes": ["Reads the newspaper"]},
                {"peer_user_id": "u3", "nickname": "Pouya", "membership": "curious",
                 "attributes": ["Likes steakhouses"]},
            ],
        )
        ctx: dict = {}
        communities_chat_turn = __import__(
            "app.community_discovery", fromlist=["communities_chat_turn"]
        ).communities_chat_turn
        communities_chat_turn(
            "u1", message="who is in Mizu Sushi", session_ctx=ctx, community_name="Mizu Sushi", community_ask="people"
        )
        self.assertEqual(
            {r["nickname"]: r["membership"] for r in ctx["peer_matches"]},
            {"Natasha": "member", "Pouya": "curious"},
        )

    @patch("app.community_surface.community_members")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_roster_bigger_than_the_wire_says_so(
        self, compose, mine, _disc, members
    ) -> None:
        """main._peer_matches_from_ctx ships at most 8 rows. Claiming 11 cards below 8 of
        them is the same count-vs-roster mismatch, one layer up."""
        from app.community_discovery import _ROSTER_CARDS_MAX, communities_chat_turn

        mine.return_value = [self._MIZU]
        roster = [{"peer_user_id": "u1", "nickname": "jake", "me": True, "attributes": []}]
        roster += [
            {"peer_user_id": f"u{i}", "nickname": f"N{i}", "membership": "member",
             "attributes": [f"Does thing {i}"]}
            for i in range(2, 14)
        ]
        members.return_value = self._roster(member_count=13, members=roster)
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="who is in Mizu Sushi", session_ctx=ctx, community_name="Mizu Sushi", community_ask="people"
        )
        self.assertEqual(len(ctx["peer_matches"]), _ROSTER_CARDS_MAX)
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn(f"Cards under your message: {_ROSTER_CARDS_MAX}", facts)
        # 12 others, 8 shown — the remainder is stated, never silently dropped.
        self.assertIn("4 more are here without a card", facts)

    def test_ampersand_and_filler_name_the_same_place(self) -> None:
        """The bug in the wild, 2026-08-21: they typed "mizu sushi and steakhouse", the row
        is "Mizu Sushi & Steakhouse", raw containment missed, and the reply said it could
        not find a community it then said they were already in."""
        from app.community_discovery import _same_place_name

        row = "Mizu Sushi & Steakhouse"
        for said in (
            "mizu sushi and steakhouse",
            "Mizu Sushi",
            "the Mizu Sushi & Steakhouse community",
            "mizu-sushi steakhouse",
            "MIZU SUSHI AND STEAKHOUSE",
        ):
            self.assertTrue(_same_place_name(said, row), said)
        # And it still refuses the places that merely share a word.
        self.assertFalse(_same_place_name("Trinity Church", row))
        self.assertFalse(_same_place_name("Lp Fit", "FIT 407 Lake Nona"))
        # Whole words only. Unpadded containment let "a" match "and" and "fit" match
        # "Fitness", so any short fragment claimed a specific place.
        self.assertFalse(_same_place_name("a", row))
        self.assertFalse(_same_place_name("on", "Crunch Fitness - Lake Nona"))
        self.assertFalse(_same_place_name("fit", "Crunch Fitness - Lake Nona"))
        # An exact word still counts.
        self.assertTrue(_same_place_name("fit", "FIT 407 Lake Nona"))

    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_near_miss_asks_which_one_with_tapable_names(
        self, compose, _mine, disc
    ) -> None:
        from app.community_discovery import communities_chat_turn

        disc.return_value = [
            {"place_id": "p1", "place_name": "FIT 407 Lake Nona", "member_count": 1},
            {"place_id": "p2", "place_name": "Crunch Fitness - Lake Nona", "member_count": 1},
            {"place_id": "p3", "place_name": "Mizu Sushi & Steakhouse", "member_count": 7},
        ]
        ctx: dict = {}
        communities_chat_turn(
            "u1",
            message="who is in the lake nona gym",
            session_ctx=ctx,
            community_name="Lake Nona gym",
            community_ask="people",
        )
        # Both Lake Nona places are candidates; Mizu shares no word and is not offered.
        chips = ctx["policy_chips"]
        self.assertEqual(
            [c["label"] for c in chips], ["FIT 407 Lake Nona", "Crunch Fitness - Lake Nona"]
        )
        # The tap posts the EXACT row name AND their own question back, so the next turn
        # matches without guessing and without changing what they asked.
        self.assertEqual(chips[0]["send"], "who is in FIT 407 Lake Nona")
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("do NOT know which", facts.replace("You ", ""))

    @patch("app.community_surface.communities_card", return_value=None)
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_name_sharing_nothing_still_says_it_plainly(
        self, compose, _mine, _disc, _card
    ) -> None:
        from app.community_discovery import communities_chat_turn

        communities_chat_turn(
            "u1",
            message="who is in the Pickleball Barn",
            session_ctx={},
            community_name="Pickleball Barn",
        )
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("NO community by that name", facts)

    def test_name_matches_either_direction(self) -> None:
        from app.community_discovery import _resolve_named_community

        with patch("app.community_discovery._my_communities", return_value=[self._MIZU]), patch(
            "app.community_discovery.discover_communities", return_value=[]
        ):
            # Shorter than the row, and longer than it — both are the same place.
            for said in ("Mizu Sushi", "the Mizu Sushi & Steakhouse community", "MIZU SUSHI"):
                self.assertEqual(
                    (_resolve_named_community("u1", said, locale="en") or {}).get("place_id"),
                    "pMizu",
                    said,
                )
            self.assertIsNone(_resolve_named_community("u1", "Trinity Church", locale="en"))


class TestAboutACommunity(unittest.TestCase):
    """"What type of community is this barnes and nobel" — the screen shows the type, the
    description, what it has and what is on; chat answered with a roster refusal and then
    a list of other communities, and the clarifier looped three times (QA 2026-08-21)."""

    _BN = {"place_id": "pBN", "place_name": "Barnes & Noble", "member_count": 1}

    _PROFILE = {
        "place_name": "Barnes & Noble",
        "place_address": "123 Main St, Orlando, FL",
        "relation": "bookstore",
        "description": "A local bookstore with a cafe.",
        "membership": "visitor",
        "member_count": 1,
        "curious_count": 0,
        "features": [{"label": "Cafe"}, {"label": "Reading nooks"}],
        "upcoming_events": [{"title": "Author talk", "starts_at": "2026-09-01T18:00:00Z"}],
    }

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_the_place_facts_reach_the_composer(self, compose, mine, _disc, prof) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._BN]
        prof.return_value = dict(self._PROFILE, membership="member")
        ctx: dict = {}
        out = communities_chat_turn(
            "u1",
            message="what type of community is Barnes & Noble",
            session_ctx=ctx,
            community_name="Barnes & Noble",
            community_ask="about",
        )
        self.assertEqual(out, "composed")
        facts = " ".join(compose.call_args.kwargs["facts"])
        for expected in ("bookstore", "A local bookstore with a cafe.", "Cafe", "Author talk"):
            self.assertIn(expected, facts)
        # An about-turn shows no faces.
        self.assertIsNone(ctx["peer_matches"])

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_non_member_still_gets_told_what_the_place_is(
        self, compose, _mine, disc, prof
    ) -> None:
        # You do not have to join a bookstore to be told it is a bookstore — the profile
        # head opens for a visitor, unlike the roster.
        from app.community_discovery import communities_chat_turn

        disc.return_value = [dict(self._BN, is_member=False)]
        prof.return_value = dict(self._PROFILE)
        ctx: dict = {}
        communities_chat_turn(
            "u1",
            message="what kind of place is Barnes & Noble",
            session_ctx=ctx,
            community_name="Barnes & Noble",
            community_ask="about",
        )
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("bookstore", facts)
        self.assertIn("NOT in this community", facts)
        # ...and a "yes" after it means something.
        self.assertEqual(ctx["community_join_pending"]["places"][0]["place_id"], "pBN")

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_typo_with_one_candidate_is_answered_not_re_asked(
        self, compose, mine, _disc, prof
    ) -> None:
        """The loop: "barnes and nobel" produced "did you mean Barnes & Noble?" three
        times in a row, because nothing consumed the answer."""
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._BN]
        prof.return_value = dict(self._PROFILE, membership="member")
        ctx: dict = {}
        communities_chat_turn(
            "u1",
            message="what type of community is this barnes and nobel",
            session_ctx=ctx,
            community_name="barnes and nobel",
            community_ask="about",
        )
        # Answered, not clarified.
        self.assertNotIn("policy_chips", ctx)
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("bookstore", facts)
        # And it must NAME the place it chose, since the name was not exact.
        self.assertIn("barnes and nobel", facts)
        self.assertIn("name it in your reply", facts)

    @patch("app.community_surface.community_members")
    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_people_still_go_to_the_roster(
        self, _compose, mine, _disc, prof, members
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._BN]
        members.return_value = {
            "member_count": 2,
            "curious_count": 0,
            "members": [
                {"peer_user_id": "u1", "me": True, "attributes": []},
                {"peer_user_id": "u2", "nickname": "Ann", "membership": "member",
                 "attributes": ["Reads a lot"]},
            ],
        }
        ctx: dict = {}
        communities_chat_turn(
            "u1",
            message="who is in Barnes & Noble",
            session_ctx=ctx,
            community_name="Barnes & Noble",
            community_ask="people",
        )
        prof.assert_not_called()
        self.assertEqual([r["nickname"] for r in ctx["peer_matches"]], ["Ann"])


class TestTheClarifierAsksTheirQuestion(unittest.TestCase):
    """Two bugs from one screenshot, 2026-08-21."""

    _BN = {"place_id": "pBN", "place_name": "Barnes & Noble", "member_count": 1}
    _MIZU = {"place_id": "pMizu", "place_name": "Mizu Sushi & Steakhouse", "member_count": 7}

    def test_and_is_not_a_name_word(self) -> None:
        """_name_tokens turns "&" into "and", so "Mizu Sushi & Steakhouse" shared the word
        "and" with "barnes and nobel" — a sushi restaurant offered as a candidate for a
        bookstore, and two candidates then stalled into a clarifier."""
        from app.community_discovery import _near_name_candidates

        found = _near_name_candidates("barnes and nobel", [[self._BN, self._MIZU]])
        self.assertEqual([c["place_id"] for c in found], ["pBN"])

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_typo_among_other_places_is_still_answered(
        self, compose, _mine, disc, prof
    ) -> None:
        from app.community_discovery import communities_chat_turn

        disc.return_value = [self._BN, self._MIZU]
        prof.return_value = {
            "place_name": "Barnes & Noble", "relation": "bookstore", "membership": "visitor",
            "member_count": 1, "curious_count": 0, "features": [], "upcoming_events": [],
        }
        ctx: dict = {}
        communities_chat_turn(
            "u1",
            message="what type of community is barnes and nobel",
            session_ctx=ctx,
            community_name="barnes and nobel",
            community_ask="about",
        )
        # Answered about the bookstore — not asked which of two unrelated places.
        self.assertIn(
            "What kind of place it is: bookstore", compose.call_args.kwargs["facts"]
        )
        self.assertEqual(
            [c["label"] for c in ctx["policy_chips"]], ["Add me", "Show me others"]
        )

    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_the_chip_re_asks_their_question_not_the_roster(self, _c, _mine, disc) -> None:
        """Tapping a candidate posted "who is in X" whatever they had asked, so someone
        asking what kind of place it was got the roster question put in their mouth."""
        from app.community_discovery import communities_chat_turn

        disc.return_value = [
            {"place_id": "p1", "place_name": "FIT 407 Lake Nona", "member_count": 1},
            {"place_id": "p2", "place_name": "Crunch Fitness - Lake Nona", "member_count": 1},
        ]
        sends = {}
        for ask in ("about", "people"):
            ctx: dict = {}
            communities_chat_turn(
                "u1",
                message="the lake nona gym",
                session_ctx=ctx,
                community_name="Lake Nona gym",
                community_ask=ask,
            )
            sends[ask] = [c["send"] for c in ctx["policy_chips"]]
        self.assertEqual(sends["about"][0], "what kind of place is FIT 407 Lake Nona")
        self.assertEqual(sends["people"][0], "who is in FIT 407 Lake Nona")


class TestCommunityEventsRender(unittest.TestCase):
    """"Are there any events on sushi and social" named the meet in prose with nothing on
    screen, and the follow-up "show me that event" re-searched the whole area and returned
    an unrelated one (QA 2026-08-21)."""

    _MIZU = {"place_id": "pM", "place_name": "Mizu Sushi & Steakhouse", "member_count": 7}
    _PROF = {
        "place_name": "Mizu Sushi & Steakhouse", "relation": "restaurant",
        "membership": "member", "member_count": 7, "curious_count": 1,
        "features": [{"label": "Charging station"}],
        "upcoming_events": [
            {"event_id": "e1", "title": "Sushi & Social Meetup",
             "starts_at": "2026-08-22T18:00:00Z", "has_time": True,
             "venue_name": "Mizu Sushi & Steakhouse", "going_count": 3}
        ],
    }

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_the_meet_ships_as_a_card_with_its_id(self, compose, mine, _d, prof) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        prof.return_value = self._PROF
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="are there any events on sushi and social", session_ctx=ctx,
            community_name="Sushi & Social", community_ask="about",
        )
        cards = ctx["activity_previews"]
        self.assertEqual([c["title"] for c in cards], ["Sushi & Social Meetup"])
        # The id is what makes the card openable — without it there is nothing to tap.
        self.assertEqual(cards[0]["activity_id"], "e1")
        # And the reply must point at it instead of describing it.
        self.assertIn("right below", " ".join(compose.call_args.kwargs["facts"]))

    def test_the_response_gate_lets_them_through(self) -> None:
        """main._onboarding_fields drops activity_previews unless the intent is this one."""
        from app.ui_intent import UI_INTENT_SHOW_ACTIVITY_PREVIEW, derive_ui_intent

        ctx = {"active_intent": "discovery.communities"}
        self.assertEqual(
            derive_ui_intent(ctx, activity_count=1, phone_verified=True),
            UI_INTENT_SHOW_ACTIVITY_PREVIEW,
        )
        # No events on the turn → no claim on the surface.
        self.assertNotEqual(
            derive_ui_intent(ctx, activity_count=0, phone_verified=True),
            UI_INTENT_SHOW_ACTIVITY_PREVIEW,
        )

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities")
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_a_place_with_no_meets_clears_a_previous_turns_cards(
        self, _c, mine, _d, prof
    ) -> None:
        from app.community_discovery import communities_chat_turn

        mine.return_value = [self._MIZU]
        prof.return_value = dict(self._PROF, upcoming_events=[])
        # activity_previews is not turn-scoped, so a browse lane's events would ride in.
        ctx: dict = {"activity_previews": [{"title": "Florida Game Room Social"}]}
        communities_chat_turn(
            "u1", message="what is Mizu Sushi", session_ctx=ctx,
            community_name="Mizu Sushi", community_ask="about",
        )
        self.assertIsNone(ctx["activity_previews"])


class TestEveryOfferIsTapable(unittest.TestCase):
    """"Want me to add you?" with nothing to press is not an offer (QA 2026-08-21)."""

    _BN = {"place_id": "pBN", "place_name": "Barnes & Noble", "member_count": 1}

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_about_turn_offer_carries_a_join_chip(self, _c, _mine, disc, prof) -> None:
        from app.community_discovery import communities_chat_turn

        disc.return_value = [dict(self._BN, is_member=False)]
        prof.return_value = {
            "place_name": "Barnes & Noble", "relation": "bookstore", "membership": "visitor",
            "member_count": 1, "curious_count": 0, "features": [], "upcoming_events": [],
        }
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="what is Barnes & Noble", session_ctx=ctx,
            community_name="Barnes & Noble", community_ask="about",
        )
        chips = ctx["policy_chips"]
        # "Join <name>" is what the join lane already reads, so the tap lands.
        self.assertEqual(chips[0]["send"], "Join Barnes & Noble")
        self.assertEqual(ctx["community_join_pending"]["places"][0]["place_id"], "pBN")

    @patch("app.community_surface.community_members", side_effect=ValueError("not_a_member"))
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_roster_refusal_offer_carries_a_join_chip(self, _c, _mine, disc, _m) -> None:
        from app.community_discovery import communities_chat_turn

        disc.return_value = [dict(self._BN, is_member=False)]
        ctx: dict = {}
        communities_chat_turn(
            "u1", message="who is in Barnes & Noble", session_ctx=ctx,
            community_name="Barnes & Noble", community_ask="people",
        )
        self.assertEqual(ctx["policy_chips"][0]["send"], "Join Barnes & Noble")

    @patch("app.community_surface.community_profile")
    @patch("app.community_discovery.discover_communities")
    @patch("app.community_discovery._my_communities", return_value=[])
    @patch("app.reply_compose.compose_reply", return_value="composed")
    def test_an_empty_calendar_is_a_fact_not_a_failed_lookup(self, compose, _m, disc, prof):
        from app.community_discovery import communities_chat_turn

        disc.return_value = [dict(self._BN, is_member=False)]
        prof.return_value = {
            "place_name": "Barnes & Noble", "relation": "bookstore", "membership": "visitor",
            "member_count": 1, "curious_count": 0, "features": [], "upcoming_events": [],
        }
        communities_chat_turn(
            "u1", message="any events at Barnes & Noble?", session_ctx={},
            community_name="Barnes & Noble", community_ask="about",
        )
        facts = " ".join(compose.call_args.kwargs["facts"])
        self.assertIn("Nothing is scheduled there", facts)
        self.assertIn("never as something you were unable to look up", facts)


class TestRoutingKeepsTheTurnsSurfaces(unittest.TestCase):
    """_routing_ctx calls clear_turn_surfaces, so anything this lane stamps has to be
    re-attached by name. The "did you mean Barnes & Noble?" clarifier shipped its question
    with no tap-able answers because policy_chips was not on that list (2026-08-21)."""

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_discovery.communities_chat_turn")
    def test_every_surface_the_lane_stamps_survives(self, chat, _linear, _met) -> None:
        from app.discovery_route import _try_layer1_intent_turn
        from app.turn_surfaces import TURN_SCOPED_SURFACES

        stamped = {
            "policy_chips": [{"label": "Barnes & Noble", "send": "who is in Barnes & Noble"}],
            "community_discovery": {"communities": [], "total": 0},
            "communities_card": {"items": [{"affiliation_id": "a1"}]},
            "peer_matches": [{"peer_user_id": "u2"}],
        }

        def _stamp(
            user_id, *, message, session_ctx, locale="en", community_name=None,
            community_ask="about",
        ):
            session_ctx.update(stamped)
            return "composed"

        chat.side_effect = _stamp
        out = _try_layer1_intent_turn(
            msg="who is in Barnes and Noble",
            slots={},
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            phase="listening",
            user_id="u1",
        )
        self.assertIsNotNone(out)
        _reply, ctx, _routing, _actions = out
        # The invariant: what the lane stamped is what the turn ships.
        for key, value in stamped.items():
            self.assertEqual(ctx.get(key), value, key)
        # And the reason three of them need naming: clear_turn_surfaces nulls them.
        # peer_matches is deliberately NOT turn-scoped (it is cross-turn state, cleared by
        # hand on the paths that must not show cards), so it survives on its own.
        self.assertIn("policy_chips", TURN_SCOPED_SURFACES)
        self.assertIn("communities_card", TURN_SCOPED_SURFACES)
        self.assertIn("community_discovery", TURN_SCOPED_SURFACES)
        self.assertNotIn("peer_matches", TURN_SCOPED_SURFACES)


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
