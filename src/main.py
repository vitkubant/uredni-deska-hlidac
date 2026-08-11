import csv
import io
import json
import unicodedata
import zipfile
import time

import requests


NKOD_GRAPHQL = "https://data.gov.cz/graphql"

RUIAN_URL = "https://services.cuzk.cz/sestavy/cis/UI_OBEC.zip"

UREDNI_DESKY_OFN = (
    "https://ofn.gov.cz/úřední-desky/2021-07-20/"
)

PARDUBICKY_OKRESY = {
    "3601",
    "3602",
    "3603",
    "3604",
}


# ============================================================
# POMOCNÉ FUNKCE
# ============================================================

def bez_diakritiky(text):
    text = text or ""

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    return "".join(
        znak
        for znak in text
        if not unicodedata.combining(znak)
    ).lower()


def text_z_objektu(objekt):
    """
    Rekurzivně vytáhne text ze struktury JSON-LD.
    """
    vysledek = []

    if isinstance(objekt, str):
        vysledek.append(objekt)

    elif isinstance(objekt, dict):
        for hodnota in objekt.values():
            vysledek.extend(
                text_z_objektu(hodnota)
            )

    elif isinstance(objekt, list):
        for polozka in objekt:
            vysledek.extend(
                text_z_objektu(polozka)
            )

    return " ".join(vysledek)


# ============================================================
# RÚIAN
# ============================================================

def nacti_obce():
    print("Stahuji seznam obcí z RÚIAN...")
    print()

    response = requests.get(
        RUIAN_URL,
        timeout=60
    )

    response.raise_for_status()

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as archive:

        csv_soubory = [
            soubor
            for soubor in archive.namelist()
            if soubor.lower().endswith(".csv")
        ]

        if not csv_soubory:
            raise RuntimeError(
                "V ZIP souboru nebyl nalezen CSV soubor."
            )

        raw_data = archive.read(
            csv_soubory[0]
        )

    text = None

    for encoding in [
        "utf-8-sig",
        "cp1250",
        "latin-1"
    ]:

        try:
            text = raw_data.decode(
                encoding
            )
            break

        except UnicodeDecodeError:
            pass

    if text is None:
        raise RuntimeError(
            "Nepodařilo se načíst CSV."
        )

    reader = csv.DictReader(
        io.StringIO(text),
        delimiter=";"
    )

    obce = []

    for row in reader:

        kod = (
            row.get("KOD")
            or ""
        ).strip()

        nazev = (
            row.get("NAZEV")
            or ""
        ).strip()

        okres = (
            row.get("OKRES_KOD")
            or ""
        ).strip()

        plati_do = (
            row.get("PLATI_DO")
            or ""
        ).strip()

        if not kod or not nazev:
            continue

        if plati_do:
            continue

        if okres not in PARDUBICKY_OKRESY:
            continue

        obce.append(
            {
                "kod": kod,
                "nazev": nazev,
                "okres": okres
            }
        )

    print(
        f"Obcí v Pardubickém kraji: "
        f"{len(obce)}"
    )

    return obce


# ============================================================
# NKOD
# ============================================================

def nacti_uredni_desky():
    print()
    print(
        "Stahuji seznam úředních desek z NKOD..."
    )

    query = f"""
    query {{
      datasets(
        limit: 1000
        filters: {{
          conformsTo: "{UREDNI_DESKY_OFN}"
        }}
      ) {{
        data {{
          iri

          title {{
            cs
          }}

          publisher {{
            title {{
              cs
            }}

            iri
          }}

          distribution {{
            accessURL
            format
          }}
        }}

        pagination {{
          totalCount
        }}
      }}
    }}
    """

    response = requests.post(
        NKOD_GRAPHQL,
        json={
            "query": query
        },
        timeout=60
    )

    response.raise_for_status()

    result = response.json()

    if "errors" in result:
        print(
            json.dumps(
                result["errors"],
                indent=2,
                ensure_ascii=False
            )
        )

        raise RuntimeError(
            "NKOD GraphQL vrátil chybu."
        )

    datasets = result["data"]["datasets"]

    total = datasets["pagination"]["totalCount"]

    data = datasets["data"]

    print(
        f"NKOD obsahuje "
        f"{total} úředních desek."
    )

    return data


