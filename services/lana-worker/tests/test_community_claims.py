"""Claiming a community from the profile (backend-asks §62) — the worker half.

The rules live in SQL (20270205120000) and are proven against a real database there. These
tests drive the REAL routes and module through FastAPI and assert on the serialized JSON:
what reaches the RPC, how a refusal word becomes a status + detail, and that a decision is
delivered once — the fake place_claims table really applies its filters, because a fake
that ignores them would pass the idempotency test with the guard deleted.
"""

import unittest
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.community_claims as cc
import app.main as main
from app.auth import AuthSession

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
REVIEWER = "99999999-9999-9999-9999-999999999999"
PLACE = "44444444-4444-4444-4444-444444444444"
CLAIM = "55555555-5555-5555-5555-555555555555"


def _auth(uid: str = ME, *, anonymous: bool = False, verified: bool = True) -> AuthSession:
    return AuthSession(
        user_id=uid, is_anonymous=anonymous, phone_verified=verified, home_block_id=None
    )


def _row(status: str = "pending", **extra: Any) -> dict[str, Any]:
    return {
        "claim_id": CLAIM,
        "reference": "CL-1042",
        "place_id": PLACE,
        "place_name": "Lake Nona Runners",
        "place_type": "fitness",
        "status": status,
        "reviewer_note": None,
        "decided_at": None,
        "submitted_at": "2026-10-12T10:00:00+00:00",
        **extra,
    }


class _RpcError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    """A table query that APPLIES eq / in_ / is_ — so a dropped guard changes the answer."""

    def __init__(self, db: "_FakeDb", table: str) -> None:
        self.db, self.table, self.filters, self.patch, self._limit = db, table, [], None, None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def update(self, patch: dict[str, Any]) -> "_Query":
        self.patch = patch
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col: str, vals: list[Any]) -> "_Query":
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def is_(self, col: str, val: str) -> "_Query":
        self.filters.append(lambda r: r.get(col) is None if val == "null" else True)
        return self

    @property
    def not_(self) -> "_Query":
        return self  # only used for the glyph read, which is decoration

    def order(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def execute(self) -> _Result:
        rows = [r for r in self.db.tables.get(self.table, []) if all(f(r) for f in self.filters)]
        if self.patch is not None:
            for r in rows:
                r.update(self.patch)
            return _Result([dict(r) for r in rows])
        return _Result([dict(r) for r in rows[: self._limit or None]])


class _Storage:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, dict]] = []

    def from_(self, bucket: str) -> "_Storage":
        self.bucket = bucket
        return self

    def upload(self, path: str, raw: bytes, file_options: dict) -> None:
        self.uploads.append((self.bucket, path, raw, file_options))


