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

def je_aktualni_datum(datum):
    """
    Vrátí True, pokud je datum vyvěšení
    v posledních MAX_STARI_DNI dnech.

    Očekáváme datum ve formátu:
        2026-08-10
    """

    if not datum or datum == "?":
        return False

    try:
        # JSON-LD může obsahovat i čas:
        # 2026-08-10T12:34:56
        datum_text = str(datum).strip()

        datum_text = datum_text[:10]

        datum_obj = datetime.strptime(
            datum_text,
            "%Y-%m-%d"
        ).date()

    except (ValueError, TypeError):
        return False

    dnes = date.today()

    hranice = (
        dnes
        - timedelta(days=MAX_STARI_DNI)
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


def je_podezrely_prodej(item):
    """
    Rozpozná nabídky týkající se pozemků
    a současně vyřadí zjevně nerelevantní dokumenty.
    """

    text = text_informace(item)

    # ----------------------------------------
    # POZEMEK / PARCELA
    # ----------------------------------------

    pozemek = any(
        x in text
        for x in (
            "pozemek",
            "pozemku",
            "pozemky",
            "pozemcich",
            "parcela",
            "parcely",
            "parcelni",
            "parcelní",
            "parc. c",
            "p. c.",
        )
    )

    if not pozemek:
        return False

    # ----------------------------------------
    # PRODEJ
    # ----------------------------------------

    prodej = any(
        x in text
        for x in (
            "prodej",
            "prodeje",
            "prodeji",
            "prodat",
            "prodej pozemku",
            "prodeji pozemku",
            "zamer prodeje",
            "zamer prodat",
        )
    )

    # ----------------------------------------
    # ZÁMĚR
    # ----------------------------------------

    zamer = (
        "zamer" in text
        or "záměr" in text
    )

    # ----------------------------------------
    # VÝBĚROVÉ ŘÍZENÍ
    # ----------------------------------------

    vyberove_rizeni = (
        "vyberove rizeni" in text
        or "vyberoveho rizeni" in text
        or "vyberove" in text
    )

    # ----------------------------------------
    # DAROVÁNÍ / VÝPŮJČKA / NÁJEM
    #
    # Tyto věci zatím nechceme.
    # ----------------------------------------

    nechtene = (
        "darovani",
        "darovat",
        "vypujcka",
        "vypujcit",
        "pronajem",
        "pronajmu",
        "najmu",
        "pacht",
        "vyprosa",
    )

    if any(
        slovo in text
        for slovo in nechtene
    ):
        return False

    # ----------------------------------------
    # HLAVNÍ PODMÍNKY
    # ----------------------------------------

    if pozemek and prodej:
        return True

    if pozemek and zamer:
        return True

    if pozemek and vyberove_rizeni:
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

            datum = (
                (
                    item.get("vyvěšení")
                    or {}
                ).get("datum")
                or "?"
            )

            # --------------------------------
            # Pouze aktuální dokumenty
            # --------------------------------

            if not je_aktualni_datum(
                datum
            ):
                continue

            # --------------------------------
            # Pouze nabídky pozemků
            # --------------------------------

            if not je_podezrely_prodej(
                item
            ):
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
                + item["datum"]
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
