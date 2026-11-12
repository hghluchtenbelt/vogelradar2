"""Method groups ("guilds") for the activity forecast and the assignment of
species to them.

Every rare or very rare species on waarneming.nl gets a guild by an explicit
override or by keywords in its Dutch name; the guild supplies the model
parameters (stay probability, movement scale, decay, habitat weights, coast
preference, wind use). A few well-studied species carry their own overrides
in model.SPECIES_OVERRIDES. Labels and descriptions are Dutch because they
are shown in the UI.
"""
from __future__ import annotations

import re
import unicodedata

# Wind use per guild: "drift" shifts the "moved" part of the kernel downwind
# (night wind, for nocturnal migrants and mobile raptors); "onshore" boosts
# coast cells that face into the wind (seabirds pushed towards the shore).
GUILDS = {
    "invasiegast": {
        "label": "Invasiegasten",
        "method": "Komen in sommige najaren massaal uit het noorden of oosten (notenkraker, pestvogel, kruisbek). "
                  "Blijven hangen waar voedsel is en schuiven dan tientallen km op. Wind speelt op dagniveau geen rol.",
        "p_stay": 0.8, "sigma_stay_km": 4.0, "scale_move_km": 30.0, "decay_days": 5.0, "coast_pref": 0.0,
        "wind": {}, "habitat": {"tree": 1.0, "shrub": 0.7, "built": 0.55, "grass": 0.25, "crop": 0.1, "bare": 0.2, "wetland": 0.1},
    },
    "wintergast-territoriaal": {
        "label": "Territoriale wintergasten",
        "method": "Bezetten een winterterritorium (klapekster, waterspreeuw, bonte kraai) en blijven vaak weken op dezelfde plek. "
                  "Hoge blijfkans, korte verplaatsing, geen wind.",
        "p_stay": 0.9, "sigma_stay_km": 3.0, "scale_move_km": 20.0, "decay_days": 7.0, "coast_pref": 0.0,
        "wind": {}, "habitat": {"shrub": 1.0, "grass": 0.6, "tree": 0.5, "bare": 0.5, "wetland": 0.6, "water": 0.3, "crop": 0.1, "built": 0.1},
    },
    "zangvogel-doortrekker": {
        "label": "Doortrekkende zangvogels",
        "method": "Dwaalgasten en doortrekkers uit Siberië en het zuiden (bladkoning, dwerggors, boszangers, piepers, tapuiten). "
                  "Korte stops in kustbosjes, struweel en open kustland, daarna verder. Wind: de nachtwind verschuift waar "
                  "doortrekkers de volgende dag opduiken (met de wind mee), en aanlandige wind zet aankomst op de kust.",
        "p_stay": 0.6, "sigma_stay_km": 3.0, "scale_move_km": 40.0, "decay_days": 3.0, "coast_pref": 0.6,
        "wind": {"drift": 1.0, "onshore": 0.5},
        "habitat": {"shrub": 1.0, "tree": 0.9, "built": 0.5, "grass": 0.4, "crop": 0.3, "bare": 0.3, "wetland": 0.3},
    },
    "watervogel": {
        "label": "Watervogels",
        "method": "Duikers, eenden, futen, aalscholvers, reigers en ibissen op open water en in moeras. Honkvast zolang het water open is; "
                  "verplaatsen zich tussen plassen. Geen wind.",
        "p_stay": 0.8, "sigma_stay_km": 5.0, "scale_move_km": 30.0, "decay_days": 7.0, "coast_pref": 0.3,
        "wind": {}, "habitat": {"water": 1.0, "wetland": 0.7, "grass": 0.3},
    },
    "gans": {
        "label": "Ganzen",
        "method": "Zeldzame ganzen trekken mee met groepen brand-, rot- of kolganzen op kustgrasland en akkers en blijven vaak weken "
                  "in dezelfde polder. Hoge blijfkans, geen wind.",
        "p_stay": 0.85, "sigma_stay_km": 5.0, "scale_move_km": 30.0, "decay_days": 7.0, "coast_pref": 0.6,
        "wind": {}, "habitat": {"grass": 1.0, "crop": 0.4, "wetland": 0.3, "water": 0.2},
    },
    "roofvogel": {
        "label": "Roofvogels en uilen",
        "method": "Zeer mobiel: een doortrekkende kiekendief of valk is de volgende dag vaak tientallen km verder, met de wind mee. "
                  "Lage blijfkans, grote verplaatsing, nachtwind als verschuiving.",
        "p_stay": 0.4, "sigma_stay_km": 5.0, "scale_move_km": 80.0, "decay_days": 2.0, "coast_pref": 0.0,
        "wind": {"drift": 0.7}, "habitat": {"crop": 1.0, "grass": 0.8, "wetland": 0.5, "shrub": 0.3, "bare": 0.3, "tree": 0.3},
    },
    "zeevogel": {
        "label": "Zeevogels",
        "method": "Jagers, pijlstormvogels, stormvogeltjes en zeldzame meeuwen trekken langs de kust en zijn vooral te zien bij "
                  "aanlandige wind. Lage blijfkans, alles draait om de kust en de wind van morgen.",
        "p_stay": 0.3, "sigma_stay_km": 5.0, "scale_move_km": 60.0, "decay_days": 1.5, "coast_pref": 1.0, "coast_scale_km": 10.0,
        "wind": {"onshore": 1.5}, "habitat": {"water": 1.0, "bare": 0.8, "grass": 0.2, "wetland": 0.2},
    },
    "steltloper": {
        "label": "Steltlopers",
        "method": "Zeldzame plevieren, ruiters en strandlopers pleisteren op slik, kwelders, plas-dras en natte akkers, vaak enkele dagen. "
                  "Verplaatsen zich tussen gebieden langs de kust. Geen wind.",
        "p_stay": 0.7, "sigma_stay_km": 4.0, "scale_move_km": 40.0, "decay_days": 4.0, "coast_pref": 0.7,
        "wind": {}, "habitat": {"wetland": 1.0, "bare": 0.8, "grass": 0.6, "water": 0.5, "crop": 0.3},
    },
    "standvogel": {
        "label": "Standvogels en zeldzame broedvogels",
        "method": "Soorten die het hele jaar op dezelfde plek zitten (graszanger, korhoen). Zeer hoge blijfkans, nauwelijks verplaatsing; "
                  "de kaart is hier vooral een onthoudkaart.",
        "p_stay": 0.95, "sigma_stay_km": 2.0, "scale_move_km": 10.0, "decay_days": 10.0, "coast_pref": 0.0,
        "wind": {}, "habitat": {"shrub": 0.7, "wetland": 0.7, "grass": 0.5, "tree": 0.5, "crop": 0.3},
    },
}