class _FakeDb:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "place_claims": [],
            "users": [{"id": REVIEWER, "founder_role": "internal"}, {"id": ME, "founder_role": None}],
            "places": [{"id": PLACE, "name": "Lake Nona Runners"}],
            "circle_affiliations": [],
        }
        self.rpcs: list[tuple[str, dict]] = []
        self.rpc_answer: dict[str, Any] = {}
        self.storage = _Storage()

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rpc(self, name: str, params: dict) -> Any:
        self.rpcs.append((name, params))
        answer = self.rpc_answer.get(name)

        class _Call:
            def execute(self) -> _Result:
                if isinstance(answer, Exception):
                    raise answer
                return _Result(answer)

        return _Call()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _FakeDb()
        self.client = TestClient(main.app)
        self.joined: list[tuple[str, str]] = []
        self.pushes: list[dict] = []
        self.mails: list[dict] = []
        patches = [
            patch.object(cc, "service_client", lambda: self.db),
            patch("app.community_discovery.join_community",
                  lambda uid, pid, **_k: self.joined.append((uid, pid)) or {}),
            patch("app.notifications.send_push",
                  lambda uid, **k: self.pushes.append({"user_id": uid, **k})),
            patch("app.notifications.send_email",
                  lambda to, **k: self.mails.append({"to": to, **k})),
            patch("app.notifications.recipient_lang", lambda _uid: None),
            patch("app.notifications._user_contact", lambda _uid: ("account@test.local", None)),
            patch.object(main, "amplitude_track", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.auth = _auth()
        p = patch.object(main, "verify_auth", lambda _h: self.auth)
        p.start()
        self.addCleanup(p.stop)

    def post(self, path: str, body: Any = None, **kw: Any):
        return self.client.post(path, json=body, headers={"Authorization": "Bearer t"}, **kw)


class TestSubmit(_Base):
    def test_submit_passes_her_fields_to_the_sql_and_returns_the_profile_row(self) -> None:
        self.db.rpc_answer["submit_community_claim_for"] = _row()
        res = self.post("/lana/circles/claims", {
            "place_id": PLACE,
            "contact_email": " owner@club.test ",
            "contact_phone": "+1 407 555 0100",
            "note": "I run Tuesdays",
            "proof_path": f"{ME}/abc.jpg",
        })
        self.assertEqual(res.status_code, 200, res.text)
        name, params = self.db.rpcs[-1]
        self.assertEqual(name, "submit_community_claim_for")
        self.assertEqual(params, {
            "p_user_id": ME,
            "p_place_id": PLACE,
            "p_contact_email": "owner@club.test",
            "p_contact_phone": "+1 407 555 0100",
            "p_note": "I run Tuesdays",
            "p_proof_path": f"{ME}/abc.jpg",
        })
        body = res.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["reference"], "CL-1042")
        self.assertEqual(body["circle_type"], "fitness")
        self.assertTrue(body["emoji"])
        self.assertNotIn("place_type", body)

    def test_someone_elses_proof_path_is_refused_before_the_sql(self) -> None:
        res = self.post("/lana/circles/claims", {
            "place_id": PLACE, "contact_email": "a@b.co", "proof_path": f"{OTHER}/x.jpg",
        })
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "invalid_proof"))
        self.assertEqual(self.db.rpcs, [])

    def test_traversal_in_proof_path_is_refused(self) -> None:
        res = self.post("/lana/circles/claims", {
            "place_id": PLACE, "contact_email": "a@b.co", "proof_path": f"{ME}/../{OTHER}/x.jpg",
        })
        self.assertEqual(res.json()["detail"], "invalid_proof")

    def test_bad_email_and_bad_place(self) -> None:
        res = self.post("/lana/circles/claims", {"place_id": PLACE, "contact_email": "nope"})
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "invalid_email"))
        res = self.post("/lana/circles/claims", {"place_id": "x", "contact_email": "a@b.co"})
        self.assertEqual(res.json()["detail"], "place_required")
        self.assertEqual(self.db.rpcs, [])

    def test_sql_refusals_travel_as_409_with_the_word(self) -> None:
        for word in ("claim_open", "already_claimed", "already_yours"):
            self.db.rpc_answer["submit_community_claim_for"] = _RpcError(word)
            res = self.post("/lana/circles/claims", {"place_id": PLACE, "contact_email": "a@b.co"})
            self.assertEqual((res.status_code, res.json()["detail"]), (409, word))

    def test_unknown_sql_failure_is_500_not_a_refusal(self) -> None:
        self.db.rpc_answer["submit_community_claim_for"] = _RpcError("deadlock detected")
        res = self.post("/lana/circles/claims", {"place_id": PLACE, "contact_email": "a@b.co"})
        self.assertEqual((res.status_code, res.json()["detail"]), (500, "claim_failed"))

    def test_an_unverified_guest_cannot_claim(self) -> None:
        self.auth = _auth(anonymous=True, verified=False)
        res = self.post("/lana/circles/claims", {"place_id": PLACE, "contact_email": "a@b.co"})
        self.assertEqual((res.status_code, res.json()["detail"]), (403, "sign_in_required"))


class TestProof(_Base):
    def _upload(self, raw: bytes, ctype: str):
        return self.client.post(
            "/lana/circles/claims/proof",
            files={"file": ("p.jpg", raw, ctype)},
            headers={"Authorization": "Bearer t"},
        )

    def test_stores_in_the_private_bucket_under_her_folder(self) -> None:
        res = self._upload(b"img", "image/jpeg")
        self.assertEqual(res.status_code, 200, res.text)
        path = res.json()["proof_path"]
        self.assertTrue(path.startswith(f"{ME}/") and path.endswith(".jpg"))
        bucket, stored_path, _raw, _opts = self.db.storage.uploads[-1]
        self.assertEqual((bucket, stored_path), ("community-claim-proofs", path))

    def test_type_and_size_caps(self) -> None:
        self.assertEqual(self._upload(b"x", "application/pdf").status_code, 415)
        self.assertEqual(self._upload(b"x" * (5 * 1024 * 1024 + 1), "image/png").status_code, 413)
        self.assertEqual(self.db.storage.uploads, [])


class TestMineAndClose(_Base):
    def test_mine_wraps_rows_in_claims(self) -> None:
        self.db.rpc_answer["my_community_claims_for"] = [_row("declined", reviewer_note="No match")]
        res = self.post("/lana/circles/claims/mine", {})
        self.assertEqual(res.status_code, 200, res.text)
        claims = res.json()["claims"]
        self.assertEqual([(c["status"], c["reviewer_note"]) for c in claims], [("declined", "No match")])
        self.assertEqual(self.db.rpcs[-1], ("my_community_claims_for", {"p_user_id": ME}))

    def test_mine_settles_her_owed_approval_silently(self) -> None:
        """An approval the portal never reported still makes her a member — without a push
        while she is looking at it."""
        self.db.tables["place_claims"] = [
            {"id": CLAIM, "requested_by": ME, "place_id": PLACE, "origin": "in_app",
             "status": "verified", "decision_notified_at": None},
            {"id": "other", "requested_by": OTHER, "place_id": PLACE, "origin": "in_app",
             "status": "verified", "decision_notified_at": None},
        ]
        self.db.rpc_answer["my_community_claims_for"] = []
        self.post("/lana/circles/claims/mine", {})
        self.assertEqual(self.joined, [(ME, PLACE)])
        self.assertEqual((self.pushes, self.mails), ([], []))
        owed = {r["id"]: r["decision_notified_at"] for r in self.db.tables["place_claims"]}
        self.assertIsNotNone(owed[CLAIM])
        self.assertIsNone(owed["other"])  # someone else's stays owed to them

    def test_close_passes_her_id_and_maps_not_decided(self) -> None:
        self.db.rpc_answer["close_community_claim_for"] = {"claim_id": CLAIM, "closed": True}
        res = self.post("/lana/circles/claims/close", {"claim_id": CLAIM})
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(self.db.rpcs[-1][1], {"p_user_id": ME, "p_claim_id": CLAIM})
        self.db.rpc_answer["close_community_claim_for"] = _RpcError("not_decided")
        res = self.post("/lana/circles/claims/close", {"claim_id": CLAIM})
        self.assertEqual((res.status_code, res.json()["detail"]), (409, "not_decided"))
        self.db.rpc_answer["close_community_claim_for"] = _RpcError("no_such_claim")
        res = self.post("/lana/circles/claims/close", {"claim_id": CLAIM})
        self.assertEqual(res.status_code, 404)


