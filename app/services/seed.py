"""The demo world (lib/admin/seed.ts, admin spec Section 9).

Every choice comes from one fixed-seed generator (the same mulberry32 as the browser) and every
date is relative to the start of today, so seeding twice on the same day builds the same world.
Seeded farmers have IDs starting "seed-farmer-", so a demo reset never touches real accounts."""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.security import fingerprint, hash_password
from app.domain.clock import as_date, now_utc
from app.domain.constants import LOAN_PERIODS, LOAN_PURPOSES, REGIONS
from app.domain.constituencies import constituencies_of
from app.domain.credit import calculate_credit_score
from app.domain.defaults import DEFAULT_DIAGNOSES, DEFAULT_LENDERS
from app.domain.loans import monthly_repayment
from app.domain.risk_rules import RiskSubject, detect_risks, risk_key
from app.domain.verification import hash_ghana_card, mask_ghana_card, rejection_message
from app.models import (
    Admin,
    AdminNote,
    Announcement,
    Diagnosis,
    FarmRecord,
    Lender,
    LoanApplication,
    LoanStatusEvent,
    MomoTransaction,
    Profile,
    RiskFlag,
    User,
    VerificationRequest,
)
from app.schemas import farm as f
from app.services import platform_store

SEED = 20260101
DEMO_PASSWORDS = {"super_admin": "Admin@2026", "team": "Team@2026", "farmer": "Farmer@2026"}
SUPER_ADMIN_ID = "admin-super"
SUPER_ADMIN_EMAIL = "superadmin@agrictec.app"
SEED_FARMER_PREFIX = "seed-farmer-"

_M = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & _M


class Random:
    """mulberry32, bit for bit the browser's: the same seed gives the same sequence."""

    def __init__(self, seed: int) -> None:
        self._a = seed & _M

    def next(self) -> float:
        self._a = (self._a + 0x6D2B79F5) & _M
        t = self._a
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul(t ^ (t >> 7), t | 61)) & _M)) & _M
        return ((t ^ (t >> 14)) & _M) / 4294967296

    def int(self, low: int, high: int) -> int:
        return low + math.floor(self.next() * (high - low + 1))

    def pick(self, items):
        return items[math.floor(self.next() * len(items))]

    def chance(self, probability: float) -> bool:
        return self.next() < probability

    def shuffle(self, items):
        copy = list(items)
        for i in range(len(copy) - 1, 0, -1):
            j = self.int(0, i)
            copy[i], copy[j] = copy[j], copy[i]
        return copy


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _round_to(value: float, step: int) -> int:
    return _js_round(value / step) * step


# ---------- Ghana reference data ----------

