"""Built-in platform settings and content for a new installation (lib/admin/defaults.ts).

Documents are kept in the API's camelCase shape, exactly as they are stored and served."""

import copy

from app.domain.constants import (
    BASE_YIELD_PER_ACRE,
    CROP_EMOJI,
    CROP_TIPS,
    LOAN_PARTNERS,
    MOCK_DIAGNOSES,
    PRICE_PER_TONNE,
)

BUILT_IN = "2026-01-01T00:00:00.000Z"

DEFAULT_CREDIT_CONFIG: dict = {
    "version": 1,
    "weights": {
        "paymentHistory": 30,
        "recordCompleteness": 25,
        "incomeStability": 25,
        "momoActivity": 10,
        "verification": 10,
    },
    "targets": {"recordsForFull": 30, "momoForFull": 20, "monthsWindow": 6},
    "bands": {"fair": 500, "good": 650, "excellent": 750},
    "eligibility": {"minRecords": 5, "minScore": 550, "maxProfitMultiplier": 3},
    "updatedAt": BUILT_IN,
    "updatedBy": "System",
}

DEFAULT_FLAGS: dict = {
    "signupOpen": True,
    "guestMode": True,
    "aiAssistant": True,
    "cropDoctor": True,
    "yieldPredictor": True,
    "loans": True,
    "market": True,
    "pdfReports": True,
    "maintenanceMode": False,
    "maintenanceMessage": (
        "AgriCtec is down for scheduled maintenance. Your records are safe. Please check back soon."
    ),
}

_NO_AI = {"aiAssistant": False, "cropDoctor": False, "yieldPredictor": False}
ALL_TOOLS = {"pdfReports": True, "aiAssistant": True, "cropDoctor": True, "yieldPredictor": True}

PLAN_CATALOG: list[dict] = [
    {
        "id": "free",
        "name": "Free",
        "priceGHS": 0,
        "active": True,
        "access": {"pdfReports": False, **_NO_AI},
        "aiCredits": 0,
        "features": [
            "Farm records and MoMo tracking",
            "Credit score and loan comparisons",
            "Market and weather information",
        ],
    },
    {
        "id": "basic",
        "name": "Basic",
        "priceGHS": 25,
        "active": True,
        "access": {"pdfReports": True, **_NO_AI},
        "aiCredits": 0,
        "features": ["Everything in Free", "Download financial reports as PDFs"],
    },
    {
        "id": "premium",
        "name": "Premium",
        "priceGHS": 50,
        "active": True,
        "access": dict(ALL_TOOLS),
        "aiCredits": 100,
        "features": [
            "Everything in Basic",
            "AI Assistant and Crop Doctor",
            "Harvest and income estimates",
            "100 shared AI credits each month",
        ],
    },
    {
        "id": "advanced",
        "name": "Advanced",
        "priceGHS": 100,
        "active": True,
        "access": dict(ALL_TOOLS),
        "aiCredits": None,
        "features": ["All available farmer tools", "Unlimited AI credits", "Unlimited PDF reports"],
    },
]

DEFAULT_LENDERS: list[dict] = [
    {**partner, "active": True, "createdAt": BUILT_IN} for partner in LOAN_PARTNERS
]

# No real prices have been sourced yet: administrators add evidence-backed observations.
DEFAULT_MARKET_PRICES: list[dict] = []

DEFAULT_CROP_CONTENT: list[dict] = [
    {
        "crop": crop,
        "emoji": emoji,
        "yieldPerAcre": BASE_YIELD_PER_ACRE[crop],
        "pricePerTonne": PRICE_PER_TONNE[crop],
        "tips": list(CROP_TIPS[crop]),
    }
    for crop, emoji in CROP_EMOJI.items()
]

DEFAULT_DIAGNOSES: list[dict] = [
    {**diagnosis, "id": f"diagnosis-{index + 1:02d}", "active": True}
    for index, diagnosis in enumerate(MOCK_DIAGNOSES)
]

ASSISTANT_FALLBACK_ID = "kb-fallback"

