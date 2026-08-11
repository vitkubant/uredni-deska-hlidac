import csv
import io
import json
import re
import unicodedata
import zipfile
import time
from datetime import date, datetime, timedelta

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

# Kolik dní zpět chceme sledovat nové nabídky.
MAX_STARI_DNI = 30


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
    Spolehlivější přiřazení úřední desky ke konkrétní obci.

    Nepoužíváme substring typu:
        "Moravany" in "Moravany u Brna"

    ale snažíme se získat skutečný název
    organizace a porovnat ho s celým názvem obce.
    """

    kandidati = []

    # ----------------------------------------
    # Pomocná funkce
    # ----------------------------------------

    def cisti_text(text):
        text = bez_diakritiky(text)

        # sjednotíme pomlčky
        text = text.replace("–", "-")
        text = text.replace("—", "-")

        # odstraníme nadbytečné mezery
        text = " ".join(text.split())

        return text.strip()

    def odpovida_obci(text, obec):

        text = cisti_text(text)

        nazev = cisti_text(
            obec["nazev"]
        )

        # ------------------------------------
        # Zakázané typy organizací
        # ------------------------------------

        zakazane = (
            "kraj ",
            "krajsky urad",
            "krajsky ",
            "ministerstvo",
            "okresni soud",
            "krajsky soud",
            "statni zastupitelstvi",
            "vojensky ujezd",
            "urad prace",
            "financni urad",
            "katastralni urad",
            "policie",
            "nemocnice",
        )

        for zakaz in zakazane:

            if text.startswith(zakaz):
                return False

        # ------------------------------------
        # Přesné tvary názvu obce
        # ------------------------------------

        mozne_tvary = [
            f"obec {nazev}",
            f"mesto {nazev}",
            f"mestys {nazev}",
            f"statutarni mesto {nazev}",
            f"obecni urad {nazev}",
            f"mestsky urad {nazev}",
            f"urad mesta {nazev}",
            f"magistrat mesta {nazev}",
            f"uredni deska {nazev}",
            nazev,
        ]

        # Přesná shoda
        if text in mozne_tvary:
            return True

        # ------------------------------------
        # Některé desky mají například:
        #
        # "Úřední deska - Lanškroun"
        # "Úřední deska města Pardubice"
        # ------------------------------------

        odstranovaci_prefixy = (
            "uredni deska - ",
            "uredni deska – ",
            "uredni deska mesta ",
            "uredni deska obce ",
            "uredni deska mestyse ",
            "uredni deska uradu ",
            "uredni deska ",
        )

        for prefix in odstranovaci_prefixy:

            if text.startswith(prefix):

                zbytek = text[
                    len(prefix):
                ].strip()

                if zbytek == nazev:
                    return True

        # ------------------------------------
        # Poskytovatel
        #
        # "Obec Dlouhá Třebová"
        # "Město Chrudim"
        # ------------------------------------

        if text.startswith("obec "):

            zbytek = text[
                len("obec "):
            ].strip()

            return zbytek == nazev

        if text.startswith("mesto "):

            zbytek = text[
                len("mesto "):
            ].strip()

            return zbytek == nazev

        if text.startswith("mestys "):

            zbytek = text[
                len("mestys "):
            ].strip()

            return zbytek == nazev

        if text.startswith(
            "statutarni mesto "
        ):

            zbytek = text[
                len("statutarni mesto "):
            ].strip()

            return zbytek == nazev

        # ------------------------------------
        # Jinak NE
        # ------------------------------------

        return False

    # ----------------------------------------
    # Procházení úředních desek
    # ----------------------------------------

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

        nalezena_obec = None

        # Nejdřív zkusíme poskytovatele
        # (ten je pro identifikaci nejdůležitější).

        for obec in obce:

            if odpovida_obci(
                publisher,
                obec
            ):
                nalezena_obec = obec
                break

        # Pokud poskytovatel nestačí,
        # zkusíme ještě název datasetu.

        if nalezena_obec is None:

            for obec in obce:

                if odpovida_obci(
                    title,
                    obec
                ):
                    nalezena_obec = obec
                    break

        if nalezena_obec is None:
            continue

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

        # ----------------------------------------
        # Normální pokus
        # ----------------------------------------

        try:

            response = session.get(
                url,
                timeout=30
            )

            response.raise_for_status()

            try:
                return response.json()

            except ValueError:

                print(
                    "      Server vrátil neplatný JSON."
                )

                print(
                    f"      Content-Type: "
                    f"{response.headers.get('Content-Type', '')}"
                )

                print(
                    f"      HTTP: {response.status_code}"
                )

                # Pokračujeme na další distribuci,
                # pokud nějaká existuje.
                continue

        except requests.exceptions.SSLError as error:

            print(
                "      SSL chyba."
            )

            print(
                "      Zkouším připojení bez ověření certifikátu..."
            )

            # Některé obecní weby mají špatně nastavený
            # SSL certifikát. Pro čtení veřejných dat
            # zkusíme ještě druhý pokus.
            try:

                response = session.get(
                    url,
                    timeout=30,
                    verify=False
                )

                response.raise_for_status()

                try:
                    return response.json()

                except ValueError:

                    print(
                        "      Ani druhý pokus nevrátil JSON."
                    )

                    continue

            except Exception as druhy_error:

                print(
                    f"      Druhý pokus selhal: "
                    f"{druhy_error}"
                )

                continue

        except requests.exceptions.RequestException as error:

            print(
                f"      Chyba připojení: {error}"
            )

            continue

        except Exception as error:

            print(
                f"      Neočekávaná chyba: {error}"
            )

            continue

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


def ziskej_datum_vyveseni(item):
    """
    Pokusí se najít datum vyvěšení v datech informace.
    """

    if not isinstance(item, dict):
        return None

    # Nejčastější přímé názvy polí.
    klice = (
        "datumVyveseni",
        "datum_vyveseni",
        "vyveseni",
        "datumZverejneni",
        "datum_zverejneni",
        "zverejneni",
    )

    for klic in klice:

        hodnota = item.get(klic)

        if hodnota:
            return hodnota

    # Pokud je datum uvnitř vnořených objektů,
    # projdeme strukturu rekurzivně.
    nalezene = []

    def projdi(obj):

        if isinstance(obj, dict):

            for klic, hodnota in obj.items():

                klic_text = bez_diakritiky(
                    str(klic)
                )

                if (
                    "vyves" in klic_text
                    or "zverej" in klic_text
                ):

                    if hodnota:
                        nalezene.append(hodnota)

                projdi(hodnota)

        elif isinstance(obj, list):

            for hodnota in obj:
                projdi(hodnota)

    projdi(item)

    if nalezene:
        return nalezene[0]

    return None


def je_aktualni_datum(datum):
    """
    Vrátí True pouze pro záznamy zveřejněné
    v posledních MAX_STARI_DNI dnech.

    Neznámé nebo chybné datum = False.
    """

    if not datum:
        return False

    datum_text = str(datum).strip()

    if datum_text in ("?", "", "None", "null"):
        return False

    # Najdeme datum YYYY-MM-DD kdekoliv v textu
    match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        datum_text
    )

    if not match:
        return False

    try:

        datum_obj = datetime.strptime(
            match.group(1),
            "%Y-%m-%d"
        ).date()

    except ValueError:
        return False

    dnes = date.today()

    hranice = dnes - timedelta(
        days=MAX_STARI_DNI
    )

    return hranice <= datum_obj <= dnes


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


def je_aktualni_nabidka_pozemku(item):
    """
    Vrátí True pouze tehdy, když jde velmi pravděpodobně
    o aktuální nabídku/prodej pozemku.

    Nechceme:
    - stavební rozhodnutí
    - běžné veřejné vyhlášky
    - stavební řízení
    - pronájmy
    - pacht
    - výpůjčky
    - darování
    """

    if not isinstance(item, dict):
        return False

    # ----------------------------------------
    # Název dokumentu
    # ----------------------------------------

    nazev = (
        (item.get("název") or {})
        .get("cs")
        or ""
    )

    text = bez_diakritiky(nazev)

    # ----------------------------------------
    # Musí se týkat pozemku
    # ----------------------------------------

    pozemek = any(
        slovo in text
        for slovo in (
            "pozemek",
            "pozemku",
            "pozemky",
            "parcela",
            "parcely",
            "parcelni",
            "p. c.",
            "ppc",
        )
    )

    if not pozemek:
        return False

    # ----------------------------------------
    # Nechtěné typy dokumentů
    # ----------------------------------------

    nechtene = (
        "rozhodnuti",
        "stavebni povoleni",
        "uzemni rozhodnuti",
        "uzemni rizeni",
        "stavebni rizeni",
        "opatreni obecne povahy",
        "verejna vyhlaska",
    )

    if any(
        slovo in text
        for slovo in nechtene
    ):
        return False

    # ----------------------------------------
    # Nechceme nájem / pacht / výpůjčku / darování
    # ----------------------------------------

    nechtene_obchody = (
        "pronajem",
        "pronajmu",
        "najem",
        "najmu",
        "pacht",
        "vypujcka",
        "vypujcit",
        "vyprosa",
        "darovani",
        "darovat",
    )

    if any(
        slovo in text
        for slovo in nechtene_obchody
    ):
        return False

    # ----------------------------------------
    # Prodej
    # ----------------------------------------

    prodej = any(
        slovo in text
        for slovo in (
            "prodej",
            "prodeje",
            "prodeji",
            "prodat",
            "prodejem",
            "zamer prodeje",
            "zamer prodat",
            "zamer prodeje pozemku",
            "zamer prodat pozemek",
        )
    )

    # ----------------------------------------
    # Výběrové řízení
    # ----------------------------------------

    vyberove = (
        "vyberove rizeni" in text
        or "vyberoveho rizeni" in text
        or "vyberove rizeni na pozemek" in text
    )

    # ----------------------------------------
    # Aukce / dražba
    # ----------------------------------------

    aukce = any(
        slovo in text
        for slovo in (
            "aukce",
            "aukcni",
            "drazba",
            "drazebni",
        )
    )

    # ----------------------------------------
    # Musí být skutečný obchodní záměr
    # ----------------------------------------

    if prodej:
        return True

    if pozemek and vyberove:
        return True

    if pozemek and aukce:
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

            # --------------------------------
            # Datum vyvěšení
            # --------------------------------

            datum = ziskej_datum_vyveseni(item)

            # --------------------------------
            # Pouze aktuální dokumenty
            # --------------------------------

            if not je_aktualni_datum(datum):
                continue

            # --------------------------------
            # Pouze nabídky pozemků
            # --------------------------------

            if not je_aktualni_nabidka_pozemku(item):
                continue

            # --------------------------------
            # Název
            # --------------------------------

            nazev = (
                (
                    item.get("název")
                    or {}
                ).get("cs")
                or "Bez názvu"
            )

            # --------------------------------
            # URL
            # --------------------------------

            url = (
                item.get("url")
                or ""
            )

            # --------------------------------
            # Uložení nalezené nabídky
            # --------------------------------

            nalezene.append(
                {
                    "nazev": nazev,
                    "url": url,
                    "datum": datum,
                    "obce": [
                        obec["nazev"]
                        for obec in kandidat["obce"]
                    ],
                    "publisher": publisher,
                }
            )

    # ------------------------------------
    # 4b. Odstranění duplicit
    # ------------------------------------

    unikatni = []
    videne = set()

    for item in nalezene:

        klic = (
            item["url"]
            or (
                item["obce"][0]
                + "|"
                + item["nazev"]
                + "|"
                + str(item["datum"])
            )
        )

        if klic in videne:
            continue

        videne.add(klic)
        unikatni.append(item)

    nalezene = unikatni

    print()
    print(
        f"Aktuálních nabídek pozemků: "
        f"{len(nalezene)}"
    )

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
