"""What a draft of the credit rules would change for every farmer (lib/admin/credit-rules.ts).
Nothing is saved: the console previews a draft before publishing it."""

from dataclasses import dataclass

from app.domain.credit import BANDS, calculate_credit_score, score_label
from app.schemas.farm import FarmRecord, MomoTransaction, Profile
from app.schemas.platform import CreditConfigInput


@dataclass
class ScoringInput:
    user_id: str
    name: str
    region: str
    records: list[FarmRecord]
    momo: list[MomoTransaction]
    profile: Profile


def meets_eligibility(records: int, score: int, config: CreditConfigInput) -> bool:
    """Loan eligibility before an amount is chosen: enough records and score."""
    return records >= config.eligibility.min_records and score >= config.eligibility.min_score


def impact_of(inputs: list[ScoringInput], live: CreditConfigInput, draft: CreditConfigInput) -> dict:
    farmers = []
    for i in inputs:
        now = calculate_credit_score(i.records, i.momo, i.profile, live).score
        then = calculate_credit_score(i.records, i.momo, i.profile, draft).score
        farmers.append(
            {
                "userId": i.user_id,
                "name": i.name,
                "region": i.region,
                "current": now,
                "draft": then,
                "delta": then - now,
                "currentBand": score_label(now, live.bands),
                "draftBand": score_label(then, draft.bands),
                "eligibleNow": meets_eligibility(len(i.records), now, live),
                "eligibleDraft": meets_eligibility(len(i.records), then, draft),
            }
        )

    def average(key: str) -> int:
        return int(sum(f[key] for f in farmers) / len(farmers) + 0.5) if farmers else 0

    def bands(key: str) -> dict[str, int]:
        return {b: sum(1 for f in farmers if f[key] == b) for b in BANDS}

    moved = [f for f in farmers if f["delta"] != 0]
    return {
        "farmers": farmers,
        "averageCurrent": average("current"),
        "averageDraft": average("draft"),
        "bandsCurrent": bands("currentBand"),
        "bandsDraft": bands("draftBand"),
        "gained": [f for f in farmers if not f["eligibleNow"] and f["eligibleDraft"]],
        "lost": [f for f in farmers if f["eligibleNow"] and not f["eligibleDraft"]],
        "biggest": sorted(moved, key=lambda f: (-abs(f["delta"]), f["name"]))[:10],
        "changedScores": len(moved),
    }
