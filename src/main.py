import csv
import io
import json
import re
import unicodedata
import zipfile
from datetime import date, datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


NKOD_GRAPHQL = "https://data.gov.cz/graphql"
RUIAN_URL = "https://services.cuzk.cz/sestavy/cis/UI_OBEC.zip"
UREDNI_DESKY_OFN = "https://ofn.gov.cz/úřední-desky/2021-07-20/"

PARDUBICKY_OKRESY = {
    "3603",  # Chrudim
    "3606",  # Pardubice
    "3609",  # Svitavy
    "3611",  # Ústí nad Orlicí
}

# Vždy se počítá od dnešního data.
# Skript tedy není potřeba po roce upravovat.
MAX_STARI_DNI = 30

USER_AGENT = (
    "UredniDeskaHlidac/1.0 "
    "(monitoring verejnych urednich desek)"
)


def bez_diakritiky(text):
    text = text or ""
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(
        znak for znak in text if not unicodedata.combining(znak)
    ).lower().strip()


def vytvor_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
    )

    return session


def nacti_obce(session):
    print("Stahuji obce Pardubického kraje...")
    print()

    response = session.get(RUIAN_URL, timeout=60)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        csv_soubory = [
            x for x in archive.namelist()
            if x.lower().endswith(".csv")
        ]

        if not csv_soubory:
            raise RuntimeError(
                "V RÚIAN ZIP nebyl nalezen CSV soubor."
            )

        raw = archive.read(csv_soubory[0])

    text = None

    for encoding in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        raise RuntimeError("CSV se nepodařilo dekódovat.")

    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    obce = []

    for row in reader:
        kod = (row.get("KOD") or "").strip()
        nazev = (row.get("NAZEV") or "").strip()
        okres = (row.get("OKRES_KOD") or "").strip()
        plati_do = (row.get("PLATI_DO") or "").strip()

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
                "normalizovany_nazev": bez_diakritiky(nazev),
            }
        )

    print(f"Nalezeno obcí: {len(obce)}")
    return obce


def nacti_uredni_desky(session):
    print()
    print("Stahuji metadata úředních desek z NKOD...")

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

    response = session.post(
        NKOD_GRAPHQL,
        json={"query": query},
        timeout=60,
    )
    response.raise_for_status()

    result = response.json()

    if "errors" in result:
        print(
            json.dumps(
                result["errors"],
                indent=2,
                ensure_ascii=False,
            )
        )
        raise RuntimeError("NKOD GraphQL vrátil chybu.")

    datasets = result["data"]["datasets"]

    print(
        f"NKOD úředních desek: "
        f"{datasets['pagination']['totalCount']}"
    )

    return datasets["data"]


def najdi_kandidaty(obce, desky):
    """
    Přiřadí úřední desku ke konkrétní obci.
    Používá přesnější shodu názvu, nikoli obecný substring.
    """

    kandidati = []

    def cisti_text(text):
        text = bez_diakritiky(text)
        text = text.replace("–", "-").replace("—", "-")
        return " ".join(text.split()).strip()

    def odpovida_obci(text, obec):
        text = cisti_text(text)
        nazev = cisti_text(obec["nazev"])

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

        if any(text.startswith(zakaz) for zakaz in zakazane):
            return False

        mozne_tvary = (
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
        )

        if text in mozne_tvary:
            return True

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
                zbytek = text[len(prefix):].strip()
                if zbytek == nazev:
                    return True

        for prefix in (
            "obec ",
            "mesto ",
            "mestys ",
            "statutarni mesto ",
        ):
            if text.startswith(prefix):
                zbytek = text[len(prefix):].strip()
                return zbytek == nazev

        return False

    for deska in desky:
        title = (
            (deska.get("title") or {}).get("cs")
            or ""
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or ""
        )

        nalezena_obec = None

        for obec in obce:
            if odpovida_obci(publisher, obec):
                nalezena_obec = obec
                break

        if nalezena_obec is None:
            for obec in obce:
                if odpovida_obci(title, obec):
                    nalezena_obec = obec
                    break

        if nalezena_obec is None:
            continue

        kandidati.append(
            {
                "deska": deska,
                "obce": [nalezena_obec],
            }
        )

    return kandidati


def stahni_jsonld(session, deska):
    for distribution in deska.get("distribution") or []:
        url = distribution.get("accessURL") or ""
        format_data = distribution.get("format") or ""

        if not url:
            continue

        if (
            "json" not in format_data.lower()
            and not url.lower().endswith((".json", ".jsonld"))
        ):
            continue

        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()

            try:
                return response.json()
            except ValueError:
                print("      Server vrátil neplatný JSON.")
                print(
                    f"      Content-Type: "
                    f"{response.headers.get('Content-Type', '')}"
                )
                print(f"      HTTP: {response.status_code}")
                continue

        except requests.exceptions.SSLError as error:
            print("      SSL chyba.")
            print("      Zkouším připojení bez ověření certifikátu...")

            try:
                response = session.get(
                    url,
                    timeout=30,
                    verify=False,
                )
                response.raise_for_status()

                try:
                    return response.json()
                except ValueError:
                    print("      Ani druhý pokus nevrátil JSON.")
                    continue

            except Exception as druhy_error:
                print(f"      Druhý pokus selhal: {druhy_error}")
                continue

        except requests.exceptions.RequestException as error:
            print(f"      Chyba připojení: {error}")
            continue

        except Exception as error:
            print(f"      Neočekávaná chyba: {error}")
            continue

    return None


def ziskej_informace(data):
    if not isinstance(data, dict):
        return []

    informace = data.get("informace") or []

    if isinstance(informace, dict):
        informace = [informace]

    return informace


