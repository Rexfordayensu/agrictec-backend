"""Reference data shared with the frontend (constants/*.ts and data/mock/diagnoses.ts)."""

from typing import Literal, get_args

REGIONS: list[str] = [
    "Ahafo",
    "Ashanti",
    "Bono",
    "Bono East",
    "Central",
    "Eastern",
    "Greater Accra",
    "North East",
    "Northern",
    "Oti",
    "Savannah",
    "Upper East",
    "Upper West",
    "Volta",
    "Western",
    "Western North",
]

YieldCrop = Literal["Maize", "Cassava", "Rice", "Cocoa", "Yam", "Plantain", "Groundnut", "Tomato"]
Crop = Literal[
    "Maize",
    "Cassava",
    "Rice",
    "Cocoa",
    "Yam",
    "Plantain",
    "Groundnut",
    "Tomato",
    "Pepper",
    "Okra",
    "Soybean",
    "Sorghum",
    "Millet",
    "Cashew",
    "Oil Palm",
    "Vegetables",
    "Other",
]
YIELD_CROPS: list[str] = list(get_args(YieldCrop))
CROPS: list[str] = list(get_args(Crop))

FARM_TYPES = ["Smallholder", "Commercial", "Cooperative", "Out-grower"]
SIZE_UNITS = ["acres", "hectares"]
ACRES_PER_HECTARE = 2.47105
MAX_FARM_SIZE = 10_000

GHANA_BOUNDS = {"min_lat": 4.5, "max_lat": 11.2, "min_lng": -3.3, "max_lng": 1.3}


def inside_ghana(lat: float, lng: float) -> bool:
    b = GHANA_BOUNDS
    return b["min_lat"] <= lat <= b["max_lat"] and b["min_lng"] <= lng <= b["max_lng"]


# Average yield in tonnes per acre under baseline conditions.
BASE_YIELD_PER_ACRE: dict[str, float] = {
    "Maize": 2.5,
    "Cassava": 15,
    "Rice": 3,
    "Cocoa": 0.8,
    "Yam": 12,
    "Plantain": 18,
    "Groundnut": 1.2,
    "Tomato": 15,
}

# Farm-gate price in GHS per tonne.
PRICE_PER_TONNE: dict[str, float] = {
    "Maize": 3200,
    "Cassava": 900,
    "Rice": 5500,
    "Cocoa": 24000,
    "Yam": 4500,
    "Plantain": 2000,
    "Groundnut": 6000,
    "Tomato": 3500,
}

CROP_EMOJI: dict[str, str] = {
    "Maize": "🌽",
    "Cassava": "🥔",
    "Rice": "🍚",
    "Cocoa": "🍫",
    "Yam": "🍠",
    "Plantain": "🍌",
    "Groundnut": "🥜",
    "Tomato": "🍅",
}

CROP_TIPS: dict[str, list[str]] = {
    "Maize": [
        "Plant at the start of the rains with 75cm × 40cm spacing for the best stand.",
        "Apply NPK 15-15-15 two weeks after planting and top-dress with urea at six weeks.",
        "Scout weekly for Fall Armyworm and act early when you see leaf damage.",
        "Dry cobs to 13% moisture before storage to prevent aflatoxin and weevils.",
    ],
    "Cassava": [
        "Use clean, disease-free stems from improved varieties such as Bankye Hemaa.",
        "Plant cuttings at a slant, about 1m × 1m apart, with two-thirds buried.",
        "Weed at 4, 8 and 12 weeks after planting, before the canopy closes.",
        "Harvest between 9 and 12 months for the best starch content and price.",
    ],
    "Rice": [
        "Level the field well so water spreads evenly and weeds are suppressed.",
        "Transplant 21-day-old seedlings at 20cm × 20cm for higher tillering.",
        "Keep 5cm of standing water during tillering and drain two weeks before harvest.",
        "Harvest when 80% of the grains are golden to reduce shattering losses.",
    ],
    "Cocoa": [
        "Prune regularly to keep the canopy open and reduce black pod disease.",
        "Remove and bury infected pods every week during the rainy season.",
        "Keep moderate shade trees to protect young cocoa and the soil.",
        "Ferment beans for 6 days and sun-dry them well to earn premium prices.",
    ],
    "Yam": [
        "Select healthy seed yams or use the minisett technique to cut costs.",
        "Make mounds or ridges about 1m apart to give tubers room to grow.",
        "Stake vines early so leaves get more sunlight and yields increase.",
        "Store harvested tubers in a cool, shaded, well-ventilated barn.",
    ],
    "Plantain": [
        "Use clean suckers and pare the corm to remove pests before planting.",
        "Space plants 3m × 2m and mulch heavily to hold soil moisture.",
        "Remove extra suckers, leaving one mother plant and one follower.",
        "Prop up heavy bunches with poles to prevent the plants from toppling.",
    ],
    "Groundnut": [
        "Plant on ridges or flat beds at 40cm × 15cm for good pod filling.",
        "Apply gypsum or lime at flowering to improve pod development.",
        "Keep the field weed-free for the first six weeks after planting.",
        "Dry pods quickly after harvest to keep aflatoxin levels low.",
    ],
    "Tomato": [
        "Raise seedlings in a nursery and transplant after 3 to 4 weeks.",
        "Stake or trellis plants to keep fruit off the soil and reduce rot.",
        "Water at the base in the morning to avoid leaf diseases like blight.",
        "Rotate with non-solanaceous crops to break the pest and disease cycle.",
    ],
}