# Explicit assignments (lower-case Dutch name) that keyword rules would get wrong.
OVERRIDES = {
    "notenkraker": "invasiegast", "pestvogel": "invasiegast", "kruisbek": "invasiegast", "grote kruisbek": "invasiegast",
    "witbandkruisbek": "invasiegast", "taigaboomkruiper": "invasiegast", "ruigpootuil": "invasiegast", "sperweruil": "invasiegast",
    "klapekster": "wintergast-territoriaal", "waterspreeuw": "wintergast-territoriaal",
    "zwartbuikwaterspreeuw": "wintergast-territoriaal", "bonte kraai": "wintergast-territoriaal",
    "grote burgemeester": "watervogel", "kleine burgemeester": "watervogel", "zwarte ibis": "watervogel",
    "graszanger": "standvogel", "korhoen": "standvogel", "cetti's zanger": "standvogel",
    "kraanvogel": "steltloper",
}

# Keyword rules on whole words or word endings (Dutch compounds: "toendraslechtvalk"
# ends with "valk"), first match wins, checked in this order.
RULES = [
    ("gans", ["gans"]),
    ("roofvogel", ["kiekendief", "valk", "arend", "buizerd", "wouw", "uil", "sperwer", "havik", "wespendief", "gier", "oehoe"]),
    ("zeevogel", ["jager", "pijlstormvogel", "stormvogel", "stormvogeltje", "meeuw", "stern", "alk", "zeekoet",
                  "papegaaiduiker", "jan-van-gent", "gent"]),
    ("watervogel", ["duiker", "eend", "fuut", "aalscholver", "reiger", "kwak", "ibis", "lepelaar", "zwaan", "ooievaar",
                    "ral", "roerdomp", "woudaap", "flamingo", "pelikaan", "zaagbek", "taling", "smient"]),
    ("steltloper", ["plevier", "ruiter", "snip", "strandloper", "kluut", "kievit", "wulp", "grutto", "tureluur",
                    "franjepoot", "griel", "renvogel", "kanoet"]),
]
DEFAULT_GUILD = "zangvogel-doortrekker"