def ziskej_datum_vyveseni(item):
    if not isinstance(item, dict):
        return None

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

    nalezene = []

    def projdi(obj):
        if isinstance(obj, dict):
            for klic, hodnota in obj.items():
                klic_text = bez_diakritiky(str(klic))

                if "vyves" in klic_text or "zverej" in klic_text:
                    if hodnota:
                        nalezene.append(hodnota)

                projdi(hodnota)

        elif isinstance(obj, list):
            for hodnota in obj:
                projdi(hodnota)

    projdi(item)

    return nalezene[0] if nalezene else None


def je_aktualni_datum(datum):
    """
    True pouze pro datum v intervalu:
    dnes - MAX_STARI_DNI až dnes.

    Datum je vždy počítáno dynamicky při každém spuštění.
    """

    if not datum:
        return False

    datum_text = str(datum).strip()

    if datum_text in ("?", "", "None", "null"):
        return False

    match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        datum_text,
    )

    if not match:
        return False

    try:
        datum_obj = datetime.strptime(
            match.group(1),
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return False

    dnes = date.today()
    hranice = dnes - timedelta(days=MAX_STARI_DNI)

    return hranice <= datum_obj <= dnes


def je_aktualni_nabidka_pozemku(item):
    """
    Vrátí True pro pravděpodobné aktuální nabídky/prodeje pozemků.

    Zahrnuje:
    - prodej
    - výběrové řízení
    - aukci / dražbu

    Vylučuje:
    - stavební a územní řízení
    - veřejné vyhlášky
    - nájem / pacht
    - výpůjčku
    - darování
    """

    if not isinstance(item, dict):
        return False

    nazev = (
        (item.get("název") or {}).get("cs")
        or ""
    )

    text = bez_diakritiky(nazev)

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

    nechtene = (
        "rozhodnuti",
        "stavebni povoleni",
        "uzemni rozhodnuti",
        "uzemni rizeni",
        "stavebni rizeni",
        "opatreni obecne povahy",
        "verejna vyhlaska",
    )

    if any(slovo in text for slovo in nechtene):
        return False

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

    if any(slovo in text for slovo in nechtene_obchody):
        return False

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
        )
    )

    vyberove = any(
        slovo in text
        for slovo in (
            "vyberove rizeni",
            "vyberoveho rizeni",
        )
    )

    aukce = any(
        slovo in text
        for slovo in (
            "aukce",
            "aukcni",
            "drazba",
            "drazebni",
        )
    )

    return prodej or vyberove or aukce


def hlavni():
    print("======================================")
    print(" ÚŘEDNÍ DESKA HLÍDAČ")
    print(" PARDUBICKÝ KRAJ")
    print("======================================")
    print()

    session = vytvor_session()

    obce = nacti_obce(session)
    desky = nacti_uredni_desky(session)

    print()
    print("Hledám možné úřední desky obcí Pardubického kraje...")

    kandidati = najdi_kandidaty(obce, desky)

    print()
    print(f"Kandidátních úředních desek: {len(kandidati)}")
    print()

    for kandidat in kandidati:
        deska = kandidat["deska"]

        title = (
            (deska.get("title") or {}).get("cs")
            or "Bez názvu"
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznámý"
        )

        obce_text = ", ".join(
            obec["nazev"] for obec in kandidat["obce"]
        )

        print(f"✓ {publisher}")
        print(f"  Dataset: {title}")
        print(f"  Pravděpodobná obec: {obce_text}")

    print()
    print("======================================")
    print(" STAHUJI VYBRANÉ ÚŘEDNÍ DESKY")
    print("======================================")
    print()

    nalezene = []

    for cislo, kandidat in enumerate(kandidati, start=1):
        deska = kandidat["deska"]

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznámý"
        )

        print(f"[{cislo}/{len(kandidati)}] {publisher}")

        data = stahni_jsonld(session, deska)

        if data is None:
            print("   ✗ nepodařilo se stáhnout")
            continue

        informace = ziskej_informace(data)
        print(f"   {len(informace)} informací")

        for item in informace:
            datum = ziskej_datum_vyveseni(item)

            if not je_aktualni_datum(datum):
                continue

            if not je_aktualni_nabidka_pozemku(item):
                continue

            nazev = (
                (item.get("název") or {}).get("cs")
                or "Bez názvu"
            )

            url = item.get("url") or ""

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

    unikatni = []
    videne = set()

    for item in nalezene:
        klic = item["url"]

        if not klic:
            klic = (
                item["obce"][0]
                + "|"
                + item["nazev"]
                + "|"
                + str(item["datum"])
            )

        if klic in videne:
            continue

        videne.add(klic)
        unikatni.append(item)

    nalezene = unikatni

    # Nejnovější nabídky zobrazíme první.
    nalezene.sort(
        key=lambda item: str(item["datum"]),
        reverse=True,
    )

    print()
    print(f"Aktuálních nabídek pozemků: {len(nalezene)}")
    print()
    print("======================================")
    print(" NALEZENÉ KANDIDÁTNÍ NABÍDKY")
    print("======================================")
    print()
    print(f"Celkem: {len(nalezene)}")
    print()

    for cislo, item in enumerate(nalezene[:100], start=1):
        print(f"{cislo}. {item['nazev']}")
        print(f"   Obec: {', '.join(item['obce'])}")
        print(f"   Poskytovatel: {item['publisher']}")
        print(f"   Vyvěšení: {item['datum']}")

        if item["url"]:
            print(f"   URL: {item['url']}")

        print()

    print("======================================")
    print(" KONEC")
    print("======================================")


if __name__ == "__main__":
    hlavni()