YIELD_MULTIPLIERS: dict[str, dict[str, float]] = {
    "fertilizer": {"both": 1.35, "inorganic": 1.25, "organic": 1.15, "none": 1},
    "irrigation": {"drip": 1.4, "sprinkler": 1.25, "rainfed": 1},
    "season": {"major": 1.1, "minor": 1},
}

# The four original lending partners (the seed defaults of the lenders table).
LOAN_PARTNERS: list[dict] = [
    {
        "id": "sinapi",
        "name": "Sinapi Aba Trust",
        "rate": 18,
        "maxAmount": 100_000,
        "minMonths": 6,
        "maxMonths": 24,
        "minScore": 550,
        "color": "#10b981",
    },
    {
        "id": "opportunity",
        "name": "Opportunity Fund",
        "rate": 22,
        "maxAmount": 50_000,
        "minMonths": 3,
        "maxMonths": 12,
        "minScore": 500,
        "color": "#3b82f6",
    },
    {
        "id": "agridev",
        "name": "AgriDev Microfinance",
        "rate": 15,
        "maxAmount": 200_000,
        "minMonths": 12,
        "maxMonths": 36,
        "minScore": 620,
        "color": "#a855f7",
    },
    {
        "id": "rural",
        "name": "Rural Bank Consortium",
        "rate": 20,
        "maxAmount": 150_000,
        "minMonths": 6,
        "maxMonths": 24,
        "minScore": 580,
        "color": "#f5c451",
    },
]

LOAN_PURPOSES = [
    "Seeds & Fertilizer",
    "Farm Equipment",
    "Land Expansion",
    "Irrigation System",
    "Storage & Processing",
]
LOAN_PERIODS = [6, 12, 24, 36]

MOCK_DIAGNOSES: list[dict] = [
    {
        "crop": "Maize",
        "disease": "Fall Armyworm",
        "confidence": 94,
        "severity": "High",
        "treatment": "Spray an approved insecticide such as emamectin benzoate early in the morning, targeting the leaf funnel. Repeat after 7 days and scout the whole field twice a week.",
    },
    {
        "crop": "Maize",
        "disease": "Maize Lethal Necrosis",
        "confidence": 88,
        "severity": "High",
        "treatment": "Uproot and destroy infected plants, control aphids and thrips, and replant with certified MLN-tolerant seed. Rotate with legumes such as groundnut next season.",
    },
    {
        "crop": "Cassava",
        "disease": "Cassava Mosaic Disease",
        "confidence": 89,
        "severity": "Moderate",
        "treatment": "Uproot and burn infected plants, control whiteflies, and replant with resistant varieties using clean, certified stems.",
    },
    {
        "crop": "Cocoa",
        "disease": "Cocoa Black Pod",
        "confidence": 91,
        "severity": "High",
        "treatment": "Remove and bury infected pods, prune to improve airflow, and apply a copper-based fungicide every 3 to 4 weeks during the rainy season.",
    },
    {
        "disease": "Healthy",
        "confidence": 97,
        "severity": "None",
        "treatment": "No disease detected. Keep up regular weeding, balanced fertilizer and weekly scouting to keep your crop healthy.",
    },
]
