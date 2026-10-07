"""Idempotent LOCAL seed for the Oct-7 e2e harness (scripts/e2e/sjsu_oct7.py).

Mirrors the prod shape behind Pouya's SJSU reports, plus a second, unrelated community
(UCF + a robotics chapter) so the harness proves generality rather than one phrasing.

Refuses to run against anything that is not 127.0.0.1/localhost. Every row it owns is keyed
by a fixed google_place_id / email / title, so a re-run updates in place instead of
duplicating. Event dates are computed relative to the run date (America/Los_Angeles).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from zoneinfo import ZoneInfo

import httpx

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
PW = "E2eOct7!pass1"

SJSU_KEY = "creator:san_jose_state_university"
RCC_KEY = "e2e:sjsu_rcc"
UCF_KEY = "e2e:university_of_central_florida"
UCF_ROBOTICS_KEY = "e2e:ucf_robotics_club"
PAUSA_KEY = "e2e:pausa_st_cloud"

RCC_BLURB = (
    "The Responsible Computing Club (RCC) at San José State University, in partnership with "
    "Mozilla, explores the ethics and social impact of computing — AI ethics, data and power, "
    "algorithmic fairness — through guest speakers, case competitions and cross-disciplinary "
    "project teams. Open to every major: about a fifth of members come from outside tech."
)
UCF_ROBOTICS_BLURB = (
    "The Robotics Club at the University of Central Florida builds autonomous robots for "
    "intercollegiate competitions — drones, rovers and underwater vehicles — and runs weekly "
    "build nights where first-years learn soldering, CAD and embedded programming."
)

# zip5 -> (lat, lng, city)
ZIPS = {
    "94404": (37.5585, -122.2711, "Foster City"),
    "95192": (37.3352, -121.8811, "San Jose"),
    "95138": (37.2510, -121.7680, "San Jose"),
    "55401": (44.9833, -93.2683, "Minneapolis"),
    "34771": (28.2730, -81.1860, "St. Cloud"),
    "32827": (28.3647, -81.2568, "Lake Nona"),
    "32816": (28.6024, -81.2001, "Orlando"),
}

USERS = {
    # key: (email, nickname, home_zip)
    "host": ("e2e-oct7-host@test.local", "Hana", "94404"),
    "member": ("e2e-oct7-member@test.local", "Milo", "94404"),
    "newbie": ("e2e-oct7-newbie@test.local", "Nora", "55401"),
    "ucf_member": ("e2e-oct7-ucf@test.local", "Uma", "32827"),
    "pausa_host": ("e2e-oct7-pausa@test.local", "Pia", "34771"),
}


class Seeder:
    def __init__(self, env: dict[str, str], db_url: str):
        self.url = env["SUPABASE_URL"].rstrip("/")
        if not any(h in self.url for h in ("127.0.0.1", "localhost")):
            raise SystemExit(f"REFUSING: SUPABASE_URL is not local ({self.url})")
        if not any(h in db_url for h in ("127.0.0.1", "localhost")):
            raise SystemExit("REFUSING: DB url is not local")
        self.anon = env["SUPABASE_ANON_KEY"]
        self.svc = env["SUPABASE_SERVICE_ROLE_KEY"]
        self.db = db_url
        self.psql = shutil.which("psql") or "/usr/local/opt/postgresql@17/bin/psql"
        self.http = httpx.Client(timeout=60)

    # ── plumbing ────────────────────────────────────────────────────────────────────
    def sql(self, q: str) -> str:
        r = subprocess.run(
            [self.psql, self.db, "-At", "-v", "ON_ERROR_STOP=1", "-c", q],
            capture_output=True, text=True,
        )
        if r.returncode:
            raise RuntimeError(f"SQL failed: {r.stderr.strip()}\n--- {q[:400]}")
        return r.stdout.strip()

    @staticmethod
    def lit(v) -> str:
        if v is None:
            return "null"
        return "'" + str(v).replace("'", "''") + "'"

    def user(self, email: str) -> tuple[str, str]:
        h = {"apikey": self.svc, "Authorization": f"Bearer {self.svc}"}
        r = self.http.post(f"{self.url}/auth/v1/admin/users", headers=h,
                           json={"email": email, "password": PW, "email_confirm": True})
        if r.status_code >= 300 and "already" not in r.text.lower():
            raise RuntimeError(f"create user {email}: {r.status_code} {r.text[:200]}")
        uid = self.sql(f"select id from auth.users where email={self.lit(email)}")
        return uid, self.token(email)

    def token(self, email: str) -> str:
        r = self.http.post(f"{self.url}/auth/v1/token?grant_type=password",
                           headers={"apikey": self.anon}, json={"email": email, "password": PW})
        r.raise_for_status()
        return r.json()["access_token"]

    # ── seed ────────────────────────────────────────────────────────────────────────
    def zips(self) -> None:
        for z, (lat, lng, city) in ZIPS.items():
            self.sql(f"select public.create_block_for_zip({self.lit(z)}, {lat}, {lng}, {self.lit(city)})")

    def place(self, key: str, **f) -> str:
        cols = {"google_place_id": key, "source": "import", **f}
        names = ", ".join(cols)
        vals = ", ".join(self.lit(v) if not isinstance(v, (int, float)) or isinstance(v, bool)
                         else str(v) for v in cols.values())
        upd = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "google_place_id")
        return self.sql(
            f"insert into public.places ({names}) values ({vals}) "
            f"on conflict (google_place_id) do update set {upd} returning id"
        )

    def member(self, uid: str, place_id: str, circle_type: str, name: str) -> None:
        self.sql(
            f"""insert into public.circle_affiliations
                  (user_id, circle_type, circle_key, place_ref, status, source, confirmed_via,
                   confidence, place_name)
                select {self.lit(uid)}, {self.lit(circle_type)}, {self.lit('m_e2e_' + place_id[:8])},
                       {self.lit(place_id)}, 'confirmed', 'community_join', 'community_join', 0.9,
                       {self.lit(name)}
                where not exists (select 1 from public.circle_affiliations
                                  where user_id={self.lit(uid)} and place_ref={self.lit(place_id)});
                update public.circle_affiliations set status='confirmed', dismissed_at=null
                 where user_id={self.lit(uid)} and place_ref={self.lit(place_id)};"""
        )

    def reset_user(self, uid: str, nickname: str, zip5: str) -> None:
        """Baseline a user's profile — called before every scenario so S9/S10 can't leak."""
        email = self.sql(f"select email from auth.users where id={self.lit(uid)}")
        phone = "+1555" + uid.replace("-", "")[:7].translate(str.maketrans("abcdef", "123456"))
        self.sql(
            f"""insert into public.users (id, email) values ({self.lit(uid)}, {self.lit(email)})
                  on conflict (id) do nothing;
                update public.users set nickname={self.lit(nickname)}, full_name={self.lit(nickname + ' E2E')},
                  home_zip={self.lit(zip5)}, home_block_id={self.lit('zip-' + zip5)},
                  email={self.lit(email)}, email_verified_at=coalesce(email_verified_at, now()),
                  phone={self.lit(phone)}, phone_verified_at=coalesce(phone_verified_at, now()),
                  locale='en'
                where id={self.lit(uid)};"""
        )

    def event(self, host: str, title: str, desc: str, start: dt.datetime, hours: float,
              venue: str, block: str, lat: float, lng: float, community: str | None,
              tags: str) -> str:
        end = start + dt.timedelta(hours=hours)
        self.sql(f"delete from public.events where host_id={self.lit(host)} and title={self.lit(title)}")
        return self.sql(
            f"""insert into public.events (host_id, cluster_id, block_id, title, description,
                   starts_at, ends_at, location, venue_name, cohort_tags, max_attendees,
                   auto_approve, status, has_time, host_rsvp_status, circle_place_ref, is_private)
                values ({self.lit(host)}, 'lake-nona', {self.lit(block)}, {self.lit(title)},
                   {self.lit(desc)}, {self.lit(start.isoformat())}, {self.lit(end.isoformat())},
                   extensions.st_setsrid(extensions.st_makepoint({lng}, {lat}), 4326)::extensions.geography,
                   {self.lit(venue)}, {self.lit('{' + tags + '}')}, 30, true, 'open', true, 'going',
                   {self.lit(community)}, false)
                returning id"""
        )

    def run(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(PT)
        today = now.astimezone(PT).date()

        def next_dow(dow: int) -> dt.date:  # Mon=0 … strictly after today, ≤ 7 days out
            d = (dow - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=d)

        def at(d: dt.date, h: int, tz=PT) -> dt.datetime:
            return dt.datetime(d.year, d.month, d.day, h, 0, tzinfo=tz)

        self.zips()
        out: dict = {"users": {}, "places": {}, "events": {}, "dates": {}}
        for k, (email, nick, z) in USERS.items():
            uid, tok = self.user(email)
            self.reset_user(uid, nick, z)
            out["users"][k] = {"id": uid, "email": email, "nickname": nick, "home_zip": z, "jwt": tok}
        host = out["users"]["host"]["id"]

        sjsu = self.place(
            SJSU_KEY, name="San Jose State University", place_type="school",
            lat=37.3352, lng=-121.8811, zip="95192", address="1 Washington Sq, San Jose, CA 95192",
            hq_city="San Jose, CA, USA", hq_lat=37.3352, hq_lng=-121.8811,
            governance_state="operator_verified", claimed_by=host,
            blurb="San José State University students, alumni and staff — clubs, meetups and campus life.",
        )
        self.sql(f"update public.places set claimed_at=coalesce(claimed_at, now()), "
                 f"verified_at=coalesce(verified_at, now()) where id={self.lit(sjsu)}")
        rcc = self.place(
            RCC_KEY, name="Responsible Computing Club (RCC)", place_type="school",
            lat=37.3352, lng=-121.8811, zip="95192", parent_place_ref=sjsu, blurb=RCC_BLURB,
            hq_city="San Jose, CA, USA",
        )
        ucf = self.place(
            UCF_KEY, name="University of Central Florida", place_type="school",
            lat=28.6024, lng=-81.2001, zip="32816", address="4000 Central Florida Blvd, Orlando, FL 32816",
            hq_city="Orlando, FL, USA", hq_lat=28.6024, hq_lng=-81.2001,
            governance_state="operator_verified", claimed_by=out["users"]["ucf_member"]["id"],
            blurb="UCF Knights — students and alumni of the University of Central Florida.",
        )
        ucf_rob = self.place(
            UCF_ROBOTICS_KEY, name="Knights Robotics Club", place_type="school",
            lat=28.6024, lng=-81.2001, zip="32816", parent_place_ref=ucf, blurb=UCF_ROBOTICS_BLURB,
            hq_city="Orlando, FL, USA",
        )
        out["places"] = {"sjsu": sjsu, "rcc": rcc, "ucf": ucf, "ucf_robotics": ucf_rob}

        for k in ("host", "member"):
            self.member(out["users"][k]["id"], sjsu, "school", "San Jose State University")
        self.member(host, rcc, "school", "Responsible Computing Club (RCC)")
        self.member(out["users"]["ucf_member"]["id"], ucf, "school", "University of Central Florida")
        self.member(out["users"]["ucf_member"]["id"], ucf_rob, "school", "Knights Robotics Club")

        thu, fri, sat = next_dow(3), next_dow(4), next_dow(5)
        out["dates"] = {"today": today.isoformat(), "thu": thu.isoformat(),
                        "fri": fri.isoformat(), "sat": sat.isoformat()}
        sj = dict(block="zip-95138", lat=37.3352, lng=-121.8811, community=sjsu)
        ev = out["events"]
        ev["language_exchange"] = self.event(
            host, "Language Exchange Club",
            "Practice Spanish, Mandarin, Hindi and more with SJSU students — swap 30 minutes in each language.",
            at(thu, 16), 2, "Student Union, SJSU", tags="book_club_learning", **sj)
        ev["career_mixer"] = self.event(
            host, "Student & Alumni Career Networking Mixer",
            "Spartan students meet alumni from tech, design and public service for informal career chats.",
            at(fri, 18), 2.5, "Diaz Compean Student Union Ballroom", tags="lifestyle_social", **sj)
        ev["garden_day"] = self.event(
            host, "Alumni Garden Volunteer Day",
            "SJSU alumni and students volunteer at the campus community garden — gloves and coffee provided.",
            at(sat, 9), 3, "SJSU Campus Community Garden", tags="lifestyle_social", **sj)
        uh = out["users"]["ucf_member"]["id"]
        uc = dict(block="zip-32816", lat=28.6024, lng=-81.2001, community=ucf)
        ev["ucf_build_night"] = self.event(
            uh, "Robot Build Night", "Hands-on build night for the Knights Robotics competition rover.",
            at(thu, 19, ET), 3, "UCF Engineering II, Room 102", tags="book_club_learning", **uc)
        ev["ucf_tailgate"] = self.event(
            uh, "Knights Alumni Tailgate", "UCF alumni tailgate before Saturday's game — grill and lawn games.",
            at(sat, 11, ET), 4, "Memory Mall, UCF", tags="lifestyle_social", **uc)
        ev["pausa"] = self.event(
            out["users"]["pausa_host"]["id"], "Pause Sip & Explore at Pausa",
            "Coffee tasting and a slow walk around downtown St. Cloud.",
            at(sat, 10, ET), 2, "Pausa Coffee, St. Cloud", block="zip-34771",
            lat=28.2489, lng=-81.2812, community=None, tags="lifestyle_social")
        return out


if __name__ == "__main__":  # python scripts/e2e/seed_oct7.py --env <file>
    import argparse
    import sys

    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from sjsu_oct7 import load_env  # noqa: E402

    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    a = ap.parse_args()
    s = Seeder(load_env(a.env), a.db_url).run()
    for u in s["users"].values():
        u.pop("jwt", None)
    print(json.dumps(s, indent=1))