# The assistant's topics as editable knowledge items, in priority order. Matching and the
# {placeholders} are documented in app/domain/assistant.py.
DEFAULT_ASSISTANT_KB: list[dict] = [
    {
        "id": "kb-maize-planting",
        "keywords": ["maize plant*", "maize when", "maize season*"],
        "priority": 100,
        "active": True,
        "reply": {
            "title": "Maize Planting in Ghana",
            "intro": "Here are the usual planting windows, {name}. Wait for reliable rains before sowing.",
            "bullets": [
                "Major season: March–April in the forest and transitional zones.",
                "Minor season: August where a second rainy season supports maize.",
                "Northern zones: May–June, following the onset of reliable rains.",
                "Use healthy, locally adapted seed and prepare a well-drained seedbed.",
            ],
            "tip": "For {region}, {plantingWindow}.",
        },
    },
    {
        "id": "kb-armyworm",
        "keywords": ["armyworm", "armyworms", "pest", "pests", "insect", "insects"],
        "priority": 90,
        "active": True,
        "reply": {
            "title": "Fall Armyworm Control",
            "intro": "Scout maize regularly and act while larvae are small. Confirm the pest before treating.",
            "bullets": [
                "Early detection: check young leaves and whorls for feeding holes, egg masses and caterpillars.",
                "Biological control: protect natural enemies and ask an extension officer about approved biological treatments.",
                "Chemical control: use only a locally approved product when needed, follow its label, wear protective equipment and observe harvest intervals.",
                "Cultural control: remove egg masses where practical, manage volunteer maize and rotate crops.",
            ],
            "tip": "If spraying is recommended, spray early morning or late evening, following the label and avoiding rain, wind and pollinator activity.",
        },
    },
    {
        "id": "kb-credit",
        "keywords": ["credit", "score", "scores", "loan", "loans"],
        "priority": 80,
        "active": True,
        "reply": {
            "title": "Improve Your Credit Score",
            "intro": "Your score is {score} ({scoreLabel}). {creditFocus}",
            "bullets": ["{creditSteps}"],
            "tip": "This is your AgriCtec farm score. Lenders make their own decisions; improving it does not guarantee a loan.",
        },
    },
    {
        "id": "kb-cocoa-prices",
        "keywords": ["cocoa price", "cocoa prices"],
        "priority": 70,
        "active": True,
        "reply": {
            "title": "Cocoa Prices",
            "intro": "Only published, source-backed market observations are shown. Prices vary by location, unit and date.",
            "bullets": [
                "{cocoaPrice}",
                "Compare the unit, weight and any deductions before agreeing to a sale.",
                "Keep your weighing slip and sale receipt, and record the income in your farm records.",
            ],
            "tip": "Check COCOBOD for official farmgate prices",
        },
    },
    {
        "id": "kb-weather",
        "keywords": ["weather", "rain*"],
        "priority": 60,
        "active": True,
        "reply": {
            "title": "Weather Intelligence",
            "intro": "Weather information: {weather}. Use the Weather page for location-specific availability.",
            "bullets": [
                "{rainAdvice}",
                "{fieldWorkAdvice}",
                "Check a local forecast for {region} before planting{cropWork}.",
            ],
            "tip": "Use rainfall onset and soil moisture together when deciding when to plant.",
        },
    },
    {
        "id": "kb-fertilizer",
        "keywords": ["fertiliz*", "fertilis*", "npk"],
        "priority": 50,
        "active": True,
        "reply": {
            "title": "Fertilizer Guide",
            "intro": "Match nutrients to the crop, soil and growth stage. Your extension officer can confirm the formulation and rate.",
            "bullets": [
                "Maize: discuss a balanced NPK basal application and split nitrogen top-dressing; apply to moist soil and keep fertilizer away from seed and stems.",
                "Cassava: use compost or well-rotted manure and a soil-test-based NPK plan, paying attention to potassium for root development.",
                "Cocoa: use cocoa-specific fertilizer where recommended, based on tree age and soil testing; retain useful organic matter and avoid fertilizing dry soil.",
            ],
            "tip": "Test your soil before buying fertilizer. A soil test helps avoid wasting money on nutrients your field already has.",
        },
    },
    {
        "id": "kb-finances",
        "keywords": [
            "profit",
            "profits",
            "income",
            "expense",
            "expenses",
            "money",
            "finance",
            "finances",
        ],
        "priority": 40,
        "active": True,
        "reply": {
            "title": "Your Farm Finances",
            "intro": "{name}, here is your all-time summary from {records} farm records. MoMo transfers are excluded to avoid double-counting.",
            "bullets": [
                "Total income: {income}.",
                "Total expenses: {expenses}.",
                "Net profit: {profit}.",
                "Profit margin: {margin}.",
            ],
            "tip": "{financeTip}",
        },
    },
    {
        "id": "kb-cassava",
        "keywords": ["cassava"],
        "priority": 30,
        "active": True,
        "reply": {
            "title": "Cassava Tips",
            "intro": "These four practices can help with your cassava crop in {region}.",
            "bullets": ["{tips:Cassava}"],
            "tip": "Ask your local MoFA extension officer which improved varieties suit your soil and market.",
        },
    },
    {
        "id": "kb-greeting",
        "keywords": ["hello", "hi", "akwaaba", "good morning"],
        "priority": 20,
        "active": True,
        "reply": {
            "title": "Akwaaba, {name}!",
            "intro": "I’m your AgriCtec AI assistant. Ask me a farming or farm-finance question in plain English.",
            "bullets": [
                "Maize planting, cassava care, pest control and fertilizer guidance.",
                "Weather information and indicative cocoa market prices.",
                "Your farm income, expenses, profit and personalised credit-score steps.",
            ],
            "tip": "Try a quick prompt below to get started. Replies are rule-based for now.",
        },
    },
    {
        "id": "kb-thanks",
        "keywords": ["thank*"],
        "priority": 10,
        "active": True,
        "reply": {
            "title": "You’re welcome!",
            "intro": "Happy farming, {name}! I’m here when you need a hand.",
        },
    },
    {
        "id": ASSISTANT_FALLBACK_ID,
        "keywords": [],
        "priority": 0,
        "active": True,
        "reply": {
            "title": "Let me help",
            "intro": "For advice specific to this issue, contact your local MoFA extension officer.",
            "bullets": [
                "Maize planting seasons and cassava tips.",
                "Fall armyworm, pests and fertilizer guidance.",
                "Weather information and indicative cocoa prices.",
                "Your credit score, farm income, expenses and profit.",
            ],
            "tip": "Tell me the crop and the issue, or choose a quick prompt below.",
        },
    },
]


def fresh(value):
    """A deep copy, so callers can never change the shared defaults."""
    return copy.deepcopy(value)