FARMERS_PER_REGION = {
    "Ashanti": 9, "Eastern": 7, "Bono": 6, "Northern": 6, "Volta": 6, "Western": 3, "Central": 3,
    "Greater Accra": 3, "Bono East": 3, "Ahafo": 2, "Western North": 2, "Oti": 2, "Savannah": 2,
    "Upper East": 2, "Upper West": 2, "North East": 2,
}
REGION_ZONE = {
    "Western": "cocoa", "Western North": "cocoa", "Ashanti": "cocoa", "Eastern": "cocoa", "Ahafo": "cocoa",
    "Central": "cocoa", "Bono": "middle", "Bono East": "middle", "Northern": "north", "Savannah": "north",
    "North East": "north", "Upper East": "north", "Upper West": "north", "Volta": "volta", "Oti": "volta",
    "Greater Accra": "coast",
}
ZONE_CROPS = {
    "cocoa": ["Cocoa", "Plantain", "Cassava", "Maize"],
    "middle": ["Maize", "Yam", "Cassava", "Cocoa"],
    "north": ["Maize", "Groundnut", "Yam", "Rice"],
    "volta": ["Rice", "Cassava", "Maize", "Yam"],
    "coast": ["Tomato", "Cassava", "Maize"],
}
NAMES = {
    "akan": (
        ["Akosua", "Abena", "Ama", "Efua", "Adwoa", "Afua", "Yaa", "Akua", "Esi", "Gifty", "Kwame", "Kofi", "Yaw",
         "Kwaku", "Kwabena", "Kojo", "Kwasi", "Kwadwo", "Samuel", "Daniel"],
        ["Mensah", "Owusu", "Boateng", "Asante", "Osei", "Agyeman", "Appiah", "Darko", "Ofori", "Amoah", "Antwi",
         "Frimpong", "Danso", "Nyarko", "Badu", "Acheampong", "Sarpong"],
    ),
    "north": (
        ["Fatima", "Amina", "Mariama", "Ayishetu", "Hawa", "Zenabu", "Seidu", "Ibrahim", "Abdulai", "Mohammed",
         "Iddrisu", "Alhassan", "Yakubu", "Mahama"],
        ["Issah", "Abdulai", "Iddrisu", "Alhassan", "Mahama", "Yakubu", "Salifu", "Sulemana", "Bawa", "Fuseini",
         "Mumuni", "Dramani"],
    ),
    "ewe": (
        ["Selasi", "Dzifa", "Enyonam", "Mawusi", "Esinam", "Selorm", "Edem", "Elikem", "Kodzo", "Senyo"],
        ["Agbeko", "Kpodo", "Amegah", "Adzraku", "Gbedemah", "Dogbe", "Agbenyo", "Ahiadzro"],
    ),
    "ga": (
        ["Naa", "Dede", "Adoley", "Nii", "Nortey", "Comfort", "Grace", "Emmanuel"],
        ["Tetteh", "Quaye", "Lamptey", "Laryea", "Ankrah", "Addo"],
    ),
}
ZONE_NAMES = {"cocoa": "akan", "middle": "akan", "north": "north", "volta": "ewe", "coast": "ga"}
NAMED_FARMERS = [
    ("Akosua Mensah", "Ashanti"), ("Kwame Owusu", "Eastern"), ("Abena Boateng", "Bono"),
    ("Yaw Asante", "Ashanti"), ("Fatima Issah", "Northern"), ("Seidu Abdulai", "Northern"),
]
REGION_CENTRE = {
    "Ahafo": (7.0, -2.4), "Ashanti": (6.7, -1.6), "Bono": (7.5, -2.5), "Bono East": (7.75, -1.05),
    "Central": (5.5, -1.0), "Eastern": (6.3, -0.5), "Greater Accra": (5.65, -0.15), "North East": (10.5, -0.4),
    "Northern": (9.4, -0.85), "Oti": (8.0, 0.3), "Savannah": (9.1, -1.8), "Upper East": (10.8, -0.9),
    "Upper West": (10.3, -2.2), "Volta": (6.6, 0.47), "Western": (5.3, -2.1), "Western North": (6.3, -2.8),
}
REGION_MARKET = {
    "Ahafo": "Goaso market", "Ashanti": "Kumasi Central Market", "Bono": "Sunyani market",
    "Bono East": "Techiman market", "Central": "Cape Coast market", "Eastern": "Koforidua market",
    "Greater Accra": "Agbogbloshie market", "North East": "Nalerigu market", "Northern": "Tamale market",
    "Oti": "Dambai market", "Savannah": "Damongo market", "Upper East": "Bolgatanga market",
    "Upper West": "Wa market", "Volta": "Ho market", "Western": "Takoradi market",
    "Western North": "Sefwi Wiawso market",
}
PHONE_PREFIXES = ["24", "54", "55", "59", "20", "50", "27", "57", "26", "56"]
NETWORKS = ["MTN", "MTN", "MTN", "Telecel", "Telecel", "AirtelTigo"]
SUSPENDED_REASONS = [
    "A lender reported repeated fake sales records.",
    "Identity details do not match the Ghana Card submitted.",
    "Under investigation for a shared MoMo wallet with another account.",
]
SEED_REJECTIONS = [
    ("Unclear", "The photo of the card is unreadable."),
    ("Doesn't match name", "The name on the card does not match your profile."),
    ("Number format invalid", "Check the number on your card and try again."),
]
# Farmer indexes with deliberately suspicious data, for the risk rules to find.
PLANTED = {
    "duplicate_momo": 4, "burst": 9, "shared_phone_a": 14, "shared_phone_b": 15, "outlier": 22,
    "spike": 31, "backdated": 40, "shared_card_a": 47, "shared_card_b": 48,
}
PLANTED_INDEXES = set(PLANTED.values())
PLANTED_CARD_LAST4 = "4821"
PLANTED_MOMO_REF = "MOMO771204"
TEAM = [
    (SUPER_ADMIN_ID, "Super Admin", SUPER_ADMIN_EMAIL, "super_admin", 365),
    ("admin-efua", "Efua Asantewaa", "efua.asantewaa@agrictec.app", "admin", 300),
    ("admin-kofi", "Kofi Adjei", "kofi.adjei@agrictec.app", "support", 200),
    ("admin-ama", "Ama Serwaa", "ama.serwaa@agrictec.app", "support", 150),
    ("admin-yaw", "Yaw Darko", "yaw.darko@agrictec.app", "analyst", 90),
]


@dataclass
class SeedFarmer:
    index: int
    user: dict
    profile: f.Profile
    records: list[f.FarmRecord] = field(default_factory=list)
    momo: list[f.MomoTransaction] = field(default_factory=list)
    diagnoses: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)


@dataclass
class SeedWorld:
    farmers: list[SeedFarmer]
    verifications: list[dict]
    applications: list[dict]
    risk_flags: list[dict]
    announcements: list[dict]