class TestDecisionHook(_Base):
    def _claim(self, status: str, **extra: Any) -> dict[str, Any]:
        row = {
            "id": CLAIM, "requested_by": ME, "place_id": PLACE, "origin": "in_app",
            "status": status, "decision_notified_at": None, "reference_no": 1042,
            "contact_email": "owner@club.test", "review_notes": None, **extra,
        }
        self.db.tables["place_claims"].append(row)
        return row

    def test_only_a_reviewer_may_call_it(self) -> None:
        self._claim("verified")
        res = self.post("/hooks/claim-decision", {"claim_id": CLAIM})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.joined, [])

    def test_approval_joins_her_and_tells_her_once(self) -> None:
        self._claim("verified")
        self.auth = _auth(REVIEWER)
        res = self.post("/hooks/claim-decision", {"claim_id": CLAIM})
        self.assertEqual(res.json(), {"ok": True, "delivered": 1})
        self.assertEqual(self.joined, [(ME, PLACE)])
        self.assertEqual([p["user_id"] for p in self.pushes], [ME])
        self.assertIn("Lake Nona Runners", self.pushes[0]["title"])
        self.assertEqual([m["to"] for m in self.mails], ["owner@club.test"])
        # The portal retries, or the sweep runs after it: nothing is sent twice.
        res = self.post("/hooks/claim-decision", {"claim_id": CLAIM})
        self.assertEqual(res.json()["delivered"], 0)
        self.assertEqual((len(self.joined), len(self.pushes), len(self.mails)), (1, 1, 1))

    def test_decline_mails_the_escaped_note_and_does_not_join(self) -> None:
        self._claim("rejected", review_notes="Email <b>didn't</b> match", contact_email=None)
        self.auth = _auth(REVIEWER)
        self.post("/hooks/claim-decision", {"claim_id": CLAIM})
        self.assertEqual(self.joined, [])
        self.assertEqual([m["to"] for m in self.mails], ["account@test.local"])  # no contact → account
        html = self.mails[0]["html"]
        self.assertIn("Email &lt;b&gt;didn&#x27;t&lt;/b&gt; match", html)
        self.assertIn("CL-1042", html)

    def test_pending_or_website_claims_are_never_delivered(self) -> None:
        self._claim("needs_more_info")
        self.db.tables["place_claims"].append({
            "id": "web", "requested_by": ME, "place_id": PLACE, "origin": "website",
            "status": "verified", "decision_notified_at": None,
        })
        self.auth = _auth(REVIEWER)
        self.assertEqual(self.post("/hooks/claim-decision", {"claim_id": CLAIM}).json()["delivered"], 0)
        self.assertEqual(self.post("/hooks/claim-decision", {"claim_id": "web"}).json()["delivered"], 0)
        self.assertEqual((self.joined, self.pushes), ([], []))

    def test_sweep_token_delivers_everything_owed(self) -> None:
        self._claim("verified")
        self.db.tables["place_claims"].append({
            "id": "c2", "requested_by": OTHER, "place_id": PLACE, "origin": "in_app",
            "status": "rejected", "decision_notified_at": None, "contact_email": "o@x.co",
        })
        with patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": "s3cret"}):
            res = self.client.post("/hooks/claim-decision", headers={"X-Sweep-Token": "s3cret"})
            self.assertEqual(res.json(), {"ok": True, "delivered": 2})
            res = self.client.post("/hooks/claim-decision", headers={"X-Sweep-Token": "s3cret"})
            self.assertEqual(res.json()["delivered"], 0)
        self.assertEqual(sorted(p["user_id"] for p in self.pushes), sorted([ME, OTHER]))

    def test_a_wrong_sweep_token_falls_back_to_reviewer_auth(self) -> None:
        self._claim("verified")
        with patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": "s3cret"}):
            res = self.client.post(
                "/hooks/claim-decision",
                headers={"X-Sweep-Token": "guess", "Authorization": "Bearer t"},
            )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
