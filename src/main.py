import csv
import io
import unicodedata
import zipfile

import requests


NKOD_GRAPHQL = "https://data.gov.cz/graphql"

# Oficiální číselník obcí RÚIAN od ČÚZK
RUIAN_URL = "https://services.cuzk.cz/sestavy/cis/UI_OBEC.zip"

UREDNI_DESKY_OFN = (
    "https://ofn.gov.cz/úřední-desky/2021-07-20/"
)

# Okresy Pardubického kraje:
#
# Chrudim          3601
# Pardubice        3602
# Svitavy          3603
# Ústí nad Orlicí  3604
#
# Používáme kódy okresů místo názvů,
# takže nehrozí problém s diakritikou.
PARDUBICKY_OKRESY = {
    "3601",
    "3602",
    "3603",
    "3604",
}


def bez_diakritiky(text):
    """Odstraní diakritiku a převede text na malá písmena."""

    text = text or ""

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = "".join(
        znak
        for znak in text
        if not unicodedata.combining(znak)
    )

    return text.lower().strip()


def nacti_obce():
    """Stáhne všechny obce z RÚIAN a vybere Pardubický kraj."""

    print("Stahuji seznam obcí z RÚIAN...")
    print(f"Zdroj: {RUIAN_URL}")
    print()

    response = requests.get(
        RUIAN_URL,
        timeout=60
    )

    response.raise_for_status()

    print(
        f"Staženo: "
        f"{len(response.content):,} bytů"
    )

    # ZIP otevřeme přímo z paměti.
    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as archive:

        soubory = archive.namelist()

        print("Obsah ZIP souboru:")
        for soubor in soubory:
            print(f"  {soubor}")

        print()

        # Najdeme CSV soubor.
        csv_soubory = [
            soubor
            for soubor in soubory
            if soubor.lower().endswith(".csv")
        ]

        if not csv_soubory:
            raise RuntimeError(
                "V UI_OBEC.zip nebyl nalezen CSV soubor."
            )

        csv_soubor = csv_soubory[0]

        print(
            f"Používám soubor: {csv_soubor}"
        )
        print()

        raw_data = archive.read(
            csv_soubor
        )

    # ČÚZK může změnit kódování.
    # Zkusíme UTF-8 a potom Windows-1250.
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

            print(
                f"CSV načteno jako {encoding}."
            )

            break

        except UnicodeDecodeError:
            continue

    if text is None:
        raise RuntimeError(
            "Nepodařilo se dekódovat CSV."
        )

    # CSV ČÚZK používá středník.
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

        # Pouze aktuálně platné obce.
        if plati_do:
            continue

        # Pouze okresy Pardubického kraje.
        if okres not in PARDUBICKY_OKRESY:
            continue

        obce.append(
            {
                "kod": kod,
                "nazev": nazev,
                "okres": okres
            }
        )

    obce.sort(
        key=lambda obec: bez_diakritiky(
            obec["nazev"]
        )
    )

    print()
    print(
        f"Obcí v Pardubickém kraji: "
        f"{len(obce)}"
    )

    return obce


def nacti_uredni_desky():
    """Stáhne úřední desky z NKOD."""

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
        raise RuntimeError(
            result["errors"]
        )

    datasets = result["data"]["datasets"]

    total = datasets["pagination"]["totalCount"]

    data = datasets["data"]

    print(
        f"NKOD obsahuje "
        f"{total} úředních desek."
    )

    print(
        f"Staženo záznamů: "
        f"{len(data)}"
    )

    return data


