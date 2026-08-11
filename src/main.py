import re
import unicodedata
import requests

from bs4 import BeautifulSoup


NKOD_GRAPHQL = "https://data.gov.cz/graphql"

# RÚIAN / VDP:
# Kód Pardubického kraje (VÚSC) = 94
RUIAN_URL = "https://vdp.cuzk.gov.cz/vdp/ruian/obce"

UREDNI_DESKY_OFN = (
    "https://ofn.gov.cz/úřední-desky/2021-07-20/"
)


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
    """Stáhne seznam obcí Pardubického kraje z RÚIAN."""

    print("Stahuji obce Pardubického kraje z RÚIAN...")
    print()

    vsechny_obce = []

    # Nejprve zjistíme první stránku.
    response = requests.get(
        RUIAN_URL,
        params={
            "kodVusc": "94"
        },
        timeout=60
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # RÚIAN standardně zobrazuje 20 obcí na stránku.
    # Z textu stránky zjistíme celkový počet.
    text = soup.get_text(" ", strip=True)

    match = re.search(
        r"\d+-\d+\s+z\s+(\d+)",
        text
    )

    if match:
        celkem = int(match.group(1))
    else:
        celkem = None

    # Zpracování první stránky.
    vsechny_obce.extend(
        extrahuj_obce(soup)
    )

    print(
        f"První stránka: "
        f"{len(vsechny_obce)} obcí"
    )

    # Pokud známe celkový počet, stáhneme další stránky.
    if celkem:
        pocet_stranek = (celkem + 19) // 20

        for page in range(2, pocet_stranek + 1):

            response = requests.get(
                RUIAN_URL,
                params={
                    "kodVusc": "94",
                    "page": str(page)
                },
                timeout=60
            )

            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            nove_obce = extrahuj_obce(soup)

            vsechny_obce.extend(
                nove_obce
            )

            print(
                f"Stránka {page}/{pocet_stranek}: "
                f"+{len(nove_obce)} obcí"
            )

    # Odstranění případných duplicit.
    unikaty = {}

    for obec in vsechny_obce:
        unikaty[obec["kod"]] = obec

    obce = list(
        unikaty.values()
    )

    print()
    print(
        f"Celkem načteno obcí: {len(obce)}"
    )

    return obce


def extrahuj_obce(soup):
    """Extrahuje obce z tabulky RÚIAN."""

    obce = []

    for row in soup.select("table tbody tr"):

        cells = row.find_all("td")

        if len(cells) < 2:
            continue

        kod = cells[0].get_text(
            " ",
            strip=True
        )

        nazev = cells[1].get_text(
            " ",
            strip=True
        )

        # Kód obce je číselný.
        if not kod.isdigit():
            continue

        if not nazev:
            continue

        obce.append(
            {
                "kod": kod,
                "nazev": nazev
            }
        )

    return obce


def nacti_uredni_desky():
    """Stáhne seznam úředních desek z NKOD."""

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
        f"NKOD obsahuje {total} úředních desek."
    )

    return data


def najdi_shodu(obec, desky):
    """Najde úřední desky, které pravděpodobně patří obci."""

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

        # Hledáme název obce v názvu
        # nebo poskytovateli.
        if (
            hledany_nazev in title_norm
            or hledany_nazev in publisher_norm
        ):
            vysledky.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "iri": deska["iri"]
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

    # 1. Obce
    obce = nacti_obce()

    # 2. Úřední desky
    desky = nacti_uredni_desky()

    print()
    print(
        "======================================"
    )
    print(
        "  POROVNÁNÍ OBCÍ A ÚŘEDNÍCH DESEK"
    )
    print(
        "======================================"
    )
    print()

    nalezene = 0
    nenalezene = 0

    for obec in obce:

        shody = najdi_shodu(
            obec,
            desky
        )

        if shody:

            nalezene += 1

            print(
                f"✓ {obec['nazev']}"
            )

            for shoda in shody:

                print(
                    f"    Úřední deska: "
                    f"{shoda['title']}"
                )

                print(
                    f"    Poskytovatel: "
                    f"{shoda['publisher']}"
                )

                print(
                    f"    IRI: "
                    f"{shoda['iri']}"
                )

        else:

            nenalezene += 1

            print(
                f"✗ {obec['nazev']} "
                f"(nenalezena v NKOD)"
            )

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

    if obce:
        pokryti = (
            nalezene / len(obce) * 100
        )

        print(
            f"Pokrytí přes NKOD: "
            f"{pokryti:.1f} %"
        )


if __name__ == "__main__":
    main()