# ============================================================
# STAŽENÍ ÚŘEDNÍ DESKY
# ============================================================

def stahni_obsah_desky(
    session,
    deska
):

    distributions = (
        deska.get("distribution")
        or []
    )

    for distribution in distributions:

        url = (
            distribution.get("accessURL")
            or ""
        )

        format_data = (
            distribution.get("format")
            or ""
        )

        if not url:
            continue

        # JSON-LD / JSON
        if (
            "json" not in
            format_data.lower()
            and not url.lower().endswith(
                (".json", ".jsonld")
            )
        ):
            continue

        try:

            response = session.get(
                url,
                timeout=20
            )

            response.raise_for_status()

            return response.json()

        except Exception as error:

            print(
                f"      Chyba: {error}"
            )

            return None

    return None


# ============================================================
# INFORMACE Z JSON-LD
# ============================================================

def ziskej_informace(data):

    if not isinstance(
        data,
        dict
    ):
        return []

    informace = (
        data.get("informace")
        or []
    )

    if isinstance(
        informace,
        dict
    ):
        informace = [
            informace
        ]

    return informace


# ============================================================
# HLEDÁNÍ PRODEJŮ POZEMKŮ
# ============================================================

def vyhodnot_informaci(informace):

    text = bez_diakritiky(
        text_z_objektu(
            informace
        )
    )

    # --------------------------------------------
    # Hlavní výrazy
    # --------------------------------------------

    ma_pozemek = any(
        slovo in text
        for slovo in [
            "pozemek",
            "pozemku",
            "pozemkem",
        ]
    )

    ma_parcelu = any(
        slovo in text
        for slovo in [
            "parcela",
            "parcelni",
            "parc. c",
            "p. c.",
        ]
    )

    ma_prodej = any(
        slovo in text
        for slovo in [
            "prodej",
            "prodeje",
            "prodeji",
            "prodat",
        ]
    )

    ma_zamer = any(
        slovo in text
        for slovo in [
            "zamer prodeje",
            "zamer na prodej",
            "zamer",
        ]
    )

    ma_nemovitost = any(
        slovo in text
        for slovo in [
            "nemovitost",
            "nemovitosti",
        ]
    )

    ma_prev = any(
        slovo in text
        for slovo in [
            "prevod",
            "prevodu",
            "prevodem",
        ]
    )

    # --------------------------------------------
    # Bodování
    # --------------------------------------------

    skore = 0

    if ma_pozemek:
        skore += 3

    if ma_parcelu:
        skore += 3

    if ma_prodej:
        skore += 3

    if ma_zamer:
        skore += 2

    if ma_nemovitost:
        skore += 1

    if ma_prev:
        skore += 1

    # --------------------------------------------
    # Kandidát
    # --------------------------------------------

    # Silný kandidát:
    # pozemek/parcela + prodej
    if (
        (ma_pozemek or ma_parcelu)
        and ma_prodej
    ):
        return skore

    # Nebo:
    # záměr + nemovitost
    if (
        ma_zamer
        and ma_nemovitost
    ):
        return skore

    return 0


# ============================================================
# HLAVNÍ PROGRAM
# ============================================================