def najdi_shodu(obec, desky):
    """
    Pokusí se najít úřední desky dané obce.

    Hledáme název obce v názvu poskytovatele
    nebo názvu datové sady.
    """

    hledany_nazev = bez_diakritiky(
        obec["nazev"]
    )

    vysledky = []

    for deska in desky:

        title = (
            (deska.get("title") or {})
            .get("cs")
            or ""
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or ""
        )

        title_norm = bez_diakritiky(
            title
        )

        publisher_norm = bez_diakritiky(
            publisher
        )

        # Preferujeme shodu v názvu poskytovatele.
        if hledany_nazev in publisher_norm:

            vysledky.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "iri": deska["iri"],
                    "typ_shody": "poskytovatel"
                }
            )

            continue

        # Druhá možnost je název samotné datové sady.
        if hledany_nazev in title_norm:

            vysledky.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "iri": deska["iri"],
                    "typ_shody": "název datové sady"
                }
            )

    return vysledky


def main():

    print(
        "======================================"
    )
    print(
        "  ÚŘEDNÍ DESKA HLÍDAČ"
    )
    print(
        "  Pardubický kraj"
    )
    print(
        "======================================"
    )
    print()

    # ------------------------------------
    # 1. Obce Pardubického kraje
    # ------------------------------------

    obce = nacti_obce()

    if not obce:
        raise RuntimeError(
            "Nenalezena žádná obec. "
            "Zkontroluj strukturu RÚIAN CSV."
        )

    # ------------------------------------
    # 2. Úřední desky z NKOD
    # ------------------------------------

    desky = nacti_uredni_desky()

    # ------------------------------------
    # 3. Porovnání
    # ------------------------------------

    print()
    print(
        "======================================"
    )
    print(
        "  POROVNÁNÍ"
    )
    print(
        "======================================"
    )
    print()

    nalezene = 0
    nenalezene = 0

    vysledky = []

    for obec in obce:

        shody = najdi_shodu(
            obec,
            desky
        )

        if shody:

            nalezene += 1

            vysledky.append(
                {
                    "obec": obec,
                    "shody": shody
                }
            )

        else:

            nenalezene += 1

            vysledky.append(
                {
                    "obec": obec,
                    "shody": []
                }
            )

    # ------------------------------------
    # 4. Výpis nalezených
    # ------------------------------------

    print(
        "OBCE S NALEZENOU ÚŘEDNÍ DESKOU"
    )
    print(
        "--------------------------------------"
    )
    print()

    for vysledek in vysledky:

        if not vysledek["shody"]:
            continue

        obec = vysledek["obec"]

        print(
            f"✓ {obec['nazev']}"
        )

        print(
            f"  Kód obce: {obec['kod']}"
        )

        print(
            f"  Okres: {obec['okres']}"
        )

        for shoda in vysledek["shody"]:

            print(
                f"  Úřední deska: "
                f"{shoda['title']}"
            )

            print(
                f"  Poskytovatel: "
                f"{shoda['publisher']}"
            )

            print(
                f"  Typ shody: "
                f"{shoda['typ_shody']}"
            )

            print(
                f"  IRI: "
                f"{shoda['iri']}"
            )

        print()

    # ------------------------------------
    # 5. Výpis nenalezených
    # ------------------------------------

    print()
    print(
        "OBCE BEZ NALEZENÉ ÚŘEDNÍ DESKY"
    )
    print(
        "--------------------------------------"
    )
    print()

    for vysledek in vysledky:

        if vysledek["shody"]:
            continue

        obec = vysledek["obec"]

        print(
            f"✗ {obec['nazev']} "
            f"(kód {obec['kod']})"
        )

    # ------------------------------------
    # 6. Souhrn
    # ------------------------------------

    print()
    print(
        "======================================"
    )
    print(
        "  VÝSLEDEK"
    )
    print(
        "======================================"
    )

    print(
        f"Obcí v Pardubickém kraji: "
        f"{len(obce)}"
    )

    print(
        f"Nalezena úřední deska: "
        f"{nalezene}"
    )

    print(
        f"Bez nalezené desky: "
        f"{nenalezene}"
    )

    pokryti = (
        nalezene / len(obce) * 100
    )

    print(
        f"Pokrytí přes NKOD: "
        f"{pokryti:.1f} %"
    )


if __name__ == "__main__":
    main()
