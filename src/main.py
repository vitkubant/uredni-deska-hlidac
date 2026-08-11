import csv
import io
import json
import re
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
    "3603",  # Chrudim
    "3606",  # Pardubice
    "3609",  # Svitavy
    "3611",  # Ústí nad Orlicí
}


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
    ).lower().strip()


def nacti_obce():
    print("Stahuji obce Pardubického kraje...")
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
            x for x in archive.namelist()
            if x.lower().endswith(".csv")
        ]

        if not csv_soubory:
            raise RuntimeError(
                "V RÚIAN ZIP nebyl nalezen CSV soubor."
            )

        raw = archive.read(
            csv_soubory[0]
        )

    text = None

    for encoding in (
        "utf-8-sig",
        "cp1250",
        "latin-1",
    ):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        raise RuntimeError(
            "CSV se nepodařilo dekódovat."
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
                "normalizovany_nazev":
                    bez_diakritiky(nazev),
            }
        )

    print(
        f"Nalezeno obcí: {len(obce)}"
    )

    return obce


def nacti_uredni_desky():
    print()
    print(
        "Stahuji metadata úředních desek z NKOD..."
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

    print(
        f"NKOD úředních desek: "
        f"{datasets['pagination']['totalCount']}"
    )

    return datasets["data"]


def najdi_kandidaty(obce, desky):
    """
    Najde úřední desky obcí Pardubického kraje.

    Důležité:
    Nehledáme pouze libovolné slovo uvnitř názvu.
    Pokud existuje obec "Vysoká" a úřední deska
    je "Vysoká nad Labem", musí se použít
    pouze úplný název "Vysoká nad Labem".
    """

    kandidati = []

    # Seřadíme obce od nejdelšího názvu.
    # Díky tomu dostane přednost například:
    #
    # Vysoká nad Labem
    #
    # před:
    #
    # Vysoká
    #
    obce_serazene = sorted(
        obce,
        key=lambda obec: len(
            obec["normalizovany_nazev"]
        ),
        reverse=True,
    )

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

        text = bez_diakritiky(
            title + " " + publisher
        )

        nalezena_obec = None

        for obec in obce_serazene:

            nazev = obec[
                "normalizovany_nazev"
            ]

            vzor = (
                r"(?<![a-z])"
                + re.escape(nazev)
                + r"(?![a-z])"
            )

            if not re.search(
                vzor,
                text
            ):
                continue

            # Ochrana proti situaci:
            #
            # "Kraj Vysočina"
            #
            # kde se sice nachází název obce
            # "Vysočina", ale poskytovatelem
            # není obec.
            #
            # U kandidáta chceme především obecní
            # nebo městský úřad.

            text_lower = text

            obecne_prefixy = (
                "obec ",
                "město ",
                "městys ",
                "obecní úřad ",
                "městský úřad ",
                "úřad městyse ",
                "magistrát města ",
                "statutární město ",
            )

            je_obecni_poskytovatel = any(
                prefix in text_lower
                for prefix in obecne_prefixy
            )

            if not je_obecni_poskytovatel:

                # Zkusíme ještě, zda je název obce
                # přesně na konci názvu poskytovatele.
                publisher_text = (
                    bez_diakritiky(
                        publisher
                    )
                )

                if not publisher_text.endswith(
                    nazev
                ):
                    continue

            nalezena_obec = obec
            break

        if nalezena_obec:

            kandidati.append(
                {
                    "deska": deska,
                    "obce": [
                        nalezena_obec
                    ],
                }
            )

    return kandidati


def stahni_jsonld(
    session,
    deska
):

    for distribution in (
        deska.get("distribution")
        or []
    ):

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


def text_informace(item):

    casti = []

    def projdi(obj):

        if isinstance(obj, str):
            casti.append(obj)

        elif isinstance(obj, dict):

            for value in obj.values():
                projdi(value)

        elif isinstance(obj, list):

            for value in obj:
                projdi(value)

    projdi(item)

    return bez_diakritiky(
        " ".join(casti)
    )


def je_podezrely_prodej(item):

    text = text_informace(item)

    pozemek = any(
        x in text
        for x in (
            "pozemek",
            "pozemku",
            "parcela",
            "parcelni",
            "parc. c",
            "p. c.",
        )
    )

    prodej = any(
        x in text
        for x in (
            "prodej",
            "prodeje",
            "prodeji",
            "prodat",
        )
    )

    zamer = (
        "zamer" in text
    )

    nemovitost = any(
        x in text
        for x in (
            "nemovitost",
            "nemovitosti",
        )
    )

    # Nejdůležitější kombinace:
    if pozemek and prodej:
        return True

    # Druhá užitečná kombinace:
    if zamer and (
        pozemek or nemovitost
    ):
        return True

    return False


def hlavni():

    print(
        "======================================"
    )
    print(
        " ÚŘEDNÍ DESKA HLÍDAČ"
    )
    print(
        " PARDUBICKÝ KRAJ"
    )
    print(
        "======================================"
    )
    print()

    # ------------------------------------
    # 1. Obce
    # ------------------------------------

    obce = nacti_obce()

    # ------------------------------------
    # 2. Metadata NKOD
    # ------------------------------------

    desky = nacti_uredni_desky()

    # ------------------------------------
    # 3. Levný filtr
    # ------------------------------------

    print()
    print(
        "Hledám možné úřední desky "
        "obcí Pardubického kraje..."
    )

    kandidati = najdi_kandidaty(
        obce,
        desky
    )

    print()
    print(
        f"Kandidátních úředních desek: "
        f"{len(kandidati)}"
    )

    print()

    for kandidat in kandidati:

        deska = kandidat["deska"]

        title = (
            (deska.get("title") or {})
            .get("cs")
            or "Bez názvu"
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznámý"
        )

        obce_text = ", ".join(
            obec["nazev"]
            for obec in kandidat["obce"]
        )

        print(
            f"✓ {publisher}"
        )

        print(
            f"  Dataset: {title}"
        )

        print(
            f"  Pravděpodobná obec: "
            f"{obce_text}"
        )

    # ------------------------------------
    # 4. Stahování pouze kandidátů
    # ------------------------------------

    print()
    print(
        "======================================"
    )
    print(
        " STAHUJI VYBRANÉ ÚŘEDNÍ DESKY"
    )
    print(
        "======================================"
    )
    print()

    session = requests.Session()

    nalezene = []

    for cislo, kandidat in enumerate(
        kandidati,
        start=1
    ):

        deska = kandidat["deska"]

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznámý"
        )

        print(
            f"[{cislo}/{len(kandidati)}] "
            f"{publisher}"
        )

        data = stahni_jsonld(
            session,
            deska
        )

        if data is None:

            print(
                "   ✗ nepodařilo se stáhnout"
            )

            continue

        informace = ziskej_informace(
            data
        )

        print(
            f"   {len(informace)} informací"
        )

        for item in informace:

            if not je_podezrely_prodej(
                item
            ):
                continue

            nazev = (
                (
                    item.get("název")
                    or {}
                ).get("cs")
                or "Bez názvu"
            )

            datum = (
                (
                    item.get("vyvěšení")
                    or {}
                ).get("datum")
                or "?"
            )

            url = (
                item.get("url")
                or ""
            )

            nalezene.append(
                {
                    "obce": [
                        obec["nazev"]
                        for obec
                        in kandidat["obce"]
                    ],
                    "publisher":
                        publisher,
                    "nazev":
                        nazev,
                    "datum":
                        datum,
                    "url":
                        url,
                }
            )

        time.sleep(0.1)

    # ------------------------------------
    # 5. Výsledky
    # ------------------------------------

    print()
    print(
        "======================================"
    )
    print(
        " NALEZENÉ KANDIDÁTNÍ NABÍDKY"
    )
    print(
        "======================================"
    )
    print()

    print(
        f"Celkem: {len(nalezene)}"
    )

    print()

    for cislo, item in enumerate(
        nalezene[:100],
        start=1
    ):

        print(
            f"{cislo}. {item['nazev']}"
        )

        print(
            f"   Obec: "
            f"{', '.join(item['obce'])}"
        )

        print(
            f"   Poskytovatel: "
            f"{item['publisher']}"
        )

        print(
            f"   Vyvěšení: "
            f"{item['datum']}"
        )

        if item["url"]:
            print(
                f"   URL: "
                f"{item['url']}"
            )

        print()

    print(
        "======================================"
    )
    print(
        " KONEC"
    )
    print(
        "======================================"
    )


if __name__ == "__main__":
    hlavni()