# Spelling variants merged into one layer (keys are norm_name() keys). Subspecies of
# rare species are merged through the export's parent field (species_list.py).
ALIASES = {
    "dunsnavelnotenkraker": "Notenkraker", "diksnavelnotenkraker": "Notenkraker",
}


def norm_name(name: str) -> str:
    """Matching key: lower case, no parentheses, single spaces ("Bonte Kraai (ssp cornix)"
    on the export and "Bonte Kraai ssp cornix" on observations become the same key)."""
    n = re.sub(r"[()]", "", name.strip().lower())
    n = re.sub(r"\s+ssp\.\s+", " ssp ", n)
    return re.sub(r"\s+", " ", n).strip()


def canonical_name(name: str) -> str | None:
    """Drop uncertain 'A of B' and 'onbekend' records; apply spelling aliases."""
    n = name.strip()
    if " of " in n.lower() or "onbekend" in n.lower():
        return None
    return ALIASES.get(norm_name(n), n)


# Family (Latin part of the export's family column) to guild; name overrides win,
# then family, then the name rules. Geese are split off Anatidae by genus.
FAMILY_GUILD = {
    "Gaviidae": "watervogel", "Podicipedidae": "watervogel", "Phalacrocoracidae": "watervogel", "Ardeidae": "watervogel",
    "Threskiornithidae": "watervogel", "Ciconiidae": "watervogel", "Rallidae": "watervogel", "Gruidae": "watervogel",
    "Phoenicopteridae": "watervogel", "Pelecanidae": "watervogel", "Anhingidae": "watervogel",
    "Laridae": "zeevogel", "Stercorariidae": "zeevogel", "Procellariidae": "zeevogel", "Hydrobatidae": "zeevogel",
    "Oceanitidae": "zeevogel", "Alcidae": "zeevogel", "Sulidae": "zeevogel", "Fregatidae": "zeevogel",
    "Phaethontidae": "zeevogel", "Diomedeidae": "zeevogel",
    "Accipitridae": "roofvogel", "Falconidae": "roofvogel", "Pandionidae": "roofvogel", "Strigidae": "roofvogel",
    "Tytonidae": "roofvogel", "Cathartidae": "roofvogel",
    "Charadriidae": "steltloper", "Scolopacidae": "steltloper", "Recurvirostridae": "steltloper", "Burhinidae": "steltloper",
    "Glareolidae": "steltloper", "Haematopodidae": "steltloper", "Pluvianidae": "steltloper", "Otididae": "steltloper",
    "Bombycillidae": "invasiegast",
}
GOOSE_GENERA = ("Anser", "Branta", "Chen")


def guild_for(name: str, family: str | None = None, sci: str | None = None) -> str:
    low = name.lower()
    if low in OVERRIDES:
        return OVERRIDES[low]
    if family:
        fam = family.split(" (")[0].strip()
        if fam == "Anatidae":
            return "gans" if sci and sci.split(" ")[0] in GOOSE_GENERA else "watervogel"
        if fam in FAMILY_GUILD:
            return FAMILY_GUILD[fam]
        if fam != "Aves":
            # any other family in the export is a landbird: name rules still catch owls etc.
            pass
    words = re.split(r"[\s'-]+", low)
    for guild, keys in RULES:
        if any(w == k or w.endswith(k) for w in words for k in keys):
            return guild
    return DEFAULT_GUILD


def slugify(name: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", base.lower())).strip("-")