def main():

    print(
        "======================================"
    )
    print(
        "      ÚŘEDNÍ DESKA HLÍDAČ"
    )
    print(
        "      HLEDÁNÍ PRODEJŮ POZEMKŮ"
    )
    print(
        "======================================"
    )
    print()

    # ----------------------------------------
    # 1. Obce
    # ----------------------------------------

    obce = nacti_obce()

    # ----------------------------------------
    # 2. Úřední desky
    # ----------------------------------------

    desky = nacti_uredni_desky()

    print()
    print(
        "======================================"
    )
    print(
        "  PROHLEDÁVÁNÍ ÚŘEDNÍCH DESEK"
    )
    print(
        "======================================"
    )
    print()

    session = requests.Session()

    kandidati = []

    uspesne = 0
    neuspesne = 0
    celkem_informaci = 0

    celkem = len(desky)

    for cislo, deska in enumerate(
        desky,
        start=1
    ):

        title = (
            (deska.get("title") or {})
            .get("cs")
            or "Bez názvu"
        )

        publisher_data = (
            deska.get("publisher")
            or {}
        )

        publisher_title = (
            (publisher_data.get("title") or {})
            .get("cs")
            or "Neznámý poskytovatel"
        )

        print(
            f"[{cislo}/{celkem}] "
            f"{publisher_title}"
        )

        data = stahni_obsah_desky(
            session,
            deska
        )

        if data is None:

            neuspesne += 1

            print(
                "      ✗ nepodařilo se stáhnout"
            )

            continue

        uspesne += 1

        informace = ziskej_informace(
            data
        )

        celkem_informaci += len(
            informace
        )

        print(
            f"      {len(informace)} informací"
        )

        for informace_item in informace:

            skore = vyhodnot_informaci(
                informace_item
            )

            if skore <= 0:
                continue

            nazev_data = (
                informace_item.get("název")
                or {}
            )

            nazev = (
                nazev_data.get("cs")
                or ""
            )

            datum_data = (
                informace_item.get("vyvěšení")
                or {}
            )

            datum = (
                datum_data.get("datum")
                or "?"
            )

            url = (
                informace_item.get("url")
                or ""
            )

            kandidati.append(
                {
                    "skore": skore,
                    "poskytovatel":
                        publisher_title,
                    "nazev": nazev,
                    "datum": datum,
                    "url": url
                }
            )

        # Malá pauza, ať zbytečně
        # nezatěžujeme servery.
        time.sleep(0.1)

    # ----------------------------------------
    # Výsledky
    # ----------------------------------------

    kandidati.sort(
        key=lambda x: (
            x["datum"],
            x["skore"]
        ),
        reverse=True
    )

    print()
    print(
        "======================================"
    )
    print(
        "  VÝSLEDKY"
    )
    print(
        "======================================"
    )
    print()

    print(
        f"Úředních desek: {celkem}"
    )

    print(
        f"Úspěšně staženo: {uspesne}"
    )

    print(
        f"Chyb při stažení: {neuspesne}"
    )

    print(
        f"Celkem informací: "
        f"{celkem_informaci}"
    )

    print(
        f"Podezřelých nabídek: "
        f"{len(kandidati)}"
    )

    print()

    if not kandidati:

        print(
            "Nebyly nalezeny žádné kandidátní nabídky."
        )

    else:

        print(
            "======================================"
        )
        print(
            "  KANDIDÁTI NA PRODEJ POZEMKU"
        )
        print(
            "======================================"
        )
        print()

        # Zatím vypíšeme maximálně 50 výsledků.
        for cislo, kandidat in enumerate(
            kandidati[:50],
            start=1
        ):

            print(
                f"{cislo}. "
                f"[skóre {kandidat['skore']}] "
                f"{kandidat['poskytovatel']}"
            )

            print(
                f"   Datum: "
                f"{kandidat['datum']}"
            )

            print(
                f"   Název: "
                f"{kandidat['nazev']}"
            )

            if kandidat["url"]:
                print(
                    f"   URL: "
                    f"{kandidat['url']}"
                )

            print()

    print(
        "======================================"
    )
    print(
        "  KONEC TESTU"
    )
    print(
        "======================================"
    )


if __name__ == "__main__":
    main()