def build_world(now: datetime | None = None) -> SeedWorld:
    """Builds the whole demo world without writing anything."""
    now = now or now_utc()
    rand = Random(SEED)
    today = datetime.combine(now.date(), datetime.min.time(), UTC)

    def at(days_ago: int, hour: int = 9, minute: int = 0) -> datetime:
        # A time later than `now` moves to the day before, so nothing is in the future.
        time = today - timedelta(days=days_ago) + timedelta(minutes=hour * 60 + minute)
        return time - timedelta(days=1) if time > now else time

    def day(days_ago: int):
        return (today - timedelta(days=days_ago)).date()

    def days_since(value: datetime) -> int:
        return (today.date() - as_date(value)).days

    def reviewer(role: str) -> str:
        return "Efua Asantewaa" if role == "admin" else rand.pick(["Kofi Adjei", "Ama Serwaa"])

    regions = rand.shuffle([r for r in REGIONS for _ in range(FARMERS_PER_REGION[r])])
    fixed_names: dict[int, str] = {}
    for name, region in NAMED_FARMERS:
        index = next(i for i, r in enumerate(regions) if r == region and i not in fixed_names)
        fixed_names[index] = name

    status_order = rand.shuffle([i for i in range(len(regions)) if i not in PLANTED_INDEXES])
    status = {i: "suspended" for i in status_order[:3]} | {i: "pending_verification" for i in status_order[3:9]}

    used_names = set(fixed_names.values())
    used_phones: set[str] = set()
    used_cards = {PLANTED_CARD_LAST4}
    momo_counter = 0
    farmers: list[SeedFarmer] = []
    pending_cards: dict[str, str] = {}
    verified_cards: dict[str, str] = {}

    for index, region in enumerate(regions):
        user_id = f"{SEED_FARMER_PREFIX}{index + 1:03d}"
        zone = REGION_ZONE[region]
        planted = index in PLANTED_INDEXES
        account_status = status.get(index, "active")

        name = fixed_names.get(index)
        while not name or (index not in fixed_names and name in used_names):
            first_names, last_names = NAMES[ZONE_NAMES[zone]]
            name = f"{rand.pick(first_names)} {rand.pick(last_names)}"
        used_names.add(name)
        first, last = name.split(" ")

        phone = ""
        while not phone or phone in used_phones:
            phone = f"+233{rand.pick(PHONE_PREFIXES)}{rand.int(0, 9_999_999):07d}"
        used_phones.add(phone)
        if index == PLANTED["shared_phone_b"]:
            phone = farmers[PLANTED["shared_phone_a"]].profile.phone

        roll = rand.next()
        farm_type = "Smallholder" if roll < 0.75 else "Cooperative" if roll < 0.9 else "Commercial"
        acres = (
            4 if planted
            else rand.int(1, 15) if farm_type == "Smallholder"
            else rand.int(20, 60) if farm_type == "Cooperative"
            else rand.int(30, 120)
        )
        zone_crops = ZONE_CROPS[zone]
        first_crop = zone_crops[0] if rand.chance(0.8) else rand.pick(zone_crops)
        extras = [rand.pick(zone_crops) for _ in range(rand.int(0, 2))]
        primary_crops = list(dict.fromkeys([first_crop, *extras]))
        lat, lng = REGION_CENTRE[region]
        gps = f.Gps(
            lat=_js_round((lat + (rand.next() - 0.5) * 0.5) * 10000) / 10000,
            lng=_js_round((lng + (rand.next() - 0.5) * 0.5) * 10000) / 10000,
        )

        shared_card = index in (PLANTED["shared_card_a"], PLANTED["shared_card_b"])
        card_verified = True if shared_card else account_status != "pending_verification" and rand.chance(0.35)
        card_last4: str | None = None
        if shared_card:
            card_last4 = PLANTED_CARD_LAST4
        elif card_verified or account_status == "pending_verification":
            while not card_last4 or card_last4 in used_cards:
                card_last4 = f"{rand.int(1000, 9999):04d}"
            used_cards.add(card_last4)

        signup_days_ago = rand.int(240, 360) if planted else rand.int(0, 364)
        constituencies = constituencies_of(region)
        cooperative = f"{region} {primary_crops[0]} Growers"
        verified_phone = rand.chance(0.85)
        verified_location = rand.chance(0.6)
        profile = f.Profile(
            user_id=user_id,
            name=name,
            phone=phone,
            email=f"{first}.{last}@agrictec.demo".lower(),
            region=region,
            farm_name=cooperative if farm_type == "Cooperative" else f"{last} {primary_crops[0]} Farm",
            constituency=constituencies[index % len(constituencies)] if constituencies else "",
            acres=acres,
            farm_type=farm_type,
            cooperative_name=cooperative if farm_type == "Cooperative" else None,
            primary_crops=primary_crops,
            gps=gps,
            ghana_card=f.GhanaCardInfo(status="verified", masked=mask_ghana_card(card_last4))
            if card_verified and card_last4
            else f.GhanaCardInfo(),
            verification=f.VerificationState(phone=verified_phone, location=verified_location, ghana_card=card_verified),
            onboarding_completed_at=at(signup_days_ago, 8, index % 60),
        )
        if card_verified and card_last4:
            verified_cards[user_id] = card_last4
        if account_status == "pending_verification" and card_last4:
            pending_cards[user_id] = card_last4

        # One income per month at most and narrow ranges, so ordinary farmers never look like
        # outliers or spikes. Amounts scale with farm size.
        scale = 1 + acres / 25
        market = REGION_MARKET[region]
        records: list[f.FarmRecord] = []
        income_months: set[str] = set()

        def add_record(days_ago: int, kind: str, crop: str, description: str, amount: float, created=None):
            records.append(
                f.FarmRecord(
                    id=f"{user_id}-rec-{len(records) + 1:02d}",
                    user_id=user_id,
                    type=kind,
                    description=description,
                    crop=crop,
                    amount=amount,
                    date=day(days_ago),
                    created_at=created or at(days_ago, 8 + len(records) % 10, len(records) % 60),
                )
            )

        for _ in range(18 if planted else rand.int(0, 40)):
            days_ago = rand.int(0, signup_days_ago)
            crop = rand.pick(primary_crops)
            roll = rand.next()
            month = day(days_ago).isoformat()[:7]
            if roll < 0.35 and month not in income_months:
                income_months.add(month)
                description = "Cocoa sales to LBC" if crop == "Cocoa" else f"{crop} sales at {market}"
                add_record(days_ago, "income", crop, description, _round_to(rand.int(2000, 6000) * scale, 10))
            elif roll < 0.75:
                description = rand.pick(
                    [
                        f"Certified {crop.lower()} seed",
                        "NPK fertilizer",
                        "Labour for weeding",
                        f"Transport to {market}",
                        "Pesticide spray",
                        "Land preparation (tractor hire)",
                    ]
                )
                add_record(days_ago, "expense", crop, description, _round_to(rand.int(300, 1500) * scale, 10))
            else:
                description = rand.pick(
                    [f"Planted {crop.lower()}", f"Weeded {crop.lower()} field", f"Harvested {crop.lower()}", "Applied fertilizer"]
                )
                add_record(days_ago, "activity", crop, description, 0)

        main_crop = primary_crops[0]
        if index == PLANTED["burst"]:
            for i in range(15):
                add_record(9, "activity" if i % 3 else "expense", main_crop, f"Field work batch {i + 1}", 0 if i % 3 else 400)
        if index == PLANTED["outlier"]:
            for i in range(6):
                add_record(20 + i * 25, "expense", main_crop, "NPK fertilizer", 800 + i * 50)
            add_record(12, "expense", main_crop, "Tractor purchase", 48000)
        if index == PLANTED["spike"]:
            for m in range(2, 8):
                add_record(m * 30 + 5, "income", main_crop, f"{main_crop} sales at {market}", 4200)
            for _ in range(3):
                add_record(5, "income", main_crop, f"Bulk {main_crop.lower()} sale", 14000)
        if index == PLANTED["backdated"]:
            # 18 records dated 100-270 days back, entered over two evenings (9 a day: no burst).
            for i in range(18):
                entered = at(3, 21, i) if i < 9 else at(4, 21, i)
                add_record(100 + i * 10, "expense", main_crop, "Late entry: farm inputs", 600, entered)
        records.sort(key=lambda r: (r.date, r.created_at), reverse=True)

        momo: list[f.MomoTransaction] = []
        for i in range(8 if planted else rand.int(0, 20)):
            days_ago = rand.int(0, signup_days_ago)
            incoming = rand.chance(0.55)
            momo_counter += 1
            counterparty = rand.pick(
                ["Produce buyer", f"{market} trader", "Cooperative payout", "LBC payment"]
                if incoming
                else ["Agro-input shop", "Farm labour", "Transport", "Tractor service"]
            )
            amount = _round_to(rand.int(300, 5000), 10) if incoming else _round_to(rand.int(100, 3000), 10)
            momo.append(
                f.MomoTransaction(
                    id=f"{user_id}-momo-{i + 1:02d}",
                    user_id=user_id,
                    direction="in" if incoming else "out",
                    counterparty=counterparty,
                    amount=amount,
                    reference=f"MOMO{100000 + momo_counter * 37}",
                    network=rand.pick(NETWORKS),
                    date=day(days_ago),
                    created_at=at(days_ago, 12, i % 60),
                )
            )
        if index == PLANTED["duplicate_momo"]:
            for suffix, amount, days_ago, hour in (("1", 2500, 16, 10), ("2", 3100, 11, 15)):
                momo.append(
                    f.MomoTransaction(
                        id=f"{user_id}-momo-dup-{suffix}", user_id=user_id, direction="in",
                        counterparty="Produce buyer", amount=amount, reference=PLANTED_MOMO_REF, network="MTN",
                        date=day(days_ago), created_at=at(days_ago, hour),
                    )
                )
        momo.sort(key=lambda t: (t.date, t.created_at), reverse=True)

        diagnoses: list[dict] = []
        if rand.chance(0.3):
            options = [d for d in DEFAULT_DIAGNOSES if not d.get("crop") or d["crop"] in primary_crops]
            for i in range(rand.int(1, 3)):
                picked = rand.pick(options)
                days_ago = rand.int(0, min(120, signup_days_ago))
                diagnoses.append(
                    {
                        "id": f"{user_id}-diag-{i + 1:02d}",
                        "disease": picked["disease"],
                        "severity": picked["severity"],
                        "confidence": picked["confidence"],
                        "treatment": picked["treatment"],
                        "crop": picked.get("crop") or main_crop,
                        "date": at(days_ago, 14),
                    }
                )

        tags = (["cooperative-lead"] if farm_type == "Cooperative" and rand.chance(0.5) else []) + (
            ["pilot-2026"] if rand.chance(0.25) else []
        )
        notes = (
            [
                {
                    "id": f"{user_id}-note-1",
                    "text": "Suspended pending review. Contact the farmer before reactivating."
                    if account_status == "suspended"
                    else "Called the farmer to confirm their farm size. Details are correct.",
                    "by": "Efua Asantewaa",
                    "at": at(rand.int(0, max(0, signup_days_ago - 1)), 11),
                }
            ]
            if account_status == "suspended" or rand.chance(0.1)
            else []
        )
        # Two of the three suspended farmers have appealed (the overview's "needs attention").
        suspended_so_far = sum(1 for x in farmers if x.user["status"] == "suspended")
        if account_status == "suspended" and suspended_so_far < 2:
            tags.append("appeal")
            notes.insert(
                0,
                {
                    "id": f"{user_id}-note-2",
                    "text": "The farmer appealed the suspension by phone and asked for a review.",
                    "by": "Kofi Adjei",
                    "at": at(max(0, min(3, signup_days_ago)), 15),
                },
            )

        user = {
            "id": user_id,
            "email": profile.email,
            "is_guest": False,
            "created_at": at(signup_days_ago, 7, index % 60),
            "status": account_status,
            "suspended_reason": SUSPENDED_REASONS[suspended_so_far] if account_status == "suspended" else None,
        }
        user["last_active_at"] = at(
            min(signup_days_ago, rand.int(40, 120) if account_status == "suspended" else rand.int(0, 60)), 18
        )
        farmers.append(SeedFarmer(index, user, profile, records, momo, diagnoses, tags, notes))

    # ---------- Verifications: 12 pending (8 Ghana Cards, 2 phone, 2 location) and the history ----------
    verifications: list[dict] = []

    def card_number(seed_index: int, last4: str) -> str:
        return f"GHA-{100000 + (seed_index * 7919) % 900000}{last4[:3]}-{last4[3]}"

    def add_verification(farmer: SeedFarmer, state: str, days_ago: int, kind: str, value: str, rejection=None):
        card = kind == "ghanaCard"
        entry = {
            "id": f"seed-verif-{len(verifications) + 1:02d}",
            "user_id": farmer.user["id"],
            "kind": kind,
            "submitted_value": mask_ghana_card("".join(c for c in value if c.isdigit())[-4:]) if card else value,
            "value_hash": hash_ghana_card(value) if card else None,
            "status": state,
            "submitted_at": at(days_ago, 10),
            "reason": rejection[0] if rejection else None,
            "note": rejection[1] if rejection else None,
        }
        if state != "pending":
            entry["reviewed_by"] = reviewer("support")
            entry["reviewed_at"] = at(max(0, days_ago - 1), 15)
        verifications.append(entry)

    yaw = next((x for x in farmers if x.profile.name == "Yaw Asante"), None)
    yaw_last4 = (pending_cards.get(yaw.user["id"]) or verified_cards.get(yaw.user["id"])) if yaw else None
    first_pending = next((x for x in farmers if x.user["id"] in pending_cards), None)
    if yaw and yaw_last4:
        duplicate_of, duplicate_owner = card_number(yaw.index, yaw_last4), yaw
    elif first_pending:
        duplicate_of = card_number(first_pending.index, pending_cards[first_pending.user["id"]])
        duplicate_owner = first_pending
    else:
        duplicate_of, duplicate_owner = None, None

    for farmer in farmers:
        last4 = pending_cards.get(farmer.user["id"])
        if last4:
            add_verification(
                farmer, "pending", rand.int(0, min(10, days_since(farmer.user["created_at"]))), "ghanaCard",
                card_number(farmer.index, last4),
            )
    unverified_active = rand.shuffle(
        [x for x in farmers if x.user["status"] == "active" and not x.profile.verification.ghana_card]
    )
    queued: set[str] = set()
    cards_queued = 0
    for farmer in unverified_active[:6]:
        last4 = ""
        while not last4 or last4 in used_cards:
            last4 = f"{rand.int(1000, 9999):04d}"
        used_cards.add(last4)
        days_ago = rand.int(0, min(14, days_since(farmer.user["created_at"])))
        if cards_queued == 2 or farmer is duplicate_owner:
            continue
        number = duplicate_of if cards_queued == 0 and duplicate_of else card_number(farmer.index, last4)
        add_verification(farmer, "pending", days_ago, "ghanaCard", number)
        queued.add(farmer.user["id"])
        cards_queued += 1

    candidates = [
        x for x in farmers
        if x.user["status"] == "active" and x.index not in PLANTED_INDEXES and x.user["id"] not in queued
    ]
    phone_checks = [x for x in candidates if not x.profile.verification.phone][:2]
    location_checks = [
        x for x in candidates if not x.profile.verification.location and x.profile.gps and x not in phone_checks
    ][:2]
    for i, farmer in enumerate(phone_checks):
        add_verification(farmer, "pending", 1 + i * 3, "phone", farmer.profile.phone)
    for i, farmer in enumerate(location_checks):
        gps = farmer.profile.gps
        add_verification(farmer, "pending", 2 + i * 2, "location", f"{gps.lat:.4f}, {gps.lng:.4f}")

    for farmer in unverified_active[6:9]:
        last4 = ""
        while not last4 or last4 in used_cards:
            last4 = f"{rand.int(1000, 9999):04d}"
        used_cards.add(last4)
        days_ago = rand.int(2, max(2, days_since(farmer.user["created_at"])))
        number = card_number(farmer.index, last4)
        add_verification(farmer, "rejected", days_ago, "ghanaCard", number, rand.pick(SEED_REJECTIONS))
    for farmer in [x for x in farmers if x.profile.verification.ghana_card]:
        # The two planted shared-card accounts really do hold the same card.
        number_index = PLANTED["shared_card_a"] if farmer.index == PLANTED["shared_card_b"] else farmer.index
        days_ago = rand.int(1, max(1, days_since(farmer.user["created_at"])))
        add_verification(
            farmer, "approved", days_ago, "ghanaCard", card_number(number_index, verified_cards.get(farmer.user["id"], "0000"))
        )

    # Each profile's Ghana Card follows its latest request, as the verifications service does.
    for farmer in farmers:
        mine = [v for v in verifications if v["user_id"] == farmer.user["id"] and v["kind"] == "ghanaCard"]
        if not mine:
            continue
        latest = max(mine, key=lambda v: v["submitted_at"])
        card_status = (
            "verified" if farmer.profile.verification.ghana_card or latest["status"] == "approved"
            else "pending" if latest["status"] == "pending"
            else "rejected"
        )
        farmer.profile.ghana_card = f.GhanaCardInfo(
            status=card_status,
            masked=latest["submitted_value"],
            hash=latest["value_hash"],
            submitted_at=latest["submitted_at"],
            rejected_reason=rejection_message(latest["reason"], latest["note"]) if card_status == "rejected" else None,
        )

    # ---------- Loan applications: 25 across every status, each with a valid history ----------
    plans = (
        [(["submitted"], (0, 6))] * 5
        + [(["submitted", "under_review"], (5, 20))] * 4
        + [(["submitted", "under_review", "approved"], (15, 40))] * 3
        + [(["submitted", "rejected"], (5, 60))] * 2
        + [(["submitted", "under_review", "rejected"], (10, 90))] * 2
        + [(["submitted", "under_review", "approved", "disbursed"], (40, 120))] * 4
        + [(["submitted", "under_review", "approved", "disbursed", "repaid"], (180, 300))] * 3
        + [(["submitted", "under_review", "approved", "disbursed", "defaulted"], (150, 300))] * 2
    )
    notes = {
        "under_review": "Documents received. Checking records with the lender.",
        "approved": "Consistent records and income. Approved by the lender.",
        "rejected": "Income records are too thin for the amount requested.",
        "disbursed": "Funds sent to the farmer’s MoMo wallet.",
        "repaid": "Fully repaid on schedule.",
        "defaulted": "No repayment for three months after several reminders.",
    }
    applicants = rand.shuffle([x for x in farmers if x.user["status"] == "active" and len(x.records) >= 5])
    used_references: set[str] = set()
    applications: list[dict] = []
    for i, (path, (low, high)) in enumerate(plans[: len(applicants)]):
        farmer = applicants[i]
        lender = rand.pick(DEFAULT_LENDERS)
        months = rand.pick([m for m in LOAN_PERIODS if lender["minMonths"] <= m <= lender["maxMonths"]])
        amount = _round_to(rand.int(2000, min(lender["maxAmount"], 30000)), 500)
        age = min(rand.int(low, high), max(0, days_since(farmer.user["created_at"]) - 1))
        step = max(1, age // len(path))
        created = at(age, 9, i)
        reference = ""
        while not reference or reference in used_references:
            reference = f"APP-{created.year}-{rand.int(10000, 99999)}"
        used_references.add(reference)
        purpose = rand.pick(LOAN_PURPOSES)
        history = []
        for s, state in enumerate(path):
            history.append(
                {
                    "status": state,
                    "by": farmer.profile.name if s == 0 else reviewer("support" if state == "under_review" else "admin"),
                    "at": created if s == 0 else at(max(0, age - s * step), 14, s),
                    "note": notes.get(state),
                }
            )
        applications.append(
            {
                "id": f"seed-app-{i + 1:02d}",
                "reference": reference,
                "user_id": farmer.user["id"],
                "partner_id": lender["id"],
                "amount": amount,
                "months": months,
                "purpose": purpose,
                "rate": lender["rate"],
                "monthly": monthly_repayment(amount, lender["rate"], months).monthly,
                "score_at_application": calculate_credit_score(farmer.records, farmer.momo, farmer.profile).score,
                "status": path[-1],
                "created_at": created,
                "history": history,
            }
        )

    # ---------- Risk flags: the real rules over the seeded data (finding the planted cases) ----------
    detected = detect_risks([RiskSubject(x.user["id"], x.profile, x.records, x.momo) for x in farmers])
    risk_flags = [
        {
            "id": f"seed-risk-{i + 1:02d}",
            "user_id": r.user_id,
            "rule": r.rule,
            "severity": r.severity,
            "description": r.description,
            "related_ids": r.related_ids,
            "finding_key": risk_key(r.rule, r.user_id, r.related_ids),
            "status": "open",
            "detected_at": at(0, 6, i),
        }
        for i, r in enumerate(detected)
    ]

    announcements = [
        {
            "id": "seed-ann-1",
            "title": "Ghana Card checks are now faster",
            "message": "Most Ghana Card verifications are now reviewed within one working day. Verify yours to raise your credit score.",
            "type": "success", "audience": {"kind": "all"}, "starts_at": at(3, 8), "ends_at": at(-30, 8),
            "active": True, "dismissible": True, "created_by": "Super Admin", "created_at": at(3, 8),
        },
        {
            "id": "seed-ann-2",
            "title": "Dry spell expected in the Northern Region",
            "message": "Little rain is expected over the next two weeks. Delay planting and mulch to keep soil moisture.",
            "type": "warning", "audience": {"kind": "region", "regions": ["Northern"]}, "starts_at": at(1, 7),
            "ends_at": at(-14, 7), "active": True, "dismissible": True, "created_by": "Efua Asantewaa",
            "created_at": at(1, 7),
        },
        {
            "id": "seed-ann-3",
            "title": "Cocoa price review",
            "message": "COCOBOD has announced the new season’s farmgate price. Check the Market page for details.",
            "type": "info", "audience": {"kind": "crop", "crops": ["Cocoa"]}, "starts_at": at(40, 8),
            "ends_at": at(10, 8), "active": False, "dismissible": True, "created_by": "Efua Asantewaa",
            "created_at": at(40, 8),
        },
    ]
    return SeedWorld(farmers, verifications, applications, risk_flags, announcements)


# ---------- Writing ----------


def ensure_team(db: DB, now: datetime | None = None) -> None:
    """The demo team members that don't exist yet (an existing team is never changed)."""
    now = now or now_utc()
    team_hash = None
    for member_id, name, email, role, days_ago in TEAM:
        if member_id == SUPER_ADMIN_ID or db.get(Admin, member_id):
            continue
        if db.scalar(select(Admin.id).where(Admin.email == email)):
            continue
        team_hash = team_hash or hash_password(DEMO_PASSWORDS["team"])
        db.add(
            Admin(
                id=member_id, name=name, email=email, role=role, status="active", must_change_password=False,
                created_at=now - timedelta(days=days_ago), created_by=SUPER_ADMIN_ID, password_hash=team_hash,
            )
        )


def ensure_super_admin(db: DB) -> None:
    """The super admin always exists. ADMIN_EMAIL/ADMIN_PASSWORD replace the demo sign-in, once
    per value (a password changed later in the console is kept until the values change)."""
    settings = get_settings()
    owner_email = settings.configured_admin_email
    marker = fingerprint(owner_email, settings.admin_password) if owner_email and settings.admin_password else None
    current = db.get(Admin, SUPER_ADMIN_ID)
    if current is None:
        db.add(
            Admin(
                id=SUPER_ADMIN_ID,
                name="Super Admin",
                email=owner_email or SUPER_ADMIN_EMAIL,
                role="super_admin",
                status="active",
                # The demo password must be changed at first sign-in; the owner's was chosen.
                must_change_password=marker is None,
                password_hash=hash_password(settings.admin_password if marker else DEMO_PASSWORDS["super_admin"]),
                created_at=now_utc(),
                configured_from=marker,
            )
        )
        return
    if marker and current.configured_from != marker:
        taken = db.scalar(select(Admin.id).where(Admin.email == owner_email, Admin.id != SUPER_ADMIN_ID))
        if not taken:
            current.email = owner_email
            current.password_hash = hash_password(settings.admin_password)
            current.must_change_password = False
            current.configured_from = marker
            append_audit(
                db, current, action="admin.credentials_configured", target_type="admin", target_id=current.id,
                summary=f"{current.name}’s sign-in was set from the server configuration",
            )


def is_seeded(db: DB) -> bool:
    return platform_store.get_raw(db, platform_store.SEEDED) is not None


def clear_seed_data(db: DB) -> None:
    """Removes the seeded farmers (and everything of theirs) and the seeded announcements. Real
    accounts, the team and the audit trail stay."""
    seeded = select(User.id).where(User.id.startswith(SEED_FARMER_PREFIX))
    db.execute(delete(LoanApplication).where(LoanApplication.user_id.in_(seeded)))
    db.execute(delete(User).where(User.id.startswith(SEED_FARMER_PREFIX)))
    db.execute(delete(Announcement).where(Announcement.id.startswith("seed-ann-")))
    db.flush()


def seed_demo(db: DB, *, force: bool = False) -> bool:
    """Seeds the demo world once (or again with `force`, after clear_seed_data)."""
    if not force and is_seeded(db):
        return False
    world = build_world()
    farmer_hash = hash_password(DEMO_PASSWORDS["farmer"])
    ensure_super_admin(db)
    ensure_team(db)
    for lender in DEFAULT_LENDERS:
        if db.get(Lender, lender["id"]) is None:
            db.add(
                Lender(
                    id=lender["id"], name=lender["name"], rate=lender["rate"], max_amount=lender["maxAmount"],
                    min_months=lender["minMonths"], max_months=lender["maxMonths"], min_score=lender["minScore"],
                    color=lender["color"], active=True, created_at=now_utc(),
                )
            )

    for x in world.farmers:
        db.add(User(**x.user, password_hash=farmer_hash, tags=x.tags))
    db.flush()
    for x in world.farmers:
        p = x.profile
        db.add(
            Profile(
                user_id=p.user_id, name=p.name, email=p.email, phone=p.phone, farm_name=p.farm_name,
                region=p.region, constituency=p.constituency, gps_lat=p.gps.lat, gps_lng=p.gps.lng,
                acres=p.acres, size_unit="acres", primary_crops=p.primary_crops, farm_type=p.farm_type,
                cooperative_name=p.cooperative_name, ghana_card_status=p.ghana_card.status,
                ghana_card_masked=p.ghana_card.masked, ghana_card_hash=p.ghana_card.hash,
                ghana_card_submitted_at=p.ghana_card.submitted_at,
                ghana_card_rejected_reason=p.ghana_card.rejected_reason,
                verified_phone=p.verification.phone, verified_location=p.verification.location,
                onboarding_completed_at=p.onboarding_completed_at,
            )
        )
        db.add_all(FarmRecord(**r.model_dump()) for r in x.records)
        db.add_all(MomoTransaction(**t.model_dump()) for t in x.momo)
        db.add_all(Diagnosis(user_id=p.user_id, created_at=d["date"], **d) for d in x.diagnoses)
        db.add_all(AdminNote(user_id=p.user_id, **n) for n in x.notes)
    db.add_all(VerificationRequest(**v) for v in world.verifications)
    for app in world.applications:
        history = app.pop("history")
        row = LoanApplication(**app)
        row.history = [LoanStatusEvent(position=i, **h) for i, h in enumerate(history)]
        db.add(row)
    db.add_all(RiskFlag(**r) for r in world.risk_flags)
    db.add_all(Announcement(**a) for a in world.announcements)
    platform_store.put_raw(db, platform_store.SEEDED, now_utc().isoformat(), None)
    db.flush()
    return True
