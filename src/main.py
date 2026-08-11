import csv
import io
import json
import unicodedata
import zipfile

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

    obce.sort(
        key=lambda obec: bez_diakritiky(
            obec["nazev"]
        )
    )

    print(
        f"Obcí v Pardubickém kraji: "
        f"{len(obce)}"
    )

    return obce


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
            "GraphQL chyba:"
        )
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

    print(
        f"Staženo metadat: "
        f"{len(data)}"
    )

    return data


def stahni_obsah_desky(deska):
    """
    Stáhne JSON-LD konkrétní úřední desky.
    """

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

        # Preferujeme JSON-LD.
        if (
            "json" not in
            format_data.lower()
            and not url.lower().endswith(
                (".json", ".jsonld")
            )
        ):
            continue

        if not url:
            continue

        try:

            response = requests.get(
                url,
                timeout=30
            )

            response.raise_for_status()

            return (
                url,
                response.json()
            )

        except Exception as error:

            print(
                f"  Chyba při stahování: "
                f"{error}"
            )

            return (
                url,
                None
            )

    return (
        None,
        None
    )


def ziskej_informace(data):
    """
    Vytáhne pole 'informace' z JSON-LD.
    """

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


def main():

    print(
        "======================================"
    )
    print(
        "  ÚŘEDNÍ DESKA HLÍDAČ"
    )
    print(
        "  Test obsahu úředních desek"
    )
    print(
        "======================================"
    )
    print()

    # ----------------------------------
    # 1. Obce
    # ----------------------------------

    obce = nacti_obce()

    # ----------------------------------
    # 2. Úřední desky
    # ----------------------------------

    desky = nacti_uredni_desky()

    print()
    print(
        "======================================"
    )
    print(
        "  TEST DISTRIBUCÍ"
    )
    print(
        "======================================"
    )
    print()

    pocet_s_url = 0
    pocet_stazenych = 0

    # Zatím pouze prvních 10.
    # Nechceme při testu zbytečně
    # stahovat stovky souborů.
    for cislo, deska in enumerate(
        desky[:10],
        start=1
    ):

        title = (
            (deska.get("title") or {})
            .get("cs")
            or "Bez názvu"
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznámý poskytovatel"
        )

        print(
            f"{cislo}. {title}"
        )

        print(
            f"   Poskytovatel: {publisher}"
        )

        distributions = (
            deska.get("distribution")
            or []
        )

        print(
            f"   Distribucí: "
            f"{len(distributions)}"
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

            if url:

                pocet_s_url += 1

                print(
                    f"   URL: {url}"
                )

                print(
                    f"   Formát: "
                    f"{format_data}"
                )

        url, data = stahni_obsah_desky(
            deska
        )

        if data is not None:

            pocet_stazenych += 1

            informace = ziskej_informace(
                data
            )

            print(
                f"   ✓ JSON-LD stažen"
            )

            print(
                f"   Počet informací: "
                f"{len(informace)}"
            )

            # Ukázka prvních tří dokumentů.
            for informace_item in (
                informace[:3]
            ):

                nazev = (
                    (
                        informace_item
                        .get("název")
                        or {}
                    )
                    .get("cs")
                    or "Bez názvu"
                )

                vyveseni = (
                    (
                        informace_item
                        .get("vyvěšení")
                        or {}
                    )
                    .get("datum")
                    or "?"
                )

                url_dokumentu = (
                    informace_item.get("url")
                    or ""
                )

                print(
                    f"      - {vyveseni} | "
                    f"{nazev}"
                )

                if url_dokumentu:
                    print(
                        f"        {url_dokumentu}"
                    )

        else:

            print(
                "   ✗ JSON-LD se nepodařilo stáhnout"
            )

        print()

    print(
        "======================================"
    )
    print(
        "  VÝSLEDEK TESTU"
    )
    print(
        "======================================"
    )

    print(
        f"Úředních desek testováno: "
        f"{min(10, len(desky))}"
    )

    print(
        f"Distribucí s URL: "
        f"{pocet_s_url}"
    )

    print(
        f"Úředních desek staženo: "
        f"{pocet_stazenych}"
    )

    print()

    print(
        "Další krok bude hledání "
        "nabídek pozemků."
    )


if __name__ == "__main__":
    main()
